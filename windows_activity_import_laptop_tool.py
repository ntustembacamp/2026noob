import csv
import json
import html as html_lib
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import urllib.parse
import urllib.request
import urllib.error
import zipfile
import io
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

if sys.stdout is None:
    sys.stdout = io.TextIOWrapper(open(os.devnull, "wb"), encoding="utf-8", write_through=True)
if sys.stderr is None:
    sys.stderr = io.TextIOWrapper(open(os.devnull, "wb"), encoding="utf-8", write_through=True)

try:
    from PIL import Image, ExifTags  # type: ignore
except Exception:  # pragma: no cover
    Image = None
    ExifTags = None

try:
    from pillow_heif import register_heif_opener  # type: ignore
except Exception:  # pragma: no cover
    register_heif_opener = None

try:
    import cv2  # type: ignore
    import numpy as np  # type: ignore
except Exception:  # pragma: no cover
    cv2 = None
    np = None

torch = None
pyiqa = None


def _runtime_base_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return Path(__file__).resolve().parent


def _tool_root_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent / "dist" / "laptop_tool"


RUNTIME_BASE = _runtime_base_path()
for _p in [
    str(RUNTIME_BASE),
    str(RUNTIME_BASE / "tools"),
    str(RUNTIME_BASE / "service"),
    str(RUNTIME_BASE / "service" / "tools"),
]:
    if _p and _p not in sys.path:
        sys.path.insert(0, _p)

# tools.new_face 仍依賴 CONFIG_PATH 尋找 config.py，便攜版統一指向 runtime 目錄。
if not os.getenv("CONFIG_PATH"):
    os.environ["CONFIG_PATH"] = str(RUNTIME_BASE)


APP_TITLE = "AI人臉辨識系統工具程式（筆電）"
BASE_DIR = Path(r"C:\AIFR_Laptop")
LOG_DIR = BASE_DIR / "logs"
TOOL_ROOT = _tool_root_path()
DEFAULT_CONFIG_PATH = TOOL_ROOT / "configs" / "activity_normalize_config.json"
DEVICE_CFG_PATH = BASE_DIR / "device.json"
DEFAULT_MODEL_DIR = BASE_DIR / "models"
MODEL_DOWNLOAD_DIR = DEFAULT_MODEL_DIR / "downloads"
MODEL_READY_META = DEFAULT_MODEL_DIR / "model_ready.json"
EMBEDDING_NAME = "faces_embedding_antelopev2.pkl"

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".heic"}
FORBIDDEN_ROOTS = (r"c:\activity", r"c:\uploadsource")
NORMALIZE_MODE_OPTIONS = [("模式 A（EXIF）", "exif"), ("模式 B（活動編號）", "schedule")]
RUN_MODE_OPTIONS = [("Server 模式", "server"), ("Local 模式", "local")]
LAPTOP_UPLOAD_MAX_IN_FLIGHT = 3


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _today_text() -> str:
    return datetime.now().strftime("%Y%m%d")


def _safe_name(text: str) -> str:
    value = str(text or "").strip()
    if not value:
        return "NA"
    for ch in '<>:"/\\|?*':
        value = value.replace(ch, "_")
    return value.replace(" ", "_")


def _to_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


def _extract_photographer_name(value: str) -> str:
    text = str(value or "").strip()
    if "｜" in text:
        return text.split("｜", 1)[0].strip()
    return text


def _json_request(url: str, timeout: int = 30) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        body = response.read().decode("utf-8", "replace")
    return json.loads(body)


def _post_form_json(url: str, payload: dict, timeout: int = 30) -> dict:
    encoded = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=encoded,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        body = response.read().decode("utf-8", "replace")
    return json.loads(body)


def _post_json(url: str, payload: dict, timeout: int = 30) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        body = response.read().decode("utf-8", "replace")
    return json.loads(body)


def _extract_exif_dt(path: Path):
    if Image is None:
        return None
    try:
        with Path(path).open("rb") as fp:
            with Image.open(fp) as img:
                exif = getattr(img, "_getexif", lambda: None)()
                if not exif:
                    return None
                exif_map = {}
                for key, value in exif.items():
                    exif_map[str(ExifTags.TAGS.get(key, key))] = value
                raw = exif_map.get("DateTimeOriginal") or exif_map.get("DateTime")
                if not raw:
                    return None
                return datetime.strptime(str(raw).strip(), "%Y:%m:%d %H:%M:%S")
    except Exception:
        return None


def _save_normalized_jpeg(source_path: Path, target_path: Path):
    if Image is None:
        raise RuntimeError("PIL 不可用，無法正規化輸出 JPEG。")
    if source_path.suffix.lower() == ".heic" and register_heif_opener is not None:
        register_heif_opener()
    with Path(source_path).open("rb") as fp:
        with Image.open(fp) as img:
            exif_data = getattr(img, "getexif", lambda: None)()
            rgb = img.convert("RGB")
            save_kwargs = {"format": "JPEG"}
            if exif_data:
                save_kwargs["exif"] = exif_data.tobytes()
            rgb.save(target_path, **save_kwargs)

def _load_image_for_local(path: Path):
    """Return (image, source, error_code).

    參考 activity-photo-import-ui 的讀圖流程。
    PIL -> RGB -> ndarray -> BGR -> contiguous。
    若 PIL 失敗，仍保留 cv2 fallback 診斷。
    """
    if np is None:
        return None, "NONE", "LOCAL_IMAGE_INVALID"

    pil_error = ""
    path = Path(path)
    path_str = os.fspath(path)

    # 1) import-ui 路徑：先用 PIL 讀取，轉 RGB 再轉 ndarray / BGR
    if Image is not None:
        try:
            if path.suffix.lower() == ".heic" and register_heif_opener is not None:
                register_heif_opener()
            with path.open("rb") as fp:
                with Image.open(fp) as pil_img:
                    rgb = pil_img.convert("RGB")
                    arr = np.array(rgb)
            if arr.ndim == 2:
                arr = np.stack([arr, arr, arr], axis=2)
            if arr.ndim != 3 or arr.shape[2] != 3:
                return arr, "PIL", "LOCAL_IMAGE_INVALID"
            if cv2 is not None:
                image = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
            else:
                image = arr[:, :, ::-1]
            return np.ascontiguousarray(image), "PIL", ""
        except Exception as exc:
            pil_error = f"{type(exc).__name__}: {exc}"

    # 2) cv2 fallback（保留診斷）
    if cv2 is None:
        return None, "NONE", "LOCAL_IMAGE_DECODE_FAIL"

    image = None
    try:
        image = cv2.imdecode(np.fromfile(path_str, dtype=np.uint8), 1)
    except Exception:
        image = None

    if image is None:
        if pil_error:
            return None, f"cv2|pil={pil_error}", "LOCAL_IMAGE_DECODE_FAIL"
        return None, "cv2", "LOCAL_IMAGE_DECODE_FAIL"

    if len(image.shape) == 2:
        image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    if len(image.shape) != 3 or image.shape[2] != 3:
        return image, "cv2", "LOCAL_IMAGE_INVALID"

    source = "cv2"
    if pil_error:
        source = f"cv2|pil={pil_error}"
    return np.ascontiguousarray(image), source, ""




def _parse_time_text(value: str):
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(text, fmt).time()
        except Exception:
            pass
    return None


def _validate_model_layout(model_base: Path):
    """
    model_base 應為 C:\\AIFR_Laptop\\models
    需存在 C:\\AIFR_Laptop\\models\\antelopev2 且含 onnx 檔。
    """
    model_dir = model_base / "antelopev2"
    if not model_dir.exists():
        return False, f"LOCAL_MODEL_LAYOUT_INVALID: 找不到模型目錄 {model_dir}", []
    onnx_files = sorted(model_dir.glob("*.onnx"))
    if len(onnx_files) < 1:
        return False, f"LOCAL_MODEL_LAYOUT_INVALID: {model_dir} 內找不到 .onnx 檔", []
    preview = [str(p) for p in onnx_files[:8]]
    return True, f"模型預檢通過：{model_dir}（onnx={len(onnx_files)}）", preview


def _load_face_recognition_class():
    try:
        from service.tools.new_face_laptop import FaceRecognition  # type: ignore
        return FaceRecognition
    except Exception:
        pass
    try:
        from tools.new_face_laptop import FaceRecognition  # type: ignore
        return FaceRecognition
    except Exception:
        pass
    alt = RUNTIME_BASE / "tools" / "new_face_laptop.py"
    if alt.exists():
        import importlib.util

        spec = importlib.util.spec_from_file_location("tools.new_face_laptop", str(alt))
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            cls = getattr(mod, "FaceRecognition", None)
            if cls is not None:
                return cls
    raise RuntimeError(
        "載入 FaceRecognition 失敗：找不到 tools.new_face。請確認使用完整工具程式 ZIP，勿單獨執行 EXE。"
    )


class LaptopTool:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1280x860")

        self.status = tk.StringVar(value="請先設定 Server API，並下載或載入設定檔。")
        self.source_hint = tk.StringVar(value="目前資料來源：未知")
        self.server_api = tk.StringVar(value="")
        self.device_id = tk.StringVar(value="")
        self.photographer = tk.StringVar(value="")
        self.photographer_choice = tk.StringVar(value="")
        self.schedule_display = tk.StringVar(value="")
        self.schedule_code = tk.StringVar(value="")
        self.normalize_mode_text = tk.StringVar(value=NORMALIZE_MODE_OPTIONS[0][0])
        self.run_mode_text = tk.StringVar(value=RUN_MODE_OPTIONS[1][0])
        self.enable_pyiqa = tk.BooleanVar(value=False)

        self.incoming_dir = tk.StringVar(value=str(BASE_DIR / "incoming"))
        self.step23_source_dir = tk.StringVar(value=str(BASE_DIR / "normalized_success"))
        self.step23_manifest_path = tk.StringVar(value="")
        self.step23_manifest_source = tk.StringVar(value="")
        self.job_id = tk.StringVar(value="")
        self.recent_job_choice = tk.StringVar(value="")
        self.log_type = tk.StringVar(value="區塊2正規化")
        self.log_file_choice = tk.StringVar(value="")
        self.log_offset = 0
        self.active_section = "section1"
        self.current_normalize_job_id = None
        self.current_step23_job_id = None
        self.upload_progress = tk.DoubleVar(value=0.0)
        self.upload_progress_text = tk.StringVar(value="上傳進度：0% (0/0)")

        self.models_map = {}
        self.recent_job_map = {}
        self.photographer_options = []
        self.photographer_display_map = {}
        self.schedule_map = {}
        self.log_file_map = {}
        self._local_state = {}
        self._startup_log_buffer = []

        self._prepare_dirs()
        self._build_ui()
        self._flush_startup_logs()
        self._load_local_state()
        self._load_config_default()
        self._ensure_config_seeded()

    def _build_ui(self):
        wrapper = ttk.Frame(self.root, padding=10)
        wrapper.pack(fill=tk.BOTH, expand=True)

        paned = ttk.Panedwindow(wrapper, orient=tk.VERTICAL)
        paned.pack(fill=tk.BOTH, expand=True)

        top = ttk.Frame(paned)
        bottom = ttk.Frame(paned)
        paned.add(top, weight=3)
        paned.add(bottom, weight=2)

        # 區塊1
        sec1 = ttk.LabelFrame(top, text="區塊1：連線與設定、模型設定")
        sec1.pack(fill=tk.X, pady=(0, 8))

        row11 = ttk.Frame(sec1)
        row11.pack(fill=tk.X, padx=8, pady=6)
        row11.columnconfigure(1, weight=1)
        ttk.Label(row11, text="Server API").grid(row=0, column=0, sticky="w")
        ttk.Entry(row11, textvariable=self.server_api).grid(row=0, column=1, sticky="ew", padx=(8, 8))
        ttk.Label(row11, textvariable=self.source_hint, foreground="#0b7285").grid(row=0, column=2, sticky="w", padx=(0, 8))
        ttk.Button(row11, text="下載設定檔", command=self.download_config, width=12).grid(row=0, column=3, padx=(0, 8))
        ttk.Button(row11, text="載入設定檔", command=self.load_config_file, width=12).grid(row=0, column=4)

        row12 = ttk.Frame(sec1)
        row12.pack(fill=tk.X, padx=8, pady=(0, 6))
        ttk.Label(row12, text="筆電編號（device_id）").grid(row=0, column=0, sticky="w")
        did_entry = ttk.Entry(row12, textvariable=self.device_id, width=10)
        did_entry.grid(row=0, column=1, sticky="w", padx=(8, 12))
        did_entry.bind("<FocusOut>", lambda _e: self._save_local_state())
        ttk.Label(row12, text="攝影師（必填）").grid(row=0, column=2, sticky="w")
        photographer_entry = ttk.Entry(row12, textvariable=self.photographer, width=10)
        photographer_entry.grid(row=0, column=3, sticky="w", padx=(8, 8))
        photographer_entry.bind("<FocusOut>", lambda _e: self._save_local_state())
        self.photographer_combo = ttk.Combobox(row12, textvariable=self.photographer_choice, state="readonly", width=22)
        self.photographer_combo.grid(row=0, column=4, sticky="w", padx=(0, 12))
        self.photographer_combo.bind("<<ComboboxSelected>>", self._on_photographer_selected)
        ttk.Label(row12, text="選擇模型").grid(row=0, column=5, sticky="e", padx=(18, 8))
        self.model_choice = tk.StringVar(value="")
        self.model_combo = ttk.Combobox(row12, textvariable=self.model_choice, state="readonly", width=24)
        self.model_combo.grid(row=0, column=6, sticky="w", padx=(0, 8))
        ttk.Button(row12, text="載入模型清單", command=self.load_model_manifest, width=12).grid(row=0, column=7, padx=(18, 8))
        ttk.Button(row12, text="下載模型檔", command=self.download_model, width=12).grid(row=0, column=8)

        # 區塊2
        sec2 = ttk.LabelFrame(top, text="區塊2：步驟1 活動照片正規化")
        sec2.pack(fill=tk.X, pady=(0, 8))

        row21 = ttk.Frame(sec2)
        row21.pack(fill=tk.X, padx=8, pady=6)
        row21.columnconfigure(1, weight=1)
        row21.columnconfigure(3, weight=1)
        ttk.Label(row21, text="正規化模式").grid(row=0, column=0, sticky="w")
        self.normalize_mode_combo = ttk.Combobox(
            row21, textvariable=self.normalize_mode_text, values=[x[0] for x in NORMALIZE_MODE_OPTIONS], state="readonly"
        )
        self.normalize_mode_combo.grid(row=0, column=1, sticky="ew", padx=(8, 18))
        self.normalize_mode_combo.bind("<<ComboboxSelected>>", self._on_normalize_mode_changed)
        ttk.Label(row21, text="活動編號（模式B）").grid(row=0, column=2, sticky="w")
        self.schedule_combo = ttk.Combobox(row21, textvariable=self.schedule_display, state="readonly")
        self.schedule_combo.grid(row=0, column=3, sticky="ew", padx=(8, 0))
        self.schedule_combo.bind("<<ComboboxSelected>>", self._on_schedule_selected)

        row22 = ttk.Frame(sec2)
        row22.pack(fill=tk.X, padx=8, pady=(0, 6))
        row22.columnconfigure(1, weight=1)
        ttk.Label(row22, text="來源資料夾").grid(row=0, column=0, sticky="w")
        ttk.Entry(row22, textvariable=self.incoming_dir).grid(row=0, column=1, sticky="ew", padx=(8, 8))
        ttk.Button(row22, text="選擇資料夾", command=lambda: self._pick_dir(self.incoming_dir), width=12).grid(row=0, column=2, padx=(0, 8))
        ttk.Button(row22, text="執行圖檔正規化", command=self.run_normalize, width=14).grid(row=0, column=3)

        # 區塊3
        sec3 = ttk.LabelFrame(top, text="區塊3：步驟2+3 活動照片匯入入庫（img_upload）與後續辨識（reco_result）")
        sec3.pack(fill=tk.X, pady=(0, 8))

        row31 = ttk.Frame(sec3)
        row31.pack(fill=tk.X, padx=8, pady=6)
        ttk.Label(row31, text="執行模式").grid(row=0, column=0, sticky="w")
        self.run_mode_combo = ttk.Combobox(row31, textvariable=self.run_mode_text, values=[x[0] for x in RUN_MODE_OPTIONS], state="readonly", width=18)
        self.run_mode_combo.grid(row=0, column=1, sticky="w", padx=(8, 0))
        self.run_mode_combo.bind("<<ComboboxSelected>>", self._on_run_mode_selected)

        row32_source = ttk.Frame(sec3)
        row32_source.pack(fill=tk.X, padx=8, pady=5)
        row32_source.columnconfigure(1, weight=1)
        ttk.Label(row32_source, text="設定來源資料夾").grid(row=0, column=0, sticky="w")
        ttk.Entry(row32_source, textvariable=self.step23_source_dir).grid(row=0, column=1, sticky="ew", padx=(8, 8))
        ttk.Button(row32_source, text="選擇資料夾", command=lambda: self._pick_dir(self.step23_source_dir), width=12).grid(row=0, column=2)

        row33_manifest = ttk.Frame(sec3)
        row33_manifest.pack(fill=tk.X, padx=8, pady=5)
        row33_manifest.columnconfigure(2, weight=1)
        ttk.Label(row33_manifest, text="設定批次設定檔（manifest）").grid(row=0, column=0, sticky="w")
        ttk.Label(row33_manifest, text="來源資料夾").grid(row=0, column=1, sticky="w", padx=(8, 4))
        ttk.Entry(row33_manifest, textvariable=self.step23_manifest_source, state="readonly").grid(row=0, column=2, sticky="ew", padx=(0, 8))
        ttk.Button(row33_manifest, text="帶入最新manifest", command=self.apply_latest_manifest, width=14).grid(row=0, column=3, padx=(0, 8))
        ttk.Button(row33_manifest, text="選擇設定檔", command=self.pick_manifest_file, width=12).grid(row=0, column=4)

        row34 = ttk.Frame(sec3)
        row34.pack(fill=tk.X, padx=8, pady=(0, 6))
        ttk.Label(row34, text="是否做影像品質評分").grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(row34, text="啟用 pyiqa_score", variable=self.enable_pyiqa).grid(row=0, column=1, sticky="w", padx=(8, 18))
        ttk.Button(row34, text="開始匯入活動照片", command=self.start_step23, width=16).grid(row=0, column=2, sticky="w")

        # 區塊4
        sec4 = ttk.LabelFrame(bottom, text="區塊4：Log")
        sec4.pack(fill=tk.BOTH, expand=True)

        row41 = ttk.Frame(sec4)
        row41.pack(fill=tk.X, padx=8, pady=6)
        row41.columnconfigure(1, weight=1)
        row41.columnconfigure(5, weight=1)
        ttk.Label(row41, text="最近任務").grid(row=0, column=0, sticky="w")
        self.recent_job_combo = ttk.Combobox(row41, textvariable=self.recent_job_choice, state="readonly")
        self.recent_job_combo.grid(row=0, column=1, sticky="ew", padx=(8, 8))
        ttk.Button(row41, text="更新清單", command=self.load_recent_jobs, width=10).grid(row=0, column=2, padx=(0, 8))
        ttk.Button(row41, text="套用最近任務", command=self.apply_recent_job, width=12).grid(row=0, column=3, padx=(0, 8))
        ttk.Label(row41, text="接續查看 Job").grid(row=0, column=4, sticky="e")
        ttk.Entry(row41, textvariable=self.job_id).grid(row=0, column=5, sticky="ew", padx=(8, 8))
        ttk.Button(row41, text="接續輪詢", command=self.attach_job, width=10).grid(row=0, column=6)

        row41.pack_forget()
        row42 = ttk.Frame(sec4)
        row42.pack(fill=tk.X, padx=8, pady=(0, 6))
        row42.columnconfigure(3, weight=1)
        ttk.Label(row42, text="Log 類型").grid(row=0, column=0, sticky="w")
        ttk.Combobox(
            row42,
            textvariable=self.log_type,
            values=["區塊1設定", "區塊2正規化", "區塊3入庫辨識", "任務log"],
            state="readonly",
            width=14,
        ).grid(row=0, column=1, sticky="w", padx=(8, 8))
        ttk.Button(row42, text="更新", command=self.refresh_log_files, width=10).grid(row=0, column=2, padx=(0, 8))
        self.log_file_combo = ttk.Combobox(row42, textvariable=self.log_file_choice, state="readonly")
        self.log_file_combo.grid(row=0, column=3, sticky="ew", padx=(0, 8))
        ttk.Button(row42, text="載入", command=self.load_selected_log, width=10).grid(row=0, column=4, padx=(0, 8))
        ttk.Button(row42, text="開啟", command=self.open_selected_log, width=10).grid(row=0, column=5, padx=(0, 8))
        ttk.Button(row42, text="複製摘要", command=self.copy_error_summary, width=10).grid(row=0, column=6)

        ttk.Label(sec4, textvariable=self.upload_progress_text).pack(fill=tk.X, padx=8, pady=(0, 2))
        ttk.Progressbar(sec4, variable=self.upload_progress, maximum=100.0).pack(fill=tk.X, padx=8, pady=(0, 6))
        ttk.Label(sec4, textvariable=self.status).pack(fill=tk.X, padx=8, pady=(0, 6))
        self.log_text = ScrolledText(sec4, height=10, wrap=tk.WORD)
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 6))

        self.root.update_idletasks()
        total_h = max(self.root.winfo_height(), 860)
        top_req = top.winfo_reqheight() + 10
        sash_y = min(max(top_req, 420), total_h - 300)
        self.root.after(80, lambda: paned.sashpos(0, sash_y))
        self.root.minsize(1450, 860)
        self._on_normalize_mode_changed()

    def _on_normalize_mode_changed(self, _evt=None):
        mode = self._normalize_mode_code()
        if mode == "schedule":
            self.schedule_combo.configure(state="readonly")
        else:
            self.schedule_display.set("")
            self.schedule_code.set("000")
            self.schedule_combo.configure(state="disabled")
        self._save_local_state()

    def _prepare_dirs(self):
        for p in [
            BASE_DIR, LOG_DIR, BASE_DIR / "incoming", BASE_DIR / "normalized_success", BASE_DIR / "normalized_complete",
            BASE_DIR / "normalized_fail", BASE_DIR / "reco_success", BASE_DIR / "reco_fail", BASE_DIR / "upload_queue",
            BASE_DIR / "embeddings",
            BASE_DIR / "_work" / "normalize", DEFAULT_MODEL_DIR, MODEL_DOWNLOAD_DIR
        ]:
            p.mkdir(parents=True, exist_ok=True)
        self._ensure_embedded_embedding()

    def _ensure_embedded_embedding(self):
        target = BASE_DIR / "embeddings" / EMBEDDING_NAME
        if target.exists():
            return
        candidates = [
            RUNTIME_BASE / "embeddings" / EMBEDDING_NAME,
            Path(__file__).resolve().parent / "service" / "embedding" / EMBEDDING_NAME,
            Path(__file__).resolve().parent / "embedding" / EMBEDDING_NAME,
        ]
        for src in candidates:
            if src.exists():
                try:
                    shutil.copy2(src, target)
                    self.append(f"[{_now_text()}] 已自動佈署 embedding：{target}")
                except Exception as exc:
                    self.append(f"[{_now_text()}] 自動佈署 embedding 失敗：{exc}")
                return

    def _write_default_config_file(self, data: dict):
        DEFAULT_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        DEFAULT_CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")

    def _refresh_config_from_server_if_needed(self, data: dict):
        if not isinstance(data, dict):
            return data
        activities = data.get("activities") or []
        photographers = data.get("photographers") or []
        if activities and photographers:
            return data
        api = str(data.get("server_api_base") or self.server_api.get() or "").strip().rstrip("/")
        if not api:
            return data
        if not (api.startswith("http://") or api.startswith("https://")):
            return data
        try:
            refreshed = _json_request(f"{api}/laptop-tool/config", timeout=30)
            if isinstance(refreshed, dict) and refreshed:
                self._write_default_config_file(refreshed)
                self._apply_config_data(refreshed, "伺服器設定檔")
                return refreshed
        except Exception as exc:
            self.append(f"[{_now_text()}] 自動更新設定檔失敗：{exc}")
        return data

    def _ensure_config_seeded(self):
        if not DEFAULT_CONFIG_PATH.exists():
            return
        try:
            data = json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8-sig"))
        except Exception:
            return
        if not isinstance(data, dict):
            return
        activities = data.get("activities") or []
        photographers = data.get("photographers") or []
        if activities and photographers:
            return
        refreshed = self._refresh_config_from_server_if_needed(data)
        if isinstance(refreshed, dict) and refreshed is not data:
            self._apply_config_data(refreshed, "伺服器設定檔")

    def _log_widget_ready(self) -> bool:
        widget = getattr(self, "log_text", None)
        if widget is None:
            return False
        try:
            return bool(widget.winfo_exists())
        except Exception:
            return False

    def _flush_startup_logs(self):
        if not self._startup_log_buffer or not self._log_widget_ready():
            return
        pending = list(self._startup_log_buffer)
        self._startup_log_buffer.clear()
        for line in pending:
            try:
                self.log_text.insert(tk.END, str(line) + "\n")
            except Exception:
                break
        try:
            self.log_text.see(tk.END)
        except Exception:
            pass

    def append(self, text: str):
        if threading.current_thread() is not threading.main_thread():
            self.root.after(0, lambda t=str(text): self.append(t))
            return
        line = str(text)
        tool_log = LOG_DIR / "laptop_activity_import_tool.log"
        with tool_log.open("a", encoding="utf-8-sig", newline="\n") as f:
            f.write(line + "\n")
        # 分流寫入：區塊主 log + 任務 log（相容保留舊主 log）
        section = self.active_section or "section1"
        today = _today_text()
        if section == "section1":
            self._write_log_file(LOG_DIR / f"tool_section1_{today}.log", line)
        elif section == "section2":
            self._write_log_file(LOG_DIR / f"tool_section2_normalize_{today}.log", line)
            if self.current_normalize_job_id:
                self._write_log_file(LOG_DIR / f"normalize_{self.current_normalize_job_id}.log", line)
        elif section == "section3":
            self._write_log_file(LOG_DIR / f"tool_section3_import_reco_{today}.log", line)
            if self.current_step23_job_id:
                self._write_log_file(LOG_DIR / f"step23_{self.current_step23_job_id}.log", line)
        if not self._log_widget_ready():
            self._startup_log_buffer.append(line)
            return
        self.log_text.insert(tk.END, line + "\n")
        self.log_text.see(tk.END)

    def _write_log_file(self, path: Path, text: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8-sig", newline="\n") as f:
            f.write(str(text) + "\n")

    def _append_with_route(self, text: str, section: str, job_id: str = ""):
        self.append(text)
        today = _today_text()
        section_path = None
        if section == "section1":
            section_path = LOG_DIR / f"tool_section1_{today}.log"
        elif section == "section2":
            section_path = LOG_DIR / f"tool_section2_normalize_{today}.log"
        elif section == "section3":
            section_path = LOG_DIR / f"tool_section3_import_reco_{today}.log"
        if section_path is not None:
            self._write_log_file(section_path, text)
        if job_id:
            if section == "section2":
                self._write_log_file(LOG_DIR / f"normalize_{job_id}.log", text)
            elif section == "section3":
                self._write_log_file(LOG_DIR / f"step23_{job_id}.log", text)

    def append_section1(self, text: str):
        self._append_with_route(text, "section1")

    def append_section2(self, text: str, job_id: str = ""):
        target_job = job_id or (self.current_normalize_job_id or "")
        self._append_with_route(text, "section2", target_job)

    def append_section3(self, text: str, job_id: str = ""):
        target_job = job_id or (self.current_step23_job_id or "")
        self._append_with_route(text, "section3", target_job)

    def _set_status_safe(self, text: str):
        if threading.current_thread() is threading.main_thread():
            self.status.set(text)
        else:
            self.root.after(0, lambda t=str(text): self.status.set(t))

    def _set_progress_safe(self, percent: float, text: str):
        if threading.current_thread() is threading.main_thread():
            self.upload_progress.set(float(percent))
            self.upload_progress_text.set(text)
        else:
            self.root.after(0, lambda p=float(percent), t=str(text): (self.upload_progress.set(p), self.upload_progress_text.set(t)))

    def _load_local_state(self):
        self._local_state = {}
        if DEVICE_CFG_PATH.exists():
            try:
                data = json.loads(DEVICE_CFG_PATH.read_text(encoding="utf-8-sig"))
                if isinstance(data, dict):
                    self._local_state = data
                self.device_id.set(str(data.get("device_id") or "").strip().upper())
                self.server_api.set(str(data.get("server_api_base") or "").strip())
                run_mode = str(data.get("default_run_mode") or "").strip()
                if run_mode:
                    self.run_mode_text.set(run_mode)
                normalize_mode = str(data.get("default_normalize_mode") or "").strip()
                if normalize_mode:
                    self.normalize_mode_text.set(normalize_mode)
                photographer = str(data.get("default_photographer") or "").strip()
                if photographer:
                    self.photographer.set(photographer)
                activity_code = str(data.get("default_activity_code") or "").strip().upper()
                if activity_code:
                    self.schedule_code.set(activity_code)
            except Exception:
                pass

    def _save_local_state(self):
        DEVICE_CFG_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "device_id": self.device_id.get().strip().upper(),
            "server_api_base": self.server_api.get().strip(),
            "default_activity_code": self.schedule_code.get().strip().upper(),
            "default_photographer": self.photographer.get().strip(),
            "default_normalize_mode": self.normalize_mode_text.get().strip(),
            "default_run_mode": self.run_mode_text.get().strip(),
            "updated_at": _now_text(),
        }
        DEVICE_CFG_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")

    def _load_config_default(self):
        if DEFAULT_CONFIG_PATH.exists():
            try:
                data = json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8-sig"))
                self._apply_config_data(data, "設定檔")
            except Exception as exc:
                self.append(f"[{_now_text()}] 讀取預設設定檔失敗：{exc}")
        else:
            self.source_hint.set("目前資料來源：本機快取")
        self._apply_saved_defaults()
        self._refresh_online_options()
        self.refresh_log_files()

    def _apply_config_data(self, data: dict, source_name: str):
        api = str(data.get("server_api_base") or "").strip()
        if api:
            self.server_api.set(api)
        self.source_hint.set(f"目前資料來源：{source_name}")

        default_activity_code = str(data.get("default_activity_code") or "").strip().upper()
        if default_activity_code and not self.schedule_code.get().strip():
            self.schedule_code.set(default_activity_code)
        default_photographer = str(data.get("default_photographer") or "").strip()
        if default_photographer and not self.photographer.get().strip():
            self.photographer.set(default_photographer)

        photographer_displays = []
        for item in (data.get("photographers") or []):
            if isinstance(item, dict):
                name = str(item.get("name") or item.get("photographer_name") or "").strip()
                note = str(item.get("note") or item.get("remark") or "").strip()
            else:
                name = str(item or "").strip()
                note = ""
            if name:
                display = f"{name}｜{note}" if note else name
                self.photographer_display_map[display] = name
                photographer_displays.append(display)
        if photographer_displays:
            self.photographer_options = sorted(set(self.photographer_options + photographer_displays))
            self.photographer_combo["values"] = self.photographer_options

        labels = []
        self.schedule_map = {}
        for item in (data.get("activities") or []):
            if not isinstance(item, dict):
                continue
            code = str(item.get("activity_code") or "").strip().upper()
            if not code:
                continue
            date = str(item.get("activity_date") or "").strip()
            t_range = str(item.get("activity_time_range") or item.get("activity_time") or "").strip()
            content = str(item.get("activity_content") or "").strip()
            label = f"{code} {date} {t_range} {content}".strip()
            self.schedule_map[label] = item
            labels.append(label)
        if labels:
            self.schedule_combo["values"] = labels
            if not self.schedule_display.get().strip():
                self.schedule_display.set(labels[0])
                self._on_schedule_selected()

    def _apply_saved_defaults(self):
        state = self._local_state if isinstance(self._local_state, dict) else {}
        if not state:
            return

        normalize_mode = str(state.get("default_normalize_mode") or "").strip()
        if normalize_mode:
            for label, code in NORMALIZE_MODE_OPTIONS:
                if normalize_mode == label or normalize_mode == code:
                    self.normalize_mode_text.set(label)
                    self._on_normalize_mode_changed()
                    break

        run_mode = str(state.get("default_run_mode") or "").strip()
        if run_mode:
            for label, code in RUN_MODE_OPTIONS:
                if run_mode == label or run_mode == code:
                    self.run_mode_text.set(label)
                    break

        photographer = str(state.get("default_photographer") or "").strip()
        if photographer:
            self.photographer.set(photographer)
            display = self._find_photographer_display(photographer)
            if display:
                self.photographer_choice.set(display)

        activity_code = str(state.get("default_activity_code") or "").strip().upper()
        if activity_code:
            self.schedule_code.set(activity_code)
            label = self._find_schedule_label_by_code(activity_code)
            if label:
                self.schedule_display.set(label)
                if self._normalize_mode_code() == "schedule":
                    self.schedule_combo.configure(state="readonly")
                self._on_schedule_selected()

    def _find_photographer_display(self, photographer_name: str) -> str:
        target = _extract_photographer_name(photographer_name).strip().upper()
        if not target:
            return ""
        for display, real_name in self.photographer_display_map.items():
            if _extract_photographer_name(real_name).strip().upper() == target:
                return display
        for display in self.photographer_options:
            if _extract_photographer_name(display).strip().upper() == target:
                return display
        return ""

    def _find_schedule_label_by_code(self, activity_code: str) -> str:
        target = str(activity_code or "").strip().upper()
        if not target:
            return ""
        for label, item in self.schedule_map.items():
            if str(item.get("activity_code") or "").strip().upper() == target:
                return label
        return ""

    def _refresh_online_options(self):
        def worker():
            try:
                api = self._ensure_server_ready()
            except Exception:
                return
            try:
                rows = _json_request(f"{api}/photographers/query?limit=1000", timeout=20).get("items") or []
                displays = []
                for r in rows:
                    name = str(r.get("photographer_name") or "").strip()
                    note = str(r.get("note") or "").strip()
                    if not name:
                        continue
                    display = f"{name}｜{note}" if note else name
                    self.photographer_display_map[display] = name
                    displays.append(display)
                if displays:
                    merged = sorted(set(self.photographer_options + displays))
                    self.photographer_options = merged
                    self.root.after(0, lambda: self.photographer_combo.configure(values=merged))
            except Exception:
                pass
            try:
                rows = _json_request(f"{api}/activity-schedules/options", timeout=20).get("items") or []
                labels = []
                for item in rows:
                    code = str(item.get("activity_code") or "").strip().upper()
                    if not code:
                        continue
                    date = str(item.get("activity_date") or "").strip()
                    t_range = str(item.get("activity_time_range") or item.get("activity_time") or "").strip()
                    content = str(item.get("activity_content") or "").strip()
                    label = f"{code} {date} {t_range} {content}".strip()
                    self.schedule_map[label] = item
                    labels.append(label)
                if labels:
                    self.root.after(0, lambda: self.schedule_combo.configure(values=labels))
            except Exception:
                pass
        threading.Thread(target=worker, daemon=True).start()

    def _pick_dir(self, var: tk.StringVar):
        path = filedialog.askdirectory(title="選擇資料夾")
        if path:
            var.set(path)

    def _api_base(self) -> str:
        api = self.server_api.get().strip()
        if not api and DEFAULT_CONFIG_PATH.exists():
            try:
                api = str(json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8-sig")).get("server_api_base") or "").strip()
            except Exception:
                api = ""
        if not api:
            raise RuntimeError("尚未設定 Server API。")
        api = api.rstrip("/")
        if not (api.startswith("http://") or api.startswith("https://")):
            raise RuntimeError(f"Server API 格式錯誤：{api}")
        self.server_api.set(api)
        return api

    def _ensure_server_ready(self):
        api = self._api_base()
        _json_request(f"{api}/openapi.json", timeout=15)
        self._save_local_state()
        return api

    def _on_schedule_selected(self, _evt=None):
        item = self.schedule_map.get(self.schedule_display.get().strip())
        if item:
            self.schedule_code.set(str(item.get("activity_code") or "").strip().upper())
        self._save_local_state()

    def _on_photographer_selected(self, _evt=None):
        selected = self.photographer_choice.get().strip()
        if not selected:
            return
        real_name = self.photographer_display_map.get(selected, _extract_photographer_name(selected))
        if real_name:
            self.photographer.set(real_name)
        self._save_local_state()

    def _on_run_mode_selected(self, _evt=None):
        self._save_local_state()

    def download_config(self):
        def worker():
            try:
                api = self._ensure_server_ready()
                with urllib.request.urlopen(f"{api}/laptop-tool/config", timeout=30) as response:
                    body = response.read()
                data = json.loads(body.decode("utf-8-sig"))
                self._write_default_config_file(data)
                self._apply_config_data(data, "設定檔")
                self.append(f"[{_now_text()}] 下載設定檔成功：{DEFAULT_CONFIG_PATH}")
                self.status.set("設定檔已更新。")
            except Exception as exc:
                self.append(f"[{_now_text()}] 下載設定檔失敗：{exc}")
                self.status.set(f"下載設定檔失敗：{exc}")
        threading.Thread(target=worker, daemon=True).start()

    def load_config_file(self):
        path = filedialog.askopenfilename(title="選擇設定檔", filetypes=[("JSON", "*.json"), ("All", "*.*")])
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
            self._apply_config_data(data, "設定檔")
            self._save_local_state()
            self.append(f"[{_now_text()}] 載入設定檔成功：{path}")
        except Exception as exc:
            self.append(f"[{_now_text()}] 載入設定檔失敗：{exc}")
            self.status.set(f"載入設定檔失敗：{exc}")

    def _validate_required(self):
        did = self.device_id.get().strip().upper()
        if not did:
            messagebox.showwarning(APP_TITLE, "請先輸入筆電編號。")
            return False
        if not _extract_photographer_name(self.photographer.get()):
            messagebox.showwarning(APP_TITLE, "請先選擇或輸入攝影師。")
            return False
        self.device_id.set(did)
        return True

    def load_model_manifest(self):
        try:
            api = self._ensure_server_ready()
            items = _json_request(f"{api}/laptop-tool/model-manifest", timeout=20).get("items") or []
            labels = []
            self.models_map = {}
            for item in items:
                name = str(item.get("name") or "").strip()
                if not name:
                    continue
                if not name.lower().endswith(".zip"):
                    continue
                label = f"{name}（推薦）" if item.get("recommended") else name
                labels.append(label)
                self.models_map[label] = item
            self.model_combo["values"] = labels
            if labels:
                self.model_combo.set(labels[0])
            self.append(f"[{_now_text()}] 模型清單載入完成，共 {len(labels)} 筆。")
        except Exception as exc:
            self.append(f"[{_now_text()}] 讀取模型清單失敗：{exc}")

    def download_model(self):
        selected = self.model_combo.get().strip()
        item = self.models_map.get(selected)
        if not item:
            messagebox.showwarning(APP_TITLE, "請先選擇模型。")
            return

        def worker():
            try:
                api = self._ensure_server_ready()
                url = str(item.get("download_url") or "").strip()
                if not url:
                    raise RuntimeError("模型清單缺少 download_url。")
                if url.startswith("/"):
                    url = f"{api}{url}"
                MODEL_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
                embedding_dir = BASE_DIR / "embeddings"
                embedding_dir.mkdir(parents=True, exist_ok=True)
                file_name = str(item.get("name") or "model.zip")
                if file_name.lower().endswith(".pkl"):
                    target = embedding_dir / file_name
                else:
                    target = MODEL_DOWNLOAD_DIR / file_name
                with urllib.request.urlopen(url, timeout=180) as response:
                    target.write_bytes(response.read())
                if target.suffix.lower() == ".zip":
                    base = DEFAULT_MODEL_DIR
                    out = base / "antelopev2"
                    out.mkdir(parents=True, exist_ok=True)
                    with zipfile.ZipFile(target, "r") as zf:
                        names = [n for n in zf.namelist() if n and not n.endswith("/")]
                        top = {n.split("/", 1)[0] for n in names if "/" in n}
                        if len(top) == 1 and "antelopev2" in top:
                            zf.extractall(base)
                        else:
                            zf.extractall(out)
                    nested = out / "antelopev2"
                    if nested.exists():
                        for c in nested.iterdir():
                            t = out / c.name
                            if not t.exists():
                                shutil.move(str(c), str(t))
                        try:
                            nested.rmdir()
                        except OSError:
                            pass
                MODEL_READY_META.write_text(
                    json.dumps(
                        {"download_time": _now_text(), "name": str(item.get("name") or ""), "version": str(item.get("version") or ""), "target_path": str(target), "model_dir": str(DEFAULT_MODEL_DIR / "antelopev2")},
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                    newline="\n",
                )
                self.append(f"[{_now_text()}] 模型下載完成：{target}")
                self.status.set("模型下載完成。")
            except Exception as exc:
                self.append(f"[{_now_text()}] 下載模型失敗：{exc}")
                self.status.set(f"下載模型失敗：{exc}")
        threading.Thread(target=worker, daemon=True).start()

    def _normalize_mode_code(self):
        for label, code in NORMALIZE_MODE_OPTIONS:
            if self.normalize_mode_text.get().strip() == label:
                return code
        return "exif"

    def _build_activity_windows(self):
        windows = []
        for item in (self.schedule_map or {}).values():
            if not isinstance(item, dict):
                continue
            code = str(item.get("activity_code") or "").strip().upper()
            date_text = str(item.get("activity_date") or "").strip().replace("/", "-")
            if not code or not date_text:
                continue

            raw_range = str(item.get("activity_time_range") or item.get("activity_time") or "").strip()
            if not raw_range:
                continue
            normalized = re.sub(r"\s+", "", raw_range.replace("～", "~").replace("至", "~"))
            parts = re.split(r"[~\-]", normalized, maxsplit=1)
            if len(parts) != 2:
                # 只有單點時間時，給 1 分鐘區間
                t0 = _parse_time_text(normalized)
                if t0 is None:
                    continue
                start_dt = datetime.fromisoformat(f"{date_text} {t0.strftime('%H:%M:%S')}")
                end_dt = start_dt + timedelta(minutes=1)
            else:
                t_start = _parse_time_text(parts[0])
                t_end = _parse_time_text(parts[1])
                if t_start is None or t_end is None:
                    continue
                start_dt = datetime.fromisoformat(f"{date_text} {t_start.strftime('%H:%M:%S')}")
                end_dt = datetime.fromisoformat(f"{date_text} {t_end.strftime('%H:%M:%S')}")
                if end_dt <= start_dt:
                    continue

            windows.append(
                {
                    "code": code,
                    "start": start_dt,
                    "end": end_dt,
                    "label": str(item.get("activity_content") or "").strip(),
                }
            )
        windows.sort(key=lambda x: (x["start"], x["code"]))
        return windows

    def _match_activity_code_for_photo(self, dt_value: datetime, activity_windows: list):
        target = dt_value
        candidates_same_day = 0
        matched = None
        for w in activity_windows:
            if w["start"].date() == target.date():
                candidates_same_day += 1
            if w["start"] <= target < w["end"]:
                matched = w
                break
        if matched:
            return matched["code"], candidates_same_day, matched
        return "000", candidates_same_day, None

    def run_normalize(self):
        if not self._validate_required():
            return
        self.active_section = "section2"
        threading.Thread(target=self._run_normalize_worker, daemon=True).start()

    def _run_normalize_worker(self):
        try:
            self.active_section = "section2"
            incoming = Path(self.incoming_dir.get().strip()); incoming.mkdir(parents=True, exist_ok=True)
            success = BASE_DIR / "normalized_success"; success.mkdir(parents=True, exist_ok=True)
            complete = BASE_DIR / "normalized_complete"; complete.mkdir(parents=True, exist_ok=True)
            fail = BASE_DIR / "normalized_fail"; fail.mkdir(parents=True, exist_ok=True)

            mode = self._normalize_mode_code()
            did = _safe_name(self.device_id.get().strip().upper())
            photographer = _safe_name(_extract_photographer_name(self.photographer.get()))
            schedule_code_ui = _safe_name(self.schedule_code.get().strip().upper() or "000")
            activity_windows = self._build_activity_windows() if mode == "exif" else []
            job_id = f"norm_{did}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            self.current_normalize_job_id = job_id
            work = BASE_DIR / "_work" / "normalize" / job_id
            work.mkdir(parents=True, exist_ok=True)

            files = sorted([p for p in incoming.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS])
            ok, ng = 0, 0
            matched_ok, matched_000, missing_exif = 0, 0, 0
            records = []
            for idx, src in enumerate(files, start=1):
                try:
                    exif_dt = _extract_exif_dt(src)
                    stem = _safe_name(src.stem)
                    if mode == "exif":
                        if exif_dt is None:
                            ng += 1
                            missing_exif += 1
                            try:
                                dst = fail / src.name
                                m = 1
                                while dst.exists():
                                    dst = fail / f"{dst.stem}_{m}{dst.suffix}"
                                    m += 1
                                shutil.move(str(src), str(dst))
                            except Exception:
                                pass
                            self.append(
                                f"[{_now_text()}] [正規化 {idx}/{len(files)}] 失敗：{src.name} 無 EXIF，已移至 normalized_fail。"
                            )
                            continue
                        dt = exif_dt
                        dt_str = dt.strftime("%Y%m%d_%H%M%S")
                        code_raw, same_day_count, matched = self._match_activity_code_for_photo(dt, activity_windows)
                        code_norm = _safe_name(code_raw or "000")
                        if code_norm == "000":
                            matched_000 += 1
                            self.append(
                                f"[{_now_text()}] [正規化 {idx}/{len(files)}] 模式A比對：photo_time={dt.strftime('%Y-%m-%d %H:%M:%S')} NO_MATCH（同日候選={same_day_count}）"
                            )
                        else:
                            matched_ok += 1
                            self.append(
                                f"[{_now_text()}] [正規化 {idx}/{len(files)}] 模式A比對：photo_time={dt.strftime('%Y-%m-%d %H:%M:%S')} matched_activity_code={code_norm}"
                        )
                        filename = f"EXIF_{code_norm}_{did}_{photographer}_{dt_str}_{stem}.jpeg"
                    else:
                        dt = exif_dt or datetime.fromtimestamp(src.stat().st_mtime)
                        dt_str = dt.strftime("%Y%m%d_%H%M%S")
                        code_norm = schedule_code_ui
                        filename = f"NONEXIF_{code_norm}_{did}_{photographer}_{dt_str}_{stem}.jpeg"
                    out = success / filename
                    n = 1
                    while out.exists():
                        out = success / f"{out.stem}_{n}{out.suffix}"
                        n += 1
                    _save_normalized_jpeg(src, out)
                    dst = complete / src.name
                    m = 1
                    while dst.exists():
                        dst = complete / f"{dst.stem}_{m}{dst.suffix}"
                        m += 1
                    shutil.move(str(src), str(dst))
                    ok += 1
                    records.append({"source_file": str(src), "normalized_file": str(out)})
                    self.append(f"[{_now_text()}] [正規化 {idx}/{len(files)}] 成功：{src.name} -> {out.name}")
                except Exception as exc:
                    ng += 1
                    try:
                        shutil.move(str(src), str(fail / src.name))
                    except Exception:
                        pass
                    self.append(f"[{_now_text()}] [正規化 {idx}/{len(files)}] 失敗：{src.name}，原因：{exc}")

            manifest = {
                "job_id": job_id,
                "device_id": did,
                "photographer": photographer,
                "normalize_mode": mode,
                "schedule_code": schedule_code_ui,
                "source_folder": str(success),
                "generated_at": _now_text(),
                "files": records,
            }
            (work / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
            self.step23_source_dir.set(str(success))
            if mode == "exif":
                self.append(f"[{_now_text()}] 模式A統計：命中活動編號 {matched_ok} 筆，EXIF_000 {matched_000} 筆，無 EXIF 失敗 {missing_exif} 筆。")
            self.append(f"[{_now_text()}] 正規化完成：成功 {ok}，失敗 {ng}")
            self.status.set(f"正規化完成：成功 {ok}，失敗 {ng}")
            self.refresh_log_files()
        except Exception as exc:
            self.append(f"[{_now_text()}] 正規化失敗：{exc}")
            self.status.set(f"正規化失敗：{exc}")

    def _find_latest_manifest_source_folder(self):
        root = BASE_DIR / "_work" / "normalize"
        if not root.exists():
            return None, None
        manifests = sorted(root.glob("*/manifest.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for m in manifests:
            try:
                data = json.loads(m.read_text(encoding="utf-8-sig"))
                sf = str(data.get("source_folder") or "").strip()
                if sf:
                    return sf, str(m)
            except Exception:
                continue
        return None, None

    def apply_latest_manifest(self):
        source, manifest_path = self._find_latest_manifest_source_folder()
        if not source:
            messagebox.showwarning(APP_TITLE, "找不到可用的 manifest。")
            return
        self.step23_manifest_path.set(manifest_path or "")
        self.step23_manifest_source.set(source)
        self.append(f"[{_now_text()}] 套用最新 manifest：{manifest_path} -> {source}")

    def pick_manifest_file(self):
        path = filedialog.askopenfilename(title="選擇 manifest 設定檔", filetypes=[("JSON", "*.json"), ("All", "*.*")])
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
            source = str(data.get("source_folder") or "").strip()
            if not source:
                raise RuntimeError("manifest 缺少 source_folder")
            self.step23_manifest_path.set(path)
            self.step23_manifest_source.set(source)
            self.append(f"[{_now_text()}] 已載入 manifest：{path} -> {source}")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"載入 manifest 失敗：{exc}")

    def start_step23(self):
        if not self._validate_required():
            return
        self.active_section = "section3"
        self.log_offset = 0
        self.upload_progress.set(0.0)
        self.upload_progress_text.set("上傳進度：0% (0/0)")
        mode = dict(RUN_MODE_OPTIONS).get(self.run_mode_text.get().strip(), "server")
        if mode == "local":
            threading.Thread(target=self._run_local_mode_worker, daemon=True).start()
        else:
            threading.Thread(target=self._run_server_mode_worker, daemon=True).start()

    def _run_server_mode_worker(self):
        try:
            api = self._ensure_server_ready()
            did = self.device_id.get().strip().upper()
            source_current = self.step23_source_dir.get().strip()

            checked = []
            manifest_source = self.step23_manifest_source.get().strip()
            manifest_path = self.step23_manifest_path.get().strip()
            if not manifest_source:
                manifest_source, manifest_path = self._find_latest_manifest_source_folder()
            if manifest_source:
                checked.append(("manifest", manifest_source, manifest_path or ""))
            if source_current and (not manifest_source or source_current != manifest_source):
                checked.append(("ui", source_current, ""))

            selected = None
            total_found = 0
            for kind, folder, meta in checked:
                low = folder.lower()
                if any(low.startswith(p) for p in FORBIDDEN_ROOTS):
                    self.append(f"[{_now_text()}] 跳過禁止根目錄來源：{folder}")
                    continue
                preview = _post_form_json(f"{api}/activity-photo-import/preview-source", {"source_folder": folder}, timeout=30)
                total = _to_int(preview.get("total_count"), 0)
                self.append(f"[{_now_text()}] 前檢({kind})：source={folder}，count={total}")
                if total > 0:
                    selected = folder
                    total_found = total
                    if kind == "manifest":
                        self.append(f"[{_now_text()}] 使用 manifest 來源：{folder} (manifest={meta})")
                    break
            if not selected:
                raise RuntimeError("來源資料夾無可處理檔案（已檢查 manifest 與目前來源）。")

            self.step23_source_dir.set(selected)
            payload = {
                "laptop_number": did,
                "photographer": _extract_photographer_name(self.photographer.get()),
                "enable_pyiqa": "true" if self.enable_pyiqa.get() else "false",
                "normalize_mode": self._normalize_mode_code(),
                "source_folder": selected,
                "output_folder": "",
                "backup_folder": "",
            }
            self.append(f"[{_now_text()}] 啟動前檢查完成：source={selected}，count={total_found}，pyiqa={self.enable_pyiqa.get()}")
            data = _post_form_json(f"{api}/activity-photo-import/start", payload, timeout=60)
            jid = str(data.get("job_id") or "").strip()
            if not jid:
                raise RuntimeError(f"啟動任務失敗：{data}")
            self.job_id.set(jid)
            self.append(f"[{_now_text()}] 任務已啟動：{jid}")
            self._poll_job(jid)
        except Exception as exc:
            self.append(f"[{_now_text()}] 啟動失敗：{exc}")
            self.status.set(f"啟動失敗：{exc}")

    def _run_local_mode_worker(self):
        try:
            self.active_section = "section3"
            self.current_step23_job_id = None
            self.append(f"[{_now_text()}] === 步驟2+3：Local 模式（本機辨識 + 自動上傳）===")
            api = self._ensure_server_ready()
            did = _safe_name(self.device_id.get().strip().upper())
            photographer = _extract_photographer_name(self.photographer.get()).strip()

            checked = []
            manifest_source = self.step23_manifest_source.get().strip()
            manifest_path = self.step23_manifest_path.get().strip()
            if not manifest_source:
                manifest_source, manifest_path = self._find_latest_manifest_source_folder()
            if manifest_source:
                checked.append(("manifest", manifest_source, manifest_path or ""))
            source_current = self.step23_source_dir.get().strip()
            if source_current and (not manifest_source or source_current != manifest_source):
                checked.append(("ui", source_current, ""))

            selected = None
            total_found = 0
            for kind, folder, meta in checked:
                low = folder.lower()
                if any(low.startswith(p) for p in FORBIDDEN_ROOTS):
                    self.append(f"[{_now_text()}] 前檢({kind})：source={folder}，命中禁用路徑，略過")
                    continue
                src_try = Path(folder)
                try:
                    files_try = [p for p in sorted(src_try.iterdir()) if p.is_file() and p.suffix.lower() in IMAGE_EXTS]
                except Exception as exc:
                    self.append(f"[{_now_text()}] 前檢({kind})：source={folder}，無法列舉檔案：{exc}")
                    continue
                self.append(f"[{_now_text()}] 前檢({kind})：source={folder}，count={len(files_try)}")
                if files_try:
                    selected = src_try
                    total_found = len(files_try)
                    if kind == "manifest":
                        self.append(f"[{_now_text()}] 使用 manifest 來源：{folder} (manifest={meta})")
                    break
            if selected is None:
                raise RuntimeError("來源資料夾無可處理檔案（已檢查 manifest 與目前來源）。")

            if cv2 is None or np is None:
                raise RuntimeError("缺少 cv2 / numpy，無法執行 Local 模式。")

            self.append(f"[{_now_text()}] L1 載入 FaceRecognition 類別")
            try:
                FaceRecognition = _load_face_recognition_class()
            except Exception as exc:
                raise RuntimeError(f"載入 FaceRecognition 失敗：{exc}") from exc

            self.append(f"[{_now_text()}] L2 驗證模型目錄")
            model_dir = DEFAULT_MODEL_DIR / "antelopev2"
            if not model_dir.exists():
                raise RuntimeError(f"找不到模型目錄：{model_dir}")

            self.append(f"[{_now_text()}] L3 驗證／部署 embedding")
            self._ensure_embedded_embedding()
            embedding_path = BASE_DIR / "embeddings" / EMBEDDING_NAME
            if not embedding_path.exists():
                raise RuntimeError(f"找不到 embedding 檔：{embedding_path}，請先下載 embedding。")

            files = [p for p in sorted(selected.iterdir()) if p.is_file() and p.suffix.lower() in IMAGE_EXTS]
            if not files:
                raise RuntimeError(f"來源資料夾無可處理檔案：{selected}")

            reco_success = BASE_DIR / "reco_success"; reco_success.mkdir(parents=True, exist_ok=True)
            reco_fail = BASE_DIR / "reco_fail"; reco_fail.mkdir(parents=True, exist_ok=True)
            upload_queue = BASE_DIR / "upload_queue"; upload_queue.mkdir(parents=True, exist_ok=True)
            upload_done = BASE_DIR / "upload_done"; upload_done.mkdir(parents=True, exist_ok=True)
            upload_fail = BASE_DIR / "upload_fail"; upload_fail.mkdir(parents=True, exist_ok=True)

            model_version = "antelopev2"
            ok_layout, layout_msg, onnx_preview = _validate_model_layout(DEFAULT_MODEL_DIR)
            self.append(f"[{_now_text()}] {layout_msg}")
            if not ok_layout:
                if onnx_preview:
                    self.append(f"[{_now_text()}] 模型預檢檔案：{onnx_preview}")
                raise RuntimeError(layout_msg)

            self.append(f"[{_now_text()}] 模型預檢通過：{model_dir}（onnx={len(list(model_dir.glob('*.onnx')))}）")
            self.append(f"[{_now_text()}] 本機辨識初始化中：model={model_version}")
            self.append(f"[{_now_text()}] L4 建立 FaceAnalysis（CUDA+CPU，可自動降級）")
            face_recognition = FaceRecognition(
                model_root=str(BASE_DIR),
                embedding_path=str(embedding_path),
                gpu_id=0,
                init_timeout_sec=90,
                stage_logger=lambda msg: self.append(f"[{_now_text()}] {msg}"),
            )
            self.append(f"[{_now_text()}] L5 載入 embedding pickle")
            face_recognition.load_faces_from_pickle()

            probe_face_recognition = None
            try:
                probe_face_recognition = FaceRecognition(
                    model_root=str(BASE_DIR),
                    embedding_path=str(embedding_path),
                    gpu_id=0,
                    init_timeout_sec=90,
                    stage_logger=lambda msg: self.append(f"[{_now_text()}] [PROBE] {msg}"),
                )
                probe_face_recognition.load_faces_from_pickle()
            except Exception as exc:
                self.append(f"[{_now_text()}] L6 探針初始化失敗（仍續跑正式流程）：{exc}")
                probe_face_recognition = face_recognition

            self.append(f"[{_now_text()}] L6 進行 synthetic probe")
            probe_images = [
                ("synthetic_black", np.zeros((640, 640, 3), dtype=np.uint8)),
                ("synthetic_noise", np.random.default_rng(0).integers(0, 256, size=(640, 640, 3), dtype=np.uint8)),
            ]
            for probe_name, probe_image in probe_images:
                try:
                    self.append(
                        f"[{_now_text()}] L6-PROBE {probe_name} shape={getattr(probe_image, 'shape', None)} dtype={getattr(probe_image, 'dtype', None)} contiguous={bool(getattr(getattr(probe_image, 'flags', None), 'c_contiguous', False))}"
                    )
                    probe_names, probe_faces = probe_face_recognition.recognition(probe_image)
                    self.append(
                        f"[{_now_text()}] L6-PROBE {probe_name} OK names={len(probe_names or [])} faces={0 if probe_faces is None else len(probe_faces)}"
                    )
                except Exception as exc:
                    self.append(f"[{_now_text()}] L6-PROBE {probe_name} FAIL：{type(exc).__name__}: {exc}")

            self.append(f"[{_now_text()}] 啟動上傳批次：total={len(files)}")
            start_payload = {
                "device_id": did,
                "laptop_label": did,
                "model_version": model_version,
                "total_count": len(files),
            }
            start_data = _post_json(f"{api}/laptop-tool/upload-batch/start", start_payload, timeout=60)
            upload_job_id = str(start_data.get("job_id") or "").strip()
            if not upload_job_id:
                raise RuntimeError(f"啟動 upload-batch job 失敗：{start_data}")
            self.current_step23_job_id = upload_job_id
            self.append(f"[{_now_text()}] upload-batch job_id={upload_job_id}")

            metric = None
            if self.enable_pyiqa.get():
                try:
                    try:
                        import torch as _torch  # type: ignore
                        local_torch = _torch
                    except Exception:
                        local_torch = None
                    try:
                        import pyiqa as _pyiqa  # type: ignore
                        local_pyiqa = _pyiqa
                    except Exception:
                        local_pyiqa = None
                    if local_pyiqa is None:
                        raise RuntimeError("LOCAL_PYIQA_MISSING：找不到 pyiqa，請先安裝或略過此功能。")
                    device = "cpu"
                    if local_torch is not None and getattr(local_torch, "cuda", None) is not None and local_torch.cuda.is_available():
                        device = "cuda"
                    metric = local_pyiqa.create_metric("clipiqa+", device=device)
                    self.append(f"[{_now_text()}] pyiqa 已啟用，device={device}")
                except Exception as exc:
                    self.append(f"[{_now_text()}] pyiqa 無法啟用，將略過：{exc}")
                    metric = None

            results = []
            ok, ng, uploaded = 0, 0, 0
            total_chunks = len(files)

            def _send_chunk_request(body: bytes, boundary: str) -> dict:
                req = urllib.request.Request(
                    f"{api}/laptop-tool/upload-batch/chunk",
                    data=body,
                    headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                    method="POST",
                )
                try:
                    with urllib.request.urlopen(req, timeout=120) as resp:
                        resp_body = resp.read().decode("utf-8", "replace")
                except urllib.error.HTTPError as http_exc:
                    detail = ""
                    try:
                        detail = http_exc.read().decode("utf-8", "replace")
                    except Exception:
                        detail = str(http_exc)
                    raise RuntimeError(f"上傳 chunk 失敗 HTTP {http_exc.code}：{detail[:500]}") from http_exc
                return json.loads(resp_body)

            def _record_success(path: Path, item: dict):
                nonlocal uploaded, ok
                results.append(item)
                uploaded += 1
                percent = int((uploaded / max(total_chunks, 1)) * 100)
                self._set_progress_safe(float(percent), f"上傳進度：{percent}% ({uploaded}/{total_chunks})")
                self.upload_progress_text.set(f"上傳進度：{percent}% ({uploaded}/{total_chunks})")
                dst_ok = reco_success / path.name
                m = 1
                while dst_ok.exists():
                    dst_ok = reco_success / f"{Path(path.name).stem}_{m}{Path(path.name).suffix}"
                    m += 1
                shutil.move(str(path), str(dst_ok))
                ok += 1
                self.append(f"[{_now_text()}] [Local] 成功：{path.name}，uploaded={uploaded}")

            def _record_failure(path: Path, item: dict, exc: Exception):
                nonlocal ng
                fail_item = dict(item)
                fail_item["reco_status"] = "FAILED"
                fail_item["reco_error"] = str(exc)
                fail_item["error_reason"] = str(exc)
                results.append(fail_item)
                ng += 1
                try:
                    dst_fail = reco_fail / path.name
                    m = 1
                    while dst_fail.exists():
                        dst_fail = reco_fail / f"{Path(path.name).stem}_{m}{Path(path.name).suffix}"
                        m += 1
                    shutil.move(str(path), str(dst_fail))
                except Exception:
                    pass
                self.append(f"[{_now_text()}] [Local] 失敗：{path.name}，原因：{exc}")

            pending_uploads = []

            def _drain_one_pending():
                task = pending_uploads.pop(0)
                try:
                    task["future"].result()
                    _record_success(task["path"], task["item"])
                except Exception as exc:
                    _record_failure(task["path"], task["item"], exc)

            with ThreadPoolExecutor(max_workers=LAPTOP_UPLOAD_MAX_IN_FLIGHT) as upload_executor:
                for idx, path in enumerate(files, start=1):
                    try:
                        file_size = path.stat().st_size if path.exists() else 0
                        self.append(f"[{_now_text()}] [Local {idx}/{len(files)}] 檔案={path.name} size={file_size} ext={path.suffix}")
                        image, image_source, image_error = _load_image_for_local(path)
                        self.append(f"[{_now_text()}] [Local {idx}/{len(files)}] 讀圖來源={image_source} err={image_error or 'OK'}")
                        if image is None:
                            raise RuntimeError(f"{image_error or 'LOCAL_IMAGE_DECODE_FAIL'}：讀圖失敗，檔案={path.name}")
                        self.append(
                            f"[{_now_text()}] [Local {idx}/{len(files)}] image.shape={getattr(image, 'shape', None)} dtype={getattr(image, 'dtype', None)} contiguous={bool(getattr(getattr(image, 'flags', None), 'c_contiguous', False))}"
                        )
                        try:
                            names, faces = face_recognition.recognition(image)
                        except Exception as reco_exc:
                            reco_text = str(reco_exc)
                            if reco_text.startswith((
                                "LOCAL_RECO_FAIL",
                                "LOCAL_IMAGE_INVALID",
                                "LOCAL_IMAGE_DECODE_FAIL",
                                "LOCAL_FACEANALYSIS_ASSERT",
                                "LOCAL_MODEL_INPUT_INVALID",
                                "LOCAL_INIT_TIMEOUT",
                                "LOCAL_MODEL_GET_NONE_SHAPE",
                                "LOCAL_MODEL_GET_ASSERT",
                                "LOCAL_MODEL_GET_ATTR",
                                "LOCAL_MODEL_GET_OTHER",
                            )):
                                raise
                            raise RuntimeError(f"LOCAL_RECO_FAIL: {reco_text}") from reco_exc
                        if names is None:
                            names = []
                        if faces is None:
                            faces = []
                        face_position = []
                        det_score = None
                        for face in faces:
                            if isinstance(face, dict):
                                score = face.get("det_score")
                                bbox_val = face.get("bbox")
                                face_name = face.get("name") or "unknown"
                            else:
                                score = getattr(face, "det_score", None)
                                bbox_val = getattr(face, "bbox", None)
                                face_name = getattr(face, "name", None) or "unknown"
                            try:
                                score = float(score)
                            except Exception:
                                score = None
                            if score is not None:
                                det_score = max(det_score, score) if det_score is not None else score
                            bbox_list = bbox_val.tolist() if hasattr(bbox_val, "tolist") else bbox_val
                            if bbox_list is None:
                                continue
                            face_position.append({"name": face_name, "det_score": score, "bbox": bbox_list})

                        pyiqa_score = None
                        if self.enable_pyiqa.get():
                            if metric is None:
                                self.append(f"[{_now_text()}] [Local {idx}/{len(files)}] pyiqa 未啟用，將略過。")
                            else:
                                try:
                                    score_input = image[:, :, ::-1]
                                    pyiqa_score = float(metric(score_input).item())
                                except Exception as exc:
                                    pyiqa_score = None
                                    self.append(f"[{_now_text()}] [Local {idx}/{len(files)}] pyiqa 計算失敗：{exc}")

                        exif_dt = _extract_exif_dt(path)
                        file_dt = datetime.fromtimestamp(path.stat().st_mtime)
                        taken_dt = exif_dt or file_dt
                        taken_time_source = "EXIF" if exif_dt else "FILE_TIME"
                        reco_status = "DONE" if len(face_position) > 0 else "NO_FACE"
                        photo_uuid = f"{did}_{uuid4().hex[:24]}"
                        item = {
                            "photo_uuid": photo_uuid,
                            "file_name": path.name,
                            "photo_taken_time": taken_dt.strftime("%Y-%m-%d %H:%M:%S"),
                            "photo_file_time": file_dt.strftime("%Y-%m-%d %H:%M:%S"),
                            "taken_time_source": taken_time_source,
                            "human_activity_date": "",
                            "human_activity_time": "",
                            "human_activity_name": "",
                            "human_owner_team": "",
                            "human_location": "",
                            "human_photographer": photographer,
                            "reco_name": names or [],
                            "reco_res": face_position,
                            "reco_count": len([x for x in (names or []) if str(x).lower() != "unknown"]),
                            "reco_unknow": len([x for x in (names or []) if str(x).lower() == "unknown"]),
                            "reco_status": reco_status,
                            "reco_error": "",
                            "img_score": pyiqa_score,
                            "det_score": det_score,
                        }
                        boundary = "----CodexLaptopToolBoundary"
                        meta_json = json.dumps(item, ensure_ascii=False)
                        file_bytes = path.read_bytes()
                        parts = []

                        def add_field(name, value):
                            parts.append(f"--{boundary}\r\n".encode("utf-8"))
                            parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
                            parts.append(str(value).encode("utf-8"))
                            parts.append(b"\r\n")

                        add_field("job_id", upload_job_id)
                        add_field("seq_no", idx)
                        add_field("item_json", meta_json)
                        for field_name in ("origin_file", "thumb_file"):
                            parts.append(f"--{boundary}\r\n".encode("utf-8"))
                            parts.append(
                                f'Content-Disposition: form-data; name="{field_name}"; filename="{path.name}"\r\n'.encode("utf-8")
                            )
                            parts.append(b"Content-Type: application/octet-stream\r\n\r\n")
                            parts.append(file_bytes)
                            parts.append(b"\r\n")
                        parts.append(f"--{boundary}--\r\n".encode("utf-8"))
                        body = b"".join(parts)
                        pending_uploads.append(
                            {
                                "path": path,
                                "item": item,
                                "future": upload_executor.submit(_send_chunk_request, body, boundary),
                            }
                        )
                        if len(pending_uploads) >= LAPTOP_UPLOAD_MAX_IN_FLIGHT:
                            _drain_one_pending()
                    except Exception as exc:
                        fail_file_dt = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
                        results.append(
                            {
                                "photo_uuid": f"{did}_{uuid4().hex[:24]}",
                                "file_name": path.name,
                                "file_path": str(path),
                                "photo_taken_time": fail_file_dt,
                                "photo_file_time": fail_file_dt,
                                "taken_time_source": "FILE_TIME",
                                "reco_name": [],
                                "reco_res": [],
                                "reco_count": 0,
                                "reco_unknow": 0,
                                "reco_status": "FAILED",
                                "reco_error": str(exc),
                                "img_score": None,
                                "det_score": None,
                                "error_reason": str(exc),
                            }
                        )
                        ng += 1
                        try:
                            dst_fail = reco_fail / path.name
                            m = 1
                            while dst_fail.exists():
                                dst_fail = reco_fail / f"{Path(path.name).stem}_{m}{Path(path.name).suffix}"
                                m += 1
                            shutil.move(str(path), str(dst_fail))
                        except Exception:
                            pass
                        self.append(f"[{_now_text()}] [Local {idx}/{len(files)}] 失敗：{path.name}，原因：{exc}")

                while pending_uploads:
                    _drain_one_pending()
            commit_payload = {"job_id": upload_job_id}
            commit_data = _post_json(f"{api}/laptop-tool/upload-batch/commit", commit_payload, timeout=180)
            committed = _to_int(commit_data.get("committed_count"), 0)
            failed_commit = _to_int(commit_data.get("failed_count"), 0)
            failed_items = commit_data.get("failed_items") or []
            if failed_commit > 0 and failed_items:
                self.append(f"[{_now_text()}] COMMIT_FAILED_ITEMS：{len(failed_items)}")
                for fi in failed_items[:20]:
                    self.append(
                        f"[{_now_text()}] COMMIT_FAIL photo_uuid={fi.get('photo_uuid','')} file={fi.get('file_name','')} code={fi.get('error_code','')} reason={fi.get('error_reason','')}"
                    )

            jpath = upload_queue / f"local_reco_{upload_job_id}.json"
            cpath = upload_queue / f"local_reco_{upload_job_id}.csv"
            jpath.write_text(
                json.dumps(
                    {
                        "job_id": upload_job_id,
                        "device_id": did,
                        "mode": "local",
                        "enable_pyiqa": self.enable_pyiqa.get(),
                        "created_at": _now_text(),
                        "source_folder": str(selected),
                        "results": results,
                        "summary": {
                            "scanned": len(files),
                            "local_success": ok,
                            "local_failed": ng,
                            "uploaded": uploaded,
                            "committed_count": committed,
                            "failed_commit": failed_commit,
                        },
                        "failed_items": failed_items,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
                newline="\n",
            )
            with cpath.open("w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(
                    f,
                    fieldnames=["file_name", "photo_uuid", "reco_name", "det_score", "reco_count", "reco_unknow", "error_reason"],
                )
                writer.writeheader()
                for r in results:
                    writer.writerow(
                        {
                            "file_name": r.get("file_name", ""),
                            "photo_uuid": r.get("photo_uuid", ""),
                            "reco_name": ",".join(r.get("reco_name") or []),
                            "det_score": r.get("det_score"),
                            "reco_count": r.get("reco_count", ""),
                            "reco_unknow": r.get("reco_unknow", ""),
                            "error_reason": r.get("error_reason", ""),
                        }
                    )

            if failed_commit > 0:
                shutil.copy2(jpath, upload_fail / jpath.name)
                shutil.copy2(cpath, upload_fail / cpath.name)
            else:
                shutil.copy2(jpath, upload_done / jpath.name)
                shutil.copy2(cpath, upload_done / cpath.name)

            self.append(f"[{_now_text()}] Local 模式完成：scanned={len(files)} local_ok={ok} local_fail={ng} uploaded={uploaded} committed={committed} commit_failed={failed_commit}")
            self.upload_progress.set(100.0)
            self.upload_progress_text.set(f"上傳進度：100% ({uploaded}/{total_chunks})")
            self._set_status_safe(
                f"Local 模式完成：scanned={len(files)} success={ok} failed={ng} uploaded={uploaded} committed={committed} commit_failed={failed_commit}"
            )
            self.refresh_log_files()
        except Exception as exc:
            self.append(f"[{_now_text()}] LOCAL_WORKER_ERROR: {exc}")
            self.append(traceback.format_exc().strip())
            self._set_status_safe(f"Local 模式失敗：{exc}")


    def load_recent_jobs(self):
        try:
            api = self._api_base()
            did = self.device_id.get().strip().upper()
            if not did:
                raise RuntimeError("請先輸入筆電編號。")
            q = urllib.parse.urlencode({"limit": 30, "device_id": did})
            data = _json_request(f"{api}/activity-photo-import/jobs-recent?{q}", timeout=20)
            labels, mapping = [], {}
            for item in data.get("items") or []:
                jid = str(item.get("job_id") or "").strip()
                if not jid:
                    continue
                ts = item.get("started_at") or item.get("updated_at") or ""
                label = f"{jid} | {item.get('status', '')} | {ts}"
                labels.append(label)
                mapping[label] = jid
            self.recent_job_map = mapping
            self.recent_job_combo["values"] = labels
            if labels:
                self.recent_job_choice.set(labels[0])
            self.append(f"[{_now_text()}] 已載入最近任務 {len(labels)} 筆。")
        except Exception as exc:
            self.append(f"[{_now_text()}] 讀取最近任務失敗：{exc}")

    def apply_recent_job(self):
        selected = self.recent_job_choice.get().strip()
        if not selected:
            messagebox.showwarning(APP_TITLE, "請先選擇最近任務。")
            return
        jid = self.recent_job_map.get(selected)
        if not jid:
            messagebox.showwarning(APP_TITLE, "找不到對應 Job ID。")
            return
        self.job_id.set(jid)
        self.status.set(f"已套用 Job：{jid}")

    def attach_job(self):
        jid = self.job_id.get().strip()
        if not jid:
            messagebox.showwarning(APP_TITLE, "請先輸入 job_id。")
            return
        self.log_offset = 0
        threading.Thread(target=self._poll_job, args=(jid,), daemon=True).start()

    def _poll_job(self, job_id: str):
        try:
            api = self._api_base()
        except Exception as exc:
            self.append(f"[{_now_text()}] 輪詢失敗：{exc}")
            return
        for _ in range(7200):
            try:
                s = _json_request(f"{api}/activity-photo-import/jobs/{urllib.parse.quote(job_id)}", timeout=20)
                l = _json_request(f"{api}/activity-photo-import/jobs/{urllib.parse.quote(job_id)}/logs?offset={self.log_offset}", timeout=20)
                for line in l.get("lines") or []:
                    self.append(str(line))
                self.log_offset = _to_int(l.get("next_offset"), self.log_offset)
                self.status.set(
                    f"Job={job_id} 狀態={s.get('status')}，進度 {s.get('processed_count',0)}/{s.get('total_count',0)}，"
                    f"成功 {s.get('success_count',0)}，失敗 {s.get('failed_count',0)}，略過 {s.get('skipped_count',0)}"
                )
                if str(s.get("status")) in {"DONE", "FAILED", "CANCELED"}:
                    self.append(f"[{_now_text()}] === 任務完成：{s.get('status')} ===")
                    return
                time.sleep(1)
            except Exception as exc:
                self.append(f"[{_now_text()}] 輪詢失敗：{exc}")
                time.sleep(2)

    def refresh_log_files(self):
        kind = self.log_type.get().strip()
        patterns = {
            "區塊1設定": ["tool_section1_*.log"],
            "區塊2正規化": ["tool_section2_normalize_*.log"],
            "區塊3入庫辨識": ["tool_section3_import_reco_*.log"],
            "任務log": ["normalize_*.log", "step23_*.log"],
        }
        selected_patterns = patterns.get(kind, ["tool_section2_normalize_*.log"])
        files = []
        for pat in selected_patterns:
            files.extend(LOG_DIR.glob(pat))
        files = sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)
        labels = []
        self.log_file_map = {}
        for p in files[:50]:
            label = f"{p.name} | {datetime.fromtimestamp(p.stat().st_mtime).strftime('%Y-%m-%d %H:%M:%S')}"
            labels.append(label)
            self.log_file_map[label] = p
        self.log_file_combo["values"] = labels
        self.log_file_choice.set(labels[0] if labels else "")

    def load_selected_log(self):
        selected = self.log_file_choice.get().strip()
        path = self.log_file_map.get(selected)
        if not path:
            messagebox.showwarning(APP_TITLE, "請先選擇 log 檔案。")
            return
        try:
            content = path.read_text(encoding="utf-8-sig", errors="replace")
            self.log_text.delete("1.0", tk.END)
            self.log_text.insert(tk.END, content)
            self.log_text.see(tk.END)
            self.status.set(f"已載入 Log：{path.name}")
        except Exception as exc:
            self.append(f"[{_now_text()}] 載入 Log 失敗：{exc}")

    def open_selected_log(self):
        selected = self.log_file_choice.get().strip()
        path = self.log_file_map.get(selected)
        if not path:
            messagebox.showwarning(APP_TITLE, "請先選擇 log 檔案。")
            return
        try:
            subprocess.Popen(["notepad.exe", str(path)], shell=False)
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"開啟 Log 失敗：{exc}")

    def copy_error_summary(self):
        text = self.log_text.get("1.0", tk.END)
        lines = [ln for ln in text.splitlines() if ("失敗" in ln or "FAILED" in ln or "error" in ln.lower())]
        summary = "\n".join(lines[-100:]) if lines else "目前沒有錯誤摘要。"
        self.root.clipboard_clear()
        self.root.clipboard_append(summary)
        self.status.set("已複製錯誤摘要。")


def main():
    root = tk.Tk()
    LaptopTool(root)
    root.mainloop()


if __name__ == "__main__":
    main()

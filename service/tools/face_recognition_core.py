from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
import traceback
from pathlib import Path
import pickle

import insightface
import numpy as np

try:
    import cv2  # type: ignore
except Exception:  # pragma: no cover
    cv2 = None


if not hasattr(np, "int"):
    np.int = int


def _normalize_embedding(embedding: np.ndarray) -> np.ndarray:
    norm = np.linalg.norm(embedding)
    if norm == 0:
        return embedding
    return embedding / norm


class SharedFaceRecognitionCore:
    def __init__(
        self,
        *,
        model_root: str,
        embedding_path: str,
        model_name: str = "antelopev2",
        gpu_id: int = 0,
        threshold: float = 1.24,
        det_thresh: float = 0.50,
        det_size=(640, 640),
        allowed_modules: list[str] | None = None,
        providers: list[str] | None = None,
        fallback_providers: list[str] | None = None,
        init_timeout_sec: int | None = None,
        stage_logger=None,
        enable_probes: bool = False,
        probe_resize_edges: tuple[int, ...] = (2048, 1024, 640),
    ):
        self.model_name = model_name
        self.gpu_id = gpu_id
        self.threshold = threshold
        self.det_thresh = det_thresh
        self.det_size = det_size
        self.model_root = str(model_root)
        self.embedding_path = str(embedding_path)
        self.allowed_modules = list(allowed_modules or ["detection", "recognition"])
        self.providers = list(providers or ["CUDAExecutionProvider", "CPUExecutionProvider"])
        self.fallback_providers = list(fallback_providers or ["CPUExecutionProvider"])
        self.init_timeout_sec = int(init_timeout_sec or 0)
        self.stage_logger = stage_logger
        self.enable_probes = bool(enable_probes)
        self.probe_resize_edges = tuple(probe_resize_edges or ())
        self.faces_embedding: list[dict] = []
        self.model = self._init_face_model()

    def _log_stage(self, text: str):
        if callable(self.stage_logger):
            self.stage_logger(str(text))

    def _model_dir(self) -> Path:
        return Path(self.model_root) / "models" / self.model_name

    def _onnx_files(self) -> list[str]:
        model_dir = self._model_dir()
        if not model_dir.exists():
            return []
        return sorted([p.name for p in model_dir.glob("*.onnx") if p.is_file()])

    def _build_model(self, providers):
        ctx_id = self._resolve_ctx_id(providers)
        self._log_stage(
            f"L4-MODEL root={self.model_root} name={self.model_name} ctx_id={ctx_id} providers={providers} allowed_modules={self.allowed_modules} onnx={self._onnx_files()}"
        )
        model = insightface.app.FaceAnalysis(
            root=self.model_root,
            name=self.model_name,
            allowed_modules=self.allowed_modules,
            providers=providers,
        )
        model.prepare(ctx_id=ctx_id, det_thresh=self.det_thresh, det_size=self.det_size)
        self._log_stage(f"L4-MODEL modules={list(getattr(model, 'models', {}).keys())}")
        return model

    def _resolve_ctx_id(self, providers) -> int:
        provider_list = list(providers or [])
        if not provider_list:
            return -1
        if all(str(provider) == "CPUExecutionProvider" for provider in provider_list):
            return -1
        if any(str(provider) == "CUDAExecutionProvider" for provider in provider_list):
            if self.gpu_id is None:
                return 0
            return self.gpu_id if self.gpu_id >= 0 else 0
        return -1

    def _init_with_timeout(self, providers):
        if self.init_timeout_sec <= 0:
            return self._build_model(providers)
        with ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(self._build_model, providers)
            try:
                return fut.result(timeout=self.init_timeout_sec)
            except FuturesTimeoutError as exc:
                raise TimeoutError(
                    f"LOCAL_INIT_TIMEOUT: 建立 FaceAnalysis 超過 {self.init_timeout_sec}s，providers={providers}"
                ) from exc

    def _init_face_model(self):
        self._log_stage(f"L4 建立 FaceAnalysis：gpu_id={self.gpu_id} providers={self.providers}")
        try:
            return self._init_with_timeout(self.providers)
        except Exception as exc:
            if self.providers == self.fallback_providers:
                raise
            self._log_stage(f"LOCAL_PROVIDER_FALLBACK: {exc}")
            self._log_stage(f"L4-FALLBACK 建立 FaceAnalysis：gpu_id={self.gpu_id} providers={self.fallback_providers}")
            return self._init_with_timeout(self.fallback_providers)

    def load_faces_from_pickle(self):
        path = Path(self.embedding_path)
        if not path.exists():
            raise FileNotFoundError(f"找不到 embedding 檔：{path}")
        with path.open("rb") as f:
            loaded = pickle.load(f)
        self.faces_embedding.clear()
        if isinstance(loaded, list):
            self.faces_embedding.extend(loaded)

    def _classify_model_get_error(self, exc: Exception) -> str:
        message = str(exc)
        if isinstance(exc, AssertionError):
            return f"LOCAL_MODEL_GET_ASSERT: {message}"
        if isinstance(exc, AttributeError) and "NoneType" in message and "shape" in message:
            return f"LOCAL_MODEL_GET_NONE_SHAPE: {message}"
        if isinstance(exc, AttributeError):
            return f"LOCAL_MODEL_GET_ATTR: {message}"
        return f"LOCAL_MODEL_GET_OTHER: {type(exc).__name__}: {message}"

    def _iter_probe_variants(self, image):
        yield "original", image
        contiguous = np.ascontiguousarray(image)
        if contiguous is not image:
            yield "contiguous", contiguous
        if not self.enable_probes or cv2 is None:
            return
        try:
            h, w = image.shape[:2]
            long_edge = max(h, w)
            for target_long_edge in self.probe_resize_edges:
                if long_edge <= target_long_edge:
                    continue
                scale = float(target_long_edge) / float(long_edge)
                new_w = max(1, int(round(w * scale)))
                new_h = max(1, int(round(h * scale)))
                resized = cv2.resize(contiguous, (new_w, new_h), interpolation=cv2.INTER_AREA)
                yield f"resized_{new_w}x{new_h}", np.ascontiguousarray(resized)
        except Exception:
            return

    def _validate_input(self, image: np.ndarray) -> np.ndarray:
        if image is None:
            raise RuntimeError("LOCAL_IMAGE_INVALID: image is None")
        if not isinstance(image, np.ndarray):
            raise RuntimeError(f"LOCAL_IMAGE_INVALID: image type={type(image).__name__}")
        image_shape = getattr(image, "shape", None)
        if getattr(image, "ndim", 0) != 3 or not image_shape or len(image_shape) < 3 or image_shape[2] != 3:
            raise RuntimeError(f"LOCAL_IMAGE_INVALID: image shape={image_shape}")
        if image.dtype != np.uint8:
            image = image.astype(np.uint8, copy=False)
        return np.ascontiguousarray(image)

    def _run_model_get(self, image: np.ndarray):
        image = self._validate_input(image)
        image_contiguous = bool(getattr(getattr(image, "flags", None), "c_contiguous", False))
        try:
            image_min = int(np.min(image))
            image_max = int(np.max(image))
        except Exception:
            image_min = None
            image_max = None
        self._log_stage(
            f"L4-INPUT image_shape={getattr(image, 'shape', None)} dtype={getattr(image, 'dtype', None)} contiguous={image_contiguous} min={image_min} max={image_max} nbytes={getattr(image, 'nbytes', None)}"
        )
        faces = None
        probe_errors = []
        final_image = image
        for probe_name, probe_image in self._iter_probe_variants(image):
            try:
                self._log_stage(
                    f"L4-PROBE {probe_name} image_shape={getattr(probe_image, 'shape', None)} dtype={getattr(probe_image, 'dtype', None)} contiguous={bool(getattr(getattr(probe_image, 'flags', None), 'c_contiguous', False))}"
                )
                faces = self.model.get(probe_image)
                if faces is None:
                    self._log_stage(f"L4-PROBE {probe_name} NO_FACE faces=0")
                    faces = []
                else:
                    self._log_stage(f"L4-PROBE {probe_name} OK faces={len(faces)}")
                final_image = probe_image
                break
            except Exception as exc:
                code = self._classify_model_get_error(exc)
                probe_errors.append(f"{probe_name}:{code}")
                self._log_stage(f"L4-PROBE {probe_name} FAIL {code}")
                self._log_stage(f"L4-PROBE {probe_name} TRACEBACK\n{traceback.format_exc().rstrip()}")
        if faces is None:
            detail = " | ".join(probe_errors[:8]) if probe_errors else "no probe details"
            raise RuntimeError(
                f"LOCAL_RECO_FAIL: model.get 影像資料格式錯誤，root={self.model_root}, name={self.model_name}, probes={detail}"
            )
        return final_image, faces

    def recognition(self, image):
        _, faces = self._run_model_get(image)
        results = []
        for face in faces:
            embedding = getattr(face, "embedding", None)
            if embedding is None and isinstance(face, dict):
                embedding = face.get("embedding")
            if embedding is None:
                continue
            try:
                emb = np.array(embedding).reshape((1, -1))
            except Exception:
                continue
            emb = _normalize_embedding(emb)
            user_name = "unknown"
            minimum_dist = self.threshold
            for saved_face in self.faces_embedding:
                feature = saved_face.get("feature") if isinstance(saved_face, dict) else None
                if feature is None:
                    continue
                diff = np.subtract(emb, feature)
                dist = np.sum(np.square(diff), 1)
                if dist < minimum_dist:
                    minimum_dist = dist
                    user_name = saved_face.get("user_name", "unknown") if isinstance(saved_face, dict) else "unknown"
                    try:
                        face["name"] = user_name
                    except Exception:
                        setattr(face, "name", user_name)
            results.append(user_name)
        return results, faces

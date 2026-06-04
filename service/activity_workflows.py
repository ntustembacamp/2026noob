import asyncio
import csv
import contextlib
import json
import hashlib
import threading
import os
import re
import shutil
import tempfile
import subprocess
import ctypes
from uuid import uuid4
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

import pandas as pd
from PIL import Image
from PIL.ExifTags import TAGS
try:
    from pillow_heif import register_heif_opener
except Exception:
    register_heif_opener = None

try:
    from new_recognize import activity_photo_reco, image_socre
except Exception:
    activity_photo_reco = None
    image_socre = None
try:
    from tools.mysql_utils import mysqlconnector
except Exception:
    mysqlconnector = None

try:
    from filebrowser_client import FilebrowserClient
except ImportError:  # pragma: no cover
    FilebrowserClient = None


BASE_DIR = Path(__file__).resolve().parent.parent
ACTIVITY_IMPORT_ROOT = BASE_DIR / "database" / "activity_import_tmp"
LOG_ROOT = BASE_DIR / "logs"
WINDOWS_BATCH_NORMALIZE_LOG_PATH = LOG_ROOT / "windows_batch_normalize.log"
FILEBROWSER_URL = "http://192.168.0.180:8080/"
FILEBROWSER_USER = "admin"
FILEBROWSER_PASSWORD = "admin"
REMOTE_ACTIVITY_ROOT = "/dev"
RUNTIME_ACTIVITY_ROOT = "/mnt/activity/dev"
RUNTIME_ACTIVITY_PATH = Path(RUNTIME_ACTIVITY_ROOT)
LOCAL_TZ = ZoneInfo("Asia/Taipei") if ZoneInfo else None
MOJIBAKE_PATTERNS = ("ä", "å", "ç", "è", "é", "æ", "Ã", "Â", "ðŸ", "ï¼", "ï½", "???", "�")
IMPORT_JOBS: dict[str, dict] = {}
IMPORT_JOBS_LOCK = threading.Lock()
NORMALIZE_JOBS: dict[str, dict] = {}
NORMALIZE_JOBS_LOCK = threading.Lock()
TABLES_READY = False
TABLES_READY_LOCK = threading.Lock()


def _now_local() -> datetime:
    if LOCAL_TZ is not None:
        return datetime.now(LOCAL_TZ)
    return datetime.utcnow() + timedelta(hours=8)


def _file_time_to_taipei_naive(epoch_seconds: float) -> datetime:
    dt_utc = datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)
    if LOCAL_TZ is not None:
        return dt_utc.astimezone(LOCAL_TZ).replace(tzinfo=None)
    return (dt_utc + timedelta(hours=8)).replace(tzinfo=None)


def _daily_log_path(prefix: str) -> Path:
    return LOG_ROOT / f"{prefix}_{_now_local().strftime('%Y%m%d')}.log"


def _update_log_compat_pointer(pointer_path: Path, latest_log_path: Path):
    pointer_path.parent.mkdir(parents=True, exist_ok=True)
    pointer_path.write_text(
        f"latest_daily_log={latest_log_path}\nupdated_at={_now_local().isoformat(sep=' ', timespec='seconds')}\n",
        encoding="utf-8-sig",
        newline="\n",
    )


def _activity_photo_import_daily_log_path() -> Path:
    daily_path = _daily_log_path("activity_photo_import")
    _update_log_compat_pointer(LOG_ROOT / "activity_photo_import.log", daily_path)
    return daily_path


def _activity_photo_normalize_daily_log_path() -> Path:
    daily_path = _daily_log_path("activity_photo_normalize")
    _update_log_compat_pointer(LOG_ROOT / "activity_photo_normalize.log", daily_path)
    return daily_path


def _resolve_runtime_dir(path_value: str):
    if not path_value:
        return None
    text = str(path_value).strip()
    if not text:
        return None
    # Windows 本機路徑直接使用；容器環境轉換成對應掛載路徑。
    if os.name == "nt":
        return Path(os.path.normpath(text))

    normalized = text.replace("\\", "/")
    low = normalized.lower()
    if low.startswith("c:/activity"):
        prefix = "c:/activity"
        normalized = "/mnt/activity" + normalized[len(prefix):]
    elif low.startswith("c:/feature_src"):
        prefix = "c:/feature_src"
        normalized = "/mnt/feature_src" + normalized[len(prefix):]
    elif low.startswith("c:/uploadsource"):
        # 舊路徑相容：仍接受 C:\uploadsource，但一律映射到 server 集中目錄 C:\activity\ingest\uploadsource
        prefix = "c:/uploadsource"
        normalized = "/mnt/activity/ingest/uploadsource" + normalized[len(prefix):]
    return Path(os.path.normpath(normalized))


def _assert_activity_ingest_root(path_value: str):
    text = str(path_value or "").strip().replace("/", "\\").lower()
    if not text:
        return
    if text.startswith(r"c:\uploadsource"):
        raise ValueError("請改用 C:\\activity\\ingest 目錄，不可混用舊路徑 C:\\uploadsource。")


def _copy_with_unique_name(source: Path, target_dir: Path):
    target_dir.mkdir(parents=True, exist_ok=True)
    candidate = target_dir / source.name
    if not candidate.exists():
        shutil.copy2(source, candidate)
        return candidate
    stem = source.stem
    suffix = source.suffix
    for index in range(1, 10000):
        candidate = target_dir / f"{stem}_{index}{suffix}"
        if not candidate.exists():
            shutil.copy2(source, candidate)
            return candidate
    raise RuntimeError("無法建立唯一檔名，請檢查目標資料夾是否有大量重複檔案。")


def _move_with_unique_name(source: Path, target_dir: Path):
    target_dir.mkdir(parents=True, exist_ok=True)
    candidate = target_dir / source.name
    if not candidate.exists():
        shutil.move(str(source), str(candidate))
        return candidate
    stem = source.stem
    suffix = source.suffix
    for index in range(1, 10000):
        candidate = target_dir / f"{stem}_{index}{suffix}"
        if not candidate.exists():
            shutil.move(str(source), str(candidate))
            return candidate
    raise RuntimeError("無法搬移檔案：目標資料夾中存在過多重複檔名。")


def _mkdir_with_status(target_dir: Path) -> str:
    existed = target_dir.exists()
    target_dir.mkdir(parents=True, exist_ok=True)
    return "existed" if existed else "created"


def _cleanup_temp_dir(target_dir: Path | None, root_dir: Path) -> bool:
    if target_dir is None:
        return True
    try:
        root_resolved = root_dir.resolve()
        target_resolved = target_dir.resolve()
    except Exception:
        return False
    try:
        target_resolved.relative_to(root_resolved)
    except Exception:
        return False
    if target_resolved == root_resolved:
        return False
    with contextlib.suppress(FileNotFoundError):
        shutil.rmtree(target_resolved)
    return not target_resolved.exists()


def _cleanup_temp_dir_with_log(target_dir: Path | None, root_dir: Path, log_path: Path, label: str) -> bool:
    cleaned = _cleanup_temp_dir(target_dir, root_dir)
    if target_dir is None:
        return True
    if cleaned:
        append_log_line(log_path, f"{_now_local().isoformat(sep=' ', timespec='seconds')} | CLEANUP | {label} | {target_dir}")
    else:
        append_log_line(log_path, f"{_now_local().isoformat(sep=' ', timespec='seconds')} | CLEANUP_FAILED | {label} | {target_dir}")
    return cleaned


def _preserve_file_times(source_file: Path, target_file: Path):
    src_stat = source_file.stat()
    os.utime(target_file, ns=(src_stat.st_atime_ns, src_stat.st_mtime_ns))

    if os.name != "nt":
        return

    FILE_WRITE_ATTRIBUTES = 0x0100
    OPEN_EXISTING = 3

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]

    def to_filetime(timestamp_ns: int) -> FILETIME:
        value = int(timestamp_ns // 100) + 116444736000000000
        return FILETIME(value & 0xFFFFFFFF, value >> 32)

    handle = ctypes.windll.kernel32.CreateFileW(
        str(target_file),
        FILE_WRITE_ATTRIBUTES,
        0,
        None,
        OPEN_EXISTING,
        0,
        None,
    )
    if handle in (0, ctypes.c_void_p(-1).value):
        return
    try:
        creation_time = to_filetime(src_stat.st_ctime_ns)
        ctypes.windll.kernel32.SetFileTime(handle, ctypes.byref(creation_time), None, None)
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _msg(text: str) -> str:
    if "\\u" in text:
        with contextlib.suppress(Exception):
            return text.encode("utf-8").decode("unicode_escape")
    return text


def _display_text(text: str) -> str:
    return normalize_error_text(text)


def normalize_error_text(text) -> str:
    if text is None:
        return ""
    if isinstance(text, str):
        raw = text
    else:
        try:
            raw = json.dumps(text, ensure_ascii=False)
        except Exception:
            raw = str(text)

    repaired = _repair_mojibake_fragments(raw)
    return unicodedata.normalize("NFC", repaired)


def _repair_mojibake_text(text: str) -> str:
    raw = str(text or "")
    if not raw:
        return raw
    candidate = raw
    with contextlib.suppress(Exception):
        repaired = raw.encode("latin1").decode("utf-8")
        if repaired:
            candidate = repaired
    return unicodedata.normalize("NFC", candidate)


def _repair_mojibake_fragments(text: str) -> str:
    raw = str(text or "")
    if not raw:
        return raw

    def _convert(match):
        fragment = match.group(0)
        try:
            return fragment.encode("latin1").decode("utf-8")
        except Exception:
            return fragment

    # 只轉換疑似「UTF-8 位元組被當成 latin1 顯示」的片段，避免誤傷正常繁中。
    repaired = raw
    if _looks_like_mojibake(raw):
        repaired = re.sub(r"(?:[\u00C2-\u00F4][\u0080-\u00BF]+)+", _convert, raw)
    return unicodedata.normalize("NFC", repaired)


def _looks_like_mojibake(text: str) -> bool:
    if not text:
        return False
    value = str(text)
    return any(pattern in value for pattern in MOJIBAKE_PATTERNS)


def write_log_file(log_path: Path, lines: list[str]):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    normalized_lines = [normalize_error_text(line) for line in (lines or [])]
    marker = f"--- run start {_now_local().isoformat(sep=' ', timespec='seconds')} ---"
    with log_path.open("a", encoding="utf-8-sig", newline="\n") as handle:
        handle.write("\n".join([marker, *normalized_lines]))
        handle.write("\n")


def append_log_line(log_path: Path, line: str):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8-sig", newline="\n") as handle:
        handle.write(normalize_error_text(line) + "\n")


def _make_import_job_log_path(job_id: str) -> Path:
    return LOG_ROOT / f"activity_photo_import_{job_id}.log"


def _make_normalize_job_log_path(job_id: str) -> Path:
    return LOG_ROOT / f"activity_photo_normalize_job_{job_id}.log"


def _create_import_job(job_id: str, payload: dict):
    db, cursor = _db_cursor()
    try:
        cursor.execute(
            """
            INSERT INTO activity_import_job (
                job_id, job_type, status, total_count, processed_count, success_count, failed_count, skipped_count,
                moved_success_count, moved_fail_count, moved_duplicate_count, source_folder, output_folder, backup_folder,
                device_id, laptop_number, schedule_id, photographer, enable_pyiqa, normalize_mode, started_at, finished_at,
                updated_at, error_summary, log_path, fail_csv_path, fail_csv_created,
                resolved_source_dir, resolved_success_dir, resolved_fail_dir, resolved_duplicate_dir,
                remaining_in_source_count, remaining_in_source_files
            ) VALUES (
                %s, 'import_reco', 'QUEUED', 0, 0, 0, 0, 0,
                0, 0, 0, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, NOW(), NULL,
                NOW(), '', %s, '', 0, '', '', '', '', 0, ''
            )
            """,
            (
                job_id,
                payload.get("source_folder", ""),
                payload.get("output_folder", ""),
                payload.get("backup_folder", ""),
                payload.get("device_id", payload.get("laptop_number", "")),
                payload.get("laptop_number", ""),
                payload.get("schedule_id"),
                payload.get("photographer", ""),
                1 if payload.get("enable_pyiqa") else 0,
                payload.get("normalize_mode", "schedule"),
                str(_make_import_job_log_path(job_id)),
            ),
        )
        db.conn.commit()
    finally:
        cursor.close()
        db.close()


def _create_normalize_job(job_id: str, payload: dict):
    db, cursor = _db_cursor()
    try:
        cursor.execute(
            """
            INSERT INTO activity_import_job (
                job_id, job_type, status, total_count, processed_count, success_count, failed_count, skipped_count,
                moved_success_count, moved_fail_count, moved_duplicate_count, source_folder, output_folder, backup_folder,
                device_id, laptop_number, schedule_id, photographer, enable_pyiqa, normalize_mode, started_at, finished_at,
                updated_at, error_summary, log_path, fail_csv_path, fail_csv_created,
                resolved_source_dir, resolved_success_dir, resolved_fail_dir, resolved_duplicate_dir,
                remaining_in_source_count, remaining_in_source_files
            ) VALUES (
                %s, 'normalize', 'QUEUED', 0, 0, 0, 0, 0,
                0, 0, 0, %s, %s, %s,
                %s, %s, %s, %s, 0, %s, NOW(), NULL,
                NOW(), '', %s, '', 0, '', '', '', '', 0, ''
            )
            """,
            (
                job_id,
                payload.get("source_folder", ""),
                payload.get("output_folder", ""),
                "",
                payload.get("device_id", payload.get("laptop_number", "")),
                payload.get("laptop_number", ""),
                payload.get("schedule_id"),
                payload.get("photographer", ""),
                payload.get("normalize_mode", "exif"),
                str(_make_normalize_job_log_path(job_id)),
            ),
        )
        db.conn.commit()
    finally:
        cursor.close()
        db.close()


def _update_import_job(job_id: str, **fields):
    if not fields:
        return
    allowed = {
        "status", "total_count", "processed_count", "success_count", "failed_count", "skipped_count",
        "moved_success_count", "moved_fail_count", "moved_duplicate_count", "finished_at", "error_summary",
        "fail_csv_path", "fail_csv_created",
        "resolved_source_dir", "resolved_success_dir", "resolved_fail_dir", "resolved_duplicate_dir",
        "remaining_in_source_count", "remaining_in_source_files",
    }
    set_parts = []
    values = []
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key == "finished_at" and value:
            set_parts.append("finished_at = %s")
            values.append(value)
        else:
            set_parts.append(f"{key} = %s")
            values.append(value)
    if not set_parts:
        return
    set_parts.append("updated_at = NOW()")
    db, cursor = _db_cursor()
    try:
        cursor.execute(f"UPDATE activity_import_job SET {', '.join(set_parts)} WHERE job_id = %s", (*values, job_id))
        db.conn.commit()
    finally:
        cursor.close()
        db.close()


def _append_import_job_item(job_id: str, item: dict):
    db, cursor = _db_cursor()
    try:
        cursor.execute(
            """
            INSERT INTO activity_import_job_item (job_id, seq_no, filename, photo_uuid, status, stage, error_reason, moved_to, reason_code, move_result, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            """,
            (
                job_id,
                int(item.get("seq_no", 0)),
                item.get("filename", ""),
                item.get("photo_uuid", ""),
                item.get("status", ""),
                item.get("stage", "import_reco"),
                item.get("error_reason", ""),
                item.get("moved_to", ""),
                item.get("reason_code", ""),
                item.get("move_result", ""),
            ),
        )
        db.conn.commit()
    finally:
        cursor.close()
        db.close()


def get_import_job_status(job_id: str):
    db, cursor = _db_cursor()
    try:
        cursor.execute("SELECT * FROM activity_import_job WHERE job_id = %s LIMIT 1", (job_id,))
        row = cursor.fetchone()
        if not row:
            return None
        for key in ("started_at", "finished_at", "updated_at"):
            if row.get(key) is not None and hasattr(row[key], "isoformat"):
                row[key] = row[key].isoformat(sep=" ", timespec="seconds")
        row["fail_csv_created"] = bool(row.get("fail_csv_created"))
        return row
    finally:
        cursor.close()
        db.close()


def get_import_job_items(job_id: str):
    db, cursor = _db_cursor()
    try:
        cursor.execute(
            """
            SELECT job_id, seq_no, filename, photo_uuid, status, stage, error_reason, moved_to, reason_code, move_result, updated_at
            FROM activity_import_job_item
            WHERE job_id = %s
            ORDER BY seq_no ASC
            """,
            (job_id,),
        )
        rows = cursor.fetchall() or []
        for row in rows:
            if row.get("updated_at") is not None and hasattr(row["updated_at"], "isoformat"):
                row["updated_at"] = row["updated_at"].isoformat(sep=" ", timespec="seconds")
        return {"job_id": job_id, "items": rows}
    finally:
        cursor.close()
        db.close()


def get_import_job_logs(job_id: str, offset: int = 0):
    db, cursor = _db_cursor()
    try:
        cursor.execute("SELECT log_path FROM activity_import_job WHERE job_id = %s LIMIT 1", (job_id,))
        row = cursor.fetchone()
        if not row:
            return None
        log_path = Path(str(row.get("log_path", "")))
    finally:
        cursor.close()
        db.close()
    if not log_path.exists():
        return {"job_id": job_id, "lines": [], "next_offset": 0}
    lines = log_path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    safe_offset = max(0, int(offset or 0))
    return {"job_id": job_id, "lines": lines[safe_offset:], "next_offset": len(lines)}


def write_failure_csv(csv_path: Path, rows: list[dict]):
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["job_id", "filename", "reason_code", "reason", "source_path", "fail_path", "timestamp"],
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _db_cursor():
    if mysqlconnector is None:
        raise RuntimeError(_msg("\\u672c\\u6a5f\\u74b0\\u5883\\u7f3a\\u5c11 MySQL \\u5957\\u4ef6\\uff0c\\u7121\\u6cd5\\u5b58\\u53d6\\u8cc7\\u6599\\u5eab\\u3002"))
    db = mysqlconnector()
    db.connect()
    return db, db.conn.cursor(dictionary=True)


def _read_tabular_file(file_path: str, sheet_name: str = ""):
    suffix = Path(file_path).suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(file_path)
    target_sheet = sheet_name or 0
    return pd.read_excel(file_path, sheet_name=target_sheet, engine="openpyxl")


def list_sheet_names(file_path: str):
    suffix = Path(file_path).suffix.lower()
    if suffix == ".csv":
        return ["CSV"]
    excel = pd.ExcelFile(file_path, engine="openpyxl")
    return excel.sheet_names


def load_columns(file_path: str, sheet_name: str = ""):
    selected_sheet = "" if sheet_name == "CSV" else sheet_name
    return [str(col) for col in _read_tabular_file(file_path, selected_sheet).columns]


def _safe_filename_part(value):
    text = _repair_mojibake_text(str(value or "")).strip()
    for char in ["\\", "/", ":", "*", "?", '"', "<", ">", "|"]:
        text = text.replace(char, "_")
    return text.replace(" ", "_")


def _safe_device_token(value: str, default: str = "NODEV") -> str:
    token = _safe_filename_part(str(value or "").strip().upper())
    if not token:
        return default
    return token[:24]


def _extract_photo_taken_time(image: Image.Image):
    exif_data = image.getexif()
    if not exif_data:
        return None, None

    with contextlib.suppress(Exception):
        exif_ifd = exif_data.get_ifd(0x8769)
        if exif_ifd:
            for key, value in exif_ifd.items():
                if TAGS.get(key, key) == "DateTimeOriginal" and value:
                    return datetime.strptime(value, "%Y:%m:%d %H:%M:%S"), exif_data

    for key, value in exif_data.items():
        tag_name = TAGS.get(key, key)
        if tag_name in ("DateTimeOriginal", "DateTimeDigitized", "DateTime") and value:
            with contextlib.suppress(ValueError, TypeError):
                return datetime.strptime(value, "%Y:%m:%d %H:%M:%S"), exif_data

    return None, exif_data


def _save_jpeg(image: Image.Image, target_path: Path, exif_data):
    save_kwargs = {"format": "JPEG"}
    if exif_data:
        save_kwargs["exif"] = exif_data.tobytes()
    image.save(target_path, **save_kwargs)


def _open_activity_image(source_path: Path) -> Image.Image:
    if source_path.suffix.lower() == ".heic":
        if register_heif_opener is None:
            raise RuntimeError(_msg("\\u8b80\\u53d6 HEIC \\u9700\\u8981 pillow-heif \\u5957\\u4ef6\\uff0c\\u8acb\\u5148\\u5b89\\u88dd\\u5f8c\\u91cd\\u8a66\\u3002"))
        register_heif_opener()
    return Image.open(source_path)


async def _upload_filebrowser(local_path: str, remote_path: str):
    if FilebrowserClient is None:
        raise RuntimeError(_msg("\\u7f3a\\u5c11 filebrowser-client \\u5957\\u4ef6\\uff0c\\u8acb\\u5148\\u91cd\\u5efa noob \\u5bb9\\u5668\\u3002"))

    client = FilebrowserClient(FILEBROWSER_URL, FILEBROWSER_USER, FILEBROWSER_PASSWORD)
    await client.connect()
    await client.upload(local_path=local_path, remote_path=remote_path, override=True, concurrent=10)


async def _store_activity_file(local_path: Path, category: str):
    runtime_dir = RUNTIME_ACTIVITY_PATH / category
    runtime_dir.mkdir(parents=True, exist_ok=True)
    runtime_target = runtime_dir / local_path.name

    try:
        shutil.copy2(local_path, runtime_target)
        return f"{RUNTIME_ACTIVITY_ROOT}/{category}/{local_path.name}", "local"
    except Exception as local_error:
        if FilebrowserClient is None:
            raise RuntimeError(
                _msg("\\u7f3a\\u5c11 filebrowser-client \\u5957\\u4ef6\\uff0c\\u4e14\\u7121\\u6cd5\\u76f4\\u63a5\\u5beb\\u5165 /mnt/activity\\uff0c\\u8acb\\u6aa2\\u67e5\\u639b\\u8f09\\u8def\\u5f91\\u3002")
            ) from local_error

        try:
            await _upload_filebrowser(str(local_path), f"{REMOTE_ACTIVITY_ROOT}/{category}/")
            return f"{RUNTIME_ACTIVITY_ROOT}/{category}/{local_path.name}", "filebrowser"
        except Exception as upload_error:
            raise RuntimeError(
                _msg(
                    f"\\u6a94\\u6848\\u5132\\u5b58\\u5931\\u6557\\uff1a\\u7121\\u6cd5\\u76f4\\u63a5\\u5beb\\u5165 /mnt/activity/dev/{category}\\uff0c"
                    f"\\u4e14 filebrowser \\u4e0a\\u50b3\\u5931\\u6557 ({upload_error})"
                )
            ) from upload_error


def ensure_activity_tables():
    db, cursor = _db_cursor()
    try:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS activity_schedule (
                id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                activity_code VARCHAR(16) NOT NULL,
                activity_date DATE NOT NULL,
                activity_time TIME NULL,
                activity_content VARCHAR(255) NOT NULL,
                owner_team VARCHAR(255) DEFAULT '',
                location VARCHAR(255) DEFAULT '',
                note TEXT NULL,
                create_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                update_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uq_activity_schedule (
                    activity_date,
                    activity_time,
                    activity_content(128)
                ),
                UNIQUE KEY uq_activity_schedule_code (activity_code)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        cursor.execute("SHOW COLUMNS FROM activity_schedule")
        schedule_cols = {row["Field"] for row in cursor.fetchall()}
        if "activity_code" not in schedule_cols:
            cursor.execute("ALTER TABLE activity_schedule ADD COLUMN activity_code VARCHAR(16) NULL")
            with contextlib.suppress(Exception):
                cursor.execute("ALTER TABLE activity_schedule ADD UNIQUE KEY uq_activity_schedule_code (activity_code)")
        cursor.execute(
            """
            SELECT id
            FROM activity_schedule
            WHERE activity_code IS NULL OR TRIM(activity_code) = ''
            ORDER BY activity_date ASC, activity_time ASC, id ASC
            """
        )
        missing_code_rows = cursor.fetchall() or []
        if missing_code_rows:
            cursor.execute(
                """
                SELECT activity_code
                FROM activity_schedule
                WHERE activity_code LIKE 'A%'
                """
            )
            existing_codes = [str(row.get("activity_code") or "").strip().upper() for row in (cursor.fetchall() or [])]
            max_code_num = 2600
            for code in existing_codes:
                if code.startswith("A") and code[1:].isdigit():
                    max_code_num = max(max_code_num, int(code[1:]))
            for row in missing_code_rows:
                max_code_num += 1
                cursor.execute(
                    "UPDATE activity_schedule SET activity_code = %s WHERE id = %s",
                    (f"A{max_code_num}", int(row["id"])),
                )
        with contextlib.suppress(Exception):
            cursor.execute("ALTER TABLE activity_schedule MODIFY COLUMN activity_code VARCHAR(16) NOT NULL")
        if "photographer" in schedule_cols:
            with contextlib.suppress(Exception):
                cursor.execute("ALTER TABLE activity_schedule DROP INDEX uq_activity_schedule")
            with contextlib.suppress(Exception):
                cursor.execute("ALTER TABLE activity_schedule DROP COLUMN photographer")
            with contextlib.suppress(Exception):
                cursor.execute(
                    """
                    ALTER TABLE activity_schedule
                    ADD UNIQUE KEY uq_activity_schedule (activity_date, activity_time, activity_content(128))
                    """
                )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS photographer_master (
                id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                photographer_name VARCHAR(255) NOT NULL,
                note TEXT NULL,
                create_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                update_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uq_photographer_name (photographer_name(128))
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS img_upload (
                id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                origin_full_path VARCHAR(400) NOT NULL,
                thumbs_full_path VARCHAR(400) NOT NULL,
                schedule_id INT NULL,
                human_activity_date DATE NULL,
                human_activity_time TIME NULL,
                human_activity_name VARCHAR(255) DEFAULT '',
                human_owner_team VARCHAR(255) DEFAULT '',
                human_location VARCHAR(255) DEFAULT '',
                human_laptop_number VARCHAR(64) DEFAULT '',
                human_photographer VARCHAR(255) DEFAULT '',
                human_photo_time DATETIME NULL,
                photo_uuid VARCHAR(64) NULL,
                photo_taken_time DATETIME NULL,
                photo_file_time DATETIME NULL,
                taken_time_source VARCHAR(16) NOT NULL DEFAULT 'NONE',
                reco_status VARCHAR(16) NOT NULL DEFAULT 'PENDING',
                reco_error TEXT NULL,
                reco_last_try_time DATETIME NULL,
                reco_retry_count INT NOT NULL DEFAULT 0,
                img_score FLOAT NULL,
                create_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                update_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uq_img_upload_path (origin_full_path(255), thumbs_full_path(255)),
                UNIQUE KEY uq_img_upload_photo_uuid (photo_uuid),
                KEY idx_img_upload_schedule (schedule_id),
                KEY idx_img_upload_photo_time (human_photo_time),
                KEY idx_img_upload_taken_time (photo_taken_time),
                KEY idx_img_upload_file_time (photo_file_time)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        cursor.execute("SHOW COLUMNS FROM img_upload")
        existing_cols = {row["Field"] for row in cursor.fetchall()}
        if "photo_uuid" not in existing_cols:
            cursor.execute("ALTER TABLE img_upload ADD COLUMN photo_uuid VARCHAR(64) NULL")
        if "photo_taken_time" not in existing_cols:
            cursor.execute("ALTER TABLE img_upload ADD COLUMN photo_taken_time DATETIME NULL")
        if "photo_file_time" not in existing_cols:
            cursor.execute("ALTER TABLE img_upload ADD COLUMN photo_file_time DATETIME NULL")
        if "taken_time_source" not in existing_cols:
            cursor.execute("ALTER TABLE img_upload ADD COLUMN taken_time_source VARCHAR(16) NOT NULL DEFAULT 'NONE'")
        if "reco_status" not in existing_cols:
            cursor.execute("ALTER TABLE img_upload ADD COLUMN reco_status VARCHAR(16) NOT NULL DEFAULT 'PENDING'")
        if "reco_error" not in existing_cols:
            cursor.execute("ALTER TABLE img_upload ADD COLUMN reco_error TEXT NULL")
        if "reco_last_try_time" not in existing_cols:
            cursor.execute("ALTER TABLE img_upload ADD COLUMN reco_last_try_time DATETIME NULL")
        if "reco_retry_count" not in existing_cols:
            cursor.execute("ALTER TABLE img_upload ADD COLUMN reco_retry_count INT NOT NULL DEFAULT 0")
        with contextlib.suppress(Exception):
            cursor.execute("ALTER TABLE img_upload ADD UNIQUE KEY uq_img_upload_photo_uuid (photo_uuid)")
        with contextlib.suppress(Exception):
            cursor.execute("ALTER TABLE img_upload ADD KEY idx_img_upload_taken_time (photo_taken_time)")
        with contextlib.suppress(Exception):
            cursor.execute("ALTER TABLE img_upload ADD KEY idx_img_upload_file_time (photo_file_time)")
        cursor.execute("SHOW TABLES LIKE 'reco_result'")
        reco_table = cursor.fetchone()
        if reco_table:
            cursor.execute("SHOW COLUMNS FROM reco_result")
            reco_cols = {row["Field"] for row in cursor.fetchall()}
            if "photo_uuid" not in reco_cols:
                cursor.execute("ALTER TABLE reco_result ADD COLUMN photo_uuid VARCHAR(64) NULL")
            if "photo_file_time" not in reco_cols:
                cursor.execute("ALTER TABLE reco_result ADD COLUMN photo_file_time DATETIME NULL")
            if "taken_time_source" not in reco_cols:
                cursor.execute("ALTER TABLE reco_result ADD COLUMN taken_time_source VARCHAR(16) NOT NULL DEFAULT 'NONE'")
            with contextlib.suppress(Exception):
                cursor.execute("ALTER TABLE reco_result ADD KEY idx_reco_result_photo_uuid (photo_uuid)")
            with contextlib.suppress(Exception):
                cursor.execute("ALTER TABLE reco_result ADD KEY idx_reco_result_photo_file_time (photo_file_time)")
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS activity_import_job (
                job_id VARCHAR(64) NOT NULL PRIMARY KEY,
                job_type VARCHAR(32) NOT NULL DEFAULT 'import_reco',
                status VARCHAR(16) NOT NULL DEFAULT 'QUEUED',
                total_count INT NOT NULL DEFAULT 0,
                processed_count INT NOT NULL DEFAULT 0,
                success_count INT NOT NULL DEFAULT 0,
                failed_count INT NOT NULL DEFAULT 0,
                skipped_count INT NOT NULL DEFAULT 0,
                moved_success_count INT NOT NULL DEFAULT 0,
                moved_fail_count INT NOT NULL DEFAULT 0,
                moved_duplicate_count INT NOT NULL DEFAULT 0,
                source_folder VARCHAR(400) DEFAULT '',
                output_folder VARCHAR(400) DEFAULT '',
                backup_folder VARCHAR(400) DEFAULT '',
                device_id VARCHAR(64) DEFAULT '',
                laptop_number VARCHAR(64) DEFAULT '',
                schedule_id INT NULL,
                photographer VARCHAR(255) DEFAULT '',
                enable_pyiqa TINYINT(1) NOT NULL DEFAULT 0,
                normalize_mode VARCHAR(32) DEFAULT 'schedule',
                started_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                finished_at DATETIME NULL,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                error_summary TEXT NULL,
                log_path VARCHAR(500) DEFAULT '',
                fail_csv_path VARCHAR(500) DEFAULT '',
                fail_csv_created TINYINT(1) NOT NULL DEFAULT 0,
                resolved_source_dir VARCHAR(500) DEFAULT '',
                resolved_success_dir VARCHAR(500) DEFAULT '',
                resolved_fail_dir VARCHAR(500) DEFAULT '',
                resolved_duplicate_dir VARCHAR(500) DEFAULT '',
                remaining_in_source_count INT NOT NULL DEFAULT 0,
                remaining_in_source_files TEXT NULL,
                KEY idx_activity_import_job_status (status),
                KEY idx_activity_import_job_updated_at (updated_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        cursor.execute("SHOW COLUMNS FROM activity_import_job")
        job_cols = {row["Field"] for row in cursor.fetchall()}
        if "resolved_source_dir" not in job_cols:
            cursor.execute("ALTER TABLE activity_import_job ADD COLUMN resolved_source_dir VARCHAR(500) DEFAULT ''")
        if "resolved_success_dir" not in job_cols:
            cursor.execute("ALTER TABLE activity_import_job ADD COLUMN resolved_success_dir VARCHAR(500) DEFAULT ''")
        if "resolved_fail_dir" not in job_cols:
            cursor.execute("ALTER TABLE activity_import_job ADD COLUMN resolved_fail_dir VARCHAR(500) DEFAULT ''")
        if "resolved_duplicate_dir" not in job_cols:
            cursor.execute("ALTER TABLE activity_import_job ADD COLUMN resolved_duplicate_dir VARCHAR(500) DEFAULT ''")
        if "remaining_in_source_count" not in job_cols:
            cursor.execute("ALTER TABLE activity_import_job ADD COLUMN remaining_in_source_count INT NOT NULL DEFAULT 0")
        if "remaining_in_source_files" not in job_cols:
            cursor.execute("ALTER TABLE activity_import_job ADD COLUMN remaining_in_source_files TEXT NULL")
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS activity_import_job_item (
                id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                job_id VARCHAR(64) NOT NULL,
                seq_no INT NOT NULL DEFAULT 0,
                filename VARCHAR(500) DEFAULT '',
                photo_uuid VARCHAR(64) DEFAULT '',
                status VARCHAR(16) NOT NULL DEFAULT 'PENDING',
                stage VARCHAR(32) NOT NULL DEFAULT 'import_reco',
                error_reason TEXT NULL,
                moved_to VARCHAR(500) DEFAULT '',
                reason_code VARCHAR(64) DEFAULT '',
                move_result VARCHAR(16) DEFAULT '',
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                KEY idx_activity_import_job_item_job_seq (job_id, seq_no),
                KEY idx_activity_import_job_item_status (status)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        cursor.execute("SHOW COLUMNS FROM activity_import_job_item")
        item_cols = {row["Field"] for row in cursor.fetchall()}
        if "reason_code" not in item_cols:
            cursor.execute("ALTER TABLE activity_import_job_item ADD COLUMN reason_code VARCHAR(64) DEFAULT ''")
        if "move_result" not in item_cols:
            cursor.execute("ALTER TABLE activity_import_job_item ADD COLUMN move_result VARCHAR(16) DEFAULT ''")
        db.conn.commit()
    finally:
        cursor.close()
        db.close()


def ensure_activity_tables_once():
    global TABLES_READY
    if TABLES_READY:
        return
    with TABLES_READY_LOCK:
        if TABLES_READY:
            return
        ensure_activity_tables()
        TABLES_READY = True


def import_photographer_master(excel_path: str, sheet_name: str, photographer_column: str, note_column: str = ""):
    ensure_activity_tables()
    df = _read_tabular_file(excel_path, "" if sheet_name == "CSV" else sheet_name)
    if photographer_column not in df.columns:
        raise ValueError(_msg("\\u627e\\u4e0d\\u5230\\u651d\\u5f71\\u5e2b\\u6b04\\u4f4d"))
    if note_column and note_column not in df.columns:
        raise ValueError(_msg("\\u627e\\u4e0d\\u5230\\u5099\\u8a3b\\u6b04\\u4f4d"))

    records = []
    for _, row in df.iterrows():
        name = str(row.get(photographer_column, "")).strip()
        if not name:
            continue
        note = ""
        if note_column:
            raw = row.get(note_column, "")
            note = "" if pd.isna(raw) else str(raw).strip()
        records.append({"photographer_name": name, "note": note})
    if not records:
        return {"imported_count": 0, "logs": [_msg("\\u6c92\\u6709\\u53ef\\u532f\\u5165\\u7684\\u651d\\u5f71\\u5e2b\\u8cc7\\u6599")]}

    db, cursor = _db_cursor()
    try:
        cursor.executemany(
            """
            INSERT INTO photographer_master (
                photographer_name, note, create_time, update_time
            ) VALUES (%(photographer_name)s, %(note)s, NOW(), NOW())
            ON DUPLICATE KEY UPDATE
                note = VALUES(note),
                update_time = NOW()
            """,
            records,
        )
        db.conn.commit()
        return {"imported_count": len(records), "logs": [_msg(f"\\u5df2\\u532f\\u5165\\u651d\\u5f71\\u5e2b\\u8cc7\\u6599\\uff0c\\u5171 {len(records)} \\u7b46")]}
    finally:
        cursor.close()
        db.close()


def query_photographer_master(keyword: str = "", limit: int = 200):
    ensure_activity_tables()
    db, cursor = _db_cursor()
    try:
        where = ""
        params = []
        if keyword:
            where = "WHERE photographer_name LIKE %s OR note LIKE %s"
            params = [f"%{keyword}%", f"%{keyword}%"]
        cursor.execute(
            f"""
            SELECT id, photographer_name, note, create_time, update_time
            FROM photographer_master
            {where}
            ORDER BY photographer_name ASC, id ASC
            LIMIT %s
            """,
            (*params, limit),
        )
        rows = cursor.fetchall()
        for row in rows:
            for key in ("create_time", "update_time"):
                if row.get(key) is not None:
                    row[key] = row[key].isoformat(sep=" ", timespec="seconds")
        return rows
    finally:
        cursor.close()
        db.close()


def update_photographer_master(item_id: int, photographer_name: str, note: str = ""):
    ensure_activity_tables()
    name = str(photographer_name or "").strip()
    if not name:
        raise ValueError(_msg("\\u651d\\u5f71\\u5e2b\\u540d\\u7a31\\u4e0d\\u53ef\\u70ba\\u7a7a"))
    db, cursor = _db_cursor()
    try:
        cursor.execute(
            """
            UPDATE photographer_master
            SET photographer_name=%s, note=%s, update_time=NOW()
            WHERE id=%s
            """,
            (name, str(note or "").strip(), item_id),
        )
        db.conn.commit()
        if cursor.rowcount == 0:
            raise ValueError(_msg("\\u627e\\u4e0d\\u5230\\u8981\\u66f4\\u65b0\\u7684\\u651d\\u5f71\\u5e2b\\u8cc7\\u6599"))
        return {"updated": True, "id": item_id}
    finally:
        cursor.close()
        db.close()


def delete_photographer_master(item_id: int):
    ensure_activity_tables()
    db, cursor = _db_cursor()
    try:
        cursor.execute("DELETE FROM photographer_master WHERE id=%s", (item_id,))
        db.conn.commit()
        if cursor.rowcount == 0:
            raise ValueError(_msg("\\u627e\\u4e0d\\u5230\\u8981\\u522a\\u9664\\u7684\\u651d\\u5f71\\u5e2b\\u8cc7\\u6599"))
        return {"deleted": True, "id": item_id}
    finally:
        cursor.close()
        db.close()


def delete_all_photographer_master():
    ensure_activity_tables()
    db, cursor = _db_cursor()
    try:
        cursor.execute("DELETE FROM photographer_master")
        deleted_count = cursor.rowcount
        db.conn.commit()
        return {"deleted": True, "deleted_count": deleted_count}
    finally:
        cursor.close()
        db.close()


def save_uploaded_excel(file_obj, target_dir: Path):
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / Path(file_obj.filename).name
    with target_path.open("wb") as handle:
        handle.write(file_obj.file.read())

    sheet_names = list_sheet_names(str(target_path))
    selected_sheet = "" if target_path.suffix.lower() == ".csv" else sheet_names[0]
    columns = load_columns(str(target_path), selected_sheet or "CSV")
    return {
        "server_path": str(target_path),
        "sheet_names": sheet_names,
        "selected_sheet": selected_sheet or "CSV",
        "columns": columns,
    }


def _generate_next_activity_code(cursor):
    cursor.execute(
        """
        SELECT activity_code
        FROM activity_schedule
        WHERE activity_code LIKE 'A%'
        """
    )
    max_code_num = 2600
    for row in (cursor.fetchall() or []):
        code = str(row.get("activity_code") or "").strip().upper()
        if code.startswith("A") and code[1:].isdigit():
            max_code_num = max(max_code_num, int(code[1:]))
    return f"A{max_code_num + 1}"


def _normalize_activity_code(raw_value, default_code):
    value = str(raw_value or "").strip().upper()
    if not value:
        return default_code
    if value.startswith("A") and value[1:].isdigit():
        return value
    if value.isdigit():
        return f"A{int(value)}"
    return default_code


def import_activity_schedule(excel_path: str, sheet_name: str, column_map: dict):
    ensure_activity_tables()
    df = _read_tabular_file(excel_path, "" if sheet_name == "CSV" else sheet_name)

    raw_records = []
    for _, row in df.iterrows():
        date_value = pd.to_datetime(row[column_map["activity_date"]], errors="coerce")
        if pd.isna(date_value):
            continue

        time_value = None
        if column_map.get("activity_time"):
            raw_time = row[column_map["activity_time"]]
            if pd.notna(raw_time):
                parsed_time = pd.to_datetime(str(raw_time), errors="coerce")
                if not pd.isna(parsed_time):
                    time_value = parsed_time.time()

        def _mapped_text(key: str):
            column_name = column_map.get(key, "")
            if not column_name:
                return ""
            value = row.get(column_name, "")
            return "" if pd.isna(value) else str(value).strip()

        raw_records.append(
            {
                "activity_date": date_value.date(),
                "activity_time": time_value,
                "activity_content": _mapped_text("activity_content"),
                "owner_team": _mapped_text("owner_team"),
                "location": _mapped_text("location"),
                "note": _mapped_text("note"),
                "activity_code_raw": _mapped_text("activity_code"),
            }
        )

    if not raw_records:
        return {"imported_count": 0, "logs": [_msg("\\u6c92\\u6709\\u8b80\\u5230\\u53ef\\u532f\\u5165\\u7684\\u6d3b\\u52d5\\u8cc7\\u6599\\u3002")]}

    db, cursor = _db_cursor()
    try:
        records = []
        cursor.execute("SELECT activity_code FROM activity_schedule")
        used_codes = {str(row.get("activity_code") or "").strip().upper() for row in (cursor.fetchall() or []) if str(row.get("activity_code") or "").strip()}
        next_code = _generate_next_activity_code(cursor)
        next_num = int(next_code[1:])
        for item in raw_records:
            while f"A{next_num}" in used_codes:
                next_num += 1
            default_code = f"A{next_num}"
            final_code = _normalize_activity_code(item.pop("activity_code_raw", ""), default_code)
            if final_code in used_codes:
                final_code = default_code
            used_codes.add(final_code)
            if final_code == default_code:
                next_num += 1
            records.append({**item, "activity_code": final_code})
        sql = """
        INSERT INTO activity_schedule (
            activity_code,
            activity_date,
            activity_time,
            activity_content,
            owner_team,
            location,
            note,
            create_time,
            update_time
        )
        VALUES (
            %(activity_code)s,
            %(activity_date)s,
            %(activity_time)s,
            %(activity_content)s,
            %(owner_team)s,
            %(location)s,
            %(note)s,
            NOW(),
            NOW()
        )
        ON DUPLICATE KEY UPDATE
            activity_code = VALUES(activity_code),
            owner_team = VALUES(owner_team),
            location = VALUES(location),
            note = VALUES(note),
            update_time = NOW()
        """
        cursor.executemany(sql, records)
        db.conn.commit()
        return {
            "imported_count": len(records),
            "logs": [_msg(f"\\u5df2\\u532f\\u5165 activity_schedule\\uff0c\\u5171 {len(records)} \\u7b46\\u3002")],
        }
    finally:
        cursor.close()
        db.close()


def query_activity_schedule(activity_date: str = "", photographer: str = "", keyword: str = "", limit: int = 200):
    ensure_activity_tables()
    db, cursor = _db_cursor()
    conditions = []
    params = []

    if activity_date:
        conditions.append("activity_date = %s")
        params.append(activity_date)
    if keyword:
        conditions.append("(activity_content LIKE %s OR note LIKE %s OR location LIKE %s)")
        params.extend([f"%{keyword}%"] * 3)

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    sql = f"""
    SELECT
        id,
        activity_code,
        activity_date,
        activity_time,
        activity_content,
        owner_team,
        location,
        note,
        create_time,
        update_time
    FROM activity_schedule
    {where_clause}
    ORDER BY activity_date ASC, activity_time ASC, id ASC
    LIMIT %s
    """
    try:
        cursor.execute(sql, (*params, limit))
        rows = cursor.fetchall()
        for row in rows:
            if row.get("activity_date") is not None:
                row["activity_date"] = row["activity_date"].isoformat()
            if row.get("activity_time") is not None:
                time_value = row["activity_time"]
                if isinstance(time_value, timedelta):
                    total_seconds = int(time_value.total_seconds())
                    hours = total_seconds // 3600
                    minutes = (total_seconds % 3600) // 60
                    seconds = total_seconds % 60
                    row["activity_time"] = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
                else:
                    row["activity_time"] = time_value.isoformat()
            row["activity_code"] = str(row.get("activity_code") or "").strip()
            for key in ("create_time", "update_time"):
                if row.get(key) is not None:
                    row[key] = row[key].isoformat(sep=" ", timespec="seconds")
        return rows
    finally:
        cursor.close()
        db.close()


def update_activity_schedule(
    schedule_id: int,
    activity_code: str,
    activity_date: str,
    activity_time: str = "",
    activity_content: str = "",
    owner_team: str = "",
    location: str = "",
    note: str = "",
):
    ensure_activity_tables()
    parsed_date = pd.to_datetime(activity_date, errors="coerce")
    if pd.isna(parsed_date):
        raise ValueError(_msg("\\u8acb\\u8f38\\u5165\\u6709\\u6548\\u7684\\u6d3b\\u52d5\\u65e5\\u671f\\u3002"))

    parsed_time = None
    if str(activity_time or "").strip():
        parsed_time_dt = pd.to_datetime(str(activity_time).strip(), errors="coerce")
        if pd.isna(parsed_time_dt):
            raise ValueError(_msg("\\u8acb\\u8f38\\u5165\\u6709\\u6548\\u7684\\u6d3b\\u52d5\\u6642\\u9593\\u3002"))
        parsed_time = parsed_time_dt.time()

    db, cursor = _db_cursor()
    try:
        cursor.execute("SELECT id FROM activity_schedule WHERE activity_code = %s AND id <> %s LIMIT 1", (str(activity_code or "").strip().upper(), schedule_id))
        if cursor.fetchone():
            raise ValueError(_msg("\\u6d3b\\u52d5\\u7de8\\u865f\\u5df2\\u5b58\\u5728\\uff0c\\u8acb\\u4f7f\\u7528\\u5176\\u4ed6\\u7de8\\u865f\\u3002"))
        cursor.execute(
            """
            UPDATE activity_schedule
            SET
                activity_code = %s,
                activity_date = %s,
                activity_time = %s,
                activity_content = %s,
                owner_team = %s,
                location = %s,
                note = %s,
                update_time = NOW()
            WHERE id = %s
            """,
            (
                str(activity_code or "").strip().upper(),
                parsed_date.date(),
                parsed_time,
                str(activity_content or "").strip(),
                str(owner_team or "").strip(),
                str(location or "").strip(),
                str(note or "").strip(),
                schedule_id,
            ),
        )
        db.conn.commit()
        if cursor.rowcount == 0:
            raise ValueError(_msg("\\u627e\\u4e0d\\u5230\\u8981\\u66f4\\u65b0\\u7684\\u6d3b\\u52d5\\u884c\\u7a0b\\u3002"))
    finally:
        cursor.close()
        db.close()

    return {"updated": True, "schedule_id": schedule_id}


def delete_activity_schedule(schedule_id: int):
    ensure_activity_tables()
    db, cursor = _db_cursor()
    try:
        cursor.execute("DELETE FROM activity_schedule WHERE id = %s", (schedule_id,))
        db.conn.commit()
        if cursor.rowcount == 0:
            raise ValueError(_msg("\\u627e\\u4e0d\\u5230\\u8981\\u522a\\u9664\\u7684\\u6d3b\\u52d5\\u884c\\u7a0b\\u3002"))
    finally:
        cursor.close()
        db.close()
    return {"deleted": True, "schedule_id": schedule_id}


def delete_all_activity_schedule():
    ensure_activity_tables()
    db, cursor = _db_cursor()
    try:
        cursor.execute("DELETE FROM activity_schedule")
        deleted_count = cursor.rowcount
        db.conn.commit()
    finally:
        cursor.close()
        db.close()
    return {"deleted": True, "deleted_count": deleted_count}


def list_activity_schedule_options(limit: int = 300):
    rows = query_activity_schedule(limit=limit)
    results = []
    for idx, row in enumerate(rows):
        date_text = row.get("activity_date") or ""
        start_time = str(row.get("activity_time") or "00:00:00").strip() or "00:00:00"
        end_time = "24:00:00"
        for next_idx in range(idx + 1, len(rows)):
            next_row = rows[next_idx]
            if (next_row.get("activity_date") or "") != date_text:
                break
            next_time = str(next_row.get("activity_time") or "").strip()
            if next_time:
                end_time = next_time
                break
        time_range = f"{start_time}~{end_time}"
        code = str(row.get("activity_code") or "").strip() or "A0000"
        content = row.get("activity_content") or _msg("\\u672a\\u8a2d\\u5b9a\\u5167\\u5bb9")
        results.append(
            {
                "id": row["id"],
                "label": f"{code} {date_text} {time_range} {content}".strip(),
                "activity_code": code,
                "activity_date": date_text,
                "activity_time": row.get("activity_time") or "00:00:00",
                "activity_time_range": time_range,
                "activity_content": row.get("activity_content") or "",
                "owner_team": row.get("owner_team") or "",
                "location": row.get("location") or "",
            }
        )
    return results


def _make_nonexif_filename(schedule_code: str, laptop_number: str, photographer: str, photo_time: datetime, original_name: str):
    stamp = photo_time.strftime("%Y%m%d_%H%M%S")
    stem = Path(original_name).stem
    return "_".join(
        [
            "NONEXIF",
            _safe_filename_part(schedule_code or "000"),
            str(laptop_number).strip(),
            _safe_filename_part(photographer),
            stamp,
            _safe_filename_part(stem),
        ]
    )


def _make_exif_filename(activity_code: str, device_id: str, photographer: str, photo_time: datetime, original_name: str):
    stamp = photo_time.strftime("%Y%m%d_%H%M%S")
    stem = Path(original_name).stem
    return "_".join(
        [
            "EXIF",
            _safe_filename_part(activity_code or "000"),
            _safe_filename_part(device_id),
            _safe_filename_part(photographer),
            stamp,
            _safe_filename_part(stem),
        ]
    )


def _to_datetime_local(text: str):
    value = str(text or "").strip()
    if not value:
        return None
    value = value.replace("/", "-")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        with contextlib.suppress(ValueError):
            return datetime.strptime(value, fmt)
    return None


def _normalize_time_text(value):
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%H:%M:%S")
    if hasattr(value, "hour") and hasattr(value, "minute"):
        second = getattr(value, "second", 0)
        return f"{int(value.hour):02d}:{int(value.minute):02d}:{int(second):02d}"
    if isinstance(value, timedelta):
        total = int(value.total_seconds())
        hours = total // 3600
        minutes = (total % 3600) // 60
        seconds = total % 60
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    text = str(value).strip()
    return text


def _to_datetime_from_day_time(day: str, time_text: str):
    norm_day = str(day or "").strip().replace("/", "-")
    norm_time = _normalize_time_text(time_text)
    if not norm_day:
        return None
    if not norm_time:
        norm_time = "00:00:00"
    if norm_time == "24:00:00":
        base = _to_datetime_local(f"{norm_day} 00:00:00")
        return base + timedelta(days=1) if base else None
    if len(norm_time) == 5 and ":" in norm_time:
        norm_time = f"{norm_time}:00"
    return _to_datetime_local(f"{norm_day} {norm_time}")


def _parse_time_range_for_date(activity_date: str, activity_time: str, activity_time_range: str):
    day = str(activity_date or "").strip().replace("/", "-")
    if not day:
        return None, None
    raw_range = _normalize_time_text(activity_time_range)
    range_text = raw_range
    for token in ("～", "至", "-", "—"):
        range_text = range_text.replace(token, "~")
    if range_text and "~" in range_text:
        start_text, end_text = [part.strip() for part in range_text.split("~", 1)]
    else:
        start_text = _normalize_time_text(activity_time) or "00:00:00"
        end_text = ""
    start_dt = _to_datetime_from_day_time(day, start_text)
    end_dt = _to_datetime_from_day_time(day, end_text) if end_text else None
    if end_dt is None and start_dt is not None:
        end_dt = start_dt + timedelta(hours=1)
    if start_dt and end_dt and end_dt <= start_dt:
        end_dt = start_dt + timedelta(hours=1)
    return start_dt, end_dt


def _find_activity_code_from_payload(target_time: datetime, activities: list[dict]):
    if not target_time or not activities:
        return "000"
    normalized = []
    for item in activities:
        start_dt, end_dt = _parse_time_range_for_date(
            item.get("activity_date"),
            item.get("activity_time"),
            item.get("activity_time_range"),
        )
        if start_dt and end_dt:
            normalized.append((start_dt, end_dt, item))
    normalized.sort(key=lambda x: x[0])
    for start_dt, end_dt, item in normalized:
        if start_dt <= target_time < end_dt:
            code = str(item.get("activity_code") or "").strip()
            return code if code else "000"
    return "000"


def _build_day_range_debug(target_time: datetime, activities: list[dict], limit: int = 5):
    if not target_time or not activities:
        return ""
    day_text = target_time.strftime("%Y-%m-%d")
    candidates = []
    for item in activities:
        if str(item.get("activity_date") or "").strip().replace("/", "-") != day_text:
            continue
        start_dt, end_dt = _parse_time_range_for_date(
            item.get("activity_date"),
            item.get("activity_time"),
            item.get("activity_time_range"),
        )
        if not (start_dt and end_dt):
            continue
        code = str(item.get("activity_code") or "").strip() or "000"
        content = str(item.get("activity_content") or "").strip()
        candidates.append(f"{code}:{start_dt.strftime('%H:%M:%S')}~{end_dt.strftime('%H:%M:%S')}:{content}")
    return " | ".join(candidates[:limit])


def _calculate_photo_uuid(file_path: Path):
    hasher = hashlib.sha256()
    with file_path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


async def process_activity_photo_import(
    laptop_number: str,
    schedule_id: int | None,
    photographer: str,
    enable_pyiqa: bool,
    files,
    normalize_mode: str = "schedule",
    source_folder: str = "",
    output_folder: str = "",
    backup_folder: str = "",
    skip_normalization: bool = False,
):
    ensure_activity_tables()
    schedule_row = None
    db, cursor = _db_cursor()
    try:
        if normalize_mode == "schedule" and not skip_normalization:
            if not schedule_id:
                raise ValueError(_msg("\\u8acb\\u5148\\u9078\\u64c7\\u6d3b\\u52d5\\u884c\\u7a0b\\u3002"))
            cursor.execute(
                """
                SELECT id, activity_code, activity_date, activity_time, activity_content, owner_team, location, note
                FROM activity_schedule
                WHERE id = %s
                """,
                (schedule_id,),
            )
            schedule_row = cursor.fetchone()
            if not schedule_row:
                raise ValueError(_msg("\\u627e\\u4e0d\\u5230\\u6307\\u5b9a\\u7684\\u6d3b\\u52d5\\u884c\\u7a0b\\u3002"))
        elif normalize_mode == "schedule" and skip_normalization and schedule_id:
            cursor.execute(
                """
                SELECT id, activity_code, activity_date, activity_time, activity_content, owner_team, location, note
                FROM activity_schedule
                WHERE id = %s
                """,
                (schedule_id,),
            )
            schedule_row = cursor.fetchone()
    finally:
        cursor.close()
        db.close()

    logs = [_msg(f"{_now_local().isoformat(sep=' ', timespec='seconds')} \\u958b\\u59cb\\u8655\\u7406 {len(files)} \\u5f35\\u7167\\u7247")]
    ACTIVITY_IMPORT_ROOT.mkdir(parents=True, exist_ok=True)
    results = []
    output_dir = _resolve_runtime_dir(output_folder)
    backup_dir = _resolve_runtime_dir(backup_folder)
    if source_folder:
        logs.append(_msg(f"{_now_local().isoformat(sep=' ', timespec='seconds')} \\u4f86\\u6e90\\u8cc7\\u6599\\u593e: {source_folder}"))
    if output_dir:
        logs.append(_msg(f"{_now_local().isoformat(sep=' ', timespec='seconds')} \\u76ee\\u7684\\u8cc7\\u6599\\u593e: {output_dir}"))
    if backup_dir:
        logs.append(_msg(f"{_now_local().isoformat(sep=' ', timespec='seconds')} \\u5099\\u4efd\\u8cc7\\u6599\\u593e: {backup_dir}"))

    for idx, upload in enumerate(files, 1):
        original_name = Path(upload.filename or f"upload_{idx}").name
        job_dir = Path(tempfile.mkdtemp(prefix="activity_", dir=ACTIVITY_IMPORT_ROOT))
        source_path = job_dir / original_name
        try:
            with source_path.open("wb") as handle:
                handle.write(upload.file.read())

            logs.append(_msg(f"{_now_local().isoformat(sep=' ', timespec='seconds')} #{idx:03d} \\u8b80\\u53d6\\u539f\\u59cb\\u6a94\\uff1a{_display_text(original_name)}"))
            image = _open_activity_image(source_path)
            photo_taken_time, exif_data = _extract_photo_taken_time(image)
            if image.mode in ("RGBA", "P"):
                image = image.convert("RGB")
            photo_file_time = _file_time_to_taipei_naive(source_path.stat().st_ctime)
            if (not skip_normalization) and normalize_mode == "exif" and not photo_taken_time:
                logs.append(
                    _msg(
                        f"{_now_local().isoformat(sep=' ', timespec='seconds')} #{idx:03d} "
                        "\\u7121 EXIF\\uff0c\\u8acb\\u6539\\u7528\\u6a21\\u5f0f B\\uff08\\u9078\\u64c7\\u6d3b\\u52d5\\u884c\\u7a0b\\u6b63\\u898f\\u5316\\uff09"
                    )
                )
                continue
            naming_time = photo_taken_time or photo_file_time
            taken_time_source = "EXIF" if photo_taken_time else "FILE_TIME"
            matched_schedule_row = schedule_row

            final_photographer = photographer or ""
            if skip_normalization:
                new_name = Path(source_path.name).stem
                jpeg_path = source_path
                logs.append(_msg(f"{_now_local().isoformat(sep=' ', timespec='seconds')} #{idx:03d} \\u76f4\\u63a5\\u532f\\u5165\\u5df2\\u6b63\\u898f\\u5316\\u6a94\\uff1a{_display_text(jpeg_path.name)}"))
            else:
                if normalize_mode == "exif":
                    inferred_schedule = _find_schedule_by_time(naming_time)
                    inferred_code = str((inferred_schedule or {}).get("activity_code") or "000").strip() or "000"
                    new_name = _make_exif_filename(inferred_code, laptop_number, final_photographer, naming_time, original_name)
                    if inferred_schedule:
                        matched_schedule_row = inferred_schedule
                else:
                    schedule_code = str((schedule_row or {}).get("activity_code") or "000").strip() or "000"
                    new_name = _make_nonexif_filename(schedule_code, laptop_number, final_photographer, naming_time, original_name)
                jpeg_path = job_dir / f"{new_name}.jpeg"
                _save_jpeg(image, jpeg_path, exif_data)
                logs.append(_msg(f"{_now_local().isoformat(sep=' ', timespec='seconds')} #{idx:03d} \\u8f49\\u70ba JPEG\\uff1a{_display_text(jpeg_path.name)}"))
                if output_dir:
                    copied_jpeg = _copy_with_unique_name(jpeg_path, output_dir)
                    logs.append(_msg(f"{_now_local().isoformat(sep=' ', timespec='seconds')} #{idx:03d} \\u6b63\\u898f\\u5316\\u6a94\\u6848\\u5df2\\u8f38\\u51fa\\uff1a{copied_jpeg}"))

            origin_runtime, origin_mode = await _store_activity_file(jpeg_path, "origin")
            if origin_mode == "local":
                logs.append(_msg(f"{_now_local().isoformat(sep=' ', timespec='seconds')} #{idx:03d} \\u539f\\u5716\\u5df2\\u5132\\u5b58\\uff1a{origin_runtime}"))
            else:
                logs.append(_msg(f"{_now_local().isoformat(sep=' ', timespec='seconds')} #{idx:03d} \\u539f\\u5716\\u5df2\\u4e0a\\u50b3\\uff1a{origin_runtime}"))

            width, height = image.size
            max_side = 1024
            scale = min(1.0, max_side / float(max(width, height)))
            new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
            thumb_image = image.resize(new_size, Image.LANCZOS)
            thumb_path = job_dir / f"{new_name}_thumb.jpeg"
            _save_jpeg(thumb_image, thumb_path, exif_data)
            thumbs_runtime, thumbs_mode = await _store_activity_file(thumb_path, "thumbs")
            if thumbs_mode == "local":
                logs.append(_msg(f"{_now_local().isoformat(sep=' ', timespec='seconds')} #{idx:03d} \\u7e2e\\u5716\\u5df2\\u5132\\u5b58\\uff1a{thumbs_runtime}"))
            else:
                logs.append(_msg(f"{_now_local().isoformat(sep=' ', timespec='seconds')} #{idx:03d} \\u7e2e\\u5716\\u5df2\\u4e0a\\u50b3\\uff1a{thumbs_runtime}"))

            img_score = None
            if enable_pyiqa:
                if image_socre is None:
                    raise RuntimeError(_msg("\\u672c\\u6a5f\\u74b0\\u5883\\u7f3a\\u5c11 pyiqa \\u6216\\u5176\\u4f9d\\u8cf4\\uff0c\\u7121\\u6cd5\\u555f\\u7528\\u5f71\\u50cf\\u54c1\\u8cea\\u8a55\\u5206\\u3002"))
                img_score = image_socre(origin_runtime)
                logs.append(_msg(f"{_now_local().isoformat(sep=' ', timespec='seconds')} #{idx:03d} \\u5f71\\u50cf\\u54c1\\u8cea\\u5206\\u6578\\uff1a{img_score}"))

            db, cursor = _db_cursor()
            try:
                photo_uuid = _calculate_photo_uuid(jpeg_path)
                cursor.execute(
                    """
                    INSERT INTO img_upload (
                        origin_full_path,
                        thumbs_full_path,
                        photo_uuid,
                        schedule_id,
                        human_activity_date,
                        human_activity_time,
                        human_activity_name,
                        human_owner_team,
                        human_location,
                        human_laptop_number,
                        human_photographer,
                        human_photo_time,
                        photo_taken_time,
                        photo_file_time,
                        taken_time_source,
                        reco_status,
                        reco_error,
                        reco_last_try_time,
                        reco_retry_count,
                        img_score,
                        create_time,
                        update_time
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW()
                    )
                    ON DUPLICATE KEY UPDATE
                        schedule_id = VALUES(schedule_id),
                        human_activity_date = VALUES(human_activity_date),
                        human_activity_time = VALUES(human_activity_time),
                        human_activity_name = VALUES(human_activity_name),
                        human_owner_team = VALUES(human_owner_team),
                        human_location = VALUES(human_location),
                        human_laptop_number = VALUES(human_laptop_number),
                        human_photographer = VALUES(human_photographer),
                        human_photo_time = VALUES(human_photo_time),
                        photo_taken_time = VALUES(photo_taken_time),
                        photo_file_time = VALUES(photo_file_time),
                        taken_time_source = VALUES(taken_time_source),
                        reco_status = 'PENDING',
                        reco_error = NULL,
                        reco_last_try_time = NOW(),
                        img_score = VALUES(img_score),
                        update_time = NOW()
                    """,
                    (
                        origin_runtime,
                        thumbs_runtime,
                        photo_uuid,
                        (matched_schedule_row or {}).get("id"),
                        (matched_schedule_row or {}).get("activity_date"),
                        (matched_schedule_row or {}).get("activity_time"),
                        (matched_schedule_row or {}).get("activity_content"),
                        (matched_schedule_row or {}).get("owner_team"),
                        (matched_schedule_row or {}).get("location"),
                        laptop_number,
                        final_photographer,
                        naming_time,
                        photo_taken_time,
                        photo_file_time,
                        taken_time_source,
                        "PENDING",
                        None,
                        _now_local(),
                        0,
                        img_score,
                    ),
                )
                db.conn.commit()
            finally:
                cursor.close()
                db.close()
            logs.append(_msg(f"{_now_local().isoformat(sep=' ', timespec='seconds')} #{idx:03d} \\u5df2\\u5beb\\u5165 img_upload"))

            reco_ok = False
            reco_error = ""
            if activity_photo_reco is None:
                reco_error = _msg("\\u672c\\u6a5f\\u74b0\\u5883\\u7f3a\\u5c11\\u8fa8\\u8b58\\u4f9d\\u8cf4\\uff08pyiqa/torch \\u7b49\\uff09\\uff0c\\u7121\\u6cd5\\u57f7\\u884c\\u8fa8\\u8b58\\u3002")
            else:
                try:
                    activity_photo_reco(origin_runtime, LABEL_FACE_NAME=False)
                    db, cursor = _db_cursor()
                    try:
                        cursor.execute(
                            """
                            UPDATE reco_result
                            SET photo_uuid = %s,
                                photo_file_time = %s,
                                taken_time_source = %s,
                                update_time = NOW()
                            WHERE origin_full_path = %s
                            """,
                            (
                                photo_uuid,
                                photo_file_time,
                                taken_time_source,
                                origin_runtime,
                            ),
                        )
                        db.conn.commit()
                    finally:
                        cursor.close()
                        db.close()

                    db, cursor = _db_cursor()
                    try:
                        cursor.execute(
                            """
                            SELECT id
                            FROM reco_result
                            WHERE photo_uuid = %s AND IFNULL(is_deleted, 0) = 0
                            LIMIT 1
                            """,
                            (photo_uuid,),
                        )
                        reco_ok = cursor.fetchone() is not None
                    finally:
                        cursor.close()
                        db.close()
                    if not reco_ok:
                        reco_error = "辨識後未寫入 reco_result（可能無有效人臉或結果被略過）"
                except Exception as exc:
                    reco_error = _display_text(str(exc))

            db, cursor = _db_cursor()
            try:
                cursor.execute(
                    """
                    UPDATE img_upload
                    SET reco_status = %s,
                        reco_error = %s,
                        reco_last_try_time = NOW(),
                        reco_retry_count = IFNULL(reco_retry_count, 0) + %s,
                        update_time = NOW()
                    WHERE photo_uuid = %s
                    """,
                    (
                        "DONE" if reco_ok else "FAILED",
                        None if reco_ok else reco_error[:2000],
                        0 if reco_ok else 1,
                        photo_uuid,
                    ),
                )
                db.conn.commit()
            finally:
                cursor.close()
                db.close()

            if reco_ok:
                logs.append(_msg(f"{_now_local().isoformat(sep=' ', timespec='seconds')} #{idx:03d} \\u5df2\\u89f8\\u767c\\u4eba\\u81c9\\u8fa8\\u8b58"))
            else:
                logs.append(_msg(f"{_now_local().isoformat(sep=' ', timespec='seconds')} #{idx:03d} \\u8fa8\\u8b58\\u5931\\u6557\\uff1a{_display_text(reco_error)}"))
            if backup_dir:
                archived = _copy_with_unique_name(source_path, backup_dir)
                logs.append(_msg(f"{_now_local().isoformat(sep=' ', timespec='seconds')} #{idx:03d} \\u5df2\\u5099\\u4efd\\u539f\\u59cb\\u6a94\\uff1a{archived}"))

            if reco_ok:
                results.append(
                    {
                        "origin_full_path": origin_runtime,
                        "thumbs_full_path": thumbs_runtime,
                        "human_photo_time": naming_time.isoformat(sep=" ", timespec="seconds"),
                        "photo_taken_time": photo_taken_time.isoformat(sep=" ", timespec="seconds") if photo_taken_time else None,
                        "photo_file_time": photo_file_time.isoformat(sep=" ", timespec="seconds"),
                        "taken_time_source": taken_time_source,
                        "photo_uuid": photo_uuid,
                        "img_score": img_score,
                    }
                )
        finally:
            with contextlib.suppress(Exception):
                if job_dir.exists():
                    if _cleanup_temp_dir(job_dir, ACTIVITY_IMPORT_ROOT):
                        logs.append(_msg(f"{_now_local().isoformat(sep=' ', timespec='seconds')} #{idx:03d} \\u5df2\\u6e05\\u9664\\u66ab\\u5b58\\u76ee\\u9304\\uff1a{job_dir}"))
                    else:
                        logs.append(_msg(f"{_now_local().isoformat(sep=' ', timespec='seconds')} #{idx:03d} \\u6e05\\u9664\\u66ab\\u5b58\\u76ee\\u9304\\u5931\\u6557\\uff1a{job_dir}"))

    daily_log_path = _activity_photo_normalize_daily_log_path()
    write_log_file(daily_log_path, logs)
    skipped_count = len(files) - len(results)
    return {
        "processed_count": len(results),
        "skipped_count": skipped_count,
        "items": results,
        "logs": logs,
        "log_path": str(daily_log_path),
    }


def _load_schedule_row(schedule_id: int | None):
    if not schedule_id:
        return None
    db, cursor = _db_cursor()
    try:
        cursor.execute(
            """
            SELECT id, activity_code, activity_date, activity_time, activity_content, owner_team, location, note
            FROM activity_schedule
            WHERE id = %s
            """,
            (schedule_id,),
        )
        return cursor.fetchone()
    finally:
        cursor.close()
        db.close()


async def normalize_activity_photo_files(
    files,
    laptop_number: str,
    photographer: str,
    normalize_mode: str = "schedule",
    schedule_id: int | None = None,
    schedule_code: str = "",
    schedule_time: str = "",
    schedule_time_range: str = "",
    schedule_source: str = "api",
    activities_json: str = "",
    schedule_date: str = "",
    schedule_content: str = "",
    source_folder: str = "",
    output_folder: str = "",
):
    job_id = f"norm_{_now_local().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:6]}"
    activities = []
    if str(activities_json or "").strip():
        try:
            parsed = json.loads(activities_json)
            if isinstance(parsed, list):
                activities = parsed
        except Exception:
            activities = []

    selected_schedule_code = str(schedule_code or "").strip().upper()
    if normalize_mode == "schedule" and not selected_schedule_code:
        if schedule_id:
            matched = next(
                (item for item in activities if str(item.get("id") or "").strip() == str(schedule_id)),
                None,
            )
            selected_schedule_code = str((matched or {}).get("activity_code") or "").strip().upper()
        if not selected_schedule_code:
            raise ValueError("模式 B 請先選擇活動行程（需帶有活動編號）。")

    _assert_activity_ingest_root(source_folder or "")
    _assert_activity_ingest_root(output_folder or "")
    resolved_source = _resolve_runtime_dir(source_folder) if str(source_folder or "").strip() else (_resolve_runtime_dir(r"C:\activity\ingest\incoming") or Path("/mnt/activity/ingest/incoming"))
    success_dir = _resolve_runtime_dir(output_folder) if str(output_folder or "").strip() else (_resolve_runtime_dir(r"C:\activity\ingest\normalized_success") or Path("/mnt/activity/ingest/normalized_success"))
    if success_dir is None:
        success_dir = _resolve_runtime_dir(r"C:\activity\ingest\normalized_success") or Path("/mnt/activity/ingest/normalized_success")
    ingest_root = success_dir.parent
    complete_dir = ingest_root / "normalized_complete"
    fail_dir = ingest_root / "normalized_fail"
    success_state = _mkdir_with_status(success_dir)
    complete_state = _mkdir_with_status(complete_dir)
    fail_state = _mkdir_with_status(fail_dir)

    logs = [_msg(f"{_now_local().isoformat(sep=' ', timespec='seconds')} 開始正規化 {len(files)} 張照片，job_id={job_id}")]
    logs.append(_msg(f"{_now_local().isoformat(sep=' ', timespec='seconds')} 實際來源目錄：{resolved_source}"))
    logs.append(_msg(f"{_now_local().isoformat(sep=' ', timespec='seconds')} 正規化輸出目錄：{success_dir}（{success_state}）"))
    logs.append(_msg(f"{_now_local().isoformat(sep=' ', timespec='seconds')} 成功原始檔目錄：{complete_dir}（{complete_state}）"))
    logs.append(_msg(f"{_now_local().isoformat(sep=' ', timespec='seconds')} 失敗原始檔目錄：{fail_dir}（{fail_state}）"))
    logs.append(_msg(f"{_now_local().isoformat(sep=' ', timespec='seconds')} 行程來源：{schedule_source or 'api'}，活動清單筆數：{len(activities)}"))
    results = []
    fail_count = 0
    fail_exif_missing_count = 0
    failed_items = []
    failed_items_exif_missing = []
    ACTIVITY_IMPORT_ROOT.mkdir(parents=True, exist_ok=True)

    for idx, upload in enumerate(files, 1):
        original_name = Path(upload.filename or f"upload_{idx}").name
        original_source_path = None
        job_dir = None
        try:
            if hasattr(upload, "original_path"):
                with contextlib.suppress(Exception):
                    candidate = Path(str(upload.original_path))
                    if candidate.exists() and candidate.is_file():
                        original_source_path = candidate
            job_dir = Path(tempfile.mkdtemp(prefix="activity_norm_", dir=ACTIVITY_IMPORT_ROOT))
            source_path = job_dir / original_name
            with source_path.open("wb") as handle:
                handle.write(upload.file.read())
            with contextlib.suppress(Exception):
                upload.file.close()
            image = _open_activity_image(source_path)
            photo_taken_time, exif_data = _extract_photo_taken_time(image)
            if image.mode in ("RGBA", "P"):
                image = image.convert("RGB")
            photo_file_time = _file_time_to_taipei_naive(source_path.stat().st_ctime)
            naming_time = photo_taken_time or photo_file_time
            final_photographer = photographer or ""

            if normalize_mode == "exif" and not photo_taken_time:
                fail_source = original_source_path if original_source_path else source_path
                moved = _move_with_unique_name(fail_source, fail_dir)
                fail_count += 1
                fail_exif_missing_count += 1
                fail_item = {
                    "job_id": job_id,
                    "filename": original_name,
                    "reason_code": "EXIF_MISSING",
                    "reason": "無 EXIF，模式 A 已略過，請改用模式 B。",
                    "source_path": str(fail_source),
                    "fail_path": str(moved),
                    "timestamp": _now_local().isoformat(sep=" ", timespec="seconds"),
                }
                failed_items.append(fail_item)
                failed_items_exif_missing.append(fail_item)
                logs.append(_msg(f"{_now_local().isoformat(sep=' ', timespec='seconds')} #{idx:03d} 失敗(EXIF_MISSING)：{_display_text(original_name)} -> {moved}"))
                continue

            if normalize_mode == "exif":
                inferred_code = _find_activity_code_from_payload(naming_time, activities)
                new_name = _make_exif_filename(inferred_code, laptop_number, final_photographer, naming_time, original_name)
                exif_text = photo_taken_time.isoformat(sep=" ", timespec="seconds") if photo_taken_time else "None"
                if inferred_code == "000":
                    day_ranges = _build_day_range_debug(naming_time, activities, limit=8)
                    logs.append(
                        _msg(
                            f"{_now_local().isoformat(sep=' ', timespec='seconds')} #{idx:03d} "
                            f"NO_MATCH -> EXIF_000，EXIF_TIME={exif_text}，候選區間={day_ranges or '無同日活動'}"
                        )
                    )
                else:
                    logs.append(
                        _msg(
                            f"{_now_local().isoformat(sep=' ', timespec='seconds')} #{idx:03d} "
                            f"MATCHED_ACTIVITY_CODE={inferred_code}，EXIF_TIME={exif_text}"
                        )
                    )
            else:
                resolved_code = selected_schedule_code or "000"
                new_name = _make_nonexif_filename(resolved_code, laptop_number, final_photographer, naming_time, original_name)

            normalized_path = job_dir / f"{new_name}.jpeg"
            _save_jpeg(image, normalized_path, exif_data)
            success_saved = _copy_with_unique_name(normalized_path, success_dir)
            time_source = original_source_path if original_source_path else source_path
            _preserve_file_times(time_source, success_saved)
            complete_source = original_source_path if original_source_path else source_path
            original_saved = _move_with_unique_name(complete_source, complete_dir)
            logs.append(_msg(f"{_now_local().isoformat(sep=' ', timespec='seconds')} #{idx:03d} 正規化成功：{success_saved.name}"))
            results.append(
                {
                    "normalized_file": str(success_saved),
                    "original_file": str(original_saved),
                    "photo_taken_time": photo_taken_time.isoformat(sep=" ", timespec="seconds") if photo_taken_time else None,
                    "photo_file_time": photo_file_time.isoformat(sep=" ", timespec="seconds"),
                }
            )
        except Exception as exc:
            fail_count += 1
            moved_path = ""
            fail_source = original_source_path
            if fail_source is None:
                with contextlib.suppress(Exception):
                    fail_source = source_path
            with contextlib.suppress(Exception):
                if fail_source and fail_source.exists():
                    moved = _move_with_unique_name(fail_source, fail_dir)
                    moved_path = str(moved)
            fail_item = {
                "job_id": job_id,
                "filename": original_name,
                "reason_code": "PROCESS_ERROR",
                "reason": str(exc),
                "source_path": str(fail_source) if fail_source else "",
                "fail_path": moved_path,
                "timestamp": _now_local().isoformat(sep=" ", timespec="seconds"),
            }
            failed_items.append(fail_item)
            logs.append(_msg(f"{_now_local().isoformat(sep=' ', timespec='seconds')} #{idx:03d} 失敗(PROCESS_ERROR)：{_display_text(original_name)}，原因：{exc}"))
        finally:
            with contextlib.suppress(Exception):
                if job_dir and job_dir.exists():
                    if _cleanup_temp_dir(job_dir, ACTIVITY_IMPORT_ROOT):
                        logs.append(_msg(f"{_now_local().isoformat(sep=' ', timespec='seconds')} #{idx:03d} 已清除暫存目錄：{job_dir}"))
                    else:
                        logs.append(_msg(f"{_now_local().isoformat(sep=' ', timespec='seconds')} #{idx:03d} 清除暫存目錄失敗：{job_dir}"))

    # 輸出本批次 manifest，供匯入/辨識頁（8000）直接帶入設定。
    manifest_root = ingest_root / "_work" / "normalize" / job_id
    manifest_root.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_root / "manifest.json"
    manifest_payload = {
        "job_id": job_id,
        "device_id": laptop_number,
        "photographer": photographer or "",
        "normalize_mode": normalize_mode or "schedule",
        "schedule_id": schedule_id,
        "schedule_code": selected_schedule_code,
        "schedule_time": schedule_time or "",
        "schedule_time_range": schedule_time_range or "",
        "schedule_source": schedule_source or "api",
        "model_version": "",
        "source_folder": str(success_dir),
        "generated_at": _now_local().isoformat(sep=" ", timespec="seconds"),
        "activities": [
            {
                "id": item.get("id"),
                "activity_code": item.get("activity_code"),
                "activity_date": item.get("activity_date"),
                "activity_time": item.get("activity_time"),
                "activity_time_range": item.get("activity_time_range"),
                "activity_content": item.get("activity_content"),
            }
            for item in activities
        ],
        "files": results,
    }
    manifest_path.write_text(json.dumps(manifest_payload, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")

    daily_log_path = _activity_photo_import_daily_log_path()
    write_log_file(daily_log_path, logs)
    fail_csv_path = ""
    fail_csv_created = False
    if fail_count > 0:
        csv_target = LOG_ROOT / f"activity_photo_normalize_fail_{job_id}.csv"
        write_failure_csv(csv_target, failed_items)
        fail_csv_path = str(csv_target)
        fail_csv_created = True
    return {
        "job_id": job_id,
        "normalized_count": len(results),
        "failed_count": fail_count,
        "failed_count_exif_missing": fail_exif_missing_count,
        "failed_items_exif_missing": failed_items_exif_missing,
        "failed_items": failed_items,
        "items": results,
        "logs": logs,
        "success_dir": str(success_dir),
        "complete_dir": str(complete_dir),
        "fail_dir": str(fail_dir),
        "resolved_source_dir": str(resolved_source),
        "resolved_success_dir": str(success_dir),
        "resolved_complete_dir": str(complete_dir),
        "resolved_fail_dir": str(fail_dir),
        "resolved_manifest_dir": str(manifest_root),
        "log_path": str(daily_log_path),
        "fail_csv_path": fail_csv_path,
        "fail_csv_created": fail_csv_created,
        "manifest_path": str(manifest_path),
        "manifest": manifest_payload,
    }


async def normalize_activity_photo_folder(
    source_folder: str,
    output_folder: str,
    laptop_number: str,
    photographer: str,
    normalize_mode: str = "schedule",
    schedule_id: int | None = None,
    schedule_code: str = "",
    schedule_time: str = "",
    schedule_time_range: str = "",
    schedule_source: str = "api",
    activities_json: str = "",
    schedule_date: str = "",
    schedule_content: str = "",
):
    _assert_activity_ingest_root(source_folder or "")
    _assert_activity_ingest_root(output_folder or "")
    source_dir = _resolve_runtime_dir(source_folder) if source_folder else None
    if not source_dir:
        raise ValueError(_msg("\\u8acb\\u586b\\u5165\\u4f86\\u6e90\\u5716\\u6a94\\u8cc7\\u6599\\u593e"))
    source_dir.mkdir(parents=True, exist_ok=True)
    files = [p for p in sorted(source_dir.iterdir()) if p.is_file()]
    if not files:
        output_dir = _resolve_runtime_dir(output_folder) if output_folder else (_resolve_runtime_dir(r"C:\activity\ingest\normalized_success") or Path("/mnt/activity/ingest/normalized_success"))
        output_dir = output_dir or (_resolve_runtime_dir(r"C:\activity\ingest\normalized_success") or Path("/mnt/activity/ingest/normalized_success"))
        ingest_root = output_dir.parent
        complete_dir = ingest_root / "normalized_complete"
        fail_dir = ingest_root / "normalized_fail"
        _mkdir_with_status(source_dir)
        _mkdir_with_status(output_dir)
        _mkdir_with_status(complete_dir)
        _mkdir_with_status(fail_dir)
        return {
            "normalized_count": 0,
            "failed_count": 0,
            "items": [],
            "logs": [_msg("\\u4f86\\u6e90\\u8cc7\\u6599\\u593e\\u6c92\\u6709\\u53ef\\u8655\\u7406\\u6a94\\u6848")],
            "resolved_source_dir": str(source_dir),
            "resolved_success_dir": str(output_dir),
            "resolved_complete_dir": str(complete_dir),
            "resolved_fail_dir": str(fail_dir),
        }

    class LocalUpload:
        def __init__(self, path: Path):
            self.filename = path.name
            self.original_path = str(path)
            self.file = path.open("rb")

    wrappers = [LocalUpload(p) for p in files]
    try:
        return await normalize_activity_photo_files(
            files=wrappers,
            laptop_number=laptop_number,
            photographer=photographer,
            normalize_mode=normalize_mode,
            schedule_id=schedule_id,
            schedule_code=schedule_code,
            schedule_time=schedule_time,
            schedule_time_range=schedule_time_range,
            schedule_source=schedule_source,
            activities_json=activities_json,
            schedule_date=schedule_date,
            schedule_content=schedule_content,
            source_folder=source_folder,
            output_folder=output_folder,
        )
    finally:
        for item in wrappers:
            with contextlib.suppress(Exception):
                item.file.close()


async def _run_import_job_async(job_id: str):
    status_row = get_import_job_status(job_id) or {}
    laptop_number = status_row.get("laptop_number", "")
    schedule_id = status_row.get("schedule_id")
    photographer = status_row.get("photographer", "")
    enable_pyiqa = bool(status_row.get("enable_pyiqa"))
    normalize_mode = status_row.get("normalize_mode", "schedule")
    ensure_activity_tables_once()

    requested_source = str(status_row.get("source_folder") or "").strip()
    _assert_activity_ingest_root(requested_source)
    source_base = _resolve_runtime_dir(requested_source) if requested_source else None
    if source_base is None:
        source_base = _resolve_runtime_dir(r"C:\activity\ingest\normalized_success") or Path("/mnt/activity/ingest/normalized_success")
    normalized_success_dir = source_base
    ingest_root_dir = normalized_success_dir.parent
    import_success_dir = ingest_root_dir / "imgupload_success"
    import_fail_dir = ingest_root_dir / "imgupload_fail"
    duplicate_dir = import_success_dir / "duplicate"
    source_state = _mkdir_with_status(normalized_success_dir)
    success_state = _mkdir_with_status(import_success_dir)
    fail_state = _mkdir_with_status(import_fail_dir)
    duplicate_state = _mkdir_with_status(duplicate_dir)
    log_path = _make_import_job_log_path(job_id)
    with contextlib.suppress(Exception):
        log_path.unlink()

    class LocalUpload:
        def __init__(self, path: Path):
            self.path = path
            self.filename = path.name
            self.file = path.open("rb")
        def close(self):
            with contextlib.suppress(Exception):
                self.file.close()

    def _is_photo_uuid_exists(photo_uuid: str):
        db, cursor = _db_cursor()
        try:
            cursor.execute(
                "SELECT id FROM img_upload WHERE photo_uuid = %s AND IFNULL(is_deleted, 0) = 0 LIMIT 1",
                (photo_uuid,),
            )
            return cursor.fetchone() is not None
        finally:
            cursor.close()
            db.close()

    candidates = [p for p in sorted(normalized_success_dir.iterdir()) if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".heic"}]
    _update_import_job(
        job_id,
        status="RUNNING",
        total_count=len(candidates),
        resolved_source_dir=str(normalized_success_dir),
        resolved_success_dir=str(import_success_dir),
        resolved_fail_dir=str(import_fail_dir),
        resolved_duplicate_dir=str(duplicate_dir),
    )
    append_log_line(log_path, f"{_now_local().isoformat(sep=' ', timespec='seconds')} | {job_id} | START | total={len(candidates)}")
    append_log_line(log_path, f"{_now_local().isoformat(sep=' ', timespec='seconds')} | {job_id} | DIR | source={normalized_success_dir} ({source_state})")
    append_log_line(log_path, f"{_now_local().isoformat(sep=' ', timespec='seconds')} | {job_id} | DIR | success={import_success_dir} ({success_state})")
    append_log_line(log_path, f"{_now_local().isoformat(sep=' ', timespec='seconds')} | {job_id} | DIR | fail={import_fail_dir} ({fail_state})")
    append_log_line(log_path, f"{_now_local().isoformat(sep=' ', timespec='seconds')} | {job_id} | DIR | duplicate={duplicate_dir} ({duplicate_state})")

    processed_count = skipped_count = failed_count = 0
    moved_success_count = moved_fail_count = moved_duplicate_count = 0
    failed_items: list[dict] = []
    seq = 0

    for file_path in candidates:
        seq += 1
        upload = LocalUpload(file_path)
        item_status = "FAILED"
        item_reason = ""
        reason_code = ""
        move_result = "NOT_REQUIRED"
        moved_to_text = ""
        photo_uuid = ""
        try:
            if _looks_like_mojibake(file_path.name):
                moved_to = _move_with_unique_name(file_path, import_fail_dir)
                moved_to_text = str(moved_to)
                moved_fail_count += 1
                failed_count += 1
                item_reason = "檔名疑似亂碼（mojibake），已拒收"
                reason_code = "mojibake_filename"
                move_result = "MOVED"
                append_log_line(log_path, f"{_now_local().isoformat(sep=' ', timespec='seconds')} | {job_id} | #{seq:03d} | FAILED | {file_path.name} | {item_reason}")
            else:
                photo_uuid = _calculate_photo_uuid(file_path)
                if _is_photo_uuid_exists(photo_uuid):
                    moved_to = _move_with_unique_name(file_path, duplicate_dir)
                    moved_to_text = str(moved_to)
                    skipped_count += 1
                    moved_duplicate_count += 1
                    item_status = "DUPLICATE"
                    item_reason = "duplicate_photo_uuid"
                    reason_code = "duplicate_photo_uuid"
                    move_result = "MOVED"
                    append_log_line(log_path, f"{_now_local().isoformat(sep=' ', timespec='seconds')} | {job_id} | #{seq:03d} | DUPLICATE | {file_path.name}")
                else:
                    result = await process_activity_photo_import(
                        laptop_number=laptop_number,
                        schedule_id=schedule_id,
                        photographer=photographer,
                        enable_pyiqa=enable_pyiqa,
                        files=[upload],
                        normalize_mode=normalize_mode,
                        skip_normalization=True,
                    )
                    processed = int(result.get("processed_count", 0) or 0)
                    skipped = int(result.get("skipped_count", 0) or 0)
                    processed_count += processed
                    skipped_count += skipped
                    if processed > 0:
                        try:
                            moved_to = _move_with_unique_name(file_path, import_success_dir)
                            moved_to_text = str(moved_to)
                            moved_success_count += 1
                            item_status = "DONE"
                            reason_code = "done"
                            move_result = "MOVED"
                            append_log_line(log_path, f"{_now_local().isoformat(sep=' ', timespec='seconds')} | {job_id} | #{seq:03d} | DONE | {file_path.name}")
                        except Exception as move_exc:
                            failed_count += 1
                            item_status = "FAILED"
                            reason_code = "move_failed"
                            item_reason = f"move_failed: {str(move_exc)}"
                            move_result = "MOVE_FAILED"
                            append_log_line(log_path, f"{_now_local().isoformat(sep=' ', timespec='seconds')} | {job_id} | #{seq:03d} | FAILED | {file_path.name} | {item_reason}")
                    else:
                        moved_to = _move_with_unique_name(file_path, import_fail_dir)
                        moved_to_text = str(moved_to)
                        moved_fail_count += 1
                        failed_count += 1
                        item_reason = "processed_count_zero"
                        reason_code = "processed_count_zero"
                        move_result = "MOVED"
                        append_log_line(log_path, f"{_now_local().isoformat(sep=' ', timespec='seconds')} | {job_id} | #{seq:03d} | FAILED | {file_path.name} | processed_count=0")
        except Exception as exc:
            failed_count += 1
            item_reason = str(exc)
            reason_code = "reco_exception"
            with contextlib.suppress(Exception):
                moved_to = _move_with_unique_name(file_path, import_fail_dir)
                moved_to_text = str(moved_to)
                moved_fail_count += 1
                move_result = "MOVED"
            append_log_line(log_path, f"{_now_local().isoformat(sep=' ', timespec='seconds')} | {job_id} | #{seq:03d} | FAILED | {file_path.name} | {item_reason}")
        finally:
            upload.close()

        if item_status == "FAILED":
            failed_items.append(
                {
                    "job_id": job_id,
                    "filename": file_path.name,
                    "reason_code": "IMPORT_FAILED",
                    "reason": item_reason,
                    "source_path": str(file_path),
                    "fail_path": moved_to_text,
                    "timestamp": _now_local().isoformat(sep=" ", timespec="seconds"),
                }
            )
        _append_import_job_item(
            job_id,
            {
                "seq_no": seq,
                "filename": file_path.name,
                "photo_uuid": photo_uuid,
                "status": item_status,
                "error_reason": item_reason,
                "moved_to": moved_to_text,
                "reason_code": reason_code,
                "move_result": move_result,
                "updated_at": _now_local().isoformat(sep=" ", timespec="seconds"),
            },
        )
        _update_import_job(
            job_id,
            processed_count=processed_count,
            success_count=processed_count,
            failed_count=failed_count,
            skipped_count=skipped_count,
            moved_success_count=moved_success_count,
            moved_fail_count=moved_fail_count,
            moved_duplicate_count=moved_duplicate_count,
        )

    remaining = [p.name for p in sorted(normalized_success_dir.iterdir()) if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".heic"}]

    fail_csv_path = ""
    fail_csv_created = False
    if failed_count > 0:
        csv_target = LOG_ROOT / f"activity_photo_import_fail_{job_id}.csv"
        write_failure_csv(csv_target, failed_items)
        fail_csv_path = str(csv_target)
        fail_csv_created = True

    append_log_line(log_path, f"{_now_local().isoformat(sep=' ', timespec='seconds')} | {job_id} | END | success={processed_count} skipped={skipped_count} failed={failed_count}")
    _update_import_job(
        job_id,
        status="DONE",
        finished_at=_now_local().isoformat(sep=" ", timespec="seconds"),
        fail_csv_path=fail_csv_path,
        fail_csv_created=fail_csv_created,
        remaining_in_source_count=len(remaining),
        remaining_in_source_files=json.dumps(remaining[:200], ensure_ascii=False),
    )


def _run_import_job_sync(job_id: str):
    asyncio.run(_run_import_job_async(job_id))


def _run_normalize_job_sync(job_id: str):
    status_row = get_import_job_status(job_id) or {}
    with NORMALIZE_JOBS_LOCK:
        payload = NORMALIZE_JOBS.get(job_id, {}).copy()

    laptop_number = str(status_row.get("laptop_number") or payload.get("laptop_number") or "").strip()
    photographer = str(status_row.get("photographer") or payload.get("photographer") or "").strip()
    normalize_mode = str(status_row.get("normalize_mode") or payload.get("normalize_mode") or "exif").strip() or "exif"
    source_folder = str(status_row.get("source_folder") or payload.get("source_folder") or r"C:\activity\ingest\incoming").strip()
    output_folder = str(status_row.get("output_folder") or payload.get("output_folder") or r"C:\activity\ingest\normalized_success").strip()
    schedule_id = payload.get("schedule_id")
    schedule_code = str(payload.get("schedule_code") or "").strip().upper()
    schedule_time = str(payload.get("schedule_time") or "").strip()
    schedule_time_range = str(payload.get("schedule_time_range") or "").strip()
    schedule_source = str(payload.get("schedule_source") or "api").strip() or "api"
    schedule_date = str(payload.get("schedule_date") or "").strip()
    schedule_content = str(payload.get("schedule_content") or "").strip()
    activities = payload.get("activities") if isinstance(payload.get("activities"), list) else []

    _assert_activity_ingest_root(source_folder)
    _assert_activity_ingest_root(output_folder)
    source_dir = _resolve_runtime_dir(source_folder) or Path("/mnt/activity/ingest/incoming")
    success_dir = _resolve_runtime_dir(output_folder) or Path("/mnt/activity/ingest/normalized_success")
    source_state = _mkdir_with_status(source_dir)
    success_state = _mkdir_with_status(success_dir)
    ingest_root = success_dir.parent
    complete_dir = ingest_root / "normalized_complete"
    fail_dir = ingest_root / "normalized_fail"
    complete_state = _mkdir_with_status(complete_dir)
    fail_state = _mkdir_with_status(fail_dir)
    ACTIVITY_IMPORT_ROOT.mkdir(parents=True, exist_ok=True)
    manifest_root = ingest_root / "_work" / "normalize" / job_id
    manifest_root.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_root / "manifest.json"
    log_path = _make_normalize_job_log_path(job_id)
    with contextlib.suppress(Exception):
        log_path.unlink()

    candidates = [p for p in sorted(source_dir.iterdir()) if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".heic"}]
    _update_import_job(
        job_id,
        status="RUNNING",
        total_count=len(candidates),
        resolved_source_dir=str(source_dir),
        resolved_success_dir=str(success_dir),
        resolved_fail_dir=str(fail_dir),
        resolved_duplicate_dir=str(complete_dir),
    )
    append_log_line(log_path, f"{_now_local().isoformat(sep=' ', timespec='seconds')} | {job_id} | START | total={len(candidates)}")
    append_log_line(log_path, f"{_now_local().isoformat(sep=' ', timespec='seconds')} | {job_id} | DIR | source={source_dir} ({source_state})")
    append_log_line(log_path, f"{_now_local().isoformat(sep=' ', timespec='seconds')} | {job_id} | DIR | success={success_dir} ({success_state})")
    append_log_line(log_path, f"{_now_local().isoformat(sep=' ', timespec='seconds')} | {job_id} | DIR | complete={complete_dir} ({complete_state})")
    append_log_line(log_path, f"{_now_local().isoformat(sep=' ', timespec='seconds')} | {job_id} | DIR | fail={fail_dir} ({fail_state})")

    results: list[dict] = []
    failed_items: list[dict] = []
    fail_exif_missing_count = 0
    success_count = 0
    failed_count = 0
    processed_count = 0
    for idx, source_file in enumerate(candidates, 1):
        original_name = source_file.name
        item_status = "FAILED"
        item_reason = ""
        reason_code = ""
        moved_to = ""
        try:
            image = _open_activity_image(source_file)
            photo_taken_time, exif_data = _extract_photo_taken_time(image)
            if image.mode in ("RGBA", "P"):
                image = image.convert("RGB")
            photo_file_time = _file_time_to_taipei_naive(source_file.stat().st_ctime)
            naming_time = photo_taken_time or photo_file_time

            if normalize_mode == "exif" and not photo_taken_time:
                moved_path = _move_with_unique_name(source_file, fail_dir)
                moved_to = str(moved_path)
                failed_count += 1
                fail_exif_missing_count += 1
                item_status = "FAILED"
                item_reason = "無 EXIF，模式 A 已略過，請改用模式 B。"
                reason_code = "EXIF_MISSING"
                failed_items.append(
                    {
                        "job_id": job_id,
                        "filename": original_name,
                        "reason_code": reason_code,
                        "reason": item_reason,
                        "source_path": str(source_file),
                        "fail_path": moved_to,
                        "timestamp": _now_local().isoformat(sep=" ", timespec="seconds"),
                    }
                )
                append_log_line(log_path, f"{_now_local().isoformat(sep=' ', timespec='seconds')} | {job_id} | #{idx:03d} | FAILED | {original_name} | {item_reason}")
            else:
                if normalize_mode == "exif":
                    inferred_code = _find_activity_code_from_payload(naming_time, activities)
                    new_name = _make_exif_filename(inferred_code, laptop_number, photographer, naming_time, original_name)
                else:
                    resolved_code = schedule_code or "000"
                    new_name = _make_nonexif_filename(resolved_code, laptop_number, photographer, naming_time, original_name)

                with tempfile.TemporaryDirectory(prefix="activity_norm_", dir=ACTIVITY_IMPORT_ROOT) as tmp_dir:
                    normalized_path = Path(tmp_dir) / f"{new_name}.jpeg"
                    _save_jpeg(image, normalized_path, exif_data)
                    success_saved = _copy_with_unique_name(normalized_path, success_dir)
                    _preserve_file_times(source_file, success_saved)
                original_saved = _move_with_unique_name(source_file, complete_dir)
                moved_to = str(original_saved)
                success_count += 1
                processed_count += 1
                item_status = "DONE"
                reason_code = "done"
                append_log_line(log_path, f"{_now_local().isoformat(sep=' ', timespec='seconds')} | {job_id} | #{idx:03d} | DONE | {success_saved.name}")
                results.append(
                    {
                        "normalized_file": str(success_saved),
                        "original_file": str(original_saved),
                        "photo_taken_time": photo_taken_time.isoformat(sep=" ", timespec="seconds") if photo_taken_time else None,
                        "photo_file_time": photo_file_time.isoformat(sep=" ", timespec="seconds"),
                    }
                )
        except Exception as exc:
            failed_count += 1
            item_status = "FAILED"
            reason_code = "PROCESS_ERROR"
            item_reason = str(exc)
            with contextlib.suppress(Exception):
                moved_path = _move_with_unique_name(source_file, fail_dir)
                moved_to = str(moved_path)
            failed_items.append(
                {
                    "job_id": job_id,
                    "filename": original_name,
                    "reason_code": reason_code,
                    "reason": item_reason,
                    "source_path": str(source_file),
                    "fail_path": moved_to,
                    "timestamp": _now_local().isoformat(sep=" ", timespec="seconds"),
                }
            )
            append_log_line(log_path, f"{_now_local().isoformat(sep=' ', timespec='seconds')} | {job_id} | #{idx:03d} | FAILED | {original_name} | {item_reason}")
        finally:
            _append_import_job_item(
                job_id,
                {
                    "seq_no": idx,
                    "filename": original_name,
                    "photo_uuid": "",
                    "status": item_status,
                    "stage": "normalize",
                    "error_reason": item_reason,
                    "moved_to": moved_to,
                    "reason_code": reason_code,
                    "move_result": "MOVED" if moved_to else "NOT_REQUIRED",
                },
            )
            _update_import_job(
                job_id,
                processed_count=processed_count,
                success_count=success_count,
                failed_count=failed_count,
                skipped_count=0,
                moved_success_count=success_count,
                moved_fail_count=failed_count,
                moved_duplicate_count=0,
            )

    remaining_files = [p.name for p in sorted(source_dir.iterdir()) if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".heic"}]
    manifest_payload = {
        "job_id": job_id,
        "device_id": laptop_number,
        "photographer": photographer or "",
        "normalize_mode": normalize_mode or "exif",
        "schedule_id": schedule_id,
        "schedule_code": schedule_code,
        "schedule_time": schedule_time,
        "schedule_time_range": schedule_time_range,
        "schedule_source": schedule_source,
        "schedule_date": schedule_date,
        "schedule_content": schedule_content,
        "model_version": "",
        "source_folder": str(success_dir).replace("/mnt/activity", "C:\\activity").replace("/", "\\"),
        "generated_at": _now_local().isoformat(sep=" ", timespec="seconds"),
        "activities": activities,
        "files": results,
    }
    manifest_path.write_text(json.dumps(manifest_payload, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    fail_csv_path = ""
    fail_csv_created = False
    if failed_items:
        csv_target = LOG_ROOT / f"activity_photo_normalize_fail_{job_id}.csv"
        write_failure_csv(csv_target, failed_items)
        fail_csv_path = str(csv_target)
        fail_csv_created = True

    daily_log_path = _activity_photo_normalize_daily_log_path()
    write_log_file(
        daily_log_path,
        [
            f"{_now_local().isoformat(sep=' ', timespec='seconds')} 正規化任務完成：job_id={job_id}",
            f"成功={success_count}，失敗={failed_count}，來源剩餘={len(remaining_files)}",
            f"source={source_dir}",
            f"success={success_dir}",
            f"complete={complete_dir}",
            f"fail={fail_dir}",
        ],
    )
    append_log_line(log_path, f"{_now_local().isoformat(sep=' ', timespec='seconds')} | {job_id} | DONE | success={success_count} failed={failed_count} remaining={len(remaining_files)}")
    _update_import_job(
        job_id,
        status="DONE",
        finished_at=_now_local().isoformat(sep=" ", timespec="seconds"),
        fail_csv_path=fail_csv_path,
        fail_csv_created=1 if fail_csv_created else 0,
        error_summary="",
        resolved_source_dir=str(source_dir),
        resolved_success_dir=str(success_dir),
        resolved_fail_dir=str(fail_dir),
        resolved_duplicate_dir=str(complete_dir),
        remaining_in_source_count=len(remaining_files),
        remaining_in_source_files=json.dumps(remaining_files[:100], ensure_ascii=False),
    )
    with contextlib.suppress(Exception):
        _cleanup_temp_dir_with_log(manifest_root, ACTIVITY_IMPORT_ROOT, log_path, "normalize_manifest")
    with NORMALIZE_JOBS_LOCK:
        NORMALIZE_JOBS.pop(job_id, None)


async def start_normalize_activity_photos_job(
    laptop_number: str = "",
    schedule_id: int | None = None,
    schedule_code: str = "",
    schedule_time: str = "",
    schedule_time_range: str = "",
    schedule_source: str = "api",
    activities_json: str = "",
    schedule_date: str = "",
    schedule_content: str = "",
    photographer: str = "",
    normalize_mode: str = "exif",
    source_folder: str = "",
    output_folder: str = "",
):
    ensure_activity_tables_once()
    final_source_folder = str(source_folder or r"C:\activity\ingest\incoming").strip() or r"C:\activity\ingest\incoming"
    final_output_folder = str(output_folder or r"C:\activity\ingest\normalized_success").strip() or r"C:\activity\ingest\normalized_success"
    _assert_activity_ingest_root(final_source_folder)
    _assert_activity_ingest_root(final_output_folder)
    resolved_source = _resolve_runtime_dir(final_source_folder)
    resolved_output = _resolve_runtime_dir(final_output_folder)
    if resolved_source is None or resolved_output is None:
        raise ValueError("來源或輸出資料夾解析失敗")
    source_dir_state = _mkdir_with_status(resolved_source)
    _mkdir_with_status(resolved_output)

    activities = []
    if str(activities_json or "").strip():
        try:
            parsed = json.loads(activities_json)
            if isinstance(parsed, list):
                activities = parsed
        except Exception:
            activities = []

    device_token = _safe_device_token(laptop_number, default="NODEV")
    job_id = f"norm_{device_token}_{_now_local().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:6]}"
    payload = {
        "device_id": laptop_number,
        "laptop_number": str(laptop_number or "").strip(),
        "schedule_id": schedule_id,
        "photographer": str(photographer or "").strip(),
        "normalize_mode": str(normalize_mode or "exif").strip() or "exif",
        "source_folder": final_source_folder,
        "output_folder": final_output_folder,
        "schedule_code": str(schedule_code or "").strip().upper(),
        "schedule_time": str(schedule_time or "").strip(),
        "schedule_time_range": str(schedule_time_range or "").strip(),
        "schedule_source": str(schedule_source or "api").strip() or "api",
        "schedule_date": str(schedule_date or "").strip(),
        "schedule_content": str(schedule_content or "").strip(),
        "activities": activities,
    }
    _create_normalize_job(job_id, payload)
    with NORMALIZE_JOBS_LOCK:
        NORMALIZE_JOBS[job_id] = payload.copy()
    asyncio.create_task(asyncio.to_thread(_run_normalize_job_sync, job_id))
    return {
        "job_id": job_id,
        "status": "QUEUED",
        "job_type": "normalize",
        "resolved_source_folder": final_source_folder,
        "resolved_output_folder": final_output_folder,
        "source_dir_state": source_dir_state,
        "server_received_at": _now_local().isoformat(sep=" ", timespec="seconds"),
    }


async def start_import_activity_photos_job(
    laptop_number: str = "",
    schedule_id: int | None = None,
    photographer: str = "",
    enable_pyiqa: bool = False,
    normalize_mode: str = "schedule",
    source_folder: str = "",
    output_folder: str = "",
    backup_folder: str = "",
    manifest_path: str = "",
):
    ensure_activity_tables_once()
    manifest_payload = {}
    if str(manifest_path or "").strip():
        if not str(manifest_path).strip().lower().endswith(".json"):
            raise ValueError(f"批次設定檔必須是 .json：{manifest_path}")
        runtime_manifest = _resolve_runtime_dir(manifest_path)
        if runtime_manifest is None or not runtime_manifest.exists():
            raise FileNotFoundError(f"找不到 manifest：{manifest_path}")
        manifest_payload = json.loads(runtime_manifest.read_text(encoding="utf-8"))
    final_laptop = str(laptop_number or manifest_payload.get("device_id") or "").strip()
    final_schedule_id = schedule_id if schedule_id is not None else manifest_payload.get("schedule_id")
    final_photographer = str(photographer or manifest_payload.get("photographer") or "").strip()
    final_mode = str(normalize_mode or manifest_payload.get("normalize_mode") or "schedule").strip() or "schedule"
    final_source_folder = str(source_folder or manifest_payload.get("source_folder") or r"C:\activity\ingest\normalized_success").strip()
    if not final_source_folder:
        final_source_folder = r"C:\activity\ingest\normalized_success"
    _assert_activity_ingest_root(final_source_folder)
    resolved_source_dir = _resolve_runtime_dir(final_source_folder)
    if resolved_source_dir is None:
        raise ValueError("來源資料夾解析失敗")
    source_dir_state = _mkdir_with_status(resolved_source_dir)

    device_token = _safe_device_token(final_laptop, default="NODEV")
    job_id = f"imp_{device_token}_{_now_local().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:6]}"
    payload = {
        "device_id": final_laptop,
        "laptop_number": final_laptop,
        "schedule_id": final_schedule_id,
        "photographer": final_photographer,
        "enable_pyiqa": bool(enable_pyiqa),
        "normalize_mode": final_mode,
        "source_folder": final_source_folder,
        "output_folder": output_folder or "",
        "backup_folder": backup_folder or "",
    }
    _create_import_job(job_id, payload)
    asyncio.create_task(asyncio.to_thread(_run_import_job_sync, job_id))
    return {
        "job_id": job_id,
        "status": "QUEUED",
        "job_type": "import_reco",
        "manifest_path": manifest_path or "",
        "resolved_source_folder": final_source_folder,
        "source_dir_state": source_dir_state,
        "server_received_at": _now_local().isoformat(sep=" ", timespec="seconds"),
    }


def preview_import_source_folder(source_folder: str = ""):
    final_source_folder = str(source_folder or r"C:\activity\ingest\normalized_success").strip()
    if not final_source_folder:
        final_source_folder = r"C:\activity\ingest\normalized_success"
    _assert_activity_ingest_root(final_source_folder)
    resolved = _resolve_runtime_dir(final_source_folder)
    if resolved is None:
        raise ValueError("來源資料夾解析失敗")
    state = _mkdir_with_status(resolved)
    candidates = [p for p in sorted(resolved.iterdir()) if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".heic"}]
    return {
        "source_folder": final_source_folder,
        "resolved_source_dir": str(resolved),
        "source_dir_state": state,
        "total_count": len(candidates),
        "sample_files": [p.name for p in candidates[:10]],
    }


async def import_activity_photos_from_normalized_folder(
    laptop_number: str,
    schedule_id: int | None,
    photographer: str,
    enable_pyiqa: bool,
    normalize_mode: str = "schedule",
):
    # backward compatibility: keep old endpoint behaviour by waiting for completion
    job = await start_import_activity_photos_job(
        laptop_number=laptop_number,
        schedule_id=schedule_id,
        photographer=photographer,
        enable_pyiqa=enable_pyiqa,
        normalize_mode=normalize_mode,
    )
    job_id = job["job_id"]
    for _ in range(3600):
        status = get_import_job_status(job_id)
        if not status:
            break
        if status.get("status") in {"DONE", "FAILED", "CANCELED"}:
            logs_payload = get_import_job_logs(job_id, 0) or {"lines": []}
            items_payload = get_import_job_items(job_id) or {"items": []}
            return {**status, "items": items_payload.get("items", []), "logs": logs_payload.get("lines", [])}
        await asyncio.sleep(0.5)
    status = get_import_job_status(job_id) or {"status": "FAILED"}
    status["error_summary"] = "import job timeout"
    return status


def retry_failed_activity_recognition(limit: int = 100):
    ensure_activity_tables()
    if activity_photo_reco is None:
        raise RuntimeError("本機環境缺少辨識依賴，無法執行補跑。")

    db, cursor = _db_cursor()
    try:
        cursor.execute(
            """
            SELECT id, origin_full_path, photo_uuid
            FROM img_upload
            WHERE IFNULL(is_deleted, 0) = 0
              AND (
                reco_status = 'FAILED'
                OR (reco_status IN ('PENDING', 'RETRY') AND IFNULL(photo_uuid, '') <> '')
              )
            ORDER BY update_time DESC, id DESC
            LIMIT %s
            """,
            (limit,),
        )
        candidates = cursor.fetchall() or []
    finally:
        cursor.close()
        db.close()

    logs: list[str] = []
    candidate_count = len(candidates)
    retried = 0
    success = 0
    failed = 0
    skip_missing_file_count = 0
    skip_no_face_count = 0
    details: list[dict] = []
    fail_reason_counter: dict[str, int] = {}

    for row in candidates:
        retried += 1
        row_id = row.get("id")
        origin_full_path = str(row.get("origin_full_path") or "")
        photo_uuid = str(row.get("photo_uuid") or "")
        reco_ok = False
        error_msg = ""
        try:
            if not origin_full_path or not os.path.isfile(origin_full_path):
                skip_missing_file_count += 1
                raise RuntimeError("找不到原始照片檔案")
            activity_photo_reco(origin_full_path, LABEL_FACE_NAME=False)
            db2, cur2 = _db_cursor()
            try:
                cur2.execute(
                    """
                    SELECT id
                    FROM reco_result
                    WHERE photo_uuid = %s AND IFNULL(is_deleted, 0) = 0
                    LIMIT 1
                    """,
                    (photo_uuid,),
                )
                reco_ok = cur2.fetchone() is not None
                if reco_ok:
                    cur2.execute(
                        """
                        UPDATE reco_result
                        SET update_time = NOW()
                        WHERE photo_uuid = %s
                        """,
                        (photo_uuid,),
                    )
                    db2.conn.commit()
                else:
                    skip_no_face_count += 1
                    error_msg = "辨識後未寫入 reco_result（可能無有效人臉或結果被略過）"
            finally:
                cur2.close()
                db2.close()
        except Exception as exc:
            error_msg = _display_text(str(exc))

        db3, cur3 = _db_cursor()
        try:
            cur3.execute(
                """
                UPDATE img_upload
                SET reco_status = %s,
                    reco_error = %s,
                    reco_last_try_time = NOW(),
                    reco_retry_count = IFNULL(reco_retry_count, 0) + 1,
                    update_time = NOW()
                WHERE id = %s
                """,
                (
                    "DONE" if reco_ok else "FAILED",
                    None if reco_ok else error_msg[:2000],
                    row_id,
                ),
            )
            db3.conn.commit()
        finally:
            cur3.close()
            db3.close()

        if reco_ok:
            success += 1
            logs.append(f"{_now_local().isoformat(sep=' ', timespec='seconds')} 補跑成功：id={row_id}, photo_uuid={photo_uuid}")
        else:
            failed += 1
            reason_key = _display_text(error_msg) or "未知錯誤"
            fail_reason_counter[reason_key] = fail_reason_counter.get(reason_key, 0) + 1
            logs.append(f"{_now_local().isoformat(sep=' ', timespec='seconds')} 補跑失敗：id={row_id}, photo_uuid={photo_uuid}, reason={reason_key}")
        details.append(
            {
                "id": row_id,
                "photo_uuid": photo_uuid,
                "origin_full_path": origin_full_path,
                "ok": reco_ok,
                "error": None if reco_ok else error_msg,
            }
        )

    daily_log_path = _activity_photo_import_daily_log_path()
    write_log_file(daily_log_path, logs)
    top_fail_reasons = [
        {"reason": reason, "count": count}
        for reason, count in sorted(fail_reason_counter.items(), key=lambda item: item[1], reverse=True)
    ][:5]
    return {
        "candidate_count": candidate_count,
        "retried_count": retried,
        "success_count": success,
        "failed_count": failed,
        "skip_missing_file_count": skip_missing_file_count,
        "skip_no_face_count": skip_no_face_count,
        "top_fail_reasons": top_fail_reasons,
        "details": details,
        "logs": logs,
        "log_path": str(daily_log_path),
    }


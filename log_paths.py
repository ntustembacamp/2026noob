from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
NOOB_LOG_ROOT = PROJECT_ROOT / "logs"
LEGACY_LOG_ROOT = PROJECT_ROOT / "database" / "logs"
LEGACY_MIGRATED_LOG_ROOT = NOOB_LOG_ROOT / "_legacy_migrated"

WINDOWS_BATCH_NORMALIZE_LOG_PATH = NOOB_LOG_ROOT / "windows_batch_normalize.log"
WINDOWS_BATCH_SERVICE_LOG_PATH = NOOB_LOG_ROOT / "windows_batch_service.log"
WINDOWS_BATCH_SERVICE_ERROR_LOG_PATH = NOOB_LOG_ROOT / "windows_batch_service.error.log"
FEATURE_BUILD_LOG_PATH = NOOB_LOG_ROOT / "feature_build.log"
ACTIVITY_PHOTO_IMPORT_LOG_PATH = NOOB_LOG_ROOT / "activity_photo_import.log"


def ensure_noob_log_root() -> Path:
    NOOB_LOG_ROOT.mkdir(parents=True, exist_ok=True)
    return NOOB_LOG_ROOT


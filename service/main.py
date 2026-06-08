# pip install fastapi
# pip install "uvicorn[standard]"
# pip install gunicorn

# gunicorn --chdir /root/noob/service/ -w 1 -k uvicorn.workers.UvicornWorker main:app --bind 0.0.0.0:8000 --log-level info --access-logfile /var/log/fastapi.log

import csv
import contextlib
import html as html_lib
import io
import json
import logging
import os
import pickle
import random
import string
import shutil
import sys
import time
import unicodedata
import zipfile
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
import pandas as pd
from PIL import Image, ImageDraw
import imagehash

try:
    from fastapi import File, UploadFile
except ImportError:  # pragma: no cover
    File = None
    UploadFile = None

try:
    import multipart  # type: ignore  # noqa: F401

    MULTIPART_AVAILABLE = True
except ImportError:
    MULTIPART_AVAILABLE = False

from new_recognize import activity_photo_reco, image_socre
from activity_workflows import (
    delete_all_photographer_master,
    delete_activity_schedule,
    delete_all_activity_schedule,
    delete_photographer_master,
    ensure_activity_tables,
    ensure_activity_tables_once,
    import_photographer_master,
    import_activity_schedule,
    list_sheet_names as list_activity_sheet_names,
    load_columns as load_activity_columns,
    list_activity_schedule_options,
    process_activity_photo_import,
    normalize_activity_photo_files,
    normalize_activity_photo_folder,
    start_normalize_activity_photos_job,
    import_activity_photos_from_normalized_folder,
    start_import_activity_photos_job,
    preview_import_source_folder,
    get_import_job_status,
    get_import_job_logs,
    get_import_job_items,
    retry_failed_activity_recognition,
    query_activity_schedule,
    query_photographer_master,
    save_uploaded_excel as save_activity_uploaded_excel,
    update_photographer_master,
    update_activity_schedule,
)
from tools.mysql_utils import mysqlconnector

if str(Path(__file__).resolve().parent.parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from log_paths import ACTIVITY_PHOTO_IMPORT_LOG_PATH, FEATURE_BUILD_LOG_PATH, LEGACY_LOG_ROOT, NOOB_LOG_ROOT

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

if not MULTIPART_AVAILABLE:
    logger.warning("python-multipart not installed; upload-related routes may be unavailable.")

PHOTO_QUERY_INDEXES_READY = False

ALLOWED_PREVIEW_PREFIXES = (
    "/mnt/activity/",
    "/root/noob/database/",
    "/mnt/database/",
    "/mnt/feature_src/",
)

BASE_DIR = Path(__file__).resolve().parent.parent
HOST_PROJECT_BASE_DIR = os.getenv(
    "HOST_PROJECT_BASE_DIR",
    r"C:\Users\Test\Desktop\Codex\AI人臉辨識\noob",
)
BATCH_UPLOAD_ROOT = BASE_DIR / "database" / "tmp_batch_uploads"
BATCH_OUTPUT_ROOT = BASE_DIR / "database" / "tmp_batch_outputs"
ACTIVITY_UPLOAD_ROOT = BASE_DIR / "database" / "activity_schedule_uploads"
WINDOWS_NORMALIZE_TOOL_BAT = f"{HOST_PROJECT_BASE_DIR}\\start_windows_normalize_tool.bat"
WINDOWS_NORMALIZE_TOOL_URI = "file:///" + quote(WINDOWS_NORMALIZE_TOOL_BAT.replace("\\", "/"), safe="/:")
WINDOWS_NORMALIZE_TOOL_PAGE = "/windows-normalize-tool"
WINDOWS_NORMALIZE_TOOL_DOWNLOAD = "/windows-normalize-tool/download"
WINDOWS_BATCH_SERVICE_PS1 = f"{HOST_PROJECT_BASE_DIR}\\start_windows_batch_service.ps1"
WINDOWS_BATCH_SERVICE_GUIDE_PAGE = "/windows-batch-service-guide"
WINDOWS_BATCH_SERVICE_GUIDE_DOWNLOAD = "/windows-batch-service-guide/download"
WINDOWS_ACTIVITY_IMPORT_TOOL_BAT = f"{HOST_PROJECT_BASE_DIR}\\start_windows_activity_import_tool.bat"
WINDOWS_ACTIVITY_IMPORT_TOOL_PAGE = "/windows-activity-import-tool"
WINDOWS_ACTIVITY_IMPORT_TOOL_DOWNLOAD = "/windows-activity-import-tool/download"
LAPTOP_TOOL_ADMIN_PAGE = "/laptop-tool-admin"
LAPTOP_TOOL_DOWNLOAD_PAGE = "/laptop-tool/download"
LAPTOP_TOOL_ADMIN_SETTINGS_PATH = BASE_DIR / "service" / "laptop_tool_admin_settings.json"
LAPTOP_TOOL_PACKAGE_ROOT = Path("/mnt/activity/laptop_tool/packages")
LAPTOP_TOOL_DOC_ROOT = Path("/mnt/activity/laptop_tool/docs")
LAPTOP_TOOL_MODEL_ROOT = BASE_DIR / "service" / "models"
LAPTOP_TOOL_STAGING_ROOT = Path("/mnt/activity/laptop_tool/staging")
LAPTOP_TOOL_DIST_ROOT = BASE_DIR / "dist" / "laptop_tool"
EMBEDDING_PKL_PATH = BASE_DIR / "service" / "embedding" / "faces_embedding_antelopev2.pkl"
EMBEDDING_BACKUP_ROOT = BASE_DIR / "service" / "embedding" / "backups"
EMBEDDING_MODEL_NAME = "antelopev2"
LAPTOP_TOOL_RECOMMENDED_MODEL_ZIP = "antelopev2.zip"
UI_TEMPLATE_DIR = BASE_DIR / "service" / "ui_templates"
LOG_DIR = NOOB_LOG_ROOT
LOCAL_TZ = ZoneInfo("Asia/Taipei")
EXPORT_ROOT_RUNTIME = Path("/mnt/activity/exports/query_selected")
EXPORT_TOKEN_TTL_SECONDS = 600
EXPORT_TOKEN_STORE: dict[str, dict] = {}


def _now_tpe() -> datetime:
    return datetime.now(LOCAL_TZ)


def _now_utc() -> datetime:
    return datetime.now(ZoneInfo("UTC"))

app = FastAPI(
    title="AI人臉辨識系統 API",
    version="1.4.0",
    description="提供人員建置、活動照片匯入、辨識查詢與管理功能。",
)


MOJIBAKE_PATTERNS = (
    "ä", "å", "ç", "è", "é", "æ", "Ã", "Â", "ðŸ", "ï¼", "ï½", "???", "�"
)


@app.middleware("http")
async def enforce_utf8_charset(request: Request, call_next):
    response = await call_next(request)
    content_type = response.headers.get("content-type", "")
    if "charset=" not in content_type and (
        content_type.startswith("text/")
        or content_type.startswith("application/json")
        or content_type.startswith("application/javascript")
    ):
        response.headers["content-type"] = f"{content_type}; charset=utf-8"
    return response


def sync_processing_wrapper(file_path: str, label_flag: bool):
    try:
        logger.info(f"開始處理: {file_path}")
        activity_photo_reco(file_full_path=file_path, LABEL_FACE_NAME=label_flag)
        logger.info(f"完成處理: {file_path}")
    except Exception as e:
        logger.error(f"處理失敗: {str(e)}")


def normalize_record_rows(rows):
    for row in rows:
        try:
            row["reco_name"] = json.loads(row["reco_name"]) if row.get("reco_name") else []
        except (json.JSONDecodeError, TypeError):
            pass

        try:
            row["reco_res"] = json.loads(row["reco_res"]) if row.get("reco_res") else []
        except (json.JSONDecodeError, TypeError):
            pass

        for key in ("create_time", "update_time", "photo_create_time", "photo_taken_time", "photo_file_time", "record_create_time"):
            if row.get(key) is not None and hasattr(row[key], "isoformat"):
                row[key] = row[key].isoformat(sep=" ", timespec="seconds")

    return rows


def write_ui_log(log_path: Path, lines: list[str]):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8-sig", newline="\n") as handle:
        handle.write("\n".join(lines or []))


def build_thumbnail_candidate(file_path: str):
    normalized = os.path.normpath(file_path).replace("\\", "/")
    if "/mnt/activity/dev/thumbs/" not in normalized:
        return None

    stem, suffix = os.path.splitext(normalized)
    if stem.endswith("_thumb"):
        return None
    return f"{stem}_thumb{suffix}"


def parse_datetime_filter(value: str, is_end: bool = False):
    if not value:
        return None

    formats = ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d")
    parsed = None

    for fmt in formats:
        try:
            parsed = datetime.strptime(value, fmt)
            break
        except ValueError:
            continue

    if parsed is None:
        raise ValueError("日期時間格式請使用 YYYY-MM-DDTHH:MM")

    if is_end:
        if "T" in value or " " in value:
            return parsed + timedelta(hours=1)
        return parsed + timedelta(days=1)

    return parsed


def _format_datetime_tpe(value) -> str:
    if not value:
        return ""
    if isinstance(value, str):
        return value
    if not isinstance(value, datetime):
        return str(value)
    # MySQL DATETIME 常為 naive；本系統約定視為 UTC 後轉台北時間顯示。
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo("UTC"))
    return value.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _looks_like_mojibake(text: str) -> bool:
    if not text:
        return False
    value = str(text)
    return any(pattern in value for pattern in MOJIBAKE_PATTERNS)


def _collect_recent_log_issues(max_lines: int = 500) -> list[dict]:
    issues = []
    if not LOG_DIR.exists():
        return issues
    for log_file in sorted(LOG_DIR.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:6]:
        try:
            lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()[-max_lines:]
        except Exception:
            continue
        for index, line in enumerate(lines, 1):
            if _looks_like_mojibake(line):
                issues.append(
                    {
                        "file": str(log_file),
                        "line": index,
                        "snippet": line[:220],
                    }
                )
                if len(issues) >= 30:
                    return issues
    return issues


def render_clean_home_html() -> str:
    return """
    <!DOCTYPE html>
    <html lang="zh-Hant">
    <head>
      <meta charset="utf-8" />
      <meta name="viewport" content="width=device-width, initial-scale=1" />
      <title>AI人臉辨識系統</title>
      <style>
        body { margin: 0; font-family: "Segoe UI", "Noto Sans TC", sans-serif; background: #f7f3eb; color: #1f2937; }
        .wrap { max-width: 1100px; margin: 0 auto; padding: 36px 20px 48px; }
        .hero { background: #fffdf8; border: 1px solid #ddd1bc; border-radius: 22px; padding: 28px; box-shadow: 0 16px 40px rgba(56,46,24,.08); }
        h1 { margin: 0 0 10px; font-size: 34px; }
        p { margin: 0; color: #6b7280; line-height: 1.7; }
        .groups { display: grid; gap: 18px; margin-top: 24px; }
        .group { padding: 18px; border-radius: 18px; background: #faf6ed; border: 1px solid #eadfcb; }
        .group h2 { margin: 0 0 8px; font-size: 20px; }
        .group p { margin: 0 0 14px; font-size: 14px; }
        .links { display: flex; flex-wrap: wrap; gap: 12px; }
        a.button { text-decoration: none; color: white; background: #0f766e; padding: 12px 18px; border-radius: 999px; font-weight: 700; }
        a.secondary { background: #e1f4ef; color: #0f766e; }
      </style>
    </head>
    <body>
      <div class="wrap">
        <div class="hero">
          <h1>AI人臉辨識系統</h1>
          <p>提供人員主檔維護、特徵建置、活動照片匯入、人臉辨識查詢與 API 文件入口。</p>
          <div class="groups">
            <section class="group">
              <h2>人員資料與建置</h2>
              <p>先整理人員照片，再建立 base 主檔與人臉 embedding 特徵庫。</p>
              <div class="links">
                <a class="button" href="/admin-batch-ui">人員資料及特徵建置</a>
                <a class="button secondary" href="/admin-ui">人員資料查詢與維護</a>
              </div>
            </section>
            <section class="group">
              <h2>活動照片建置與辨識</h2>
              <p>先建立活動行程，再匯入活動照片，系統會寫入照片資料並觸發人臉辨識。</p>
              <div class="links">
                <a class="button" href="/activity-schedule-ui">活動行程建立</a>
                <a class="button secondary" href="/activity-photo-normalize-ui">活動照片正規化處理</a>
                <a class="button secondary" href="/activity-photo-import-ui">活動照片匯入入庫與後續辨識</a>
                <a class="button secondary" href="/photographer-ui">攝影師資料建立維護</a>
                <a class="button secondary" href="/activity-award-ui">活動獎項資料建立維護</a>
              </div>
            </section>
            <section class="group">
              <h2>AI人臉辨識系統工具程式</h2>
              <p>查看各台筆電的上傳作業、目前進度與最近狀態，方便快速確認哪一台正在傳檔。</p>
              <div class="links">
                <a class="button secondary" href="/laptop-tool-admin">AI人臉辨識系統工具程式設定維護</a>
                <a class="button secondary" href="/laptop-tool-upload-monitor">AI人臉辨識系統工具程式上傳作業檢視</a>
              </div>
            </section>
            <section class="group">
              <h2>活動照片辨識查詢</h2>
              <p>依人員、建立時間、拍攝時間與活動照片結果條件進行查詢，並可查看縮圖與匯出 CSV。</p>
              <div class="links">
                <a class="button" href="/query-ui-advanced">活動照片辨識查詢</a>
              </div>
            </section>
            <section class="group">
              <h2>API 文件</h2>
              <p>查看 Swagger UI 與 OpenAPI JSON。</p>
              <div class="links">
                <a class="button secondary" href="/docs">API 文件</a>
                <a class="button secondary" href="/openapi.json">OpenAPI JSON</a>
              </div>
            </section>
          </div>
        </div>
      </div>
    </body>
    </html>
    """


def render_laptop_tool_upload_monitor_html() -> str:
    return """
    <!DOCTYPE html>
    <html lang="zh-Hant">
    <head>
      <meta charset="utf-8" />
      <meta name="viewport" content="width=device-width, initial-scale=1" />
      <title>AI人臉辨識系統工具程式上傳作業檢視</title>
      <style>
        :root {
          --bg: #f4efe7;
          --panel: #fffdfa;
          --ink: #1f2937;
          --muted: #64748b;
          --line: #d8cfbf;
          --accent: #0f766e;
          --accent-2: #92400e;
          --good: #15803d;
          --bad: #b91c1c;
          --info: #1d4ed8;
        }
        * { box-sizing: border-box; }
        body { margin: 0; font-family: "Segoe UI", "Noto Sans TC", sans-serif; background: linear-gradient(180deg, #e8ddcd, var(--bg)); color: var(--ink); }
        .wrap { max-width: 1320px; margin: 0 auto; padding: 28px 18px 56px; }
        .hero { background: var(--panel); border: 1px solid var(--line); border-radius: 22px; padding: 24px; box-shadow: 0 16px 40px rgba(56, 46, 24, .08); }
        h1 { margin: 0 0 8px; font-size: 32px; }
        .sub { margin: 0; color: var(--muted); line-height: 1.7; }
        .toolbar { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 18px; align-items: end; }
        .field { display: flex; flex-direction: column; gap: 6px; }
        .field span { font-weight: 700; }
        input[type="text"] { min-width: 220px; padding: 10px 12px; border: 1px solid var(--line); border-radius: 10px; background: #fff; }
        label.check { display: inline-flex; align-items: center; gap: 8px; padding: 10px 12px; border: 1px solid var(--line); border-radius: 10px; background: #fff; }
        .btn, button { display: inline-flex; align-items: center; justify-content: center; padding: 10px 14px; border-radius: 12px; border: 0; text-decoration: none; color: #fff; background: linear-gradient(135deg, var(--accent), var(--accent-2)); cursor: pointer; font-weight: 700; }
        .btn.secondary, button.secondary { background: #6b7280; }
        .btn.ghost { background: #e2e8f0; color: #111827; }
        .summary-grid { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 12px; margin-top: 18px; }
        .summary-card { background: #fff; border: 1px solid var(--line); border-radius: 16px; padding: 14px; }
        .summary-card strong { display: block; font-size: 28px; line-height: 1.1; }
        .summary-card span { color: var(--muted); font-size: 14px; }
        .panel { margin-top: 18px; background: var(--panel); border: 1px solid var(--line); border-radius: 18px; padding: 18px; box-shadow: 0 12px 32px rgba(41, 33, 18, .06); }
        .panel h2 { margin: 0 0 12px; font-size: 22px; }
        .hint { color: var(--muted); margin: 0 0 10px; line-height: 1.6; }
        .status { margin-top: 14px; padding: 10px 12px; border-radius: 10px; background: #eef8f6; color: var(--accent); white-space: pre-wrap; }
        .table-wrap { overflow-x: auto; }
        table { width: 100%; border-collapse: collapse; }
        th, td { text-align: left; padding: 10px 8px; border-bottom: 1px solid #e8dfd0; vertical-align: top; }
        th { color: var(--muted); font-size: 13px; }
        td { font-size: 14px; }
        .badge { display: inline-flex; align-items: center; padding: 4px 10px; border-radius: 999px; font-size: 12px; font-weight: 700; }
        .badge.queued { background: #e2e8f0; color: #334155; }
        .badge.running { background: #dbeafe; color: var(--info); }
        .badge.done { background: #dcfce7; color: var(--good); }
        .badge.failed { background: #fee2e2; color: var(--bad); }
        .badge.unknown { background: #f3f4f6; color: #6b7280; }
        .progress { width: 100%; height: 10px; border-radius: 999px; background: #e5e7eb; overflow: hidden; }
        .progress > span { display: block; height: 100%; background: linear-gradient(90deg, var(--accent), #2dd4bf); }
        .stack { display: grid; gap: 8px; }
        .small { color: var(--muted); font-size: 12px; }
        .foot { margin-top: 14px; color: var(--muted); }
        .empty { padding: 14px; border: 1px dashed #d6c9b5; border-radius: 14px; color: var(--muted); background: #fff; }
        @media (max-width: 1200px) {
          .summary-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
        }
        @media (max-width: 760px) {
          .summary-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
          .toolbar { align-items: stretch; }
          input[type="text"] { min-width: 0; width: 100%; }
        }
      </style>
    </head>
    <body>
      <div class="wrap">
        <div class="hero">
          <h1>AI人臉辨識系統工具程式上傳作業檢視</h1>
          <p class="sub">只讀監看頁，集中查看各台筆電上傳作業的即時狀態、最近更新與失敗摘要，不影響既有上傳與 commit 流程。</p>
          <div class="toolbar">
            <div class="field">
              <span>筆電編號（device_id）</span>
              <input id="deviceFilter" type="text" placeholder="留空表示全部" />
            </div>
            <label class="check">
              <input id="activeOnly" type="checkbox" />
              <span>只看上傳中</span>
            </label>
            <div class="field">
              <span>顯示筆數</span>
              <input id="limitInput" type="text" value="30" />
            </div>
            <button id="refreshBtn" type="button">重新整理</button>
            <a class="btn ghost" href="/">回首頁</a>
          </div>
        </div>

        <div class="summary-grid">
          <div class="summary-card"><strong id="activeDeviceCount">0</strong><span>上傳中筆電</span></div>
          <div class="summary-card"><strong id="activeJobCount">0</strong><span>上傳中 Job</span></div>
          <div class="summary-card"><strong id="totalJobCount">0</strong><span>總 Job 數</span></div>
          <div class="summary-card"><strong id="uploadedTotal">0</strong><span>累計上傳數</span></div>
          <div class="summary-card"><strong id="committedTotal">0</strong><span>累計提交數</span></div>
          <div class="summary-card"><strong id="failedTotal">0</strong><span>累計失敗數</span></div>
        </div>

        <section class="panel">
          <h2>目前上傳中</h2>
          <p class="hint">顯示 status 為 QUEUED / RUNNING 的作業，方便快速查看哪幾台筆電正在上傳。</p>
          <div id="activeJobs" class="table-wrap"></div>
        </section>

        <section class="panel">
          <h2>最近作業</h2>
          <p class="hint">顯示最近更新的作業；可搭配上方 device_id 過濾。</p>
          <div id="recentJobs" class="table-wrap"></div>
        </section>

        <div id="status" class="status">載入中...</div>
        <div class="foot">提示：此頁每 15 秒自動更新一次，頁面停留時可即時看到新的上傳狀態。</div>
      </div>
      <script>
        const ids = {
          deviceFilter: document.getElementById('deviceFilter'),
          activeOnly: document.getElementById('activeOnly'),
          limitInput: document.getElementById('limitInput'),
          refreshBtn: document.getElementById('refreshBtn'),
          activeDeviceCount: document.getElementById('activeDeviceCount'),
          activeJobCount: document.getElementById('activeJobCount'),
          totalJobCount: document.getElementById('totalJobCount'),
          uploadedTotal: document.getElementById('uploadedTotal'),
          committedTotal: document.getElementById('committedTotal'),
          failedTotal: document.getElementById('failedTotal'),
          activeJobs: document.getElementById('activeJobs'),
          recentJobs: document.getElementById('recentJobs'),
          status: document.getElementById('status'),
        };

        function esc(value) {
          return String(value ?? '').replace(/[&<>"']/g, s => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[s]));
        }

        function badge(status, label) {
          const key = String(status || '').toLowerCase();
          return `<span class="badge ${key || 'unknown'}">${esc(label || status || '未知')}</span>`;
        }

        function progressBar(pct) {
          const safe = Math.max(0, Math.min(100, Number(pct || 0)));
          return `<div class="progress" title="${safe}%"><span style="width:${safe}%"></span></div>`;
        }

        function jobRowsHtml(items) {
          if (!items || !items.length) {
            return '<div class="empty">目前沒有資料。</div>';
          }
          const rows = items.map(item => {
            const jobId = esc(item.job_id);
            const statusBadge = badge(item.status, item.status_label);
            const device = esc(item.device_id || '');
            const laptop = esc(item.laptop_label || '');
            const model = esc(item.model_version || '');
            const total = Number(item.total_count || 0);
            const uploaded = Number(item.uploaded_count || 0);
            const committed = Number(item.committed_count || 0);
            const failed = Number(item.failed_count || 0);
            const progress = Number(item.progress_pct || 0);
            const commitPct = Number(item.commit_pct || 0);
            const updated = esc(item.updated_at || '');
            const finished = esc(item.finished_at || '');
            const errorSummary = esc(item.error_summary || '');
            return `
              <tr>
                <td>
                  <div class="stack">
                    <div><code>${jobId}</code> ${statusBadge}</div>
                    <div class="small">裝置：<strong>${device || 'N/A'}</strong>　標籤：${laptop || 'N/A'}　模型：${model || 'N/A'}</div>
                    <div class="small">更新時間：${updated || 'N/A'}${finished ? `　完成時間：${finished}` : ''}</div>
                  </div>
                </td>
                <td>
                  <div class="stack">
                    ${progressBar(progress)}
                    <div class="small">上傳 ${uploaded} / ${total} (${progress}%)</div>
                    <div class="small">提交 ${committed} / ${total} (${commitPct}%)　失敗 ${failed}</div>
                  </div>
                </td>
                <td>${errorSummary ? `<div class="small" style="white-space:pre-wrap;">${errorSummary}</div>` : '<span class="small">-</span>'}</td>
                <td><a href="/laptop-tool/upload-batch/${encodeURIComponent(item.job_id || '')}" target="_blank" rel="noreferrer">JSON</a></td>
              </tr>
            `;
          }).join('');
          return `
            <table>
              <thead>
                <tr>
                  <th style="min-width:360px;">作業資訊</th>
                  <th style="min-width:260px;">進度</th>
                  <th style="min-width:300px;">錯誤摘要</th>
                  <th style="width:80px;">檢視</th>
                </tr>
              </thead>
              <tbody>${rows}</tbody>
            </table>
          `;
        }

        function setSummary(summary) {
          ids.activeDeviceCount.textContent = String(summary.active_device_count || 0);
          ids.activeJobCount.textContent = String(summary.active_job_count || 0);
          ids.totalJobCount.textContent = String(summary.total_job_count || 0);
          ids.uploadedTotal.textContent = String(summary.uploaded_total || 0);
          ids.committedTotal.textContent = String(summary.committed_total || 0);
          ids.failedTotal.textContent = String(summary.failed_total || 0);
        }

        function setStatus(text, isError = false) {
          ids.status.textContent = text;
          ids.status.style.background = isError ? '#fee2e2' : '#eef8f6';
          ids.status.style.color = isError ? '#b91c1c' : '#0f766e';
        }

        async function loadData() {
          const params = new URLSearchParams();
          params.set('limit', String(Math.max(1, Math.min(100, Number(ids.limitInput.value || 30) || 30))));
          const deviceId = String(ids.deviceFilter.value || '').trim();
          if (deviceId) params.set('device_id', deviceId);
          if (ids.activeOnly.checked) params.set('active_only', '1');
          const response = await fetch(`/laptop-tool/upload-monitor/data?${params.toString()}`, { cache: 'no-store' });
          const payload = await response.json();
          if (!response.ok) throw new Error(payload.detail || '載入上傳監看資料失敗');
          setSummary(payload.summary || {});
          ids.activeJobs.innerHTML = jobRowsHtml(payload.active_jobs || []);
          ids.recentJobs.innerHTML = jobRowsHtml(payload.recent_jobs || []);
          const filters = payload.filters || {};
          const activeCount = Number(payload.summary?.active_job_count || 0);
          const deviceText = filters.device_id ? `，device_id=${filters.device_id}` : '';
          const activeText = filters.active_only ? '，只看上傳中' : '';
          const limitText = filters.limit ? `，顯示筆數=${filters.limit}` : '';
          setStatus(`已更新監看資料：上傳中 Job ${activeCount} 筆${deviceText}${activeText}${limitText}。最後更新：${payload.summary?.last_updated_at || 'N/A'}`);
        }

        ids.refreshBtn.addEventListener('click', async () => {
          try { await loadData(); } catch (error) { setStatus(error.message || String(error), true); }
        });
        ids.deviceFilter.addEventListener('keydown', async (event) => {
          if (event.key === 'Enter') {
            event.preventDefault();
            try { await loadData(); } catch (error) { setStatus(error.message || String(error), true); }
          }
        });
        ids.activeOnly.addEventListener('change', async () => {
          try { await loadData(); } catch (error) { setStatus(error.message || String(error), true); }
        });
        ids.limitInput.addEventListener('change', async () => {
          try { await loadData(); } catch (error) { setStatus(error.message || String(error), true); }
        });
        loadData().catch(error => setStatus(error.message || String(error), true));
        setInterval(() => {
          loadData().catch(error => setStatus(error.message || String(error), true));
        }, 15000);
      </script>
    </body>
    </html>
    """


def load_ui_template(filename: str) -> str:
    return (UI_TEMPLATE_DIR / filename).read_text(encoding="utf-8")


def html_no_cache_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    }


def _normalize_laptop_upload_job_row(row: dict) -> dict:
    total_count = int(row.get("total_count") or 0)
    uploaded_count = int(row.get("uploaded_count") or 0)
    committed_count = int(row.get("committed_count") or 0)
    failed_count = int(row.get("failed_count") or 0)
    status = str(row.get("status") or "").strip().upper()
    progress_pct = round((uploaded_count / total_count) * 100, 1) if total_count > 0 else 0.0
    commit_pct = round((committed_count / total_count) * 100, 1) if total_count > 0 else 0.0
    return {
        "job_id": str(row.get("job_id") or "").strip(),
        "status": status,
        "status_label": {
            "QUEUED": "排隊中",
            "RUNNING": "上傳中",
            "PAUSED": "已暫停",
            "CANCELED": "已取消",
            "DONE": "完成",
            "FAILED": "失敗",
        }.get(status, status or "未知"),
        "active": status in {"QUEUED", "RUNNING"},
        "device_id": str(row.get("device_id") or "").strip(),
        "laptop_label": str(row.get("laptop_label") or "").strip(),
        "model_version": str(row.get("model_version") or "").strip(),
        "total_count": total_count,
        "uploaded_count": uploaded_count,
        "committed_count": committed_count,
        "failed_count": failed_count,
        "progress_pct": progress_pct,
        "commit_pct": commit_pct,
        "error_summary": str(row.get("error_summary") or "").strip(),
        "staging_dir": str(row.get("staging_dir") or "").strip(),
        "created_at": _format_datetime_tpe(row.get("created_at")),
        "updated_at": _format_datetime_tpe(row.get("updated_at")),
        "finished_at": _format_datetime_tpe(row.get("finished_at")),
    }


def _fetch_laptop_tool_upload_monitor_snapshot(
    cursor,
    *,
    device_id: str = "",
    active_only: bool = False,
    limit: int = 30,
) -> dict:
    cursor.execute(
        """
        SELECT
            COUNT(*) AS total_job_count,
            SUM(CASE WHEN status IN ('QUEUED', 'RUNNING') THEN 1 ELSE 0 END) AS active_job_count,
            COUNT(DISTINCT CASE WHEN status IN ('QUEUED', 'RUNNING') THEN device_id ELSE NULL END) AS active_device_count,
            SUM(CASE WHEN status = 'PAUSED' THEN 1 ELSE 0 END) AS paused_job_count,
            SUM(CASE WHEN status = 'CANCELED' THEN 1 ELSE 0 END) AS canceled_job_count,
            SUM(CASE WHEN status = 'DONE' THEN 1 ELSE 0 END) AS done_job_count,
            SUM(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END) AS failed_job_count,
            COALESCE(SUM(uploaded_count), 0) AS uploaded_total,
            COALESCE(SUM(committed_count), 0) AS committed_total,
            COALESCE(SUM(failed_count), 0) AS failed_total,
            MAX(updated_at) AS last_updated_at
        FROM laptop_upload_job
        """
    )
    summary = cursor.fetchone() or {}

    filters: list[str] = []
    params: list[object] = []
    did = str(device_id or "").strip()
    if did:
        filters.append("device_id = %s")
        params.append(did)

    active_filters = list(filters)
    active_filters.append("status IN ('QUEUED', 'RUNNING')")
    active_where = " AND ".join(active_filters) if active_filters else "1=1"
    cursor.execute(
        f"""
        SELECT
            job_id, status, device_id, laptop_label, model_version,
            total_count, uploaded_count, committed_count, failed_count,
            error_summary, staging_dir, created_at, updated_at, finished_at
        FROM laptop_upload_job
        WHERE {active_where}
        ORDER BY COALESCE(updated_at, created_at) DESC
        LIMIT %s
        """,
        tuple(params + [int(limit)]),
    )
    active_rows = [_normalize_laptop_upload_job_row(row) for row in (cursor.fetchall() or [])]

    recent_filters = list(filters)
    recent_filters.append("status NOT IN ('QUEUED', 'RUNNING')")
    recent_where = " AND ".join(recent_filters) if recent_filters else "1=1"
    cursor.execute(
        f"""
        SELECT
            job_id, status, device_id, laptop_label, model_version,
            total_count, uploaded_count, committed_count, failed_count,
            error_summary, staging_dir, created_at, updated_at, finished_at
        FROM laptop_upload_job
        WHERE {recent_where}
        ORDER BY COALESCE(updated_at, created_at) DESC
        LIMIT %s
        """,
        tuple(params + [int(limit)]),
    )
    recent_rows = [_normalize_laptop_upload_job_row(row) for row in (cursor.fetchall() or [])]

    return {
        "summary": {
            "total_job_count": int(summary.get("total_job_count") or 0),
            "active_job_count": int(summary.get("active_job_count") or 0),
            "active_device_count": int(summary.get("active_device_count") or 0),
            "paused_job_count": int(summary.get("paused_job_count") or 0),
            "canceled_job_count": int(summary.get("canceled_job_count") or 0),
            "done_job_count": int(summary.get("done_job_count") or 0),
            "failed_job_count": int(summary.get("failed_job_count") or 0),
            "uploaded_total": int(summary.get("uploaded_total") or 0),
            "committed_total": int(summary.get("committed_total") or 0),
            "failed_total": int(summary.get("failed_total") or 0),
            "last_updated_at": _format_datetime_tpe(summary.get("last_updated_at")),
        },
        "filters": {
            "device_id": did,
            "active_only": bool(active_only),
            "limit": int(limit),
        },
        "active_jobs": active_rows,
        "recent_jobs": recent_rows,
    }


def _read_laptop_tool_admin_settings() -> dict:
    settings = {
        "server_api_base": "",
        "public_base_url": "",
        "default_activity_code": "",
        "default_photographer": "",
        "updated_at": "",
    }
    if not LAPTOP_TOOL_ADMIN_SETTINGS_PATH.exists():
        return settings
    try:
        raw = json.loads(LAPTOP_TOOL_ADMIN_SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return settings
    if not isinstance(raw, dict):
        return settings
    server_api_base = str(raw.get("server_api_base") or raw.get("public_base_url") or "").strip().rstrip("/")
    settings.update(raw)
    settings["server_api_base"] = server_api_base
    settings["public_base_url"] = server_api_base
    settings["default_activity_code"] = str(raw.get("default_activity_code") or "").strip().upper()
    settings["default_photographer"] = str(raw.get("default_photographer") or "").strip()
    settings["updated_at"] = str(raw.get("updated_at") or "").strip()
    return settings


def _resolve_laptop_tool_request_base(request: Request) -> str:
    request_base = str(request.base_url).rstrip("/")
    host_header = str(request.headers.get("host") or "").strip()
    header_base = f"{request.url.scheme}://{host_header}".rstrip("/") if host_header else ""
    if header_base and not any(token in header_base.lower() for token in ("localhost", "127.0.0.1")):
        return header_base
    return request_base


def _resolve_laptop_tool_server_base(settings: dict, request: Request | None = None) -> str:
    configured = str((settings or {}).get("server_api_base") or (settings or {}).get("public_base_url") or "").strip().rstrip("/")
    if configured:
        return configured
    if request is not None:
        return _resolve_laptop_tool_request_base(request)
    return ""


def _save_laptop_tool_admin_settings(server_api_base: str, default_activity_code: str = "", default_photographer: str = "") -> dict:
    resolved_base = str(server_api_base or "").strip().rstrip("/")
    if not resolved_base:
        raise ValueError("請輸入對外 IP / Base URL。")
    payload = dict(_read_laptop_tool_admin_settings())
    payload["server_api_base"] = resolved_base
    payload["public_base_url"] = resolved_base
    payload["default_activity_code"] = str(default_activity_code or "").strip().upper()
    payload["default_photographer"] = str(default_photographer or "").strip()
    payload["updated_at"] = _now_tpe().isoformat(timespec="seconds")
    LAPTOP_TOOL_ADMIN_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    LAPTOP_TOOL_ADMIN_SETTINGS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    return payload


def _cleanup_laptop_tool_staging_dir(job_id: str, staging_dir: str | Path | None) -> tuple[bool, str]:
    """
    保守清理：只刪除單一 job 的 staging 子目錄，且必須位於既定 staging root 底下。
    """
    if not staging_dir:
        return False, "staging_dir 空白，略過清理"
    try:
        target = Path(staging_dir).resolve()
        root = LAPTOP_TOOL_STAGING_ROOT.resolve()
        try:
            target.relative_to(root)
        except ValueError:
            return False, f"staging_dir 不在允許範圍內：{target}"
        if target.exists():
            shutil.rmtree(target)
            return True, f"已清除 staging：{target}"
        return True, f"staging 已不存在：{target}"
    except Exception as exc:
        logger.warning("清除 laptop_tool staging 失敗 job_id=%s staging_dir=%s error=%s", job_id, staging_dir, exc)
        return False, f"清除 staging 失敗：{exc}"


def render_clean_activity_schedule_ui_html() -> str:
    return """
    <!DOCTYPE html>
    <html lang="zh-Hant">
    <head>
      <meta charset="utf-8" />
      <meta name="viewport" content="width=device-width, initial-scale=1" />
      <title>活動行程建立</title>
      <style>
        :root {
          --bg: #f4efe7; --panel: #fffdfa; --ink: #1f2937; --muted: #6b7280;
          --line: #d8cfbf; --accent: #92400e; --accent2: #0f766e;
        }
        * { box-sizing: border-box; }
        body { margin: 0; font-family: "Segoe UI","Noto Sans TC",sans-serif; color: var(--ink); background: linear-gradient(180deg, #e6ddcf, var(--bg)); min-height: 100vh; }
        .wrap { max-width: 1320px; margin: 0 auto; padding: 32px 20px 56px; }
        .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 24px; padding: 24px; box-shadow: 0 18px 46px rgba(41,33,18,.08); }
        .topbar { display:flex; justify-content:space-between; align-items:flex-start; gap:16px; margin-bottom:16px; }
        h1 { margin:0 0 8px; font-size:34px; }
        h2 { margin:0 0 12px; font-size:22px; }
        p,label { color:var(--muted); line-height:1.7; }
        .actions { display:flex; flex-wrap:wrap; gap:12px; }
        .section { margin-top:20px; padding:20px; border:1px solid #ece3d4; border-radius:20px; background:#fffaf4; }
        .grid { display:grid; grid-template-columns: 220px minmax(220px,1fr); gap:12px 16px; align-items:center; }
        .grid .full { grid-column: 1 / -1; }
        input, select, button, .file-button { width:100%; border-radius:14px; border:1px solid var(--line); padding:12px 14px; font-size:15px; background:white; }
        button, .file-button { border:0; background: linear-gradient(135deg, var(--accent), var(--accent2)); color:white; cursor:pointer; font-weight:700; text-align:center; text-decoration:none; display:inline-flex; align-items:center; justify-content:center; }
        button.secondary, a.link-button.secondary { background:#e8f4f1; color:#0f766e; }
        .file-button input { display:none; }
        .status { margin-top:14px; padding:14px 16px; border-radius:14px; background:#eef8f6; color:#0f766e; white-space:pre-wrap; }
        .status.error { background:#fff1f2; color:#be123c; }
        .result-box { margin-top:14px; padding:16px; min-height:220px; border-radius:18px; background:#fffdf8; border:1px solid #eadfcb; overflow:auto; white-space:pre-wrap; font-family:Consolas,"Courier New",monospace; font-size:13px; }
        .table-wrap { margin-top:16px; overflow:auto; }
        table { width:100%; border-collapse:collapse; background:white; border-radius:16px; overflow:hidden; }
        th, td { padding:12px 10px; border-bottom:1px solid #eee4d5; text-align:left; vertical-align:top; }
        th { background:#f8efe3; }
        td.note { min-width:340px; white-space:pre-wrap; line-height:1.6; }
        @media (max-width: 920px) { .grid { grid-template-columns: 1fr; } }
      </style>
    </head>
    <body>
      <div class="wrap">
        <div class="panel">
          <div class="topbar">
            <div>
              <h1>活動行程建立</h1>
              <p>先上傳 Excel 或 CSV，讀取欄位後匯入活動日期、時間、內容、負責組別、地點與備註；下方可直接查詢與全部刪除。</p>
            </div>
            <div class="actions">
              <a class="file-button" href="/">回到首頁</a>
              <a class="file-button" href="/activity-photo-import-ui">前往活動照片匯入入庫與後續辨識</a>
            </div>
          </div>

          <section class="section">
            <h2>Excel 匯入</h2>
            <div class="grid">
              <label>活動行程 Excel</label>
              <input id="excelPath" readonly placeholder="請選擇活動行程 Excel 或 CSV" />
              <label class="file-button">選擇檔案<input id="excelFileInput" type="file" accept=".xlsx,.xls,.xlsm,.csv" /></label>
              <div></div>

              <label>工作表</label>
              <select id="sheetName"></select>
              <button id="loadColumnsBtn" type="button">讀取欄位</button>
              <div></div>

              <label>編號欄位</label><select id="activityCodeColumn"></select>
              <label>日期欄位</label><select id="activityDateColumn"></select>
              <label>時間欄位</label><select id="activityTimeColumn"></select>
              <label>內容欄位</label><select id="activityContentColumn"></select>
              <label>負責組別欄位</label><select id="ownerTeamColumn"></select>
              <label>地點欄位</label><select id="locationColumn"></select>
              <label>備註欄位</label><select id="noteColumn"></select>
            </div>
            <div class="actions" style="margin-top:16px;">
              <button id="importBtn" type="button">匯入活動行程</button>
            </div>
            <div id="importStatus" class="status">匯入結果會顯示在這裡。</div>
            <pre id="importResult" class="result-box"></pre>
          </section>

          <section class="section">
            <h2>活動行程查詢及維護</h2>
            <div class="grid">
              <label>活動日期</label><input id="queryDate" type="date" />
              <label>關鍵字</label><input id="queryKeyword" class="full" placeholder="可搜尋內容、地點或備註" />
            </div>
            <div class="actions" style="margin-top:16px;">
              <button id="queryBtn" type="button">查詢活動行程</button>
              <button id="deleteAllBtn" type="button">全部刪除</button>
            </div>
            <div id="queryStatus" class="status">查詢結果會顯示在下方表格。</div>
            <div class="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>日期</th>
                    <th>時間區間</th>
                    <th>編號</th>
                    <th>內容</th>
                    <th>負責組別</th>
                    <th>地點</th>
                    <th>備註</th>
                    <th>建立時間</th>
                    <th>更新時間</th>
                  </tr>
                </thead>
                <tbody id="queryRows">
                  <tr><td colspan="9">請先查詢活動行程。</td></tr>
                </tbody>
              </table>
            </div>
          </section>
        </div>
      </div>
      <script>
        const ids = {
          excelPath: document.getElementById('excelPath'),
          excelFileInput: document.getElementById('excelFileInput'),
          sheetName: document.getElementById('sheetName'),
          activityCodeColumn: document.getElementById('activityCodeColumn'),
          activityDateColumn: document.getElementById('activityDateColumn'),
          activityTimeColumn: document.getElementById('activityTimeColumn'),
          activityContentColumn: document.getElementById('activityContentColumn'),
          ownerTeamColumn: document.getElementById('ownerTeamColumn'),
          locationColumn: document.getElementById('locationColumn'),
          noteColumn: document.getElementById('noteColumn'),
          loadColumnsBtn: document.getElementById('loadColumnsBtn'),
          importBtn: document.getElementById('importBtn'),
          importStatus: document.getElementById('importStatus'),
          importResult: document.getElementById('importResult'),
          queryDate: document.getElementById('queryDate'),
          queryKeyword: document.getElementById('queryKeyword'),
          queryBtn: document.getElementById('queryBtn'),
          deleteAllBtn: document.getElementById('deleteAllBtn'),
          queryStatus: document.getElementById('queryStatus'),
          queryRows: document.getElementById('queryRows'),
        };
        let uploadedExcelServerPath = '';

        function setStatus(el, msg, isError = false) {
          el.textContent = msg;
          el.className = isError ? 'status error' : 'status';
        }
        function fillSelect(select, columns, allowEmpty = true) {
          const items = [];
          if (allowEmpty) items.push('<option value="">請選擇欄位</option>');
          for (const col of columns) items.push(`<option value="${String(col).replaceAll('"','&quot;')}">${col}</option>`);
          select.innerHTML = items.join('');
        }
        function fillSheetNames(names, selected='') {
          const items = (names && names.length ? names : ['CSV']).map(name => `<option value="${name}">${name}</option>`);
          ids.sheetName.innerHTML = items.join('');
          ids.sheetName.value = selected || (names && names.length ? names[0] : 'CSV');
        }
        async function uploadExcel(file) {
          const formData = new FormData();
          formData.append('file', file);
          const response = await fetch('/activity-schedules/upload-excel', { method: 'POST', body: formData });
          const payload = await response.json();
          if (!response.ok) throw new Error(payload.detail || '活動行程 Excel 上傳失敗');
          uploadedExcelServerPath = payload.server_path;
          ids.excelPath.value = file.name;
          fillSheetNames(payload.sheet_names, payload.selected_sheet);
          const cols = payload.columns || [];
          [ids.activityCodeColumn, ids.activityDateColumn, ids.activityTimeColumn, ids.activityContentColumn, ids.ownerTeamColumn, ids.locationColumn, ids.noteColumn]
            .forEach(select => fillSelect(select, cols));
          setStatus(ids.importStatus, `Excel 上傳完成：${file.name}`);
        }
        async function loadColumns() {
          if (!uploadedExcelServerPath) throw new Error('請先選擇 Excel 或 CSV 檔案');
          const params = new URLSearchParams({ excel_path: uploadedExcelServerPath, sheet_name: ids.sheetName.value || '' });
          const response = await fetch(`/activity-schedules/excel-columns?${params.toString()}`);
          const payload = await response.json();
          if (!response.ok) throw new Error(payload.detail || '讀取欄位失敗');
          fillSheetNames(payload.sheet_names, payload.selected_sheet);
          const cols = payload.columns || [];
          [ids.activityCodeColumn, ids.activityDateColumn, ids.activityTimeColumn, ids.activityContentColumn, ids.ownerTeamColumn, ids.locationColumn, ids.noteColumn]
            .forEach(select => fillSelect(select, cols));
          setStatus(ids.importStatus, `已讀取工作表 ${payload.selected_sheet} 欄位，共 ${cols.length} 欄`);
        }
        async function importSchedule() {
          if (!uploadedExcelServerPath) throw new Error('請先上傳 Excel 或 CSV');
          const payload = {
            excel_path: uploadedExcelServerPath,
            sheet_name: ids.sheetName.value || '',
            activity_code_column: ids.activityCodeColumn.value,
            activity_date_column: ids.activityDateColumn.value,
            activity_time_column: ids.activityTimeColumn.value,
            activity_content_column: ids.activityContentColumn.value,
            owner_team_column: ids.ownerTeamColumn.value,
            location_column: ids.locationColumn.value,
            note_column: ids.noteColumn.value,
          };
          const response = await fetch('/activity-schedules/import-excel', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
          });
          const result = await response.json();
          if (!response.ok) throw new Error(result.detail || '匯入活動行程失敗');
          setStatus(ids.importStatus, `已匯入 ${result.imported_count || 0} 筆活動行程`);
          ids.importResult.textContent = (result.logs || []).join('\\n');
          await querySchedules();
        }
        function renderRows(items) {
          if (!items.length) {
            ids.queryRows.innerHTML = '<tr><td colspan="9">查無資料。</td></tr>';
            return;
          }
          const buildTimeRange = (rows, index) => {
            const current = rows[index] || {};
            const dateText = current.activity_date || '';
            const startTime = current.activity_time || '00:00:00';
            let endTime = '24:00:00';
            for (let i = index + 1; i < rows.length; i += 1) {
              const next = rows[i];
              if ((next.activity_date || '') !== dateText) break;
              if (next.activity_time) {
                endTime = next.activity_time;
                break;
              }
            }
            return `${startTime} ~ ${endTime}`;
          };
          ids.queryRows.innerHTML = items.map((item, index) => `
            <tr>
              <td>${item.activity_date || ''}</td>
              <td>${buildTimeRange(items, index)}</td>
              <td>${item.activity_code || ''}</td>
              <td>${item.activity_content || ''}</td>
              <td>${item.owner_team || ''}</td>
              <td>${item.location || ''}</td>
              <td class="note">${item.note || ''}</td>
              <td>${item.create_time || ''}</td>
              <td>${item.update_time || ''}</td>
            </tr>
          `).join('');
        }
        async function querySchedules() {
          const params = new URLSearchParams({
            activity_date: ids.queryDate.value || '',
            keyword: ids.queryKeyword.value || '',
            limit: '500',
          });
          const response = await fetch(`/activity-schedules/query?${params.toString()}`);
          const payload = await response.json();
          if (!response.ok) throw new Error(payload.detail || '活動行程查詢失敗');
          renderRows(payload.items || []);
          setStatus(ids.queryStatus, `查詢完成，共 ${payload.total || 0} 筆`);
        }
        async function deleteAllSchedules() {
          if (!confirm('確定要刪除全部活動行程嗎？')) return;
          if (!confirm('這個動作無法復原，是否繼續？')) return;
          const response = await fetch('/activity-schedules/delete-all', { method: 'POST' });
          const payload = await response.json();
          if (!response.ok) throw new Error(payload.detail || '全部刪除失敗');
          setStatus(ids.queryStatus, `已刪除 ${payload.deleted_count || 0} 筆活動行程`);
          renderRows([]);
        }
        ids.excelFileInput.addEventListener('change', async (event) => {
          const file = event.target.files && event.target.files[0];
          if (!file) return;
          try { await uploadExcel(file); } catch (error) { setStatus(ids.importStatus, error.message, true); }
          event.target.value = '';
        });
        ids.loadColumnsBtn.addEventListener('click', async () => {
          try { await loadColumns(); } catch (error) { setStatus(ids.importStatus, error.message, true); }
        });
        ids.importBtn.addEventListener('click', async () => {
          try { await importSchedule(); } catch (error) { setStatus(ids.importStatus, error.message, true); }
        });
        ids.queryBtn.addEventListener('click', async () => {
          try { await querySchedules(); } catch (error) { setStatus(ids.queryStatus, error.message, true); }
        });
        ids.deleteAllBtn.addEventListener('click', async () => {
          try { await deleteAllSchedules(); } catch (error) { setStatus(ids.queryStatus, error.message, true); }
        });
      </script>
    </body>
    </html>
    """


def render_clean_activity_photo_import_ui_html() -> str:
    return """
    <!DOCTYPE html>
    <html lang="zh-Hant">
    <head>
      <meta charset="utf-8" />
      <meta name="viewport" content="width=device-width, initial-scale=1" />
      <title>活動照片匯入</title>
      <style>
        :root {
          --bg: #f4efe7; --panel: #fffdfa; --ink: #1f2937; --muted: #6b7280;
          --line: #d8cfbf; --accent: #92400e; --accent2: #0f766e;
        }
        * { box-sizing: border-box; }
        body { margin: 0; font-family: "Segoe UI","Noto Sans TC",sans-serif; color: var(--ink); background: linear-gradient(180deg, #e6ddcf, var(--bg)); min-height: 100vh; }
        .wrap { max-width: 1320px; margin: 0 auto; padding: 32px 20px 56px; }
        .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 24px; padding: 24px; box-shadow: 0 18px 46px rgba(41,33,18,.08); }
        .topbar { display:flex; justify-content:space-between; align-items:flex-start; gap:16px; margin-bottom:16px; }
        h1 { margin:0 0 8px; font-size:34px; }
        h2 { margin:0 0 12px; font-size:22px; }
        p,label { color:var(--muted); line-height:1.7; }
        .actions { display:flex; flex-wrap:wrap; gap:12px; }
        .section { margin-top:20px; padding:20px; border:1px solid #ece3d4; border-radius:20px; background:#fffaf4; }
        .grid { display:grid; grid-template-columns: 220px minmax(220px,1fr); gap:12px 16px; align-items:center; }
        .inline-actions { display:flex; gap:12px; flex-wrap:wrap; }
        .inline-actions button, .inline-actions .file-button { width:auto; min-width:150px; }
        input, select, button, .file-button { width:100%; border-radius:14px; border:1px solid var(--line); padding:12px 14px; font-size:15px; background:white; }
        button, .file-button { border:0; background: linear-gradient(135deg, var(--accent), var(--accent2)); color:white; cursor:pointer; font-weight:700; text-align:center; text-decoration:none; display:inline-flex; align-items:center; justify-content:center; }
        .file-button input { display:none; }
        .inline-check { display:flex; align-items:center; gap:10px; color:#1f2937; }
        .status { margin-top:14px; padding:14px 16px; border-radius:14px; background:#eef8f6; color:#0f766e; white-space:pre-wrap; }
        .status.error { background:#fff1f2; color:#be123c; }
        .result-box { margin-top:14px; padding:16px; min-height:260px; border-radius:18px; background:#fffdf8; border:1px solid #eadfcb; overflow:auto; white-space:pre-wrap; font-family:Consolas,"Courier New",monospace; font-size:13px; }
        .service-box { margin-top:14px; padding:14px 16px; border-radius:14px; background:#fff8e8; color:#92400e; border:1px solid #ecd8b0; }
        @media (max-width: 920px) { .grid { grid-template-columns: 1fr; } }
      </style>
    </head>
    <body>
      <div class="wrap">
        <div class="panel">
          <div class="topbar">
            <div>
              <h1>活動照片正規化與匯入辨識</h1>
              <p style="margin:0 0 6px;color:#92400e;">UI 版本：2026-05-10-01</p>
              <p>本頁流程分三段：1. 檔名正規化處理（模式 A/B）→ 2. 匯入入庫（寫入 img_upload）→ 3. 後續辨識（寫入 reco_result）。</p>
            </div>
            <div class="actions">
              <a class="file-button" href="/">回到首頁</a>
              <a class="file-button" href="/activity-schedule-ui">前往活動行程建立</a>
              <a class="file-button" href="/windows-activity-import-tool">以 Windows 工具執行（不透過網頁）</a>
            </div>
          </div>

          <section class="section">
            <h2>步驟 1：正規化處理設定</h2>
            <div id="serviceStatusBox" class="service-box">Windows 正規化服務狀態檢查中...</div>
            <div class="actions" style="margin-top:10px;">
              <button id="checkServiceBtn" type="button">檢查 Windows 服務</button>
              <a class="file-button" id="serviceGuideBtn" href="/windows-batch-service-guide">啟動指引（請先執行 start_windows_batch_service.ps1）</a>
            </div>
            <div class="grid">
              <label>筆電編號</label>
              <input id="laptopNumber" class="full" placeholder="系統自動產生" readonly />

              <label>設定檔（JSON）</label>
              <div class="full inline-actions"><button id="downloadConfigBtn" type="button">下載設定檔</button><label class="file-button">載入設定檔<input id="configFileInput" type="file" accept=".json,application/json" /></label><button id="clearSelectionBtn" type="button">清除選擇</button></div>

              <label>攝影師</label>
              <div class="full" style="display:flex;gap:12px;">
                <input id="photographer" style="flex:1;" list="photographerOptions" placeholder="可手動輸入攝影師" autocomplete="off" />
                <select id="photographerPreset" style="max-width:420px;">
                  <option value="">從清單選擇（姓名＋備註）</option>
                </select>
              </div>
              <datalist id="photographerOptions"></datalist>

              <label>正規化模式及模式說明</label>
              <div class="full">
                <select id="normalizeMode">
                  <option value="exif">模式 A（EXIF，自動正規化）</option>
                  <option value="schedule">模式 B（NONEXIF，選活動行程）</option>
                </select>
                <div style="margin-top:8px;color:#6b7280;">模式 A 依 EXIF 比對活動時段命名；比對不到時活動編號使用 000。模式 B 需選活動行程。</div>
              </div>

              <label>活動行程</label>
              <select id="scheduleId" class="full"></select>

              <label>來源圖檔資料夾</label>
              <div class="full" style="display:flex;gap:12px;">
                <input id="sourceFolder" value="C:\\activity\\ingest\\normalized_success" />
                <label class="file-button" style="max-width:180px;">選擇資料夾<input id="sourceFolderPicker" type="file" webkitdirectory directory multiple /></label>
              </div>

              <label>目的圖檔資料夾（正規化輸出）</label>
              <input id="outputFolder" class="full" value="C:\\activity\\ingest\\normalized_success" />
              <input id="backupFolder" type="hidden" value="C:\\activity\\ingest\\normalized_complete" />
            </div>
            <div class="actions" style="margin-top:16px;">
              <button id="normalizeBtn" type="button">執行圖檔正規化</button>
              <button id="openSuccessFolderBtn" type="button">開啟成功正規化資料夾</button>
              <button id="openCompleteFolderBtn" type="button">開啟成功原始檔資料夾</button>
              <button id="openFailFolderBtn" type="button">開啟失敗原始檔資料夾</button>
            </div>
            <h2 style="margin-top:18px;">步驟 2：匯入入庫（img_upload）與步驟 3：後續辨識（reco_result）</h2>
            <p>批次作業提示：Server 端請先將來源檔案放在 C:\\activity\\ingest\\normalized_success，先按「執行圖檔正規化」，再按「開始匯入活動照片」。注意：開始匯入只做入庫與辨識，不會再做正規化。</p>
            <div class="grid">
              <label>是否做影像品質評分</label>
              <div class="inline-check"><input id="enablePyiqa" type="checkbox" /><span>啟用 pyiqa_score</span></div>
              <div></div><div></div>
            </div>
            <div class="actions" style="margin-top:16px;">
              <button id="importBtn" type="button">開始匯入活動照片</button>
              <button id="retryFailedRecoBtn" type="button">補跑未完成</button>
              <button id="openLogBtn" type="button">用 Notepad 開啟匯入 Log</button>
            </div>
            <div class="grid" style="margin-top:10px;">
              <label>重新附著 Job</label>
              <div style="display:flex;gap:12px;">
                <input id="attachJobId" placeholder="輸入既有 job_id（例如 imp_20260520_093324_xxxxxx）" />
                <button id="attachJobBtn" type="button" style="max-width:200px;">接續查看</button>
              </div>
            </div>
            <div id="status" class="status">請先選擇活動行程與照片。</div>
            <h3 style="margin:12px 0 6px;">步驟 1 正規化執行紀錄</h3>
            <pre id="normalizeResult" class="result-box"></pre>
            <h3 style="margin:12px 0 6px;">步驟 2+3 匯入/辨識執行紀錄</h3>
            <pre id="importResult" class="result-box"></pre>
          </section>
        </div>
      </div>
      <script>
        const ids = {
          laptopNumber: document.getElementById('laptopNumber'),
          scheduleId: document.getElementById('scheduleId'),
          photographer: document.getElementById('photographer'),
          photographerPreset: document.getElementById('photographerPreset'),
          normalizeMode: document.getElementById('normalizeMode'),
          enablePyiqa: document.getElementById('enablePyiqa'),
          sourceFolderPicker: document.getElementById('sourceFolderPicker'),
          configFileInput: document.getElementById('configFileInput'),
          downloadConfigBtn: document.getElementById('downloadConfigBtn'),
          clearSelectionBtn: document.getElementById('clearSelectionBtn'),
          photographerOptions: document.getElementById('photographerOptions'),
          sourceFolder: document.getElementById('sourceFolder'),
          outputFolder: document.getElementById('outputFolder'),
          backupFolder: document.getElementById('backupFolder'),
          normalizeBtn: document.getElementById('normalizeBtn'),
          openSuccessFolderBtn: document.getElementById('openSuccessFolderBtn'),
          openCompleteFolderBtn: document.getElementById('openCompleteFolderBtn'),
          openFailFolderBtn: document.getElementById('openFailFolderBtn'),
          importBtn: document.getElementById('importBtn'),
          retryFailedRecoBtn: document.getElementById('retryFailedRecoBtn'),
          openLogBtn: document.getElementById('openLogBtn'),
          attachJobId: document.getElementById('attachJobId'),
          attachJobBtn: document.getElementById('attachJobBtn'),
          status: document.getElementById('status'),
          normalizeResult: document.getElementById('normalizeResult'),
          importResult: document.getElementById('importResult'),
          serviceStatusBox: document.getElementById('serviceStatusBox'),
          checkServiceBtn: document.getElementById('checkServiceBtn'),
        };
        let scheduleItems = [];
        let configPayload = null;
        const DEVICE_KEY_STORAGE = 'noob_device_client_key_v1';
        const WINDOWS_BATCH_CANDIDATES = [
          `${window.location.protocol}//${window.location.hostname}:8010`,
          `${window.location.protocol}//localhost:8010`,
          `${window.location.protocol}//127.0.0.1:8010`,
        ].filter((value, index, array) => array.indexOf(value) === index);
        function setStatus(msg, isError = false) {
          ids.status.textContent = msg;
          ids.status.className = isError ? 'status error' : 'status';
        }
        function getOrCreateClientKey() {
          let key = window.localStorage.getItem(DEVICE_KEY_STORAGE);
          if (!key) {
            key = `web-${crypto.randomUUID ? crypto.randomUUID() : Date.now().toString(36)}`;
            window.localStorage.setItem(DEVICE_KEY_STORAGE, key);
          }
          return key;
        }
        async function ensureDeviceId() {
          const formData = new FormData();
          formData.append('client_key', getOrCreateClientKey());
          formData.append('device_name', window.location.hostname || 'server-web');
          const response = await fetch('/device/register', { method: 'POST', body: formData });
          const payload = await response.json();
          if (!response.ok) throw new Error(payload.detail || '取得裝置編號失敗');
          ids.laptopNumber.value = payload.device_id || '';
        }
        async function fetchWindowsBatch(path, options = {}) {
          let lastError = null;
          for (const baseUrl of WINDOWS_BATCH_CANDIDATES) {
            try {
              return await fetch(`${baseUrl}${path}`, options);
            } catch (error) {
              lastError = error;
            }
          }
          throw new Error(`無法連線到 Windows 正規化服務。${lastError && lastError.message ? ` 詳細原因：${lastError.message}` : ''}`);
        }
        function setServiceStatus(msg, isError = false) {
          ids.serviceStatusBox.textContent = msg;
          ids.serviceStatusBox.style.background = isError ? '#fff1f2' : '#eef8f6';
          ids.serviceStatusBox.style.color = isError ? '#be123c' : '#0f766e';
          ids.serviceStatusBox.style.borderColor = isError ? '#fecdd3' : '#bfe8dd';
        }
        function errorToMessage(error) {
          if (!error) return '未知錯誤';
          if (typeof error === 'string') return error;
          if (error.message) return error.message;
          if (error.detail) return typeof error.detail === 'string' ? error.detail : JSON.stringify(error.detail);
          try {
            return JSON.stringify(error);
          } catch (_) {
            return String(error);
          }
        }
        function normalizeLogs(lines) {
          return (lines || []).map((line) => {
            if (line === null || line === undefined) return '';
            if (typeof line === 'string') return line;
            try { return JSON.stringify(line, null, 2); } catch (_) { return String(line); }
          });
        }
        async function watchImportJob(jobId) {
          ids.importResult.textContent = '';
          setStatus(`活動照片匯入任務監看中，Job=${jobId}...`);
          let offset = 0;
          for (let i = 0; i < 7200; i++) {
            const [statusRes, logsRes] = await Promise.all([
              fetchWindowsBatch(`/activity-photo-import/jobs/${encodeURIComponent(jobId)}`).catch(() => fetch(`/activity-photo-import/jobs/${encodeURIComponent(jobId)}`)),
              fetchWindowsBatch(`/activity-photo-import/jobs/${encodeURIComponent(jobId)}/logs?offset=${offset}`).catch(() => fetch(`/activity-photo-import/jobs/${encodeURIComponent(jobId)}/logs?offset=${offset}`)),
            ]);
            const statusPayload = await statusRes.json();
            const logsPayload = await logsRes.json();
            if (logsRes.ok) {
              const lines = normalizeLogs(logsPayload.lines || []);
              if (lines.length) {
                ids.importResult.textContent += (ids.importResult.textContent ? '\\n' : '') + lines.join('\\n');
                offset = Number(logsPayload.next_offset || offset);
              }
            }
            if (statusRes.ok) {
              const remain = Number(statusPayload.remaining_in_source_count || 0);
              setStatus(`活動照片匯入處理中：${statusPayload.processed_count || 0}/${statusPayload.total_count || 0}，成功 ${statusPayload.success_count || 0}，失敗 ${statusPayload.failed_count || 0}，略過 ${statusPayload.skipped_count || 0}，來源剩餘 ${remain}。Job=${jobId}`);
              if (['DONE', 'FAILED', 'CANCELED'].includes(String(statusPayload.status || ''))) {
                const failCsvMsg = statusPayload.fail_csv_created ? `失敗清單 CSV：${statusPayload.fail_csv_path || ''}` : '本次無失敗，未產生失敗清單 CSV';
                const duplicateSummary = `重複（duplicate）${statusPayload.moved_duplicate_count || 0} 張`;
                const remainSummary = `來源目錄剩餘 ${remain} 張`;
                setStatus(`活動照片匯入完成：成功 ${statusPayload.success_count || 0}，失敗 ${statusPayload.failed_count || 0}，略過 ${statusPayload.skipped_count || 0}，${duplicateSummary}，${remainSummary}。Job=${jobId}。${failCsvMsg}`);
                break;
              }
            }
            await new Promise((resolve) => setTimeout(resolve, 1000));
            if (i === 7199) {
              setStatus(`活動照片匯入仍在背景執行（Job=${jobId}），目前僅停止前端輪詢，並非失敗。可稍後用「接續查看」繼續監看。`, true);
            }
          }
        }
        async function checkWindowsService() {
          try {
            const response = await fetchWindowsBatch('/health');
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            const payload = await response.json();
            if (String(payload.proxy_mode || '') !== 'start_and_poll') {
              throw new Error('Windows 服務版本過舊（proxy_mode 非 start_and_poll），請重啟 start_windows_batch_service.ps1。');
            }
            const warnLegacy = Number(payload.legacy_log_recent_count || 0) > 0;
            const baseMsg = `Windows 正規化服務已啟動（127.0.0.1:8010 可連線）`;
            if (warnLegacy) {
              setServiceStatus(`${baseMsg}。警示：偵測到最近 1 分鐘仍有 legacy log 寫入（${payload.legacy_log_recent_count} 筆），疑似舊程序仍在運行。`, true);
            } else {
              setServiceStatus(baseMsg);
            }
            return true;
          } catch (error) {
            setServiceStatus(`Windows 正規化服務尚未啟動，請先執行 start_windows_batch_service.ps1。詳細原因：${errorToMessage(error)}`, true);
            return false;
          }
        }
        function fillPhotographers() {
          const options = [];
          if (configPayload && Array.isArray(configPayload.photographers)) {
            for (const item of configPayload.photographers) if (item && item.name) options.push(item.name);
          } else {
            for (const item of scheduleItems) if (item && item.photographer) options.push(item.photographer);
          }
          const unique = [...new Set(options)];
          ids.photographerOptions.innerHTML = unique.map(name => `<option value="${name}"></option>`).join('');
        }
        async function loadPhotographersMaster() {
          try {
            const response = await fetch('/photographers/query?limit=1000');
            const payload = await response.json();
            if (!response.ok) return;
            const rows = (payload.items || []).filter(item => item && item.photographer_name);
            const names = rows.map(item => item.photographer_name).filter(Boolean);
            const existing = ids.photographerOptions.innerHTML;
            const merged = [...new Set(names)];
            ids.photographerOptions.innerHTML = merged.map(name => `<option value="${name}"></option>`).join('') || existing;
            ids.photographerPreset.innerHTML = [
              '<option value="">從清單選擇（姓名＋備註）</option>',
              ...rows.map(item => {
                const name = String(item.photographer_name || '').trim();
                const note = String(item.note || '').trim();
                const label = note ? `${name}（${note}）` : name;
                const value = name.replaceAll('"', '&quot;');
                const text = label.replaceAll('<', '&lt;').replaceAll('>', '&gt;');
                return `<option value="${value}">${text}</option>`;
              }),
            ].join('');
          } catch (_) {}
        }
        function clearSelections() {
          ids.photographer.value = '';
          ids.scheduleId.value = '';
          ids.normalizeMode.value = 'exif';
          ids.sourceFolder.value = 'C:\\\\activity\\\\ingest\\\\normalized_success';
          ids.outputFolder.value = 'C:\\\\activity\\\\ingest\\\\normalized_success';
          ids.enablePyiqa.checked = false;
          setStatus('已清除選擇。');
          syncModeUi();
        }
        async function loadSchedules() {
          const response = await fetch('/activity-schedules/options');
          const payload = await response.json();
          if (!response.ok) throw new Error(payload.detail || '讀取活動行程失敗');
          scheduleItems = payload.items || [];
          ids.scheduleId.innerHTML = ['<option value="">請選擇活動行程</option>', ...scheduleItems.map(item => `<option value="${item.id}">${item.label}</option>`)].join('');
          fillPhotographers();
        }
        async function normalizePhotos() {
          const serviceReady = await checkWindowsService();
          if (!serviceReady) throw new Error('Windows 正規化服務尚未啟動');
          if (!ids.laptopNumber.value.trim()) throw new Error('請先輸入筆電編號');
          if (ids.normalizeMode.value === 'schedule' && !ids.scheduleId.value) throw new Error('模式 B 請先選擇活動行程');
          const selectedSchedule = scheduleItems.find(item => String(item.id) === ids.scheduleId.value) || null;
          const formData = new FormData();
          formData.append('laptop_number', ids.laptopNumber.value.trim());
          if (ids.scheduleId.value) formData.append('schedule_id', ids.scheduleId.value);
          if (selectedSchedule && selectedSchedule.activity_date) formData.append('schedule_date', selectedSchedule.activity_date);
          if (selectedSchedule && selectedSchedule.activity_content) formData.append('schedule_content', selectedSchedule.activity_content);
          formData.append('photographer', ids.photographer.value || '');
          formData.append('normalize_mode', ids.normalizeMode.value || 'schedule');
          formData.append('source_folder', ids.sourceFolder.value || '');
          formData.append('output_folder', ids.outputFolder.value || '');
          formData.append('backup_folder', ids.backupFolder.value || '');
          // 活動照片正規化固定走「來源資料夾模式」，避免走上傳暫存檔導致時間/搬移來源錯誤。
          const endpoint = '/activity-photo-normalize-folder';
          let response = await fetchWindowsBatch(endpoint, { method: 'POST', body: formData });
          if (response.status === 404) {
            response = await fetch(endpoint, { method: 'POST', body: formData });
          }
          const payload = await response.json();
          if (!response.ok) throw new Error(payload.detail || '活動照片正規化失敗（Windows 本機服務）');
          const successPath = payload.success_dir || ids.outputFolder.value || 'C:\\\\activity\\\\ingest\\\\normalized_success';
          const completePath = payload.complete_dir || ids.backupFolder.value || 'C:\\\\activity\\\\ingest\\\\normalized_complete';
          const failPath = payload.fail_dir || 'C:\\\\activity\\\\ingest\\\\normalized_fail';
          const exifMissingCount = payload.failed_count_exif_missing || 0;
          const failCsvPath = payload.fail_csv_path || '';
          const jobId = payload.job_id || 'N/A';
          setStatus(`圖檔正規化完成：成功 ${payload.normalized_count || 0} 張，失敗 ${payload.failed_count || 0} 張（無 EXIF：${exifMissingCount}）。\\nJob=${jobId}\\n成功正規化：${successPath}\\n成功原始檔：${completePath}\\n失敗原始檔：${failPath}\\n失敗清單 CSV：${failCsvPath}`);
          const exifFailedList = (payload.failed_items_exif_missing || []).slice(0, 50).map((item, idx) => `${String(idx + 1).padStart(2, '0')}. ${String(item.filename || '')} -> ${String(item.fail_path || '')}`);
          const sections = [
            ...(normalizeLogs(payload.logs || [])),
            '',
            `--- 無 EXIF 失敗清單（最多顯示 50 筆 / 共 ${exifMissingCount} 筆）---`,
            ...(exifFailedList.length ? exifFailedList : ['(無)']),
            '',
            `失敗清單 CSV：${failCsvPath || '(無)'}`,
          ];
          ids.normalizeResult.textContent = sections.join('\\n');
        }
        async function openFolder(folderPath) {
          const target = String(folderPath || '').trim();
          if (!target) throw new Error('資料夾路徑為空');
          const response = await fetchWindowsBatch('/admin/open-folder', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ folder_path: target }),
          });
          const payload = await response.json();
          if (!response.ok) {
            const detail = String(payload.detail || '');
            if (response.status === 404 || detail.includes('Not Found')) {
              throw new Error('Windows 正規化服務版本過舊（缺少 /admin/open-folder）。請以系統管理員重啟 start_windows_batch_service.ps1。');
            }
            throw new Error(detail || '開啟資料夾失敗');
          }
          setStatus(`已開啟資料夾：${payload.folder_path}`);
        }
        async function importPhotos() {
          if (!ids.laptopNumber.value.trim()) throw new Error('請先輸入筆電編號');
          if (ids.normalizeMode.value === 'schedule' && !ids.scheduleId.value) throw new Error('模式 B 請先選擇活動行程');
          ids.importResult.textContent = '';
          const formData = new FormData();
          formData.append('laptop_number', ids.laptopNumber.value.trim());
          if (ids.scheduleId.value) formData.append('schedule_id', ids.scheduleId.value);
          formData.append('photographer', ids.photographer.value || '');
          formData.append('enable_pyiqa', ids.enablePyiqa.checked ? 'true' : 'false');
          formData.append('normalize_mode', ids.normalizeMode.value || 'schedule');
          formData.append('source_folder', ids.sourceFolder.value || '');
          formData.append('output_folder', ids.outputFolder.value || '');
          formData.append('backup_folder', ids.backupFolder.value || '');
          let response = await fetchWindowsBatch('/activity-photo-import-proxy', { method: 'POST', body: formData });
          if (response.status === 404) response = await fetch('/activity-photo-import/start', { method: 'POST', body: formData });
          const payload = await response.json();
          if (!response.ok) throw new Error(payload.detail || '活動照片匯入失敗');
          const jobId = payload.job_id || '';
          if (!jobId) {
            setStatus(`活動照片匯入完成：成功 ${payload.processed_count || 0} 張，略過 ${payload.skipped_count || 0} 張。`);
            ids.importResult.textContent = normalizeLogs(payload.logs || []).join('\\n');
            return;
          }
          setStatus(`活動照片匯入任務已啟動，Job=${jobId}，正在處理中...`);
          await watchImportJob(jobId);
        }
        async function openImportLog() {
          const response = await fetchWindowsBatch('/admin/open-log', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ log_name: 'activity-photo-import' }),
          });
          const payload = await response.json();
          if (!response.ok) throw new Error(payload.detail || '開啟匯入 Log 失敗');
          setStatus(`已嘗試用 Notepad 開啟 Log：${payload.log_path}`);
        }
        async function retryFailedRecognition() {
          const formData = new FormData();
          formData.append('limit', '200');
          let response = await fetchWindowsBatch('/activity-photo-import-retry-failed-proxy', { method: 'POST', body: formData });
          if (response.status === 404) {
            response = await fetch('/activity-photo-import-retry-failed', { method: 'POST', body: formData });
          }
          const payload = await response.json();
          if (!response.ok) throw new Error(payload.detail || '補跑失敗辨識失敗');
          setStatus(`補跑完成：候選 ${payload.candidate_count || 0} 張，重試 ${payload.retried_count || 0} 張，成功 ${payload.success_count || 0} 張，失敗 ${payload.failed_count || 0} 張。`);
          ids.importResult.textContent = normalizeLogs(payload.logs || []).join('\\n');
        }
        function syncModeUi() {
          const isSchedule = ids.normalizeMode.value === 'schedule';
          ids.scheduleId.disabled = !isSchedule;
          if (!isSchedule) ids.scheduleId.value = '';
          fillPhotographers();
        }
        ids.scheduleId.addEventListener('change', fillPhotographers);
        ids.photographerPreset.addEventListener('change', () => {
          if (ids.photographerPreset.value) ids.photographer.value = ids.photographerPreset.value;
        });
        ids.normalizeMode.addEventListener('change', syncModeUi);
        ids.checkServiceBtn.addEventListener('click', async () => {
          await checkWindowsService();
        });
        ids.sourceFolderPicker.addEventListener('change', () => {
          const files = [...(ids.sourceFolderPicker.files || [])];
          if (!files.length) return;
          const folder = files[0].webkitRelativePath ? files[0].webkitRelativePath.split('/')[0] : '';
          if (folder) {
            const currentSource = String(ids.sourceFolder.value || '').trim();
            const normalizedCurrent = currentSource.split('/').join('\\\\').toLowerCase();
            if (normalizedCurrent.startsWith('c:\\\\activity\\\\ingest')) {
              ids.sourceFolder.value = `C:\\\\activity\\\\ingest\\\\${folder}`;
            } else if (!currentSource) {
              ids.sourceFolder.value = `C:\\\\activity\\\\ingest\\\\${folder}`;
            }
          }
          const tip = folder ? `已選資料夾預覽：${folder}（${files.length} 檔）` : `已選 ${files.length} 檔`;
          setStatus(`${tip}。來源圖檔資料夾已帶入：${ids.sourceFolder.value}。`);
        });
        ids.downloadConfigBtn.addEventListener('click', async () => {
          const response = await fetch('/activity-normalize-config/export');
          const payload = await response.json();
          if (!response.ok) throw new Error(payload.detail || '下載設定檔失敗');
          const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json;charset=utf-8' });
          const url = URL.createObjectURL(blob);
          const a = document.createElement('a');
          a.href = url; a.download = 'activity_normalize_config.json'; a.click();
          URL.revokeObjectURL(url);
        });
        ids.configFileInput.addEventListener('change', async (event) => {
          const file = event.target.files && event.target.files[0];
          if (!file) return;
          const text = await file.text();
          configPayload = JSON.parse(text);
          if (Array.isArray(configPayload.activities)) {
            scheduleItems = configPayload.activities;
            ids.scheduleId.innerHTML = ['<option value="">請選擇活動行程</option>', ...scheduleItems.map(item => `<option value="${item.id}">${item.label || item.activity_content || ''}</option>`)].join('');
          }
          fillPhotographers();
          setStatus('已載入設定檔。');
          event.target.value = '';
        });
        ids.clearSelectionBtn.addEventListener('click', clearSelections);
        ids.normalizeBtn.addEventListener('click', async () => {
          try { await normalizePhotos(); } catch (error) { setStatus(error.message, true); }
        });
        ids.openSuccessFolderBtn.addEventListener('click', async () => {
          try { await openFolder(ids.outputFolder.value || 'C:\\\\uploadsource\\\\normalized_success'); } catch (error) { setStatus(error.message, true); }
        });
        ids.openCompleteFolderBtn.addEventListener('click', async () => {
          try { await openFolder(ids.backupFolder.value || 'C:\\\\uploadsource\\\\normalized_complete'); } catch (error) { setStatus(error.message, true); }
        });
        ids.openFailFolderBtn.addEventListener('click', async () => {
          try { await openFolder('C:\\\\uploadsource\\\\normalized_fail'); } catch (error) { setStatus(error.message, true); }
        });
        ids.importBtn.addEventListener('click', async () => {
          try { await importPhotos(); } catch (error) { setStatus(error.message, true); }
        });
        ids.retryFailedRecoBtn.addEventListener('click', async () => {
          try { await retryFailedRecognition(); } catch (error) { setStatus(error.message, true); }
        });
        ids.openLogBtn.addEventListener('click', async () => {
          try { await openImportLog(); } catch (error) { setStatus(error.message, true); }
        });
        ids.attachJobBtn.addEventListener('click', async () => {
          try {
            const jobId = String(ids.attachJobId.value || '').trim();
            if (!jobId) throw new Error('請先輸入 job_id');
            await watchImportJob(jobId);
          } catch (error) {
            setStatus(error.message, true);
          }
        });
        (async () => {
          try { ids.photographer.value = ''; await ensureDeviceId(); await loadSchedules(); await loadPhotographersMaster(); syncModeUi(); await checkWindowsService(); }
          catch (error) { setStatus(error.message, true); }
        })();
      </script>
    </body>
    </html>
    """


def render_activity_photo_normalize_ui_html() -> str:
    return """
    <!DOCTYPE html>
    <html lang="zh-Hant">
    <head>
      <meta charset="utf-8" />
      <meta name="viewport" content="width=device-width, initial-scale=1" />
      <title>活動照片正規化處理</title>
      <style>
        * { box-sizing: border-box; }
        body { margin:0; font-family:"Segoe UI","Noto Sans TC",sans-serif; background:#f4efe7; color:#1f2937; }
        .wrap { max-width:1200px; margin:0 auto; padding:28px 16px; }
        .panel { background:#fffdfa; border:1px solid #d8cfbf; border-radius:20px; padding:20px; }
        .grid { display:grid; grid-template-columns:220px 1fr; gap:10px 14px; align-items:center; }
        .actions { display:flex; gap:10px; margin-top:12px; flex-wrap:wrap; }
        input, select, button, label.file-btn { width:100%; padding:10px 12px; border-radius:10px; border:1px solid #d8cfbf; background:white; }
        button, .link, label.file-btn { border:0; background:linear-gradient(135deg,#92400e,#0f766e); color:#fff; font-weight:700; text-decoration:none; display:inline-flex; align-items:center; justify-content:center; cursor:pointer; }
        .file-btn input { display:none; }
        .status { margin-top:10px; background:#eef8f6; color:#0f766e; border-radius:10px; padding:10px 12px; white-space:pre-wrap; }
        .status.err { background:#fff1f2; color:#be123c; }
        pre { margin-top:10px; background:#fff; border:1px solid #eee4d5; border-radius:12px; min-height:240px; padding:12px; white-space:pre-wrap; overflow:auto; }
      </style>
    </head>
    <body>
      <div class="wrap">
        <div class="panel">
          <h1>活動照片正規化處理</h1>
          <div class="actions">
            <a class="link" href="/">回到首頁</a>
            <a class="link" href="/activity-photo-import-ui">前往活動照片匯入入庫與後續辨識</a>
          </div>
          <div id="serviceStatus" class="status">Windows 正規化服務狀態檢查中...</div>
          <div class="actions">
            <button id="checkServiceBtn" type="button">檢查 Windows 服務</button>
            <a class="link" href="/windows-batch-service-guide">啟動指引（start_windows_batch_service.ps1）</a>
          </div>
          <div class="grid">
            <label>筆電編號（device_id）</label>
            <div style="display:flex;gap:8px;">
              <input id="laptopNumber" placeholder="請輸入筆電編號（A-Z、0-9、_、-）" />
              <button id="clearDeviceIdBtn" type="button" style="max-width:160px;">清除筆電編號</button>
            </div>
            <label>設定檔（JSON）</label><div style="display:flex;gap:8px;"><button id="downloadConfigBtn" type="button" style="max-width:180px;">下載設定檔</button><label class="file-btn" style="max-width:180px;">載入設定檔<input id="configFileInput" type="file" accept=".json,application/json" /></label></div>
            <label>目前活動資料來源</label>
            <div style="display:flex; gap:8px; align-items:center;">
              <input id="scheduleSource" value="API" readonly style="max-width:180px;" />
              <span style="color:#64748b;">未載入 JSON 時使用 API，載入後使用設定檔。</span>
            </div>
            <label>攝影師</label>
            <div style="display:flex;gap:8px;">
              <input id="photographer" list="photographerOptions" placeholder="可手動輸入攝影師" />
              <select id="photographerPreset" style="max-width:360px;">
                <option value="">從清單選擇（姓名＋備註）</option>
              </select>
            </div>
            <datalist id="photographerOptions"></datalist>
            <label>正規化模式</label>
            <select id="normalizeMode">
              <option value="exif">模式 A（EXIF，自動正規化）</option>
              <option value="schedule">模式 B（NONEXIF，選活動行程）</option>
            </select>
            <label>活動行程（模式 B 必填）</label><select id="scheduleId"></select>
            <label>來源資料夾</label><div style="display:flex;gap:8px;"><input id="sourceFolder" value="C:\\activity\\ingest\\incoming" /><label class="file-btn" style="max-width:180px;">選擇資料夾<input id="sourceFolderPicker" type="file" webkitdirectory directory multiple /></label></div>
            <label>輸出資料夾</label><input id="outputFolder" value="C:\\activity\\ingest\\normalized_success" />
          </div>
          <div class="actions">
            <button id="normalizeBtn" type="button">執行圖檔正規化</button>
            <button id="clearStateBtn" type="button">清除狀態</button>
            <button id="openSuccessBtn" type="button">開啟成功正規化資料夾</button>
            <button id="openCompleteBtn" type="button">開啟成功原始檔資料夾</button>
            <button id="openFailBtn" type="button">開啟失敗原始檔資料夾</button>
          </div>
          <div class="grid" style="margin-top:12px;">
            <label>接續查看 Job</label>
            <div style="display:flex;gap:8px;">
              <select id="jobSelect"><option value="">請選擇最近任務</option></select>
              <button id="watchJobBtn" type="button" style="max-width:180px;">接續查看</button>
              <button id="refreshJobsBtn" type="button" style="max-width:180px;">更新任務清單</button>
            </div>
          </div>
          <div id="status" class="status">請先設定欄位。</div>
          <pre id="result"></pre>
        </div>
      </div>
      <script>
        const ids = {
          serviceStatus: document.getElementById('serviceStatus'),
          checkServiceBtn: document.getElementById('checkServiceBtn'),
          laptopNumber: document.getElementById('laptopNumber'),
          clearDeviceIdBtn: document.getElementById('clearDeviceIdBtn'),
          photographer: document.getElementById('photographer'),
          photographerPreset: document.getElementById('photographerPreset'),
          photographerOptions: document.getElementById('photographerOptions'),
          normalizeMode: document.getElementById('normalizeMode'),
          scheduleId: document.getElementById('scheduleId'),
          sourceFolder: document.getElementById('sourceFolder'),
          sourceFolderPicker: document.getElementById('sourceFolderPicker'),
          outputFolder: document.getElementById('outputFolder'),
          downloadConfigBtn: document.getElementById('downloadConfigBtn'),
          configFileInput: document.getElementById('configFileInput'),
          scheduleSource: document.getElementById('scheduleSource'),
          normalizeBtn: document.getElementById('normalizeBtn'),
          clearStateBtn: document.getElementById('clearStateBtn'),
          openSuccessBtn: document.getElementById('openSuccessBtn'),
          openCompleteBtn: document.getElementById('openCompleteBtn'),
          openFailBtn: document.getElementById('openFailBtn'),
          jobSelect: document.getElementById('jobSelect'),
          watchJobBtn: document.getElementById('watchJobBtn'),
          refreshJobsBtn: document.getElementById('refreshJobsBtn'),
          status: document.getElementById('status'),
          result: document.getElementById('result'),
        };
        const MANUAL_DEVICE_ID_STORAGE = 'noob_manual_device_id';
        const WINDOWS_BATCH_CANDIDATES = [
          `${window.location.protocol}//${window.location.hostname}:8010`,
          `${window.location.protocol}//localhost:8010`,
          `${window.location.protocol}//127.0.0.1:8010`,
        ].filter((value, index, array) => array.indexOf(value) === index);
        let scheduleItems = [];
        let currentScheduleSource = 'api';
        let normalizeWatchTimer = null;
        let normalizeLogOffset = 0;
        let normalizeCurrentJobId = '';
        let lastResolvedPaths = {
          success: '',
          complete: '',
          fail: '',
          source: '',
          manifest: '',
        };
        function setStatus(msg, err=false){ ids.status.textContent=msg; ids.status.className=err?'status err':'status'; }
        async function fetchWindowsBatch(path, options = {}) {
          let lastError = null;
          const timeoutMs = Number(options.timeoutMs || 2500);
          for (const baseUrl of WINDOWS_BATCH_CANDIDATES) {
            let timer = null;
            try {
              const controller = new AbortController();
              timer = setTimeout(() => controller.abort(), timeoutMs);
              const requestOptions = { ...options, signal: controller.signal };
              const response = await fetch(`${baseUrl}${path}`, requestOptions);
              return response;
            } catch (e) {
              lastError = e;
            } finally {
              if (timer) clearTimeout(timer);
            }
          }
          throw new Error(`無法連線到 Windows 正規化服務。${lastError && lastError.message ? ` 詳細原因：${lastError.message}` : ''}`);
        }
        async function checkService(){
          try{
            const res = await fetchWindowsBatch('/health');
            const p = await res.json();
            if(!res.ok) throw new Error(p.detail || `HTTP ${res.status}`);
            ids.serviceStatus.textContent = 'Windows 正規化服務已啟動（127.0.0.1:8010 可連線）';
            ids.serviceStatus.className = 'status';
            return true;
          }catch(err){
            ids.serviceStatus.textContent = `Windows 正規化服務尚未啟動，請先執行 start_windows_batch_service.ps1。詳細原因：${err.message || err}`;
            ids.serviceStatus.className = 'status err';
            return false;
          }
        }
        function normalizeManualDeviceId(value){
          return String(value || '').trim().toUpperCase();
        }
        function isValidManualDeviceId(value){
          return /^[A-Z0-9_-]+$/.test(String(value || '').trim());
        }
        function initManualDeviceId(){
          const stored = normalizeManualDeviceId(window.localStorage.getItem(MANUAL_DEVICE_ID_STORAGE) || '');
          ids.laptopNumber.value = stored;
        }
        function persistManualDeviceId(){
          const normalized = normalizeManualDeviceId(ids.laptopNumber.value);
          ids.laptopNumber.value = normalized;
          if(!normalized){
            window.localStorage.removeItem(MANUAL_DEVICE_ID_STORAGE);
            return;
          }
          window.localStorage.setItem(MANUAL_DEVICE_ID_STORAGE, normalized);
        }
        function confirmAndPersistManualDeviceId(){
          const currentStored = normalizeManualDeviceId(window.localStorage.getItem(MANUAL_DEVICE_ID_STORAGE) || '');
          const inputValue = normalizeManualDeviceId(ids.laptopNumber.value);
          ids.laptopNumber.value = inputValue;
          if(inputValue === currentStored) return true;
          if(!inputValue){
            const okClear = window.confirm('確定要清除筆電編號嗎？');
            if(!okClear){
              ids.laptopNumber.value = currentStored;
              return false;
            }
            window.localStorage.removeItem(MANUAL_DEVICE_ID_STORAGE);
            setStatus('已清除筆電編號，請重新輸入。');
            return true;
          }
          const ok = window.confirm(`確認將筆電編號由「${currentStored || '未設定'}」改為「${inputValue}」？`);
          if(!ok){
            ids.laptopNumber.value = currentStored;
            return false;
          }
          window.localStorage.setItem(MANUAL_DEVICE_ID_STORAGE, inputValue);
          setStatus(`已更新筆電編號：${inputValue}`);
          return true;
        }
        function ensureManualDeviceIdOrThrow(){
          const normalized = normalizeManualDeviceId(ids.laptopNumber.value);
          ids.laptopNumber.value = normalized;
          if(!normalized) throw new Error('請先輸入筆電編號（device_id）');
          if(!isValidManualDeviceId(normalized)) throw new Error('筆電編號格式錯誤，只允許 A-Z、0-9、底線（_）與連字號（-）');
          window.localStorage.setItem(MANUAL_DEVICE_ID_STORAGE, normalized);
          return normalized;
        }
        async function loadSchedules(){
          const res = await fetch('/activity-schedules/options');
          const p = await res.json();
          if(!res.ok) throw new Error(p.detail || '讀取活動行程失敗');
          scheduleItems = (p.items || []).map(it => ({
            ...it,
            __key: (it.id !== undefined && it.id !== null && String(it.id).trim() !== '')
              ? `id:${String(it.id).trim()}`
              : `code:${String(it.activity_code || '').trim()}`,
          }));
          ids.scheduleId.innerHTML = ['<option value="">請選擇活動行程</option>', ...scheduleItems.map(it=>`<option value="${it.__key}">${it.label}</option>`)].join('');
          currentScheduleSource = 'api';
          ids.scheduleSource.value = 'API';
        }
        async function loadPhotographers(){
          const res = await fetch('/photographers/query?limit=1000');
          const p = await res.json();
          if(!res.ok) return;
          const rows = (p.items || []).filter(i => i && i.photographer_name);
          const names = [...new Set(rows.map(i=>i.photographer_name).filter(Boolean))];
          ids.photographerOptions.innerHTML = names.map(n=>`<option value="${n}"></option>`).join('');
          ids.photographerPreset.innerHTML = [
            '<option value="">從清單選擇（姓名＋備註）</option>',
            ...rows.map(item => {
              const name = String(item.photographer_name || '').trim();
              const note = String(item.note || '').trim();
              const label = note ? `${name}（${note}）` : name;
              return `<option value="${name.replaceAll('"','&quot;')}">${label.replaceAll('<','&lt;').replaceAll('>','&gt;')}</option>`;
            }),
          ].join('');
        }
        function stopNormalizeWatch(){
          if(normalizeWatchTimer){ clearInterval(normalizeWatchTimer); normalizeWatchTimer = null; }
        }
        function clearNormalizeState(resetText=true){
          stopNormalizeWatch();
          normalizeLogOffset = 0;
          normalizeCurrentJobId = '';
          if(resetText){
            ids.status.textContent = '請先設定欄位。';
            ids.status.className = 'status';
            ids.result.textContent = '';
          }
        }
        function renderNormalizeSummary(status){
          const total = Number(status.total_count || 0);
          const processed = Number(status.processed_count || 0);
          const success = Number(status.success_count || 0);
          const failed = Number(status.failed_count || 0);
          const remaining = Number(status.remaining_in_source_count || 0);
          const jobId = String(status.job_id || normalizeCurrentJobId || '');
          const state = String(status.status || '');
          return `Job=${jobId} 狀態=${state}，進度 ${processed}/${total}，成功 ${success}，失敗 ${failed}，來源剩餘 ${remaining}`;
        }
        async function refreshNormalizeJobs(){
          const res = await fetchWindowsBatch('/activity-photo-normalize/jobs-recent?limit=30');
          const p = await res.json();
          if(!res.ok) throw new Error(p.detail || '讀取最近正規化任務失敗');
          const items = Array.isArray(p.items) ? p.items : [];
          ids.jobSelect.innerHTML = '<option value="">請選擇最近任務</option>' + items.map(item=>{
            const started = item.started_at_tpe || item.started_at || '--';
            return `<option value="${item.job_id}">${item.job_id} | ${item.status || ''} | ${started}</option>`;
          }).join('');
        }
        async function pollNormalizeJobOnce(jobId){
          const [statusRes, logsRes] = await Promise.all([
            fetchWindowsBatch(`/activity-photo-normalize/jobs/${encodeURIComponent(jobId)}`),
            fetchWindowsBatch(`/activity-photo-normalize/jobs/${encodeURIComponent(jobId)}/logs?offset=${normalizeLogOffset}`),
          ]);
          const statusPayload = await statusRes.json();
          const logsPayload = await logsRes.json();
          if(!statusRes.ok) throw new Error(statusPayload.detail || '讀取正規化任務狀態失敗');
          if(!logsRes.ok) throw new Error(logsPayload.detail || '讀取正規化任務紀錄失敗');
          const lines = Array.isArray(logsPayload.lines) ? logsPayload.lines : [];
          if(lines.length){
            ids.result.textContent += (ids.result.textContent ? '\\n' : '') + lines.join('\\n');
            ids.result.scrollTop = ids.result.scrollHeight;
          }
          normalizeLogOffset = Number(logsPayload.next_offset || normalizeLogOffset || 0);
          lastResolvedPaths = {
            success: statusPayload.resolved_success_dir || lastResolvedPaths.success || '',
            complete: statusPayload.resolved_duplicate_dir || lastResolvedPaths.complete || '',
            fail: statusPayload.resolved_fail_dir || lastResolvedPaths.fail || '',
            source: statusPayload.resolved_source_dir || lastResolvedPaths.source || '',
            manifest: lastResolvedPaths.manifest || '',
          };
          setStatus(renderNormalizeSummary(statusPayload));
          if(['DONE','FAILED','CANCELED'].includes(String(statusPayload.status || '').toUpperCase())){
            stopNormalizeWatch();
          }
        }
        async function watchNormalizeJob(jobId, resetLog=false){
          if(!jobId) throw new Error('請提供 job_id');
          normalizeCurrentJobId = jobId;
          stopNormalizeWatch();
          if(resetLog){ normalizeLogOffset = 0; ids.result.textContent = ''; }
          await pollNormalizeJobOnce(jobId);
          normalizeWatchTimer = setInterval(async ()=>{
            try { await pollNormalizeJobOnce(jobId); } catch (error) { setStatus(error.message || String(error), true); stopNormalizeWatch(); }
          }, 1000);
        }
        async function normalize(){
          if(!(await checkService())) throw new Error('Windows 正規化服務尚未啟動');
          const manualDeviceId = ensureManualDeviceIdOrThrow();
          if(ids.normalizeMode.value==='schedule' && !ids.scheduleId.value) throw new Error('模式 B 請先選擇活動行程');
          const selected = scheduleItems.find(i=>String(i.__key||'')===ids.scheduleId.value) || null;
          const fd = new FormData();
          fd.append('source_folder', ids.sourceFolder.value || '');
          fd.append('output_folder', ids.outputFolder.value || '');
          fd.append('laptop_number', manualDeviceId);
          fd.append('photographer', ids.photographer.value || '');
          fd.append('normalize_mode', ids.normalizeMode.value || 'schedule');
          const scheduleIdText = String((selected && selected.id !== undefined && selected.id !== null) ? selected.id : '').trim();
          if(
            ids.normalizeMode.value === 'schedule' &&
            scheduleIdText &&
            scheduleIdText.toLowerCase() !== 'none' &&
            scheduleIdText.toLowerCase() !== 'null' &&
            /^\\d+$/.test(scheduleIdText)
          ){
            fd.append('schedule_id', scheduleIdText);
          }
          if(selected && selected.activity_code) fd.append('schedule_code', selected.activity_code);
          if(selected && selected.activity_time) fd.append('schedule_time', selected.activity_time);
          if(selected && selected.activity_time_range) fd.append('schedule_time_range', selected.activity_time_range);
          fd.append('schedule_source', currentScheduleSource);
          fd.append('activities_json', JSON.stringify((scheduleItems || []).map(it => ({
            id: it.id ?? null,
            activity_code: String(it.activity_code || '').trim(),
            activity_date: String(it.activity_date || '').trim(),
            activity_time: String(it.activity_time || '').trim(),
            activity_time_range: String(it.activity_time_range || '').trim(),
            activity_content: String(it.activity_content || '').trim(),
          }))));
          if(selected && selected.activity_date) fd.append('schedule_date', selected.activity_date);
          if(selected && selected.activity_content) fd.append('schedule_content', selected.activity_content);
          clearNormalizeState(false);
          setStatus('任務啟動中...');
          const res = await fetchWindowsBatch('/activity-photo-normalize/start', { method:'POST', body:fd });
          const p = await res.json();
          if(!res.ok){
            const detailText = typeof p.detail === 'string' ? p.detail : JSON.stringify(p.detail || {});
            if(String(detailText).includes('422') || String(detailText).includes('schedule_id')){
              throw new Error('活動行程欄位格式錯誤，請重新選擇活動行程後再試。');
            }
            throw new Error(p.detail || '活動照片正規化任務啟動失敗');
          }
          normalizeCurrentJobId = String(p.job_id || '');
          if(!normalizeCurrentJobId) throw new Error('正規化任務未回傳 job_id');
          lastResolvedPaths.source = p.resolved_source_folder || ids.sourceFolder.value || '';
          lastResolvedPaths.success = p.resolved_output_folder || ids.outputFolder.value || '';
          setStatus(`已建立正規化任務：${normalizeCurrentJobId}，開始即時監看...`);
          await refreshNormalizeJobs().catch(()=>{});
          await watchNormalizeJob(normalizeCurrentJobId, true);
        }
        async function openFolder(path){
          const res = await fetchWindowsBatch('/admin/open-folder', {
            method:'POST',
            headers:{'Content-Type':'application/json'},
            body: JSON.stringify({ folder_path: path }),
          });
          const p = await res.json();
          if(!res.ok) throw new Error(p.detail || '開啟資料夾失敗');
          setStatus(`已開啟資料夾：${p.folder_path}`);
        }
        ids.sourceFolderPicker.addEventListener('change', ()=>{
          const files = [...(ids.sourceFolderPicker.files||[])];
          if(!files.length) return;
          const folder = files[0].webkitRelativePath ? files[0].webkitRelativePath.split('/')[0] : '';
          if(folder) ids.sourceFolder.value = `C:\\\\activity\\\\ingest\\\\${folder}`;
        });
        ids.photographerPreset.addEventListener('change', ()=>{
          if(ids.photographerPreset.value) ids.photographer.value = ids.photographerPreset.value;
        });
        ids.laptopNumber.addEventListener('change', ()=>{ confirmAndPersistManualDeviceId(); });
        ids.laptopNumber.addEventListener('blur', ()=>{ confirmAndPersistManualDeviceId(); });
        ids.clearDeviceIdBtn.addEventListener('click', ()=>{
          const currentStored = normalizeManualDeviceId(window.localStorage.getItem(MANUAL_DEVICE_ID_STORAGE) || '');
          if(!currentStored && !normalizeManualDeviceId(ids.laptopNumber.value)){
            ids.laptopNumber.value = '';
            return;
          }
          if(window.confirm('確定要清除筆電編號嗎？')){
            ids.laptopNumber.value = '';
            window.localStorage.removeItem(MANUAL_DEVICE_ID_STORAGE);
            setStatus('已清除筆電編號，請重新輸入。');
          }
        });
        ids.downloadConfigBtn.addEventListener('click', async ()=>{
          const res = await fetch('/activity-normalize-config/export');
          const p = await res.json();
          if(!res.ok) throw new Error(p.detail || '下載設定檔失敗');
          const blob = new Blob([JSON.stringify(p, null, 2)], { type:'application/json;charset=utf-8' });
          const url = URL.createObjectURL(blob); const a=document.createElement('a'); a.href=url; a.download='activity_normalize_config.json'; a.click(); URL.revokeObjectURL(url);
        });
        ids.configFileInput.addEventListener('change', async (event)=>{
          const file = event.target.files && event.target.files[0];
          if(!file) return;
          const text = await file.text(); const p = JSON.parse(text);
          if(Array.isArray(p.activities)){
            scheduleItems = p.activities;
            scheduleItems = scheduleItems.map(it => ({
              ...it,
              __key: (it.id !== undefined && it.id !== null && String(it.id).trim() !== '')
                ? `id:${String(it.id).trim()}`
                : `code:${String(it.activity_code || '').trim()}`,
            }));
            ids.scheduleId.innerHTML=['<option value="">請選擇活動行程</option>', ...scheduleItems.map(it=>{
              const code = String(it.activity_code || '').trim();
              const date = String(it.activity_date || '').trim();
              const range = String(it.activity_time_range || it.activity_time || '').trim();
              const content = String(it.activity_content || '').trim();
              const label = [code, date, range, content].filter(Boolean).join(' ');
              return `<option value="${it.__key}">${label || '未命名活動'}</option>`;
            })].join('');
            currentScheduleSource = 'config';
            ids.scheduleSource.value = '設定檔';
          }
          if(Array.isArray(p.photographers)){ const names=[...new Set(p.photographers.map(it=>it.name).filter(Boolean))]; ids.photographerOptions.innerHTML=names.map(n=>`<option value="${n}"></option>`).join('');}
          event.target.value='';
          setStatus('已載入設定檔。');
        });
        ids.checkServiceBtn.addEventListener('click', async ()=>{ await checkService(); });
        ids.normalizeBtn.addEventListener('click', async ()=>{ try{ await normalize(); } catch(err){ setStatus(err.message || String(err), true); }});
        ids.clearStateBtn.addEventListener('click', ()=>{ clearNormalizeState(true); });
        ids.refreshJobsBtn.addEventListener('click', async ()=>{ try{ await refreshNormalizeJobs(); setStatus('已更新最近任務清單。'); } catch(err){ setStatus(err.message || String(err), true); }});
        ids.watchJobBtn.addEventListener('click', async ()=>{
          const jobId = String(ids.jobSelect.value || '').trim();
          if(!jobId){ setStatus('請先選擇要接續查看的 job_id。', true); return; }
          try{
            clearNormalizeState(false);
            setStatus(`接續查看任務：${jobId}`);
            await watchNormalizeJob(jobId, true);
          }catch(err){
            setStatus(err.message || String(err), true);
          }
        });
        ids.openSuccessBtn.addEventListener('click', async ()=>{ try{ await openFolder(lastResolvedPaths.success || ids.outputFolder.value || 'C:\\\\activity\\\\ingest\\\\normalized_success'); } catch(err){ setStatus(err.message || String(err), true); }});
        ids.openCompleteBtn.addEventListener('click', async ()=>{ try{ await openFolder(lastResolvedPaths.complete || 'C:\\\\activity\\\\ingest\\\\normalized_complete'); } catch(err){ setStatus(err.message || String(err), true); }});
        ids.openFailBtn.addEventListener('click', async ()=>{ try{ await openFolder(lastResolvedPaths.fail || 'C:\\\\activity\\\\ingest\\\\normalized_fail'); } catch(err){ setStatus(err.message || String(err), true); }});
        (async()=>{
          try { initManualDeviceId(); } catch(err){ setStatus(err.message || String(err), true); }
          try { await loadSchedules(); } catch(err){ setStatus(err.message || String(err), true); }
          try { await loadPhotographers(); } catch(err){ setStatus(err.message || String(err), true); }
          try { await refreshNormalizeJobs(); } catch(err){ setStatus(`最近任務清單讀取失敗：${err.message || String(err)}`, true); }
          await checkService();
        })();
      </script>
    </body>
    </html>
    """


def render_activity_photo_import_runtime_ui_html() -> str:
    return """
    <!DOCTYPE html>
    <html lang="zh-Hant">
    <head>
      <meta charset="utf-8" />
      <meta name="viewport" content="width=device-width, initial-scale=1" />
      <title>活動照片匯入入庫與後續辨識</title>
      <style>
        body { margin:0; font-family:"Segoe UI","Noto Sans TC",sans-serif; background:#f4efe7; color:#1f2937; }
        .wrap { max-width:1200px; margin:0 auto; padding:28px 16px; }
        .panel { background:#fffdfa; border:1px solid #d8cfbf; border-radius:20px; padding:20px; }
        .grid { display:grid; grid-template-columns:220px 1fr; gap:10px 14px; align-items:center; }
        .actions { display:flex; gap:10px; margin-top:12px; flex-wrap:wrap; }
        input, button, label.file-btn { width:100%; padding:10px 12px; border-radius:10px; border:1px solid #d8cfbf; background:white; }
        button, .link, label.file-btn { border:0; background:linear-gradient(135deg,#92400e,#0f766e); color:#fff; font-weight:700; text-decoration:none; display:inline-flex; align-items:center; justify-content:center; cursor:pointer; }
        .file-btn input { display:none; }
        .inline-check { display:inline-flex; align-items:center; gap:6px; }
        .inline-check input[type="checkbox"] { width:auto; margin:0; }
        .status { margin-top:10px; background:#eef8f6; color:#0f766e; border-radius:10px; padding:10px 12px; white-space:pre-wrap; }
        .status.err { background:#fff1f2; color:#be123c; }
        pre { margin-top:10px; background:#fff; border:1px solid #eee4d5; border-radius:12px; min-height:220px; padding:12px; white-space:pre-wrap; overflow:auto; }
      </style>
    </head>
    <body>
      <div class="wrap">
        <div class="panel">
          <h1>活動照片匯入入庫（img_upload）與後續辨識（reco_result）</h1>
          <div class="actions">
            <a class="link" href="/">回到首頁</a>
            <a class="link" href="/activity-photo-normalize-ui">前往活動照片正規化處理</a>
          </div>
          <p>本頁只走 8000 API，不使用 8010。模型在容器內執行辨識。</p>
          <p>批次設定檔（manifest）用途：由正規化步驟產生，記錄本批次的來源資料夾與命名設定，匯入辨識時可直接套用，避免手動重填。</p>
          <p>接續查看 Job：頁面重整或短暫斷線後，輸入 job_id 可以接回同一批任務進度，不會重跑。job_id 可從「開始匯入」後狀態區或匯入 log 檔名取得。</p>
          <p>補跑未完成（唯一入口）：只針對 reco_status=FAILED/PENDING/RETRY 重新辨識，不會重跑已 DONE 的資料。</p>
          <div class="grid">
            <label>來源資料夾</label>
            <div style="display:flex;gap:8px;">
              <input id="sourceFolder" value="C:\\activity\\ingest\\normalized_success" />
              <label class="file-btn" style="max-width:180px;">選擇資料夾<input id="sourceFolderPicker" type="file" webkitdirectory directory multiple /></label>
            </div>
            <label>批次設定檔（manifest）</label>
            <div style="display:flex;gap:8px;">
              <input id="manifestPath" placeholder="例如 C:\\activity\\ingest\\_work\\normalize\\norm_xxx\\manifest.json" />
              <button id="latestManifestBtn" type="button" style="max-width:200px;">帶入最新 manifest</button>
            </div>
            <label>批次設定檔操作</label>
            <div style="display:flex;gap:8px;">
              <span style="align-self:center;color:#475569;">預設路徑：C:\\activity\\ingest\\_work\\normalize</span>
              <label class="file-btn" style="max-width:220px;">選擇批次設定檔<input id="manifestFileInput" type="file" accept=".json,application/json" /></label>
            </div>
            <label>是否做影像品質評分</label><div class="inline-check"><input id="enablePyiqa" type="checkbox" /><span>啟用 pyiqa_score</span></div>
            <label>接續查看 Job</label>
            <div>
              <div style="display:flex;gap:8px;">
                <select id="jobIdSelect"><option value="">請選擇最近 Job</option></select>
                <input id="jobId" placeholder="輸入 job_id（imp_xxx）" />
                <button id="attachBtn" type="button" style="max-width:160px;">接續查看</button>
              </div>
              <div style="margin-top:6px;color:#64748b;font-size:13px;">下拉清單時間為台北時間（UTC+8）。</div>
            </div>
          </div>
          <div class="actions">
            <button id="startBtn" type="button">開始匯入活動照片</button>
            <button id="retryBtn" type="button">補跑未完成</button>
            <button id="clearStatusBtn" type="button">清除狀態</button>
          </div>
          <div id="status" class="status">請先設定來源資料夾或帶入 manifest。</div>
          <h3>匯入/辨識執行紀錄</h3>
          <pre id="result"></pre>
        </div>
      </div>
      <script>
        const ids = {
          sourceFolder: document.getElementById('sourceFolder'),
          sourceFolderPicker: document.getElementById('sourceFolderPicker'),
          manifestPath: document.getElementById('manifestPath'),
          latestManifestBtn: document.getElementById('latestManifestBtn'),
          manifestFileInput: document.getElementById('manifestFileInput'),
          enablePyiqa: document.getElementById('enablePyiqa'),
          startBtn: document.getElementById('startBtn'),
          retryBtn: document.getElementById('retryBtn'),
          clearStatusBtn: document.getElementById('clearStatusBtn'),
          jobId: document.getElementById('jobId'),
          jobIdSelect: document.getElementById('jobIdSelect'),
          attachBtn: document.getElementById('attachBtn'),
          status: document.getElementById('status'),
          result: document.getElementById('result'),
        };
        const defaultState = {
          sourceFolder: 'C:\\\\activity\\\\ingest\\\\normalized_success',
          enablePyiqa: false,
          status: '請先設定來源資料夾或帶入 manifest。',
        };
        let currentWatchingJobId = '';
        let watchToken = 0;
        const POLL_INTERVAL_MS = 1000;
        const MAX_TRANSIENT_ERRORS = 5;
        function setStatus(msg, err=false){ ids.status.textContent=msg; ids.status.className=err?'status err':'status'; }
        window.addEventListener('error', (event) => {
          const message = event && event.message ? event.message : '未知錯誤';
          setStatus(`前端初始化失敗：${message}`, true);
        });
        function parseJsonSafe(response){
          return response.text().then((text)=>{
            try { return JSON.parse(text || '{}'); } catch (_) { return { detail: text || `HTTP ${response.status}` }; }
          });
        }
        async function fetchWithTimeout(url, options = {}, timeoutMs = 15000){
          const controller = new AbortController();
          const timer = setTimeout(()=>controller.abort(), timeoutMs);
          try {
            return await fetch(url, { ...options, signal: controller.signal });
          } finally {
            clearTimeout(timer);
          }
        }
        async function checkApiReady(){
          const probes = ['/openapi.json', '/docs'];
          let lastError = null;
          for (const probe of probes){
            try{
              const response = await fetchWithTimeout(probe, { method:'GET' }, 20000);
              if (response.ok) return true;
              lastError = new Error(`HTTP ${response.status} (${probe})`);
            }catch(error){
              lastError = error;
            }
          }
          const reason = lastError && lastError.message ? lastError.message : String(lastError || 'unknown');
          throw new Error(`8000 無法連線（${reason}）。請先確認 noob API 容器已啟動。`);
        }
        async function checkSourceFolderHasFiles(sourceFolder){
          const folder = String(sourceFolder || '').trim();
          if (!folder) throw new Error('請先填入來源資料夾。');
          const fd = new FormData();
          fd.append('source_folder', folder);
          const response = await fetchWithTimeout('/activity-photo-import/preview-source', { method:'POST', body:fd }, 30000);
          const payload = await parseJsonSafe(response);
          if (!response.ok) throw new Error(payload.detail || '檢查來源資料夾失敗');
          const total = Number(payload.total_count || 0);
          if (total <= 0) {
            throw new Error(`來源資料夾目前 0 張可處理照片：${payload.resolved_source_dir || folder}\n請先放入正規化完成檔案後再執行匯入。`);
          }
          return payload;
        }
        async function refreshRecentJobs(){
          const res = await fetch('/activity-photo-import/jobs-recent?limit=30');
          const payload = await parseJsonSafe(res);
          if (!res.ok) throw new Error(payload.detail || '讀取最近 Job 清單失敗');
          const options = Array.isArray(payload.items) ? payload.items : [];
          ids.jobIdSelect.innerHTML = '<option value="">請選擇最近 Job</option>';
          for (const item of options){
            const opt = document.createElement('option');
            const jobId = String(item.job_id || '').trim();
            if (!jobId) continue;
            opt.value = jobId;
            const startedAtText = item.started_at_tpe || item.started_at || '--';
            const label = `${jobId} | ${item.status || ''} | ${startedAtText}`;
            opt.textContent = label;
            ids.jobIdSelect.appendChild(opt);
          }
          return options;
        }
        function parseServerDatetime(value){
          const text = String(value || '').trim();
          if (!text) return NaN;
          const normalized = text.replace(' ', 'T');
          const ts = Date.parse(normalized);
          return Number.isFinite(ts) ? ts : NaN;
        }
        async function tryAttachLatestActiveJob(expectedSourceFolder='', requestStartedAtTs=0){
          const sourceExpected = String(expectedSourceFolder || '').trim().toLowerCase();
          for (let attempt = 1; attempt <= 30; attempt += 1){
            try{
              const items = await refreshRecentJobs();
              const list = Array.isArray(items) ? items : [];
              const candidate = list.find((it)=>{
                const status = String(it && it.status || '').toUpperCase();
                if (!(status === 'RUNNING' || status === 'QUEUED')) return false;
                const sourceActual = String(it && it.source_folder || '').trim().toLowerCase();
                if (sourceExpected && sourceActual && sourceActual !== sourceExpected) return false;
                if (requestStartedAtTs > 0){
                  const startedAtTs = parseServerDatetime(it && it.started_at);
                  if (Number.isFinite(startedAtTs) && startedAtTs + 2000 < requestStartedAtTs) return false;
                }
                return true;
              });
              if (candidate && candidate.job_id){
                const jobId = String(candidate.job_id).trim();
                if (jobId){
                  ids.jobId.value = jobId;
                  if (ids.jobIdSelect) ids.jobIdSelect.value = jobId;
                  setStatus(`啟動請求未即時回應，已自動附著同批任務 job_id=${jobId}（狀態=${candidate.status || 'UNKNOWN'}）。`);
                  await watch(jobId);
                  return true;
                }
              }
            }catch(_){
            }
            setStatus(`啟動請求逾時，正在自動附著同批任務中...（${attempt}/30）`);
            await new Promise(r=>setTimeout(r,1000));
          }
          return false;
        }
        function resetPageState(){
          watchToken += 1;
          currentWatchingJobId = '';
          ids.sourceFolder.value = defaultState.sourceFolder;
          ids.manifestPath.value = '';
          ids.enablePyiqa.checked = defaultState.enablePyiqa;
          ids.jobId.value = '';
          if (ids.jobIdSelect) ids.jobIdSelect.value = '';
          if (ids.manifestFileInput) ids.manifestFileInput.value = '';
          ids.result.textContent = '';
          setStatus(defaultState.status);
        }
        async function watch(jobId){
          const token = ++watchToken;
          currentWatchingJobId = jobId;
          let offset = 0;
          let lineCount = 0;
          let transientErrors = 0;
          let noNewLogRounds = 0;
          ids.result.textContent = '';
          ids.result.textContent = `[${new Date().toLocaleString()}] 已附著 Job：${jobId}`;
          for(let i=0;i<7200;i++){
            if (token !== watchToken) return;
            let sRes;
            let lRes;
            let s;
            let l;
            try{
              [sRes, lRes] = await Promise.all([
                fetchWithTimeout(`/activity-photo-import/jobs/${encodeURIComponent(jobId)}`, {}, 15000),
                fetchWithTimeout(`/activity-photo-import/jobs/${encodeURIComponent(jobId)}/logs?offset=${offset}`, {}, 15000),
              ]);
              s = await parseJsonSafe(sRes);
              l = await parseJsonSafe(lRes);
              transientErrors = 0;
            }catch(error){
              transientErrors += 1;
              setStatus(`Job=${jobId} 連線重試中（${transientErrors}/${MAX_TRANSIENT_ERRORS}）...`);
              if (transientErrors >= MAX_TRANSIENT_ERRORS){
                const reason = error && error.name === 'AbortError'
                  ? '請求逾時'
                  : (error && error.message ? error.message : String(error));
                throw new Error(`查詢任務連線中斷（${reason}）。背景可能仍在執行，可用 job_id=${jobId} 稍後接續查看。`);
              }
              await new Promise(r=>setTimeout(r,POLL_INTERVAL_MS));
              if (token !== watchToken) return;
              continue;
            }
            if (lRes.ok && Array.isArray(l.lines) && l.lines.length){
              ids.result.textContent += (ids.result.textContent ? '\\n' : '') + l.lines.join('\\n');
              offset = Number(l.next_offset || offset);
              lineCount = Number(l.line_count || offset);
              noNewLogRounds = 0;
            } else {
              if (lRes.ok) lineCount = Number(l.line_count || offset);
              noNewLogRounds += 1;
            }
            if (sRes.ok){
              const idleHint = (String(s.status||'').toUpperCase() === 'RUNNING' && noNewLogRounds >= 10)
                ? '（任務執行中，暫無新增 log）'
                : '';
              const lastPolledAt = s.last_polled_at || new Date().toLocaleString();
              const lastLogReadAt = l.server_log_read_at_tpe || '-';
              setStatus(`Job=${jobId} 狀態=${s.status}，進度 ${s.processed_count||0}/${s.total_count||0}，成功 ${s.success_count||0}，失敗 ${s.failed_count||0}，略過 ${s.skipped_count||0}，來源剩餘 ${s.remaining_in_source_count||0}，offset=${offset}，line_count=${lineCount}，最後輪詢=${lastPolledAt}，最後讀log=${lastLogReadAt}${idleHint}`);
              if(['DONE','FAILED','CANCELED'].includes(String(s.status||''))){
                ids.result.textContent += `\n[${new Date().toLocaleString()}] Job=${jobId} 完成，狀態=${s.status}，成功=${s.success_count||0}，失敗=${s.failed_count||0}，略過=${s.skipped_count||0}`;
                await Promise.allSettled([refreshRecentJobs()]);
                return;
              }
            } else {
              throw new Error(s.detail || '查詢任務狀態失敗');
            }
            await new Promise(r=>setTimeout(r,POLL_INTERVAL_MS));
            if (token !== watchToken) return;
          }
          setStatus(`Job=${jobId} 仍在背景執行，前端輪詢逾時，可稍後接續查看。`, true);
        }
        ids.latestManifestBtn.addEventListener('click', async ()=>{
          const res = await fetch('/activity-photo-import/latest-manifest');
          const p = await res.json();
          if(!res.ok) return setStatus(p.detail || '讀取最新 manifest 失敗', true);
          ids.manifestPath.value = p.manifest_path || '';
          ids.sourceFolder.value = p.source_folder || ids.sourceFolder.value;
          setStatus(`已帶入 manifest：${p.manifest_path || ''}`);
        });
        ids.startBtn.addEventListener('click', async ()=>{
          const requestStartedAtTs = Date.now();
          try{
            setStatus('檢查 API...');
            await checkApiReady();
            const sourcePath = String(ids.sourceFolder.value || '').trim();
            if (sourcePath.toLowerCase().startsWith('c:\\\\uploadsource')){
              throw new Error('請改用 C:\\\\activity\\\\ingest 目錄，不可混用舊路徑 C:\\\\uploadsource。');
            }
            if (String(ids.manifestPath.value || '').trim() && !String(ids.manifestPath.value || '').trim().toLowerCase().endsWith('.json')){
              throw new Error('批次設定檔必須是 .json 檔案');
            }
            setStatus('檢查來源資料夾...');
            const sourcePreview = await checkSourceFolderHasFiles(sourcePath);
            const fd = new FormData();
            fd.append('source_folder', sourcePath);
            fd.append('manifest_path', ids.manifestPath.value || '');
            fd.append('enable_pyiqa', ids.enablePyiqa.checked ? 'true' : 'false');
            setStatus('啟動任務中...');
            const res = await fetchWithTimeout('/activity-photo-import/start', { method:'POST', body:fd }, 180000);
            const p = await parseJsonSafe(res);
            if(!res.ok) throw new Error(p.detail || '啟動匯入失敗');
            const jobId = String(p.job_id || '').trim();
            if(!jobId) throw new Error('回傳缺少 job_id');
            ids.jobId.value = jobId;
            if (ids.jobIdSelect){
              const exists = [...ids.jobIdSelect.options].some(opt => opt.value === jobId);
              if (!exists){
                const opt = document.createElement('option');
                opt.value = jobId;
                opt.textContent = `${jobId} | QUEUED | 剛啟動`;
                ids.jobIdSelect.insertBefore(opt, ids.jobIdSelect.options[1] || null);
              }
              ids.jobIdSelect.value = jobId;
            }
            const resolvedPath = p.resolved_source_folder || sourcePreview.resolved_source_dir || ids.sourceFolder.value || '';
            const dirState = p.source_dir_state || sourcePreview.source_dir_state || 'unknown';
            setStatus(`已取得 job_id，開始輪詢...\njob_id=${jobId}\nAPI：8000\n來源資料夾：${resolvedPath}\n來源目錄狀態：${dirState}\nstart_response_ms=${p.start_response_ms ?? 'n/a'}`);
            ids.result.textContent = `[${new Date().toLocaleString()}] Job=${jobId} 已啟動，開始輪詢進度...`;
            await watch(jobId);
          }catch(err){
            const sourcePath = String(ids.sourceFolder.value || '').trim();
            if (err && err.name === 'AbortError'){
              setStatus('啟動請求逾時，正在自動附著同批任務中...');
              const recovered = await tryAttachLatestActiveJob(sourcePath, requestStartedAtTs);
              if (recovered) return;
              setStatus(`啟動任務逾時（來源：${sourcePath || '未填'}），且暫時找不到可接續的任務；請按「接續查看 Job」或稍後重試。`, true);
              return;
            }
            setStatus('啟動請求未即時完成，正在嘗試自動附著同批任務...');
            const recovered = await tryAttachLatestActiveJob(sourcePath, requestStartedAtTs);
            if (recovered) return;
            setStatus(err.message || String(err), true);
          }
        });
        ids.attachBtn.addEventListener('click', async ()=>{
          const jobId = String(ids.jobId.value || ids.jobIdSelect.value || '').trim();
          if(!jobId) return setStatus('請先輸入 job_id', true);
          ids.jobId.value = jobId;
          if (ids.jobIdSelect) ids.jobIdSelect.value = jobId;
          try{ await watch(jobId); }catch(err){ setStatus(err.message || String(err), true); }
        });
        ids.retryBtn.addEventListener('click', async ()=>{
          try{
            const fd = new FormData(); fd.append('limit', '200');
            const res = await fetch('/activity-photo-import-retry-failed', { method:'POST', body:fd });
            const p = await res.json();
            if(!res.ok) throw new Error(p.detail || '補跑失敗');
            ids.result.textContent = (p.logs || []).join('\\n');
            setStatus(`補跑完成：候選 ${p.candidate_count||0}，成功 ${p.success_count||0}，失敗 ${p.failed_count||0}`);
          }catch(err){ setStatus(err.message || String(err), true); }
        });
        ids.manifestFileInput.addEventListener('change', async ()=>{
          try{
            const file = ids.manifestFileInput.files && ids.manifestFileInput.files[0];
            if (!file) return;
            const formData = new FormData();
            formData.append('file', file);
            const res = await fetch('/activity-photo-import/upload-manifest', { method:'POST', body:formData });
            const payload = await parseJsonSafe(res);
            if (!res.ok) throw new Error(payload.detail || '上傳批次設定檔失敗');
            ids.manifestPath.value = payload.manifest_path || '';
            if (payload.source_folder) ids.sourceFolder.value = payload.source_folder;
            setStatus(`已載入批次設定檔：${payload.manifest_path || file.name}`);
          }catch(err){
            setStatus(`載入批次設定檔失敗：${err.message || String(err)}`, true);
          } finally {
            ids.manifestFileInput.value = '';
          }
        });
        ids.sourceFolderPicker.addEventListener('change', ()=>{
          const files = [...(ids.sourceFolderPicker.files || [])];
          if (!files.length) return;
          const relative = String(files[0].webkitRelativePath || '');
          const folder = relative.split('/')[0] || '';
          if (!folder) return;
          ids.sourceFolder.value = `C:\\\\activity\\\\ingest\\\\${folder}`;
          setStatus(`來源資料夾已帶入：${ids.sourceFolder.value}`);
        });
        ids.jobIdSelect.addEventListener('change', ()=>{
          const value = String(ids.jobIdSelect.value || '').trim();
          if (value) ids.jobId.value = value;
        });
        ids.clearStatusBtn.addEventListener('click', ()=>{
          resetPageState();
        });
        (async()=>{
          try{
            resetPageState();
            await Promise.allSettled([refreshRecentJobs()]);
          }catch(err){
            setStatus(err.message || String(err), true);
          }
        })();
      </script>
    </body>
    </html>
    """


def render_clean_photographer_ui_html() -> str:
    return """
    <!DOCTYPE html>
    <html lang="zh-Hant">
    <head>
      <meta charset="utf-8" />
      <meta name="viewport" content="width=device-width, initial-scale=1" />
      <title>攝影師資料建立維護</title>
      <style>
        body { margin:0; font-family:"Segoe UI","Noto Sans TC",sans-serif; background:#f4efe7; color:#1f2937; }
        .wrap { max-width:1080px; margin:0 auto; padding:28px 16px; }
        .panel { background:#fffdfa; border:1px solid #d8cfbf; border-radius:20px; padding:20px; }
        .grid { display:grid; grid-template-columns:120px 320px 120px 320px; gap:12px 16px; align-items:center; }
        .actions { display:flex; gap:10px; margin-top:12px; flex-wrap:wrap; }
        input, select { width:100%; max-width:320px; padding:6px 10px; height:35px; line-height:21px; border-radius:10px; border:1px solid #d8cfbf; }
        button { width:auto; min-width:136px; height:44px; padding:10px 14px; border-radius:10px; border:0; }
        button, .link, .file-btn { background:linear-gradient(135deg,#92400e,#0f766e); color:#fff; font-weight:700; text-decoration:none; display:inline-flex; align-items:center; justify-content:center; cursor:pointer; border-radius:10px; }
        .status { margin-top:10px; background:#eef8f6; color:#0f766e; border-radius:10px; padding:10px 12px; white-space:pre-wrap; }
        #excelPath, #queryKeyword, #sheetName, #nameColumn, #noteColumn { max-width:320px; }
        #queryBtn, #deleteAllBtn, #loadColumnsBtn { width:136px; min-width:136px; }
        .file-btn { width:136px; min-width:136px; height:44px; padding:10px 14px; border:0; }
        .file-btn input { display:none; }
        .table-wrap { margin-top:12px; overflow-x:auto; }
        table { width:100%; min-width:1020px; table-layout:fixed; border-collapse:collapse; background:#fff; }
        col.col-id { width:70px; }
        col.col-name { width:200px; }
        col.col-note { width:200px; }
        col.col-create { width:180px; }
        col.col-update { width:180px; }
        col.col-actions { width:180px; }
        th, td { border-bottom:1px solid #eee4d5; padding:8px; text-align:left; vertical-align:top; word-break:break-word; }
        td input { width:100%; min-width:0; max-width:100%; height:35px; }
        .cell-actions { display:flex; gap:8px; flex-wrap:wrap; align-items:flex-start; }
        .cell-actions button { width:72px; min-width:72px; height:40px; padding:8px 10px; }
        .actions .link { width:136px; min-width:136px; height:44px; }
        .grid > label { font-weight:600; }
        @media (max-width: 1100px) {
          .grid { grid-template-columns:1fr; }
          input, select { max-width:100%; }
          #queryBtn, #deleteAllBtn, #loadColumnsBtn { width:auto; }
        }
      </style>
    </head>
    <body>
      <div class="wrap">
        <div class="panel">
          <h1>攝影師資料建立維護</h1>
          <div class="actions">
            <a class="link" href="/">回到首頁</a>
            <a class="link" href="/activity-photo-import-ui">前往活動照片匯入</a>
          </div>
          <h2>Excel 匯入</h2>
          <div class="grid">
            <label>Excel 檔案</label><input id="excelPath" readonly placeholder="請選擇 Excel / CSV" />
            <label class="file-btn">選擇檔案<input id="excelFileInput" type="file" accept=".xlsx,.xls,.xlsm,.csv" /></label><div></div>
            <label>工作表</label><select id="sheetName"></select>
            <label>攝影師欄位</label><select id="nameColumn"></select>
            <label>姓名欄位</label><select id="noteColumn"></select>
            <button id="loadColumnsBtn" type="button">讀取欄位</button>
          </div>
          <div class="actions"><button id="importBtn" type="button">匯入攝影師資料</button></div>
          <div id="importStatus" class="status">匯入結果會顯示在這裡。</div>

          <h2>查詢及維護</h2>
          <div class="grid">
            <label>關鍵字</label><input id="queryKeyword" placeholder="可搜尋攝影師或姓名" />
            <button id="queryBtn" type="button">查詢</button><button id="deleteAllBtn" type="button">全部刪除</button>
          </div>
          <div id="queryStatus" class="status">查詢結果會顯示在下方。</div>
          <div class="table-wrap">
            <table>
              <colgroup>
                <col class="col-id" />
                <col class="col-name" />
                <col class="col-note" />
                <col class="col-create" />
                <col class="col-update" />
                <col class="col-actions" />
              </colgroup>
              <thead><tr><th>ID</th><th>攝影師</th><th>姓名</th><th>建立時間</th><th>更新時間</th><th>操作</th></tr></thead>
              <tbody id="rows"><tr><td colspan="6">請先查詢。</td></tr></tbody>
            </table>
          </div>
        </div>
      </div>
      <script>
        const ids = {
          excelPath:document.getElementById('excelPath'), excelFileInput:document.getElementById('excelFileInput'), sheetName:document.getElementById('sheetName'),
          nameColumn:document.getElementById('nameColumn'), noteColumn:document.getElementById('noteColumn'), loadColumnsBtn:document.getElementById('loadColumnsBtn'),
          importBtn:document.getElementById('importBtn'), importStatus:document.getElementById('importStatus'),
          queryKeyword:document.getElementById('queryKeyword'), queryBtn:document.getElementById('queryBtn'), deleteAllBtn:document.getElementById('deleteAllBtn'),
          queryStatus:document.getElementById('queryStatus'), rows:document.getElementById('rows')
        };
        let excelServerPath = '';
        function s(el,m,e=false){el.textContent=m; el.style.color=e?'#be123c':'#0f766e';}
        function fill(sel, cols, empty=true){ sel.innerHTML = (empty?['<option value="">請選擇欄位</option>']:[]).concat(cols.map(c=>`<option value="${c}">${c}</option>`)).join('');}
        function fillSheets(names, selected=''){const list=(names&&names.length?names:['CSV']); ids.sheetName.innerHTML=list.map(n=>`<option value="${n}">${n}</option>`).join(''); ids.sheetName.value=selected||list[0];}
        async function uploadExcel(file){
          const fd=new FormData(); fd.append('file',file);
          const r=await fetch('/activity-schedules/upload-excel',{method:'POST',body:fd}); const p=await r.json();
          if(!r.ok) throw new Error(p.detail||'上傳失敗');
          excelServerPath=p.server_path; ids.excelPath.value=file.name; fillSheets(p.sheet_names,p.selected_sheet); fill(ids.nameColumn,p.columns||[]); fill(ids.noteColumn,p.columns||[]);
        }
        async function loadCols(){
          const q=new URLSearchParams({excel_path:excelServerPath, sheet_name:ids.sheetName.value||''});
          const r=await fetch('/activity-schedules/excel-columns?'+q.toString()); const p=await r.json();
          if(!r.ok) throw new Error(p.detail||'讀取欄位失敗'); fillSheets(p.sheet_names,p.selected_sheet); fill(ids.nameColumn,p.columns||[]); fill(ids.noteColumn,p.columns||[]);
        }
        async function importData(){
          const r=await fetch('/photographers/import-excel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({excel_path:excelServerPath,sheet_name:ids.sheetName.value||'',photographer_column:ids.nameColumn.value,note_column:ids.noteColumn.value})});
          const p=await r.json(); if(!r.ok) throw new Error(p.detail||'匯入失敗'); s(ids.importStatus,`已匯入 ${p.imported_count||0} 筆`); await query();
        }
        function render(items){
          if(!items.length){ids.rows.innerHTML='<tr><td colspan="6">查無資料。</td></tr>'; return;}
          ids.rows.innerHTML=items.map(i=>`<tr><td>${i.id}</td><td><input data-k="name" data-id="${i.id}" value="${i.photographer_name||''}"></td><td><input data-k="note" data-id="${i.id}" value="${i.note||''}"></td><td>${i.create_time||''}</td><td>${i.update_time||''}</td><td><div class="cell-actions"><button data-a="save" data-id="${i.id}">儲存</button><button data-a="del" data-id="${i.id}">刪除</button></div></td></tr>`).join('');
        }
        async function query(){
          const q=new URLSearchParams({keyword:ids.queryKeyword.value||'',limit:'500'}); const r=await fetch('/photographers/query?'+q.toString()); const p=await r.json();
          if(!r.ok) throw new Error(p.detail||'查詢失敗'); render(p.items||[]); s(ids.queryStatus,`查詢完成，共 ${p.total||0} 筆`);
        }
        async function saveRow(id){
          const name=document.querySelector(`input[data-k="name"][data-id="${id}"]`).value; const note=document.querySelector(`input[data-k="note"][data-id="${id}"]`).value;
          const r=await fetch('/photographers/update',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:Number(id),photographer_name:name,note:note})}); const p=await r.json();
          if(!r.ok) throw new Error(p.detail||'更新失敗'); await query();
        }
        async function delRow(id){
          if(!confirm('確定刪除這筆攝影師資料？')) return;
          const r=await fetch('/photographers/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:Number(id)})}); const p=await r.json();
          if(!r.ok) throw new Error(p.detail||'刪除失敗'); await query();
        }
        async function delAll(){
          if(!confirm('確定全部刪除攝影師資料？')) return; if(!confirm('此動作無法復原，確認繼續？')) return;
          const r=await fetch('/photographers/delete-all',{method:'POST'}); const p=await r.json(); if(!r.ok) throw new Error(p.detail||'全部刪除失敗'); await query();
        }
        ids.excelFileInput.addEventListener('change',async e=>{const f=e.target.files&&e.target.files[0]; if(!f)return; try{await uploadExcel(f); s(ids.importStatus,'Excel 上傳完成');}catch(err){s(ids.importStatus,err.message,true)} e.target.value='';});
        ids.loadColumnsBtn.addEventListener('click',async()=>{try{await loadCols(); s(ids.importStatus,'欄位讀取完成');}catch(err){s(ids.importStatus,err.message,true)}});
        ids.importBtn.addEventListener('click',async()=>{try{await importData();}catch(err){s(ids.importStatus,err.message,true)}});
        ids.queryBtn.addEventListener('click',async()=>{try{await query();}catch(err){s(ids.queryStatus,err.message,true)}});
        ids.deleteAllBtn.addEventListener('click',async()=>{try{await delAll();}catch(err){s(ids.queryStatus,err.message,true)}});
        ids.rows.addEventListener('click',async e=>{const t=e.target; if(!(t instanceof HTMLElement)) return; const id=t.getAttribute('data-id'); if(!id) return; try{ if(t.getAttribute('data-a')==='save') await saveRow(id); if(t.getAttribute('data-a')==='del') await delRow(id);}catch(err){s(ids.queryStatus,err.message,true)}});
      </script>
    </body>
    </html>
    """


def render_clean_activity_award_ui_html() -> str:
    return """
    <!DOCTYPE html>
    <html lang="zh-Hant">
    <head>
      <meta charset="utf-8" />
      <meta name="viewport" content="width=device-width, initial-scale=1" />
      <title>活動獎項資料建立維護</title>
      <style>
        * { box-sizing: border-box; }
        body { margin:0; font-family:"Segoe UI","Noto Sans TC",sans-serif; background:#f4efe7; color:#1f2937; }
        .wrap { max-width:1200px; margin:0 auto; padding:28px 16px; }
        .panel { background:#fffdfa; border:1px solid #d8cfbf; border-radius:20px; padding:20px; }
        .grid { display:grid; grid-template-columns:120px 320px 120px 320px; gap:12px 16px; align-items:center; }
        .actions { display:flex; gap:10px; margin-top:12px; flex-wrap:wrap; }
        input, select { width:100%; max-width:320px; padding:6px 10px; height:35px; line-height:21px; border-radius:10px; border:1px solid #d8cfbf; }
        button { width:auto; min-width:136px; height:44px; padding:10px 14px; border-radius:10px; border:0; }
        button, .link, .file-btn { background:linear-gradient(135deg,#92400e,#0f766e); color:#fff; font-weight:700; text-decoration:none; display:inline-flex; align-items:center; justify-content:center; cursor:pointer; border-radius:10px; }
        .status { margin-top:10px; background:#eef8f6; color:#0f766e; border-radius:10px; padding:10px 12px; white-space:pre-wrap; }
        #excelPath, #queryKeyword, #serialNoColumn, #categoryColumn, #activityItemColumn, #mappedAwardColumn, #awardNameColumn, #noteColumn, #sheetName { max-width:320px; }
        #queryBtn, #deleteAllBtn, #loadColumnsBtn { width:136px; min-width:136px; }
        .file-btn { width:136px; min-width:136px; height:44px; padding:10px 14px; border:0; }
        .file-btn input { display:none; }
        .table-wrap { margin-top:12px; overflow-x:hidden; }
        table { width:100%; min-width:0; table-layout:fixed; border-collapse:collapse; background:#fff; }
        col.col-id { width:5%; }
        col.col-serial { width:6%; }
        col.col-category { width:12%; }
        col.col-item { width:12%; }
        col.col-mapped { width:12%; }
        col.col-award { width:12%; }
        col.col-note { width:16%; }
        col.col-create { width:9%; }
        col.col-update { width:9%; }
        col.col-actions { width:7%; }
        th, td { border-bottom:1px solid #eee4d5; padding:8px; text-align:left; vertical-align:top; word-break:break-word; }
        td input { width:100%; min-width:0; max-width:100%; height:35px; }
        td.note-cell { white-space:normal; word-break:break-word; }
        .cell-actions { display:flex; gap:8px; flex-wrap:wrap; align-items:flex-start; }
        .cell-actions button { width:60px; min-width:60px; height:36px; padding:6px 8px; font-size:13px; }
        .actions .link { width:160px; min-width:160px; height:44px; }
        .grid > label { font-weight:600; }
        @media (max-width: 1100px) {
          .grid { grid-template-columns:1fr; }
          input, select { max-width:100%; }
          #queryBtn, #deleteAllBtn, #loadColumnsBtn { width:auto; }
        }
      </style>
    </head>
    <body>
      <div class="wrap">
        <div class="panel">
          <h1>活動獎項資料建立維護</h1>
          <div class="actions">
            <a class="link" href="/">回到首頁</a>
            <a class="link" href="/activity-photo-import-ui">前往活動照片匯入</a>
          </div>
          <h2>Excel 匯入</h2>
          <div class="grid">
            <label>Excel 檔案</label><input id="excelPath" readonly placeholder="請選擇 Excel / CSV" />
            <label class="file-btn">選擇檔案<input id="excelFileInput" type="file" accept=".xlsx,.xls,.xlsm,.csv" /></label><div></div>
            <label>工作表</label><select id="sheetName"></select>
            <label>編號欄位</label><select id="serialNoColumn"></select>
            <label>獎項歸類欄位</label><select id="categoryColumn"></select>
            <label>活動項目欄位</label><select id="activityItemColumn"></select>
            <label>對應獎項欄位</label><select id="mappedAwardColumn"></select>
            <label>獎項名稱欄位</label><select id="awardNameColumn"></select>
            <label>備註欄位</label><select id="noteColumn"></select>
            <button id="loadColumnsBtn" type="button">讀取欄位</button>
          </div>
          <div class="actions"><button id="importBtn" type="button">匯入活動獎項資料</button></div>
          <div id="importStatus" class="status">匯入結果會顯示在這裡。</div>

          <h2>查詢及維護</h2>
          <div class="grid">
            <label>關鍵字</label><input id="queryKeyword" placeholder="可搜尋獎項歸類、活動項目、獎項名稱或備註" />
            <button id="queryBtn" type="button">查詢</button><button id="deleteAllBtn" type="button">全部刪除</button>
          </div>
          <div id="queryStatus" class="status">查詢結果會顯示在下方。</div>
          <div class="table-wrap">
            <table>
              <colgroup>
                <col class="col-id" />
                <col class="col-serial" />
                <col class="col-category" />
                <col class="col-item" />
                <col class="col-mapped" />
                <col class="col-award" />
                <col class="col-note" />
                <col class="col-create" />
                <col class="col-update" />
                <col class="col-actions" />
              </colgroup>
              <thead><tr><th>ID</th><th>編號</th><th>獎項歸類</th><th>活動項目</th><th>對應獎項</th><th>獎項名稱</th><th>備註</th><th>建立時間</th><th>更新時間</th><th>操作</th></tr></thead>
              <tbody id="rows"><tr><td colspan="10">請先查詢。</td></tr></tbody>
            </table>
          </div>
        </div>
      </div>
      <script>
        const ids = {
          excelPath:document.getElementById('excelPath'), excelFileInput:document.getElementById('excelFileInput'), sheetName:document.getElementById('sheetName'),
          serialNoColumn:document.getElementById('serialNoColumn'),
          categoryColumn:document.getElementById('categoryColumn'), activityItemColumn:document.getElementById('activityItemColumn'),
          mappedAwardColumn:document.getElementById('mappedAwardColumn'), awardNameColumn:document.getElementById('awardNameColumn'), noteColumn:document.getElementById('noteColumn'),
          loadColumnsBtn:document.getElementById('loadColumnsBtn'), importBtn:document.getElementById('importBtn'), importStatus:document.getElementById('importStatus'),
          queryKeyword:document.getElementById('queryKeyword'), queryBtn:document.getElementById('queryBtn'), deleteAllBtn:document.getElementById('deleteAllBtn'),
          queryStatus:document.getElementById('queryStatus'), rows:document.getElementById('rows')
        };
        let excelServerPath = '';
        function s(el,m,e=false){el.textContent=m; el.style.color=e?'#be123c':'#0f766e';}
        function fill(sel, cols, empty=true){ sel.innerHTML = (empty?['<option value="">請選擇欄位</option>']:[]).concat(cols.map(c=>`<option value="${c}">${c}</option>`)).join('');}
        function fillSheets(names, selected=''){const list=(names&&names.length?names:['CSV']); ids.sheetName.innerHTML=list.map(n=>`<option value="${n}">${n}</option>`).join(''); ids.sheetName.value=selected||list[0];}
        async function uploadExcel(file){
          const fd=new FormData(); fd.append('file',file);
          const r=await fetch('/activity-schedules/upload-excel',{method:'POST',body:fd}); const p=await r.json();
          if(!r.ok) throw new Error(p.detail||'上傳失敗');
          excelServerPath=p.server_path; ids.excelPath.value=file.name; fillSheets(p.sheet_names,p.selected_sheet);
          const cols=p.columns||[]; fill(ids.serialNoColumn,cols); fill(ids.categoryColumn,cols); fill(ids.activityItemColumn,cols); fill(ids.mappedAwardColumn,cols); fill(ids.awardNameColumn,cols); fill(ids.noteColumn,cols);
        }
        async function loadCols(){
          const q=new URLSearchParams({excel_path:excelServerPath, sheet_name:ids.sheetName.value||''});
          const r=await fetch('/activity-schedules/excel-columns?'+q.toString()); const p=await r.json();
          if(!r.ok) throw new Error(p.detail||'讀取欄位失敗');
          fillSheets(p.sheet_names,p.selected_sheet);
          const cols=p.columns||[]; fill(ids.serialNoColumn,cols); fill(ids.categoryColumn,cols); fill(ids.activityItemColumn,cols); fill(ids.mappedAwardColumn,cols); fill(ids.awardNameColumn,cols); fill(ids.noteColumn,cols);
        }
        async function importData(){
          const payload={excel_path:excelServerPath,sheet_name:ids.sheetName.value||'',serial_no_column:ids.serialNoColumn.value,category_column:ids.categoryColumn.value,activity_item_column:ids.activityItemColumn.value,mapped_award_column:ids.mappedAwardColumn.value,award_name_column:ids.awardNameColumn.value,note_column:ids.noteColumn.value};
          const r=await fetch('/activity-awards/import-excel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
          const p=await r.json(); if(!r.ok) throw new Error(p.detail||'匯入失敗'); s(ids.importStatus,`已匯入 ${p.imported_count||0} 筆`); await query();
        }
        function render(items){
          if(!items.length){ids.rows.innerHTML='<tr><td colspan="10">查無資料。</td></tr>'; return;}
          ids.rows.innerHTML=items.map(i=>`<tr><td>${i.id}</td><td><input data-k="serial" data-id="${i.id}" type="number" min="1" value="${i.serial_no||''}"></td><td><input data-k="category" data-id="${i.id}" value="${i.award_category||''}"></td><td><input data-k="item" data-id="${i.id}" value="${i.activity_item||''}"></td><td><input data-k="mapped" data-id="${i.id}" value="${i.mapped_award||''}"></td><td><input data-k="name" data-id="${i.id}" value="${i.award_name||''}"></td><td class="note-cell"><input data-k="note" data-id="${i.id}" value="${i.note||''}"></td><td>${(i.create_time||'').slice(0,16)}</td><td>${(i.update_time||'').slice(0,16)}</td><td><div class="cell-actions"><button data-a="save" data-id="${i.id}">儲存</button><button data-a="del" data-id="${i.id}">刪除</button></div></td></tr>`).join('');
        }
        async function query(){
          const q=new URLSearchParams({keyword:ids.queryKeyword.value||'',limit:'500'}); const r=await fetch('/activity-awards/query?'+q.toString()); const p=await r.json();
          if(!r.ok) throw new Error(p.detail||'查詢失敗'); render(p.items||[]); s(ids.queryStatus,`查詢完成，共 ${p.total||0} 筆`);
        }
        async function saveRow(id){
          const payload={id:Number(id),serial_no:Number(document.querySelector(`input[data-k="serial"][data-id="${id}"]`).value||0),award_category:document.querySelector(`input[data-k="category"][data-id="${id}"]`).value,activity_item:document.querySelector(`input[data-k="item"][data-id="${id}"]`).value,mapped_award:document.querySelector(`input[data-k="mapped"][data-id="${id}"]`).value,award_name:document.querySelector(`input[data-k="name"][data-id="${id}"]`).value,note:document.querySelector(`input[data-k="note"][data-id="${id}"]`).value};
          const r=await fetch('/activity-awards/update',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}); const p=await r.json();
          if(!r.ok) throw new Error(p.detail||'更新失敗'); await query();
        }
        async function delRow(id){
          if(!confirm('確定刪除這筆活動獎項資料？')) return;
          const r=await fetch('/activity-awards/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:Number(id)})}); const p=await r.json();
          if(!r.ok) throw new Error(p.detail||'刪除失敗'); await query();
        }
        async function delAll(){
          if(!confirm('確定全部刪除活動獎項資料？')) return; if(!confirm('此動作無法復原，確認繼續？')) return;
          const r=await fetch('/activity-awards/delete-all',{method:'POST'}); const p=await r.json(); if(!r.ok) throw new Error(p.detail||'全部刪除失敗'); await query();
        }
        ids.excelFileInput.addEventListener('change',async e=>{const f=e.target.files&&e.target.files[0]; if(!f)return; try{await uploadExcel(f); s(ids.importStatus,'Excel 上傳完成');}catch(err){s(ids.importStatus,err.message,true)} e.target.value='';});
        ids.loadColumnsBtn.addEventListener('click',async()=>{try{await loadCols(); s(ids.importStatus,'欄位讀取完成');}catch(err){s(ids.importStatus,err.message,true)}});
        ids.importBtn.addEventListener('click',async()=>{try{await importData();}catch(err){s(ids.importStatus,err.message,true)}});
        ids.queryBtn.addEventListener('click',async()=>{try{await query();}catch(err){s(ids.queryStatus,err.message,true)}});
        ids.deleteAllBtn.addEventListener('click',async()=>{try{await delAll();}catch(err){s(ids.queryStatus,err.message,true)}});
        ids.rows.addEventListener('click',async e=>{const t=e.target; if(!(t instanceof HTMLElement)) return; const id=t.getAttribute('data-id'); if(!id) return; try{ if(t.getAttribute('data-a')==='save') await saveRow(id); if(t.getAttribute('data-a')==='del') await delRow(id);}catch(err){s(ids.queryStatus,err.message,true)}});
      </script>
    </body>
    </html>
    """


def render_admin_batch_ui_html() -> str:
    # Legacy inline UI removed; use clean template.
    return load_ui_template("admin_batch_ui.html")


def render_activity_schedule_ui_html() -> str:
    # Legacy inline UI removed; use clean template.
    return load_ui_template("activity_schedule_ui.html")


def render_activity_photo_import_ui_html() -> str:
    # Legacy inline UI removed; use clean template.
    return load_ui_template("activity_photo_import_ui.html")


def render_admin_ui_html() -> str:
    # Legacy inline UI removed; use clean template.
    return load_ui_template("admin_ui.html")


def get_db_cursor():
    db = mysqlconnector()
    db.connect()
    if db.conn is None or not db.conn.is_connected():
        raise RuntimeError("MySQL 連線失敗")
    return db, db.conn.cursor(dictionary=True)


def ensure_recognition_soft_delete_schema():
    db, cursor = get_db_cursor()
    try:
        cursor.execute("SHOW COLUMNS FROM reco_result")
        reco_cols = {row["Field"] for row in cursor.fetchall()}
        if "is_deleted" not in reco_cols:
            cursor.execute("ALTER TABLE reco_result ADD COLUMN is_deleted TINYINT(1) NOT NULL DEFAULT 0")
        with contextlib.suppress(Exception):
            cursor.execute("ALTER TABLE reco_result ADD KEY idx_reco_result_is_deleted (is_deleted)")

        cursor.execute("SHOW COLUMNS FROM img_upload")
        upload_cols = {row["Field"] for row in cursor.fetchall()}
        if "is_deleted" not in upload_cols:
            cursor.execute("ALTER TABLE img_upload ADD COLUMN is_deleted TINYINT(1) NOT NULL DEFAULT 0")
        with contextlib.suppress(Exception):
            cursor.execute("ALTER TABLE img_upload ADD KEY idx_img_upload_is_deleted (is_deleted)")

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS reco_delete_log (
                id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                reco_result_id BIGINT NULL,
                img_upload_id BIGINT NULL,
                origin_full_path VARCHAR(500) NULL,
                photo_uuid VARCHAR(128) NULL,
                source_status VARCHAR(16) NOT NULL DEFAULT '',
                deleted_reason VARCHAR(255) NOT NULL DEFAULT '',
                deleted_by VARCHAR(64) NOT NULL DEFAULT 'query_ui_advanced',
                deleted_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                KEY idx_reco_delete_log_reco_id (reco_result_id),
                KEY idx_reco_delete_log_img_upload_id (img_upload_id),
                KEY idx_reco_delete_log_photo_uuid (photo_uuid),
                KEY idx_reco_delete_log_deleted_at (deleted_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        with contextlib.suppress(Exception):
            cursor.execute("ALTER TABLE reco_delete_log MODIFY COLUMN reco_result_id BIGINT NULL")
        with contextlib.suppress(Exception):
            cursor.execute("ALTER TABLE reco_delete_log ADD COLUMN img_upload_id BIGINT NULL AFTER reco_result_id")
        with contextlib.suppress(Exception):
            cursor.execute("ALTER TABLE reco_delete_log ADD COLUMN source_status VARCHAR(16) NOT NULL DEFAULT '' AFTER photo_uuid")
        with contextlib.suppress(Exception):
            cursor.execute("ALTER TABLE reco_delete_log ADD KEY idx_reco_delete_log_img_upload_id (img_upload_id)")
        # 停用舊版 5 碼（或非 3 碼）device_id，要求裝置重新註冊為 3 碼。
        cursor.execute(
            """
            UPDATE device_registry
            SET status = 'inactive',
                updated_at = NOW()
            WHERE CHAR_LENGTH(TRIM(device_id)) <> 3
              AND status <> 'inactive'
            """
        )
        db.conn.commit()
    finally:
        cursor.close()
        db.close()


def ensure_photo_mark_schema():
    db, cursor = get_db_cursor()
    try:
        cursor.execute("SHOW COLUMNS FROM img_upload")
        upload_cols = {row["Field"] for row in cursor.fetchall()}
        if "is_video_pick" not in upload_cols:
            cursor.execute("ALTER TABLE img_upload ADD COLUMN is_video_pick TINYINT(1) NOT NULL DEFAULT 0")
        with contextlib.suppress(Exception):
            cursor.execute("ALTER TABLE img_upload ADD KEY idx_img_upload_is_video_pick (is_video_pick)")

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS img_upload_award_tag (
                id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                img_upload_id BIGINT NOT NULL,
                award_id BIGINT NOT NULL,
                create_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                update_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uq_img_upload_award_tag (img_upload_id, award_id),
                KEY idx_img_upload_award_tag_img_upload_id (img_upload_id),
                KEY idx_img_upload_award_tag_award_id (award_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        db.conn.commit()
    finally:
        cursor.close()
        db.close()


def ensure_photo_query_indexes():
    global PHOTO_QUERY_INDEXES_READY
    if PHOTO_QUERY_INDEXES_READY:
        return

    db, cursor = get_db_cursor()
    try:
        cursor.execute("SHOW COLUMNS FROM img_upload")
        upload_cols = {row["Field"] for row in cursor.fetchall()}
        cursor.execute("SHOW COLUMNS FROM reco_result")
        reco_cols = {row["Field"] for row in cursor.fetchall()}
        cursor.execute("SHOW COLUMNS FROM base")
        base_cols = {row["Field"] for row in cursor.fetchall()}

        if {"photo_uuid", "create_time"}.issubset(upload_cols):
            upload_index_columns = ["photo_uuid"]
            if "is_deleted" in upload_cols:
                upload_index_columns.append("is_deleted")
            upload_index_columns.append("create_time")
            upload_index_name = "idx_img_upload_" + "_".join(upload_index_columns)
            with contextlib.suppress(Exception):
                cursor.execute(
                    f"ALTER TABLE img_upload ADD KEY {upload_index_name} ({', '.join(upload_index_columns)})"
                )

        if {"photo_uuid", "create_time"}.issubset(reco_cols):
            reco_index_columns = ["photo_uuid"]
            if "is_deleted" in reco_cols:
                reco_index_columns.append("is_deleted")
            reco_index_columns.append("create_time")
            reco_index_name = "idx_reco_result_" + "_".join(reco_index_columns)
            with contextlib.suppress(Exception):
                cursor.execute(
                    f"ALTER TABLE reco_result ADD KEY {reco_index_name} ({', '.join(reco_index_columns)})"
                )

        if {"dept", "year", "team", "name"}.issubset(base_cols):
            with contextlib.suppress(Exception):
                cursor.execute(
                    "ALTER TABLE base ADD KEY idx_base_dept_year_team_name (dept, year, team, name)"
                )

        db.conn.commit()
        PHOTO_QUERY_INDEXES_READY = True
        logger.info("photo query indexes ensured")
    finally:
        cursor.close()
        db.close()


def _build_photo_join_expr(join_key: str) -> str:
    normalized_key = str(join_key or "").strip().lower()
    if normalized_key == "origin_full_path":
        return "rr.origin_full_path = iu.origin_full_path"
    return "(rr.photo_uuid IS NOT NULL AND rr.photo_uuid = iu.photo_uuid)"


def ensure_device_registry_table():
    db, cursor = get_db_cursor()
    try:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS device_registry (
                id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                device_id VARCHAR(5) NOT NULL,
                client_key VARCHAR(128) NOT NULL,
                device_name VARCHAR(255) DEFAULT '',
                status VARCHAR(32) NOT NULL DEFAULT 'active',
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                last_seen_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY uq_device_registry_device_id (device_id),
                UNIQUE KEY uq_device_registry_client_key (client_key)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        db.conn.commit()
    finally:
        cursor.close()
        db.close()


def _generate_unique_device_id(cursor):
    chars = string.ascii_uppercase + string.digits
    for _ in range(100):
        candidate = "".join(random.choice(chars) for _ in range(3))
        cursor.execute("SELECT 1 FROM device_registry WHERE device_id = %s LIMIT 1", (candidate,))
        if not cursor.fetchone():
            return candidate
    raise RuntimeError("無法產生唯一 device_id，請稍後再試")


def register_or_get_device(client_key: str, device_name: str = ""):
    if not client_key.strip():
        raise ValueError("client_key 不可為空")
    ensure_device_registry_table()
    db, cursor = get_db_cursor()
    try:
        cursor.execute(
            """
            SELECT device_id, client_key, device_name, status
            FROM device_registry
            WHERE client_key = %s
            LIMIT 1
            """,
            (client_key.strip(),),
        )
        row = cursor.fetchone()
        if row:
            existing_id = str(row.get("device_id") or "").strip()
            existing_status = str(row.get("status") or "").strip().lower()
            if len(existing_id) != 3 or existing_status != "active":
                device_id = _generate_unique_device_id(cursor)
                cursor.execute(
                    """
                    UPDATE device_registry
                    SET device_id = %s,
                        status = 'active',
                        last_seen_at = NOW(),
                        updated_at = NOW(),
                        device_name = CASE WHEN %s = '' THEN device_name ELSE %s END
                    WHERE client_key = %s
                    """,
                    (device_id, device_name.strip(), device_name.strip(), client_key.strip()),
                )
                db.conn.commit()
                return {"device_id": device_id, "client_key": row["client_key"], "device_name": (device_name.strip() or row.get("device_name", ""))}

            cursor.execute(
                """
                UPDATE device_registry
                SET last_seen_at = NOW(),
                    status = 'active',
                    device_name = CASE WHEN %s = '' THEN device_name ELSE %s END
                WHERE client_key = %s
                """,
                (device_name.strip(), device_name.strip(), client_key.strip()),
            )
            db.conn.commit()
            return {"device_id": row["device_id"], "client_key": row["client_key"], "device_name": row.get("device_name", "")}

        device_id = _generate_unique_device_id(cursor)
        cursor.execute(
            """
            INSERT INTO device_registry (device_id, client_key, device_name, status, created_at, updated_at, last_seen_at)
            VALUES (%s, %s, %s, 'active', NOW(), NOW(), NOW())
            """,
            (device_id, client_key.strip(), device_name.strip()),
        )
        db.conn.commit()
        return {"device_id": device_id, "client_key": client_key.strip(), "device_name": device_name.strip()}
    finally:
        cursor.close()
        db.close()


def get_activity_time_range(activity_schedule_id: int):
    ensure_activity_tables()
    db, cursor = get_db_cursor()
    try:
        cursor.execute(
            """
            SELECT id, activity_date, activity_time
            FROM activity_schedule
            WHERE id = %s
            LIMIT 1
            """,
            (activity_schedule_id,),
        )
        current = cursor.fetchone()
        if not current:
            raise ValueError("找不到指定的活動行程")

        activity_date = current.get("activity_date")
        activity_time = current.get("activity_time")
        if activity_date is None:
            raise ValueError("活動行程缺少日期，無法套用活動名稱查詢")

        date_text = str(activity_date)
        if hasattr(activity_date, "strftime"):
            date_text = activity_date.strftime("%Y-%m-%d")

        start_dt = datetime.strptime(f"{date_text} {str(activity_time or '00:00:00')}", "%Y-%m-%d %H:%M:%S")

        cursor.execute(
            """
            SELECT activity_date, activity_time
            FROM activity_schedule
            WHERE activity_date = %s
              AND COALESCE(activity_time, '00:00:00') > %s
            ORDER BY COALESCE(activity_time, '00:00:00') ASC
            LIMIT 1
            """,
            (date_text, str(activity_time or "00:00:00")),
        )
        next_row = cursor.fetchone()
        if next_row:
            end_dt = datetime.strptime(
                f"{date_text} {str(next_row.get('activity_time') or '00:00:00')}",
                "%Y-%m-%d %H:%M:%S",
            )
        else:
            end_dt = datetime.strptime(f"{date_text} 23:59:59", "%Y-%m-%d %H:%M:%S") + timedelta(seconds=1)
        return start_dt, end_dt
    finally:
        cursor.close()
        db.close()


def build_advanced_conditions(
    dept: list[str] | None = None,
    year: int | None = None,
    years: list[int] | None = None,
    team: list[str] | None = None,
    name: str = "",
    start_time: str = "",
    end_time: str = "",
    taken_start_time: str = "",
    taken_end_time: str = "",
    det_score_min: float | None = None,
    det_score_max: float | None = None,
    reco_count: int | None = None,
    activity_schedule_id: int | None = None,
    recognition_status: str = "",
    mark_type: str = "",
    award_ids: list[int] | None = None,
):
    conditions = []
    params = []

    normalized_depts = [item for item in (dept or []) if str(item).strip()]
    if normalized_depts:
        placeholders = ", ".join(["%s"] * len(normalized_depts))
        conditions.append(f"COALESCE(b.dept, '') IN ({placeholders})")
        params.extend(normalized_depts)
    normalized_years = []
    for item in (years or []):
        with contextlib.suppress(Exception):
            normalized_years.append(int(item))
    normalized_years = sorted(set(normalized_years))
    if year is not None:
        conditions.append("b.year = %s")
        params.append(year)
    elif normalized_years:
        placeholders = ", ".join(["%s"] * len(normalized_years))
        conditions.append(f"b.year IN ({placeholders})")
        params.extend(normalized_years)
    normalized_teams = [item for item in (team or []) if str(item).strip()]
    if normalized_teams:
        placeholders = ", ".join(["%s"] * len(normalized_teams))
        conditions.append(f"COALESCE(b.team, '') IN ({placeholders})")
        params.extend(normalized_teams)
    if name:
        conditions.append("COALESCE(b.name, '') LIKE %s")
        params.append(f"%{name}%")
    if start_time:
        conditions.append("COALESCE(iu.photo_file_time, rr.create_time, iu.create_time) >= %s")
        params.append(parse_datetime_filter(start_time, is_end=False))
    if end_time:
        conditions.append("COALESCE(iu.photo_file_time, rr.create_time, iu.create_time) < %s")
        params.append(parse_datetime_filter(end_time, is_end=True))
    if taken_start_time:
        conditions.append("COALESCE(rr.photo_taken_time, iu.photo_taken_time) >= %s")
        params.append(parse_datetime_filter(taken_start_time, is_end=False))
    if taken_end_time:
        conditions.append("COALESCE(rr.photo_taken_time, iu.photo_taken_time) < %s")
        params.append(parse_datetime_filter(taken_end_time, is_end=True))
    if det_score_min is not None:
        conditions.append(
            """
            EXISTS (
                SELECT 1
                FROM JSON_TABLE(
                    COALESCE(rr.reco_res, '[]'),
                    '$[*]' COLUMNS(det_score DECIMAL(12,6) PATH '$.det_score')
                ) jt
                WHERE jt.det_score IS NOT NULL AND jt.det_score >= %s
            )
            """
        )
        params.append(det_score_min)
    if det_score_max is not None:
        conditions.append(
            """
            EXISTS (
                SELECT 1
                FROM JSON_TABLE(
                    COALESCE(rr.reco_res, '[]'),
                    '$[*]' COLUMNS(det_score DECIMAL(12,6) PATH '$.det_score')
                ) jt
                WHERE jt.det_score IS NOT NULL AND jt.det_score <= %s
            )
            """
        )
        params.append(det_score_max)
    if reco_count is not None:
        conditions.append("COALESCE(rr.reco_count, 0) = %s")
        params.append(reco_count)
    if activity_schedule_id is not None:
        start_time_by_activity, end_time_by_activity = get_activity_time_range(activity_schedule_id)
        conditions.append("COALESCE(rr.photo_taken_time, iu.photo_taken_time, iu.photo_file_time) >= %s")
        params.append(start_time_by_activity)
        conditions.append("COALESCE(rr.photo_taken_time, iu.photo_taken_time, iu.photo_file_time) < %s")
        params.append(end_time_by_activity)
    if recognition_status:
        normalized_status = str(recognition_status).strip().upper()
        if normalized_status == "DONE":
            conditions.append("COALESCE(iu.reco_status, 'PENDING') = 'DONE'")
            conditions.append(
                """
                NOT (
                  LOWER(COALESCE(rr.reco_name, '')) LIKE '%"unknown"%'
                  OR LOWER(COALESCE(rr.reco_name, '')) = 'unknown'
                )
                """
            )
        elif normalized_status == "MIXED":
            conditions.append("COALESCE(iu.reco_status, 'PENDING') = 'DONE'")
            conditions.append("COALESCE(rr.reco_count, 0) > 0")
            conditions.append(
                """
                (
                  LOWER(COALESCE(rr.reco_name, '')) LIKE '%"unknown"%'
                  OR LOWER(COALESCE(rr.reco_name, '')) = 'unknown'
                )
                """
            )
        elif normalized_status == "FAILED":
            conditions.append("COALESCE(iu.reco_status, 'PENDING') = 'FAILED'")
        elif normalized_status == "PENDING":
            conditions.append("COALESCE(iu.reco_status, 'PENDING') IN ('PENDING','RETRY')")
        elif normalized_status == "UNKNOWN":
            conditions.append(
                """
                (
                  COALESCE(iu.reco_status, 'PENDING') = 'DONE'
                  AND COALESCE(rr.reco_count, 0) = 0
                  AND (
                    LOWER(COALESCE(rr.reco_name, '')) LIKE '%"unknown"%'
                    OR LOWER(COALESCE(rr.reco_name, '')) = 'unknown'
                  )
                )
                """
                )

    normalized_mark_type = str(mark_type or "").strip().lower()
    normalized_award_ids = [int(item) for item in (award_ids or []) if str(item).strip().isdigit()]
    if normalized_mark_type == "award":
        conditions.append(
            "EXISTS (SELECT 1 FROM img_upload_award_tag iuat WHERE iuat.img_upload_id = iu.id)"
        )
    elif normalized_mark_type == "video":
        conditions.append("COALESCE(iu.is_video_pick, 0) = 1")
    elif normalized_mark_type == "both":
        conditions.append("COALESCE(iu.is_video_pick, 0) = 1")
        conditions.append(
            "EXISTS (SELECT 1 FROM img_upload_award_tag iuat WHERE iuat.img_upload_id = iu.id)"
        )
    elif normalized_mark_type == "none":
        conditions.append("COALESCE(iu.is_video_pick, 0) = 0")
        conditions.append(
            "NOT EXISTS (SELECT 1 FROM img_upload_award_tag iuat WHERE iuat.img_upload_id = iu.id)"
        )

    if normalized_award_ids:
        placeholders = ", ".join(["%s"] * len(normalized_award_ids))
        conditions.append(
            f"""
            EXISTS (
              SELECT 1
              FROM img_upload_award_tag iuat
              WHERE iuat.img_upload_id = iu.id
                AND iuat.award_id IN ({placeholders})
            )
            """
        )
        params.extend(normalized_award_ids)

    return conditions, params


def build_advanced_base_exists_clause(
    dept: list[str] | None = None,
    year: int | None = None,
    years: list[int] | None = None,
    team: list[str] | None = None,
    name: str = "",
):
    clauses = []
    params = []

    normalized_depts = [item for item in (dept or []) if str(item).strip()]
    if normalized_depts:
        placeholders = ", ".join(["%s"] * len(normalized_depts))
        clauses.append(f"COALESCE(b.dept, '') IN ({placeholders})")
        params.extend(normalized_depts)

    normalized_years = []
    for item in (years or []):
        with contextlib.suppress(Exception):
            normalized_years.append(int(item))
    normalized_years = sorted(set(normalized_years))
    if year is not None:
        clauses.append("b.year = %s")
        params.append(year)
    elif normalized_years:
        placeholders = ", ".join(["%s"] * len(normalized_years))
        clauses.append(f"b.year IN ({placeholders})")
        params.extend(normalized_years)

    normalized_teams = [item for item in (team or []) if str(item).strip()]
    if normalized_teams:
        placeholders = ", ".join(["%s"] * len(normalized_teams))
        clauses.append(f"COALESCE(b.team, '') IN ({placeholders})")
        params.extend(normalized_teams)

    if name:
        clauses.append("COALESCE(b.name, '') LIKE %s")
        params.append(f"%{name}%")

    if not clauses:
        return "", []

    clause = f"""
    EXISTS (
        SELECT 1
        FROM base b
        WHERE JSON_SEARCH(
            COALESCE(rr.reco_name, '[]'),
            'one',
            CONCAT(b.dept, '_', b.year, '_', COALESCE(b.team, ''), '_', COALESCE(b.name, ''))
        ) IS NOT NULL
          AND {' AND '.join(clauses)}
    )
    """
    return clause, params


def query_advanced_recognition_records(
    dept: list[str] | None = None,
    year: int | None = None,
    years: list[int] | None = None,
    team: list[str] | None = None,
    name: str = "",
    start_time: str = "",
    end_time: str = "",
    taken_start_time: str = "",
    taken_end_time: str = "",
    det_score_min: float | None = None,
    det_score_max: float | None = None,
    reco_count: int | None = None,
    activity_schedule_id: int | None = None,
    recognition_status: str = "",
    mark_type: str = "",
    award_ids: list[int] | None = None,
    page: int = 1,
    limit: int = 50,
    result_mode: str = "photo",
):
    ensure_activity_tables()
    ensure_recognition_soft_delete_schema()
    ensure_photo_mark_schema()
    ensure_photo_query_indexes()
    db, cursor = get_db_cursor()
    normalized_mode = str(result_mode or "photo").strip().lower()
    if normalized_mode not in {"photo", "detail"}:
        normalized_mode = "photo"

    try:
        if normalized_mode == "photo":
            query_started = time.perf_counter()
            photo_conditions, photo_params = build_advanced_conditions(
                dept=None,
                year=None,
                years=None,
                team=None,
                name="",
                start_time=start_time,
                end_time=end_time,
                taken_start_time=taken_start_time,
                taken_end_time=taken_end_time,
                det_score_min=det_score_min,
                det_score_max=det_score_max,
                reco_count=reco_count,
                activity_schedule_id=activity_schedule_id,
                recognition_status=recognition_status,
                mark_type=mark_type,
                award_ids=award_ids,
            )
            base_clause, base_params = build_advanced_base_exists_clause(
                dept=dept,
                year=year,
                years=years,
                team=team,
                name=name,
            )
            if base_clause:
                photo_conditions.append(base_clause)
                photo_params.extend(base_params)

            where_clause = ""
            if photo_conditions:
                where_clause = "WHERE " + " AND ".join(photo_conditions)
            base_where = "COALESCE(iu.is_deleted, 0) = 0"
            if where_clause:
                where_clause = where_clause + " AND " + base_where
            else:
                where_clause = "WHERE " + base_where

            def _build_photo_path_subquery(join_key: str, path_rank: int) -> str:
                join_expr = _build_photo_join_expr(join_key)
                return f"""
                SELECT
                    iu.id AS img_upload_id,
                    MAX(COALESCE(rr.create_time, iu.create_time)) AS sort_time,
                    {path_rank} AS path_rank
                FROM img_upload iu
                LEFT JOIN reco_result rr
                  ON ({join_expr})
                  AND COALESCE(rr.is_deleted, 0) = 0
                {where_clause}
                GROUP BY iu.id
                """

            photo_uuid_path_sql = _build_photo_path_subquery("photo_uuid", 0)
            origin_path_sql = _build_photo_path_subquery("origin_full_path", 1)

            count_sql = f"""
            SELECT COUNT(*) AS total_count
            FROM (
                SELECT img_upload_id
                FROM (
                    {photo_uuid_path_sql}
                    UNION ALL
                    {origin_path_sql}
                ) photo_matches
                GROUP BY img_upload_id
            ) counted
            """
            count_started = time.perf_counter()
            cursor.execute(count_sql, tuple(photo_params + photo_params))
            count_row = cursor.fetchone() or {}
            total = int(count_row.get("total_count") or 0)
            count_elapsed_ms = int((time.perf_counter() - count_started) * 1000)

            if total <= 0:
                logger.info(
                    "query-ui-advanced photo count zero elapsed_ms=%s filters=%s",
                    count_elapsed_ms,
                    {"year": year, "years": years, "page": page, "limit": limit},
                )
                return {
                    "total": 0,
                    "page": page,
                    "page_size": limit,
                    "total_pages": 1,
                    "result_mode": normalized_mode,
                    "items": [],
                }

            offset = max(page - 1, 0) * limit
            page_sql = f"""
            SELECT
                img_upload_id,
                MAX(sort_time) AS sort_time,
                MIN(path_rank) AS path_rank
            FROM (
                {photo_uuid_path_sql}
                UNION ALL
                {origin_path_sql}
            ) photo_matches
            GROUP BY img_upload_id
            ORDER BY sort_time DESC, path_rank ASC, img_upload_id DESC
            LIMIT %s OFFSET %s
            """
            page_started = time.perf_counter()
            cursor.execute(page_sql, tuple(photo_params + photo_params + [int(limit), int(offset)]))
            page_id_rows = cursor.fetchall() or []
            page_ids = [int(row.get("img_upload_id") or 0) for row in page_id_rows if int(row.get("img_upload_id") or 0) > 0]
            page_elapsed_ms = int((time.perf_counter() - page_started) * 1000)

            if not page_ids:
                logger.info(
                    "query-ui-advanced photo no page ids count_elapsed_ms=%s page_elapsed_ms=%s total=%s",
                    count_elapsed_ms,
                    page_elapsed_ms,
                    total,
                )
                return {
                    "total": total,
                    "page": page,
                    "page_size": limit,
                    "total_pages": max((total + limit - 1) // limit, 1),
                    "result_mode": normalized_mode,
                    "items": [],
                }

            placeholders = ", ".join(["%s"] * len(page_ids))

            def _build_photo_detail_sql(join_key: str, path_rank: int) -> str:
                join_expr = _build_photo_join_expr(join_key)
                return f"""
                SELECT
                    rr.id,
                    rr.id AS reco_result_id,
                    iu.id AS img_upload_id,
                    COALESCE(rr.photo_uuid, iu.photo_uuid) AS photo_uuid,
                    COALESCE(rr.origin_full_path, iu.origin_full_path) AS origin_full_path,
                    COALESCE(rr.thumbs_full_path, iu.thumbs_full_path) AS thumbs_full_path,
                    COALESCE(rr.photo_taken_time, iu.photo_taken_time) AS photo_taken_time,
                    iu.photo_file_time,
                    iu.img_score AS image_score,
                    COALESCE(rr.reco_count, 0) AS reco_count,
                    COALESCE(rr.reco_unknow, 0) AS reco_unknow,
                    rr.reco_name,
                    rr.reco_res,
                    COALESCE(iu.photo_file_time, rr.create_time, iu.create_time) AS photo_create_time,
                    COALESCE(rr.create_time, iu.create_time) AS record_create_time,
                    COALESCE(rr.update_time, iu.update_time) AS update_time,
                    COALESCE(iu.reco_error, '') AS reco_error,
                    COALESCE(iu.reco_status, 'PENDING') AS reco_status_raw,
                    COALESCE(iu.is_video_pick, 0) AS is_video_pick,
                    {path_rank} AS path_rank,
                    CASE
                      WHEN COALESCE(iu.reco_status, 'PENDING') = 'FAILED' THEN 'FAILED'
                      WHEN rr.id IS NULL THEN 'PENDING'
                      WHEN COALESCE(iu.reco_status, 'PENDING') = 'DONE'
                           AND COALESCE(rr.reco_count, 0) > 0
                           AND (
                             LOWER(COALESCE(rr.reco_name, '')) LIKE '%"unknown"%'
                             OR LOWER(COALESCE(rr.reco_name, '')) = 'unknown'
                           ) THEN 'MIXED'
                      WHEN LOWER(COALESCE(rr.reco_name, '')) LIKE '%"unknown"%'
                           OR LOWER(COALESCE(rr.reco_name, '')) = 'unknown' THEN
                           CASE
                             WHEN COALESCE(rr.reco_count, 0) = 0 THEN 'UNKNOWN'
                             ELSE 'MIXED'
                           END
                      WHEN COALESCE(iu.reco_status, 'PENDING') = 'DONE' THEN 'DONE'
                      ELSE COALESCE(iu.reco_status, 'PENDING')
                    END AS recognition_status,
                    CASE
                      WHEN LOWER(COALESCE(rr.reco_name, '')) LIKE '%"unknown"%'
                           OR LOWER(COALESCE(rr.reco_name, '')) = 'unknown' THEN 1
                      ELSE 0
                    END AS is_unknown,
                    b.dept,
                    b.year,
                    COALESCE(b.team, '') AS team,
                    COALESCE(b.name, '') AS name,
                    CASE
                      WHEN b.id IS NOT NULL THEN CONCAT(b.dept, '_', b.year, '_', COALESCE(b.team, ''), '_', COALESCE(b.name, ''))
                      WHEN LOWER(COALESCE(rr.reco_name, '')) LIKE '%"unknown"%'
                           OR LOWER(COALESCE(rr.reco_name, '')) = 'unknown' THEN 'unknown'
                      ELSE ''
                    END AS matched_person
                FROM img_upload iu
                LEFT JOIN reco_result rr
                  ON ({join_expr})
                  AND COALESCE(rr.is_deleted, 0) = 0
                LEFT JOIN base b
                  ON JSON_SEARCH(
                    COALESCE(rr.reco_name, '[]'),
                    'one',
                    CONCAT(b.dept, '_', b.year, '_', COALESCE(b.team, ''), '_', COALESCE(b.name, ''))
                  ) IS NOT NULL
                {where_clause}
                  AND iu.id IN ({placeholders})
                ORDER BY COALESCE(rr.create_time, iu.create_time) DESC, iu.id DESC, rr.id DESC
                """

            combined_rows = []
            detail_elapsed_ms = 0
            for join_key, path_rank in (("photo_uuid", 0), ("origin_full_path", 1)):
                detail_sql = _build_photo_detail_sql(join_key, path_rank)
                detail_started = time.perf_counter()
                cursor.execute(detail_sql, tuple(photo_params + page_ids))
                path_rows = normalize_record_rows(cursor.fetchall())
                detail_elapsed_ms += int((time.perf_counter() - detail_started) * 1000)
                for row in path_rows:
                    row["_path_rank"] = path_rank
                combined_rows.extend(path_rows)

            deduped_rows: dict[tuple[str, int], dict] = {}
            for row in combined_rows:
                reco_result_id = int(row.get("reco_result_id") or 0)
                upload_id = int(row.get("img_upload_id") or 0)
                key = ("reco", reco_result_id) if reco_result_id > 0 else ("upload", upload_id)
                existing = deduped_rows.get(key)
                current_path_rank = int(row.get("_path_rank") or 0)
                if existing is None or current_path_rank < int(existing.get("_path_rank") or 0):
                    deduped_rows[key] = row

            rows = list(deduped_rows.values())
            rows.sort(
                key=lambda r: (
                    _to_naive_datetime(r.get("record_create_time") or r.get("photo_create_time")) or datetime.min,
                    -int(r.get("_path_rank") or 0),
                    int(r.get("img_upload_id") or 0),
                    int(r.get("reco_result_id") or 0),
                ),
                reverse=True,
            )

            grouped: dict[int, dict] = {}
            for row in rows:
                upload_id = int(row.get("img_upload_id") or 0)
                if upload_id <= 0:
                    continue

                if upload_id not in grouped:
                    grouped[upload_id] = dict(row)
                    grouped[upload_id]["_known_names"] = []
                    grouped[upload_id]["_known_name_set"] = set()
                    grouped[upload_id]["_unknown_count"] = 0
                    grouped[upload_id]["_det_scores"] = []
                    grouped[upload_id]["_has_reco_result"] = bool(row.get("reco_result_id"))

                item = grouped[upload_id]

                reco_names = row.get("reco_name") if isinstance(row.get("reco_name"), list) else []
                for name_value in reco_names:
                    name_text = str(name_value or "").strip()
                    if not name_text:
                        continue
                    if name_text.lower() == "unknown":
                        continue
                    if name_text not in item["_known_name_set"]:
                        item["_known_name_set"].add(name_text)
                        item["_known_names"].append(name_text)

                row_unknown = int(row.get("reco_unknow") or 0)
                if row_unknown > item["_unknown_count"]:
                    item["_unknown_count"] = row_unknown

                reco_res = row.get("reco_res") if isinstance(row.get("reco_res"), list) else []
                for entry in reco_res:
                    if not isinstance(entry, dict):
                        continue
                    value = entry.get("det_score")
                    if value is None:
                        continue
                    with contextlib.suppress(TypeError, ValueError):
                        item["_det_scores"].append(float(value))

                if not item.get("photo_taken_time") and row.get("photo_taken_time"):
                    item["photo_taken_time"] = row.get("photo_taken_time")
                if not item.get("origin_full_path") and row.get("origin_full_path"):
                    item["origin_full_path"] = row.get("origin_full_path")
                if not item.get("thumbs_full_path") and row.get("thumbs_full_path"):
                    item["thumbs_full_path"] = row.get("thumbs_full_path")
                if not item.get("photo_uuid") and row.get("photo_uuid"):
                    item["photo_uuid"] = row.get("photo_uuid")
                if row.get("reco_result_id") and not item.get("reco_result_id"):
                    item["reco_result_id"] = row.get("reco_result_id")
                    item["_has_reco_result"] = True

            aggregated_rows = []
            for item in grouped.values():
                known_names = item.get("_known_names", [])
                unknown_count = int(item.get("_unknown_count") or 0)
                known_count = len(known_names)
                face_total_count = known_count + max(unknown_count, 0)

                if face_total_count <= 0:
                    fallback_known = int(item.get("reco_count") or 0)
                    fallback_unknown = int(item.get("reco_unknow") or 0)
                    if fallback_known > 0 or fallback_unknown > 0:
                        known_count = max(fallback_known, known_count)
                        unknown_count = max(fallback_unknown, unknown_count)
                        face_total_count = known_count + unknown_count

                reco_status_raw = str(item.get("reco_status_raw") or "").upper()
                if reco_status_raw == "FAILED":
                    recognition_status_value = "FAILED"
                elif not item.get("_has_reco_result"):
                    recognition_status_value = "PENDING"
                elif known_count > 0 and unknown_count > 0:
                    recognition_status_value = "MIXED"
                elif known_count > 0:
                    recognition_status_value = "DONE"
                elif unknown_count > 0:
                    recognition_status_value = "UNKNOWN"
                else:
                    recognition_status_value = "PENDING"

                det_scores = item.get("_det_scores", [])
                item["known_names_full"] = known_names
                item["known_names_preview"] = known_names[:20]
                item["known_names_hidden_count"] = max(len(known_names) - 20, 0)
                item["known_count"] = known_count
                item["unknown_count"] = unknown_count
                item["face_total_count"] = face_total_count
                item["is_mixed"] = 1 if recognition_status_value == "MIXED" else 0
                item["recognition_status"] = recognition_status_value
                item["is_unknown"] = 1 if recognition_status_value == "UNKNOWN" else 0
                item["det_score_values"] = det_scores
                if det_scores:
                    item["det_score_max"] = max(det_scores)
                    item["det_score_min"] = min(det_scores)
                    item["det_score_avg"] = sum(det_scores) / len(det_scores)
                else:
                    item["det_score_max"] = None
                    item["det_score_min"] = None
                    item["det_score_avg"] = None

                item.pop("_known_names", None)
                item.pop("_known_name_set", None)
                item.pop("_unknown_count", None)
                item.pop("_det_scores", None)
                item.pop("_has_reco_result", None)
                item.pop("_path_rank", None)
                aggregated_rows.append(item)

            img_upload_ids = [int(row.get("img_upload_id")) for row in aggregated_rows if row.get("img_upload_id")]
            award_tags_map: dict[int, list[dict]] = {}
            if img_upload_ids:
                placeholders = ", ".join(["%s"] * len(img_upload_ids))
                cursor.execute(
                    f"""
                    SELECT
                        iuat.img_upload_id,
                        aam.id AS award_id,
                        COALESCE(aam.serial_no, 0) AS serial_no,
                        COALESCE(aam.award_category, '') AS award_category,
                        COALESCE(aam.activity_item, '') AS activity_item,
                        COALESCE(aam.mapped_award, '') AS mapped_award,
                        COALESCE(aam.award_name, '') AS award_name
                    FROM img_upload_award_tag iuat
                    INNER JOIN activity_award_master aam ON aam.id = iuat.award_id
                    WHERE iuat.img_upload_id IN ({placeholders})
                    ORDER BY COALESCE(aam.serial_no, 0) ASC, aam.id ASC
                    """,
                    tuple(img_upload_ids),
                )
                for tag_row in normalize_record_rows(cursor.fetchall() or []):
                    upload_id = int(tag_row.get("img_upload_id") or 0)
                    if upload_id <= 0:
                        continue
                    award_tags_map.setdefault(upload_id, []).append(
                        {
                            "award_id": int(tag_row.get("award_id") or 0),
                            "serial_no": int(tag_row.get("serial_no") or 0),
                            "award_category": tag_row.get("award_category") or "",
                            "activity_item": tag_row.get("activity_item") or "",
                            "mapped_award": tag_row.get("mapped_award") or "",
                            "award_name": tag_row.get("award_name") or "",
                        }
                    )
            for row in aggregated_rows:
                upload_id = int(row.get("img_upload_id") or 0)
                row["is_video_pick"] = int(row.get("is_video_pick") or 0)
                row["award_tags"] = award_tags_map.get(upload_id, [])

            logger.info(
                "query-ui-advanced photo timings count_ms=%s page_id_ms=%s detail_ms=%s items=%s total=%s year=%s years=%s",
                count_elapsed_ms,
                page_elapsed_ms,
                detail_elapsed_ms,
                len(aggregated_rows),
                total,
                year,
                years,
            )
            return {
                "total": total,
                "page": page,
                "page_size": limit,
                "total_pages": max((total + limit - 1) // limit, 1),
                "result_mode": normalized_mode,
                "items": aggregated_rows,
            }

        conditions, params = build_advanced_conditions(
            dept=dept,
            year=year,
            years=years,
            team=team,
            name=name,
            start_time=start_time,
            end_time=end_time,
            taken_start_time=taken_start_time,
            taken_end_time=taken_end_time,
            det_score_min=None,
            det_score_max=None,
            reco_count=reco_count,
            activity_schedule_id=activity_schedule_id,
            recognition_status=recognition_status,
            mark_type=mark_type,
            award_ids=award_ids,
        )
        offset = max(page - 1, 0) * limit

        where_clause = ""
        if conditions:
            where_clause = "WHERE " + " AND ".join(conditions)
        base_where = "COALESCE(iu.is_deleted, 0) = 0"
        if where_clause:
            where_clause = where_clause + " AND " + base_where
        else:
            where_clause = "WHERE " + base_where

        sql = f"""
        SELECT
            rr.id,
            rr.id AS reco_result_id,
            iu.id AS img_upload_id,
            COALESCE(rr.photo_uuid, iu.photo_uuid) AS photo_uuid,
            COALESCE(rr.origin_full_path, iu.origin_full_path) AS origin_full_path,
            COALESCE(rr.thumbs_full_path, iu.thumbs_full_path) AS thumbs_full_path,
            COALESCE(rr.photo_taken_time, iu.photo_taken_time) AS photo_taken_time,
            iu.photo_file_time,
            iu.img_score AS image_score,
            COALESCE(rr.reco_count, 0) AS reco_count,
            COALESCE(rr.reco_unknow, 0) AS reco_unknow,
            rr.reco_name,
            rr.reco_res,
            COALESCE(iu.photo_file_time, rr.create_time, iu.create_time) AS photo_create_time,
            COALESCE(rr.create_time, iu.create_time) AS record_create_time,
            COALESCE(rr.update_time, iu.update_time) AS update_time,
            COALESCE(iu.reco_error, '') AS reco_error,
            COALESCE(iu.reco_status, 'PENDING') AS reco_status_raw,
            COALESCE(iu.is_video_pick, 0) AS is_video_pick,
            CASE
              WHEN COALESCE(iu.reco_status, 'PENDING') = 'FAILED' THEN 'FAILED'
              WHEN rr.id IS NULL THEN 'PENDING'
              WHEN COALESCE(iu.reco_status, 'PENDING') = 'DONE'
                   AND COALESCE(rr.reco_count, 0) > 0
                   AND (
                     LOWER(COALESCE(rr.reco_name, '')) LIKE '%"unknown"%'
                     OR LOWER(COALESCE(rr.reco_name, '')) = 'unknown'
                   ) THEN 'MIXED'
              WHEN LOWER(COALESCE(rr.reco_name, '')) LIKE '%"unknown"%'
                   OR LOWER(COALESCE(rr.reco_name, '')) = 'unknown' THEN
                   CASE
                     WHEN COALESCE(rr.reco_count, 0) = 0 THEN 'UNKNOWN'
                     ELSE 'MIXED'
                   END
              WHEN COALESCE(iu.reco_status, 'PENDING') = 'DONE' THEN 'DONE'
              ELSE COALESCE(iu.reco_status, 'PENDING')
            END AS recognition_status,
            CASE
              WHEN LOWER(COALESCE(rr.reco_name, '')) LIKE '%"unknown"%'
                   OR LOWER(COALESCE(rr.reco_name, '')) = 'unknown' THEN 1
              ELSE 0
            END AS is_unknown,
            b.dept,
            b.year,
            COALESCE(b.team, '') AS team,
            COALESCE(b.name, '') AS name,
            CASE
              WHEN b.id IS NOT NULL THEN CONCAT(b.dept, '_', b.year, '_', COALESCE(b.team, ''), '_', COALESCE(b.name, ''))
              WHEN LOWER(COALESCE(rr.reco_name, '')) LIKE '%"unknown"%'
                   OR LOWER(COALESCE(rr.reco_name, '')) = 'unknown' THEN 'unknown'
              ELSE ''
            END AS matched_person
        FROM img_upload iu
        LEFT JOIN reco_result rr
          ON (
            (rr.photo_uuid IS NOT NULL AND rr.photo_uuid = iu.photo_uuid)
            OR rr.origin_full_path = iu.origin_full_path
          )
          AND COALESCE(rr.is_deleted, 0) = 0
        LEFT JOIN base b
          ON JSON_SEARCH(
            COALESCE(rr.reco_name, '[]'),
            'one',
            CONCAT(b.dept, '_', b.year, '_', COALESCE(b.team, ''), '_', COALESCE(b.name, ''))
          ) IS NOT NULL
        {where_clause}
        ORDER BY COALESCE(rr.create_time, iu.create_time) DESC, iu.id DESC
        """

        query_started = time.perf_counter()
        cursor.execute(sql, tuple(params))
        rows = normalize_record_rows(cursor.fetchall())
        query_elapsed_ms = int((time.perf_counter() - query_started) * 1000)

        if det_score_min is not None or det_score_max is not None:
            filtered_rows = []
            for row in rows:
                reco_res = row.get("reco_res")
                if not isinstance(reco_res, list):
                    continue
                det_scores = []
                for entry in reco_res:
                    if not isinstance(entry, dict):
                        continue
                    value = entry.get("det_score")
                    if value is None:
                        continue
                    try:
                        det_scores.append(float(value))
                    except (TypeError, ValueError):
                        continue
                if not det_scores:
                    continue
                if det_score_min is not None and not any(score >= det_score_min for score in det_scores):
                    continue
                if det_score_max is not None and not any(score <= det_score_max for score in det_scores):
                    continue
                filtered_rows.append(row)
            rows = filtered_rows

        total = len(rows)
        rows = rows[offset: offset + limit]

        img_upload_ids = [int(row.get("img_upload_id")) for row in rows if row.get("img_upload_id")]
        award_tags_map: dict[int, list[dict]] = {}
        if img_upload_ids:
            placeholders = ", ".join(["%s"] * len(img_upload_ids))
            cursor.execute(
                f"""
                SELECT
                    iuat.img_upload_id,
                    aam.id AS award_id,
                    COALESCE(aam.serial_no, 0) AS serial_no,
                    COALESCE(aam.award_category, '') AS award_category,
                    COALESCE(aam.activity_item, '') AS activity_item,
                    COALESCE(aam.mapped_award, '') AS mapped_award,
                    COALESCE(aam.award_name, '') AS award_name
                FROM img_upload_award_tag iuat
                INNER JOIN activity_award_master aam ON aam.id = iuat.award_id
                WHERE iuat.img_upload_id IN ({placeholders})
                ORDER BY COALESCE(aam.serial_no, 0) ASC, aam.id ASC
                """,
                tuple(img_upload_ids),
            )
            for tag_row in normalize_record_rows(cursor.fetchall() or []):
                upload_id = int(tag_row.get("img_upload_id") or 0)
                if upload_id <= 0:
                    continue
                award_tags_map.setdefault(upload_id, []).append(
                    {
                        "award_id": int(tag_row.get("award_id") or 0),
                        "serial_no": int(tag_row.get("serial_no") or 0),
                        "award_category": tag_row.get("award_category") or "",
                        "activity_item": tag_row.get("activity_item") or "",
                        "mapped_award": tag_row.get("mapped_award") or "",
                        "award_name": tag_row.get("award_name") or "",
                    }
                )
        for row in rows:
            upload_id = int(row.get("img_upload_id") or 0)
            row["is_video_pick"] = int(row.get("is_video_pick") or 0)
            row["award_tags"] = award_tags_map.get(upload_id, [])

        logger.info(
            "query-ui-advanced detail timings query_ms=%s items=%s total=%s year=%s years=%s",
            query_elapsed_ms,
            len(rows),
            total,
            year,
            years,
        )
        return {
            "total": total,
            "page": page,
            "page_size": limit,
            "total_pages": max((total + limit - 1) // limit, 1),
            "result_mode": normalized_mode,
            "items": rows,
        }
    finally:
        cursor.close()
        db.close()


def query_advanced_preview_count(
    dept: list[str] | None = None,
    year: int | None = None,
    years: list[int] | None = None,
    team: list[str] | None = None,
    name: str = "",
    start_time: str = "",
    end_time: str = "",
    taken_start_time: str = "",
    taken_end_time: str = "",
    det_score_min: float | None = None,
    det_score_max: float | None = None,
    reco_count: int | None = None,
    activity_schedule_id: int | None = None,
    recognition_status: str = "",
    mark_type: str = "",
    award_ids: list[int] | None = None,
    result_mode: str = "photo",
) -> int:
    ensure_activity_tables()
    ensure_recognition_soft_delete_schema()
    ensure_photo_mark_schema()
    ensure_photo_query_indexes()
    db, cursor = get_db_cursor()
    try:
        conditions, params = build_advanced_conditions(
            dept=dept,
            year=year,
            years=years,
            team=team,
            name=name,
            start_time=start_time,
            end_time=end_time,
            taken_start_time=taken_start_time,
            taken_end_time=taken_end_time,
            det_score_min=det_score_min,
            det_score_max=det_score_max,
            reco_count=reco_count,
            activity_schedule_id=activity_schedule_id,
            recognition_status=recognition_status,
            mark_type=mark_type,
            award_ids=award_ids,
        )
        where_clause = ""
        if conditions:
            where_clause = "WHERE " + " AND ".join(conditions)
        base_where = "COALESCE(iu.is_deleted, 0) = 0"
        if where_clause:
            where_clause = where_clause + " AND " + base_where
        else:
            where_clause = "WHERE " + base_where

        normalized_mode = str(result_mode or "photo").strip().lower()
        if normalized_mode == "detail":
            sql = f"""
            SELECT COUNT(*) AS total_count
            FROM img_upload iu
            LEFT JOIN reco_result rr
              ON (
                (rr.photo_uuid IS NOT NULL AND rr.photo_uuid = iu.photo_uuid)
                OR rr.origin_full_path = iu.origin_full_path
              )
              AND COALESCE(rr.is_deleted, 0) = 0
            LEFT JOIN base b
              ON JSON_SEARCH(
                COALESCE(rr.reco_name, '[]'),
                'one',
                CONCAT(b.dept, '_', b.year, '_', COALESCE(b.team, ''), '_', COALESCE(b.name, ''))
              ) IS NOT NULL
            {where_clause}
            """
        else:
            def _build_photo_path_subquery(join_key: str) -> str:
                join_expr = _build_photo_join_expr(join_key)
                return f"""
                SELECT
                    iu.id AS img_upload_id
                FROM img_upload iu
                LEFT JOIN reco_result rr
                  ON ({join_expr})
                  AND COALESCE(rr.is_deleted, 0) = 0
                {where_clause}
                GROUP BY iu.id
                """

            photo_uuid_path_sql = _build_photo_path_subquery("photo_uuid")
            origin_path_sql = _build_photo_path_subquery("origin_full_path")
            sql = f"""
            SELECT COUNT(*) AS total_count
            FROM (
                SELECT img_upload_id
                FROM (
                    {photo_uuid_path_sql}
                    UNION ALL
                    {origin_path_sql}
                ) photo_matches
                GROUP BY img_upload_id
            ) counted
            """
        cursor.execute(sql, tuple(params + params) if normalized_mode != "detail" else tuple(params))
        row = cursor.fetchone() or {}
        return int(row.get("total_count") or 0)
    finally:
        cursor.close()
        db.close()


def build_name_score_pairs(reco_names, reco_res):
    names = reco_names if isinstance(reco_names, list) else []
    scores = reco_res if isinstance(reco_res, list) else []
    pairs = []
    for idx, raw_name in enumerate(names):
        name_text = str(raw_name or "").strip()
        if not name_text:
            continue
        det_text = "None"
        entry = scores[idx] if idx < len(scores) and isinstance(scores[idx], dict) else None
        if entry is not None:
            with contextlib.suppress(Exception):
                det_text = f"{float(entry.get('det_score')):.4f}"
        pairs.append({"name": name_text, "det_score": det_text})
    return pairs


class LogicalDeleteRequest(BaseModel):
    reco_ids: list[int] = Field(default_factory=list, description="要邏輯刪除的 reco_result.id 清單")
    img_upload_ids: list[int] = Field(default_factory=list, description="要邏輯刪除的 img_upload.id 清單")
    reason: str = Field(default="使用者於查詢頁執行邏輯刪除", max_length=255)


class RecognitionAwardMarkRequest(BaseModel):
    img_upload_ids: list[int] = Field(default_factory=list, description="要標記的 img_upload.id 清單")
    award_ids: list[int] = Field(default_factory=list, description="活動獎項 id 清單")


class RecognitionVideoMarkRequest(BaseModel):
    img_upload_ids: list[int] = Field(default_factory=list, description="要標記的 img_upload.id 清單")
    is_video_pick: int = Field(default=1, description="1=標記影片, 0=取消影片標記")


class ExportSelectedRequest(BaseModel):
    img_upload_ids: list[int] = Field(default_factory=list, description="勾選的 img_upload.id 清單")
    delivery_mode: str = Field(default="zip", description="zip/folder/both")
    photo_variant: str = Field(default="both", description="both/origin/thumb")


class RecognitionPreviewCountRequest(BaseModel):
    dept: list[str] = Field(default_factory=list)
    year: int | None = None
    years: list[int] = Field(default_factory=list)
    team: list[str] = Field(default_factory=list)
    name: str = ""
    start_time: str = ""
    end_time: str = ""
    taken_start_time: str = ""
    taken_end_time: str = ""
    det_score_min: float | None = None
    det_score_max: float | None = None
    reco_count: int | None = None
    activity_schedule_id: int | None = None
    recognition_status: str = ""
    mark_type: str = ""
    award_ids: list[int] = Field(default_factory=list)
    result_mode: str = "photo"


def _prune_export_tokens():
    now = _now_utc()
    expired = []
    for token, payload in EXPORT_TOKEN_STORE.items():
        expires_at = payload.get("expires_at")
        if isinstance(expires_at, datetime) and now >= expires_at:
            expired.append(token)
    for token in expired:
        EXPORT_TOKEN_STORE.pop(token, None)


def _ensure_export_allowed(path_value: str):
    resolved = resolve_preview_path(path_value)
    normalized = resolved.replace("\\", "/").lower()
    if not (normalized.startswith("/mnt/activity/dev/origin/") or normalized.startswith("/mnt/activity/dev/thumbs/")):
        raise HTTPException(status_code=403, detail="僅允許匯出活動原圖或縮圖目錄內的檔案。")
    return Path(resolved)


def _safe_folder_part(value: str, fallback: str = "unknown") -> str:
    text = str(value or "").strip()
    if not text:
        return fallback
    cleaned = "".join("_" if ch in '<>:"/\\|?*' else ch for ch in text)
    cleaned = cleaned.replace("\n", "_").replace("\r", "_").strip(" .")
    return cleaned or fallback


def _build_unique_target_path(folder: Path, filename: str) -> tuple[Path, bool]:
    folder.mkdir(parents=True, exist_ok=True)
    candidate = folder / filename
    if not candidate.exists():
        return candidate, False
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    index = 1
    while True:
        next_name = f"{stem}_{index}{suffix}"
        next_path = folder / next_name
        if not next_path.exists():
            return next_path, True
        index += 1


def _to_naive_datetime(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    text = str(value).strip()
    if not text:
        return None
    formats = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d")
    for fmt in formats:
        with contextlib.suppress(ValueError):
            return datetime.strptime(text, fmt)
    return None


def _build_activity_ranges(cursor):
    cursor.execute(
        """
        SELECT id, activity_date, COALESCE(activity_time, '') AS activity_time, COALESCE(activity_content, '') AS activity_content
        FROM activity_schedule
        ORDER BY activity_date ASC, activity_time ASC, id ASC
        """
    )
    rows = cursor.fetchall() or []
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        day = str(row.get("activity_date") or "").strip()
        if not day:
            continue
        grouped.setdefault(day, []).append(row)

    ranges = []
    for day, items in grouped.items():
        for idx, item in enumerate(items):
            start_time = str(item.get("activity_time") or "00:00:00").strip() or "00:00:00"
            end_time = "23:59:59"
            for next_idx in range(idx + 1, len(items)):
                candidate = str(items[next_idx].get("activity_time") or "").strip()
                if candidate:
                    end_time = candidate
                    break
            with contextlib.suppress(Exception):
                start_dt = datetime.strptime(f"{day} {start_time}", "%Y-%m-%d %H:%M:%S")
                end_dt = datetime.strptime(f"{day} {end_time}", "%Y-%m-%d %H:%M:%S")
                if end_dt <= start_dt:
                    end_dt = start_dt + timedelta(hours=1)
                ranges.append(
                    {
                        "start": start_dt,
                        "end": end_dt,
                        "activity_name": str(item.get("activity_content") or "").strip() or "unknown_activity",
                    }
                )
    return ranges


def _resolve_activity_name(record_dt: datetime | None, ranges: list[dict]) -> str:
    if record_dt is None:
        return "unknown_activity"
    for item in ranges:
        if item["start"] <= record_dt <= item["end"]:
            return _safe_folder_part(item.get("activity_name") or "", "unknown_activity")
    return "unknown_activity"


def _export_selected_photos(
    img_upload_ids: list[int],
    delivery_mode: str,
    photo_variant: str,
):
    valid_delivery = {"zip", "folder", "both"}
    valid_variant = {"both", "origin", "thumb"}
    selected_delivery = (delivery_mode or "zip").strip().lower()
    selected_variant = (photo_variant or "both").strip().lower()
    if selected_delivery not in valid_delivery:
        raise HTTPException(status_code=400, detail="delivery_mode 只允許 zip/folder/both。")
    if selected_variant not in valid_variant:
        raise HTTPException(status_code=400, detail="photo_variant 只允許 both/origin/thumb。")

    unique_ids = sorted({int(item) for item in (img_upload_ids or []) if int(item) > 0})
    if not unique_ids:
        raise HTTPException(status_code=400, detail="請先勾選要匯出的照片。")

    ensure_activity_tables()
    ensure_recognition_soft_delete_schema()

    db, cursor = get_db_cursor()
    try:
        placeholders = ", ".join(["%s"] * len(unique_ids))
        cursor.execute(
            f"""
            SELECT
                id,
                COALESCE(photo_uuid, '') AS photo_uuid,
                COALESCE(origin_full_path, '') AS origin_full_path,
                COALESCE(thumbs_full_path, '') AS thumbs_full_path,
                COALESCE(photo_taken_time, NULL) AS photo_taken_time,
                COALESCE(photo_file_time, NULL) AS photo_file_time,
                COALESCE(is_video_pick, 0) AS is_video_pick,
                COALESCE(is_deleted, 0) AS is_deleted
            FROM img_upload
            WHERE id IN ({placeholders})
            ORDER BY id ASC
            """,
            tuple(unique_ids),
        )
        rows = cursor.fetchall() or []

        cursor.execute(
            f"""
            SELECT
                iuat.img_upload_id,
                aam.id AS award_id,
                COALESCE(aam.serial_no, 0) AS award_serial_no,
                COALESCE(aam.award_name, '') AS award_name
            FROM img_upload_award_tag iuat
            INNER JOIN activity_award_master aam ON aam.id = iuat.award_id
            WHERE iuat.img_upload_id IN ({placeholders})
            ORDER BY COALESCE(aam.serial_no, 0) ASC, aam.id ASC
            """,
            tuple(unique_ids),
        )
        award_rows = cursor.fetchall() or []
        activity_ranges = _build_activity_ranges(cursor)
    finally:
        cursor.close()
        db.close()

    row_map = {int(row["id"]): row for row in rows if row.get("id") is not None}
    award_map: dict[int, list[dict]] = {}
    for row in award_rows:
        upload_id = int(row.get("img_upload_id") or 0)
        if upload_id <= 0:
            continue
        award_map.setdefault(upload_id, []).append(
            {
                "award_id": int(row.get("award_id") or 0),
                "award_serial_no": int(row.get("award_serial_no") or 0),
                "award_name": str(row.get("award_name") or ""),
            }
        )
    job_id = f"exp_{_now_tpe().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:6]}"
    export_dir = EXPORT_ROOT_RUNTIME / job_id
    export_dir.mkdir(parents=True, exist_ok=True)

    manifest_rows = []
    copied_files = []
    success_count = 0
    skipped_count = 0
    failed_count = 0
    collision_count = 0

    for upload_id in unique_ids:
        row = row_map.get(upload_id)
        if not row:
            skipped_count += 1
            manifest_rows.append({
                "img_upload_id": upload_id, "photo_uuid": "", "original_filename": "", "final_filename": "",
                "collision_resolved": False, "activity_name": "unknown_activity", "tag_type": "",
                "award_id": "", "award_serial_no": "", "award_name": "",
                "origin_path": "", "thumb_path": "", "selected_variant": selected_variant,
                "export_status": "skipped", "reason": "not_found",
            })
            continue
        if int(row.get("is_deleted") or 0) == 1:
            skipped_count += 1
            manifest_rows.append({
                "img_upload_id": upload_id, "photo_uuid": row.get("photo_uuid", ""), "original_filename": "", "final_filename": "",
                "collision_resolved": False, "activity_name": "unknown_activity", "tag_type": "",
                "award_id": "", "award_serial_no": "", "award_name": "",
                "origin_path": row.get("origin_full_path", ""), "thumb_path": row.get("thumbs_full_path", ""),
                "selected_variant": selected_variant, "export_status": "skipped", "reason": "deleted",
            })
            continue

        targets = []
        if selected_variant in {"both", "origin"}:
            targets.append(("origin", row.get("origin_full_path", "")))
        if selected_variant in {"both", "thumb"}:
            targets.append(("thumb", row.get("thumbs_full_path", "")))

        copied_any = False
        reasons = []
        tags = []
        upload_awards = award_map.get(upload_id, [])
        if upload_awards:
            tags.extend([("award", award) for award in upload_awards])
        if int(row.get("is_video_pick") or 0) == 1:
            tags.append(("video", None))
        if not tags:
            tags.append(("untagged", None))

        record_dt = _to_naive_datetime(row.get("photo_taken_time")) or _to_naive_datetime(row.get("photo_file_time"))
        activity_name = _resolve_activity_name(record_dt, activity_ranges)

        for kind, path_value in targets:
            if not path_value:
                reasons.append(f"{kind}_missing_path")
                continue
            try:
                source_path = _ensure_export_allowed(path_value)
            except HTTPException as ex:
                reasons.append(f"{kind}_forbidden:{ex.detail}")
                continue
            except Exception:
                reasons.append(f"{kind}_resolve_failed")
                continue

            if not source_path.exists():
                reasons.append(f"{kind}_missing_file")
                continue

            original_filename = source_path.name
            for tag_type, award in tags:
                if tag_type == "award":
                    serial = int((award or {}).get("award_serial_no") or 0)
                    award_name = _safe_folder_part((award or {}).get("award_name") or "", "award")
                    award_folder = f"{serial}_{award_name}" if serial > 0 else award_name
                    target_folder = export_dir / activity_name / "award" / award_folder / kind
                    manifest_tag_type = "award"
                elif tag_type == "video":
                    target_folder = export_dir / activity_name / "video" / kind
                    manifest_tag_type = "video"
                else:
                    target_folder = export_dir / activity_name / "untagged" / kind
                    manifest_tag_type = "untagged"

                target_path, collision = _build_unique_target_path(target_folder, original_filename)
                try:
                    shutil.copy2(source_path, target_path)
                    copied_any = True
                    copied_files.append(target_path)
                    if collision:
                        collision_count += 1
                    manifest_rows.append({
                        "img_upload_id": upload_id,
                        "photo_uuid": row.get("photo_uuid", ""),
                        "original_filename": original_filename,
                        "final_filename": target_path.name,
                        "collision_resolved": collision,
                        "activity_name": activity_name,
                        "tag_type": manifest_tag_type,
                        "award_id": int((award or {}).get("award_id") or 0) if tag_type == "award" else "",
                        "award_serial_no": int((award or {}).get("award_serial_no") or 0) if tag_type == "award" else "",
                        "award_name": (award or {}).get("award_name", "") if tag_type == "award" else "",
                        "origin_path": row.get("origin_full_path", ""),
                        "thumb_path": row.get("thumbs_full_path", ""),
                        "selected_variant": kind,
                        "export_status": "success",
                        "reason": "",
                    })
                except Exception:
                    reasons.append(f"{kind}_copy_failed")

        if copied_any:
            success_count += 1
        else:
            failed_count += 1
            reason = ";".join(reasons) if reasons else "no_target"
            manifest_rows.append({
                "img_upload_id": upload_id,
                "photo_uuid": row.get("photo_uuid", ""),
                "original_filename": "",
                "final_filename": "",
                "collision_resolved": False,
                "activity_name": activity_name,
                "tag_type": ",".join(sorted({tag for tag, _ in tags if tag in {"award", "video"}})) or "untagged",
                "award_id": "",
                "award_serial_no": "",
                "award_name": "",
                "origin_path": row.get("origin_full_path", ""),
                "thumb_path": row.get("thumbs_full_path", ""),
                "selected_variant": selected_variant,
                "export_status": "failed",
                "reason": reason,
            })

    manifest_path = export_dir / "manifest.csv"
    with manifest_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "img_upload_id", "photo_uuid", "original_filename", "final_filename", "collision_resolved",
                "activity_name", "tag_type", "award_id", "award_serial_no", "award_name",
                "origin_path", "thumb_path", "selected_variant", "export_status", "reason",
            ],
        )
        writer.writeheader()
        writer.writerows(manifest_rows)

    summary = {
        "job_id": job_id,
        "delivery_mode": selected_delivery,
        "photo_variant": selected_variant,
        "requested_count": len(unique_ids),
        "success_count": success_count,
        "skipped_count": skipped_count,
        "failed_count": failed_count,
        "collision_count": collision_count,
        "manifest_path": str(manifest_path),
        "export_folder": str(export_dir),
        "export_folder_host": runtime_path_to_windows(str(export_dir)),
        "created_at": _now_tpe().strftime("%Y-%m-%d %H:%M:%S"),
    }
    summary_path = export_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    zip_path = None
    download_token = ""
    if selected_delivery in {"zip", "both"}:
        zip_path = EXPORT_ROOT_RUNTIME / f"{job_id}.zip"
        with zipfile.ZipFile(zip_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            for file_path in copied_files:
                arcname = str(file_path.relative_to(export_dir)).replace("\\", "/")
                zf.write(file_path, arcname=arcname)
            zf.write(manifest_path, arcname="manifest.csv")
            zf.write(summary_path, arcname="summary.json")
        _prune_export_tokens()
        token = uuid4().hex
        EXPORT_TOKEN_STORE[token] = {"zip_path": str(zip_path), "job_id": job_id, "expires_at": _now_utc() + timedelta(seconds=EXPORT_TOKEN_TTL_SECONDS)}
        download_token = token

    response = {
        **summary,
        "download_token": download_token,
        "download_url": f"/query-recognitions-advanced/export-download/{download_token}" if download_token else "",
        "zip_path": str(zip_path) if zip_path else "",
        "zip_path_host": runtime_path_to_windows(str(zip_path)) if zip_path else "",
    }
    if selected_delivery == "zip":
        response["export_folder"] = ""
        response["export_folder_host"] = ""
    return response


def _collect_delete_filter_payload(payload: dict):
    raw_dept = payload.get("dept", [])
    raw_team = payload.get("team", [])
    dept = [str(item).strip() for item in (raw_dept if isinstance(raw_dept, list) else [raw_dept]) if str(item).strip()]
    team = [str(item).strip() for item in (raw_team if isinstance(raw_team, list) else [raw_team]) if str(item).strip()]

    year = None
    raw_year = payload.get("year", None)
    if str(raw_year or "").strip():
        year = int(raw_year)

    reco_count = None
    raw_reco_count = payload.get("reco_count", None)
    if str(raw_reco_count or "").strip():
        reco_count = int(raw_reco_count)

    activity_schedule_id = None
    raw_schedule_id = payload.get("activity_schedule_id", None)
    if str(raw_schedule_id or "").strip():
        activity_schedule_id = int(raw_schedule_id)

    raw_award_ids = payload.get("award_ids", [])
    if isinstance(raw_award_ids, str):
        raw_award_ids = [raw_award_ids]
    award_ids: list[int] = []
    for item in (raw_award_ids or []):
        text = str(item or "").strip()
        if text.isdigit():
            award_ids.append(int(text))

    det_score_min = None
    raw_det_min = payload.get("det_score_min", None)
    if str(raw_det_min or "").strip():
        det_score_min = float(raw_det_min)

    det_score_max = None
    raw_det_max = payload.get("det_score_max", None)
    if str(raw_det_max or "").strip():
        det_score_max = float(raw_det_max)

    filters = {
        "dept": dept,
        "year": year,
        "years": sorted({int(item) for item in (payload.get("years", []) or []) if str(item).strip().isdigit()}),
        "team": team,
        "name": str(payload.get("name", "") or "").strip(),
        "start_time": str(payload.get("start_time", "") or "").strip(),
        "end_time": str(payload.get("end_time", "") or "").strip(),
        "taken_start_time": str(payload.get("taken_start_time", "") or "").strip(),
        "taken_end_time": str(payload.get("taken_end_time", "") or "").strip(),
        "det_score_min": det_score_min,
        "det_score_max": det_score_max,
        "reco_count": reco_count,
        "recognition_status": str(payload.get("recognition_status", "") or "").strip(),
        "activity_schedule_id": activity_schedule_id,
        "mark_type": str(payload.get("mark_type", "") or "").strip(),
        "award_ids": award_ids,
    }
    has_filter = any(
        [
            len(filters["dept"]) > 0,
            filters["year"] is not None,
            len(filters["years"]) > 0,
            len(filters["team"]) > 0,
            bool(filters["name"]),
            bool(filters["start_time"]),
            bool(filters["end_time"]),
            bool(filters["taken_start_time"]),
            bool(filters["taken_end_time"]),
            filters["det_score_min"] is not None,
            filters["det_score_max"] is not None,
            filters["reco_count"] is not None,
            bool(filters["recognition_status"]),
            filters["activity_schedule_id"] is not None,
            bool(filters["mark_type"]),
            len(filters["award_ids"]) > 0,
        ]
    )
    return filters, has_filter


def _query_logical_delete_targets_by_filter(cursor, filters: dict):
    conditions, params = build_advanced_conditions(
        dept=filters["dept"],
        year=filters["year"],
        years=filters["years"],
        team=filters["team"],
        name=filters["name"],
        start_time=filters["start_time"],
        end_time=filters["end_time"],
        taken_start_time=filters["taken_start_time"],
        taken_end_time=filters["taken_end_time"],
        det_score_min=None,
        det_score_max=None,
        reco_count=filters["reco_count"],
        activity_schedule_id=filters["activity_schedule_id"],
        recognition_status=filters["recognition_status"],
        mark_type=filters.get("mark_type", ""),
        award_ids=filters.get("award_ids", []),
    )

    where_clause = ""
    if conditions:
        where_clause = "WHERE " + " AND ".join(conditions)
    base_where = "COALESCE(iu.is_deleted, 0) = 0"
    if where_clause:
        where_clause = where_clause + " AND " + base_where
    else:
        where_clause = "WHERE " + base_where

    cursor.execute(
        f"""
        SELECT
            rr.id AS reco_result_id,
            iu.id AS img_upload_id,
            COALESCE(iu.origin_full_path, rr.origin_full_path, '') AS origin_full_path,
            COALESCE(iu.photo_uuid, rr.photo_uuid, '') AS photo_uuid,
            COALESCE(iu.reco_status, 'PENDING') AS source_status,
            rr.reco_res
        FROM img_upload iu
        LEFT JOIN reco_result rr
          ON (
            (rr.photo_uuid IS NOT NULL AND rr.photo_uuid = iu.photo_uuid)
            OR rr.origin_full_path = iu.origin_full_path
          )
          AND COALESCE(rr.is_deleted, 0) = 0
        LEFT JOIN base b
          ON JSON_SEARCH(
            COALESCE(rr.reco_name, '[]'),
            'one',
            CONCAT(b.dept, '_', b.year, '_', COALESCE(b.team, ''), '_', COALESCE(b.name, ''))
          ) IS NOT NULL
        {where_clause}
        """,
        tuple(params),
    )
    rows = normalize_record_rows(cursor.fetchall() or [])

    det_min = filters.get("det_score_min")
    det_max = filters.get("det_score_max")
    if det_min is not None or det_max is not None:
        filtered = []
        for row in rows:
            reco_res = row.get("reco_res")
            if not isinstance(reco_res, list):
                continue
            det_scores = []
            for entry in reco_res:
                if not isinstance(entry, dict):
                    continue
                value = entry.get("det_score")
                if value is None:
                    continue
                try:
                    det_scores.append(float(value))
                except (TypeError, ValueError):
                    continue
            if not det_scores:
                continue
            if det_min is not None and not any(score >= det_min for score in det_scores):
                continue
            if det_max is not None and not any(score <= det_max for score in det_scores):
                continue
            filtered.append(row)
        rows = filtered

    dedup = {}
    for row in rows:
        key = (
            int(row["img_upload_id"]) if row.get("img_upload_id") else 0,
            int(row["reco_result_id"]) if row.get("reco_result_id") else 0,
        )
        dedup[key] = row
    return list(dedup.values())


def _apply_logical_delete_rows(cursor, rows: list[dict], reason: str, deleted_by: str):
    target_reco_ids = sorted({int(row["reco_result_id"]) for row in rows if row.get("reco_result_id")})
    target_img_upload_ids = sorted({int(row["img_upload_id"]) for row in rows if row.get("img_upload_id")})

    deleted_reco_count = 0
    if target_reco_ids:
        target_placeholders = ", ".join(["%s"] * len(target_reco_ids))
        cursor.execute(
            f"UPDATE reco_result SET is_deleted = 1, update_time = NOW() WHERE id IN ({target_placeholders})",
            tuple(target_reco_ids),
        )
        deleted_reco_count = cursor.rowcount or 0

    deleted_upload_count = 0
    if target_img_upload_ids:
        target_placeholders = ", ".join(["%s"] * len(target_img_upload_ids))
        cursor.execute(
            f"UPDATE img_upload SET is_deleted = 1, update_time = NOW() WHERE id IN ({target_placeholders})",
            tuple(target_img_upload_ids),
        )
        deleted_upload_count = cursor.rowcount or 0

    log_rows = [
        (
            int(row["reco_result_id"]) if row.get("reco_result_id") else None,
            int(row["img_upload_id"]) if row.get("img_upload_id") else None,
            str(row.get("origin_full_path") or ""),
            str(row.get("photo_uuid") or ""),
            str(row.get("source_status") or "")[:16],
            (reason or "使用者於查詢頁執行邏輯刪除")[:255],
            deleted_by,
        )
        for row in rows
    ]
    if log_rows:
        cursor.executemany(
            """
            INSERT INTO reco_delete_log (
                reco_result_id, img_upload_id, origin_full_path, photo_uuid, source_status, deleted_reason, deleted_by
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            log_rows,
        )
    return deleted_reco_count, deleted_upload_count


@app.post("/query-recognitions-advanced/logical-delete")
async def logical_delete_recognitions(request: LogicalDeleteRequest):
    ensure_activity_tables()
    ensure_recognition_soft_delete_schema()
    reco_ids = sorted({int(item) for item in (request.reco_ids or []) if int(item) > 0})
    img_upload_ids = sorted({int(item) for item in (request.img_upload_ids or []) if int(item) > 0})
    if not reco_ids and not img_upload_ids:
        return JSONResponse(status_code=400, content={"detail": "請先勾選要刪除的資料。"})

    db, cursor = get_db_cursor()
    try:
        selected_rows = []
        if reco_ids:
            placeholders = ", ".join(["%s"] * len(reco_ids))
            cursor.execute(
                f"""
                SELECT
                    rr.id AS reco_result_id,
                    iu.id AS img_upload_id,
                    COALESCE(iu.origin_full_path, rr.origin_full_path, '') AS origin_full_path,
                    COALESCE(iu.photo_uuid, rr.photo_uuid, '') AS photo_uuid,
                    COALESCE(iu.reco_status, 'PENDING') AS source_status
                FROM reco_result rr
                LEFT JOIN img_upload iu
                  ON (iu.photo_uuid IS NOT NULL AND iu.photo_uuid <> '' AND iu.photo_uuid = rr.photo_uuid)
                  OR (iu.origin_full_path = rr.origin_full_path)
                WHERE rr.id IN ({placeholders}) AND COALESCE(rr.is_deleted, 0) = 0
                """,
                tuple(reco_ids),
            )
            selected_rows.extend(cursor.fetchall() or [])

        if img_upload_ids:
            placeholders = ", ".join(["%s"] * len(img_upload_ids))
            cursor.execute(
                f"""
                SELECT
                    rr.id AS reco_result_id,
                    iu.id AS img_upload_id,
                    COALESCE(iu.origin_full_path, rr.origin_full_path, '') AS origin_full_path,
                    COALESCE(iu.photo_uuid, rr.photo_uuid, '') AS photo_uuid,
                    COALESCE(iu.reco_status, 'PENDING') AS source_status
                FROM img_upload iu
                LEFT JOIN reco_result rr
                  ON (iu.photo_uuid IS NOT NULL AND iu.photo_uuid <> '' AND rr.photo_uuid = iu.photo_uuid)
                  OR (rr.origin_full_path = iu.origin_full_path)
                WHERE iu.id IN ({placeholders}) AND COALESCE(iu.is_deleted, 0) = 0
                """,
                tuple(img_upload_ids),
            )
            selected_rows.extend(cursor.fetchall() or [])

        dedup = {}
        for row in selected_rows:
            key = (int(row["img_upload_id"]) if row.get("img_upload_id") else 0, int(row["reco_result_id"]) if row.get("reco_result_id") else 0)
            dedup[key] = row
        rows = list(dedup.values())
        if not rows:
            requested_count = len(reco_ids) + len(img_upload_ids)
            return {"deleted_count": 0, "deleted_img_upload_count": 0, "requested_count": requested_count, "message": "沒有可刪除資料（可能已刪除）。"}

        deleted_reco_count, deleted_upload_count = _apply_logical_delete_rows(
            cursor=cursor,
            rows=rows,
            reason=request.reason or "使用者於查詢頁執行邏輯刪除",
            deleted_by="query_ui_page",
        )
        db.conn.commit()
        return {
            "deleted_count": deleted_reco_count,
            "deleted_img_upload_count": deleted_upload_count,
            "requested_count": len(reco_ids) + len(img_upload_ids),
        }
    except Exception as e:
        db.conn.rollback()
        logger.error(f"邏輯刪除辨識資料失敗: {str(e)}")
        return JSONResponse(status_code=500, content={"detail": f"邏輯刪除失敗：{str(e)}"})
    finally:
        cursor.close()
        db.close()


@app.post("/query-recognitions-advanced/logical-delete-by-filter/preview")
async def logical_delete_by_filter_preview(request: Request):
    ensure_activity_tables()
    ensure_recognition_soft_delete_schema()
    payload = await request.json()
    filters, has_filter = _collect_delete_filter_payload(payload if isinstance(payload, dict) else {})
    if not has_filter:
        return JSONResponse(status_code=400, content={"detail": "請至少設定一個查詢條件。"})

    db, cursor = get_db_cursor()
    try:
        rows = _query_logical_delete_targets_by_filter(cursor, filters)
        matched_count = len(rows)
        matched_reco_count = len({int(row["reco_result_id"]) for row in rows if row.get("reco_result_id")})
        matched_upload_count = len({int(row["img_upload_id"]) for row in rows if row.get("img_upload_id")})
        return {
            "matched_count": matched_count,
            "matched_reco_count": matched_reco_count,
            "matched_img_upload_count": matched_upload_count,
        }
    except Exception as e:
        logger.error(f"預覽跨頁邏輯刪除失敗: {str(e)}")
        return JSONResponse(status_code=500, content={"detail": f"預覽跨頁邏輯刪除失敗：{str(e)}"})
    finally:
        cursor.close()
        db.close()


@app.post("/query-recognitions-advanced/logical-delete-by-filter")
async def logical_delete_by_filter(request: Request):
    ensure_activity_tables()
    ensure_recognition_soft_delete_schema()
    payload = await request.json()
    payload_dict = payload if isinstance(payload, dict) else {}
    filters, has_filter = _collect_delete_filter_payload(payload_dict)
    if not has_filter:
        return JSONResponse(status_code=400, content={"detail": "請至少設定一個查詢條件。"})
    reason = str(payload_dict.get("reason") or "使用者於查詢頁執行跨頁邏輯刪除")[:255]

    db, cursor = get_db_cursor()
    try:
        rows = _query_logical_delete_targets_by_filter(cursor, filters)
        if not rows:
            return {"deleted_count": 0, "deleted_img_upload_count": 0, "requested_count": 0, "message": "沒有可刪除資料。"}
        deleted_reco_count, deleted_upload_count = _apply_logical_delete_rows(
            cursor=cursor,
            rows=rows,
            reason=reason,
            deleted_by="query_ui_filter_all",
        )
        db.conn.commit()
        return {
            "deleted_count": deleted_reco_count,
            "deleted_img_upload_count": deleted_upload_count,
            "requested_count": len(rows),
        }
    except Exception as e:
        db.conn.rollback()
        logger.error(f"跨頁邏輯刪除失敗: {str(e)}")
        return JSONResponse(status_code=500, content={"detail": f"跨頁邏輯刪除失敗：{str(e)}"})
    finally:
        cursor.close()
        db.close()


@app.get("/activity-awards/options")
async def activity_award_options():
    try:
        ensure_activity_tables()
        ensure_activity_award_table()
        items = query_activity_award_master(keyword="", limit=1000)
        option_items = []
        for item in items:
            option_items.append(
                {
                    "id": int(item.get("id") or 0),
                    "serial_no": int(item.get("serial_no") or 0),
                    "award_category": item.get("award_category") or "",
                    "activity_item": item.get("activity_item") or "",
                    "mapped_award": item.get("mapped_award") or "",
                    "award_name": item.get("award_name") or "",
                    "label": f"{int(item.get('serial_no') or 0)} {item.get('award_category') or ''} {item.get('activity_item') or ''} {item.get('mapped_award') or ''} {item.get('award_name') or ''}".strip(),
                }
            )
        return {"items": option_items}
    except Exception as e:
        logger.error(f"讀取活動獎項選單失敗: {str(e)}")
        return JSONResponse(status_code=500, content={"detail": f"讀取活動獎項選單失敗：{str(e)}"})


@app.post("/query-recognitions-advanced/mark-video")
async def mark_video_for_recognitions(payload: RecognitionVideoMarkRequest):
    ensure_activity_tables()
    ensure_recognition_soft_delete_schema()
    ensure_photo_mark_schema()
    upload_ids = sorted({int(item) for item in (payload.img_upload_ids or []) if int(item) > 0})
    if not upload_ids:
        return JSONResponse(status_code=400, content={"detail": "請至少勾選一筆照片資料。"})
    mark_value = 1 if int(payload.is_video_pick or 0) == 1 else 0
    db, cursor = get_db_cursor()
    try:
        placeholders = ", ".join(["%s"] * len(upload_ids))
        cursor.execute(
            f"""
            UPDATE img_upload
            SET is_video_pick = %s, update_time = NOW()
            WHERE id IN ({placeholders})
              AND COALESCE(is_deleted, 0) = 0
            """,
            tuple([mark_value, *upload_ids]),
        )
        db.conn.commit()
        return {"updated_count": int(cursor.rowcount or 0), "is_video_pick": mark_value}
    except Exception as e:
        db.conn.rollback()
        logger.error(f"標記影片照片失敗: {str(e)}")
        return JSONResponse(status_code=500, content={"detail": f"標記影片照片失敗：{str(e)}"})
    finally:
        cursor.close()
        db.close()


@app.post("/query-recognitions-advanced/mark-award")
async def mark_award_for_recognitions(payload: RecognitionAwardMarkRequest):
    ensure_activity_tables()
    ensure_recognition_soft_delete_schema()
    ensure_photo_mark_schema()
    upload_ids = sorted({int(item) for item in (payload.img_upload_ids or []) if int(item) > 0})
    award_ids = sorted({int(item) for item in (payload.award_ids or []) if int(item) > 0})
    if not upload_ids:
        return JSONResponse(status_code=400, content={"detail": "請至少勾選一筆照片資料。"})
    if not award_ids:
        return JSONResponse(status_code=400, content={"detail": "請至少選擇一個活動獎項。"})
    db, cursor = get_db_cursor()
    try:
        upload_placeholders = ", ".join(["%s"] * len(upload_ids))
        cursor.execute(
            f"SELECT id FROM img_upload WHERE id IN ({upload_placeholders}) AND COALESCE(is_deleted, 0) = 0",
            tuple(upload_ids),
        )
        valid_upload_ids = sorted({int(row["id"]) for row in (cursor.fetchall() or []) if row.get("id")})
        if not valid_upload_ids:
            return JSONResponse(status_code=400, content={"detail": "勾選的照片都已刪除或不存在。"})

        award_placeholders = ", ".join(["%s"] * len(award_ids))
        cursor.execute(f"SELECT id FROM activity_award_master WHERE id IN ({award_placeholders})", tuple(award_ids))
        valid_award_ids = sorted({int(row["id"]) for row in (cursor.fetchall() or []) if row.get("id")})
        if not valid_award_ids:
            return JSONResponse(status_code=400, content={"detail": "選擇的活動獎項不存在。"})

        inserted_count = 0
        for upload_id in valid_upload_ids:
            for award_id in valid_award_ids:
                cursor.execute(
                    """
                    INSERT INTO img_upload_award_tag (img_upload_id, award_id)
                    VALUES (%s, %s)
                    ON DUPLICATE KEY UPDATE update_time = NOW()
                    """,
                    (upload_id, award_id),
                )
                inserted_count += 1
        db.conn.commit()
        return {
            "updated_count": inserted_count,
            "img_upload_count": len(valid_upload_ids),
            "award_count": len(valid_award_ids),
        }
    except Exception as e:
        db.conn.rollback()
        logger.error(f"標記獎項照片失敗: {str(e)}")
        return JSONResponse(status_code=500, content={"detail": f"標記獎項照片失敗：{str(e)}"})
    finally:
        cursor.close()
        db.close()


@app.post("/query-recognitions-advanced/unmark-award")
async def unmark_award_for_recognitions(payload: RecognitionAwardMarkRequest):
    ensure_activity_tables()
    ensure_recognition_soft_delete_schema()
    ensure_photo_mark_schema()
    upload_ids = sorted({int(item) for item in (payload.img_upload_ids or []) if int(item) > 0})
    if not upload_ids:
        return JSONResponse(status_code=400, content={"detail": "請至少勾選一筆照片資料。"})
    award_ids = sorted({int(item) for item in (payload.award_ids or []) if int(item) > 0})
    db, cursor = get_db_cursor()
    try:
        upload_placeholders = ", ".join(["%s"] * len(upload_ids))
        if award_ids:
            award_placeholders = ", ".join(["%s"] * len(award_ids))
            cursor.execute(
                f"""
                DELETE FROM img_upload_award_tag
                WHERE img_upload_id IN ({upload_placeholders})
                  AND award_id IN ({award_placeholders})
                """,
                tuple([*upload_ids, *award_ids]),
            )
        else:
            cursor.execute(
                f"DELETE FROM img_upload_award_tag WHERE img_upload_id IN ({upload_placeholders})",
                tuple(upload_ids),
            )
        deleted_count = int(cursor.rowcount or 0)
        db.conn.commit()
        return {"deleted_count": deleted_count}
    except Exception as e:
        db.conn.rollback()
        logger.error(f"移除獎項標記失敗: {str(e)}")
        return JSONResponse(status_code=500, content={"detail": f"移除獎項標記失敗：{str(e)}"})
    finally:
        cursor.close()
        db.close()


@app.post("/query-recognitions-advanced/export-selected")
async def export_selected_recognitions(payload: ExportSelectedRequest):
    try:
        return _export_selected_photos(
            img_upload_ids=payload.img_upload_ids,
            delivery_mode=payload.delivery_mode,
            photo_variant=payload.photo_variant,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"匯出勾選照片失敗: {str(e)}")
        return JSONResponse(status_code=500, content={"detail": f"匯出勾選照片失敗：{str(e)}"})


@app.get("/query-recognitions-advanced/export-download/{token}")
async def export_selected_download(token: str):
    _prune_export_tokens()
    payload = EXPORT_TOKEN_STORE.get(token)
    if not payload:
        raise HTTPException(status_code=404, detail="下載連結已失效，請重新匯出。")
    zip_path = Path(str(payload.get("zip_path") or ""))
    if not zip_path.exists():
        raise HTTPException(status_code=404, detail="找不到匯出檔案，請重新匯出。")
    return FileResponse(zip_path, media_type="application/zip", filename=zip_path.name)


def purge_soft_deleted_records(retention_days: int = 30, limit: int = 1000):
    ensure_activity_tables()
    ensure_recognition_soft_delete_schema()
    if retention_days < 0:
        retention_days = 0
    if limit < 1:
        limit = 1

    db, cursor = get_db_cursor()
    try:
        cursor.execute(
            """
            SELECT
                rr.id AS reco_id,
                iu.id AS upload_id,
                COALESCE(rr.origin_full_path, iu.origin_full_path, '') AS origin_full_path,
                COALESCE(rr.thumbs_full_path, iu.thumbs_full_path, '') AS thumbs_full_path,
                COALESCE(rr.photo_uuid, iu.photo_uuid, '') AS photo_uuid,
                COALESCE(MAX(rdl.deleted_at), rr.update_time, iu.update_time) AS deleted_at
            FROM reco_result rr
            LEFT JOIN img_upload iu
              ON (rr.photo_uuid IS NOT NULL AND rr.photo_uuid <> '' AND iu.photo_uuid = rr.photo_uuid)
              OR (iu.origin_full_path = rr.origin_full_path)
            LEFT JOIN reco_delete_log rdl ON rdl.reco_result_id = rr.id OR rdl.img_upload_id = iu.id
            WHERE COALESCE(rr.is_deleted, 0) = 1
            GROUP BY rr.id, iu.id, COALESCE(rr.origin_full_path, iu.origin_full_path, ''), COALESCE(rr.thumbs_full_path, iu.thumbs_full_path, ''), COALESCE(rr.photo_uuid, iu.photo_uuid, ''), rr.update_time, iu.update_time
            HAVING deleted_at <= DATE_SUB(NOW(), INTERVAL %s DAY)
            ORDER BY deleted_at ASC, reco_id ASC
            LIMIT %s
            """,
            (retention_days, limit),
        )
        reco_rows = cursor.fetchall() or []

        remaining = max(0, limit - len(reco_rows))
        upload_only_rows = []
        if remaining > 0:
            cursor.execute(
                """
                SELECT
                    NULL AS reco_id,
                    iu.id AS upload_id,
                    COALESCE(iu.origin_full_path, '') AS origin_full_path,
                    COALESCE(iu.thumbs_full_path, '') AS thumbs_full_path,
                    COALESCE(iu.photo_uuid, '') AS photo_uuid,
                    COALESCE(MAX(rdl.deleted_at), iu.update_time) AS deleted_at
                FROM img_upload iu
                LEFT JOIN reco_delete_log rdl ON rdl.img_upload_id = iu.id
                WHERE COALESCE(iu.is_deleted, 0) = 1
                  AND NOT EXISTS (
                    SELECT 1
                    FROM reco_result rr
                    WHERE COALESCE(rr.is_deleted, 0) = 1
                      AND (
                        (iu.photo_uuid IS NOT NULL AND iu.photo_uuid <> '' AND rr.photo_uuid = iu.photo_uuid)
                        OR rr.origin_full_path = iu.origin_full_path
                      )
                  )
                GROUP BY iu.id, COALESCE(iu.origin_full_path, ''), COALESCE(iu.thumbs_full_path, ''), COALESCE(iu.photo_uuid, ''), iu.update_time
                HAVING deleted_at <= DATE_SUB(NOW(), INTERVAL %s DAY)
                ORDER BY deleted_at ASC, upload_id ASC
                LIMIT %s
                """,
                (retention_days, remaining),
            )
            upload_only_rows = cursor.fetchall() or []

        rows = reco_rows + upload_only_rows
        if not rows:
            return {
                "scanned": 0,
                "scanned_from_reco_result": 0,
                "scanned_from_img_upload_only": 0,
                "purged_reco_result": 0,
                "purged_img_upload": 0,
                "deleted_files": 0,
                "missing_files": 0,
                "failed_files": 0,
                "retention_days": retention_days,
                "limit": limit,
            }

        reco_ids = sorted({int(row["reco_id"]) for row in rows if row.get("reco_id")})
        upload_ids = sorted({int(row["upload_id"]) for row in rows if row.get("upload_id")})
        origin_paths = [str(row.get("origin_full_path") or "").strip() for row in rows if str(row.get("origin_full_path") or "").strip()]
        thumb_paths = [str(row.get("thumbs_full_path") or "").strip() for row in rows if str(row.get("thumbs_full_path") or "").strip()]

        deleted_files = 0
        missing_files = 0
        failed_files = 0
        checked = set()
        file_candidates = []
        deleted_file_paths = []
        missing_file_paths = []
        failed_file_paths = []

        def _to_local_candidates(raw_path: str):
            text = str(raw_path or "").strip()
            if not text:
                return []
            # Windows 清理模式下只檢查 Windows 可讀寫路徑，避免 /mnt/* 被誤算 missing
            if os.name == "nt":
                candidates = []
                mapped = runtime_path_to_windows(text)
                if mapped and str(mapped).strip():
                    candidates.append(str(mapped).strip())
                if text.lower().startswith("c:\\") or text.lower().startswith("d:\\"):
                    candidates.append(text)
                # fallback：若 DB 路徑已是斜線格式但代表本機磁碟
                slash = text.replace("/", "\\")
                if slash.lower().startswith("c:\\") or slash.lower().startswith("d:\\"):
                    candidates.append(slash)
                unique = []
                seen = set()
                for item in candidates:
                    normalized = os.path.normpath(item)
                    if normalized.lower() in seen:
                        continue
                    seen.add(normalized.lower())
                    unique.append(normalized)
                return unique

            mapped = runtime_path_to_windows(text)
            paths = [text]
            if mapped and mapped not in paths:
                paths.append(mapped)
            return [os.path.normpath(item) for item in paths if str(item).strip()]

        def _thumb_variants(raw_path: str):
            text = str(raw_path or "").strip()
            if not text:
                return []
            slash = text.replace("\\", "/")
            variants = []
            if "/origin/" in slash:
                filename = slash.rsplit("/", 1)[-1]
                stem, ext = os.path.splitext(filename)
                base = slash.replace("/origin/", "/thumbs/")
                variants.append(base)
                variants.append(base.replace(filename, f"{stem}_thumb{ext}"))
            else:
                filename = slash.rsplit("/", 1)[-1]
                stem, ext = os.path.splitext(filename)
                if stem and not stem.endswith("_thumb"):
                    variants.append(slash.replace(filename, f"{stem}_thumb{ext}"))
            return [item for item in variants if item and item != text]

        source_paths = list(origin_paths) + list(thumb_paths)
        for raw_origin in origin_paths:
            source_paths.extend(_thumb_variants(raw_origin))
        for raw_thumb in thumb_paths:
            source_paths.extend(_thumb_variants(raw_thumb))

        for raw_path in source_paths:
            for candidate in _to_local_candidates(raw_path):
                key = candidate.lower()
                if key in checked:
                    continue
                checked.add(key)
                file_candidates.append(candidate)

        for file_path in file_candidates:
            try:
                if os.path.isfile(file_path):
                    os.remove(file_path)
                    deleted_files += 1
                    deleted_file_paths.append(file_path)
                else:
                    missing_files += 1
                    missing_file_paths.append(file_path)
            except Exception:
                failed_files += 1
                failed_file_paths.append(file_path)

        purged_reco_result = 0
        if reco_ids:
            reco_placeholders = ", ".join(["%s"] * len(reco_ids))
            cursor.execute(
                f"DELETE FROM reco_result WHERE id IN ({reco_placeholders})",
                tuple(reco_ids),
            )
            purged_reco_result = cursor.rowcount or 0

        purged_img_upload = 0
        if upload_ids:
            upload_placeholders = ", ".join(["%s"] * len(upload_ids))
            cursor.execute(
                f"""
                DELETE FROM img_upload
                WHERE id IN ({upload_placeholders})
                  AND COALESCE(is_deleted, 0) = 1
                """,
                tuple(upload_ids),
            )
            purged_img_upload = cursor.rowcount or 0

        db.conn.commit()
        return {
            "scanned": len(rows),
            "scanned_from_reco_result": len(reco_rows),
            "scanned_from_img_upload_only": len(upload_only_rows),
            "purged_reco_result": purged_reco_result,
            "purged_img_upload": purged_img_upload,
            "deleted_files": deleted_files,
            "missing_files": missing_files,
            "failed_files": failed_files,
            "deleted_file_paths": deleted_file_paths[:100],
            "missing_file_paths": missing_file_paths[:100],
            "failed_file_paths": failed_file_paths[:100],
            "deleted_file_paths_display": [repair_mojibake_text(item) for item in deleted_file_paths[:100]],
            "missing_file_paths_display": [repair_mojibake_text(item) for item in missing_file_paths[:100]],
            "failed_file_paths_display": [repair_mojibake_text(item) for item in failed_file_paths[:100]],
            "retention_days": retention_days,
            "limit": limit,
        }
    finally:
        cursor.close()
        db.close()


def query_filter_options(dept: str = "", year: int | None = None):
    db, cursor = get_db_cursor()

    try:
        cursor.execute("SELECT DISTINCT dept FROM base ORDER BY dept")
        dept_options = [row["dept"] for row in cursor.fetchall() if row["dept"]]

        cursor.execute("SELECT DISTINCT year FROM base ORDER BY year DESC")
        year_options = [row["year"] for row in cursor.fetchall() if row["year"] is not None]

        team_conditions = []
        team_params = []
        if dept:
            team_conditions.append("dept = %s")
            team_params.append(dept)
        if year is not None:
            team_conditions.append("year = %s")
            team_params.append(year)

        team_sql = "SELECT DISTINCT COALESCE(team, '') AS team FROM base"
        if team_conditions:
            team_sql += " WHERE " + " AND ".join(team_conditions)
        team_sql += " ORDER BY team"

        cursor.execute(team_sql, tuple(team_params))
        team_options = [row["team"] for row in cursor.fetchall() if row["team"]]

        return {
            "dept_options": dept_options,
            "year_options": year_options,
            "team_options": team_options,
        }
    finally:
        cursor.close()
        db.close()


def build_csv_rows(items):
    rows = []
    for item in items:
        rows.append(
            {
                "dept": item.get("dept", ""),
                "year": item.get("year", ""),
                "team": item.get("team", ""),
                "name": item.get("name", ""),
                "matched_person": item.get("matched_person", ""),
                "photo_uuid": item.get("photo_uuid", ""),
                "photo_create_time": item.get("photo_create_time", ""),
                "record_create_time": item.get("record_create_time", ""),
                "photo_taken_time": item.get("photo_taken_time") if item.get("photo_taken_time") is not None else "None",
                "image_score": item.get("image_score", ""),
                "origin_full_path": item.get("origin_full_path", ""),
                "thumbs_full_path": item.get("thumbs_full_path", ""),
                "reco_count": item.get("reco_count", 0),
                "reco_unknow": item.get("reco_unknow", 0),
                "recognition_status": item.get("recognition_status", ""),
                "reco_error": item.get("reco_error", ""),
                "is_unknown": bool(item.get("is_unknown", False)),
                "reco_name": json.dumps(item.get("reco_name", []), ensure_ascii=False),
                "reco_res": json.dumps(item.get("reco_res", []), ensure_ascii=False),
                "update_time": item.get("update_time", ""),
            }
        )
    return rows


def backfill_img_upload_photo_file_time(limit: int = 2000):
    db, cursor = get_db_cursor()
    try:
        cursor.execute(
            """
            SELECT id, origin_full_path, photo_file_time
            FROM img_upload
            WHERE photo_file_time IS NULL
            ORDER BY id ASC
            LIMIT %s
            """,
            (limit,),
        )
        rows = cursor.fetchall()
        updated = 0
        missing = 0
        for row in rows:
            origin_full_path = (row.get("origin_full_path") or "").strip()
            candidates = [origin_full_path]
            windows_candidate = runtime_path_to_windows(origin_full_path)
            if windows_candidate and windows_candidate not in candidates:
                candidates.append(windows_candidate)

            file_ctime = None
            for candidate in candidates:
                try:
                    normalized = os.path.normpath(candidate)
                    if os.path.isfile(normalized):
                        file_ctime = datetime.fromtimestamp(os.path.getctime(normalized))
                        break
                except Exception:
                    continue

            if file_ctime is None:
                missing += 1
                continue

            cursor.execute(
                "UPDATE img_upload SET photo_file_time = %s, update_time = NOW() WHERE id = %s",
                (file_ctime, row["id"]),
            )
            updated += 1

        db.conn.commit()
        return {"scanned": len(rows), "updated": updated, "missing": missing, "limit": limit}
    finally:
        cursor.close()
        db.close()


def _calculate_file_sha256(file_path: str):
    import hashlib as _hashlib
    hasher = _hashlib.sha256()
    with open(file_path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def backfill_img_upload_photo_uuid(limit: int = 2000):
    db, cursor = get_db_cursor()
    try:
        cursor.execute(
            """
            SELECT id, origin_full_path
            FROM img_upload
            WHERE photo_uuid IS NULL OR photo_uuid = ''
            ORDER BY id ASC
            LIMIT %s
            """,
            (limit,),
        )
        rows = cursor.fetchall()
        updated = 0
        missing = 0
        duplicated = 0
        for row in rows:
            origin_full_path = (row.get("origin_full_path") or "").strip()
            candidates = [origin_full_path]
            windows_candidate = runtime_path_to_windows(origin_full_path)
            if windows_candidate and windows_candidate not in candidates:
                candidates.append(windows_candidate)

            target_file = None
            for candidate in candidates:
                try:
                    normalized = os.path.normpath(candidate)
                    if os.path.isfile(normalized):
                        target_file = normalized
                        break
                except Exception:
                    continue
            if target_file is None:
                missing += 1
                continue
            photo_uuid = _calculate_file_sha256(target_file)
            try:
                cursor.execute(
                    "UPDATE img_upload SET photo_uuid = %s, update_time = NOW() WHERE id = %s",
                    (photo_uuid, row["id"]),
                )
                updated += 1
            except Exception:
                duplicated += 1
        db.conn.commit()
        return {"scanned": len(rows), "updated": updated, "missing": missing, "duplicated": duplicated, "limit": limit}
    finally:
        cursor.close()
        db.close()


def backfill_reco_result_photo_meta(limit: int = 5000):
    db, cursor = get_db_cursor()
    try:
        cursor.execute(
            """
            SELECT rr.id
            FROM reco_result rr
            LEFT JOIN img_upload iu ON iu.origin_full_path = rr.origin_full_path
            WHERE iu.origin_full_path IS NOT NULL
              AND (rr.photo_uuid IS NULL OR rr.photo_file_time IS NULL OR rr.taken_time_source IS NULL OR rr.taken_time_source = '' OR rr.taken_time_source = 'NONE')
            ORDER BY rr.id ASC
            LIMIT %s
            """,
            (limit,),
        )
        ids = [row["id"] for row in cursor.fetchall()]
        if not ids:
            return {"updated": 0, "limit": limit}
        placeholders = ", ".join(["%s"] * len(ids))
        cursor.execute(
            f"""
            UPDATE reco_result rr
            JOIN img_upload iu ON iu.origin_full_path = rr.origin_full_path
            SET
                rr.photo_uuid = COALESCE(rr.photo_uuid, iu.photo_uuid),
                rr.photo_file_time = COALESCE(rr.photo_file_time, iu.photo_file_time),
                rr.taken_time_source = CASE
                    WHEN rr.taken_time_source IS NULL OR rr.taken_time_source = '' OR rr.taken_time_source = 'NONE'
                    THEN COALESCE(iu.taken_time_source, rr.taken_time_source, 'NONE')
                    ELSE rr.taken_time_source
                END,
                rr.update_time = NOW()
            WHERE rr.id IN ({placeholders})
            """,
            tuple(ids),
        )
        updated = cursor.rowcount
        db.conn.commit()
        return {"updated": int(updated), "limit": limit}
    finally:
        cursor.close()
        db.close()


def build_person_label(dept: str, year: int, team: str | None, name: str | None):
    return f"{dept}_{year}_{team or ''}_{name or ''}"


def ensure_batch_roots():
    BATCH_UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    BATCH_OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)


def create_batch_job_dir(root: Path, prefix: str):
    ensure_batch_roots()
    job_dir = root / f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"
    job_dir.mkdir(parents=True, exist_ok=True)
    return job_dir


def list_tabular_sheets(file_path: str | Path):
    file_path = Path(file_path)
    suffix = file_path.suffix.lower()

    if suffix == ".csv":
        return ["CSV"]

    if suffix in {".xlsx", ".xlsm", ".xltx", ".xltm"}:
        try:
            excel_file = pd.ExcelFile(file_path, engine="openpyxl")
            return excel_file.sheet_names
        except ImportError as exc:
            raise RuntimeError("缺少 openpyxl，請先安裝後再讀取 Excel 檔案。") from exc

    if suffix == ".xls":
        try:
            excel_file = pd.ExcelFile(file_path)
            return excel_file.sheet_names
        except ImportError as exc:
            raise RuntimeError("讀取 .xls 檔案缺少必要套件，請先安裝 xlrd 或改存成 .xlsx。") from exc

    raise ValueError("不支援的檔案格式，僅接受 .xlsx、.xls、.xlsm、.xltx、.xltm、.csv")


def read_tabular_file(file_path: str | Path, sheet_name: str = ""):
    file_path = Path(file_path)
    suffix = file_path.suffix.lower()

    if suffix == ".csv":
        return pd.read_csv(file_path).fillna("")

    if suffix in {".xlsx", ".xlsm", ".xltx", ".xltm"}:
        try:
            read_kwargs = {"engine": "openpyxl"}
            if sheet_name:
                read_kwargs["sheet_name"] = sheet_name
            return pd.read_excel(file_path, **read_kwargs).fillna("")
        except ImportError as exc:
            raise RuntimeError("缺少 openpyxl，請先安裝後再讀取 Excel 檔案。") from exc

    if suffix == ".xls":
        try:
            read_kwargs = {}
            if sheet_name:
                read_kwargs["sheet_name"] = sheet_name
            return pd.read_excel(file_path, **read_kwargs).fillna("")
        except ImportError as exc:
            raise RuntimeError("讀取 .xls 檔案缺少必要套件，請先安裝 xlrd 或改存成 .xlsx。") from exc

    raise ValueError("不支援的檔案格式，僅接受 .xlsx、.xls、.xlsm、.xltx、.xltm、.csv")


def safe_filename_part(value):
    text = str(value or "").strip()
    for char in ['\\', '/', ':', '*', '?', '"', '<', '>', '|']:
        text = text.replace(char, "_")
    return text


def search_base_people(
    dept: str = "",
    year: int | None = None,
    team: str = "",
    name: str = "",
    limit: int = 50,
):
    db, cursor = get_db_cursor()
    conditions = []
    params = []

    if dept:
        conditions.append("dept = %s")
        params.append(dept)
    if year is not None:
        conditions.append("year = %s")
        params.append(year)
    if team:
        conditions.append("COALESCE(team, '') = %s")
        params.append(team)
    if name:
        conditions.append("COALESCE(name, '') LIKE %s")
        params.append(f"%{name}%")

    sql = """
    SELECT
        id,
        dept,
        year,
        COALESCE(team, '') AS team,
        COALESCE(name, '') AS name,
        phash,
        file_path,
        file_name,
        create_time,
        update_time
    FROM base
    """

    if conditions:
        sql += " WHERE " + " AND ".join(conditions)

    sql += " ORDER BY year DESC, dept, team, name LIMIT %s"

    try:
        cursor.execute(sql, (*params, limit))
        rows = cursor.fetchall()
        for row in rows:
            row["person_label"] = build_person_label(
                row["dept"], row["year"], row["team"], row["name"]
            )
        return normalize_record_rows(rows)
    finally:
        cursor.close()
        db.close()


def load_excel_columns(excel_path: str, sheet_name: str = ""):
    df = read_tabular_file(excel_path, sheet_name=sheet_name)
    return [str(column) for column in df.columns]


def normalize_runtime_path(path_value: str):
    normalized = (path_value or "").strip()
    if not normalized:
        return ""

    lower = normalized.lower().replace("/", "\\")
    mappings = {
        r"c:\feature_src": "/mnt/feature_src",
        r"c:\activity": "/mnt/activity",
        HOST_PROJECT_BASE_DIR.lower().replace("/", "\\"): "/root/noob",
    }

    for host_prefix, container_prefix in mappings.items():
        if lower.startswith(host_prefix):
            suffix = normalized[len(host_prefix):].lstrip("\\/")
            suffix = suffix.replace("\\", "/")
            return f"{container_prefix}/{suffix}" if suffix else container_prefix

    return normalized


def repair_mojibake_text(text: str):
    raw = str(text or "")
    if not raw:
        return raw
    candidate = raw
    with contextlib.suppress(Exception):
        fixed = raw.encode("latin1").decode("utf-8")
        if fixed:
            candidate = fixed
    return unicodedata.normalize("NFC", candidate)


def runtime_path_to_windows(path_value: str):
    normalized = (path_value or "").strip()
    if not normalized:
        return ""

    mappings = {
        "/mnt/feature_src": r"C:\feature_src",
        "/mnt/activity": r"C:\activity",
        "/root/noob": HOST_PROJECT_BASE_DIR,
        str(BASE_DIR).replace("\\", "/"): HOST_PROJECT_BASE_DIR,
    }

    normalized_slash = normalized.replace("\\", "/")
    lower = normalized_slash.lower()

    for runtime_prefix, host_prefix in mappings.items():
        runtime_prefix_slash = runtime_prefix.replace("\\", "/")
        if lower.startswith(runtime_prefix_slash.lower()):
            suffix = normalized_slash[len(runtime_prefix_slash):].lstrip("/")
            if host_prefix.startswith("C:\\"):
                return f"{host_prefix}\\{suffix.replace('/', '\\')}" if suffix else host_prefix
            return str(Path(host_prefix) / Path(suffix)) if suffix else str(Path(host_prefix))

    return normalized


def build_normalized_filename(row, filename_fields, delimiter, original_ext, extension_override):
    filename_parts = [safe_filename_part(row.get(field, "")) for field in filename_fields]
    filename_parts = [part for part in filename_parts if part]
    normalized_stem = delimiter.join(filename_parts)
    extension = extension_override.strip() if extension_override else original_ext
    if extension and not extension.startswith("."):
        extension = f".{extension}"
    return f"{normalized_stem}{extension}"


def build_unique_archive_path(target_dir: Path, original_name: str):
    candidate = target_dir / original_name
    if not candidate.exists():
        return candidate

    stem = candidate.stem
    suffix = candidate.suffix
    counter = 1
    while True:
        candidate = target_dir / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def archive_processed_source_files(source_path: Path, processed: list[dict]):
    archive_dir = source_path / "_normalized_success"
    archive_dir.mkdir(parents=True, exist_ok=True)

    archived = []
    for item in processed:
        source_file = Path(item["source_file"])
        archive_path = build_unique_archive_path(archive_dir, source_file.name)
        shutil.copy2(source_file, archive_path)
        source_file.unlink()
        archived.append(
            {
                "original_name": source_file.name,
                "archive_file": str(archive_path),
            }
        )

    return archive_dir, archived


def resolve_source_file(source_path: Path, original_name: str):
    direct_match = source_path / original_name
    if direct_match.is_file():
        return direct_match

    target_stem = Path(original_name).stem if Path(original_name).suffix else original_name
    matches = sorted(
        candidate
        for candidate in source_path.iterdir()
        if candidate.is_file() and candidate.stem == target_stem
    )
    if matches:
        return matches[0]

    return None


def normalize_headshot_batch(
    source_dir: str,
    destination_dir: str,
    excel_path: str,
    sheet_name: str,
    original_filename_column: str,
    filename_fields: list[str],
    delimiter: str,
    extension_override: str = "",
):
    source_path = Path(normalize_runtime_path(source_dir))
    normalized_destination_dir = normalize_runtime_path(destination_dir)
    destination_path = (
        Path(normalized_destination_dir)
        if normalized_destination_dir
        else create_batch_job_dir(BATCH_OUTPUT_ROOT, "normalized")
    )
    excel_file = Path(normalize_runtime_path(excel_path))

    if not source_path.is_dir():
        raise ValueError("來源圖檔資料夾不存在。")
    if not excel_file.is_file():
        raise ValueError("Excel 檔案不存在。")
    if not filename_fields:
        raise ValueError("請至少選擇一個檔名組合欄位。")

    destination_path.mkdir(parents=True, exist_ok=True)
    df = read_tabular_file(excel_file, sheet_name=sheet_name)

    if original_filename_column not in df.columns:
        raise ValueError("Excel 找不到你指定的原始檔名欄位。")

    for field in filename_fields:
        if field not in df.columns:
            raise ValueError(f"Excel 找不到欄位：{field}")

    processed = []
    missing = []
    duplicated_targets = []
    target_names_seen = set()

    for _, row in df.iterrows():
        original_name = str(row.get(original_filename_column, "")).strip()
        if not original_name:
            continue

        source_file = resolve_source_file(source_path, original_name)
        if source_file is None:
            missing.append(original_name)
            continue

        normalized_name = build_normalized_filename(
            row=row,
            filename_fields=filename_fields,
            delimiter=delimiter,
            original_ext=source_file.suffix,
            extension_override=extension_override,
        )
        target_file = destination_path / normalized_name

        if normalized_name in target_names_seen:
            duplicated_targets.append(normalized_name)
            continue
        target_names_seen.add(normalized_name)

        shutil.copy2(source_file, target_file)
        processed.append(
            {
                "original_name": original_name,
                "normalized_name": normalized_name,
                "source_file": str(source_file),
                "target_file": str(target_file),
            }
        )

    archive_dir, archived = archive_processed_source_files(source_path, processed)
    destination_host_dir = runtime_path_to_windows(str(destination_path))
    archive_host_dir = runtime_path_to_windows(str(archive_dir))

    processed_with_host = []
    for item in processed:
        item_with_host = dict(item)
        item_with_host["source_file_host"] = runtime_path_to_windows(item["source_file"])
        item_with_host["target_file_host"] = runtime_path_to_windows(item["target_file"])
        processed_with_host.append(item_with_host)

    archived_with_host = []
    for item in archived:
        item_with_host = dict(item)
        item_with_host["archive_file_host"] = runtime_path_to_windows(item["archive_file"])
        archived_with_host.append(item_with_host)

    return {
        "destination_dir": str(destination_path),
        "destination_host_dir": destination_host_dir,
        "archive_dir": str(archive_dir),
        "archive_host_dir": archive_host_dir,
        "archived_count": len(archived),
        "processed_count": len(processed),
        "missing_count": len(missing),
        "duplicate_target_count": len(duplicated_targets),
        "processed": processed_with_host[:100],
        "archived_files": archived_with_host[:100],
        "missing_files": missing[:100],
        "duplicate_targets": duplicated_targets[:100],
    }


def insert_feature_src_from_folder(folder_path: str):
    file_list = os.listdir(folder_path)
    results = []
    logs = [f"[new_insert_feature_src.py] ??鞈?憭? {folder_path}"]

    for filename in file_list:
        if filename not in [".DS_Store", ".gitkeep"]:
            file_path = os.path.join(folder_path, filename)
            if os.path.isfile(file_path):
                logs.append(f"[new_insert_feature_src.py] 霈??獢? {filename}")
                tmp = (filename.split(".")[0]).split("_")
                if len(tmp) < 4:
                    logs.append(f"[new_insert_feature_src.py] ?仿?瑼??澆?銝雲 4 畾? {filename}")
                    continue
                try:
                    img = Image.open(file_path)
                    phash = imagehash.phash(img)
                    tmp = tmp[:4]
                    tmp.append(str(phash))
                    logs.append(f"[new_insert_feature_src.py] phash 計算成功: {filename}")
                except Exception:
                    tmp = tmp[:4]
                    tmp.append("")
                    logs.append(f"[new_insert_feature_src.py] phash 計算失敗，已略過: {filename}")

                tmp.append(folder_path)
                tmp.append(filename)
                results.append(tmp)

    df = pd.DataFrame(
        data=results,
        columns=["dept", "year", "team", "name", "phash", "file_path", "file_name"],
    )
    if df.empty:
        logs.append("[new_insert_feature_src.py] 沒有可寫入 base 的有效照片資料。")
        return {"inserted_count": 0, "processed_files": [], "logs": logs}

    df["year"] = df["year"].astype(int)

    db, cursor = get_db_cursor()
    try:
        upsert_query = """
        INSERT INTO base (
            dept,
            year,
            team,
            name,
            phash,
            file_path,
            file_name,
            create_time,
            update_time
        )
        VALUES (
            %(dept)s,
            %(year)s,
            %(team)s,
            %(name)s,
            %(phash)s,
            %(file_path)s,
            %(file_name)s,
            NOW(),
            NOW()
        )
        ON DUPLICATE KEY UPDATE
            phash = VALUES(phash),
            file_path = VALUES(file_path),
            file_name = VALUES(file_name),
            update_time = NOW();
        """
        cursor.executemany(upsert_query, df.to_dict("records"))
        db.conn.commit()
        logs.append(f"[new_insert_feature_src.py] 已完成 base upsert：{len(df)} 筆")
        return {
            "inserted_count": len(df),
            "processed_files": [
                {
                    "file_path": record["file_path"],
                    "file_name": record["file_name"],
                    "full_path": str(Path(record["file_path"]) / record["file_name"]),
                }
                for record in df.to_dict("records")
            ],
            "logs": logs,
        }
    finally:
        cursor.close()
        db.close()


def archive_feature_build_files(feature_folder_path: str, processed_files: list[dict]):
    feature_path = Path(feature_folder_path)
    archive_dir = feature_path / "_feature_build_success"
    archive_dir.mkdir(parents=True, exist_ok=True)

    archived = []
    for item in processed_files:
        source_file = Path(item["full_path"])
        if not source_file.is_file():
            continue
        archive_path = build_unique_archive_path(archive_dir, source_file.name)
        shutil.copy2(source_file, archive_path)
        source_file.unlink()
        archived.append(
            {
                "original_name": source_file.name,
                "archive_file": str(archive_path),
                "old_file_path": item["file_path"],
                "old_file_name": item["file_name"],
                "new_file_path": str(archive_dir),
                "new_file_name": archive_path.name,
            }
        )

    return archive_dir, archived


def update_base_archived_feature_paths(archived_files: list[dict]):
    if not archived_files:
        return 0

    db, cursor = get_db_cursor()
    updated_count = 0
    try:
        for item in archived_files:
            cursor.execute(
                """
                UPDATE base
                SET file_path = %s,
                    file_name = %s,
                    update_time = NOW()
                WHERE file_path = %s
                  AND file_name = %s
                """,
                (
                    item["new_file_path"],
                    item["new_file_name"],
                    item["old_file_path"],
                    item["old_file_name"],
                ),
            )
            updated_count += cursor.rowcount

        db.conn.commit()
        return updated_count
    finally:
        cursor.close()
        db.close()


def run_feature_build_batch(feature_folder_path: str):
    normalized_feature_folder_path = normalize_runtime_path(feature_folder_path)
    logs = [f"[feature-build] 雿輻鞈?憭? {feature_folder_path}"]
    if normalized_feature_folder_path != feature_folder_path:
        logs.append(f"[feature-build] 摰孵頝臬?頧?: {normalized_feature_folder_path}")
    insert_result = insert_feature_src_from_folder(normalized_feature_folder_path)
    logs.extend(insert_result.get("logs", []))
    started = time.perf_counter()
    logs.append("[new_construct_face_db.py] ???遣 embedding")
    stdout_buffer = io.StringIO()
    with contextlib.redirect_stdout(stdout_buffer):
        embedding_count = rebuild_face_embeddings()
    rebuild_log_lines = [line for line in stdout_buffer.getvalue().splitlines() if line.strip()]
    logs.extend([f"[new_construct_face_db.py] {line}" for line in rebuild_log_lines])
    rebuild_seconds = round(time.perf_counter() - started, 2)
    logs.append(f"[new_construct_face_db.py] embedding 重建完成：{embedding_count} 筆，耗時 {rebuild_seconds} 秒")
    archive_dir, archived_files = archive_feature_build_files(
        normalized_feature_folder_path,
        insert_result.get("processed_files", []),
    )
    updated_base_count = update_base_archived_feature_paths(archived_files)
    logs.append(f"[feature-build] 已封存成功建立特徵的來源照片：{len(archived_files)} 筆")
    logs.append(f"[feature-build] 已同步更新 base 檔案路徑：{updated_base_count} 筆")
    write_ui_log(FEATURE_BUILD_LOG_PATH, logs)

    archive_host_dir = runtime_path_to_windows(str(archive_dir))
    archived_with_host = []
    for item in archived_files:
        item_with_host = dict(item)
        item_with_host["archive_file_host"] = runtime_path_to_windows(item["archive_file"])
        archived_with_host.append(item_with_host)

    return {
        "inserted_count": insert_result["inserted_count"],
        "embedding_count": embedding_count,
        "embedding_rebuilt": True,
        "rebuild_seconds": rebuild_seconds,
        "archive_dir": str(archive_dir),
        "archive_host_dir": archive_host_dir,
        "archived_count": len(archived_files),
        "updated_base_count": updated_base_count,
        "archived_files": archived_with_host[:100],
        "logs": logs[:500],
    }


def rebuild_face_embeddings():
    from tools.new_face import FaceRecognition

    face_recognition = FaceRecognition()
    face_recognition.load_faces()
    return len(face_recognition.faces_embedding)


def load_runtime_embeddings():
    if not EMBEDDING_PKL_PATH.exists():
        return []
    with EMBEDDING_PKL_PATH.open("rb") as handle:
        return pickle.load(handle)


def backup_embedding_file(reason: str):
    if not EMBEDDING_PKL_PATH.exists():
        return ""

    EMBEDDING_BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = EMBEDDING_BACKUP_ROOT / f"{EMBEDDING_PKL_PATH.stem}_{reason}_{timestamp}.pkl"
    shutil.copy2(EMBEDDING_PKL_PATH, backup_path)
    return str(backup_path)


def save_runtime_embeddings(entries):
    EMBEDDING_PKL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with EMBEDDING_PKL_PATH.open("wb") as handle:
        pickle.dump(entries, handle)


def get_runtime_embedding_names():
    entries = load_runtime_embeddings()
    return entries, {str(item.get("user_name", "")) for item in entries if item.get("user_name")}


def build_base_filters(
    dept: str = "",
    year: int | None = None,
    team: str = "",
    name: str = "",
):
    conditions = []
    params = []

    if dept:
        conditions.append("b.dept = %s")
        params.append(dept)
    if year is not None:
        conditions.append("b.year = %s")
        params.append(year)
    if team:
        conditions.append("COALESCE(b.team, '') = %s")
        params.append(team)
    if name:
        conditions.append("COALESCE(b.name, '') LIKE %s")
        params.append(f"%{name}%")

    return conditions, params


def query_embedding_meta_rows(
    dept: str = "",
    year: int | None = None,
    team: str = "",
    name: str = "",
    status: str = "",
    limit: int = 100,
):
    db, cursor = get_db_cursor()
    conditions, params = build_base_filters(dept=dept, year=year, team=team, name=name)
    if status:
        conditions.append("COALESCE(fem.status, 'missing_meta') = %s")
        params.append(status)

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    sql = f"""
    SELECT
        b.id AS base_id,
        b.dept,
        b.year,
        COALESCE(b.team, '') AS team,
        COALESCE(b.name, '') AS name,
        CONCAT(b.dept, '_', b.year, '_', COALESCE(b.team, ''), '_', COALESCE(b.name, '')) AS person_label,
        b.file_path,
        b.file_name,
        b.phash,
        fem.id AS meta_id,
        fem.user_name,
        COALESCE(fem.embedding_exists, 0) AS embedding_exists,
        COALESCE(fem.face_count, 0) AS face_count,
        COALESCE(fem.status, 'missing_meta') AS status,
        COALESCE(fem.error_message, '') AS error_message,
        fem.embedding_update_time,
        fem.update_time AS meta_update_time
    FROM base b
    LEFT JOIN face_embedding_meta fem
      ON fem.base_id = b.id
     AND fem.model_name = %s
    {where_clause}
    ORDER BY b.year DESC, b.dept, team, name
    LIMIT %s
    """

    try:
        cursor.execute(sql, (EMBEDDING_MODEL_NAME, *params, limit))
        rows = cursor.fetchall()
        _, runtime_names = get_runtime_embedding_names()
        for row in rows:
            row["runtime_present"] = row["person_label"] in runtime_names
            for key in ("embedding_update_time", "meta_update_time"):
                if row.get(key) is not None and hasattr(row[key], "isoformat"):
                    row[key] = row[key].isoformat(sep=" ", timespec="seconds")
        return rows
    finally:
        cursor.close()
        db.close()


def summarize_embedding_meta(
    dept: str = "",
    year: int | None = None,
    team: str = "",
    name: str = "",
):
    db, cursor = get_db_cursor()
    conditions, params = build_base_filters(dept=dept, year=year, team=team, name=name)
    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    summary_sql = f"""
    SELECT
        COUNT(*) AS base_total,
        SUM(CASE WHEN fem.id IS NOT NULL THEN 1 ELSE 0 END) AS meta_total,
        SUM(CASE WHEN fem.embedding_exists = 1 THEN 1 ELSE 0 END) AS ready_total,
        SUM(CASE WHEN COALESCE(fem.status, '') = 'deleted' THEN 1 ELSE 0 END) AS deleted_total,
        SUM(CASE
                WHEN fem.id IS NULL THEN 1
                WHEN COALESCE(fem.status, '') NOT IN ('ready', 'deleted') THEN 1
                ELSE 0
            END) AS issue_total
    FROM base b
    LEFT JOIN face_embedding_meta fem
      ON fem.base_id = b.id
     AND fem.model_name = %s
    {where_clause}
    """

    try:
        cursor.execute(summary_sql, (EMBEDDING_MODEL_NAME, *params))
        row = cursor.fetchone() or {}
        runtime_entries = load_runtime_embeddings()
        row["runtime_pkl_total"] = len(runtime_entries)
        return {
            "base_total": int(row.get("base_total") or 0),
            "meta_total": int(row.get("meta_total") or 0),
            "ready_total": int(row.get("ready_total") or 0),
            "deleted_total": int(row.get("deleted_total") or 0),
            "issue_total": int(row.get("issue_total") or 0),
            "runtime_pkl_total": int(row.get("runtime_pkl_total") or 0),
        }
    finally:
        cursor.close()
        db.close()


def delete_embedding_entry(base_id: int):
    db, cursor = get_db_cursor()
    try:
        cursor.execute(
            """
            SELECT
                b.id AS base_id,
                CONCAT(b.dept, '_', b.year, '_', COALESCE(b.team, ''), '_', COALESCE(b.name, '')) AS person_label,
                fem.id AS meta_id,
                COALESCE(fem.status, 'missing_meta') AS status
            FROM base b
            LEFT JOIN face_embedding_meta fem
              ON fem.base_id = b.id
             AND fem.model_name = %s
            WHERE b.id = %s
            """,
            (EMBEDDING_MODEL_NAME, base_id),
        )
        current = cursor.fetchone()
        if not current:
            raise HTTPException(status_code=404, detail="找不到對應的人員資料")

        runtime_entries = load_runtime_embeddings()
        backup_path = backup_embedding_file("admin_delete")
        filtered_entries = [
            item for item in runtime_entries
            if str(item.get("user_name", "")) != current["person_label"]
        ]
        removed_count = len(runtime_entries) - len(filtered_entries)

        if removed_count > 0:
            save_runtime_embeddings(filtered_entries)

        cursor.execute(
            """
            INSERT INTO face_embedding_meta (
                base_id,
                user_name,
                file_path,
                file_name,
                phash,
                model_name,
                embedding_exists,
                face_count,
                status,
                error_message,
                embedding_update_time,
                create_time,
                update_time
            )
            SELECT
                b.id,
                CONCAT(b.dept, '_', b.year, '_', COALESCE(b.team, ''), '_', COALESCE(b.name, '')),
                b.file_path,
                b.file_name,
                COALESCE(b.phash, ''),
                %s,
                0,
                0,
                'deleted',
                %s,
                NOW(),
                NOW(),
                NOW()
            FROM base b
            WHERE b.id = %s
            ON DUPLICATE KEY UPDATE
                user_name = VALUES(user_name),
                file_path = VALUES(file_path),
                file_name = VALUES(file_name),
                phash = VALUES(phash),
                embedding_exists = 0,
                face_count = 0,
                status = 'deleted',
                error_message = VALUES(error_message),
                embedding_update_time = NOW(),
                update_time = NOW()
            """,
            (EMBEDDING_MODEL_NAME, "Deleted from admin-ui", base_id),
        )
        db.conn.commit()
        return {
            "base_id": base_id,
            "person_label": current["person_label"],
            "removed_count": removed_count,
            "backup_path": backup_path,
            "status": "deleted",
        }
    except Exception:
        db.conn.rollback()
        raise
    finally:
        cursor.close()
        db.close()


def update_history_labels(cursor, old_label: str, new_label: str):
    cursor.execute(
        """
        SELECT id, reco_name, reco_res
        FROM reco_result
        WHERE reco_name LIKE %s
        """,
        (f"%{old_label}%",),
    )
    rows = cursor.fetchall()
    updated_count = 0

    for row in rows:
        reco_name = json.loads(row["reco_name"]) if row["reco_name"] else []
        reco_res = json.loads(row["reco_res"]) if row["reco_res"] else []

        changed = False
        for index, value in enumerate(reco_name):
            if value == old_label:
                reco_name[index] = new_label
                changed = True

        for item in reco_res:
            if isinstance(item, dict) and item.get("name") == old_label:
                item["name"] = new_label
                changed = True

        if changed:
            cursor.execute(
                """
                UPDATE reco_result
                SET reco_name = %s,
                    reco_res = %s,
                    update_time = NOW()
                WHERE id = %s
                """,
                (
                    json.dumps(reco_name, ensure_ascii=False),
                    json.dumps(reco_res, ensure_ascii=False),
                    row["id"],
                ),
            )
            updated_count += 1

    return updated_count


def remove_history_label(cursor, old_label: str):
    cursor.execute(
        """
        SELECT id, reco_name, reco_res
        FROM reco_result
        WHERE reco_name LIKE %s
        """,
        (f"%{old_label}%",),
    )
    rows = cursor.fetchall()
    updated_count = 0

    for row in rows:
        reco_name = json.loads(row["reco_name"]) if row["reco_name"] else []
        reco_res = json.loads(row["reco_res"]) if row["reco_res"] else []

        filtered_names = [value for value in reco_name if value != old_label]
        filtered_res = [
            item for item in reco_res
            if not (isinstance(item, dict) and item.get("name") == old_label)
        ]

        if filtered_names != reco_name or filtered_res != reco_res:
            reco_count = sum(1 for value in filtered_names if value != "unknown")
            reco_unknow = sum(1 for value in filtered_names if value == "unknown")
            cursor.execute(
                """
                UPDATE reco_result
                SET reco_name = %s,
                    reco_res = %s,
                    reco_count = %s,
                    reco_unknow = %s,
                    update_time = NOW()
                WHERE id = %s
                """,
                (
                    json.dumps(filtered_names, ensure_ascii=False),
                    json.dumps(filtered_res, ensure_ascii=False),
                    reco_count,
                    reco_unknow,
                    row["id"],
                ),
            )
            updated_count += 1

    return updated_count


class PersonUpdatePayload(BaseModel):
    id: int
    dept: str = Field(..., min_length=1, max_length=64)
    year: int = Field(..., ge=1, le=9999)
    team: str = Field(default="", max_length=64)
    name: str = Field(..., min_length=1, max_length=255)


class PersonDeletePayload(BaseModel):
    id: int = Field(..., ge=1)


class PersonBulkDeletePayload(BaseModel):
    ids: list[int] = Field(..., min_length=1, max_length=5000)


class BatchNormalizePayload(BaseModel):
    source_dir: str = Field(..., min_length=1)
    destination_dir: str = Field(default="")
    excel_path: str = Field(..., min_length=1)
    sheet_name: str = Field(default="")
    original_filename_column: str = Field(..., min_length=1)
    filename_fields: list[str] = Field(..., min_length=1)
    delimiter: str = Field(default="_", min_length=1, max_length=8)
    extension_override: str = Field(default="", max_length=16)


class FeatureBuildPayload(BaseModel):
    feature_folder_path: str = Field(..., min_length=1)


class EmbeddingDeletePayload(BaseModel):
    base_id: int = Field(..., ge=1)


class ActivityScheduleImportPayload(BaseModel):
    excel_path: str = Field(..., min_length=1)
    sheet_name: str = Field(default="")
    activity_code_column: str = Field(default="")
    activity_date_column: str = Field(..., min_length=1)
    activity_time_column: str = Field(default="")
    activity_content_column: str = Field(..., min_length=1)
    owner_team_column: str = Field(default="")
    location_column: str = Field(default="")
    photographer_column: str = Field(default="")
    note_column: str = Field(default="")


class ActivityScheduleUpdatePayload(BaseModel):
    id: int = Field(..., ge=1)
    activity_code: str = Field(..., min_length=1)
    activity_date: str = Field(..., min_length=1)
    activity_time: str = Field(default="")
    activity_content: str = Field(default="")
    owner_team: str = Field(default="")
    location: str = Field(default="")
    photographer: str = Field(default="")
    note: str = Field(default="")


class ActivityScheduleDeletePayload(BaseModel):
    id: int = Field(..., ge=1)


class PhotographerImportPayload(BaseModel):
    excel_path: str = Field(..., min_length=1)
    sheet_name: str = Field(default="")
    photographer_column: str = Field(..., min_length=1)
    note_column: str = Field(default="")


class PhotographerUpdatePayload(BaseModel):
    id: int = Field(..., ge=1)
    photographer_name: str = Field(..., min_length=1)
    note: str = Field(default="")


class PhotographerDeletePayload(BaseModel):
    id: int = Field(..., ge=1)


class ActivityAwardImportPayload(BaseModel):
    excel_path: str = Field(..., min_length=1)
    sheet_name: str = Field(default="")
    serial_no_column: str = Field(default="")
    category_column: str = Field(..., min_length=1)
    activity_item_column: str = Field(..., min_length=1)
    mapped_award_column: str = Field(default="")
    award_name_column: str = Field(..., min_length=1)
    note_column: str = Field(default="")


class ActivityAwardUpdatePayload(BaseModel):
    id: int = Field(..., ge=1)
    serial_no: int = Field(..., ge=1)
    award_category: str = Field(..., min_length=1)
    activity_item: str = Field(..., min_length=1)
    mapped_award: str = Field(default="")
    award_name: str = Field(..., min_length=1)
    note: str = Field(default="")


class ActivityAwardDeletePayload(BaseModel):
    id: int = Field(..., ge=1)


class LaptopToolUploadStartPayload(BaseModel):
    device_id: str = Field(..., min_length=1, max_length=64)
    laptop_label: str = Field(default="", max_length=128)
    model_version: str = Field(default="", max_length=64)
    total_count: int = Field(default=0, ge=0, le=100000)


class LaptopToolUploadChunkMeta(BaseModel):
    photo_uuid: str = Field(..., min_length=1, max_length=128)
    file_name: str = Field(..., min_length=1, max_length=260)
    photo_taken_time: str = Field(default="")
    photo_file_time: str = Field(default="")
    taken_time_source: str = Field(default="NONE", max_length=16)
    human_activity_date: str = Field(default="")
    human_activity_time: str = Field(default="")
    human_activity_name: str = Field(default="", max_length=255)
    human_owner_team: str = Field(default="", max_length=255)
    human_location: str = Field(default="", max_length=255)
    human_photographer: str = Field(default="", max_length=255)
    reco_name: list[str] = Field(default_factory=list)
    reco_res: list[dict] = Field(default_factory=list)
    reco_count: int = Field(default=0, ge=0)
    reco_unknow: int = Field(default=0, ge=0)
    reco_status: str = Field(default="DONE", max_length=16)
    reco_error: str = Field(default="")
    img_score: float | None = None
    det_score: float | None = None


class LaptopToolUploadCommitPayload(BaseModel):
    job_id: str = Field(..., min_length=1, max_length=80)
    finalize: bool = Field(default=True)


class LaptopToolUploadControlPayload(BaseModel):
    job_id: str = Field(..., min_length=1, max_length=80)
    action: str = Field(..., min_length=1, max_length=16)
    reason: str = Field(default="", max_length=1000)


class LaptopToolAdminSettingsPayload(BaseModel):
    server_api_base: str = Field(default="", max_length=512)
    public_base_url: str = Field(default="", max_length=512)
    default_activity_code: str = Field(default="", max_length=64)
    default_photographer: str = Field(default="", max_length=255)


def ensure_laptop_tool_tables():
    db = None
    cursor = None
    try:
        db = mysqlconnector()
        db.connect()
        if db.conn is None:
            raise RuntimeError("資料庫連線失敗")
        cursor = db.conn.cursor(dictionary=True)
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS laptop_upload_job (
                job_id VARCHAR(80) NOT NULL PRIMARY KEY,
                status VARCHAR(16) NOT NULL DEFAULT 'QUEUED',
                device_id VARCHAR(64) NOT NULL,
                laptop_label VARCHAR(128) DEFAULT '',
                model_version VARCHAR(64) DEFAULT '',
                total_count INT NOT NULL DEFAULT 0,
                uploaded_count INT NOT NULL DEFAULT 0,
                committed_count INT NOT NULL DEFAULT 0,
                failed_count INT NOT NULL DEFAULT 0,
                error_summary TEXT NULL,
                staging_dir VARCHAR(500) DEFAULT '',
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                finished_at DATETIME NULL,
                KEY idx_laptop_upload_job_status (status),
                KEY idx_laptop_upload_job_updated (updated_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        with contextlib.suppress(Exception):
            cursor.execute(
                """
                ALTER TABLE laptop_upload_job
                ADD INDEX idx_laptop_upload_job_device_status_updated (device_id, status, updated_at)
                """
            )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS laptop_upload_job_item (
                id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                job_id VARCHAR(80) NOT NULL,
                seq_no INT NOT NULL,
                photo_uuid VARCHAR(128) NOT NULL,
                file_name VARCHAR(260) NOT NULL,
                payload_json LONGTEXT NULL,
                origin_staging_path VARCHAR(500) DEFAULT '',
                thumb_staging_path VARCHAR(500) DEFAULT '',
                status VARCHAR(16) NOT NULL DEFAULT 'UPLOADED',
                reason_code VARCHAR(64) DEFAULT '',
                error_reason TEXT NULL,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uq_laptop_upload_job_item (job_id, photo_uuid),
                KEY idx_laptop_upload_job_item_status (status),
                KEY idx_laptop_upload_job_item_job (job_id, seq_no)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        with contextlib.suppress(Exception):
            cursor.execute(
                """
                ALTER TABLE laptop_upload_job_item
                ADD INDEX idx_laptop_upload_job_item_job_status_seq (job_id, status, seq_no)
                """
            )
        db.conn.commit()
    finally:
        with contextlib.suppress(Exception):
            if cursor:
                cursor.close()
        with contextlib.suppress(Exception):
            if db:
                db.close()


def _safe_leaf_name(value: str) -> str:
    return Path(str(value or "")).name.replace("\x00", "")


def _to_datetime_or_none(value: str):
    text = str(value or "").strip()
    if not text:
        return None
    fmts = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d")
    for fmt in fmts:
        with contextlib.suppress(ValueError):
            return datetime.strptime(text, fmt)
    return None


_TABLE_COLUMNS_CACHE: dict[str, set[str]] = {}


def _get_table_columns(cursor, table_name: str, *, refresh: bool = False) -> set[str]:
    cached = _TABLE_COLUMNS_CACHE.get(table_name)
    if cached is not None and not refresh:
        return set(cached)
    cursor.execute(f"SHOW COLUMNS FROM `{table_name}`")
    cols = {row["Field"] for row in cursor.fetchall()}
    _TABLE_COLUMNS_CACHE[table_name] = set(cols)
    return set(cols)


def _upsert_laptop_item_to_main_tables(
    cursor,
    item_payload: dict,
    origin_target_path: str,
    thumb_target_path: str,
    *,
    reco_result_cols: set[str] | None = None,
):
    photo_uuid = str(item_payload.get("photo_uuid") or "").strip()
    if not photo_uuid:
        raise ValueError("photo_uuid 不可空白")
    reco_name = item_payload.get("reco_name") or []
    reco_res = item_payload.get("reco_res") or []
    reco_status = str(item_payload.get("reco_status") or "DONE").upper()
    reco_error = str(item_payload.get("reco_error") or "")
    taken_time_source = str(item_payload.get("taken_time_source") or "NONE").upper()
    photo_taken_time = _to_datetime_or_none(item_payload.get("photo_taken_time", ""))
    photo_file_time = _to_datetime_or_none(item_payload.get("photo_file_time", ""))
    human_activity_date = item_payload.get("human_activity_date") or None
    human_activity_time = item_payload.get("human_activity_time") or None
    cursor.execute(
        """
        INSERT INTO img_upload (
            origin_full_path, thumbs_full_path, schedule_id,
            human_activity_date, human_activity_time, human_activity_name,
            human_owner_team, human_location, human_laptop_number, human_photographer, human_photo_time,
            photo_uuid, photo_taken_time, photo_file_time, taken_time_source,
            reco_status, reco_error, reco_last_try_time, reco_retry_count, img_score
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), 1, %s)
        ON DUPLICATE KEY UPDATE
            origin_full_path = VALUES(origin_full_path),
            thumbs_full_path = VALUES(thumbs_full_path),
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
            reco_status = VALUES(reco_status),
            reco_error = VALUES(reco_error),
            reco_last_try_time = NOW(),
            reco_retry_count = reco_retry_count + 1,
            img_score = VALUES(img_score),
            update_time = NOW()
        """,
        (
            origin_target_path,
            thumb_target_path,
            item_payload.get("schedule_id"),
            human_activity_date,
            human_activity_time,
            item_payload.get("human_activity_name", ""),
            item_payload.get("human_owner_team", ""),
            item_payload.get("human_location", ""),
            item_payload.get("human_laptop_number", ""),
            item_payload.get("human_photographer", ""),
            photo_taken_time or photo_file_time,
            photo_uuid,
            photo_taken_time,
            photo_file_time,
            taken_time_source,
            reco_status,
            reco_error,
            item_payload.get("img_score"),
        ),
    )

    reco_result_cols = set(reco_result_cols or _get_table_columns(cursor, "reco_result"))
    reco_insert_columns = [
        "origin_full_path",
        "thumbs_full_path",
        "photo_taken_time",
        "reco_count",
        "reco_unknow",
        "reco_res",
        "reco_name",
    ]
    reco_values = [
        origin_target_path,
        thumb_target_path,
        photo_taken_time,
        int(item_payload.get("reco_count") or 0),
        int(item_payload.get("reco_unknow") or 0),
        json.dumps(reco_res, ensure_ascii=False),
        json.dumps(reco_name, ensure_ascii=False),
    ]
    reco_update_sets = [
        "photo_taken_time = VALUES(photo_taken_time)",
        "reco_count = VALUES(reco_count)",
        "reco_unknow = VALUES(reco_unknow)",
        "reco_res = VALUES(reco_res)",
        "reco_name = VALUES(reco_name)",
    ]
    if "photo_uuid" in reco_result_cols:
        reco_insert_columns.append("photo_uuid")
        reco_values.append(photo_uuid)
        reco_update_sets.append("photo_uuid = VALUES(photo_uuid)")
    if "photo_file_time" in reco_result_cols:
        reco_insert_columns.append("photo_file_time")
        reco_values.append(photo_file_time)
        reco_update_sets.append("photo_file_time = VALUES(photo_file_time)")
    if "taken_time_source" in reco_result_cols:
        reco_insert_columns.append("taken_time_source")
        reco_values.append(taken_time_source)
        reco_update_sets.append("taken_time_source = VALUES(taken_time_source)")
    if "create_time" in reco_result_cols:
        reco_insert_columns.append("create_time")
    if "update_time" in reco_result_cols:
        reco_insert_columns.append("update_time")

    reco_insert_placeholders = ["%s"] * len(reco_values)
    if "create_time" in reco_result_cols:
        reco_insert_placeholders.append("NOW()")
    if "update_time" in reco_result_cols:
        reco_insert_placeholders.append("NOW()")
    if "update_time" in reco_result_cols:
        reco_update_sets.append("update_time = NOW()")

    reco_insert_sql = f"""
        INSERT INTO reco_result (
            {", ".join(reco_insert_columns)}
        ) VALUES (
            {", ".join(reco_insert_placeholders)}
        )
        ON DUPLICATE KEY UPDATE
            {", ".join(reco_update_sets)}
    """
    cursor.execute(
        reco_insert_sql,
        tuple(reco_values),
    )
@app.post("/activity-schedules/update")
async def activity_schedule_update(payload: ActivityScheduleUpdatePayload):
    try:
        ensure_activity_tables()
        return update_activity_schedule(
            schedule_id=payload.id,
            activity_code=payload.activity_code,
            activity_date=payload.activity_date,
            activity_time=payload.activity_time,
            activity_content=payload.activity_content,
            owner_team=payload.owner_team,
            location=payload.location,
            note=payload.note,
        )
    except Exception as e:
        logger.error(f"活動行程更新失敗: {str(e)}")
        return JSONResponse(status_code=500, content={"detail": f"活動行程更新失敗：{str(e)}"})


@app.post("/activity-schedules/delete")
async def activity_schedule_delete(payload: ActivityScheduleDeletePayload):
    try:
        ensure_activity_tables()
        return delete_activity_schedule(payload.id)
    except Exception as e:
        logger.error(f"活動行程刪除失敗: {str(e)}")
        return JSONResponse(status_code=500, content={"detail": f"活動行程刪除失敗：{str(e)}"})


@app.post("/activity-schedules/delete-all")
async def activity_schedule_delete_all():
    try:
        ensure_activity_tables()
        return delete_all_activity_schedule()
    except Exception as e:
        logger.error(f"活動行程全部刪除失敗: {str(e)}")
        return JSONResponse(status_code=500, content={"detail": f"活動行程全部刪除失敗：{str(e)}"})


@app.get("/photographers/query")
async def photographer_query(keyword: str = Query(""), limit: int = Query(200, ge=1, le=1000)):
    try:
        ensure_activity_tables()
        items = query_photographer_master(keyword=keyword, limit=limit)
        return {"total": len(items), "items": items}
    except Exception as e:
        logger.error(f"攝影師查詢失敗: {str(e)}")
        return JSONResponse(status_code=500, content={"detail": f"攝影師查詢失敗：{str(e)}"})


@app.post("/photographers/import-excel")
async def photographer_import_excel(payload: PhotographerImportPayload):
    try:
        ensure_activity_tables()
        return import_photographer_master(
            excel_path=payload.excel_path,
            sheet_name=payload.sheet_name,
            photographer_column=payload.photographer_column,
            note_column=payload.note_column,
        )
    except Exception as e:
        logger.error(f"攝影師 Excel 匯入失敗: {str(e)}")
        return JSONResponse(status_code=500, content={"detail": f"攝影師 Excel 匯入失敗：{str(e)}"})


@app.post("/photographers/update")
async def photographer_update(payload: PhotographerUpdatePayload):
    try:
        ensure_activity_tables()
        return update_photographer_master(
            item_id=payload.id,
            photographer_name=payload.photographer_name,
            note=payload.note,
        )
    except Exception as e:
        logger.error(f"攝影師更新失敗: {str(e)}")
        return JSONResponse(status_code=500, content={"detail": f"攝影師更新失敗：{str(e)}"})


@app.post("/photographers/delete")
async def photographer_delete(payload: PhotographerDeletePayload):
    try:
        ensure_activity_tables()
        return delete_photographer_master(payload.id)
    except Exception as e:
        logger.error(f"攝影師刪除失敗: {str(e)}")
        return JSONResponse(status_code=500, content={"detail": f"攝影師刪除失敗：{str(e)}"})


@app.post("/photographers/delete-all")
async def photographer_delete_all():
    try:
        ensure_activity_tables()
        return delete_all_photographer_master()
    except Exception as e:
        logger.error(f"攝影師全部刪除失敗: {str(e)}")
        return JSONResponse(status_code=500, content={"detail": f"攝影師全部刪除失敗：{str(e)}"})


def ensure_activity_award_table():
    db, cursor = get_db_cursor()
    try:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS activity_award_master (
                id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                serial_no INT NOT NULL,
                award_category VARCHAR(255) NOT NULL,
                activity_item VARCHAR(255) NOT NULL,
                mapped_award VARCHAR(255) NULL,
                award_name VARCHAR(255) NOT NULL,
                note TEXT NULL,
                create_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                update_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                KEY idx_activity_award_serial_no (serial_no),
                KEY idx_activity_award_category (award_category),
                KEY idx_activity_award_item (activity_item),
                KEY idx_activity_award_name (award_name)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
            """
        )
        cursor.execute("SHOW COLUMNS FROM activity_award_master LIKE 'serial_no'")
        serial_col = cursor.fetchone()
        if not serial_col:
            cursor.execute("ALTER TABLE activity_award_master ADD COLUMN serial_no INT NULL")
        with contextlib.suppress(Exception):
            cursor.execute("ALTER TABLE activity_award_master ADD KEY idx_activity_award_serial_no (serial_no)")
        cursor.execute(
            """
            UPDATE activity_award_master
            SET serial_no = id
            WHERE serial_no IS NULL OR serial_no <= 0
            """
        )
        with contextlib.suppress(Exception):
            cursor.execute("ALTER TABLE activity_award_master MODIFY COLUMN serial_no INT NOT NULL")
        db.conn.commit()
    finally:
        cursor.close()
        db.close()


def _utc_naive_to_tpe_naive(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(LOCAL_TZ).replace(tzinfo=None)
    return value.replace(tzinfo=ZoneInfo("UTC")).astimezone(LOCAL_TZ).replace(tzinfo=None)


def backfill_file_time_timezone(limit: int = 5000, apply_changes: bool = False):
    db, cursor = get_db_cursor()
    run_id = f"tzfix_{_now_tpe().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:6]}"
    preview_rows: list[dict] = []
    backup_rows: list[dict] = []
    updated_img_upload = 0
    updated_reco_result = 0
    skipped_marked = 0
    scanned = 0
    try:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS maintenance_backfill_audit (
                id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                task_name VARCHAR(64) NOT NULL,
                target_table VARCHAR(64) NOT NULL,
                target_id BIGINT NOT NULL,
                old_value VARCHAR(32) NULL,
                new_value VARCHAR(32) NULL,
                run_id VARCHAR(64) NOT NULL,
                create_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY uq_task_target (task_name, target_table, target_id)
            )
            """
        )
        db.conn.commit()

        cursor.execute(
            """
            SELECT iu.id, iu.photo_file_time, iu.create_time
            FROM img_upload iu
            LEFT JOIN maintenance_backfill_audit mba
              ON mba.task_name = 'tzfix_file_time_v1'
             AND mba.target_table = 'img_upload'
             AND mba.target_id = iu.id
            WHERE IFNULL(iu.is_deleted, 0) = 0
              AND iu.taken_time_source = 'FILE_TIME'
              AND iu.photo_file_time IS NOT NULL
              AND mba.id IS NULL
            ORDER BY iu.id ASC
            LIMIT %s
            """,
            (limit,),
        )
        img_rows = cursor.fetchall()

        cursor.execute(
            """
            SELECT rr.id, rr.photo_file_time, rr.create_time
            FROM reco_result rr
            LEFT JOIN maintenance_backfill_audit mba
              ON mba.task_name = 'tzfix_file_time_v1'
             AND mba.target_table = 'reco_result'
             AND mba.target_id = rr.id
            WHERE IFNULL(rr.is_deleted, 0) = 0
              AND rr.taken_time_source = 'FILE_TIME'
              AND rr.photo_file_time IS NOT NULL
              AND mba.id IS NULL
            ORDER BY rr.id ASC
            LIMIT %s
            """,
            (limit,),
        )
        reco_rows = cursor.fetchall()

        scanned = len(img_rows) + len(reco_rows)

        for row in img_rows:
            original = row.get("photo_file_time")
            corrected = _utc_naive_to_tpe_naive(_to_naive_datetime(original))
            if corrected is None:
                continue
            preview_rows.append(
                {
                    "table_name": "img_upload",
                    "id": row["id"],
                    "old_photo_file_time": str(original),
                    "new_photo_file_time": corrected.strftime("%Y-%m-%d %H:%M:%S"),
                    "create_time": str(row.get("create_time") or ""),
                }
            )
            if apply_changes:
                cursor.execute(
                    "UPDATE img_upload SET photo_file_time = %s, update_time = NOW() WHERE id = %s",
                    (corrected, row["id"]),
                )
                cursor.execute(
                    """
                    INSERT INTO maintenance_backfill_audit
                    (task_name, target_table, target_id, old_value, new_value, run_id)
                    VALUES ('tzfix_file_time_v1', 'img_upload', %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE run_id = VALUES(run_id)
                    """,
                    (row["id"], str(original), corrected.strftime("%Y-%m-%d %H:%M:%S"), run_id),
                )
                updated_img_upload += 1

        for row in reco_rows:
            original = row.get("photo_file_time")
            corrected = _utc_naive_to_tpe_naive(_to_naive_datetime(original))
            if corrected is None:
                continue
            preview_rows.append(
                {
                    "table_name": "reco_result",
                    "id": row["id"],
                    "old_photo_file_time": str(original),
                    "new_photo_file_time": corrected.strftime("%Y-%m-%d %H:%M:%S"),
                    "create_time": str(row.get("create_time") or ""),
                }
            )
            if apply_changes:
                cursor.execute(
                    "UPDATE reco_result SET photo_file_time = %s, update_time = NOW() WHERE id = %s",
                    (corrected, row["id"]),
                )
                cursor.execute(
                    """
                    INSERT INTO maintenance_backfill_audit
                    (task_name, target_table, target_id, old_value, new_value, run_id)
                    VALUES ('tzfix_file_time_v1', 'reco_result', %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE run_id = VALUES(run_id)
                    """,
                    (row["id"], str(original), corrected.strftime("%Y-%m-%d %H:%M:%S"), run_id),
                )
                updated_reco_result += 1

        if apply_changes:
            db.conn.commit()
            timestamp = _now_tpe().strftime("%Y%m%d_%H%M%S")
            backup_path = LOG_DIR / f"backfill_file_time_timezone_{timestamp}_{run_id}.csv"
            with backup_path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=["table_name", "id", "old_photo_file_time", "new_photo_file_time", "create_time"],
                )
                writer.writeheader()
                for item in preview_rows:
                    writer.writerow(item)
            log_path = LOG_DIR / f"backfill_file_time_timezone_{_now_tpe().strftime('%Y%m%d')}.log"
            with log_path.open("a", encoding="utf-8-sig", newline="\n") as log_handle:
                log_handle.write(
                    f"{_now_tpe().strftime('%Y-%m-%d %H:%M:%S')} run_id={run_id} "
                    f"updated_img_upload={updated_img_upload} updated_reco_result={updated_reco_result} "
                    f"scanned={scanned}\n"
                )
        else:
            backup_path = None

        cursor.execute(
            """
            SELECT COUNT(*) AS c
            FROM maintenance_backfill_audit
            WHERE task_name = 'tzfix_file_time_v1'
            """
        )
        marked_row = cursor.fetchone() or {"c": 0}
        skipped_marked = int(marked_row.get("c") or 0)
        sample = preview_rows[:10]
        return {
            "mode": "apply" if apply_changes else "preview",
            "run_id": run_id,
            "scanned": scanned,
            "candidate_count": len(preview_rows),
            "updated_img_upload": updated_img_upload,
            "updated_reco_result": updated_reco_result,
            "already_marked_total": skipped_marked,
            "backup_csv": str(backup_path) if backup_path else "",
            "sample": sample,
        }
    finally:
        cursor.close()
        db.close()


def query_activity_award_master(keyword: str = "", limit: int = 200):
    db, cursor = get_db_cursor()
    try:
        sql = """
            SELECT id, serial_no, award_category, activity_item, mapped_award, award_name, note, create_time, update_time
            FROM activity_award_master
        """
        params = []
        if keyword.strip():
            sql += """
                WHERE award_category LIKE %s
                   OR activity_item LIKE %s
                   OR mapped_award LIKE %s
                   OR award_name LIKE %s
                   OR note LIKE %s
            """
            pattern = f"%{keyword.strip()}%"
            params.extend([pattern, pattern, pattern, pattern, pattern])
        sql += " ORDER BY serial_no ASC, id ASC LIMIT %s"
        params.append(int(limit))
        cursor.execute(sql, tuple(params))
        rows = cursor.fetchall() or []
        return normalize_record_rows(rows)
    finally:
        cursor.close()
        db.close()


def import_activity_award_master(
    excel_path: str,
    sheet_name: str,
    serial_no_column: str,
    category_column: str,
    activity_item_column: str,
    mapped_award_column: str,
    award_name_column: str,
    note_column: str,
):
    path_obj = Path(excel_path)
    if not path_obj.exists():
        raise RuntimeError("找不到 Excel 檔案，請重新上傳後再匯入。")

    if path_obj.suffix.lower() == ".csv":
        df = pd.read_csv(path_obj)
    else:
        read_sheet = sheet_name if sheet_name and sheet_name != "CSV" else 0
        df = pd.read_excel(path_obj, sheet_name=read_sheet)

    for required_col in (category_column, activity_item_column, award_name_column):
        if required_col not in df.columns:
            raise RuntimeError(f"Excel 缺少必要欄位：{required_col}")

    db, cursor = get_db_cursor()
    imported_count = 0
    try:
        sql = """
            INSERT INTO activity_award_master (serial_no, award_category, activity_item, mapped_award, award_name, note)
            VALUES (%s, %s, %s, %s, %s, %s)
        """
        cursor.execute("SELECT COALESCE(MAX(serial_no), 0) AS max_serial_no FROM activity_award_master")
        max_serial_no_row = cursor.fetchone() or {}
        next_serial_no = int(max_serial_no_row.get("max_serial_no") or 0)
        for _, row in df.iterrows():
            award_category = str(row.get(category_column, "")).strip()
            activity_item = str(row.get(activity_item_column, "")).strip()
            award_name = str(row.get(award_name_column, "")).strip()
            if not award_category or not activity_item or not award_name:
                continue
            serial_no = None
            if serial_no_column:
                raw_serial_no = row.get(serial_no_column, "")
                if pd.notna(raw_serial_no):
                    serial_text = str(raw_serial_no).strip()
                    if serial_text:
                        try:
                            serial_no = int(float(serial_text))
                        except Exception:
                            serial_no = None
            if serial_no is None or serial_no <= 0:
                next_serial_no += 1
                serial_no = next_serial_no
            else:
                next_serial_no = max(next_serial_no, serial_no)
            mapped_award = str(row.get(mapped_award_column, "")).strip() if mapped_award_column else ""
            note = str(row.get(note_column, "")).strip() if note_column else ""
            cursor.execute(sql, (int(serial_no), award_category, activity_item, mapped_award, award_name, note))
            imported_count += 1
        db.conn.commit()
        return {"imported_count": imported_count}
    finally:
        cursor.close()
        db.close()


def update_activity_award_master(item_id: int, serial_no: int, award_category: str, activity_item: str, mapped_award: str, award_name: str, note: str):
    db, cursor = get_db_cursor()
    try:
        cursor.execute(
            """
            UPDATE activity_award_master
            SET serial_no = %s,
                award_category = %s,
                activity_item = %s,
                mapped_award = %s,
                award_name = %s,
                note = %s
            WHERE id = %s
            """,
            (int(serial_no), award_category.strip(), activity_item.strip(), (mapped_award or "").strip(), award_name.strip(), (note or "").strip(), int(item_id)),
        )
        db.conn.commit()
        return {"updated_count": int(cursor.rowcount or 0)}
    finally:
        cursor.close()
        db.close()


def delete_activity_award_master(item_id: int):
    db, cursor = get_db_cursor()
    try:
        cursor.execute("DELETE FROM activity_award_master WHERE id = %s", (int(item_id),))
        db.conn.commit()
        return {"deleted_count": int(cursor.rowcount or 0)}
    finally:
        cursor.close()
        db.close()


def delete_all_activity_award_master():
    db, cursor = get_db_cursor()
    try:
        cursor.execute("DELETE FROM activity_award_master")
        db.conn.commit()
        return {"deleted_count": int(cursor.rowcount or 0)}
    finally:
        cursor.close()
        db.close()


@app.get("/activity-awards/query")
async def activity_award_query(keyword: str = Query(""), limit: int = Query(200, ge=1, le=1000)):
    try:
        ensure_activity_tables()
        ensure_activity_award_table()
        items = query_activity_award_master(keyword=keyword, limit=limit)
        return {"total": len(items), "items": items}
    except Exception as e:
        logger.error(f"活動獎項查詢失敗: {str(e)}")
        return JSONResponse(status_code=500, content={"detail": f"活動獎項查詢失敗：{str(e)}"})


@app.post("/activity-awards/import-excel")
async def activity_award_import_excel(payload: ActivityAwardImportPayload):
    try:
        ensure_activity_tables()
        ensure_activity_award_table()
        return import_activity_award_master(
            excel_path=payload.excel_path,
            sheet_name=payload.sheet_name,
            serial_no_column=payload.serial_no_column,
            category_column=payload.category_column,
            activity_item_column=payload.activity_item_column,
            mapped_award_column=payload.mapped_award_column,
            award_name_column=payload.award_name_column,
            note_column=payload.note_column,
        )
    except Exception as e:
        logger.error(f"活動獎項 Excel 匯入失敗: {str(e)}")
        return JSONResponse(status_code=500, content={"detail": f"活動獎項 Excel 匯入失敗：{str(e)}"})


@app.post("/activity-awards/update")
async def activity_award_update(payload: ActivityAwardUpdatePayload):
    try:
        ensure_activity_tables()
        ensure_activity_award_table()
        return update_activity_award_master(
            item_id=payload.id,
            serial_no=payload.serial_no,
            award_category=payload.award_category,
            activity_item=payload.activity_item,
            mapped_award=payload.mapped_award,
            award_name=payload.award_name,
            note=payload.note,
        )
    except Exception as e:
        logger.error(f"活動獎項更新失敗: {str(e)}")
        return JSONResponse(status_code=500, content={"detail": f"活動獎項更新失敗：{str(e)}"})


@app.post("/activity-awards/delete")
async def activity_award_delete(payload: ActivityAwardDeletePayload):
    try:
        ensure_activity_tables()
        ensure_activity_award_table()
        return delete_activity_award_master(payload.id)
    except Exception as e:
        logger.error(f"活動獎項刪除失敗: {str(e)}")
        return JSONResponse(status_code=500, content={"detail": f"活動獎項刪除失敗：{str(e)}"})


@app.post("/activity-awards/delete-all")
async def activity_award_delete_all():
    try:
        ensure_activity_tables()
        ensure_activity_award_table()
        return delete_all_activity_award_master()
    except Exception as e:
        logger.error(f"活動獎項全部刪除失敗: {str(e)}")
        return JSONResponse(status_code=500, content={"detail": f"活動獎項全部刪除失敗：{str(e)}"})


if MULTIPART_AVAILABLE:
    async def save_upload_file(upload: UploadFile, target_path: Path):
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with target_path.open("wb") as buffer:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                buffer.write(chunk)
        await upload.close()


def update_person_and_history(payload: PersonUpdatePayload):
    db, cursor = get_db_cursor()

    try:
        cursor.execute(
            """
            SELECT id, dept, year, COALESCE(team, '') AS team, COALESCE(name, '') AS name
            FROM base
            WHERE id = %s
            """,
            (payload.id,),
        )
        current = cursor.fetchone()
        if not current:
            raise HTTPException(status_code=404, detail="找不到對應的 base 資料")

        old_label = build_person_label(
            current["dept"], current["year"], current["team"], current["name"]
        )
        new_label = build_person_label(
            payload.dept, payload.year, payload.team, payload.name
        )

        cursor.execute(
            """
            UPDATE base
            SET dept = %s,
                year = %s,
                team = %s,
                name = %s,
                update_time = NOW()
            WHERE id = %s
            """,
            (payload.dept, payload.year, payload.team, payload.name, payload.id),
        )

        updated_history_count = 0
        if old_label != new_label:
            updated_history_count = update_history_labels(cursor, old_label, new_label)

        db.conn.commit()
    except Exception:
        db.conn.rollback()
        raise
    finally:
        cursor.close()
        db.close()

    started = time.perf_counter()
    rebuild_face_embeddings()
    rebuild_seconds = round(time.perf_counter() - started, 2)

    return {
        "old_label": old_label,
        "new_label": new_label,
        "updated_history_count": updated_history_count,
        "embedding_rebuilt": True,
        "rebuild_seconds": rebuild_seconds,
    }


def delete_person_and_history(person_id: int):
    db, cursor = get_db_cursor()

    try:
        cursor.execute(
            """
            SELECT
                id,
                dept,
                year,
                COALESCE(team, '') AS team,
                COALESCE(name, '') AS name,
                COALESCE(file_path, '') AS file_path,
                COALESCE(file_name, '') AS file_name
            FROM base
            WHERE id = %s
            """,
            (person_id,),
        )
        current = cursor.fetchone()
        if not current:
            raise HTTPException(status_code=404, detail="找不到要刪除的 base 資料")

        old_label = build_person_label(
            current["dept"], current["year"], current["team"], current["name"]
        )
        deleted_file = os.path.join(current["file_path"], current["file_name"]).replace("\\", "/")

        updated_history_count = remove_history_label(cursor, old_label)

        cursor.execute(
            """
            DELETE FROM face_embedding_meta
            WHERE base_id = %s
              AND model_name = %s
            """,
            (person_id, EMBEDDING_MODEL_NAME),
        )
        deleted_meta_count = cursor.rowcount

        cursor.execute("DELETE FROM base WHERE id = %s", (person_id,))
        deleted_base_count = cursor.rowcount

        db.conn.commit()
    except Exception:
        db.conn.rollback()
        raise
    finally:
        cursor.close()
        db.close()

    backup_path = backup_embedding_file("admin_delete_person")
    started = time.perf_counter()
    embedding_count = rebuild_face_embeddings()
    rebuild_seconds = round(time.perf_counter() - started, 2)

    return {
        "deleted_id": person_id,
        "deleted_label": old_label,
        "deleted_file": deleted_file,
        "deleted_base_count": deleted_base_count,
        "deleted_meta_count": deleted_meta_count,
        "updated_history_count": updated_history_count,
        "embedding_count": embedding_count,
        "embedding_rebuilt": True,
        "rebuild_seconds": rebuild_seconds,
        "backup_path": backup_path,
    }


def delete_all_people_and_history():
    db, cursor = get_db_cursor()

    try:
        cursor.execute(
            """
            SELECT
                id,
                dept,
                year,
                COALESCE(team, '') AS team,
                COALESCE(name, '') AS name
            FROM base
            ORDER BY id ASC
            """
        )
        current_rows = cursor.fetchall()

        deleted_base_count = len(current_rows)
        if deleted_base_count == 0:
            return {
                "deleted_base_count": 0,
                "deleted_meta_count": 0,
                "updated_history_count": 0,
                "embedding_count": len(load_runtime_embeddings()),
                "embedding_rebuilt": False,
                "rebuild_seconds": 0,
                "backup_path": "",
            }

        updated_history_count = 0
        for row in current_rows:
            person_label = build_person_label(
                row["dept"], row["year"], row["team"], row["name"]
            )
            updated_history_count += remove_history_label(cursor, person_label)

        cursor.execute(
            """
            DELETE FROM face_embedding_meta
            WHERE model_name = %s
            """,
            (EMBEDDING_MODEL_NAME,),
        )
        deleted_meta_count = cursor.rowcount

        cursor.execute("DELETE FROM base")
        cursor.rowcount

        db.conn.commit()
    except Exception:
        db.conn.rollback()
        raise
    finally:
        cursor.close()
        db.close()

    backup_path = backup_embedding_file("admin_delete_all_people")
    started = time.perf_counter()
    embedding_count = rebuild_face_embeddings()
    rebuild_seconds = round(time.perf_counter() - started, 2)

    return {
        "deleted_base_count": deleted_base_count,
        "deleted_meta_count": deleted_meta_count,
        "updated_history_count": updated_history_count,
        "embedding_count": embedding_count,
        "embedding_rebuilt": True,
        "rebuild_seconds": rebuild_seconds,
        "backup_path": backup_path,
    }


def delete_filtered_people_and_history(person_ids: list[int]):
    unique_ids = sorted({int(item) for item in person_ids if int(item) > 0})
    if not unique_ids:
        return {
            "deleted_base_count": 0,
            "deleted_meta_count": 0,
            "updated_history_count": 0,
            "embedding_count": len(load_runtime_embeddings()),
            "embedding_rebuilt": False,
            "rebuild_seconds": 0,
            "backup_path": "",
        }

    db, cursor = get_db_cursor()
    placeholders = ", ".join(["%s"] * len(unique_ids))
    try:
        cursor.execute(
            f"""
            SELECT
                id,
                dept,
                year,
                COALESCE(team, '') AS team,
                COALESCE(name, '') AS name
            FROM base
            WHERE id IN ({placeholders})
            ORDER BY id ASC
            """,
            tuple(unique_ids),
        )
        current_rows = cursor.fetchall()
        deleted_base_count = len(current_rows)
        if deleted_base_count == 0:
            return {
                "deleted_base_count": 0,
                "deleted_meta_count": 0,
                "updated_history_count": 0,
                "embedding_count": len(load_runtime_embeddings()),
                "embedding_rebuilt": False,
                "rebuild_seconds": 0,
                "backup_path": "",
            }

        existing_ids = [int(row["id"]) for row in current_rows]
        existing_placeholders = ", ".join(["%s"] * len(existing_ids))

        updated_history_count = 0
        for row in current_rows:
            person_label = build_person_label(row["dept"], row["year"], row["team"], row["name"])
            updated_history_count += remove_history_label(cursor, person_label)

        cursor.execute(
            f"""
            DELETE FROM face_embedding_meta
            WHERE model_name = %s
              AND base_id IN ({existing_placeholders})
            """,
            (EMBEDDING_MODEL_NAME, *existing_ids),
        )
        deleted_meta_count = cursor.rowcount

        cursor.execute(
            f"DELETE FROM base WHERE id IN ({existing_placeholders})",
            tuple(existing_ids),
        )
        db.conn.commit()
    except Exception:
        db.conn.rollback()
        raise
    finally:
        cursor.close()
        db.close()

    backup_path = backup_embedding_file("admin_delete_filtered_people")
    started = time.perf_counter()
    embedding_count = rebuild_face_embeddings()
    rebuild_seconds = round(time.perf_counter() - started, 2)

    return {
        "deleted_base_count": deleted_base_count,
        "deleted_meta_count": deleted_meta_count,
        "updated_history_count": updated_history_count,
        "embedding_count": embedding_count,
        "embedding_rebuilt": True,
        "rebuild_seconds": rebuild_seconds,
        "backup_path": backup_path,
    }


def resolve_preview_path(file_path: str):
    if not file_path:
        raise HTTPException(status_code=404, detail="找不到圖片路徑")

    normalized = os.path.normpath(file_path).replace("\\", "/")
    if not any(normalized.startswith(prefix) for prefix in ALLOWED_PREVIEW_PREFIXES):
        raise HTTPException(status_code=403, detail="不允許讀取這個路徑")

    candidates = [normalized]
    fallback_thumb = build_thumbnail_candidate(normalized)
    if fallback_thumb:
        candidates.insert(0, fallback_thumb)

    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate

    raise HTTPException(status_code=404, detail="找不到圖片檔案")


@app.get("/legacy-home", response_class=HTMLResponse)
async def legacy_home():
    return HTMLResponse(render_clean_home_html(), headers=html_no_cache_headers())

@app.get("/", response_class=HTMLResponse)
async def home():
    return HTMLResponse(render_clean_home_html())

@app.get("/laptop-tool-upload-monitor", response_class=HTMLResponse)
async def laptop_tool_upload_monitor():
    return HTMLResponse(render_laptop_tool_upload_monitor_html(), headers=html_no_cache_headers())

@app.get("/laptop-tool/upload-monitor/data")
async def laptop_tool_upload_monitor_data(
    limit: int = Query(30, ge=1, le=100),
    device_id: str = Query(""),
    active_only: bool = Query(False),
):
    db = None
    cursor = None
    try:
        ensure_laptop_tool_tables()
        db = mysqlconnector()
        db.connect()
        if db.conn is None:
            raise RuntimeError("資料庫連線失敗")
        cursor = db.conn.cursor(dictionary=True)
        snapshot = _fetch_laptop_tool_upload_monitor_snapshot(
            cursor,
            device_id=device_id,
            active_only=active_only,
            limit=limit,
        )
        return snapshot
    except Exception as exc:
        logger.error("讀取工具程式上傳監看資料失敗: %s", exc)
        return JSONResponse(status_code=500, content={"detail": f"讀取工具程式上傳監看資料失敗：{exc}"})
    finally:
        with contextlib.suppress(Exception):
            if cursor:
                cursor.close()
        with contextlib.suppress(Exception):
            if db:
                db.close()

@app.get("/query-ui-advanced", response_class=HTMLResponse)
async def query_ui_advanced():
    return HTMLResponse(load_ui_template("query_ui_advanced.html"), headers=html_no_cache_headers())

@app.get("/admin-ui", response_class=HTMLResponse)
async def admin_ui():
    return HTMLResponse(load_ui_template("admin_ui.html"), headers=html_no_cache_headers())

@app.get("/admin-batch-ui", response_class=HTMLResponse)
async def admin_batch_ui():
    return HTMLResponse(load_ui_template("admin_batch_ui.html"), headers=html_no_cache_headers())


@app.get("/query-ui-advanced-inline-legacy", response_class=HTMLResponse)
async def query_ui_advanced_inline_legacy():
    return HTMLResponse(load_ui_template("query_ui_advanced.html"), headers=html_no_cache_headers())

@app.get("/admin-ui-legacy", response_class=HTMLResponse)
async def admin_ui_legacy():
    return HTMLResponse(load_ui_template("admin_ui.html"), headers=html_no_cache_headers())

@app.get("/admin-ui-inline-legacy", response_class=HTMLResponse)
async def admin_ui_inline_legacy():
    return HTMLResponse(load_ui_template("admin_ui.html"), headers=html_no_cache_headers())

@app.get("/admin-batch-ui-legacy", response_class=HTMLResponse)
async def admin_batch_ui_legacy():
    return HTMLResponse(load_ui_template("admin_batch_ui.html"), headers=html_no_cache_headers())

@app.get("/preview-image")
async def preview_image(file_path: str = Query(..., min_length=1)):
    resolved_path = resolve_preview_path(file_path)
    return FileResponse(resolved_path)


@app.get("/preview-annotated-image")
async def preview_annotated_image(img_upload_id: int = Query(..., ge=1)):
    ensure_activity_tables()
    ensure_recognition_soft_delete_schema()
    db, cursor = get_db_cursor()
    try:
        cursor.execute(
            """
            SELECT
                iu.id AS img_upload_id,
                COALESCE(rr.origin_full_path, iu.origin_full_path) AS origin_full_path,
                COALESCE(rr.reco_name, '[]') AS reco_name,
                COALESCE(rr.reco_res, '[]') AS reco_res
            FROM img_upload iu
            LEFT JOIN reco_result rr
              ON (
                (rr.photo_uuid IS NOT NULL AND rr.photo_uuid = iu.photo_uuid)
                OR rr.origin_full_path = iu.origin_full_path
              )
              AND COALESCE(rr.is_deleted, 0) = 0
            WHERE iu.id = %s AND COALESCE(iu.is_deleted, 0) = 0
            ORDER BY rr.update_time DESC, rr.id DESC
            LIMIT 1
            """,
            (int(img_upload_id),),
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="找不到指定照片資料。")
        normalized_rows = normalize_record_rows([row])
        item = normalized_rows[0] if normalized_rows else {}
        reco_res = item.get("reco_res") if isinstance(item.get("reco_res"), list) else []
        reco_names = item.get("reco_name") if isinstance(item.get("reco_name"), list) else []
        if not reco_res:
            raise HTTPException(status_code=404, detail="此照片沒有可標註的人臉辨識結果。")

        resolved_path = resolve_preview_path(str(item.get("origin_full_path") or ""))
        image = Image.open(resolved_path).convert("RGB")
        draw = ImageDraw.Draw(image)

        for idx, entry in enumerate(reco_res):
            if not isinstance(entry, dict):
                continue
            bbox = entry.get("bbox")
            if not isinstance(bbox, (list, tuple)) or len(bbox) < 4:
                continue
            try:
                x1, y1, x2, y2 = [int(float(v)) for v in bbox[:4]]
            except (TypeError, ValueError):
                continue
            label_name = str(idx + 1)
            draw.rectangle([(x1, y1), (x2, y2)], outline=(255, 64, 64), width=3)
            text_y = y1 - 18 if y1 > 20 else y1 + 4
            draw.rectangle([(x1, text_y - 2), (x1 + max(len(label_name), 4) * 8, text_y + 14)], fill=(255, 64, 64))
            draw.text((x1 + 2, text_y), label_name, fill=(255, 255, 255))

        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=92)
        buffer.seek(0)
        return StreamingResponse(buffer, media_type="image/jpeg")
    finally:
        cursor.close()
        db.close()


@app.get(WINDOWS_BATCH_SERVICE_GUIDE_PAGE)
async def windows_batch_service_guide_page():
    html = f"""
    <!DOCTYPE html>
    <html lang="zh-Hant">
    <head>
      <meta charset="utf-8" />
      <meta name="viewport" content="width=device-width, initial-scale=1" />
      <title>Windows 服務啟動指引</title>
      <style>
        body {{ margin:0; font-family:"Segoe UI","Noto Sans TC",sans-serif; background:#f4efe7; color:#1f2937; }}
        .wrap {{ max-width:920px; margin:0 auto; padding:32px 20px 48px; }}
        .panel {{ background:#fffdfa; border:1px solid #d8cfbf; border-radius:18px; padding:24px; box-shadow:0 18px 46px rgba(41,33,18,.08); }}
        h1 {{ margin:0 0 12px; font-size:30px; }}
        p, li {{ line-height:1.7; color:#4b5563; }}
        code {{ background:#f8f5ee; border:1px solid #e6dccb; border-radius:8px; padding:2px 6px; }}
        .code {{ margin-top:12px; padding:14px 16px; background:#f8f5ee; border:1px solid #e6dccb; border-radius:12px; word-break:break-all; font-family:Consolas,"Courier New",monospace; }}
        .actions {{ display:flex; flex-wrap:wrap; gap:12px; margin-top:20px; }}
        .btn {{ display:inline-flex; align-items:center; justify-content:center; padding:12px 18px; border-radius:999px; text-decoration:none; color:#fff; background:linear-gradient(135deg,#92400e,#0f766e); }}
        .btn.secondary {{ background:#fffdfa; color:#1f2937; border:1px solid #d8cfbf; }}
      </style>
    </head>
    <body>
      <div class="wrap">
        <div class="panel">
          <h1>Windows 服務啟動指引</h1>
          <p>此頁用於啟動本機 8010 Windows 批次服務（<code>start_windows_batch_service.ps1</code>）。</p>
          <div class="code">{WINDOWS_BATCH_SERVICE_PS1}</div>
          <ol>
            <li>以系統管理員身分開啟 PowerShell。</li>
            <li>切換到 noob 專案目錄，執行 <code>.\\start_windows_batch_service.ps1</code>。</li>
            <li>啟動後，開啟 <code>http://127.0.0.1:8010/health</code> 確認服務可連線。</li>
          </ol>
          <p>常見問題：若無法啟動，請確認 8010 埠未被占用、Python 與 uvicorn 可執行、目前帳號有目錄讀寫權限。</p>
          <div class="actions">
            <a class="btn" href="{WINDOWS_BATCH_SERVICE_GUIDE_DOWNLOAD}">下載 start_windows_batch_service.ps1</a>
            <a class="btn secondary" href="/activity-photo-normalize-ui">回活動照片正規化處理</a>
            <a class="btn secondary" href="/">回首頁</a>
          </div>
        </div>
      </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html, headers=html_no_cache_headers())


@app.get(WINDOWS_BATCH_SERVICE_GUIDE_DOWNLOAD)
async def windows_batch_service_guide_download():
    script_path = Path(WINDOWS_BATCH_SERVICE_PS1)
    if not script_path.exists():
        raise HTTPException(status_code=404, detail="找不到 start_windows_batch_service.ps1")
    return FileResponse(script_path, filename=script_path.name, media_type="application/octet-stream")


@app.get(WINDOWS_NORMALIZE_TOOL_PAGE)
async def windows_normalize_tool_page():
    html = load_ui_template("windows_normalize_tool.html")
    html = html.replace("__TOOL_PATH__", WINDOWS_NORMALIZE_TOOL_BAT)
    html = html.replace("__DOWNLOAD_URL__", WINDOWS_NORMALIZE_TOOL_DOWNLOAD)
    return HTMLResponse(content=html, headers=html_no_cache_headers())


@app.get(WINDOWS_NORMALIZE_TOOL_DOWNLOAD)
async def windows_normalize_tool_download():
    tool_path = Path(WINDOWS_NORMALIZE_TOOL_BAT)
    if not tool_path.exists():
        raise HTTPException(status_code=404, detail="找不到 start_windows_normalize_tool.bat")
    return FileResponse(tool_path, filename=tool_path.name, media_type="application/octet-stream")


@app.get(WINDOWS_ACTIVITY_IMPORT_TOOL_PAGE)
async def windows_activity_import_tool_page():
    html = f"""
    <!DOCTYPE html>
    <html lang="zh-Hant">
    <head>
      <meta charset="utf-8" />
      <meta name="viewport" content="width=device-width, initial-scale=1" />
      <title>Windows 活動照片匯入工具</title>
      <style>
        body {{ margin:0; font-family:"Segoe UI","Noto Sans TC",sans-serif; background:#f4efe7; color:#1f2937; }}
        .wrap {{ max-width:920px; margin:0 auto; padding:36px 20px; }}
        .panel {{ background:#fffdfa; border:1px solid #d8cfbf; border-radius:20px; padding:24px; }}
        h1 {{ margin:0 0 8px; font-size:30px; }}
        p {{ color:#6b7280; line-height:1.7; }}
        .actions {{ display:flex; flex-wrap:wrap; gap:12px; margin-top:16px; }}
        a {{ text-decoration:none; padding:12px 16px; border-radius:12px; background:linear-gradient(135deg,#92400e,#0f766e); color:#fff; font-weight:700; }}
        .hint {{ margin-top:12px; background:#eef8f6; border-radius:12px; padding:12px 14px; color:#0f766e; }}
      </style>
    </head>
    <body>
      <div class="wrap">
        <div class="panel">
          <h1>Windows 活動照片匯入工具</h1>
          <p>此工具提供本機模式操作，流程比照活動照片匯入頁：先做檔名正規化，再執行匯入入庫與後續辨識。</p>
          <div class="actions">
            <a href="{WINDOWS_ACTIVITY_IMPORT_TOOL_DOWNLOAD}">下載啟動器</a>
            <a href="/activity-photo-import-ui">回到網頁版匯入頁</a>
            <a href="/">回首頁</a>
          </div>
          <div class="hint">下載後請執行 <code>start_windows_activity_import_tool.bat</code>。若檔案不存在，請先通知管理員部署工具。</div>
        </div>
      </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html, headers=html_no_cache_headers())


@app.get(WINDOWS_ACTIVITY_IMPORT_TOOL_DOWNLOAD)
async def windows_activity_import_tool_download():
    tool_path = Path(WINDOWS_ACTIVITY_IMPORT_TOOL_BAT)
    if not tool_path.exists():
        raise HTTPException(status_code=404, detail="找不到 start_windows_activity_import_tool.bat")
    return FileResponse(tool_path, filename=tool_path.name, media_type="application/octet-stream")


@app.get("/query-filter-options")
async def get_query_filter_options(
    dept: str = Query("", description="系所"),
    year: int | None = Query(None, description="級別"),
):
    try:
        return query_filter_options(dept=dept, year=year)
    except Exception as e:
        logger.error(f"讀取查詢條件失敗: {str(e)}")
        return JSONResponse(status_code=500, content={"detail": "讀取查詢條件失敗"})


@app.post("/admin/backfill-photo-file-time")
async def admin_backfill_photo_file_time(limit: int = Query(2000, ge=1, le=20000)):
    try:
        result = backfill_img_upload_photo_file_time(limit=limit)
        return result
    except Exception as e:
        logger.error(f"回填 photo_file_time 失敗: {str(e)}")
        return JSONResponse(status_code=500, content={"detail": f"回填 photo_file_time 失敗：{str(e)}"})


@app.post("/admin/backfill-file-time-timezone/preview")
async def admin_backfill_file_time_timezone_preview(limit: int = Query(5000, ge=1, le=50000)):
    try:
        return backfill_file_time_timezone(limit=limit, apply_changes=False)
    except Exception as e:
        logger.error(f"預覽回填 FILE_TIME 時區失敗: {str(e)}")
        return JSONResponse(status_code=500, content={"detail": f"預覽回填 FILE_TIME 時區失敗：{str(e)}"})


@app.post("/admin/backfill-file-time-timezone/apply")
async def admin_backfill_file_time_timezone_apply(limit: int = Query(5000, ge=1, le=50000)):
    try:
        return backfill_file_time_timezone(limit=limit, apply_changes=True)
    except Exception as e:
        logger.error(f"執行回填 FILE_TIME 時區失敗: {str(e)}")
        return JSONResponse(status_code=500, content={"detail": f"執行回填 FILE_TIME 時區失敗：{str(e)}"})


@app.post("/admin/backfill-photo-uuid")
async def admin_backfill_photo_uuid(limit: int = Query(2000, ge=1, le=20000)):
    try:
        result = backfill_img_upload_photo_uuid(limit=limit)
        return result
    except Exception as e:
        logger.error(f"回填 photo_uuid 失敗: {str(e)}")
        return JSONResponse(status_code=500, content={"detail": f"回填 photo_uuid 失敗：{str(e)}"})


@app.post("/admin/backfill-reco-photo-meta")
async def admin_backfill_reco_photo_meta(limit: int = Query(5000, ge=1, le=50000)):
    try:
        result = backfill_reco_result_photo_meta(limit=limit)
        return result
    except Exception as e:
        logger.error(f"回填 reco_result 照片欄位失敗: {str(e)}")
        return JSONResponse(status_code=500, content={"detail": f"回填 reco_result 照片欄位失敗：{str(e)}"})


@app.post("/admin/purge-soft-deleted")
async def admin_purge_soft_deleted(
    retention_days: int = Query(30, ge=0, le=3650),
    limit: int = Query(1000, ge=1, le=20000),
):
    try:
        result = purge_soft_deleted_records(retention_days=retention_days, limit=limit)
        return result
    except Exception as e:
        logger.error(f"清理邏輯刪除資料失敗: {str(e)}")
        return JSONResponse(status_code=500, content={"detail": f"清理邏輯刪除資料失敗：{str(e)}"})


@app.post("/device/register")
async def device_register(
    client_key: str = Form(...),
    device_name: str = Form(""),
):
    try:
        return register_or_get_device(client_key=client_key, device_name=device_name)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})
    except Exception as e:
        logger.error(f"裝置註冊失敗: {str(e)}")
        return JSONResponse(status_code=500, content={"detail": f"裝置註冊失敗：{str(e)}"})


@app.get("/admin-batch-ui-inline-legacy", response_class=HTMLResponse)
async def admin_batch_ui_inline_legacy():
    return HTMLResponse(load_ui_template("admin_batch_ui.html"), headers=html_no_cache_headers())

@app.get("/admin/base-search")
async def admin_base_search(
    dept: str = Query("", description="系所"),
    year: int | None = Query(None, description="級別"),
    team: str = Query("", description="小隊"),
    name: str = Query("", description="姓名"),
    limit: int = Query(100, ge=1, le=500, description="最多顯示筆數"),
):
    try:
        items = search_base_people(dept=dept, year=year, team=team, name=name, limit=limit)
        return {"total": len(items), "items": items}
    except Exception as e:
        logger.error(f"查詢 base 人員主檔失敗: {str(e)}")
        return JSONResponse(status_code=500, content={"detail": "查詢 base 人員主檔時發生錯誤"})


@app.post("/admin/update-person")
async def admin_update_person(payload: PersonUpdatePayload):
    try:
        return update_person_and_history(payload)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"更新 base 人員主檔失敗: {str(e)}")
        return JSONResponse(status_code=500, content={"detail": "更新 base 人員主檔時發生錯誤"})


@app.post("/admin/delete-person")
async def admin_delete_person(payload: PersonDeletePayload):
    try:
        return delete_person_and_history(payload.id)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"刪除 base 人員主檔失敗: {str(e)}")
        return JSONResponse(status_code=500, content={"detail": "刪除 base 人員主檔時發生錯誤"})


@app.post("/admin/delete-all-persons")
async def admin_delete_all_persons():
    try:
        return delete_all_people_and_history()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"全部刪除 base 人員主檔失敗: {str(e)}")
        return JSONResponse(status_code=500, content={"detail": f"全部刪除 base 人員主檔失敗：{str(e)}"})


@app.post("/admin/delete-filtered-persons")
async def admin_delete_filtered_persons(payload: PersonBulkDeletePayload):
    try:
        return delete_filtered_people_and_history(payload.ids)
    except HTTPException as http_error:
        return JSONResponse(status_code=http_error.status_code, content={"detail": http_error.detail})
    except Exception as e:
        logger.error(f"刪除查詢結果 base 人員主檔失敗: {str(e)}")
        return JSONResponse(status_code=500, content={"detail": "刪除查詢結果 base 人員主檔時發生錯誤"})


@app.get("/admin/embedding-search")
async def admin_embedding_search(
    dept: str = Query("", description="系所"),
    year: int | None = Query(None, description="級別"),
    team: str = Query("", description="小隊"),
    name: str = Query("", description="姓名"),
    status: str = Query("", description="embedding 狀態"),
    limit: int = Query(100, ge=1, le=500, description="最多顯示筆數"),
):
    try:
        items = query_embedding_meta_rows(
            dept=dept,
            year=year,
            team=team,
            name=name,
            status=status,
            limit=limit,
        )
        summary = summarize_embedding_meta(
            dept=dept,
            year=year,
            team=team,
            name=name,
        )
        return {"total": len(items), "summary": summary, "items": items}
    except Exception as e:
        logger.error(f"查詢 embedding 管理資料失敗: {str(e)}")
        return JSONResponse(status_code=500, content={"detail": "查詢 embedding 管理資料時發生錯誤"})


@app.post("/admin/embedding-delete")
async def admin_embedding_delete(payload: EmbeddingDeletePayload):
    try:
        return delete_embedding_entry(payload.base_id)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"刪除 embedding 失敗: {str(e)}")
        return JSONResponse(status_code=500, content={"detail": "刪除 embedding 時發生錯誤"})


@app.post("/admin/embedding-rebuild")
async def admin_embedding_rebuild():
    try:
        backup_path = backup_embedding_file("admin_rebuild")
        started = time.perf_counter()
        embedding_count = rebuild_face_embeddings()
        rebuild_seconds = round(time.perf_counter() - started, 2)
        summary = summarize_embedding_meta()
        return {
            "embedding_count": embedding_count,
            "rebuild_seconds": rebuild_seconds,
            "backup_path": backup_path,
            "summary": summary,
        }
    except Exception as e:
        logger.error(f"重建 embedding 失敗: {str(e)}")
        return JSONResponse(status_code=500, content={"detail": "重建 embedding 時發生錯誤"})


if MULTIPART_AVAILABLE:
    @app.post("/admin/upload-excel")
    async def admin_upload_excel(file: UploadFile = File(...)):
        try:
            if not file.filename:
                raise HTTPException(status_code=400, detail="請選擇 Excel 或 CSV 檔案。")

            suffix = Path(file.filename).suffix.lower()
            if suffix not in {".xlsx", ".xls", ".xlsm", ".xltx", ".xltm", ".csv"}:
                raise HTTPException(status_code=400, detail="不支援的檔案格式，僅接受 Excel 或 CSV。")

            job_dir = create_batch_job_dir(BATCH_UPLOAD_ROOT, "excel")
            target_path = job_dir / Path(file.filename).name
            await save_upload_file(file, target_path)
            sheet_names = list_tabular_sheets(target_path)
            selected_sheet = "" if Path(file.filename).suffix.lower() == ".csv" else sheet_names[0]
            columns = load_excel_columns(target_path, sheet_name=selected_sheet)

            return {
                "filename": Path(file.filename).name,
                "server_path": str(target_path),
                "sheet_names": sheet_names,
                "selected_sheet": selected_sheet or "CSV",
                "columns": columns,
            }
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"上傳 Excel 失敗: {str(e)}")
            return JSONResponse(status_code=500, content={"detail": f"上傳 Excel 失敗：{str(e)}"})


    @app.post("/admin/upload-source-folder")
    async def admin_upload_source_folder(files: list[UploadFile] = File(...)):
        try:
            if not files:
                raise HTTPException(status_code=400, detail="請選擇來源資料夾中的檔案。")

            source_dir = create_batch_job_dir(BATCH_UPLOAD_ROOT, "source")
            suggested_destination_dir = str(BATCH_OUTPUT_ROOT / f"{source_dir.name}_normalized")
            saved_count = 0

            for upload in files:
                if not upload.filename:
                    continue
                target_path = source_dir / Path(upload.filename).name
                await save_upload_file(upload, target_path)
                saved_count += 1

            if saved_count == 0:
                raise HTTPException(status_code=400, detail="來源資料夾中沒有可用檔案。")

            return {
                "server_path": str(source_dir),
                "host_path": runtime_path_to_windows(str(source_dir)),
                "file_count": saved_count,
                "suggested_destination_dir": suggested_destination_dir,
                "suggested_destination_host_dir": runtime_path_to_windows(suggested_destination_dir),
            }
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"上傳來源資料夾失敗: {str(e)}")
            return JSONResponse(status_code=500, content={"detail": f"上傳來源資料夾失敗：{str(e)}"})


@app.get("/admin/excel-columns")
async def admin_excel_columns(
    excel_path: str = Query(..., min_length=1),
    sheet_name: str = Query("", description="工作表名稱"),
):
    try:
        sheet_names = list_tabular_sheets(excel_path)
        normalized_sheet = ""
        if sheet_names != ["CSV"]:
            normalized_sheet = sheet_name or sheet_names[0]
            if normalized_sheet not in sheet_names:
                raise ValueError(f"Excel 找不到工作表：{normalized_sheet}")

        columns = load_excel_columns(excel_path, sheet_name=normalized_sheet)
        return {
            "sheet_names": sheet_names,
            "selected_sheet": normalized_sheet or "CSV",
            "columns": columns,
        }
    except Exception as e:
        logger.error(f"讀取 Excel 欄位失敗: {str(e)}")
        return JSONResponse(status_code=500, content={"detail": f"讀取 Excel 欄位時發生錯誤：{str(e)}"})


@app.post("/admin/batch-normalize")
async def admin_batch_normalize(payload: BatchNormalizePayload):
    try:
        return normalize_headshot_batch(
            source_dir=payload.source_dir,
            destination_dir=payload.destination_dir,
            excel_path=payload.excel_path,
            sheet_name=payload.sheet_name,
            original_filename_column=payload.original_filename_column,
            filename_fields=payload.filename_fields,
            delimiter=payload.delimiter,
            extension_override=payload.extension_override,
        )
    except Exception as e:
        logger.error(f"圖檔正規化批次作業失敗: {str(e)}")
        return JSONResponse(status_code=500, content={"detail": f"圖檔正規化批次作業失敗：{str(e)}"})


@app.post("/admin/feature-build")
async def admin_feature_build(payload: FeatureBuildPayload):
    try:
        return run_feature_build_batch(payload.feature_folder_path)
    except Exception as e:
        logger.error(f"建立人員 base 資料及特徵資料作業失敗: {str(e)}")
        return JSONResponse(status_code=500, content={"detail": f"建立人員 base 資料及特徵資料作業失敗：{str(e)}"})


@app.get("/activity-schedule-ui")
async def activity_schedule_ui():
    return HTMLResponse(render_clean_activity_schedule_ui_html())


@app.get("/activity-photo-normalize-ui")
async def activity_photo_normalize_ui():
    return HTMLResponse(
        render_activity_photo_normalize_ui_html(),
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/activity-photo-import-ui")
async def activity_photo_import_ui():
    return HTMLResponse(
        render_activity_photo_import_runtime_ui_html(),
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/photographer-ui")
async def photographer_ui():
    return HTMLResponse(render_clean_photographer_ui_html())


@app.get("/activity-award-ui")
async def activity_award_ui():
    return HTMLResponse(render_clean_activity_award_ui_html())


@app.get("/activity-schedules/options")
async def activity_schedule_options():
    try:
        ensure_activity_tables()
        return {"items": list_activity_schedule_options()}
    except Exception as e:
        logger.error(f"讀取活動行程選單失敗: {str(e)}")
        return JSONResponse(status_code=500, content={"detail": f"讀取活動行程選單失敗：{str(e)}"})


@app.get("/activity-normalize-config/export")
async def export_activity_normalize_config():
    try:
        ensure_activity_tables()
        activities = list_activity_schedule_options(limit=1000)
        photographers = query_photographer_master(limit=1000)
        payload = {
            "version": "1.0",
            "exported_at": datetime.now().isoformat(timespec="seconds"),
            "activities": activities,
            "photographers": [
                {
                    "id": item.get("id"),
                    "name": item.get("photographer_name", ""),
                    "note": item.get("note", ""),
                }
                for item in photographers
            ],
            "naming_rules": {
                "mode_a_template": "EXIF_{activity_code_or_000}_{device_id}_{photographer}_{taken_yyyymmdd_hhmmss}_{origin_stem}.jpg",
                "mode_b_template": "NONEXIF_{activity_code}_{device_id}_{photographer}_{file_yyyymmdd_hhmmss}_{origin_stem}.jpg",
            },
        }
        return payload
    except Exception as e:
        logger.error(f"匯出活動正規化設定失敗: {str(e)}")
        return JSONResponse(status_code=500, content={"detail": f"匯出活動正規化設定失敗：{str(e)}"})


@app.get(LAPTOP_TOOL_ADMIN_PAGE)
async def laptop_tool_admin_page(request: Request):
    settings = _read_laptop_tool_admin_settings()
    resolved_server_base = _resolve_laptop_tool_server_base(settings, request)
    default_activity_code = str(settings.get("default_activity_code") or "").strip().upper()
    default_photographer = str(settings.get("default_photographer") or "").strip()
    package_items = []
    doc_items = []
    model_items = []
    build_version = ""
    build_time = ""
    build_note = ""
    for root, bucket in (
        (LAPTOP_TOOL_PACKAGE_ROOT, package_items),
        (LAPTOP_TOOL_DOC_ROOT, doc_items),
        (LAPTOP_TOOL_MODEL_ROOT, model_items),
    ):
        if root.exists():
            for p in sorted(root.glob("*"), key=lambda x: x.stat().st_mtime, reverse=True):
                if not p.is_file():
                    continue
                rel = f"{root.name}/{p.name}"
                bucket.append(
                    {
                        "name": p.name,
                        "size": p.stat().st_size,
                        "updated_at": datetime.fromtimestamp(p.stat().st_mtime, LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S"),
                        "url": f"{LAPTOP_TOOL_DOWNLOAD_PAGE}?bucket={root.name}&name={quote(p.name)}",
                        "rel": rel,
                    }
                )
    if LAPTOP_TOOL_DIST_ROOT.exists():
        version_path = LAPTOP_TOOL_DIST_ROOT / "version.json"
        if version_path.exists():
            try:
                version_payload = json.loads(version_path.read_text(encoding="utf-8"))
                build_version = str(version_payload.get("version") or "").strip()
                raw_build_time = str(version_payload.get("build_time") or "").strip()
                if raw_build_time:
                    with contextlib.suppress(Exception):
                        parsed_dt = datetime.fromisoformat(raw_build_time.replace("Z", "+00:00"))
                        build_time = parsed_dt.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")
                if not build_time:
                    build_time = datetime.fromtimestamp(version_path.stat().st_mtime, LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                build_note = "版本檔讀取失敗，已改用檔案時間。"
        for p in sorted(LAPTOP_TOOL_DIST_ROOT.glob("*"), key=lambda x: x.stat().st_mtime, reverse=True):
            if not p.is_file():
                continue
            package_items.append(
                {
                    "name": f"[build] {p.name}",
                    "size": p.stat().st_size,
                    "updated_at": datetime.fromtimestamp(p.stat().st_mtime, LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S"),
                    "url": f"{LAPTOP_TOOL_DOWNLOAD_PAGE}?bucket=dist&name={quote(p.name)}",
                    "rel": f"dist/laptop_tool/{p.name}",
                }
            )
    rows_html = ""
    recommended_model = next((item for item in model_items if item["name"] == LAPTOP_TOOL_RECOMMENDED_MODEL_ZIP), None)
    if not build_time and LAPTOP_TOOL_DIST_ROOT.exists():
        build_time = datetime.fromtimestamp(LAPTOP_TOOL_DIST_ROOT.stat().st_mtime, LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")
    if not build_note and not build_version:
        build_note = "未提供版本檔，已改用檔案時間。"

    preview_activities: list[dict] = []
    preview_photographers: list[dict] = []
    preview_error = ""
    try:
        preview_payload = await export_activity_normalize_config()
        if isinstance(preview_payload, dict):
            preview_activities = list(preview_payload.get("activities") or [])
            preview_photographers = list(preview_payload.get("photographers") or [])
        elif isinstance(preview_payload, JSONResponse):
            preview_error = "活動/攝影師清單暫時無法載入。"
    except Exception as exc:
        preview_error = str(exc)

    activity_preview_html = "".join(
        f"<li><code>{html_lib.escape(str(item.get('activity_code') or '').strip())}</code> "
        f"{html_lib.escape(str(item.get('activity_date') or '').strip())} "
        f"{html_lib.escape(str(item.get('activity_time_range') or item.get('activity_time') or '').strip())} "
        f"{html_lib.escape(str(item.get('activity_content') or '').strip())}</li>"
        for item in preview_activities[:8]
        if isinstance(item, dict)
    )
    if not activity_preview_html:
        activity_preview_html = "<li>目前沒有可顯示的活動資料。</li>"

    photographer_preview_html = "".join(
        f"<li><code>{html_lib.escape(str(item.get('name') or item.get('photographer_name') or '').strip())}</code>"
        f"{' <span class=\"hint\">' + html_lib.escape(str(item.get('note') or '').strip()) + '</span>' if str(item.get('note') or '').strip() else ''}</li>"
        for item in preview_photographers[:8]
        if isinstance(item, dict)
    )
    if not photographer_preview_html:
        photographer_preview_html = "<li>目前沒有可顯示的攝影師資料。</li>"

    for title, items in (("工具程式", package_items), ("操作文件", doc_items), ("模型檔", model_items)):
        if title == "工具程式":
            meta = (
                f"<li>版本：{build_version or '未標示'}；建置時間：{build_time or '未取得'}</li>"
                f"<li>{build_note}</li>" if build_note else f"<li>版本：{build_version or '未標示'}；建置時間：{build_time or '未取得'}</li>"
            )
            links = (
                "<li><a href=\"/laptop-tool/download-package-zip\">下載完整工具程式 ZIP（建議）</a>"
                "（包含 _internal，請整包解壓後執行，不要只取單一 exe）</li>"
            )
            rows_html += f"<h3>{title}</h3><ul>{meta}{links}</ul>"
            continue
        if title == "模型檔":
            if recommended_model:
                links = (
                    f"<li><a href=\"{recommended_model['url']}\">{recommended_model['name']}</a>"
                    f"（版本：{EMBEDDING_MODEL_NAME}，{recommended_model['updated_at']}，{recommended_model['size']} bytes）</li>"
                )
                rows_html += f"<h3>{title}</h3><ul>{links}</ul>"
            else:
                rows_html += (
                    f"<h3>{title}</h3><ul>"
                    f"<li>尚未找到 {LAPTOP_TOOL_RECOMMENDED_MODEL_ZIP}，"
                    "請先放到 noob/service/models（容器：/root/noob/service/models）。</li></ul>"
                )
            continue
        links = "".join(
            f"<li><a href=\"{item['url']}\">{item['name']}</a>（{item['updated_at']}，{item['size']} bytes）</li>"
            for item in items
        )
        if not links:
            links = "<li>尚無檔案</li>"
        rows_html += f"<h3>{title}</h3><ul>{links}</ul>"
    settings_panel_html = f"""
    <section class="panel-block">
      <h2>設定維護</h2>
      <div class="form-grid">
        <label>
          <span>對外 Base URL / IP</span>
          <input id="serverApiBase" type="text" value="{html_lib.escape(resolved_server_base)}" placeholder="例如：http://10.79.140.107:8000" />
        </label>
        <label>
          <span>預設活動編號</span>
          <input id="defaultActivityCode" type="text" value="{html_lib.escape(default_activity_code)}" placeholder="例如：A51" />
        </label>
        <label>
          <span>預設攝影師</span>
          <input id="defaultPhotographer" type="text" value="{html_lib.escape(default_photographer)}" placeholder="例如：王小明" />
        </label>
        <label>
          <span>設定檔位置</span>
          <input type="text" value="service/laptop_tool_admin_settings.json" readonly />
        </label>
      </div>
      <div class="actions">
        <button id="saveSettingsBtn" type="button">儲存設定</button>
        <button id="reloadSettingsBtn" type="button" class="secondary">重新載入</button>
        <a class="btn secondary" href="/laptop-tool/config">下載目前設定檔</a>
      </div>
      <div id="settingsStatus" class="status">目前設定已載入。</div>
    </section>
    """
    preview_panel_html = f"""
    <section class="panel-block">
      <h2>設定預覽</h2>
      <div class="summary-grid">
        <div class="summary-item"><strong id="activityCount">{len(preview_activities)}</strong><span>活動編號</span></div>
        <div class="summary-item"><strong id="photographerCount">{len(preview_photographers)}</strong><span>攝影師</span></div>
      </div>
      <div class="preview-columns">
        <div>
          <h3>活動清單預覽</h3>
          <ul id="activityPreview">{activity_preview_html}</ul>
        </div>
        <div>
          <h3>攝影師預覽</h3>
          <ul id="photographerPreview">{photographer_preview_html}</ul>
        </div>
      </div>
      <div class="hint">{html_lib.escape(preview_error) if preview_error else "活動與攝影師清單由資料庫即時匯出，儲存設定不會覆蓋這些清單。"}</div>
    </section>
    """
    html = f"""
    <!DOCTYPE html>
    <html lang="zh-Hant">
    <head>
      <meta charset="utf-8" />
      <meta name="viewport" content="width=device-width, initial-scale=1" />
      <title>AI人臉辨識系統工具程式設定維護</title>
      <style>
        :root {{
          --bg: #f4efe7; --panel: #fffdfa; --ink: #1f2937; --muted: #64748b;
          --line: #d8cfbf; --accent: #92400e; --accent2: #0f766e;
        }}
        * {{ box-sizing: border-box; }}
        body {{ margin:0; font-family:"Segoe UI","Noto Sans TC",sans-serif; background:linear-gradient(180deg,#e6ddcf,var(--bg)); color:var(--ink); }}
        .wrap {{ max-width:1180px; margin:0 auto; padding:28px 18px 56px; }}
        .panel {{ background:var(--panel); border:1px solid var(--line); border-radius:18px; padding:20px; box-shadow:0 12px 32px rgba(41,33,18,.08); }}
        .panel-block {{ margin-top:18px; padding-top:18px; border-top:1px solid var(--line); }}
        .form-grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:14px 16px; }}
        .form-grid label {{ display:flex; flex-direction:column; gap:6px; }}
        .form-grid span {{ font-weight:700; }}
        input {{ width:100%; padding:10px 12px; border:1px solid var(--line); border-radius:10px; background:#fff; }}
        .actions {{ display:flex; gap:10px; flex-wrap:wrap; margin-top:14px; }}
        .btn, button {{ display:inline-flex; align-items:center; justify-content:center; padding:10px 14px; border-radius:12px; border:0; text-decoration:none; color:#fff; background:linear-gradient(135deg,var(--accent),var(--accent2)); cursor:pointer; font-weight:700; }}
        .btn.secondary, button.secondary {{ background:#6b7280; }}
        .status {{ margin-top:12px; padding:10px 12px; border-radius:10px; background:#eef8f6; color:#0f766e; white-space:pre-wrap; }}
        .hint {{ color:var(--muted); }}
        .summary-grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; margin:12px 0 16px; }}
        .summary-item {{ background:#fff; border:1px solid var(--line); border-radius:14px; padding:12px 14px; }}
        .summary-item strong {{ display:block; font-size:30px; line-height:1.1; }}
        .summary-item span {{ color:var(--muted); }}
        .preview-columns {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:16px; }}
        .preview-columns ul {{ margin:0; padding-left:20px; }}
        .preview-columns li {{ margin-bottom:6px; word-break:break-word; }}
        @media (max-width: 980px) {{
          .form-grid, .summary-grid, .preview-columns {{ grid-template-columns:1fr; }}
        }}
      </style>
    </head>
    <body>
      <div class="wrap">
        <div class="panel">
          <h1>AI人臉辨識系統工具程式設定維護</h1>
          <p class="hint">此頁同時提供管理設定維護與下載入口，不影響現有 Server 網頁作業流程。</p>
          {settings_panel_html}
          {preview_panel_html}
          <div>
            <a class="btn" href="/laptop-tool/config">下載設定檔（JSON）</a>
            <a class="btn" href="/laptop-tool/model-manifest">模型清單（JSON）</a>
            <a class="btn" href="/laptop-tool/download-package-zip">下載工具程式 ZIP（dist/laptop_tool）</a>
            <a class="btn secondary" href="/laptop-tool-upload-monitor">上傳作業檢視</a>
            <a class="btn" href="/">回首頁</a>
          </div>
          {rows_html}
        </div>
      </div>
      <script>
        const ids = {{
          serverApiBase: document.getElementById('serverApiBase'),
          defaultActivityCode: document.getElementById('defaultActivityCode'),
          defaultPhotographer: document.getElementById('defaultPhotographer'),
          saveSettingsBtn: document.getElementById('saveSettingsBtn'),
          reloadSettingsBtn: document.getElementById('reloadSettingsBtn'),
          settingsStatus: document.getElementById('settingsStatus'),
          activityCount: document.getElementById('activityCount'),
          photographerCount: document.getElementById('photographerCount'),
          activityPreview: document.getElementById('activityPreview'),
          photographerPreview: document.getElementById('photographerPreview'),
        }};
        function setStatus(text, isError=false) {{
          ids.settingsStatus.textContent = text;
          ids.settingsStatus.style.background = isError ? '#fee2e2' : '#eef8f6';
          ids.settingsStatus.style.color = isError ? '#b91c1c' : '#0f766e';
        }}
        function escapeHtml(value) {{
          return String(value || '').replace(/[&<>"']/g, s => ({{'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}}[s]));
        }}
        function renderPreview(payload) {{
          const activities = Array.isArray(payload.activities) ? payload.activities : [];
          const photographers = Array.isArray(payload.photographers) ? payload.photographers : [];
          ids.activityCount.textContent = String(activities.length);
          ids.photographerCount.textContent = String(photographers.length);
          ids.activityPreview.innerHTML = activities.slice(0, 8).map(item => {{
            const code = escapeHtml(item && item.activity_code ? item.activity_code : '');
            const date = escapeHtml(item && item.activity_date ? item.activity_date : '');
            const timeRange = escapeHtml(item && (item.activity_time_range || item.activity_time) ? (item.activity_time_range || item.activity_time) : '');
            const content = escapeHtml(item && item.activity_content ? item.activity_content : '');
            return `<li><code>${{code}}</code> ${{date}} ${{timeRange}} ${{content}}</li>`;
          }}).join('') || '<li>目前沒有可顯示的活動資料。</li>';
          ids.photographerPreview.innerHTML = photographers.slice(0, 8).map(item => {{
            const name = escapeHtml(item && (item.name || item.photographer_name) ? (item.name || item.photographer_name) : '');
            const note = escapeHtml(item && item.note ? item.note : '');
            return `<li><code>${{name}}</code>${{note ? ` <span class="hint">${{note}}</span>` : ''}}</li>`;
          }}).join('') || '<li>目前沒有可顯示的攝影師資料。</li>';
        }}
        async function loadSettings() {{
          const response = await fetch('/laptop-tool-admin/settings', {{ cache: 'no-store' }});
          const payload = await response.json();
          if (!response.ok) throw new Error(payload.detail || '載入管理設定失敗');
          ids.serverApiBase.value = String(payload.server_api_base || payload.public_base_url || '').trim();
          ids.defaultActivityCode.value = String(payload.default_activity_code || '').trim();
          ids.defaultPhotographer.value = String(payload.default_photographer || '').trim();
          setStatus(`已載入設定（更新時間：${{payload.updated_at || '尚未保存'}}）。`);
        }}
        async function saveSettings() {{
          const payload = {{
            server_api_base: String(ids.serverApiBase.value || '').trim(),
            default_activity_code: String(ids.defaultActivityCode.value || '').trim().toUpperCase(),
            default_photographer: String(ids.defaultPhotographer.value || '').trim(),
          }};
          const response = await fetch('/laptop-tool-admin/settings', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json; charset=utf-8' }},
            body: JSON.stringify(payload),
          }});
          const result = await response.json();
          if (!response.ok) throw new Error(result.detail || '儲存設定失敗');
          ids.serverApiBase.value = String(result.server_api_base || result.public_base_url || '').trim();
          ids.defaultActivityCode.value = String(result.default_activity_code || '').trim();
          ids.defaultPhotographer.value = String(result.default_photographer || '').trim();
          setStatus(`設定已儲存：${{result.updated_at || '已完成'}}`);
        }}
        async function loadPreview() {{
          try {{
            const response = await fetch('/activity-normalize-config/export', {{ cache: 'no-store' }});
            const payload = await response.json();
            if (!response.ok) throw new Error(payload.detail || '載入活動/攝影師清單失敗');
            renderPreview(payload);
          }} catch (error) {{
            ids.activityCount.textContent = '0';
            ids.photographerCount.textContent = '0';
            ids.activityPreview.innerHTML = '<li>載入活動清單失敗。</li>';
            ids.photographerPreview.innerHTML = '<li>載入攝影師清單失敗。</li>';
            setStatus(error.message || String(error), true);
          }}
        }}
        ids.saveSettingsBtn.addEventListener('click', async () => {{
          try {{ await saveSettings(); }} catch (error) {{ setStatus(error.message || String(error), true); }}
        }});
        ids.reloadSettingsBtn.addEventListener('click', async () => {{
          try {{ await loadSettings(); await loadPreview(); }} catch (error) {{ setStatus(error.message || String(error), true); }}
        }});
        (async () => {{
          try {{ await loadSettings(); await loadPreview(); }}
          catch (error) {{ setStatus(error.message || String(error), true); }}
        }})();
      </script>
    </body>
    </html>
    """
    return HTMLResponse(html, headers=html_no_cache_headers())


@app.get("/laptop-tool-admin/settings")
async def laptop_tool_admin_settings():
    return _read_laptop_tool_admin_settings()


@app.post("/laptop-tool-admin/settings")
async def laptop_tool_admin_settings_save(payload: LaptopToolAdminSettingsPayload, request: Request):
    try:
        resolved_base = str(payload.server_api_base or payload.public_base_url or "").strip().rstrip("/")
        if not resolved_base:
            resolved_base = _resolve_laptop_tool_request_base(request)
        saved = _save_laptop_tool_admin_settings(
            resolved_base,
            default_activity_code=payload.default_activity_code,
            default_photographer=payload.default_photographer,
        )
        return saved
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/laptop-tool/admin/upload-asset")
async def laptop_tool_admin_upload_asset(
    bucket: str = Form(...),
    file: UploadFile = File(...),
):
    raise HTTPException(status_code=410, detail="此功能已停用，請由伺服器目錄部署檔案。")


@app.get(LAPTOP_TOOL_DOWNLOAD_PAGE)
async def laptop_tool_download(bucket: str = Query(...), name: str = Query(...)):
    safe_bucket = str(bucket or "").strip()
    safe_name = _safe_leaf_name(name)
    roots = {
        "packages": LAPTOP_TOOL_PACKAGE_ROOT,
        "docs": LAPTOP_TOOL_DOC_ROOT,
        "models": LAPTOP_TOOL_MODEL_ROOT,
        "dist": LAPTOP_TOOL_DIST_ROOT,
    }
    root = roots.get(safe_bucket)
    if not root:
        raise HTTPException(status_code=400, detail="bucket 參數錯誤")
    path = root / safe_name
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="找不到指定檔案")
    return FileResponse(path, filename=safe_name, media_type="application/octet-stream")


@app.get("/laptop-tool/download-package-zip")
async def laptop_tool_download_package_zip():
    if not LAPTOP_TOOL_DIST_ROOT.exists():
        raise HTTPException(status_code=404, detail="找不到 dist/laptop_tool 目錄，請先執行 build。")
    files = [p for p in sorted(LAPTOP_TOOL_DIST_ROOT.rglob("*")) if p.is_file()]
    if not files:
        raise HTTPException(status_code=404, detail="dist/laptop_tool 目錄沒有可下載檔案。")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file_path in files:
            arcname = str(file_path.relative_to(LAPTOP_TOOL_DIST_ROOT)).replace("\\", "/")
            zf.write(file_path, arcname=arcname)
    buf.seek(0)
    filename = f"laptop_tool_{_now_tpe().strftime('%Y%m%d_%H%M%S')}.zip"
    headers = {"Content-Disposition": f"attachment; filename={filename}"}
    return StreamingResponse(iter([buf.getvalue()]), media_type="application/zip", headers=headers)


@app.get("/laptop-tool/config")
async def laptop_tool_config(request: Request):
    settings = _read_laptop_tool_admin_settings()
    payload = await export_activity_normalize_config()
    if isinstance(payload, JSONResponse):
        return payload
    base_url = _resolve_laptop_tool_server_base(settings, request)
    payload["server_api_base"] = base_url
    payload["public_base_url"] = base_url
    payload["default_activity_code"] = str(settings.get("default_activity_code") or "").strip().upper()
    payload["default_photographer"] = str(settings.get("default_photographer") or "").strip()
    payload["upload_api"] = {
        "start": f"{base_url}/laptop-tool/upload-batch/start",
        "chunk": f"{base_url}/laptop-tool/upload-batch/chunk",
        "commit": f"{base_url}/laptop-tool/upload-batch/commit",
        "status": f"{base_url}/laptop-tool/upload-batch/{{job_id}}",
    }
    payload["tool_release_page"] = f"{base_url}{LAPTOP_TOOL_ADMIN_PAGE}"
    return payload


@app.get("/laptop-tool/model-manifest")
async def laptop_tool_model_manifest():
    model_items = []
    recommended_path = LAPTOP_TOOL_MODEL_ROOT / LAPTOP_TOOL_RECOMMENDED_MODEL_ZIP
    if recommended_path.exists() and recommended_path.is_file():
        sha256 = _calculate_file_sha256(str(recommended_path))
        model_items.append(
                {
                    "name": recommended_path.name,
                    "size": recommended_path.stat().st_size,
                    "sha256": sha256,
                    "updated_at": datetime.fromtimestamp(recommended_path.stat().st_mtime, LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S"),
                    "download_url": f"{LAPTOP_TOOL_DOWNLOAD_PAGE}?bucket=models&name={quote(recommended_path.name)}",
                    "recommended": True,
                    "extract_dir": "antelopev2",
                    "model_version": EMBEDDING_MODEL_NAME,
                }
            )
    payload = {
        "model_version": EMBEDDING_MODEL_NAME,
        "items": model_items,
        "count": len(model_items),
        "recommended_name": LAPTOP_TOOL_RECOMMENDED_MODEL_ZIP,
    }
    if not model_items:
        payload["hint"] = (
            f"模型目錄目前缺少 {LAPTOP_TOOL_RECOMMENDED_MODEL_ZIP}，"
            "請先放到 noob/service/models（容器：/root/noob/service/models）。"
        )
    return payload


@app.post("/laptop-tool/upload-batch/start")
async def laptop_tool_upload_batch_start(payload: LaptopToolUploadStartPayload):
    ensure_activity_tables_once()
    ensure_laptop_tool_tables()
    device_id = str(payload.device_id or "").strip()
    if not device_id:
        return JSONResponse(status_code=400, content={"detail": "device_id 不可空白。"})
    now_str = _now_tpe().strftime("%Y%m%d_%H%M%S")
    rand = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    job_id = f"lap_{now_str}_{rand}"
    db = None
    cursor = None
    try:
        db = mysqlconnector()
        db.connect()
        if db.conn is None:
            raise RuntimeError("資料庫連線失敗")
        cursor = db.conn.cursor()
        cursor.execute(
            """
            SELECT job_id
            FROM laptop_upload_job
            WHERE device_id = %s AND status IN ('QUEUED', 'RUNNING', 'PAUSED')
            ORDER BY updated_at DESC, created_at DESC
            LIMIT 1
            """,
            (device_id,),
        )
        active_job = cursor.fetchone()
        if active_job:
            active_job_id = active_job.get("job_id") if isinstance(active_job, dict) else (active_job[0] if active_job else None)
            return JSONResponse(
                status_code=409,
                content={
                    "detail": "同一台裝置已有上傳作業進行中，請先完成或停止現有 job。",
                    "job_id": active_job_id,
                    "device_id": device_id,
                },
            )
        staging_dir = LAPTOP_TOOL_STAGING_ROOT / job_id
        staging_dir.mkdir(parents=True, exist_ok=True)
        cursor.execute(
            """
            INSERT INTO laptop_upload_job (
                job_id, status, device_id, laptop_label, model_version, total_count, staging_dir
            ) VALUES (%s, 'QUEUED', %s, %s, %s, %s, %s)
            """,
            (job_id, device_id, payload.laptop_label, payload.model_version, payload.total_count, str(staging_dir)),
        )
        db.conn.commit()
        return {"job_id": job_id, "status": "QUEUED", "staging_dir": str(staging_dir)}
    finally:
        with contextlib.suppress(Exception):
            if cursor:
                cursor.close()
        with contextlib.suppress(Exception):
            if db:
                db.close()


@app.get("/laptop-tool/upload-batch/{job_id}")
async def laptop_tool_upload_batch_status(job_id: str):
    ensure_laptop_tool_tables()
    db = None
    cursor = None
    try:
        db = mysqlconnector()
        db.connect()
        if db.conn is None:
            raise RuntimeError("資料庫連線失敗")
        cursor = db.conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM laptop_upload_job WHERE job_id = %s LIMIT 1", (job_id,))
        row = cursor.fetchone()
        if not row:
            return JSONResponse(status_code=404, content={"detail": "找不到 job_id"})
        return row
    finally:
        with contextlib.suppress(Exception):
            if cursor:
                cursor.close()
        with contextlib.suppress(Exception):
            if db:
                db.close()


@app.post("/laptop-tool/upload-batch/control")
async def laptop_tool_upload_batch_control(payload: LaptopToolUploadControlPayload):
    ensure_laptop_tool_tables()
    action = str(payload.action or "").strip().upper()
    if action not in {"PAUSE", "RESUME", "CANCEL", "FAIL"}:
        return JSONResponse(status_code=400, content={"detail": "action 只能是 PAUSE / RESUME / CANCEL / FAIL"})
    db = None
    cursor = None
    try:
        db = mysqlconnector()
        db.connect()
        if db.conn is None:
            raise RuntimeError("資料庫連線失敗")
        cursor = db.conn.cursor(dictionary=True)
        cursor.execute("SELECT job_id, status FROM laptop_upload_job WHERE job_id = %s LIMIT 1", (payload.job_id,))
        job = cursor.fetchone()
        if not job:
            return JSONResponse(status_code=404, content={"detail": "找不到 job_id"})
        current_status = str(job.get("status") or "").upper()
        if action == "PAUSE":
            if current_status not in {"QUEUED", "RUNNING"}:
                return JSONResponse(status_code=409, content={"detail": f"目前狀態 {current_status} 不可暫停"})
            new_status = "PAUSED"
            finished_at = None
        elif action == "RESUME":
            if current_status != "PAUSED":
                return JSONResponse(status_code=409, content={"detail": f"目前狀態 {current_status} 不可繼續"})
            new_status = "RUNNING"
            finished_at = None
        else:
            if action == "CANCEL":
                if current_status in {"DONE", "FAILED", "CANCELED"}:
                    return JSONResponse(status_code=409, content={"detail": f"目前狀態 {current_status} 不可取消"})
                new_status = "CANCELED"
                finished_at = _now_tpe().strftime("%Y-%m-%d %H:%M:%S")
            else:
                if current_status in {"DONE", "FAILED", "CANCELED"}:
                    return JSONResponse(status_code=409, content={"detail": f"目前狀態 {current_status} 不可標記失敗"})
                new_status = "FAILED"
                finished_at = _now_tpe().strftime("%Y-%m-%d %H:%M:%S")
                error_summary = str(payload.reason or "").strip() or "工具程式本機執行失敗"
        cursor.execute(
            """
            UPDATE laptop_upload_job
            SET status = %s,
                finished_at = %s,
                error_summary = %s,
                updated_at = NOW()
            WHERE job_id = %s
            """,
            (new_status, finished_at, error_summary if action == "FAIL" else None, payload.job_id),
        )
        db.conn.commit()
        return {"job_id": payload.job_id, "status": new_status}
    except Exception as exc:
        if db and db.conn:
            with contextlib.suppress(Exception):
                db.conn.rollback()
        return JSONResponse(status_code=500, content={"detail": f"更新上傳作業狀態失敗：{exc}"})
    finally:
        with contextlib.suppress(Exception):
            if cursor:
                cursor.close()
        with contextlib.suppress(Exception):
            if db:
                db.close()

@app.get("/activity-schedules/query")
async def activity_schedule_query(
    activity_date: str = Query(""),
    photographer: str = Query(""),
    keyword: str = Query(""),
    limit: int = Query(200, ge=1, le=1000),
):
    try:
        ensure_activity_tables()
        items = query_activity_schedule(
            activity_date=activity_date,
            photographer=photographer,
            keyword=keyword,
            limit=limit,
        )
        return {
            "filters": {
                "activity_date": activity_date,
                "photographer": photographer,
                "keyword": keyword,
                "limit": limit,
            },
            "total": len(items),
            "items": items,
        }
    except Exception as e:
        logger.error(f"活動行程查詢失敗: {str(e)}")
        return JSONResponse(status_code=500, content={"detail": f"活動行程查詢失敗：{str(e)}"})


if MULTIPART_AVAILABLE:
    @app.post("/activity-schedules/upload-excel")
    async def activity_schedule_upload_excel(file: UploadFile = File(...)):
        try:
            ensure_activity_tables()
            return save_activity_uploaded_excel(file, ACTIVITY_UPLOAD_ROOT)
        except Exception as e:
            logger.error(f"活動行程 Excel 上傳失敗: {str(e)}")
            return JSONResponse(status_code=500, content={"detail": f"活動行程 Excel 上傳失敗：{str(e)}"})


@app.get("/activity-schedules/excel-columns")
async def activity_schedule_excel_columns(
    excel_path: str = Query(..., min_length=1),
    sheet_name: str = Query(""),
):
    try:
        sheet_names = list_activity_sheet_names(excel_path)
        normalized_sheet = "CSV" if sheet_names == ["CSV"] else (sheet_name or sheet_names[0])
        if sheet_names != ["CSV"] and normalized_sheet not in sheet_names:
            raise ValueError(f"活動行程 Excel 找不到工作表：{normalized_sheet}")
        columns = load_activity_columns(excel_path, normalized_sheet)
        return {
            "sheet_names": sheet_names,
            "selected_sheet": normalized_sheet,
            "columns": columns,
        }
    except Exception as e:
        logger.error(f"活動行程 Excel 欄位讀取失敗: {str(e)}")
        return JSONResponse(status_code=500, content={"detail": f"活動行程 Excel 讀取欄位失敗：{str(e)}"})


@app.post("/activity-schedules/import-excel")
async def activity_schedule_import_excel(payload: ActivityScheduleImportPayload):
    try:
        ensure_activity_tables()
        column_map = {
            "activity_code": payload.activity_code_column,
            "activity_date": payload.activity_date_column,
            "activity_time": payload.activity_time_column,
            "activity_content": payload.activity_content_column,
            "owner_team": payload.owner_team_column,
            "location": payload.location_column,
            "photographer": payload.photographer_column,
            "note": payload.note_column,
        }
        return import_activity_schedule(payload.excel_path, payload.sheet_name, column_map)
    except Exception as e:
        logger.error(f"活動行程 Excel 匯入失敗: {str(e)}")
        return JSONResponse(status_code=500, content={"detail": f"活動行程 Excel 匯入失敗：{str(e)}"})


if MULTIPART_AVAILABLE:
    @app.post("/laptop-tool/upload-batch/chunk")
    async def laptop_tool_upload_batch_chunk(
        job_id: str = Form(...),
        seq_no: int = Form(...),
        item_json: str = Form(...),
        origin_file: UploadFile | None = File(None),
        thumb_file: UploadFile | None = File(None),
    ):
        ensure_laptop_tool_tables()
        db = None
        cursor = None
        chunk_started = time.perf_counter()
        try:
            payload = LaptopToolUploadChunkMeta.model_validate_json(item_json)
            db = mysqlconnector()
            db.connect()
            if db.conn is None:
                raise RuntimeError("資料庫連線失敗")
            cursor = db.conn.cursor(dictionary=True)
            cursor.execute("SELECT job_id, staging_dir FROM laptop_upload_job WHERE job_id = %s LIMIT 1", (job_id,))
            job = cursor.fetchone()
            if not job:
                return JSONResponse(status_code=404, content={"detail": "找不到 job_id"})
            staging_dir = Path(job.get("staging_dir") or (LAPTOP_TOOL_STAGING_ROOT / job_id))
            staging_dir.mkdir(parents=True, exist_ok=True)
            origin_path = ""
            thumb_path = ""
            if origin_file is not None:
                origin_name = _safe_leaf_name(origin_file.filename or payload.file_name)
                origin_path_obj = staging_dir / f"{seq_no:06d}_origin_{origin_name}"
                origin_path_obj.write_bytes(await origin_file.read())
                origin_path = str(origin_path_obj)
            if thumb_file is not None:
                thumb_name = _safe_leaf_name(thumb_file.filename or payload.file_name)
                thumb_path_obj = staging_dir / f"{seq_no:06d}_thumb_{thumb_name}"
                thumb_path_obj.write_bytes(await thumb_file.read())
                thumb_path = str(thumb_path_obj)
            cursor.execute(
                """
                INSERT INTO laptop_upload_job_item (
                    job_id, seq_no, photo_uuid, file_name, payload_json,
                    origin_staging_path, thumb_staging_path, status, reason_code
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'UPLOADED', '')
                ON DUPLICATE KEY UPDATE
                    seq_no = VALUES(seq_no),
                    file_name = VALUES(file_name),
                    payload_json = VALUES(payload_json),
                    origin_staging_path = VALUES(origin_staging_path),
                    thumb_staging_path = VALUES(thumb_staging_path),
                    status = 'UPLOADED',
                    reason_code = '',
                    error_reason = NULL,
                    updated_at = NOW()
                """,
                (
                    job_id,
                    seq_no,
                    payload.photo_uuid,
                    payload.file_name,
                    json.dumps(payload.model_dump(mode="json"), ensure_ascii=False),
                    origin_path,
                    thumb_path,
                ),
            )
            cursor.execute(
                """
                UPDATE laptop_upload_job
                SET status = 'RUNNING',
                    uploaded_count = (
                        SELECT COUNT(*) FROM laptop_upload_job_item WHERE job_id = %s AND status IN ('UPLOADED','DONE')
                    ),
                    updated_at = NOW()
                WHERE job_id = %s
                """,
                (job_id, job_id),
            )
            db.conn.commit()
            chunk_elapsed_ms = int((time.perf_counter() - chunk_started) * 1000)
            logger.info("laptop-tool chunk job_id=%s seq_no=%s elapsed_ms=%s", job_id, seq_no, chunk_elapsed_ms)
            return {
                "job_id": job_id,
                "seq_no": seq_no,
                "photo_uuid": payload.photo_uuid,
                "status": "UPLOADED",
                "elapsed_ms": chunk_elapsed_ms,
            }
        except Exception as e:
            if db and db.conn:
                with contextlib.suppress(Exception):
                    db.conn.rollback()
            return JSONResponse(status_code=500, content={"detail": f"上傳 chunk 失敗：{str(e)}"})
        finally:
            with contextlib.suppress(Exception):
                if cursor:
                    cursor.close()
            with contextlib.suppress(Exception):
                if db:
                    db.close()

    @app.post("/activity-photo-normalize")
    async def activity_photo_normalize(
        laptop_number: str = Form(...),
        schedule_id: int | None = Form(None),
        schedule_code: str = Form(""),
        schedule_time: str = Form(""),
        schedule_time_range: str = Form(""),
        schedule_source: str = Form("api"),
        activities_json: str = Form(""),
        photographer: str = Form(""),
        normalize_mode: str = Form("schedule"),
        files: list[UploadFile] = File(...),
    ):
        try:
            return await normalize_activity_photo_files(
                files=files,
                laptop_number=laptop_number,
                schedule_id=schedule_id,
                schedule_code=schedule_code,
                schedule_time=schedule_time,
                schedule_time_range=schedule_time_range,
                schedule_source=schedule_source,
                activities_json=activities_json,
                photographer=photographer,
                normalize_mode=normalize_mode,
            )
        except Exception as e:
            logger.error(f"活動照片正規化失敗: {str(e)}")
            return JSONResponse(status_code=500, content={"detail": f"活動照片正規化失敗：{str(e)}"})

    @app.post("/activity-photo-normalize-folder")
    async def activity_photo_normalize_folder(
        laptop_number: str = Form(...),
        schedule_id: int | None = Form(None),
        schedule_code: str = Form(""),
        schedule_time: str = Form(""),
        schedule_time_range: str = Form(""),
        schedule_source: str = Form("api"),
        activities_json: str = Form(""),
        schedule_date: str = Form(""),
        schedule_content: str = Form(""),
        photographer: str = Form(""),
        normalize_mode: str = Form("schedule"),
        source_folder: str = Form(""),
        output_folder: str = Form(""),
    ):
        try:
            return await normalize_activity_photo_folder(
                source_folder=source_folder,
                output_folder=output_folder,
                laptop_number=laptop_number,
                schedule_id=schedule_id,
                schedule_code=schedule_code,
                schedule_time=schedule_time,
                schedule_time_range=schedule_time_range,
                schedule_source=schedule_source,
                activities_json=activities_json,
                schedule_date=schedule_date,
                schedule_content=schedule_content,
                photographer=photographer,
                normalize_mode=normalize_mode,
            )
        except Exception as e:
            logger.error(f"活動照片資料夾正規化失敗: {str(e)}")
            return JSONResponse(status_code=500, content={"detail": f"活動照片資料夾正規化失敗：{str(e)}"})

    @app.post("/activity-photo-normalize/start")
    async def activity_photo_normalize_start(
        laptop_number: str = Form(""),
        schedule_id: int | None = Form(None),
        schedule_code: str = Form(""),
        schedule_time: str = Form(""),
        schedule_time_range: str = Form(""),
        schedule_source: str = Form("api"),
        activities_json: str = Form(""),
        schedule_date: str = Form(""),
        schedule_content: str = Form(""),
        photographer: str = Form(""),
        normalize_mode: str = Form("exif"),
        source_folder: str = Form(""),
        output_folder: str = Form(""),
    ):
        try:
            ensure_activity_tables_once()
            started = time.perf_counter()
            payload = await start_normalize_activity_photos_job(
                laptop_number=laptop_number,
                schedule_id=schedule_id,
                schedule_code=schedule_code,
                schedule_time=schedule_time,
                schedule_time_range=schedule_time_range,
                schedule_source=schedule_source,
                activities_json=activities_json,
                schedule_date=schedule_date,
                schedule_content=schedule_content,
                photographer=photographer,
                normalize_mode=normalize_mode,
                source_folder=source_folder,
                output_folder=output_folder,
            )
            payload["start_response_ms"] = int((time.perf_counter() - started) * 1000)
            if payload.get("server_received_at"):
                payload["server_received_at_tpe"] = payload.get("server_received_at")
            return payload
        except Exception as e:
            logger.error(f"活動照片正規化任務啟動失敗: {str(e)}")
            return JSONResponse(status_code=500, content={"detail": f"活動照片正規化任務啟動失敗：{str(e)}"})

    @app.get("/activity-photo-normalize/jobs/{job_id}")
    async def activity_photo_normalize_job_status(job_id: str):
        payload = get_import_job_status(job_id)
        if not payload or str(payload.get("job_type") or "") != "normalize":
            return JSONResponse(status_code=404, content={"detail": "找不到指定正規化任務"})
        payload["last_polled_at"] = _now_tpe().strftime("%Y-%m-%d %H:%M:%S")
        payload["last_log_offset"] = None
        log_path = str(payload.get("log_path") or "").strip()
        if log_path:
            try:
                path_obj = Path(log_path)
                if path_obj.exists():
                    payload["last_log_offset"] = len(path_obj.read_text(encoding="utf-8-sig", errors="replace").splitlines())
            except Exception:
                payload["last_log_offset"] = None
        return payload

    @app.get("/activity-photo-normalize/jobs/{job_id}/logs")
    async def activity_photo_normalize_job_logs(job_id: str, offset: int = Query(0, ge=0)):
        payload = get_import_job_logs(job_id, offset=offset)
        if not payload:
            return JSONResponse(status_code=404, content={"detail": "找不到指定正規化任務"})
        status = get_import_job_status(job_id)
        if not status or str(status.get("job_type") or "") != "normalize":
            return JSONResponse(status_code=404, content={"detail": "找不到指定正規化任務"})
        payload["line_count"] = int(payload.get("next_offset") or 0)
        payload["server_log_read_at_tpe"] = _now_tpe().strftime("%Y-%m-%d %H:%M:%S")
        return payload

    @app.get("/activity-photo-normalize/jobs-recent")
    async def activity_photo_normalize_recent_jobs(
        limit: int = Query(30, ge=1, le=200),
        device_id: str = Query("", description="筆電編號；提供時只回該筆電任務"),
    ):
        db = None
        cursor = None
        try:
            ensure_activity_tables_once()
            db = mysqlconnector()
            db.connect()
            if db.conn is None:
                raise RuntimeError("資料庫連線失敗，請先確認 MySQL 容器與連線設定。")
            cursor = db.conn.cursor(dictionary=True)
            filters = ["job_type = 'normalize'"]
            params = []
            did = str(device_id or "").strip()
            if did:
                filters.append("device_id = %s")
                params.append(did)
            where_clause = " AND ".join(filters)
            sql = f"""
                SELECT
                    job_id, status, total_count, processed_count, success_count, failed_count, skipped_count,
                    source_folder,
                    started_at, finished_at, updated_at
                FROM activity_import_job
                WHERE {where_clause}
                ORDER BY COALESCE(updated_at, started_at) DESC
                LIMIT %s
            """
            params.append(int(limit))
            cursor.execute(sql, tuple(params))
            rows = cursor.fetchall() or []
            items = []
            for row in rows:
                items.append(
                    {
                        "job_id": row.get("job_id") or "",
                        "status": row.get("status") or "",
                        "total_count": int(row.get("total_count") or 0),
                        "processed_count": int(row.get("processed_count") or 0),
                        "success_count": int(row.get("success_count") or 0),
                        "failed_count": int(row.get("failed_count") or 0),
                        "skipped_count": int(row.get("skipped_count") or 0),
                        "source_folder": row.get("source_folder") or "",
                        "started_at": _format_datetime_tpe(row.get("started_at")),
                        "started_at_tpe": _format_datetime_tpe(row.get("started_at")),
                        "finished_at": _format_datetime_tpe(row.get("finished_at")),
                        "updated_at": _format_datetime_tpe(row.get("updated_at")),
                    }
                )
            return {"items": items}
        except Exception as e:
            logger.error(f"讀取最近正規化任務清單失敗: {str(e)}")
            return JSONResponse(status_code=500, content={"detail": f"讀取最近正規化任務清單失敗：{str(e)}"})
        finally:
            with contextlib.suppress(Exception):
                if cursor:
                    cursor.close()
            with contextlib.suppress(Exception):
                if db:
                    db.close()

    @app.post("/activity-photo-import")
    async def activity_photo_import(
        laptop_number: str = Form(...),
        schedule_id: int | None = Form(None),
        photographer: str = Form(""),
        enable_pyiqa: bool = Form(False),
        normalize_mode: str = Form("schedule"),
        source_folder: str = Form(""),
        output_folder: str = Form(""),
        backup_folder: str = Form(""),
    ):
        try:
            ensure_activity_tables()
            return await import_activity_photos_from_normalized_folder(
                laptop_number=laptop_number,
                schedule_id=schedule_id,
                photographer=photographer,
                enable_pyiqa=enable_pyiqa,
                normalize_mode=normalize_mode,
            )
        except Exception as e:
            logger.error(f"活動照片匯入失敗: {str(e)}")
            return JSONResponse(status_code=500, content={"detail": f"活動照片匯入失敗：{str(e)}"})

    @app.post("/laptop-tool/upload-batch/commit")
    async def laptop_tool_upload_batch_commit(payload: LaptopToolUploadCommitPayload):
        ensure_activity_tables_once()
        ensure_laptop_tool_tables()
        db = None
        cursor = None
        try:
            db = mysqlconnector()
            db.connect()
            if db.conn is None:
                raise RuntimeError("資料庫連線失敗")
            cursor = db.conn.cursor(dictionary=True)
            cursor.execute("SELECT * FROM laptop_upload_job WHERE job_id = %s LIMIT 1", (payload.job_id,))
            job = cursor.fetchone()
            if not job:
                return JSONResponse(status_code=404, content={"detail": "找不到 job_id"})
            cursor.execute(
                """
                SELECT id, seq_no, photo_uuid, file_name, payload_json, origin_staging_path, thumb_staging_path
                FROM laptop_upload_job_item
                WHERE job_id = %s AND status = 'UPLOADED'
                ORDER BY seq_no ASC
                """,
                (payload.job_id,),
            )
            rows = cursor.fetchall() or []
            committed = 0
            failed = 0
            failed_items: list[dict] = []
            origin_root = Path("/mnt/activity/dev/origin")
            thumbs_root = Path("/mnt/activity/dev/thumbs")
            origin_root.mkdir(parents=True, exist_ok=True)
            thumbs_root.mkdir(parents=True, exist_ok=True)
            commit_started = time.perf_counter()
            reco_result_cols = _get_table_columns(cursor, "reco_result", refresh=True)
            commit_batch_size = 100

            def copy_commit_artifacts(row: dict) -> dict:
                item_id = row.get("id")
                item_payload = {}
                file_name = _safe_leaf_name(row.get("file_name") or "")
                photo_uuid = str(row.get("photo_uuid") or "").strip()
                origin_staging = str(row.get("origin_staging_path") or "").strip()
                thumb_staging = str(row.get("thumb_staging_path") or "").strip()
                try:
                    item_payload = json.loads(row.get("payload_json") or "{}")
                    file_name = _safe_leaf_name(item_payload.get("file_name") or file_name)
                    photo_uuid = str(item_payload.get("photo_uuid") or photo_uuid).strip()
                    if not file_name:
                        raise ValueError("file_name 缺失")
                    if not origin_staging or not Path(origin_staging).exists():
                        raise ValueError("origin_file 缺失")
                    origin_target = origin_root / file_name
                    if origin_target.exists():
                        origin_target = origin_root / f"{uuid4().hex[:6]}_{file_name}"
                    shutil.copy2(origin_staging, origin_target)
                    thumb_target = thumbs_root / file_name
                    if thumb_staging and Path(thumb_staging).exists():
                        if thumb_target.exists():
                            thumb_target = thumbs_root / f"{uuid4().hex[:6]}_{file_name}"
                        shutil.copy2(thumb_staging, thumb_target)
                    else:
                        shutil.copy2(origin_staging, thumb_target)
                    item_payload["human_laptop_number"] = str(job.get("device_id") or "")
                    return {
                        "ok": True,
                        "item_id": item_id,
                        "item_payload": item_payload,
                        "origin_target": str(origin_target),
                        "thumb_target": str(thumb_target),
                        "origin_staging": origin_staging,
                        "thumb_staging": thumb_staging,
                        "file_name": file_name,
                        "photo_uuid": photo_uuid,
                    }
                except Exception as exc:
                    return {
                        "ok": False,
                        "item_id": item_id,
                        "error": str(exc),
                        "file_name": file_name,
                        "photo_uuid": photo_uuid,
                        "origin_staging": origin_staging,
                        "thumb_staging": thumb_staging,
                    }

            copy_elapsed_total_ms = 0
            db_elapsed_total_ms = 0
            with ThreadPoolExecutor(max_workers=max(1, min(4, len(rows) or 1))) as executor:
                for batch_start in range(0, len(rows), commit_batch_size):
                    batch_rows = rows[batch_start:batch_start + commit_batch_size]
                    if not batch_rows:
                        continue
                    copy_started = time.perf_counter()
                    copied_rows = [future.result() for future in [executor.submit(copy_commit_artifacts, row) for row in batch_rows]]
                    copy_elapsed_total_ms += int((time.perf_counter() - copy_started) * 1000)
                    db_started = time.perf_counter()
                    batch_committed = 0
                    batch_failed = 0
                    logger.info(
                        "laptop-tool commit copy job_id=%s batch_start=%s batch_rows=%s finalize=%s",
                        payload.job_id,
                        batch_start,
                        len(batch_rows),
                        bool(payload.finalize),
                    )
                    for entry in copied_rows:
                        item_id = entry.get("item_id")
                        item_payload = entry.get("item_payload") if isinstance(entry.get("item_payload"), dict) else {}
                        item_photo_uuid = str((item_payload or {}).get("photo_uuid") or entry.get("photo_uuid") or "").strip()
                        item_file_name = _safe_leaf_name((item_payload or {}).get("file_name") or entry.get("file_name") or "")
                        origin_staging = str(entry.get("origin_staging") or "").strip()
                        thumb_staging = str(entry.get("thumb_staging") or "").strip()
                        if not entry.get("ok"):
                            batch_failed += 1
                            error_reason = str(entry.get("error") or "未知錯誤")
                            cursor.execute(
                                """
                                UPDATE laptop_upload_job_item
                                SET status = 'FAILED', reason_code = 'COMMIT_ERROR', error_reason = %s, updated_at = NOW()
                                WHERE id = %s
                                """,
                                (error_reason, item_id),
                            )
                            failed_items.append(
                                {
                                    "photo_uuid": item_photo_uuid,
                                    "file_name": item_file_name,
                                    "error_code": "COMMIT_COPY_ERROR",
                                    "error_reason": error_reason,
                                    "sql_detail": error_reason,
                                }
                            )
                            continue
                        try:
                            _upsert_laptop_item_to_main_tables(
                                cursor,
                                item_payload,
                                entry["origin_target"],
                                entry["thumb_target"],
                                reco_result_cols=reco_result_cols,
                            )
                            cursor.execute(
                                """
                                UPDATE laptop_upload_job_item
                                SET status = 'DONE', reason_code = '', error_reason = NULL, updated_at = NOW()
                                WHERE id = %s
                                """,
                                (item_id,),
                            )
                            batch_committed += 1
                            with contextlib.suppress(Exception):
                                if origin_staging:
                                    origin_staging_path = Path(origin_staging)
                                    if origin_staging_path.exists():
                                        origin_staging_path.unlink()
                            with contextlib.suppress(Exception):
                                if thumb_staging:
                                    thumb_staging_path = Path(thumb_staging)
                                    if thumb_staging_path.exists():
                                        thumb_staging_path.unlink()
                        except Exception as item_error:
                            batch_failed += 1
                            error_reason = str(item_error)
                            cursor.execute(
                                """
                                UPDATE laptop_upload_job_item
                                SET status = 'FAILED', reason_code = 'COMMIT_ERROR', error_reason = %s, updated_at = NOW()
                                WHERE id = %s
                                """,
                                (error_reason, item_id),
                            )
                            failed_items.append(
                                {
                                    "photo_uuid": item_photo_uuid,
                                    "file_name": item_file_name,
                                    "error_code": "COMMIT_SQL_ERROR",
                                    "error_reason": error_reason,
                                    "sql_detail": error_reason,
                                }
                            )
                    db.conn.commit()
                    db_elapsed_total_ms += int((time.perf_counter() - db_started) * 1000)
                    committed += batch_committed
                    failed += batch_failed
                    existing_failed = int(job.get("failed_count") or 0)
                    final_failed_total = existing_failed + failed
                    if payload.finalize:
                        next_status = "FAILED" if final_failed_total > 0 else "DONE"
                    else:
                        next_status = "RUNNING"
                    cursor.execute(
                        """
                        UPDATE laptop_upload_job
                        SET committed_count = committed_count + %s,
                            failed_count = failed_count + %s,
                            status = %s,
                            error_summary = %s,
                            finished_at = CASE WHEN %s IN ('DONE', 'FAILED') THEN NOW() ELSE finished_at END,
                            updated_at = NOW()
                        WHERE job_id = %s
                        """,
                        (
                            batch_committed,
                            batch_failed,
                            next_status,
                            json.dumps(failed_items[:50], ensure_ascii=False) if failed_items else None,
                            next_status,
                            payload.job_id,
                        ),
                    )
                    db.conn.commit()
            logger.info("laptop-tool commit db job_id=%s committed=%s failed=%s elapsed_ms=%s", payload.job_id, committed, failed, db_elapsed_total_ms)
            commit_elapsed_ms = int((time.perf_counter() - commit_started) * 1000)
            if payload.finalize:
                cursor.execute(
                    """
                    UPDATE laptop_upload_job
                    SET status = CASE WHEN %s > 0 THEN 'FAILED' ELSE 'DONE' END,
                        error_summary = %s,
                        finished_at = NOW(),
                        updated_at = NOW()
                    WHERE job_id = %s
                    """,
                    (
                        int(job.get("failed_count") or 0) + failed,
                        json.dumps(failed_items[:50], ensure_ascii=False) if failed_items else None,
                        payload.job_id,
                    ),
                )
                db.conn.commit()
            logger.info(
                "laptop-tool commit finished job_id=%s committed=%s failed=%s elapsed_ms=%s finalize=%s",
                payload.job_id,
                committed,
                failed,
                commit_elapsed_ms,
                bool(payload.finalize),
            )
            staging_cleared = False
            staging_cleanup_message = ""
            if payload.finalize and (int(job.get("failed_count") or 0) + failed) == 0:
                staging_cleared, staging_cleanup_message = _cleanup_laptop_tool_staging_dir(
                    payload.job_id,
                    job.get("staging_dir") or (LAPTOP_TOOL_STAGING_ROOT / payload.job_id),
                )
            elif payload.finalize:
                staging_cleanup_message = f"保留失敗 staging 以利排錯，failed_items={len(failed_items)}"
            else:
                staging_cleanup_message = "批次提交完成，保留剩餘 staging 以供後續續跑。"
            return {
                "job_id": payload.job_id,
                "committed_count": committed,
                "failed_count": failed,
                "status": "FAILED" if payload.finalize and (int(job.get("failed_count") or 0) + failed) > 0 else ("DONE" if payload.finalize else "RUNNING"),
                "copy_elapsed_ms": copy_elapsed_total_ms,
                "db_elapsed_ms": db_elapsed_total_ms,
                "commit_elapsed_ms": commit_elapsed_ms,
                "staging_cleared": staging_cleared,
                "staging_cleanup_message": staging_cleanup_message,
                "failed_items": failed_items,
                "finalize": bool(payload.finalize),
            }
        except Exception as e:
            if db and db.conn:
                with contextlib.suppress(Exception):
                    db.conn.rollback()
            return JSONResponse(status_code=500, content={"detail": f"提交批次失敗：{str(e)}"})
        finally:
            with contextlib.suppress(Exception):
                if cursor:
                    cursor.close()
            with contextlib.suppress(Exception):
                if db:
                    db.close()

    @app.post("/activity-photo-import/start")
    async def activity_photo_import_start(
        laptop_number: str = Form(""),
        schedule_id: int | None = Form(None),
        photographer: str = Form(""),
        enable_pyiqa: bool = Form(False),
        normalize_mode: str = Form("schedule"),
        source_folder: str = Form(""),
        output_folder: str = Form(""),
        backup_folder: str = Form(""),
        manifest_path: str = Form(""),
    ):
        try:
            ensure_activity_tables_once()
            started = time.perf_counter()
            payload = await start_import_activity_photos_job(
                laptop_number=laptop_number,
                schedule_id=schedule_id,
                photographer=photographer,
                enable_pyiqa=enable_pyiqa,
                normalize_mode=normalize_mode,
                source_folder=source_folder,
                output_folder=output_folder,
                backup_folder=backup_folder,
                manifest_path=manifest_path,
            )
            payload["start_response_ms"] = int((time.perf_counter() - started) * 1000)
            if payload.get("server_received_at"):
                payload["server_received_at_tpe"] = payload.get("server_received_at")
            return payload
        except FileNotFoundError as e:
            logger.error(f"活動照片匯入啟動失敗[MANIFEST_NOT_FOUND]: {str(e)}")
            return JSONResponse(status_code=400, content={"error_code": "MANIFEST_NOT_FOUND", "detail": f"活動照片匯入啟動失敗：{str(e)}"})
        except ValueError as e:
            logger.error(f"活動照片匯入啟動失敗[INVALID_INPUT]: {str(e)}")
            return JSONResponse(status_code=400, content={"error_code": "INVALID_INPUT", "detail": f"活動照片匯入啟動失敗：{str(e)}"})
        except RuntimeError as e:
            err_text = str(e)
            err_code = "IMPORT_START_RUNTIME_ERROR"
            if "資料庫連線失敗" in err_text or "MySQL" in err_text:
                err_code = "DB_CONNECTION_ERROR"
            elif "來源資料夾" in err_text or "路徑" in err_text:
                err_code = "SOURCE_PATH_ERROR"
            logger.error(f"活動照片匯入啟動失敗[{err_code}]: {err_text}")
            return JSONResponse(status_code=500, content={"error_code": err_code, "detail": f"活動照片匯入啟動失敗：{err_text}"})
        except Exception as e:
            logger.error(f"活動照片匯入啟動失敗[INTERNAL_ERROR]: {str(e)}")
            return JSONResponse(status_code=500, content={"error_code": "INTERNAL_ERROR", "detail": f"活動照片匯入啟動失敗：{str(e)}"})

    @app.post("/activity-photo-import/preview-source")
    async def activity_photo_import_preview_source(
        source_folder: str = Form(""),
    ):
        try:
            ensure_activity_tables_once()
            return preview_import_source_folder(source_folder=source_folder)
        except Exception as e:
            logger.error(f"活動照片來源資料夾預檢失敗: {str(e)}")
            return JSONResponse(status_code=500, content={"detail": f"活動照片來源資料夾預檢失敗：{str(e)}"})

    @app.post("/activity-photo-import/upload-manifest")
    async def activity_photo_import_upload_manifest(file: UploadFile = File(...)):
        try:
            ensure_activity_tables()
            suffix = Path(file.filename or "").suffix.lower()
            if suffix != ".json":
                return JSONResponse(status_code=400, content={"detail": "批次設定檔必須是 .json"})
            manifest_root = Path("/mnt/activity/ingest/_work/normalize/_ui_upload")
            manifest_root.mkdir(parents=True, exist_ok=True)
            safe_name = Path(file.filename or "manifest.json").name
            target_path = manifest_root / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{safe_name}"
            content = await file.read()
            target_path.write_bytes(content)
            payload = json.loads(target_path.read_text(encoding="utf-8"))
            source_folder = str(payload.get("source_folder") or r"C:\activity\ingest\normalized_success")
            windows_path = str(target_path).replace("/mnt/activity", "C:\\activity").replace("/", "\\")
            return {
                "manifest_path": windows_path,
                "source_folder": source_folder,
                "job_id": payload.get("job_id"),
            }
        except json.JSONDecodeError:
            return JSONResponse(status_code=400, content={"detail": "批次設定檔內容不是合法 JSON"})
        except Exception as e:
            logger.error(f"上傳批次設定檔失敗: {str(e)}")
            return JSONResponse(status_code=500, content={"detail": f"上傳批次設定檔失敗：{str(e)}"})

    @app.get("/activity-photo-import/latest-manifest")
    async def activity_photo_import_latest_manifest():
        try:
            manifest_root = Path("/mnt/activity/ingest/_work/normalize")
            if not manifest_root.exists():
                return JSONResponse(status_code=404, content={"detail": "尚未找到 manifest（目錄不存在）"})
            manifests = sorted(manifest_root.glob("*/manifest.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            if not manifests:
                return JSONResponse(status_code=404, content={"detail": "尚未找到 manifest"})
            latest = manifests[0]
            payload = json.loads(latest.read_text(encoding="utf-8"))
            return {
                "manifest_path": str(latest).replace("/mnt/activity", "C:\\activity").replace("/", "\\"),
                "source_folder": str(payload.get("source_folder") or "C:\\activity\\ingest\\normalized_success"),
                "job_id": payload.get("job_id"),
                "device_id": payload.get("device_id"),
                "photographer": payload.get("photographer"),
                "schedule_id": payload.get("schedule_id"),
                "normalize_mode": payload.get("normalize_mode"),
            }
        except Exception as e:
            return JSONResponse(status_code=500, content={"detail": f"讀取最新 manifest 失敗：{str(e)}"})

    @app.get("/activity-photo-import/jobs/{job_id}")
    async def activity_photo_import_job_status(job_id: str):
        payload = get_import_job_status(job_id)
        if not payload:
            return JSONResponse(status_code=404, content={"detail": "找不到指定匯入任務"})
        payload["last_polled_at"] = _now_tpe().strftime("%Y-%m-%d %H:%M:%S")
        payload["last_log_offset"] = None
        log_path = str(payload.get("log_path") or "").strip()
        if log_path:
            try:
                path_obj = Path(log_path)
                if path_obj.exists():
                    payload["last_log_offset"] = len(path_obj.read_text(encoding="utf-8-sig", errors="replace").splitlines())
            except Exception:
                payload["last_log_offset"] = None
        return payload

    @app.get("/activity-photo-import/jobs/{job_id}/logs")
    async def activity_photo_import_job_logs(job_id: str, offset: int = Query(0, ge=0)):
        payload = get_import_job_logs(job_id, offset=offset)
        if not payload:
            return JSONResponse(status_code=404, content={"detail": "找不到指定匯入任務"})
        payload["line_count"] = int(payload.get("next_offset") or 0)
        payload["server_log_read_at_tpe"] = _now_tpe().strftime("%Y-%m-%d %H:%M:%S")
        return payload

    @app.get("/activity-photo-import/jobs/{job_id}/items")
    async def activity_photo_import_job_items(job_id: str):
        payload = get_import_job_items(job_id)
        if not payload:
            return JSONResponse(status_code=404, content={"detail": "找不到指定匯入任務"})
        return payload

    @app.get("/activity-photo-import/jobs-recent")
    async def activity_photo_import_recent_jobs(
        limit: int = Query(30, ge=1, le=200),
        device_id: str = Query("", description="筆電編號；提供時只回該筆電任務"),
    ):
        db = None
        cursor = None
        try:
            ensure_activity_tables_once()
            db = mysqlconnector()
            db.connect()
            if db.conn is None:
                raise RuntimeError("資料庫連線失敗，請先確認 MySQL 容器與連線設定。")
            cursor = db.conn.cursor(dictionary=True)
            filters = ["job_type = 'import_reco'"]
            params = []
            did = str(device_id or "").strip()
            if did:
                filters.append("device_id = %s")
                params.append(did)
            where_clause = " AND ".join(filters)
            sql = f"""
                SELECT
                    job_id, status, total_count, processed_count, success_count, failed_count, skipped_count,
                    source_folder,
                    started_at, finished_at, updated_at
                FROM activity_import_job
                WHERE {where_clause}
                ORDER BY COALESCE(updated_at, started_at) DESC
                LIMIT %s
            """
            params.append(int(limit))
            cursor.execute(sql, tuple(params))
            rows = cursor.fetchall() or []
            items = []
            for row in rows:
                items.append(
                    {
                        "job_id": row.get("job_id") or "",
                        "status": row.get("status") or "",
                        "total_count": int(row.get("total_count") or 0),
                        "processed_count": int(row.get("processed_count") or 0),
                        "success_count": int(row.get("success_count") or 0),
                        "failed_count": int(row.get("failed_count") or 0),
                        "skipped_count": int(row.get("skipped_count") or 0),
                        "source_folder": row.get("source_folder") or "",
                        "started_at": _format_datetime_tpe(row.get("started_at")),
                        "started_at_tpe": _format_datetime_tpe(row.get("started_at")),
                        "finished_at": _format_datetime_tpe(row.get("finished_at")),
                        "updated_at": _format_datetime_tpe(row.get("updated_at")),
                    }
                )
            return {"items": items}
        except Exception as e:
            logger.error(f"讀取最近匯入任務清單失敗: {str(e)}")
            return JSONResponse(status_code=500, content={"detail": f"讀取最近匯入任務清單失敗：{str(e)}"})
        finally:
            with contextlib.suppress(Exception):
                if cursor:
                    cursor.close()
            with contextlib.suppress(Exception):
                if db:
                    db.close()

@app.post("/activity-photo-import-retry-failed")
async def activity_photo_import_retry_failed(limit: int = Form(100)):
        try:
            ensure_activity_tables()
            safe_limit = max(1, min(int(limit or 100), 1000))
            return retry_failed_activity_recognition(limit=safe_limit)
        except Exception as e:
            logger.error(f"活動照片辨識補跑失敗: {str(e)}")
            return JSONResponse(status_code=500, content={"detail": f"活動照片辨識補跑失敗：{str(e)}"})


@app.post("/query-recognitions-advanced/preview-count")
async def query_recognitions_advanced_preview_count(request: RecognitionPreviewCountRequest):
    try:
        total_count = query_advanced_preview_count(
            dept=request.dept,
            year=request.year,
            years=request.years,
            team=request.team,
            name=request.name,
            start_time=request.start_time,
            end_time=request.end_time,
            taken_start_time=request.taken_start_time,
            taken_end_time=request.taken_end_time,
            det_score_min=request.det_score_min,
            det_score_max=request.det_score_max,
            reco_count=request.reco_count,
            activity_schedule_id=request.activity_schedule_id,
            recognition_status=request.recognition_status,
            mark_type=request.mark_type,
            award_ids=request.award_ids,
            result_mode=request.result_mode,
        )
        return {"total_count": total_count, "threshold_default": 1000}
    except ValueError:
        return JSONResponse(status_code=400, content={"detail": "日期時間格式請使用 YYYY-MM-DDTHH:MM"})
    except Exception as e:
        logger.error(f"查詢總數預覽失敗: {str(e)}")
        return JSONResponse(status_code=500, content={"detail": f"查詢總數預覽失敗：{str(e)}"})


@app.get("/query-recognitions-advanced")
@app.get("/query-recognitions-advanced/")
async def query_recognitions_advanced(
    dept: list[str] = Query([], description="系所，可多選"),
    year: int | None = Query(None, description="級別"),
    years: list[int] = Query([], description="級別，可多選"),
    team: list[str] = Query([], description="小隊，可多選"),
    name: str = Query("", description="姓名"),
    start_time: str = Query("", description="檔案建立時間起 YYYY-MM-DDTHH:MM"),
    end_time: str = Query("", description="檔案建立時間迄 YYYY-MM-DDTHH:MM"),
    taken_start_time: str = Query("", description="拍攝時間起 YYYY-MM-DDTHH:MM"),
    taken_end_time: str = Query("", description="拍攝時間迄 YYYY-MM-DDTHH:MM"),
    det_score_min: float | None = Query(None, description="臉部偵測 det_score 起"),
    det_score_max: float | None = Query(None, description="臉部偵測 det_score 迄"),
    reco_count: int | None = Query(None, ge=0, description="辨識筆數"),
    recognition_status: str = Query("", description="辨識狀態：DONE/MIXED/FAILED/UNKNOWN/PENDING"),
    mark_type: str = Query("", description="標記篩選：all/award/video/both/none"),
    award_ids: list[int] = Query([], description="活動獎項 ID，可多選"),
    activity_schedule_id: int | None = Query(None, ge=1, description="活動行程 ID"),
    result_mode: str = Query("photo", description="顯示模式：photo/detail"),
    start_date: str = Query("", description="檔案建立時間起（舊欄位，相容用）"),
    end_date: str = Query("", description="檔案建立時間迄（舊欄位，相容用）"),
    page: int = Query(1, ge=1, description="頁碼"),
    limit: int = Query(20, ge=1, le=100, description="最多顯示筆數"),
):
    try:
        effective_start = start_time or start_date
        effective_end = end_time or end_date
        rows = query_advanced_recognition_records(
            dept=dept,
            year=year,
            years=years,
            team=team,
            name=name,
            start_time=effective_start,
            end_time=effective_end,
            taken_start_time=taken_start_time,
            taken_end_time=taken_end_time,
            det_score_min=det_score_min,
            det_score_max=det_score_max,
            reco_count=reco_count,
            recognition_status=recognition_status,
            mark_type=mark_type,
            award_ids=award_ids,
            activity_schedule_id=activity_schedule_id,
            page=page,
            limit=limit,
            result_mode=result_mode,
        )
        items = rows["items"]
        latest_photo_create_time = items[0]["photo_create_time"] if items else None
        latest_photo_taken_time = items[0]["photo_taken_time"] if items else None
        return {
            "filters": {
                "dept": dept,
                "year": year,
                "years": years,
                "team": team,
                "name": name,
                "start_time": effective_start,
                "end_time": effective_end,
                "taken_start_time": taken_start_time,
                "taken_end_time": taken_end_time,
                "det_score_min": det_score_min,
                "det_score_max": det_score_max,
                "reco_count": reco_count,
                "recognition_status": recognition_status,
                "mark_type": mark_type,
                "award_ids": award_ids,
                "activity_schedule_id": activity_schedule_id,
                "result_mode": result_mode,
                "page": page,
                "limit": limit,
            },
            "total": rows["total"],
            "page": rows["page"],
            "page_size": rows["page_size"],
            "total_pages": rows["total_pages"],
            "latest_photo_create_time": latest_photo_create_time,
            "latest_photo_taken_time": latest_photo_taken_time,
            "items": items,
        }
    except ValueError:
        return JSONResponse(status_code=400, content={"detail": "日期時間格式請使用 YYYY-MM-DDTHH:MM"})
    except Exception as e:
        logger.error(f"活動照片辨識查詢失敗: {str(e)}")
        return JSONResponse(status_code=500, content={"detail": "活動照片辨識查詢時發生錯誤"})


@app.get("/export-recognitions-csv")
async def export_recognitions_csv(
    dept: list[str] = Query([], description="系所，可多選"),
    year: int | None = Query(None, description="級別"),
    years: list[int] = Query([], description="級別，可多選"),
    team: list[str] = Query([], description="小隊，可多選"),
    name: str = Query("", description="姓名"),
    start_time: str = Query("", description="檔案建立時間起 YYYY-MM-DDTHH:MM"),
    end_time: str = Query("", description="檔案建立時間迄 YYYY-MM-DDTHH:MM"),
    taken_start_time: str = Query("", description="拍攝時間起 YYYY-MM-DDTHH:MM"),
    taken_end_time: str = Query("", description="拍攝時間迄 YYYY-MM-DDTHH:MM"),
    det_score_min: float | None = Query(None, description="臉部偵測 det_score 起"),
    det_score_max: float | None = Query(None, description="臉部偵測 det_score 迄"),
    reco_count: int | None = Query(None, ge=0, description="辨識筆數"),
    recognition_status: str = Query("", description="辨識狀態：DONE/MIXED/FAILED/UNKNOWN/PENDING"),
    mark_type: str = Query("", description="標記篩選：all/award/video/both/none"),
    award_ids: list[int] = Query([], description="活動獎項 ID，可多選"),
    activity_schedule_id: int | None = Query(None, ge=1, description="活動行程 ID"),
    start_date: str = Query("", description="檔案建立時間起（舊欄位，相容用）"),
    end_date: str = Query("", description="檔案建立時間迄（舊欄位，相容用）"),
    limit: int = Query(100, ge=1, le=1000, description="最多匯出筆數"),
):
    try:
        effective_start = start_time or start_date
        effective_end = end_time or end_date
        items = query_advanced_recognition_records(
            dept=dept,
            year=year,
            years=years,
            team=team,
            name=name,
            start_time=effective_start,
            end_time=effective_end,
            taken_start_time=taken_start_time,
            taken_end_time=taken_end_time,
            det_score_min=det_score_min,
            det_score_max=det_score_max,
            reco_count=reco_count,
            recognition_status=recognition_status,
            mark_type=mark_type,
            award_ids=award_ids,
            activity_schedule_id=activity_schedule_id,
            limit=limit,
            result_mode="detail",
        )["items"]

        output = io.StringIO()
        writer = csv.DictWriter(
            output,
            fieldnames=[
                "dept",
                "year",
                "team",
                "name",
                "matched_person",
                "photo_uuid",
                "photo_taken_time",
                "photo_create_time",
                "record_create_time",
                "origin_full_path",
                "thumbs_full_path",
                "reco_count",
                "reco_unknow",
                "recognition_status",
                "reco_error",
                "is_unknown",
                "reco_name",
                "reco_res",
                "update_time",
            ],
        )
        writer.writeheader()
        writer.writerows(build_csv_rows(items))
        output.seek(0)

        filename = f"recognitions_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        headers = {"Content-Disposition": f"attachment; filename={filename}"}
        return StreamingResponse(iter([output.getvalue()]), media_type="text/csv; charset=utf-8", headers=headers)
    except ValueError:
        return JSONResponse(status_code=400, content={"detail": "日期時間格式請使用 YYYY-MM-DDTHH:MM"})
    except Exception as e:
        logger.error(f"匯出 CSV 失敗: {str(e)}")
        return JSONResponse(status_code=500, content={"detail": "匯出 CSV 時發生錯誤"})


@app.get("/encoding-health")
async def encoding_health_check():
    db_charset = {}
    db_error = ""
    try:
        db = mysqlconnector()
        db.connect()
        if db.conn is None:
            raise RuntimeError("資料庫連線失敗")
        cursor = db.conn.cursor(dictionary=True)
        try:
            cursor.execute("SELECT @@character_set_server AS character_set_server, @@collation_server AS collation_server")
            server_row = cursor.fetchone() or {}
            cursor.execute(
                """
                SELECT @@character_set_client AS character_set_client,
                       @@character_set_connection AS character_set_connection,
                       @@character_set_results AS character_set_results,
                       @@collation_connection AS collation_connection
                """
            )
            conn_row = cursor.fetchone() or {}
            db_charset = {**server_row, **conn_row}
        finally:
            cursor.close()
            db.close()
    except Exception as exc:
        db_error = str(exc)

    log_issues = _collect_recent_log_issues()
    utf8_ready = not db_error and all(
        "utf8mb4" in str(v).lower() for k, v in db_charset.items() if "character_set" in k or "collation" in k
    )

    return {
        "status": "ok" if utf8_ready and not log_issues else "warning",
        "utf8_ready": utf8_ready,
        "db_charset": db_charset,
        "db_error": db_error,
        "log_issue_count": len(log_issues),
        "log_issues": log_issues,
        "runtime_env": {
            "LANG": os.getenv("LANG", ""),
            "LC_ALL": os.getenv("LC_ALL", ""),
            "PYTHONUTF8": os.getenv("PYTHONUTF8", ""),
            "PYTHONIOENCODING": os.getenv("PYTHONIOENCODING", ""),
        },
    }


@app.post("/image_socre")
async def async_image_socre(file_path: str):
    return image_socre(file_path)


@app.post("/async-recognize/")
async def async_recognize(
    file_path: str = Query(..., min_length=3, example="/path/to/image.jpg"),
    label_face_name: bool = Query(False),
    background_tasks: BackgroundTasks = BackgroundTasks(),
):
    if not os.path.isfile(file_path):
        return JSONResponse(
            status_code=404,
            content={"status": "error", "message": "找不到指定的照片檔案。"},
        )

    try:
        background_tasks.add_task(sync_processing_wrapper, file_path, label_face_name)
        return JSONResponse(
            status_code=202,
            content={
                "status": "accepted",
                "details": {
                    "file_path": file_path,
                    "processing_mode": "background_task",
                },
            },
        )
    except Exception as e:
        logger.error(f"async-recognize API 處理失敗: {str(e)}")
        return JSONResponse(
            status_code=500,
            content={"status": "error", "message": "活動照片辨識請求失敗。"},
        )

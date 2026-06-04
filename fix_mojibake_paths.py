#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
一次性修復資料庫內既有 mojibake 路徑字串（不改檔案本身）。

預設修復欄位：
- img_upload.origin_full_path
- img_upload.thumbs_full_path
- reco_result.origin_full_path
- reco_result.thumbs_full_path
- reco_delete_log.origin_full_path

用法：
1) 先預覽（不寫入）
   python fix_mojibake_paths.py --dry-run

2) 實際寫入
   python fix_mojibake_paths.py --apply
"""

from __future__ import annotations

import argparse
import csv
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

try:
    import mysql.connector
except ModuleNotFoundError as exc:
    raise SystemExit(
        "缺少 mysql 套件。請先安裝：\n"
        "  python -m pip install mysql-connector-python\n"
        "或改在 noob 容器內執行本腳本。"
    ) from exc


ROOT_DIR = Path(__file__).resolve().parent
LOG_DIR = ROOT_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

MOJIBAKE_HINT_RE = re.compile(r"[ÃÂâäåæçéèêëïîðñóôõöúûüýþÿ]|æ|ä|å|ç")


@dataclass
class DbConfig:
    host: str
    port: int
    user: str
    password: str
    database: str


def load_db_config() -> DbConfig:
    # 優先吃環境變數
    host = os.getenv("MYSQL_HOST")
    port = os.getenv("MYSQL_PORT")
    user = os.getenv("MYSQL_USER")
    pwd = os.getenv("MYSQL_PWD")
    db = os.getenv("MYSQL_DB")
    if host and port and user and pwd and db:
        return DbConfig(host=host, port=int(port), user=user, password=pwd, database=db)

    # 其次讀 shared/config.py
    import importlib.util

    config_path = ROOT_DIR / "shared" / "config.py"
    spec = importlib.util.spec_from_file_location("shared_config", str(config_path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"無法讀取 DB 設定檔：{config_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[attr-defined]

    return DbConfig(
        host=str(getattr(module, "MYSQL_HOST")),
        port=int(getattr(module, "MYSQL_PORT")),
        user=str(getattr(module, "MYSQL_USER")),
        password=str(getattr(module, "MYSQL_PWD")),
        database=str(getattr(module, "MYSQL_DB")),
    )


def repair_mojibake_text(text: str) -> str:
    raw = str(text or "")
    if not raw:
        return raw
    try:
        fixed = raw.encode("latin1").decode("utf-8")
    except Exception:
        return raw
    return fixed or raw


def should_try_repair(text: str) -> bool:
    if not text:
        return False
    return bool(MOJIBAKE_HINT_RE.search(text))


def rows(cursor) -> Iterable[dict]:
    for row in cursor:
        yield dict(row)


def table_exists(cur, table_name: str) -> bool:
    cur.execute("SHOW TABLES LIKE %s", (table_name,))
    return cur.fetchone() is not None


def column_exists(cur, table_name: str, column_name: str) -> bool:
    cur.execute(f"SHOW COLUMNS FROM {table_name} LIKE %s", (column_name,))
    return cur.fetchone() is not None


def main() -> int:
    parser = argparse.ArgumentParser(description="批次修復 DB 路徑 mojibake 字串")
    parser.add_argument("--dry-run", action="store_true", help="只預覽，不寫入")
    parser.add_argument("--apply", action="store_true", help="實際寫入")
    parser.add_argument("--limit", type=int, default=200000, help="每欄最多掃描筆數")
    args = parser.parse_args()

    if args.dry_run and args.apply:
        raise SystemExit("請擇一使用 --dry-run 或 --apply")
    do_apply = bool(args.apply and not args.dry_run)

    cfg = load_db_config()
    conn = mysql.connector.connect(
        host=cfg.host,
        port=cfg.port,
        user=cfg.user,
        password=cfg.password,
        database=cfg.database,
        autocommit=False,
    )
    cur = conn.cursor(dictionary=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_csv = LOG_DIR / f"fix_mojibake_paths_{stamp}.csv"
    report_log = LOG_DIR / f"fix_mojibake_paths_{stamp}.log"

    targets = [
        ("img_upload", "id", "origin_full_path"),
        ("img_upload", "id", "thumbs_full_path"),
        ("reco_result", "id", "origin_full_path"),
        ("reco_result", "id", "thumbs_full_path"),
        ("reco_delete_log", "id", "origin_full_path"),
    ]

    updated_total = 0
    scanned_total = 0
    skipped_total = 0
    update_sql_cache: dict[tuple[str, str, str], str] = {}

    with report_csv.open("w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.writer(fp)
        writer.writerow(["table", "id", "column", "before", "after", "applied"])

        for table_name, pk_name, col_name in targets:
            if not table_exists(cur, table_name):
                continue
            if not column_exists(cur, table_name, col_name):
                continue

            cur.execute(
                f"""
                SELECT {pk_name} AS rid, {col_name} AS raw_value
                FROM {table_name}
                WHERE {col_name} IS NOT NULL AND {col_name} <> ''
                ORDER BY {pk_name} ASC
                LIMIT %s
                """,
                (args.limit,),
            )
            for row in rows(cur):
                scanned_total += 1
                rid = row["rid"]
                raw_value = str(row["raw_value"] or "")
                if not should_try_repair(raw_value):
                    skipped_total += 1
                    continue
                fixed_value = repair_mojibake_text(raw_value)
                if fixed_value == raw_value:
                    skipped_total += 1
                    continue

                applied = "N"
                if do_apply:
                    key = (table_name, col_name, pk_name)
                    if key not in update_sql_cache:
                        update_sql_cache[key] = f"UPDATE {table_name} SET {col_name} = %s WHERE {pk_name} = %s"
                    cur.execute(update_sql_cache[key], (fixed_value, rid))
                    applied = "Y"
                updated_total += 1
                writer.writerow([table_name, rid, col_name, raw_value, fixed_value, applied])

    if do_apply:
        conn.commit()
    else:
        conn.rollback()

    with report_log.open("w", encoding="utf-8-sig", newline="\n") as fp:
        fp.write(f"time: {datetime.now().isoformat(sep=' ', timespec='seconds')}\n")
        fp.write(f"mode: {'APPLY' if do_apply else 'DRY-RUN'}\n")
        fp.write(f"scanned_total: {scanned_total}\n")
        fp.write(f"updated_total: {updated_total}\n")
        fp.write(f"skipped_total: {skipped_total}\n")
        fp.write(f"report_csv: {report_csv}\n")

    cur.close()
    conn.close()

    print(f"mode={'APPLY' if do_apply else 'DRY-RUN'} scanned={scanned_total} updated={updated_total} skipped={skipped_total}")
    print(f"CSV: {report_csv}")
    print(f"LOG: {report_log}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

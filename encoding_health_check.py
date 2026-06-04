import json
import os
from pathlib import Path

from service.tools.mysql_utils import mysqlconnector

LOG_DIR = Path(__file__).resolve().parent / "logs"
MOJIBAKE_PATTERNS = ("ä", "å", "ç", "è", "é", "æ", "Ã", "Â", "ðŸ", "ï¼", "ï½", "???", "�")


def looks_like_mojibake(text: str) -> bool:
    value = str(text or "")
    return any(pattern in value for pattern in MOJIBAKE_PATTERNS)


def collect_log_issues():
    issues = []
    if not LOG_DIR.exists():
        return issues
    for log_file in sorted(LOG_DIR.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:10]:
        try:
            lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()[-500:]
        except Exception:
            continue
        for idx, line in enumerate(lines, 1):
            if looks_like_mojibake(line):
                issues.append({"file": str(log_file), "line": idx, "snippet": line[:220]})
                if len(issues) >= 50:
                    return issues
    return issues


def db_charset():
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
        return {**server_row, **conn_row}
    finally:
        cursor.close()
        db.close()


def main():
    charset_info = {}
    db_error = ""
    try:
        charset_info = db_charset()
    except Exception as exc:
        db_error = str(exc)

    log_issues = collect_log_issues()
    payload = {
        "status": "ok" if not db_error and not log_issues else "warning",
        "db_charset": charset_info,
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
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

@echo off
setlocal
chcp 65001 >nul

set "BASE_DIR=%~dp0"
echo 啟動 Server 版活動照片匯入工具...
python "%BASE_DIR%windows_activity_import_server_tool.py"

if errorlevel 1 (
  echo 啟動失敗，請確認 Python 環境與檔案存在。
  pause
)


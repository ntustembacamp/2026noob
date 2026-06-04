$ErrorActionPreference = "Stop"

$now = Get-Date
$logDir = Join-Path $PSScriptRoot "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logPath = Join-Path $logDir ("cleanup_temp_files_{0}.log" -f $now.ToString("yyyyMMdd_HHmmss"))

function Write-Log {
    param([string]$Message)
    $line = "[{0}] {1}" -f (Get-Date).ToString("yyyy-MM-dd HH:mm:ss"), $Message
    $line | Tee-Object -FilePath $logPath -Append
}

Write-Log "開始清理暫存檔"

$targets = @(
    @{ Path = "C:\uploadsource\normalized_success"; KeepDays = 1; Type = "file" },
    @{ Path = "C:\uploadsource\imgupload_fail"; KeepDays = 1; Type = "file" },
    @{ Path = "C:\Users\Test\Desktop\Codex\AI人臉辨識\noob\database\activity_import_tmp"; KeepDays = 0; Type = "dir" },
    @{ Path = "C:\Users\Test\Desktop\Codex\AI人臉辨識\noob\database\uploadsource\normalized_success"; KeepDays = 0; Type = "file" }
)

$deletedCount = 0

foreach ($targetItem in $targets) {
    $path = $targetItem.Path
    $days = [int]$targetItem.KeepDays
    $type = $targetItem.Type

    if (-not (Test-Path $path)) {
        Write-Log "略過不存在路徑: $path"
        continue
    }

    $cutoff = (Get-Date).AddDays(-$days)
    Write-Log "掃描路徑: $path (保留 $days 天內資料)"

    if ($type -eq "dir") {
        $items = Get-ChildItem -Path $path -Directory -ErrorAction SilentlyContinue | Where-Object { $_.LastWriteTime -lt $cutoff }
        foreach ($item in $items) {
            try {
                Remove-Item -LiteralPath $item.FullName -Recurse -Force -ErrorAction Stop
                $deletedCount++
                Write-Log "已刪除資料夾: $($item.FullName)"
            }
            catch {
                Write-Log "刪除失敗(資料夾): $($item.FullName) - $($_.Exception.Message)"
            }
        }
    }
    else {
        $items = Get-ChildItem -Path $path -File -ErrorAction SilentlyContinue | Where-Object { $_.LastWriteTime -lt $cutoff }
        foreach ($item in $items) {
            try {
                Remove-Item -LiteralPath $item.FullName -Force -ErrorAction Stop
                $deletedCount++
                Write-Log "已刪除檔案: $($item.FullName)"
            }
            catch {
                Write-Log "刪除失敗(檔案): $($item.FullName) - $($_.Exception.Message)"
            }
        }
    }
}

Write-Log "清理完成，刪除數量: $deletedCount"
Write-Log "Log 路徑: $logPath"

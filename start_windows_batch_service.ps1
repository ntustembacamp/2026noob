$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$logDir = Join-Path $projectRoot "logs"
$legacyLogDir = Join-Path $projectRoot "database\logs"
$logPath = Join-Path $logDir "windows_batch_service.log"
$errPath = Join-Path $logDir "windows_batch_service.error.log"
$psExe = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"

New-Item -ItemType Directory -Force $logDir | Out-Null

function Test-ServiceHealth {
    try {
        $health = Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:8010/health" -TimeoutSec 2
        return ($health.StatusCode -eq 200)
    } catch {
        return $false
    }
}

function Stop-LegacyBatchServiceProcesses {
    try {
        $processes = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction Stop
        foreach ($proc in $processes) {
            $cmd = [string]$proc.CommandLine
            if ($cmd -match "uvicorn\s+windows_batch_service:app" -and $proc.ProcessId) {
                Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
            }
        }
    } catch {}
}

function Get-RecentLegacyLogCount {
    if (-not (Test-Path $legacyLogDir)) { return 0 }
    $threshold = (Get-Date).AddMinutes(-1)
    try {
        return (Get-ChildItem -Path $legacyLogDir -Filter *.log -File -ErrorAction SilentlyContinue | Where-Object { $_.LastWriteTime -ge $threshold }).Count
    } catch {
        return 0
    }
}

if (Test-ServiceHealth) {
    Write-Output "Windows batch service is already running."
    exit 0
}

Stop-LegacyBatchServiceProcesses

$command = @"
Set-Location -LiteralPath '$projectRoot'
\$env:PYTHONUTF8='1'
\$env:PYTHONIOENCODING='utf-8'
\$env:LANG='C.UTF-8'
\$env:LC_ALL='C.UTF-8'
python -X utf8 -m uvicorn windows_batch_service:app --host 127.0.0.1 --port 8010 *>> '$logPath' 2>> '$errPath'
"@

try {
    Start-Process `
        -FilePath $psExe `
        -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-Command", $command) `
        -WindowStyle Hidden `
        -ErrorAction Stop | Out-Null
} catch {
    Write-Output "Background launch failed, fallback to foreground launch."
    Write-Output "Error: $($_.Exception.Message)"
    Set-Location -LiteralPath $projectRoot
    $env:PYTHONUTF8='1'
    $env:PYTHONIOENCODING='utf-8'
    $env:LANG='C.UTF-8'
    $env:LC_ALL='C.UTF-8'
    python -X utf8 -m uvicorn windows_batch_service:app --host 127.0.0.1 --port 8010
    exit $LASTEXITCODE
}

Start-Sleep -Seconds 2
if (Test-ServiceHealth) {
    Write-Output "Windows batch service started on http://127.0.0.1:8010"
    $legacyCount = Get-RecentLegacyLogCount
    if ($legacyCount -gt 0) {
        Write-Output "WARNING: detected $legacyCount legacy log writes in $legacyLogDir within last 1 minute. Old process may still exist."
    }
    exit 0
}

Write-Output "Windows batch service started but health check failed. Please check logs:"
Write-Output "LOG: $logPath"
Write-Output "ERR: $errPath"
exit 1

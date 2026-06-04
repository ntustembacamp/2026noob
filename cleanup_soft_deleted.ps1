[CmdletBinding()]
param(
    [int]$RetentionDays = 0,
    [int]$Limit = 1000,
    [string]$BaseUrl = "http://127.0.0.1:8000",
    [switch]$WhatIf
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$logDir = Join-Path $scriptDir "logs"
if (-not (Test-Path -LiteralPath $logDir)) {
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
}

$logPath = Join-Path $logDir ("cleanup_soft_deleted_{0}.log" -f (Get-Date -Format "yyyyMMdd_HHmmss"))

function Write-Log {
    param([string]$Message)
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Write-Host $line
    $line | Out-File -LiteralPath $logPath -Append -Encoding utf8
}

if ($RetentionDays -lt 0) { $RetentionDays = 0 }
if ($Limit -lt 1) { $Limit = 1 }

$base = $BaseUrl.TrimEnd("/")
$apiUrl = "{0}/admin/purge-soft-deleted?retention_days={1}`&limit={2}" -f $base, $RetentionDays, $Limit

Write-Log "Start cleanup soft-deleted records"
Write-Log ("Params: retention_days={0}, limit={1}, base_url={2}, whatif={3}" -f $RetentionDays, $Limit, $base, $WhatIf.IsPresent)

if ($WhatIf.IsPresent) {
    Write-Log ("WhatIf mode, API URL: {0}" -f $apiUrl)
    Write-Log ("Log path: {0}" -f $logPath)
    exit 0
}

try {
    Write-Log ("Call API: {0}" -f $apiUrl)
    $response = Invoke-RestMethod -Method Post -Uri $apiUrl -TimeoutSec 600
    $json = $response | ConvertTo-Json -Depth 8
    Write-Log ("API response: {0}" -f $json)
    if ($response.deleted_file_paths_display) {
        Write-Log ("Deleted files (display): {0}" -f (($response.deleted_file_paths_display -join " | ")))
    }
    if ($response.missing_file_paths_display) {
        Write-Log ("Missing files (display): {0}" -f (($response.missing_file_paths_display -join " | ")))
    }
    if ($response.failed_file_paths_display) {
        Write-Log ("Failed files (display): {0}" -f (($response.failed_file_paths_display -join " | ")))
    }
    Write-Log (
        "Done: scanned={0}, scanned_from_reco_result={1}, scanned_from_img_upload_only={2}, purged_reco_result={3}, purged_img_upload={4}, deleted_files={5}, missing_files={6}, failed_files={7}" -f `
        ($response.scanned), `
        ($response.scanned_from_reco_result), `
        ($response.scanned_from_img_upload_only), `
        ($response.purged_reco_result), `
        ($response.purged_img_upload), `
        ($response.deleted_files), `
        ($response.missing_files), `
        ($response.failed_files)
    )
}
catch {
    $msg = $_.Exception.Message
    Write-Log ("Failed: {0}" -f $msg)
    if ($_.ErrorDetails -and $_.ErrorDetails.Message) {
        Write-Log ("Error detail: {0}" -f $_.ErrorDetails.Message)
    }
    Write-Log ("Log path: {0}" -f $logPath)
    exit 1
}

Write-Log ("Log path: {0}" -f $logPath)

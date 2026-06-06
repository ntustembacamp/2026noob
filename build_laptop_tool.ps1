param(
    [ValidateSet("core", "addon", "all")]
    [string]$Target = "all"
)
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

function Get-PreferredIPv4 {
    try {
        $configs = Get-NetIPConfiguration -ErrorAction Stop | Where-Object { $_.IPv4Address -and $_.NetAdapter.Status -eq "Up" }
    }
    catch {
        $configs = @()
    }
    if (-not $configs) { return $null }

    $wired = $configs | Where-Object {
        $_.NetAdapter.HardwareInterface -eq $true -and
        $_.NetAdapter.InterfaceDescription -notmatch 'Wi-?Fi|Wireless|WLAN|Virtual|VMware|Hyper-V|Loopback|Bluetooth'
    } | Select-Object -First 1
    if ($wired) { return $wired.IPv4Address.IPAddress }

    $wifi = $configs | Where-Object {
        $_.NetAdapter.InterfaceDescription -match 'Wi-?Fi|Wireless|WLAN'
    } | Select-Object -First 1
    if ($wifi) { return $wifi.IPv4Address.IPAddress }

    return ($configs | Select-Object -First 1).IPv4Address.IPAddress
}

function Get-FallbackIPv4 {
    try {
        $ips = [System.Net.Dns]::GetHostAddresses([System.Net.Dns]::GetHostName()) |
            Where-Object { $_.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork } |
            Select-Object -ExpandProperty IPAddressToString
        foreach ($ip in $ips) {
            if ($ip -and $ip -ne "127.0.0.1") {
                return $ip
            }
        }
    }
    catch {}
    return $null
}

$baseDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$toolScript = Join-Path $baseDir "windows_activity_import_laptop_tool.py"
$specPath = Join-Path $baseDir "laptop_activity_import_tool.spec"
$distRoot = Join-Path $baseDir "dist"
$buildRoot = Join-Path $baseDir "build"
$finalDir = Join-Path $distRoot "laptop_tool"
$rawOutputDir = Join-Path $distRoot "laptop_activity_import_tool"

if (-not (Test-Path -LiteralPath $toolScript)) {
    throw "Tool script not found: $toolScript"
}
if (-not (Test-Path -LiteralPath $specPath)) {
    throw "Spec file not found: $specPath"
}

Write-Host "=== Build laptop tool (target: $Target) ===" -ForegroundColor Cyan

try {
    python -m PyInstaller --version | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller is not installed."
    }
}
catch {
    throw "PyInstaller is not installed."
}

if (Test-Path -LiteralPath $finalDir) {
    Remove-Item -LiteralPath $finalDir -Recurse -Force
}
if (Test-Path -LiteralPath $rawOutputDir) {
    Remove-Item -LiteralPath $rawOutputDir -Recurse -Force
}

$pyiArgs = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--distpath", $distRoot,
    "--workpath", $buildRoot,
    $specPath
)

if ($Target -in @("core", "all")) {
    python @pyiArgs
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller build failed."
    }
    if (-not (Test-Path -LiteralPath $rawOutputDir)) {
        throw "Build finished but output folder not found: $rawOutputDir"
    }
    Move-Item -LiteralPath $rawOutputDir -Destination $finalDir
}

$requiredToolModule = Join-Path $finalDir "_internal\\tools\\new_face_laptop.py"
if ($Target -in @("core", "all")) {
    if (-not (Test-Path -LiteralPath $requiredToolModule)) {
        throw "Build verification failed: missing $requiredToolModule"
    }
}

if ($Target -in @("core", "all")) {
    $cfgDir = Join-Path $finalDir "configs"
    if (-not (Test-Path -LiteralPath $cfgDir)) {
        New-Item -ItemType Directory -Path $cfgDir | Out-Null
    }
    $cfgPath = Join-Path $cfgDir "activity_normalize_config.json"
    $serverIp = Get-PreferredIPv4
    if (-not $serverIp) { $serverIp = Get-FallbackIPv4 }
    if (-not $serverIp) { $serverIp = "<SERVER-IP>" }
    $serverBase = "http://$serverIp`:8000"

$cfgTemplate = @"
{
  "version": "1.0",
  "server_api_base": "$serverBase",
  "public_base_url": "$serverBase",
  "tool_release_page": "$serverBase/laptop-tool-admin",
  "default_activity_code": "",
  "default_photographer": "",
  "activities": [],
  "photographers": [],
  "naming_rules": {
    "mode_a_template": "EXIF_{activity_code_or_000}_{device_id}_{photographer}_{taken_yyyymmdd_hhmmss}_{origin_stem}.jpg",
    "mode_b_template": "NONEXIF_{activity_code}_{device_id}_{photographer}_{file_yyyymmdd_hhmmss}_{origin_stem}.jpg"
  }
}
"@
    $cfgTemplate | Out-File -LiteralPath $cfgPath -Encoding utf8

    $versionPath = Join-Path $finalDir "version.json"
    $versionPayload = @{
      version = (Get-Date -Format "yyyy.MM.dd.HHmm")
      build_time = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
      build_time_tz = "Asia/Taipei"
      package_type = "core"
    }
    $versionPayload | ConvertTo-Json -Depth 3 | Out-File -LiteralPath $versionPath -Encoding utf8

    $reportPath = Join-Path $finalDir "build_report.json"
    $report = @{
      version = $versionPayload.version
      build_time = $versionPayload.build_time
      build_time_tz = $versionPayload.build_time_tz
      includes = @("cv2", "numpy", "insightface", "onnxruntime", "tools.new_face_laptop", "embedding:faces_embedding_antelopev2.pkl")
    }
    $report | ConvertTo-Json -Depth 3 | Out-File -LiteralPath $reportPath -Encoding utf8

    $zipName = "laptop_tool_core_{0}.zip" -f (Get-Date -Format "yyyyMMdd_HHmmss")
    $zipPath = Join-Path $distRoot $zipName
    if (Test-Path -LiteralPath $zipPath) {
        Remove-Item -LiteralPath $zipPath -Force
    }
    Compress-Archive -Path (Join-Path $finalDir "*") -DestinationPath $zipPath -CompressionLevel Optimal

    Write-Host "Core Done: $finalDir" -ForegroundColor Green
    Write-Host "Core ZIP: $zipPath" -ForegroundColor Green
}

if ($Target -in @("addon", "all")) {
    $addonSrc = Join-Path $baseDir "addon_pyiqa"
    if (Test-Path -LiteralPath $addonSrc) {
        $addonZip = Join-Path $distRoot ("laptop_tool_pyiqa_addon_{0}.zip" -f (Get-Date -Format "yyyyMMdd_HHmmss"))
        if (Test-Path -LiteralPath $addonZip) { Remove-Item -LiteralPath $addonZip -Force }
        Compress-Archive -Path (Join-Path $addonSrc "*") -DestinationPath $addonZip -CompressionLevel Optimal
        Write-Host "Addon ZIP: $addonZip" -ForegroundColor Green
    }
    else {
        Write-Host "Addon source not found: $addonSrc (skip addon zip)" -ForegroundColor Yellow
    }
}

Write-Host "Upload dist/laptop_tool* ZIP to /mnt/activity/laptop_tool/packages for download." -ForegroundColor Yellow

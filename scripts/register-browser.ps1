# Registers the IDM Clone native messaging host for Chrome and Edge so the browser
# extension can talk to the desktop app. Run again after moving the app.
#
#   powershell -ExecutionPolicy Bypass -File scriptsegister-browser.ps1            # packaged app (after install.ps1 / build-exe.ps1)
#   powershell -ExecutionPolicy Bypass -File scriptsegister-browser.ps1 -Dev       # run from this source tree with the venv
#   powershell -ExecutionPolicy Bypass -File scriptsegister-browser.ps1 -Unregister
param([switch]$Dev, [switch]$Unregister)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$hostName = "com.idmclone.host"
$keyInfo = Get-Content (Join-Path $root "extension\.extension-key.json") | ConvertFrom-Json
$extId = $keyInfo.id
$hostDir = Join-Path $env:LOCALAPPDATA "Programs\IDM Clone\host"
$manifestPath = Join-Path $hostDir "$hostName.json"

$regPaths = @(
    "HKCU:\Software\Google\Chrome\NativeMessagingHosts\$hostName",
    "HKCU:\Software\Microsoft\Edge\NativeMessagingHosts\$hostName",
    "HKCU:\Software\Chromium\NativeMessagingHosts\$hostName",
    "HKCU:\Software\BraveSoftware\Brave-Browser\NativeMessagingHosts\$hostName"
)

if ($Unregister) {
    foreach ($p in $regPaths) { if (Test-Path $p) { Remove-Item -Path $p -Recurse -Force } }
    if (Test-Path $hostDir) { Remove-Item -Recurse -Force $hostDir }
    Write-Host "Native host unregistered."
    exit 0
}

New-Item -ItemType Directory -Force $hostDir | Out-Null

if ($Dev) {
    # A .cmd wrapper that runs the host from the source tree with the venv interpreter.
    $py = Join-Path $root ".venv\Scripts\python.exe"
    if (-not (Test-Path $py)) { throw "No .venv found; create it first (see README)." }
    # Chrome needs the module importable: the wrapper sets PYTHONPATH to the project dir.
    $cmd = Join-Path $hostDir "idm-host.cmd"
    @("@echo off", "set PYTHONPATH=$root\src", "`"$py`" -m idm.native_host %*") | Set-Content -Path $cmd -Encoding ascii
    $hostExe = $cmd
    # Make the app launchable on demand from the source tree.
    $appCmd = "`"$py`" -m idm"
} else {
    $hostExe = Join-Path $hostDir "idm-host.exe"
    $built = Join-Path $root "release\idm-host.exe"
    if (-not (Test-Path $built)) { $built = Join-Path $root "dist\idm-host\idm-host.exe" }
    if (-not (Test-Path $built)) { throw "idm-host.exe not built yet. Run build-exe.ps1 first." }
    Copy-Item -Force $built $hostExe
    $appExe = Join-Path $env:LOCALAPPDATA "Programs\IDM Clone\IDM Clone.exe"
    if (-not (Test-Path $appExe)) { $appExe = Join-Path $root "release\IDM Clone.exe" }
    $appCmd = "`"$appExe`""
}

# Record how to launch the app so the host can start it when the browser needs it.
$dataDir = Join-Path $env:APPDATA "idm-clone"
New-Item -ItemType Directory -Force $dataDir | Out-Null
Set-Content -Path (Join-Path $dataDir "app_path.txt") -Value $appCmd -Encoding utf8

$manifest = @{
    name = $hostName
    description = "IDM Clone native messaging host"
    path = $hostExe
    type = "stdio"
    allowed_origins = @("chrome-extension://$extId/")
} | ConvertTo-Json
Set-Content -Path $manifestPath -Value $manifest -Encoding ascii

foreach ($p in $regPaths) {
    New-Item -Path $p -Force | Out-Null
    Set-ItemProperty -Path $p -Name "(Default)" -Value $manifestPath
}

Write-Host ""
Write-Host "Native host registered for Chrome, Edge, Chromium and Brave."
Write-Host "Host manifest: $manifestPath"
Write-Host ""
Write-Host "Now load the extension:"
Write-Host "  1. Open chrome://extensions (or edge://extensions) and turn on Developer mode."
Write-Host "  2. Click 'Load unpacked' and choose: $(Join-Path $root 'extension')"
Write-Host "  3. The extension ID must be: $extId"

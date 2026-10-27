# Builds the Windows installer:
#   1. PyInstaller one-folder app  -> dist\IDM Clone\
#   2. PyInstaller one-file host   -> dist\idm-host.exe
#   3. Inno Setup                  -> release\IDM-Clone-Setup-<version>.exe
#   4. Portable one-file app       -> release\IDM Clone.exe
#
# Needs Inno Setup 6 (https://jrsoftware.org/isdl.php) for step 3; the other steps run without it.
#
#   powershell -ExecutionPolicy Bypass -File scripts\build-installer.ps1

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

$version = & $py -c "import re,pathlib;print(re.search(r'__version__\s*=\s*\"([^\"]+)\"', pathlib.Path(r'$root\src\idm\__init__.py').read_text()).group(1))"
Write-Host "Building IDM Clone $version"

# Version resources for the exe properties
& $py (Join-Path $root "packaging\version_info.py") (Join-Path $root "build\version_app.txt") app
& $py (Join-Path $root "packaging\version_info.py") (Join-Path $root "build\version_host.txt") host

# 1. App (one folder: fastest start, friendliest to Smart App Control)
& $py -m PyInstaller --noconfirm --clean --windowed `
    --name "IDM Clone" `
    --icon (Join-Path $root "assets\icon.ico") `
    --version-file (Join-Path $root "build\version_app.txt") `
    --distpath (Join-Path $root "dist") `
    --workpath (Join-Path $root "build\onedir") `
    --specpath (Join-Path $root "build\onedir") `
    (Join-Path $root "packaging\launcher.py")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed (app)" }

# 2. Native messaging host (console app: it talks over stdin/stdout)
& $py -m PyInstaller --noconfirm --clean --onefile --console `
    --name "idm-host" `
    --version-file (Join-Path $root "build\version_host.txt") `
    --distpath (Join-Path $root "dist") `
    --workpath (Join-Path $root "build\host") `
    --specpath (Join-Path $root "build\host") `
    (Join-Path $root "packaging\host_launcher.py")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed (host)" }

# 3. Installer
New-Item -ItemType Directory -Force (Join-Path $root "release") | Out-Null
$iscc = @(
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($iscc) {
    & $iscc "/DAppVersion=$version" (Join-Path $root "packaging\installer.iss")
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }
    Write-Host "Installer: $(Join-Path $root "release\IDM-Clone-Setup-$version.exe")"
} else {
    Write-Warning "Inno Setup 6 not found; skipping the installer. Install it from https://jrsoftware.org/isdl.php and re-run."
}

# 4. Portable single-file build
& $py -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name "IDM Clone" `
    --icon (Join-Path $root "assets\icon.ico") `
    --version-file (Join-Path $root "build\version_app.txt") `
    --distpath (Join-Path $root "release") `
    --workpath (Join-Path $root "build\onefile") `
    --specpath (Join-Path $root "build\onefile") `
    (Join-Path $root "packaging\launcher.py")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed (portable)" }
Copy-Item -Force (Join-Path $root "dist\idm-host.exe") (Join-Path $root "release\idm-host.exe")
Write-Host "Portable: $(Join-Path $root 'release\IDM Clone.exe')"

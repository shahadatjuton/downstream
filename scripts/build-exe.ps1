# Builds a single self-contained "IDM Clone.exe" into the release\ folder.
# The exe needs no Python and no other files; copy it anywhere.
#
#   powershell -ExecutionPolicy Bypass -File scriptsuild-exe.ps1

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "Creating virtual environment..."
    python -m venv (Join-Path $root ".venv")
    & $py -m pip install --quiet --upgrade pip
    & $py -m pip install --quiet -e "$root" pyinstaller
}

& $py -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name "IDM Clone" `
    --icon (Join-Path $root "assets\icon.ico") `
    --distpath (Join-Path $root "release") `
    --workpath (Join-Path $root "build\onefile") `
    --specpath (Join-Path $root "build\onefile") `
    (Join-Path $root "packaging\launcher.py")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

# Native messaging host for the browser extension (console app: it talks over stdin/stdout).
& $py -m PyInstaller --noconfirm --clean --onefile --console `
    --name "idm-host" `
    --distpath (Join-Path $root "release") `
    --workpath (Join-Path $root "build\host") `
    --specpath (Join-Path $root "build\host") `
    (Join-Path $root "packaging\host_launcher.py")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed (host)" }

$exe = Join-Path $root "release\IDM Clone.exe"
$size = [math]::Round((Get-Item $exe).Length / 1MB, 1)
Write-Host ""
Write-Host "Built: $exe ($size MB)"
Write-Host "Built: $(Join-Path $root 'release\idm-host.exe')"
Write-Host "Next: register-browser.ps1 to connect the browser extension."

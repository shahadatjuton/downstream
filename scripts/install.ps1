# Builds a standalone Windows app with PyInstaller, copies it to
# %LOCALAPPDATA%\Programs\IDM Clone, and creates Start Menu + Desktop shortcuts.
# Re-run after changing the code to update the installed app.
#
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "Creating virtual environment..."
    python -m venv (Join-Path $root ".venv")
    & $py -m pip install --quiet --upgrade pip
    & $py -m pip install --quiet -e "$root" pyinstaller
}

Write-Host "Building with PyInstaller..."
& $py -m PyInstaller --noconfirm --clean --windowed `
    --name "IDM Clone" `
    --icon (Join-Path $root "assets\icon.ico") `
    --distpath (Join-Path $root "dist") `
    --workpath (Join-Path $root "build") `
    --specpath (Join-Path $root "build") `
    (Join-Path $root "packaging\launcher.py")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$target = Join-Path $env:LOCALAPPDATA "Programs\IDM Clone"
Write-Host "Installing to $target"
Get-Process "IDM Clone" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
if (Test-Path $target) { Remove-Item -Recurse -Force $target }
New-Item -ItemType Directory -Force $target | Out-Null
Copy-Item -Recurse -Force (Join-Path $root "dist\IDM Clone\*") $target

$exe = Join-Path $target "IDM Clone.exe"
$ws = New-Object -ComObject WScript.Shell
$startMenu = [Environment]::GetFolderPath("Programs")
$desktop = [Environment]::GetFolderPath("Desktop")
foreach ($dir in @($startMenu, $desktop)) {
    $lnk = $ws.CreateShortcut((Join-Path $dir "IDM Clone.lnk"))
    $lnk.TargetPath = $exe
    $lnk.WorkingDirectory = $target
    $lnk.IconLocation = "$exe,0"
    $lnk.Description = "IDM Clone download manager"
    $lnk.Save()
}

Write-Host ""
Write-Host "Installed. Launch 'IDM Clone' from the Start Menu or the desktop shortcut."
Write-Host "Executable: $exe"

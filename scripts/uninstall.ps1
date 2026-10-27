# Removes the installed app and its shortcuts. Settings and download history in
# %APPDATA%\idm-clone are kept unless you pass -PurgeData.
param([switch]$PurgeData)

$target = Join-Path $env:LOCALAPPDATA "Programs\IDM Clone"
Get-Process "IDM Clone" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
if (Test-Path $target) { Remove-Item -Recurse -Force $target }
foreach ($dir in @([Environment]::GetFolderPath("Programs"), [Environment]::GetFolderPath("Desktop"))) {
    $lnk = Join-Path $dir "IDM Clone.lnk"
    if (Test-Path $lnk) { Remove-Item -Force $lnk }
}
if ($PurgeData) {
    $data = Join-Path $env:APPDATA "idm-clone"
    if (Test-Path $data) { Remove-Item -Recurse -Force $data }
}
Write-Host "IDM Clone removed."

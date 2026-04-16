# build.ps1 — compile Kane and hot-replace the local copy
# Usage: right-click → Run with PowerShell  (or: .\build.ps1)

$ErrorActionPreference = "Stop"
$root   = $PSScriptRoot
$dist   = Join-Path $root "dist\Kane.exe"
$target = Join-Path $root "Kane.exe"

Write-Host ""
Write-Host "  KANE  /  building..." -ForegroundColor Yellow

python -m PyInstaller `
    --noconsole `
    --onefile `
    --collect-all customtkinter `
    --name Kane `
    --distpath "$root\dist" `
    --workpath "$root\build" `
    --specpath "$root" `
    "$root\app.py" | Out-Null

if (-not (Test-Path $dist)) {
    Write-Host "  BUILD FAILED — dist\Kane.exe not found" -ForegroundColor Red
    exit 1
}

Copy-Item -Path $dist -Destination $target -Force
Write-Host "  DONE  →  $target" -ForegroundColor Green
Write-Host ""

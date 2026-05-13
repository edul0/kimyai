param(
  [string]$Python = "python"
)

$ErrorActionPreference = "Stop"

Set-Location (Join-Path $PSScriptRoot "..")

Write-Host "[Kemy] Gerando .exe local..." -ForegroundColor Cyan

& $Python -m pip install --upgrade pip pyinstaller

& $Python -m PyInstaller `
  --noconfirm `
  --clean `
  --name "KemyDesktop" `
  --windowed `
  --add-data "static;static" `
  --add-data "app;app" `
  --add-data ".env.example;." `
  local_app\kemy_desktop.py

Write-Host ""
Write-Host "Build concluido." -ForegroundColor Green
Write-Host "Exe: dist\\KemyDesktop\\KemyDesktop.exe" -ForegroundColor Yellow

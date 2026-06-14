param(
  [string]$Python = "python"
)

$ErrorActionPreference = "Stop"

Set-Location (Join-Path $PSScriptRoot "..")

Write-Host "[Kemy] Gerando .exe local (assistente de voz)..." -ForegroundColor Cyan

& $Python -m pip install --upgrade pip pyinstaller
& $Python -m pip install -r local_app\requirements.txt

& $Python -m PyInstaller `
  --noconfirm `
  --clean `
  --name "KemyDesktop" `
  --windowed `
  --add-data "static;static" `
  --add-data "app;app" `
  --add-data ".env.example;." `
  --hidden-import "pyttsx3.drivers" `
  --hidden-import "pyttsx3.drivers.sapi5" `
  --hidden-import "comtypes" `
  --hidden-import "speech_recognition" `
  --hidden-import "pyaudio" `
  local_app\kemy_desktop.py

Write-Host ""
Write-Host "Build concluido." -ForegroundColor Green
Write-Host "Exe: dist\KemyDesktop\KemyDesktop.exe" -ForegroundColor Yellow

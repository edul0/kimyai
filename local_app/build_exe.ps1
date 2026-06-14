param(
  [string]$Python = "python"
)

$ErrorActionPreference = "Stop"

Set-Location (Join-Path $PSScriptRoot "..")

Write-Host "[Kemy] Gerando .exe local (assistente de voz)..." -ForegroundColor Cyan

& $Python -m pip install --upgrade pip pyinstaller
& $Python -m pip install -r requirements-cloud.txt
& $Python -m pip install -r local_app\requirements.txt

& $Python -m PyInstaller `
  --noconfirm `
  --clean `
  --name "KemyDesktop" `
  --windowed `
  --paths "." `
  --add-data "static;static" `
  --add-data "conhecimento;conhecimento" `
  --add-data ".env.example;." `
  --collect-submodules "app" `
  --collect-submodules "uvicorn" `
  --collect-submodules "anyio" `
  --hidden-import "agencia_kemy" `
  --hidden-import "pyttsx3.drivers" `
  --hidden-import "pyttsx3.drivers.sapi5" `
  --hidden-import "comtypes" `
  --hidden-import "speech_recognition" `
  --hidden-import "pyaudio" `
  local_app\kemy_desktop.py

Write-Host ""
Write-Host "Build concluido." -ForegroundColor Green
Write-Host "Exe: dist\KemyDesktop\KemyDesktop.exe" -ForegroundColor Yellow

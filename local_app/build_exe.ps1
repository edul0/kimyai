param(
  [string]$Python = "python"
)

$ErrorActionPreference = "Stop"

Set-Location (Join-Path $PSScriptRoot "..")

Write-Host "[Kemy] Gerando .exe local (assistente de voz)..." -ForegroundColor Cyan

& $Python -m pip install --upgrade pip pyinstaller
& $Python -m pip install -r requirements-cloud.txt
& $Python -m pip install -r local_app\requirements.txt

# .env embutido (gerado pelo CI a partir dos GitHub Secrets). Garante que o
# arquivo exista para o --add-data; vazio = sem chaves embutidas.
if (-not (Test-Path kemy_bundled.env)) { New-Item -ItemType File kemy_bundled.env | Out-Null }
if (-not (Test-Path kemy_version.txt)) { New-Item -ItemType File kemy_version.txt | Out-Null }

& $Python -m PyInstaller `
  --noconfirm `
  --clean `
  --name "KemyDesktop" `
  --windowed `
  --paths "." `
  --add-data "static;static" `
  --add-data "conhecimento;conhecimento" `
  --add-data ".env.example;." `
  --add-data "kemy_bundled.env;." `
  --add-data "kemy_version.txt;." `
  --add-data "local_app/ui.html;." `
  --add-data "local_app/avatar.html;." `
  --collect-all "webview" `
  --collect-all "clr_loader" `
  --collect-submodules "app" `
  --collect-submodules "uvicorn" `
  --collect-submodules "anyio" `
  --hidden-import "agencia_kemy" `
  --hidden-import "pyttsx3.drivers" `
  --hidden-import "pyttsx3.drivers.sapi5" `
  --hidden-import "comtypes" `
  --hidden-import "speech_recognition" `
  --hidden-import "pyaudio" `
  --collect-all "pyaudio" `
  --collect-all "speech_recognition" `
  --collect-all "edge_tts" `
  --collect-submodules "aiohttp" `
  --collect-all "openpyxl" `
  --hidden-import "PIL" `
  --hidden-import "PIL._tkinter_finder" `
  --hidden-import "clr" `
  --hidden-import "websocket" `
  --collect-submodules "pystray" `
  --hidden-import "pystray._win32" `
  local_app\kemy_desktop.py

Write-Host ""
Write-Host "Build concluido." -ForegroundColor Green
Write-Host "Exe: dist\KemyDesktop\KemyDesktop.exe" -ForegroundColor Yellow

# rebuild trigger: ler KEMY_ENV apos secret cadastrado

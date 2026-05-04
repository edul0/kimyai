# ╔══════════════════════════════════════════════════════════════╗
# ║   KEMY AI v2.0 — Script de Setup Automático (Windows)        ║
# ║   Uso: PowerShell -ExecutionPolicy Bypass -File setup.ps1    ║
# ╚══════════════════════════════════════════════════════════════╝

Write-Host "════════════════════════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host "  🚀 KEMY AI v2.0 — Setup Automático (Windows)" -ForegroundColor Cyan
Write-Host "════════════════════════════════════════════════════════════════" -ForegroundColor Cyan

# ─── Verificar Python ───────────────────────────────────────────
Write-Host ""
Write-Host "1️⃣  Verificando Python..." -ForegroundColor Yellow
$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCmd) {
    Write-Host "❌ Python não encontrado. Instale em https://python.org" -ForegroundColor Red
    exit 1
}
$pythonVersion = python --version
Write-Host "✅ $pythonVersion encontrado" -ForegroundColor Green

# ─── Criar Virtual Environment ──────────────────────────────────
Write-Host ""
Write-Host "2️⃣  Criando virtual environment..." -ForegroundColor Yellow
if (-not (Test-Path "venv")) {
    python -m venv venv
    Write-Host "✅ Venv criado" -ForegroundColor Green
} else {
    Write-Host "✅ Venv já existe" -ForegroundColor Green
}

# ─── Ativar Venv ───────────────────────────────────────────────
Write-Host "✅ Ativando Venv..." -ForegroundColor Green
& .\venv\Scripts\Activate.ps1

# ─── Instalar Dependencies ─────────────────────────────────────
Write-Host ""
Write-Host "3️⃣  Instalando dependências Python..." -ForegroundColor Yellow
python -m pip install --upgrade pip setuptools wheel | Out-Null
pip install -r requirements.txt | Out-Null
Write-Host "✅ Dependências instaladas" -ForegroundColor Green

# ─── Instalar Playwright ───────────────────────────────────────
Write-Host ""
Write-Host "4️⃣  Instalando Playwright (navegador)..." -ForegroundColor Yellow
playwright install chromium | Out-Null
Write-Host "✅ Playwright pronto" -ForegroundColor Green

# ─── Copiar .env.example para .env ─────────────────────────────
Write-Host ""
Write-Host "5️⃣  Configurando .env..." -ForegroundColor Yellow
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "✅ .env criado (preencha com suas chaves de API)" -ForegroundColor Green
} else {
    Write-Host "✅ .env já existe" -ForegroundColor Green
}

# ─── Criar diretórios ──────────────────────────────────────────
Write-Host ""
Write-Host "6️⃣  Criando diretórios..." -ForegroundColor Yellow
$dirs = @(
    "conhecimento\brenno_prompt",
    "conhecimento\diego_arquiteto",
    "conhecimento\paulo_front",
    "conhecimento\felipe_backend",
    "conhecimento\bianca_cyber",
    "conhecimento\leonardo_qa",
    "agentes_customizados",
    "logs"
)
foreach ($dir in $dirs) {
    if (-not (Test-Path $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
}
Write-Host "✅ Diretórios criados" -ForegroundColor Green

# ─── Resumo Final ───────────────────────────────────────────────
Write-Host ""
Write-Host "════════════════════════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host "  ✅ SETUP CONCLUÍDO!" -ForegroundColor Green
Write-Host "════════════════════════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host ""
Write-Host "📋 Próximos passos:" -ForegroundColor White
Write-Host ""
Write-Host "1️⃣  Preencha suas chaves de API em .env:" -ForegroundColor Yellow
Write-Host "   notepad .env" -ForegroundColor Gray
Write-Host ""
Write-Host "2️⃣  Inicie o servidor:" -ForegroundColor Yellow
Write-Host "   python agencia_kemy.py" -ForegroundColor Gray
Write-Host ""
Write-Host "3️⃣  Acesse a API em:" -ForegroundColor Yellow
Write-Host "   http://localhost:8000/docs" -ForegroundColor Gray
Write-Host ""
Write-Host "4️⃣  (Alternativa) Use Docker Compose:" -ForegroundColor Yellow
Write-Host "   docker-compose up" -ForegroundColor Gray
Write-Host ""
Write-Host "📚 Documentação: https://github.com/seu-repo/kemyai" -ForegroundColor Cyan
Write-Host "📖 Deploy: Veja DEPLOY.md" -ForegroundColor Cyan
Write-Host ""
Write-Host "════════════════════════════════════════════════════════════════" -ForegroundColor Cyan

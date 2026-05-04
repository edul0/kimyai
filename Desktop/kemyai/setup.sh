#!/bin/bash
# ╔══════════════════════════════════════════════════════════════╗
# ║   KEMY AI v2.0 — Script de Setup Automático (Linux/Mac)     ║
# ║   Uso: chmod +x setup.sh && ./setup.sh                      ║
# ╚══════════════════════════════════════════════════════════════╝

set -e

echo "════════════════════════════════════════════════════════════════"
echo "  🚀 KEMY AI v2.0 — Setup Automático"
echo "════════════════════════════════════════════════════════════════"

# ─── Verificar Python ───────────────────────────────────────────
echo ""
echo "1️⃣  Verificando Python..."
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 não encontrado. Instale em https://python.org"
    exit 1
fi
PYTHON_VERSION=$(python3 --version | cut -d' ' -f2)
echo "✅ Python $PYTHON_VERSION encontrado"

# ─── Criar Virtual Environment ──────────────────────────────────
echo ""
echo "2️⃣  Criando virtual environment..."
if [ ! -d "venv" ]; then
    python3 -m venv venv
    echo "✅ Venv criado"
else
    echo "✅ Venv já existe"
fi

# ─── Ativar Venv ───────────────────────────────────────────────
source venv/bin/activate
echo "✅ Venv ativado"

# ─── Instalar Dependencies ─────────────────────────────────────
echo ""
echo "3️⃣  Instalando dependências Python..."
pip install --upgrade pip setuptools wheel > /dev/null 2>&1
pip install -r requirements.txt > /dev/null 2>&1
echo "✅ Dependências instaladas"

# ─── Instalar Playwright ───────────────────────────────────────
echo ""
echo "4️⃣  Instalando Playwright (navegador)..."
playwright install chromium > /dev/null 2>&1
echo "✅ Playwright pronto"

# ─── Copiar .env.example para .env ─────────────────────────────
echo ""
echo "5️⃣  Configurando .env..."
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo "✅ .env criado (preencha com suas chaves de API)"
else
    echo "✅ .env já existe"
fi

# ─── Criar diretórios ──────────────────────────────────────────
echo ""
echo "6️⃣  Criando diretórios..."
mkdir -p conhecimento/{brenno_prompt,diego_arquiteto,paulo_front,felipe_backend,bianca_cyber,leonardo_qa}
mkdir -p agentes_customizados
mkdir -p logs
echo "✅ Diretórios criados"

# ─── Resumo Final ───────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════════════"
echo "  ✅ SETUP CONCLUÍDO!"
echo "════════════════════════════════════════════════════════════════"
echo ""
echo "📋 Próximos passos:"
echo ""
echo "1️⃣  Preencha suas chaves de API em .env:"
echo "   nano .env"
echo ""
echo "2️⃣  Inicie o servidor:"
echo "   python agencia_kemy.py"
echo ""
echo "3️⃣  Acesse a API em:"
echo "   http://localhost:8000/docs"
echo ""
echo "4️⃣  (Alternativa) Use Docker Compose:"
echo "   docker-compose up"
echo ""
echo "📚 Documentação: https://github.com/seu-repo/kemyai"
echo "📖 Deploy: Veja DEPLOY.md"
echo ""
echo "════════════════════════════════════════════════════════════════"

# ╔══════════════════════════════════════════════════════════════╗
# ║         KEMY AI v2.0 — Docker Cloud Container                ║
# ║    Build: docker build -t kemy-ai:latest .                   ║
# ║    Run:   docker run -p 8000:8000 --env-file .env kemy-ai    ║
# ╚══════════════════════════════════════════════════════════════╝

FROM python:3.11-slim

# ─── Metadados ────────────────────────────────
LABEL maintainer="Kemy AI <dev@kemy.ai>"
LABEL description="Kemy AI - Multi-LLM Agency Engine"
LABEL version="2.0.0"

# ─── Variáveis de Build ──────────────────────
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive

# ─── Workdir ──────────────────────────────────
WORKDIR /app

# ─── System Dependencies ──────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    wget \
    git \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# ─── Python Dependencies ──────────────────────
COPY requirements.txt .
RUN pip install --upgrade pip setuptools wheel && \
    pip install -r requirements.txt

# ─── Playwright (Automação de Browser) ────────
RUN playwright install chromium && \
    playwright install-deps

# ─── App Files ────────────────────────────────
COPY . .

# ─── Criar diretórios necessários ──────────────
RUN mkdir -p conhecimento logs agentes_customizados && \
    mkdir -p conhecimento/{brenno_prompt,diego_arquiteto,paulo_front,felipe_backend,bianca_cyber,leonardo_qa}

# ─── Health Check ─────────────────────────────
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:${PORT:-8000}/api/status || exit 1

# ─── Expose Port ──────────────────────────────
EXPOSE 8000

# ─── Entry Point ──────────────────────────────
CMD ["python", "-m", "uvicorn", "agencia_kemy:app", "--host", "0.0.0.0", "--port", "8000"]

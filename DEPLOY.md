# 🚀 Kemy AI — Guia Completo de Deploy em Nuvem

## Table of Contents
1. [Local (Desenvolvimento)](#1-local-desenvolvimento)
2. [Render.com (Recomendado)](#2-rendercom-recomendado)
3. [Railway.app](#3-railwayapp)
4. [Fly.io](#4-flyio)
5. [AWS ECS / Docker](#5-aws-ecs--docker)
6. [Configuração de CI/CD](#6-configuração-de-cicd)
7. [Monitoramento em Produção](#7-monitoramento-em-produção)
8. [Troubleshooting](#8-troubleshooting)

---

## 1. Local (Desenvolvimento)

### 1.1 Setup Inicial

```bash
# Clone
git clone <seu-repo> && cd kemyai

# Virtual Environment
python -m venv venv
source venv/bin/activate  # ou `venv\Scripts\activate` no Windows

# Dependências
pip install -r requirements.txt

# Playwright
playwright install chromium

# Configurar .env
cp .env.example .env
# Editar .env com suas chaves
```

### 1.2 Rodar Local

```bash
# Opção A: Python direto
python agencia_kemy.py

# Opção B: Docker Compose (recomendado)
docker-compose up

# Acesse
open http://localhost:8000/docs
```

### 1.3 Testar API Localmente

```bash
# Criar sessão
curl -X POST http://localhost:8000/api/sessao/nova

# Executar comando
curl -X POST http://localhost:8000/api/comando \
  -H "Content-Type: application/json" \
  -d '{
    "mensagem": "Crie um botão azul com Tailwind",
    "session_id": "seu-session-id"
  }'
```

---

## 2. Render.com (Recomendado)

### 2.1 Por que Render.com?

✅ **Vantagens:**
- Deploy automático via GitHub (sem pagar por CI)
- Free tier funcional
- Zero cold starts com Starter plan
- Suporte a Redis grátis (beta)
- Documentação excelente em português
- Suporte 24/7

### 2.2 Setup Passo a Passo

**Pré-requisitos:**
- Repositório já publicado no GitHub
- Conta no Render conectada ao GitHub
- Chaves de API dos provedores que você quer usar

**Processo recomendado: Blueprint com `render.yaml`**

1. Confirme que o arquivo [`render.yaml`](/C:/Users/carlos.lesse/Documents/Codex/2026-05-05/github-plugin-github-openai-curated-vamos/kimyai/render.yaml) está na raiz do repositório.

2. Faça `git push` para o GitHub.

3. No Render, abra:
   [https://dashboard.render.com/blueprint/new?repo=https://github.com/edul0/kimyai](https://dashboard.render.com/blueprint/new?repo=https://github.com/edul0/kimyai)

4. Revise o serviço criado pelo Blueprint:
   - `kemy-ai`: web service público da aplicação

5. Preencha as secrets pedidas pelo Blueprint:
   ```
   KEMY_AUTH_PASSWORD=<senha forte>
   GEMINI_API_KEY=<sua-chave>
   GROQ_API_KEY=<sua-chave>
   CEREBRAS_API_KEY=<sua-chave>
   OPENROUTER_API_KEY=<sua-chave>
   TAVILY_API_KEY=<opcional>
   SERPER_API_KEY=<opcional>
   E2B_API_KEY=<opcional>
   BROWSERLESS_API_KEY=<opcional>
   BROWSERLESS_URL=<opcional>
   POLLINATIONS_API_KEY=<opcional>
   KIMI_SUPABASE_URL=<opcional>
   KIMI_SUPABASE_ANON_KEY=<opcional>
   KIMI_SUPABASE_SERVICE_ROLE_KEY=<opcional>
   ```

6. Clique em **Apply**.

7. Aguarde o primeiro deploy terminar. O Render vai buildar a aplicação pelo `Dockerfile` e subir um único serviço no plano grátis.

**Importante sobre custo**

- Esse blueprint agora fica compatível com o `free tier`.
- `GOTENBERG_URL` deve ficar vazio no Render grátis.
- PDFs comuns usam `Playwright + Chromium` e slides usam `marp-cli` dentro da própria aplicação, sem serviço extra pago.
- Se o Chromium não estiver disponível por algum detalhe do ambiente, ainda existe fallback final com `reportlab`.

### 2.3 Verificar Deploy

```bash
# Sua URL
https://kemy-ai.onrender.com

# Swagger Docs
https://kemy-ai.onrender.com/docs

# Health Check
curl https://kemy-ai.onrender.com/api/status
```

Cheque no JSON de status:

- `tools.gotenberg: false` no modo grátis
- `llm_mode: "providers"` se você configurou ao menos um provedor
- `storage.supabase_enabled: true` se configurou Supabase

### 2.4 Ver Logs

Em Render Dashboard:
1. Clique no seu serviço
2. Aba **"Logs"**
3. Veja logs em tempo real

```bash
# Ou via CLI (se instalado):
render logs --service=kemy-ai
```

Para problemas de PDF/slides no plano grátis, confira apenas os logs do `kemy-ai`, porque a renderização acontece dentro da própria aplicação.

---

## 3. Railway.app

### 3.1 Setup

```bash
# Install Railway CLI
npm install -g @railway/cli

# Login
railway login

# Ir para diretório do projeto
cd kemyai

# Criar novo projeto
railway init

# Deploy
railway up
```

### 3.2 Configurar Variáveis

```bash
railway variables set GEMINI_API_KEY=<chave>
railway variables set GROQ_API_KEY=<chave>
railway variables set CEREBRAS_API_KEY=<chave>
railway variables set OPENROUTER_API_KEY=<chave>
```

### 3.3 Conectar Repositório GitHub (Auto-deploy)

1. Acesse [railway.app/dashboard](https://railway.app/dashboard)
2. Projeto → Settings
3. Conectar GitHub repo
4. Branch: `main`
5. Auto-deploy: ✅ ativado

---

## 4. Fly.io

### 4.1 Setup

```bash
# Install Flyctl
curl -L https://fly.io/install.sh | sh

# Login
fly auth login

# Ir para diretório
cd kemyai

# Launch
fly launch --dockerfile Dockerfile
# Responda as perguntas:
# - App name: kemy-ai
# - Organization: (usar default)
# - Region: iad (Ashburn, US — mais rápido para Brasil)
```

### 4.2 Configurar Segredos

```bash
fly secrets set \
  GEMINI_API_KEY=<chave> \
  GROQ_API_KEY=<chave> \
  CEREBRAS_API_KEY=<chave> \
  OPENROUTER_API_KEY=<chave>
```

### 4.3 Deploy

```bash
fly deploy
```

### 4.4 Monitore

```bash
fly logs -a kemy-ai
fly status -a kemy-ai
```

---

## 5. AWS ECS + Docker

### 5.1 ECR (Elastic Container Registry)

```bash
# Login no AWS CLI
aws ecr get-login-password --region us-east-1 | \
  docker login --username AWS --password-stdin \
  <seu-account-id>.dkr.ecr.us-east-1.amazonaws.com

# Build
docker build -t kemy-ai:latest .

# Tag
docker tag kemy-ai:latest \
  <seu-account-id>.dkr.ecr.us-east-1.amazonaws.com/kemy-ai:latest

# Push
docker push \
  <seu-account-id>.dkr.ecr.us-east-1.amazonaws.com/kemy-ai:latest
```

### 5.2 ECS Task Definition

```json
{
  "family": "kemy-ai",
  "containerDefinitions": [
    {
      "name": "kemy-ai",
      "image": "<account-id>.dkr.ecr.us-east-1.amazonaws.com/kemy-ai:latest",
      "memory": 1024,
      "cpu": 512,
      "essential": true,
      "portMappings": [
        {
          "containerPort": 8000,
          "hostPort": 8000,
          "protocol": "tcp"
        }
      ],
      "environment": [
        {
          "name": "PORT",
          "value": "8000"
        },
        {
          "name": "ENVIRONMENT",
          "value": "production"
        }
      ],
      "secrets": [
        {
          "name": "GEMINI_API_KEY",
          "valueFrom": "arn:aws:secretsmanager:us-east-1:<account-id>:secret:kemy/gemini::"
        }
      ]
    }
  ]
}
```

---

## 6. Configuração de CI/CD

### 6.1 GitHub Actions (Já Incluso)

Ver arquivo `.github/workflows/deploy.yml`

**O que ele faz:**

1. **On Push para `main`:**
   - Lint (flake8)
   - Type check (mypy)
   - Build Docker image
   - Deploy para Render (se secrets configurados)

### 6.2 Configurar Secrets no GitHub

1. Acesse: Settings → Secrets and variables → Actions
2. Clique em **"New repository secret"**
3. Adicione:
   ```
   RENDER_API_KEY        = <sua-chave-render>
   RENDER_SERVICE_ID     = <seu-service-id>
   RAILWAY_TOKEN         = <seu-token-railway>
   ```

---

## 7. Monitoramento em Produção

### 7.1 Health Checks

Render/Railway/Fly.io verificam automaticamente:

```
GET /api/status
Response: HTTP 200
```

Se falhar 3x consecutivas, reinicia o container.

### 7.2 Logs e Alertas

**Sentry (Error Tracking)**

```bash
pip install sentry-sdk
```

Em `.env`:
```
SENTRY_DSN=https://xxx@sentry.io/xxx
```

No código (já incluído em `cache_analytics.py`):
```python
import sentry_sdk
sentry_sdk.init(dsn=os.getenv("SENTRY_DSN"))
```

### 7.3 Métricas Customizadas

Use o endpoint `/api/analytics/{session_id}` para:
- Tokens gastos por agente
- Tempo de execução
- Taxa de sucesso
- Custo estimado

```bash
curl https://seu-app.onrender.com/api/analytics/abc-123 | jq
```

---

## 8. Troubleshooting

### ❌ "Build Failing on Render"

```
ERROR: failed to solve with frontend dockerfile.v0
```

**Solução:**
```bash
# Verificar Dockerfile localmente
docker build -t kemy-ai:test .

# Se falhar, rodar com debug
docker build -t kemy-ai:test . --no-cache --verbose
```

### ❌ "Module Not Found: crewai"

```
ModuleNotFoundError: No module named 'crewai'
```

**Solução:**
- Verificar se `requirements.txt` está atualizado
- Push novo commit para Render redeploy

### ❌ "CORS Error"

```
Access to XMLHttpRequest blocked by CORS policy
```

**Solução:**
Em `.env`:
```
ORIGENS_CORS=https://seu-frontend.com,https://outro-dominio.com
```

Redeploy.

### ❌ "Redis Connection Error"

```
ConnectionError: Error 111 connecting to redis-host:6379
```

**Solução (Fallback seguro):**
- Kemy AI funciona sem Redis (usa memória local)
- Remova `REDIS_URL` do `.env`
- Redeploy

### ❌ "Timeout 503 — Todos os motores falharam"

**Causas:**
1. Chaves de API inválidas
2. Rate limit atingido
3. Conexão com internet ruim

**Solução:**
- Verificar chaves em `.env`
- Ver logs: `GET /api/logs/{session_id}`
- Testar manualmente: `GET /api/status`

---

## Sumário de Deploy

| Plataforma | Complexidade | Custo | Auto-Deploy | Recomendado |
|-----------|------------|------|------------|-----------|
| Render.com | ⭐ Fácil | $7-25/mês | ✅ Sim | ⭐⭐⭐⭐⭐ |
| Railway.app | ⭐ Fácil | $5-50/mês | ✅ Sim | ⭐⭐⭐⭐ |
| Fly.io | ⭐⭐ Médio | $3-50/mês | ✅ Sim | ⭐⭐⭐⭐ |
| AWS ECS | ⭐⭐⭐ Complexo | $15-100/mês | ⚠️ Manual | ⭐⭐ |
| Docker Local | ⭐ Fácil | Grátis | ❌ Não | Dev only |

**Recomendação:** Comece com **Render.com** — melhor balance entre facilidade e funcionalidade.

---

## Próximos Passos

1. ✅ Deploy uma vez em Render.com
2. ✅ Testar `/docs` e `/api/status`
3. ✅ Criar um agente customizado via API
4. ✅ Executar uma missão completa
5. ✅ Verificar analytics em `/api/analytics/{session_id}`
6. ✅ Configurar GitHub Actions para auto-deploy
7. ✅ Monitorar com Sentry
8. ✅ Escalar com Redis externo (Upstash, Redis Cloud)

---

## Contato + Suporte

- Issues: GitHub Issues
- Documentação API: `/docs` (Swagger)
- Base de conhecimento: `/conhecimento`

🚀 **Bem-vindo à Kemy AI em nuvem!**

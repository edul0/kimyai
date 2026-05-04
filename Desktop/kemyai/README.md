# 🚀 Kemy AI — Agência Multi-Agente v2.0

> **Equipe de engenharia de software autônoma sob demanda via nuvem**
> 
> Você é o Product Owner — Kemy AI executa. Brenno especifica, Diego arquiteta, Paulo codifica, Felipe implementa backend, Bianca audita, Leonardo valida.

---

## ⚡ Quick Start (Local)

```bash
# 1. Clone o projeto
git clone <seu-repo>
cd kemyai

# 2. Dependências
pip install -r requirements.txt && playwright install chromium

# 3. Configure
cp .env.example .env
# Preencha: GEMINI_API_KEY, GROQ_API_KEY, CEREBRAS_API_KEY, OPENROUTER_API_KEY

# 4. Rode local (opção A — Python direto)
python agencia_kemy.py

# 4. Rode local (opção B — Docker)
docker-compose up

# Acesse: http://localhost:8000/docs
```

---

## ☁️ Deploy em Nuvem

### Option 1: Render.com (Recomendado — mais simples)

```bash
# 1. Push para GitHub
git push origin main

# 2. Conecte em render.com/dashboard
# - New Service → Web Service → Connect GitHub repo
# - Seleção: Main branch, Dockerfile
# - Environment Variables:
#   GEMINI_API_KEY, GROQ_API_KEY, CEREBRAS_API_KEY, OPENROUTER_API_KEY
# - Port: 8000
# - Deploy

# 3. Redis automático? Use Redis Cloud (free tier)
# Adicione: REDIS_URL = redis://:password@redis-host:port
```

### Option 2: Railway.app

```bash
# 1. Install Railway CLI
npm install -g @railway/cli

# 2. Login
railway login

# 3. Deploy
railway up

# 4. Definir variáveis de ambiente
railway variables set GEMINI_API_KEY=xxx GROQ_API_KEY=xxx ...
```

### Option 3: Fly.io

```bash
# 1. Install Flyctl
curl -L https://fly.io/install.sh | sh

# 2. Launch
fly launch --dockerfile Dockerfile

# 3. Set variables
fly secrets set GEMINI_API_KEY=xxx GROQ_API_KEY=xxx ...

# 4. Deploy
fly deploy
```

---

## 🏗️ Arquitetura

### Agentes (7 especialistas)

| Agente | Motor | Função |
|--------|-------|--------|
| **Gabriel** (Gerente) | Gemini 2.5 | Orquestra, planeja, emite instruções |
| **Brenno** (Prompt) | Gemini 2.5 | Especificação técnica detalhada |
| **Diego** (Arquiteto) | Groq | Design patterns, APIs, banco de dados |
| **Paulo** (Frontend) | Groq | HTML5/Tailwind, Mobile-First |
| **Felipe** (Backend) | Groq | APIs, queries otimizadas, validação |
| **Bianca** (Cyber) | Cerebras | Auditoria OWASP, bloqueio de vulnerabilidades |
| **Leonardo** (QA) | Cerebras | Validação HTML, formatting, entrega limpa |

### Pipeline de Execução

```
Usuário (Pedido + Imagem?) 
  ↓
Visão Computacional (Gemini 2.5 → Laudo Visual)
  ↓
Task 1: Brenno (Especificação Técnica)
  ↓
Task 2: Diego (Arquitetura) — recebe output de Task 1
  ↓
Task 3: Paulo (Frontend) — recebe Tasks 1 + 2
  ↓
Task 4: Felipe (Backend) — recebe Tasks 1 + 2
  ↓
Task 5: Bianca (Auditoria) — recebe Tasks 3 + 4
  ↓
Task 6: Leonardo (QA) — recebe Tasks 3 + 5
  ↓
Resultado: HTML Validado ou Relatório Técnico
```

### Cache + Analytics

- **Redis**: Cache de respostas, session store distribuído
- **Analytics Engine**: Rastreia tokens, tempo, custo, taxa de sucesso por agente
- **Fallback**: Memória local se Redis indisponível

---

## 📡 API Endpoints

### Sessões

```bash
# Criar nova sessão
POST /api/sessao/nova
→ { "session_id": "uuid-here", "mensagem": "Kemy AI pronta..." }

# Listar sessões ativas
GET /api/sessao/listar
→ { "sessoes": [...] }

# Limpar memória da sessão
POST /api/sessao/{sid}/limpar

# Histórico de conversas
GET /api/sessao/{sid}/historico
```

### Executar Missão

```bash
POST /api/comando
{
  "mensagem": "Crie uma landing page de SaaS moderno",
  "imagem_base64": "data:image/png;base64,...",  # Opcional
  "session_id": "seu-session-id"  # Opcional — cria nova se omitido
}

→ {
  "status": "sucesso",
  "session_id": "...",
  "resposta": "...",
  "codigo_gerado": "<html>...</html>",
  "html_valido": true
}
```

### Agentes Customizados

```bash
# Criar agente novo
POST /api/agente/criar
{
  "nome": "SEO Specialist",
  "cargo": "Especialista em SEO",
  "objetivo": "Otimizar conteúdo para buscadores",
  "backstory": "Você é especialista em SEO e Google Ranking",
  "motor": "groq",
  "pasta_rag": "conhecimento/seo_specialist"
}

# Listar agentes
GET /api/agente/listar

# Remover agente
DELETE /api/agente/{slug}
```

### Logs + Telemetria

```bash
# Logs estruturados da sessão
GET /api/logs/{session_id}
→ { "session_id": "...", "total": 15, "logs": [...] }

# Status + Métricas
GET /api/status
→ {
  "nome": "Kemy AI",
  "versao": "2.0.0",
  "status": "online",
  "sessoes_ativas": 3,
  "motores": {
    "gemini": { "usos_min": 8, "limite": 60, "livre": true, "pct": 13.3 },
    "groq": { "usos_min": 12, "limite": 30, "livre": true, "pct": 40 },
    ...
  }
}
```

---

## 🔐 Segurança

- ✅ Sessões isoladas (sem conflito multi-usuário)
- ✅ CORS restringido por variável de ambiente
- ✅ Rate limiting automático por motor LLM
- ✅ Auditoria OWASP Top 10 pelo agente Bianca
- ✅ Validação HTML antes de salvar memória
- ✅ Logger estruturado para compliance

---

## 📊 Monitoramento

```bash
# Ver métricas de uma sessão
curl http://localhost:8000/api/logs/{session_id} | jq

# Exemplo de evento registrado:
{
  "ts": "2026-05-04T10:30:15.123456",
  "sessao_id": "abc-123",
  "agente": "diego",
  "tipo": "architecture_design",
  "tempo_s": 12.5,
  "tokens_entrada": 4500,
  "tokens_saida": 2100,
  "sucesso": true,
  "custo_estimado_usd": 0.0089
}
```

---

## 🎯 Agentes Customizados

Crie especialistas novos sem reiniciar o servidor:

```yaml
# agentes_customizados/seo_specialist.yaml
nome: SEO Specialist
cargo: Especialista em SEO
objetivo: Otimizar conteúdo para mecanismos de busca
backstory: |
  Você é especialista em SEO com 10 anos de experiência.
  Conhece Google Ranking, keywords, E-E-A-T, Core Web Vitals.
motor: groq  # gemini, groq, cerebras, openrouter
pasta_rag: conhecimento/seo_specialist
```

Ele entra automaticamente na próxima missão! 🤖

---

## 📚 Endpoints Principais
```http
POST /api/sessao/nova
→ { "session_id": "uuid-aqui" }
```

### Enviar comando (principal)
```http
POST /api/comando
{
  "mensagem": "Crie uma landing page para um app de delivery",
  "session_id": "uuid-da-sessao",       // opcional — cria nova se omitido
  "imagem_base64": "data:image/..."     // opcional — mockup ou print
}
```

### Ver histórico da sessão
```http
GET /api/sessao/{session_id}/historico
```

### Limpar memória da sessão
```http
POST /api/sessao/{session_id}/limpar
```

### Criar agente customizado (Fábrica)
```http
POST /api/agente/criar
{
  "nome": "Carlos Marketing",
  "cargo": "Especialista em SEO e Copywriting",
  "objetivo": "Criar textos persuasivos e otimizados para SEO",
  "motor": "gemini"
}
```

### Status do sistema
```http
GET /api/status
→ carga de cada motor LLM, sessões ativas, versão
```

### Logs de uma sessão
```http
GET /api/logs/{session_id}
→ log estruturado JSON de toda a missão
```

---

## Scripts de Automação ML

### 1. Extrair links dos editores de fotos
```bash
# Abra o Chrome no modo debug primeiro:
chrome.exe --remote-debugging-port=9222

# Execute o extrator:
python extrator_anuncios_ml.py
# → Gera: links_anuncios.txt + progresso_extracao.json
```

### 2. Ativar "Melhorar resolução" em todas as fotos
```bash
python otimizador_fotos_ml.py
# → Gera: relatorio_otimizacao.json + otimizacao.log
```

---

## Arquitetura dos Agentes

```
Cliente
  │
  ▼
Gabriel Junior (Gerente / Gemini 2.5 Flash)
  │ delega
  ├──▶ Brenno Prado    (Spec/Prompt / Gemini)   → DRT
  ├──▶ Diego Rodrigues (Arquitetura / Groq)      → Design Patterns
  ├──▶ Paulo Lima      (Front-end / Groq)        → HTML5 + Tailwind
  ├──▶ Felipe Lima     (Backend / Groq)          → APIs + Queries
  ├──▶ Bianca Lima     (Cyber / Cerebras)        → Auditoria OWASP
  ├──▶ Leonardo Hideki (QA / Cerebras)           → Validação Final
  └──▶ [Agentes Customizados via YAML/API]
```

### Failover em Cascata
```
Groq/Gemini/Cerebras → FALHA → OpenRouter (todos os agentes migram)
```

---

## Fábrica de Agentes

Adicione especialistas sem parar o servidor. Duas formas:

**Via API:**
```bash
curl -X POST http://localhost:8000/api/agente/criar \
  -H "Content-Type: application/json" \
  -d '{"nome":"Ana DevOps","cargo":"Especialista em CI/CD","objetivo":"Criar pipelines GitHub Actions","motor":"groq"}'
```

**Via YAML** (coloque em `/agentes_customizados/`):
```yaml
nome: Ana DevOps
cargo: Especialista em CI/CD
objetivo: Criar pipelines GitHub Actions e Docker Compose otimizados
backstory: Você é Ana, SRE com foco em DevOps e automação de infraestrutura.
motor: groq
```

---

## Roadmap (Whitepaper)

- [ ] **Plano Freemium**: Groq/Gemini gratuitos, equipe reduzida, limite diário
- [ ] **Plano PRO**: todos os motores, equipe completa, histórico ilimitado
- [ ] **RAG Customizado**: upload de pastas de código do próprio usuário
- [ ] **Drag-and-Drop de PDFs**: além de imagens, aceitar documentos de spec
- [ ] **Dashboard de Monitoramento**: visualização de custo/token por sessão

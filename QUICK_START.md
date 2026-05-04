# ⚡ Kemy AI v2.0 — Quick Start (5 minutos)

Comece com Kemy AI em 5 minutos, localmente ou na nuvem.

---

## 🏃 Opção 1: Rodar Localmente (Python)

### Pré-requisitos
- Python 3.11+
- Git

### Setup (3 minutos)

```bash
# 1. Clone
git clone <seu-repo> && cd kemyai

# 2. Setup automático (escolha um)

# macOS/Linux:
chmod +x setup.sh && ./setup.sh

# Windows:
powershell -ExecutionPolicy Bypass -File setup.ps1
```

### Configurar API Keys (1 minuto)

```bash
# Editar .env
nano .env  # ou notepad .env no Windows

# Preencher:
GEMINI_API_KEY=xxxxx
GROQ_API_KEY=xxxxx
CEREBRAS_API_KEY=xxxxx
OPENROUTER_API_KEY=xxxxx
```

### Rodar (1 minuto)

```bash
python agencia_kemy.py
```

**Acesse:** http://localhost:8000/docs

---

## 🐳 Opção 2: Docker (Recomendado)

```bash
# Clone
git clone <seu-repo> && cd kemyai

# Configure .env (editar com suas chaves)
cp .env.example .env
nano .env

# Rode tudo
docker-compose up

# Acesse
open http://localhost:8000/docs
```

---

## ☁️ Opção 3: Deploy em Nuvem (5 minutos)

### Render.com (Mais Simples)

1. **Git Push**
   ```bash
   git add .
   git commit -m "v2.0 production ready"
   git push origin main
   ```

2. **Conecte em Render**
   - Acesse: https://dashboard.render.com
   - Clique: **New** → **Web Service**
   - Conecte GitHub
   - Selecione este repositório

3. **Configure**
   ```
   Name: kemy-ai
   Environment: Docker
   Plan: Starter ($7/mês)
   Branch: main
   ```

4. **Add Environment Variables**
   ```
   GEMINI_API_KEY=xxxxx
   GROQ_API_KEY=xxxxx
   CEREBRAS_API_KEY=xxxxx
   OPENROUTER_API_KEY=xxxxx
   ```

5. **Deploy!** 🚀
   - Clique: **Create Web Service**
   - Aguarde 2-3 minutos

**Sua URL:** https://kemy-ai.onrender.com/docs

---

## 🧪 Primeiro Teste

### Via cURL

```bash
# 1. Criar sessão
curl -X POST http://localhost:8000/api/sessao/nova

# 2. Copie o session_id retornado

# 3. Executar comando
curl -X POST http://localhost:8000/api/comando \
  -H "Content-Type: application/json" \
  -d '{
    "mensagem": "Crie um botão azul com Tailwind",
    "session_id": "seu-session-id-aqui"
  }'
```

### Via Python

```python
import requests

# Criar sessão
r = requests.post("http://localhost:8000/api/sessao/nova")
session_id = r.json()["session_id"]

# Executar comando
r = requests.post(
    "http://localhost:8000/api/comando",
    json={
        "mensagem": "Crie uma landing page",
        "session_id": session_id
    }
)

resultado = r.json()
print(f"HTML válido: {resultado['html_valido']}")
print(f"Código:\n{resultado['codigo_gerado'][:200]}")
```

### Via Swagger (Mais Fácil)

1. Acesse: http://localhost:8000/docs
2. Clique em **POST /api/sessao/nova**
3. Clique em **Try it out** → **Execute**
4. Copie o `session_id`
5. Clique em **POST /api/comando**
6. Cole o JSON:
   ```json
   {
     "mensagem": "Crie um botão",
     "session_id": "seu-id-aqui"
   }
   ```
7. Clique **Execute**

---

## 📚 Documentação Completa

Após o Quick Start, explore:

| Documento | Para quem? | Tempo |
|-----------|-----------|-------|
| [README.md](README.md) | Visão geral | 5 min |
| [API_REFERENCE.md](API_REFERENCE.md) | Integração via API | 15 min |
| [DEPLOY.md](DEPLOY.md) | Deploy em nuvem | 20 min |
| [PRODUCTION_CHECKLIST.md](PRODUCTION_CHECKLIST.md) | Antes de produção | 10 min |
| [exemplos_uso.py](exemplos_uso.py) | Código pronto para usar | 10 min |

---

## 🚨 Troubleshooting

### ❌ "ModuleNotFoundError: No module named 'crewai'"
```bash
# Solução
pip install -r requirements.txt
```

### ❌ "Playwright not found"
```bash
# Solução
playwright install chromium
```

### ❌ "CORS Error"
- Editar `.env`:
  ```
  ORIGENS_CORS=http://seu-frontend:3000
  ```

### ❌ "API Key inválida"
- Verificar chaves em: https://console.groq.com/keys

### ❌ Precisa de Help?
- Ver `/docs` (Swagger)
- Ver [API_REFERENCE.md](API_REFERENCE.md)
- GitHub Issues

---

## 🎯 Casos de Uso

### 📱 Gerar Landing Pages
```json
{
  "mensagem": "Crie uma landing page de app de delivery"
}
```

### 🎨 Traduzir Designs
```json
{
  "mensagem": "Converta este design em HTML",
  "imagem_base64": "data:image/png;base64,..."
}
```

### 🔧 Implementar Backend
```json
{
  "mensagem": "Crie uma API em FastAPI para gerenciar tarefas"
}
```

### 🤖 Criar Agente Customizado
```
POST /api/agente/criar
{
  "nome": "SEO Specialist",
  "cargo": "Especialista em SEO",
  "objetivo": "Otimizar conteúdo para buscadores"
}
```

---

## 🎓 Próximos Passos

1. ✅ Rodou localmente
2. ✅ Fez seu primeiro teste
3. ⏭️ **Agora:**
   - [ ] Explore `/docs`
   - [ ] Crie um agente novo
   - [ ] Teste uma missão complexa
   - [ ] Deploy em Render/Railway

---

## 💡 Dicas Pro

- **Reuse session_id:** Mande múltiplas mensagens na mesma sessão
- **Ver Analytics:** `GET /api/analytics/{session_id}`
- **Ver Logs:** `GET /api/logs/{session_id}`
- **Cache:** Automático via Redis (ou memória local)
- **Rate Limit:** Monitore com `GET /api/status`

---

## 🚀 Você está pronto!

```
╔════════════════════════════════════════════╗
║   🎉 Bem-vindo ao Kemy AI v2.0!           ║
║                                            ║
║   Agência Multi-LLM em nuvem              ║
║   Ready para produção                     ║
║                                            ║
║   Comece agora: /docs                     ║
║   Deploy: render.com/dashboard            ║
╚════════════════════════════════════════════╝
```

---

## 📞 Links Úteis

- 📖 Documentação: `/docs`
- 🚀 Deploy Render: https://render.com/dashboard
- 🚀 Deploy Railway: https://railway.app/dashboard
- 🚀 Deploy Fly.io: https://fly.io/apps
- 🐛 Issues: GitHub Issues
- 💬 Discussões: GitHub Discussions

**Divirta-se criando! 🎉**

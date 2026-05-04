# ✅ Kemy AI — Pre-Production Checklist

Use este checklist antes de fazer deploy para produção.

---

## 🔐 Segurança

- [ ] **Chaves de API** — Todas as 4 chaves criadas e testadas localmente?
  ```bash
  # Testar localmente
  curl http://localhost:8000/api/status
  ```

- [ ] **Variáveis de Ambiente** — Nenhuma chave está em `.env.example`?
  ```bash
  # Verificar
  grep -r "sk-or-v1-" .env.example  # Deve estar vazio
  ```

- [ ] **CORS** — Configurado para domínios conhecidos?
  ```
  ORIGENS_CORS=https://seu-frontend.com,https://outro-dominio.com
  ```
  ❌ NUNCA use `ORIGENS_CORS=*` em produção

- [ ] **Secret Key** — Mudou para string longa e aleatória?
  ```
  SECRET_KEY=SuaChaveAleatoriaComMaiusculasNumeoseCaracteresEspeciais123!@#
  ```

- [ ] **Debug Mode** — Desativado em produção?
  ```
  DEBUG=false
  ENVIRONMENT=production
  ```

- [ ] **Rate Limiting** — Configurado?
  ```
  RATE_LIMIT_REQUESTS=100
  RATE_LIMIT_WINDOW_SECONDS=60
  ```

---

## 📦 Deploy

- [ ] **Dockerfile** — Testado localmente?
  ```bash
  docker build -t kemy-ai:test .
  docker run -p 8000:8000 kemy-ai:test
  ```

- [ ] **Docker Compose** — Funciona sem erros?
  ```bash
  docker-compose up
  docker-compose down
  ```

- [ ] **requirements.txt** — Atualizado com todas as dependências?
  ```bash
  pip list > requirements.txt.verify
  # Comparar com requirements.txt
  ```

- [ ] **Plataforma Escolhida** — Testada em staging?
  - [ ] Render.com
  - [ ] Railway.app
  - [ ] Fly.io
  - [ ] AWS

- [ ] **Health Check** — Respondendo?
  ```bash
  curl https://seu-app.onrender.com/api/status
  ```

---

## 🗄️ Banco de Dados

- [ ] **Redis** — Configurado?
  ```
  REDIS_URL=redis://:password@host:port/0
  ```

- [ ] **PostgreSQL** (se usar)? — Migrations rodadas?
  ```bash
  alembic upgrade head
  ```

- [ ] **Backups** — Configurados?
  - [ ] Redis dumps (RDB/AOF)
  - [ ] PostgreSQL backups automáticos

---

## 🔄 CI/CD

- [ ] **GitHub Actions** — Configuradas?
  - [ ] Lint passando
  - [ ] Docker build OK
  - [ ] Tests passando (se houver)

- [ ] **Secrets no GitHub** — Adicionados?
  ```
  RENDER_API_KEY
  RENDER_SERVICE_ID
  RAILWAY_TOKEN
  ```

- [ ] **.gitignore** — Protegendo arquivos sensíveis?
  ```bash
  # Deve estar em .gitignore:
  .env
  .env.*.local
  venv/
  __pycache__/
  *.pyc
  ```

---

## 📊 Monitoramento

- [ ] **Sentry** — Configurado para error tracking?
  ```
  SENTRY_DSN=https://xxx@sentry.io/xxx
  ```

- [ ] **Logs** — Estruturados e acessíveis?
  ```bash
  # Verificar logs em produção
  curl https://seu-app.onrender.com/api/logs/{session_id}
  ```

- [ ] **Alertas** — Configurados para downtime?
  - [ ] Email alert quando `GET /api/status` falha
  - [ ] Slack webhook para erros críticos

- [ ] **Analytics** — Ativado?
  ```
  ENABLE_ANALYTICS=true
  ```

---

## 🧪 Testes Funcionais

- [ ] **Criar Sessão** — Endpoint funcionando?
  ```bash
  curl -X POST https://seu-app.onrender.com/api/sessao/nova
  ```

- [ ] **Executar Comando Simples** — Funcionando?
  ```bash
  curl -X POST https://seu-app.onrender.com/api/comando \
    -H "Content-Type: application/json" \
    -d '{"mensagem":"Olá","session_id":"test"}'
  ```

- [ ] **Processar Imagem** — Visão computacional OK?
  ```python
  # Enviar imagem base64
  ```

- [ ] **Criar Agente Customizado** — Via API?
  ```bash
  curl -X POST https://seu-app.onrender.com/api/agente/criar \
    -H "Content-Type: application/json" \
    -d '{...}'
  ```

- [ ] **Listar Logs** — Retornando dados?
  ```bash
  curl https://seu-app.onrender.com/api/logs/{session_id}
  ```

---

## 🎯 Performance

- [ ] **Tempo de Resposta** — < 5 segundos para `/api/status`?
  ```bash
  time curl https://seu-app.onrender.com/api/status
  ```

- [ ] **Cold Start** — Aceitável?
  - Render: ~10 segundos (normal)
  - Railway: ~5 segundos (bom)
  - Fly.io: ~3 segundos (excelente)

- [ ] **Memory Usage** — Dentro do esperado?
  - Esperado: 200-500 MB por instância

- [ ] **Rate Limiting** — Funcionando?
  ```bash
  # 100 requisições em 60 segundos
  for i in {1..101}; do
    curl https://seu-app.onrender.com/api/status
  done
  ```

---

## 📚 Documentação

- [ ] **README.md** — Atualizado com URL de produção?

- [ ] **DEPLOY.md** — Instruções claras?

- [ ] **API_REFERENCE.md** — Endpoints documentados?

- [ ] **Swagger** — Acessível em `/docs`?
  ```
  https://seu-app.onrender.com/docs
  ```

- [ ] **Changelog** — Registrado mudanças da v1 para v2?

---

## 🚀 Antes do Go-Live

- [ ] **Teste de Carga** — Como se comporta com 10 requisições simultâneas?
  ```bash
  # Usando Apache Bench
  ab -n 10 -c 10 https://seu-app.onrender.com/api/status
  ```

- [ ] **Failover** — O que acontece se Groq cai?
  ```
  Esperado: Fallback automático para Gemini → Cerebras → OpenRouter
  ```

- [ ] **Downtime Zero** — Blue-green deployment configurado?
  - Render: ✅ Automático
  - Railway: ✅ Automático
  - Fly.io: ✅ Automático

- [ ] **Rollback Plan** — Como revert rápido se algo quebrar?
  ```bash
  # Render: Use versão anterior
  # Railway: Push commit anterior
  # Fly.io: fly rollback
  ```

---

## 📋 Pós-Deploy (Primeiras 24 Horas)

- [ ] **Monitorar Logs** — Nenhum erro crítico?

- [ ] **Testar Features Principais** — Todas funcionando?

- [ ] **Verificar Custos** — Dentro do esperado?
  ```
  Estimativa:
  - Gemini: 0.3-0.5 centavos por request
  - Groq: 0.2-0.3 centavos
  - OpenRouter: 0.4-0.6 centavos
  ```

- [ ] **Comunicar aos Usuários** — Informar que está em produção?

- [ ] **Receber Feedback** — Qualidade do output OK?

- [ ] **Backup Inicial** — Fazer backup manual dos dados?

---

## 🎓 Problemas Comuns

### ❌ "SSL Certificate Error"
- ✅ Certificado é gerado automaticamente por Render/Railway
- ✅ Aguarde 5-10 minutos após deploy

### ❌ "Timeout 503"
- ✅ Verifique chaves de API
- ✅ Aumento do timeout em cliente (180 segundos)
- ✅ Ver `/api/logs` para detalhes

### ❌ "High Memory Usage"
- ✅ Desativar `DEBUG=true`
- ✅ Limitar histórico: `MAX_HISTORY_SIZE=50`
- ✅ Adicionar mais RAM ou instâncias

### ❌ "Redis Connection Refused"
- ✅ Fallback automático para memória local
- ✅ Funciona normalmente sem Redis

---

## 📞 Contatos de Suporte

- **Render:** https://support.render.com
- **Railway:** https://railway.app/support
- **Fly.io:** https://fly.io/help
- **GitHub:** Issues neste repositório

---

## ✨ Parabéns!

Se você chegou aqui com tudo checado, **Kemy AI está pronto para produção** 🚀

```
╔════════════════════════════════════════════╗
║  KEMY AI v2.0 — PRODUCTION READY!         ║
║  Deploy Status: ✅ LIVE                    ║
║  URLs:                                     ║
║   - API: https://seu-app.onrender.com     ║
║   - Docs: /docs                           ║
║   - Analytics: /api/status                ║
╚════════════════════════════════════════════╝
```

Monitore regularmente e divirta-se! 🎉

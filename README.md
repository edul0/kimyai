# Kimi AI

Plataforma de agentes para coding, arquitetura, auditoria e deploy, com backend em FastAPI, interface web simples e persistencia opcional em Supabase e Redis.

## Visao geral

- O projeto roda com entrypoint compativel em `agencia_kemy.py` e app principal em `app/main.py`.
- A execucao acontece por jobs assíncronos em `POST /api/comando` com acompanhamento em `GET /api/jobs/{id}`.
- O deploy principal foi pensado para GitHub + Render.
- O sistema aceita Redis como cache/persistencia curta e Supabase como persistencia remota de sessoes, mensagens e jobs.
- Quando houver dois projetos Supabase no ecossistema, use as variaveis `KIMI_SUPABASE_*` para o Kimi AI e deixe o Kanban isolado no projeto dele.
- O modo `documento` identifica pedidos de `.docx`, Word, relatorio, proposta ou PDF e gera os dois arquivos com links de download.

## Agentes nativos

Os slugs internos foram mantidos para compatibilidade, mas os nomes exibidos agora sao funcionais:

| Agente | Papel | Motor preferido | Funcao |
| --- | --- | --- | --- |
| `Kimi Core` | Orquestrador de Execucao | Gemini | Quebra o pedido, decide a rota e coordena os especialistas |
| `Kimi Spec` | Analista de Requisitos | Gemini | Transforma pedidos vagos em requisitos tecnicos claros |
| `Kimi Arquiteto` | Arquiteto de Software e Cloud | Groq | Define arquitetura, APIs, banco e deploy |
| `Kimi Frontend` | Especialista em Interface | Groq | Cuida da experiencia visual e da interface |
| `Kimi Backend` | Especialista em APIs e Persistencia | Groq | Implementa regras de negocio, API e integracoes |
| `Kimi Security` | Auditor de Seguranca | Cerebras | Revisa riscos, secrets e pontos OWASP |
| `Kimi QA` | Validador Final de Entrega | Cerebras | Faz a ultima validacao de consistencia e entrega |

## Fluxo de execucao

1. O usuario autentica e cria ou reaproveita uma sessao.
2. `Kimi Core` classifica a intencao do pedido.
3. Os especialistas entram conforme o tipo de tarefa: especificacao, arquitetura, frontend, backend, seguranca e QA.
4. O resultado final e salvo em memoria local ou Redis, e opcionalmente sincronizado com o Supabase do Kimi AI.

## Estrutura principal

```text
kemyai/
├── agencia_kemy.py
├── app/
│   ├── main.py
│   ├── jobs.py
│   ├── agents.py
│   ├── llm_router.py
│   ├── supabase_store.py
│   └── storage.py
├── static/
├── supabase/
│   └── migrations/
├── Dockerfile
└── render.yaml
```

## Rodar localmente

```bash
pip install -r requirements-cloud.txt
copy .env.example .env
python agencia_kemy.py
```

Acesse:

- `http://localhost:8000/`
- `http://localhost:8000/docs`
- `http://localhost:8000/api/status`

## Variaveis importantes

### Basicas

- `FREE_ONLY`
- `LLM_MODE`
- `ORIGENS_CORS`
- `REDIS_URL`
- `KEMY_AUTH_USER`
- `KEMY_AUTH_PASSWORD`
- `KEMY_AUTH_SECRET`

### LLMs

- `GEMINI_API_KEY`
- `GROQ_API_KEY`
- `CEREBRAS_API_KEY`
- `OPENROUTER_API_KEY`

### PDF via Gotenberg

- `GOTENBERG_URL`
- `GOTENBERG_TIMEOUT_SECONDS`

No deploy gratis do Render, deixe `GOTENBERG_URL` vazio. A Kimi usa `Playwright + Chromium` para PDFs em HTML e `marp-cli` para slides, sem precisar de servico pago extra. Se o Chromium falhar por ambiente, ainda existe fallback final com `reportlab`.

### Supabase do Kimi AI

Prefira estas variaveis no Render:

- `KIMI_SUPABASE_URL`
- `KIMI_SUPABASE_ANON_KEY`
- `KIMI_SUPABASE_SERVICE_ROLE_KEY`

As variaveis `SUPABASE_*` continuam funcionando como fallback legado.

## Supabase

Para subir o schema do Kimi AI:

1. Rode `supabase/migrations/001_kemy_schema.sql`
2. Rode `supabase/migrations/002_sessions_owner_email.sql`
3. Exponha o schema `kemy` na Data API do Supabase
4. Configure as variaveis `KIMI_SUPABASE_*` no Render

## Deploy no Render

1. Publique o codigo no GitHub
2. Conecte o repositorio no Render
3. Use o `render.yaml` da raiz
4. Configure as secrets no painel do Render
5. Mantenha `GOTENBERG_URL` vazio para ficar no free tier
6. Valide `GET /api/status` e confirme que a app esta online
7. Teste login, sessao, anexos, PDF e slides ja na URL publica

## Endpoints principais

- `POST /api/auth/login`
- `POST /api/auth/register`
- `GET /api/auth/me`
- `POST /api/sessao/nova`
- `GET /api/sessao/listar`
- `GET /api/sessao/{sid}/historico`
- `POST /api/comando`
- `GET /api/jobs/{job_id}`
- `GET /api/artefatos/{job_id}/{filename}`
- `GET /api/agente/listar`
- `POST /api/agente/criar`
- `GET /api/status`

## Observacoes

- O nome da pasta e alguns arquivos ainda carregam o legado `kemyai` para manter compatibilidade com o que ja estava montado.
- O health check do Render usa `/api/status`, que agora fica publico.
- O backend seleciona o schema `kemy` no Supabase usando headers de profile do PostgREST.

# Kemy AI v3 Cloud-Free Strategy

## Objetivo

Transformar a Kemy AI em uma agencia multi-agente focada em coding, rodando em nuvem com custo zero enquanto estiver dentro das cotas gratuitas dos provedores.

## Stack recomendada

- GitHub: fonte unica, CI e deploy trigger.
- Render Free: backend FastAPI via Docker.
- Upstash Redis Free: sessoes, jobs, cache e telemetria leve.
- Frontend estatico: servido pelo proprio FastAPI para reduzir partes moveis.
- Gemini/Groq/Cerebras: motores gratuitos com rate limit.
- OpenRouter: fallback opcional, bloqueado por `FREE_ONLY=true`.

## Fallback inteligente por tarefa

O roteador nao troca para qualquer API aleatoria. Ele usa a API gratuita mais proxima do trabalho pedido:

- `coding`: Groq -> Gemini -> Cerebras -> OpenRouter se permitido.
- `site`: Gemini -> Groq -> Cerebras -> OpenRouter se permitido.
- `auditoria`: Cerebras -> Groq -> Gemini -> OpenRouter se permitido.
- `planejamento`: Gemini -> Groq -> Cerebras -> OpenRouter se permitido.

Com `FREE_ONLY=true`, OpenRouter fica bloqueado. Se todas as rotas gratuitas falharem por limite, erro ou chave ausente, o sistema retorna um plano offline estruturado em vez de gerar custo.

## Principios

- Nunca commitar `.env`, chaves, logs ou dados de usuario.
- Toda missao longa vira job assincrono.
- O endpoint `/api/comando` apenas enfileira; `/api/jobs/{id}` acompanha progresso.
- A resposta deve ser orientada a codigo: arquivos, diffs, testes, riscos e deploy.
- O modo padrao `LLM_MODE=mock` permite validar nuvem sem gastar cota de IA.
- Para ativar provedores reais, usar `LLM_MODE=providers` e secrets do host.

## Proximas melhorias tecnicas

1. Streaming via Server-Sent Events em `/api/jobs/{id}/events`.
2. Persistencia de agentes customizados em Redis ou GitHub Contents API.
3. RAG leve lendo arquivos versionados de `conhecimento/`.
4. Geracao de patches multi-arquivo com schema JSON validado.
5. Sandbox de testes gratuito/isolado antes de entregar codigo ao usuario.
6. Auditoria automatica de secrets em cada job.
7. Deploy preview por branch usando GitHub Actions.

## Risco principal

"100% gratuito" significa operar dentro de cotas. O sistema precisa degradar com elegancia: quando um provedor atingir limite, ele deve pausar, trocar para outro motor gratuito ou responder com plano offline, nunca acionar fallback pago sem configuracao explicita.

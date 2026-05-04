# Supabase setup for Kemy AI

1. Crie ou identifique o projeto Supabase do Kimi AI.
2. Nao reutilize por engano o projeto Supabase do Kanban. Este backend deve apontar para o projeto dedicado do Kimi AI.
3. Abra SQL Editor.
4. Cole e execute `supabase/migrations/001_kemy_schema.sql`.
5. Execute tambem `supabase/migrations/002_sessions_owner_email.sql`.
6. Em `Project Settings > Data API`, exponha o schema `kemy`.
7. Se houver mais de um schema exposto, mantenha `kemy` disponivel para a API REST.
8. No Render, prefira adicionar:
   - `KIMI_SUPABASE_URL`
   - `KIMI_SUPABASE_ANON_KEY`
   - `KIMI_SUPABASE_SERVICE_ROLE_KEY`
9. As variaveis antigas `SUPABASE_*` ainda funcionam como compatibilidade, mas nao sao a melhor opcao quando existe tambem um Supabase do Kanban.
10. Redeploy o servico.

O backend usa `SERVICE_ROLE_KEY` apenas no servidor Render. Nunca coloque essa chave no frontend.

As tabelas ficam isoladas no schema `kemy`, para poder compartilhar um projeto Supabase com outro sistema sem misturar dados. O backend seleciona esse schema na API REST usando os headers de profile do PostgREST.

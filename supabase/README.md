# Supabase setup for Kemy AI

1. Crie um projeto em Supabase.
2. Abra SQL Editor.
3. Cole e execute `supabase/migrations/001_kemy_schema.sql`.
4. No Render, adicione:
   - `SUPABASE_URL`
   - `SUPABASE_ANON_KEY`
   - `SUPABASE_SERVICE_ROLE_KEY`
5. Redeploy o servico.

O backend usa `SERVICE_ROLE_KEY` apenas no servidor Render. Nunca coloque essa chave no frontend.

As tabelas ficam isoladas no schema `kemy`, para poder compartilhar um projeto Supabase com outro sistema sem misturar dados.

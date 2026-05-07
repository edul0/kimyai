alter table if exists kemy.generated_files
  add column if not exists name text,
  add column if not exists mime_type text,
  add column if not exists download_url text,
  add column if not exists content_base64 text,
  add column if not exists size_bytes bigint;

create index if not exists kemy_generated_files_job_name_idx
  on kemy.generated_files (job_id, name);

create index if not exists kemy_generated_files_job_path_created_idx
  on kemy.generated_files (job_id, path, created_at desc);

grant usage on schema kemy to anon, authenticated, service_role;
grant select, insert, update, delete on all tables in schema kemy to authenticated, service_role;
grant usage, select on all sequences in schema kemy to authenticated, service_role;

alter role authenticator
  set pgrst.db_schemas = 'public,graphql_public,kemy';

notify pgrst, 'reload config';

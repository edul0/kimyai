do $$
begin
  if not exists (
    select 1 from pg_publication_tables
    where pubname = 'supabase_realtime' and schemaname = 'kemy' and tablename = 'sessions'
  ) then
    alter publication supabase_realtime add table kemy.sessions;
  end if;

  if not exists (
    select 1 from pg_publication_tables
    where pubname = 'supabase_realtime' and schemaname = 'kemy' and tablename = 'messages'
  ) then
    alter publication supabase_realtime add table kemy.messages;
  end if;

  if not exists (
    select 1 from pg_publication_tables
    where pubname = 'supabase_realtime' and schemaname = 'kemy' and tablename = 'jobs'
  ) then
    alter publication supabase_realtime add table kemy.jobs;
  end if;

  if not exists (
    select 1 from pg_publication_tables
    where pubname = 'supabase_realtime' and schemaname = 'kemy' and tablename = 'generated_files'
  ) then
    alter publication supabase_realtime add table kemy.generated_files;
  end if;
end $$;

create index if not exists kemy_jobs_session_updated_idx
  on kemy.jobs (session_id, updated_at desc);

create index if not exists kemy_messages_session_created_idx
  on kemy.messages (session_id, created_at asc);

create index if not exists kemy_generated_files_job_idx
  on kemy.generated_files (job_id, created_at asc);

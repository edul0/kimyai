alter table if exists kemy.sessions
  add column if not exists owner_email text;

create index if not exists kemy_sessions_owner_email_idx
  on kemy.sessions (owner_email, updated_at desc);

drop policy if exists "kemy sessions owner all" on kemy.sessions;
drop policy if exists "kemy jobs session owner read" on kemy.jobs;
drop policy if exists "kemy messages session owner read" on kemy.messages;
drop policy if exists "kemy files job session owner read" on kemy.generated_files;

create policy "kemy sessions owner all" on kemy.sessions
  for all
  using (
    auth.uid() = owner_id
    or (owner_email is not null and auth.email() is not null and lower(owner_email) = lower(auth.email()))
  )
  with check (
    auth.uid() = owner_id
    or (owner_email is not null and auth.email() is not null and lower(owner_email) = lower(auth.email()))
  );

create policy "kemy jobs session owner read" on kemy.jobs
  for select
  using (
    exists (
      select 1
      from kemy.sessions s
      where s.id = jobs.session_id
        and (
          s.owner_id = auth.uid()
          or (s.owner_email is not null and auth.email() is not null and lower(s.owner_email) = lower(auth.email()))
        )
    )
  );

create policy "kemy messages session owner read" on kemy.messages
  for select
  using (
    exists (
      select 1
      from kemy.sessions s
      where s.id = messages.session_id
        and (
          s.owner_id = auth.uid()
          or (s.owner_email is not null and auth.email() is not null and lower(s.owner_email) = lower(auth.email()))
        )
    )
  );

create policy "kemy files job session owner read" on kemy.generated_files
  for select
  using (
    exists (
      select 1
      from kemy.jobs j
      join kemy.sessions s on s.id = j.session_id
      where j.id = generated_files.job_id
        and (
          s.owner_id = auth.uid()
          or (s.owner_email is not null and auth.email() is not null and lower(s.owner_email) = lower(auth.email()))
        )
    )
  );

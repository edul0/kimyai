alter table if exists kemy.sessions
  add column if not exists owner_email text;

create index if not exists kemy_sessions_owner_email_idx
  on kemy.sessions (owner_email, updated_at desc);

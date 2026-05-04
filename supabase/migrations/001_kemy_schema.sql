create extension if not exists pgcrypto;

create schema if not exists kemy;

create table if not exists kemy.profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  email text,
  name text,
  created_at timestamptz not null default now()
);

create table if not exists kemy.projects (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid references auth.users(id) on delete cascade,
  name text not null,
  description text,
  created_at timestamptz not null default now()
);

create table if not exists kemy.sessions (
  id uuid primary key,
  owner_id uuid references auth.users(id) on delete set null,
  project_id uuid references kemy.projects(id) on delete set null,
  title text not null default 'Nova sessao',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists kemy.jobs (
  id uuid primary key,
  session_id uuid references kemy.sessions(id) on delete cascade,
  status text not null,
  mode text not null default 'coding',
  prompt text not null,
  progress int not null default 0,
  stage text,
  result jsonb,
  error text,
  events jsonb not null default '[]'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists kemy.messages (
  id uuid primary key default gen_random_uuid(),
  session_id uuid references kemy.sessions(id) on delete cascade,
  role text not null check (role in ('user', 'assistant', 'system', 'tool')),
  content text not null,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create table if not exists kemy.generated_files (
  id uuid primary key default gen_random_uuid(),
  job_id uuid references kemy.jobs(id) on delete cascade,
  path text not null,
  language text,
  content text not null,
  created_at timestamptz not null default now()
);

create table if not exists kemy.custom_agents (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid references auth.users(id) on delete cascade,
  name text not null,
  role text not null,
  goal text not null,
  backstory text,
  engine text not null default 'groq',
  knowledge_path text,
  created_at timestamptz not null default now()
);

alter table kemy.profiles enable row level security;
alter table kemy.projects enable row level security;
alter table kemy.sessions enable row level security;
alter table kemy.jobs enable row level security;
alter table kemy.messages enable row level security;
alter table kemy.generated_files enable row level security;
alter table kemy.custom_agents enable row level security;

drop policy if exists "kemy profiles self read" on kemy.profiles;
drop policy if exists "kemy profiles self update" on kemy.profiles;
drop policy if exists "kemy projects owner all" on kemy.projects;
drop policy if exists "kemy sessions owner all" on kemy.sessions;
drop policy if exists "kemy jobs session owner read" on kemy.jobs;
drop policy if exists "kemy messages session owner read" on kemy.messages;
drop policy if exists "kemy files job session owner read" on kemy.generated_files;
drop policy if exists "kemy custom agents owner all" on kemy.custom_agents;

create policy "kemy profiles self read" on kemy.profiles for select using (auth.uid() = id);
create policy "kemy profiles self update" on kemy.profiles for update using (auth.uid() = id);
create policy "kemy projects owner all" on kemy.projects for all using (auth.uid() = owner_id) with check (auth.uid() = owner_id);
create policy "kemy sessions owner all" on kemy.sessions for all using (auth.uid() = owner_id or owner_id is null) with check (auth.uid() = owner_id or owner_id is null);
create policy "kemy jobs session owner read" on kemy.jobs for select using (
  exists (select 1 from kemy.sessions s where s.id = jobs.session_id and (s.owner_id = auth.uid() or s.owner_id is null))
);
create policy "kemy messages session owner read" on kemy.messages for select using (
  exists (select 1 from kemy.sessions s where s.id = messages.session_id and (s.owner_id = auth.uid() or s.owner_id is null))
);
create policy "kemy files job session owner read" on kemy.generated_files for select using (
  exists (
    select 1 from kemy.jobs j
    join kemy.sessions s on s.id = j.session_id
    where j.id = generated_files.job_id and (s.owner_id = auth.uid() or s.owner_id is null)
  )
);
create policy "kemy custom agents owner all" on kemy.custom_agents for all using (auth.uid() = owner_id) with check (auth.uid() = owner_id);

grant usage on schema kemy to anon, authenticated, service_role;
grant select, insert, update, delete on all tables in schema kemy to authenticated, service_role;
grant usage, select on all sequences in schema kemy to authenticated, service_role;

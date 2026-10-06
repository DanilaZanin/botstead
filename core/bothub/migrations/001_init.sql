create schema if not exists bothub;
set search_path = bothub;

create table bots (
  id text primary key,                 -- slug: scout, mac, sre
  name text not null,
  role text not null default '',       -- одна фраза
  instructions text not null default '',
  avatar text not null default 'robot',   -- scout|mac|sre|coder|archive|owl|spark|robot
  provider text not null check (provider in ('claude','codex','gemini','fake')),
  model text not null,
  executor text not null default 'container' check (executor in ('container','mac')),
  mac_full_control boolean not null default false,
  auto_allow jsonb not null default '[]',   -- список правил, раздел 4
  mcp_allow jsonb not null default '[]',    -- имена MCP-инструментов, пусто = только bothub
  budget_daily_tokens integer not null default 200000,
  max_turn_seconds integer not null default 1800,
  status text not null default 'idle',       -- idle|running|waiting|stopped|error
  created_at timestamptz not null default now()
);

create table threads (
  id uuid primary key default gen_random_uuid(),
  bot_id text not null references bots(id),
  kind text not null default 'direct' check (kind in ('direct','routine','incident')),
  title text not null default '',
  status text not null default 'active' check (status in ('active','archived')),
  cli_session_id text,
  summary text not null default '',
  dry_run boolean not null default false,
  last_seq bigint not null default 0,
  created_at timestamptz not null default now()
);

create table turns (
  id uuid primary key default gen_random_uuid(),
  thread_id uuid not null references threads(id),
  status text not null default 'queued' check (status in ('queued','running','waiting_approval','waiting_mac','done','stopped','error')),
  prompt text not null,
  client text not null default 'api',          -- iphone|mac|schedule|hook|api
  lease_until timestamptz,
  steps integer not null default 0,
  error text,
  started_at timestamptz,
  finished_at timestamptz,
  created_at timestamptz not null default now()
);

create table events (                            -- append-only, это же аудит-лог
  thread_id uuid not null references threads(id),
  seq bigint not null,
  ts timestamptz not null default now(),
  turn_id uuid references turns(id),
  kind text not null,                             -- раздел 3
  actor text not null,                            -- owner | bot:<id> | system
  client text,
  payload jsonb not null default '{}',
  primary key (thread_id, seq)
);

create table approvals (
  id uuid primary key default gen_random_uuid(),
  thread_id uuid not null references threads(id),
  turn_id uuid references turns(id),
  bot_id text not null references bots(id),
  risk text not null check (risk in ('pay','send','delete','login','push','other')),
  title text not null,
  tool text not null,
  args jsonb not null,
  args_hash text not null,                        -- sha256 канонического JSON args
  status text not null default 'pending' check (status in ('pending','approved','rejected','expired')),
  remember boolean not null default false,        -- «разрешать без спроса» создать правило
  expires_at timestamptz not null,
  decided_at timestamptz,
  decided_from text,
  created_at timestamptz not null default now()
);

create table memory (
  id uuid primary key default gen_random_uuid(),
  bot_id text references bots(id),                -- null = общая
  text text not null,
  source text not null default 'owner',          -- owner | bot:<id>
  source_thread_id uuid,
  status text not null default 'active' check (status in ('proposed','active','archived')),
  version integer not null default 1,
  expires_at timestamptz,
  created_at timestamptz not null default now()
);

create table usage (
  id bigserial primary key,
  bot_id text not null references bots(id),
  thread_id uuid,
  turn_id uuid,
  provider text not null,
  model text not null,
  tokens_in integer not null default 0,
  tokens_out integer not null default 0,
  ts timestamptz not null default now()
);

create table schedules (
  id uuid primary key default gen_random_uuid(),
  bot_id text not null references bots(id),
  name text not null,
  kind text not null check (kind in ('cron','hook','mac_folder')),
  cron text,                                       -- для kind=cron
  timezone text not null default 'Europe/Moscow',
  prompt text not null,
  hook_token text,                                 -- для kind=hook, случайный
  enabled boolean not null default true,
  next_run_at timestamptz,
  last_turn_id uuid,
  created_at timestamptz not null default now()
);

create table files (
  id uuid primary key default gen_random_uuid(),
  thread_id uuid not null references threads(id),
  name text not null,
  origin text not null,                            -- mac:<путь> | container:<путь> | upload
  size bigint not null,
  mime text not null,
  storage_path text not null,                      -- путь в FILES_DIR на сервере
  expires_at timestamptz,
  created_at timestamptz not null default now()
);

create table outbox (
  id bigserial primary key,
  kind text not null check (kind in ('push','telegram')),
  dedup_key text unique,
  payload jsonb not null,
  attempts integer not null default 0,
  sent_at timestamptz,
  created_at timestamptz not null default now()
);

create table push_subscriptions (
  endpoint text primary key,
  keys jsonb not null,
  device text not null default '',
  created_at timestamptz not null default now()
);

create table mac_status (
  id integer primary key default 1 check (id = 1),
  state text not null default 'offline' check (state in ('online','sleep','offline','locked','needs_permission')),
  last_seen timestamptz,
  info jsonb not null default '{}'
);

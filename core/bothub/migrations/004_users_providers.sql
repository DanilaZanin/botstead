-- Этап 0.5: пользователи, инвайты, сессии, реестр провайдеров и моделей, процедуры, owner_id (контракты: docs/contracts.md, разделы 10-14).
-- Все owner_id в существующих таблицах nullable: строки до появления первого админа остаются «ничьими»,
-- админ забирает их при первичной настройке (docs/contracts.md, раздел 10).
set search_path = bothub;

create table users (
  id uuid primary key default gen_random_uuid(),
  email text not null unique check (email = lower(email)),   -- хранится в нижнем регистре, ядро нормализует до вставки
  password_hash text not null,                                -- argon2id, строка в формате PHC
  role text not null default 'member' check (role in ('admin','member')),
  status text not null default 'active' check (status in ('active','disabled')),
  created_at timestamptz not null default now()
);

create table invites (
  token_hash text primary key check (token_hash ~ '^[0-9a-f]{64}$'),   -- sha256(токен), hex в нижнем регистре; сам токен в БД не хранится
  created_by uuid not null references users(id),
  role text not null default 'member' check (role in ('admin','member')),
  expires_at timestamptz not null,
  used_by uuid references users(id),
  used_at timestamptz,
  check ((used_by is null) or (used_at is not null))
);
create index invites_created_by_idx on invites(created_by);

create table sessions (
  id_hash text primary key check (id_hash ~ '^[0-9a-f]{64}$'),         -- sha256(значение cookie), hex в нижнем регистре
  user_id uuid not null references users(id) on delete cascade,
  created_at timestamptz not null default now(),
  expires_at timestamptz not null,
  revoked_at timestamptz,
  user_agent text not null default ''
);
create index sessions_user_idx on sessions(user_id);

create table providers (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null references users(id),
  kind text not null check (kind in ('anthropic_api','openai_api','google_api','openai_compatible','cli_subscription')),
  cli text check (cli in ('claude','codex','agy')),           -- только для cli_subscription
  name text not null,
  base_url text,                                              -- обязателен для openai_compatible, для остальных необязателен
  secret_encrypted bytea,                                     -- API-ключ, раздел 11; для cli_subscription null
  status text not null default 'new' check (status in ('new','ok','error','disabled')),
  last_check_at timestamptz,
  last_error text,
  created_at timestamptz not null default now(),
  unique (owner_id, name),
  unique (id, owner_id),                                      -- цель составного FK из bots: бот видит только провайдера своего владельца
  check ((kind = 'cli_subscription') = (cli is not null)),
  check (kind <> 'openai_compatible' or base_url is not null),
  check (kind <> 'cli_subscription' or (secret_encrypted is null and base_url is null))
);

create table models (
  id uuid primary key default gen_random_uuid(),
  provider_id uuid not null references providers(id) on delete cascade,
  name text not null,                                         -- идентификатор модели у провайдера
  display_name text not null default '',
  enabled boolean not null default true,
  context_window integer check (context_window is null or context_window > 0),
  unique (provider_id, name),
  unique (id, provider_id)                                    -- цель составного FK из bots: модель принадлежит провайдеру бота
);

-- Процедура = записанная последовательность шагов (раздел 14). Шаги валидирует ядро, БД проверяет только форму.
create table procedures (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null references users(id),
  bot_id text references bots(id) on delete set null,
  name text not null,
  description text not null default '',
  params jsonb not null default '[]' check (jsonb_typeof(params) = 'array'),
  steps jsonb not null default '[]' check (jsonb_typeof(steps) = 'array'),
  source text not null default 'bot' check (source in ('bot','human','import')),
  status text not null default 'active' check (status in ('active','archived')),
  version integer not null default 1,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (owner_id, name)
);
create index procedures_bot_idx on procedures(bot_id);

create table procedure_runs (
  id uuid primary key default gen_random_uuid(),
  procedure_id uuid not null references procedures(id) on delete cascade,
  procedure_version integer not null,                         -- версия шагов на момент запуска
  bot_id text references bots(id) on delete set null,
  thread_id uuid references threads(id) on delete set null,
  turn_id uuid references turns(id) on delete set null,
  status text not null default 'queued'
    check (status in ('queued','running','waiting_approval','waiting_model','waiting_human','done','failed','stopped')),
  params jsonb not null default '{}',
  next_step integer not null default 0 check (next_step >= 0),
  step_log jsonb not null default '[]' check (jsonb_typeof(step_log) = 'array'),
  error text,
  started_at timestamptz,
  finished_at timestamptz,
  created_at timestamptz not null default now()
);
create index procedure_runs_procedure_idx on procedure_runs(procedure_id);
create index procedure_runs_status_idx on procedure_runs(status) where status in ('queued','running','waiting_approval','waiting_model','waiting_human');

-- owner_id в существующих таблицах с пользовательскими данными. turns, events, approvals, usage владельца не получают:
-- он выводится через thread_id или bot_id, чтобы не плодить расхождения.
alter table bots add column owner_id uuid references users(id);
alter table threads add column owner_id uuid references users(id);
alter table memory add column owner_id uuid references users(id);
alter table schedules add column owner_id uuid references users(id);
alter table files add column owner_id uuid references users(id);
alter table push_subscriptions add column owner_id uuid references users(id);
alter table outbox add column owner_id uuid references users(id);
alter table mac_status add column owner_id uuid references users(id);

create index bots_owner_idx on bots(owner_id);
create index threads_owner_idx on threads(owner_id);
create index memory_owner_idx on memory(owner_id);
create index schedules_owner_idx on schedules(owner_id);
create index files_owner_idx on files(owner_id);
create index push_subscriptions_owner_idx on push_subscriptions(owner_id);
create index outbox_owner_idx on outbox(owner_id);
create index mac_status_owner_idx on mac_status(owner_id);

-- Выбор модели из реестра. provider/model (text) остаются: на них держатся раннеры и старые боты.
-- Составные FK (PG 15+, on delete set null (колонка) обнуляет только указанную колонку):
--   (provider_id, owner_id) -> providers(id, owner_id): провайдер должен принадлежать владельцу бота;
--   (model_id, provider_id) -> models(id, provider_id): модель должна принадлежать провайдеру бота.
-- Удаление провайдера обнуляет bots.provider_id (owner_id остаётся) и через каскад на models обнуляет model_id;
-- удаление модели обнуляет только model_id. MATCH SIMPLE пропускает проверку, если в паре есть null, поэтому
-- два CHECK закрывают дыры: провайдер без владельца и модель без провайдера недопустимы.
alter table bots add column provider_id uuid;
alter table bots add column model_id uuid;
alter table bots add constraint bots_provider_owner_fk
  foreign key (provider_id, owner_id) references providers (id, owner_id) on delete set null (provider_id);
alter table bots add constraint bots_model_provider_fk
  foreign key (model_id, provider_id) references models (id, provider_id) on delete set null (model_id);
alter table bots add constraint bots_provider_needs_owner check (provider_id is null or owner_id is not null);
-- Удаление провайдера обнуляет provider_id раньше, чем каскад дойдёт до model_id; триггер снимает модель в том же
-- UPDATE, иначе CHECK ниже отклонил бы удаление провайдера.
create function bots_drop_model_without_provider() returns trigger language plpgsql as $$
begin
  if new.provider_id is null then new.model_id := null; end if;
  return new;
end $$;
create trigger bots_drop_model_without_provider before update of provider_id on bots
  for each row execute function bots_drop_model_without_provider();
alter table bots add constraint bots_model_needs_provider check (model_id is null or provider_id is not null);

-- Режимы разрешений по инструментам (docs/contracts.md, раздел 10). Пустой объект: поведение как раньше.
alter table bots add column tool_modes jsonb not null default '{}' check (jsonb_typeof(tool_modes) = 'object');

-- Расход по реестру: какой провайдер и модель, плюс токены кэша. Строки истории переживают удаление провайдера.
alter table usage add column provider_id uuid references providers(id) on delete set null;
alter table usage add column model_id uuid references models(id) on delete set null;
alter table usage add column tokens_cache_read integer not null default 0;
alter table usage add column tokens_cache_write integer not null default 0;
create index usage_provider_idx on usage(provider_id);
create index usage_model_idx on usage(model_id);

-- Секреты для процедур (ссылка vault:<имя>, раздел 14). Значение шифруется модулем secrets.py (раздел 11).
-- bot_id null: секрет пользователя, виден всем его ботам; иначе только этому боту. Имя уникально в пределах (владелец, бот),
-- null в bot_id считается равным null, чтобы не заводить два общих секрета с одним именем.
create table secrets (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null references users(id),
  bot_id text references bots(id) on delete cascade,
  name text not null,
  value_encrypted bytea not null,
  created_at timestamptz not null default now(),
  unique nulls not distinct (owner_id, bot_id, name)
);
create index secrets_bot_idx on secrets(bot_id);

-- mac_status: строка id=1 остаётся (код читает её по id=1), но id больше не прибит к единице.
-- Новые строки получают id из последовательности со старта 2, по одной на Mac пользователя.
alter table mac_status drop constraint mac_status_id_check;
create sequence mac_status_id_seq start 2 owned by mac_status.id;
alter table mac_status alter column id set default nextval('mac_status_id_seq');

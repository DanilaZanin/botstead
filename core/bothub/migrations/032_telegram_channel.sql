-- Канал Telegram для бота: токен хранится зашифрованным (как slack_signing_secret).
-- webhook_secret генерируется при сохранении токена; allowed_chat_ids задаёт владелец.
create table bothub.bot_channels (
  id uuid primary key default gen_random_uuid(),
  bot_id text not null references bothub.bots(id) on delete cascade,
  owner_id uuid not null references bothub.users(id) on delete cascade,
  kind text not null default 'telegram' check (kind = 'telegram'),
  token_enc bytea,
  allowed_chat_ids bigint[] not null default '{}',
  webhook_secret text,
  enabled boolean not null default true,
  created_at timestamptz not null default now(),
  unique(bot_id, kind)
);

create table bothub.telegram_threads (
  channel_id uuid not null references bothub.bot_channels(id) on delete cascade,
  chat_id bigint not null,
  thread_id uuid not null references bothub.threads(id) on delete cascade,
  primary key (channel_id, chat_id),
  unique (thread_id)
);

create table bothub.telegram_updates (
  channel_id uuid not null references bothub.bot_channels(id) on delete cascade,
  update_id bigint not null,
  chat_id bigint not null,
  turn_id uuid unique references bothub.turns(id) on delete set null,
  primary key (channel_id, update_id)
);

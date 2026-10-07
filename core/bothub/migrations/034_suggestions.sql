-- Подсказки отдельны от обычных сообщений бота: принятие запускает обычный ход.
alter table bothub.bots add column proactive_interval_hours integer
  check (proactive_interval_hours is null or proactive_interval_hours between 1 and 720);
alter table bothub.bots add column last_proactive_at timestamptz;
alter table bothub.turns drop constraint if exists turns_turn_type_check;
alter table bothub.turns add constraint turns_turn_type_check
  check (turn_type in ('normal','compact','proactive'));
alter table bothub.usage drop constraint if exists usage_turn_type_check;
alter table bothub.usage add constraint usage_turn_type_check
  check (turn_type in ('normal','compact','proactive'));

create table bothub.suggestions (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null references bothub.users(id),
  bot_id text not null references bothub.bots(id) on delete cascade,
  text text not null check (length(text) between 1 and 2000),
  status text not null default 'proposed' check (status in ('proposed','accepted','dismissed')),
  created_at timestamptz not null default now()
);
create index suggestions_owner_status_created on bothub.suggestions(owner_id,status,created_at desc);

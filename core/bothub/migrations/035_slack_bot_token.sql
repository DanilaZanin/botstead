-- Ответ бота в Slack (docs/contracts.md, раздел 18): токен приложения и обработанные event_id.
alter table bothub.schedules add column if not exists slack_bot_token bytea;
alter table bothub.schedules add column if not exists slack_team_id text;

create table if not exists bothub.slack_handled_events (
  event_id text primary key,
  created_at timestamptz not null default now()
);

create table if not exists bothub.slack_hook_payloads (
  turn_id uuid primary key references bothub.turns(id) on delete cascade,
  schedule_id uuid not null references bothub.schedules(id) on delete cascade,
  payload jsonb not null
);

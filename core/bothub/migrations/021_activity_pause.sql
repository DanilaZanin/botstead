-- Этап 8 (docs/contracts.md, раздел 16): лента активности, пауза бота, пауза триггеров при недоступном исполнителе.
set search_path = bothub;

-- Пауза бота: worker не берёт его queued turn, расписания и hooks пропускают запуск, запуск процедур даёт 409.
alter table bots add column if not exists paused boolean not null default false;
alter table bots add column if not exists paused_at timestamptz;
alter table bots add column if not exists paused_reason text;

-- Пропуски триггеров. skipped_count: пропусков подряд с последнего успешного запуска; после 5 расписание
-- получает paused_by_unavailable и проверяется не чаще раза в 15 минут. last_skip_event_at ограничивает
-- событие о пропуске одним в час. catch_up: один запуск после возобновления вместо пропущенных.
alter table schedules add column if not exists skipped_count integer not null default 0 check (skipped_count >= 0);
alter table schedules add column if not exists last_skipped_at timestamptz;
alter table schedules add column if not exists last_skip_reason text
  check (last_skip_reason is null or last_skip_reason in ('executor_unavailable','bot_paused','provider_unavailable'));
alter table schedules add column if not exists last_skip_event_at timestamptz;
alter table schedules add column if not exists paused_by_unavailable boolean not null default false;
alter table schedules add column if not exists catch_up boolean not null default false;

-- Журнал событий уровня бота и расписания, которым не принадлежит тред: пауза и возобновление бота, пропуски
-- и возобновление расписаний. Хранит короткий код и параметры, человекочитаемую строку собирает клиент.
-- bot_id и thread_id без внешних ключей: запись аудита переживает удаление бота и треда.
create table if not exists activity_log (
  id bigserial primary key,
  owner_id uuid not null references users(id),
  bot_id text,
  thread_id uuid,
  kind text not null check (kind in ('pause','schedule')),
  code text not null check (code in ('bot_paused','bot_resumed','schedule_skipped','schedule_resumed')),
  params jsonb not null default '{}' check (jsonb_typeof(params) = 'object'),
  at timestamptz not null default now()
);
create index if not exists activity_log_owner_at_idx on activity_log(owner_id, at desc, id desc);

-- Индексы под выборки ленты: у каждой ветки запроса своё время и отбор по владельцу через thread/bot.
create index if not exists threads_owner_bot_idx on threads(owner_id, bot_id);
create index if not exists turns_started_idx on turns(started_at desc) where started_at is not null;
create index if not exists turns_finished_idx on turns(finished_at desc) where finished_at is not null;
create index if not exists turns_schedule_created_idx on turns(created_at desc) where client in ('schedule','hook');
create index if not exists turns_thread_idx on turns(thread_id);
create index if not exists approvals_created_idx on approvals(created_at desc);
create index if not exists approvals_decided_idx on approvals(decided_at desc) where decided_at is not null;
create index if not exists events_activity_idx on events(ts desc) where kind in ('browser_step','browser_control');
create index if not exists procedure_runs_created_idx on procedure_runs(created_at desc);
create index if not exists procedure_runs_finished_idx on procedure_runs(finished_at desc) where finished_at is not null;
create index if not exists memory_proposed_idx on memory(owner_id, created_at desc) where source like 'bot:%';

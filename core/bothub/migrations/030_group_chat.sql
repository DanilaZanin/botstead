-- Групповой чат ботов (docs/contracts.md, раздел 21): общий тред `group`, участники, запуски обсуждения.
-- Ход бота в обсуждении это обычный ход этого бота в служебном треде `group_bot` (по одному на пару группа и бот),
-- а его ответ ядро переписывает в общий тред как assistant_msg. Так очередь, лимиты, бюджет и одобрения остаются общими.
set search_path = bothub;

alter table threads alter column bot_id drop not null;  -- у общего треда группы нет одного бота
alter table threads add column if not exists group_id uuid references threads(id) on delete cascade;  -- служебный тред бота -> общий тред
alter table threads drop constraint if exists threads_kind_check;
alter table threads add constraint threads_kind_check check (kind in ('direct','routine','incident','group','group_bot'));

create table if not exists group_runs (
  id uuid primary key default gen_random_uuid(),
  thread_id uuid not null references threads(id),
  owner_id uuid not null references users(id),
  status text not null default 'running' check (status in ('running','done','stopped','failed')),
  mode text not null check (mode in ('round','debate','moderated')),
  round int not null default 0,
  max_rounds int not null check (max_rounds between 1 and 10),
  tokens_used bigint not null default 0,
  token_budget bigint,
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  stop_reason text,
  -- состояние автомата обсуждения (его двигает group_tick, поэтому рестарт ядра ничего не теряет)
  owner_text text not null default '',
  phase text not null default 'bots' check (phase in ('bots','moderator')),
  pending jsonb not null default '[]',
  agreed jsonb not null default '[]',
  current_turn_id uuid,
  current_bot_id text,
  user_seq bigint,
  waiting_notice boolean not null default false
);
create unique index if not exists group_runs_one_running_idx on group_runs(thread_id) where status = 'running';
create index if not exists group_runs_thread_idx on group_runs(thread_id, started_at desc);

create table if not exists group_members (
  thread_id uuid not null references threads(id) on delete cascade,
  bot_id text not null references bots(id),
  position int not null,
  turn_thread_id uuid references threads(id),
  primary key (thread_id, bot_id)
);

-- Настройки группы живут в строке общего треда.
alter table threads add column if not exists group_mode text check (group_mode is null or group_mode in ('round','debate','moderated'));
alter table threads add column if not exists group_max_rounds int check (group_max_rounds is null or group_max_rounds between 1 and 10);
alter table threads add column if not exists group_moderator text;
alter table threads add column if not exists group_token_budget bigint;

alter table turns add column if not exists group_run_id uuid references group_runs(id) on delete set null;
create index if not exists turns_group_run_idx on turns(group_run_id) where group_run_id is not null;

alter table activity_log drop constraint if exists activity_log_code_check;
alter table activity_log add constraint activity_log_code_check
  check (code in ('bot_paused','bot_resumed','schedule_skipped','schedule_resumed','wakeup_scheduled','wakeup_fired','wakeup_skipped','delegation_sent','delegation_done','group_started','group_finished'));

-- Вид ленты `group` для записей групп (тот же журнал activity_log).
alter table activity_log drop constraint if exists activity_log_kind_check;
alter table activity_log add constraint activity_log_kind_check check (kind in ('pause','schedule','group'));

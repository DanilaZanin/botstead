-- Самопробуждение бота (docs/contracts.md, раздел 17): бот сам просит ядро разбудить его в заданный момент.
-- Строка живёт до срабатывания: active -> fired (создан turn) или skipped (бот на паузе или исполнитель так и не
-- появился за льготное окно). Отмена владельцем удаляет строку.
-- thread_id: тред, из которого бот попросил пробуждение; пробуждение продолжает его, а если тред уже не активен,
-- ядро заводит рутину-тред. Без внешнего ключа с запретом удаления: тред может исчезнуть, строка останется.
-- skip_reason: почему пропущено (те же коды, что у пауз триггеров, activity.SKIP_REASONS).
create table if not exists bothub.wakeups (
  id uuid primary key default gen_random_uuid(),
  bot_id text not null references bothub.bots(id) on delete cascade,
  thread_id uuid references bothub.threads(id) on delete set null,
  scheduled_at timestamptz not null,
  status text not null default 'active' check (status in ('active','fired','skipped')),
  prompt text not null check (char_length(prompt) between 1 and 2000),
  reason text not null default '' check (char_length(reason) <= 200),
  skip_reason text check (skip_reason is null or skip_reason in ('executor_unavailable','bot_paused','provider_unavailable','check_failed')),
  created_at timestamptz not null default now(),
  fired_at timestamptz
);
-- Планировщик берёт только активные по сроку; список бота идёт по его индексу.
create index if not exists wakeups_due_idx on bothub.wakeups(scheduled_at) where status = 'active';
create index if not exists wakeups_bot_idx on bothub.wakeups(bot_id, scheduled_at desc);

-- Лента активности: три новых кода журнала (вид `schedule`, как у пропусков расписаний).
alter table bothub.activity_log drop constraint if exists activity_log_code_check;
alter table bothub.activity_log add constraint activity_log_code_check
  check (code in ('bot_paused','bot_resumed','schedule_skipped','schedule_resumed','wakeup_scheduled','wakeup_fired','wakeup_skipped'));

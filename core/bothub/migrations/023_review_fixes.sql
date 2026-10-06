-- Этап 8, правки по ревью (docs/contracts.md, разделы 15 и 16).
-- Раннер миграций исполняет файл в одной транзакции, поэтому `create index concurrently` здесь невозможен: индексы строятся
-- обычным create index и на время сборки блокируют запись в свою таблицу. На большой базе запускать в окно обслуживания.

-- Лента активности идёт от владельца: сначала его треды и процедуры, затем по каждому родителю индекс, начинающийся с
-- thread_id или procedure_id. Индексы 021 шли по времени всей системы (на чужих строках выборка читала их все): они больше
-- не нужны и убираются, чтобы не замедлять запись.
drop index if exists bothub.turns_started_idx;
drop index if exists bothub.turns_finished_idx;
drop index if exists bothub.turns_schedule_created_idx;
drop index if exists bothub.approvals_created_idx;
drop index if exists bothub.approvals_decided_idx;
drop index if exists bothub.events_activity_idx;
drop index if exists bothub.procedure_runs_created_idx;
drop index if exists bothub.procedure_runs_finished_idx;

create index if not exists turns_thread_started_idx on bothub.turns(thread_id, started_at desc) where started_at is not null;
create index if not exists turns_thread_finished_idx on bothub.turns(thread_id, finished_at desc) where finished_at is not null;
create index if not exists turns_thread_schedule_idx on bothub.turns(thread_id, created_at desc) where client in ('schedule','hook');
create index if not exists approvals_thread_created_idx on bothub.approvals(thread_id, created_at desc);
create index if not exists approvals_thread_decided_idx on bothub.approvals(thread_id, (coalesce(decided_at, expires_at)) desc)
  where status in ('approved','rejected','expired');
create index if not exists events_thread_activity_idx on bothub.events(thread_id, ts desc) where kind in ('browser_step','browser_control');
create index if not exists procedure_runs_proc_created_idx on bothub.procedure_runs(procedure_id, created_at desc);
create index if not exists procedure_runs_proc_finished_idx on bothub.procedure_runs(procedure_id, finished_at desc) where finished_at is not null;

-- Расписание, у которого проверка исполнителя сама упала (trigger_block бросил исключение): срабатывание считается пропущенным с
-- причиной check_failed, расписание сдвигается на следующий срок и не крутится каждые 30 секунд.
alter table bothub.schedules drop constraint if exists schedules_last_skip_reason_check;
alter table bothub.schedules add constraint schedules_last_skip_reason_check
  check (last_skip_reason is null or last_skip_reason in ('executor_unavailable','bot_paused','provider_unavailable','check_failed'));

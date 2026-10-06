-- Поручения бота боту (docs/contracts.md, раздел 19): ход получателя помнит, из какого хода и каким ботом он создан.
-- delegated_from_turn: ход бота-отправителя (по нему видна глубина: ход, созданный поручением, дальше не поручает).
-- delegated_by_bot: id бота-отправителя; без внешнего ключа, как у журнала: поручение переживает удаление бота.
alter table bothub.turns add column if not exists delegated_from_turn uuid references bothub.turns(id) on delete set null;
alter table bothub.turns add column if not exists delegated_by_bot text;
create index if not exists turns_delegated_by_idx on bothub.turns(delegated_by_bot, status) where delegated_by_bot is not null;

-- Лента активности: два новых кода журнала (вид `schedule`, как у самопробуждений).
alter table bothub.activity_log drop constraint if exists activity_log_code_check;
alter table bothub.activity_log add constraint activity_log_code_check
  check (code in ('bot_paused','bot_resumed','schedule_skipped','schedule_resumed','wakeup_scheduled','wakeup_fired','wakeup_skipped','delegation_sent','delegation_done'));

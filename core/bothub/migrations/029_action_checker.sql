-- Проверяющая модель действий (docs/contracts.md, раздел 20): вторая оценка рискованного действия бота до владельца.
-- bots.checker_model_id: модель того же владельца из реестра (владельца проверяет API, как model_id); модель удалена:
-- проверка у бота выключается (set null). Метки approvals: вердикт и причина проверяющей модели (подсказка владельцу).
set search_path = bothub;

alter table bots add column if not exists checker_model_id uuid references models(id) on delete set null;
alter table approvals add column if not exists checker_verdict text check (checker_verdict is null or checker_verdict in ('allow','deny','ask'));
alter table approvals add column if not exists checker_reason text check (checker_reason is null or char_length(checker_reason) <= 200);

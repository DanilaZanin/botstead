-- Воспроизведение процедур (docs/contracts.md, раздел 14): состояние, которое нужно исполнителю между шагами и после
-- рестарта ядра. Данных страницы и значений параметров здесь нет: только коды, счётчики и ссылки.
--   reason         почему запуск в waiting_human (precondition_failed, element_not_found, element_ambiguous, unknown_outcome,
--                  browser_human, launcher_unavailable, ...); у остальных статусов null
--   approval_id    подтверждение текущего шага (waiting_approval и шаг после одобрения)
--   attempt        сколько раз текущий шаг уже повторялся после провала expect (предел 2); сбрасывается на следующем шаге
--   in_flight      действие шага запущено, исход ещё не записан: при рестарте это «неизвестный исход»
--   secret_params  имена секретных параметров процедуры на момент запуска: у них в params лежит vault:<имя>
alter table bothub.procedure_runs
  add column reason text,
  add column approval_id uuid references bothub.approvals(id) on delete set null,
  add column attempt integer not null default 0 check (attempt >= 0),
  add column in_flight boolean not null default false,
  add column secret_params jsonb not null default '[]'::jsonb check (jsonb_typeof(secret_params) = 'array');

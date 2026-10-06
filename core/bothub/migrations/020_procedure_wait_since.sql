-- Ожидание человека не длиннее BOTHUB_PROCEDURE_WAIT_HOURS (docs/contracts.md, раздел 14, «Ожидание человека»).
--   waiting_since  когда запуск последний раз перешёл в waiting_approval или waiting_human (его ставит pause() и восстановление
--                  после рестарта ядра); у запусков вне ожидания значение прежнее и ни на что не влияет
-- Запуски, которые уже ждут на момент миграции, получают now(): срок для них считается с выката, а не задним числом.
alter table bothub.procedure_runs
  add column waiting_since timestamptz;

update bothub.procedure_runs set waiting_since = now() where status in ('waiting_approval', 'waiting_human');

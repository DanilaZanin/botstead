-- Этап 1, финальная доводка (только добавление, существующие данные не меняются).
set search_path = bothub;

-- Пункт 6: запись о попытке доставки создаётся ДО сетевой отправки со статусом 'sending'.
alter table outbox_deliveries drop constraint if exists outbox_deliveries_status_check;
alter table outbox_deliveries add constraint outbox_deliveries_status_check
  check (status in ('sending','sent','failed','gone'));

-- Пункт 7: одобрение mac-вызова расходуется. null = ещё не использовано; не null = потрачено.
-- Одобрения с remember=true не расходуются (повтор разрешает правило).
alter table approvals add column if not exists used_at timestamptz;

-- Этап 1Б: надёжность (только добавление колонок и таблиц, существующие данные не меняются).
set search_path = bothub;

-- Пункт 9: последний «пинг» раннера по turn (событие раннера, wait/usage/mac_call от бота).
-- null = пинга ещё не было, тогда считаем от lease_until/started_at/created_at.
alter table turns add column if not exists runner_ping_at timestamptz;

-- Пункт 6: хэш операции SHA256(tool + канонический JSON args). args_hash остаётся как был
-- (только args), op_hash привязывает решение к инструменту и аргументам вместе.
alter table approvals add column if not exists op_hash text;

-- Пункт 8: статус доставки outbox по каждому получателю. Без него сбой одной подписки
-- откатывал запись целиком и остальным подписчикам push уходил повторно.
alter table outbox add column if not exists next_attempt_at timestamptz not null default now();
alter table outbox add column if not exists failed_at timestamptz;   -- исчерпаны попытки

create table if not exists outbox_deliveries (
  outbox_id bigint not null references outbox(id) on delete cascade,
  endpoint text not null,                        -- push_subscriptions.endpoint (без FK: 410 удаляет подписку)
  status text not null check (status in ('sent','failed','gone')),
  attempts integer not null default 0,
  last_error text,
  updated_at timestamptz not null default now(),
  primary key (outbox_id, endpoint)
);

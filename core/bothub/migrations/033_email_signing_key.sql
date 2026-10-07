-- Ключ подписи Mailgun (Signing Key с панели Mailgun): его выдаёт Mailgun, владелец не выбирает. Хранится зашифрованным
-- (encrypt_secret, AAD = id расписания), наружу не отдаётся, только флаг has_email_signing_key.
alter table bothub.schedules add column if not exists email_signing_key bytea;
alter table bothub.schedules add column if not exists email_route_token text;
-- Прямые вставки hook-расписаний также получают отдельный токен.
alter table bothub.schedules alter column email_route_token set default
  (replace(gen_random_uuid()::text, '-', '') || replace(gen_random_uuid()::text, '-', ''));
-- Старые hook-расписания получают независимый токен до публикации почтового URL.
update bothub.schedules set email_route_token =
  replace(gen_random_uuid()::text, '-', '') || replace(gen_random_uuid()::text, '-', '')
  where kind = 'hook' and email_route_token is null;
create unique index if not exists schedules_email_route_token_idx on bothub.schedules(email_route_token);
alter table bothub.schedules add constraint schedules_email_route_token_distinct
  check (kind <> 'hook' or (email_route_token is not null and email_route_token is distinct from hook_token));

-- Отпечатки подписанных запросов на 31 минуту: повтор Mailgun не должен запускать второй ход.
create table if not exists bothub.email_webhook_receipts (
  schedule_id uuid not null references bothub.schedules(id) on delete cascade,
  fingerprint bytea not null,
  received_at timestamptz not null default now(),
  primary key (schedule_id, fingerprint)
);
create index if not exists email_webhook_receipts_received_at_idx on bothub.email_webhook_receipts(received_at);

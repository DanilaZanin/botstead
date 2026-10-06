-- Проверка ключа до сохранения и приватные адреса через администратора (docs/contracts.md, раздел 11).
-- unchecked: ключ сохранён с force без пробного запроса. pending_admin: приватный адрес участника ждёт одобрения администратора.
-- secret_tail: последние 4 символа ключа при длине не короче 12, для подсказки «какой ключ сохранён».
-- Для строк, созданных до этой миграции, secret_tail остаётся null до замены ключа: шифртекст в SQL не читается.
alter table bothub.providers add column secret_tail text check (secret_tail is null or char_length(secret_tail) = 4);
alter table bothub.providers add column allow_private boolean not null default false;
alter table bothub.providers drop constraint providers_status_check;
alter table bothub.providers add constraint providers_status_check
  check (status in ('new','ok','error','disabled','unchecked','pending_admin'));

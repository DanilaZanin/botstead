-- Истёкший вход подписочного CLI требует повторного входа владельца.
alter table bothub.providers drop constraint providers_status_check;
alter table bothub.providers add constraint providers_status_check
  check (status in ('new','ok','error','disabled','unchecked','pending_admin','needs_login'));

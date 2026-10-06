-- Черновик процедуры (docs/contracts.md, раздел 14): в записанных шагах есть fill без значения (needs_value), запуск такой
-- процедуры не разрешён, пока человек не задаст параметр или secret_ref. Остальная схема procedures не меняется.
do $$
declare old_name text;
begin
  select conname into old_name from pg_constraint
   where conrelid = 'bothub.procedures'::regclass and contype = 'c' and pg_get_constraintdef(oid) like '%archived%';
  if old_name is not null then
    execute format('alter table bothub.procedures drop constraint %I', old_name);
  end if;
end $$;
alter table bothub.procedures add constraint procedures_status_check check (status in ('draft','active','archived'));

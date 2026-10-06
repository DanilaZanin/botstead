alter table bothub.bots add column registry_bound boolean not null default false;
alter table bothub.bots add column stop_retry_exec_id text;
update bothub.bots set registry_bound=true where provider_id is not null;

create function bothub.mark_registry_bound() returns trigger language plpgsql as $$
begin
  if new.provider_id is not null or (tg_op='UPDATE' and old.registry_bound) then
    new.registry_bound := true;
  end if;
  return new;
end;
$$;
create trigger bots_mark_registry_bound before insert or update of provider_id,registry_bound on bothub.bots
for each row execute function bothub.mark_registry_bound();

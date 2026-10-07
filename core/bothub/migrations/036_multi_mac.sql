set search_path = bothub;

create table macs (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null references users(id) on delete cascade,
  name text not null check (char_length(name) between 1 and 80),
  token_hash text unique check (token_hash ~ '^[0-9a-f]{64}$'),
  last_seen_at timestamptz,
  created_at timestamptz not null default now(),
  unique (id, owner_id)
);
create index macs_owner_idx on macs(owner_id);

-- Переносим действующий токен и последнюю связь прежнего Mac каждого владельца.
-- В старой конфигурации токен мог быть только в MAC_AGENT_TOKEN; тогда хеш пуст до новой регистрации.
insert into macs(owner_id, name, token_hash, last_seen_at, created_at)
select u.id, left(coalesce(nullif(s.info->>'host', ''), 'Mac'), 80), t.token_hash, s.last_seen,
       coalesce(t.created_at, now())
from users u
left join mac_tokens t on t.user_id=u.id and t.revoked_at is null
left join settings setup on setup.key='setup_user_id' and (setup.value #>> '{}')=u.id::text
left join lateral (
  select id, info, last_seen from mac_status where owner_id=u.id order by id limit 1
) s on true
where t.token_hash is not null or s.id is not null
   or (setup.key is not null and u.role='admin' and u.status='active');

-- MAC_AGENT_TOKEN из окружения привязан к этой записи навсегда. Удаление Mac
-- оставляет настройку: другой Mac не наследует прежний токен.
insert into settings(key,value)
select 'legacy_mac_id',to_jsonb(m.id::text)
from macs m join settings setup on setup.key='setup_user_id' and (setup.value #>> '{}')=m.owner_id::text
order by m.created_at,m.id limit 1
on conflict(key) do update set value=excluded.value;

drop table mac_tokens;

alter table mac_status add column mac_id uuid references macs(id) on delete cascade;
update mac_status s set mac_id=m.id from macs m where s.owner_id=m.owner_id;
create index mac_status_mac_idx on mac_status(mac_id);

alter table bots add column mac_id uuid;
alter table bots add constraint bots_mac_owner_fk
  foreign key (mac_id, owner_id) references macs(id, owner_id) on delete set null (mac_id);
-- Существующие боты сохраняют доступ к Mac, которым пользовались до обновления.
update bots b set mac_id=m.id from macs m where b.owner_id=m.owner_id;

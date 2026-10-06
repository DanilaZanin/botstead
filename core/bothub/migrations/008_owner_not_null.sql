set search_path = bothub;

-- Existing rows can be ownerless until /api/setup adopts them. Upgrades with
-- an existing admin adopt rows here; fresh installations set NOT NULL in setup.

-- Existing installations may already have their first admin.
do $$
declare first_admin uuid;
declare owned_table text;
begin
  select id into first_admin from users where role='admin' order by created_at,id limit 1;
  if first_admin is not null then
    foreach owned_table in array array['bots','threads','memory','schedules','files','push_subscriptions','outbox','mac_status'] loop
      execute format('update bothub.%I set owner_id=$1 where owner_id is null', owned_table) using first_admin;
      execute format('alter table bothub.%I alter column owner_id set not null', owned_table);
    end loop;
  end if;
end $$;

alter table sessions add column last_extended_at timestamptz not null default now();

create table mac_tokens (
  token_hash text primary key check (token_hash ~ '^[0-9a-f]{64}$'),
  user_id uuid not null unique references users(id) on delete cascade,
  created_at timestamptz not null default now(),
  revoked_at timestamptz
);

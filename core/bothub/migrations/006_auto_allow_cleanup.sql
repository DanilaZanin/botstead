-- Этап 1, финальная доводка: чистка старых правил bots.auto_allow.
-- Удаляются правила, которые после ужесточения либо не действуют, либо слишком широки:
--   1) с непустым match, но без op_hash (старое «запомнить»: нельзя отличить от подмножества аргументов);
--   2) без match (или с пустым) для Bash, WebFetch и mcp__bothub__mac_delegate.
-- Удалённые правила сохраняются в auto_allow_removed, чтобы владелец мог их просмотреть.
-- Остальные правила (в том числе без match для читающих инструментов и правила с op_hash) не трогаются.
-- Миграция идемпотентна: повторный запуск ничего не находит.
set search_path = bothub;

create table if not exists auto_allow_removed (
  id bigserial primary key,
  bot_id text not null references bots(id) on delete cascade,
  rule jsonb not null,
  removed_at timestamptz not null default now()
);

create function pg_temp.is_legacy_auto_allow_rule(r jsonb) returns boolean
language sql immutable as $$
  select coalesce(
    jsonb_typeof(r) = 'object' and (
      (coalesce(r -> 'match', 'null'::jsonb) not in ('null'::jsonb, '{}'::jsonb)
        and coalesce(r ->> 'op_hash', '') = '')
      or (coalesce(r -> 'match', 'null'::jsonb) in ('null'::jsonb, '{}'::jsonb)
        and r ->> 'tool' in ('Bash', 'WebFetch', 'mcp__bothub__mac_delegate'))
    ),
    false)
$$;

insert into auto_allow_removed (bot_id, rule)
select b.id, e.rule
from bots b
cross join lateral jsonb_array_elements(case when jsonb_typeof(b.auto_allow) = 'array' then b.auto_allow else '[]'::jsonb end) with ordinality as e(rule, ord)
where jsonb_typeof(b.auto_allow) = 'array' and pg_temp.is_legacy_auto_allow_rule(e.rule)
order by b.id, e.ord;

update bots b
set auto_allow = coalesce(
  (select jsonb_agg(e.rule order by e.ord)
   from jsonb_array_elements(b.auto_allow) with ordinality as e(rule, ord)
   where not pg_temp.is_legacy_auto_allow_rule(e.rule)),
  '[]'::jsonb)
where jsonb_typeof(b.auto_allow) = 'array'
  and exists (select 1 from jsonb_array_elements(case when jsonb_typeof(b.auto_allow) = 'array' then b.auto_allow else '[]'::jsonb end) as e(rule) where pg_temp.is_legacy_auto_allow_rule(e.rule));

drop function pg_temp.is_legacy_auto_allow_rule(jsonb);

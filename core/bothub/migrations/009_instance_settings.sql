set search_path = bothub;

create table settings (
  key text primary key,
  value jsonb not null
);

alter table schedules add column paused_by_disabled boolean not null default false;

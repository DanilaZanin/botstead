set search_path = bothub;

-- Keep an owner's disabled choice when provider discovery runs again.
alter table models add column manually_disabled boolean not null default false;
-- Existing disabled rows may reflect an owner choice; keep them disabled until reenabled explicitly.
update models set manually_disabled = true where enabled = false;

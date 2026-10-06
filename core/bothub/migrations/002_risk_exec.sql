-- Категория риска exec: shell/AppleScript на Mac, меняющие состояние или запутанные (ревью Gemini 2026-09-25).
set search_path = bothub;
alter table approvals drop constraint if exists approvals_risk_check;
alter table approvals add constraint approvals_risk_check check (risk in ('pay','send','delete','login','push','exec','other'));

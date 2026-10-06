alter table bothub.bots add column browser_control text not null default 'bot'
  check (browser_control in ('bot', 'human', 'returning'));

alter table bothub.bots alter column budget_daily_tokens type bigint;
alter table bothub.usage
  alter column tokens_in type bigint,
  alter column tokens_out type bigint,
  alter column tokens_cache_read type bigint,
  alter column tokens_cache_write type bigint;

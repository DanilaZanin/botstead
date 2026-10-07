set search_path = bothub;

-- Price columns on models: USD per 1M tokens.
-- Filled during provider check for openai_compatible and openai_api providers
-- from GET /v1/models (OpenRouter pricing field).
alter table models add column if not exists price_in_per_mtok numeric check (price_in_per_mtok is null or price_in_per_mtok >= 0);
alter table models add column if not exists price_out_per_mtok numeric check (price_out_per_mtok is null or price_out_per_mtok >= 0);

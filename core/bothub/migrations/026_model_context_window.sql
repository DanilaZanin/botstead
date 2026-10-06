set search_path = bothub;

-- context_window column for models: filled by fetch_provider_models for
-- openai_compatible and openai_api providers (from /v1/models context_length).
alter table models add column if not exists context_window integer check (context_window is null or context_window > 0);

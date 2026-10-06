-- Заполнение контекста и сжатие треда (docs/contracts.md, раздел 15).
-- threads: размер контекста по событиям usage, окно модели, счётчики сжатия. models.context_window уже есть (004).
alter table bothub.threads
  add column if not exists context_tokens bigint check (context_tokens is null or context_tokens >= 0),
  add column if not exists context_window integer check (context_window is null or context_window > 0),
  add column if not exists context_estimated boolean not null default false,
  add column if not exists compacted_at timestamptz,
  add column if not exists compactions integer not null default 0,
  -- оценка размера контекста сразу после сжатия и seq события compacted: от них считается оценка по символам событий
  add column if not exists context_base_tokens bigint not null default 0,
  add column if not exists context_base_seq bigint not null default 0,
  -- завершённых обычных ходов с последнего сжатия; новый тред сразу может сжаться автоматически
  add column if not exists turns_since_compact integer not null default 3,
  add column if not exists auto_compact_disabled boolean not null default false;

-- Ход сжатия: тот же turn, но невидимый в ленте, со своим типом в turns и usage.
alter table bothub.turns
  add column if not exists turn_type text not null default 'normal' check (turn_type in ('normal','compact'));
alter table bothub.usage
  add column if not exists turn_type text not null default 'normal' check (turn_type in ('normal','compact'));

-- Порог автосжатия: 50..95 процентов окна, null выключает.
alter table bothub.bots
  add column if not exists auto_compact_percent integer default 80
    check (auto_compact_percent is null or auto_compact_percent between 50 and 95);

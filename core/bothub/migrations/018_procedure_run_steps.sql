-- Снимок шагов запуска (docs/contracts.md, раздел 14): запуск хранит копию steps процедуры своей версии, поэтому правка
-- процедуры после запуска не меняет то, что экран запуска показывает по этому запуску. Заполняет создание запуска;
-- у записей без снимка пустой массив.
alter table bothub.procedure_runs
  add column steps jsonb not null default '[]'::jsonb check (jsonb_typeof(steps) = 'array');

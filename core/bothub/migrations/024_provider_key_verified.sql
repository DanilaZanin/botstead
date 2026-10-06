-- Проверил ли сервер провайдера сам ключ (docs/contracts.md, раздел 11).
-- true: на заведомо неверный ключ сервер ответил 401/403 (или это anthropic/google, где 200 на настоящем ключе уже проверка).
-- false: сервер принял и неверный ключ (так у OpenRouter: GET /v1/models публичный), ключ не проверен, бот увидит отказ при первом запросе.
-- null: не определено (cli_subscription, второй запрос не ответил, строки до этой миграции).
alter table bothub.providers add column if not exists key_verified boolean;

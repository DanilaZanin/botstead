-- Секрет подписи Slack (Signing Secret приложения): его выдаёт Slack, владелец не выбирает. Хранится зашифрованным
-- (encrypt_secret, AAD = id расписания), наружу не отдаётся, только флаг has_slack_signing_secret.
alter table bothub.schedules add column if not exists slack_signing_secret bytea;

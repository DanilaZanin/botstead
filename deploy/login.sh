#!/usr/bin/env bash
# Войти во все CLI подписок для одного пользователя. Логины ложатся в его том bothub-login-<owner>;
# боты этого пользователя получают том read-only и копируют логины в свой HOME при старте.
# Общего каталога логинов и общего CLAUDE_CODE_OAUTH_TOKEN больше нет.
# Запуск на сервере: ./login.sh <owner-id>   (owner-id из bots.owner_id)
set -euo pipefail
cd "$(dirname "$0")"

OWNER=${1:-}
[[ $OWNER =~ ^[a-z0-9][a-z0-9-]{0,39}$ ]] || { echo "owner-id: строчные латинские буквы, цифры и дефис" >&2; exit 2; }

./launcherctl.sh login-container "$OWNER" >/dev/null
cat <<MSG
Откроется оболочка в логин-контейнере пользователя '$OWNER' (без root, без доступа к чужим данным).
Выполни по очереди и подтверди вход в браузере:
  claude          (внутри: /login, потом /exit)
  codex login --device-auth
  agy             (вход по ссылке, потом выход)
Затем: exit. Чтобы боты увидели новые логины, пересоздай их: ./launcherctl.sh recreate <bot-id>
MSG
exec docker exec -it --user 1000:1000 --workdir /home/bot "login-$OWNER" bash -l

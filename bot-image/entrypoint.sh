#!/bin/bash
# Запускается лаунчером от обычного пользователя (uid 1000), без capabilities, на read-only rootfs.
# Писать можно в $HOME (том бота), /tmp и /run (tmpfs).
set -euo pipefail

if [ "$(id -u)" = "0" ]; then
    echo "entrypoint: запуск от root запрещён" >&2
    exit 1
fi

# Рабочий каталог контейнера /home/bot: том бота, в нём пишет враждебный код. До exec ничего не запускается и не
# читается относительным путём, а PID 1 получает cwd=/ (иначе `python3 -` кладёт cwd первым в sys.path и
# импортирует /home/bot/ctypes.py, а freeze_bot PID 1 не убивает).
cd /

# Весь скрипт стартует под bot-guard (ENTRYPOINT в Dockerfile), дети наследуют фильтр. Файлы в /home/bot пишет
# враждебный код: FIFO или устройство на месте конфига заблокировали бы чтение и затянули бы запуск, поэтому читаются
# только обычные файлы (-f), а чтение ещё и ограничено по времени (гонка между проверкой и открытием).

# Copy logins from read-only mount to home directory if present
if [ -d "$HOME/.auth" ]; then
    echo "Copying logins from $HOME/.auth to $HOME/"
    timeout 60 cp -r "$HOME/.auth/." "$HOME/" || true
fi

# MCP-сервер bothub для Codex (Claude получает его через --mcp-config от раннера)
CODEX_CONFIG="$HOME/.codex/config.toml"
if ! mkdir -p "$HOME/.codex" 2>/dev/null; then
    echo "entrypoint: $HOME/.codex недоступен, MCP bothub для Codex не настроен" >&2
elif [ -e "$CODEX_CONFIG" ] && [ ! -f "$CODEX_CONFIG" ]; then
    echo "entrypoint: $CODEX_CONFIG не обычный файл, MCP bothub для Codex не настроен" >&2
elif ! timeout 5 grep -q "mcp_servers.bothub" "$CODEX_CONFIG" 2>/dev/null; then
    timeout 5 sh -c 'cat >> "$1"' _ "$CODEX_CONFIG" <<TOML

[mcp_servers.bothub]
command = "python"
args = ["-m", "bothub.mcp_server"]
env = { BOTHUB_URL = "${BOTHUB_URL:-}", BOTHUB_TOKEN = "${BOTHUB_TOKEN:-}" }
TOML
fi

# PID 1 must be the only uid 1000 process during freeze_bot. -I: isolated mode, no cwd or script directory in
# sys.path, PYTHON* variables and the user site are ignored. The filter is already installed (ENTRYPOINT runs this
# script through bot-guard); the interpreter goes through it once more by absolute paths: PATH and cwd are
# bot-writable, and a script started without the image ENTRYPOINT must not run unfiltered.
# If the filter cannot be installed bot-guard exits non-zero and the container does not become ready.
exec /usr/local/libexec/bot-guard /usr/bin/python3 -I - <<'PY'
import ctypes
import os
import signal

# Protect PID 1 before it reports readiness or can supervise bot commands.
libc = ctypes.CDLL(None, use_errno=True)
if libc.prctl(4, 0, 0, 0, 0) != 0:  # PR_SET_DUMPABLE = 4
    error = ctypes.get_errno()
    raise OSError(error, os.strerror(error), "prctl(PR_SET_DUMPABLE, 0)")


def reap(_signum, _frame):
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def stop(_signum, _frame):
    reap(0, None)
    raise SystemExit(0)


signal.signal(signal.SIGCHLD, reap)
signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
print("Bot image ready.", flush=True)
while True:
    signal.pause()
PY

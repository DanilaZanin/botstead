"""Сборка argv для docker. Только чистые функции: всё, что зависит от вызывающего, проходит validation,
всё остальное берётся из Config. Секреты (токен бота, значения env exec) в argv не попадают: в `-e NAME`
остаётся имя, значение docker CLI берёт из окружения своего процесса."""
import hashlib
import hmac
import os
from dataclasses import dataclass, field

from .config import Config
from .errors import ValidationFailed
from .validation import validate_argv, validate_bot_id, validate_exec_id, validate_owner_id

BRIDGE_OPT = "com.docker.network.bridge.name"
KILL_SIGNALS = ("TERM", "KILL")
PID_FILE = "/tmp/bothub-exec-$id.pid"

# Второй seccomp-фильтр (bot-image/bot-guard.c) ставится префиксом перед каждой командой под uid 1000. Пути
# абсолютные: PATH и cwd в контейнере пишет код бота. Браузерный стек (uid 1001) идёт без загрузчика.
BOT_GUARD = "/usr/local/libexec/bot-guard"
BASH = "/usr/bin/bash"
SETSID = "/usr/bin/setsid"
BROWSER_DOWNLOADS = "/home/browser/Downloads"
BROWSER_VNC_SOCKET = "/home/browser/.vnc/rfb.sock"  # создаёт chromium-supervisor.sh, права 0600 в каталоге 0700
# Режим браузера (bot или human) и адрес для человека пишет лаунчер, читает chromium-supervisor.sh; файлы в томе
# /home/browser (uid 1001, 0700), поэтому режим переживает рестарт контейнера. `.browser-active` и
# `.browser-merge-status` пишет супервизор.
BROWSER_MODE_FILE = "$HOME/.browser-mode"
BROWSER_URL_FILE = "$HOME/.browser-human-url"
BROWSER_ACTIVE_FILE = "$HOME/.browser-active"
BROWSER_MERGE_STATUS_FILE = "$HOME/.browser-merge-status"
BROWSER_ENV = ["-e", "HOME=/home/browser"]
# Исполнитель шагов процедур (bot-image/procedure-step.mjs): Node под uid 1001 подключается к CDP на 127.0.0.1:9222.
# Файл root:root 0644 в rootfs, подменить его нельзя. Данные шага (значения параметров и секретов) идут только в stdin.
NODE = "/usr/bin/node"
ENV_BIN = "/usr/bin/env"
PROCEDURE_STEP_SCRIPT = "/usr/local/libexec/procedure-step.mjs"
# PID-файл по абсолютному пути: у исполнителя нет HOME (см. procedure_step_args). Каталог принадлежит uid 1001.
PROCEDURE_PID_FILE = "/home/browser/.procedure-step.pid"
PROCEDURE_MARKER = "bothub-procedure-"
# Окружение исполнителя: ни HOME с .node_modules, ни NODE_PATH, ни NODE_OPTIONS, ни cwd не влияют на загрузку модулей.
PROCEDURE_HOME = "/nonexistent"
PROCEDURE_PATH = "/usr/bin:/bin"


@dataclass(frozen=True)
class RunSpec:
    argv: list[str]
    env: dict[str, str] = field(default_factory=dict, repr=False)


def bridge_name(owner_id: str) -> str:
    """Имя bridge-интерфейса, до 15 символов: по нему iptables отличает сети пользователей."""
    return "bhu" + hashlib.sha256(validate_owner_id(owner_id).encode()).hexdigest()[:10]


def bot_token(secret: str, bot_id: str) -> str:
    """Тот же формат, что проверяет ядро (main.py, principal) и считал deploy/bot-create.sh."""
    return f"bot:{bot_id}:{hmac.new(secret.encode(), bot_id.encode(), hashlib.sha256).hexdigest()}"


def label_args(cfg: Config, role: str, *, bot_id: str | None = None, owner_id: str | None = None) -> list[str]:
    labels = [f"{cfg.label_key}={cfg.label_value}", f"bothub.role={role}"]
    if bot_id:
        labels.append(f"bothub.bot_id={bot_id}")
    if owner_id:
        labels.append(f"bothub.owner_id={owner_id}")
    out: list[str] = []
    for label in labels:
        out += ["--label", label]
    return out


def guarded(argv: list[str]) -> list[str]:
    """Команда под uid 1000: bot-guard первым, затем исполняемый файл по абсолютному пути. Префикс задаёт только
    лаунчер: argv от клиента попадает в хвост (после `bash -c ... _ id`) и на него не влияет."""
    if not argv or not argv[0].startswith("/") or "\x00" in argv[0]:
        raise ValidationFailed("команда под uid 1000 запускается по абсолютному пути")
    return [BOT_GUARD, *argv]


def _hardening(cfg: Config, *, init: bool = True, seccomp: bool = False) -> list[str]:
    """Общие флаги безопасности и лимиты для контейнеров бота и логина.

    seccomp=True: профиль из конфигурации (только контейнеры ботов; логины остаются на стандартном профиле Docker,
    в них браузера нет)."""
    lim = cfg.limits
    args = [
        "--pull", "never",
        "--user", cfg.user,
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--memory", lim.memory, "--memory-swap", lim.memory,
        "--cpus", lim.cpus,
        "--pids-limit", str(lim.pids),
        "--read-only",
        "--tmpfs", f"/tmp:rw,nosuid,nodev,size={lim.tmp_size}",
        "--tmpfs", f"/run:rw,nosuid,nodev,uid=1000,gid=1000,mode=0755,size={lim.run_size}",
        "--sysctl", "net.ipv6.conf.all.disable_ipv6=1",
        "--ulimit", "nofile=1024:1024",
        "--shm-size", lim.shm_size,
        "--log-driver", "json-file", "--log-opt", "max-size=10m", "--log-opt", "max-file=3",
    ]
    if init:
        args.insert(0, "--init")
    if seccomp and cfg.seccomp_profile:
        # Docker CLI читает файл сам, поэтому путь нужен абсолютный (cwd лаунчера не гарантирован).
        args += ["--security-opt", f"seccomp={os.path.abspath(cfg.seccomp_profile)}"]
    for server in ((cfg.internal_dns,) if cfg.internal_dns else cfg.dns):
        args += ["--dns", server]
    if cfg.runtime:
        args += ["--runtime", cfg.runtime]
    return args


def bot_run_args(cfg: Config, bot_id: str, owner_id: str, *, home_volume: str | None = None,
                 login_volume: str | None = None, browser_volume: str | None = None) -> RunSpec:
    bot_id, owner_id = validate_bot_id(bot_id), validate_owner_id(owner_id)
    if not cfg.bot_token_secret:
        raise ValidationFailed("BOT_TOKEN_SECRET не задан: токен бота не из чего посчитать")
    name = cfg.bot_container(bot_id)
    argv = [
        cfg.docker_bin, "run", "-d",
        "--name", name, "--hostname", name,
        "--restart", "no",
        *label_args(cfg, "bot", bot_id=bot_id, owner_id=owner_id),
        "--label", "bothub.browser_isolation=2",
        "--network", cfg.network_name(owner_id),
        *_hardening(cfg, init=False, seccomp=True),
        "-e", f"BOTHUB_URL={cfg.core_url}",
        "-e", "BOTHUB_TOKEN",
        "-v", f"{home_volume or cfg.home_volume(bot_id)}:{cfg.home}",
        "-v", f"{login_volume or cfg.login_volume(owner_id)}:{cfg.home}/.auth:ro",
        "-v", f"{browser_volume or cfg.browser_volume(bot_id)}:/home/browser",
        # Каталог загрузок Chromium (политика DefaultDownloadDirectory): noexec, uid 1001, пропадает с контейнером.
        # Загрузки при CDP политикой НЕ закрыты: Browser.setDownloadBehavior обходит DownloadRestrictions=3, и Chromium
        # бота пишет в любое место, доступное uid 1001, в том числе в /home/browser (профиль, маркер, каталог человека).
        # Защита от этого не каталог, а проверки супервизора (токен сессии, чистый старт человека, слияние только cookies).
        # Политика закрывает загрузки лишь человеку и обычной вкладке; каталог страхует на случай её ослабления.
        "--tmpfs", f"{BROWSER_DOWNLOADS}:rw,noexec,nosuid,nodev,uid=1001,gid=1001,mode=0700,size=64m",
        cfg.image,
    ]
    return RunSpec(argv, {"BOTHUB_TOKEN": bot_token(cfg.bot_token_secret, bot_id)})


def login_run_args(cfg: Config, owner_id: str, *, login_volume: str | None = None) -> RunSpec:
    owner_id = validate_owner_id(owner_id)
    name = cfg.login_container(owner_id)
    argv = [
        cfg.docker_bin, "run", "-d",
        "--name", name, "--hostname", name,
        "--restart", "no",
        *label_args(cfg, "login", owner_id=owner_id),
        "--network", cfg.network_name(owner_id),
        *_hardening(cfg),
        "-v", f"{login_volume or cfg.login_volume(owner_id)}:{cfg.home}",
        "--entrypoint", "sleep",
        cfg.image, str(cfg.login_ttl),
    ]
    return RunSpec(argv)


def network_create_args(cfg: Config, owner_id: str) -> list[str]:
    owner_id = validate_owner_id(owner_id)
    return [
        cfg.docker_bin, "network", "create", "--driver", "bridge", "--ipv6=false",
        *label_args(cfg, "network", owner_id=owner_id),
        "--opt", f"{BRIDGE_OPT}={bridge_name(owner_id)}",
        cfg.network_name(owner_id),
    ]


def volume_create_args(cfg: Config, name: str, owner_id: str, kind: str) -> list[str]:
    owner_id = validate_owner_id(owner_id)
    return [cfg.docker_bin, "volume", "create",
            *label_args(cfg, kind, owner_id=owner_id), name]


def marker_wrap(argv: list[str], exec_id: str) -> list[str]:
    """Запуск в контейнере так, чтобы stop_exec мог найти и убить всю группу процессов.

    `setsid -w` делает процесс лидером группы и ждёт его (иначе поток вывода оборвётся), bash пишет PID в
    /tmp/bothub-exec-<id>.pid и делает exec: PID сохраняется, `kill -- -<pid>` гасит CLI вместе с дочерними.
    `exec -a` кладёт маркер в argv[0] для запасного pkill -f. Нужен именно bash: в dash нет `exec -a`.
    Фильтр bot-guard ставится выше (перед setsid), поэтому argv клиента остаётся именем из PATH, а маркер цел."""
    script = f'id=$1; shift; echo $$ > "{PID_FILE}"; exec -a "bothub-exec-$id" "$@"'
    return [SETSID, "-w", BASH, "-c", script, "_", validate_exec_id(exec_id), *argv]


def exec_args(cfg: Config, container: str, argv: list[str], env_names: list[str], exec_id: str) -> list[str]:
    argv = validate_argv(argv)
    args = [cfg.docker_bin, "exec", "-i", "--user", cfg.user, "--workdir", cfg.home]
    for name in env_names:
        args += ["-e", name]
    return [*args, container, *guarded(marker_wrap(argv, exec_id))]


def pty_exec_args(cfg: Config, container: str, argv: list[str]) -> list[str]:
    # bash -c 'exec "$@"': bot-guard требует абсолютный путь, а команды входа (claude, codex, bash -l) заданы именами.
    return [cfg.docker_bin, "exec", "-it", "--user", cfg.user, "--workdir", cfg.home,
            container, *guarded([BASH, "-c", 'exec "$@"', "_", *validate_argv(argv)])]


def bot_script_args(cfg: Config, container: str, script: str, *args: str) -> list[str]:
    """Служебный скрипт лаунчера (заморозка, проверка процессов) под uid бота: тоже через bot-guard."""
    tail = ["_", *args] if args else []  # "_" занимает $0, чтобы аргументы начинались с $1
    return [cfg.docker_bin, "exec", "--user", cfg.user, container, *guarded([BASH, "-c", script, *tail])]


def kill_args(cfg: Config, container: str, exec_id: str, signal_name: str) -> list[str]:
    if signal_name not in KILL_SIGNALS:
        raise ValidationFailed("сигнал: TERM или KILL")
    script = ('id=$1; sig=$2; p=$(cat "' + PID_FILE + '" 2>/dev/null); '
              '[ -n "$p" ] && kill -"$sig" -- -"$p" 2>/dev/null; '
              'pkill -"$sig" -f "bothub-exec-$id" 2>/dev/null; true')
    return bot_script_args(cfg, container, script, validate_exec_id(exec_id), signal_name)


def procedure_step_args(cfg: Config, container: str, exec_id: str) -> list[str]:
    """Исполнитель шага процедуры под uid 1001, без bot-guard (как остальной браузерный стек). В argv и окружении только
    константы и exec_id: payload (в нём бывают значения секретов) идёт через stdin и в список процессов не попадает.
    Окружение чистое: `env -i` с HOME=/nonexistent и коротким PATH (ни NODE_PATH, ни NODE_OPTIONS из образа или контейнера), cwd /.
    Модуль playwright-core скрипт берёт по абсолютному пути из образа, поэтому ни `$HOME/.node_modules`, ни рабочий каталог
    (оба доступны браузеру под тем же uid) подменить его не могут. `setsid -w` делает процесс лидером группы, PID пишется в
    PROCEDURE_PID_FILE для `procedure_kill_args` (`exec` сохраняет PID через env и node); метка с exec_id идёт последним
    аргументом скрипта (его он игнорирует) для запасного `pkill -f`."""
    script = f'id=$1; shift; echo $$ > "{PROCEDURE_PID_FILE}"; exec "$@" "{PROCEDURE_MARKER}$id"'
    return [cfg.docker_bin, "exec", "-i", "--user", "1001:1001", "-e", f"HOME={PROCEDURE_HOME}", "--workdir", "/", container,
            SETSID, "-w", BASH, "-c", script, "_", validate_exec_id(exec_id),
            ENV_BIN, "-i", f"HOME={PROCEDURE_HOME}", f"PATH={PROCEDURE_PATH}", NODE, "--no-warnings", PROCEDURE_STEP_SCRIPT]


def procedure_kill_args(cfg: Config, container: str, exec_id: str, signal_name: str) -> list[str]:
    """Сигнал группе исполнителя шага: процесс принадлежит uid 1001, поэтому `kill_args` (uid 1000) до него не дотянется."""
    if signal_name not in KILL_SIGNALS:
        raise ValidationFailed("сигнал: TERM или KILL")
    script = ('sig=$1; id=$2; p=$(cat "' + PROCEDURE_PID_FILE + '" 2>/dev/null); '
              'case "$p" in ""|*[!0-9]*) p=;; esac; '
              '[ -n "$p" ] && [ "$p" -gt 1 ] && kill -"$sig" -- -"$p" 2>/dev/null; '
              'pkill -"$sig" -f "' + PROCEDURE_MARKER + '$id" 2>/dev/null; true')
    return [cfg.docker_bin, "exec", "--user", "1001:1001", "-e", f"HOME={PROCEDURE_HOME}", "--workdir", "/", container,
            "sh", "-c", script, "_", signal_name, validate_exec_id(exec_id)]

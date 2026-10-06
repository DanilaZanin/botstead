"""Конфигурация лаунчера. Образ, mounts, сеть и лимиты живут здесь и не приходят из API.

Источники: необязательный TOML-файл (LAUNCHER_CONFIG) и переменные окружения для секретов и путей.
Любая опечатка в ключе или недопустимое значение останавливают запуск."""
import ipaddress
import json
import math
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path

SECRET_MIN = 32

DEFAULT_BLOCKED_V4 = (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
    "172.16.0.0/12", "192.168.0.0/16", "224.0.0.0/4", "240.0.0.0/4",
)
DEFAULT_BLOCKED_V6 = ("::1/128", "fc00::/7", "fe80::/10", "ff00::/8")

DEFAULT_LOGIN_COMMANDS = {
    "shell": ("bash", "-l"),
    "claude": ("claude", "auth", "login"),
    "codex": ("codex", "login", "--device-auth"),
    # Голый agy открывает TUI на альтернативном экране; headless-промпт без входа печатает ссылку и ждёт код,
    # после входа выполняет промпт и выходит с 0 (docs/contracts.md §12).
    "agy": ("agy", "--mode", "plan", "-p", "Reply OK"),
    "claude_status": ("claude", "auth", "status"),
    "codex_status": ("codex", "login", "status"),
    "agy_status": ("agy", "--mode", "plan", "--output-format", "json", "-p", "Reply OK"),
}

NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}")
IMAGE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/@-]{0,200}")
LABEL_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,62}")
SIZE_RE = re.compile(r"[1-9][0-9]*[kmgKMG]?")
CPUS_RE = re.compile(r"[0-9]+(\.[0-9]+)?")
USER_RE = re.compile(r"[0-9]+:[0-9]+")
RUNTIME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,31}")
PATH_RE = re.compile(r"/[A-Za-z0-9_./-]{0,200}")
SECCOMP_ACTION_PREFIX = "SCMP_ACT_"


class ConfigError(ValueError):
    pass


def _check(pattern: re.Pattern, value, what: str) -> None:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ConfigError(f"{what}: недопустимое значение {value!r}")


@dataclass(frozen=True)
class AllowRule:
    """Исключение из запрета: адрес (и необязательно протокол/порт), доступный ботам."""
    cidr: str
    proto: str = "tcp"
    port: int | None = None
    comment: str = ""

    def __post_init__(self):
        try:
            net = ipaddress.ip_network(self.cidr, strict=True)
        except ValueError as exc:
            raise ConfigError(f"allow.cidr: {exc}") from exc
        object.__setattr__(self, "cidr", str(net))
        if self.proto not in ("tcp", "udp", "any"):
            raise ConfigError("allow.proto: tcp, udp или any")
        if self.port is not None and (isinstance(self.port, bool) or not isinstance(self.port, int)
                                      or not 1 <= self.port <= 65535):
            raise ConfigError("allow.port: 1..65535")
        if self.port is not None and self.proto == "any":
            raise ConfigError("allow.port требует proto tcp или udp")

    @property
    def version(self) -> int:
        return ipaddress.ip_network(self.cidr).version


@dataclass(frozen=True)
class Limits:
    memory: str = "2g"
    cpus: str = "2"
    pids: int = 512
    shm_size: str = "512m"
    tmp_size: str = "512m"
    run_size: str = "64m"

    def __post_init__(self):
        for name in ("memory", "shm_size", "tmp_size", "run_size"):
            _check(SIZE_RE, getattr(self, name), f"limits.{name}")
        _check(CPUS_RE, self.cpus, "limits.cpus")
        if float(self.cpus) <= 0:
            raise ConfigError("limits.cpus: больше нуля")
        if isinstance(self.pids, bool) or not isinstance(self.pids, int) or self.pids < 1:
            raise ConfigError("limits.pids: целое больше нуля")


def _cidrs(values, version: int, what: str) -> tuple[str, ...]:
    out = []
    for v in values:
        try:
            net = ipaddress.ip_network(v, strict=True)
        except ValueError as exc:
            raise ConfigError(f"{what}: {exc}") from exc
        if net.version != version:
            raise ConfigError(f"{what}: {v} не IPv{version}")
        out.append(str(net))
    return tuple(out)


@dataclass(frozen=True)
class Config:
    secret: str = field(repr=False)
    bot_token_secret: str = field(default="", repr=False)
    image: str = "bothub-bot"
    label_key: str = "bothub.managed"
    label_value: str = "1"
    runtime: str | None = None
    # Файл seccomp-профиля контейнеров ботов (раздел [seccomp] profile). Docker CLI читает его у себя, то есть
    # внутри контейнера лаунчера. None: стандартный профиль Docker (Chromium тогда не стартует).
    seccomp_profile: str | None = None
    user: str = "1000:1000"
    home: str = "/home/bot"
    limits: Limits = field(default_factory=Limits)
    dns: tuple[str, ...] = ("1.1.1.1", "8.8.8.8")
    internal_dns: str | None = None
    core_container: str = "bothub-core"
    core_alias: str = "core"
    core_url: str = "http://core:8080"
    # Приоритет шлюза сети пользователя при `docker network connect` ядра (Docker 28+, --gw-priority). Основная сеть
    # ядра в compose имеет gw_priority 100, поэтому отрицательное значение не даёт сети пользователя стать шлюзом
    # по умолчанию: иначе ответы на опубликованный порт уходят через чужой bridge.
    core_gw_priority: int = -100
    api_port: int = 8080
    blocked_v4: tuple[str, ...] = DEFAULT_BLOCKED_V4
    blocked_v6: tuple[str, ...] = DEFAULT_BLOCKED_V6
    allow: tuple[AllowRule, ...] = ()
    host_allow: tuple[AllowRule, ...] = ()
    adopt_legacy_volumes: bool = False
    container_prefix: str = "bot-"
    login_prefix: str = "login-"
    network_prefix: str = "bothub-u-"
    login_volume_prefix: str = "bothub-login-"
    login_commands: Mapping[str, tuple[str, ...]] = field(default_factory=lambda: dict(DEFAULT_LOGIN_COMMANDS))
    login_ttl: int = 3600
    login_idle: int = 900
    max_screen_sessions: int = 32
    screen_idle: int = 900
    screen_connect_timeout: float = 5.0
    max_execs_per_bot: int = 8
    max_execs_total: int = 64
    exec_timeout: float = 1800
    exec_timeout_max: float = 7200
    kill_grace: float = 5.0
    heartbeat: float = 15.0
    reconcile_interval: float = 30.0
    socket_path: str = "/run/bothub-launcher/launcher.sock"
    docker_bin: str = "docker"
    iptables_bin: str = "iptables"
    ip6tables_bin: str = "ip6tables"

    def __post_init__(self):
        if not isinstance(self.secret, str) or len(self.secret) < SECRET_MIN:
            raise ConfigError(f"LAUNCHER_SECRET: нужно не меньше {SECRET_MIN} символов")
        _check(IMAGE_RE, self.image, "image")
        _check(LABEL_RE, self.label_key, "label_key")
        _check(LABEL_RE, self.label_value, "label_value")
        if self.runtime is not None:
            _check(RUNTIME_RE, self.runtime, "runtime")
        if self.seccomp_profile is not None:
            _check(PATH_RE, self.seccomp_profile, "seccomp.profile")
        # Бот не должен быть root: ни uid 0, ни gid 0, ни имени.
        _check(USER_RE, self.user, "user")
        if any(int(part) == 0 for part in self.user.split(":")):
            raise ConfigError("user: root запрещён")
        _check(PATH_RE, self.home, "home")
        for name in ("core_container", "core_alias"):
            _check(NAME_RE, getattr(self, name), name)
        for name in ("container_prefix", "login_prefix", "network_prefix", "login_volume_prefix"):
            _check(NAME_RE, getattr(self, name), name)
        for d in self.dns:
            try:
                ipaddress.ip_address(d)
            except ValueError as exc:
                raise ConfigError(f"dns: {exc}") from exc
        if self.internal_dns is not None:
            try:
                if ipaddress.ip_address(self.internal_dns).version != 4:
                    raise ConfigError("internal_dns: нужен IPv4 адрес")
            except ValueError as exc:
                raise ConfigError(f"internal_dns: {exc}") from exc
        if isinstance(self.api_port, bool) or not isinstance(self.api_port, int) or not 1 <= self.api_port <= 65535:
            raise ConfigError("api_port: 1..65535")
        if isinstance(self.core_gw_priority, bool) or not isinstance(self.core_gw_priority, int):
            raise ConfigError("core_gw_priority: целое число")
        if self.core_gw_priority >= 100:  # 100 у основной сети ядра в compose: сеть пользователя должна проигрывать
            raise ConfigError("core_gw_priority: меньше 100")
        if isinstance(self.max_screen_sessions, bool) or not isinstance(self.max_screen_sessions, int) \
                or self.max_screen_sessions < 1:
            raise ConfigError("max_screen_sessions: целое больше нуля")
        if isinstance(self.screen_idle, bool) or not isinstance(self.screen_idle, int) or self.screen_idle < 1:
            raise ConfigError("screen_idle: целое больше нуля")
        if isinstance(self.screen_connect_timeout, bool) or not isinstance(self.screen_connect_timeout, (int, float)) \
                or not math.isfinite(self.screen_connect_timeout) or self.screen_connect_timeout <= 0:
            raise ConfigError("screen_connect_timeout: больше нуля")
        object.__setattr__(self, "blocked_v4", _cidrs(self.blocked_v4, 4, "blocked_v4"))
        object.__setattr__(self, "blocked_v6", _cidrs(self.blocked_v6, 6, "blocked_v6"))
        for cmd, argv in self.login_commands.items():
            _check(re.compile(r"[a-z][a-z0-9_-]{0,31}"), cmd, "login_commands")
            if not argv or any(not isinstance(a, str) or "\x00" in a for a in argv) or argv[0].startswith("-"):
                raise ConfigError(f"login_commands.{cmd}: нужна команда")

    # имена объектов Docker: единственное место, где они собираются
    def bot_container(self, bot_id: str) -> str:
        return f"{self.container_prefix}{bot_id}"

    def home_volume(self, bot_id: str) -> str:
        return f"{self.container_prefix}{bot_id}-home"

    def browser_volume(self, bot_id: str) -> str:
        return f"{self.container_prefix}{bot_id}-browser"

    def network_name(self, owner_id: str) -> str:
        return f"{self.network_prefix}{owner_id}"

    def login_container(self, owner_id: str) -> str:
        return f"{self.login_prefix}{owner_id}"

    def login_volume(self, owner_id: str) -> str:
        return f"{self.login_volume_prefix}{owner_id}"

    def container_name(self, role: str, key: str) -> str:
        return self.bot_container(key) if role == "bot" else self.login_container(key)


_TOP_SCALARS = {
    "image", "label_key", "label_value", "runtime", "user", "home", "core_container", "core_alias", "core_url",
    "api_port", "core_gw_priority", "adopt_legacy_volumes", "container_prefix", "login_prefix", "network_prefix",
    "login_volume_prefix", "login_ttl", "login_idle", "max_screen_sessions", "screen_idle",
    "screen_connect_timeout", "max_execs_per_bot", "max_execs_total", "exec_timeout",
    "exec_timeout_max", "kill_grace", "heartbeat", "reconcile_interval", "socket_path", "docker_bin",
    "iptables_bin", "ip6tables_bin", "internal_dns",
}
_TOP_KEYS = _TOP_SCALARS | {"dns", "blocked_v4", "blocked_v6", "allow", "host_allow", "limits", "login_commands",
                            "seccomp"}


def check_seccomp_profile(path: str) -> None:
    """Профиль должен существовать и разбираться как JSON с defaultAction: иначе Docker запустил бы бота на другом
    профиле или отказал позже и по-другому. Лаунчер не стартует, а не продолжает без профиля."""
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"seccomp.profile: файл не найден: {path}")
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError) as exc:  # UnicodeDecodeError и JSONDecodeError тоже ValueError
        raise ConfigError(f"seccomp.profile {path}: {exc}") from exc
    action = data.get("defaultAction") if isinstance(data, dict) else None
    if not isinstance(action, str) or not action.startswith(SECCOMP_ACTION_PREFIX):
        raise ConfigError(f"seccomp.profile {path}: нет defaultAction (SCMP_ACT_*)")


def _rules(items, what: str) -> tuple[AllowRule, ...]:
    out = []
    for item in items:
        unknown = set(item) - {"cidr", "proto", "port", "comment"}
        if unknown:
            raise ConfigError(f"{what}: неизвестные ключи {sorted(unknown)}")
        if "cidr" not in item:
            raise ConfigError(f"{what}: нужен cidr")
        out.append(AllowRule(**item))
    return tuple(out)


def load_config(path: str | None = None, env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    path = path or env.get("LAUNCHER_CONFIG") or None
    data: dict = {}
    if path:
        p = Path(path)
        if not p.is_file():
            raise ConfigError(f"файл конфигурации не найден: {path}")
        try:
            data = tomllib.loads(p.read_text())
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{path}: {exc}") from exc
    unknown = set(data) - _TOP_KEYS
    if unknown:
        raise ConfigError(f"неизвестные ключи конфигурации: {sorted(unknown)}")

    kwargs: dict = {k: data[k] for k in _TOP_SCALARS if k in data}
    for key in ("dns", "blocked_v4", "blocked_v6"):
        if key in data:
            kwargs[key] = tuple(data[key])
    if "limits" in data:
        lim = data["limits"]
        bad = set(lim) - {f.name for f in fields(Limits)}
        if bad:
            raise ConfigError(f"limits: неизвестные ключи {sorted(bad)}")
        kwargs["limits"] = Limits(**lim)
    if "seccomp" in data:
        section = data["seccomp"]
        if not isinstance(section, dict) or set(section) - {"profile"}:
            raise ConfigError("seccomp: допустим только ключ profile")
        if "profile" in section:
            _check(PATH_RE, section["profile"], "seccomp.profile")
            check_seccomp_profile(section["profile"])
            kwargs["seccomp_profile"] = section["profile"]
    if "allow" in data:
        kwargs["allow"] = _rules(data["allow"], "allow")
    if "host_allow" in data:
        kwargs["host_allow"] = _rules(data["host_allow"], "host_allow")
    if "login_commands" in data:
        kwargs["login_commands"] = {k: tuple(v) for k, v in data["login_commands"].items()}

    if env.get("LAUNCHER_SOCKET"):
        kwargs["socket_path"] = env["LAUNCHER_SOCKET"]
    if env.get("LAUNCHER_RUNTIME"):
        kwargs["runtime"] = env["LAUNCHER_RUNTIME"]
    bot_secret = env.get("BOT_TOKEN_SECRET", "")
    if not bot_secret:
        raise ConfigError("BOT_TOKEN_SECRET: задайте тот же секрет, что у ядра (токены ботов)")
    try:
        return Config(secret=env.get("LAUNCHER_SECRET", ""), bot_token_secret=bot_secret, **kwargs)
    except TypeError as exc:
        raise ConfigError(str(exc)) from exc

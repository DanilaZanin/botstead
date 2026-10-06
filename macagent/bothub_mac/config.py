"""Конфигурация: переменные окружения, раздел 7 контракта, с запасным .env-файлом
вне репозитория (секреты никогда не коммитятся)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

DEFAULT_ENV_FILE = Path.home() / "Library" / "Application Support" / "BotHubMac" / "config.env"


class ConfigError(RuntimeError):
    pass


def _parse_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _http_to_ws(url: str) -> str:
    parts = urlsplit(url)
    scheme = {"http": "ws", "https": "wss"}.get(parts.scheme, parts.scheme)
    return urlunsplit((scheme, parts.netloc, parts.path.rstrip("/") + "/agent/mac", "", ""))


def _split_urls(raw: str) -> tuple[str, ...]:
    return tuple(u.strip().rstrip("/") for u in raw.split(",") if u.strip())


class CurrentUrl:
    """Адрес ядра, к которому агент подключён прямо сейчас — обновляет client.py
    при (пере)подключении, читает upload_file (tools/http.py), чтобы слать файлы
    туда же, куда ходит WS, а не всегда на основной адрес из списка."""

    def __init__(self, url: str = "") -> None:
        self.url = url


@dataclass(frozen=True)
class Config:
    bothub_url: str
    mac_agent_token: str
    env_file: Path = DEFAULT_ENV_FILE
    bothub_urls: tuple[str, ...] = ()
    current: CurrentUrl = field(default_factory=CurrentUrl)

    @property
    def urls(self) -> tuple[str, ...]:
        """Адреса ядра по приоритету: BOTHUB_URLS, а если не задан — BOTHUB_URL как список из одного."""
        return self.bothub_urls or (self.bothub_url,)

    def ws_url_for(self, url: str) -> str:
        return f"{_http_to_ws(url)}?token={self.mac_agent_token}"

    @property
    def ws_url(self) -> str:
        return self.ws_url_for(self.bothub_url)

    @classmethod
    def load(cls, env: dict[str, str] | None = None, env_file: Path = DEFAULT_ENV_FILE) -> "Config":
        env = dict(env if env is not None else os.environ)
        file_values = _parse_env_file(env_file)
        urls_raw = env.get("BOTHUB_URLS") or file_values.get("BOTHUB_URLS")
        if urls_raw:
            urls = _split_urls(urls_raw)
        else:
            bothub_url = env.get("BOTHUB_URL") or file_values.get("BOTHUB_URL")
            if not bothub_url:
                raise ConfigError(f"BOTHUB_URL/BOTHUB_URLS не заданы ни в окружении, ни в {env_file}")
            urls = (bothub_url.rstrip("/"),)
        if not urls:
            raise ConfigError(f"BOTHUB_URLS пуст в окружении и в {env_file}")
        token = env.get("MAC_AGENT_TOKEN") or file_values.get("MAC_AGENT_TOKEN")
        if not token:
            raise ConfigError(f"MAC_AGENT_TOKEN не задан ни в окружении, ни в {env_file}")
        return cls(
            bothub_url=urls[0], mac_agent_token=token, env_file=env_file,
            bothub_urls=urls, current=CurrentUrl(urls[0]),
        )

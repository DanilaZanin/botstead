"""Запуск: `python -m bothub_launcher`. Слушает unix-сокет (по умолчанию) или внутренний TCP-адрес.

LAUNCHER_LISTEN: `unix:/путь/к/сокету` или `tcp://127.0.0.1:8099`. Без него берётся socket_path из конфига."""
import logging
import os
import sys
from pathlib import Path

import uvicorn

from .app import create_app
from .config import ConfigError, load_config


def parse_listen(value: str | None, default_socket: str) -> dict:
    """Аргументы uvicorn.run для адреса прослушивания."""
    if not value:
        return {"uds": default_socket}
    if value.startswith("unix:") and len(value) > 5:
        return {"uds": value[5:]}
    if value.startswith("tcp://") and ":" in value[6:]:
        host, _, port = value[6:].rpartition(":")
        if host and port.isdigit() and 0 < int(port) < 65536:
            return {"host": host, "port": int(port)}
    raise ConfigError(f"LAUNCHER_LISTEN: ожидается unix:/path или tcp://host:port, получено {value!r}")


def main() -> int:
    logging.basicConfig(level=os.environ.get("LAUNCHER_LOG", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        cfg = load_config()
        listen = parse_listen(os.environ.get("LAUNCHER_LISTEN"), cfg.socket_path)
    except ConfigError as exc:
        print(f"bothub-launcher: конфигурация: {exc}", file=sys.stderr)
        return 2
    if "uds" in listen:
        sock = Path(listen["uds"])
        sock.parent.mkdir(parents=True, exist_ok=True)
        sock.unlink(missing_ok=True)
        os.umask(0o117)  # сокет 0660: доступ только владельцу и группе
    uvicorn.run(create_app(cfg), log_level="info", access_log=False, **listen)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

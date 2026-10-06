from __future__ import annotations

import asyncio
import logging
import sys

from .client import run_forever
from .config import Config, ConfigError


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = Config.load()
    except ConfigError as e:
        logging.getLogger("bothub_mac").error(str(e))
        return 1
    try:
        asyncio.run(run_forever(config))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

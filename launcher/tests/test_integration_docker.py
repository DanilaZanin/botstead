"""Интеграционные проверки на настоящем Docker. По умолчанию пропускаются: `pytest -m docker`.

Нужны: Linux, docker daemon, образ `bothub-bot`, права на iptables (запуск от root или с NET_ADMIN) и
запущенный контейнер ядра `bothub-core` (сеть пользователя подключает его). Полная проверка изоляции:
`deploy/tests/isolation_check.sh`.
"""
import dataclasses
import uuid

import pytest

from bothub_launcher.backend import DockerCLIBackend
from bothub_launcher.config import Config
from bothub_launcher.netpolicy import NetPolicy
from bothub_launcher.procs import SubprocessRunner
from bothub_launcher.service import Launcher

pytestmark = pytest.mark.docker


@pytest.fixture
def real():
    cfg = Config(secret="i" * 40, bot_token_secret="integration", kill_grace=0.5)
    cfg = dataclasses.replace(cfg, container_prefix="it-bot-", login_prefix="it-login-", network_prefix="it-u-",
                              login_volume_prefix="it-login-")
    runner = SubprocessRunner()
    return Launcher(cfg, DockerCLIBackend(cfg, runner), NetPolicy(cfg, runner))


async def test_create_exec_recreate_remove(real):
    owner = "it" + uuid.uuid4().hex[:8]
    bot = "b" + uuid.uuid4().hex[:8]
    await real.startup()
    try:
        st = await real.create_bot(bot, owner)
        assert st["running"]
        handle = await real.start_exec(bot, ["id", "-u"])
        frames = [f async for f in real.stream_exec(handle)]
        assert frames[-1]["code"] == 0
        await real.recreate_bot(bot)
        assert (await real.status(bot))["running"]
    finally:
        await real.remove_bot(bot, purge=True)

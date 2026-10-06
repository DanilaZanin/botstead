"""Return from human on the launcher side: the freeze marker of a stopped container, the total time budget of a mode
switch, and the address a human may be given."""
import dataclasses

import pytest

from bothub_launcher import service
from bothub_launcher import validation as v
from bothub_launcher.errors import BackendError, Conflict, Frozen, ValidationFailed
from bothub_launcher.service import Launcher
from bothub_launcher.testing import FakeBackend, FakeNetPolicy


@pytest.fixture
def backend():
    return FakeBackend()


@pytest.fixture
def launcher(cfg, backend, tmp_path):
    cfg = dataclasses.replace(cfg, socket_path=str(tmp_path / "launcher.sock"))
    return Launcher(cfg, backend, FakeNetPolicy(order=backend.order))


def marker(launcher, bot_id="scout"):
    return launcher._frozen_file(bot_id)


def stop_container(backend, name="bot-scout"):
    backend.containers[name] = dataclasses.replace(backend.containers[name], running=False, status="exited")


# ---------- frozen marker and a stopped container (regression of the core start) ----------

async def test_stopped_container_cannot_switch_mode_and_keeps_the_marker(launcher, backend):
    await launcher.create_bot("scout", "o1")
    await launcher.freeze_bot("scout")
    await launcher.browser_mode("scout", "human", "https://example.com/")
    stop_container(backend)
    with pytest.raises(Conflict, match="не запущен"):
        await launcher.browser_mode("scout", "bot")
    assert marker(launcher).exists(), "the failed switch must not thaw the bot by itself"


async def test_unfreeze_of_a_stopped_container_is_allowed_and_a_recreated_container_runs_commands(launcher, backend):
    """The core thaws a bot whatever browser_mode(bot) answered: the stopped container of the probe, then recreate."""
    await launcher.create_bot("scout", "o1")
    await launcher.freeze_bot("scout")
    await launcher.browser_mode("scout", "human", "https://example.com/")
    stop_container(backend)
    with pytest.raises(Conflict):
        await launcher.browser_mode("scout", "bot")
    assert marker(launcher).exists()

    assert (await launcher.unfreeze_bot("scout"))["frozen"] is False
    assert not marker(launcher).exists()
    await launcher.create_bot("scout", "o1")  # recreate: the old container was stopped
    handle = await launcher.start_exec("scout", ["true"])
    assert [frame async for frame in launcher.stream_exec(handle)], "exec(true) runs on the recreated container"


async def test_without_the_thaw_the_recreated_container_stays_frozen(launcher, backend):
    await launcher.create_bot("scout", "o1")
    await launcher.freeze_bot("scout")
    stop_container(backend)
    await launcher.create_bot("scout", "o1")
    with pytest.raises(Frozen):
        await launcher.start_exec("scout", ["true"])


async def test_unfreeze_next_to_a_running_human_browser_is_still_refused(launcher):
    await launcher.create_bot("scout", "o1")
    await launcher.freeze_bot("scout")
    await launcher.browser_mode("scout", "human", "https://example.com/")
    with pytest.raises(Conflict, match="human"):
        await launcher.unfreeze_bot("scout")
    assert marker(launcher).exists()


# ---------- the total time budget of a mode switch ----------

def test_the_launcher_budget_is_45_seconds_and_the_polling_fits_in_it():
    assert service.BROWSER_MODE_TOTAL_TIMEOUT == 45.0
    assert service.MODE_POLL_INTERVAL * service.MODE_POLL_ATTEMPTS < service.BROWSER_MODE_TOTAL_TIMEOUT


async def test_browser_mode_gives_up_at_the_total_budget(launcher, backend, monkeypatch):
    await launcher.create_bot("scout", "o1")
    monkeypatch.setattr(service, "BROWSER_MODE_TOTAL_TIMEOUT", 0.3)
    backend.mode_ready_after = 10 ** 9  # the supervisor never reports the mode
    with pytest.raises(BackendError, match="не уложилось"):
        await launcher.browser_mode("scout", "human", "https://example.com/")
    assert not launcher._lock("bot-scout").locked(), "the lock is released after the timeout"


async def test_the_wait_for_a_busy_container_counts_into_the_budget(launcher, monkeypatch):
    await launcher.create_bot("scout", "o1")
    monkeypatch.setattr(service, "BROWSER_MODE_TOTAL_TIMEOUT", 0.2)
    async with launcher._lock("bot-scout"):
        with pytest.raises(BackendError, match="не уложилось"):
            await launcher.browser_mode("scout", "human", "https://example.com/")


async def test_a_switch_that_timed_out_can_be_repeated_and_finishes(launcher, backend, monkeypatch):
    await launcher.create_bot("scout", "o1")
    monkeypatch.setattr(service, "BROWSER_MODE_TOTAL_TIMEOUT", 0.3)
    backend.mode_ready_after = 10 ** 9
    with pytest.raises(BackendError):
        await launcher.browser_mode("scout", "human", "https://example.com/")
    backend.mode_ready_after = 0
    monkeypatch.setattr(service, "BROWSER_MODE_TOTAL_TIMEOUT", 45.0)
    assert (await launcher.browser_mode("scout", "human", "https://example.com/"))["mode"] == "human"


async def test_return_that_cannot_start_the_bot_browser_is_an_error_not_a_success(launcher, backend, monkeypatch):
    """The supervisor refuses to start the bot's Chromium when the human's directory could not be removed: the mode is
    never reported ready, and the launcher answers with an error (the core stays in `returning`)."""
    await launcher.create_bot("scout", "o1")
    await launcher.browser_mode("scout", "human", "https://example.com/")
    monkeypatch.setattr(service, "BROWSER_MODE_TOTAL_TIMEOUT", 0.3)

    async def never_ready(container, mode):
        return False
    backend.browser_mode_ready = never_ready
    with pytest.raises(BackendError):
        await launcher.browser_mode("scout", "bot")


# ---------- the address of the human's tab ----------

HIDDEN = ["‪", "‫", "‬", "‭", "‮", "⁦", "⁧", "⁨", "⁩",
          "​", "‌", "‍", "‎", "‏", "﻿"]


@pytest.mark.parametrize("char", HIDDEN)
def test_validation_refuses_invisible_and_direction_characters_in_the_address(char):
    for url in (f"https://example.com/a{char}b", f"https://exa{char}mple.com/", f"https://example.com/?q={char}"):
        with pytest.raises(ValidationFailed):
            v.validate_browser_url(url)


async def test_browser_mode_never_writes_an_address_with_hidden_characters(launcher, backend):
    await launcher.create_bot("scout", "o1")
    with pytest.raises(ValidationFailed):
        await launcher.browser_mode("scout", "human", "https://bank.example/login‮/gnp.exe")
    assert not [entry for entry in backend.log if entry[0] == "set_browser_mode"]


# ---------- a stale human mode without a freeze marker is healed when the container is (re)created or started ----------

async def test_launcher_start_heals_a_human_mode_left_without_a_marker(launcher, backend):
    await launcher.create_bot("scout", "o1")
    await launcher.browser_mode("scout", "human", "https://example.com/")
    assert not marker(launcher).exists()
    await launcher.startup()
    assert backend.browser_modes["bot-scout"] == "bot" and "bot-scout" in backend.browser_running_names


async def test_launcher_start_keeps_a_human_session_that_has_its_marker(launcher, backend):
    await launcher.create_bot("scout", "o1")
    await launcher.freeze_bot("scout")
    await launcher.browser_mode("scout", "human", "https://example.com/")
    await launcher.startup()
    assert backend.browser_modes["bot-scout"] == "human"
    assert marker(launcher).exists()

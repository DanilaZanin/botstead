"""seccomp-профиль ботов и bot-guard: аргументы docker run, префикс перед каждой командой под uid 1000,
проверка файла профиля при старте лаунчера."""
import dataclasses

import pytest

from bothub_launcher import docker_args as da
from bothub_launcher.backend import DockerCLIBackend
from bothub_launcher.config import Config, ConfigError, load_config
from bothub_launcher.errors import ValidationFailed

from .test_backend_cli import Runner

ENV = {"LAUNCHER_SECRET": "x" * 40, "BOT_TOKEN_SECRET": "bts"}
PROFILE = "/etc/bothub-launcher/seccomp-bot.json"
GOOD = '{"defaultAction": "SCMP_ACT_ERRNO", "syscalls": []}'


def values(argv, flag):
    return [b for a, b in zip(argv, argv[1:]) if a == flag]


def tail_after(argv, container):
    return argv[argv.index(container) + 1:]


def with_profile(cfg, path=PROFILE):
    return dataclasses.replace(cfg, seccomp_profile=path)


# ---------- docker run ----------

def test_bot_run_passes_seccomp_profile_when_configured(cfg):
    argv = da.bot_run_args(with_profile(cfg), "scout", "owner1").argv
    assert f"seccomp={PROFILE}" in values(argv, "--security-opt")
    assert "no-new-privileges" in values(argv, "--security-opt")  # профиль добавляется, а не заменяет
    assert argv.index(f"seccomp={PROFILE}") < argv.index(cfg.image)
    assert "seccomp=unconfined" not in argv


def test_bot_run_without_profile_has_no_seccomp_option(cfg):
    argv = da.bot_run_args(cfg, "scout", "owner1").argv
    assert not any(a.startswith("seccomp=") for a in argv)


def test_seccomp_profile_path_is_made_absolute(cfg):
    argv = da.bot_run_args(with_profile(cfg, "/etc/bothub-launcher//x/../seccomp-bot.json"), "scout", "owner1").argv
    assert "seccomp=/etc/bothub-launcher/seccomp-bot.json" in argv


def test_login_container_stays_on_default_seccomp_profile(cfg):
    argv = da.login_run_args(with_profile(cfg), "owner1").argv
    assert not any(a.startswith("seccomp=") for a in values(argv, "--security-opt"))
    assert "no-new-privileges" in values(argv, "--security-opt")


# ---------- bot-guard перед командами uid 1000 ----------

@pytest.mark.parametrize("build", [
    lambda cfg: da.exec_args(cfg, "bot-a", ["claude", "-p"], ["X"], "t1"),
    lambda cfg: da.pty_exec_args(cfg, "bot-a", ["bash", "-l"]),
    lambda cfg: da.kill_args(cfg, "bot-a", "t1", "TERM"),
    lambda cfg: da.bot_script_args(cfg, "bot-a", "true"),
])
def test_every_uid_1000_exec_starts_with_bot_guard_and_absolute_path(cfg, build):
    argv = build(cfg)
    assert ("--user", "1000:1000") in list(zip(argv, argv[1:]))
    tail = tail_after(argv, "bot-a")
    assert tail[0] == "/usr/local/libexec/bot-guard"
    assert tail[1].startswith("/")  # execv по абсолютному пути: PATH и cwd под контролем бота не участвуют
    assert tail.count(da.BOT_GUARD) == 1


@pytest.mark.parametrize("client_argv", [
    ["id"], ["/usr/bin/env", "-i", "id"], [da.BOT_GUARD, "/bin/sh"], ["setsid", "-w", "sh"], ["bash", "-c", "x"],
])
def test_client_argv_cannot_displace_guard_prefix(cfg, client_argv):
    tail = tail_after(da.exec_args(cfg, "bot-a", client_argv, [], "t1"), "bot-a")
    assert tail[:5] == [da.BOT_GUARD, da.SETSID, "-w", da.BASH, "-c"]
    assert tail[6:8] == ["_", "t1"]  # дальше только данные для `exec "$@"`
    assert tail[8:] == client_argv


def test_client_flaglike_argv_is_rejected(cfg):
    with pytest.raises(ValidationFailed):
        da.exec_args(cfg, "bot-a", ["--privileged", "id"], [], "t1")


def test_pty_command_cannot_displace_guard(cfg):
    tail = tail_after(da.pty_exec_args(cfg, "login-o1", ["codex", "login"]), "login-o1")
    assert tail[:4] == [da.BOT_GUARD, da.BASH, "-c", 'exec "$@"']
    assert tail[-2:] == ["codex", "login"]


@pytest.mark.parametrize("bad", [[], ["bash"], ["./bash"], ["--privileged"], [""]])
def test_guarded_requires_absolute_executable(bad):
    with pytest.raises(ValidationFailed):
        da.guarded(bad)


def test_guarded_accepts_absolute_path():
    assert da.guarded(["/usr/bin/true", "x"]) == [da.BOT_GUARD, "/usr/bin/true", "x"]


async def test_browser_stack_commands_run_without_guard(cfg):
    runner = Runner()

    async def spawn_stream(argv, env):
        runner.calls.append(argv)
        return object()

    be = DockerCLIBackend(cfg, runner, spawn_stream=spawn_stream)
    await be.browser_running("bot-a")
    await be.start_browser("bot-a")
    await be.spawn_screen("bot-a")
    browser_calls = [c for c in runner.calls if "1001:1001" in c]
    assert len(browser_calls) == 3
    for call in browser_calls:
        assert da.BOT_GUARD not in call  # Chromium (uid 1001) получает user namespaces от профиля, без bot-guard


async def test_bot_commands_through_backend_use_guard(cfg):
    runner = Runner()
    be = DockerCLIBackend(cfg, runner)
    await be.terminate_bot_processes("bot-a")
    await be.bot_processes("bot-a")
    await be.kill_in_container("bot-a", "t1", "KILL")
    assert len(runner.calls) == 3
    for call in runner.calls:
        assert tail_after(call, "bot-a")[0] == da.BOT_GUARD


# ---------- проверка файла профиля при старте ----------

def write(tmp_path, text):
    p = tmp_path / "launcher.toml"
    p.write_text(text)
    return str(p)


def profile_file(tmp_path, text=GOOD):
    f = tmp_path / "seccomp-bot.json"
    f.write_text(text)
    return f


def test_seccomp_profile_is_none_by_default():
    assert load_config(None, ENV).seccomp_profile is None


def test_seccomp_profile_loaded_from_toml(tmp_path):
    f = profile_file(tmp_path)
    cfg = load_config(write(tmp_path, f'[seccomp]\nprofile = "{f}"\n'), ENV)
    assert cfg.seccomp_profile == str(f)


@pytest.mark.parametrize("make", [
    lambda tmp: str(tmp / "missing.json"),                             # файла нет
    lambda tmp: str(tmp),                                              # каталог: так выглядит bind mount без файла
    lambda tmp: str(profile_file(tmp, "{not json")),                   # битый JSON
    lambda tmp: str(profile_file(tmp, "")),                            # пустой файл
    lambda tmp: str(profile_file(tmp, "[]")),                          # не объект
    lambda tmp: str(profile_file(tmp, '{"syscalls": []}')),            # нет defaultAction
    lambda tmp: str(profile_file(tmp, '{"defaultAction": 1}')),        # defaultAction не строка
    lambda tmp: str(profile_file(tmp, '{"defaultAction": "allow"}')),  # не SCMP_ACT_*
])
def test_launcher_refuses_to_start_on_missing_or_broken_profile(tmp_path, make):
    with pytest.raises(ConfigError, match="seccomp"):
        load_config(write(tmp_path, f'[seccomp]\nprofile = "{make(tmp_path)}"\n'), ENV)


def test_launcher_refuses_binary_profile(tmp_path):
    f = tmp_path / "seccomp-bot.json"
    f.write_bytes(b"\xff\xfe\x00\x01")
    with pytest.raises(ConfigError, match="seccomp"):
        load_config(write(tmp_path, f'[seccomp]\nprofile = "{f}"\n'), ENV)


@pytest.mark.parametrize("value", ["relative.json", "unconfined", "/etc/x,y.json", "/etc/a b.json", ""])
def test_seccomp_profile_path_is_validated(tmp_path, value):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, f'[seccomp]\nprofile = "{value}"\n'), ENV)


def test_seccomp_section_rejects_unknown_keys_and_wrong_type(tmp_path):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, '[seccomp]\nprofile = "/x.json"\nextra = 1\n'), ENV)
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, 'seccomp = "/x.json"\n'), ENV)


def test_seccomp_profile_cannot_be_set_from_environment():
    cfg = load_config(None, {**ENV, "LAUNCHER_SECCOMP_PROFILE": "/x.json", "SECCOMP_PROFILE": "/x.json"})
    assert cfg.seccomp_profile is None


def test_config_object_checks_profile_path_shape():
    with pytest.raises(ConfigError):
        Config(secret="x" * 40, seccomp_profile="unconfined")
    assert Config(secret="x" * 40, seccomp_profile="/etc/p.json").seccomp_profile == "/etc/p.json"

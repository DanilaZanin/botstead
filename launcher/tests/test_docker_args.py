"""Аргументы docker: флаги безопасности, отказ на инъекциях, секреты вне argv."""
import dataclasses
import hashlib
import hmac

import pytest

from bothub_launcher import docker_args as da
from bothub_launcher.errors import ValidationFailed


def pairs(argv):
    """Все пары (флаг, значение) подряд идущих токенов."""
    return list(zip(argv, argv[1:]))


def values(argv, flag):
    return [b for a, b in pairs(argv) if a == flag]


@pytest.fixture
def run(cfg):
    return da.bot_run_args(cfg, "scout", "owner1")


def test_bot_run_starts_with_docker_run_detached(run, cfg):
    assert run.argv[:3] == [cfg.docker_bin, "run", "-d"]


def test_bot_security_flags(run):
    argv = run.argv
    assert ("--cap-drop", "ALL") in pairs(argv)
    assert ("--security-opt", "no-new-privileges") in pairs(argv)
    assert ("--memory", "2g") in pairs(argv)
    assert ("--memory-swap", "2g") in pairs(argv)
    assert ("--cpus", "2") in pairs(argv)
    assert ("--pids-limit", "512") in pairs(argv)
    assert "--read-only" in argv
    assert "--init" not in argv  # uid 1000 entrypoint must be PID 1 for freeze
    assert ("--pull", "never") in pairs(argv)
    assert ("--user", "1000:1000") in pairs(argv)
    tmpfs = values(argv, "--tmpfs")
    assert any(t.startswith("/tmp:") for t in tmpfs)
    assert any(t.startswith("/run:") for t in tmpfs)
    assert all("nosuid" in t and "nodev" in t for t in tmpfs)
    assert any("uid=1000,gid=1000,mode=0755" in t for t in tmpfs if t.startswith("/run:"))
    assert ("--ulimit", "nofile=1024:1024") in pairs(argv)
    assert ("--sysctl", "net.ipv6.conf.all.disable_ipv6=1") in pairs(argv)
    assert values(argv, "--restart") == ["no"]


def test_bot_runs_the_image_entrypoint_as_is(run, cfg):
    # ENTRYPOINT образа (bot-guard + entrypoint.sh) действует только если лаунчер ничего не переопределяет:
    # ни --entrypoint, ни команды после имени образа.
    assert "--entrypoint" not in run.argv
    assert run.argv[-1] == cfg.image


def test_browser_downloads_dir_is_a_noexec_tmpfs_of_the_browser_uid(run):
    tmpfs = [t for t in values(run.argv, "--tmpfs") if t.startswith("/home/browser/Downloads:")]
    assert len(tmpfs) == 1
    for flag in ("noexec", "nosuid", "nodev", "uid=1001", "gid=1001", "mode=0700"):
        assert flag in tmpfs[0]


def test_tmp_and_run_stay_executable_for_bot_tooling(run):
    # /tmp и /run без noexec: агенты собирают и запускают инструменты там. Решение и причины: docs/isolation.md.
    tmpfs = {t.split(":")[0]: t for t in values(run.argv, "--tmpfs")}
    assert "noexec" not in tmpfs["/tmp"] and "noexec" not in tmpfs["/run"]


def test_bot_user_is_never_root(cfg, run):
    user = values(run.argv, "--user")[0]
    assert user.split(":")[0] not in ("0", "root", "")


def test_config_refuses_root_user():
    from bothub_launcher.config import Config
    for bad in ("0:0", "root", "0", "root:root", ""):
        with pytest.raises(ValueError):
            Config(secret="s" * 40, user=bad)


def test_bot_mounts_are_named_volumes_only(run):
    mounts = values(run.argv, "-v")
    assert mounts == ["bot-scout-home:/home/bot", "bothub-login-owner1:/home/bot/.auth:ro",
                      "bot-scout-browser:/home/browser"]
    for m in mounts:
        src = m.split(":")[0]
        assert not src.startswith(("/", ".", "~"))
    assert "--mount" not in run.argv


def test_bot_has_no_dangerous_flags(run):
    argv = run.argv
    joined = " ".join(argv)
    for flag in ("--privileged", "--cap-add", "--device", "--pid", "--ipc", "--uts", "--userns",
                 "--volumes-from", "--security-opt=seccomp=unconfined", "--publish", "-p"):
        assert flag not in argv, flag
    assert "docker.sock" not in joined
    assert "host" not in values(argv, "--network")
    assert "seccomp=unconfined" not in joined
    assert "apparmor=unconfined" not in joined


def test_bot_network_and_labels(run, cfg):
    assert values(run.argv, "--network") == ["bothub-u-owner1"]
    labels = values(run.argv, "--label")
    assert f"{cfg.label_key}={cfg.label_value}" in labels
    assert "bothub.role=bot" in labels
    assert "bothub.bot_id=scout" in labels
    assert "bothub.owner_id=owner1" in labels
    assert values(run.argv, "--name") == ["bot-scout"]


def test_bot_dns_is_public_and_fixed(run, cfg):
    assert values(run.argv, "--dns") == list(cfg.dns)


def test_internal_dns_replaces_public_resolvers(cfg):
    configured = dataclasses.replace(cfg, internal_dns="172.20.0.1")
    assert values(da.bot_run_args(configured, "scout", "owner1").argv, "--dns") == ["172.20.0.1"]


def test_bot_logs_are_capped(run):
    assert ("--log-opt", "max-size=10m") in pairs(run.argv)


def test_image_is_last_and_fixed(run, cfg):
    assert run.argv[-1] == cfg.image


def test_image_not_overridable_by_ids(cfg):
    with pytest.raises(ValidationFailed):
        da.bot_run_args(cfg, "x bothub-evil", "o")


def test_runtime_only_when_configured(cfg):
    assert "--runtime" not in da.bot_run_args(cfg, "a", "o").argv
    gv = dataclasses.replace(cfg, runtime="runsc")
    assert ("--runtime", "runsc") in pairs(da.bot_run_args(gv, "a", "o").argv)
    assert ("--runtime", "runsc") in pairs(da.login_run_args(gv, "o").argv)


def test_token_not_in_argv_but_in_env(cfg, run):
    expected = hmac.new(b"bot-token-secret", b"scout", hashlib.sha256).hexdigest()
    assert run.env == {"BOTHUB_TOKEN": f"bot:scout:{expected}"}
    assert "BOTHUB_TOKEN" in values(run.argv, "-e")
    assert not any("bot:scout:" in a for a in run.argv)
    assert "BOTHUB_URL=http://core:8080" in values(run.argv, "-e")


def test_token_helper_matches_core_formula():
    secret = "k"
    assert da.bot_token(secret, "mac") == "bot:mac:" + hmac.new(b"k", b"mac", hashlib.sha256).hexdigest()


def test_bot_token_secret_required(cfg):
    empty = dataclasses.replace(cfg, bot_token_secret="")
    with pytest.raises(ValidationFailed):
        da.bot_run_args(empty, "a", "o")


@pytest.mark.parametrize("bot_id", ["x --privileged", "x;rm -rf /", "-x", "--cap-add=ALL", "x\n", "X", "x/../y", "", "$(id)"])
def test_bot_id_injection_rejected(cfg, bot_id):
    with pytest.raises(ValidationFailed):
        da.bot_run_args(cfg, bot_id, "o")


@pytest.mark.parametrize("owner_id", ["o --network host", "o\n--cap-add=ALL", "-o", "O", "a" * 41, "o:ro", "../../etc"])
def test_owner_id_injection_rejected(cfg, owner_id):
    with pytest.raises(ValidationFailed):
        da.bot_run_args(cfg, "a", owner_id)
    with pytest.raises(ValidationFailed):
        da.login_run_args(cfg, owner_id)
    with pytest.raises(ValidationFailed):
        da.network_create_args(cfg, owner_id)


def test_login_container(cfg):
    spec = da.login_run_args(cfg, "owner1")
    argv = spec.argv
    assert values(argv, "--name") == ["login-owner1"]
    assert "bothub.role=login" in values(argv, "--label")
    assert values(argv, "-v") == ["bothub-login-owner1:/home/bot"]
    assert ("--cap-drop", "ALL") in pairs(argv)
    assert ("--security-opt", "no-new-privileges") in pairs(argv)
    assert "--read-only" in argv
    assert ("--memory", "2g") in pairs(argv)
    assert values(argv, "--network") == ["bothub-u-owner1"]
    assert spec.env == {}
    assert "BOTHUB_TOKEN" not in " ".join(argv)
    assert ("--entrypoint", "sleep") in pairs(argv)
    assert argv[-2:] == [cfg.image, str(cfg.login_ttl)]


def test_exec_args(cfg):
    argv = da.exec_args(cfg, "bot-scout", ["claude", "-p"], ["BOTHUB_TURN_ID", "BOTHUB_THREAD_ID"], "turn-1")
    assert argv[:3] == [cfg.docker_bin, "exec", "-i"]
    assert ("--user", "1000:1000") in pairs(argv)
    assert ("--workdir", "/home/bot") in pairs(argv)
    assert values(argv, "-e") == ["BOTHUB_TURN_ID", "BOTHUB_THREAD_ID"]  # только имена, значения идут через окружение
    assert "--privileged" not in argv
    i = argv.index("bot-scout")
    assert argv[i + 1:i + 4] == [da.BOT_GUARD, da.SETSID, "-w"]  # bot-guard всегда первый, пути абсолютные
    assert argv[-2:] == ["claude", "-p"]


def test_exec_extra_flag_in_argv_cannot_reach_docker(cfg):
    argv = da.exec_args(cfg, "bot-a", ["echo", "--privileged", "-u", "root"], [], "t1")
    assert argv.index("bot-a") < argv.index("--privileged")
    assert ("--user", "1000:1000") in pairs(argv[:argv.index("bot-a")])
    assert "root" not in argv[:argv.index("bot-a")]


def test_exec_rejects_flaglike_command(cfg):
    with pytest.raises(ValidationFailed):
        da.exec_args(cfg, "bot-a", ["--privileged", "id"], [], "t1")


def test_exec_marker_script_writes_pid_and_execs(cfg):
    argv = da.marker_wrap(["claude", "-p"], "turn-1")
    assert argv[:4] == [da.SETSID, "-w", da.BASH, "-c"]
    assert "/tmp/bothub-exec-$id.pid" in argv[4]
    assert argv[5:] == ["_", "turn-1", "claude", "-p"]


def test_kill_args(cfg):
    argv = da.kill_args(cfg, "bot-a", "turn-1", "TERM")
    assert argv[:3] == [cfg.docker_bin, "exec", "--user"]
    assert "turn-1" in argv and "TERM" in argv
    assert not any("rm " in a for a in argv)
    with pytest.raises(ValidationFailed):
        da.kill_args(cfg, "bot-a", "turn-1", "TERM; reboot")
    with pytest.raises(ValidationFailed):
        da.kill_args(cfg, "bot-a", "turn 1", "TERM")


def test_network_create_args(cfg):
    argv = da.network_create_args(cfg, "owner1")
    assert argv[:4] == [cfg.docker_bin, "network", "create", "--driver"]
    assert argv[-1] == "bothub-u-owner1"
    assert "--internal" not in argv  # интернет нужен ботам
    assert "--ipv6=false" in argv
    assert f"{da.BRIDGE_OPT}={da.bridge_name('owner1')}" in values(argv, "--opt")
    assert "bothub.owner_id=owner1" in values(argv, "--label")
    assert "bothub.managed=1" in values(argv, "--label")


def test_bridge_name_is_short_stable_and_distinct():
    a, b = da.bridge_name("owner1"), da.bridge_name("owner2")
    assert a == da.bridge_name("owner1")
    assert a != b
    assert len(a) <= 15 and a.startswith("bhu")
    assert all(c.isalnum() for c in a)


def test_volume_create_args(cfg):
    argv = da.volume_create_args(cfg, "bothub-login-owner1", "owner1", "login")
    assert argv[:4] == [cfg.docker_bin, "volume", "create", "--label"]
    assert "bothub.managed=1" in values(argv, "--label")
    assert "bothub.owner_id=owner1" in values(argv, "--label")
    assert argv[-1] == "bothub-login-owner1"


def test_names(cfg):
    assert cfg.bot_container("a") == "bot-a"
    assert cfg.home_volume("a") == "bot-a-home"
    assert cfg.network_name("o") == "bothub-u-o"
    assert cfg.login_container("o") == "login-o"
    assert cfg.login_volume("o") == "bothub-login-o"

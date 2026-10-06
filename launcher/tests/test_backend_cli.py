"""DockerCLIBackend: разбор вывода docker, защита метками, сборка argv (без настоящего Docker)."""
import dataclasses
import json
import logging
import re

import pytest

from bothub_launcher import docker_args as da
from bothub_launcher.backend import DockerCLIBackend, parse_docker_version
from bothub_launcher.errors import BackendError, Conflict, NetPolicyError, NotManaged
from bothub_launcher.procs import CmdResult

MANAGED = {"bothub.managed": "1", "bothub.role": "bot", "bothub.bot_id": "scout", "bothub.owner_id": "o1"}


class Runner:
    def __init__(self):
        self.calls: list[list[str]] = []
        self.envs: list[dict] = []
        self.rules: list[tuple[tuple[str, ...], list[CmdResult]]] = []

    def on(self, *needles, rc=0, out="", err=""):
        self.rules.append((needles, [CmdResult(rc, out, err)]))

    def seq(self, *needles, results):
        self.rules.append((needles, list(results)))

    async def __call__(self, argv, *, input=None, env=None):
        self.calls.append(list(argv))
        self.envs.append(dict(env or {}))
        for needles, results in self.rules:
            if all(n in argv for n in needles):
                return results.pop(0) if len(results) > 1 else results[0]
        return CmdResult(0, "", "")

    def ran(self, *needles):
        return [c for c in self.calls if all(n in c for n in needles)]


def container_json(name="bot-scout", labels=None, running=True):
    return json.dumps([{
        "Id": "c0ffee" * 10,
        "Name": f"/{name}",
        "Config": {"Labels": MANAGED if labels is None else labels, "Image": "bothub-bot"},
        "State": {"Running": running, "Status": "running" if running else "exited", "ExitCode": 0,
                  "OOMKilled": False, "StartedAt": "2026-10-04T10:00:00Z"},
        "RestartCount": 2,
        "NetworkSettings": {"Networks": {"bothub-u-o1": {"IPAddress": "172.20.0.5"}}},
    }])


def network_json(labels=None, core=True, bridge="bhuabcdef0123", enable_ipv6=False):
    containers = {"id1": {"Name": "bothub-core", "IPv4Address": "172.20.0.2/16"}} if core else {}
    return json.dumps([{
        "Id": "f" * 64, "Name": "bothub-u-o1",
        "EnableIPv6": enable_ipv6,
        "Labels": {"bothub.managed": "1", "bothub.owner_id": "o1"} if labels is None else labels,
        "Options": {da.BRIDGE_OPT: bridge} if bridge else {},
        "Containers": containers,
    }])


@pytest.fixture
def runner():
    return Runner()


@pytest.fixture
def be(cfg, runner):
    return DockerCLIBackend(cfg, runner)


async def test_inspect_container_parses_state_and_labels(be, runner):
    runner.on("inspect", "bot-scout", out=container_json())
    info = await be.inspect_container("bot-scout")
    assert info.name == "bot-scout" and info.running and info.status == "running"
    assert info.labels["bothub.owner_id"] == "o1" and info.restart_count == 2
    assert info.networks == ("bothub-u-o1",) and info.image == "bothub-bot"
    assert runner.calls[0][:4] == ["docker", "inspect", "--type", "container"]


@pytest.mark.parametrize("err", ["Error: No such object: bot-x", "Error response from daemon: No such container: bot-x"])
async def test_inspect_missing_is_none(be, runner, err):
    runner.on("inspect", rc=1, err=err)
    assert await be.inspect_container("bot-x") is None


async def test_docker_unreachable_is_backend_error(be, runner):
    runner.on("inspect", rc=1, err="Cannot connect to the Docker daemon at unix:///var/run/docker.sock")
    with pytest.raises(BackendError):
        await be.inspect_container("bot-x")


async def test_docker_binary_missing_is_backend_error(be, runner):
    runner.on("inspect", rc=127, err="docker: not found")
    with pytest.raises(BackendError):
        await be.inspect_container("bot-x")


async def test_run_bot_uses_built_args_and_env(be, runner, cfg):
    runner.on("run", out="abc123def\n")
    cid = await be.run_bot("scout", "o1")
    spec = da.bot_run_args(cfg, "scout", "o1")
    assert cid == "abc123def"
    assert runner.calls[0] == spec.argv
    assert runner.envs[0]["BOTHUB_TOKEN"] == spec.env["BOTHUB_TOKEN"]
    assert ("--cap-drop", "ALL") in list(zip(runner.calls[0], runner.calls[0][1:]))


async def test_run_bot_failure_message_without_secrets(be, runner):
    runner.on("run", rc=125, err="docker: Error response from daemon: pull access denied for bothub-bot")
    with pytest.raises(BackendError) as exc:
        await be.run_bot("scout", "o1")
    assert "pull access denied" in str(exc.value)
    assert "bot:scout:" not in str(exc.value)


async def test_run_bot_name_in_use_is_conflict(be, runner):
    runner.on("run", rc=125, err='docker: Error response from daemon: Conflict. The container name "/bot-scout" is already in use')
    with pytest.raises(Conflict):
        await be.run_bot("scout", "o1")


async def test_disable_restart_updates_only_managed_bot(be, runner):
    runner.on("inspect", "bot-scout", out=container_json())
    await be.disable_restart("bot-scout")
    assert runner.ran("update", "--restart=no", "bot-scout")
    runner.rules.clear()
    runner.on("inspect", "bot-scout", out=container_json(labels={}))
    with pytest.raises(NotManaged):
        await be.disable_restart("bot-scout")
    assert len(runner.ran("update")) == 1


async def test_run_login_uses_login_args(be, runner, cfg):
    runner.on("run", out="deadbeef\n")
    await be.run_login("o1")
    assert runner.calls[0] == da.login_run_args(cfg, "o1").argv


async def test_remove_container_requires_label(be, runner):
    runner.on("inspect", out=container_json(labels={"some": "label"}))
    with pytest.raises(NotManaged):
        await be.remove_container("bot-scout")
    assert not runner.ran("rm")


async def test_remove_container_requires_exact_label_value(be, runner):
    runner.on("inspect", out=container_json(labels={"bothub.managed": "0"}))
    with pytest.raises(NotManaged):
        await be.remove_container("bot-scout")
    assert not runner.ran("rm")


async def test_remove_container_ok_and_missing(be, runner):
    runner.seq("inspect", results=[CmdResult(0, container_json(), ""), CmdResult(1, "", "Error: No such object: x")])
    assert await be.remove_container("bot-scout") is True
    assert runner.ran("rm", "-f", "bot-scout")
    assert await be.remove_container("bot-scout") is False


def new_network_then_core(runner):
    runner.seq("network", "inspect", results=[
        CmdResult(1, "", "Error response from daemon: network bothub-u-o1 not found"),
        CmdResult(0, network_json(core=False), ""),
        CmdResult(0, network_json(core=True), ""),
    ])


async def test_ensure_network_creates_and_connects_core(be, runner, cfg):
    runner.on("version", out="28.0.1\n")
    await be.detect_docker_version()
    new_network_then_core(runner)
    info = await be.ensure_network("o1")
    assert runner.ran("network", "create", "bothub-u-o1")
    assert runner.ran("network", "connect")[0] == [
        "docker", "network", "connect", "--alias", "core", "--gw-priority", "-100", "bothub-u-o1", "bothub-core"]
    assert info.bridge == "bhuabcdef0123" and info.core_ip == "172.20.0.2" and info.name == "bothub-u-o1"


@pytest.mark.parametrize("version", ["28.0.0", "28.0.0-rc.1", "29.1.3"])
async def test_connect_passes_gw_priority_on_docker_28_and_newer(be, runner, version):
    runner.on("version", out=f"{version}\n")
    await be.detect_docker_version()
    new_network_then_core(runner)
    await be.ensure_network("o1")
    connect = runner.ran("network", "connect")[0]
    assert connect[connect.index("--gw-priority"):connect.index("--gw-priority") + 2] == ["--gw-priority", "-100"]
    assert connect[-2:] == ["bothub-u-o1", "bothub-core"]


async def test_connect_has_no_gw_priority_on_docker_27(be, runner, caplog):
    runner.on("version", out="27.5.1\n")
    with caplog.at_level(logging.WARNING, logger="bothub_launcher"):
        await be.detect_docker_version()
    new_network_then_core(runner)
    await be.ensure_network("o1")
    assert runner.ran("network", "connect")[0] == [
        "docker", "network", "connect", "--alias", "core", "bothub-u-o1", "bothub-core"]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "27.5.1" in warnings[0].getMessage() and "--gw-priority" in warnings[0].getMessage()


async def test_connect_uses_gw_priority_value_from_config(cfg, runner):
    runner.on("version", out="28.3.0\n")
    custom = DockerCLIBackend(dataclasses.replace(cfg, core_gw_priority=-7), runner)
    await custom.detect_docker_version()
    new_network_then_core(runner)
    await custom.ensure_network("o1")
    connect = runner.ran("network", "connect")[0]
    assert connect[connect.index("--gw-priority") + 1] == "-7"


async def test_connect_has_no_gw_priority_before_version_detection(be, runner):
    new_network_then_core(runner)
    await be.ensure_network("o1")
    assert "--gw-priority" not in runner.ran("network", "connect")[0]


async def test_docker_version_is_asked_from_the_daemon(be, runner):
    runner.on("version", out="28.0.0\n")
    await be.detect_docker_version()
    assert runner.calls == [["docker", "version", "--format", "{{.Server.Version}}"]]


@pytest.mark.parametrize("out", ["", "garbage", "docker 28", "28", "v", "28.x.1"])
async def test_unparsable_docker_version_is_unknown_with_one_warning(be, runner, caplog, out):
    runner.on("version", out=out)
    with caplog.at_level(logging.WARNING, logger="bothub_launcher"):
        await be.detect_docker_version()
    new_network_then_core(runner)
    await be.ensure_network("o1")
    assert "--gw-priority" not in runner.ran("network", "connect")[0]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "неизвестна" in warnings[0].getMessage()


@pytest.mark.parametrize("result", [CmdResult(1, "", "Cannot connect to the Docker daemon"),
                                    CmdResult(127, "", "docker: not found")])
async def test_failed_docker_version_is_unknown_not_a_startup_crash(be, runner, caplog, result):
    runner.on("version", rc=result.rc, err=result.err)
    with caplog.at_level(logging.WARNING, logger="bothub_launcher"):
        await be.detect_docker_version()
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
    new_network_then_core(runner)
    await be.ensure_network("o1")
    assert "--gw-priority" not in runner.ran("network", "connect")[0]


async def test_redetecting_an_old_docker_turns_the_flag_off(be, runner):
    runner.seq("version", results=[CmdResult(0, "28.0.0\n", ""), CmdResult(0, "27.5.1\n", "")])
    await be.detect_docker_version()
    await be.detect_docker_version()
    new_network_then_core(runner)
    await be.ensure_network("o1")
    assert "--gw-priority" not in runner.ran("network", "connect")[0]


@pytest.mark.parametrize("text, expected", [
    ("29.1.3", (29, 1, 3)), ("28.0.0-rc.1", (28, 0, 0)), ("27.5.1", (27, 5, 1)), ("28.0", (28, 0, 0)),
    ("v28.2.2", (28, 2, 2)), ("28.0.0+azure", (28, 0, 0)), (" 28.1.1\n", (28, 1, 1)),
    ("", None), ("garbage", None), ("28", None), ("28.", None), ("a.b.c", None), ("28.0.0 extra", None),
    ("-28.0.0", None),
])
def test_parse_docker_version(text, expected):
    assert parse_docker_version(text) == expected


async def test_ensure_network_existing_with_core_is_noop(be, runner):
    runner.on("network", "inspect", out=network_json())
    info = await be.ensure_network("o1")
    assert not runner.ran("network", "create") and not runner.ran("network", "connect")
    assert info.core_ip == "172.20.0.2"


async def test_ensure_network_rejects_existing_ipv6_before_core_connect(be, runner):
    runner.on("network", "inspect", out=network_json(core=False, enable_ipv6=True))
    with pytest.raises(NetPolicyError, match="IPv6"):
        await be.ensure_network("o1")
    assert not runner.ran("network", "connect")


async def test_ensure_network_refuses_foreign_network(be, runner):
    runner.on("network", "inspect", out=network_json(labels={}))
    with pytest.raises(NotManaged):
        await be.ensure_network("o1")
    assert not runner.ran("network", "connect")


async def test_ensure_network_core_connect_failure_is_explicit(be, runner):
    runner.seq("network", "inspect", results=[CmdResult(0, network_json(core=False), "")])
    runner.on("network", "connect", rc=1, err="Error response from daemon: No such container: bothub-core")
    with pytest.raises(BackendError) as exc:
        await be.ensure_network("o1")
    assert "bothub-core" in str(exc.value)


async def test_network_bridge_falls_back_to_docker_default_name(be, runner):
    runner.on("network", "inspect", out=network_json(bridge=None))
    info = await be.ensure_network("o1")
    assert info.bridge == "br-" + "f" * 12


async def test_list_networks_only_labelled(be, runner):
    runner.on("network", "ls", out="bothub-u-o1\n")
    runner.on("network", "inspect", out=network_json())
    nets = await be.list_networks()
    assert [n.name for n in nets] == ["bothub-u-o1"]
    assert "label=bothub.managed=1" in runner.ran("network", "ls")[0]


async def test_list_networks_empty(be, runner):
    runner.on("network", "ls", out="")
    assert await be.list_networks() == []
    assert not runner.ran("network", "inspect")


async def test_list_containers_filters_by_label_and_role(be, runner):
    runner.on("ps", out="bot-scout\n")
    runner.on("inspect", out=container_json())
    items = await be.list_containers(role="bot")
    assert [i.name for i in items] == ["bot-scout"]
    ps = runner.ran("ps")[0]
    assert "label=bothub.managed=1" in ps and "label=bothub.role=bot" in ps and "-a" in ps


async def test_ensure_volume_creates_missing(be, runner, cfg):
    runner.seq("volume", "inspect", results=[
        CmdResult(1, "", "Error response from daemon: get x: no such volume"),
        CmdResult(0, json.dumps([{"Name": "bothub-login-o1", "Labels": {
            cfg.label_key: cfg.label_value, "bothub.owner_id": "o1", "bothub.role": "login"}}]), "")])
    await be.ensure_volume("bothub-login-o1", "o1", "login")
    assert runner.ran("volume", "create", "bothub-login-o1")[0] == da.volume_create_args(cfg, "bothub-login-o1", "o1", "login")


async def test_ensure_volume_rechecks_labels_after_create_race(cfg, runner):
    cfg = dataclasses.replace(cfg, adopt_legacy_volumes=True)
    name = cfg.browser_volume("scout")
    runner.seq("volume", "inspect", results=[
        CmdResult(1, "", "no such volume"),
        CmdResult(0, json.dumps([{"Name": name, "Labels": {
            cfg.label_key: cfg.label_value, "bothub.owner_id": "other", "bothub.role": "browser"}}]), "")])
    with pytest.raises(Conflict):
        await DockerCLIBackend(cfg, runner).ensure_volume(name, "o1", "browser")
    assert runner.ran("volume", "create", name)


async def test_remove_volume_never_adopts_browser(cfg, runner):
    name = cfg.browser_volume("scout")
    runner.on("volume", "inspect", name, out=json.dumps([{"Name": name, "Labels": None}]))
    runner.on("volume", "inspect", f"{name}-adopted", out=json.dumps([{"Name": f"{name}-adopted",
        "Labels": {cfg.label_key: cfg.label_value, "bothub.owner_id": "o1", "bothub.role": "browser"}}]))
    with pytest.raises(NotManaged):
        await DockerCLIBackend(cfg, runner).remove_volume(name, "o1", "browser")
    assert not runner.ran("volume", "inspect", f"{name}-adopted")
    assert not runner.ran("volume", "rm")


async def test_ensure_volume_labelled_is_noop(be, runner):
    runner.on("volume", "inspect", out=json.dumps([{"Name": "v", "Labels": {
        "bothub.managed": "1", "bothub.owner_id": "o1", "bothub.role": "home"}}]))
    await be.ensure_volume("v", "o1", "home")
    assert not runner.ran("volume", "create")


async def test_ensure_volume_unlabelled_refused_unless_adopted(cfg, runner):
    runner.on("volume", "inspect", "bot-mac-home", out=json.dumps([{"Name": "bot-mac-home", "Labels": None}]))
    runner.on("volume", "inspect", "bot-mac-home-adopted", rc=1, err="no such volume")
    with pytest.raises(NotManaged) as exc:
        await DockerCLIBackend(cfg, runner).ensure_volume("bot-mac-home", "o1", "home")
    assert "adopt_legacy_volumes" in str(exc.value)
    adopting = DockerCLIBackend(dataclasses.replace(cfg, adopt_legacy_volumes=True), runner)
    await adopting.ensure_volume("bot-mac-home", "o1", "home")
    assert runner.ran("volume", "create", "bot-mac-home-adopted")
    assert runner.ran("volume", "create", "bot-mac-home-adopted-complete")
    assert "rm -rf /to/.claude /to/.codex /to/.gemini /to/.auth" in runner.ran("run")[0][-1]
    assert adopting._volume_names["bot-mac-home"] == "bot-mac-home-adopted"


async def test_legacy_login_volume_copy_removes_old_login_dirs(cfg, runner):
    runner.on("volume", "inspect", "bothub-login-o1", out=json.dumps([{
        "Name": "bothub-login-o1", "Labels": None}]))
    runner.on("volume", "inspect", "bothub-login-o1-adopted", rc=1, err="no such volume")
    adopting = DockerCLIBackend(dataclasses.replace(cfg, adopt_legacy_volumes=True), runner)
    await adopting.ensure_volume("bothub-login-o1", "o1", "login")
    script = runner.ran("run")[-1][-1]
    assert "rm -rf /to/.claude /to/.codex /to/.gemini /to/.auth" in script
    assert "chown -R 1000:1000 /to" in script
    assert runner.ran("volume", "create", "bothub-login-o1-adopted-complete")


async def test_ensure_volume_rejects_wrong_owner(be, runner):
    runner.on("volume", "inspect", out=json.dumps([{"Name": "v", "Labels": {
        "bothub.managed": "1", "bothub.owner_id": "someone-else", "bothub.role": "home"}}]))
    with pytest.raises(Conflict):
        await be.ensure_volume("v", "o1", "home")


async def test_adopted_volume_survives_adopt_flag_off(cfg, runner):
    runner.on("volume", "inspect", "bot-scout-home", out=json.dumps([{"Name": "bot-scout-home", "Labels": None}]))
    runner.on("volume", "inspect", "bot-scout-home-adopted", out=json.dumps([{
        "Name": "bot-scout-home-adopted", "Labels": {"bothub.managed": "1", "bothub.owner_id": "o1",
                                                 "bothub.role": "home"}}]))
    runner.on("volume", "inspect", "bot-scout-home-adopted-complete", out=json.dumps([{
        "Name": "bot-scout-home-adopted-complete", "Labels": {"bothub.managed": "1", "bothub.owner_id": "o1",
                                                          "bothub.role": "adoption-complete"}}]))
    be = DockerCLIBackend(cfg, runner)
    await be.ensure_volume("bot-scout-home", "o1", "home")
    runner.on("run", out="container-id\n")
    await be.run_bot("scout", "o1")
    assert "bot-scout-home-adopted:/home/bot" in runner.ran("run")[-1]
    assert not runner.ran("volume", "create")


async def test_incomplete_adoption_rejected_then_retried(cfg, runner):
    runner.on("volume", "inspect", "bot-scout-home", out=json.dumps([{"Name": "bot-scout-home", "Labels": None}]))
    runner.on("volume", "inspect", "bot-scout-home-adopted", out=json.dumps([{
        "Name": "bot-scout-home-adopted", "Labels": {"bothub.managed": "1", "bothub.owner_id": "o1",
                                                 "bothub.role": "home"}}]))
    runner.on("volume", "inspect", "bot-scout-home-adopted-complete", rc=1, err="no such volume")
    with pytest.raises(Conflict, match="не завершено"):
        await DockerCLIBackend(cfg, runner).ensure_volume("bot-scout-home", "o1", "home")
    adopting = DockerCLIBackend(dataclasses.replace(cfg, adopt_legacy_volumes=True), runner)
    await adopting.ensure_volume("bot-scout-home", "o1", "home")
    assert runner.ran("volume", "create", "bot-scout-home-adopted-complete")
    assert adopting._volume_names["bot-scout-home"] == "bot-scout-home-adopted"


async def test_remove_volume_requires_label(be, runner):
    runner.on("volume", "inspect", out=json.dumps([{"Name": "v", "Labels": {}}]))
    with pytest.raises(NotManaged):
        await be.remove_volume("v", "o1", "home")
    assert not runner.ran("volume", "rm")
    runner.rules.clear()
    runner.on("volume", "inspect", out=json.dumps([{"Name": "v", "Labels": {
        "bothub.managed": "1", "bothub.owner_id": "o1", "bothub.role": "home"}}]))
    await be.remove_volume("v", "o1", "home")
    assert runner.ran("volume", "rm", "v")


async def test_remove_adopted_volume_requires_owner_and_role(be, runner):
    runner.on("volume", "inspect", "v", out=json.dumps([{"Name": "v", "Labels": None}]))
    runner.on("volume", "inspect", "v-adopted", out=json.dumps([{"Name": "v-adopted", "Labels": {
        "bothub.managed": "1", "bothub.owner_id": "other", "bothub.role": "home"}}]))
    with pytest.raises(Conflict):
        await be.remove_volume("v", "o1", "home")
    assert not runner.ran("volume", "rm")


async def test_adoption_marker_is_not_mounted_in_bot(cfg, runner):
    runner.on("volume", "inspect", "bot-scout-home", out=json.dumps([{"Name": "bot-scout-home", "Labels": None}]))
    runner.on("volume", "inspect", "bot-scout-home-adopted", out=json.dumps([{
        "Name": "bot-scout-home-adopted", "Labels": {"bothub.managed": "1", "bothub.owner_id": "o1",
                                                 "bothub.role": "home"}}]))
    runner.on("volume", "inspect", "bot-scout-home-adopted-complete", out=json.dumps([{
        "Name": "bot-scout-home-adopted-complete", "Labels": {"bothub.managed": "1", "bothub.owner_id": "o1",
                                                          "bothub.role": "adoption-complete"}}]))
    be = DockerCLIBackend(cfg, runner)
    await be.ensure_volume("bot-scout-home", "o1", "home")
    await be.run_bot("scout", "o1")
    assert not any("bot-scout-home-adopted-complete" in token for token in runner.ran("run")[-1])
    assert not runner.ran("volume", "create")


async def test_purge_adopted_volume_removes_marker_first(be, runner):
    runner.on("volume", "inspect", "v", out=json.dumps([{"Name": "v", "Labels": None}]))
    runner.on("volume", "inspect", "v-adopted", out=json.dumps([{"Name": "v-adopted", "Labels": {
        "bothub.managed": "1", "bothub.owner_id": "o1", "bothub.role": "home"}}]))
    runner.on("volume", "inspect", "v-adopted-complete", out=json.dumps([{"Name": "v-adopted-complete",
        "Labels": {"bothub.managed": "1", "bothub.owner_id": "o1", "bothub.role": "adoption-complete"}}]))
    await be.remove_volume("v", "o1", "home")
    assert runner.ran("volume", "rm") == [["docker", "volume", "rm", "v-adopted-complete"],
                                           ["docker", "volume", "rm", "v-adopted"]]


async def test_spawn_exec_builds_argv_and_passes_env_by_name(cfg, runner):
    captured = {}

    async def spawn(argv, env):
        captured["argv"], captured["env"] = argv, env
        return object()

    be = DockerCLIBackend(cfg, runner, spawn=spawn)
    await be.spawn_exec("bot-scout", ["claude", "-p"], {"ANTHROPIC_AUTH_TOKEN": "tok-123"}, "t1")
    assert captured["argv"] == da.exec_args(cfg, "bot-scout", ["claude", "-p"], ["ANTHROPIC_AUTH_TOKEN"], "t1")
    assert "tok-123" not in " ".join(captured["argv"])
    assert captured["env"]["ANTHROPIC_AUTH_TOKEN"] == "tok-123"
    assert "LAUNCHER_SECRET" not in captured["env"] and "BOT_TOKEN_SECRET" not in captured["env"]


async def test_spawn_pty_builds_tty_exec(cfg, runner):
    captured = {}

    async def spawn_pty(argv, cols, rows):
        captured.update(argv=argv, size=(cols, rows))
        return object()

    be = DockerCLIBackend(cfg, runner, spawn_pty=spawn_pty)
    await be.spawn_pty("login-o1", ["bash", "-l"], 100, 30)
    argv = captured["argv"]
    assert argv[:3] == ["docker", "exec", "-it"]
    assert ("--user", "1000:1000") in list(zip(argv, argv[1:]))
    i = argv.index("login-o1")
    assert argv[i + 1:] == [da.BOT_GUARD, da.BASH, "-c", 'exec "$@"', "_", "bash", "-l"]
    assert captured["size"] == (100, 30)


async def test_spawn_screen_uses_fixed_unix_socket_rfb_command(cfg, runner):
    spawned = []

    async def spawn(argv, env):
        spawned.append((argv, env))
        return object()

    backend = DockerCLIBackend(cfg, runner, spawn_stream=spawn)
    await backend.spawn_screen("bot-scout")
    assert spawned == [(["docker", "exec", "-i", "--user", "1001:1001", "bot-scout", "socat", "-",
                         "UNIX-CONNECT:/home/browser/.vnc/rfb.sock"], {})]
    assert "TCP" not in " ".join(spawned[0][0])


async def test_kill_in_container_runs_kill_args(be, runner, cfg):
    await be.kill_in_container("bot-scout", "t1", "TERM")
    assert runner.calls[-1] == da.kill_args(cfg, "bot-scout", "t1", "TERM")

async def test_browser_and_freeze_docker_exec_users(be, runner):
    runner.seq('exec', 'sh', results=[CmdResult(1, '', ''), CmdResult(0, '', '')])
    assert not await be.browser_running('bot-scout')
    await be.start_browser('bot-scout')
    await be.terminate_bot_processes('bot-scout')
    browser_check = runner.ran('exec', 'sh')[0]
    browser_start = runner.ran('exec', '-d')[0]
    freeze = runner.ran('exec', da.BASH)[0]
    assert browser_check[:7] == ['docker', 'exec', '--user', '1001:1001', '-e', 'HOME=/home/browser', 'bot-scout']
    assert '.browser-ready' in browser_check[-1]
    assert '127.0.0.1:9222/json/version' in browser_check[-1]
    assert browser_start[:8] == ['docker', 'exec', '-d', '--user', '1001:1001', '-e', 'HOME=/home/browser', 'bot-scout']
    assert browser_start[-3:-1] == ['sh', '-c']
    assert 'exec /usr/local/bin/chromium-supervisor.sh' in browser_start[-1]
    assert 'case "$cmd" in *chromium-supervisor.sh*) exit 0' in browser_start[-1]
    assert freeze[:6] == ['docker', 'exec', '--user', '1000:1000', 'bot-scout', da.BOT_GUARD]
    assert freeze[6:8] == [da.BASH, '-c']
    assert da.BOT_GUARD not in browser_check and da.BOT_GUARD not in browser_start  # браузер uid 1001 без загрузчика
    assert 'docker' == runner.ran('exec', da.BASH)[0][0]
    await be.terminate_bot_processes('bot-scout', 'KILL')
    assert 'kill -KILL' in runner.ran('exec', da.BASH)[-1][-1]


async def test_browser_volume_rejects_foreign_owner(be, runner, cfg):
    name = cfg.browser_volume('scout')
    runner.on('volume', 'inspect', name, out=json.dumps([{'Labels': {
        cfg.label_key: cfg.label_value, 'bothub.owner_id': 'other', 'bothub.role': 'browser'}}]))
    with pytest.raises(Conflict):
        await be.ensure_volume(name, 'o1', 'browser')
    assert not runner.ran('volume', 'create')


async def test_browser_volume_never_adopts_unlabelled_data(cfg, runner):
    cfg = dataclasses.replace(cfg, adopt_legacy_volumes=True)
    be = DockerCLIBackend(cfg, runner)
    name = cfg.browser_volume('scout')
    runner.on('volume', 'inspect', name, out=json.dumps([{'Labels': {}}]))
    with pytest.raises(NotManaged):
        await be.ensure_volume(name, 'o1', 'browser')
    assert not runner.ran('volume', 'create')
    assert not runner.ran('run')


async def test_proc_scan_excludes_pid1_and_fails_closed(be, runner):
    runner.on('exec', da.BASH, out='9999\n')
    assert await be.bot_processes('bot-scout') == [9999]
    scan = runner.ran('exec', da.BASH)[-1]
    assert scan[:6] == ['docker', 'exec', '--user', '1000:1000', 'bot-scout', da.BOT_GUARD]
    assert scan[6:8] == [da.BASH, '-c']
    assert '/proc/[0-9]*/status' in scan[-1]
    assert '"$p" = "$$"' in scan[-1]
    runner.rules.clear()
    runner.on('exec', da.BASH, out='bad\n')
    with pytest.raises(BackendError):
        await be.bot_processes('bot-scout')


# ---------- режим браузера ----------

import os  # noqa: E402
import subprocess  # noqa: E402


def _script(call):
    """Тело `sh -c` из argv и позиционные аргументы после `_`."""
    i = call.index("-c")
    return call[i + 1], call[i + 3:]


def _run_sh(call, home, stdin=""):
    script, args = _script(call)
    return subprocess.run(["sh", "-c", script, "_", *args], input=stdin, text=True, capture_output=True,
                          env={"HOME": str(home), "PATH": os.environ["PATH"]}, check=False)


async def test_browser_mode_commands_run_as_uid_1001_without_bot_guard(be, runner):
    runner.on("exec", out="human\n")
    await be.get_browser_mode("bot-scout")
    await be.set_browser_mode("bot-scout", "human", "https://example.com/")
    await be.browser_mode_ready("bot-scout", "human")
    await be.browser_merge_status("bot-scout")
    await be.browser_tab_url("bot-scout")
    calls = runner.ran("exec")
    assert len(calls) == 5
    for call in calls:
        assert call[:2] == ["docker", "exec"] and "--user" in call
        assert call[call.index("--user") + 1] == "1001:1001"
        assert ["-e", "HOME=/home/browser"] == call[call.index("-e"):call.index("-e") + 2]
        assert da.BOT_GUARD not in call and "1000:1000" not in call
        assert "bot-scout" in call


TOKEN = "0123456789abcdef0123456789abcdef"
OTHER = "fedcba9876543210fedcba9876543210"


async def test_get_browser_mode_parses_and_distrusts_other_content(be, runner):
    lines = ["human\n", "bot\n", "invalid\n", "--remote-debugging-port=9222\n", "", f"human {TOKEN}\n", "human nothex\n",
             f"bot {TOKEN}\n", f"human {TOKEN} extra\n", f"human {TOKEN[:31]}\n", f"human {TOKEN.upper()}\n"]
    runner.seq("exec", results=[CmdResult(0, line, "") for line in lines])
    assert [await be.get_browser_mode("bot-scout") for _ in lines] == [
        "human", "bot", "invalid", "invalid", "invalid", "human", "invalid", "invalid", "invalid", "invalid", "invalid"]


async def test_get_browser_mode_failure_is_a_backend_error(be, runner):
    runner.on("exec", rc=1, err="Error response from daemon: container is not running")
    with pytest.raises(BackendError, match="режима браузера"):
        await be.get_browser_mode("bot-scout")


async def test_set_browser_mode_human_sends_the_url_on_stdin_and_never_in_argv(be, runner):
    seen = []
    original = runner.__call__

    async def spy(argv, *, input=None, env=None):
        seen.append((list(argv), input))
        return await original(argv, input=input, env=env)

    be._run = spy
    url = "https://example.com/reset?token=s3cret"
    await be.set_browser_mode("bot-scout", "human", url)
    argv, stdin = seen[0]
    first, token, rest = stdin.split("\n")
    assert first == url and rest == "" and re.fullmatch(r"[0-9a-f]{32}", token)  # the session token goes on stdin too
    assert url not in " ".join(argv) and "s3cret" not in " ".join(argv) and token not in " ".join(argv)
    assert "-i" in argv[:4] and argv[-1] == "human"
    script, _ = _script(argv)
    assert "remote-debugging" not in script
    # адрес, затем режим, оба через rename: супервизор не увидит human без адреса
    assert script.index(".browser-human-url") < script.index('.browser-mode.tmp')
    assert script.count("mv -f") == 2 and "umask 077" in script


async def test_set_browser_mode_bot_removes_the_url_file_and_needs_no_stdin(be, runner):
    seen = []
    original = runner.__call__

    async def spy(argv, *, input=None, env=None):
        seen.append((list(argv), input))
        return await original(argv, input=input, env=env)

    be._run = spy
    await be.set_browser_mode("bot-scout", "bot", None)
    argv, stdin = seen[0]
    assert stdin is None and "-i" not in argv[:4] and argv[-1] == "bot"
    assert 'rm -f "$HOME/.browser-human-url"' in _script(argv)[0]


async def test_set_browser_mode_rejects_other_modes_and_reports_docker_failure(be, runner):
    with pytest.raises(BackendError):
        await be.set_browser_mode("bot-scout", "both", None)
    assert not runner.calls
    runner.on("exec", rc=1, err="No space left on device")
    with pytest.raises(BackendError, match="No space"):
        await be.set_browser_mode("bot-scout", "human", "https://example.com/")


async def test_mode_scripts_really_write_and_read_the_state_files(be, runner, tmp_path, monkeypatch):
    monkeypatch.setattr(be, "_new_session_token", lambda: TOKEN)
    runner.on("exec")
    await be.get_browser_mode("bot-scout")
    await be.set_browser_mode("bot-scout", "human", "https://example.com/a?b=1")
    await be.set_browser_mode("bot-scout", "bot", None)
    get_call, set_human, set_bot = runner.ran("exec")
    assert _run_sh(get_call, tmp_path).stdout == "bot\n"  # нет файла: bot
    done = _run_sh(set_human, tmp_path, f"https://example.com/a?b=1\n{TOKEN}\n")
    assert done.returncode == 0, done.stderr
    assert (tmp_path / ".browser-mode").read_text() == f"human {TOKEN}\n"
    assert (tmp_path / ".browser-human-url").read_text() == "https://example.com/a?b=1\n"
    assert (tmp_path / ".browser-mode").stat().st_mode & 0o777 == 0o600
    assert _run_sh(get_call, tmp_path).stdout == f"human {TOKEN}\n"
    assert not list(tmp_path.glob("*.tmp"))
    done = _run_sh(set_bot, tmp_path)
    assert done.returncode == 0, done.stderr
    assert (tmp_path / ".browser-mode").read_text() == "bot\n"
    assert not (tmp_path / ".browser-human-url").exists()
    (tmp_path / ".browser-mode").write_text("garbage\n")
    assert _run_sh(get_call, tmp_path).stdout == "garbage\n"  # парсер на стороне Python: invalid
    (tmp_path / ".browser-mode").unlink()
    (tmp_path / "elsewhere").write_text("bot\n")
    (tmp_path / ".browser-mode").symlink_to(tmp_path / "elsewhere")
    assert _run_sh(get_call, tmp_path).stdout == "invalid\n"


async def test_human_token_is_kept_while_the_session_runs_and_new_after_a_return(be, runner, tmp_path):
    runner.on("exec")
    await be.set_browser_mode("bot-scout", "human", "https://example.com/")
    await be.set_browser_mode("bot-scout", "bot", None)
    set_human, set_bot = runner.ran("exec")
    mode = tmp_path / ".browser-mode"
    assert _run_sh(set_human, tmp_path, f"https://a.example/\n{TOKEN}\n").returncode == 0
    # the same session asked again (the supervisor was not ready, the address changed): the token stays, the directory resumes
    assert _run_sh(set_human, tmp_path, f"https://b.example/\n{OTHER}\n").returncode == 0
    assert mode.read_text() == f"human {TOKEN}\n"
    assert (tmp_path / ".browser-human-url").read_text() == "https://b.example/\n"
    assert _run_sh(set_bot, tmp_path).returncode == 0
    assert mode.read_text() == "bot\n"
    # the next takeover is a new session with a new token
    assert _run_sh(set_human, tmp_path, f"https://a.example/\n{OTHER}\n").returncode == 0
    assert mode.read_text() == f"human {OTHER}\n"


async def test_human_token_is_not_taken_from_a_broken_mode_file_and_a_bad_token_writes_nothing(be, runner, tmp_path):
    runner.on("exec")
    await be.set_browser_mode("bot-scout", "human", "https://example.com/")
    (set_human,) = runner.ran("exec")
    mode = tmp_path / ".browser-mode"
    for broken in ("human nothex\n", f"human {TOKEN} extra\n", f"bot {TOKEN}\n", "garbage\n", ""):
        mode.write_text(broken)
        assert _run_sh(set_human, tmp_path, f"https://a.example/\n{OTHER}\n").returncode == 0
        assert mode.read_text() == f"human {OTHER}\n", broken
    mode.unlink()
    (tmp_path / "elsewhere").write_text(f"human {TOKEN}\n")
    mode.symlink_to(tmp_path / "elsewhere")
    assert _run_sh(set_human, tmp_path, f"https://a.example/\n{OTHER}\n").returncode == 0
    assert not mode.is_symlink() and mode.read_text() == f"human {OTHER}\n"  # a link is replaced, not followed
    assert (tmp_path / "elsewhere").read_text() == f"human {TOKEN}\n"
    mode.unlink()
    for bad in ("", "nothex", TOKEN[:31], TOKEN + "0", TOKEN.upper()):
        done = _run_sh(set_human, tmp_path, f"https://a.example/\n{bad}\n")
        assert done.returncode != 0 and not mode.exists(), bad


async def test_ready_script_reads_the_mode_from_a_line_with_a_token(be, runner, tmp_path):
    await be.browser_mode_ready("bot-scout", "human")
    script, _ = _script(runner.ran("exec")[0])
    marker = '[ "$cur" = "$m" ] || exit 1;'
    assert marker in script
    head = script[:script.index(marker)] + 'echo "[$cur]"'
    mode = tmp_path / ".browser-mode"
    for text, want in ((f"human {TOKEN}\n", "[human]"), ("human\n", "[human]"), ("bot\n", "[bot]"), ("", "[]")):
        mode.write_text(text)
        done = subprocess.run(["sh", "-c", head, "_", "human"], capture_output=True, text=True,
                              env={"HOME": str(tmp_path), "PATH": os.environ["PATH"]}, check=False)
        assert done.stdout.strip() == want, (text, done.stdout, done.stderr)


async def test_browser_mode_ready_script_checks_the_mode_and_the_process(be, runner):
    runner.seq("exec", results=[CmdResult(0, "", ""), CmdResult(1, "", ""), CmdResult(2, "", "daemon error")])
    assert await be.browser_mode_ready("bot-scout", "human") is True
    assert await be.browser_mode_ready("bot-scout", "bot") is False
    with pytest.raises(BackendError, match="режима браузера"):
        await be.browser_mode_ready("bot-scout", "human")
    script, args = _script(runner.ran("exec")[0])
    assert args == ["human"]
    for needle in ('.browser-mode', '.browser-active', '.browser-ready', 'chromium-supervisor.sh',
                   '*remote-debugging*) exit 1', '*botstead-browser-human*', '-ge 2',
                   '127.0.0.1:9222/json/version', 'remote-debugging-port=9222'):
        assert needle in script, needle
    # human: ответ CDP считается провалом, bot: отсутствие ответа
    assert 'if curl' in script and '|| exit 1; fi' in script
    with pytest.raises(BackendError):
        await be.browser_mode_ready("bot-scout", "x")


async def test_browser_merge_status_and_tab_url(be, runner):
    runner.seq("exec", results=[
        CmdResult(0, "merged 3\n", ""),
        CmdResult(0, json.dumps([{"type": "service_worker", "url": "https://x/sw.js"},
                                 {"type": "page", "url": "https://example.com/page?q=1"},
                                 {"type": "page", "url": "https://other/"}]), ""),
        CmdResult(0, json.dumps([{"type": "background_page", "url": "x"}]), ""),
        CmdResult(7, "", "curl: (7) Failed to connect"),
        CmdResult(0, "not json", ""),
        CmdResult(0, json.dumps({"type": "page"}), "")])
    assert await be.browser_merge_status("bot-scout") == "merged 3"
    assert await be.browser_tab_url("bot-scout") == "https://example.com/page?q=1"
    assert await be.browser_tab_url("bot-scout") is None  # вкладок типа page нет
    assert await be.browser_tab_url("bot-scout") is None  # браузер не отвечает
    assert await be.browser_tab_url("bot-scout") is None
    assert await be.browser_tab_url("bot-scout") is None
    assert "127.0.0.1:9222/json/list" in _script(runner.ran("exec")[1])[0]


def _targets(*pairs):
    return CmdResult(0, json.dumps([{"type": kind, "url": url} for kind, url in pairs]), "")


async def test_browser_tab_skips_service_surfaces_and_reports_a_blocked_new_tab_as_about_blank(be, runner):
    service = [("page", "chrome://omnibox-popup.top-chrome/"), ("page", "chrome://omnibox-popup.top-chrome/omnibox_popup_aim.html")]
    runner.seq("exec", results=[
        # свежий бот: служебные поверхности и заблокированная политикой новая вкладка
        _targets(*service, ("page", "chrome://newtab/")),
        _targets(("page", "chrome://new-tab-page/")),
        # служебные поверхности пропускаются, даже когда идут первыми
        _targets(*service, ("page", "https://example.com/page?q=1")),
        _targets(("page", "chrome://settings/"), ("page", "https://example.com/a"), ("page", "https://example.com/b")),
        # вкладок нет: расширения, devtools, страница ошибки, чужие типы целей
        _targets(("page", "chrome-extension://abc/p.html"), ("page", "devtools://devtools/bundled/x.html"),
                 ("page", "chrome-untrusted://x/"), ("page", "chrome-error://chromewebdata/"), ("service_worker", "https://x/sw.js"),
                 ("page", "chrome://settings/")),
        # target без адреса или не строка
        CmdResult(0, json.dumps([{"type": "page"}, {"type": "page", "url": 5}]), "")])
    assert await be.browser_tab_url("bot-scout") == "about:blank"
    assert await be.browser_tab_url("bot-scout") == "about:blank"
    assert await be.browser_tab_url("bot-scout") == "https://example.com/page?q=1"
    assert await be.browser_tab_url("bot-scout") == "https://example.com/a"
    assert await be.browser_tab_url("bot-scout") is None
    assert await be.browser_tab_url("bot-scout") is None

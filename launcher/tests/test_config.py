import pytest

from bothub_launcher.config import Config, ConfigError, load_config

ENV = {"LAUNCHER_SECRET": "x" * 40, "BOT_TOKEN_SECRET": "bts"}


def write(tmp_path, text):
    p = tmp_path / "launcher.toml"
    p.write_text(text)
    return str(p)


def test_defaults_match_the_spec():
    cfg = load_config(None, ENV)
    assert cfg.image == "bothub-bot" and cfg.user == "1000:1000" and cfg.runtime is None
    assert (cfg.limits.memory, cfg.limits.cpus, cfg.limits.pids) == ("2g", "2", 512)
    assert cfg.api_port == 8080 and cfg.core_container == "bothub-core" and cfg.core_gw_priority == -100
    assert cfg.label_key == "bothub.managed"
    assert "10.0.0.0/8" in cfg.blocked_v4 and "169.254.0.0/16" in cfg.blocked_v4 and "100.64.0.0/10" in cfg.blocked_v4
    assert "fc00::/7" in cfg.blocked_v6 and "fe80::/10" in cfg.blocked_v6
    assert cfg.max_screen_sessions == 32 and cfg.screen_idle == 900 and cfg.screen_connect_timeout == 5


def test_screen_session_limits_must_be_positive():
    with pytest.raises(ConfigError):
        Config(secret="x" * 40, max_screen_sessions=0)
    with pytest.raises(ConfigError):
        Config(secret="x" * 40, screen_idle=0)
    with pytest.raises(ConfigError):
        Config(secret="x" * 40, screen_connect_timeout=0)
    with pytest.raises(ConfigError):
        Config(secret="x" * 40, screen_connect_timeout=float("inf"))
    with pytest.raises(ConfigError):
        Config(secret="x" * 40, screen_connect_timeout=float("nan"))


def test_secret_is_required_and_long_enough():
    with pytest.raises(ConfigError):
        load_config(None, {"BOT_TOKEN_SECRET": "x"})
    with pytest.raises(ConfigError):
        load_config(None, {"LAUNCHER_SECRET": "short", "BOT_TOKEN_SECRET": "x"})


def test_secret_not_in_repr():
    assert "x" * 40 not in repr(load_config(None, ENV))


def test_bot_token_secret_required():
    with pytest.raises(ConfigError):
        load_config(None, {"LAUNCHER_SECRET": "x" * 40})


def test_env_overrides_paths():
    cfg = load_config(None, {**ENV, "LAUNCHER_SOCKET": "/run/x.sock", "LAUNCHER_RUNTIME": "runsc"})
    assert cfg.socket_path == "/run/x.sock" and cfg.runtime == "runsc"


def test_toml_file(tmp_path):
    path = write(tmp_path, '''
image = "bothub-bot:2026-10"
runtime = "runsc"
core_container = "core-1"
api_port = 9090
core_gw_priority = -250
dns = ["9.9.9.9"]

[limits]
memory = "4g"
cpus = "1.5"
pids = 256

[[allow]]
cidr = "192.168.1.5/32"
proto = "tcp"
port = 11434
comment = "Ollama"

[[host_allow]]
cidr = "10.9.0.1/32"
port = 9100
''')
    cfg = load_config(path, ENV)
    assert cfg.image == "bothub-bot:2026-10" and cfg.runtime == "runsc" and cfg.api_port == 9090
    assert cfg.core_gw_priority == -250
    assert cfg.limits.memory == "4g" and cfg.limits.cpus == "1.5" and cfg.limits.pids == 256
    assert cfg.allow[0].cidr == "192.168.1.5/32" and cfg.allow[0].port == 11434
    assert cfg.host_allow[0].port == 9100 and cfg.dns == ("9.9.9.9",)


def test_unknown_toml_key_rejected(tmp_path):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, 'imagee = "x"'), ENV)
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, '[limits]\nmemmory = "1g"'), ENV)


def test_missing_explicit_config_file_is_an_error(tmp_path):
    with pytest.raises(ConfigError):
        load_config(str(tmp_path / "nope.toml"), ENV)


@pytest.mark.parametrize("toml", [
    'image = "bad image"', 'image = "-evil"', 'image = ""',
    'runtime = "runsc --privileged"', 'runtime = "-x"',
    '[limits]\nmemory = "2g; reboot"', '[limits]\nmemory = "lots"', '[limits]\ncpus = "-1"', '[limits]\npids = 0',
    '[limits]\nshm_size = "1 g"',
    'blocked_v4 = ["not-a-cidr"]', 'blocked_v4 = ["fc00::/7"]', 'blocked_v6 = ["10.0.0.0/8"]',
    'dns = ["1.1.1.1; reboot"]', 'dns = ["name.example"]',
    'api_port = 0', 'api_port = 70000',
    'core_gw_priority = "low"', 'core_gw_priority = true', 'core_gw_priority = -100.5', 'core_gw_priority = [-100]', 'core_gw_priority = 100', 'core_gw_priority = 500',
    'user = "0:0"', 'user = "root"',
    'label_key = "bad key"', 'core_container = "-x"', 'core_alias = "a b"',
    '[[allow]]\ncidr = "300.0.0.0/8"',
])
def test_invalid_values_rejected(tmp_path, toml):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, toml), ENV)


@pytest.mark.parametrize("value", ["-100", True, 1.5, None])
def test_core_gw_priority_must_be_an_integer(value):
    with pytest.raises(ConfigError, match="core_gw_priority"):
        Config(secret="x" * 40, bot_token_secret="bts", core_gw_priority=value)


def test_config_direct_construction_validates():
    with pytest.raises(ValueError):
        Config(secret="x" * 40, image="a b")
    with pytest.raises(ValueError):
        Config(secret="short")

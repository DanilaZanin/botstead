import pytest

from bothub_mac.config import Config, ConfigError


def test_load_from_env(tmp_path):
    env = {"BOTHUB_URL": "https://bots.example.com", "MAC_AGENT_TOKEN": "secret"}
    config = Config.load(env=env, env_file=tmp_path / "missing.env")
    assert config.bothub_url == "https://bots.example.com"
    assert config.mac_agent_token == "secret"
    assert config.ws_url == "wss://bots.example.com/agent/mac?token=secret"


def test_load_from_file_fallback(tmp_path):
    env_file = tmp_path / "config.env"
    env_file.write_text('BOTHUB_URL=https://bots.example.com\nMAC_AGENT_TOKEN="file-token"\n')
    config = Config.load(env={}, env_file=env_file)
    assert config.mac_agent_token == "file-token"


def test_env_takes_precedence_over_file(tmp_path):
    env_file = tmp_path / "config.env"
    env_file.write_text("BOTHUB_URL=https://from-file\nMAC_AGENT_TOKEN=from-file\n")
    config = Config.load(env={"MAC_AGENT_TOKEN": "from-env"}, env_file=env_file)
    assert config.mac_agent_token == "from-env"
    assert config.bothub_url == "https://from-file"


def test_missing_values_raise(tmp_path):
    with pytest.raises(ConfigError):
        Config.load(env={}, env_file=tmp_path / "missing.env")


def test_trailing_slash_stripped(tmp_path):
    config = Config.load(
        env={"BOTHUB_URL": "https://bots.example.com/", "MAC_AGENT_TOKEN": "t"},
        env_file=tmp_path / "missing.env",
    )
    assert config.bothub_url == "https://bots.example.com"


def test_ws_url_keeps_subpath():
    from bothub_mac.config import _http_to_ws
    assert _http_to_ws("https://bots.example.com/bots") == "wss://bots.example.com/bots/agent/mac"
    assert _http_to_ws("http://127.0.0.1:8000/") == "ws://127.0.0.1:8000/agent/mac"


def test_bothub_urls_parsed_in_priority_order(tmp_path):
    env = {
        "BOTHUB_URLS": "https://bots.example.com/bots, http://127.0.0.1:18080/",
        "MAC_AGENT_TOKEN": "t",
    }
    config = Config.load(env=env, env_file=tmp_path / "missing.env")
    assert config.urls == ("https://bots.example.com/bots", "http://127.0.0.1:18080")
    assert config.bothub_url == "https://bots.example.com/bots"
    assert config.current.url == "https://bots.example.com/bots"


def test_bothub_url_falls_back_to_single_item_list(tmp_path):
    config = Config.load(
        env={"BOTHUB_URL": "https://bots.example.com", "MAC_AGENT_TOKEN": "t"},
        env_file=tmp_path / "missing.env",
    )
    assert config.urls == ("https://bots.example.com",)


def test_bothub_urls_from_file_fallback(tmp_path):
    env_file = tmp_path / "config.env"
    env_file.write_text("BOTHUB_URLS=https://a.example.com,https://b.example.com\nMAC_AGENT_TOKEN=t\n")
    config = Config.load(env={}, env_file=env_file)
    assert config.urls == ("https://a.example.com", "https://b.example.com")


def test_ws_url_for_arbitrary_address():
    config = Config(bothub_url="https://a", mac_agent_token="tok", bothub_urls=("https://a", "https://b"))
    assert config.ws_url_for("https://b") == "wss://b/agent/mac?token=tok"

import pytest

from bothub_launcher.__main__ import main, parse_listen
from bothub_launcher.config import ConfigError


def test_default_is_the_configured_unix_socket():
    assert parse_listen(None, "/run/x.sock") == {"uds": "/run/x.sock"}
    assert parse_listen("", "/run/x.sock") == {"uds": "/run/x.sock"}


def test_explicit_unix_and_tcp():
    assert parse_listen("unix:/tmp/a.sock", "/run/x.sock") == {"uds": "/tmp/a.sock"}
    assert parse_listen("tcp://127.0.0.1:8099", "/run/x.sock") == {"host": "127.0.0.1", "port": 8099}


@pytest.mark.parametrize("value", ["tcp://host", "tcp://:80", "tcp://h:0", "tcp://h:99999", "http://h:1", "unix:", "x"])
def test_bad_listen_value(value):
    with pytest.raises(ConfigError):
        parse_listen(value, "/run/x.sock")


def test_main_fails_cleanly_without_secret(monkeypatch, capsys):
    for k in ("LAUNCHER_SECRET", "BOT_TOKEN_SECRET", "LAUNCHER_CONFIG"):
        monkeypatch.delenv(k, raising=False)
    assert main() == 2
    assert "bothub-launcher" in capsys.readouterr().err

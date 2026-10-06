import pytest

from bothub_launcher import validation as v
from bothub_launcher.errors import ValidationFailed

BAD_IDS = [
    "", "-x", "--privileged", "A", "a_b", "a b", "a;b", "a/b", "../x", "a\n", "a\x00", "a$(id)",
    "a`id`", "а", "a.b", None, 5, ["a"],
]


@pytest.mark.parametrize("value", ["a", "mac", "scout-2", "0abc", "a" * 32, "a-"])
def test_bot_id_good(value):
    assert v.validate_bot_id(value) == value


@pytest.mark.parametrize("value", BAD_IDS + ["a" * 33])
def test_bot_id_bad(value):
    with pytest.raises(ValidationFailed):
        v.validate_bot_id(value)


@pytest.mark.parametrize("value", ["u1", "dev-owner", "3f2b8c1e-5d1a-4c6e-9b7a-0123456789ab", "a" * 40])
def test_owner_id_good(value):
    assert v.validate_owner_id(value) == value


@pytest.mark.parametrize("value", BAD_IDS + ["a" * 41, "Owner"])
def test_owner_id_bad(value):
    with pytest.raises(ValidationFailed):
        v.validate_owner_id(value)


@pytest.mark.parametrize("value", ["turn-1", "A_b-9", "a" * 64, "0"])
def test_exec_id_good(value):
    assert v.validate_exec_id(value) == value


@pytest.mark.parametrize("value", ["", "-x", "a b", "a/b", "a;b", "a" * 65, "a\n", None])
def test_exec_id_bad(value):
    with pytest.raises(ValidationFailed):
        v.validate_exec_id(value)


def test_argv_good():
    assert v.validate_argv(["claude", "-p", "--model", "x y;z"]) == ["claude", "-p", "--model", "x y;z"]


@pytest.mark.parametrize("argv", [
    [], "claude -p", ["claude", 1], ["a\x00b"], [""], ["--privileged"], ["x"] * 257, ["x", "y" * 200_000],
])
def test_argv_bad(argv):
    with pytest.raises(ValidationFailed):
        v.validate_argv(argv)


def test_env_good():
    env = {"BOTHUB_TURN_ID": "t1", "ANTHROPIC_BASE_URL": "http://core:8080/gw"}
    assert v.validate_env(env) == env


@pytest.mark.parametrize("env", [
    {"foo": "x"}, {"1A": "x"}, {"A-B": "x"}, {"LD_PRELOAD": "/x.so"}, {"LD_LIBRARY_PATH": "/x"},
    {"DOCKER_HOST": "tcp://x"}, {"PATH": "/x"}, {"HOME": "/x"}, {"BASH_ENV": "/x"}, {"IFS": "x"},
    {"A": "x\x00y"}, {"A": 1}, {"A": "x" * 20_000}, {f"V{i}": "x" for i in range(65)}, ["A=1"],
])
def test_env_bad(env):
    with pytest.raises(ValidationFailed):
        v.validate_env(env)


def test_env_none_is_empty():
    assert v.validate_env(None) == {}


def test_stdin_limit():
    assert v.validate_stdin(None) == b""
    assert v.validate_stdin("привет") == "привет".encode()
    with pytest.raises(ValidationFailed):
        v.validate_stdin("x" * (v.STDIN_MAX + 1))


@pytest.mark.parametrize("cols,rows", [(80, 24), (1, 1), (500, 500)])
def test_term_size_good(cols, rows):
    assert v.validate_term_size(cols, rows) == (cols, rows)


@pytest.mark.parametrize("cols,rows", [(0, 24), (80, 0), (501, 24), (-1, 5), ("80", 24), (True, 24)])
def test_term_size_bad(cols, rows):
    with pytest.raises(ValidationFailed):
        v.validate_term_size(cols, rows)


@pytest.mark.parametrize("mode", ["bot", "human"])
def test_browser_mode_good(mode):
    assert v.validate_browser_mode(mode) == mode


@pytest.mark.parametrize("mode", ["", "Human", "bot ", "human\n", None, 1, ["bot"]])
def test_browser_mode_bad(mode):
    with pytest.raises(ValidationFailed):
        v.validate_browser_mode(mode)


@pytest.mark.parametrize("url", ["https://example.com/", "http://example.com:8080/a?b=1#c", "about:blank"])
def test_browser_url_good(url):
    assert v.validate_browser_url(url) == url


def test_browser_url_none_is_blank():
    assert v.validate_browser_url(None) == "about:blank"


@pytest.mark.parametrize("url", [
    "", "-x", "--remote-debugging-port=9222", "javascript:alert(1)", "file:///etc/passwd", "chrome://settings",
    "data:text/html,x", "view-source:https://a.com", "https://", "https://a b", "https://a.com/\n", "https://a.com/\x00",
    "https://a.com/\x7f", "http://a.com\\b", " https://a.com", "ftp://a.com", "https://e.com/" + "a" * 2100, 5,
    ["https://a.com"],
])
def test_browser_url_bad(url):
    with pytest.raises(ValidationFailed):
        v.validate_browser_url(url)

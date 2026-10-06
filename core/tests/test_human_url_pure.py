"""The address the human gets, round 3: the full list of hidden characters and hosts where IDNA 2003 (Python) and
UTS 46 (Chromium) disagree. No Postgres required."""
import ast
import re
from pathlib import Path
from urllib.parse import quote

import pytest

from bothub import browser_control
from bothub.browser_control import human_url

pytestmark = pytest.mark.pure

EXTRA_HIDDEN = ["\u00ad", "\u034f", "\u061c", "\u115f", "\u1160", "\u17b4", "\u17b5", "\u180b", "\u180c", "\u180d", "\u180e",
                "\u180f", "\u2028", "\u2029", "\u2060", "\u2061", "\u2062", "\u2063", "\u2064", "\u206a", "\u206b", "\u206c",
                "\u206d", "\u206e", "\u206f", "\u3164", "\ufe00", "\ufe08", "\ufe0f", "\uffa0", "\ufff9", "\ufffa", "\ufffb",
                "\U0001d173", "\U0001d17a", "\U000e0000", "\U000e0001", "\U000e0020", "\U000e0fff"]


@pytest.mark.parametrize("char", EXTRA_HIDDEN)
@pytest.mark.parametrize("template", ["https://example.com/a{c}b", "https://exa{c}mple.com/", "https://example.com/?q={c}",
                                      "https://example.com/#{c}", "https://example.com:8080/{c}"])
def test_human_url_refuses_the_extended_hidden_characters(char, template):
    assert human_url(template.format(c=char)) is None


@pytest.mark.parametrize("char", ["\u202e", "\u200b", "\u00ad", "\u2060", "\ufe0f", "\U000e0001", "\u3164"])
def test_human_url_refuses_a_hidden_character_in_a_percent_encoded_host(char):
    assert human_url(f"https://exa{quote(char)}mple.com/") is None


def _literal(path, name):
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in {path}")


def test_hidden_character_list_is_written_as_escapes():
    core_file = Path(browser_control.__file__)
    assert not re.search("[\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180b-\u180f\u200b-\u200f\u2028-\u202e\u2060-\u206f"
                         "\u3164\ufe00-\ufe0f\ufeff\uffa0\ufff9-\ufffb\U0001d173-\U0001d17a\U000e0000-\U000e0fff]",
                         core_file.read_text()), "the source must hold the characters as escapes"
    assert _literal(core_file, "HIDDEN_URL_CHARS") == browser_control.HIDDEN_URL_CHARS


def test_hidden_character_list_matches_the_launcher():
    launcher_file = Path(browser_control.__file__).parents[2] / "launcher/bothub_launcher/validation.py"
    if not launcher_file.exists():
        pytest.skip("no launcher next to core")
    assert _literal(launcher_file, "HIDDEN_URL_CHARS") == browser_control.HIDDEN_URL_CHARS


@pytest.mark.parametrize("url", [
    "https://fa\u00df.de/",                         # Python: fass.de, Chromium: xn--fa-hia.de
    "https://\u03b2\u03cc\u03bb\u03bf\u03c2.com/",  # final sigma
    "https://\u1e9e.de/",                           # capital sharp s
    "https://fa%C3%9F.de/",                         # percent-encoded sharp s
])
def test_human_url_refuses_hosts_where_idna_2003_and_uts46_differ(url):
    assert human_url(url) is None


@pytest.mark.parametrize("url,expected", [
    ("https://\u043f\u0440\u0438\u043c\u0435\u0440.\u0440\u0444/", "https://xn--e1afmkfd.xn--p1ai/"),
    ("https://\u041f\u0420\u0418\u041c\u0415\u0420.\u0420\u0424/x", "https://xn--e1afmkfd.xn--p1ai/x"),
    ("https://b\u00fccher.example/", "https://xn--bcher-kva.example/"),
    ("https://\u043f\u0440\u0438\u043c\u0435\u0440.\u0440\u0444:8443/\u043f\u0443\u0442\u044c?q=\u044f#\u0444",
     "https://xn--e1afmkfd.xn--p1ai:8443/\u043f\u0443\u0442\u044c?q=\u044f#\u0444"),
    ("http://example.com:8080/a/b?x=1&y=\u044f#\u0444", "http://example.com:8080/a/b?x=1&y=\u044f#\u0444"),
    ("https://xn--fa-hia.de/", "https://xn--fa-hia.de/"),
    ("https://example.com./", "https://example.com./"),
    ("https://[2001:4860:4860::8888]:8443/p", "https://[2001:4860:4860::8888]:8443/p"),
    ("https://%D0%BF%D1%80%D0%B8%D0%BC%D0%B5%D1%80.%D1%80%D1%84/", "https://xn--e1afmkfd.xn--p1ai/"),
])
def test_human_url_keeps_ordinary_addresses(url, expected):
    assert human_url(url) == expected

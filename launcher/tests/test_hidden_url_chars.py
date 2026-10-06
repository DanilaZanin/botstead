"""The launcher's second check of the human's address: the same hidden-character list as the core, in every part of the
address and in a percent-encoded host."""
import ast
import re
from pathlib import Path
from urllib.parse import quote

import pytest

from bothub_launcher import validation as v
from bothub_launcher.errors import ValidationFailed

# Code points, not characters: the test source holds no invisible character either.
EXTRA_HIDDEN = [0x00AD, 0x034F, 0x061C, 0x115F, 0x1160, 0x17B4, 0x17B5, 0x180B, 0x180C, 0x180D, 0x180E, 0x180F, 0x2028,
                0x2029, 0x2060, 0x2061, 0x2062, 0x2063, 0x2064, 0x206A, 0x206B, 0x206C, 0x206D, 0x206E, 0x206F, 0x3164,
                0xFE00, 0xFE08, 0xFE0F, 0xFFA0, 0xFFF9, 0xFFFA, 0xFFFB, 0x1D173, 0x1D17A, 0xE0000, 0xE0001, 0xE0020, 0xE0FFF]


@pytest.mark.parametrize("cp", EXTRA_HIDDEN)
@pytest.mark.parametrize("template", ["https://example.com/a{c}b", "https://exa{c}mple.com/", "https://example.com/?q={c}",
                                      "https://example.com/#{c}", "https://example.com:8080/{c}"])
def test_validation_refuses_the_extended_hidden_characters(cp, template):
    with pytest.raises(ValidationFailed):
        v.validate_browser_url(template.format(c=chr(cp)))


@pytest.mark.parametrize("cp", [0x202E, 0x200B, 0x00AD, 0x2060, 0xFE0F, 0xE0001, 0x3164])
def test_validation_refuses_a_hidden_character_in_a_percent_encoded_host(cp):
    with pytest.raises(ValidationFailed):
        v.validate_browser_url(f"https://exa{quote(chr(cp))}mple.com/")


def test_validation_keeps_ordinary_addresses():
    for url in ("https://example.com/", "http://example.com:8080/a/b?x=1#f", "about:blank",
                "https://xn--e1afmkfd.xn--p1ai/", "https://exa%6Dple.com/"):
        assert v.validate_browser_url(url) == url


def test_hidden_character_list_is_written_as_escapes_and_matches_the_core():
    source = Path(v.__file__).read_text()
    assert not re.search(f"[{v.HIDDEN_URL_CHARS}]", source), "the source must hold the characters as escapes"

    def literal(text):
        for node in ast.parse(text).body:
            if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "HIDDEN_URL_CHARS" for t in node.targets):
                return ast.literal_eval(node.value)
        raise AssertionError("HIDDEN_URL_CHARS not found")

    assert literal(source) == v.HIDDEN_URL_CHARS
    core_file = Path(v.__file__).resolve().parents[2] / "core/bothub/browser_control.py"
    if not core_file.exists():
        pytest.skip("no core next to launcher")
    assert literal(core_file.read_text()) == v.HIDDEN_URL_CHARS

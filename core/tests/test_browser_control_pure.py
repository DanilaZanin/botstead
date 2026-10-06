"""Stage 6 state and RFB framing checks; no Postgres required."""
import struct
import random

import pytest

from bothub.browser_control import RFBClientFilter, RFBProtocolError, transition
from bothub.main import browser_safe_url
from bothub.mcp_server import PLAYWRIGHT_MCP_COMMAND


pytestmark = pytest.mark.pure


def test_browser_three_state_machine():
    assert transition('bot', 'takeover') == 'human'
    assert transition('human', 'return') == 'returning'
    assert transition('returning', 'takeover') == 'human'
    assert transition('returning', 'snapshot') == 'bot'
    with pytest.raises(ValueError, match='invalid_transition'):
        transition('human', 'snapshot')


def test_rfb_handshake_fragmented_and_input_filtered():
    parser = RFBClientFilter()
    assert parser.feed(b'RFB 003.', allow_input=False) == b''
    assert parser.feed(b'008\n\x01\x01', allow_input=False) == b'RFB 003.008\n\x01\x01'
    key = struct.pack('!BBHI', 4, 1, 0, 65)
    pointer = b'\x05\x01\x00\x10\x00\x20'
    cut = b'\x06\x00\x00\x00' + struct.pack('!I', 3) + b'abc'
    request = b'\x03\x00\x00\x00\x00\x00\x00\x00\x00\x00'
    assert parser.feed(key[:3], allow_input=False) == b''
    assert parser.feed(key[3:] + pointer + cut + request, allow_input=False) == request
    assert parser.feed(key + pointer + cut, allow_input=True) == key + pointer + cut


def test_rfb_rejects_oversized_cut_text_and_unknown_messages():
    parser = RFBClientFilter()
    parser.feed(b'RFB 003.008\n\x01\x01', allow_input=False)
    with pytest.raises(RFBProtocolError, match='too_large'):
        parser.feed(b'\x06\0\0\0' + struct.pack('!I', 1024 * 1024 + 1), allow_input=True)
    parser = RFBClientFilter()
    parser.feed(b'RFB 003.008\n\x01\x01', allow_input=False)
    with pytest.raises(RFBProtocolError, match='unknown'):
        parser.feed(b'\xff', allow_input=True)


def test_rfb_malformed_input_is_bounded():
    random_source = random.Random(6)
    for _ in range(500):
        parser = RFBClientFilter()
        parser.feed(b'RFB 003.008\n\x01\x01', allow_input=False)
        payload = random_source.randbytes(random_source.randrange(0, 2048))
        # Include malicious declared lengths as well as random message types.
        if random_source.randrange(2):
            payload = b'\x06\x00\x00\x00' + random_source.randbytes(4) + payload
        start = 0
        while start < len(payload):
            size = random_source.randrange(1, 65)
            try:
                parser.feed(payload[start:start + size], allow_input=False)
            except RFBProtocolError:
                break
            assert len(parser.buffer) <= parser.MAX_BUFFER
            start += size


def test_browser_audit_url_strips_credentials_and_query():
    assert browser_safe_url('https://user:pass@example.com/private?token=secret#frag') == 'https://example.com/private'
    assert browser_safe_url('javascript:alert(1)') == '[redacted]'
    assert PLAYWRIGHT_MCP_COMMAND == 'playwright-mcp'


async def test_fake_screen_session_owner_and_input_limit():
    from bothub.launcher_client import FakeLauncherClient, LauncherForbidden, LauncherInvalid

    fake = FakeLauncherClient()
    await fake.create_bot('scout', 'owner-a')
    with pytest.raises(LauncherForbidden):
        await fake.open_screen_session('scout', 'owner-b')
    sid = await fake.open_screen_session('scout', 'owner-a')
    await fake.screen_input(sid, b'RFB 003.008\n')
    assert fake.screen_inputs[sid] == [b'RFB 003.008\n']
    with pytest.raises(LauncherInvalid):
        await fake.screen_input(sid, b'x' * 65537)
    await fake.close_screen_session(sid)
    assert sid not in fake.screen_inputs


# --- The address the human gets: no hidden or direction-changing characters, the host as punycode -----------

HIDDEN = ["\u202a", "\u202b", "\u202c", "\u202d", "\u202e", "\u2066", "\u2067", "\u2068", "\u2069",
          "\u200b", "\u200c", "\u200d", "\u200e", "\u200f", "\ufeff"]


@pytest.mark.parametrize("char", HIDDEN)
@pytest.mark.parametrize("template", ["https://example.com/a{c}b", "https://exa{c}mple.com/", "https://example.com/?q={c}",
                                      "https://example.com/#{c}", "{c}https://example.com/"])
def test_human_url_refuses_invisible_and_direction_characters(char, template):
    from bothub.browser_control import human_url
    assert human_url(template.format(c=char)) is None


@pytest.mark.parametrize("url,expected", [
    ("about:blank", "about:blank"),
    ("https://example.com/a?b=1#c", "https://example.com/a?b=1#c"),
    ("http://example.com:8080/x", "http://example.com:8080/x"),
    ("https://\u043f\u0440\u0438\u043c\u0435\u0440.\u0440\u0444/x", "https://xn--e1afmkfd.xn--p1ai/x"),
    ("https://EXAMPLE.com/A", "https://example.com/A"),
    ("https://g\u043e\u043egle.com/login", "https://xn--ggle-55da.com/login"),  # Cyrillic o: shown as what it is
    ("https://xn--e1afmkfd.xn--p1ai/", "https://xn--e1afmkfd.xn--p1ai/"),
])
def test_human_url_turns_the_host_into_punycode(url, expected):
    from bothub.browser_control import human_url
    assert human_url(url) == expected


@pytest.mark.parametrize("url", [None, "", "javascript:alert(1)", "file:///etc/passwd", "http://127.0.0.1/",
                                 "https://user@example.com/", "https://example.com/ x", "https://" + "a" * 70 + ".com/",
                                 "https://exa mple.com/", "chrome://settings"])
def test_human_url_keeps_every_other_refusal(url):
    from bothub.browser_control import human_url
    assert human_url(url) is None


@pytest.mark.pure
def test_rfb_filter_accepts_extended_clipboard_caps_message():
    # noVNC шлёт ClientCutText с отрицательной длиной (Extended Clipboard) сразу после ServerInit
    import struct
    from bothub.browser_control import RFBClientFilter
    parser = RFBClientFilter()
    parser.feed(b"RFB 003.008\n", allow_input=False)
    parser.feed(b"\x01", allow_input=False)
    parser.feed(b"\x01", allow_input=False)
    body = b"\x10\x00\x00\x00" + b"\x00\x00\x00\x00"  # flags caps + одна запись
    message = b"\x06\x00\x00\x00" + struct.pack("!i", -len(body)) + body
    assert parser.feed(message, allow_input=False) == b""  # буфер обмена это ввод: в режиме bot не пропускается
    assert parser.buffer == bytearray()
    parser2 = RFBClientFilter()
    for part in (b"RFB 003.008\n", b"\x01", b"\x01"):
        parser2.feed(part, allow_input=True)
    assert parser2.feed(message, allow_input=True) == message

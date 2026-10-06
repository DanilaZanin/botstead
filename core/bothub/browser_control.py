"""Browser ownership transitions, bounded RFB client-message filtering, URL policy and value masking."""

import ipaddress
import os
import re
import struct
import unicodedata
from urllib.parse import unquote, urlsplit, urlunsplit


TRANSITIONS = {
    ("bot", "takeover"): "human",
    ("human", "return"): "returning",
    ("returning", "takeover"): "human",
    ("returning", "snapshot"): "bot",
}


def transition(state: str, action: str) -> str:
    try:
        return TRANSITIONS[(state, action)]
    except KeyError as exc:
        raise ValueError("invalid_transition") from exc


class RFBProtocolError(ValueError):
    pass


class RFBClientFilter:
    """Preserve the RFB handshake, then emit complete, allowed client messages.

    RFB 3.7/3.8 with a one-byte security selection is required. Parsing is
    incremental so a denied message cannot be split across WebSocket frames.
    """

    MAX_BUFFER = 1024 * 1024 + 8

    def __init__(self):
        self.buffer = bytearray()
        self.phase = 0
        self.framebuffer_requested = False

    def feed(self, chunk: bytes, *, allow_input: bool) -> bytes:
        if len(chunk) + len(self.buffer) > self.MAX_BUFFER:
            raise RFBProtocolError("rfb_message_too_large")
        self.buffer.extend(chunk)
        out = bytearray()
        while self.buffer:
            if self.phase == 0:
                length = 12
                if len(self.buffer) < length:
                    break
                if bytes(self.buffer[:4]) != b"RFB " or self.buffer[7] != ord('.') or self.buffer[11] != ord('\n'):
                    raise RFBProtocolError("rfb_version")
                if bytes(self.buffer[4:7]) != b"003" or bytes(self.buffer[8:11]) not in (b"007", b"008"):
                    raise RFBProtocolError("rfb_version")
            elif self.phase in (1, 2):
                length = 1
            else:
                kind = self.buffer[0]
                if kind == 0:
                    length = 20
                elif kind == 2:
                    if len(self.buffer) < 4:
                        break
                    count = struct.unpack("!H", self.buffer[2:4])[0]
                    if count > 4096:
                        raise RFBProtocolError("rfb_encodings_too_many")
                    length = 4 + 4 * count
                elif kind == 3:
                    length = 10
                elif kind == 4:
                    length = 8
                elif kind == 5:
                    length = 6
                elif kind == 6:
                    if len(self.buffer) < 8:
                        break
                    count = struct.unpack("!I", self.buffer[4:8])[0]
                    if count > 1024 * 1024:
                        raise RFBProtocolError("rfb_cut_text_too_large")
                    length = 8 + count
                else:
                    raise RFBProtocolError("rfb_unknown_message")
            if len(self.buffer) < length:
                break
            if self.phase == 3 and self.buffer[0] == 3:
                self.framebuffer_requested = True
            if self.phase < 3 or self.buffer[0] not in (4, 5, 6) or allow_input:
                out.extend(self.buffer[:length])
            del self.buffer[:length]
            if self.phase < 3:
                self.phase += 1
        return bytes(out)


# --- Masking: the one place that decides what of a browser call may reach events and approvals ---

MASK = "[redacted]"
BROWSER_TOOLS = frozenset({"mcp__bothub__browser", "bothub.browser"})
_VALUE_KEYS = ("value", "text")  # bothub `fill` value and Playwright `browser_type` text
_TEXT_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s<>\"'`)\]}]+")


def is_browser_tool(name) -> bool:
    return isinstance(name, str) and name in BROWSER_TOOLS


def mask_url(url):
    """scheme://host[:port]/path. Userinfo, query and fragment can carry passwords or tokens; any
    other scheme (file:, javascript:, data:) is not echoed at all."""
    if not url or not isinstance(url, str):
        return url
    if url == "about:blank":
        return url
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return MASK
    if parts.scheme not in ("http", "https") or not host:
        return MASK
    if ":" in host:
        host = f"[{host}]"
    return f"{parts.scheme}://{host}{f':{port}' if port else ''}{parts.path or '/'}"


def mask_browser_args(args):
    """Copy of the args of `mcp__bothub__browser` that is safe to store: typed text is replaced,
    the address is reduced. Idempotent."""
    if not isinstance(args, dict):
        return args
    masked = dict(args)
    for key in _VALUE_KEYS:
        if masked.get(key) not in (None, ""):
            masked[key] = MASK
    if "url" in masked:
        masked["url"] = mask_url(masked["url"])
    return masked


HIDDEN = "[скрыто]"
_INPUT_ROLES = "textbox|searchbox|combobox|spinbutton|slider"
_QUOTED = r'"(?:[^"\\\n]|\\.)*"'
# A snapshot line of an input: `- textbox "Password" [active] [ref=e5]: hunter2`, the same without quotes or dash,
# or without attributes. The value is everything after the first colon that follows the name and attributes, so
# `]:` and quotes inside the value do not end it. The two alternatives are disjoint: no nested backtracking.
_INPUT_LINE_RE = re.compile(
    rf'^(?P<indent>[ \t]*)(?P<head>(?:-[ \t]+)?(?:{_INPUT_ROLES})\b'
    rf'(?:(?:[ \t]+{_QUOTED}|[^\n\[\]]*)(?:[ \t]*\[[^\]\n]*\])+[ \t]*'
    rf'|(?:[ \t]+{_QUOTED}|[^\n\[\]:]*)[ \t]*):)(?P<rest>[^\n]*)$')
_BLOCK_SCALAR_RE = re.compile(r"[|>][+-]?\d?")
QUOTE_SPAN = 50  # lines searched for the closing quote of a value that has a raw newline inside quotes


def _quote_closes(text: str, start: int) -> bool:
    i = start
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == '"':
            return True
        i += 1
    return False


def mask_input_values(text):
    """Page state from a browser tool names the value of every input: `textbox "Password" [ref=e5]: hunter2`.
    The value of textbox, searchbox, combobox, spinbutton and slider is replaced with HIDDEN; role, name, attributes
    and ref stay. The person types passwords in this browser, the model must not read them back from a
    snapshot. Handles quoted values, values containing `]:` or quotes, and values that continue on the
    following lines (block scalar, raw newline in a quoted string, indented or plain continuation).
    An input without a value on the line (options follow as children) is left as is. Options are not inputs:
    `option "x" [selected]` stays readable, the choice shows in the combobox line, which is masked. Idempotent."""
    if not isinstance(text, str) or ":" not in text:
        return text
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        m = _INPUT_LINE_RE.match(line)
        i += 1
        value = m["rest"].strip() if m else ""
        if not value or value == HIDDEN:
            out.append(line)
            continue
        tail = "\r" if line.endswith("\r") else ""
        out.append(m["indent"] + m["head"] + " " + HIDDEN + tail)
        depth = len(m["indent"].expandtabs())
        if value.startswith('"') and _quote_closes(value, 1):
            continue
        # The value goes on: raw newline inside quotes, a block scalar, or a plain multi-line scalar.
        if value.startswith('"'):
            window = lines[i:i + QUOTE_SPAN]
            close = next((n for n, nxt in enumerate(window) if _quote_closes(nxt, 0)), None)
            if close is not None:
                i += close + 1
                continue
            # No closing quote nearby: the quote is part of the typed text, continue as a plain value.
        block = bool(_BLOCK_SCALAR_RE.fullmatch(value))
        # Only a textbox (textarea) holds a multi-line value. Under any other input a deeper `- ` line is a child
        # node (`- option "x" [selected]` under a combobox), not a continuation: it must stay readable.
        multiline = m["head"].lstrip("- \t").startswith("textbox")
        while i < len(lines):
            nxt = lines[i]
            stripped = nxt.strip()
            indent = len(nxt) - len(nxt.lstrip())
            if block:
                if stripped and indent <= depth:
                    break
            elif not stripped or stripped.startswith(("```", "#")) or (
                    stripped.startswith("- ") and (indent <= depth or not multiline)):
                break
            i += 1
    return "\n".join(out)


def mask_browser_text(text):
    """Free text from a browser tool (page state, errors): every URL is reduced like mask_url and input
    values are hidden (mask_input_values)."""
    if not isinstance(text, str):
        return text

    def reduce(match):
        raw = match.group(0)
        body = raw.rstrip(".,;:!?")
        return mask_url(body) + raw[len(body):]
    return _TEXT_URL_RE.sub(reduce, mask_input_values(text))


def mask_browser_result(action, text):
    """Result of a browser call. The output of a fill echoes the typed text (Playwright prints the
    generated code), so it is dropped whole; so is any result whose action is unknown."""
    if text in (None, ""):
        return text
    if action in (None, "fill"):
        return MASK
    return mask_browser_text(text)


class BrowserEventMasker:
    """Per-turn masking of runner events. tool_call carries the args, tool_result only the call id,
    so the masker remembers which calls were browser calls."""

    LIMIT = 256

    def __init__(self):
        self._calls: dict[str, str | None] = {}

    def call(self, payload: dict) -> dict:
        if not is_browser_tool(payload.get("tool")):
            return payload
        args = payload.get("args")
        if len(self._calls) >= self.LIMIT:
            self._calls.pop(next(iter(self._calls)))
        self._calls[str(payload.get("call_id", ""))] = args.get("action") if isinstance(args, dict) else None
        return payload | {"args": mask_browser_args(args)}

    def result(self, payload: dict, *, tool=None, args=None) -> dict:
        call_id = str(payload.get("call_id", ""))
        if call_id in self._calls:
            action = self._calls[call_id]
        elif is_browser_tool(tool):  # result of a call this masker did not see
            action = args.get("action") if isinstance(args, dict) else None
        else:
            return payload
        masked = dict(payload)
        for key in ("summary", "error"):
            if key in masked:
                masked[key] = mask_browser_result(action, masked[key])
        if isinstance(masked.get("data"), dict):
            masked["data"] = {key: mask_browser_result(action, value) if key in ("result", "error") else value
                              for key, value in masked["data"].items()}
        return masked

    def event(self, kind: str, payload: dict) -> dict:
        if kind == "tool_call":
            return self.call(payload)
        if kind == "tool_result":
            return self.result(payload)
        return payload


# --- URL policy: where the bot's browser may go ---

_BAD_URL_CHARS = re.compile(r"[\x00-\x20\x7f\\]")
_INTERNAL_SUFFIXES = (".local", ".localdomain", ".internal", ".lan", ".home", ".corp", ".intranet",
                      ".home.arpa", ".docker", ".svc", ".cluster")
# Public names that resolve to loopback or private addresses by design.
_REBIND_DOMAINS = ("nip.io", "sslip.io", "xip.io", "localtest.me", "lvh.me", "vcap.me", "traefik.me")
_NUMERIC_LABEL_RE = re.compile(r"0x[0-9a-f]*|\d+")
_BAD_DECODED_HOST_CHARS = re.compile(r"[\x00-\x20\x7f/?#@\\%]")


def _parse_ipv4(host: str):
    """inet_aton forms that browsers accept: 127.1, 0x7f.0.0.1, 0177.0.0.1, 2130706433."""
    labels = host.split(".")
    if not 1 <= len(labels) <= 4:
        return None
    numbers = []
    for label in labels:
        try:
            if label.startswith("0x"):
                numbers.append(int(label[2:] or "0", 16))
            elif len(label) > 1 and label.startswith("0"):
                numbers.append(int(label, 8))
            else:
                numbers.append(int(label, 10))
        except ValueError:
            return None
    if any(n > 255 for n in numbers[:-1]) or numbers[-1] >= 256 ** (5 - len(numbers)):
        return None
    value = 0
    for n in numbers[:-1]:
        value = (value << 8) | n
    value = (value << (8 * (5 - len(numbers)))) | numbers[-1]
    return ipaddress.IPv4Address(value)


def _address_forbidden(ip) -> str | None:
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = ip.ipv4_mapped or ip.sixtofour
        if embedded is None and ip in ipaddress.ip_network("64:ff9b::/96"):
            embedded = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if embedded is not None and not embedded.is_global:
            return "private_address"
    return None if ip.is_global else "private_address"


def _configured_denied_hosts() -> set[str]:
    denied = {item.strip().lower().rstrip(".") for item in os.environ.get("BOTHUB_BROWSER_DENY_HOSTS", "").split(",")}
    try:
        denied.add((urlsplit(os.environ.get("BOTHUB_URL", "")).hostname or "").lower())
    except ValueError:
        pass
    denied.discard("")
    return denied


_DOT_VARIANTS = re.compile("[\u3002\uff0e\uff61]")  # ideographic, fullwidth and halfwidth full stops: the browser reads them as "."


def _canonical_host(host: str) -> str:
    """Host as the browser will see it: NFKC, every dot variant a plain ".", trailing dots removed.
    Repeated until stable: NFKC can produce a new dot variant (U+FF61 becomes U+3002) and a trailing
    dot can sit behind another one (127.0.0.1.。)."""
    while True:
        normalized = _DOT_VARIANTS.sub(".", unicodedata.normalize("NFKC", host)).rstrip(".")
        if normalized == host:
            return host
        host = normalized


def _host_forbidden(host: str) -> str | None:
    host = _canonical_host(host)
    if not host:
        return "url_invalid"
    if ":" in host:
        try:
            return _address_forbidden(ipaddress.IPv6Address(host.split("%", 1)[0]))
        except ValueError:
            return "url_invalid"
    try:
        host = host.encode("idna").decode("ascii").lower()  # fullwidth digits and the like become what the browser sees
    except UnicodeError:
        return "url_invalid"
    host = _canonical_host(host)  # idna turns the remaining dot variants into "." too
    if not host:
        return "url_invalid"
    if _NUMERIC_LABEL_RE.fullmatch(host.rsplit(".", 1)[-1]):
        ip = _parse_ipv4(host)  # a numeric last label is an IPv4 address, or an error for the browser
        return "url_invalid" if ip is None else _address_forbidden(ip)
    if host == "localhost" or host.endswith(".localhost"):
        return "loopback_host"
    if "." not in host or host.endswith(_INTERNAL_SUFFIXES):
        return "internal_host"  # compose service names (core, gateway), mDNS, corporate DNS
    if any(host == name or host.endswith("." + name) for name in _REBIND_DOMAINS):
        return "rebinding_domain"
    if host in _configured_denied_hosts():
        return "internal_host"
    return None


def url_forbidden(url) -> str | None:
    """Reason code if the bot's browser must not open `url`, None if it may. Only http, https and
    about:blank; no loopback, link-local, metadata, private (RFC 1918, ULA, CGNAT) addresses and no
    internal names (core and gateway are single-label or private). A scheme is required. DNS is not
    resolved here: a public name pointing at a private address needs the egress rules of the network."""
    if url == "about:blank":
        return None
    if not isinstance(url, str) or not url or len(url) > 2048 or _BAD_URL_CHARS.search(url):
        return "url_invalid"
    try:
        parts = urlsplit(url)
        host, _ = parts.hostname, parts.port
    except ValueError:
        return "url_invalid"
    if parts.scheme not in ("http", "https"):
        return "scheme_forbidden"
    if not parts.netloc or "@" in parts.netloc or not host:
        return "url_invalid"  # userinfo hides the real host from a reader
    if "%" in host:
        # The browser decodes a percent-encoded host (%31%32%37.0.0.1 is 127.0.0.1): judge what it will see.
        # What stays encoded after one decoding (%25 and the like) is never a legitimate host.
        try:
            host = unquote(host, errors="strict")
        except UnicodeDecodeError:
            return "url_invalid"
        if _BAD_DECODED_HOST_CHARS.search(host):
            return "url_invalid"
    return _host_forbidden(host)


def url_origin(url) -> str | None:
    """scheme://host[:port] of an allowed http(s) URL, else None."""
    if url == "about:blank" or url_forbidden(url):
        return None
    parts = urlsplit(url)
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    return f"{parts.scheme}://{host}{f':{parts.port}' if parts.port else ''}"


# Characters that reorder or hide text, as the body of a regex character class: a path ending in `exe.png` with a
# right-to-left override in front reads differently from what it is. Written as escapes on purpose: the source holds no
# such character itself. One list for the package; launcher/bothub_launcher/validation.py keeps an identical copy and a
# test compares the two.
HIDDEN_URL_CHARS = (
    "\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180b-\u180f"  # soft hyphen, grapheme joiner, Arabic letter mark, fillers, Mongolian
    "\u200b-\u200f\u2028\u2029\u202a-\u202e"  # zero-width, marks, line and paragraph separators, bidi embeddings and overrides
    "\u2060-\u2064\u2066-\u206f"  # word joiner, invisible operators, bidi isolates, deprecated format characters
    "\u3164\ufe00-\ufe0f\ufeff\uffa0\ufff9-\ufffb"  # fillers, variation selectors, BOM, annotation characters
    "\U0001d173-\U0001d17a\U000e0000-\U000e0fff"  # musical format characters, tags and variation selectors supplement
)
_HIDDEN_URL_CHARS = re.compile(f"[{HIDDEN_URL_CHARS}]")
# IDNA 2003 (Python's `idna` codec) folds sharp s and final sigma and drops the joiners; Chromium follows UTS 46 and keeps
# them: "fa" + sharp s + ".de" is fass.de for Python and xn--fa-hia.de for the browser. Such hosts are refused, not translated.
_IDNA_DIVERGENT_CHARS = re.compile("[\u00df\u03c2\u200c\u200d]")


def _ascii_host(host: str) -> str | None:
    """The host as the browser will show it (punycode labels), or None when it must not be passed on: Python and
    Chromium would not agree on it. The codec result must equal a by-hand build of the same labels."""
    host = unicodedata.normalize("NFKC", host).lower()
    if _IDNA_DIVERGENT_CHARS.search(host):
        return None
    try:
        by_codec = host.encode("idna").decode("ascii")
        by_hand = ".".join(label if label.isascii() else "xn--" + label.encode("punycode").decode("ascii")
                           for label in host.split("."))
    except UnicodeError:
        return None
    return by_codec if by_codec == by_hand else None


def human_url(url) -> str | None:
    """The address for the human's tab, or None when it must not be opened. Everything `url_forbidden` refuses stays
    refused; on top, no invisible or direction-changing characters anywhere in the address (a percent-encoded host
    included), and the host is shown as punycode (a lookalike host such as a Cyrillic "o" in google.com then reads as
    xn--...). A host on which IDNA 2003 and UTS 46 disagree is refused."""
    if url == "about:blank":
        return url
    if not isinstance(url, str) or _HIDDEN_URL_CHARS.search(url) or url_forbidden(url):
        return None
    parts = urlsplit(url)
    host = parts.hostname
    if "%" in host:
        try:
            host = unquote(host, errors="strict")
        except UnicodeDecodeError:
            return None
        if _HIDDEN_URL_CHARS.search(host):
            return None
    host = _ascii_host(host)
    if host is None:
        return None
    if ":" in host:
        host = f"[{host}]"
    netloc = f"{host}:{parts.port}" if parts.port else host
    return urlunsplit(parts._replace(netloc=netloc))

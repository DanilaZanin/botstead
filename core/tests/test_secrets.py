import base64
import hashlib
import hmac
import os

import pytest
from cryptography.fernet import InvalidToken

from bothub.secrets import (
    decrypt_secret, encrypt_secret, issue_gateway_token, rotate_secret,
    verify_gateway_token, gateway_token_turn_id,
)


def key(key_id):
    return f"{key_id}:{base64.b64encode(os.urandom(32)).decode()}"


def test_aes_gcm_rotation_and_row_binding(monkeypatch):
    old, new = key(7), key(8)
    monkeypatch.setenv("BOTHUB_SECRET_KEYS", old)
    ciphertext = encrypt_secret("sëcret".encode(), b"owner-1:row-1")
    raw = ciphertext
    assert raw[0] == 7
    assert len(raw) == 1 + 12 + len("sëcret".encode()) + 16
    monkeypatch.setenv("BOTHUB_SECRET_KEYS", f"{new},{old}")
    assert decrypt_secret(ciphertext, b"owner-1:row-1") == "sëcret".encode()
    with pytest.raises(InvalidToken):
        decrypt_secret(ciphertext, b"owner-2:row-1")
    rotated = rotate_secret(ciphertext, b"owner-1:row-1")
    assert rotated[0] == 8
    monkeypatch.setenv("BOTHUB_SECRET_KEYS", new)
    assert decrypt_secret(rotated, b"owner-1:row-1") == "sëcret".encode()
    with pytest.raises(InvalidToken):
        decrypt_secret(ciphertext, b"owner-1:row-1")
    tampered = bytearray(rotated)
    tampered[-1] ^= 1
    with pytest.raises(InvalidToken):
        decrypt_secret(bytes(tampered), b"owner-1:row-1")


def test_secrets_bytes():
    configured = [key(9)]
    ciphertext = encrypt_secret(b"payload", b"row", configured)
    assert type(ciphertext) is bytes
    assert ciphertext[0] == 9
    assert len(ciphertext) == 1 + 12 + len(b"payload") + 16
    assert decrypt_secret(ciphertext, b"row", configured) == b"payload"


@pytest.mark.parametrize("keys", [[], ["bad"], [""], ["256:" + base64.b64encode(b"x" * 32).decode()],
                                  ["1:" + base64.b64encode(b"x" * 31).decode()],
                                  ["1:" + base64.b64encode(b"x" * 32).decode()] * 2])
def test_invalid_aes_key_configuration(keys):
    with pytest.raises(ValueError):
        encrypt_secret(b"value", b"row-1", keys)


def test_row_id_is_required():
    with pytest.raises(ValueError):
        encrypt_secret(b"value", b"", [key(1)])


def test_gateway_token_scope_expiry_and_tamper():
    token = issue_gateway_token("bot-1", "provider-1", "hmac-secret", ttl=30, now=100)
    payload, signature = token.rsplit(":", 1)
    assert payload == "gw:bot-1:provider-1:130"
    assert signature == hmac.new(b"hmac-secret", payload.encode(), hashlib.sha256).hexdigest()
    assert verify_gateway_token(token, "provider-1", "hmac-secret", now=100) == "bot-1"
    assert verify_gateway_token(token, "provider-2", "hmac-secret", now=100) is None
    assert verify_gateway_token(token, "provider-1", "wrong", now=100) is None
    assert verify_gateway_token(token, "provider-1", "hmac-secret", now=130) is None
    assert verify_gateway_token(token + "x", "provider-1", "hmac-secret", now=100) is None
    assert verify_gateway_token("bad", "provider-1", "hmac-secret", now=100) is None
    assert verify_gateway_token("bot:bot-1:" + signature, "provider-1", "hmac-secret", now=100) is None
    assert verify_gateway_token(token.replace("gw:", "bot:", 1), "provider-1", "hmac-secret", now=100) is None
    with pytest.raises(ValueError):
        issue_gateway_token("bot", "provider", "secret", ttl=0)
    with pytest.raises(ValueError):
        issue_gateway_token("bot:other", "provider", "secret")


def test_bytes_round_trip_and_aad_type():
    configured = [key(1)]
    raw = bytes(range(256))
    assert decrypt_secret(encrypt_secret(raw, b"row", configured), b"row", configured) == raw
    with pytest.raises(ValueError):
        encrypt_secret(raw, "row", configured)
    with pytest.raises(ValueError):
        decrypt_secret(encrypt_secret(raw, b"row", configured), "row", configured)


def test_zero_key_id_rejected_and_parse_error_omits_secret(monkeypatch):
    with pytest.raises(ValueError):
        encrypt_secret(b"value", b"row", [key(0)])
    marker = "secret-do-not-print"
    monkeypatch.setenv("BOTHUB_SECRET_KEYS", "1:" + marker)
    with pytest.raises(ValueError) as info:
        encrypt_secret(b"value", b"row")
    import traceback
    assert marker not in "".join(traceback.format_exception(info.value))
    assert info.value.__cause__ is None


def test_gateway_token_turn_ttl_and_expiry():
    token = issue_gateway_token("bot", "provider", "secret", now=100)
    assert verify_gateway_token(token, "provider", "secret", now=100) == "bot"
    assert verify_gateway_token(token, "provider", "secret", now=2020) is None
    assert verify_gateway_token(token, "provider", "secret", now=100) == "bot"


def test_gateway_token_supports_configured_turn_deadline():
    ttl = 3600 + 120
    token = issue_gateway_token("bot", "provider", "secret", ttl=ttl, now=100)
    assert verify_gateway_token(token, "provider", "secret", now=100) == "bot"
    assert verify_gateway_token(token, "provider", "secret", now=100 + ttl) is None


@pytest.mark.parametrize("ttl", [0, -1, True, 1.5])
def test_gateway_token_rejects_invalid_ttl(ttl):
    with pytest.raises(ValueError):
        issue_gateway_token("bot", "provider", "secret", ttl=ttl, now=100)


def test_turn_scoped_gateway_token_cannot_change_turn():
    token = issue_gateway_token('bot', 'provider', 'secret', now=100, turn_id='turn-1')
    assert verify_gateway_token(token, 'provider', 'secret', now=100) == 'bot'
    assert gateway_token_turn_id(token) == 'turn-1'
    assert verify_gateway_token(token.replace('turn-1', 'turn-2'), 'provider', 'secret', now=100) is None

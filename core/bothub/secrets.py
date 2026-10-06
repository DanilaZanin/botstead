"""Encrypted provider secrets and scoped, short-lived gateway credentials."""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import time
from collections.abc import Sequence

from cryptography.exceptions import InvalidTag
from cryptography.fernet import InvalidToken
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def _keys(keys: Sequence[str] | None) -> list[tuple[int, bytes]]:
    values = list(keys) if keys is not None else os.environ.get("BOTHUB_SECRET_KEYS", "").split(",")
    parsed: list[tuple[int, bytes]] = []
    seen: set[int] = set()
    for value in values:
        try:
            key_id_text, encoded = value.strip().split(":", 1)
            key_id = int(key_id_text)
            key = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("BOTHUB_SECRET_KEYS requires key_id:base64key entries") from None
        if not 1 <= key_id <= 255 or key_id in seen or len(key) != 32:
            raise ValueError("secret keys require unique byte IDs and 32-byte AES keys")
        seen.add(key_id)
        parsed.append((key_id, key))
    if not parsed:
        raise ValueError("at least one secret key is required")
    return parsed


def _row_id(aad: bytes) -> bytes:
    if not isinstance(aad, bytes) or not aad:
        raise ValueError("aad must be a nonempty row ID as bytes")
    return aad


def encrypt_secret(value: bytes, aad: bytes, keys: Sequence[str] | None = None) -> bytes:
    """Encrypt under the first key and bind the value to its row ID."""
    row_id = _row_id(aad)
    key_id, key = _keys(keys)[0]
    nonce = os.urandom(12)
    if not isinstance(value, bytes):
        raise ValueError("secret value must be bytes")
    raw = bytes([key_id]) + nonce + AESGCM(key).encrypt(nonce, value, row_id)
    return raw


def decrypt_secret(value: bytes, aad: bytes, keys: Sequence[str] | None = None) -> bytes:
    """Decrypt with the ciphertext's key ID and reject a foreign row ID."""
    row_id = _row_id(aad)
    if type(value) is not bytes:
        raise InvalidToken
    raw = value
    if len(raw) < 1 + 12 + 16:
        raise InvalidToken
    key = dict(_keys(keys)).get(raw[0])
    if key is None:
        raise InvalidToken
    try:
        return AESGCM(key).decrypt(raw[1:13], raw[13:], row_id)
    except InvalidTag as exc:
        raise InvalidToken from exc


def rotate_secret(value: bytes, aad: bytes, keys: Sequence[str] | None = None) -> bytes:
    """Re-encrypt a row under the first configured key."""
    plaintext = decrypt_secret(value, aad, keys)
    return encrypt_secret(plaintext, aad, keys)


def issue_gateway_token(bot_id: str, provider_id: str, secret: str, *, ttl: int = 1920,
                        now: int | None = None, turn_id: str | None = None) -> str:
    """Create a token bound to one bot and provider for one turn."""
    if (not bot_id or not provider_id or not secret or ":" in bot_id or ":" in provider_id
            or type(ttl) is not int or ttl <= 0):
        raise ValueError("bot, provider, secret and a positive integer ttl are required")
    expires = int(time.time() if now is None else now) + ttl
    if turn_id is not None and (not turn_id or ':' in turn_id):
        raise ValueError('invalid turn ID')
    payload = f"gw:{bot_id}:{provider_id}:{turn_id}:{expires}" if turn_id else f"gw:{bot_id}:{provider_id}:{expires}"
    signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{signature}"


def verify_gateway_token(token: str, provider_id: str, secret: str, *, now: int | None = None) -> str | None:
    """Return the bot ID only for a valid token bound to the provider."""
    try:
        parts = token.split(":")
        if len(parts) == 5:
            prefix, bot_id, token_provider, expires_text, signature = parts
            payload = f"gw:{bot_id}:{token_provider}:{expires_text}"
        elif len(parts) == 6:
            prefix, bot_id, token_provider, turn_id, expires_text, signature = parts
            if not turn_id: return None
            payload = f"gw:{bot_id}:{token_provider}:{turn_id}:{expires_text}"
        else: return None
        if prefix != "gw" or not bot_id or token_provider != provider_id or not secret:
            return None
        expires = int(expires_text)
        if str(expires) != expires_text:
            return None
        current = int(time.time() if now is None else now)
        if not current < expires:
            return None
        expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        return bot_id if hmac.compare_digest(signature, expected) else None
    except (ValueError, TypeError, OverflowError):
        return None


def gateway_token_turn_id(token: str) -> str | None:
    """Read a turn ID only after verify_gateway_token has accepted the signed token."""
    parts = token.split(':')
    return parts[3] if len(parts) == 6 else None

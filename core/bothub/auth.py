"""Pure authentication rules. Database operations live in main.py."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
from datetime import datetime, timedelta
from functools import lru_cache
from urllib.parse import urlsplit

_alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"


@lru_cache(maxsize=1)
def _hasher():
    from argon2 import PasswordHasher
    from argon2.low_level import Type
    return PasswordHasher(type=Type.ID)


def hash_password(password: str) -> str:
    if not isinstance(password, str) or len(password) < 10:
        raise ValueError("password must contain at least 10 characters")
    return _hasher().hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    if not isinstance(password_hash, str) or not isinstance(password, str):
        return False
    from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
    try:
        return _hasher().verify(password_hash, password)
    except (InvalidHashError, VerificationError, VerifyMismatchError):
        return False


def new_token() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")


def decode_token(token: str) -> bytes:
    return base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))


def _bytes(text: str) -> bytes:
    # surrogatepass: непарный суррогат из JSON кодируется, а не бросает UnicodeEncodeError. Такой текст не совпадает
    # ни с одним выданным токеном (они ASCII), так что ответ тот же, что для любого неизвестного токена.
    return text.encode("utf-8", "surrogatepass")


def is_clean_text(value: str) -> bool:
    """Текст, который можно записать в базу: без NUL и без непарных суррогатов (кодируется в UTF-8)."""
    if "\x00" in value:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def token_equals(token: str, secret: str | None) -> bool:
    """Сравнение за постоянное время; пустой секрет не совпадает ни с чем, непарный суррогат не бросает исключение."""
    return bool(secret) and hmac.compare_digest(_bytes(token), _bytes(secret))


def token_hash(token: str) -> str:
    return hashlib.sha256(_bytes(token)).hexdigest()


def session_expiry(now: datetime) -> datetime:
    return now + timedelta(days=30)


def invite_expiry(now: datetime, days: int = 7) -> datetime:
    if days <= 0:
        raise ValueError("invite days must be positive")
    return now + timedelta(days=days)


def should_extend_session(last_extended: datetime, now: datetime) -> bool:
    return now - last_extended >= timedelta(days=1)


def csrf_token(session_token: str, secret: str) -> str:
    return hmac.new(_bytes(secret), _bytes("csrf:" + session_token), hashlib.sha256).hexdigest()


def same_origin(origin: str, host: str, *, scheme: str = "https") -> bool:
    try:
        parsed = urlsplit(origin)
        public = os.getenv("BOTHUB_PUBLIC_ORIGIN")
        if public:
            expected = urlsplit(public)
            return (parsed.scheme.lower(), parsed.netloc.lower()) == (expected.scheme.lower(), expected.netloc.lower()) and not parsed.username and not parsed.password and not expected.username and not expected.password
    except ValueError:
        return False
    return parsed.scheme == scheme and parsed.netloc.lower() == host.lower() and not parsed.username and not parsed.password


def new_bot_id(name: str) -> str:
    slug = re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", name.lower())).strip("-")[:27].rstrip("-") or "bot"
    return slug + "-" + "".join(secrets.choice(_alphabet) for _ in range(4))

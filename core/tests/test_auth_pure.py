"""Password, token, expiry and origin rules without a database."""
from datetime import datetime, timedelta, timezone
import hashlib
import re

import pytest

from bothub import auth


pytestmark = pytest.mark.pure
NOW = datetime(2026, 10, 4, tzinfo=timezone.utc)


def test_password_argon2id_and_length_policy():
    with pytest.raises(ValueError):
        auth.hash_password("123456789")
    digest = auth.hash_password("correct horse battery staple")
    assert digest.startswith("$argon2id$")
    assert auth.verify_password(digest, "correct horse battery staple")
    assert not auth.verify_password(digest, "incorrect password")


def test_tokens_are_random_32_bytes_and_hashed_for_storage():
    first, second = auth.new_token(), auth.new_token()
    assert first != second
    assert len(auth.decode_token(first)) == 32
    assert auth.token_hash(first) == hashlib.sha256(first.encode()).hexdigest()
    assert re.fullmatch(r"[0-9a-f]{64}", auth.token_hash(first))


def test_session_and_invite_expiry_and_daily_sliding_window():
    assert auth.session_expiry(NOW) == NOW + timedelta(days=30)
    assert auth.invite_expiry(NOW) == NOW + timedelta(days=7)
    assert auth.invite_expiry(NOW, days=2) == NOW + timedelta(days=2)
    assert not auth.should_extend_session(NOW, NOW + timedelta(hours=23))
    assert auth.should_extend_session(NOW, NOW + timedelta(days=1))


@pytest.mark.parametrize("origin,host,expected", [
    ("https://example.com", "example.com", True),
    ("https://evil.example", "example.com", False),
    ("http://example.com", "example.com", False),
    ("https://example.com.evil", "example.com", False),
])
def test_same_origin(origin, host, expected):
    assert auth.same_origin(origin, host, scheme="https") is expected


def test_same_origin_uses_public_origin_when_configured(monkeypatch):
    monkeypatch.setenv("BOTHUB_PUBLIC_ORIGIN", "https://public.example")
    assert auth.same_origin("https://public.example/page", "internal:8000", scheme="http")
    assert not auth.same_origin("http://internal:8000", "internal:8000", scheme="http")


def test_bot_id_uses_name_slug_and_random_suffix():
    bot_id = auth.new_bot_id("My Scout Bot")
    assert re.fullmatch(r"my-scout-bot-[a-z0-9]{4}", bot_id)
    assert len(auth.new_bot_id("A" * 100)) <= 32
    assert re.fullmatch(r"bot-[a-z0-9]{4}", auth.new_bot_id("!!!"))


@pytest.mark.parametrize("token", ["", "☃", "a" * 10000])
def test_untrusted_token_hash_is_safe(token):
    assert re.fullmatch(r"[0-9a-f]{64}", auth.token_hash(token))

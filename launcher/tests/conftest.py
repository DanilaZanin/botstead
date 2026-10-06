import pytest

from bothub_launcher.config import Config

SECRET = "s" * 40


@pytest.fixture
def cfg() -> Config:
    return Config(secret=SECRET, bot_token_secret="bot-token-secret", kill_grace=0.01)

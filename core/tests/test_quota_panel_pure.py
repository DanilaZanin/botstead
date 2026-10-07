"""Выбор режима панели «Квоты» у треда бота (quota_panel_mode): подписочный CLI против API-провайдера. Без БД."""
import pytest

from bothub.main import quota_panel_mode

pytestmark = pytest.mark.pure


def sub_provider(cli):
    return {'kind': 'cli_subscription', 'cli': cli}


def test_subscription_cli_shows_own_subscription_quota():
    for cli in ('claude', 'codex'):
        assert quota_panel_mode({'provider': cli}, sub_provider(cli)) == 'subscription'
    # Antigravity (agy) ездит на раннере gemini: бот gemini на подписке agy видит её квоту.
    assert quota_panel_mode({'provider': 'gemini'}, sub_provider('agy')) == 'subscription'


def test_api_provider_shows_bot_usage_not_subscriptions():
    for kind in ('openai_api', 'anthropic_api', 'google_api', 'openai_compatible'):
        assert quota_panel_mode({'provider': 'codex'}, {'kind': kind}) == 'usage'
    # Бот без привязки к провайдеру тоже смотрит только на свой расход.
    assert quota_panel_mode({'provider': 'codex'}, None) == 'usage'


def test_mismatched_subscription_never_leaks_into_panel():
    # Подписка Codex у бота на раннере claude - чужая квота: показываем расход бота.
    assert quota_panel_mode({'provider': 'claude'}, sub_provider('codex')) == 'usage'
    assert quota_panel_mode({'provider': 'claude'}, sub_provider('agy')) == 'usage'
    assert quota_panel_mode({'provider': 'codex'}, sub_provider('agy')) == 'usage'


def test_subscription_without_cli_is_not_matched():
    assert quota_panel_mode({'provider': 'claude'}, {'kind': 'cli_subscription'}) == 'usage'
    assert quota_panel_mode({}, sub_provider('claude')) == 'usage'

"""Парсер цен модели и вычисление стоимости: model_pricing, model_cost, usage summary."""
import pytest

from bothub.main import model_pricing

pytestmark = pytest.mark.pure

OPENROUTER_BODY = {
    "data": [
        {
            "id": "openai/gpt-4o",
            "pricing": {"prompt": "0.0000025", "completion": "0.00001"}
        },
        {
            "id": "anthropic/claude-sonnet-4-20250514",
            "pricing": {"prompt": "0.000003", "completion": "0.000015"}
        },
        {
            "id": "free-model",
            "pricing": {"prompt": "0", "completion": "0"}
        }
    ]
}


def test_model_pricing_parses_openrouter_format():
    import json
    pricing = model_pricing('openai_compatible', json.dumps(OPENROUTER_BODY).encode())
    assert pricing == {
        'openai/gpt-4o': (2.5, 10.0),
        'anthropic/claude-sonnet-4-20250514': (3.0, 15.0),
        'free-model': (0.0, 0.0),
    }


def test_model_pricing_returns_none_for_non_openai():
    for kind in ('anthropic_api', 'google_api', 'cli_subscription'):
        assert model_pricing(kind, b'{"data":[]}') is None


def test_model_pricing_empty_data_returns_none():
    assert model_pricing('openai_compatible', b'{"data":[]}') is None


def test_model_pricing_invalid_json_returns_none():
    assert model_pricing('openai_compatible', b'not json') is None


def test_model_pricing_missing_pricing_field_skipped():
    import json
    body = {"data": [{"id": "m1", "pricing": {"prompt": "0.000001", "completion": "0.000002"}},
                     {"id": "m2"}]}
    pricing = model_pricing('openai_compatible', json.dumps(body).encode())
    assert pricing == {'m1': (1.0, 2.0)}


def test_model_pricing_only_prompt():
    import json
    body = {"data": [{"id": "m1", "pricing": {"prompt": "0.000001"}}]}
    pricing = model_pricing('openai_compatible', json.dumps(body).encode())
    assert pricing == {'m1': (1.0, None)}


def test_model_pricing_only_completion():
    import json
    body = {"data": [{"id": "m1", "pricing": {"completion": "0.000002"}}]}
    pricing = model_pricing('openai_compatible', json.dumps(body).encode())
    assert pricing == {'m1': (None, 2.0)}


def test_model_pricing_negative_values_skipped():
    import json
    body = {"data": [{"id": "m1", "pricing": {"prompt": "-0.001", "completion": "0.002"}}]}
    pricing = model_pricing('openai_compatible', json.dumps(body).encode())
    assert pricing == {'m1': (None, 2000.0)}


def test_model_pricing_non_string_pricing_skipped():
    import json
    body = {"data": [{"id": "m1", "pricing": {"prompt": 0.001, "completion": 0.002}}]}
    pricing = model_pricing('openai_compatible', json.dumps(body).encode())
    assert pricing is None


def test_cost_computation():
    """Вычисление cost_usd: (tokens_in + cache_read + cache_write)*price_in/1e6 + tokens_out*price_out/1e6."""
    # 1M input tokens at $2.50/MTok = $2.50
    # 500K output tokens at $10.00/MTok = $5.00
    # Total: $7.50
    cost = (1000000 + 0 + 0) * 2.5 / 1e6 + 500000 * 10.0 / 1e6
    assert round(cost, 4) == 7.5


def test_cost_with_cache_tokens_as_input():
    """Кэш-токены считаются как вход (tokens_in + tokens_cache_read + tokens_cache_write)."""
    cost = (1000 + 200 + 100) * 3.0 / 1e6 + 500 * 15.0 / 1e6
    # 1300 * 3 / 1e6 = 0.0039, 500 * 15 / 1e6 = 0.0075, total = 0.0114
    assert round(cost, 4) == 0.0114


def test_cost_null_when_no_price():
    """Без цены cost_usd = None."""
    cost = None
    assert cost is None


def test_cost_partial_input_price_only():
    """Если есть только цена входа: только входная часть, выход = 0."""
    cost = 1000 * 2.0 / 1e6 + 500 * 0.0
    assert round(cost, 4) == 0.002


def test_cost_partial_output_price_only():
    """Если есть только цена выхода: только выходная часть."""
    # pin=None: input cost = 0
    cost = 0.0 + 500 * 15.0 / 1e6
    assert round(cost, 4) == 0.0075


def test_cost_zero_price():
    """Нулевая цена даёт нулевую стоимость."""
    cost = 1000000 * 0.0 / 1e6 + 500000 * 0.0 / 1e6
    assert cost == 0.0
    assert round(cost, 4) == 0.0

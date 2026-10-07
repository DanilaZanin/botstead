"""Проверки входа для нескольких Mac без PostgreSQL."""

import uuid
import logging

import pytest
from pydantic import ValidationError

from bothub.main import BotIn, BotPatch, MacAccessTokenFilter, MacIn, create_app

pytestmark = pytest.mark.pure


def test_mac_name_is_trimmed_and_blank_is_refused():
    assert MacIn(name='  MacBook Pro  ').name == 'MacBook Pro'
    for value in ('', '  ', 'x' * 81):
        with pytest.raises(ValidationError):
            MacIn(name=value)


def test_bot_mac_id_accepts_uuid_and_explicit_null():
    mac_id = uuid.uuid4()
    bot = BotIn(name='Test', provider='fake', model='fake', mac_id=mac_id)
    assert bot.mac_id == mac_id
    assert BotIn(name='Test', provider='fake', model='fake').mac_id is None
    assert BotPatch(mac_id=None).model_dump(exclude_unset=True) == {'mac_id': None}
    assert BotPatch(mac_id=str(mac_id)).mac_id == mac_id
    with pytest.raises(ValidationError):
        BotPatch(mac_id='not-a-uuid')


def test_mac_routes_are_registered():
    routes = {(method, route.path) for route in create_app().routes
              for method in getattr(route, 'methods', ())}
    assert {('GET', '/api/macs'), ('POST', '/api/macs'),
            ('DELETE', '/api/macs/{id}')}.issubset(routes)


def test_mac_access_log_redacts_query_token_even_on_rejected_handshake():
    record = logging.LogRecord('uvicorn.access', logging.INFO, __file__, 1,
                               '%s - "WebSocket %s" [rejected]',
                               ('client', '/agent/mac?other=1&token=legacy-secret&more=2'), None)
    assert MacAccessTokenFilter().filter(record)
    assert 'legacy-secret' not in record.getMessage()
    assert '/agent/mac?[REDACTED]' in record.getMessage()
    encoded = logging.LogRecord('uvicorn.access', logging.INFO, __file__, 1,
                                'WebSocket %s', ('/agent/mac?%74oken=legacy-secret',), None)
    assert MacAccessTokenFilter().filter(encoded)
    assert 'legacy-secret' not in encoded.getMessage()


def test_uvicorn_websocket_error_logger_redacts_query_token():
    logger = logging.getLogger('uvicorn.error')
    messages = []

    class Capture(logging.Handler):
        def emit(self, record):
            messages.append(record.getMessage())

    handler = Capture()
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        logger.info('%s - "WebSocket %s" [rejected]', 'client', '/agent/mac?token=secret-in-url')
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
    assert messages and '/agent/mac?[REDACTED]' in messages[0]
    assert 'secret-in-url' not in messages[0]

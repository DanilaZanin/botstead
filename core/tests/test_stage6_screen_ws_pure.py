"""Screen WebSocket framing and launcher error checks without Postgres."""
import asyncio
import threading
import time
import uuid
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from bothub.launcher_client import FakeBot, FakeLauncherClient, LauncherUnavailable
from bothub.main import create_app


OWNER = uuid.UUID(int=1)
HEADERS = {'Cookie':'bothub_session=test-session','Origin':'https://testserver'}


class FakeConnection:
    session_checks = 0

    async def fetchrow(self, query, *args):
        return {'owner_id':OWNER}

    async def fetchval(self, query, *args):
        if 'from bothub.sessions s join bothub.users u' in query:
            self.session_checks += 1
        return 'bot' if 'browser_control' in query else 1


class FakePool:
    def __init__(self):
        self.connection = FakeConnection()

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


@asynccontextmanager
async def no_lifespan(app):
    yield


class HeldScreenLauncher(FakeLauncherClient):
    def __init__(self):
        super().__init__()
        self.bots['alpha'] = FakeBot('alpha',str(OWNER))
        self.gate = threading.Event()

    async def screen_output(self, session_id):
        yield b'RFB 003.008\n'
        await asyncio.to_thread(self.gate.wait)


@pytest.mark.pure
def test_screen_ws_filters_input_and_rejects_second_client():
    launcher = HeldScreenLauncher()
    app = create_app(launcher=launcher)
    app.state.pool = FakePool()
    app.router.lifespan_context = no_lifespan
    try:
        with TestClient(app,base_url='https://testserver') as client:
            with client.websocket_connect('/api/bots/alpha/screen',headers=HEADERS) as ws:
                try:
                    assert ws.receive_bytes() == b'RFB 003.008\n'
                    try:
                        with client.websocket_connect('/api/bots/alpha/screen',headers=HEADERS):
                            pytest.fail('second screen accepted')
                    except Exception as exc:
                        assert getattr(exc,'status_code',None)==409
                    key = b'\x04\x01\x00\x00\x00\x00\x00A'
                    ws.send_bytes(b'RFB 003.008\n\x01\x01' + key)
                    deadline=time.monotonic()+1
                    while time.monotonic()<deadline and not any(launcher.screen_inputs.values()):
                        time.sleep(.01)
                    sid=next(iter(launcher.screen_inputs))
                    assert launcher.screen_inputs[sid] == [b'RFB 003.008\n\x01\x01']
                    assert app.state.pool.connection.session_checks == 0
                    ws.send({'type':'websocket.disconnect'})
                finally:
                    launcher.gate.set()
    finally:
        launcher.gate.set()


@pytest.mark.pure
def test_screen_ws_launcher_open_failure_has_clear_close_code():
    launcher = HeldScreenLauncher()
    launcher.fail_next(LauncherUnavailable('offline',code='unavailable'))
    app = create_app(launcher=launcher)
    app.state.pool = FakePool()
    app.router.lifespan_context = no_lifespan
    with pytest.raises(WebSocketDisconnect) as failure:
        with TestClient(app,base_url='https://testserver').websocket_connect('/api/bots/alpha/screen',headers=HEADERS):
            pass
    assert failure.value.code == 1013

"""Launcher stop failures close turns even without a database server."""
import asyncio
import json
import uuid
from contextlib import asynccontextmanager

import pytest

from bothub import main
from bothub.launcher_client import FakeLauncherClient, LauncherServerError, LauncherTimeout


@pytest.mark.pure
async def test_stop_launcher_timeout_retries_and_blocks_bot(monkeypatch):
    turn_id = uuid.uuid4()
    thread_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    turn = {'id': turn_id, 'thread_id': thread_id, 'bot_id': 'probe', 'status': 'running', 'error': None, 'turn_type': 'normal'}
    bot = {'status': 'running'}
    events = []

    class Connection:
        @asynccontextmanager
        async def transaction(self):
            yield

        async def fetchrow(self, query, *args):
            if 'from bothub.turns t join bothub.threads' in query:
                return dict(turn)
            if 'from bothub.turns where id=' in query:
                return dict(turn)
            if 'from bothub.bots where id=' in query:
                return None
            if 'insert into bothub.events' in query:
                event = {'thread_id': str(thread_id), 'turn_id': str(turn_id),
                         'kind': args[3], 'payload': json.loads(args[-1])}
                events.append(event)
                return event
            raise AssertionError(query)

        async def fetchval(self, query, *args):
            if 'update bothub.turns set status=' in query:
                turn.update(status='error', error=args[1])
                return turn_id
            if 'update bothub.threads set last_seq=' in query:
                return len(events)+1
            if 'select th.owner_id' in query:
                return owner_id
            raise AssertionError(query)

        async def fetch(self, query, *args):
            if 'update bothub.approvals' in query:
                return []
            raise AssertionError(query)

        async def execute(self, query, *args):
            if 'update bothub.bots set need_restart=false' in query:  # исчерпаны повторы остановки
                return 'UPDATE 1'
            if 'update bothub.bots set status=' in query:
                if "status='error_starting'" in query:
                    bot['status'] = 'error_starting'
                elif "status='error'" in query and bot['status'] != 'error_starting':
                    bot['status'] = 'error'
                return 'UPDATE 1'
            if 'insert into bothub.outbox' in query:
                return
            raise AssertionError(query)

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield Connection()

    class Runner:
        async def stop(self, turn):
            raise LauncherTimeout('deadline')

    launcher = FakeLauncherClient()
    for _ in range(3):
        launcher.fail_next(LauncherServerError('still alive'))
    monkeypatch.setattr(main, 'LAUNCHER_STOP_RETRY_DELAY', .01)
    app = main.create_app(launcher=launcher)
    app.state.pool = Pool()
    active = asyncio.create_task(asyncio.Event().wait())
    app.state.running[str(turn_id)] = (active, Runner())
    result = await app.state.stop_turn(turn_id)
    assert result['status'] == 'error' and result['error'] == 'launcher_timeout'
    async with asyncio.timeout(1):
        while not any(event['payload'].get('reason') == 'launcher_stop_failed' for event in events):
            await asyncio.sleep(.01)
    assert sum(call[0] == 'stop_exec' for call in launcher.calls) == 3
    assert bot['status'] == 'error_starting'
    await asyncio.gather(active, return_exceptions=True)

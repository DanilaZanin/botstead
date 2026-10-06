"""Ответ hook и сбой проверки исполнителя у расписаний (docs/contracts.md, раздел 16). Без БД: фейковый пул.

Hook. Проба оценщика: при пропуске тело `{"status":"accepted"}`, при запуске полный turn, поэтому по форме тела внешний отправитель
узнаёт состояние бота (пауза, недоступный исполнитель). Теперь ответ всегда `202 {"status":"accepted"}`, а проверка и постановка
в очередь идут до ответа в обоих случаях.

Расписания. Исключение в `trigger_block` не должно зацикливать расписание: раньше бот не попадал в `blocked`, `next_run_at`
оставался в прошлом, и каждый проход (30 с) снова бросал исключение и писал в журнал. Теперь срабатывание считается
пропущенным с причиной `check_failed`, расписание сдвигается на следующий срок."""
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from bothub import activity
from bothub import main as hub

pytestmark = pytest.mark.pure

OWNER_ID = uuid.UUID(int=1)
SCHEDULE_ID = uuid.uuid4()
THREAD_ID = uuid.uuid4()
TURN_ID = uuid.uuid4()
TOKEN = 'hook-token-1'
NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


class State:
    def __init__(self, *, paused=False, kind='hook', trigger_error=None):
        self.bot = {'id': 'alpha', 'owner_id': OWNER_ID, 'status': 'idle', 'provider_id': None, 'registry_bound': False,
                    'executor': 'container', 'paused': paused, 'model_id': None}
        self.schedule = {'id': SCHEDULE_ID, 'owner_id': OWNER_ID, 'bot_id': 'alpha', 'name': 'Хук', 'kind': kind, 'enabled': True,
                         'hook_token': TOKEN, 'prompt': 'go', 'skipped_count': 0, 'paused_by_unavailable': False, 'catch_up': False,
                         'last_skip_event_at': None, 'last_turn_id': None, 'cron': '0 9 * * *', 'timezone': 'UTC',
                         'next_run_at': NOW - timedelta(minutes=1), 'last_skip_reason': None}
        self.trigger_error = trigger_error
        self.queries = []
        self.last_seq = 0

    def ran(self, text):
        return [(q, a) for q, a in self.queries if text in q]


class Con:
    def __init__(self, state):
        self.s = state

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def fetchrow(self, query, *args):
        s = self.s
        s.queries.append((query, args))
        if 'bothub.settings' in query:
            return None
        if 'from bothub.schedules s join bothub.users u' in query or 'from bothub.schedules where id=$1 for update' in query:
            return dict(s.schedule)
        if 'select * from bothub.bots where id=$1' in query:
            if s.trigger_error:
                raise s.trigger_error
            return dict(s.bot)
        if 'select status,provider_id,registry_bound from bothub.bots' in query:
            return dict(s.bot)
        if 'insert into bothub.turns' in query:
            return {'id': TURN_ID, 'thread_id': args[0], 'prompt': args[1], 'client': args[2], 'status': 'queued', 'turn_type': 'normal'}
        if 'insert into bothub.events' in query:
            return {'id': 1, 'thread_id': str(args[0]), 'seq': args[1], 'turn_id': str(args[2]), 'kind': args[3], 'payload': json.loads(args[6])}
        raise AssertionError('fetchrow: ' + query)

    async def fetchval(self, query, *args):
        s = self.s
        s.queries.append((query, args))
        if 'bothub.settings' in query:
            return True
        if 'insert into bothub.threads' in query:
            return THREAD_ID
        if 'update bothub.threads set last_seq' in query:
            s.last_seq += 1
            return s.last_seq
        raise AssertionError('fetchval: ' + query)

    async def fetch(self, query, *args):
        s = self.s
        s.queries.append((query, args))
        if 'select distinct s.bot_id' in query:
            return [{'bot_id': 'alpha'}]
        if 'for update of s skip locked' in query:
            return [dict(s.schedule)]
        raise AssertionError('fetch: ' + query)

    async def execute(self, query, *args):
        self.s.queries.append((query, args))
        return 'OK'


class Pool:
    def __init__(self, state):
        self.state = state

    @asynccontextmanager
    async def acquire(self):
        yield Con(self.state)


def make_app(state):
    app = hub.create_app(lambda provider: None)
    app.state.pool = Pool(state)
    return app


async def post_hook(state, body=None, token=TOKEN):
    app = make_app(state)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://testserver') as client:
        return await client.post(f'/hooks/{SCHEDULE_ID}', headers={'X-Hook-Token': token, 'content-type': 'application/json'},
                                 content=json.dumps(body or {'order': 7}).encode())


# ---- ответ hook ----

async def test_hook_answers_the_same_body_when_the_turn_is_created_and_when_the_run_is_skipped():
    started = State(paused=False)
    skipped = State(paused=True)
    first, second = await post_hook(started), await post_hook(skipped)
    assert (first.status_code, first.json()) == (202, {'status': 'accepted'})
    assert (second.status_code, second.json()) == (202, {'status': 'accepted'})
    assert first.content == second.content and first.headers['content-type'] == second.headers['content-type']
    # проверка и постановка в очередь сделаны до ответа в обоих случаях
    assert started.ran('insert into bothub.turns') and not started.ran('update bothub.schedules set skipped_count')
    assert skipped.ran('update bothub.schedules set skipped_count') and not skipped.ran('insert into bothub.turns')


async def test_hook_response_has_no_turn_fields_whichever_way_it_went():
    for state in (State(paused=False), State(paused=True)):
        body = (await post_hook(state)).json()
        assert set(body) == {'status'} and 'id' not in body and 'prompt' not in body and 'thread_id' not in body


async def test_hook_with_a_wrong_token_is_still_403_and_does_nothing():
    state = State()
    response = await post_hook(state, token='nope')
    assert response.status_code == 403 and not state.ran('insert into bothub.turns') and not state.ran('update bothub.schedules')


async def test_a_failing_executor_check_in_a_hook_is_a_skip_with_the_same_202():
    state = State(trigger_error=RuntimeError('db hiccup'))
    response = await post_hook(state)
    assert (response.status_code, response.json()) == (202, {'status': 'accepted'})
    (update,) = state.ran('update bothub.schedules set skipped_count')
    assert update[1][3] == 'check_failed' and not state.ran('insert into bothub.turns')


async def test_the_hook_prompt_is_still_built_from_the_body_for_the_created_turn():
    state = State()
    await post_hook(state, {'order': 7})
    ((query, args),) = state.ran('insert into bothub.turns')
    assert args[1] == 'go\n\nДанные события:\n```json\n{"order": 7}\n```' and args[2] == 'hook'


# ---- расписания: сбой проверки исполнителя ----

async def run_pass(state):
    app = make_app(state)
    await app.state.run_due_schedules()


async def test_a_failing_executor_check_skips_the_schedule_and_moves_it_to_the_next_term():
    state = State(kind='cron', trigger_error=RuntimeError('launcher exploded'))
    await run_pass(state)
    (skip,) = state.ran('update bothub.schedules set skipped_count')
    sched_id, count, _, reason, paused, event = skip[1]
    assert (sched_id, count, reason, paused) == (SCHEDULE_ID, 1, 'check_failed', False)
    (moved,) = state.ran('update bothub.schedules set next_run_at')
    assert moved[1][0] == SCHEDULE_ID and moved[1][1] > datetime.now(timezone.utc)  # следующий срок, не прошлое: цикл разорван
    assert not state.ran('insert into bothub.turns')
    (logged,) = state.ran('insert into bothub.activity_log')
    assert logged[1][3:5] == ('schedule', 'schedule_skipped') and json.loads(logged[1][5])['reason'] == 'check_failed'


async def test_a_failing_check_does_not_log_a_traceback_on_every_pass_after_the_first_skip(caplog):
    state = State(kind='cron', trigger_error=RuntimeError('boom'))
    with caplog.at_level('ERROR'):
        await run_pass(state)
    assert sum('trigger_block_failed' in record.getMessage() for record in caplog.records) == 1


async def test_five_failed_checks_in_a_row_pause_the_schedule_and_it_is_probed_every_15_minutes():
    state = State(kind='cron', trigger_error=RuntimeError('boom'))
    state.schedule['skipped_count'] = 4
    await run_pass(state)
    (skip,) = state.ran('update bothub.schedules set skipped_count')
    assert skip[1][1] == 5 and skip[1][4] is True  # paused_by_unavailable
    (moved,) = state.ran('update bothub.schedules set next_run_at')
    assert moved[1][1] >= datetime.now(timezone.utc) + timedelta(minutes=14)


def test_check_failed_is_a_documented_skip_reason_with_text():
    assert 'check_failed' in activity.SKIP_REASONS
    assert 'проверка' in activity.skip_text('check_failed', 1, False)

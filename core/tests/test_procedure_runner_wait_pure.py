"""Ожидание человека не длиннее BOTHUB_PROCEDURE_WAIT_HOURS (по умолчанию 24 ч), docs/contracts.md раздел 14.

Запуск, который стоит в `waiting_human` или `waiting_approval` дольше срока, переходит в `stopped` с кодом `wait_expired`:
брать `in_flight` и подтверждение закрывает, бот освобождается для turn'ов и новых запусков, в тред пишется системное событие
с идентификатором запуска. Расписание, которое наступило у бота с неконечным запуском, откладывается, а не плодит turn'ы.
Без Postgres: хранилище в памяти (procedure_fakes.MemStore). SQL тех же правил: test_procedure_runner_db.py."""
import uuid
from datetime import timedelta

import pytest

from bothub import main as hub
from bothub import procedure_runner as R
from bothub.procedure_runner import ACTIVE, PgStore, RunError
from procedure_fakes import OWNER
from test_procedure_runner_pure import Env, click, env, fast_match  # noqa: F401  (фикстуры)

pytestmark = pytest.mark.pure
WAIT = 'BOTHUB_PROCEDURE_WAIT_HOURS'


@pytest.fixture(autouse=True)
def default_wait(monkeypatch):
    monkeypatch.delenv(WAIT, raising=False)


async def wait_for_owner(env, **fields):
    """Запуск, который встал в waiting_human: на странице нет цели шага (element_not_found)."""
    run = env.run([click('s1')], **fields)
    await env.drive()
    assert (env.status(run), env.get(run)['reason']) == ('waiting_human', 'element_not_found')
    return run


# --- срок из окружения ---------------------------------------------------------------------------------------------------

def test_the_wait_limit_defaults_to_24_hours_and_reads_the_environment(monkeypatch):
    assert R.wait_hours() == 24
    monkeypatch.setenv(WAIT, '6')
    assert R.wait_hours() == 6
    monkeypatch.setenv(WAIT, ' 0.5 ')
    assert R.wait_hours() == 0.5


@pytest.mark.parametrize('value', ['', 'abc', '0', '-1', '-0.5', 'nan', 'inf', '-inf', '1e400', '1e12'])
def test_a_missing_invalid_or_non_positive_limit_falls_back_to_the_default(monkeypatch, value):
    monkeypatch.setenv(WAIT, value)
    assert R.wait_hours() == 24


# --- остановка по сроку ---------------------------------------------------------------------------------------------------

async def test_a_run_waiting_for_the_owner_longer_than_24_hours_is_stopped_with_wait_expired(env):
    run = await wait_for_owner(env)
    env.store.now += timedelta(hours=23, minutes=59)
    await env.drive()
    assert env.status(run) == 'waiting_human'  # срок ещё не вышел
    env.store.now += timedelta(minutes=2)
    await env.drive()
    row = env.get(run)
    assert (row['status'], row['error'], row['reason'], row['in_flight']) == ('stopped', 'wait_expired', None, False)
    assert row['finished_at'] is not None and row['next_step'] == 0 and not env.actions()


async def test_the_limit_comes_from_the_environment_and_a_bad_value_means_24_hours(env, monkeypatch):
    monkeypatch.setenv(WAIT, '1')
    run = await wait_for_owner(env)
    env.store.now += timedelta(minutes=61)
    await env.drive()
    assert (env.status(run), env.get(run)['error']) == ('stopped', 'wait_expired')
    monkeypatch.setenv(WAIT, 'garbage')
    second = await wait_for_owner(env, bot_id='alpha')
    env.store.now += timedelta(hours=23)
    await env.drive()
    assert env.status(second) == 'waiting_human'  # один час при значении «garbage» не срок: действует 24 ч
    env.store.now += timedelta(hours=2)
    await env.drive()
    assert env.status(second) == 'stopped'


async def test_a_run_waiting_for_an_approval_also_stops_when_the_limit_comes_first(env, monkeypatch):
    monkeypatch.setenv(WAIT, '0.5')  # короче срока подтверждения (60 мин)
    env.world.add('button', 'Оплатить картой')
    run = env.run([click('s1', 'Оплатить картой')])
    await env.drive()
    assert env.status(run) == 'waiting_approval'
    approval = env.get(run)['approval_id']
    env.store.now += timedelta(minutes=29)
    await env.drive()
    assert env.status(run) == 'waiting_approval'
    env.store.now += timedelta(minutes=2)
    await env.drive()
    row = env.get(run)
    assert (row['status'], row['error']) == ('stopped', 'wait_expired')
    assert env.store.approvals[approval]['status'] == 'expired' and not env.actions()  # подтверждение закрыто, шаг не выполнен


async def test_the_approval_lifetime_still_wins_when_it_is_shorter_than_the_limit(env):
    env.world.add('button', 'Оплатить картой')
    run = env.run([click('s1', 'Оплатить картой')])
    await env.drive()
    env.store.now += timedelta(minutes=61)
    await env.drive()
    assert (env.status(run), env.get(run)['error']) == ('failed', 'approval_expired')


async def test_the_clock_starts_again_with_every_pause(env):
    run = await wait_for_owner(env)
    env.store.now += timedelta(hours=23)
    await env.drive()
    await env.runner.decide_run(OWNER, run['id'], 'retry')  # шаг идёт снова, цели по-прежнему нет: новая пауза
    await env.drive()
    assert env.status(run) == 'waiting_human'
    env.store.now += timedelta(hours=23)  # с первой паузы прошло 46 ч, со второй 23
    await env.drive()
    assert env.status(run) == 'waiting_human'
    env.store.now += timedelta(hours=2)
    await env.drive()
    assert (env.status(run), env.get(run)['error']) == ('stopped', 'wait_expired')


async def test_a_run_that_is_not_waiting_never_expires_and_a_finished_one_is_not_touched(env):
    env.world.add('button', 'Продолжить')
    done = env.run([click('s1')])
    await env.drive()
    assert env.status(done) == 'done'
    env.store.now += timedelta(hours=100)
    await env.drive()
    assert (env.status(done), env.get(done)['error']) == ('done', None)


async def test_the_owner_can_still_decide_before_the_limit_and_the_late_expiry_loses_the_race(env):
    run = await wait_for_owner(env)
    env.store.now += timedelta(hours=30)
    # владелец остановил запуск между выборкой и остановкой по сроку: условный переход ничего не меняет
    assert await env.store.stop_run(OWNER, run['id']) and env.status(run) == 'stopped'
    assert await env.store.expire_wait(run['id'], env.store.now - timedelta(hours=24)) is None
    assert env.get(run)['error'] is None


# --- turn'ы, браузер, события -------------------------------------------------------------------------------------------

async def test_expiry_frees_the_bot_for_new_runs_and_does_not_depend_on_a_turn_of_that_bot(env):
    run = await wait_for_owner(env)
    proc = env.store.add_procedure([click('s1')], bot_id='alpha')
    with pytest.raises(RunError) as busy:
        await env.runner.create_run(OWNER, proc['id'], 'alpha', None, {})  # пока запуск ждёт, бот занят
    assert busy.value.status == 409
    env.store.turn_bots.add('alpha')  # у бота идёт turn: остановка по сроку от этого не зависит
    env.store.now += timedelta(hours=25)
    await env.drive()
    assert env.status(run) == 'stopped'
    env.store.turn_bots.clear()
    created = await env.runner.create_run(OWNER, proc['id'], 'alpha', None, {})  # бот свободен
    assert created['status'] == 'queued'


async def test_expiry_clears_in_flight_so_that_a_new_pass_does_not_report_an_unknown_outcome(env):
    run = await wait_for_owner(env)
    env.get(run)['in_flight'] = True  # остаток от прошлого шага
    env.store.now += timedelta(hours=25)
    await env.drive()
    row = env.get(run)
    assert (row['status'], row['in_flight'], row['step_log']) == ('stopped', False, [])


async def test_expiry_writes_one_system_note_with_the_run_id_and_no_long_dash(env):
    run = await wait_for_owner(env)
    before = len(env.store.notes)
    env.store.now += timedelta(hours=25)
    await env.drive()
    await env.drive()
    new = env.store.notes[before:]
    assert len(new) == 1  # событие одно, повторный проход его не дублирует
    text = new[0]
    assert str(run['id']) in text and 'wait_expired' in text and '24' in text and chr(0x2014) not in text


async def test_a_frozen_bot_with_no_human_is_released_and_a_human_takeover_is_left_alone():
    env = Env()
    run = await wait_for_owner(env)
    env.get(run)['reason'] = 'bot_frozen'
    env.launcher.frozen.add('alpha')
    env.store.now += timedelta(hours=25)
    await env.drive()
    assert env.status(run) == 'stopped' and 'alpha' not in env.launcher.frozen  # разморозка по сроку
    human = Env(browser_control='human')
    waiting = human.run([click('s1')])
    await human.drive()
    assert (human.status(waiting), human.get(waiting)['reason']) == ('waiting_human', 'browser_human')
    human.launcher.frozen.add('alpha')
    human.store.now += timedelta(hours=25)
    await human.drive()
    assert human.status(waiting) == 'stopped'
    assert 'alpha' in human.launcher.frozen and human.store.bots['alpha']['browser_control'] == 'human'  # человека не трогаем


async def test_a_failing_unfreeze_does_not_undo_the_stop(env):
    from bothub.launcher_client import LauncherUnavailable
    run = await wait_for_owner(env)
    env.get(run)['reason'] = 'bot_frozen'
    env.launcher.fail_next(LauncherUnavailable('down'))
    env.store.now += timedelta(hours=25)
    await env.drive()
    assert (env.status(run), env.get(run)['error']) == ('stopped', 'wait_expired')


async def test_pgstore_note_carries_the_run_id_in_the_event_payload():
    events = []

    class Con:
        pass

    class Pool:
        def acquire(self):
            return self

        async def __aenter__(self):
            return Con()

        async def __aexit__(self, *exc):
            return False

    class Hooks:
        async def append_event(self, con, thread_id, turn_id, kind, actor, payload):
            events.append((thread_id, turn_id, kind, actor, payload))

    run = {'id': uuid.uuid4(), 'thread_id': uuid.uuid4(), 'turn_id': None}
    await PgStore(lambda: Pool(), Hooks()).note(run, 'текст')
    assert events == [(run['thread_id'], None, 'system', 'system', {'text': 'текст', 'run_id': str(run['id'])})]


# --- расписания ----------------------------------------------------------------------------------------------------------

class _Con:
    def __init__(self):
        self.queries = []

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def fetch(self, query, *args):
        self.queries.append(query)
        return []

    async def execute(self, *args):
        return 'OK'

    async def fetchrow(self, *args):
        return None


class _Pool:
    def __init__(self):
        self.con = _Con()

    def acquire(self):
        return self.con


async def test_the_scheduler_query_leaves_out_bots_with_an_unfinished_procedure_run():
    app = hub.create_app(lambda provider: None)
    app.state.pool = _Pool()
    await app.state.run_due_schedules()
    # проход расписаний делает и другие запросы (паузы триггеров, раздел 16): нужен тот, что выбирает сработавшие
    (query,) = [q for q in app.state.pool.con.queries if 'for update of s skip locked' in q]
    # расписание бота с неконечным запуском не выбирается: next_run_at остаётся в прошлом, оно сработает одним turn'ом после
    # освобождения бота (не плодит очередь turn'ов за 24 часа ожидания человека и не теряется)
    assert 'not exists' in query and 'procedure_runs' in query and 'pr.bot_id=s.bot_id' in query
    assert all(f"'{status}'" in query for status in ACTIVE)

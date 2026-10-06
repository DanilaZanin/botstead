"""Пауза бота и запуски процедур (docs/contracts.md, раздел 16, «Пауза бота»). Без Postgres: хранилище в памяти.

Проба оценщика: запуск в `queued`, затем POST /api/bots/alpha/pause: движок всё равно брал запуск (`active_runs`, `begin`).
Теперь движок не начинает и не продолжает шаги запуска бота на паузе; запуск остаётся в своём статусе с `reason=bot_paused`
и идёт дальше после возобновления. Срок ожидания человека (24 ч) на время паузы не тикает. SQL тех же правил: test_stage8_review_db.py."""
from datetime import timedelta

import pytest

from bothub.procedure_runner import RunError
from procedure_fakes import NOW, OWNER
from test_procedure_runner_pure import Env, click, env, fast_match  # noqa: F401  (фикстуры)

pytestmark = pytest.mark.pure


def pause(env, bot_id='alpha', *, at=None):
    env.store.bots[bot_id].update(paused=True, paused_at=at or env.store.now)


def resume(env, bot_id='alpha'):
    env.store.bots[bot_id].update(paused=False, paused_at=None)


def reason_of(env, run):
    return env.get(run)['reason']


async def test_a_queued_run_of_a_paused_bot_is_not_taken_and_shows_why(env):
    env.world.add('button', 'Продолжить')
    run = env.run([click('s1')])
    pause(env)
    await env.drive()
    assert env.status(run) == 'queued' and reason_of(env, run) == 'bot_paused'
    assert not env.world.calls and env.get(run)['started_at'] is None


async def test_the_run_goes_on_after_resume_and_the_reason_is_cleared(env):
    env.world.add('button', 'Продолжить')
    run = env.run([click('s1')])
    pause(env)
    await env.drive()
    resume(env)
    await env.drive()
    row = env.get(run)
    assert (row['status'], row['reason'], row['next_step']) == ('done', None, 1) and env.actions()


async def test_a_running_run_is_held_before_its_next_step(env):
    env.world.add('button', 'Продолжить')
    run = env.run([click('s1')], status='running')
    pause(env)
    await env.drive()
    assert (env.status(run), reason_of(env, run), env.get(run)['next_step']) == ('running', 'bot_paused', 0)
    assert not env.world.calls
    resume(env)
    await env.drive()
    assert env.status(run) == 'done' and reason_of(env, run) is None


async def test_pause_in_the_middle_lets_the_step_finish_and_stops_before_the_next_one(env):
    env.world.add('button', 'Первая').add('button', 'Вторая').add('button', 'Третья')
    # во время действия второго шага владелец ставит бота на паузу: шаг доводится до конца, третий не начинается
    env.world.effects[('click', 'button', 'Вторая')] = lambda w: pause(env)
    run = env.run([click('s1', 'Первая'), click('s2', 'Вторая'), click('s3', 'Третья')])
    await env.drive()
    row = env.get(run)
    assert (row['status'], row['next_step'], row['reason']) == ('running', 2, 'bot_paused')
    assert [e['step_id'] for e in row['step_log']] == ['s1', 's2'] and [e['status'] for e in row['step_log']] == ['ok', 'ok']
    assert len(env.actions()) == 2
    resume(env)
    await env.drive()
    row = env.get(run)
    assert (row['status'], row['next_step'], row['reason']) == ('done', 3, None) and len(env.actions()) == 3


async def test_repeated_passes_while_paused_change_nothing(env):
    env.world.add('button', 'Продолжить')
    run = env.run([click('s1')])
    pause(env)
    for _ in range(5):
        await env.drive()
    assert (env.status(run), reason_of(env, run)) == ('queued', 'bot_paused') and not env.world.calls and not env.runner._tasks


async def test_other_bots_runs_are_not_held(env):
    env.store.add_bot('beta')
    env.launcher.bots['beta'] = type(env.launcher.bots['alpha'])('beta', 'o1')
    env.world.add('button', 'Продолжить')
    held = env.run([click('s1')], bot_id='alpha')
    free = env.run([click('s1')], bot_id='beta')
    pause(env, 'alpha')
    await env.drive()
    assert env.status(held) == 'queued' and env.status(free) == 'done'


async def test_begin_refuses_a_paused_bot_even_if_the_pass_already_chose_the_run(env):
    run = env.run([click('s1')])
    pause(env)
    assert await env.store.begin(run['id']) is False and env.status(run) == 'queued'
    resume(env)
    assert await env.store.begin(run['id']) is True and env.status(run) == 'running'


async def test_a_run_waiting_for_the_owner_is_not_held_by_the_pause_and_keeps_its_own_reason(env):
    run = env.run([click('s1')])
    await env.drive()
    assert (env.status(run), reason_of(env, run)) == ('waiting_human', 'element_not_found')
    pause(env)
    await env.drive()
    assert (env.status(run), reason_of(env, run)) == ('waiting_human', 'element_not_found')


# ---- срок ожидания человека не тикает, пока бот на паузе ----

async def wait_for_owner(env):
    run = env.run([click('s1')])
    await env.drive()
    assert env.status(run) == 'waiting_human'
    return run


async def test_the_wait_limit_does_not_expire_a_run_while_the_bot_is_paused(env):
    run = await wait_for_owner(env)
    pause(env)
    env.store.now += timedelta(hours=50)
    await env.drive()
    assert env.status(run) == 'waiting_human'


async def test_resume_gives_the_run_back_the_time_it_spent_paused(env):
    run = await wait_for_owner(env)
    env.store.now += timedelta(hours=20)  # ждёт 20 ч
    pause(env)
    env.store.now += timedelta(hours=100)  # пауза долгая: в срок не входит
    paused_at = env.store.bots['alpha']['paused_at']
    resume(env)
    await env.store.thaw_waits(None, 'alpha', paused_at)
    await env.drive()
    assert env.status(run) == 'waiting_human'  # 20 ч из 24
    env.store.now += timedelta(hours=3, minutes=59)
    await env.drive()
    assert env.status(run) == 'waiting_human'
    env.store.now += timedelta(minutes=2)
    await env.drive()
    assert (env.status(run), env.get(run)['error']) == ('stopped', 'wait_expired')


async def test_a_wait_that_began_during_the_pause_counts_only_from_the_pause_end(env):
    pause(env)
    paused_at = env.store.now
    env.store.now += timedelta(hours=10)
    run = env.run([click('s1')], status='waiting_human', reason='element_not_found', waiting_since=env.store.now)
    env.store.now += timedelta(hours=5)
    resume(env)
    await env.store.thaw_waits(None, 'alpha', paused_at)
    assert env.get(run)['waiting_since'] == env.store.now  # время паузы после начала ожидания вычтено целиком


async def test_thaw_leaves_other_bots_and_runs_without_a_wait_alone(env):
    env.store.add_bot('beta')
    other = env.run([click('s1')], status='waiting_human', reason='no_page', bot_id='beta', waiting_since=NOW)
    queued = env.run([click('s1')])
    env.store.now += timedelta(hours=9)
    await env.store.thaw_waits(None, 'alpha', NOW)
    assert env.get(other)['waiting_since'] == NOW and env.get(queued)['waiting_since'] is None


# ---- запуск процедуры на паузе: проверка под блокировкой бота ----

async def test_create_run_on_a_paused_bot_is_refused_inside_the_store(env):
    proc = env.store.add_procedure([click('s1')], bot_id='alpha')
    pause(env)
    with pytest.raises(RunError) as refused:
        await env.runner.create_run(OWNER, proc['id'], None, None, {})
    assert (refused.value.status, refused.value.code) == (409, 'bot_paused')
    assert not env.store.runs
    resume(env)
    run = await env.runner.create_run(OWNER, proc['id'], None, None, {})
    assert run['status'] == 'queued'


async def test_a_run_without_a_bot_is_not_affected_by_any_pause(env):
    proc = env.store.add_procedure([click('s1')], bot_id=None)
    pause(env)
    run = await env.runner.create_run(OWNER, proc['id'], None, None, {})
    assert (run['status'], run['error']) == ('failed', 'no_bot')

"""Раунд 2 ревью Opus: коды исполнителя `changed_after`, `stat_not_actionable`, `ambiguous`, повтор шага при `internal_error` до
действия, полный адрес по SHA-256. Без Postgres и Docker: хранилище в памяти и страница-модель из procedure_fakes."""
import hashlib

import pytest

from bothub import procedure_runner as R
from procedure_fakes import OWNER
from test_procedure_runner_pure import SECRET, click, env, fast_match  # noqa: F401  (фикстуры)
from test_procedure_runner_review2_pure import BANK, actions, bank_env, password_step

pytestmark = pytest.mark.pure


def sha(url):
    return hashlib.sha256(url.encode('utf-8')).hexdigest()


# --- ответ исполнителя: url_sha256 и phase ----------------------------------------------------------------------------------

def raw(**fields):
    return {'v': 1, 'ok': True, 'code': None, 'acted': False, 'url': 'https://a.example/', 'url_sha256': sha('https://a.example/'),
            'precondition_visible': None, 'found': None, 'expect': None} | fields


def test_clean_result_keeps_the_address_hash_and_the_phase():
    assert R.clean_result(raw())['url_sha256'] == sha('https://a.example/')
    assert R.clean_result(raw(url_sha256=None))['url_sha256'] is None
    assert R.clean_result({k: v for k, v in raw().items() if k != 'url_sha256'})['url_sha256'] is None  # старый исполнитель
    for bad in ('A' * 64, 'a' * 63, 5, 'g' * 64, 'a' * 65):
        assert R.clean_result(raw(url_sha256=bad)) is None, bad
    crash = R.clean_result(raw(ok=False, code='internal_error', phase='before_action'))
    assert (crash['code'], crash['phase'], crash['acted']) == ('internal_error', 'before_action', False)
    assert R.clean_result(raw(ok=False, code='internal_error', phase='after_action', acted=None))['phase'] == 'after_action'
    assert R.clean_result(raw())['phase'] is None
    assert R.clean_result(raw(phase='whenever')) is None


def test_expected_page_carries_the_hash_of_the_full_address_when_it_is_known():
    long_url = 'https://bank.example/login?t=' + 'x' * 3000
    shown = long_url[:2048]
    got = R.expected_page(shown, {'role': 'textbox', 'name': 'Пароль'}, sha(long_url))
    assert got == {'origin': 'https://bank.example', 'url': shown, 'url_sha256': sha(long_url), 'role': 'textbox', 'name': 'Пароль'}
    assert 'url_sha256' not in R.expected_page('https://bank.example/', None)
    assert 'url_sha256' not in R.expected_page('https://bank.example/', None, None)


async def test_the_action_carries_the_hash_from_the_read_when_the_executor_gave_one(env):
    bank_env(env)
    original = env.world.handler

    def with_hash(payload, dry_run):
        out = original(payload, dry_run)
        if isinstance(out, dict):
            out['url_sha256'] = sha(BANK)
        return out

    env.launcher.procedure_handler = with_hash
    run = env.run([click('s1')])
    env.world.add('button', 'Продолжить')
    await env.drive()
    assert env.status(run) == 'done'
    assert actions(env)[0]['payload']['expected']['url_sha256'] == sha(BANK)
    probe = env.launcher.procedure_steps[0]['payload']
    assert 'expected' not in probe


# --- changed_after, stat_not_actionable, ambiguous --------------------------------------------------------------------------

@pytest.mark.parametrize('acted', [True, None])
async def test_changed_after_is_page_changed_after_action_and_the_step_is_never_repeated_even_if_safe(env, acted):
    env.world.add('button', 'Продолжить')
    run = env.run([click('s1', safe_to_retry=True)])
    env.world.fail_action, env.world.fail_action_acted = 'changed_after', acted
    await env.drive()
    row = env.get(run)
    assert (row['status'], row['reason']) == ('waiting_human', 'page_changed_after_action')
    assert len(actions(env)) == 1  # повторного клика (теперь уже на другой странице) нет


async def test_a_not_actionable_element_pauses_with_a_reason_and_nothing_was_done(env):
    env.world.add('button', 'Продолжить')
    run = env.run([click('s1', safe_to_retry=True)])
    env.world.fail_action, env.world.fail_action_acted = 'stat_not_actionable', False
    await env.drive()
    row = env.get(run)
    assert (row['status'], row['reason'], row['in_flight']) == ('waiting_human', 'element_not_actionable', False)
    assert 'element_not_actionable' in R.REASONS and len(actions(env)) == 1


async def test_two_tabs_with_the_same_address_pause_with_a_reason_on_the_read_and_on_the_action(env):
    env.world.add('button', 'Продолжить')
    original = env.world.handler
    run = env.run([click('s1')])
    env.launcher.procedure_handler = lambda payload, dry_run: env.world._result(ok=False, code='ambiguous') if dry_run \
        else original(payload, dry_run)
    await env.drive()
    assert (env.status(run), env.get(run)['reason']) == ('waiting_human', 'page_ambiguous') and not actions(env)
    assert 'page_ambiguous' in R.REASONS
    second = env.run([click('s1')])
    env.launcher.procedure_handler = original
    env.world.fail_action, env.world.fail_action_acted = 'ambiguous', False
    await env.drive()
    assert (env.status(second), env.get(second)['reason']) == ('waiting_human', 'page_ambiguous')
    assert env.world.acted == []


# --- internal_error до действия повторяется ---------------------------------------------------------------------------------

def crash(env, world_fn, *, phase='before_action', acted=False, times=1, on='probe'):
    """Исполнитель падает `times` раз до действия (чтение) либо в вызове действия, дальше отвечает как страница-модель."""
    original = env.world.handler
    state = {'n': 0}

    def handler(payload, dry_run):
        if (dry_run if on == 'probe' else not dry_run) and state['n'] < times:
            state['n'] += 1
            return env.world._result(ok=False, code='internal_error', acted=acted) | {'phase': phase}
        return original(payload, dry_run)

    env.launcher.procedure_handler = handler
    return state


async def test_an_internal_error_in_the_read_is_retried_by_itself_up_to_twice(env):
    env.world.add('button', 'Продолжить')
    state = crash(env, None, times=2, on='probe')
    run = env.run([click('s1')])  # safe_to_retry по умолчанию False: до действия повтор всё равно безопасен
    await env.drive()
    assert (env.status(run), state['n']) == ('done', 2) and len(env.world.acted) == 1


async def test_an_internal_error_before_the_action_is_retried_for_a_secret_with_a_fresh_approval_and_never_acts_twice(env):
    bank_env(env)
    run = env.run([password_step()])
    await env.drive()
    env.store.approve()
    state = crash(env, None, times=1, on='action')
    await env.drive()
    # подтверждение израсходовано вызовом, который упал до действия: повтор просит новое, значение никуда не ушло
    assert (env.status(run), state['n'], env.world.acted, len(env.store.approvals)) == ('waiting_approval', 1, [], 2)
    env.store.approve()
    await env.drive()
    assert (env.status(run), len(env.world.acted)) == ('done', 1)
    assert SECRET not in env.everything(run)


async def test_internal_error_before_the_action_gives_up_to_the_owner_after_the_retries(env):
    env.world.add('button', 'Продолжить')
    state = crash(env, None, times=10, on='action')
    run = env.run([click('s1')])
    await env.drive()
    row = env.get(run)
    assert (row['status'], row['reason'], row['in_flight']) == ('waiting_human', 'executor_crashed', False)
    assert state['n'] == R.RETRY_MAX + 1 and env.world.acted == [] and 'executor_crashed' in R.REASONS
    probe_crash = crash(env, None, times=10, on='probe')
    second = env.run([click('s1')])
    await env.drive()
    assert (env.status(second), env.get(second)['reason']) == ('waiting_human', 'executor_crashed') and probe_crash['n'] == R.RETRY_MAX + 1


async def test_an_internal_error_after_the_action_started_is_still_an_unknown_outcome_without_retry(env):
    env.world.add('button', 'Продолжить')
    run = env.run([click('s1', safe_to_retry=False)])
    state = crash(env, None, phase='after_action', acted=None, times=10, on='action')
    await env.drive()
    row = env.get(run)
    assert (row['status'], row['reason']) == ('waiting_human', 'unknown_outcome') and state['n'] == 1

"""Переход на другую страницу после действия (docs/contracts.md, раздел 14, «Переход после действия»): click, press, select и fill
без секрета, после которых вкладка оказалась на другом origin, это обычный успех шага (`navigated`, `origin_changed`), а не
неизвестный исход. Ядро проверяет новый адрес на url_forbidden, затем expect по нему же; следующее чтение ждёт дольше.
Без Postgres и Docker: хранилище в памяти, страница-модель procedure_fakes.World по протоколу procedure-step.mjs."""
import pytest

from bothub import procedure_runner as R
from bothub.launcher_client import LauncherTimeout, LauncherUnavailable
from test_procedure_runner_pure import SECRET, click, env, fast_match  # noqa: F401  (фикстуры)
from test_procedure_runner_review2_pure import BANK, EVIL, actions, bank_env, password_step

pytestmark = pytest.mark.pure
OTHER = 'https://other.example/home'


def go_to(url, *appear):
    """Эффект клика: страница ушла на `url`, на ней появились элементы `appear` (роль, имя)."""
    def effect(world):
        world.url = url
        for role, name in appear:
            world.add(role, name)
    return effect


def probes(env, action='click'):
    return [call['payload'] for call in env.launcher.procedure_steps if call['dry_run'] and call['payload']['step']['action'] == action]


# --- чистые части ------------------------------------------------------------------------------------------------------------

def test_clean_result_keeps_navigated_and_origin_changed_as_booleans_and_defaults_them_to_false():
    base = {'v': 1, 'ok': True, 'code': None, 'acted': True, 'url': 'https://a.example/', 'precondition_visible': None,
            'found': None, 'expect': None}
    assert R.clean_result(base)['navigated'] is False and R.clean_result(base)['origin_changed'] is False  # старый исполнитель
    both = R.clean_result({**base, 'navigated': True, 'origin_changed': True})
    assert (both['navigated'], both['origin_changed']) == (True, True)
    for bad in ('yes', 1, None, [], {}):
        assert R.clean_result({**base, 'navigated': bad}) is None, bad
        assert R.clean_result({**base, 'origin_changed': bad}) is None, bad


def test_the_settle_after_a_navigation_is_longer_than_the_usual_one_and_fits_the_executor_limit():
    assert R.SETTLE_MS < R.SETTLE_AFTER_NAV_MS <= 5000  # 5000: предел settle_ms в procedure-step.mjs
    assert 'page_changed_after_action' in R.REASONS


# --- пункт 2: переход на другой origin это успех шага -----------------------------------------------------------------------

async def test_a_click_that_leaves_for_another_origin_with_a_met_expect_url_matches_goes_on_with_the_run(env):
    env.world.add('button', 'Продолжить')
    env.world.effects[('click', 'button', 'Продолжить')] = go_to(OTHER, ('button', 'Дальше'))
    run = env.run([click('s1', expect={'url_matches': r'^https://other\.example/'}), click('s2', name='Дальше')])
    await env.drive()
    row = env.get(run)
    assert (row['status'], row['next_step'], row['error'], row['reason'], row['in_flight']) == ('done', 2, None, None, False)
    assert [(e['step_id'], e['status']) for e in row['step_log']] == [('s1', 'ok'), ('s2', 'ok')]
    assert len(actions(env)) == 2 and env.world.url == OTHER
    assert not [n for n in env.store.notes if 'unknown_outcome' in n]


async def test_a_click_that_leaves_for_another_origin_without_expect_goes_on_with_the_run(env):
    env.world.add('button', 'Продолжить')
    env.world.effects[('click', 'button', 'Продолжить')] = go_to(OTHER, ('button', 'Дальше'))
    run = env.run([click('s1'), click('s2', name='Дальше')])
    await env.drive()
    row = env.get(run)
    assert (row['status'], row['next_step'], row['reason']) == ('done', 2, None)
    assert len(actions(env)) == 2 and env.world.url == OTHER


@pytest.mark.parametrize('action,effect_key', [('press', ('press', 'textbox', 'Поиск')), ('select', ('select', 'combobox', 'Город'))])
async def test_press_and_select_that_leave_for_another_origin_are_a_success_too(env, action, effect_key):
    role, name = effect_key[1:]
    env.world.add(role, name)
    env.world.effects[effect_key] = go_to(OTHER)
    step = {'id': 's1', 'action': action, 'target': {'role': role, 'name': name}, 'value': 'Enter' if action == 'press' else 'Москва',
            'safe_to_retry': False}
    run = env.run([step])
    await env.drive()
    assert (env.status(run), env.get(run)['reason']) == ('done', None) and env.world.url == OTHER


# --- пункт 2: новый адрес проходит url_forbidden ----------------------------------------------------------------------------

@pytest.mark.parametrize('target', ['http://169.254.169.254/latest/meta-data', 'http://127.0.0.1:8000/admin', 'ftp://files.example/x'])
async def test_a_click_that_lands_on_a_forbidden_address_fails_the_run_and_does_not_go_on(env, target):
    env.world.add('button', 'Продолжить')
    env.world.effects[('click', 'button', 'Продолжить')] = go_to(target, ('button', 'Дальше'))
    run = env.run([click('s1', expect={'url_matches': r'.*'}), click('s2', name='Дальше')])
    await env.drive()
    row = env.get(run)
    assert (row['status'], row['error'], row['next_step'], row['in_flight']) == ('failed', 'url_forbidden', 0, False)
    assert [(e['step_id'], e['status'], e['error']) for e in row['step_log']] == [('s1', 'failed', 'url_forbidden')]
    assert len(actions(env)) == 1  # второй шаг на запрещённой странице не начат, первый не повторён


async def test_an_address_that_did_not_change_is_not_judged_by_url_forbidden(env):
    env.world.url = 'https://example.com/login'
    env.world.add('button', 'Продолжить')
    run = env.run([click('s1')])
    await env.drive()
    assert env.status(run) == 'done'


# --- пункт 3: секрет и смена origin ---------------------------------------------------------------------------------------------

async def test_a_secret_fill_that_changes_the_origin_waits_for_the_owner_with_page_changed_after_action(env):
    bank_env(env)
    run = env.run([password_step(), click('s2')])
    await env.drive()
    env.store.approve()
    env.world.url_after_fill = EVIL
    await env.drive()
    row = env.get(run)
    assert (row['status'], row['reason'], row['in_flight'], row['next_step']) == ('waiting_human', 'page_changed_after_action', False, 0)
    assert len(actions(env)) == 1 and len(env.world.acted) == 1 and SECRET not in env.everything(run)
    assert actions(env)[0]['payload']['secret_input'] is True


async def test_a_secret_fill_that_stays_on_the_origin_goes_on_even_when_the_page_navigates_inside_the_site(env):
    bank_env(env)
    run = env.run([password_step()])
    await env.drive()
    env.store.approve()
    env.world.url_after_fill = 'https://bank.example/next'
    await env.drive()
    assert (env.status(run), env.get(run)['reason']) == ('done', None)


async def test_an_executor_that_reports_a_secret_fill_as_ok_with_origin_changed_is_still_stopped(env):
    bank_env(env)
    run = env.run([password_step()])
    await env.drive()
    env.store.approve()
    original = env.world.handler

    def lenient(payload, dry_run):
        out = original(payload, dry_run)
        if not dry_run and isinstance(out, dict):
            out.update(ok=True, code=None, acted=True, navigated=True, origin_changed=True)
        return out

    env.launcher.procedure_handler = lenient
    env.world.url_after_fill = EVIL
    await env.drive()
    assert (env.status(run), env.get(run)['reason']) == ('waiting_human', 'page_changed_after_action')


async def test_changed_after_from_any_executor_is_page_changed_after_action_and_is_never_repeated(env):
    env.world.add('button', 'Продолжить')
    run = env.run([click('s1', safe_to_retry=True)])
    env.world.fail_action, env.world.fail_action_acted = 'changed_after', True
    await env.drive()
    row = env.get(run)
    assert (row['status'], row['reason'], row['in_flight']) == ('waiting_human', 'page_changed_after_action', False)
    assert len(actions(env)) == 1


# --- пункт 4: ожидание в чтении, а не в действии --------------------------------------------------------------------------------

async def test_the_step_after_a_navigation_reads_with_a_longer_settle_and_the_next_one_goes_back_to_the_usual(env):
    env.world.add('button', 'Продолжить')
    env.world.effects[('click', 'button', 'Продолжить')] = go_to(OTHER, ('button', 'Дальше'), ('button', 'Ещё'))
    run = env.run([click('s1'), click('s2', name='Дальше'), click('s3', name='Ещё')])
    await env.drive()
    assert env.status(run) == 'done'
    assert [p['settle_ms'] for p in probes(env)] == [R.SETTLE_MS, R.SETTLE_AFTER_NAV_MS, R.SETTLE_MS]
    assert all(p['settle_ms'] == 0 for p in probes(env, 'wait'))  # проверки expect опрашивают сами, без ожидания в вызове
    assert all(call['payload']['settle_ms'] == 0 for call in actions(env))  # ожидания в действии нет


async def test_the_longer_settle_is_not_spent_after_a_step_that_stayed_on_the_page(env):
    env.world.add('button', 'Продолжить')
    run = env.run([click('s1'), click('s2')])
    await env.drive()
    assert [p['settle_ms'] for p in probes(env)] == [R.SETTLE_MS, R.SETTLE_MS]


# --- провал expect и повтор ---------------------------------------------------------------------------------------------------

async def test_an_action_that_navigated_and_missed_its_expect_is_not_repeated_when_it_is_not_safe_to_retry(env):
    env.world.add('button', 'Продолжить')
    env.world.effects[('click', 'button', 'Продолжить')] = go_to(OTHER)
    run = env.run([click('s1', safe_to_retry=False, expect={'url_matches': r'^https://example\.com/', 'timeout_ms': 1000})])
    await env.drive()
    row = env.get(run)
    assert (row['status'], row['error'], row['attempt'], row['in_flight']) == ('failed', 'expect_failed', 0, False)
    assert len(actions(env)) == 1 and len(env.world.acted) == 1  # повторного клика (теперь на чужой странице) нет


async def test_a_safe_to_retry_step_is_retried_after_a_missed_expect_at_most_retry_max_times(env):
    env.world.add('button', 'Продолжить')
    env.world.effects[('click', 'button', 'Продолжить')] = go_to(OTHER)
    run = env.run([click('s1', safe_to_retry=True, expect={'url_matches': r'^https://example\.com/', 'timeout_ms': 500})])
    await env.drive()
    assert (env.status(run), env.get(run)['error']) == ('failed', 'expect_failed')
    assert len(actions(env)) <= R.RETRY_MAX + 1


# --- пункт 5: неизвестный исход только при обрыве, таймауте и падении исполнителя -----------------------------------------------

@pytest.mark.parametrize('trouble', ['lost', 'unavailable', 'timeout', 'crash_after_action', 'action_failed'])
async def test_an_unknown_outcome_comes_only_from_a_broken_link_a_timeout_or_an_executor_failure(env, trouble):
    env.world.add('button', 'Продолжить')
    env.world.effects[('click', 'button', 'Продолжить')] = go_to(OTHER)
    run = env.run([click('s1')])
    if trouble == 'lost':
        env.world.lost_on_action = True
    elif trouble == 'unavailable':
        env.world.raise_on_action = LauncherUnavailable('gone', code='unavailable')
    elif trouble == 'timeout':
        env.world.raise_on_action = LauncherTimeout('slow', code='timeout')
    elif trouble == 'crash_after_action':
        env.world.fail_action, env.world.fail_action_acted = 'internal_error', None
    else:
        env.world.fail_action, env.world.fail_action_acted = 'action_failed', None
    await env.drive()
    row = env.get(run)
    assert (row['status'], row['reason']) == ('waiting_human', 'unknown_outcome') and len(actions(env)) == 1


async def test_a_navigation_alone_never_makes_an_unknown_outcome(env):
    env.world.add('button', 'Продолжить')
    env.world.effects[('click', 'button', 'Продолжить')] = go_to(OTHER)
    run = env.run([click('s1', safe_to_retry=True)])
    await env.drive()
    row = env.get(run)
    assert (row['status'], row['reason'], row['attempt']) == ('done', None, 0) and len(actions(env)) == 1

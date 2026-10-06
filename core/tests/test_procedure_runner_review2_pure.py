"""Правки по ревью Opus движка воспроизведения (пункты 1–4 и 11): подтверждение привязано к origin страницы, страница
сверяется в вызове действия, признак in_flight, один запуск на бота и взаимное исключение с turn, остановка шага в полёте.
Проверки без Postgres и Docker: хранилище в памяти, страница-модель (procedure_fakes.World) с протоколом исполнителя.
SQL тех же правил: test_procedure_runner_db.py."""
import json
import logging

import pytest

import test_procedure_runner_pure as T
from bothub import procedure_runner as R
from bothub import procedures
from bothub.launcher_client import LauncherNotFound
from bothub.procedure_runner import RunError, action_payload, approval_args, approval_title, args_hash, expected_page
from procedure_fakes import OWNER
from test_procedure_runner_pure import CTX, SECRET, Env, click, env, fast_match, nav  # noqa: F401  (фикстуры)

pytestmark = pytest.mark.pure
BANK = 'https://bank.example/login'
EVIL = 'https://evil.example/phish'
HOMOGLYPH_HOST = 'b' + chr(0x430) + 'nk.example'  # кириллическая «а» вместо латинской
PUNYCODE_HOST = HOMOGLYPH_HOST.encode('idna').decode()


def password_step(**extra):
    step = {'id': 's1', 'action': 'fill', 'target': {'role': 'textbox', 'name': 'Пароль'}, 'secret_ref': 'vault:bank',
            'safe_to_retry': False, 'precondition': {'url_matches': r'^https://'}}
    return step | extra


def bank_env(env):
    env.store.secrets[(OWNER, 'alpha', 'bank')] = SECRET
    env.world.url = BANK
    env.world.add('textbox', 'Пароль')
    return env


def actions(env):
    return [call for call in env.launcher.procedure_steps if not call['dry_run']]


# --- пункт 1: подтверждение привязано к адресу страницы ----------------------------------------------------------------------

def test_args_title_and_hash_carry_the_origin_and_differ_between_pages():
    run = {'id': T.uuid.uuid4(), 'steps': [1, 2], 'procedure_name': 'Вход'}
    step = {'id': 's1', 'action': 'click', 'target': {'role': 'button', 'name': 'Оплатить'}}
    live = {'role': 'button', 'name': 'Оплатить'}
    good = approval_args(run, 0, step, live, 'pay', [], set(), origin='https://shop.example')
    other = approval_args(run, 0, step, live, 'pay', [], set(), origin='https://evil.example')
    assert good['origin'] == 'https://shop.example' and args_hash(good) != args_hash(other)
    assert args_hash(good) == args_hash(dict(reversed(list(good.items()))))  # порядок ключей хэш не меняет
    title = approval_title(run, 0, step, live, origin='https://shop.example')
    assert title == 'Процедура «Вход», шаг 1 из 2: нажать «Оплатить» на https://shop.example'
    assert approval_title(run, 0, step, live) == 'Процедура «Вход», шаг 1 из 2: нажать «Оплатить»'  # без страницы как раньше
    assert approval_args(run, 0, step, live, 'pay', [], set())['origin'] is None


def test_a_long_label_is_cut_but_never_the_origin_or_the_secret_phrase():
    run = {'id': T.uuid.uuid4(), 'steps': [1], 'procedure_name': 'П' * 300}
    step = {'id': 's1', 'action': 'fill', 'target': {'role': 'textbox', 'name': 'x'}, 'value': 'v'}
    title = approval_title(run, 0, step, {'role': 'textbox', 'name': 'Н' * 500}, origin=f'https://{PUNYCODE_HOST}', secret=True)
    assert len(title) <= R.TITLE_MAX
    assert title.endswith(f' на https://{PUNYCODE_HOST}.' + R.SECRET_PHRASE) and 'ввести секрет' not in title[:20]


async def test_the_approval_shows_the_punycode_origin_and_nothing_from_the_query(env):
    env.world.url = f'https://{HOMOGLYPH_HOST}:8443/pay?token=T0KEN#frag'
    env.world.add('button', 'Оплатить картой')
    run = env.run([click('s1', 'Оплатить картой')])
    await env.drive()
    approval = list(env.store.approvals.values())[0]
    origin = approval['args']['origin']
    assert origin == f'https://{PUNYCODE_HOST}:8443' and origin.startswith('https://xn--') and origin.isascii()
    assert approval['title'].endswith(f' на {origin}') and 'T0KEN' not in env.everything(run)
    assert [c['payload']['expected']['origin'] for c in env.launcher.procedure_steps if not c['dry_run']] == []  # действия ещё нет


async def test_a_page_change_after_consent_expires_the_approval_and_the_secret_is_not_typed(env):
    """Проба P1 оценщика: подтвердили ввод на bank.example, страница стала evil.example с той же подписью."""
    bank_env(env)
    run = env.run([password_step()])
    await env.drive()
    first = list(env.store.approvals.values())[0]
    assert first['args']['origin'] == 'https://bank.example' and 'bank.example' in first['title']
    env.store.approve()
    env.world.url = EVIL  # страница ушла на другой origin, подпись «Пароль» та же
    env.world.pages = None
    await env.drive()
    assert env.status(run) == 'waiting_approval' and env.world.acted == [] and not actions(env)
    assert first['status'] == 'expired' and len(env.store.approvals) == 2
    second = list(env.store.approvals.values())[1]
    assert second['args']['origin'] == 'https://evil.example' and 'evil.example' in second['title']
    assert args_hash(first['args']) != args_hash(second['args']) and SECRET not in env.everything(run)


async def test_the_same_origin_and_label_keep_the_approval_valid(env):
    bank_env(env)
    run = env.run([password_step()])
    await env.drive()
    env.store.approve()
    await env.drive()
    assert env.status(run) == 'done' and env.world.acted == [('fill', ('textbox', 'Пароль'), SECRET)] and len(env.store.approvals) == 1


async def test_a_secret_step_asks_every_run_even_when_the_field_looks_harmless(env):
    """Секрет в поле «Комментарий» риска не имеет (`risk: none`), но ввод из хранилища подтверждается всегда."""
    env.store.secrets[(OWNER, 'alpha', 'note')] = SECRET
    env.world.add('textbox', 'Комментарий')
    step = {'id': 's1', 'action': 'fill', 'target': {'role': 'textbox', 'name': 'Комментарий'}, 'secret_ref': 'vault:note',
            'precondition': CTX}
    first = env.run([step])
    assert env.get(first)['steps'][0]['risk'] == 'none'
    await env.drive()
    approval = list(env.store.approvals.values())[0]
    assert approval['risk'] == 'login' and approval['args']['secret_input'] is True and approval['args']['value'] == '[redacted]'
    assert 'ввести секрет' in approval['title'] and 'код бота' in approval['title'] and 'example.com' in approval['title']
    assert env.status(first) == 'waiting_approval' and not actions(env) and SECRET not in env.everything(first)
    env.store.approve()
    await env.drive()
    assert env.status(first) == 'done' and env.world.acted == [('fill', ('textbox', 'Комментарий'), SECRET)]
    second = env.run([step])  # второй запуск того же шага просит своё подтверждение: прежнее израсходовано
    await env.drive()
    assert env.status(second) == 'waiting_approval' and len(env.store.approvals) == 2 and len(actions(env)) == 1


async def test_a_secret_parameter_step_asks_too_and_the_old_approval_without_the_secret_flag_is_not_enough(env):
    env.store.secrets[(OWNER, None, 'shared')] = SECRET
    env.world.add('textbox', 'Комментарий')
    step = {'id': 's1', 'action': 'fill', 'target': {'role': 'textbox', 'name': 'Комментарий'}, 'value': '{{note}}', 'precondition': CTX}
    run = env.run([step], params={'note': 'vault:shared'}, secret_params=['note'], declared=[{'name': 'note', 'secret': True}])
    await env.drive()
    approval = list(env.store.approvals.values())[0]
    assert approval['args']['secret_input'] is True and env.status(run) == 'waiting_approval'
    legacy = {k: v for k, v in approval['args'].items() if k != 'secret_input'}
    approval['args'] = legacy  # одобрение без признака секрета (записанное до правки) не принимается
    env.store.approve()
    await env.drive()
    assert env.world.acted == [] and len(env.store.approvals) == 2 and approval['status'] == 'expired'


async def test_a_secret_is_never_typed_on_a_page_without_an_http_origin(env):
    env.store.secrets[(OWNER, 'alpha', 'bank')] = SECRET
    env.world.url = 'about:blank'
    env.world.add('textbox', 'Пароль')
    run = env.run([password_step(precondition={'url_matches': r'^about:'})])  # условие шага выполнено, адреса http(s) всё равно нет
    await env.drive()
    assert (env.status(run), env.get(run)['reason']) == ('waiting_human', 'precondition_failed')
    assert env.world.acted == [] and not env.store.approvals and len(env.launcher.procedure_steps) == 1  # только чтение


# --- пункт 2: сверка в вызове действия ----------------------------------------------------------------------------------------

def test_page_origin_is_scheme_host_port_in_punycode():
    cyr = f'https://{HOMOGLYPH_HOST}/x'
    cases = {'https://Bank.Example/login?x=1#f': 'https://bank.example', 'http://bank.example:80/': 'http://bank.example',
             'https://bank.example:443/': 'https://bank.example', 'https://bank.example:8443/a': 'https://bank.example:8443',
             'https://u:p@bank.example/': 'https://bank.example', cyr: f'https://{PUNYCODE_HOST}',
             'https://[::1]:3000/': 'https://[::1]:3000', 'about:blank': None, 'file:///etc/passwd': None, 'javascript:1': None,
             '': None, None: None, 'https://': None, 'https://bank.example:99999/': None}
    for url, origin in cases.items():
        assert procedures.page_origin(url) == origin, url


async def test_the_action_call_carries_expected_from_the_read_and_the_read_does_not(env):
    env.world.url = 'https://example.com/login?a=1'
    env.world.add('button', 'Продолжить')
    run = env.run([click('s1')])
    await env.drive()
    assert env.status(run) == 'done'
    probe, act = env.launcher.procedure_steps[0], env.launcher.procedure_steps[1]
    assert 'expected' not in probe['payload'] and 'secret_input' not in act['payload']
    assert act['payload']['expected'] == {'origin': 'https://example.com', 'url': 'https://example.com/login?a=1',
                                          'role': 'button', 'name': 'Продолжить'}
    verify = env.launcher.procedure_steps[2:]
    assert all('expected' not in call['payload'] for call in verify)


async def test_the_first_navigation_has_no_expected_only_when_the_read_has_no_url(env):
    env.world.url = None
    run = env.run([nav('s1', 'https://example.com/')])
    await env.drive()
    assert env.status(run) == 'done' and 'expected' not in actions(env)[0]['payload']
    env.world.url = 'about:blank'
    second = env.run([nav('s1', 'https://example.com/')])
    await env.drive()
    assert actions(env)[1]['payload']['expected'] == {'origin': None, 'url': 'about:blank', 'role': None, 'name': None}
    assert env.status(second) == 'done'
    env.world.url = None
    third = env.run([click('s1')])
    env.world.add('button', 'Продолжить')
    await env.drive()
    assert (env.status(third), env.get(third)['reason']) == ('waiting_human', 'no_page')  # кроме navigate, без адреса не действуем


async def test_a_redirect_between_the_read_and_the_action_is_page_changed_and_nothing_happens(env):
    """Проба P2 оценщика: precondition url_matches проверен по чтению, а между чтением и действием страница ушла."""
    bank_env(env)
    run = env.run([password_step(safe_to_retry=True)])
    await env.drive()
    env.store.approve()
    original = env.world.handler

    def redirect(payload, dry_run):
        if not dry_run:
            env.world.url = EVIL
        return original(payload, dry_run)

    env.launcher.procedure_handler = redirect
    await env.drive()
    row = env.get(run)
    assert (row['status'], row['reason'], row['in_flight']) == ('waiting_human', 'page_changed', False)
    assert env.world.acted == [] and len(actions(env)) == 1 and 'page_changed' in R.REASONS
    expected = actions(env)[0]['payload']['expected']
    assert expected['url'] == BANK and expected['origin'] == 'https://bank.example'  # то, что одобрил владелец
    assert SECRET not in env.everything(run)


async def test_a_renamed_element_between_the_read_and_the_action_is_changed(env):
    env.world.selectors['#go'] = {'role': 'button', 'name': 'Продолжить', 'count': 1}
    run = env.run([{'id': 's1', 'action': 'click', 'target': {'selector': '#go'}, 'safe_to_retry': False}])
    original = env.world.handler

    def rename(payload, dry_run):
        if not dry_run:
            env.world.selectors['#go']['name'] = 'Оплатить'
        return original(payload, dry_run)

    env.launcher.procedure_handler = rename
    await env.drive()
    assert (env.status(run), env.get(run)['reason']) == ('waiting_human', 'page_changed') and env.world.acted == []


async def test_a_secret_fill_asks_the_script_to_read_the_address_again_and_an_origin_change_is_page_changed_after_action(env):
    bank_env(env)
    run = env.run([password_step()])
    await env.drive()
    env.store.approve()
    env.store.runs[run['id']]['steps'][0]['safe_to_retry'] = True  # данные, сохранённые до правки: повторять всё равно нельзя
    env.world.url_after_fill = EVIL
    await env.drive()
    assert actions(env)[0]['payload']['secret_input'] is True
    row = env.get(run)
    assert (row['status'], row['reason'], row['in_flight']) == ('waiting_human', 'page_changed_after_action', False)
    assert len(env.world.acted) == 1 and len(actions(env)) == 1 and SECRET not in env.everything(run)


# --- пункт 3: in_flight ------------------------------------------------------------------------------------------------------

async def test_a_crash_right_after_the_click_never_repeats_it(env):
    """Проба P5 оценщика: сбой базы сразу после клика; следующий проход не имеет права кликнуть ещё раз."""
    env.world.add('button', 'Продолжить')
    run = env.run([click('s1')])
    original = env.store.advance
    state = {'n': 0}

    async def flaky(run_id, index, entry, *, last):
        if state['n'] == 0:
            state['n'] += 1
            raise ConnectionError('db blip')
        return await original(run_id, index, entry, last=last)

    env.store.advance = flaky
    await env.drive()
    row = env.get(run)
    assert (row['status'], row['reason'], row['in_flight'], row['next_step']) == ('waiting_human', 'unknown_outcome', False, 0)
    assert len(env.actions()) == 1 and len(env.world.acted) == 1 and row['step_log'] == []


async def test_any_pass_that_finds_in_flight_set_asks_the_owner_even_for_a_safe_step(env):
    env.world.add('button', 'Продолжить')
    safe = env.run([click('s1', safe_to_retry=True)], status='running', in_flight=True)
    unsafe = env.run([click('s1')], status='running', in_flight=True)
    await env.drive()
    for run in (safe, unsafe):
        row = env.get(run)
        assert (row['status'], row['reason'], row['in_flight']) == ('waiting_human', 'unknown_outcome', False)
    assert not env.launcher.procedure_steps  # браузер не тронут


async def test_a_crash_while_checking_expect_leaves_the_flag_and_the_next_pass_asks(env):
    env.world.add('button', 'Продолжить')
    run = env.run([click('s1', expect={'text': 'Готово'})])
    env.world.texts.add('Готово')

    async def broken(*args, **kwargs):
        raise RuntimeError('boom')

    original = env.runner._verify
    env.runner._verify = broken
    await env.drive(rounds=1, settle=False)  # один проход: действие выполнено, проверка expect упала
    assert env.get(run)['in_flight'] is True and env.status(run) == 'running'
    env.runner._verify = original
    await env.drive()
    assert (env.status(run), env.get(run)['reason']) == ('waiting_human', 'unknown_outcome') and len(env.actions()) == 1


async def test_the_flag_is_raised_before_the_action_and_cleared_only_with_next_step_and_the_log(env):
    env.world.add('button', 'Продолжить')
    run = env.run([click('s1', expect={'text': 'Готово'})])
    env.world.texts.add('Готово')
    flag_calls, advances = [], []
    set_flag, advance = env.store.set_in_flight, env.store.advance

    async def watch_flag(run_id, flag):
        flag_calls.append(flag)
        return await set_flag(run_id, flag)

    async def watch_advance(run_id, index, entry, *, last):
        advances.append((env.get(run)['in_flight'], env.get(run)['next_step'], len(env.get(run)['step_log'])))
        return await advance(run_id, index, entry, last=last)

    env.store.set_in_flight, env.store.advance = watch_flag, watch_advance
    seen = []
    base = env.world.handler
    env.launcher.procedure_handler = lambda payload, dry_run: (seen.append((dry_run, payload['step']['action'], env.get(run)['in_flight'])),
                                                               base(payload, dry_run))[1]
    await env.drive()
    assert flag_calls == [True]  # снимает не отдельная запись, а advance
    assert advances == [(True, 0, 0)] and env.get(run)['in_flight'] is False and env.get(run)['next_step'] == 1
    assert seen[:2] == [(True, 'click', False), (False, 'click', True)] and (True, 'wait', True) in seen


# --- пункт 4: конкуренция за браузер -----------------------------------------------------------------------------------------

async def test_a_second_run_for_a_bot_with_an_unfinished_run_is_409_until_it_ends(env):
    proc = env.store.add_procedure([click('s1')], bot_id='alpha')
    first = await env.runner.create_run(OWNER, proc['id'], None, None, {})
    for status in ('queued', 'running', 'waiting_approval', 'waiting_human'):
        env.store.runs[first['id']]['status'] = status
        with pytest.raises(RunError) as exc:
            await env.runner.create_run(OWNER, proc['id'], None, None, {})
        assert (exc.value.status, exc.value.code) == (409, 'conflict'), status
    assert len(env.store.runs) == 1
    env.store.add_bot('beta')
    other = await env.runner.create_run(OWNER, proc['id'], 'beta', None, {})  # другой бот: свой браузер
    assert other['bot_id'] == 'beta'
    for status in ('done', 'failed', 'stopped'):
        env.store.runs[first['id']]['status'] = status
        again = await env.runner.create_run(OWNER, proc['id'], None, None, {})
        env.store.runs[again['id']]['status'] = 'done'
    nobody = env.store.add_procedure([click('s1')], name='Без бота')
    for _ in range(2):  # запуск без бота сразу failed и бота не занимает
        assert (await env.runner.create_run(OWNER, nobody['id'], None, None, {}))['status'] == 'failed'


async def test_the_engine_does_not_start_a_step_while_the_bot_has_a_turn(env):
    env.world.add('button', 'Продолжить')
    run = env.run([click('s1'), click('s2')])
    env.store.turn_bots.add('alpha')
    await env.drive(rounds=5, settle=False)
    assert env.status(run) == 'queued' and not env.launcher.procedure_steps and env.get(run)['started_at'] is None
    env.store.turn_bots.discard('alpha')
    base = env.world.handler

    def turn_arrives(payload, dry_run):
        out = base(payload, dry_run)
        if not dry_run:
            env.store.turn_bots.add('alpha')  # turn появился сразу после первого шага
        return out

    env.launcher.procedure_handler = turn_arrives
    await env.drive(rounds=5, settle=False)
    assert env.status(run) == 'running' and env.get(run)['next_step'] == 1 and len(env.actions()) == 1
    env.store.turn_bots.discard('alpha')
    await env.drive(rounds=1, settle=False)
    env.store.turn_bots.discard('alpha')
    await env.drive()
    assert env.status(run) == 'done' and len(env.actions()) == 2


async def test_a_turn_of_another_bot_does_not_block_the_run(env):
    env.world.add('button', 'Продолжить')
    env.store.turn_bots.add('beta')
    run = env.run([click('s1')])
    await env.drive()
    assert env.status(run) == 'done'


def test_the_worker_claim_query_skips_a_bot_with_an_unfinished_run():
    """SQL проверяет test_procedure_runner_db.py; здесь только то, что условие осталось в запросе захвата turn."""
    import inspect

    from bothub import main
    source = inspect.getsource(main)
    assert 'from bothub.procedure_runs pr where pr.bot_id=th.bot_id and pr.status in' in source
    assert "('queued','running','waiting_approval','waiting_model','waiting_human')) \"" in source


# --- пункт 11: остановка шага в полёте ---------------------------------------------------------------------------------------

async def test_stopping_a_run_with_the_step_in_flight_cancels_the_executor_and_marks_the_step_unknown(env):
    env.world.add('button', 'Продолжить')
    run = env.run([click('s1'), click('s2')], status='running', in_flight=True)
    stopped = await env.runner.stop_run(OWNER, run['id'])
    assert stopped['status'] == 'stopped' and stopped['in_flight'] is False
    assert env.launcher.cancelled == [('alpha', f'proc-{run["id"].hex}')]
    assert [(e['step_id'], e['status'], e['error']) for e in stopped['step_log']] == [('s1', 'failed', 'unknown_outcome')]
    assert not env.launcher.procedure_steps and 'остановлен' in env.store.notes[-1]


async def test_stopping_a_run_without_a_step_in_flight_writes_no_unknown_outcome_and_survives_a_cancel_error(env):
    idle = env.run([click('s1')], status='waiting_human', reason='element_not_found')
    stopped = await env.runner.stop_run(OWNER, idle['id'])
    assert stopped['step_log'] == [] and env.launcher.cancelled == []
    flying = env.run([click('s1')], status='running', in_flight=True)
    del env.launcher.bots['alpha']  # лаунчер не знает бота: отмена вернёт not_found, остановка владельцем всё равно проходит
    stopped = await env.runner.stop_run(OWNER, flying['id'])
    assert stopped['status'] == 'stopped' and stopped['step_log'][0]['error'] == 'unknown_outcome'


async def test_stopping_works_when_the_launcher_is_not_connected(env):
    flying = env.run([click('s1')], status='running', in_flight=True)
    env.runner._launcher = lambda: None
    assert (await env.runner.stop_run(OWNER, flying['id']))['status'] == 'stopped'


# --- проверки мутаций оценщика: тест секрета ловит утечки ---------------------------------------------------------------------

def leaky_args(original):
    def wrapper(*args, **kwargs):
        out = original(*args, **kwargs)
        out['value'] = args[2].get('value')
        return out
    return wrapper


async def test_the_secret_test_catches_an_approval_that_keeps_the_typed_value(env, caplog, monkeypatch):
    monkeypatch.setattr(R, 'approval_args', leaky_args(R.approval_args))
    with pytest.raises(AssertionError):
        await T.test_a_secret_reaches_only_the_action_call_and_is_nowhere_else(env, caplog)


async def test_the_secret_test_catches_a_probe_that_carries_the_value(env, caplog, monkeypatch):
    original = R.probe_payload

    def leaky(step, **kwargs):
        out = original(step, **kwargs)
        out['step']['value'] = step.get('value')
        return out

    monkeypatch.setattr(R, 'probe_payload', leaky)
    with pytest.raises(AssertionError):
        await T.test_a_secret_reaches_only_the_action_call_and_is_nowhere_else(env, caplog)


async def test_the_secret_test_catches_a_log_line_with_the_value(env, caplog, monkeypatch):
    original = R.ProcedureRunner._resolve

    async def leaky(self, run, bot, step, index):
        out = await original(self, run, bot, step, index)
        R.log.info('resolved %s', out[0].get('value'))
        return out

    monkeypatch.setattr(R.ProcedureRunner, '_resolve', leaky)
    with pytest.raises(AssertionError):
        await T.test_a_secret_reaches_only_the_action_call_and_is_nowhere_else(env, caplog)


async def test_a_secret_stays_out_of_the_new_payload_fields_and_the_log(env, caplog):
    caplog.set_level(logging.DEBUG)
    bank_env(env)
    run = env.run([password_step()])
    await env.drive()
    env.store.approve()
    await env.drive()
    assert env.status(run) == 'done'
    reads = [json.dumps(call['payload']) for call in env.launcher.procedure_steps if call['dry_run']]
    assert all(SECRET not in text for text in reads) and SECRET not in env.everything(run) and SECRET not in caplog.text
    assert json.dumps(actions(env)[0]['payload']).count(SECRET) == 1  # только значение шага в вызове действия
    page = expected_page(BANK, {'role': 'textbox', 'name': 'Пароль'})
    assert SECRET not in json.dumps(page) and action_payload({'action': 'fill', 'value': SECRET}, expected=page)['expected'] == page


# --- PgStore на записывающей заглушке соединения: форма запросов и порядок проверок (живой SQL: test_procedure_runner_db.py) ------

class RecordingCon:
    """Соединение, которое отвечает по подстроке запроса и помнит все запросы. Без Postgres это единственный способ увидеть,
    что PgStore спрашивает базу о занятости бота и пишет unknown_outcome той же записью."""

    def __init__(self, *, active=False, thread_ok=True, run=None, paused=False):
        self.queries, self.active, self.thread_ok, self.run, self.paused = [], active, thread_ok, run, paused
        self.proc = {'id': T.uuid.uuid4(), 'owner_id': OWNER, 'name': 'P', 'status': 'active', 'params': [], 'version': 1,
                     'steps': [click('s1')], 'bot_id': 'alpha'}

    def transaction(self):
        import contextlib

        @contextlib.asynccontextmanager
        async def tx():
            yield self
        return tx()

    async def fetchrow(self, query, *args):
        self.queries.append((query, args))
        if 'from bothub.procedures where' in query:
            return self.proc
        if 'from bothub.bots where' in query:
            return {'id': 'alpha', 'executor': 'container', 'paused': self.paused}
        if 'from bothub.procedure_runs r join' in query:
            return self.run
        if query.startswith('insert into bothub.procedure_runs') or query.startswith('update bothub.procedure_runs'):
            return {'id': T.uuid.uuid4(), 'bot_id': 'alpha', 'thread_id': T.uuid.uuid4(), 'status': 'queued', 'in_flight': False,
                    'step_log': [], 'approval_id': None}
        if 'from bothub.turns' in query:
            return {'busy': 1} if self.active else None
        return None

    async def fetchval(self, query, *args):
        self.queries.append((query, args))
        if 'from bothub.procedure_runs where bot_id=$1' in query:
            return 1 if self.active else None
        if 'from bothub.threads where id=$1' in query:
            return 1 if self.thread_ok else None
        if query.startswith('insert into bothub.threads'):
            return T.uuid.uuid4()
        return None

    async def execute(self, query, *args):
        self.queries.append((query, args))

    def seen(self, text):
        return [q for q, _ in self.queries if text in q]


def pg_store(con):
    import contextlib
    from types import SimpleNamespace

    @contextlib.asynccontextmanager
    async def acquire():
        yield con

    async def nothing(*args, **kwargs):
        return None

    pool = SimpleNamespace(acquire=acquire)
    return R.PgStore(lambda: pool, SimpleNamespace(append_event=nothing, outbox=nothing))


async def test_pg_create_run_locks_the_bot_and_is_409_when_it_has_an_unfinished_run_but_after_the_400_checks():
    busy = RecordingCon(active=True)
    with pytest.raises(RunError) as exc:
        await pg_store(busy).create_run(OWNER, busy.proc['id'], None, None, {})
    assert (exc.value.status, exc.value.code) == (409, 'conflict')
    assert busy.seen('from bothub.bots where id=$1 and owner_id=$2 for update')  # блокировка строки бота
    active = busy.seen("status in ('queued','running','waiting_approval','waiting_model','waiting_human')")
    assert len(active) == 1 and not busy.seen('insert into bothub.threads') and not busy.seen('insert into bothub.procedure_runs')
    wrong_thread = RecordingCon(active=True, thread_ok=False)  # плохой тред это 400 раньше занятости
    with pytest.raises(RunError) as exc:
        await pg_store(wrong_thread).create_run(OWNER, wrong_thread.proc['id'], None, T.uuid.uuid4(), {})
    assert exc.value.status == 400
    free = RecordingCon(active=False)
    run = await pg_store(free).create_run(OWNER, free.proc['id'], None, None, {})
    assert run['status'] == 'queued' and free.seen('insert into bothub.threads') and free.seen('insert into bothub.procedure_runs')


async def test_pg_create_run_refuses_a_paused_bot_under_the_row_lock_before_any_insert():
    # раздел 16: проверка паузы внутри транзакции создания запуска, после блокировки строки бота
    con = RecordingCon(paused=True)
    with pytest.raises(RunError) as exc:
        await pg_store(con).create_run(OWNER, con.proc['id'], None, None, {})
    assert (exc.value.status, exc.value.code, exc.value.detail) == (409, 'bot_paused', 'bot_paused')
    locked = con.seen('select id, executor, paused from bothub.bots where id=$1 and owner_id=$2 for update')
    assert locked and not con.seen('insert into bothub.threads') and not con.seen('insert into bothub.procedure_runs')
    assert not con.seen("status in ('queued','running','waiting_approval','waiting_model','waiting_human')")  # занятость не смотрели


def test_pg_store_pause_sql_is_conditional_on_the_bot_row():
    # begin не переводит запуск в running при паузе бота; hold меняет причину только у queued и running
    import inspect
    begin = inspect.getsource(R.PgStore.begin)
    assert 'b.paused' in begin and "r.status='queued'" in begin
    hold = inspect.getsource(R.PgStore.hold)
    assert "status in ('queued','running')" in hold and "'bot_paused'" in hold
    active = inspect.getsource(R.PgStore.active_runs)
    assert 'bot_paused' in active and 'bothub.bots' in active
    thaw = inspect.getsource(R.PgStore.thaw_waits)
    assert 'greatest(waiting_since' in thaw and "'waiting_approval','waiting_human'" in thaw


async def test_pg_store_asks_the_database_about_a_turn_of_the_bot():
    con = RecordingCon(active=True)
    assert await pg_store(con).bot_has_turn('alpha') is True
    query, args = [(q, a) for q, a in con.queries if 'from bothub.turns' in q][0]
    assert "t.status in ('running','waiting_approval','waiting_mac')" in query and 'th.bot_id=$1' in query and args == ('alpha',)
    assert await pg_store(RecordingCon(active=False)).bot_has_turn('alpha') is False


async def test_pg_stop_writes_unknown_outcome_into_the_same_update_only_when_a_step_was_in_flight():
    steps = [click('s1'), click('s2')]
    flying = {'id': T.uuid.uuid4(), 'status': 'running', 'in_flight': True, 'next_step': 1, 'steps': steps, 'approval_id': None}
    con = RecordingCon(run=flying)
    await pg_store(con).stop_run(OWNER, flying['id'])
    update = [(q, a) for q, a in con.queries if q.startswith('update bothub.procedure_runs')]
    assert len(update) == 1 and "status='stopped'" in update[0][0] and 'step_log=case when $2::jsonb is null' in update[0][0]
    entry = json.loads(update[0][1][1])
    assert [(e['step_id'], e['status'], e['error']) for e in entry] == [('s2', 'failed', 'unknown_outcome')]
    idle = RecordingCon(run={**flying, 'in_flight': False})
    await pg_store(idle).stop_run(OWNER, flying['id'])
    assert [a for q, a in idle.queries if q.startswith('update bothub.procedure_runs')][0][1] is None


async def test_pg_recover_keeps_a_secret_fill_for_the_owner_and_advance_clears_the_flag_in_one_update():
    con = RecordingCon()
    await pg_store(con).recover()
    safe = [q for q in con.seen('set in_flight=false where status') if 'safe_to_retry' in q][0]
    assert "(steps -> next_step ->> 'secret_ref') is null" in safe
    advance = RecordingCon()
    await pg_store(advance).advance(T.uuid.uuid4(), 0, {'step_id': 's1', 'status': 'ok'}, last=False)
    update = advance.seen('update bothub.procedure_runs')[0]
    assert 'step_log=step_log||$3::jsonb' in update and 'next_step=$2+1' in update and 'in_flight=false' in update  # одной записью

"""Исполнитель процедур (bothub/procedure_runner.py) без Postgres и Docker: хранилище в памяти, лаунчер-фейк, страница-модель.
Проходит ветки контракта (раздел 14): порядок статусов, подтверждения по живой подписи, expect и повторы, неизвестный исход,
`waiting_human` с причиной, возврат браузера, секреты нигде кроме stdin исполнителя. SQL проверяет test_procedure_runner_db.py."""
import asyncio
import json
import logging
import re
import uuid
from datetime import timedelta

import pytest

from bothub import procedures
from bothub.launcher_client import (FakeBot, FakeLauncherClient, LauncherConflict, LauncherForbidden, LauncherInvalid,
                                    LauncherNotFound, LauncherServerError, LauncherTimeout, LauncherUnavailable)
from bothub.procedure_runner import (ACTION_TIMEOUT, CRASH_MAX, EXPECT_TIMEOUT_MS, LAUNCHER_FAILURES_MAX, PROBE_TIMEOUT, REASONS,
                                     RETRY_MAX, SETTLE_MS, ProcedureRunner, RunError, action_payload, approval_args,
                                     approval_title, check_run_params, clean_result, probe_payload, verify_payload)
from procedure_fakes import NOW, OWNER, Clock, MemStore, World

pytestmark = pytest.mark.pure
SECRET = 'hunter2-SECRET-VALUE'
LOGIN_URL = 'https://example.com/login'
CTX = {'url_matches': r'^https://example\.com/'}  # куда привели процедуру: ввод секрета без этого не сохраняется


@pytest.fixture(autouse=True)
def fast_match(monkeypatch):
    """Сопоставление адреса без пула процессов (в песочнице он не стартует); сам match_url проверяют test_procedures_*."""
    async def match(pattern, url):
        return re.search(pattern, url) is not None, False
    monkeypatch.setattr(procedures, 'match_url', match)


def nav(id, url=LOGIN_URL, **extra):
    return {'id': id, 'action': 'navigate', 'target': {'url': url}, 'safe_to_retry': True} | extra


def click(id, name='Продолжить', **extra):
    return {'id': id, 'action': 'click', 'target': {'role': 'button', 'name': name}, 'safe_to_retry': False} | extra


def fill(id, name='Email', **extra):
    return {'id': id, 'action': 'fill', 'target': {'role': 'textbox', 'name': name}, 'value': 'a@b.c', 'safe_to_retry': True} | extra


class Env:
    def __init__(self, **bot):
        self.store = MemStore()
        self.store.add_bot(**bot)
        self.world = World()
        self.launcher = FakeLauncherClient()
        self.launcher.bots['alpha'] = FakeBot('alpha', 'o1')
        self.launcher.procedure_handler = self.world.handler
        self.clock = Clock()
        self.released = []
        self.runner = ProcedureRunner(self.store, lambda: self.launcher, release_browser=self.release, clock=self.clock,
                                      sleep=self.clock.sleep, now=lambda: self.store.now)

    async def release(self, bot_id, owner_id):
        self.released.append((bot_id, owner_id))
        self.store.bots[bot_id]['browser_control'] = 'bot'

    def run(self, steps, **fields):
        return self.store.add_run(steps, **fields)

    async def drive(self, rounds=40, settle=True):
        """Циклы runner.tick до покоя: задачи шагов дожидаются, пока что-то движется. settle=False: ровно `rounds` проходов
        (запуск, который сам повторяет шаг, покоя не достигает)."""
        for _ in range(rounds):
            busy = await self.runner.tick()
            tasks = list(self.runner._tasks.values())
            if tasks:
                await asyncio.gather(*tasks)
            if not busy and not tasks:
                return
        if settle:
            raise AssertionError('runner did not settle')

    def status(self, run):
        return self.store.runs[run['id']]['status']

    def get(self, run):
        return self.store.runs[run['id']]

    def actions(self):
        return [call for call in self.launcher.procedure_steps if not call['dry_run']]

    def everything(self, run):
        """Всё, что сохраняется или уходит наружу от запуска: строка запуска, подтверждения, события, push, записи в тред."""
        return json.dumps([self.get(run), list(self.store.approvals.values()), self.store.events, self.store.pushes,
                           self.store.notes], default=str, ensure_ascii=False)


@pytest.fixture
def env():
    return Env()


# --- чистые части -----------------------------------------------------------------------------------------------------------

def test_check_run_params_applies_defaults_types_and_secret_refs():
    declared = [{'name': 'email', 'type': 'string', 'required': True, 'default': None, 'secret': False},
                {'name': 'count', 'type': 'number', 'required': False, 'default': 3, 'secret': False},
                {'name': 'flag', 'type': 'boolean', 'required': False, 'default': None, 'secret': False},
                {'name': 'pw', 'type': 'string', 'required': False, 'default': None, 'secret': True}]
    values, secret, refs = check_run_params(declared, {'email': 'a@b.c', 'pw': 'vault:bank'})
    assert values == {'email': 'a@b.c', 'count': 3, 'pw': 'vault:bank'} and secret == ['pw'] and refs == {'pw': 'bank'}
    bad = [({}, 'params.email: required: a value is required'), ({'email': 'x', 'zzz': 1}, 'params: unknown_field: unknown parameter'),
           ({'email': 5}, 'params.email: type: a string is expected'), ({'email': 'x', 'count': True}, 'params.count: type: a number is expected'),
           ({'email': 'x', 'flag': 'yes'}, 'params.flag: type: a boolean is expected'),
           ({'email': 'x' * 2001}, 'params.email: too_long: value is too long'),
           ({'email': 'x', 'pw': SECRET}, 'params.pw: invalid: a secret parameter takes only a vault reference'),
           ({'email': 'x', 'pw': 'vault:'}, 'params.pw: invalid: a secret parameter takes only a vault reference')]
    for given, detail in bad:
        with pytest.raises(RunError) as exc:
            check_run_params(declared, given)
        assert (exc.value.status, exc.value.code, exc.value.detail) == (400, 'invalid', detail)
        assert SECRET not in exc.value.detail
    with pytest.raises(RunError):
        check_run_params(declared, 'x')
    assert check_run_params([], None) == ({}, [], {})


def test_clean_result_keeps_only_known_fields_of_known_shape():
    good = {'v': 1, 'ok': True, 'code': None, 'acted': True, 'url': 'https://x.y/', 'precondition_visible': None,
            'found': {'count': 1, 'role': 'button', 'name': 'Go', 'extra': 'dropped'}, 'expect': None, 'page_text': 'dropped'}
    cleaned = clean_result(good)
    assert cleaned['found'] == {'count': 1, 'role': 'button', 'name': 'Go'} and 'page_text' not in cleaned
    for bad in (None, [], {'ok': 1}, {'ok': True, 'acted': 'yes'}, {'ok': True, 'acted': None, 'code': 'Bad Code!'},
                {'ok': True, 'acted': None, 'url': 5}, {'ok': True, 'acted': None, 'url': 'x' * 3000},
                {'ok': True, 'acted': None, 'found': {'count': True}}, {'ok': True, 'acted': None, 'found': {'count': -1}},
                {'ok': True, 'acted': None, 'found': {'count': 1, 'name': 'x' * 201}},
                {'ok': True, 'acted': None, 'expect': {'visible': 'yes'}}, {'ok': True, 'acted': None, 'precondition_visible': 1}):
        assert clean_result(bad) is None, bad


def test_payloads_keep_url_matches_and_the_value_out_of_reads():
    step = {'action': 'fill', 'target': {'role': 'textbox', 'name': 'Email'}, 'value': SECRET,
            'precondition': {'url_matches': '^https://', 'visible': {'role': 'heading', 'name': 'Вход'}},
            'expect': {'url_matches': 'done', 'text': 'Готово', 'timeout_ms': 9000}}
    probe = probe_payload(step, settle_ms=SETTLE_MS)
    assert SECRET not in json.dumps(probe) and probe['step']['value'] is None
    assert probe['step']['precondition'] == {'visible': {'role': 'heading', 'name': 'Вход'}}
    assert probe['step']['expect'] == {'text': 'Готово'} and 'check_expect' not in probe
    assert probe_payload(step, check_expect=True)['check_expect'] is True
    act = action_payload(step)
    assert act['step']['value'] == SECRET and act['step']['expect'] is None
    assert act['step']['precondition'] == {'visible': {'role': 'heading', 'name': 'Вход'}}
    verify = verify_payload(step['expect'])
    assert verify['check_expect'] is True and verify['step']['action'] == 'wait' and verify['step']['target'] is None
    assert verify['step']['expect'] == {'text': 'Готово'}
    assert probe_payload({'action': 'click', 'target': None, 'precondition': {'url_matches': 'x'}})['step']['precondition'] is None


def test_approval_args_never_carry_typed_values_or_url_secrets():
    run = {'id': uuid.uuid4(), 'steps': [1, 2, 3], 'procedure_name': 'Вход'}
    fill_step = {'id': 's1', 'action': 'fill', 'target': {'role': 'textbox', 'name': 'Пароль'}, 'value': SECRET}
    args = approval_args(run, 0, fill_step, {'role': 'textbox', 'name': 'Пароль'}, 'login', [], {SECRET})
    assert args['value'] == '[redacted]' and SECRET not in json.dumps(args)
    assert args['live'] == {'role': 'textbox', 'name': 'Пароль'} and 'flags' not in args
    press = {'id': 's2', 'action': 'press', 'target': None, 'value': 'x'}
    assert approval_args(run, 1, press, None, 'other', [], {'x'})['value'] == '[redacted]'
    assert approval_args(run, 1, {**press, 'value': 'Enter'}, None, 'other', [], {'x'})['value'] == 'Enter'
    go = {'id': 's3', 'action': 'navigate', 'target': {'url': 'https://u:p@pay.example.com/checkout?token=T0KEN#frag'}}
    args = approval_args(run, 2, go, None, 'pay', ['login'], set())
    assert args['target'] == {'url': 'https://pay.example.com/checkout'} and args['flags'] == ['login']
    assert 'T0KEN' not in json.dumps(args) and 'p@' not in json.dumps(args)
    assert approval_title(run, 2, go, None) == 'Процедура «Вход», шаг 3 из 3: открыть страницу «https://pay.example.com/checkout»'
    live = {'role': 'button', 'name': 'Оплатить'}
    assert approval_title(run, 0, {'id': 's', 'action': 'click', 'target': {'role': 'button', 'name': 'Далее'}}, live) \
        == 'Процедура «Вход», шаг 1 из 3: нажать «Оплатить»'


# --- обычный ход --------------------------------------------------------------------------------------------------------

async def test_a_run_walks_its_steps_and_calls_the_launcher_in_order(env):
    steps = [nav('s1', expect={'url_matches': r'^https://example\.com/login'}),
             click('s2', precondition={'url_matches': r'/login$'}, expect={'visible': {'role': 'heading', 'name': 'Кабинет'}})]
    env.world.add('button', 'Продолжить')
    env.world.effects[('click', 'button', 'Продолжить')] = lambda w: (w.add('heading', 'Кабинет'), setattr(w, 'url', 'https://example.com/me'))
    run = env.run(steps)
    await env.drive()
    row = env.get(run)
    assert (row['status'], row['next_step'], row['error'], row['reason']) == ('done', 2, None, None)
    assert [e['status'] for e in row['step_log']] == ['ok', 'ok'] and [e['step_id'] for e in row['step_log']] == ['s1', 's2']
    assert all(set(e) == {'step_id', 'status', 'at', 'duration_ms'} for e in row['step_log'])
    assert row['started_at'] is not None and row['finished_at'] is not None
    assert [(c[0], c[1]) for c in env.world.calls] == [(True, 'navigate'), (False, 'navigate'), (True, 'wait'),
                                                      (True, 'click'), (False, 'click'), (True, 'wait')]
    first = env.launcher.procedure_steps[0]
    assert first['dry_run'] is True and first['timeout'] == PROBE_TIMEOUT and first['exec_id'] == f'proc-{run["id"].hex}'
    assert first['payload']['settle_ms'] == SETTLE_MS
    assert env.launcher.procedure_steps[1]['timeout'] == ACTION_TIMEOUT
    assert any('выполнена' in note for note in env.store.notes)
    assert env.store.approvals == {}  # риск none: подтверждения нет


async def test_statuses_follow_the_contract_queued_running_done_and_only_one_waiting_human_flavour(env):
    env.world.add('button', 'Продолжить')
    run = env.run([click('s1')])
    assert env.status(run) == 'queued'
    seen = []
    original = env.store.begin

    async def watching(run_id):
        seen.append(env.status(run))
        ok = await original(run_id)
        seen.append(env.status(run))
        return ok

    env.store.begin = watching
    await env.drive()
    assert seen == ['queued', 'running'] and env.status(run) == 'done'
    assert 'waiting_model' not in REASONS and 'waiting_model' not in {c for c in dir(env.store) if c.startswith('waiting')}


async def test_a_procedure_without_steps_is_done_at_once(env):
    run = env.run([])
    await env.drive()
    assert env.status(run) == 'done' and env.get(run)['finished_at'] is not None


# --- условия шага: waiting_human с причиной ----------------------------------------------------------------------------------

@pytest.mark.parametrize('setup, reason', [
    (lambda w: None, 'element_not_found'),
    (lambda w: w.add('button', 'Продолжить', count=2), 'element_ambiguous'),
])
async def test_missing_or_ambiguous_target_waits_for_the_owner_with_a_reason(env, setup, reason):
    setup(env.world)
    run = env.run([click('s1')])
    await env.drive()
    row = env.get(run)
    assert (row['status'], row['reason'], row['next_step'], row['error']) == ('waiting_human', reason, 0, None)
    assert env.world.acted == [] and not env.actions()
    assert row['step_log'] == [] and reason in env.store.notes[-1]


async def test_url_precondition_that_does_not_match_waits_and_nothing_is_clicked(env):
    env.world.add('button', 'Продолжить')
    run = env.run([click('s1', precondition={'url_matches': r'^https://other\.example/'})])
    await env.drive()
    assert (env.status(run), env.get(run)['reason']) == ('waiting_human', 'precondition_failed')
    assert not env.actions()


async def test_visible_precondition_is_read_by_the_executor_and_pauses_the_run(env):
    env.world.add('button', 'Продолжить')
    run = env.run([click('s1', precondition={'visible': {'role': 'heading', 'name': 'Вход'}})])
    await env.drive()
    assert (env.status(run), env.get(run)['reason']) == ('waiting_human', 'precondition_failed') and not env.actions()
    env.world.add('heading', 'Вход')
    await env.runner.decide_run(OWNER, run['id'], 'retry')
    await env.drive()
    assert env.status(run) == 'done'


async def test_a_precondition_regex_that_times_out_counts_as_not_matching(env, monkeypatch):
    async def slow(pattern, url):
        return False, True
    monkeypatch.setattr(procedures, 'match_url', slow)
    env.world.add('button', 'Продолжить')
    run = env.run([click('s1', precondition={'url_matches': 'x'})])
    await env.drive()
    assert (env.status(run), env.get(run)['reason']) == ('waiting_human', 'precondition_failed') and not env.actions()


@pytest.mark.parametrize('code, reason', [('cdp_unavailable', 'browser_unavailable'), ('playwright_missing', 'browser_unavailable'),
                                          ('runtime_missing', 'browser_unavailable'), ('changed', 'page_changed'),
                                          ('no_page', 'no_page')])
async def test_executor_environment_codes_pause_the_run_without_acting(env, code, reason):
    env.launcher.procedure_handler = lambda payload, dry_run: {'v': 1, 'ok': False, 'code': code, 'acted': False, 'url': None,
                                                              'precondition_visible': None, 'found': None, 'expect': None}
    run = env.run([click('s1')])
    await env.drive()
    assert (env.status(run), env.get(run)['reason']) == ('waiting_human', reason)


async def test_owner_decisions_retry_skip_and_stop(env):
    run = env.run([click('s1'), click('s2', 'Далее')])
    await env.drive()
    assert (env.status(run), env.get(run)['reason']) == ('waiting_human', 'element_not_found')
    skipped = await env.runner.decide_run(OWNER, run['id'], 'skip')
    assert (skipped['status'], skipped['next_step']) == ('running', 1) and skipped['step_log'][0]['status'] == 'skipped'
    env.world.add('button', 'Далее')
    await env.drive()
    row = env.get(run)
    assert (row['status'], row['next_step'], [e['status'] for e in row['step_log']]) == ('done', 2, ['skipped', 'ok'])
    # retry: после правки страницы шаг идёт с next_step
    second = env.run([click('s1', 'Назад')])
    await env.drive()
    assert env.status(second) == 'waiting_human'
    env.world.add('button', 'Назад')
    retried = await env.runner.decide_run(OWNER, second['id'], 'retry')
    assert (retried['status'], retried['reason'], retried['attempt']) == ('running', None, 0)
    await env.drive()
    assert env.status(second) == 'done'
    # stop
    third = env.run([click('s1', 'Нет такой')])
    await env.drive()
    stopped = await env.runner.decide_run(OWNER, third['id'], 'stop')
    assert stopped['status'] == 'stopped' and stopped['finished_at'] is not None
    for action in ('retry', 'skip', 'stop'):
        with pytest.raises(RunError) as exc:
            await env.runner.decide_run(OWNER, third['id'], action)
        assert (exc.value.status, exc.value.code) == (409, 'conflict')
    assert 'остановлен' in env.store.notes[-1]


async def test_skipping_the_last_step_finishes_the_run(env):
    run = env.run([click('s1')])
    await env.drive()
    done = await env.runner.decide_run(OWNER, run['id'], 'skip')
    assert (done['status'], done['next_step']) == ('done', 1) and done['finished_at'] is not None


# --- подтверждения ----------------------------------------------------------------------------------------------------------

async def test_a_risky_step_waits_for_approval_then_runs_once_after_consent(env):
    env.world.add('button', 'Оплатить картой')
    run = env.run([click('s1', 'Оплатить картой')])
    assert env.get(run)['steps'][0]['risk'] == 'pay'
    await env.drive()
    row = env.get(run)
    assert (row['status'], row['reason']) == ('waiting_approval', None) and row['approval_id'] in env.store.approvals
    approval = env.store.approvals[row['approval_id']]
    assert (approval['tool'], approval['risk'], approval['status'], approval['thread_id'], approval['bot_id']) \
        == ('procedure_step', 'pay', 'pending', row['thread_id'], 'alpha')
    assert approval['args']['step_id'] == 's1' and approval['args']['run_id'] == str(run['id'])
    assert approval['args']['live'] == {'role': 'button', 'name': 'Оплатить картой'}
    assert approval['title'].startswith('Процедура «Вход», шаг 1 из 1: нажать') and approval['expires_at'] > NOW
    assert env.store.events[0][0] == 'approval_req' and env.store.pushes == [f'approval:{approval["id"]}']
    assert not env.actions()
    calls_before = len(env.launcher.procedure_steps)
    await env.drive()  # ответа нет: ничего не происходит, чтений нет
    assert env.status(run) == 'waiting_approval' and len(env.launcher.procedure_steps) == calls_before
    env.store.approve()
    await env.drive()
    row = env.get(run)
    assert row['status'] == 'done' and len(env.actions()) == 1
    assert env.store.approvals[approval['id']]['used_at'] is not None  # одобрение израсходовано
    assert len(env.store.approvals) == 1
    # перед действием страница читалась заново (после одобрения)
    assert [c['dry_run'] for c in env.launcher.procedure_steps[:3]] == [True, True, False]


async def test_a_rejected_approval_fails_the_run_and_nothing_is_clicked(env):
    env.world.add('button', 'Оплатить картой')
    run = env.run([click('s1', 'Оплатить картой')])
    await env.drive()
    env.store.reject()
    await env.drive()
    row = env.get(run)
    assert (row['status'], row['error'], row['next_step']) == ('failed', 'approval_rejected', 0)
    assert row['step_log'][0]['status'] == 'failed' and row['step_log'][0]['error'] == 'approval_rejected'
    assert not env.actions()


async def test_an_expired_approval_fails_the_run(env):
    env.world.add('button', 'Оплатить картой')
    run = env.run([click('s1', 'Оплатить картой')])
    await env.drive()
    env.store.now += timedelta(minutes=61)
    await env.drive()
    assert (env.status(run), env.get(run)['error']) == ('failed', 'approval_expired') and not env.actions()
    # так же, если срок вышел и его пометил другой код (периодическая expire_approvals в ядре)
    second = env.run([click('s1', 'Оплатить картой')])
    env.store.now -= timedelta(minutes=61)
    await env.drive()
    env.store.approvals[env.get(second)['approval_id']]['status'] = 'expired'
    await env.drive()
    assert (env.status(second), env.get(second)['error']) == ('failed', 'approval_expired')


async def test_the_live_label_of_the_element_sets_the_risk_when_the_step_looks_harmless(env):
    env.world.selectors['#go'] = {'role': 'button', 'name': 'Оплатить заказ', 'count': 1}
    run = env.run([{'id': 's1', 'action': 'click', 'target': {'selector': '#go'}, 'safe_to_retry': False}])
    assert env.get(run)['steps'][0]['risk'] in ('none', 'other')
    await env.drive()
    approval = list(env.store.approvals.values())[0]
    assert approval['risk'] == 'pay' and approval['args']['live'] == {'role': 'button', 'name': 'Оплатить заказ'}
    assert env.status(run) == 'waiting_approval' and not env.actions()


async def test_an_approval_for_one_page_state_does_not_cover_a_changed_label(env):
    env.world.selectors['#go'] = {'role': 'button', 'name': 'Отправить заявку', 'count': 1}
    run = env.run([{'id': 's1', 'action': 'click', 'target': {'selector': '#go'}, 'safe_to_retry': False}])
    await env.drive()
    first = list(env.store.approvals.values())[0]
    assert env.status(run) == 'waiting_approval'
    env.store.approve()
    env.world.selectors['#go'] = {'role': 'button', 'name': 'Удалить аккаунт', 'count': 1}  # страница сменилась, пока ждали
    await env.drive()
    assert env.store.approvals[first['id']]['status'] == 'expired' and not env.actions()
    assert len(env.store.approvals) == 2 and env.status(run) == 'waiting_approval'
    second = list(env.store.approvals.values())[1]
    assert second['risk'] == 'delete' and second['args']['live']['name'] == 'Удалить аккаунт'
    env.store.approve()
    await env.drive()
    assert env.status(run) == 'done' and len(env.actions()) == 1


async def test_every_attempt_of_a_risky_step_needs_its_own_approval(env):
    env.world.add('button', 'Оплатить картой')
    step = click('s1', 'Оплатить картой', safe_to_retry=True, expect={'visible': {'role': 'heading', 'name': 'Оплачено'}})
    run = env.run([step])
    await env.drive()
    env.store.approve()
    await env.drive()  # expect не выполнился: попытка 2 просит новое подтверждение
    assert env.status(run) == 'waiting_approval' and len(env.store.approvals) == 2 and len(env.actions()) == 1
    env.world.add('heading', 'Оплачено')
    env.store.approve()
    await env.drive()
    assert env.status(run) == 'done' and len(env.actions()) == 2


async def test_login_and_secret_steps_always_go_through_approval(env):
    env.store.secrets[(OWNER, 'alpha', 'bank')] = SECRET
    env.world.add('textbox', 'Пароль')
    run = env.run([{'id': 's1', 'action': 'fill', 'target': {'role': 'textbox', 'name': 'Пароль'}, 'secret_ref': 'vault:bank',
                    'safe_to_retry': False, 'precondition': CTX}])
    await env.drive()
    approval = list(env.store.approvals.values())[0]
    assert approval['risk'] == 'login' and env.status(run) == 'waiting_approval'
    assert approval['args']['value'] == '[redacted]' and SECRET not in json.dumps(approval, default=str)


# --- секреты ---------------------------------------------------------------------------------------------------------------

async def test_a_secret_reaches_only_the_action_call_and_is_nowhere_else(env, caplog):
    caplog.set_level(logging.DEBUG)
    env.store.secrets[(OWNER, 'alpha', 'bank')] = SECRET
    env.world.add('textbox', 'Пароль')
    env.world.add('button', 'Продолжить')
    steps = [{'id': 's1', 'action': 'fill', 'target': {'role': 'textbox', 'name': 'Пароль'}, 'secret_ref': 'vault:bank',
              'safe_to_retry': False, 'precondition': CTX, 'expect': {'visible': {'role': 'button', 'name': 'Продолжить'}}}, click('s2')]
    run = env.run(steps)
    await env.drive()
    env.store.approve()
    await env.drive()
    assert env.status(run) == 'done'
    typed = [c for c in env.launcher.procedure_steps if not c['dry_run']]
    assert [json.dumps(c['payload']).count(SECRET) for c in typed] == [1, 0]
    assert all(SECRET not in json.dumps(c['payload']) for c in env.launcher.procedure_steps if c['dry_run'])
    assert SECRET not in env.everything(run) and SECRET not in caplog.text
    assert all(SECRET not in json.dumps(c, default=str) for c in env.world.calls)


async def test_a_secret_parameter_is_resolved_from_the_vault_at_the_moment_of_the_step(env):
    env.store.secrets[(OWNER, None, 'shared-pw')] = SECRET  # общий секрет
    env.world.add('textbox', 'Пароль')
    run = env.run([{'id': 's1', 'action': 'fill', 'target': {'role': 'textbox', 'name': 'Пароль'}, 'value': '{{pw}}',
                    'safe_to_retry': False, 'precondition': CTX}], params={'pw': 'vault:shared-pw'}, secret_params=['pw'],
                  declared=[{'name': 'pw', 'secret': True}])
    await env.drive()
    env.store.approve()
    await env.drive()
    assert env.status(run) == 'done' and env.world.acted == [('fill', ('textbox', 'Пароль'), SECRET)]
    assert env.get(run)['params'] == {'pw': 'vault:shared-pw'} and SECRET not in env.everything(run)


async def test_the_bot_secret_wins_over_the_shared_one(env):
    env.store.secrets[(OWNER, None, 'pw')] = 'shared-value'
    env.store.secrets[(OWNER, 'alpha', 'pw')] = 'bot-value'
    env.world.add('textbox', 'Пароль')
    run = env.run([{'id': 's1', 'action': 'fill', 'target': {'role': 'textbox', 'name': 'Пароль'}, 'secret_ref': 'vault:pw',
                    'precondition': CTX}])
    await env.drive()
    env.store.approve()
    await env.drive()
    assert env.world.acted[0][2] == 'bot-value'


async def test_a_missing_or_unreadable_secret_fails_the_run_with_a_code(env):
    env.world.add('textbox', 'Пароль')
    step = {'id': 's1', 'action': 'fill', 'target': {'role': 'textbox', 'name': 'Пароль'}, 'secret_ref': 'vault:gone', 'precondition': CTX}
    run = env.run([step])
    await env.drive()
    row = env.get(run)
    assert (row['status'], row['error']) == ('failed', 'secret_not_found') and row['step_log'][0]['error'] == 'secret_not_found'
    assert not env.launcher.procedure_steps  # до исполнителя дело не дошло
    env.store.secrets[(OWNER, 'alpha', 'gone')] = None  # не расшифровался
    other = env.run([step])
    await env.drive()
    assert (env.status(other), env.get(other)['error']) == ('failed', 'secret_not_found')


async def test_a_literal_typed_into_a_field_that_turns_out_to_be_secret_is_refused(env):
    env.world.selectors['#f'] = {'role': 'textbox', 'name': 'Пароль', 'count': 1}
    step = {'id': 's1', 'action': 'fill', 'target': {'selector': '#f'}, 'value': 'plain-text', 'safe_to_retry': False}
    run = env.run([step])
    await env.drive()
    row = env.get(run)
    assert (row['status'], row['error']) == ('failed', 'secret_required') and not env.actions()


async def test_resolve_errors_become_run_errors_with_their_code(env):
    env.world.add('button', 'Продолжить')
    cases = [(click('s1', '{{who}}'), {}, 'param_missing'),
             (nav('s1', 'https://{{host}}/x'), {'host': 'localhost'}, 'url_forbidden'),
             (click('s1', '{{who}}'), {'who': 'x'}, None)]
    for step, params, code in cases:
        _, steps = procedures.normalize_procedure([{'name': 'who'}, {'name': 'host'}], [step], strict_risk=False)
        run = env.store.add_run([], params=params)
        run['steps'] = steps
        await env.drive()
        if code:
            assert (env.status(run), env.get(run)['error']) == ('failed', code), step
            assert env.get(run)['step_log'][0]['error'] == code
    # шаг с value = None (черновик) не отправляется исполнителю
    draft = env.run([{'id': 's1', 'action': 'fill', 'target': {'role': 'textbox', 'name': 'Email'}, 'value': None, 'needs_value': True}])
    env.world.add('textbox', 'Email')
    await env.drive()
    assert (env.status(draft), env.get(draft)['error']) == ('failed', 'step_incomplete')


async def test_a_parameter_that_makes_the_button_a_payment_raises_the_risk_by_the_recomputation(env):
    env.world.add('button', 'Оплатить')
    steps = [click('s1', '{{label}}')]
    run = env.run([], params={'label': 'Оплатить'})
    run['steps'] = procedures.normalize_procedure([{'name': 'label'}], steps, strict_risk=False)[1]
    await env.drive()
    assert env.status(run) == 'waiting_approval' and list(env.store.approvals.values())[0]['risk'] == 'pay'


# --- expect и повторы -------------------------------------------------------------------------------------------------------

async def test_expect_polls_until_it_holds_within_its_timeout(env):
    env.world.add('button', 'Продолжить')
    run = env.run([click('s1', expect={'visible': {'role': 'heading', 'name': 'Кабинет'}, 'timeout_ms': 3000})])
    polls = {'n': 0}
    original = env.world.handler

    def slow(payload, dry_run):
        if dry_run and payload['step']['action'] == 'wait':
            polls['n'] += 1
            if polls['n'] == 3:
                env.world.add('heading', 'Кабинет')
        return original(payload, dry_run)

    env.launcher.procedure_handler = slow
    await env.drive()
    assert env.status(run) == 'done' and polls['n'] == 3 and env.clock.sleeps.count(0.4) == 2


async def test_expect_that_never_holds_fails_a_step_that_is_not_safe_to_retry(env):
    env.world.add('button', 'Продолжить')
    run = env.run([click('s1', expect={'text': 'Готово', 'timeout_ms': 1000})])
    await env.drive()
    row = env.get(run)
    assert (row['status'], row['error'], row['next_step']) == ('failed', 'expect_failed', 0)
    assert row['step_log'][0]['status'] == 'failed' and row['step_log'][0]['error'] == 'expect_failed'
    assert len(env.actions()) == 1 and row['finished_at'] is not None


async def test_expect_failure_retries_a_safe_step_twice_then_fails(env):
    env.world.add('button', 'Продолжить')
    run = env.run([click('s1', safe_to_retry=True, expect={'text': 'Готово', 'timeout_ms': 1000})])
    await env.drive()
    row = env.get(run)
    assert (row['status'], row['error'], row['attempt']) == ('failed', 'expect_failed', RETRY_MAX + 1)
    assert len(env.actions()) == RETRY_MAX + 1  # первая попытка и два повтора


async def test_a_retry_that_succeeds_resets_the_attempt_counter_for_the_next_step(env):
    env.world.add('button', 'Продолжить')
    env.world.effects[('click', 'button', 'Продолжить')] = lambda w: w.texts.add('Готово') if len(w.acted) == 2 else None
    run = env.run([click('s1', safe_to_retry=True, expect={'text': 'Готово', 'timeout_ms': 500}), click('s2')])
    env.world.add('button', 'Продолжить')
    await env.drive()
    row = env.get(run)
    assert (row['status'], row['attempt'], [e['status'] for e in row['step_log']]) == ('done', 0, ['ok', 'ok'])


async def test_url_expectation_uses_match_url_on_the_live_address(env):
    env.world.add('button', 'Продолжить')
    env.world.effects[('click', 'button', 'Продолжить')] = lambda w: setattr(w, 'url', 'https://example.com/me')
    ok = env.run([click('s1', expect={'url_matches': r'^https://example\.com/me$'})])
    await env.drive()
    assert env.status(ok) == 'done'
    env.world.url = LOGIN_URL
    env.world.effects.clear()
    bad = env.run([click('s1', expect={'url_matches': r'/me$', 'timeout_ms': 1000})])
    await env.drive()
    assert (env.status(bad), env.get(bad)['error']) == ('failed', 'expect_failed')


async def test_assert_failure_is_a_step_failure_not_an_unknown_outcome(env):
    env.world.add('status', 'Итог')
    step = {'id': 's1', 'action': 'assert', 'target': {'role': 'status', 'name': 'Итог'}, 'value': 'ок', 'safe_to_retry': False}
    env.world.assert_failures = 1
    run = env.run([step])
    await env.drive()
    assert (env.status(run), env.get(run)['error']) == ('failed', 'assert_failed')
    safe = env.run([{**step, 'safe_to_retry': True}])
    env.world.assert_failures = 1
    await env.drive()
    assert env.status(safe) == 'done' and env.get(safe)['attempt'] == 0


async def test_wait_steps_get_a_launcher_timeout_that_covers_their_delay(env):
    run = env.run([{'id': 's1', 'action': 'wait', 'target': None, 'value': '60000'}])
    await env.drive()
    assert env.status(run) == 'done'
    assert [c['timeout'] for c in env.launcher.procedure_steps if not c['dry_run']] == [75.0]


# --- неизвестный исход -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('trouble', ['unavailable', 'timeout', 'lost', 'action_failed', 'timeout_code'])
async def test_unknown_outcome_of_an_unsafe_step_is_left_to_the_owner(env, trouble):
    env.world.add('button', 'Продолжить')
    if trouble == 'unavailable':
        env.world.raise_on_action = LauncherUnavailable('gone', code='unavailable')
    elif trouble == 'timeout':
        env.world.raise_on_action = LauncherTimeout('slow', code='timeout')
    elif trouble == 'lost':
        env.world.lost_on_action = True
    else:
        env.world.fail_action = 'action_failed' if trouble == 'action_failed' else 'timeout'
        env.world.fail_action_acted = None
    run = env.run([click('s1')])
    await env.drive()
    row = env.get(run)
    assert (row['status'], row['reason'], row['next_step'], row['in_flight']) == ('waiting_human', 'unknown_outcome', 0, False)
    assert len(env.actions()) == 1 and row['step_log'] == [] and row['error'] is None  # само не повторяется


async def test_unknown_outcome_of_a_safe_step_is_retried_up_to_the_limit_then_waits(env):
    env.world.add('button', 'Продолжить')
    env.world.raise_on_action = LauncherUnavailable('gone', code='unavailable')
    run = env.run([click('s1', safe_to_retry=True)])
    await env.drive()
    row = env.get(run)
    assert (row['status'], row['reason']) == ('waiting_human', 'unknown_outcome') and len(env.actions()) == RETRY_MAX + 1
    env.world.raise_on_action = None
    await env.runner.decide_run(OWNER, run['id'], 'retry')
    await env.drive()
    assert env.status(run) == 'done'


async def test_a_failed_action_that_certainly_did_not_happen_is_not_unknown(env):
    env.world.add('button', 'Продолжить')
    env.world.fail_action, env.world.fail_action_acted = 'element_not_found', False
    run = env.run([click('s1')])
    await env.drive()
    assert (env.status(run), env.get(run)['reason']) == ('waiting_human', 'element_not_found')
    env.world.fail_action = 'weird_code'
    other = env.run([click('s1')])
    await env.drive()
    assert (env.status(other), env.get(other)['error']) == ('failed', 'executor_error')


async def test_in_flight_marker_is_set_during_the_action_and_cleared_after(env):
    env.world.add('button', 'Продолжить')
    run = env.run([click('s1')])
    seen = []
    original = env.world.handler

    def watching(payload, dry_run):
        seen.append((dry_run, env.get(run)['in_flight']))
        return original(payload, dry_run)

    env.launcher.procedure_handler = watching
    await env.drive()
    assert seen == [(True, False), (False, True)] and env.get(run)['in_flight'] is False
    checked = env.run([click('s1', expect={'text': 'Готово', 'timeout_ms': 500})])
    env.world.texts.add('Готово')
    seen.clear()
    env.launcher.procedure_handler = lambda payload, dry_run: (seen.append((dry_run, payload['step']['action'], env.get(checked)['in_flight'])),
                                                               original(payload, dry_run))[1]
    await env.drive()
    # Пункт 3 ревью Opus: признак держится до записи next_step и step_log (одна запись), сбой между ними даёт unknown_outcome
    assert (True, 'wait', True) in seen and env.get(checked)['in_flight'] is False


async def test_recovery_after_a_core_restart_follows_the_contract(env):
    safe = env.run([click('s1', safe_to_retry=True)], status='running', in_flight=True)
    unsafe = env.run([click('s1')], status='running', in_flight=True)
    idle = env.run([click('s1')], status='running', in_flight=False)
    waiting = env.run([click('s1')], status='waiting_human', reason='precondition_failed', in_flight=True)
    await env.runner.recover()
    assert (env.status(safe), env.get(safe)['in_flight']) == ('running', False)
    assert (env.status(unsafe), env.get(unsafe)['reason']) == ('waiting_human', 'unknown_outcome')
    assert (env.status(idle), env.get(idle)['reason']) == ('running', None)
    assert env.get(waiting)['reason'] == 'precondition_failed'
    env.world.add('button', 'Продолжить')
    await env.drive()
    assert env.status(safe) == 'done' and env.status(idle) == 'done' and env.status(unsafe) == 'waiting_human'


# --- браузер и лаунчер ---------------------------------------------------------------------------------------------------------

async def test_human_in_control_pauses_the_run_without_touching_the_browser_and_resumes_after_return():
    env = Env(browser_control='human')
    env.world.add('button', 'Продолжить')
    run = env.run([click('s1')])
    await env.drive()
    assert (env.status(run), env.get(run)['reason']) == ('waiting_human', 'browser_human') and not env.launcher.procedure_steps
    await env.drive()  # человек всё ещё у браузера
    assert env.status(run) == 'waiting_human'
    env.store.bots['alpha']['browser_control'] = 'returning'
    await env.drive()
    assert env.status(run) == 'done'
    assert env.released == [('alpha', OWNER)]  # returning -> bot после чтения страницы шага
    assert env.world.calls[0][0] is True  # сначала чтение (precondition заново), потом действие


async def test_a_frozen_bot_is_retried_and_then_left_to_the_owner_as_bot_frozen(env):
    env.world.add('button', 'Продолжить')
    for _ in range(LAUNCHER_FAILURES_MAX):
        env.launcher.fail_next(LauncherConflict('frozen', code='frozen', status=409))
    run = env.run([click('s1')])
    await env.drive()
    assert (env.status(run), env.get(run)['reason']) == ('waiting_human', 'bot_frozen') and not env.actions()
    await env.drive()  # состояние бота bot: само не возобновляется (иначе перезапуски по кругу)
    assert env.status(run) == 'waiting_human'
    await env.runner.decide_run(OWNER, run['id'], 'retry')
    await env.drive()
    assert env.status(run) == 'done'


async def test_a_takeover_that_lands_after_a_frozen_answer_pauses_as_browser_human(env):
    env.world.add('button', 'Продолжить')
    env.launcher.fail_next(LauncherConflict('frozen', code='frozen', status=409))
    run = env.run([click('s1')])
    await env.runner.tick()
    await asyncio.gather(*env.runner._tasks.values())
    env.store.bots['alpha']['browser_control'] = 'human'  # перехват дошёл до базы
    await env.drive()
    assert (env.status(run), env.get(run)['reason']) == ('waiting_human', 'browser_human')
    env.store.bots['alpha']['browser_control'] = 'bot'
    await env.drive()
    assert env.status(run) == 'done'


async def test_takeover_during_the_action_leaves_an_unknown_outcome_not_a_silent_retry(env):
    env.world.add('button', 'Продолжить')
    env.world.lost_on_action = True  # исполнитель убит заморозкой
    run = env.run([click('s1')])
    await env.drive()
    env.store.bots['alpha']['browser_control'] = 'human'
    await env.drive()
    assert (env.status(run), env.get(run)['reason']) == ('waiting_human', 'unknown_outcome')
    env.store.bots['alpha']['browser_control'] = 'bot'
    await env.drive()
    assert env.status(run) == 'waiting_human'  # reason не browser_human: само не возобновляется


async def test_frozen_answer_to_the_action_means_it_did_not_run_and_clears_the_marker(env):
    env.world.add('button', 'Продолжить')
    original = env.world.handler

    def freeze_on_action(payload, dry_run):
        if not dry_run:
            return LauncherConflict('frozen', code='frozen', status=409)
        return original(payload, dry_run)

    env.launcher.procedure_handler = freeze_on_action
    run = env.run([click('s1')])
    await env.drive()
    row = env.get(run)
    assert (row['status'], row['reason'], row['in_flight'], row['step_log']) == ('waiting_human', 'bot_frozen', False, [])


async def test_a_launcher_that_keeps_failing_pauses_the_run_after_a_few_ticks(env):
    env.world.raise_on_probe = LauncherUnavailable('gone', code='unavailable')
    run = env.run([click('s1')])
    for tick in range(LAUNCHER_FAILURES_MAX - 1):
        await env.drive(rounds=1, settle=False)  # один проход цикла: одна неудача
        assert env.status(run) == 'running', tick
    await env.drive()
    assert (env.status(run), env.get(run)['reason']) == ('waiting_human', 'launcher_unavailable')
    for failure in (LauncherServerError('x', code='bad_response'), LauncherTimeout('x', code='timeout')):
        other = env.run([click('s1')])
        env.world.raise_on_probe = failure
        await env.drive()
        assert env.get(other)['reason'] == 'launcher_unavailable'


async def test_a_recovered_launcher_resets_the_failure_count(env):
    env.world.add('button', 'Продолжить')
    env.world.raise_on_probe = LauncherUnavailable('gone', code='unavailable')
    run = env.run([click('s1'), click('s2', 'Далее')])
    for _ in range(LAUNCHER_FAILURES_MAX - 1):
        await env.drive(rounds=1, settle=False)
    env.world.raise_on_probe = None
    env.world.add('button', 'Далее')
    await env.drive()
    assert env.status(run) == 'done'


@pytest.mark.parametrize('error', [LauncherNotFound('x', code='not_found', status=404), LauncherForbidden('x', code='not_managed', status=403)])
async def test_a_bot_without_a_container_fails_the_run(env, error):
    env.world.raise_on_probe = error
    run = env.run([click('s1')])
    await env.drive()
    assert (env.status(run), env.get(run)['error']) == ('failed', 'bot_unavailable')


async def test_a_stopped_container_is_waited_for_and_a_rejected_call_is_our_error(env):
    env.world.raise_on_probe = LauncherConflict('контейнер не запущен', code='conflict', status=409)
    run = env.run([click('s1')])
    await env.drive(rounds=LAUNCHER_FAILURES_MAX, settle=False)
    assert (env.status(run), env.get(run)['reason']) == ('waiting_human', 'launcher_unavailable') and not env.actions()
    env.world.raise_on_probe = LauncherInvalid('payload', code='invalid', status=400)
    other = env.run([click('s1')])
    await env.drive()
    assert (env.status(other), env.get(other)['error']) == ('failed', 'executor_error')
    # отказ лаунчера на самом действии: действия не было, маркер снят
    env.world.raise_on_probe = None
    env.world.add('button', 'Продолжить')
    env.world.raise_on_action = LauncherInvalid('payload', code='invalid', status=400)
    third = env.run([click('s1')])
    await env.drive()
    row = env.get(third)
    assert (row['status'], row['error'], row['in_flight']) == ('failed', 'executor_error', False)


async def test_run_without_a_bot_a_mac_bot_or_a_thread_fails_with_a_code():
    env = Env()
    gone = env.run([click('s1')], bot_id='ghost')
    mac = Env(executor='mac')
    mac_run = mac.run([click('s1')])
    nothread = env.run([click('s1')])
    nothread['thread_id'] = None
    for scenario, run, code in ((env, gone, 'no_bot'), (mac, mac_run, 'bot_unavailable'), (env, nothread, 'no_thread')):
        await scenario.drive()
        assert (scenario.status(run), scenario.get(run)['error']) == ('failed', code)


async def test_without_a_ready_launcher_the_run_just_waits():
    env = Env()
    env.runner._launcher = lambda: None
    run = env.run([click('s1')])
    await env.drive(rounds=3, settle=False)
    assert env.status(run) == 'running'


async def test_malformed_executor_answers_are_treated_as_a_lost_result(env):
    env.world.add('button', 'Продолжить')
    env.launcher.procedure_handler = lambda payload, dry_run: {'ok': 'maybe'}
    run = env.run([click('s1')])
    await env.drive(rounds=2, settle=False)
    assert env.status(run) == 'running'  # чтение без разбираемого ответа: попробует снова


# --- остановка и параллельность ----------------------------------------------------------------------------------------------

async def test_stop_closes_the_run_and_its_approval_and_a_late_result_is_ignored(env):
    env.world.add('button', 'Оплатить картой')
    run = env.run([click('s1', 'Оплатить картой'), click('s2')])
    await env.drive()
    approval_id = env.get(run)['approval_id']
    stopped = await env.runner.stop_run(OWNER, run['id'])
    assert stopped['status'] == 'stopped' and env.store.approvals[approval_id]['status'] == 'expired'
    await env.drive()
    assert env.status(run) == 'stopped' and not env.actions()
    with pytest.raises(RunError) as exc:
        await env.runner.stop_run(OWNER, run['id'])
    assert (exc.value.status, exc.value.code) == (409, 'conflict')
    with pytest.raises(RunError) as exc:
        await env.runner.stop_run(uuid.UUID(int=99), run['id'])
    assert exc.value.status == 404


async def test_stop_of_a_run_with_a_live_step_stops_the_executor_by_its_exec_id(env):
    env.world.add('button', 'Продолжить')
    run = env.run([click('s1')])
    await env.runner.tick()  # задача шага создана и ещё не стартовала
    stopped = await env.runner.stop_run(OWNER, run['id'])
    assert stopped['status'] == 'stopped'
    assert ('procedure_step_cancel', 'alpha', f'proc-{run["id"].hex}') in env.launcher.calls
    assert env.launcher.cancelled == [('alpha', f'proc-{run["id"].hex}')]
    await asyncio.gather(*env.runner._tasks.values())
    assert env.status(run) == 'stopped' and not env.launcher.procedure_steps


async def test_a_result_that_arrives_after_the_owner_stopped_the_run_is_ignored(env):
    env.world.add('button', 'Продолжить')
    run = env.run([click('s1')])
    original = env.world.handler

    def stopped_meanwhile(payload, dry_run):
        outcome = original(payload, dry_run)
        if not dry_run:  # владелец остановил запуск, пока действие шло
            env.get(run).update(status='stopped', finished_at=NOW, in_flight=False)
        return outcome

    env.launcher.procedure_handler = stopped_meanwhile
    await env.drive()
    row = env.get(run)
    assert (row['status'], row['step_log'], row['next_step']) == ('stopped', [], 0) and len(env.actions()) == 1
    assert not any('выполнена' in note for note in env.store.notes)


async def test_one_run_per_bot_at_a_time_and_other_bots_in_parallel(env):
    env.store.add_bot('beta')
    env.launcher.bots['beta'] = FakeBot('beta', 'o1')
    env.world.add('button', 'Продолжить')
    first = env.run([click('s1')])
    second = env.run([click('s1')])
    other = env.run([click('s1')], bot_id='beta')
    await env.runner.tick()
    assert set(env.runner._tasks) == {first['id'], other['id']}  # второй запуск того же бота ждёт
    await asyncio.gather(*env.runner._tasks.values())
    assert env.status(second) == 'queued'
    await env.drive()
    assert {env.status(r) for r in (first, second, other)} == {'done'}


async def test_unexpected_errors_are_counted_logged_without_their_text_and_end_in_failed(env, caplog):
    caplog.set_level(logging.DEBUG)
    run = env.run([click('s1')])
    env.store.crash = RuntimeError(f'boom {SECRET}')
    for _ in range(CRASH_MAX):
        await env.runner.tick()
        await asyncio.gather(*env.runner._tasks.values())
    assert (env.status(run), env.get(run)['error']) == ('failed', 'internal_error')
    crashed = [r for r in caplog.records if r.getMessage() == 'procedure_run_crashed']
    assert len(crashed) == CRASH_MAX and all(r.error.startswith('RuntimeError at procedure_fakes.py') for r in crashed)
    assert SECRET not in caplog.text and SECRET not in json.dumps([vars(r) for r in crashed], default=str)


async def test_shutdown_cancels_running_tasks(env):
    env.world.add('button', 'Продолжить')
    gate = asyncio.Event()

    async def hang(*args, **kwargs):
        await gate.wait()

    env.runner.sleep = hang
    env.world.raise_on_action = LauncherUnavailable('x', code='unavailable')  # безопасный шаг ждёт перед повтором
    env.run([click('s1', safe_to_retry=True)])
    await env.runner.tick()
    await asyncio.sleep(0.01)  # задача дошла до ожидания повтора
    assert len(env.runner._tasks) == 1
    await env.runner.tick()  # живую задачу цикл не дублирует
    assert len(env.runner._tasks) == 1
    await env.runner.shutdown()
    assert not env.runner._tasks


# --- создание запуска (общая часть логики хранилищ) -----------------------------------------------------------------------------

async def test_create_run_validates_bot_thread_params_and_snapshots_the_steps(env):
    proc = env.store.add_procedure([click('s1'), fill('s2')], [{'name': 'email'}], bot_id='alpha')
    run = await env.runner.create_run(OWNER, proc['id'], None, None, {'email': 'a@b.c'})
    assert (run['status'], run['bot_id'], run['procedure_version'], run['params']) == ('queued', 'alpha', 3, {'email': 'a@b.c'})
    assert run['steps'] == proc['steps'] and run['steps'] is not proc['steps'] and run['thread_id'] in env.store.threads
    proc['steps'][0]['target']['name'] = 'Изменено после запуска'
    assert env.store.runs[run['id']]['steps'][0]['target']['name'] == 'Продолжить'  # снимок не меняется
    env.store.runs[run['id']]['status'] = 'done'  # у бота один неконечный запуск за раз (409), см. тест ниже
    mine = env.store._thread('alpha')
    again = await env.runner.create_run(OWNER, proc['id'], 'alpha', mine, {'email': 'a@b.c'})
    assert again['thread_id'] == mine
    env.store.runs[again['id']]['status'] = 'done'
    for args, status, code in (((uuid.uuid4(), None, None, {}), 404, 'not_found'),
                               ((proc['id'], 'ghost', None, {'email': 'x'}), 400, 'invalid'),
                               ((proc['id'], None, uuid.uuid4(), {'email': 'x'}), 400, 'invalid'),
                               ((proc['id'], None, None, {}), 400, 'invalid')):
        with pytest.raises(RunError) as exc:
            await env.runner.create_run(OWNER, *args)
        assert (exc.value.status, exc.value.code) == (status, code)
    env.store.add_bot('macbot', executor='mac')
    with pytest.raises(RunError) as exc:
        await env.runner.create_run(OWNER, proc['id'], 'macbot', None, {'email': 'x'})
    assert exc.value.status == 400
    proc['status'] = 'archived'
    with pytest.raises(RunError) as exc:
        await env.runner.create_run(OWNER, proc['id'], None, None, {'email': 'x'})
    assert (exc.value.status, exc.value.code, exc.value.detail) == (409, 'not_runnable', 'archived')


async def test_create_run_without_any_bot_is_recorded_as_failed_no_bot(env):
    proc = env.store.add_procedure([click('s1')])
    run = await env.runner.create_run(OWNER, proc['id'], None, None, {})
    assert (run['status'], run['error'], run['thread_id'], run['bot_id']) == ('failed', 'no_bot', None, None)
    await env.drive()
    assert env.status(run) == 'failed' and not env.launcher.procedure_steps


async def test_create_run_checks_that_a_secret_parameter_names_an_existing_secret(env):
    proc = env.store.add_procedure([click('s1')], [{'name': 'pw', 'secret': True}], bot_id='alpha')
    with pytest.raises(RunError) as exc:
        await env.runner.create_run(OWNER, proc['id'], None, None, {'pw': 'vault:nope'})
    assert (exc.value.status, exc.value.detail) == (400, 'params.pw: secret_not_found: secret was not found')
    env.store.secrets[(OWNER, None, 'nope')] = SECRET
    run = await env.runner.create_run(OWNER, proc['id'], None, None, {'pw': 'vault:nope'})
    assert run['secret_params'] == ['pw'] and run['params'] == {'pw': 'vault:nope'} and SECRET not in json.dumps(run, default=str)


# --- редкие ветки ------------------------------------------------------------------------------------------------------------

async def test_a_run_waiting_for_the_human_fails_when_its_bot_is_gone(env):
    run = env.run([click('s1')], status='waiting_human', reason='browser_human')
    del env.store.bots['alpha']
    await env.drive()
    assert (env.status(run), env.get(run)['error']) == ('failed', 'no_bot')


async def test_a_waiting_run_whose_approval_vanished_asks_again(env):
    env.world.add('button', 'Оплатить картой')
    run = env.run([click('s1', 'Оплатить картой')], status='waiting_approval', approval_id=uuid.uuid4())
    await env.drive()
    assert env.status(run) == 'waiting_approval' and env.get(run)['approval_id'] in env.store.approvals


async def test_a_vanished_run_is_ignored_when_failing_by_id(env):
    await env.runner._fail_by_id(uuid.uuid4(), 'approval_expired')
    assert env.store.notes == []


@pytest.mark.parametrize('code, error', [('url_forbidden', 'url_forbidden'), ('weird_code', 'executor_error')])
async def test_a_read_that_fails_for_another_reason_fails_the_run(env, code, error):
    env.launcher.procedure_handler = lambda payload, dry_run: {'v': 1, 'ok': False, 'code': code, 'acted': False, 'url': None,
                                                              'precondition_visible': None, 'found': None, 'expect': None}
    run = env.run([click('s1')])
    await env.drive()
    assert (env.status(run), env.get(run)['error']) == ('failed', error)


async def test_a_run_stopped_between_the_read_and_the_action_does_not_act(env):
    env.world.add('button', 'Продолжить')
    run = env.run([click('s1')])
    original = env.world.handler

    def stop_after_read(payload, dry_run):
        outcome = original(payload, dry_run)
        env.get(run).update(status='stopped', finished_at=NOW)
        return outcome

    env.launcher.procedure_handler = stop_after_read
    await env.drive()
    assert env.status(run) == 'stopped' and not env.actions()


async def test_expect_that_cannot_be_checked_leaves_an_unknown_outcome(env):
    env.world.add('button', 'Продолжить')
    env.world.effects[('click', 'button', 'Продолжить')] = lambda w: setattr(w, 'raise_on_probe', LauncherUnavailable('gone', code='unavailable'))
    run = env.run([click('s1', expect={'text': 'Готово', 'timeout_ms': 1000})])
    await env.drive()
    assert (env.status(run), env.get(run)['reason']) == ('waiting_human', 'unknown_outcome') and len(env.actions()) == 1


async def test_a_retry_after_a_stop_does_nothing(env):
    env.world.add('button', 'Продолжить')
    run = env.run([click('s1', safe_to_retry=True, expect={'text': 'Готово', 'timeout_ms': 500})])
    original = env.world.handler

    def stop_during_verify(payload, dry_run):
        if dry_run and payload['step']['action'] == 'wait':
            env.get(run).update(status='stopped', finished_at=NOW)
        return original(payload, dry_run)

    env.launcher.procedure_handler = stop_during_verify
    await env.drive()
    assert env.status(run) == 'stopped' and len(env.actions()) == 1 and env.get(run)['attempt'] == 0


async def test_an_empty_run_that_cannot_be_finished_is_left_alone(env):
    run = env.run([])

    async def refuse(run_id):
        return False

    env.store.finish = refuse
    await env.drive(rounds=2, settle=False)
    assert env.status(run) == 'running' and 'выполнена' not in ' '.join(env.store.notes)


async def _seeded_approval(env, run, status, *, minutes=60):
    approval = await env.store.create_approval(run, 'pay', 'title', {'step_id': 's1', 'risk': 'pay', 'live': {'role': 'button', 'name': 'Оплатить картой'}})
    row = env.store.approvals[approval['id']]
    row.update(status=status, expires_at=env.store.now + timedelta(minutes=minutes))
    env.get(run).update(approval_id=approval['id'])
    return row


@pytest.mark.parametrize('status, minutes, expected', [('rejected', 60, 'approval_rejected'), ('expired', 60, 'approval_expired'),
                                                       ('pending', -1, 'approval_expired')])
async def test_a_running_run_with_a_settled_approval_follows_it(env, status, minutes, expected):
    env.world.add('button', 'Оплатить картой')
    run = env.run([click('s1', 'Оплатить картой')], status='running')
    row = await _seeded_approval(env, run, status, minutes=minutes)
    await env.drive()
    assert (env.status(run), env.get(run)['error']) == ('failed', expected) and not env.actions()
    assert env.store.approvals[row['id']]['status'] in ('rejected', 'expired')


async def test_a_running_run_with_a_pending_approval_goes_back_to_waiting(env):
    env.world.add('button', 'Оплатить картой')
    run = env.run([click('s1', 'Оплатить картой')], status='running')
    row = await _seeded_approval(env, run, 'pending')
    await env.drive()
    assert (env.status(run), env.get(run)['approval_id']) == ('waiting_approval', row['id']) and len(env.store.approvals) == 1


async def test_risk_with_live_takes_the_strictest_of_stored_and_live_labels():
    assert procedures.risk_with_live({'action': 'click', 'risk': 'none'}, 'button', 'Оплатить картой') == ('pay', [])
    assert procedures.risk_with_live({'action': 'click', 'risk': 'other', 'flags': ['login']}, 'button', 'Далее') == ('login', [])
    assert procedures.risk_with_live({'action': 'click', 'risk': 'pay', 'flags': ['login']}, 'button', 'Далее') == ('pay', ['login'])
    assert procedures.risk_with_live({'action': 'fill', 'risk': 'none'}, 'textbox', 'Пароль') == ('login', [])
    assert procedures.risk_with_live({'action': 'click', 'risk': 'none'}, None, None) == ('none', [])
    assert procedures.risk_with_live({'action': 'navigate', 'risk': 'none'}, 'button', 'Оплатить') == ('none', [])
    hidden = 'Опла' + chr(0x202e) + 'тить'  # управляющий символ внутри слова не прячет метку
    assert procedures.risk_with_live({'action': 'click', 'risk': 'none'}, 'button', hidden)[0] == 'pay'


async def test_a_run_that_someone_else_started_first_is_not_started_twice(env):
    run = env.run([click('s1')])

    async def lost_race(run_id):
        return False

    env.store.begin = lost_race
    await env.drive(rounds=2, settle=False)
    assert env.status(run) == 'queued' and not env.launcher.procedure_steps and env.store.notes == []


def test_remember_never_creates_a_rule_for_a_procedure_step_and_it_is_no_browser_tool():
    from bothub.browser_control import is_browser_tool
    from bothub.risk import PROCEDURE_TOOL, remember_rule
    from bothub.procedure_runner import TOOL
    assert TOOL == PROCEDURE_TOOL == 'procedure_step'
    assert remember_rule(TOOL, {'run_id': 'x', 'step_id': 's1', 'action': 'click', 'risk': 'other'}) is None
    assert remember_rule(TOOL, {}) is None
    # takeover и return гасят браузерные approvals по префиксу: шаг процедуры под него не попадает
    assert not is_browser_tool(TOOL) and not TOOL.startswith(('browser_', 'mcp__bothub__browser', 'mcp__playwright__'))


async def test_a_literal_typed_into_a_risky_field_is_redacted_in_the_approval(env):
    env.world.add('textbox', 'Комментарий')
    literal = 'my-literal-text-77'
    run = env.run([{'id': 's1', 'action': 'fill', 'target': {'role': 'textbox', 'name': 'Комментарий'}, 'value': literal,
                    'risk': 'other', 'safe_to_retry': False}])
    await env.drive()
    approval = list(env.store.approvals.values())[0]
    assert approval['risk'] == 'other' and approval['args']['value'] == '[redacted]'
    assert literal not in json.dumps([approval, env.store.events, env.store.notes], default=str, ensure_ascii=False)
    env.store.approve()
    await env.drive()
    assert env.world.acted == [('fill', ('textbox', 'Комментарий'), literal)] and env.status(run) == 'done'


async def test_an_approval_does_not_cover_another_element_with_the_same_risk(env):
    env.world.selectors['#go'] = {'role': 'button', 'name': 'Отправить заявку', 'count': 1}
    run = env.run([{'id': 's1', 'action': 'click', 'target': {'selector': '#go'}, 'safe_to_retry': False}])
    await env.drive()
    first = list(env.store.approvals.values())[0]
    env.store.approve()
    env.world.selectors['#go'] = {'role': 'button', 'name': 'Отправить письмо директору', 'count': 1}  # тот же риск send, другая кнопка
    await env.drive()
    assert first['risk'] == 'send' and env.store.approvals[first['id']]['status'] == 'expired' and not env.actions()
    second = list(env.store.approvals.values())[1]
    assert second['risk'] == 'send' and second['args']['live']['name'] == 'Отправить письмо директору'
    assert env.status(run) == 'waiting_approval'

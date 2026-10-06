"""Общие подмены для тестов воспроизведения процедур без Postgres: хранилище в памяти (то же поведение переходов, что у PgStore:
каждый переход условен по статусу), модель страницы с протоколом исполнителя (bot-image/procedure-step.mjs), часы."""
import copy
import uuid
from datetime import datetime, timedelta, timezone

from bothub import procedures
from bothub.procedure_runner import ACTIVE, RunError, check_run_params
from bothub.risk import op_hash

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
OWNER = uuid.UUID(int=1)


class Clock:
    """Монотонные часы, которые двигает `sleep`: ожидания в тестах мгновенные."""

    def __init__(self):
        self.t = 1000.0
        self.sleeps = []

    def __call__(self):
        return self.t

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


class MemStore:
    """Store из procedure_runner.py в памяти. `runs` можно делить с тестовым пулом маршрутов."""

    def __init__(self, runs=None):
        self.runs = runs if runs is not None else {}
        self.procedures = {}  # id -> строка процедуры
        self.bots = {}  # id -> {'id','owner_id','executor','browser_control'}
        self.threads = {}  # id -> {'bot_id','owner_id'}
        self.secrets = {}  # (owner_id, bot_id | None, name) -> значение или None (не расшифровался)
        self.approvals = {}
        self.notes = []
        self.events = []  # (kind, payload) approval_req
        self.pushes = []
        self.now = NOW
        self.crash = None  # исключение, которое бросит следующий get_run
        self.turn_bots = set()  # боты, у которых идёт turn (running, waiting_approval, waiting_mac)

    # --- подготовка ---
    def add_bot(self, bot_id='alpha', owner_id=OWNER, executor='container', browser_control='bot'):
        self.bots[bot_id] = {'id': bot_id, 'owner_id': owner_id, 'executor': executor, 'browser_control': browser_control,
                             'paused': False, 'paused_at': None}
        return self.bots[bot_id]

    def add_procedure(self, steps, params=(), *, name='Вход', owner_id=OWNER, bot_id=None, status='active', version=3):
        params, steps = procedures.normalize_procedure(list(params), steps, strict_risk=False)
        row = {'id': uuid.uuid4(), 'owner_id': owner_id, 'bot_id': bot_id, 'name': name, 'params': params, 'steps': steps,
               'status': status, 'version': version}
        self.procedures[row['id']] = row
        return row

    def add_run(self, steps, *, status='queued', bot_id='alpha', params=None, secret_params=(), next_step=0, declared=(), **fields):
        _, steps = procedures.normalize_procedure(list(declared), steps, strict_risk=False)
        thread = fields.pop('thread_id', None) or self._thread(bot_id)
        row = {'id': uuid.uuid4(), 'procedure_id': uuid.uuid4(), 'procedure_version': 1, 'bot_id': bot_id, 'thread_id': thread,
               'turn_id': None, 'status': status, 'params': params or {}, 'steps': steps, 'next_step': next_step, 'step_log': [],
               'error': None, 'reason': None, 'approval_id': None, 'attempt': 0, 'in_flight': False,
               'secret_params': list(secret_params), 'started_at': None, 'finished_at': None, 'waiting_since': None,
               'created_at': NOW + timedelta(seconds=len(self.runs)), 'owner_id': OWNER, 'procedure_name': 'Вход'} | fields
        self.runs[row['id']] = row
        return row

    def _thread(self, bot_id):
        thread = uuid.uuid4()
        self.threads[thread] = {'bot_id': bot_id, 'owner_id': OWNER}
        return thread

    # --- Store ---
    async def create_run(self, owner_id, procedure_id, bot_id, thread_id, params):
        proc = self.procedures.get(procedure_id)
        if not proc or proc['owner_id'] != owner_id:
            raise RunError(404, 'not_found')
        if proc['status'] != 'active':
            raise RunError(409, 'not_runnable', proc['status'])
        values, secret_params, refs = check_run_params(proc['params'], params)
        bot_id = bot_id or proc['bot_id']
        base = {'id': uuid.uuid4(), 'procedure_id': procedure_id, 'procedure_version': proc['version'], 'params': values,
                'steps': copy.deepcopy(proc['steps']), 'next_step': 0, 'step_log': [], 'error': None, 'reason': None,
                'approval_id': None, 'attempt': 0, 'in_flight': False, 'secret_params': secret_params, 'started_at': None,
                'waiting_since': None, 'finished_at': None, 'created_at': self.now, 'turn_id': None}
        if not bot_id:
            run = base | {'bot_id': None, 'thread_id': None, 'status': 'failed', 'error': 'no_bot', 'finished_at': self.now}
        else:
            bot = self.bots.get(bot_id)
            if not bot or bot['owner_id'] != owner_id:
                raise RunError(400, 'invalid', 'bot_id: invalid: bot was not found')
            if bot['paused']:  # проверка под блокировкой бота, как в PgStore: пауза между проверкой и вставкой не проскакивает
                raise RunError(409, 'bot_paused', 'bot_paused')
            if bot['executor'] != 'container':
                raise RunError(400, 'invalid', 'bot_id: not_allowed: the bot has no container browser')
            for name, vault in refs.items():
                if (owner_id, bot_id, vault) not in self.secrets and (owner_id, None, vault) not in self.secrets:
                    raise RunError(400, 'invalid', f'params.{name}: secret_not_found: secret was not found')
            if thread_id is not None:
                if self.threads.get(thread_id) != {'bot_id': bot_id, 'owner_id': owner_id}:
                    raise RunError(400, 'invalid', 'thread_id: invalid: thread was not found')
            if any(r['bot_id'] == bot_id and r['status'] in ACTIVE for r in self.runs.values()):
                raise RunError(409, 'conflict', 'the bot already has an active run')
            if thread_id is None:
                thread_id = self._thread(bot_id)
            run = base | {'bot_id': bot_id, 'thread_id': thread_id, 'status': 'queued'}
        self.runs[run['id']] = run
        return dict(run)

    def _owned(self, owner_id, run_id):
        run = self.runs.get(run_id)
        proc = self.procedures.get(run['procedure_id']) if run else None
        if not run or (proc['owner_id'] if proc else run.get('owner_id')) != owner_id:
            raise RunError(404, 'not_found')
        return run

    async def stop_run(self, owner_id, run_id):
        run = self._owned(owner_id, run_id)
        if run['status'] not in ACTIVE:
            raise RunError(409, 'conflict', 'run is already finished')
        self._close_approval(run['approval_id'])
        if run['in_flight'] and run['next_step'] < len(run['steps']):
            run['step_log'].append({'step_id': run['steps'][run['next_step']]['id'], 'status': 'failed', 'at': 'x',
                                    'duration_ms': 0, 'error': 'unknown_outcome'})
        run.update(status='stopped', finished_at=self.now, in_flight=False, reason=None)
        return dict(run)

    async def decide_run(self, owner_id, run_id, action):
        run = self._owned(owner_id, run_id)
        if run['status'] != 'waiting_human':
            raise RunError(409, 'conflict', 'run is not waiting for a decision')
        if action == 'stop':
            run.update(status='stopped', finished_at=self.now, in_flight=False, reason=None)
        elif action == 'retry':
            run.update(status='running', reason=None, attempt=0, in_flight=False, approval_id=None)
        else:
            index = run['next_step']
            run['step_log'].append({'step_id': run['steps'][index]['id'], 'status': 'skipped', 'at': 'x', 'duration_ms': 0})
            last = index + 1 >= len(run['steps'])
            run.update(next_step=index + 1, attempt=0, in_flight=False, approval_id=None, reason=None,
                       status='done' if last else 'running', finished_at=self.now if last else run['finished_at'])
        return dict(run)

    async def active_runs(self):
        keep = ('queued', 'running', 'waiting_approval', 'waiting_human')
        return [{k: r[k] for k in ('id', 'status', 'bot_id', 'reason', 'approval_id', 'waiting_since')}
                | {'bot_paused': bool(self.bots.get(r['bot_id'], {}).get('paused'))}
                for r in sorted(self.runs.values(), key=lambda r: (r['created_at'], str(r['id']))) if r['status'] in keep]

    async def get_run(self, run_id):
        if self.crash is not None:
            raise self.crash
        run = self.runs.get(run_id)
        return copy.deepcopy(run) if run else None

    async def get_bot(self, bot_id):
        bot = self.bots.get(bot_id)
        return dict(bot) if bot else None

    async def load_secrets(self, owner_id, bot_id, names):
        out = {}
        for name in names:
            for key in ((owner_id, bot_id, name), (owner_id, None, name)):
                if key in self.secrets:
                    out[name] = self.secrets[key]
                    break
        return out

    def _is(self, run_id, *statuses):
        run = self.runs.get(run_id)
        return run if run and run['status'] in statuses else None

    async def bot_has_turn(self, bot_id):
        return bot_id in self.turn_bots

    async def bot_is_paused(self, bot_id):
        return bool(self.bots.get(bot_id, {}).get('paused'))

    async def hold(self, run_id, held):
        run = self._is(run_id, 'queued', 'running')
        if run and held:
            run['reason'] = 'bot_paused'
        elif run and run['reason'] == 'bot_paused':
            run['reason'] = None
        return bool(run)

    async def thaw_waits(self, con, bot_id, paused_at):
        for run in self.runs.values():
            if run['bot_id'] == bot_id and run['status'] in ('waiting_approval', 'waiting_human') and run['waiting_since'] is not None:
                run['waiting_since'] += self.now - max(run['waiting_since'], paused_at)

    async def begin(self, run_id):
        run = self._is(run_id, 'queued')
        if run and run['bot_id'] and self.bots.get(run['bot_id'], {}).get('paused'):
            return False
        if run:
            run.update(status='running', started_at=run['started_at'] or self.now)
        return bool(run)

    async def set_in_flight(self, run_id, flag):
        run = self._is(run_id, 'running')
        if run:
            run['in_flight'] = flag
        return bool(run)

    async def bump_attempt(self, run_id):
        run = self._is(run_id, 'running')
        if not run:
            return None
        run.update(attempt=run['attempt'] + 1, in_flight=False)
        return run['attempt']

    async def pause(self, run_id, reason, *, status='waiting_human', approval_id=None):
        run = self._is(run_id, 'running')
        if run:
            run.update(status=status, reason=reason, approval_id=approval_id, in_flight=False, waiting_since=self.now)
        return bool(run)

    async def resume(self, run_id, from_status, *, keep_approval=False):
        run = self._is(run_id, from_status)
        if run:
            run.update(status='running', reason=None, approval_id=run['approval_id'] if keep_approval else None)
        return bool(run)

    async def advance(self, run_id, index, entry, *, last):
        run = self._is(run_id, 'running')
        if not run or run['next_step'] != index:
            return False
        run['step_log'].append(entry)
        run.update(next_step=index + 1, attempt=0, in_flight=False, approval_id=None, reason=None)
        if last:
            run.update(status='done', finished_at=self.now)
        return True

    async def finish(self, run_id):
        run = self._is(run_id, 'running')
        if run:
            run.update(status='done', finished_at=self.now, in_flight=False)
        return bool(run)

    async def fail(self, run_id, code, entry=None):
        run = self._is(run_id, 'queued', 'running', 'waiting_approval', 'waiting_human')
        if not run:
            return False
        run.update(status='failed', error=code, finished_at=self.now, in_flight=False, reason=None)
        if entry is not None:
            run['step_log'].append(entry)
        self._close_approval(run['approval_id'])
        return True

    async def recover(self):
        for run in self.runs.values():
            if run['status'] == 'running' and run['in_flight']:
                step = run['steps'][run['next_step']] if run['next_step'] < len(run['steps']) else {}
                if step.get('safe_to_retry') and not step.get('secret_ref'):
                    run['in_flight'] = False
                else:
                    run.update(status='waiting_human', reason='unknown_outcome', in_flight=False, waiting_since=self.now)

    async def expire_wait(self, run_id, cutoff):
        run = self._is(run_id, 'waiting_approval', 'waiting_human')
        if not run or run['waiting_since'] is None or run['waiting_since'] >= cutoff:
            return None
        was = {'was_status': run['status'], 'was_reason': run['reason']}
        self._close_approval(run['approval_id'])
        run.update(status='stopped', error='wait_expired', finished_at=self.now, in_flight=False, reason=None)
        return dict(run) | was

    async def get_approval(self, approval_id):
        found = self.approvals.get(approval_id)
        return copy.deepcopy(found) if found else None

    async def create_approval(self, run, risk, title, args):
        row = {'id': uuid.uuid4(), 'thread_id': run['thread_id'], 'turn_id': run['turn_id'], 'bot_id': run['bot_id'], 'risk': risk,
               'title': title, 'tool': 'procedure_step', 'args': copy.deepcopy(args), 'op_hash': op_hash('procedure_step', args),
               'status': 'pending', 'used_at': None, 'expires_at': self.now + timedelta(minutes=60)}
        self.approvals[row['id']] = row
        self.events.append(('approval_req', {'approval_id': str(row['id']), 'risk': risk, 'title': title}))
        self.pushes.append(f'approval:{row["id"]}')
        return copy.deepcopy(row)

    async def consume_approval(self, approval_id):
        row = self.approvals.get(approval_id)
        if row and row['status'] == 'approved' and row['used_at'] is None and row['expires_at'] > self.now:
            row['used_at'] = self.now
            return True
        return False

    async def expire_approval(self, approval_id):
        self._close_approval(approval_id)

    def _close_approval(self, approval_id):
        row = self.approvals.get(approval_id) if approval_id else None
        if row and row['status'] in ('pending', 'approved') and row['used_at'] is None:
            row['status'] = 'expired'

    async def note(self, run, text):
        self.notes.append(text)

    # --- действия владельца над подтверждением (то, что делает POST /api/approvals/{id}/decide) ---
    def approve(self, approval_id=None):
        row = self.approvals[approval_id] if approval_id else list(self.approvals.values())[-1]
        row['status'] = 'approved'
        return row

    def reject(self, approval_id=None):
        row = self.approvals[approval_id] if approval_id else list(self.approvals.values())[-1]
        row['status'] = 'rejected'
        return row


class World:
    """Страница глазами исполнителя: `handler` отвечает на вызовы `procedure_step` по протоколу procedure-step.mjs (чтение при
    dry_run, действие иначе). Элементы {(роль, имя): видимость}; `effects[(action, role, name)]` меняет страницу после действия."""

    def __init__(self, url='https://example.com/login'):
        self.url = url
        self.elements = {}  # (role, name) -> {'count': n, 'visible': bool}
        self.texts = set()
        self.selectors = {}  # css -> {'role', 'name', 'count'}
        self.effects = {}
        self.calls = []  # (dry_run, action, step-view) без значений
        self.acted = []  # действия: (action, target, value)
        self.fail_action = None  # код результата на действие вместо успеха
        self.fail_action_acted = None
        self.raise_on_action = None
        self.raise_on_probe = None
        self.lost_on_action = False
        self.assert_failures = 0
        self.url_after_fill = None  # адрес страницы сразу после fill (редирект во время ввода)

    def add(self, role, name, *, visible=True, count=1):
        self.elements[(role, name)] = {'count': count, 'visible': visible}
        return self

    def _result(self, **fields):
        base = {'v': 1, 'ok': True, 'code': None, 'acted': False, 'url': self.url, 'precondition_visible': None,
                'found': None, 'expect': None}
        return base | fields

    def _visible(self, target):
        entry = self.elements.get((target['role'], target['name'])) if 'role' in target else None
        return bool(entry and entry['count'] > 0 and entry['visible'])

    def handler(self, payload, dry_run):
        step = payload['step']
        action, target = step['action'], step.get('target')
        self.calls.append((dry_run, action, copy.deepcopy(step) if dry_run else {k: v for k, v in step.items() if k != 'value'}))
        if dry_run and self.raise_on_probe is not None:
            raise self.raise_on_probe
        if not dry_run and self.raise_on_action is not None:
            raise self.raise_on_action
        if not dry_run and self.lost_on_action:
            return (1, 'exit', None)
        expected = payload.get('expected')
        if not dry_run and action != 'navigate' and not expected:
            return self._result(ok=False, code='invalid_input')  # скрипт: действие без сверки страницы только у navigate
        if expected and (expected['url'] != self.url or expected['origin'] != procedures.page_origin(self.url)):
            return self._result(ok=False, code='changed')  # вкладки с этим адресом нет или origin не тот: действия не было
        result = self._result()
        pre = (step.get('precondition') or {}).get('visible')
        if pre:
            result['precondition_visible'] = self._visible(pre)
            if not result['precondition_visible']:
                return self._result(ok=False, code='precondition_failed', precondition_visible=False)
        located = None
        if target and action != 'navigate':
            if 'selector' in target:
                entry = self.selectors.get(target['selector'], {'count': 0})
                count, role, name = entry['count'], entry.get('role'), entry.get('name')
            else:
                entry = self.elements.get((target['role'], target['name']), {'count': 0, 'visible': True})
                count, role, name = entry['count'], target['role'], target['name']
            result['found'] = {'count': count, 'role': role, 'name': name}
            if count == 0:
                return self._result(ok=False, code='element_not_found', found=result['found'])
            if count > 1:
                return self._result(ok=False, code='element_ambiguous', found=result['found'])
            located = (role, name)
            if expected and expected.get('role') is not None and located != (expected['role'], expected['name']):
                return self._result(ok=False, code='changed', found=result['found'])
        if dry_run:
            if payload.get('check_expect') and step.get('expect'):
                expect = step['expect']
                result['expect'] = {'visible': self._visible(expect['visible']) if expect.get('visible') else None,
                                    'text': (expect['text'] in self.texts) if expect.get('text') else None}
            return result
        if self.fail_action:
            return self._result(ok=False, code=self.fail_action, acted=self.fail_action_acted, found=result['found'])
        if action == 'assert' and self.assert_failures:
            self.assert_failures -= 1
            return self._result(ok=False, code='assert_failed', acted=False, found=result['found'])
        self.acted.append((action, located or target, step.get('value')))
        before = self.url
        if action == 'navigate':
            self.url = target['url']
        effect = self.effects.get((action, *(located or (None, None))))
        if effect:
            effect(self)
        if action == 'fill' and self.url_after_fill:
            self.url = self.url_after_fill
        # Как procedure-step.mjs: после click/press/select/fill адрес читается заново. Другой origin при вводе секрета это
        # `changed_after` (значение могло уйти не туда), иначе обычный успех с navigated и origin_changed.
        navigated = action != 'navigate' and self.url != before
        away = navigated and procedures.page_origin(self.url) != (expected['origin'] if expected else procedures.page_origin(before))
        if away and payload.get('secret_input') and action == 'fill':
            return self._result(ok=False, code='changed_after', acted=True, found=result['found'])
        return self._result(acted=True, found=result['found'], navigated=navigated, origin_changed=away)

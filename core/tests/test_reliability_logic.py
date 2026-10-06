"""Этап 1Б: надёжность и логирование (без БД, чистая логика и цикл-обёртки).

Запуск без Postgres: pytest --noconftest tests/test_reliability_logic.py
"""
import asyncio
import hashlib
import io
import json
import logging
import sys

import httpx
import pytest

from bothub import main as hub
from bothub.main import JsonFormatter, configure_logging, create_app, push_status, supervise
from bothub.risk import op_hash, remember_rule, rule_matches


# ---- п.13: JSON-логирование ------------------------------------------------

def _record(msg="hello", level=logging.INFO, **extra):
    record = logging.LogRecord("bothub", level, __file__, 1, msg, (), None)
    record.__dict__.update(extra)
    return record


def test_json_formatter_emits_one_json_line_with_extras():
    line = JsonFormatter().format(_record("turn done", turn_id="t1", spent=5))
    assert "\n" not in line
    parsed = json.loads(line)
    assert parsed["msg"] == "turn done" and parsed["level"] == "INFO" and parsed["logger"] == "bothub"
    assert parsed["turn_id"] == "t1" and parsed["spent"] == 5 and "ts" in parsed


def test_json_formatter_includes_traceback():
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord("bothub", logging.ERROR, __file__, 1, "failed", (), sys.exc_info())
    parsed = json.loads(JsonFormatter().format(record))
    assert "ValueError: boom" in parsed["exc"]


def test_json_formatter_survives_unserializable_extra():
    parsed = json.loads(JsonFormatter().format(_record(obj=object())))
    assert isinstance(parsed["obj"], str)


@pytest.fixture
def restore_logger():
    logger = logging.getLogger("bothub")
    handlers, level = list(logger.handlers), logger.level
    yield logger
    logger.handlers[:] = handlers
    logger.setLevel(level)


def test_configure_logging_writes_json_to_stdout_and_reads_level(monkeypatch, restore_logger):
    stream = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setenv("BOTHUB_LOG_LEVEL", "warning")
    configure_logging()
    restore_logger.info("hidden")
    restore_logger.warning("shown", extra={"k": 1})
    lines = [json.loads(x) for x in stream.getvalue().splitlines()]
    assert [x["msg"] for x in lines] == ["shown"] and lines[0]["k"] == 1


def test_configure_logging_default_info_bad_level_falls_back_and_is_idempotent(monkeypatch, restore_logger):
    stream = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setenv("BOTHUB_LOG_LEVEL", "nonsense")
    configure_logging()
    configure_logging()
    assert restore_logger.level == logging.INFO
    restore_logger.info("once")
    assert len(stream.getvalue().splitlines()) == 1


# ---- п.13: /api/health проверяет БД ------------------------------------------

class _Con:
    def __init__(self, fail):
        self.fail = fail

    async def fetchval(self, sql, *args):
        if self.fail:
            raise ConnectionError("db down")
        assert sql.strip().lower() == "select 1"
        return 1


class _Pool:
    def __init__(self, fail=False, hang=False):
        self.fail, self.hang = fail, hang

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self):
                if pool.hang:
                    await asyncio.sleep(60)
                return _Con(pool.fail)

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


async def _get_health(pool):
    app = create_app(lambda provider: None)
    if pool is not None:
        app.state.pool = pool
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.get("/api/health")


async def test_health_ok_when_db_answers():
    response = await _get_health(_Pool())
    assert response.status_code == 200 and response.json()["status"] == "ok"


async def test_health_503_when_db_fails_without_leaking_error():
    response = await _get_health(_Pool(fail=True))
    assert response.status_code == 503
    assert response.json()["status"] == "error" and "db down" not in response.text


async def test_health_503_when_pool_not_ready():
    assert (await _get_health(None)).status_code == 503


async def test_health_503_when_db_hangs(monkeypatch):
    monkeypatch.setattr(hub, "HEALTH_TIMEOUT", 0.05)
    assert (await _get_health(_Pool(hang=True))).status_code == 503


async def test_send_push_has_bounded_network_timeout(monkeypatch):
    import pywebpush

    calls = []
    monkeypatch.setenv("VAPID_PRIVATE", "private")
    monkeypatch.setenv("VAPID_SUBJECT", "mailto:test@example.com")
    monkeypatch.setattr(pywebpush, "webpush", lambda *args, **kwargs: calls.append((args, kwargs)))
    app = create_app(lambda provider: None)
    await app.state.send_push({"endpoint": "https://push.example/x", "keys": {}}, "body")
    assert calls[0][0][1] == "body"
    assert 0 < calls[0][1]["timeout"] <= 30


# ---- п.7: циклы не умирают от исключения -------------------------------------

async def test_supervise_logs_error_and_keeps_looping(caplog):
    calls = []

    async def step():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("first pass fails")

    caplog.set_level(logging.ERROR, logger="bothub")
    task = asyncio.create_task(supervise("demo", step, interval=0.01, error_delay=0.01))
    await asyncio.sleep(0.15)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert len(calls) >= 3
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and errors[0].loop == "demo" and errors[0].exc_info


async def test_supervise_cancel_is_not_swallowed():
    async def step():
        await asyncio.sleep(10)

    task = asyncio.create_task(supervise("demo", step, interval=0.01))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_supervise_busy_step_does_not_sleep():
    calls = []

    async def step():
        calls.append(1)
        return True  # занят: следующая итерация сразу

    task = asyncio.create_task(supervise("demo", step, interval=5))
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert len(calls) > 5


@pytest.mark.parametrize("loop_name,hook,interval_env", [
    ("scheduler", "run_due_schedules", "BOTHUB_SCHEDULER_INTERVAL"),
    ("worker", "claim_turn", "BOTHUB_WORKER_INTERVAL"),
    ("outbox_sender", "deliver_outbox", "BOTHUB_OUTBOX_INTERVAL"),
])
async def test_background_loops_survive_exception_in_one_pass(monkeypatch, caplog, loop_name, hook, interval_env):
    monkeypatch.setenv(interval_env, "0.01")
    monkeypatch.setenv("BOTHUB_LOOP_ERROR_DELAY", "0.01")
    app = create_app(lambda provider: None)
    calls = []

    async def flaky():
        calls.append(1)
        if len(calls) <= 2:
            raise RuntimeError(f"{hook} exploded")
        return None

    setattr(app.state, hook, flaky)
    caplog.set_level(logging.ERROR, logger="bothub")
    task = asyncio.create_task(getattr(app.state, loop_name)())
    await asyncio.sleep(0.2)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert len(calls) >= 4, "цикл остановился после исключения"
    assert sum(1 for r in caplog.records if getattr(r, "loop", None) == loop_name) == 2


# ---- п.8: ответ push-сервиса ---------------------------------------------------

class _Resp:
    def __init__(self, code):
        self.status_code = code


class _PushError(Exception):
    def __init__(self, code):
        super().__init__(f"push {code}")
        self.response = _Resp(code) if code else None


@pytest.mark.parametrize("exc,code", [(_PushError(410), 410), (_PushError(404), 404), (_PushError(500), 500), (_PushError(None), None), (RuntimeError("x"), None)])
def test_push_status_reads_response_code(exc, code):
    assert push_status(exc) == code


def test_outbox_backoff_grows_and_is_capped():
    assert hub.outbox_backoff(1) < hub.outbox_backoff(2) < hub.outbox_backoff(3)
    assert hub.outbox_backoff(50) == hub.OUTBOX_BACKOFF_CAP


# ---- п.6: решение привязано к операции (tool + args) ---------------------------

def test_op_hash_is_sha256_of_tool_and_canonical_args():
    expected = hashlib.sha256(('send_message' + '{"a":1,"b":"x"}').encode()).hexdigest()
    assert op_hash("send_message", {"b": "x", "a": 1}) == expected


def test_op_hash_differs_by_tool_and_args():
    assert op_hash("a", {"x": 1}) != op_hash("b", {"x": 1})
    assert op_hash("a", {"x": 1}) != op_hash("a", {"x": 2})
    assert op_hash("a", {"x": 1}) != op_hash("a", {"x": 1, "y": 2})


def test_rule_with_op_hash_does_not_match_extra_args():
    # раньше правило {"match": {"channel": "ops"}} пропускало и {"channel":"ops","to":"all"}
    args = {"channel": "ops"}
    rule = remember_rule("send_message", args) | {"op_hash": op_hash("send_message", args)}
    assert rule_matches(rule, "send_message", args) is True
    assert rule_matches(rule, "send_message", {"channel": "ops", "to": "all"}) is False


def test_rule_with_op_hash_rejects_tampered_hash():
    args = {"channel": "ops"}
    rule = remember_rule("send_message", args) | {"op_hash": op_hash("send_message", {"channel": "other"})}
    assert rule_matches(rule, "send_message", args) is False


def test_legacy_rule_without_op_hash_no_longer_matches():
    # этап 1 (финал): правило с match без op_hash не отличить от подмножества аргументов, миграция 006 их вычищает
    assert rule_matches({"tool": "send_message", "match": {"channel": "ops"}}, "send_message", {"channel": "ops"}) is False
    assert rule_matches({"tool": "send_message"}, "send_message", {"channel": "ops"}) is True

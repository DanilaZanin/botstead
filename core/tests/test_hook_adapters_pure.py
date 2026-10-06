"""Чистые тесты для адаптеров вебхуков GitHub и Slack (этап 10).

Проверка подписей HMAC-SHA256, сборка промптов, обработка событий, ping и url_verification.
Без реальной БД Postgres (pytestmark = pytest.mark.pure).
"""
import base64
import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from bothub import hook_adapters
from bothub.main import create_app
from bothub.secrets import encrypt_secret

pytestmark = pytest.mark.pure

SECRET = "test-hook-token-secret-12345"
SCHEDULE_ID = uuid.uuid4()
BOT_ID = "bot-scout-1234"
OWNER_ID = uuid.uuid4()


def _github_sig(body: bytes, secret: str = SECRET) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _slack_sig(body: bytes, ts: int, secret: str = SECRET) -> str:
    basestring = b"v0:" + str(ts).encode("ascii") + b":" + body
    digest = hmac.new(secret.encode("utf-8"), basestring, hashlib.sha256).hexdigest()
    return f"v0={digest}"


# ---- 1. Проверка подписей GitHub ----

def test_github_signature_valid_prefixed_and_raw():
    body = b'{"action":"opened"}'
    valid_sig = _github_sig(body)
    assert hook_adapters.verify_github_signature(body, SECRET, valid_sig) is True
    # Без префикса sha256=
    assert hook_adapters.verify_github_signature(body, SECRET, valid_sig.removeprefix("sha256=")) is True


def test_github_signature_invalid_and_tampered():
    body = b'{"action":"opened"}'
    valid_sig = _github_sig(body)
    # Подделанное тело
    assert hook_adapters.verify_github_signature(b'{"action":"closed"}', SECRET, valid_sig) is False
    # Поддельная подпись
    assert hook_adapters.verify_github_signature(body, SECRET, "sha256=0000000000000000000000000000000000000000000000000000000000000000") is False
    # Неверный секрет
    assert hook_adapters.verify_github_signature(body, "wrong-secret", valid_sig) is False


def test_github_signature_missing_headers_or_secret():
    body = b'{"action":"opened"}'
    assert hook_adapters.verify_github_signature(body, None, _github_sig(body)) is False
    assert hook_adapters.verify_github_signature(body, "", _github_sig(body)) is False
    assert hook_adapters.verify_github_signature(body, SECRET, None) is False
    assert hook_adapters.verify_github_signature(body, SECRET, "") is False


def test_github_signature_empty_body_and_unicode():
    empty = b""
    assert hook_adapters.verify_github_signature(empty, SECRET, _github_sig(empty)) is True

    unicode_body = '{"title":"Тестовый заголовок"}'.encode("utf-8")
    assert hook_adapters.verify_github_signature(unicode_body, SECRET, _github_sig(unicode_body)) is True


# ---- 2. Проверка подписей Slack ----

def test_slack_signature_valid_within_window():
    body = b'{"type":"event_callback"}'
    now = 1700000000
    sig = _slack_sig(body, now)
    assert hook_adapters.verify_slack_signature(body, SECRET, sig, str(now), now_ts=now) is True
    # Запрос 2 минуты назад (в пределах 300 с)
    assert hook_adapters.verify_slack_signature(body, SECRET, sig, str(now), now_ts=now + 120) is True


def test_slack_signature_expired_or_future_timestamp():
    body = b'{"type":"event_callback"}'
    now = 1700000000
    sig = _slack_sig(body, now)
    # Старше 5 минут (301 секунда)
    assert hook_adapters.verify_slack_signature(body, SECRET, sig, str(now), now_ts=now + 301) is False
    # Из будущего более чем на 5 минут
    assert hook_adapters.verify_slack_signature(body, SECRET, sig, str(now), now_ts=now - 301) is False


def test_slack_signature_invalid_timestamp_or_missing_headers():
    body = b'{"type":"event_callback"}'
    now = 1700000000
    sig = _slack_sig(body, now)
    for bad_ts in ("not-a-number", "", "12.34", None):
        assert hook_adapters.verify_slack_signature(body, SECRET, sig, bad_ts, now_ts=now) is False
    assert hook_adapters.verify_slack_signature(body, None, sig, str(now), now_ts=now) is False
    assert hook_adapters.verify_slack_signature(body, SECRET, None, str(now), now_ts=now) is False
    assert hook_adapters.verify_slack_signature(b'tampered', SECRET, sig, str(now), now_ts=now) is False


# ---- 3. Сборка промптов GitHub ----

def test_github_prompt_issues():
    payload = {
        "action": "opened",
        "repository": {"full_name": "org/repo"},
        "issue": {
            "number": 42,
            "title": "Fix login bug",
            "user": {"login": "alice"},
            "html_url": "https://github.com/org/repo/issues/42",
            "body": "Detailed report about the login issue.",
        },
    }
    prompt = hook_adapters.build_github_prompt("issues", payload)
    assert prompt is not None
    assert "GitHub событие: issues" in prompt
    assert "Репозиторий: org/repo" in prompt
    assert "Действие: opened" in prompt
    assert "Заголовок / номер: #42 Fix login bug" in prompt
    assert "Автор: alice" in prompt
    assert "Ссылка: https://github.com/org/repo/issues/42" in prompt
    assert "Detailed report about the login issue." in prompt
    assert "—" not in prompt


def test_github_prompt_issue_comment():
    payload = {
        "action": "created",
        "repository": {"full_name": "org/repo"},
        "issue": {
            "number": 42,
            "title": "Fix login bug",
            "html_url": "https://github.com/org/repo/issues/42",
        },
        "comment": {
            "user": {"login": "bob"},
            "html_url": "https://github.com/org/repo/issues/42#issuecomment-1",
            "body": "LGTM! Approved.",
        },
    }
    prompt = hook_adapters.build_github_prompt("issue_comment", payload)
    assert prompt is not None
    assert "GitHub событие: issue_comment" in prompt
    assert "Автор: bob" in prompt
    assert "Заголовок / номер: #42 Fix login bug" in prompt
    assert "Ссылка: https://github.com/org/repo/issues/42#issuecomment-1" in prompt
    assert "LGTM! Approved." in prompt
    assert "—" not in prompt


def test_github_prompt_pull_request():
    payload = {
        "action": "opened",
        "repository": {"full_name": "org/repo"},
        "pull_request": {
            "number": 101,
            "title": "Add webhook adapters",
            "user": {"login": "charlie"},
            "html_url": "https://github.com/org/repo/pull/101",
            "body": "Implements plan stage 10.",
        },
    }
    prompt = hook_adapters.build_github_prompt("pull_request", payload)
    assert prompt is not None
    assert "GitHub событие: pull_request" in prompt
    assert "Заголовок / номер: #101 Add webhook adapters" in prompt
    assert "Автор: charlie" in prompt
    assert "Ссылка: https://github.com/org/repo/pull/101" in prompt
    assert "Implements plan stage 10." in prompt
    assert "—" not in prompt


def test_github_prompt_pull_request_review():
    payload = {
        "action": "submitted",
        "repository": {"full_name": "org/repo"},
        "pull_request": {
            "number": 101,
            "title": "Add webhook adapters",
            "html_url": "https://github.com/org/repo/pull/101",
        },
        "review": {
            "user": {"login": "dave"},
            "html_url": "https://github.com/org/repo/pull/101#pullrequestreview-5",
            "body": "Looks great to me.",
        },
    }
    prompt = hook_adapters.build_github_prompt("pull_request_review", payload)
    assert prompt is not None
    assert "GitHub событие: pull_request_review" in prompt
    assert "Заголовок / номер: #101 Add webhook adapters" in prompt
    assert "Автор: dave" in prompt
    assert "Looks great to me." in prompt
    assert "—" not in prompt


def test_github_prompt_push():
    payload = {
        "ref": "refs/heads/main",
        "repository": {"full_name": "org/repo"},
        "pusher": {"name": "eve"},
        "compare": "https://github.com/org/repo/compare/abc...def",
        "commits": [
            {"message": "commit 1: add feature"},
            {"message": "commit 2: fix test"},
        ],
    }
    prompt = hook_adapters.build_github_prompt("push", payload)
    assert prompt is not None
    assert "GitHub событие: push" in prompt
    assert "Действие: push refs/heads/main" in prompt
    assert "Автор: eve" in prompt
    assert "Ссылка: https://github.com/org/repo/compare/abc...def" in prompt
    assert "commit 1: add feature\ncommit 2: fix test" in prompt
    assert "—" not in prompt


def test_github_prompt_workflow_run():
    payload = {
        "action": "completed",
        "repository": {"full_name": "org/repo"},
        "workflow_run": {
            "id": 99999,
            "name": "CI Tests",
            "run_number": 7,
            "actor": {"login": "frank"},
            "html_url": "https://github.com/org/repo/actions/runs/99999",
            "status": "completed",
            "conclusion": "success",
            "display_title": "CI Run for main",
        },
    }
    prompt = hook_adapters.build_github_prompt("workflow_run", payload)
    assert prompt is not None
    assert "GitHub событие: workflow_run" in prompt
    assert "Заголовок / номер: #7 CI Tests" in prompt
    assert "Автор: frank" in prompt
    assert "Ссылка: https://github.com/org/repo/actions/runs/99999" in prompt
    assert "CI Run for main" in prompt
    assert "Статус: completed, результат: success" in prompt
    assert "—" not in prompt


def test_github_prompt_truncation_to_2000():
    long_body = "x" * 3000
    payload = {
        "action": "opened",
        "repository": {"full_name": "org/repo"},
        "issue": {"number": 1, "title": "Long", "body": long_body},
    }
    prompt = hook_adapters.build_github_prompt("issues", payload)
    assert prompt is not None
    # Тело обрезано до 2000 символов
    assert "x" * 2000 in prompt
    assert "x" * 2001 not in prompt


def test_github_prompt_ignored_events():
    for ign in ("ping", "star", "release", "fork", "watch", "status", "gollum"):
        assert hook_adapters.build_github_prompt(ign, {}) is None


# ---- 4. Сборка промптов Slack ----

def test_slack_prompt_app_mention():
    payload = {
        "type": "event_callback",
        "event": {
            "type": "app_mention",
            "channel": "C12345",
            "user": "U67890",
            "text": "<@BOT> what is the server status?",
        },
    }
    prompt = hook_adapters.build_slack_prompt(payload)
    assert prompt is not None
    assert "Slack событие: app_mention" in prompt
    assert "Канал: C12345" in prompt
    assert "Пользователь: U67890" in prompt
    assert "<@BOT> what is the server status?" in prompt
    assert "—" not in prompt


def test_slack_prompt_message_without_bot_or_subtype():
    payload = {
        "type": "event_callback",
        "event": {
            "type": "message",
            "channel": "C99999",
            "user": "U11111",
            "text": "Hello bot!",
        },
    }
    prompt = hook_adapters.build_slack_prompt(payload)
    assert prompt is not None
    assert "Slack событие: message" in prompt
    assert "Канал: C99999" in prompt
    assert "Пользователь: U11111" in prompt
    assert "Hello bot!" in prompt
    assert "—" not in prompt


def test_slack_prompt_skips_bot_id_or_subtype():
    # Сообщение от другого бота
    payload_bot = {
        "type": "event_callback",
        "event": {
            "type": "message",
            "bot_id": "B123",
            "channel": "C123",
            "text": "Bot automated msg",
        },
    }
    assert hook_adapters.build_slack_prompt(payload_bot) is None

    # Служебное сообщение с subtype (message_changed, channel_join и т. д.)
    payload_subtype = {
        "type": "event_callback",
        "event": {
            "type": "message",
            "subtype": "message_changed",
            "channel": "C123",
            "text": "Edited text",
        },
    }
    assert hook_adapters.build_slack_prompt(payload_subtype) is None


def test_slack_prompt_skips_other_events():
    for other in ("reaction_added", "channel_created", "member_joined_channel"):
        payload = {
            "type": "event_callback",
            "event": {"type": other, "channel": "C123"},
        }
        assert hook_adapters.build_slack_prompt(payload) is None


def test_slack_prompt_truncation_to_2000():
    payload = {
        "type": "event_callback",
        "event": {
            "type": "app_mention",
            "channel": "C123",
            "user": "U123",
            "text": "z" * 3000,
        },
    }
    prompt = hook_adapters.build_slack_prompt(payload)
    assert prompt is not None
    assert "z" * 2000 in prompt
    assert "z" * 2001 not in prompt


# ---- 5. HTTP маршруты через TestClient с мок-пулом БД ----

class MockPool:
    """Мок соединения с БД для чистых тестов эндпоинтов hook."""

    def __init__(self, schedule=None):
        self.schedule = schedule or {
            "id": SCHEDULE_ID,
            "name": "Hook Schedule",
            "kind": "hook",
            "enabled": True,
            "owner_id": OWNER_ID,
            "bot_id": BOT_ID,
            "hook_token": SECRET,
            "prompt": "Schedule base prompt",
            "skipped_count": 0,
            "paused_by_unavailable": False,
        }
        self.dispatched_turns = []

    def acquire(self):
        pool = self

        class Context:
            async def __aenter__(self):
                con = AsyncMock()
                bot = {"id": BOT_ID, "owner_id": OWNER_ID, "status": "idle", "provider_id": None,
                       "registry_bound": False, "executor": "container", "paused": False, "model_id": None}
                state = {"last_seq": 0}

                async def fetchrow(query, *args):
                    if "bothub.settings" in query:
                        return None
                    if "bothub.schedules" in query:
                        if args and args[0] == SCHEDULE_ID:
                            return pool.schedule
                        return None
                    if "bothub.bots" in query:
                        return dict(bot)
                    if "insert into bothub.turns" in query:
                        pool.dispatched_turns.append({"thread_id": args[0], "prompt": args[1], "client": args[2]})
                        return {"id": uuid.uuid4(), "thread_id": args[0], "prompt": args[1], "client": args[2],
                                "status": "queued", "turn_type": "normal"}
                    if "insert into bothub.events" in query:
                        return {"id": 1, "thread_id": str(args[0]), "seq": args[1], "turn_id": str(args[2]),
                                "kind": args[3], "payload": json.loads(args[6])}
                    return None

                async def fetchval(query, *args):
                    if "bothub.settings" in query:
                        return True
                    if "insert into bothub.threads" in query:
                        return uuid.uuid4()
                    if "update bothub.threads set last_seq" in query:
                        state["last_seq"] += 1
                        return state["last_seq"]
                    return None

                async def execute(query, *args):
                    return "OK"

                def transaction():
                    class TxContext:
                        async def __aenter__(self):
                            return con
                        async def __aexit__(self, *a):
                            return False
                    return TxContext()

                con.fetchrow = fetchrow
                con.fetchval = fetchval
                con.execute = execute
                con.transaction = transaction
                return con

            async def __aexit__(self, *args):
                pass

        return Context()


@pytest.fixture
def mock_app(monkeypatch):
    # Slack проверяется Signing Secret приложения (зашифрован ключом ядра), а не hook_token
    monkeypatch.setenv("BOTHUB_SECRET_KEYS", "1:" + base64.b64encode(b"k" * 32).decode())
    app = create_app()
    pool = MockPool()
    pool.schedule["slack_signing_secret"] = encrypt_secret(SECRET.encode(), SCHEDULE_ID.bytes)
    app.state.pool = pool
    return app, pool


def test_github_route_ping_returns_202_without_queueing(mock_app):
    app, pool = mock_app
    client = TestClient(app)
    body = json.dumps({"zen": "Keep it simple"}).encode()
    sig = _github_sig(body)

    resp = client.post(
        f"/hooks/{SCHEDULE_ID}/github",
        content=body,
        headers={"X-Hub-Signature-256": sig, "X-GitHub-Event": "ping"},
    )
    assert resp.status_code == 202
    assert resp.json() == {"status": "accepted"}
    assert len(pool.dispatched_turns) == 0


def test_github_route_valid_event_queues_turn(mock_app):
    app, pool = mock_app
    client = TestClient(app)
    payload = {
        "action": "opened",
        "repository": {"full_name": "acme/botstead"},
        "issue": {"number": 1, "title": "Test Issue", "body": "Details"},
    }
    body = json.dumps(payload).encode()
    sig = _github_sig(body)

    resp = client.post(
        f"/hooks/{SCHEDULE_ID}/github",
        content=body,
        headers={"X-Hub-Signature-256": sig, "X-GitHub-Event": "issues"},
    )
    assert resp.status_code == 202
    assert resp.json() == {"status": "accepted"}
    assert len(pool.dispatched_turns) == 1
    dispatched = pool.dispatched_turns[0]
    assert "Schedule base prompt" in dispatched["prompt"]
    assert "GitHub событие: issues" in dispatched["prompt"]
    assert "#1 Test Issue" in dispatched["prompt"]


def test_github_route_invalid_signature_403(mock_app):
    app, pool = mock_app
    client = TestClient(app)
    body = json.dumps({"action": "opened"}).encode()

    resp = client.post(
        f"/hooks/{SCHEDULE_ID}/github",
        content=body,
        headers={"X-Hub-Signature-256": "sha256=bad", "X-GitHub-Event": "issues"},
    )
    assert resp.status_code == 403


def test_github_route_unsupported_event_skipped_silently_202(mock_app):
    app, pool = mock_app
    client = TestClient(app)
    body = json.dumps({"action": "started"}).encode()
    sig = _github_sig(body)

    resp = client.post(
        f"/hooks/{SCHEDULE_ID}/github",
        content=body,
        headers={"X-Hub-Signature-256": sig, "X-GitHub-Event": "star"},
    )
    assert resp.status_code == 202
    assert resp.json() == {"status": "accepted"}
    assert len(pool.dispatched_turns) == 0


def test_github_route_body_too_large_413(mock_app):
    app, _ = mock_app
    client = TestClient(app)
    large_body = b"x" * 65537

    resp = client.post(
        f"/hooks/{SCHEDULE_ID}/github",
        content=large_body,
        headers={"X-Hub-Signature-256": "sha256=test", "X-GitHub-Event": "issues"},
    )
    assert resp.status_code == 413


def test_github_route_schedule_not_found_404(mock_app):
    app, _ = mock_app
    client = TestClient(app)
    random_id = uuid.uuid4()
    body = json.dumps({"action": "opened"}).encode()

    resp = client.post(
        f"/hooks/{random_id}/github",
        content=body,
        headers={"X-Hub-Signature-256": "sha256=test", "X-GitHub-Event": "issues"},
    )
    assert resp.status_code == 404


def test_slack_route_url_verification_returns_200_challenge(mock_app):
    app, pool = mock_app
    client = TestClient(app)
    now = int(time.time())
    payload = {"type": "url_verification", "challenge": "secret_challenge_12345"}
    body = json.dumps(payload).encode()
    sig = _slack_sig(body, now)

    resp = client.post(
        f"/hooks/{SCHEDULE_ID}/slack",
        content=body,
        headers={"X-Slack-Signature": sig, "X-Slack-Request-Timestamp": str(now)},
    )
    assert resp.status_code == 200
    assert resp.json() == {"challenge": "secret_challenge_12345"}
    assert len(pool.dispatched_turns) == 0


def test_slack_route_app_mention_queues_turn(mock_app):
    app, pool = mock_app
    client = TestClient(app)
    now = int(time.time())
    payload = {
        "type": "event_callback",
        "event": {
            "type": "app_mention",
            "channel": "C123",
            "user": "U456",
            "text": "run deploy",
        },
    }
    body = json.dumps(payload).encode()
    sig = _slack_sig(body, now)

    resp = client.post(
        f"/hooks/{SCHEDULE_ID}/slack",
        content=body,
        headers={"X-Slack-Signature": sig, "X-Slack-Request-Timestamp": str(now)},
    )
    assert resp.status_code == 202
    assert resp.json() == {"status": "accepted"}
    assert len(pool.dispatched_turns) == 1
    dispatched = pool.dispatched_turns[0]
    assert "Schedule base prompt" in dispatched["prompt"]
    assert "Slack событие: app_mention" in dispatched["prompt"]
    assert "run deploy" in dispatched["prompt"]


def test_slack_route_ignored_event_skips_turn_202(mock_app):
    app, pool = mock_app
    client = TestClient(app)
    now = int(time.time())
    payload = {
        "type": "event_callback",
        "event": {
            "type": "message",
            "bot_id": "B999",  # Сообщение бота игнорируется
            "channel": "C123",
            "text": "automated",
        },
    }
    body = json.dumps(payload).encode()
    sig = _slack_sig(body, now)

    resp = client.post(
        f"/hooks/{SCHEDULE_ID}/slack",
        content=body,
        headers={"X-Slack-Signature": sig, "X-Slack-Request-Timestamp": str(now)},
    )
    assert resp.status_code == 202
    assert resp.json() == {"status": "accepted"}
    assert len(pool.dispatched_turns) == 0


def test_slack_route_expired_timestamp_403(mock_app):
    app, _ = mock_app
    client = TestClient(app)
    old_ts = int(time.time()) - 305  # старше 5 минут
    payload = {"type": "event_callback", "event": {"type": "app_mention"}}
    body = json.dumps(payload).encode()
    sig = _slack_sig(body, old_ts)

    resp = client.post(
        f"/hooks/{SCHEDULE_ID}/slack",
        content=body,
        headers={"X-Slack-Signature": sig, "X-Slack-Request-Timestamp": str(old_ts)},
    )
    assert resp.status_code == 403


def test_slack_route_invalid_signature_403(mock_app):
    app, _ = mock_app
    client = TestClient(app)
    now = int(time.time())
    payload = {"type": "event_callback"}
    body = json.dumps(payload).encode()

    resp = client.post(
        f"/hooks/{SCHEDULE_ID}/slack",
        content=body,
        headers={"X-Slack-Signature": "v0=bad", "X-Slack-Request-Timestamp": str(now)},
    )
    assert resp.status_code == 403


def test_generic_hook_route_still_works(mock_app):
    app, pool = mock_app
    client = TestClient(app)
    body = json.dumps({"custom": "data"}).encode()

    # С неверным токеном -> 403
    resp_bad = client.post(
        f"/hooks/{SCHEDULE_ID}",
        content=body,
        headers={"X-Hook-Token": "wrong-token"},
    )
    assert resp_bad.status_code == 403

    # С верным токеном -> 202, turn создан
    resp_ok = client.post(
        f"/hooks/{SCHEDULE_ID}",
        content=body,
        headers={"X-Hook-Token": SECRET},
    )
    assert resp_ok.status_code == 202
    assert resp_ok.json() == {"status": "accepted"}
    assert len(pool.dispatched_turns) == 1
    assert "Данные события:" in pool.dispatched_turns[0]["prompt"]


# ---- 6. Порядок проверок: подпись до разбора JSON, лимит, не-ASCII в подписи ----

def test_github_route_bad_signature_wins_over_invalid_json(mock_app):
    app, pool = mock_app
    client = TestClient(app)
    resp = client.post(
        f"/hooks/{SCHEDULE_ID}/github",
        content=b"{not json",
        headers={"X-Hub-Signature-256": "sha256=" + "0" * 64, "X-GitHub-Event": "issues"},
    )
    assert resp.status_code == 403  # не 400: тело не разбирается, пока подпись не подтверждена
    assert len(pool.dispatched_turns) == 0


def test_github_route_valid_signature_invalid_json_400(mock_app):
    app, _ = mock_app
    client = TestClient(app)
    body = b"{not json"
    resp = client.post(
        f"/hooks/{SCHEDULE_ID}/github",
        content=body,
        headers={"X-Hub-Signature-256": _github_sig(body), "X-GitHub-Event": "issues"},
    )
    assert resp.status_code == 400


def test_slack_route_bad_signature_wins_over_invalid_json(mock_app):
    app, pool = mock_app
    client = TestClient(app)
    now = int(time.time())
    resp = client.post(
        f"/hooks/{SCHEDULE_ID}/slack",
        content=b"{not json",
        headers={"X-Slack-Signature": "v0=" + "0" * 64, "X-Slack-Request-Timestamp": str(now)},
    )
    assert resp.status_code == 403
    assert len(pool.dispatched_turns) == 0


def test_slack_route_body_too_large_413(mock_app):
    app, _ = mock_app
    client = TestClient(app)
    now = int(time.time())
    resp = client.post(
        f"/hooks/{SCHEDULE_ID}/slack",
        content=b"x" * (hook_adapters.HOOK_MAX_BODY + 1),
        headers={"X-Slack-Signature": "v0=test", "X-Slack-Request-Timestamp": str(now)},
    )
    assert resp.status_code == 413


def test_body_at_exact_limit_is_not_rejected_by_size(mock_app):
    app, _ = mock_app
    client = TestClient(app)
    body = b"x" * hook_adapters.HOOK_MAX_BODY
    resp = client.post(
        f"/hooks/{SCHEDULE_ID}/github",
        content=body,
        headers={"X-Hub-Signature-256": _github_sig(body), "X-GitHub-Event": "issues"},
    )
    assert resp.status_code == 400  # ровно 64 КиБ проходит лимит, дальше падает на разборе JSON


def test_chunked_body_over_limit_413(mock_app):
    app, _ = mock_app
    client = TestClient(app)

    def chunks():
        for _ in range(3):
            yield b"x" * 30000

    resp = client.post(
        f"/hooks/{SCHEDULE_ID}/github",
        content=chunks(),
        headers={"X-Hub-Signature-256": "sha256=test", "X-GitHub-Event": "issues"},
    )
    assert resp.status_code == 413


def test_non_ascii_signature_header_is_403_not_500(mock_app):
    app, _ = mock_app
    client = TestClient(app, raise_server_exceptions=False)
    now = int(time.time())
    body = json.dumps({"action": "opened"}).encode()
    resp_gh = client.post(
        f"/hooks/{SCHEDULE_ID}/github",
        content=body,
        headers={"X-Hub-Signature-256": "sha256=\u00e9".encode("utf-8"), "X-GitHub-Event": "issues"},
    )
    assert resp_gh.status_code == 403
    resp_sl = client.post(
        f"/hooks/{SCHEDULE_ID}/slack",
        content=body,
        headers={"X-Slack-Signature": "v0=\u00e9".encode("utf-8"), "X-Slack-Request-Timestamp": str(now)},
    )
    assert resp_sl.status_code == 403


def test_signature_functions_accept_non_ascii_without_raising():
    body = b"{}"
    assert hook_adapters.verify_github_signature(body, SECRET, "sha256=\u00e9\u0416") is False
    assert hook_adapters.verify_slack_signature(body, SECRET, "v0=\u00e9", "1700000000", now_ts=1700000000) is False


def test_slack_timestamp_must_be_plain_ascii_digits():
    body = b"{}"
    now = 1700000000
    sig = _slack_sig(body, now)
    for bad_ts in ("+1700000000", "-1", "\u0661\u0667\u0660\u0660\u0660\u0660\u0660\u0660\u0660\u0660", "1_700_000_000"):
        assert hook_adapters.verify_slack_signature(body, SECRET, sig, bad_ts, now_ts=now) is False


def test_slack_route_without_signing_secret_is_403_even_with_hook_token_signature(mock_app):
    app, pool = mock_app
    pool.schedule["slack_signing_secret"] = None
    client = TestClient(app)
    now = int(time.time())
    body = json.dumps({"type": "url_verification", "challenge": "x"}).encode()
    resp = client.post(
        f"/hooks/{SCHEDULE_ID}/slack",
        content=body,
        headers={"X-Slack-Signature": _slack_sig(body, now, pool.schedule["hook_token"]), "X-Slack-Request-Timestamp": str(now)},
    )
    assert resp.status_code == 403

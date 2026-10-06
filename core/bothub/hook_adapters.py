"""Адаптеры вебхуков GitHub и Slack (этап 10).

Чистая логика: проверка подписей HMAC-SHA256, сборка промптов, определение типов событий.
Без обращений к БД и сети.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any

# Лимит тела вебхука: 64 КиБ
HOOK_MAX_BODY = 65536
# Допустимое отклонение времени в Slack: 5 минут (300 с)
SLACK_TIMESTAMP_TOLERANCE = 300
# Предел длины текста события в промпте
PROMPT_CONTENT_LIMIT = 2000

GITHUB_SUPPORTED_EVENTS = frozenset({
    "issues",
    "issue_comment",
    "pull_request",
    "pull_request_review",
    "push",
    "workflow_run",
})


def _sig_equals(signature_header: str, expected: str, bare: str) -> bool:
    """Сравнение за постоянное время по байтам: compare_digest на str с не-ASCII символами бросает TypeError (был бы 500)."""
    sig = signature_header.strip().lower().encode("utf-8")
    # Обе проверки выполняются всегда, без раннего выхода
    matched_prefixed = hmac.compare_digest(sig, expected.encode("ascii"))
    matched_bare = hmac.compare_digest(sig, bare.encode("ascii"))
    return matched_prefixed or matched_bare


def verify_github_signature(raw_body: bytes, secret: str | None, signature_header: str | None) -> bool:
    """Проверка подписи X-Hub-Signature-256 (HMAC-SHA256 сырого тела с hook_token).

    Сравнение за постоянное время. При отсутствии секрета или заголовка возвращает False.
    Поддерживает стандартный формат с префиксом sha256= и hex-строку без него.
    """
    if not secret or not signature_header:
        return False
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    expected = f"sha256={digest}"
    return _sig_equals(signature_header, expected, digest)


def build_github_prompt(event: str, payload: dict[str, Any]) -> str | None:
    """Сборка короткого русскоязычного промпта для поддерживаемых событий GitHub.

    Для событий issues, issue_comment, pull_request, pull_request_review, push, workflow_run
    возвращает строку промпта. Для ping и остальных событий возвращает None (пропуск).
    """
    if event not in GITHUB_SUPPORTED_EVENTS:
        return None

    repo_data = payload.get("repository")
    repo = repo_data.get("full_name", "") if isinstance(repo_data, dict) else ""
    action = str(payload.get("action", ""))

    title_number = ""
    author = ""
    html_url = ""
    body = ""

    if event == "issues":
        issue = payload.get("issue") if isinstance(payload.get("issue"), dict) else {}
        num = issue.get("number", "")
        title = issue.get("title", "")
        title_number = f"#{num} {title}".strip() if (num or title) else ""
        user = issue.get("user") if isinstance(issue.get("user"), dict) else {}
        sender = payload.get("sender") if isinstance(payload.get("sender"), dict) else {}
        author = user.get("login") or sender.get("login", "")
        html_url = issue.get("html_url", "")
        body = issue.get("body") or ""

    elif event == "issue_comment":
        issue = payload.get("issue") if isinstance(payload.get("issue"), dict) else {}
        num = issue.get("number", "")
        title = issue.get("title", "")
        title_number = f"#{num} {title}".strip() if (num or title) else ""
        comment = payload.get("comment") if isinstance(payload.get("comment"), dict) else {}
        cuser = comment.get("user") if isinstance(comment.get("user"), dict) else {}
        sender = payload.get("sender") if isinstance(payload.get("sender"), dict) else {}
        author = cuser.get("login") or sender.get("login", "")
        html_url = comment.get("html_url") or issue.get("html_url", "")
        body = comment.get("body") or ""

    elif event == "pull_request":
        pr = payload.get("pull_request") if isinstance(payload.get("pull_request"), dict) else {}
        num = pr.get("number", "")
        title = pr.get("title", "")
        title_number = f"#{num} {title}".strip() if (num or title) else ""
        puser = pr.get("user") if isinstance(pr.get("user"), dict) else {}
        sender = payload.get("sender") if isinstance(payload.get("sender"), dict) else {}
        author = puser.get("login") or sender.get("login", "")
        html_url = pr.get("html_url", "")
        body = pr.get("body") or ""

    elif event == "pull_request_review":
        pr = payload.get("pull_request") if isinstance(payload.get("pull_request"), dict) else {}
        num = pr.get("number", "")
        title = pr.get("title", "")
        title_number = f"#{num} {title}".strip() if (num or title) else ""
        review = payload.get("review") if isinstance(payload.get("review"), dict) else {}
        ruser = review.get("user") if isinstance(review.get("user"), dict) else {}
        sender = payload.get("sender") if isinstance(payload.get("sender"), dict) else {}
        author = ruser.get("login") or sender.get("login", "")
        html_url = review.get("html_url") or pr.get("html_url", "")
        body = review.get("body") or ""

    elif event == "push":
        ref = str(payload.get("ref", ""))
        action = f"push {ref}".strip() if ref else "push"
        pusher = payload.get("pusher") if isinstance(payload.get("pusher"), dict) else {}
        sender = payload.get("sender") if isinstance(payload.get("sender"), dict) else {}
        author = pusher.get("name") or sender.get("login", "")
        head_commit = payload.get("head_commit") if isinstance(payload.get("head_commit"), dict) else {}
        title_number = ref or (head_commit.get("id", "")[:7] if head_commit else "")
        html_url = payload.get("compare") or (head_commit.get("url", "") if head_commit else "")

        commits = payload.get("commits")
        commit_msgs = []
        if isinstance(commits, list):
            for c in commits:
                if isinstance(c, dict) and c.get("message"):
                    commit_msgs.append(str(c["message"]))
        if not commit_msgs and head_commit and head_commit.get("message"):
            commit_msgs.append(str(head_commit["message"]))
        body = "\n".join(commit_msgs)

    elif event == "workflow_run":
        wf = payload.get("workflow_run") if isinstance(payload.get("workflow_run"), dict) else {}
        num = wf.get("run_number", "")
        wfname = wf.get("name", "")
        title_number = f"#{num} {wfname}".strip() if (num or wfname) else str(wf.get("id", ""))
        actor = wf.get("actor") if isinstance(wf.get("actor"), dict) else {}
        trig = wf.get("triggering_actor") if isinstance(wf.get("triggering_actor"), dict) else {}
        sender = payload.get("sender") if isinstance(payload.get("sender"), dict) else {}
        author = actor.get("login") or trig.get("login") or sender.get("login", "")
        html_url = wf.get("html_url", "")

        status = wf.get("status", "")
        conclusion = wf.get("conclusion", "")
        display_title = wf.get("display_title", "")
        parts = []
        if display_title:
            parts.append(display_title)
        if status or conclusion:
            parts.append(f"Статус: {status}, результат: {conclusion}")
        body = "\n".join(parts)

    body_cut = str(body)[:PROMPT_CONTENT_LIMIT]
    lines = [
        f"GitHub событие: {event}",
        f"Репозиторий: {repo}",
        f"Действие: {action}",
        f"Заголовок / номер: {title_number}",
        f"Автор: {author}",
        f"Ссылка: {html_url}",
    ]
    if body_cut:
        lines.append("")
        lines.append(body_cut)
    return "\n".join(lines)


def verify_slack_signature(raw_body: bytes, secret: str | None, signature_header: str | None,
                           timestamp_header: str | None, *, now_ts: float | None = None) -> bool:
    """Проверка подписи Slack: X-Slack-Signature v0=HMAC-SHA256(v0:{timestamp}:{raw_body}, Signing Secret приложения Slack).

    Временная метка должна быть не старше 5 минут (300 с).
    Сравнение за постоянное время.
    """
    if not secret or not signature_header or not timestamp_header:
        return False

    ts_text = timestamp_header.strip()
    if not (ts_text.isascii() and ts_text.isdigit()):
        return False
    ts = int(ts_text)

    current_time = time.time() if now_ts is None else now_ts
    if abs(current_time - ts) > SLACK_TIMESTAMP_TOLERANCE:
        return False

    basestring = b"v0:" + ts_text.encode("ascii") + b":" + raw_body
    digest = hmac.new(secret.encode("utf-8"), basestring, hashlib.sha256).hexdigest()
    expected = f"v0={digest}"
    return _sig_equals(signature_header, expected, digest)


def build_slack_prompt(payload: dict[str, Any]) -> str | None:
    """Сборка промпта для event_callback Slack.

    Поддерживаются app_mention и message (без bot_id и без subtype).
    Остальные события возвращают None.
    """
    if payload.get("type") != "event_callback":
        return None

    event = payload.get("event")
    if not isinstance(event, dict):
        return None

    event_type = event.get("type")
    if event_type == "app_mention":
        pass
    elif event_type == "message":
        if event.get("bot_id") or event.get("subtype"):
            return None
    else:
        return None

    channel = str(event.get("channel", ""))
    user = str(event.get("user", ""))
    text = str(event.get("text") or "")[:PROMPT_CONTENT_LIMIT]

    lines = [
        f"Slack событие: {event_type}",
        f"Канал: {channel}",
        f"Пользователь: {user}",
    ]
    if text:
        lines.append("")
        lines.append(text)
    return "\n".join(lines)

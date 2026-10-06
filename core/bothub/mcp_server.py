"""MCP-сервер bothub (stdio). Контракт: docs/contracts.md, разделы 4-6.

Запуск: `python -m bothub.mcp_server`, внутри контейнера бота.
Env: BOTHUB_URL, BOTHUB_TOKEN, BOTHUB_THREAD_ID, BOTHUB_TURN_ID — читаются
лениво внутри вызовов инструментов, не при импорте модуля (иначе процесс не
стартовал бы без них, что мешает даже проверить, что модуль импортируется).
"""
import asyncio
import base64
import logging
import os
import re
import time
from pathlib import Path

import httpx
from mcp.server.mcpserver import Image, MCPServer
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from .browser_control import is_browser_tool, mask_browser_args, mask_input_values, url_forbidden, url_origin
from . import delegation, wakeups
from .mcp_policy import NOT_ALLOWED
from .risk import classify

log = logging.getLogger("bothub.mcp_server")

mcp = MCPServer("bothub")

MAC_TOOLS = (
    "find_files", "read_file", "upload_file", "preview", "screenshot",
    "open", "applescript", "shortcut", "shell", "delegate", "click", "type_text",
    "move_to_trash",
)

# mac_screenshot/mac_preview: картинка большая (сотни КБ - пара МБ base64), модель
# такое не смотрит как JSON-текст. Вместо этого возвращаем ImageContent (Image
# из mcpserver) + короткую подпись, и отдельно кладём файл в тред через /api/files,
# чтобы владелец видел скриншот в PWA (ошибка загрузки не должна ломать ответ).
IMAGE_MAC_TOOLS = {"screenshot", "preview"}

# delegate: раздел 5/6, args.timeout по умолчанию 900 максимум 1800 на самой
# Mac-машине; таймаут HTTP-вызова /api/mac/call = это значение + запас 60 с,
# чтобы core не сдался раньше, чем агент успеет вернуть результат/таймаут сам.
DELEGATE_TIMEOUT_DEFAULT = 900
DELEGATE_TIMEOUT_MAX = 1800
DELEGATE_HTTP_SLACK = 60

# ponytail: раздел 8 контракта (таймауты) в docs/contracts.md на момент
# написания не существует (файл кончается на разделе 7) — константы ниже
# разумные значения по умолчанию, а не цитата контракта.
MAC_RETRY_INTERVAL = 15.0
MAC_RETRY_TIMEOUT = 120.0
APPROVAL_WAIT_TIMEOUT = 1800.0  # 30 минут, как сказано в задаче
APPROVAL_WAIT_POLL = 25  # секунд, GET /api/approvals/{id}/wait?timeout=25


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=os.environ["BOTHUB_URL"],
        headers={"Authorization": f"Bearer {os.environ['BOTHUB_TOKEN']}"},
        timeout=40.0,  # выше APPROVAL_WAIT_POLL, чтобы long-poll не резался клиентом
    )


def approval_title(tool_name: str, args: dict) -> str:
    """Заголовок запроса на одобрение: что именно хочет сделать бот, а не только имя инструмента.
    Значения берутся из уже замаскированных args (ввод в браузер сюда не попадает)."""
    def short(value, limit=120):
        text = " ".join(str(value).split())
        return text if len(text) <= limit else text[:limit - 1] + "…"
    if not isinstance(args, dict):
        return tool_name
    if is_browser_tool(tool_name):
        action = str(args.get("action", ""))
        detail = args.get("url") if action == "navigate" else args.get("element") or args.get("target") or ""
        return short(f"browser {action} {detail}".strip())
    for key in ("command", "url", "file_path", "path", "pattern", "query", "prompt", "description"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return short(f"{tool_name}: {value}")
    return tool_name


@mcp.tool()
async def approve(tool_name: str, input: dict) -> dict:
    """Раздел 4: permission-prompt-tool для claude. Решает allow/deny через ядро."""
    risk = classify(tool_name, input)
    # Введённый в браузер текст ядру не отправляется: маскируем здесь, до запроса.
    args = _browser_approval_args(input) if is_browser_tool(tool_name) else input
    payload = {
        "thread_id": os.environ["BOTHUB_THREAD_ID"],
        "turn_id": os.environ["BOTHUB_TURN_ID"],
        "risk": risk,
        "title": approval_title(tool_name, args)[:512],
        "tool": tool_name,
        "args": args,
    }
    async with _client() as client:
        resp = await client.post("/api/approvals", json=payload)
        resp.raise_for_status()
        approval = resp.json()
        deadline = time.monotonic() + APPROVAL_WAIT_TIMEOUT
        while approval.get("status") == "pending" and time.monotonic() < deadline:
            resp = await client.get(
                f"/api/approvals/{approval['id']}/wait",
                params={"timeout": APPROVAL_WAIT_POLL},
            )
            resp.raise_for_status()
            approval = resp.json()

    if approval.get("status") == "approved":
        return {"behavior": "allow", "updatedInput": input}
    if approval.get("status") == "rejected" and approval.get("reason") == NOT_ALLOWED:
        # чужой MCP-инструмент вне mcp_allow бота: ядро отклонило сразу, владельцу вопрос не ушёл
        return {"behavior": "deny", "message": NOT_ALLOWED}
    if approval.get("status") == "rejected" and approval.get("reason") == "checker_denied":
        # проверяющая модель бота отклонила действие, владельцу вопрос не ушёл (раздел 20)
        return {"behavior": "deny", "message": f"checker_denied: {str(approval.get('checker_reason') or '')[:200]}".rstrip(": ")}
    return {"behavior": "deny", "message": f"approval {approval.get('status', 'expired')}"}


async def _mac_call(tool: str, args: dict) -> dict:
    payload = {
        "thread_id": os.environ["BOTHUB_THREAD_ID"],
        "turn_id": os.environ["BOTHUB_TURN_ID"],
        "tool": tool,
        "args": args,
    }
    deadline = time.monotonic() + MAC_RETRY_TIMEOUT
    async with _client() as client:
        while True:
            resp = await client.post("/api/mac/call", json=payload)
            if resp.status_code == 409:
                if time.monotonic() < deadline:
                    await asyncio.sleep(MAC_RETRY_INTERVAL)
                    continue
                return resp.json()  # сдаёмся: отдаём {"state": ...} как есть
            resp.raise_for_status()
            return resp.json()


def _delegate_client(timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=os.environ["BOTHUB_URL"],
        headers={"Authorization": f"Bearer {os.environ['BOTHUB_TOKEN']}"},
        timeout=timeout,
    )


async def _mac_delegate(args: dict) -> dict:
    """Отдать подзадачу субагенту на Mac владельца: engine=gemini для ресёрча и
    длинного чтения, codex для кода, claude для второго мнения. Если Mac
    недоступен (ошибка mac_unavailable), делай задачу сам."""
    requested = min(int(args.get("timeout", DELEGATE_TIMEOUT_DEFAULT)), DELEGATE_TIMEOUT_MAX)
    call_timeout = requested + DELEGATE_HTTP_SLACK
    payload = {
        "thread_id": os.environ["BOTHUB_THREAD_ID"],
        "turn_id": os.environ["BOTHUB_TURN_ID"],
        "tool": "delegate",
        "args": args,
        "timeout": call_timeout,
    }
    async with _delegate_client(call_timeout + 20.0) as client:
        resp = await client.post("/api/mac/call", json=payload)
    if resp.status_code == 409:
        # без повтора: Mac не в сети — модель должна сделать задачу сама, не ждать
        return {"ok": False, "error": "Mac не в сети, выполни сам"}
    resp.raise_for_status()
    return resp.json()


async def _attach_image(data: bytes, fmt: str, origin: str) -> None:
    """POST /api/files, чтобы владелец видел картинку в PWA. Ошибка не должна ломать ответ модели."""
    try:
        async with _client() as client:
            resp = await client.post(
                "/api/files",
                data={"thread_id": os.environ["BOTHUB_THREAD_ID"], "origin": origin},
                files={"file": (f"{origin.split(':')[-1]}.{fmt}", data, f"image/{fmt}")},
            )
            resp.raise_for_status()
    except Exception:
        log.warning("не смог приложить %s в тред", origin, exc_info=True)


async def _mac_image_tool(tool: str, args: dict) -> list[Image | str]:
    result = await _mac_call(tool, args)
    # Реальный ответ ядра: {"type": "result", "ok": true, "data": {...}} (раздел 5); плоский вид тоже принимаем.
    payload = result.get("data") if isinstance(result, dict) and isinstance(result.get("data"), dict) else result
    b64 = payload.get("jpeg_b64") or payload.get("png_b64") if isinstance(payload, dict) else None
    if not b64:
        # Mac не в сети (409) или неожиданный ответ — модели текст лучше, чем пусто.
        return [str(result)]
    fmt = "jpeg" if "jpeg_b64" in payload else "png"
    data = base64.b64decode(b64)
    await _attach_image(data, fmt, origin=f"mac:{tool}")
    caption = f"{tool}: {payload.get('width')}x{payload.get('height')}, {len(data) // 1024} КБ"
    return [Image(data=data, format=fmt), caption]


def _register_mac_tool(tool: str) -> None:
    if tool == "delegate":
        handler = _mac_delegate
        doc = handler.__doc__
    elif tool in IMAGE_MAC_TOOLS:
        async def handler(args: dict) -> list[Image | str]:
            return await _mac_image_tool(tool, args)

        doc = (
            f"Раздел 5: Mac-инструмент '{tool}' через POST /api/mac/call. "
            "Возвращает картинку моделью как ImageContent и прикладывает файлом в тред."
        )
    else:
        async def handler(args: dict) -> dict:
            return await _mac_call(tool, args)

        doc = f"Раздел 5: Mac-инструмент '{tool}' через POST /api/mac/call."
    handler.__name__ = f"mac_{tool}"
    handler.__doc__ = doc
    mcp.tool(name=f"mac_{tool}")(handler)


for _tool in MAC_TOOLS:
    _register_mac_tool(_tool)


_browser_jobs = None
_browser_task = None
PLAYWRIGHT_MCP_COMMAND = "playwright-mcp"

# Что бот видит на странице: origin и подписи элементов по ref из snapshot. Подпись берётся со страницы,
# а не из слов модели: по ней ядро отличает кнопку оплаты от безобидной, даже если модель назвала её иначе.
_page_origin: str | None = None
_page_labels: dict[str, str] = {}
_page_elements: dict[str, tuple[str, str]] = {}  # ref -> (роль, имя): ядро кладёт их в событие шага для процедур (раздел 14)
_PAGE_URL_RE = re.compile(r"^- Page URL: (\S+)", re.M)
_REF_LINE_RE = re.compile(r'^\s*-\s+(?P<role>[\w-]+)(?:\s+"(?P<name>(?:[^"\\\n]|\\.)*)")?[^\n]*?\[ref=(?P<ref>\w+)\][^\n:]*(?::[ \t]*(?P<tail>[^\n]*))?', re.M)
_NAME_ESCAPE_RE = re.compile(r'\\(u[0-9a-fA-F]{4}|.)')
_NAME_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f"}
_PAGE_LABEL_LIMIT = 2000


def _unquote_name(quoted: str | None) -> str:
    """Имя элемента из кавычек строки snapshot: `\\"` это кавычка, `\\\\` обратная косая, `\\n` и `\\t` управляющие символы
    (ядро их вычистит). Без кавычек имя пустое: текст после двоеточия это значение или содержимое страницы, не имя."""
    if quoted is None:
        return ""
    return _NAME_ESCAPE_RE.sub(
        lambda m: chr(int(m[1][1:], 16)) if len(m[1]) == 5 else _NAME_ESCAPES.get(m[1], m[1]), quoted)


def _remember_page(text: str) -> None:
    """Обновить origin и подписи по тексту ответа Playwright. Первая строка Page URL: заголовок состояния."""
    global _page_origin
    page = _PAGE_URL_RE.search(text)
    if page:
        origin = url_origin(page.group(1))
        if origin != _page_origin:
            _page_labels.clear()
            _page_elements.clear()
        _page_origin = origin
    for match in _REF_LINE_RE.finditer(text):
        name = _unquote_name(match["name"])
        label = " ".join(filter(None, (match["role"], name, (match["tail"] or "").strip())))[:200]
        if len(_page_labels) < _PAGE_LABEL_LIMIT or match["ref"] in _page_labels:
            _page_labels[match["ref"]] = label
            _page_elements[match["ref"]] = (match["role"], name[:200])


def _browser_approval_args(input: dict) -> dict:
    args = mask_browser_args(input)
    if isinstance(args, dict) and args.get("action") in ("click", "fill"):
        if _page_origin:
            args["origin"] = _page_origin
        label = _page_labels.get(str(args.get("target", "")))
        if label:
            args["page_label"] = label
    return args


async def _browser_worker(jobs):
    server = StdioServerParameters(command=PLAYWRIGHT_MCP_COMMAND,
                                   args=["--cdp-endpoint", "http://127.0.0.1:9222"])
    try:
        async with stdio_client(server) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                while True:
                    name, args, future = await jobs.get()
                    try:
                        result = await session.call_tool(name, args, read_timeout_seconds=30)
                        if not future.done():
                            future.set_result(result)
                    except Exception as exc:
                        if not future.done():
                            future.set_exception(exc)
    finally:
        while not jobs.empty():
            _, _, future = jobs.get_nowait()
            if not future.done():
                future.set_exception(RuntimeError("browser_tool_failed"))


async def _browser_call(name: str, args: dict):
    global _browser_jobs, _browser_task
    if _browser_task is None or _browser_task.done():
        _browser_jobs = asyncio.Queue()
        _browser_task = asyncio.create_task(_browser_worker(_browser_jobs))
    future = asyncio.get_running_loop().create_future()
    await _browser_jobs.put((name, args, future))
    done, _ = await asyncio.wait((future, _browser_task), timeout=40,
                                 return_when=asyncio.FIRST_COMPLETED)
    if future in done:
        return future.result()
    future.cancel()
    raise RuntimeError("browser_tool_failed")


@mcp.tool()
async def browser(action: str, target: str = "", value: str = "", url: str = "",
                  element: str = "") -> list[Image | str]:
    """Control the bot's headed Chromium. Actions: navigate, click, fill, snapshot, screenshot.

    Use snapshot to refresh page state after a human returns control. For click and fill give
    `element`: a short description of the button or field (its visible text or label), the owner
    sees it when approving. Text passed to fill is never sent to the core. navigate accepts only
    http and https addresses of public sites.
    """
    names = {"navigate": "browser_navigate", "click": "browser_click",
             "fill": "browser_type", "snapshot": "browser_snapshot",
             "screenshot": "browser_take_screenshot"}
    if action not in names:
        return ["invalid_browser_action"]
    if action == "navigate" and (reason := url_forbidden(url)):
        return [f"url_forbidden: {reason}"]
    payload = {"thread_id": os.environ["BOTHUB_THREAD_ID"], "turn_id": os.environ["BOTHUB_TURN_ID"],
               "action": action}
    async with _client() as client:
        gate = await client.post("/api/browser/authorize",
                                 json=payload | ({"url": url} if action == "navigate" else {}))
    if gate.status_code != 200:
        return [str(gate.json().get("detail", "browser_unavailable"))]
    authorization_id = gate.json().get("authorization_id")
    args = ({"url": url} if action == "navigate" else
            {"target": target, "element": element or target} if action == "click" else
            {"target": target, "element": element or target, "text": value} if action == "fill" else {})
    detail = ""
    try:
        result = await _browser_call(names[action], args)
        # mcp 2.x: поле is_error, у mcp 1.x оно называлось isError
        success = not (getattr(result, "is_error", None) or getattr(result, "isError", False))
        if not success:  # текст ошибки Playwright MCP: без него модель и владелец видят только browser_tool_failed
            detail = " ".join(getattr(block, "text", "") for block in result.content)[:300]
    except Exception as exc:
        result = None
        success = False
        detail = f"{type(exc).__name__}: {exc}"[:300]
    element = _page_elements.get(target) if action in ("click", "fill") else None
    async with _client() as client:
        audit = await client.post("/api/browser/step", json=payload | {
            "authorization_id": authorization_id,
            "target": target[:256], "url": url[:2048] if action == "navigate" else None,
            "result": "ok" if success else "error"} | ({"role": element[0], "name": element[1]} if element else {}))
    if audit.status_code != 200:
        return [str(audit.json().get("detail", "browser_unavailable"))]
    if not success:
        return [f"browser_tool_failed: {detail}" if detail else "browser_tool_failed"]
    output: list[Image | str] = []
    for block in result.content:
        if block.type == "image":
            output.append(Image(data=base64.b64decode(block.data), format=block.mimeType.split("/")[-1]))
        elif block.type == "text":
            # The person types passwords in this browser: input values never reach the model or the approval label.
            text = mask_input_values(block.text)
            _remember_page(text)
            output.append(text)
    return output or ["ok"]


@mcp.tool()
async def attach_file(path: str) -> dict:
    """Раздел 6: путь в контейнере -> POST /api/files (multipart)."""
    file_path = Path(path)
    async with _client() as client:
        with file_path.open("rb") as fh:
            resp = await client.post(
                "/api/files",
                data={
                    "thread_id": os.environ["BOTHUB_THREAD_ID"],
                    "origin": f"container:{path}",
                },
                files={"file": (file_path.name, fh)},
            )
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
async def remember(text: str, expires_at: str | None = None) -> dict:
    """Раздел 6: POST /api/memory, статус proposed (решает ядро)."""
    payload: dict = {"text": text}
    if expires_at:
        payload["expires_at"] = expires_at
    async with _client() as client:
        resp = await client.post("/api/memory", json=payload)
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
async def schedule_wakeup(prompt: str, reason: str = "", at: str | None = None,
                          in_minutes: int | None = None) -> dict:
    """Wake yourself up later: the core starts a new turn with `prompt` at the given moment.

    Give exactly one of `at` (ISO 8601 time, no zone means UTC) or `in_minutes`. Allowed range: from 1 minute
    to 30 days from now. `prompt` (up to 2000 characters) is what you will be asked then, write it so that it
    makes sense without this conversation. `reason` (up to 200 characters) is a short note the owner sees.
    At most 20 wakeups can be active; asking twice for the same prompt within a minute of the same time returns
    the existing one (`deduplicated: true`). A paused bot skips the wakeup. Refusals come back as
    `{"ok": false, "error": <code>}`; a successful call returns the wakeup with its `id` and `scheduled_at`.
    """
    try:
        wakeups.clean_prompt(prompt)
        wakeups.clean_reason(reason)
    except wakeups.WakeupError as exc:
        return {"ok": False, "error": exc.code}
    payload: dict = {"prompt": prompt, "reason": reason, "thread_id": os.environ["BOTHUB_THREAD_ID"]}
    if at is not None:
        payload["at"] = at
    if in_minutes is not None:
        payload["in_minutes"] = in_minutes
    async with _client() as client:
        resp = await client.post("/api/bots/wakeups", json=payload)
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail")
        except ValueError:
            detail = None
        return {"ok": False, "error": detail if isinstance(detail, str) and detail else f"http_{resp.status_code}"}
    return {"ok": True, **resp.json()}


async def _core_error(resp) -> dict:
    try:
        detail = resp.json().get("detail")
    except ValueError:
        detail = None
    if isinstance(detail, dict):
        detail = detail.get("detail") or detail.get("error")
    return {"ok": False, "error": detail if isinstance(detail, str) and detail else f"http_{resp.status_code}"}


@mcp.tool()
async def delegate_to_bot(bot: str, task: str) -> dict:
    """Hand a task to another bot of the same owner. It runs in that bot's own thread; you do not wait for it.

    `bot` is the other bot's name or id. `task` (1 to 4000 characters) must make sense without this conversation:
    the other bot sees only this text, prefixed with who sent it. Returns at once with `turn_id` and `thread_id`;
    call `delegation_result` with the `turn_id` later (when you have other work, or after a while) to get the answer.
    Limits: not to yourself, not to a paused bot or one without a model, at most 5 unfinished delegations at a time,
    and a bot that is itself working on a delegated task cannot delegate further. The owner approves this call like
    other actions unless a rule allows it. Refusals come back as `{"ok": false, "error": <code>}`.
    """
    try:
        delegation.clean_target(bot)
        delegation.clean_task(task)
    except delegation.DelegationError as exc:
        return {"ok": False, "error": exc.code}
    payload = {"bot": bot, "task": task, "turn_id": os.environ["BOTHUB_TURN_ID"]}
    async with _client() as client:
        resp = await client.post("/api/bots/delegations", json=payload)
    if resp.status_code >= 400:
        return await _core_error(resp)
    return {"ok": True, **resp.json()}


@mcp.tool()
async def delegation_result(turn_id: str) -> dict:
    """Check a task you handed over with `delegate_to_bot`. Only the bot that delegated it can read it.

    Returns `status` (queued, running, waiting_approval, waiting_mac, done, stopped, error). When `status` is `done`,
    `result` holds the other bot's final reply (up to 8000 characters); on `error`, `error` says why.
    """
    async with _client() as client:
        resp = await client.get(f"/api/bots/delegations/{turn_id}")
    if resp.status_code >= 400:
        return await _core_error(resp)
    return {"ok": True, **resp.json()}


if __name__ == "__main__":
    mcp.run()

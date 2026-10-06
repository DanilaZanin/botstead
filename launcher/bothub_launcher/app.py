"""HTTP API лаунчера. Секрет в `Authorization: Bearer`, открыт только /v1/health. Принимает одни идентификаторы
и команды: образ, mounts, сеть и лимиты в запросе задать нельзя (лишние поля дают 400)."""
import asyncio
import hmac
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, Body, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict

from .backend import Backend, DockerCLIBackend
from .config import Config
from .errors import LauncherError
from .netpolicy import NetPolicy
from .procs import SubprocessRunner
from .service import Launcher

log = logging.getLogger("bothub_launcher")
MAX_BODY = 12 * 1024 * 1024
SCREEN_INPUT_MAX = 64 * 1024
OPEN_PATHS = {"/v1/health"}


def error_body(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


class AuthMiddleware:
    """Чистый ASGI, а не BaseHTTPMiddleware: стримы exec должны отменяться при обрыве клиента."""

    def __init__(self, app, secret: str):
        self.app = app
        self._secret = secret.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self.app(scope, receive, send)
        if scope["type"] != "http":
            return  # websocket и прочее лаунчеру не нужны
        if scope["path"] not in OPEN_PATHS:
            auth = dict(scope["headers"]).get(b"authorization", b"")
            if not (auth.startswith(b"Bearer ") and hmac.compare_digest(auth[7:], self._secret)):
                return await self._reject(send, 401, "unauthorized", "нужен заголовок Authorization: Bearer")
            body_limit = SCREEN_INPUT_MAX if scope["path"].startswith("/v1/screen-sessions/") \
                and scope["path"].endswith("/input") else MAX_BODY
            length = dict(scope["headers"]).get(b"content-length", b"0")
            if length.isdigit() and int(length) > body_limit:
                return await self._reject(send, 413, "invalid", "тело запроса слишком большое")
            if scope["method"] in {"POST", "PUT", "PATCH"}:
                body = bytearray()
                while True:
                    message = await receive()
                    if message["type"] != "http.request":
                        return
                    chunk = message.get("body", b"")
                    if len(body) + len(chunk) > body_limit:
                        return await self._reject(send, 413, "invalid", "тело запроса слишком большое")
                    body.extend(chunk)
                    if not message.get("more_body", False):
                        break

                original_receive = receive
                replayed = False

                async def replay():
                    nonlocal replayed
                    if not replayed:
                        replayed = True
                        return {"type": "http.request", "body": bytes(body), "more_body": False}
                    return await original_receive()

                receive = replay
        await self.app(scope, receive, send)

    @staticmethod
    async def _reject(send, status: int, code: str, message: str):
        body = json.dumps(error_body(code, message)).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateBot(Strict):
    bot_id: str
    owner_id: str


class ExecRequest(Strict):
    argv: list[str]
    env: dict[str, str] = {}
    stdin: str | None = None
    exec_id: str | None = None
    timeout: float | None = None


class BrowserModeRequest(Strict):
    mode: str
    url: str | None = None


class ProcedureStepRequest(Strict):
    payload_json: str
    dry_run: bool = False
    timeout: float | None = None
    exec_id: str | None = None


class StopRequest(Strict):
    bot_id: str | None = None


class ProcedureCancelRequest(Strict):
    exec_id: str


class SessionRequest(Strict):
    command: str = "shell"
    cols: int = 80
    rows: int = 24


class ResizeRequest(Strict):
    cols: int
    rows: int


class ScreenSessionRequest(Strict):
    owner_id: str


async def _ndjson(frames: AsyncIterator[dict]) -> AsyncIterator[bytes]:
    try:
        async for frame in frames:
            yield (json.dumps(frame, separators=(",", ":")) + "\n").encode()
    except LauncherError as exc:
        yield (json.dumps({"t": "error", "code": exc.code, "message": str(exc)}) + "\n").encode()
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("ошибка в потоке")
        yield (json.dumps({"t": "error", "code": "error", "message": "внутренняя ошибка"}) + "\n").encode()
    finally:
        await frames.aclose()


def _stream(frames: AsyncIterator[dict], headers: dict | None = None) -> StreamingResponse:
    return StreamingResponse(_ndjson(frames), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-store", **(headers or {})})


class _ScreenStreamingResponse(StreamingResponse):
    """Closes the reserved launcher slot even if the ASGI send fails before iteration starts."""

    def __init__(self, content, launcher: Launcher, session_id: str):
        super().__init__(content, media_type="application/octet-stream", headers={"Cache-Control": "no-store"})
        self._launcher = launcher
        self._session_id = session_id

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            await asyncio.shield(self._launcher.close_screen_session(self._session_id))


def build_router() -> APIRouter:
    r = APIRouter(prefix="/v1")

    def svc(request: Request) -> Launcher:
        return request.app.state.launcher

    @r.get("/health")
    async def health():
        return {"ok": True}

    @r.get("/info")
    async def info(request: Request):
        return svc(request).info()

    @r.post("/networks/{owner_id}")
    async def ensure_network(owner_id: str, request: Request):
        return await svc(request).ensure_owner_network(owner_id)

    @r.post("/bots", status_code=201)
    async def create_bot(request: Request, body: CreateBot):
        return await svc(request).create_bot(body.bot_id, body.owner_id)

    @r.get("/bots")
    async def list_bots(request: Request):
        return {"bots": await svc(request).list_bots()}

    @r.get("/bots/{bot_id}")
    async def status(bot_id: str, request: Request):
        return await svc(request).status(bot_id)

    @r.delete("/bots/{bot_id}")
    async def remove_bot(bot_id: str, request: Request, purge: bool = False):
        return await svc(request).remove_bot(bot_id, purge=purge)

    @r.post("/bots/{bot_id}/recreate")
    async def recreate_bot(bot_id: str, request: Request):
        return await svc(request).recreate_bot(bot_id)

    @r.post("/bots/{bot_id}/browser")
    async def ensure_browser(bot_id: str, request: Request):
        return await svc(request).ensure_browser(bot_id)

    @r.post("/bots/{bot_id}/browser-mode")
    async def browser_mode(bot_id: str, request: Request, body: BrowserModeRequest):
        return await svc(request).browser_mode(bot_id, body.mode, body.url)

    @r.get("/bots/{bot_id}/browser-tab")
    async def browser_tab(bot_id: str, request: Request):
        return await svc(request).browser_tab(bot_id)

    @r.post("/bots/{bot_id}/freeze")
    async def freeze_bot(bot_id: str, request: Request):
        return await svc(request).freeze_bot(bot_id)

    @r.post("/bots/{bot_id}/unfreeze")
    async def unfreeze_bot(bot_id: str, request: Request):
        return await svc(request).unfreeze_bot(bot_id)

    @r.post("/bots/{bot_id}/exec")
    async def exec_bot(bot_id: str, request: Request, body: ExecRequest):
        launcher = svc(request)
        handle = await launcher.start_exec(bot_id, body.argv, body.env, body.stdin, body.exec_id, body.timeout)
        return _stream(launcher.stream_exec(handle), {"X-Exec-Id": handle.exec_id})

    @r.post("/bots/{bot_id}/procedure-step")
    async def procedure_step(bot_id: str, request: Request, body: ProcedureStepRequest):
        return await svc(request).procedure_step(bot_id, body.payload_json, body.dry_run, body.timeout, body.exec_id)

    @r.post("/bots/{bot_id}/procedure-step/cancel")
    async def procedure_step_cancel(bot_id: str, request: Request, body: ProcedureCancelRequest):
        return await svc(request).procedure_step_cancel(bot_id, body.exec_id)

    @r.post("/bots/{bot_id}/screen-sessions", status_code=201)
    async def open_screen(bot_id: str, request: Request, body: ScreenSessionRequest):
        return await svc(request).open_screen_session(bot_id, body.owner_id)

    @r.get("/screen-sessions/{session_id}/output")
    async def screen_output(session_id: str, request: Request):
        launcher = svc(request)
        return _ScreenStreamingResponse(launcher.screen_output(session_id), launcher, session_id)

    @r.post("/screen-sessions/{session_id}/input", status_code=204)
    async def screen_input(session_id: str, request: Request):
        await svc(request).screen_input(session_id, await request.body())
        return Response(status_code=204)

    @r.delete("/screen-sessions/{session_id}", status_code=204)
    async def screen_close(session_id: str, request: Request):
        await svc(request).close_screen_session(session_id)
        return Response(status_code=204)

    @r.post("/execs/{exec_id}/stop")
    async def stop_exec(exec_id: str, request: Request, body: StopRequest | None = Body(default=None)):
        return await svc(request).stop_exec(exec_id, body.bot_id if body else None)

    @r.post("/logins/{owner_id}", status_code=201)
    async def create_login(owner_id: str, request: Request):
        return await svc(request).create_login_container(owner_id)

    @r.delete("/logins/{owner_id}")
    async def remove_login(owner_id: str, request: Request):
        return await svc(request).remove_login_container(owner_id)

    @r.post("/logins/{owner_id}/sessions", status_code=201)
    async def open_session(owner_id: str, request: Request, body: SessionRequest | None = Body(default=None)):
        body = body or SessionRequest()
        return await svc(request).open_login_session(owner_id, body.command, body.cols, body.rows)

    @r.get("/login-sessions/{session_id}/output")
    async def session_output(session_id: str, request: Request):
        launcher = svc(request)
        launcher._session(session_id)  # 404 до начала стрима
        return _stream(launcher.login_output(session_id))

    @r.post("/login-sessions/{session_id}/input", status_code=204)
    async def session_input(session_id: str, request: Request):
        await svc(request).login_input(session_id, await request.body())
        return Response(status_code=204)

    @r.post("/login-sessions/{session_id}/resize", status_code=204)
    async def session_resize(session_id: str, request: Request, body: ResizeRequest):
        await svc(request).login_resize(session_id, body.cols, body.rows)
        return Response(status_code=204)

    @r.delete("/login-sessions/{session_id}", status_code=204)
    async def session_close(session_id: str, request: Request):
        await svc(request).close_login_session(session_id)
        return Response(status_code=204)

    return r


async def _maintenance(launcher: Launcher, interval: float) -> None:
    """Периодически восстанавливает правила iptables (ufw reload, рестарт Docker) и закрывает старые терминалы."""
    while True:
        await asyncio.sleep(interval)
        try:
            await launcher.reconcile()
        except Exception as exc:  # noqa: BLE001
            log.error("не удалось обновить сетевую политику: %s", exc)
        try:
            await launcher.reap_idle()
        except Exception:  # noqa: BLE001
            log.exception("reap_idle")


def create_app(cfg: Config, backend: Backend | None = None, netpolicy: NetPolicy | None = None) -> FastAPI:
    runner = SubprocessRunner()
    launcher = Launcher(cfg, backend or DockerCLIBackend(cfg, runner), netpolicy or NetPolicy(cfg, runner))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await launcher.startup()  # NetPolicyError без прав или без br_netfilter: сервис не стартует
        task = asyncio.create_task(_maintenance(launcher, cfg.reconcile_interval))
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await launcher.shutdown()

    app = FastAPI(title="bothub-launcher", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.launcher = launcher
    app.add_middleware(AuthMiddleware, secret=cfg.secret)
    app.include_router(build_router())

    @app.exception_handler(LauncherError)
    async def launcher_error(_request: Request, exc: LauncherError):
        return JSONResponse(error_body(exc.code, str(exc)), status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, exc: RequestValidationError):
        fields = ", ".join(".".join(str(p) for p in e["loc"][1:]) or str(e["loc"][0]) for e in exc.errors())
        return JSONResponse(error_body("invalid", f"неверный запрос: {fields}"), status_code=400)

    @app.exception_handler(Exception)
    async def unexpected(_request: Request, exc: Exception):
        log.exception("необработанная ошибка", exc_info=exc)
        return JSONResponse(error_body("error", "внутренняя ошибка лаунчера"), status_code=500)

    return app

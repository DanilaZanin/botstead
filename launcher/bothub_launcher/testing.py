"""Фейки для тестов: iptables, Docker backend, процессы exec и pty, сетевая политика. В проде не используются."""
import asyncio
import copy
import itertools
import dataclasses
from collections.abc import Sequence

from . import docker_args as da
from .backend import ContainerInfo, NetworkInfo
from .errors import Conflict, NotManaged
from .netpolicy import NetSpec, PolicyReport
from .procs import CmdResult

MANAGED = {"bothub.managed": "1"}


class FakeIptables:
    """Имитация iptables, ip6tables и их restore: цепочки, -S, -I, -D и атомарная загрузка из restore."""

    BUILTIN = ("INPUT", "FORWARD", "OUTPUT")

    def __init__(self, *, has_docker_user: bool = True, v6_docker_user: bool = True, permission_denied: bool = False,
                 missing_bins: Sequence[str] = (), restore_fails: bool = False):
        self.calls: list[list[str]] = []
        self.inputs: list[str] = []
        self.permission_denied = permission_denied
        self.missing_bins = set(missing_bins)
        self.restore_fails = restore_fails
        self._chains: dict[int, dict[str, list[str]]] = {
            4: {c: [] for c in self.BUILTIN}, 6: {c: [] for c in self.BUILTIN}}
        if has_docker_user:
            self._chains[4]["DOCKER-USER"] = ["-j RETURN"]
        if v6_docker_user:
            self._chains[6]["DOCKER-USER"] = ["-j RETURN"]

    # ---- доступ из тестов ----
    def rules(self, family: int, chain: str) -> list[str]:
        return list(self._chains[family].get(chain, []))

    def insert_rule(self, family: int, chain: str, pos: int, rule: str) -> None:
        self._chains[family][chain].insert(pos - 1, rule)

    def snapshot(self) -> dict:
        return copy.deepcopy(self._chains)

    # ---- «команда» ----
    async def __call__(self, argv, *, input=None, env=None) -> CmdResult:
        argv = list(argv)
        self.calls.append(argv)
        binary = argv[0]
        if binary in self.missing_bins:
            return CmdResult(127, "", f"{binary}: not found")
        family = 6 if binary.startswith("ip6") else 4
        if self.permission_denied:
            return CmdResult(4, "", "iptables v1.8.10 (nf_tables): Could not fetch rule set generation id: "
                                    "Permission denied (you must be root)")
        chains = self._chains[family]
        if binary.endswith("-restore"):
            return self._restore(chains, input or "")
        op = argv[1]
        if op == "-S":
            chain = argv[2]
            if chain not in chains:
                return CmdResult(1, "", f"{binary}: No chain/target/match by that name.")
            head = f"-P {chain} ACCEPT" if chain in self.BUILTIN else f"-N {chain}"
            return CmdResult(0, "\n".join([head, *[f"-A {chain} {r}" for r in chains[chain]]]) + "\n", "")
        if op == "-I":
            chain, pos, rule = argv[2], int(argv[3]), " ".join(argv[4:])
            if chain not in chains:
                return CmdResult(1, "", "No chain/target/match by that name.")
            chains[chain].insert(pos - 1, rule)
            return CmdResult(0, "", "")
        if op == "-D":
            chain, pos = argv[2], int(argv[3])
            if chain not in chains or not 1 <= pos <= len(chains[chain]):
                return CmdResult(1, "", "Index of deletion too big.")
            del chains[chain][pos - 1]
            return CmdResult(0, "", "")
        return CmdResult(2, "", f"fake iptables: неизвестная операция {op}")

    def _restore(self, chains: dict[str, list[str]], text: str) -> CmdResult:
        self.inputs.append(text)
        if self.restore_fails:
            return CmdResult(1, "", "iptables-restore: line 2 failed")
        staged = copy.deepcopy(chains)
        for line in text.splitlines():
            if line.startswith(":"):
                staged.setdefault(line[1:].split()[0], [])
            elif line.startswith("-F "):
                name = line[3:].strip()
                if name not in staged:
                    return CmdResult(1, "", f"iptables-restore: no chain {name}")
                staged[name] = []
            elif line.startswith("-A "):
                name, _, rule = line[3:].partition(" ")
                if name not in staged:
                    return CmdResult(1, "", f"iptables-restore: no chain {name}")
                staged[name].append(rule)
        chains.clear()
        chains.update(staged)  # всё или ничего: как COMMIT
        return CmdResult(0, "", "")


class FakeNetPolicy:
    def __init__(self, *, order: list | None = None, check_error: Exception | None = None,
                 reconcile_error: Exception | None = None):
        self.events: list = []
        self.order = order if order is not None else []
        self.check_error = check_error
        self.reconcile_error = reconcile_error

    async def check(self) -> None:
        self.events.append("check")
        self.order.append("check")
        if self.check_error:
            raise self.check_error

    async def reconcile(self, nets: Sequence[NetSpec]) -> PolicyReport:
        self.events.append(("reconcile", [n.name for n in nets]))
        self.order.append("reconcile")
        if self.reconcile_error:
            raise self.reconcile_error
        return PolicyReport(True, True, len(nets))


class FakeExec:
    """Процесс exec с готовым выводом. hang=True держит его живым, пока не придёт kill/release."""

    def __init__(self, chunks: Sequence[tuple[str, bytes]] = (), code: int = 0, hang: bool = False):
        self.chunks = list(chunks)
        self.code = code
        self.hang = hang
        self.stdin = b""
        self.stdin_closed = False
        self.killed = False
        self._released = asyncio.Event()
        self._exit_code: int | None = None

    async def write_stdin(self, data: bytes) -> None:
        self.stdin += data
        self.stdin_closed = True

    async def output(self):
        for item in self.chunks:
            yield item
        if self.hang:
            await self._released.wait()

    async def wait(self) -> int:
        if self.hang:
            await self._released.wait()
            return self._exit_code if self._exit_code is not None else self.code
        return self.code

    def release(self, code: int) -> None:
        self._exit_code = code
        self._released.set()

    async def kill(self) -> None:
        self.killed = True
        self.release(-9)


class FakeStream:
    """Двунаправленный фейк для RFB-процесса."""

    def __init__(self, chunks: Sequence[bytes] = (b"RFB 003.008\n",), code: int = 0, hang: bool = True):
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        for chunk in chunks:
            self._queue.put_nowait(chunk)
        self.written: list[bytes] = []
        self.killed = False
        self._done = asyncio.Event()
        self._code: int | None = None
        if not hang:
            self.finish(code)

    def feed(self, data: bytes) -> None:
        self._queue.put_nowait(data)

    def finish(self, code: int = 0) -> None:
        if self._code is None:
            self._code = code
            self._queue.put_nowait(b"")
            self._done.set()

    async def read(self) -> bytes:
        return await self._queue.get()

    async def write_stdin(self, data: bytes) -> None:
        if self._done.is_set():
            raise BrokenPipeError("screen stream closed")
        self.written.append(data)

    async def wait(self) -> int:
        await self._done.wait()
        return self._code or 0

    async def kill(self) -> None:
        self.killed = True
        self.finish(-9)


class FakePty:
    def __init__(self, cols: int, rows: int):
        self.size = (cols, rows)
        self.written: list[bytes] = []
        self.killed = False
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._done = asyncio.Event()
        self._code: int | None = None
        self._eof = False

    def feed(self, data: bytes) -> None:
        self._queue.put_nowait(data)

    def finish(self, code: int = 0) -> None:
        if self._code is None:
            self._code = code
            self._queue.put_nowait(b"")
            self._done.set()

    async def read(self) -> bytes:
        if self._eof:
            return b""
        data = await self._queue.get()
        if not data:
            self._eof = True
        return data

    def write(self, data: bytes) -> None:
        self.written.append(data)

    def resize(self, cols: int, rows: int) -> None:
        self.size = (cols, rows)

    async def wait(self) -> int:
        await self._done.wait()
        return self._code or 0

    def kill(self) -> None:
        self.killed = True
        self.finish(-9)


class FakeBackend:
    """Docker в памяти. Журнал операций в `log` (кортежи) и `order` (только имена, общий с FakeNetPolicy)."""

    def __init__(self):
        self.containers: dict[str, ContainerInfo] = {}
        self.networks: dict[str, NetworkInfo] = {}
        self.volumes: dict[str, dict] = {}
        self.log: list[tuple] = []
        self.order: list[str] = []
        self.next_exec: FakeExec | None = None
        self.last_exec: FakeExec | None = None
        self.last_pty: FakePty | None = None
        self.next_screen: FakeStream | None = None
        self.last_screen: FakeStream | None = None
        self.kill_ends_exec = True
        self._execs: dict[str, FakeExec] = {}
        self._ids = itertools.count(1)
        self.browser_running_names: set[str] = set()
        self.browser_modes: dict[str, str] = {}
        self.browser_urls: dict[str, str] = {}
        self.browser_tabs: dict[str, str | None] = {}
        self.merge_status: dict[str, str] = {}
        self.mode_ready_after = 0
        self._mode_checks = 0
        self.remaining_bot_pids: list[int] = []

    def _rec(self, name: str, *args) -> None:
        self.log.append((name, *args))
        self.order.append(name)

    def add_foreign_container(self, name: str) -> None:
        """Контейнер без метки лаунчера (чужой или старый, от bot-create.sh)."""
        self.containers[name] = ContainerInfo(name=name, id=f"foreign{next(self._ids)}", labels={}, running=True,
                                              status="running")

    # ---- тома и сети ----
    async def detect_docker_version(self):
        pass

    async def ensure_volume(self, name, owner_id, kind):
        self._rec("ensure_volume", name, owner_id, kind)
        self.volumes[name] = {"owner": owner_id, "kind": kind}

    async def remove_volume(self, name, owner_id, kind):
        self._rec("remove_volume", name, owner_id, kind)
        self.volumes.pop(name, None)

    async def ensure_network(self, owner_id):
        self._rec("ensure_network", owner_id)
        name = f"bothub-u-{owner_id}"
        current = self.networks.get(name)
        if current is None or current.core_ip is None:  # как docker network connect: ядро снова в сети
            self.networks[name] = NetworkInfo(
                name=name, id=current.id if current else f"net{next(self._ids)}",
                labels={**MANAGED, "bothub.owner_id": owner_id}, bridge=da.bridge_name(owner_id),
                core_ip="172.20.0.2")
        return self.networks[name]

    def detach_core(self, owner_id: str) -> None:
        """Имитация пересоздания ядра: оно выпало из сети пользователя."""
        name = f"bothub-u-{owner_id}"
        self.networks[name] = NetworkInfo(name=name, id=self.networks[name].id, labels=self.networks[name].labels,
                                          bridge=self.networks[name].bridge, core_ip=None)

    async def list_networks(self):
        return list(self.networks.values())

    # ---- контейнеры ----
    async def inspect_container(self, name):
        return self.containers.get(name)

    async def list_containers(self, role=None):
        return [c for c in self.containers.values()
                if c.labels.get("bothub.managed") == "1" and (role is None or c.labels.get("bothub.role") == role)]

    def _add(self, name: str, role: str, owner_id: str, bot_id: str | None) -> str:
        if name in self.containers:
            raise Conflict(f"имя {name} занято")
        labels = {**MANAGED, "bothub.role": role, "bothub.owner_id": owner_id}
        if bot_id:
            labels["bothub.bot_id"] = bot_id
            labels["bothub.browser_isolation"] = "2"
        cid = f"c{next(self._ids):04d}"
        self.containers[name] = ContainerInfo(
            name=name, id=cid, labels=labels, running=True, status="running", image="bothub-bot",
            networks=(f"bothub-u-{owner_id}",), started_at="2026-10-04T10:00:00Z")
        return cid

    async def run_bot(self, bot_id, owner_id):
        self._rec("run_bot", bot_id, owner_id)
        return self._add(f"bot-{bot_id}", "bot", owner_id, bot_id)

    async def run_login(self, owner_id):
        self._rec("run_login", owner_id)
        return self._add(f"login-{owner_id}", "login", owner_id, None)

    async def start_container(self, name):
        info = self.containers[name]
        if info.labels.get("bothub.managed") != "1" or info.labels.get("bothub.role") != "bot":
            raise NotManaged(f"контейнер {name} не принадлежит лаунчеру")
        self._rec("start_container", name)
        self.containers[name] = dataclasses.replace(info, running=True, status="running")

    async def disable_restart(self, name):
        info = self.containers[name]
        if info.labels.get("bothub.managed") != "1" or info.labels.get("bothub.role") != "bot":
            raise NotManaged(f"контейнер {name} не принадлежит лаунчеру")
        self._rec("disable_restart", name)

    async def remove_container(self, name):
        info = self.containers.get(name)
        if info is None:
            return False
        if info.labels.get("bothub.managed") != "1":
            raise NotManaged(f"контейнер {name} без метки лаунчера")
        self._rec("remove_container", name)
        del self.containers[name]
        return True

    # ---- exec и pty ----
    async def spawn_exec(self, container, argv, env, exec_id):
        self._rec("spawn_exec", container, argv, env, exec_id)
        proc = self.next_exec or FakeExec()
        self.next_exec = None
        self.last_exec = proc
        self._execs[exec_id] = proc
        return proc

    async def kill_in_container(self, container, exec_id, signal_name):
        self._rec("kill_in_container", container, exec_id, signal_name)
        proc = self._execs.get(exec_id)
        if proc is not None and self.kill_ends_exec:
            proc.release(-15 if signal_name == "TERM" else -9)

    async def spawn_procedure_step(self, container, exec_id):
        self._rec("spawn_procedure_step", container, exec_id)
        proc = self.next_exec or FakeExec()
        self.next_exec = None
        self.last_exec = proc
        self._execs[exec_id] = proc
        return proc

    async def kill_procedure_step(self, container, exec_id, signal_name):
        self._rec("kill_procedure_step", container, exec_id, signal_name)
        proc = self._execs.get(exec_id)
        if proc is not None and self.kill_ends_exec:
            proc.release(-15 if signal_name == "TERM" else -9)

    async def spawn_pty(self, container, argv, cols, rows):
        self._rec("spawn_pty", container, argv, cols, rows)
        self.last_pty = FakePty(cols, rows)
        return self.last_pty

    async def spawn_screen(self, container):
        self._rec("spawn_screen", container)
        self.last_screen = self.next_screen or FakeStream()
        self.next_screen = None
        return self.last_screen

    async def browser_running(self, container):
        self._rec("browser_running", container)
        return container in self.browser_running_names

    async def start_browser(self, container):
        self._rec("start_browser", container)
        if self.browser_modes.get(container, "bot") == "bot":  # супервизор в human CDP не поднимает
            self.browser_running_names.add(container)

    async def get_browser_mode(self, container):
        self._rec("get_browser_mode", container)
        return self.browser_modes.get(container, "bot")

    async def set_browser_mode(self, container, mode, url):
        """Как супервизор: человек получает Chromium без CDP, бот Chromium с CDP. Старый Chromium остановлен."""
        self._rec("set_browser_mode", container, mode, url)
        previous = self.browser_modes.get(container, "bot")
        self.browser_modes[container] = mode
        self._mode_checks = 0
        if mode == "human":
            self.browser_urls[container] = url or "about:blank"
            self.browser_running_names.discard(container)
        else:
            self.browser_urls.pop(container, None)
            self.browser_running_names.add(container)
            if previous == "human":
                self.merge_status[container] = "merged 2"

    async def browser_mode_ready(self, container, mode):
        self._rec("browser_mode_ready", container, mode)
        self._mode_checks += 1
        return self.browser_modes.get(container, "bot") == mode and self._mode_checks > self.mode_ready_after \
            and (mode == "human" or container in self.browser_running_names)

    async def browser_merge_status(self, container):
        self._rec("browser_merge_status", container)
        return self.merge_status.get(container, "")

    async def browser_tab_url(self, container):
        self._rec("browser_tab_url", container)
        return self.browser_tabs.get(container)

    async def terminate_bot_processes(self, container, signal_name="TERM"):
        self._rec("terminate_bot_processes", container, signal_name)

    async def bot_processes(self, container):
        self._rec("bot_processes", container)
        return self.remaining_bot_pids

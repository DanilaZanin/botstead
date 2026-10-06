"""Сетевая политика ботов: правила iptables в своих цепочках, подключённых к DOCKER-USER и INPUT.

Что делают правила (на каждую сеть пользователя `-i <bridge>`):
  * к ядру только TCP на API-порт; всё остальное к ядру и к другим контейнерам чужих сетей закрыто;
  * наружу разрешено всё, кроме loopback, RFC1918, CGNAT (Tailscale), link-local 169.254 и метаданных облака,
    multicast, IPv6 ULA и link-local; исключения берутся из конфига (AllowRule);
  * в хост (цепочка INPUT) бот попасть не может, кроме ответов на свои соединения и исключений host_allow.

Одна транзакция iptables-restore пересоздаёт обе цепочки целиком, поэтому повторный вызов даёт то же состояние
(идемпотентно), а убранная сеть исчезает из правил сама. Прыжок в цепочки ставится только после загрузки правил и
всегда на первую позицию, чужие правила не трогаются."""
import ipaddress
import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .config import AllowRule, Config
from .errors import NetPolicyError
from .procs import CmdResult

log = logging.getLogger("bothub_launcher.netpolicy")

FORWARD_CHAIN = "BOTHUB-ISO"
INPUT_CHAIN = "BOTHUB-IN"
SYSCTL_BRIDGE_NF = "/proc/sys/net/bridge/bridge-nf-call-iptables"
BRIDGE_RE = re.compile(r"[A-Za-z0-9_-]{1,15}")


@dataclass(frozen=True)
class NetSpec:
    name: str
    bridge: str
    core_ip: str | None = None
    core_ipv6: str | None = None

    def __post_init__(self):
        if not BRIDGE_RE.fullmatch(self.bridge):
            raise ValueError(f"недопустимое имя bridge: {self.bridge!r}")
        if self.core_ip is not None:
            ipaddress.IPv4Address(self.core_ip)  # ValueError при мусоре
        if self.core_ipv6 is not None:
            ipaddress.IPv6Address(self.core_ipv6)


@dataclass(frozen=True)
class PolicyReport:
    ipv4: bool
    ipv6: bool
    networks: int


def _match(rule: AllowRule) -> str:
    out = f" -d {rule.cidr}"
    if rule.proto != "any":
        out += f" -p {rule.proto}"
        if rule.port is not None:
            out += f" --dport {rule.port}"
    return out


def render_restore(cfg: Config, nets: Sequence[NetSpec], family: int) -> str:
    """Текст для `iptables-restore --noflush`: пересоздаёт только BOTHUB-ISO и BOTHUB-IN."""
    blocked = cfg.blocked_v4 if family == 4 else cfg.blocked_v6
    rows = ["*filter", f":{FORWARD_CHAIN} - [0:0]", f":{INPUT_CHAIN} - [0:0]",
            f"-F {FORWARD_CHAIN}", f"-F {INPUT_CHAIN}"]
    for net in sorted(nets, key=lambda n: n.bridge):
        br, fwd, inp = net.bridge, f"-A {FORWARD_CHAIN}", f"-A {INPUT_CHAIN}"
        if family == 4 and net.core_ip:
            core = f"{net.core_ip}/32"
            rows.append(f"{fwd} -i {br} -o {br} -d {core} -p tcp --dport {cfg.api_port} -j ACCEPT")
            rows.append(f"{fwd} -i {br} -o {br} -d {core} -j DROP")
        if family == 6 and net.core_ipv6:
            core = f"{net.core_ipv6}/128"
            rows.append(f"{fwd} -i {br} -o {br} -d {core} -p tcp --dport {cfg.api_port} -j ACCEPT")
            rows.append(f"{fwd} -i {br} -o {br} -d {core} -j DROP")
        core_known = net.core_ip if family == 4 else net.core_ipv6
        rows.append(f"{fwd} -i {br} -o {br} -j {'ACCEPT' if core_known else 'DROP'}")
        for rule in cfg.allow:
            if rule.version == family:
                rows.append(f"{fwd} -i {br}{_match(rule)} -j ACCEPT")
        if family == 4 and cfg.internal_dns:
            for proto in ("udp", "tcp"):
                rows.append(f"{fwd} -i {br} -d {cfg.internal_dns}/32 -p {proto} --dport 53 -j ACCEPT")
        for cidr in blocked:
            rows.append(f"{fwd} -i {br} -d {cidr} -j DROP")
        rows.append(f"{fwd} -o {br} ! -i {br} -m conntrack --ctstate NEW -j DROP")

        rows.append(f"{inp} -i {br} -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT")
        if family == 4 and cfg.internal_dns:
            for proto in ("udp", "tcp"):
                rows.append(f"{inp} -i {br} -d {cfg.internal_dns}/32 -p {proto} --dport 53 -j ACCEPT")
        for rule in cfg.host_allow:
            if rule.version == family:
                rows.append(f"{inp} -i {br}{_match(rule)} -j ACCEPT")
        rows.append(f"{inp} -i {br} -j DROP")
    rows.append("COMMIT")
    return "\n".join(rows) + "\n"


def _read_text(path: str) -> str:
    with open(path) as f:
        return f.read()


class NetPolicy:
    def __init__(self, cfg: Config, runner, read_file: Callable[[str], str] | None = None):
        self.cfg = cfg
        self._run = runner
        self._read = read_file or _read_text

    # ---------- проверка при старте ----------

    async def check(self) -> None:
        """Явная ошибка, если политику поставить нельзя: без неё контейнеры ботов запускать нельзя."""
        try:
            value = self._read(SYSCTL_BRIDGE_NF).strip()
        except OSError as exc:
            raise NetPolicyError(
                f"не читается {SYSCTL_BRIDGE_NF} ({exc.strerror or exc}): загрузите модуль br_netfilter "
                "(modprobe br_netfilter) и запускайте лаунчер в сети хоста (network_mode: host)") from exc
        if value != "1":
            raise NetPolicyError(
                "net.bridge.bridge-nf-call-iptables не равен 1: без br_netfilter правила не видят трафик внутри "
                "bridge. Выполните: modprobe br_netfilter && sysctl -w net.bridge.bridge-nf-call-iptables=1")
        res = await self._run([self.cfg.iptables_bin, "-S", "DOCKER-USER"])
        if res.rc == 0:
            return
        raise NetPolicyError(self._explain(self.cfg.iptables_bin, res))

    @staticmethod
    def _explain(binary: str, res: CmdResult) -> str:
        err = res.err.strip()[:300]
        low = err.lower()
        if res.rc == 127:
            return f"{binary} не найден: установите iptables в образ лаунчера"
        if "permission denied" in low or "not permitted" in low or "must be root" in low or res.rc == 126:
            return (f"{binary}: нет прав на iptables ({err}). Лаунчеру нужны cap_add NET_ADMIN и NET_RAW и сеть "
                    "хоста (network_mode: host), иначе правила попадут не в тот netns")
        if "no chain/target/match" in low:
            return (f"{binary}: цепочки DOCKER-USER нет ({err}). Docker запущен и не отключал iptables "
                    "(daemon.json: iptables=false)?")
        return f"{binary}: {err or f'код {res.rc}'}"

    # ---------- применение ----------

    async def reconcile(self, nets: Sequence[NetSpec]) -> PolicyReport:
        await self._apply(4, nets)
        await self._apply(6, nets)
        return PolicyReport(True, True, len(nets))

    async def _apply(self, family: int, nets: Sequence[NetSpec]) -> None:
        binary = self.cfg.iptables_bin if family == 4 else self.cfg.ip6tables_bin
        restore = f"{binary}-restore"
        res = await self._run([restore, "--noflush"], input=render_restore(self.cfg, nets, family))
        if res.rc != 0:
            raise NetPolicyError(self._explain(restore, res))
        res = await self._run([binary, "-S", "DOCKER-USER"])
        if res.rc == 0:
            forward_parent = "DOCKER-USER"
        elif family == 6:
            forward_parent = "FORWARD"  # Docker без IPv6 цепочку DOCKER-USER для v6 не создаёт
        else:
            raise NetPolicyError(self._explain(binary, res))
        await self._ensure_jump(binary, forward_parent, FORWARD_CHAIN)
        await self._ensure_jump(binary, "INPUT", INPUT_CHAIN)

    async def _ensure_jump(self, binary: str, parent: str, target: str) -> None:
        res = await self._run([binary, "-S", parent])
        if res.rc != 0:
            raise NetPolicyError(self._explain(binary, res))
        prefix = f"-A {parent} "
        rules = [ln[len(prefix):] for ln in res.out.splitlines() if ln.startswith(prefix)]
        ours = f"-j {target}"
        positions = [i for i, r in enumerate(rules) if r == ours]
        if positions[:1] == [0]:
            extras = positions[1:]  # наш прыжок уже первый; лишние копии убираем
        else:
            ins = await self._run([binary, "-I", parent, "1", "-j", target])
            if ins.rc != 0:
                raise NetPolicyError(self._explain(binary, ins))
            extras = [p + 1 for p in positions]  # старые копии сдвинулись на одну позицию
        for pos in sorted(extras, reverse=True):
            dele = await self._run([binary, "-D", parent, str(pos + 1)])
            if dele.rc != 0:
                raise NetPolicyError(self._explain(binary, dele))

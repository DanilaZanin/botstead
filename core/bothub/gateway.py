"""Injectable proxy for model APIs. No database or application wiring lives here."""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import socket
import sys
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .secrets import issue_gateway_token, verify_gateway_token, gateway_token_turn_id


logger = logging.getLogger(__name__)
DEFAULT_ANTHROPIC_BETAS = ("claude-code-20250219",)
MAX_UPSTREAM_USAGE_TOKENS = 1_000_000_000


class UpstreamUsageLimitExceeded(Exception):
    """Raised when an upstream usage count exceeds the safe accounting range."""


@dataclass(frozen=True)
class GatewayProvider:
    id: str
    kind: str
    base_url: str
    api_key: str = field(repr=False)
    owner_active: bool
    allowed_models: list[str]
    allowed_anthropic_betas: list[str] | None = None
    owner_id: str | None = None
    max_turn_seconds: int | None = None
    allow_private: bool = False
    # Приватные IP, одобренные администратором; пустой набор при флаге не одобряет ничего (docs/contracts.md, раздел 11).
    allow_private_ips: tuple[str, ...] = ()


ProviderBinding = GatewayProvider
ProviderLookup = Callable[[str, str], Awaitable[GatewayProvider | None]]
UsageRecorder = Callable[[str, str, str, int, int, int, int, int], Awaitable[None]]
Resolver = Callable[[str, int], Awaitable[list[tuple]]]


async def _resolve_host(host: str, port: int):
    return await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)


class PrivateAddressError(ValueError):
    """The address is valid only with an administrator's private-network approval."""


class ApprovalChangedError(PrivateAddressError):
    """The administrator's flag is set, but the name now resolves to a private IP outside the approved set."""


class UnresolvedHostError(ValueError):
    """The provider hostname has no DNS answer."""


ADDRESS_CHANGED_DETAIL = "адрес изменился, нужно повторное одобрение"

# Разрешаются только с одобрением администратора (флаг провайдера или PROVIDER_PRIVATE_ALLOW).
PRIVATE_NETWORKS = (ipaddress.ip_network("10.0.0.0/8"), ipaddress.ip_network("172.16.0.0/12"),
                    ipaddress.ip_network("192.168.0.0/16"), ipaddress.ip_network("fc00::/7"),
                    ipaddress.ip_network("100.64.0.0/10"))
# Недоступны ни при каком одобрении: NAT64, IPv4-compatible ::/96 (включает :: и ::1), метаданные облаков
# (AWS fd00:ec2::254, Alibaba 100.100.100.200). IPv4-mapped ::ffff:0:0/96 сводится к IPv4 и проверяется по его правилам.
ALWAYS_FORBIDDEN = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("::/96"),
                    ipaddress.ip_network("fd00:ec2::/32"), ipaddress.ip_network("100.100.100.200/32"))

Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def configured_forbidden_networks(value: str | None = None) -> tuple[Network, ...]:
    """PROVIDER_FORBIDDEN_CIDRS (список через запятую). Опечатка не молчит: закрытая настройка не должна пропадать."""
    raw = os.environ.get("PROVIDER_FORBIDDEN_CIDRS", "") if value is None else value
    networks = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            raise ValueError(f"PROVIDER_FORBIDDEN_CIDRS: invalid network {item!r}") from None
    return tuple(networks)


def _read_proc_file(path: str) -> str | None:
    try:
        with open(path, encoding="ascii") as handle:
            return handle.read()
    except (OSError, UnicodeDecodeError):
        return None


def _socket_addresses() -> list[str]:
    """Запасной источник без /proc: адреса, видимые через имя хоста и таблицу маршрутов (пакет не уходит)."""
    found: list[str] = []
    try:
        found += [info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None)]
    except OSError:
        pass
    for family, probe in ((socket.AF_INET, "192.0.2.1"), (socket.AF_INET6, "2001:db8::1")):
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as sock:
                sock.connect((probe, 9))
                found.append(sock.getsockname()[0])
        except OSError:
            pass
    return found


# Подсеть интерфейса уже этого префикса; широкие маршруты (default dev wg0, VPN 0.0.0.0/1 и 128.0.0.0/1) блок не получают:
# иначе под запрет попали бы все публичные адреса.
MIN_OWN_PREFIX_V4 = 8
MIN_OWN_PREFIX_V6 = 16


def _proc_route_networks(text: str) -> list[Network]:
    """/proc/net/route: подсети, достижимые напрямую (без шлюза) у поднятого интерфейса.

    Маршрут по умолчанию и любой префикс короче /8 не берём: это не подсеть интерфейса, а широкий маршрут."""
    networks: list[Network] = []
    for line in text.splitlines()[1:]:
        fields = line.split()
        try:
            destination, gateway, flags, mask = (int(fields[i], 16) for i in (1, 2, 3, 7))
            if not flags & 1 or gateway != 0:
                continue
            address = ipaddress.IPv4Address(destination.to_bytes(4, sys.byteorder))
            netmask = ipaddress.IPv4Address(mask.to_bytes(4, sys.byteorder))
            network = ipaddress.IPv4Network((address, str(netmask)), strict=False)
            if network.prefixlen >= MIN_OWN_PREFIX_V4:
                networks.append(network)
        except (ValueError, IndexError, OverflowError):
            continue
    return networks


def _proc_inet6_networks(text: str) -> list[Network]:
    networks: list[Network] = []
    for line in text.splitlines():
        fields = line.split()
        try:
            address = ipaddress.IPv6Address(bytes.fromhex(fields[0]))
            network = ipaddress.IPv6Network((address, int(fields[2], 16)), strict=False)
            if network.prefixlen >= MIN_OWN_PREFIX_V6:
                networks.append(network)
        except (ValueError, IndexError):
            continue
    return networks


def own_networks(read: Callable[[str], str | None] = _read_proc_file,
                 addresses: Callable[[], Iterable[str]] = _socket_addresses) -> tuple[Network, ...]:
    """Подсети собственных сетевых интерфейсов ядра (сети Docker с Postgres и ботами): всегда запрещены как цель провайдера.

    Основной источник: /proc/net/route и /proc/net/if_inet6 с настоящими масками. Нет /proc: адреса из `addresses`
    как одиночные хосты. Публичные подсети не берём: сосед по публичному префиксу не внутренняя служба.
    `read` и `addresses` подменяются в тестах."""
    networks: list[Network] = []
    route_text, inet6_text = read("/proc/net/route"), read("/proc/net/if_inet6")
    if route_text is not None:
        networks += _proc_route_networks(route_text)
    if inet6_text is not None:
        networks += _proc_inet6_networks(inet6_text)
    if route_text is None and inet6_text is None:
        try:
            for item in addresses():
                try:
                    networks.append(ipaddress.ip_network(str(item).split("%")[0]))
                except ValueError:
                    continue
        except OSError:
            pass
    return tuple(dict.fromkeys(net for net in networks if not net.network_address.is_global))


OWN_NETWORKS_TTL = 30  # секунд: лаунчер подключает ядро к сетям ботов (bothub-u-*) уже после запуска


class NetworksCache:
    """Подсети собственных интерфейсов ядра с коротким TTL: вызывается при каждой проверке адреса.

    source, ttl и clock подменяются в тестах. Сбой источника оставляет последнее удачное значение."""

    def __init__(self, source: Callable[[], Iterable[Network]] = lambda: own_networks(), ttl: float = OWN_NETWORKS_TTL,
                 clock: Callable[[], float] = time.monotonic):
        self.source, self.ttl, self.clock = source, ttl, clock
        self.value: tuple[Network, ...] = ()
        self.expires: float | None = None

    def reset(self) -> None:
        self.expires = None

    def __call__(self) -> tuple[Network, ...]:
        now = self.clock()
        if self.expires is None or now >= self.expires:
            try:
                self.value = tuple(self.source())
            except Exception:
                logger.exception("own networks refresh failed")
            self.expires = now + self.ttl
        return self.value


@dataclass(frozen=True)
class Target:
    origin: str
    pinned: str
    hostname: str
    addresses: tuple[str, ...]  # все ответы DNS в канонической форме (IPv4-mapped сведён к IPv4)
    private: tuple[str, ...]    # из них приватные: то, что одобряет администратор
    exempt: bool                # хост в устаревшем PROVIDER_PRIVATE_ALLOW


ForbiddenNetworks = Sequence[Network] | Callable[[], Sequence[Network]]


async def inspect_target(base_url: str, allowed_private_hosts: Sequence[str], resolver: Resolver,
                         forbidden_networks: ForbiddenNetworks = ()) -> Target:
    """Everything except the administrator's approval: syntax, DNS, forbidden ranges, HTTP rule. One DNS question.

    forbidden_networks may be a callable: it is read once per check, so subnets that appear later are covered."""
    try:
        parsed = urlsplit(base_url)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("malformed provider URL") from exc
    if (parsed.scheme not in {"https", "http"} or not host or parsed.username is not None
            or parsed.password is not None or parsed.fragment or parsed.query
            or host.endswith(".") or "%" in host or "\\" in base_url
            or any(c.isspace() for c in base_url)):
        raise ValueError("provider URL must be an origin without credentials, query or fragment")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("invalid provider port")
    if (not re.fullmatch(r"[A-Za-z0-9.\-]+", host) and ":" not in host) or ".." in host or host.startswith("-"):
        raise ValueError("invalid provider hostname")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    try:
        if literal is not None:
            answers = [(None, None, None, None, (str(literal), port or 443))]
        else:
            answers = await resolver(host, port or (443 if parsed.scheme == "https" else 80))
    except (socket.gaierror, OSError) as exc:
        raise UnresolvedHostError("provider hostname did not resolve") from exc
    if not answers:
        raise UnresolvedHostError("provider hostname did not resolve")
    forbidden = (*ALWAYS_FORBIDDEN, *(forbidden_networks() if callable(forbidden_networks) else forbidden_networks))
    addresses = []
    for answer in answers:
        try:
            address = ipaddress.ip_address(answer[4][0])
        except ValueError as exc:
            raise ValueError("invalid DNS answer") from exc
        if address.version == 6 and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        private = any(address in network for network in PRIVATE_NETWORKS)
        if (address.is_multicast or address.is_loopback or address.is_link_local or address.is_unspecified
                or any(address in network for network in forbidden)
                or (not address.is_global and not private)):
            raise ValueError("provider hostname resolves to a non-public address")
        if parsed.scheme == "http" and not private:
            raise ValueError("HTTP is allowed only for approved private network addresses")
        addresses.append(address)
    address = addresses[0]
    pinned_host = f"[{address}]" if address.version == 6 else str(address)
    suffix = f":{port}" if port is not None else ""
    origin = urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))
    pinned = urlunsplit((parsed.scheme, pinned_host + suffix, parsed.path.rstrip("/"), "", ""))
    private_ips = tuple(dict.fromkeys(str(a) for a in addresses if any(a in network for network in PRIVATE_NETWORKS)))
    return Target(origin, pinned, host, tuple(dict.fromkeys(str(a) for a in addresses)), private_ips,
                  host.lower() in {item.lower() for item in allowed_private_hosts})


def unapproved_addresses(target: Target, allow_private: bool, approved_ips: Iterable[str] | None) -> tuple[str, ...]:
    """Private IPs of the target that nobody approved. approved_ips=None with the flag: no limit (the approval step itself)."""
    if target.exempt:
        return ()
    if not allow_private:
        return target.private
    if approved_ips is None:
        return ()
    approved = {str(ipaddress.ip_address(item)) for item in approved_ips}
    return tuple(ip for ip in target.private if ip not in approved)


def require_approval(target: Target, allow_private: bool, approved_ips: Iterable[str] | None) -> None:
    if unapproved_addresses(target, allow_private, approved_ips):
        if allow_private:
            raise ApprovalChangedError(ADDRESS_CHANGED_DETAIL)
        raise PrivateAddressError("provider address requires administrator approval")


async def _validated_target(base_url: str, allowed_private_hosts: Sequence[str], resolver: Resolver,
                            allow_private: bool = False, approved_ips: Iterable[str] | None = None,
                            forbidden_networks: ForbiddenNetworks = ()) -> tuple[str, str, str]:
    target = await inspect_target(base_url, allowed_private_hosts, resolver, forbidden_networks)
    require_approval(target, allow_private, approved_ips)
    return target.origin, target.pinned, target.hostname


async def validate_base_url(base_url: str, *, allowed_private_hosts: Sequence[str] = (),
                            resolver: Resolver = _resolve_host, allow_private: bool = False,
                            approved_ips: Iterable[str] | None = None,
                            forbidden_networks: ForbiddenNetworks = ()) -> str:
    """Validate every DNS answer without blocking the event loop.

    Raises PrivateAddressError when only the administrator's approval (allow_private or the PROVIDER_PRIVATE_ALLOW host
    list) is missing, ApprovalChangedError (its subclass) when the flag is set but a private IP is outside approved_ips;
    any other ValueError is a refusal approval cannot lift. forbidden_networks are refused in every case."""
    return (await _validated_target(base_url, allowed_private_hosts, resolver, allow_private, approved_ips,
                                    forbidden_networks))[0]


def _allowed(kind: str, method: str, path: str) -> bool:
    if kind == "anthropic_api":
        return method == "POST" and path in {"v1/messages", "v1/messages/count_tokens"}
    if kind in {"openai_api", "openai_compatible"}:
        return (method == "POST" and path in {"v1/chat/completions", "v1/responses", "v1/embeddings"}) or (method == "GET" and path == "v1/models")
    if kind == "google_api":
        return method == "POST" and bool(re.fullmatch(r"v1beta/models/[A-Za-z0-9._-]+:(?:generateContent|streamGenerateContent|countTokens)", path))
    return False


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(value: str):
    raise ValueError("non-JSON numeric constant")


def _parse_body(body: bytes) -> dict:
    if body.startswith((b"\xef\xbb\xbf", b"\x1f\x8b")) or b"\x00" in body:
        raise HTTPException(403, "model_disabled")
    try:
        body.decode("utf-8")
    except UnicodeError:
        raise HTTPException(403, "model_disabled") from None
    depth = 0
    in_string = escaped = False
    for char in body:
        if in_string:
            if escaped:
                escaped = False
            elif char == 92:  # Backslash
                escaped = True
            elif char == 34:  # Quote
                in_string = False
        elif char == 34:
            in_string = True
        elif char in (91, 123):  # [ or {
            depth += 1
            if depth > 100:
                raise HTTPException(403, "model_disabled")
        elif char in (93, 125):  # ] or }
            depth -= 1
    try:
        parsed = json.loads(body, object_pairs_hook=_strict_object, parse_constant=_invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise HTTPException(403, "model_disabled") from None
    if not isinstance(parsed, dict):
        raise HTTPException(403, "model_disabled")
    if any(key.lower() == "model" and key != "model" for key in parsed):
        raise HTTPException(403, "model_disabled")
    if "stream" in parsed and type(parsed["stream"]) is not bool:
        raise HTTPException(400, "stream must be boolean")
    return parsed


def _model_from_body(body: bytes) -> tuple[str, dict]:
    parsed = _parse_body(body)
    if not isinstance(parsed.get("model"), str):
        raise HTTPException(403, "model_disabled")
    return parsed["model"], parsed


class GatewayRouter(APIRouter):
    client: httpx.AsyncClient | None = None

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None, token_secret: str, token_ttl: int):
        self._transport = transport
        self._token_secret = token_secret
        self.token_ttl = token_ttl
        @asynccontextmanager
        async def lifespan(_app):
            await self.startup()
            try:
                yield
            finally:
                await self.shutdown()
        super().__init__(prefix="/gateway", lifespan=lifespan)

    async def startup(self):
        if self.client is None:
            self.client = httpx.AsyncClient(follow_redirects=False, trust_env=False, timeout=120,
                                             limits=httpx.Limits(max_connections=None, max_keepalive_connections=0),
                                             transport=self._transport)

    async def shutdown(self):
        if self.client is not None:
            await self.client.aclose()
            self.client = None

    def issue_token(self, bot_id: str, provider_id: str, *, max_turn_seconds: int | None = None,
                    turn_id: str | None = None) -> str:
        if max_turn_seconds is not None and (type(max_turn_seconds) is not int or max_turn_seconds <= 0):
            raise ValueError("max_turn_seconds must be a positive integer")
        ttl = self.token_ttl if max_turn_seconds is None else max_turn_seconds + 120
        return issue_gateway_token(bot_id, provider_id, self._token_secret, ttl=ttl, turn_id=turn_id)


def _usage_from_object(obj: object) -> tuple[str, int, int, int, int] | None:
    if not isinstance(obj, dict):
        return None
    model = str(obj.get("model") or obj.get("modelVersion") or "")
    usage = obj.get("usage") or obj.get("usageMetadata")
    if obj.get("type") == "message_start":
        message = obj.get("message") or {}
        if not isinstance(message, dict):
            return None
        model = str(message.get("model") or model)
        usage = message.get("usage")
    elif obj.get("type") == "message_delta":
        usage = obj.get("usage")
    elif obj.get("type") in {"response.completed", "response.incomplete"}:
        response = obj.get("response") or {}
        if not isinstance(response, dict):
            return None
        model = str(response.get("model") or model)
        usage = response.get("usage")
    if not isinstance(usage, dict):
        return None

    def validate_token_counts(value: object):
        if isinstance(value, dict):
            for key, item in value.items():
                if (isinstance(key, str) and "token" in key.casefold()
                        and "persecond" not in key.casefold().replace("_", "")
                        and type(item) is int and item > MAX_UPSTREAM_USAGE_TOKENS):
                    raise UpstreamUsageLimitExceeded
                validate_token_counts(item)
        elif isinstance(value, list):
            for item in value:
                validate_token_counts(item)

    validate_token_counts(usage)

    def count(*keys: str) -> int:
        for key in keys:
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return value
        return 0
    details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
    if not isinstance(details, dict):
        details = {}
    cached = details.get("cached_tokens")
    cache_read = count("cache_read_input_tokens", "cachedContentTokenCount")
    if not cache_read and isinstance(cached, int) and not isinstance(cached, bool) and cached >= 0:
        cache_read = cached
    return (model, count("input_tokens", "prompt_tokens", "promptTokenCount"),
            count("output_tokens", "completion_tokens", "candidatesTokenCount"),
            cache_read, count("cache_creation_input_tokens"))


def create_gateway_router(
    provider_lookup: ProviderLookup,
    on_usage: UsageRecorder,
    token_secret: str,
    *,
    allowed_private_hosts: Sequence[str] = (),
    transport: httpx.AsyncBaseTransport | None = None,
    resolver: Resolver = _resolve_host,
    max_turn_seconds: int = 1800,
    max_parallel_per_bot: int = 4,
    max_connections_per_user: int = 4,
    max_upstream_response_bytes: int = 32 * 1024 * 1024,
    token_ttl: int = 1920,
    default_anthropic_betas: Sequence[str] = DEFAULT_ANTHROPIC_BETAS,
    turn_authorize: Callable[[str, str, str], Awaitable[bool]] | None = None,
    forbidden_networks: ForbiddenNetworks = (),
    on_address_changed: Callable[[GatewayProvider], Awaitable[None]] | None = None,
) -> GatewayRouter:
    """Build a scoped provider proxy with a bounded request lifetime."""
    if (not token_secret or max_turn_seconds <= 0 or max_parallel_per_bot <= 0
            or max_connections_per_user <= 0 or type(max_upstream_response_bytes) is not int
            or max_upstream_response_bytes <= 0 or token_ttl <= 0):
        raise ValueError("gateway secret and positive resource limits are required")
    if any(type(beta) is not str for beta in default_anthropic_betas):
        raise ValueError("default Anthropic betas must be strings")
    router = GatewayRouter(transport=transport, token_secret=token_secret, token_ttl=token_ttl)
    semaphores: dict[str, asyncio.Semaphore] = {}
    owner_semaphores: dict[str, asyncio.Semaphore] = {}
    router.pre_route_counts = {401: 0, 404: 0, 405: 0, 429: 0}
    last_pre_route_log = time.monotonic()

    def pre_route(status: int, detail: str):
        nonlocal last_pre_route_log
        router.pre_route_counts[status] += 1
        now = time.monotonic()
        if now - last_pre_route_log >= 60:
            logger.info("gateway pre-route rejects: %s", router.pre_route_counts)
            last_pre_route_log = now
        raise HTTPException(status, detail)

    async def record(*args, turn_id=None):
        task = asyncio.create_task(on_usage(*args, turn_id) if turn_authorize else on_usage(*args))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    @router.api_route("/{provider_id}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE"],
                       include_in_schema=False)
    async def proxy(provider_id: str, path: str, request: Request):
        authorization = request.headers.get("authorization", "")
        if authorization and not authorization.startswith("Bearer "):
            pre_route(401, "invalid gateway authorization")
        supplied = [value for value in (authorization[7:] if authorization else "",
                     request.headers.get("x-api-key", ""), request.headers.get("x-goog-api-key", "")) if value]
        if not supplied or any(value != supplied[0] for value in supplied[1:]):
            pre_route(401, "one gateway token is required")
        bot_id = verify_gateway_token(supplied[0], provider_id, token_secret)
        if bot_id is None:
            pre_route(401, "invalid gateway token")
        turn_id = gateway_token_turn_id(supplied[0])
        if turn_authorize and (not turn_id or not await turn_authorize(bot_id,provider_id,turn_id)):
            pre_route(401, 'turn is not active')
        provider = await provider_lookup(bot_id, provider_id)
        if provider is None or provider.id != provider_id or provider.owner_active is not True:
            raise HTTPException(403, "provider unavailable to bot")
        if request.method not in {"GET", "POST"}:
            pre_route(405, "unsupported gateway method")
        turn_seconds = provider.max_turn_seconds if provider.max_turn_seconds is not None else max_turn_seconds
        if type(turn_seconds) is not int or turn_seconds <= 0:
            raise HTTPException(403, "invalid bot deadline")
        if provider.kind == "google_api" and path.startswith("v1beta/models/") and not _allowed(provider.kind, request.method, path):
            raise HTTPException(403, "model_disabled")
        if not _allowed(provider.kind, request.method, path):
            pre_route(404, "unsupported gateway route")
        status = 500
        model = ""
        tokens_in = tokens_out = cache_read = cache_write = 0
        transferred = False
        semaphore = semaphores.setdefault(bot_id, asyncio.Semaphore(max_parallel_per_bot))
        owner_semaphore = owner_semaphores.setdefault(provider.owner_id or bot_id, asyncio.Semaphore(max_connections_per_user))
        acquired = False
        owner_acquired = False
        upstream_reached = False
        upstream = None
        started = time.monotonic()
        try:
            try:
                await asyncio.wait_for(semaphore.acquire(), timeout=0.01)
                acquired = True
            except TimeoutError:
                pre_route(429, "gateway concurrency limit")
            try:
                await asyncio.wait_for(owner_semaphore.acquire(), timeout=0.01)
                owner_acquired = True
            except TimeoutError:
                pre_route(429, "gateway connection limit")
            if type(provider.allowed_models) is not list or any(type(name) is not str for name in provider.allowed_models):
                raise HTTPException(403, "model_disabled")
            if provider.kind in {"openai_api", "openai_compatible"} and path == "v1/models":
                status = 200
                return JSONResponse({"object": "list", "data": [{"id": name, "object": "model"} for name in provider.allowed_models]})
            if provider.kind == "google_api":
                if "%" in path:
                    raise HTTPException(403, "model_disabled")
                match = re.fullmatch(r"v1beta/models/([A-Za-z0-9._-]+):(?:generateContent|streamGenerateContent|countTokens)", path)
                model = match.group(1) if match else ""
                parsed = None
            else:
                body = await _read_body(request, turn_seconds - (time.monotonic() - started))
                if request.headers.get("content-encoding", "identity").lower() != "identity":
                    raise HTTPException(403, "model_disabled")
                model, parsed = _model_from_body(body)
            if model not in provider.allowed_models:
                raise HTTPException(403, "model_disabled")
            allowed_betas = default_anthropic_betas if provider.allowed_anthropic_betas is None else provider.allowed_anthropic_betas
            if type(allowed_betas) not in {list, tuple} or any(type(item) is not str for item in allowed_betas):
                allowed_betas = ()
            if not provider.api_key:
                raise HTTPException(503, "provider key unavailable")
            try:
                remaining = turn_seconds - (time.monotonic() - started)
                _, pinned, hostname = await asyncio.wait_for(
                    _validated_target(provider.base_url, allowed_private_hosts, resolver, provider.allow_private,
                                      provider.allow_private_ips, forbidden_networks),
                    timeout=max(0, remaining))
            except TimeoutError:
                raise HTTPException(504, "gateway deadline exceeded") from None
            except ApprovalChangedError:
                if on_address_changed is not None:
                    try:
                        await on_address_changed(provider)
                    except Exception:
                        logger.exception("provider address change report failed")
                raise HTTPException(502, "invalid provider address") from None
            except ValueError:
                raise HTTPException(502, "invalid provider address") from None
            query = [(key, value) for key, value in parse_qsl(request.url.query, keep_blank_values=True)
                     if key.lower() not in {"key", "api_key", "access_token"}]
            if provider.kind == "google_api" and path.endswith(":streamGenerateContent"):
                query = [(key, value) for key, value in query if key.lower() != "alt"] + [("alt", "sse")]
            url = f"{pinned}/{path}" + ("?" + urlencode(query) if query else "")
            headers = {key: request.headers[key] for key in ("content-type", "accept", "anthropic-version", "anthropic-beta") if key in request.headers}
            headers.pop("anthropic-beta", None)
            if provider.kind == "anthropic_api" and "anthropic-beta" in request.headers:
                forwarded_betas = [item.strip() for item in request.headers["anthropic-beta"].split(",")
                                   if item.strip() in allowed_betas]
                if forwarded_betas:
                    headers["anthropic-beta"] = ",".join(forwarded_betas)
            headers["host"] = urlsplit(provider.base_url).netloc
            headers["connection"] = "close"
            if provider.kind == "anthropic_api":
                headers["x-api-key"] = provider.api_key
                headers.setdefault("anthropic-version", "2023-06-01")
            elif provider.kind == "google_api":
                headers["x-goog-api-key"] = provider.api_key
            else:
                headers["authorization"] = f"Bearer {provider.api_key}"
            if provider.kind == "google_api":
                body = await _read_body(request, turn_seconds - (time.monotonic() - started))
                if request.headers.get("content-encoding", "identity").lower() != "identity":
                    raise HTTPException(403, "model_disabled")
                parsed = _parse_body(body)
                if "model" in parsed and parsed["model"] != model:
                    raise HTTPException(403, "model_disabled")
                body = json.dumps(parsed, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
            elif path == "v1/chat/completions" and parsed.get("stream") is True:
                options = parsed.get("stream_options")
                if not isinstance(options, dict):
                    options = {}
                options["include_usage"] = True
                parsed["stream_options"] = options
                body = json.dumps(parsed, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
            elif parsed is not None:
                body = json.dumps(parsed, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
            remaining = turn_seconds - (time.monotonic() - started)
            if remaining <= 0:
                raise HTTPException(504, "gateway deadline exceeded")
            if router.client is None:
                raise HTTPException(503, "gateway not started")
            upstream_request = router.client.build_request(request.method, url, headers=headers, content=body)
            upstream_request.extensions["sni_hostname"] = hostname
            try:
                upstream = await asyncio.wait_for(router.client.send(upstream_request, stream=True), timeout=remaining)
                upstream_reached = True
            except TimeoutError:
                raise HTTPException(504, "gateway deadline exceeded") from None
            except httpx.HTTPError:
                raise HTTPException(502, "upstream connection failed") from None
            if 300 <= upstream.status_code < 400:
                raise HTTPException(502, "upstream redirect refused")
            status = upstream.status_code
            content_type = upstream.headers.get("content-type", "application/octet-stream")
            key_bytes = provider.api_key.encode()
            response_headers = {"content-type": content_type.replace(provider.api_key, "***")}
            if "retry-after" in upstream.headers:
                response_headers["retry-after"] = upstream.headers["retry-after"].replace(provider.api_key, "***")
            if status >= 400:
                try:
                    raw_error = bytearray()
                    async with asyncio.timeout(max(0, turn_seconds - (time.monotonic() - started))):
                        async for chunk in upstream.aiter_bytes():
                            raw_error.extend(chunk[:max(0, 65536 - len(raw_error))])
                            if len(raw_error) >= 65536:
                                break
                except Exception:
                    raw_error = b""
                try:
                    error = json.loads(raw_error).get("error", {})
                except (ValueError, UnicodeError, AttributeError):
                    error = {}
                if not isinstance(error, dict):
                    error = {}
                safe = {key: str(error[key]).replace(provider.api_key, "***").encode("utf-8")[:2048].decode("utf-8", "ignore")
                        for key in ("type", "code", "message")
                        if type(error.get(key)) in {str, int, float}}
                return JSONResponse({"error": safe}, status_code=status)
            if content_type.lower().startswith("text/event-stream"):
                async def events():
                    nonlocal tokens_in, tokens_out, cache_read, cache_write, status, transferred, acquired, owner_acquired
                    pending = b""
                    redact_pending = b""
                    try:
                        async with asyncio.timeout(max(0, turn_seconds - (time.monotonic() - started))):
                            async for chunk in upstream.aiter_bytes():
                                pending = (pending + chunk).replace(b"\r\n", b"\n")
                                while b"\n\n" in pending:
                                    event, pending = pending.split(b"\n\n", 1)
                                    if len(event) > 10 * 1024 * 1024:
                                        raise ValueError("upstream SSE event too large")
                                    for line in event.split(b"\n"):
                                        if not line.startswith(b"data:"):
                                            continue
                                        try:
                                            found = _usage_from_object(json.loads(line[5:].strip()))
                                        except (ValueError, UnicodeError, RecursionError):
                                            found = None
                                        if found:
                                            tokens_in = max(tokens_in, found[1])
                                            tokens_out = max(tokens_out, found[2])
                                            cache_read = max(cache_read, found[3])
                                            cache_write = max(cache_write, found[4])
                                if len(pending) > 10 * 1024 * 1024:
                                    status = 502
                                    raise ValueError("upstream SSE event too large")
                                redact_pending = (redact_pending + chunk).replace(key_bytes, b"***")
                                safe = max(0, len(redact_pending) - len(key_bytes) + 1)
                                if safe:
                                    yield redact_pending[:safe]
                                    redact_pending = redact_pending[safe:]
                            if redact_pending:
                                yield redact_pending
                    except TimeoutError:
                        status = 504
                    except UpstreamUsageLimitExceeded:
                        status = 502
                    except httpx.HTTPError:
                        status = 502
                    except Exception:
                        status = 502
                    except asyncio.CancelledError:
                        status = 499
                        raise
                    finally:
                        try:
                            await upstream.aclose()
                        except Exception:
                            status = 502
                        finally:
                            if acquired:
                                semaphore.release()
                                acquired = False
                            if owner_acquired:
                                owner_semaphore.release()
                                owner_acquired = False
                            await record(bot_id, provider_id, model if provider.api_key not in model else "",
                                         tokens_in, tokens_out, cache_read, cache_write, status, turn_id=turn_id)
                transferred = True
                return StreamingResponse(events(), status_code=status, headers=response_headers)
            try:
                remaining = turn_seconds - (time.monotonic() - started)
                content = bytearray()
                async with asyncio.timeout(max(0, remaining)):
                    async for chunk in upstream.aiter_bytes():
                        if len(content) + len(chunk) > max_upstream_response_bytes:
                            raise HTTPException(502, "upstream response too large")
                        content.extend(chunk)
            except TimeoutError:
                raise HTTPException(504, "gateway deadline exceeded") from None
            except HTTPException:
                raise
            except Exception:
                raise HTTPException(502, "upstream read failed") from None
            try:
                found = _usage_from_object(json.loads(content))
            except UpstreamUsageLimitExceeded:
                raise HTTPException(502, "upstream usage exceeds limit") from None
            except (ValueError, UnicodeError, RecursionError):
                found = None
            if found:
                tokens_in, tokens_out, cache_read, cache_write = found[1:]
            return Response(bytes(content).replace(key_bytes, b"***"), status_code=status, headers=response_headers)
        except HTTPException as exc:
            status = exc.status_code
            raise
        except asyncio.CancelledError:
            status = 499
            raise
        finally:
            if not transferred:
                try:
                    if upstream is not None:
                        await upstream.aclose()
                except Exception:
                    pass
                finally:
                    if acquired:
                        semaphore.release()
                    if owner_acquired:
                        owner_semaphore.release()
                    if upstream_reached:
                        await record(bot_id, provider_id, model if provider.api_key not in model else "",
                                     tokens_in, tokens_out, cache_read, cache_write, status, turn_id=turn_id)

    return router


async def _read_body(request: Request, remaining: float) -> bytes:
    body = bytearray()
    try:
        async with asyncio.timeout(max(0, remaining)):
            async for chunk in request.stream():
                if len(body) + len(chunk) > 10 * 1024 * 1024:
                    raise HTTPException(413, "request body too large")
                body.extend(chunk)
    except TimeoutError:
        raise HTTPException(504, "gateway deadline exceeded") from None
    return bytes(body)

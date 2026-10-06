"""Approval of a private address is bound to a set of IPs; the core's own subnets are never reachable.

Gateway-level tests: no database, no network. Route behaviour is in test_provider_review_pure.py."""
import ipaddress
import sys

import pytest

from bothub.gateway import (ALWAYS_FORBIDDEN, ApprovalChangedError, GatewayProvider, PrivateAddressError,
                            configured_forbidden_networks, own_networks, validate_base_url)
from test_provider_private_pure import call_gateway, provider_at, resolving

pytestmark = pytest.mark.pure

DOCKER = ipaddress.ip_network("172.18.0.0/16")


# ---- одобрение привязано к набору IP (DNS rebinding после одобрения) --------------------------------------------

async def test_flag_lets_through_only_the_approved_ips():
    base = "https://llm.lan"
    assert await validate_base_url(base, resolver=resolving("192.168.1.20"), allow_private=True,
                                   approved_ips=["192.168.1.20"]) == base
    for moved in ("172.18.0.2", "192.168.1.21", "10.0.0.5", "100.64.0.9", "fd12::1"):
        with pytest.raises(ApprovalChangedError) as caught:
            await validate_base_url(base, resolver=resolving(moved), allow_private=True, approved_ips=["192.168.1.20"])
        assert isinstance(caught.value, PrivateAddressError)  # старые обработчики видят «нужно одобрение»
        assert "повторное одобрение" in str(caught.value) and moved not in str(caught.value)


async def test_empty_approved_set_approves_nothing():
    with pytest.raises(ApprovalChangedError):  # строки, у которых флаг стоял до миграции 016
        await validate_base_url("https://llm.lan", resolver=resolving("192.168.1.20"), allow_private=True,
                                approved_ips=[])


async def test_without_a_set_the_flag_alone_keeps_its_old_meaning_for_the_approval_step():
    assert await validate_base_url("https://llm.lan", resolver=resolving("172.20.1.1"),
                                   allow_private=True) == "https://llm.lan"


async def test_every_private_answer_must_be_approved():
    resolver = resolving("192.168.1.20", "192.168.1.21")
    assert await validate_base_url("https://llm.lan", resolver=resolver, allow_private=True,
                                   approved_ips=["192.168.1.21", "192.168.1.20"]) == "https://llm.lan"
    with pytest.raises(ApprovalChangedError):
        await validate_base_url("https://llm.lan", resolver=resolver, allow_private=True, approved_ips=["192.168.1.20"])


async def test_set_is_compared_in_canonical_form_and_ignores_public_answers():
    assert await validate_base_url("https://llm.lan", resolver=resolving("::ffff:10.0.0.5"), allow_private=True,
                                   approved_ips=["10.0.0.5"]) == "https://llm.lan"
    assert await validate_base_url("https://llm.lan", resolver=resolving("FD12:0:0:0:0:0:0:1"), allow_private=True,
                                   approved_ips=["fd12::1"]) == "https://llm.lan"
    assert await validate_base_url("https://api.example", resolver=resolving("8.8.8.8", "10.0.0.5"), allow_private=True,
                                   approved_ips=["10.0.0.5"]) == "https://api.example"
    assert await validate_base_url("https://api.example", resolver=resolving("8.8.8.8"), allow_private=True,
                                   approved_ips=[]) == "https://api.example"


async def test_set_without_the_flag_does_not_approve_anything():
    with pytest.raises(PrivateAddressError) as caught:
        await validate_base_url("https://llm.lan", resolver=resolving("192.168.1.20"), approved_ips=["192.168.1.20"])
    assert not isinstance(caught.value, ApprovalChangedError)


async def test_legacy_host_list_is_not_bound_to_a_set():
    assert await validate_base_url("http://llm.lan", allowed_private_hosts=["llm.lan"], resolver=resolving("10.9.9.9"),
                                   approved_ips=[]) == "http://llm.lan"


@pytest.mark.parametrize("kwargs", [{"allow_private": True}, {"allow_private": True, "approved_ips": ["172.18.0.2"]},
                                    {"allowed_private_hosts": ["llm.lan"]}], ids=["flag", "flag+set", "legacy-host"])
async def test_own_subnets_are_refused_whatever_was_approved(kwargs):
    for answer in ("172.18.0.2", "::ffff:172.18.7.7"):
        with pytest.raises(ValueError) as caught:
            await validate_base_url("https://llm.lan", resolver=resolving(answer), forbidden_networks=[DOCKER], **kwargs)
        assert not isinstance(caught.value, PrivateAddressError)
    outside = dict(kwargs, approved_ips=["172.19.0.2"]) if "allow_private" in kwargs else kwargs
    assert await validate_base_url("https://llm.lan", resolver=resolving("172.19.0.2"), forbidden_networks=[DOCKER],
                                   **outside) == "https://llm.lan"


async def test_forbidden_networks_may_be_a_callable_read_at_every_check():
    current = []
    kwargs = dict(resolver=resolving("172.18.0.2"), allow_private=True, forbidden_networks=lambda: current)
    assert await validate_base_url("https://llm.lan", **kwargs) == "https://llm.lan"
    current.append(DOCKER)  # сеть появилась после первой проверки
    with pytest.raises(ValueError):
        await validate_base_url("https://llm.lan", **kwargs)


async def test_forbidden_networks_also_apply_to_literal_ips_and_public_names():
    with pytest.raises(ValueError):
        await validate_base_url("https://172.18.0.2", allow_private=True, forbidden_networks=[DOCKER])
    with pytest.raises(ValueError):
        await validate_base_url("https://api.example", resolver=resolving("8.8.8.8"),
                                forbidden_networks=[ipaddress.ip_network("8.8.8.0/24")])


def hexed(address):
    """/proc/net/route prints the network-order address as a native-endian u32."""
    return "%08X" % int.from_bytes(ipaddress.ip_address(address).packed, sys.byteorder)


def route(iface, dest, gateway, flags, mask):
    return "\t".join([iface, hexed(dest), hexed(gateway), flags, "0", "0", "0", hexed(mask), "0", "0", "0"])


ROUTES = "\n".join([
    "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT",
    route("eth0", "0.0.0.0", "192.168.1.1", "0003", "0.0.0.0"),        # маршрут по умолчанию
    route("eth0", "172.18.0.0", "0.0.0.0", "0001", "255.255.0.0"),     # сеть Docker
    route("eth1", "10.77.0.0", "0.0.0.0", "0001", "255.255.255.0"),    # вторая сеть
    route("eth0", "10.0.0.0", "172.18.0.1", "0003", "255.0.0.0"),      # через шлюз: не подсеть интерфейса
    route("eth2", "10.55.0.0", "0.0.0.0", "0000", "255.255.0.0"),      # маршрут выключен
    route("eth3", "8.8.8.0", "0.0.0.0", "0001", "255.255.255.0"),      # публичный префикс
    "garbage line",
]) + "\n"
INET6 = "\n".join([
    "fd000000000000000000000000000001 02 40 00 80    eth1",
    "fe80000000000000a00027fffe000001 02 40 20 80    eth1",
    "20010db8000000000000000000000001 03 40 00 80    eth3",
    "00000000000000000000000000000001 01 80 10 80      lo",
]) + "\n"


def test_own_networks_come_from_proc_with_real_masks():
    files = {"/proc/net/route": ROUTES, "/proc/net/if_inet6": INET6}

    def no_fallback():
        pytest.fail("fallback must not run when /proc is readable")

    found = set(own_networks(read=files.get, addresses=no_fallback))
    assert ipaddress.ip_network("172.18.0.0/16") in found and ipaddress.ip_network("10.77.0.0/24") in found
    assert ipaddress.ip_network("fd00::/64") in found
    for absent in ("0.0.0.0/0", "10.0.0.0/8", "10.55.0.0/16", "8.8.8.0/24"):  # шлюз, выключенный маршрут, публичный префикс
        assert ipaddress.ip_network(absent) not in found


def test_own_networks_ignore_broad_routes_and_vpn_default_routes():
    routes = "\n".join([
        "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT",
        route("wg0", "0.0.0.0", "0.0.0.0", "0001", "0.0.0.0"),            # default dev wg0: без шлюза, маска 0
        route("wg0", "0.0.0.0", "0.0.0.0", "0001", "128.0.0.0"),          # VPN def1: 0.0.0.0/1
        route("wg0", "128.0.0.0", "0.0.0.0", "0001", "128.0.0.0"),        # VPN def1: 128.0.0.0/1
        route("wg0", "10.0.0.0", "0.0.0.0", "0001", "254.0.0.0"),         # /7: шире допустимого
        route("eth1", "10.0.0.0", "0.0.0.0", "0001", "255.0.0.0"),        # /8: граница, подсеть интерфейса
        route("eth0", "172.18.0.0", "0.0.0.0", "0001", "255.255.0.0"),
    ]) + "\n"
    found = set(own_networks(read={"/proc/net/route": routes}.get, addresses=lambda: []))
    assert found == {ipaddress.ip_network("10.0.0.0/8"), ipaddress.ip_network("172.18.0.0/16")}


def test_own_networks_drop_the_default_route_without_a_gateway_on_its_own():
    routes = "\n".join([
        "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT",
        route("wg0", "0.0.0.0", "0.0.0.0", "0001", "0.0.0.0"),
    ]) + "\n"
    assert own_networks(read={"/proc/net/route": routes}.get, addresses=lambda: []) == ()


def test_own_networks_ignore_ipv6_prefixes_shorter_than_16():
    inet6 = "\n".join([
        "fd000000000000000000000000000001 02 10 00 80    eth1",   # /16: граница, остаётся
        "fc000000000000000000000000000001 03 0f 00 80    eth2",   # /15: шире допустимого
        "fd120000000000000000000000000001 04 00 00 80    wg0",    # /0
        "fd120000000000000000000000000002 05 40 00 80    eth3",
    ]) + "\n"
    found = set(own_networks(read={"/proc/net/if_inet6": inet6}.get, addresses=lambda: []))
    assert found == {ipaddress.ip_network("fd00::/16"), ipaddress.ip_network("fd12::/64")}


def test_own_networks_use_the_address_fallback_when_proc_is_missing():
    found = own_networks(read=lambda path: None, addresses=lambda: ["172.18.0.2", "8.8.8.8", "fe80::1%eth0", "junk"])
    assert set(found) == {ipaddress.ip_network("172.18.0.2/32"), ipaddress.ip_network("fe80::1/128")}


def test_own_networks_survive_a_broken_source():
    def boom():
        raise OSError("no network")
    assert own_networks(read=lambda path: None, addresses=boom) == ()
    assert own_networks(read=lambda path: "x\ny z\n", addresses=lambda: []) == ()


def test_own_networks_default_sources_run_on_this_machine():
    assert isinstance(own_networks(), tuple)


def test_configured_forbidden_networks_from_env(monkeypatch):
    assert configured_forbidden_networks("10.9.0.0/16, fd12::/32 ,") == (
        ipaddress.ip_network("10.9.0.0/16"), ipaddress.ip_network("fd12::/32"))
    assert configured_forbidden_networks("") == () and configured_forbidden_networks("  ") == ()
    monkeypatch.setenv("PROVIDER_FORBIDDEN_CIDRS", "192.168.50.0/24")
    assert configured_forbidden_networks() == (ipaddress.ip_network("192.168.50.0/24"),)
    monkeypatch.delenv("PROVIDER_FORBIDDEN_CIDRS")
    assert configured_forbidden_networks() == ()


@pytest.mark.parametrize("bad", ["10.0.0.0/33", "not-a-cidr", "10.0.0.0/8,oops"])
def test_a_typo_in_forbidden_cidrs_fails_loudly(bad):
    with pytest.raises(ValueError, match="PROVIDER_FORBIDDEN_CIDRS"):
        configured_forbidden_networks(bad)


# ---- IPv4-compatible и mapped ----------------------------------------------------------------------------------

def test_ipv4_compatible_range_is_always_forbidden():
    assert ipaddress.ip_network("::/96") in ALWAYS_FORBIDDEN


async def test_ipv4_mapped_addresses_follow_the_ipv4_rules():
    with pytest.raises(PrivateAddressError):
        await validate_base_url("https://llm.lan", resolver=resolving("::ffff:10.0.0.5"))
    for mapped in ("::ffff:127.0.0.1", "::ffff:169.254.169.254", "::ffff:0.0.0.0"):
        with pytest.raises(ValueError) as caught:
            await validate_base_url("https://llm.lan", resolver=resolving(mapped), allow_private=True)
        assert not isinstance(caught.value, PrivateAddressError)
    with pytest.raises(ValueError):
        await validate_base_url("https://[::ffff:127.0.0.1]", allow_private=True)


# ---- шлюз ------------------------------------------------------------------------------------------------------

async def test_gateway_refuses_an_unapproved_ip_and_reports_a_changed_approval():
    reported = []

    async def changed(provider):
        reported.append(provider.id)

    provider = provider_at("https://llm.lan", allow_private=True, ips=("192.168.1.20",))
    ok, seen = await call_gateway(provider, resolving("192.168.1.20"), on_address_changed=changed)
    assert ok.status_code == 200 and not reported
    moved, seen = await call_gateway(provider, resolving("172.18.0.2"), on_address_changed=changed)
    assert moved.status_code == 502 and not seen and reported == ["p1"]
    # запрещённый адрес не «изменение одобренного»: повторное одобрение его не исправит
    banned, seen = await call_gateway(provider, resolving("127.0.0.1"), on_address_changed=changed)
    assert banned.status_code == 502 and not seen and reported == ["p1"]


async def test_gateway_without_a_stored_set_refuses_a_flagged_provider():
    provider = GatewayProvider("p1", "openai_api", "https://llm.lan", "real-key", True, ["gpt-test"], allow_private=True)
    response, seen = await call_gateway(provider, resolving("192.168.1.20"))
    assert response.status_code == 502 and not seen


async def test_failure_of_the_report_hook_does_not_change_the_refusal():
    async def broken(provider):
        raise RuntimeError("db down")
    response, seen = await call_gateway(provider_at("https://llm.lan", allow_private=True), resolving("172.18.0.2"),
                                        on_address_changed=broken)
    assert response.status_code == 502 and not seen


async def test_gateway_refuses_the_core_own_subnets_even_for_an_approved_ip():
    provider = provider_at("https://llm.lan", allow_private=True, ips=("172.18.0.2",))
    allowed, seen = await call_gateway(provider, resolving("172.18.0.2"))
    assert allowed.status_code == 200
    refused, seen = await call_gateway(provider, resolving("172.18.0.2"), forbidden_networks=[DOCKER])
    assert refused.status_code == 502 and not seen

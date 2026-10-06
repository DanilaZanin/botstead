"""Сетевая политика: генерация правил iptables, идемпотентность, ошибки без прав."""
import dataclasses

import pytest

from bothub_launcher.config import AllowRule
from bothub_launcher.errors import NetPolicyError
from bothub_launcher.netpolicy import FORWARD_CHAIN, INPUT_CHAIN, NetPolicy, NetSpec, render_restore
from bothub_launcher.testing import FakeIptables

A = NetSpec("bothub-u-a", "bhuaaaaaaaaaa", "172.20.0.2")
B = NetSpec("bothub-u-b", "bhubbbbbbbbbb", "172.21.0.2")


def lines(text, chain):
    return [ln for ln in text.splitlines() if ln.startswith(f"-A {chain} ")]


def idx(seq, needle):
    return next(i for i, ln in enumerate(seq) if needle in ln)


# ---------- генерация ----------

def test_restore_is_a_transaction_that_flushes_only_its_chains(cfg):
    text = render_restore(cfg, [A], 4)
    rows = text.splitlines()
    assert rows[0] == "*filter" and rows[-1] == "COMMIT"
    assert f":{FORWARD_CHAIN} - [0:0]" in rows and f":{INPUT_CHAIN} - [0:0]" in rows
    assert f"-F {FORWARD_CHAIN}" in rows and f"-F {INPUT_CHAIN}" in rows
    # не трогает чужие цепочки (DOCKER, DOCKER-USER, INPUT, FORWARD)
    assert not any(ln.startswith(("-F INPUT", "-F FORWARD", "-F DOCKER")) for ln in rows)
    assert all(ln.startswith(("-A BOTHUB-", "-F BOTHUB-", ":BOTHUB-", "*", "COMMIT")) for ln in rows)


@pytest.mark.parametrize("cidr", [
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "100.64.0.0/10", "127.0.0.0/8",
])
def test_private_ranges_dropped_from_bot_bridge(cfg, cidr):
    fwd = lines(render_restore(cfg, [A], 4), FORWARD_CHAIN)
    assert f"-A {FORWARD_CHAIN} -i {A.bridge} -d {cidr} -j DROP" in fwd


def test_ipv6_ula_and_link_local_dropped(cfg):
    text = render_restore(cfg, [A], 6)
    fwd = lines(text, FORWARD_CHAIN)
    assert f"-A {FORWARD_CHAIN} -i {A.bridge} -d fc00::/7 -j DROP" in fwd
    assert f"-A {FORWARD_CHAIN} -i {A.bridge} -d fe80::/10 -j DROP" in fwd
    assert "10.0.0.0/8" not in text and "172.20.0.2" not in text


def test_ipv6_core_only_api_port(cfg):
    net = NetSpec("bothub-u-a", "bhuaaaaaaaaaa", "172.20.0.2", "fd00::2")
    rows = lines(render_restore(cfg, [net], 6), FORWARD_CHAIN)
    assert idx(rows, "-d fd00::2/128 -p tcp --dport 8080 -j ACCEPT") < idx(
        rows, "-d fd00::2/128 -j DROP") < idx(rows, "-o bhuaaaaaaaaaa -j ACCEPT")


def test_ipv4_text_has_no_v6_ranges(cfg):
    text = render_restore(cfg, [A], 4)
    assert "fc00::/7" not in text and "fe80::/10" not in text


def test_core_reachable_on_api_port_only(cfg):
    fwd = lines(render_restore(cfg, [A], 4), FORWARD_CHAIN)
    accept = idx(fwd, f"-d 172.20.0.2/32 -p tcp --dport {cfg.api_port} -j ACCEPT")
    drop_core = idx(fwd, "-d 172.20.0.2/32 -j DROP")
    intra = idx(fwd, f"-i {A.bridge} -o {A.bridge} -j ACCEPT")
    first_private_drop = idx(fwd, "-d 10.0.0.0/8 -j DROP")
    assert accept < drop_core < intra < first_private_drop


def test_no_core_rules_when_core_not_attached(cfg):
    spec = NetSpec("bothub-u-a", "bhuaaaaaaaaaa", None)
    fwd = lines(render_restore(cfg, [spec], 4), FORWARD_CHAIN)
    assert not any("--dport" in ln for ln in fwd)
    assert f"-A {FORWARD_CHAIN} -i {spec.bridge} -o {spec.bridge} -j DROP" in fwd
    assert any("-d 10.0.0.0/8 -j DROP" in ln for ln in fwd)


def test_new_inbound_from_other_interfaces_dropped(cfg):
    fwd = lines(render_restore(cfg, [A], 4), FORWARD_CHAIN)
    assert f"-A {FORWARD_CHAIN} -o {A.bridge} ! -i {A.bridge} -m conntrack --ctstate NEW -j DROP" in fwd


def test_host_input_dropped_except_established(cfg):
    inp = lines(render_restore(cfg, [A], 4), INPUT_CHAIN)
    est = idx(inp, "--ctstate RELATED,ESTABLISHED -j ACCEPT")
    drop = idx(inp, f"-i {A.bridge} -j DROP")
    assert est < drop
    assert inp[-1] == f"-A {INPUT_CHAIN} -i {A.bridge} -j DROP"


def test_exceptions_from_config_come_before_drops(cfg):
    cfg2 = dataclasses.replace(cfg, allow=(
        AllowRule("192.168.1.5/32", "tcp", 11434, "Ollama"),
        AllowRule("fd00::5/128", "tcp", 11434, "Ollama v6"),
    ), host_allow=(AllowRule("10.9.0.1/32", "tcp", 9100, "node_exporter"),))
    v4 = render_restore(cfg2, [A], 4)
    fwd = lines(v4, FORWARD_CHAIN)
    allow = idx(fwd, "-d 192.168.1.5/32 -p tcp --dport 11434 -j ACCEPT")
    assert allow < idx(fwd, "-d 192.168.0.0/16 -j DROP")
    assert "fd00::5" not in v4
    inp = lines(v4, INPUT_CHAIN)
    assert idx(inp, "-d 10.9.0.1/32 -p tcp --dport 9100 -j ACCEPT") < idx(inp, f"-i {A.bridge} -j DROP")
    v6 = render_restore(cfg2, [A], 6)
    assert "-d fd00::5/128 -p tcp --dport 11434 -j ACCEPT" in v6 and "192.168.1.5" not in v6


def test_allow_rule_without_port_and_proto_any(cfg):
    cfg2 = dataclasses.replace(cfg, allow=(AllowRule("192.168.1.9/32", "any", None),))
    fwd = lines(render_restore(cfg2, [A], 4), FORWARD_CHAIN)
    assert f"-A {FORWARD_CHAIN} -i {A.bridge} -d 192.168.1.9/32 -j ACCEPT" in fwd


def test_allow_rule_validation():
    for bad in [("192.168.1.5; reboot", "tcp", 1), ("not-an-ip", "tcp", 1), ("10.0.0.1/8", "tcp", 1),
                ("10.0.0.0/8", "icmp", None), ("10.0.0.0/8", "tcp", 70000), ("10.0.0.0/8", "tcp", 0)]:
        with pytest.raises(ValueError):
            AllowRule(*bad)


def test_render_is_deterministic_and_order_independent(cfg):
    assert render_restore(cfg, [A, B], 4) == render_restore(cfg, [A, B], 4)
    assert render_restore(cfg, [A, B], 4) == render_restore(cfg, [B, A], 4)


def test_each_network_gets_its_own_bridge_rules(cfg):
    text = render_restore(cfg, [A, B], 4)
    assert f"-i {A.bridge} -d 10.0.0.0/8" in text and f"-i {B.bridge} -d 10.0.0.0/8" in text
    assert f"-d {A.core_ip}/32" in text and f"-d {B.core_ip}/32" in text


def test_empty_network_list_yields_empty_chains(cfg):
    text = render_restore(cfg, [], 4)
    assert not lines(text, FORWARD_CHAIN) and not lines(text, INPUT_CHAIN)


def test_bridge_name_must_be_safe(cfg):
    with pytest.raises(ValueError):
        NetSpec("n", "br0; reboot", None)
    with pytest.raises(ValueError):
        NetSpec("n", "x" * 16, None)
    with pytest.raises(ValueError):
        NetSpec("n", "ok", "not-an-ip")


# ---------- применение ----------

@pytest.fixture
def ipt():
    return FakeIptables()


def policy(cfg, ipt, sysctl="1"):
    return NetPolicy(cfg, ipt, read_file=lambda path: sysctl)


async def test_reconcile_installs_chains_and_jumps_first(cfg, ipt):
    np = policy(cfg, ipt)
    await np.check()
    report = await np.reconcile([A])
    assert report.ipv4 and report.ipv6 and report.networks == 1
    assert ipt.rules(4, "DOCKER-USER")[0] == f"-j {FORWARD_CHAIN}"
    assert ipt.rules(4, "INPUT")[0] == f"-j {INPUT_CHAIN}"
    assert any(f"-i {A.bridge} -d 10.0.0.0/8 -j DROP" in r for r in ipt.rules(4, FORWARD_CHAIN))
    assert ipt.rules(6, "DOCKER-USER")[0] == f"-j {FORWARD_CHAIN}"


async def test_rules_are_restored_before_the_jump_is_inserted(cfg, ipt):
    await policy(cfg, ipt).reconcile([A])
    names = [c[0] for c in ipt.calls]
    first_restore = next(i for i, c in enumerate(ipt.calls) if c[0].endswith("-restore"))
    first_insert = next(i for i, c in enumerate(ipt.calls) if "-I" in c)
    assert first_restore < first_insert, names


async def test_reconcile_twice_is_idempotent(cfg, ipt):
    np = policy(cfg, ipt)
    await np.reconcile([A, B])
    snapshot = ipt.snapshot()
    ipt.calls.clear()
    await np.reconcile([A, B])
    assert ipt.snapshot() == snapshot
    assert not [c for c in ipt.calls if "-I" in c or "-D" in c or "-A" in c], "повторный проход не должен менять цепочки"


async def test_jump_not_duplicated_after_many_runs(cfg, ipt):
    np = policy(cfg, ipt)
    for _ in range(3):
        await np.reconcile([A])
    assert ipt.rules(4, "DOCKER-USER").count(f"-j {FORWARD_CHAIN}") == 1
    assert ipt.rules(4, "INPUT").count(f"-j {INPUT_CHAIN}") == 1


async def test_jump_moved_back_to_first_position_and_duplicates_removed(cfg, ipt):
    np = policy(cfg, ipt)
    await np.reconcile([A])
    ipt.insert_rule(4, "DOCKER-USER", 1, "-j SOMEONE-ELSE")  # чужое правило сверху (например, ufw reload)
    await np.reconcile([A])
    rules = ipt.rules(4, "DOCKER-USER")
    assert rules[0] == f"-j {FORWARD_CHAIN}"
    assert rules.count(f"-j {FORWARD_CHAIN}") == 1
    assert "-j SOMEONE-ELSE" in rules and "-j RETURN" in rules  # чужое не потеряно


async def test_removed_network_disappears_from_rules(cfg, ipt):
    np = policy(cfg, ipt)
    await np.reconcile([A, B])
    await np.reconcile([A])
    text = "\n".join(ipt.rules(4, FORWARD_CHAIN) + ipt.rules(4, INPUT_CHAIN))
    assert B.bridge not in text and A.bridge in text


async def test_foreign_rules_untouched(cfg, ipt):
    ipt.insert_rule(4, "DOCKER-USER", 1, "-s 1.2.3.4/32 -j DROP")
    await policy(cfg, ipt).reconcile([A])
    assert "-s 1.2.3.4/32 -j DROP" in ipt.rules(4, "DOCKER-USER")


async def test_ipv6_without_docker_user_chain_jumps_from_forward(cfg):
    ipt = FakeIptables(v6_docker_user=False)
    report = await policy(cfg, ipt).reconcile([A])
    assert report.ipv6
    assert ipt.rules(6, "FORWARD")[0] == f"-j {FORWARD_CHAIN}"


async def test_ipv6_unavailable_fails_closed(cfg):
    ipt = FakeIptables(missing_bins={"ip6tables", "ip6tables-restore"})
    np = policy(cfg, ipt)
    await np.check()
    with pytest.raises(NetPolicyError):
        await np.reconcile([A])


def test_internal_dns_only_port_53_before_private_drop(cfg):
    cfg = dataclasses.replace(cfg, internal_dns="172.20.0.1")
    rows = lines(render_restore(cfg, [A], 4), FORWARD_CHAIN)
    for proto in ("udp", "tcp"):
        rule = f"-d 172.20.0.1/32 -p {proto} --dport 53 -j ACCEPT"
        assert idx(rows, rule) < idx(rows, "-d 172.16.0.0/12 -j DROP")
    assert all("172.20.0.1/32" not in row or "--dport 53" in row for row in rows)
    input_rows = lines(render_restore(cfg, [A], 4), INPUT_CHAIN)
    for proto in ("udp", "tcp"):
        assert idx(input_rows, f"-d 172.20.0.1/32 -p {proto} --dport 53 -j ACCEPT") < idx(
            input_rows, f"-i {A.bridge} -j DROP")


# ---------- ошибки без прав ----------

async def test_check_fails_without_permissions(cfg):
    np = policy(cfg, FakeIptables(permission_denied=True))
    with pytest.raises(NetPolicyError) as exc:
        await np.check()
    assert "NET_ADMIN" in str(exc.value) and "host" in str(exc.value).lower()


async def test_check_fails_without_docker_user_chain(cfg):
    with pytest.raises(NetPolicyError) as exc:
        await policy(cfg, FakeIptables(has_docker_user=False)).check()
    assert "DOCKER-USER" in str(exc.value)


async def test_check_fails_when_iptables_binary_missing(cfg):
    with pytest.raises(NetPolicyError) as exc:
        await policy(cfg, FakeIptables(missing_bins={"iptables"})).check()
    assert "iptables" in str(exc.value)


@pytest.mark.parametrize("sysctl", ["0", "garbage"])
async def test_check_fails_without_bridge_netfilter(cfg, ipt, sysctl):
    with pytest.raises(NetPolicyError) as exc:
        await policy(cfg, ipt, sysctl=sysctl).check()
    assert "br_netfilter" in str(exc.value)


async def test_check_fails_when_sysctl_missing(cfg, ipt):
    def reader(path):
        raise FileNotFoundError(path)
    with pytest.raises(NetPolicyError) as exc:
        await NetPolicy(cfg, ipt, read_file=reader).check()
    assert "br_netfilter" in str(exc.value)


async def test_reconcile_failure_is_explicit(cfg):
    ipt = FakeIptables(restore_fails=True)
    with pytest.raises(NetPolicyError):
        await policy(cfg, ipt).reconcile([A])
    assert ipt.rules(4, "DOCKER-USER")[:1] != [f"-j {FORWARD_CHAIN}"], "без правил цепочку не включаем"

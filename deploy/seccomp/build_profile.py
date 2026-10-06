#!/usr/bin/env python3
"""Генератор seccomp-профиля ботов: deploy/seccomp/bot.json из moby-default.json.

Зачем. Chromium в контейнере бота (uid 1001) строит песочницу через user namespaces:
clone(CLONE_NEWUSER|CLONE_NEWPID|CLONE_NEWNET) и unshare. Профиль moby отдаёт clone/unshare только при
CAP_SYS_ADMIN, а контейнер бота идёт с --cap-drop ALL, поэтому без изменений Chromium падает с
EPERM (unshare -U). Включать --no-sandbox или CAP_SYS_ADMIN нельзя.

Что делает генератор (всё остальное из moby-default.json остаётся как есть):
  а) clone, unshare и chroot разрешены без условий (chroot: песочница Chromium делает его внутри своего user
     namespace; без него Chromium падает с «Check failed: sys_chroot», подтверждено на живом Docker; вне user
     namespace ядро возвращает EPERM без CAP_SYS_CHROOT, а код бота в user namespace не попадает: bot-guard). setns не разрешается (Chromium обходится без него),
     clone3 остаётся ENOSYS (glibc и Chromium откатываются на clone).
  б) mount, umount2, pivot_root, fanotify_init и прочее, что база даёт только при capabilities,
     не трогается и остаётся закрытым.
  в) из разрешённых убираются userfaultfd, io_uring_*, keyctl, add_key, request_key, open_by_handle_at,
     kexec_*, bpf, perf_event_open. Это поверхность ядра, которая нужна только для эскалации. Запрещать явно
     не нужно: defaultAction профиля ERRNO.
  г) socket: AF_UNIX, AF_INET, AF_INET6 без ограничений; AF_NETLINK только NETLINK_ROUTE и
     NETLINK_KOBJECT_UEVENT; остальные семейства (AF_PACKET, AF_ALG, AF_VSOCK, AF_KEY, ...) закрыты.
     socketcall убирается (мультиплексор для 32-битных ABI обходил бы правила на socket).
  д) setsockopt и getsockopt: опции netfilter (x_tables, CVE-2021-22555) закрыты на уровнях SOL_IP (0) и
     SOL_IPV6 (41); остальные уровни и опции разрешены (подробности в sockopt_rules).

Правила с includes.caps при контейнере без capabilities отпадают: Docker сверяет их с набором capabilities
контейнера один раз, при создании. Процесс, который вошёл в user namespace, получает capabilities только
внутри своего namespace, а фильтр остаётся тем же. Поэтому открытые clone/unshare не дают ни mount, ни bpf.

Запуск:
    python3 deploy/seccomp/build_profile.py            # записать bot.json
    python3 deploy/seccomp/build_profile.py --check    # код 1, если bot.json отличается от вывода генератора
Результат воспроизводим: тот же вход даёт тот же байт-в-байт файл (порядок ключей и правил фиксирован).
"""
import argparse
import copy
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE_PROFILE = HERE / "moby-default.json"
BOT_PROFILE = HERE / "bot.json"

# в) убираются из всех правил профиля
REMOVED_SYSCALLS = (
    "userfaultfd",
    "io_uring_setup", "io_uring_enter", "io_uring_register",
    "keyctl", "add_key", "request_key",
    "open_by_handle_at",
    "kexec_load", "kexec_file_load",
    "bpf", "perf_event_open",
)
# а) открываются без условий
OPENED_SYSCALLS = ("clone", "unshare")
# Остаются закрытыми и проверяются инвариантами ниже.
KEPT_CLOSED = ("setns", "mount", "umount2", "pivot_root")
# д) setsockopt и getsockopt живут в безусловном списке базы; генератор их оттуда убирает и добавляет правила.
SOCKOPT_SYSCALLS = ("setsockopt", "getsockopt")
SOL_IP, SOL_IPV6 = 0, 41
# IPT_SO_SET_REPLACE/ADD_COUNTERS (ip_tables.h), IP6T_SO_SET_* (тот же номер), ARPT_SO_SET_* (96, 97), EBT_SO_SET_*
# (128, 129): сеттеры x_tables, arp_tables и ebtables, достижимые из user namespace с CAP_NET_ADMIN.
NETFILTER_SET = (64, 65, 96, 97, 128, 129)
# getsockopt: IPT_SO_GET_INFO/ENTRIES 64, 65, GET_REVISION_MATCH/TARGET 66, 67 (SOL_IP); у IP6T ревизии 68, 69,
# а 66 и 67 на уровне SOL_IPV6 это IPV6_RECVTCLASS и IPV6_TCLASS из linux/in6.h: их нельзя закрывать.
# ARPT_SO_GET_INFO/ENTRIES/REVISION_TARGET 96, 97, 99; EBT_SO_GET_* 128-131.
NETFILTER_GET = {
    SOL_IP: (64, 65, 66, 67, 96, 97, 98, 99, 128, 129, 130, 131),
    SOL_IPV6: (64, 65, 68, 69, 96, 97, 98, 99, 128, 129, 130, 131),
}
# На SOL_IP и SOL_IPV6 нет нативных опций выше 99 (ядро: IP_* до 52, IPV6_* до 78, SO_ORIGINAL_DST 80), поэтому
# для этих двух уровней optname >= 128 не разрешается вовсе: так правила короче, а потерь нет.
SOCKOPT_OPTNAME_LIMIT = 128
# Аргументы seccomp 64-битные, а ядро берёт level и optname как int, то есть младшие 32 бита (на x86_64
# `(int)regs->si`). Разрешающее правило «level != 0» без маски пропустило бы level = 1<<32 как «не ноль», а ядро
# увидело бы SOL_IP. Поэтому условия строятся префиксами с маской на все 64 бита: ненулевые старшие биты
# не совпадают ни с одним правилом, и вызов отклоняется.
FULL_MASK = (1 << 64) - 1

# г)
AF_UNIX, AF_INET, AF_INET6, AF_NETLINK = 1, 2, 10, 16
NETLINK_ROUTE, NETLINK_KOBJECT_UEVENT = 0, 15

OPEN_COMMENT = (
    "clone и unshare без условия по CAP_SYS_ADMIN: Chromium входит в user namespace "
    "(CLONE_NEWUSER|CLONE_NEWPID|CLONE_NEWNET). Правила с includes.caps при cap-drop ALL отпадают, а "
    "capabilities внутри user namespace фильтр не меняют: mount, bpf, setns остаются закрытыми. "
    "Коду бота (uid 1000) эти вызовы закрывает второй фильтр bot-guard."
)
SOCKET_COMMENT = (
    "socket: AF_UNIX, AF_INET, AF_INET6 без ограничений; AF_NETLINK только NETLINK_ROUTE и "
    "NETLINK_KOBJECT_UEVENT; остальные семейства закрыты."
)
SOCKOPT_COMMENT = (
    "setsockopt/getsockopt: на уровнях SOL_IP (0) и SOL_IPV6 (41) закрыты опции netfilter (x_tables, arp_tables, "
    "ebtables; CVE-2021-22555), остальное разрешено. Условия через маски по всем 64 битам: ядро берёт level и "
    "optname как int, и значение с ненулевыми старшими битами не должно проскочить как «другой уровень»."
)
CHROOT_COMMENT = (
    "chroot без условия (отключается флагом --no-chroot): Chromium вызывает его внутри своего user namespace, "
    "где у него есть CAP_SYS_CHROOT над собственным namespace. В init namespace ядро вернёт EPERM без capability."
)


def _caps(rule: dict, key: str) -> list[str]:
    return rule.get(key, {}).get("caps", [])


def _arg(index: int, value: int) -> dict:
    return {"index": index, "value": value, "op": "SCMP_CMP_EQ"}


def socket_rules() -> list[dict]:
    allow = "SCMP_ACT_ALLOW"
    return [
        {"names": ["socket"], "action": allow, "args": [_arg(0, AF_UNIX)], "comment": SOCKET_COMMENT},
        {"names": ["socket"], "action": allow, "args": [_arg(0, AF_INET)]},
        {"names": ["socket"], "action": allow, "args": [_arg(0, AF_INET6)]},
        {"names": ["socket"], "action": allow, "args": [_arg(0, AF_NETLINK), _arg(2, NETLINK_ROUTE)]},
        {"names": ["socket"], "action": allow, "args": [_arg(0, AF_NETLINK), _arg(2, NETLINK_KOBJECT_UEVENT)]},
    ]


def _cover(denied: set[int], bits: int) -> list[tuple[int, int]]:
    """Минимальный набор префиксов (значение, число свободных младших бит), покрывающий [0, 2**bits) без denied."""
    out: list[tuple[int, int]] = []

    def walk(prefix: int, free: int) -> None:
        size = 1 << free
        inside = [x for x in denied if prefix <= x < prefix + size]
        if not inside:
            out.append((prefix, free))
        elif len(inside) < size:
            half = size >> 1
            walk(prefix, free - 1)
            walk(prefix + half, free - 1)

    walk(0, bits)
    return out


def _match(index: int, prefix: int, free: int) -> dict:
    if free == 0:
        return _arg(index, prefix)
    return {"index": index, "value": FULL_MASK ^ ((1 << free) - 1), "valueTwo": prefix, "op": "SCMP_CMP_MASKED_EQ"}


def sockopt_rules() -> list[dict]:
    """Разрешающие правила setsockopt/getsockopt: всё, кроме опций netfilter на SOL_IP и SOL_IPV6.

    Запрет выражен как набор разрешений: (а) level не 0 и не 41; (б) для level 0 и 41 optname вне закрытого набора."""
    allow = "SCMP_ACT_ALLOW"
    rules: list[dict] = []
    for call in SOCKOPT_SYSCALLS:
        denied_levels = {SOL_IP, SOL_IPV6}
        for prefix, free in _cover(denied_levels, 32):
            rules.append({"names": [call], "action": allow, "args": [_match(1, prefix, free)]})
        for level in (SOL_IP, SOL_IPV6):
            bad = NETFILTER_SET if call == "setsockopt" else NETFILTER_GET[level]
            for prefix, free in _cover(set(bad), SOCKOPT_OPTNAME_LIMIT.bit_length() - 1):
                rules.append({"names": [call], "action": allow,
                              "args": [_arg(1, level), _match(2, prefix, free)]})
    rules[0]["comment"] = SOCKOPT_COMMENT
    return rules


def build(base: dict, *, allow_chroot: bool = True) -> dict:
    profile = copy.deepcopy(base)
    rules: list[dict] = []
    replaced = set()
    for rule in profile["syscalls"]:
        names = list(rule["names"])
        replaced |= set(names) & set(SOCKOPT_SYSCALLS)
        if "socket" in names:
            if names != ["socket"] or "args" not in rule:
                raise ValueError("ожидалось отдельное правило socket с args; база изменилась, проверьте генератор")
            continue  # г) заменяется на socket_rules()
        if names == ["clone"] and "args" in rule:
            continue  # правила clone с маской флагов поглощены безусловным правилом
        if "unshare" in names and "clone" in names and _caps(rule, "includes") == ["CAP_SYS_ADMIN"]:
            names = [n for n in names if n not in OPENED_SYSCALLS]  # а) переехали в безусловное правило
        names = [n for n in names if n not in REMOVED_SYSCALLS and n != "socketcall" and n not in SOCKOPT_SYSCALLS]
        if not names:
            continue
        rule["names"] = names
        rules.append(rule)

    if replaced != set(SOCKOPT_SYSCALLS):
        raise ValueError("в базе нет setsockopt/getsockopt; база изменилась, проверьте генератор")
    opened = list(OPENED_SYSCALLS) + (["chroot"] if allow_chroot else [])
    comment = OPEN_COMMENT + (" " + CHROOT_COMMENT if allow_chroot else "")
    new_rules = [{"names": opened, "action": "SCMP_ACT_ALLOW", "comment": comment}, *socket_rules(), *sockopt_rules()]
    # Новые правила идут сразу после безусловного списка базы: профиль читается сверху вниз.
    profile["syscalls"] = [rules[0], *new_rules, *rules[1:]]
    verify(profile, allow_chroot=allow_chroot)
    return profile


def _unconditional(rule: dict) -> bool:
    return rule["action"] == "SCMP_ACT_ALLOW" and not rule.get("args") and not rule.get("includes") \
        and not rule.get("excludes")


def verify(profile: dict, *, allow_chroot: bool = True) -> None:
    """Инварианты результата: без них генератор не пишет файл."""
    if profile.get("defaultAction") != "SCMP_ACT_ERRNO":
        raise ValueError("defaultAction должен быть SCMP_ACT_ERRNO")
    for rule in profile["syscalls"]:
        if rule["action"] != "SCMP_ACT_ALLOW":
            continue
        bad = set(rule["names"]) & set(REMOVED_SYSCALLS)
        if bad:
            raise ValueError(f"запрещённые вызовы остались разрешены: {sorted(bad)}")
        if "socketcall" in rule["names"]:
            raise ValueError("socketcall должен быть убран")
    open_names = {n for r in profile["syscalls"] if _unconditional(r) for n in r["names"]}
    if not set(OPENED_SYSCALLS) <= open_names:
        raise ValueError("clone и unshare должны быть разрешены без условий")
    closed = set(KEPT_CLOSED) if allow_chroot else {*KEPT_CLOSED, "chroot"}
    for rule in profile["syscalls"]:
        if rule["action"] == "SCMP_ACT_ALLOW" and not _caps(rule, "includes") \
                and set(rule["names"]) & closed:
            raise ValueError(f"вызовы {sorted(set(rule['names']) & closed)} должны требовать capabilities")
    for call in SOCKOPT_SYSCALLS:
        if call in open_names:
            raise ValueError(f"{call} не должен быть разрешён без условий")
        if not any(call in r["names"] and r["action"] == "SCMP_ACT_ALLOW" and r.get("args") for r in profile["syscalls"]):
            raise ValueError(f"правила {call} потеряны")
    if "clone3" in open_names:
        raise ValueError("clone3 должен оставаться ENOSYS")
    if not any(r["action"] == "SCMP_ACT_ERRNO" and r["names"] == ["clone3"] and r.get("errnoRet") == 38
               for r in profile["syscalls"]):
        raise ValueError("правило clone3 -> ENOSYS потеряно")


def render(profile: dict) -> str:
    return json.dumps(profile, indent="\t", ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--base", type=Path, default=BASE_PROFILE)
    parser.add_argument("--output", type=Path, default=BOT_PROFILE)
    parser.add_argument("--check", action="store_true", help="не писать файл, вернуть 1 при расхождении")
    parser.add_argument("--no-chroot", dest="allow_chroot", action="store_false",
                        help="закрыть chroot (Chromium с песочницей при этом не стартует; см. docs/isolation.md)")
    args = parser.parse_args(argv)
    text = render(build(json.loads(args.base.read_text()), allow_chroot=args.allow_chroot))
    if args.check:
        if not args.output.is_file() or args.output.read_text() != text:
            print(f"{args.output} не совпадает с выводом генератора: python3 {Path(__file__).name}", file=sys.stderr)
            return 1
        return 0
    args.output.write_text(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

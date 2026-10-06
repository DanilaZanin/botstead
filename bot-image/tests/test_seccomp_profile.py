"""deploy/seccomp/bot.json: воспроизводимость генератора и модель профиля (как Docker читает правила).

Модель повторяет разбор профиля в Docker: правило действует, если выполнены includes/excludes по arch, caps и
minKernel, а args совпали; первое подходящее правило решает, иначе defaultAction. Контейнер бота идёт с
--cap-drop ALL, поэтому набор capabilities пуст."""
import importlib.util
import json
import tomllib
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SECCOMP = REPO / 'deploy' / 'seccomp'

spec = importlib.util.spec_from_file_location('build_profile', SECCOMP / 'build_profile.py')
build_profile = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_profile)

ARCHES = ('amd64', 'arm64')
ALLOW = ('allow', None)
EPERM = ('errno', 1)
ENOSYS = ('errno', 38)

AF = dict(UNIX=1, INET=2, INET6=10, NETLINK=16, PACKET=17, ALG=38, VSOCK=40, KEY=15, X25=9, BLUETOOTH=31, CAN=29,
          NFC=39, QIPCRTR=42, XDP=44, RDS=21, TIPC=30, IUCV=32, PPPOX=24, SNA=22, NETROM=6, AX25=3, ROSE=11)
NETLINK = dict(ROUTE=0, UNUSED=1, USERSOCK=2, FIREWALL=3, SOCK_DIAG=4, NFLOG=5, XFRM=6, SELINUX=7, ISCSI=8,
               AUDIT=9, FIB_LOOKUP=10, CONNECTOR=11, NETFILTER=12, IP6_FW=13, DNRTMSG=14, KOBJECT_UEVENT=15,
               GENERIC=16, SCSITRANSPORT=18, ECRYPTFS=19, RDMA=20, CRYPTO=21)

CMP = {
    'SCMP_CMP_EQ': lambda a, v, v2: a == v,
    'SCMP_CMP_NE': lambda a, v, v2: a != v,
    'SCMP_CMP_LT': lambda a, v, v2: a < v,
    'SCMP_CMP_LE': lambda a, v, v2: a <= v,
    'SCMP_CMP_GT': lambda a, v, v2: a > v,
    'SCMP_CMP_GE': lambda a, v, v2: a >= v,
    'SCMP_CMP_MASKED_EQ': lambda a, v, v2: (a & v) == v2,
}


def kernel(text):
    return tuple(int(x) for x in text.split('.'))


def decide(profile, name, args=(), *, arch='amd64', caps=frozenset(), kernel_version=(6, 8)):
    """Решение профиля Docker для вызова name с аргументами args (недостающие считаются нулями)."""
    padded = list(args) + [0] * (6 - len(args))
    for rule in profile['syscalls']:
        if name not in rule['names']:
            continue
        includes, excludes = rule.get('includes', {}), rule.get('excludes', {})
        if excludes.get('arches') and arch in excludes['arches']:
            continue
        if includes.get('arches') and arch not in includes['arches']:
            continue
        if any(c in caps for c in excludes.get('caps', [])):
            continue
        if not all(c in caps for c in includes.get('caps', [])):
            continue
        if includes.get('minKernel') and kernel_version < kernel(includes['minKernel']):
            continue
        if excludes.get('minKernel') and kernel_version >= kernel(excludes['minKernel']):
            continue
        if not all(CMP[a['op']](padded[a['index']], a['value'], a.get('valueTwo', 0)) for a in rule.get('args', [])):
            continue
        if rule['action'] == 'SCMP_ACT_ALLOW':
            return ALLOW
        if rule['action'] == 'SCMP_ACT_ERRNO':
            return ('errno', rule.get('errnoRet', profile.get('defaultErrnoRet', 1)))
        raise AssertionError(rule['action'])
    default = profile['defaultAction']
    assert default == 'SCMP_ACT_ERRNO', default
    return ('errno', profile.get('defaultErrnoRet', 1))


class ReproducibilityTests(unittest.TestCase):
    def test_bot_json_equals_generator_output(self):
        base = json.loads((SECCOMP / 'moby-default.json').read_text())
        expected = build_profile.render(build_profile.build(base))
        self.assertEqual((SECCOMP / 'bot.json').read_text(), expected,
                         'bot.json устарел: python3 deploy/seccomp/build_profile.py')

    def test_generator_is_deterministic(self):
        base = json.loads((SECCOMP / 'moby-default.json').read_text())
        runs = {build_profile.render(build_profile.build(json.loads(json.dumps(base)))) for _ in range(3)}
        self.assertEqual(len(runs), 1)

    def test_check_mode_reports_drift(self):
        self.assertEqual(build_profile.main(['--check']), 0)
        stale = SECCOMP / '.not-created'
        self.assertEqual(build_profile.main(['--check', '--output', str(stale)]), 1)
        self.assertFalse(stale.exists())

    def test_generator_does_not_mutate_its_input(self):
        base = json.loads((SECCOMP / 'moby-default.json').read_text())
        snapshot = json.dumps(base, sort_keys=True)
        build_profile.build(base)
        self.assertEqual(json.dumps(base, sort_keys=True), snapshot)

    def test_generator_refuses_a_base_it_does_not_understand(self):
        base = json.loads((SECCOMP / 'moby-default.json').read_text())
        for rule in base['syscalls']:
            if rule['names'] == ['socket']:
                rule.pop('args', None)
        with self.assertRaises(ValueError):
            build_profile.build(base)

    def test_base_profile_is_the_pinned_upstream_one(self):
        source = (SECCOMP / 'moby-default.SOURCE').read_text()
        self.assertIn('moby/profiles', source)
        self.assertIn('Apache-2.0', source)

    def test_other_allowed_syscalls_are_untouched(self):
        base = json.loads((SECCOMP / 'moby-default.json').read_text())
        bot = json.loads((SECCOMP / 'bot.json').read_text())
        unconditional = {n for r in base['syscalls'] if r['action'] == 'SCMP_ACT_ALLOW' and not r.get('args')
                         and not r.get('includes') and not r.get('excludes') for n in r['names']}
        for name in sorted(unconditional - {'socketcall'}):
            for arch in ARCHES:
                self.assertEqual(decide(bot, name, arch=arch), ALLOW, name)
        self.assertEqual(bot['defaultAction'], base['defaultAction'])
        self.assertEqual(bot['archMap'], base['archMap'])


class ProfileModelTests(unittest.TestCase):
    """Контейнер без capabilities, оба интерпретатора: amd64 и arm64."""

    @classmethod
    def setUpClass(cls):
        cls.profile = json.loads((SECCOMP / 'bot.json').read_text())

    def decide(self, name, args=(), **kw):
        return {arch: decide(self.profile, name, args, arch=arch, **kw) for arch in ARCHES}

    def assertAll(self, results, expected, label):
        for arch, got in results.items():
            self.assertEqual(got, expected, f'{label} на {arch}')

    def test_clone_and_unshare_are_allowed_with_namespace_flags(self):
        flags = 0x10000000 | 0x40000000 | 0x20000000 | 17  # NEWUSER | NEWNET | NEWPID | SIGCHLD
        self.assertAll(self.decide('clone', (flags,)), ALLOW, 'clone с NEWUSER|NEWPID|NEWNET')
        self.assertAll(self.decide('clone', (0x3D0F00,)), ALLOW, 'clone потока')
        self.assertAll(self.decide('unshare', (0x10000000,)), ALLOW, 'unshare -U')
        self.assertAll(self.decide('unshare', (0x10000000 | 0x40000000,)), ALLOW, 'unshare -rn')

    def test_clone3_stays_enosys_so_that_glibc_falls_back_to_clone(self):
        self.assertAll(self.decide('clone3'), ENOSYS, 'clone3')

    def test_setns_stays_closed(self):
        self.assertAll(self.decide('setns', (3, 0x10000000)), EPERM, 'setns')

    def test_capability_gated_syscalls_stay_closed(self):
        for name in ('mount', 'umount2', 'umount', 'pivot_root', 'move_mount', 'open_tree', 'fsopen',
                     'fsmount', 'mount_setattr', 'fanotify_init', 'lookup_dcookie', 'quotactl', 'sethostname',
                     'setdomainname', 'reboot', 'init_module', 'finit_module', 'delete_module', 'ioperm', 'iopl',
                     'settimeofday', 'clock_settime', 'acct', 'swapon', 'swapoff'):
            with self.subTest(name=name):
                self.assertAll(self.decide(name), EPERM, name)

    def test_removed_syscalls_are_denied_with_and_without_capabilities(self):
        removed = ('userfaultfd', 'io_uring_setup', 'io_uring_enter', 'io_uring_register', 'keyctl', 'add_key',
                   'request_key', 'open_by_handle_at', 'kexec_load', 'kexec_file_load', 'bpf', 'perf_event_open')
        everything = frozenset({'CAP_SYS_ADMIN', 'CAP_SYS_PTRACE', 'CAP_BPF', 'CAP_PERFMON', 'CAP_DAC_READ_SEARCH',
                                'CAP_SYS_BOOT', 'CAP_SYS_MODULE', 'CAP_NET_ADMIN'})
        for name in removed:
            with self.subTest(name=name):
                self.assertAll(self.decide(name), EPERM, name)
                self.assertAll(self.decide(name, caps=everything), EPERM, f'{name} с capabilities')

    def test_rules_with_includes_caps_drop_out_without_capabilities(self):
        # Модель верна: с CAP_SYS_ADMIN база открыла бы setns, значит без неё он закрыт именно правилом caps.
        self.assertAll(self.decide('setns', caps=frozenset({'CAP_SYS_ADMIN'})), ALLOW, 'setns с CAP_SYS_ADMIN')
        # chroot открыт без условий: он нужен песочнице Chromium внутри её user namespace (живой Docker).
        self.assertAll(self.decide('chroot', caps=frozenset()), ALLOW, 'chroot без capabilities')

    def test_socket_families(self):
        allowed = [(AF['UNIX'], 1, 0), (AF['UNIX'], 2, 0), (AF['UNIX'], 5 | 0o4000 | 0o2000000, 0),
                   (AF['INET'], 1, 0), (AF['INET'], 2, 17), (AF['INET'], 3, 1),
                   (AF['INET6'], 1, 0), (AF['INET6'], 2, 17)]
        for args in allowed:
            with self.subTest(args=args):
                self.assertAll(self.decide('socket', args), ALLOW, f'socket{args}')
        closed = [name for name in AF if name not in ('UNIX', 'INET', 'INET6', 'NETLINK')]
        for name in closed:
            with self.subTest(family=name):
                self.assertAll(self.decide('socket', (AF[name], 3, 0)), EPERM, f'AF_{name}')
        self.assertAll(self.decide('socket', (0, 1, 0)), EPERM, 'AF_UNSPEC')

    def test_socket_netlink_only_route_and_uevent(self):
        for name, proto in NETLINK.items():
            expected = ALLOW if name in ('ROUTE', 'KOBJECT_UEVENT') else EPERM
            with self.subTest(protocol=name):
                self.assertAll(self.decide('socket', (AF['NETLINK'], 3, proto)), expected, f'NETLINK_{name}')
        self.assertAll(self.decide('socket', (AF['NETLINK'], 3, 12)), EPERM, 'AF_NETLINK, NETLINK_NETFILTER (12)')
        self.assertAll(self.decide('socket', (AF['NETLINK'], 3, 0)), ALLOW, 'AF_NETLINK, NETLINK_ROUTE')
        # SOCK_NONBLOCK|SOCK_CLOEXEC в type не мешают: правило смотрит только family и protocol.
        self.assertAll(self.decide('socket', (AF['NETLINK'], 3 | 0o4000 | 0o2000000, 0)), ALLOW, 'netlink с флагами')

    def test_socketcall_multiplexer_is_gone(self):
        self.assertAll(self.decide('socketcall', (1, 0)), EPERM, 'socketcall')
        for rule in self.profile['syscalls']:
            self.assertNotIn('socketcall', rule['names'])

    def test_socketpair_and_other_socket_calls_still_work(self):
        for name in ('socketpair', 'connect', 'bind', 'listen', 'accept4', 'sendto', 'recvfrom', 'getsockopt',
                     'setsockopt', 'sendmsg', 'recvmsg', 'shutdown', 'getsockname', 'getpeername'):
            self.assertAll(self.decide(name), ALLOW, name)

    def test_chromium_basics_remain_allowed(self):
        for name in ('futex', 'mmap', 'mprotect', 'munmap', 'openat', 'read', 'write', 'execve', 'prctl', 'seccomp',
                     'getrandom', 'epoll_wait', 'memfd_create', 'setresuid', 'setresgid', 'capset', 'capget',
                     'unlinkat', 'eventfd2', 'pipe2', 'sched_getaffinity', 'rt_sigaction', 'set_robust_list',
                     'ptrace', 'process_vm_readv'):
            self.assertAll(self.decide(name), ALLOW, name)

    def test_x86_only_syscalls_follow_arch_rules(self):
        self.assertEqual(decide(self.profile, 'arch_prctl', arch='amd64'), ALLOW)
        self.assertEqual(decide(self.profile, 'arch_prctl', arch='arm64'), EPERM)

    def test_defaults_keep_the_safe_baseline(self):
        self.assertEqual(self.profile['defaultAction'], 'SCMP_ACT_ERRNO')
        self.assertEqual(self.profile['defaultErrnoRet'], 1)


class SockoptTests(unittest.TestCase):
    """Опции netfilter закрыты на SOL_IP (0) и SOL_IPV6 (41), всё остальное работает (CVE-2021-22555)."""

    SOL_SOCKET, SOL_TCP, SOL_UDP = 1, 6, 17
    SET_BAD = (64, 65, 96, 97, 128, 129)
    GET_BAD = {0: (64, 65, 66, 67, 96, 97, 98, 99, 128, 129, 130, 131),
               41: (64, 65, 68, 69, 96, 97, 98, 99, 128, 129, 130, 131)}

    @classmethod
    def setUpClass(cls):
        cls.profile = json.loads((SECCOMP / 'bot.json').read_text())
        # Только правила sockopt: перебор значений не должен проходить по всему профилю.
        cls.trimmed = {**cls.profile, 'syscalls': [r for r in cls.profile['syscalls']
                                                   if {'setsockopt', 'getsockopt'} & set(r['names'])]}

    def decide(self, name, level, optname, arch='amd64'):
        return decide(self.trimmed, name, (5, level, optname), arch=arch)

    def test_no_unconditional_rule_for_sockopt_calls(self):
        for rule in self.profile['syscalls']:
            if {'setsockopt', 'getsockopt'} & set(rule['names']):
                self.assertTrue(rule.get('args'), rule['names'])
                self.assertEqual(rule['action'], 'SCMP_ACT_ALLOW')

    def test_setsockopt_netfilter_options_are_refused_on_ip_and_ipv6(self):
        for arch in ARCHES:
            for level in (0, 41):
                for optname in self.SET_BAD:
                    with self.subTest(arch=arch, level=level, optname=optname):
                        self.assertEqual(self.decide('setsockopt', level, optname, arch), EPERM)

    def test_getsockopt_netfilter_options_are_refused(self):
        for arch in ARCHES:
            for level, bad in self.GET_BAD.items():
                for optname in bad:
                    with self.subTest(arch=arch, level=level, optname=optname):
                        self.assertEqual(self.decide('getsockopt', level, optname, arch), EPERM)

    def test_ipv6_tclass_options_stay_open_for_getsockopt_and_setsockopt(self):
        # IPV6_RECVTCLASS = 66 и IPV6_TCLASS = 67 в linux/in6.h; у IPT_SO_GET_REVISION_* те же номера, но на SOL_IP.
        for call in ('getsockopt', 'setsockopt'):
            for optname in (66, 67):
                self.assertEqual(self.decide(call, 41, optname), ALLOW, f'{call} SOL_IPV6 {optname}')
        for optname in (66, 67):
            self.assertEqual(self.decide('getsockopt', 0, optname), EPERM, f'getsockopt SOL_IP {optname}')
            self.assertEqual(self.decide('setsockopt', 0, optname), ALLOW, f'setsockopt SOL_IP {optname}')

    def test_ordinary_ip_and_ipv6_options_work(self):
        for call in ('setsockopt', 'getsockopt'):
            for level in (0, 41):
                for optname in (*range(0, 64), *range(66, 96), *range(98, 128)):
                    if call == 'getsockopt' and optname in self.GET_BAD[level]:
                        continue
                    with self.subTest(call=call, level=level, optname=optname):
                        self.assertEqual(self.decide(call, level, optname), ALLOW)
        self.assertEqual(self.decide('setsockopt', 0, 1), ALLOW, 'IP_TOS')
        self.assertEqual(self.decide('setsockopt', 41, 26), ALLOW, 'IPV6_V6ONLY')
        self.assertEqual(self.decide('getsockopt', 41, 80), ALLOW, 'IP6T_SO_ORIGINAL_DST')

    def test_other_levels_are_open_for_every_option(self):
        # SOL_SOCKET: SO_REUSEADDR (2), SO_TIMESTAMPING_NEW (65), SO_RCVTIMEO_NEW (66); TCP; UDP; SOL_NETLINK (270).
        for level in (self.SOL_SOCKET, self.SOL_TCP, self.SOL_UDP, 58, 132, 136, 255, 263, 270, 282):
            for optname in (0, 1, 2, 13, 63, 64, 65, 66, 67, 96, 97, 128, 129, 130, 200, 1000, 0x7fffffff):
                for call in ('setsockopt', 'getsockopt'):
                    with self.subTest(call=call, level=level, optname=optname):
                        self.assertEqual(self.decide(call, level, optname), ALLOW)

    def test_so_reuseaddr_works(self):
        for arch in ARCHES:
            self.assertEqual(self.decide('setsockopt', 1, 2, arch), ALLOW)

    def test_wide_register_values_do_not_dodge_the_filter(self):
        # Ядро берёт level и optname как int (младшие 32 бита); seccomp видит весь регистр.
        wide = 1 << 32
        for arch in ARCHES:
            for level in (0, 41):
                for optname in self.SET_BAD:
                    for call_level, call_opt in ((level | wide, optname), (level, optname | wide),
                                                 (level | wide, optname | wide), (level | (0xffff << 32), optname)):
                        with self.subTest(arch=arch, level=call_level, optname=call_opt):
                            self.assertEqual(self.decide('setsockopt', call_level, call_opt, arch), EPERM)
                for optname in self.GET_BAD[level]:
                    with self.subTest(arch=arch, level=level | wide, optname=optname):
                        self.assertEqual(self.decide('getsockopt', level | wide, optname, arch), EPERM)
                        self.assertEqual(self.decide('getsockopt', level, optname | wide, arch), EPERM)

    def test_property_nothing_the_kernel_would_treat_as_netfilter_is_allowed(self):
        highs = (0, 1, 0xffff, 0x7fffffff, 0xffffffff)
        levels = [0, 1, 6, 17, 41, 42, 58, 255, 0x80000000, 0xffffffff]
        optnames = [*range(0, 140), 255, 256, 1000, 0x80000000, 0xffffffff]
        for call, bad_for in (('setsockopt', lambda level: self.SET_BAD), ('getsockopt', lambda level: self.GET_BAD.get(level, ()))):
            for hi_l in highs:
                for hi_o in highs:
                    for lo_l in levels:
                        for lo_o in optnames:
                            level, optname = (hi_l << 32) | lo_l, (hi_o << 32) | lo_o
                            if lo_l in (0, 41) and lo_o in bad_for(lo_l):
                                self.assertEqual(self.decide(call, level, optname), EPERM, (call, level, optname))
        # Те же значения без старших битов дают ожидаемое: закрыто ровно набор netfilter на двух уровнях.
        for lo_l in levels:
            for lo_o in range(0, 140):
                closed = lo_l in (0, 41) and lo_o in self.SET_BAD
                self.assertEqual(self.decide('setsockopt', lo_l, lo_o) == EPERM, closed or (lo_l in (0, 41) and lo_o >= 128), (lo_l, lo_o))

    def test_ip_levels_refuse_options_above_127_without_losing_native_ones(self):
        # На SOL_IP и SOL_IPV6 нативных опций выше 99 нет: правила короче, а 128 и выше закрыты целиком.
        for level in (0, 41):
            for optname in (130, 131, 200, 255, 1000):
                self.assertEqual(self.decide('setsockopt', level, optname), EPERM)

    def test_rule_count_stays_small_enough_for_the_bpf_limit(self):
        count = sum(len(r['names']) for r in self.trimmed['syscalls'])
        self.assertLessEqual(count, 130, 'профиль раздувает BPF: лимит ядра 4096 инструкций на фильтр')

    def test_cover_is_exact_on_a_small_universe(self):
        import itertools
        for denied in ({0}, {3}, {1, 6}, {0, 5, 7}, set(itertools.chain(range(4, 8)))):
            cover = build_profile._cover(denied, 4)
            hit = [0] * 16
            for prefix, free in cover:
                for value in range(prefix, prefix + (1 << free)):
                    hit[value] += 1
            self.assertEqual([h for h in hit], [0 if v in denied else 1 for v in range(16)], denied)


class ChrootVariantTests(unittest.TestCase):
    def test_chroot_is_open_by_default_and_closed_with_the_flag(self):
        base = json.loads((SECCOMP / 'moby-default.json').read_text())
        fallback = build_profile.build(base)
        default = build_profile.build(base, allow_chroot=False)
        for arch in ARCHES:
            self.assertEqual(decide(default, 'chroot', arch=arch), EPERM)
            self.assertEqual(decide(fallback, 'chroot', arch=arch), ALLOW)
            self.assertEqual(decide(fallback, 'setns', arch=arch), EPERM)
            self.assertEqual(decide(fallback, 'mount', arch=arch), EPERM)


class DeploymentWiringTests(unittest.TestCase):
    """Профиль доходит до лаунчера единственным путём: bind mount в compose, путь в launcher.toml."""

    def test_launcher_toml_points_to_the_mounted_path(self):
        config = tomllib.loads((REPO / 'deploy' / 'launcher.toml').read_text())
        self.assertEqual(config['seccomp']['profile'], '/etc/bothub-launcher/seccomp-bot.json')

    def test_compose_mounts_the_generated_profile_read_only(self):
        compose = (REPO / 'deploy' / 'docker-compose.yml').read_text()
        self.assertIn('./seccomp/bot.json:/etc/bothub-launcher/seccomp-bot.json:ro', compose)

    def test_the_mounted_file_is_a_valid_profile(self):
        data = json.loads((SECCOMP / 'bot.json').read_text())
        self.assertTrue(data['defaultAction'].startswith('SCMP_ACT_'))


class NoticeTests(unittest.TestCase):
    def test_notice_credits_the_moby_profile(self):
        notice = (REPO / 'NOTICE').read_text()
        self.assertIn('moby', notice.lower())
        self.assertIn('Apache', notice)
        self.assertIn('deploy/seccomp/moby-default.json', notice)


if __name__ == '__main__':
    unittest.main()

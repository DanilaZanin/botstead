"""bot-guard: таблица номеров вызовов, программа BPF (собирается и исполняется в Python-интерпретаторе BPF),
сборка образа и цепочка запуска. Docker не нужен.

Программу фильтра печатает сам bot-guard.c при сборке с -DBOT_GUARD_DUMP. На Linux тест берёт настоящие
заголовки ядра, на macOS подставляет abi_stubs/ (те же значения ABI). Без компилятора проверки BPF пропускаются,
таблица номеров проверяется статически всегда."""
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'bot-guard.c'
STUBS = ROOT / 'tests' / 'abi_stubs'

ALLOW = 0x7fff0000
KILL_PROCESS = 0x80000000
EPERM, ENOSYS = 1, 38


def errno_ret(code):
    return 0x00050000 | code


CLONE_NEWUSER = 0x10000000
CLONE_NEWNET = 0x40000000
CLONE_NEWPID = 0x20000000
CLONE_VM = 0x100
CLONE_THREAD = 0x10000
SIGCHLD = 17
X32_BIT = 0x40000000

# Номера вызовов из linux/include/uapi/asm-generic/unistd.h (aarch64) и arch/x86/entry/syscalls/syscall_64.tbl.
ARCHES = {
    'x86_64': dict(define='BOT_GUARD_TARGET_X86_64', audit=0xC000003E, unshare=272, setns=308, clone=56, clone3=435,
                   seccomp=317, other_audit=0xC00000B7, read=0, write=1, execve=59, mount=165),
    'aarch64': dict(define='BOT_GUARD_TARGET_AARCH64', audit=0xC00000B7, unshare=97, setns=268, clone=220, clone3=435,
                    seccomp=277, other_audit=0xC000003E, read=63, write=64, execve=221, mount=40),
}
AUDIT_ARCH_I386 = 0x40000003
AUDIT_ARCH_ARM = 0x40000028


def run_bpf(program, *, arch, nr, args=()):
    """Классический BPF для seccomp: только те инструкции, которые использует bot-guard."""
    data = struct.pack('<IIQ6Q', nr & 0xFFFFFFFF, arch, 0, *(list(args) + [0] * 6)[:6])
    acc = 0
    pc = 0
    for _ in range(1000):
        code, jt, jf, k = program[pc]
        klass = code & 0x07
        if klass == 0x00:  # BPF_LD | BPF_W | BPF_ABS
            assert code == 0x20, hex(code)
            acc = struct.unpack_from('<I', data, k)[0]
            pc += 1
        elif klass == 0x05:  # BPF_JMP | op | BPF_K
            op = code & 0xF0
            assert code & 0x08 == 0, 'только константы'
            if op == 0x10:
                cond = acc == k
            elif op == 0x40:
                cond = bool(acc & k)
            else:
                raise AssertionError(f'неожиданная операция перехода {hex(code)}')
            pc += 1 + (jt if cond else jf)
        elif klass == 0x06:  # BPF_RET | BPF_K
            assert code == 0x06
            return k
        else:
            raise AssertionError(f'неожиданная инструкция {hex(code)}')
        assert pc < len(program), 'переход за конец программы'
    raise AssertionError('программа не завершилась')


def compiler():
    return shutil.which('cc') or shutil.which('gcc') or shutil.which('clang')


def dump_program(cc, arch, tmp):
    """Собрать bot-guard.c с -DBOT_GUARD_DUMP для архитектуры arch и вернуть программу как список кортежей."""
    out = Path(tmp) / f'bot-guard-dump-{arch}'
    base = [cc, f'-D{ARCHES[arch]["define"]}', '-DBOT_GUARD_DUMP', '-Wall', '-Wextra', '-Werror',
            '-Wno-deprecated-declarations', '-o', str(out), str(SOURCE)]
    if sys.platform.startswith('linux'):
        attempts = [base, [*base[:1], f'-I{STUBS}', *base[1:]]]
    else:
        attempts = [[*base[:1], f'-I{STUBS}', *base[1:]]]
    errors = []
    for cmd in attempts:
        built = subprocess.run(cmd, capture_output=True, text=True)
        if built.returncode == 0:
            break
        errors.append(built.stderr)
    else:
        raise unittest.SkipTest('bot-guard.c не собирается без заголовков ядра: ' + errors[-1][:200])
    result = subprocess.run([str(out)], capture_output=True, text=True, check=True)
    return [tuple(int(x) for x in line.split()) for line in result.stdout.splitlines()]


class StaticTableTests(unittest.TestCase):
    """Номера вызовов и константы читаются из исходника: ошибка в номере молча открыла бы обход фильтра."""

    @classmethod
    def setUpClass(cls):
        cls.source = SOURCE.read_text()

    def block(self, arch):
        pattern = {
            'x86_64': r'#if defined\(BOT_GUARD_TARGET_X86_64\).*?\n(.*?)#elif',
            'aarch64': r'#elif defined\(BOT_GUARD_TARGET_AARCH64\).*?\n(.*?)#else',
        }[arch]
        match = re.search(pattern, self.source, re.S)
        self.assertIsNotNone(match, arch)
        return match.group(1)

    def define(self, block, name):
        match = re.search(rf'#define {name} (0x[0-9A-Fa-f]+|\d+)u?\b', block)
        self.assertIsNotNone(match, name)
        return int(match.group(1), 0)

    def test_syscall_numbers_and_audit_arch_match_the_kernel_tables(self):
        for arch, expected in ARCHES.items():
            block = self.block(arch)
            with self.subTest(arch=arch):
                self.assertEqual(self.define(block, 'GUARD_AUDIT_ARCH'), expected['audit'])
                for name in ('unshare', 'setns', 'clone', 'clone3', 'seccomp'):
                    self.assertEqual(self.define(block, f'NR_{name.upper()}'), expected[name], name)

    def test_x32_bit_only_on_x86_64(self):
        self.assertEqual(self.define(self.block('x86_64'), 'X32_SYSCALL_BIT'), X32_BIT)
        self.assertNotIn('X32_SYSCALL_BIT', self.block('aarch64'))

    def test_clone_newuser_constant(self):
        self.assertEqual(self.define(self.source, 'CLONE_NEWUSER_FLAG'), CLONE_NEWUSER)

    def test_source_installs_filter_or_refuses_to_exec(self):
        main = self.source[self.source.index('int main('):]
        self.assertIn('prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)', main)
        self.assertIn('SECCOMP_SET_MODE_FILTER', main)
        self.assertLess(main.index('PR_SET_NO_NEW_PRIVS'), main.index('SECCOMP_SET_MODE_FILTER'))
        self.assertLess(main.index('SECCOMP_SET_MODE_FILTER'), main.index('execv('))
        self.assertIn("argv[1][0] != '/'", main)  # только абсолютный путь
        self.assertIn('execv(argv[1], argv + 1)', main)
        # Любая ошибка установки фильтра: die() выходит ненулевым кодом до execv.
        self.assertEqual(main.count('die('), 2)
        self.assertIn('_exit(EXIT_GUARD_FAILED)', self.source)
        self.assertRegex(self.source, r'#define EXIT_GUARD_FAILED [1-9]')

    def test_filter_kills_foreign_architectures(self):
        self.assertIn('jump(BPF_JEQ, GUARD_AUDIT_ARCH, NEXT, L_KILL)', self.source)
        self.assertIn('SECCOMP_RET_KILL_PROCESS', self.source)


class FilterProgramTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cc = compiler()
        if cls.cc is None:
            raise unittest.SkipTest('нет компилятора C: программа BPF проверяется на сборке образа и на стенде')
        cls.tmp = tempfile.TemporaryDirectory()
        cls.programs = {}
        for arch in ARCHES:
            try:
                cls.programs[arch] = dump_program(cls.cc, arch, cls.tmp.name)
            except unittest.SkipTest as exc:
                cls.skip_reason = str(exc)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def each_arch(self):
        if not self.programs:
            self.skipTest(getattr(self, 'skip_reason', 'программа не собралась'))
        yield from self.programs.items()

    def verdict(self, arch, name, *args, nr=None):
        spec = ARCHES[arch]
        return run_bpf(self.programs[arch], arch=spec['audit'], nr=spec[name] if nr is None else nr, args=args)

    def test_programs_are_short_and_forward_only(self):
        for arch, program in self.each_arch():
            with self.subTest(arch=arch):
                self.assertLess(len(program), 40)
                for index, (code, jt, jf, k) in enumerate(program):
                    if code & 0x07 == 0x05:
                        self.assertLess(index + 1 + max(jt, jf), len(program))

    def test_unshare_and_setns_get_eperm_whatever_the_arguments(self):
        for arch, _ in self.each_arch():
            for name in ('unshare', 'setns'):
                for arg0 in (0, CLONE_NEWUSER, 0xFFFFFFFFFFFFFFFF, CLONE_NEWNET):
                    with self.subTest(arch=arch, name=name, arg0=hex(arg0)):
                        self.assertEqual(self.verdict(arch, name, arg0), errno_ret(EPERM))

    def test_clone3_is_enosys(self):
        for arch, _ in self.each_arch():
            with self.subTest(arch=arch):
                self.assertEqual(self.verdict(arch, 'clone3', CLONE_NEWUSER), errno_ret(ENOSYS))
                self.assertEqual(self.verdict(arch, 'clone3', 0), errno_ret(ENOSYS))

    def test_clone_with_newuser_is_eperm_other_clone_is_allowed(self):
        for arch, _ in self.each_arch():
            denied = [CLONE_NEWUSER | SIGCHLD, CLONE_NEWUSER | CLONE_NEWNET | CLONE_NEWPID | SIGCHLD,
                      CLONE_NEWUSER, 0xFFFFFFFF, (1 << 63) | CLONE_NEWUSER, (0xABCD << 32) | CLONE_NEWUSER | SIGCHLD]
            allowed = [0, SIGCHLD, CLONE_VM | CLONE_THREAD | 0x100 | 0x200, 0x3D0F00,  # pthread_create
                       (1 << 32), (CLONE_NEWUSER << 4) & 0xFFFFFFFFFFFFFFFF]
            for flags in denied:
                with self.subTest(arch=arch, flags=hex(flags)):
                    self.assertEqual(self.verdict(arch, 'clone', flags), errno_ret(EPERM))
            for flags in allowed:
                with self.subTest(arch=arch, flags=hex(flags)):
                    self.assertEqual(self.verdict(arch, 'clone', flags), ALLOW)

    def test_clone_newnet_newpid_without_newuser_are_left_to_the_kernel(self):
        # Без CLONE_NEWUSER ядро требует CAP_SYS_ADMIN, а у бота её нет (cap-drop ALL): фильтр их не трогает.
        for arch, _ in self.each_arch():
            for flags in (CLONE_NEWNET, CLONE_NEWPID, CLONE_NEWNET | CLONE_NEWPID | SIGCHLD):
                with self.subTest(arch=arch, flags=hex(flags)):
                    self.assertEqual(self.verdict(arch, 'clone', flags), ALLOW)

    def test_ordinary_syscalls_are_allowed(self):
        for arch, _ in self.each_arch():
            for name in ('read', 'write', 'execve', 'mount', 'seccomp'):
                with self.subTest(arch=arch, name=name):
                    self.assertEqual(self.verdict(arch, name, CLONE_NEWUSER), ALLOW)  # остальное решает профиль Docker

    def test_foreign_architecture_is_killed(self):
        for arch, _ in self.each_arch():
            spec = ARCHES[arch]
            for audit in (spec['other_audit'], AUDIT_ARCH_I386, AUDIT_ARCH_ARM, 0):
                with self.subTest(arch=arch, audit=hex(audit)):
                    self.assertEqual(run_bpf(self.programs[arch], arch=audit, nr=spec['read']), KILL_PROCESS)
                    self.assertEqual(run_bpf(self.programs[arch], arch=audit, nr=spec['unshare']), KILL_PROCESS)

    def test_x32_numbers_are_killed_on_x86_64_only(self):
        program = self.programs.get('x86_64')
        if program is None:
            self.skipTest('x86_64 не собрался')
        spec = ARCHES['x86_64']
        for nr in (X32_BIT | spec['read'], X32_BIT | spec['unshare'], X32_BIT | 1000):
            with self.subTest(nr=hex(nr)):
                self.assertEqual(run_bpf(program, arch=spec['audit'], nr=nr), KILL_PROCESS)

    def test_x32_bit_check_absent_on_aarch64(self):
        program = self.programs.get('aarch64')
        if program is None:
            self.skipTest('aarch64 не собрался')
        self.assertNotIn(X32_BIT, [k for code, _, _, k in program if code == 0x45])


@unittest.skipUnless(sys.platform.startswith('linux') and compiler(), 'нужны Linux и компилятор')
class RealBinaryTests(unittest.TestCase):
    """Настоящий статический бинарник на Linux: ставит фильтр и запускает программу."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.binary = Path(cls.tmp.name) / 'bot-guard'
        built = subprocess.run([compiler(), '-O2', '-Wall', '-Wextra', '-Werror', '-o', str(cls.binary), str(SOURCE)],
                               capture_output=True, text=True)
        if built.returncode != 0:
            raise unittest.SkipTest('bot-guard.c не собирается на этом хосте: ' + built.stderr[:200])

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_guard(self, *args):
        return subprocess.run([str(self.binary), *args], capture_output=True, text=True, timeout=10)

    def test_relative_or_missing_program_is_refused(self):
        for args in ((), ('true',), ('./true',)):
            with self.subTest(args=args):
                self.assertEqual(self.run_guard(*args).returncode, 125)
        self.assertEqual(self.run_guard('/nonexistent/program').returncode, 127)

    def test_filter_applies_to_the_started_program(self):
        python = sys.executable
        probe = ('import ctypes, os, sys\n'
                 'libc = ctypes.CDLL(None, use_errno=True)\n'
                 'r = libc.unshare(0x10000000)\n'
                 'sys.exit(0 if r == -1 and ctypes.get_errno() == 1 else 3)\n')
        result = self.run_guard(python, '-c', probe)
        if result.returncode == 125:
            self.skipTest('seccomp запрещён в этой среде: ' + result.stderr.strip())
        self.assertEqual(result.returncode, 0, result.stderr)
        status = self.run_guard(python, '-c', 'print(open("/proc/self/status").read())').stdout
        self.assertIn('NoNewPrivs:\t1', status)
        self.assertIn('Seccomp:\t2', status)


class ImageBuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dockerfile = (ROOT / 'Dockerfile').read_text()
        cls.entrypoint = (ROOT / 'entrypoint.sh').read_text()

    def test_guard_is_built_static_in_a_separate_stage_and_installed_root_0755(self):
        stages = re.split(r'(?m)^FROM ', self.dockerfile)[1:]
        self.assertEqual(len(stages), 2)
        build, final = stages
        self.assertIn('AS guard-build', build.splitlines()[0])
        self.assertIn('gcc', build)
        self.assertIn('-static', build)
        self.assertIn('-Werror', build)
        self.assertNotIn('gcc', final.replace('--from=guard-build', ''))  # компилятора в итоговом образе нет
        self.assertIn('COPY --from=guard-build', final)
        self.assertIn('/usr/local/libexec/bot-guard', final)
        self.assertIn('chown root:root /usr/local/libexec/bot-guard', final)
        self.assertIn('chmod 0755 /usr/local/libexec/bot-guard', final)

    def test_entrypoint_runs_pid1_through_guard_by_absolute_paths(self):
        lines = [line for line in self.entrypoint.splitlines() if line.startswith('exec ')]
        self.assertEqual(lines, ["exec /usr/local/libexec/bot-guard /usr/bin/python3 -I - <<'PY'"])

    def test_browser_supervisor_has_no_guard_and_never_disables_the_sandbox(self):
        source = (ROOT / 'chromium-supervisor.sh').read_text()
        for line in source.splitlines():
            if not line.lstrip().startswith('#'):
                self.assertNotIn('bot-guard', line)
                self.assertNotIn('--no-sandbox', line)
                self.assertNotIn('--disable-setuid-sandbox', line)
        self.assertNotIn('--no-sandbox', self.dockerfile.replace('--no-sandbox` не', ''))

    def test_no_stub_headers_leak_into_the_image(self):
        self.assertNotIn('abi_stubs', self.dockerfile)


if __name__ == '__main__':
    unittest.main()

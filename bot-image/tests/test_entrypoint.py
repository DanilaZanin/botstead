"""Docker-free browser image checks."""
from pathlib import Path
import json
import os
import re
import signal
import select
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


def write_flock_stub(bindir):
    flock = bindir / 'flock'
    flock.write_text('''#!/usr/bin/env python3
import fcntl, sys
try:
    fcntl.flock(int(sys.argv[-1]), fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    raise SystemExit(1)
''')
    flock.chmod(0o755)


def write_empty_process_tools(bindir):
    for name in ('pkill', 'pgrep'):
        command = bindir / name
        command.write_text('#!/bin/sh\nexit 1\n')
        command.chmod(0o755)


def write_prctl_python_stub(bindir):
    python = bindir / 'python3'
    python.write_text(f'''#!{sys.executable}
import os, sys

args = sys.argv[1:]
if '-I' in args and not sys.flags.isolated:
    # python3 -I: изолированный режим, как у настоящего интерпретатора
    os.execv(sys.executable, [sys.executable, '-I', sys.argv[0], *args])
if not sys.flags.isolated:
    sys.path.insert(0, '')  # python3 - читает скрипт со stdin: cwd первым в sys.path
if os.environ.get('INTERPRETER_CALLS'):
    with open(os.environ['INTERPRETER_CALLS'], 'a') as calls:
        calls.write(repr((os.getcwd(), args)) + '\\n')
import ctypes

class FakeLibc:
    def prctl(self, *args):
        with open(os.environ['PRCTL_CALLS'], 'a') as calls:
            calls.write(repr(args) + '\\n')
        if os.environ.get('PRCTL_FAIL'):
            ctypes.set_errno(1)
            return -1
        return 0

ctypes.CDLL = lambda *args, **kwargs: FakeLibc()
exec(compile(sys.stdin.read(), '<entrypoint>', 'exec'), {{'__name__': '__main__'}})
''')
    python.chmod(0o755)


ENTRYPOINT_EXEC = "exec /usr/local/libexec/bot-guard /usr/bin/python3 -I - <<'PY'"


def prepare_entrypoint(bindir):
    """Копия entrypoint.sh, где абсолютные пути образа (bot-guard, python3) заменены заглушками из bindir.
    Строка с абсолютными путями проверяется в тексте оригинала: тест исполняет всё остальное как есть."""
    source = (ROOT / 'entrypoint.sh').read_text()
    assert ENTRYPOINT_EXEC in source
    guard = bindir / 'bot-guard'
    guard.write_text('#!/bin/sh\n[ -z "$GUARD_CALLS" ] || printf "%s\\n" "$*" >> "$GUARD_CALLS"\nexec "$@"\n')
    guard.chmod(0o755)
    source = source.replace('/usr/local/libexec/bot-guard', str(guard)).replace('/usr/bin/python3', str(bindir / 'python3'))
    script = bindir.parent / 'entrypoint-under-test.sh'
    script.write_text(source)
    return script


class BrowserImageTests(unittest.TestCase):
    def test_entrypoint_does_not_start_browser(self):
        source = (ROOT / 'entrypoint.sh').read_text()
        for command in ('Xvfb ', 'openbox ', 'x11vnc ', 'chromium ', 'websockify '):
            self.assertNotIn(command, source)
        self.assertIn(ENTRYPOINT_EXEC, source)
        lines = source.splitlines()
        cd_root = lines.index('cd /')
        self.assertLess(cd_root, lines.index(ENTRYPOINT_EXEC))
        commands = [i for i, line in enumerate(lines)
                    if not line.lstrip().startswith('#') and re.search(r'\b(cp|mkdir|grep|cat)\b', line)]
        self.assertTrue(commands)
        self.assertLess(cd_root, min(commands))
        self.assertNotIn('PYTHON_PID=', source)
        self.assertIn('os.waitpid(-1, os.WNOHANG)', source)

    def test_entrypoint_exits_on_sigterm(self):
        with tempfile.TemporaryDirectory() as tmp:
            bindir = Path(tmp) / 'bin'
            bindir.mkdir()
            write_prctl_python_stub(bindir)
            prctl_calls = Path(tmp) / 'prctl-calls'
            env = {**os.environ, 'HOME': tmp, 'PATH': f'{bindir}:{os.environ["PATH"]}',
                   'PRCTL_CALLS': str(prctl_calls)}
            proc = subprocess.Popen(['bash', str(prepare_entrypoint(bindir))], env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                ready, _, _ = select.select([proc.stdout], [], [], 3)
                self.assertTrue(ready)
                self.assertEqual(proc.stdout.readline(), 'Bot image ready.\n')
                self.assertEqual(prctl_calls.read_text().splitlines(), ['(4, 0, 0, 0, 0)'])
                proc.send_signal(signal.SIGTERM)
                stdout, stderr = proc.communicate(timeout=3)
                self.assertEqual(proc.returncode, 0, (stdout, stderr))
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=3)

    def test_pid1_is_isolated_from_files_the_bot_writes_in_its_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bindir = root / 'bin'
            workdir = root / 'workdir'  # как /home/bot: рабочий каталог контейнера, в нём пишет бот
            pythonpath = root / 'pythonpath'
            home = root / 'home'
            for directory in (bindir, workdir, pythonpath, home):
                directory.mkdir()
            write_prctl_python_stub(bindir)
            for directory, name in ((workdir, 'ctypes.py'), (workdir, 'signal.py'), (pythonpath, 'ctypes.py')):
                marker = root / f'pwned-{directory.name}-{name}'
                (directory / name).write_text(f'open({str(marker)!r}, "w").close()\n')
            interpreter_calls = root / 'interpreter-calls'
            env = {**os.environ, 'HOME': str(home), 'PATH': f'{bindir}:{os.environ["PATH"]}',
                   'PRCTL_CALLS': str(root / 'prctl-calls'), 'INTERPRETER_CALLS': str(interpreter_calls),
                   'PYTHONPATH': str(pythonpath)}
            proc = subprocess.Popen(['bash', str(prepare_entrypoint(bindir))], env=env, cwd=workdir,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                ready, _, _ = select.select([proc.stdout], [], [], 3)
                self.assertTrue(ready)
                self.assertEqual(proc.stdout.readline(), 'Bot image ready.\n')
                cwd, args = eval(interpreter_calls.read_text().splitlines()[0])
                self.assertEqual(Path(cwd).resolve(), Path('/').resolve())
                self.assertEqual(args, ['-I', '-'])
                self.assertEqual(sorted(p.name for p in root.glob('pwned-*')), [])
                proc.send_signal(signal.SIGTERM)
                stdout, stderr = proc.communicate(timeout=3)
                self.assertEqual(proc.returncode, 0, (stdout, stderr))
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=3)

    def test_entrypoint_fails_closed_when_dumpability_cannot_be_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            bindir = Path(tmp) / 'bin'
            bindir.mkdir()
            write_prctl_python_stub(bindir)
            prctl_calls = Path(tmp) / 'prctl-calls'
            env = {**os.environ, 'HOME': tmp, 'PATH': f'{bindir}:{os.environ["PATH"]}',
                   'PRCTL_CALLS': str(prctl_calls), 'PRCTL_FAIL': '1'}
            result = subprocess.run(
                ['bash', str(prepare_entrypoint(bindir))], env=env, text=True,
                capture_output=True, timeout=3,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('Bot image ready.', result.stdout)
            self.assertEqual(prctl_calls.read_text().splitlines(), ['(4, 0, 0, 0, 0)'])

    def test_whole_entrypoint_runs_under_bot_guard(self):
        source = (ROOT / 'Dockerfile').read_text()
        entry = re.findall(r'(?m)^ENTRYPOINT (.*)$', source)
        self.assertEqual(entry, ['["/usr/local/libexec/bot-guard", "/usr/local/bin/entrypoint.sh"]'])
        self.assertEqual(re.findall(r'(?m)^CMD ', source), [])  # CMD дописал бы аргументы к bot-guard
        self.assertLess(source.index('COPY --from=guard-build'), source.index('ENTRYPOINT'))
        self.assertTrue((ROOT / 'entrypoint.sh').read_text().startswith('#!/bin/bash\n'))  # execve по шебангу

    def test_entrypoint_reads_bot_files_only_when_regular_and_with_a_time_limit(self):
        body = (ROOT / 'entrypoint.sh').read_text().split("exec /usr/local/libexec/bot-guard")[0]
        self.assertIn('[ ! -f "$CODEX_CONFIG" ]', body)
        for line in body.splitlines():
            if not line.lstrip().startswith('#') and re.search(r'\b(grep|cp)\b', line):
                self.assertIn('timeout ', line, line)

    def _run_entrypoint(self, prepare_home):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bindir = root / 'bin'
            home = root / 'home'
            bindir.mkdir()
            home.mkdir()
            write_prctl_python_stub(bindir)
            prepare_home(home)
            env = {**os.environ, 'HOME': str(home), 'PATH': f'{bindir}:{os.environ["PATH"]}',
                   'PRCTL_CALLS': str(root / 'prctl-calls'), 'BOTHUB_URL': 'http://core:8080', 'BOTHUB_TOKEN': 'tok'}
            proc = subprocess.Popen(['bash', str(prepare_entrypoint(bindir))], env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                ready, _, _ = select.select([proc.stdout], [], [], 8)
                self.assertTrue(ready, 'entrypoint завис на файле бота')
                self.assertEqual(proc.stdout.readline(), 'Bot image ready.\n')
                config = home / '.codex' / 'config.toml'
                return config.read_text() if config.is_file() else None
            finally:
                proc.kill()
                proc.wait(timeout=3)

    def test_fifo_in_place_of_codex_config_does_not_block_startup(self):
        def prepare(home):
            (home / '.codex').mkdir()
            os.mkfifo(home / '.codex' / 'config.toml')
        self.assertIsNone(self._run_entrypoint(prepare))

    def test_symlink_to_fifo_does_not_block_startup(self):
        def prepare(home):
            (home / '.codex').mkdir()
            os.mkfifo(home / 'pipe')
            os.symlink(home / 'pipe', home / '.codex' / 'config.toml')
        self.assertIsNone(self._run_entrypoint(prepare))

    def test_codex_config_is_written_once(self):
        config = self._run_entrypoint(lambda home: None)
        self.assertEqual(config.count('[mcp_servers.bothub]'), 1)
        self.assertIn('BOTHUB_URL = "http://core:8080"', config)

        def prepare(home):
            (home / '.codex').mkdir()
            (home / '.codex' / 'config.toml').write_text('[mcp_servers.bothub]\ncommand = "own"\n')
        self.assertEqual(self._run_entrypoint(prepare), '[mcp_servers.bothub]\ncommand = "own"\n')

    def test_chromium_policy_and_user(self):
        source = (ROOT / 'Dockerfile').read_text()
        self.assertIn('-u 1001 browser', source)
        self.assertIn('COPY bot-image/chromium-policy.json /etc/chromium/policies/managed/bothub.json', source)
        self.assertNotIn('websockify', source)
        policy = json.loads((ROOT / 'chromium-policy.json').read_text())
        for key in ('PasswordManagerEnabled', 'AutofillAddressEnabled', 'AutofillCreditCardEnabled'):
            self.assertIs(policy[key], False)
        self.assertEqual(policy['ExtensionInstallBlocklist'], ['*'])
        self.assertEqual(policy['NativeMessagingBlocklist'], ['*'])
        self.assertEqual(policy['DownloadRestrictions'], 3)  # 3: все загрузки закрыты
        self.assertEqual(policy['DefaultDownloadDirectory'], '/home/browser/Downloads')
        self.assertFalse(policy['DefaultDownloadDirectory'].startswith(('/home/bot', '/tmp', '/run')))

    def test_chromium_policy_closes_search_suggestions_sync_metrics_signin_and_local_pages(self):
        policy = json.loads((ROOT / 'chromium-policy.json').read_text())
        for key in ('SearchSuggestEnabled', 'DefaultSearchProviderEnabled', 'AutofillAddressEnabled',
                    'AutofillCreditCardEnabled', 'PasswordManagerEnabled', 'MetricsReportingEnabled'):
            self.assertIs(policy[key], False, key)
        self.assertIs(policy['SyncDisabled'], True)
        self.assertEqual(policy['BrowserSignin'], 0)
        self.assertEqual(sorted(policy['URLBlocklist']), ['chrome://*', 'file://*'])
        # Playwright over CDP and the human's first tab open about:blank and http(s): none of them is blocked, and no
        # exception list or allow list reopens the blocked schemes.
        for pattern in policy['URLBlocklist']:
            self.assertTrue(pattern.startswith(('file://', 'chrome://')), pattern)
        self.assertNotIn('about:blank', ' '.join(policy['URLBlocklist']))
        self.assertNotIn('URLAllowlist', policy)
        for key, value in policy.items():
            if key not in ('URLBlocklist', 'ExtensionInstallBlocklist', 'NativeMessagingBlocklist'):
                self.assertNotIsInstance(value, list, key)

    def test_chromium_policy_sends_a_fresh_browser_to_about_blank_not_to_a_blocked_new_tab_page(self):
        # chrome://* закрыт URLBlocklist, а страница новой вкладки по умолчанию chrome://newtab/: свежий бот стартовал со страницей
        # ошибки, и любая процедура вставала на первом navigate. Все точки входа ведут на about:blank, URLBlocklist цел.
        policy = json.loads((ROOT / 'chromium-policy.json').read_text())
        self.assertEqual(policy['NewTabPageLocation'], 'about:blank')
        self.assertEqual(policy['HomepageLocation'], 'about:blank')
        self.assertIs(policy['HomepageIsNewTabPage'], False)
        self.assertEqual(policy['RestoreOnStartup'], 5)  # 5: страница новой вкладки; последнюю сессию не восстанавливать
        self.assertNotIn('RestoreOnStartupURLs', policy)
        self.assertEqual(sorted(policy['URLBlocklist']), ['chrome://*', 'file://*'])
        self.assertNotIn('URLAllowlist', policy)

    def test_chromium_policy_is_valid_json_with_unique_keys(self):
        def no_duplicates(pairs):
            keys = [key for key, _ in pairs]
            self.assertEqual(len(keys), len(set(keys)), keys)
            return dict(pairs)
        json.loads((ROOT / 'chromium-policy.json').read_text(), object_pairs_hook=no_duplicates)

    def test_openbox_config_has_no_execute_keys_or_menu_entries(self):
        import xml.etree.ElementTree as ET
        rc = ET.parse(ROOT / 'openbox' / 'rc.xml').getroot()
        menu = ET.parse(ROOT / 'openbox' / 'menu.xml').getroot()
        names = {el.get('name') for el in rc.iter() if el.tag.endswith('action')}
        self.assertEqual(names, {'Focus', 'Raise'})  # ни Execute, ни ShowMenu, ни Exit, ни Reconfigure
        self.assertEqual([el for el in rc.iter() if el.tag.endswith('keybind')], [])
        self.assertEqual([el for el in menu.iter() if el.tag.endswith('item')], [])
        self.assertEqual([el for el in menu.iter() if el.tag.endswith('action')], [])
        text = (ROOT / 'openbox' / 'rc.xml').read_text().replace('Execute, ', '')
        self.assertNotIn('<action name="Execute"', text)
        source = (ROOT / 'Dockerfile').read_text()
        self.assertIn('COPY bot-image/openbox/rc.xml /etc/bothub/openbox-rc.xml', source)
        self.assertIn('COPY bot-image/openbox/menu.xml /etc/bothub/openbox-menu.xml', source)
        self.assertIn('chown -R root:root /etc/bothub', source)
        self.assertIn('--config-file "$OPENBOX_RC"', (ROOT / 'chromium-supervisor.sh').read_text())

    def test_screen_is_a_unix_socket_without_a_tcp_port(self):
        source = (ROOT / 'chromium-supervisor.sh').read_text()
        self.assertNotIn('-rfbport', source)
        self.assertNotIn('-localhost', source)
        self.assertIn('UNIX-LISTEN:$VNC_SOCKET,fork,mode=0600', source)
        self.assertIn('x11vnc -inetd', source)
        self.assertIn('VNC_DIR="$HOME/.vnc"', source)

    def test_supervisor_starts_stack_and_backoff_grows(self):
        self._supervisor_starts_stack('1440x900', '1440,900')

    def test_supervisor_uses_default_screen_size(self):
        self._supervisor_starts_stack(None, '1280,800')

    def _supervisor_starts_stack(self, screen_size, expected_window):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / 'home'
            bindir = root / 'bin'
            home.mkdir()
            bindir.mkdir()
            write_flock_stub(bindir)
            write_empty_process_tools(bindir)
            calls = root / 'calls.jsonl'
            socket_dir = root / 'x11'
            source = (ROOT / 'chromium-supervisor.sh').read_text()
            source = source.replace('if [[ $(id -u) != 1001 || "$HOME" != /home/browser ]]; then', 'if false; then')
            source = source.replace('/tmp/.X11-unix', str(socket_dir)).replace('[[ -S "$socket" ]]', '[[ -e "$socket" ]]')
            script = root / 'supervisor.sh'
            script.write_text(source)
            script.chmod(0o755)
            for name in ('openbox', 'socat'):
                p = bindir / name
                p.write_text('#!/bin/sh\necho "' + name + ' $*" >> "$STACK_CALLS"\nexec /bin/sleep 60\n')
                p.chmod(0o755)
            p = bindir / 'x11vnc'  # only inside socat EXEC: the supervisor must not start it with a TCP port itself
            p.write_text('#!/bin/sh\necho "x11vnc $*" >> "$STACK_CALLS"\nexit 0\n')
            p.chmod(0o755)
            p = bindir / 'Xvfb'
            p.write_text('''#!/usr/bin/env python3
import os, time
open(os.environ['SOCKET_DIR'] + '/X99', 'w').close()
while True: time.sleep(0.1)
''')
            p.chmod(0o755)
            p = bindir / 'chromium'
            p.write_text('''#!/usr/bin/env python3
import json, os, sys
with open(os.environ['CALLS'], 'a') as f: f.write(json.dumps(['chromium', *sys.argv[1:]])+'\\n')
''')
            p.chmod(0o755)
            p = bindir / 'sleep'
            p.write_text('''#!/bin/sh
case "$1" in 1|2|4|8|16|30) echo "$1" >> "$PAUSES";; esac
exec /bin/sleep 0.01
''')
            p.chmod(0o755)
            p = bindir / 'xauth'
            p.write_text('#!/bin/sh\ntouch "$2"\n')
            p.chmod(0o755)
            env = {**os.environ, 'HOME': str(home), 'PATH': f'{bindir}:{os.environ["PATH"]}',
                   'SOCKET_DIR': str(socket_dir), 'CALLS': str(calls), 'PAUSES': str(root / 'pauses'),
                   'STACK_CALLS': str(root / 'stack-calls')}
            env.pop('BOT_BROWSER_SCREEN_SIZE', None)
            if screen_size is not None:
                env['BOT_BROWSER_SCREEN_SIZE'] = screen_size
            error_log = root / 'error.log'
            with error_log.open('w') as errors:
                proc = subprocess.Popen(['bash', str(script)], env=env, start_new_session=True,
                                        stdout=subprocess.DEVNULL, stderr=errors)
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if (root / 'pauses').exists() and len((root / 'pauses').read_text().splitlines()) >= 3:
                        break
                    time.sleep(0.05)
                pauses = (root / 'pauses').read_text().splitlines()
                self.assertEqual(pauses[:3], ['1', '2', '4'])
                self.assertTrue(calls.exists(), error_log.read_text())
                args = json.loads(calls.read_text().splitlines()[0])
                self.assertIn('--remote-debugging-address=127.0.0.1', args)
                self.assertIn('--remote-debugging-port=9222', args)
                self.assertIn(f'--window-size={expected_window}', args)
                self.assertNotIn('--no-sandbox', args)
                self.assertIn(f'--user-data-dir={home}/.config/botstead-browser', args)
                self.assertEqual((home / '.config/botstead-browser').stat().st_mode & 0o777, 0o700)
                self.assertEqual(socket_dir.stat().st_mode & 0o777, 0o700)
                stack = (root / 'stack-calls').read_text().splitlines()
                self.assertIn('openbox --config-file /etc/bothub/openbox-rc.xml', stack)
                socat = next(line for line in stack if line.startswith('socat '))
                self.assertIn(f'UNIX-LISTEN:{home}/.vnc/rfb.sock,fork,mode=0600', socat)
                self.assertIn('EXEC:x11vnc -inetd -nopw -quiet', socat)
                self.assertNotIn('-rfbport', source)  # TCP-порта у x11vnc нет
                self.assertNotIn('5900', source)
                self.assertEqual((home / '.vnc').stat().st_mode & 0o777, 0o700)
            finally:
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=5)
                    self.fail('supervisor continued after SIGTERM')
                self.assertEqual(proc.returncode, 0, error_log.read_text())

    def test_supervisor_rejects_malformed_and_out_of_range_screen_size(self):
        source = (ROOT / 'chromium-supervisor.sh').read_text()
        source = source.replace('if [[ $(id -u) != 1001 || "$HOME" != /home/browser ]]; then', 'if false; then')
        for value in ('1280X800', '0800x800', '0x800', '100x800', '4097x800', '1280x2161', '1280x800;id'):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                home = root / 'home'
                bindir = root / 'bin'
                home.mkdir()
                bindir.mkdir()
                write_flock_stub(bindir)
                script = root / 'supervisor.sh'
                script.write_text(source)
                env = {**os.environ, 'HOME': str(home), 'PATH': f'{bindir}:{os.environ["PATH"]}',
                       'BOT_BROWSER_SCREEN_SIZE': value}
                result = subprocess.run(['bash', str(script)], env=env, text=True, capture_output=True,
                                        timeout=5, check=False)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn('screen size', result.stderr)
                self.assertFalse((home / '.browser-ready').exists())
                self.assertFalse((home / '.config').exists())

    def test_supervisor_term_stops_children_without_restarting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / 'home'
            bindir = root / 'bin'
            home.mkdir()
            bindir.mkdir()
            write_flock_stub(bindir)
            socket_dir = root / 'x11'
            socket_dir.mkdir()
            child_pids = root / 'child-pids'
            chromium_calls = root / 'chromium-calls'
            xauth_calls = root / 'xauth-calls'
            stale_processes = root / 'stale-processes'
            term_sent = root / 'term-sent'
            poll_once = root / 'poll-once'
            process_kills = root / 'process-kills'
            stale_socket = socket_dir / 'X99'
            stale_x_lock = root / '.X99-lock'
            stale_processes.touch()
            stale_socket.touch()
            stale_x_lock.write_text('99999999\n')
            source = (ROOT / 'chromium-supervisor.sh').read_text()
            source = source.replace('if [[ $(id -u) != 1001 || "$HOME" != /home/browser ]]; then', 'if false; then')
            source = source.replace('/tmp/.X11-unix', str(socket_dir)).replace('[[ -S "$socket" ]]', '[[ -e "$socket" ]]')
            script = root / 'supervisor.sh'
            script.write_text(source)
            script.chmod(0o755)

            for name in ('openbox', 'socat'):
                executable = bindir / name
                executable.write_text('#!/bin/sh\necho "$$" >> "$CHILD_PIDS"\nexec /bin/sleep 60\n')
                executable.chmod(0o755)
            xvfb = bindir / 'Xvfb'
            xvfb.write_text('''#!/usr/bin/env python3
import os, time
socket = os.environ['SOCKET_DIR'] + '/X99'
if os.path.exists(socket): raise SystemExit('stale X socket was not removed')
open(socket, 'w').close()
with open(os.environ['CHILD_PIDS'], 'a') as f: f.write(str(os.getpid()) + '\\n')
while True: time.sleep(0.1)
''')
            xvfb.chmod(0o755)
            chromium = bindir / 'chromium'
            chromium.write_text('''#!/usr/bin/env python3
import os, time
with open(os.environ['CHILD_PIDS'], 'a') as f: f.write(str(os.getpid()) + '\\n')
with open(os.environ['CHROMIUM_CALLS'], 'a') as f: f.write('started\\n')
while True: time.sleep(0.1)
''')
            chromium.chmod(0o755)
            xauth = bindir / 'xauth'
            xauth.write_text('''#!/bin/sh
if [ -e "$STALE_PROCESSES" ] || [ -e "$STALE_SOCKET" ] || [ -e "$STALE_X_LOCK" ]; then
    echo 'X setup ran before stale cleanup' >> "$XAUTH_CALLS"
    exit 1
fi
printf "call\\n" >> "$XAUTH_CALLS"
printf "auth-%s\\n" "$(wc -l < "$XAUTH_CALLS")" > "$2"
''')
            xauth.chmod(0o755)
            pkill = bindir / 'pkill'
            pkill.write_text('''#!/bin/sh
printf '%s\\n' "$*" >> "$PROCESS_KILLS"
if [ "$1" = '-TERM' ]; then touch "$TERM_SENT"; fi
''')
            pkill.chmod(0o755)
            pgrep = bindir / 'pgrep'
            pgrep.write_text('''#!/bin/sh
if [ -e "$STALE_PROCESSES" ] && [ -e "$TERM_SENT" ]; then
    if [ -e "$POLL_ONCE" ]; then
        rm -f "$STALE_PROCESSES"
        exit 1
    fi
    touch "$POLL_ONCE"
    exit 0
fi
if [ -e "$STALE_PROCESSES" ]; then exit 0; fi
exit 1
''')
            pgrep.chmod(0o755)

            env = {
                **os.environ,
                'HOME': str(home),
                'PATH': f'{bindir}:{os.environ["PATH"]}',
                'SOCKET_DIR': str(socket_dir),
                'CHILD_PIDS': str(child_pids),
                'CHROMIUM_CALLS': str(chromium_calls),
                'XAUTH_CALLS': str(xauth_calls),
                'STALE_PROCESSES': str(stale_processes),
                'STALE_SOCKET': str(stale_socket),
                'STALE_X_LOCK': str(stale_x_lock),
                'TERM_SENT': str(term_sent),
                'POLL_ONCE': str(poll_once),
                'PROCESS_KILLS': str(process_kills),
            }
            error_log = root / 'error.log'
            with error_log.open('w') as errors:
                proc = subprocess.Popen(
                    ['bash', str(script)], env=env, start_new_session=True,
                    stdout=subprocess.DEVNULL, stderr=errors,
                )
            duplicate = None
            try:
                deadline = time.monotonic() + 5
                ready = home / '.browser-ready'
                while time.monotonic() < deadline and not ready.exists():
                    if proc.poll() is not None:
                        self.fail(f'supervisor exited before becoming ready: {error_log.read_text()}')
                    time.sleep(0.05)
                self.assertTrue(ready.exists(), error_log.read_text())
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    if chromium_calls.exists() and child_pids.exists() \
                            and len(child_pids.read_text().splitlines()) == 4:
                        break
                    time.sleep(0.05)
                self.assertTrue(chromium_calls.exists())
                self.assertEqual(len(child_pids.read_text().splitlines()), 4)
                self.assertEqual(chromium_calls.read_text().splitlines(), ['started'])
                self.assertFalse(stale_processes.exists())
                self.assertTrue(stale_socket.exists())
                self.assertFalse(stale_x_lock.exists())
                self.assertEqual(process_kills.read_text().splitlines(), [
                    '-TERM -u 1001 -x Xvfb',
                    '-TERM -u 1001 -x openbox',
                    '-TERM -u 1001 -x socat',
                    '-TERM -u 1001 -x x11vnc',
                    '-TERM -u 1001 -x chromium',
                    '-TERM -u 1001 -x chrome_crashpad',
                ])
                pidfile = home / '.browser-supervisor.pid'
                xauthority = home / '.Xauthority'
                original_pidfile = pidfile.read_text()
                original_xauthority = xauthority.read_text()
                self.assertEqual(xauth_calls.read_text().splitlines(), ['call'])

                with (root / 'duplicate-error.log').open('w') as errors:
                    duplicate = subprocess.Popen(
                        ['bash', str(script)], env=env, start_new_session=True,
                        stdout=subprocess.DEVNULL, stderr=errors,
                    )
                    duplicate.wait(timeout=3)
                self.assertEqual(duplicate.returncode, 0)
                self.assertIsNone(proc.poll())
                self.assertEqual(pidfile.read_text(), original_pidfile)
                self.assertEqual(xauthority.read_text(), original_xauthority)
                self.assertEqual(xauth_calls.read_text().splitlines(), ['call'])
                self.assertEqual(len(child_pids.read_text().splitlines()), 4)
                self.assertEqual(chromium_calls.read_text().splitlines(), ['started'])

                os.kill(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.fail('supervisor did not exit after SIGTERM')
                self.assertEqual(proc.returncode, 0, error_log.read_text())
                self.assertFalse(ready.exists())
                self.assertFalse((home / '.browser-supervisor.pid').exists())

                pids = [int(line) for line in child_pids.read_text().splitlines()]
                for pid in pids:
                    with self.assertRaises(ProcessLookupError):
                        os.kill(pid, 0)
                time.sleep(1.1)
                self.assertEqual(chromium_calls.read_text().splitlines(), ['started'])
            finally:
                if duplicate is not None and duplicate.poll() is None:
                    os.killpg(duplicate.pid, signal.SIGKILL)
                    duplicate.wait(timeout=5)
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()

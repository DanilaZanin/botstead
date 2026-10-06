"""Browser modes of chromium-supervisor.sh without Docker: the human gets a clean Chromium with no debugging port."""
from pathlib import Path
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
SESSION_TOKEN = '0123456789abcdef0123456789abcdef'
SCHEMA = ('CREATE TABLE cookies (creation_utc INTEGER NOT NULL, host_key TEXT NOT NULL, '
          'top_frame_site_key TEXT NOT NULL, name TEXT NOT NULL, value TEXT NOT NULL, path TEXT NOT NULL, '
          'source_scheme INTEGER NOT NULL DEFAULT 0, source_port INTEGER NOT NULL DEFAULT -1, '
          'UNIQUE (host_key, top_frame_site_key, name, path, source_scheme, source_port))')

CHROMIUM_STUB = f'''#!{sys.executable}
import json, os, sqlite3, sys, time
args = sys.argv[1:]
alive = []
if os.path.exists(os.environ['CALLS']):
    for line in open(os.environ['CALLS']):
        pid = json.loads(line)['pid']
        try:
            os.kill(pid, 0)
            alive.append(pid)
        except ProcessLookupError:
            pass
ENV_KEYS = ('HOME', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME', 'TMPDIR')
env = {{key: os.environ.get(key) for key in ENV_KEYS}}
with open(os.environ['CALLS'], 'a') as f:
    f.write(json.dumps({{'args': args, 'pid': os.getpid(), 'others_alive': alive, 'env': env,
                         't': time.time_ns()}}) + '\\n')
profile = next(a.split('=', 1)[1] for a in args if a.startswith('--user-data-dir='))
os.makedirs(profile + '/Default/Network', exist_ok=True)
if not any(a.startswith('--remote-debugging') for a in args):
    # a real Chromium writes its cache, crash dumps, XDG data, temp files and ~/.pki wherever it is told to
    spots = [a.split('=', 1)[1] for a in args if a.startswith(('--disk-cache-dir=', '--crash-dumps-dir='))]
    spots += [value for key, value in env.items() if key != 'HOME' and value]
    if env['HOME']:
        spots.append(env['HOME'] + '/.pki/nssdb')
    for spot in spots:
        os.makedirs(spot, exist_ok=True)
        open(os.path.join(spot, 'human-instance-file'), 'w').write('x')
if not any(a.startswith('--remote-debugging') for a in args):
    # the human logs in: a persistent cookie lands in the human profile
    con = sqlite3.connect(profile + '/Default/Network/Cookies')
    con.execute({SCHEMA!r})
    con.execute("INSERT OR REPLACE INTO cookies VALUES (1, '.example.com', '', 'sid', 'human-value', '/', 0, -1)")
    con.execute("INSERT OR REPLACE INTO cookies VALUES (1, '.new.test', '', 't', 'v', '/', 0, -1)")
    con.commit()
    con.close()
open(profile + '/marker', 'w').write('x')
if os.environ.get('CHROMIUM_EXIT'):
    raise SystemExit(0)
while True:
    time.sleep(0.05)
'''


XSEL_STUB = f'''#!{sys.executable}
import json, os, sys, time
alive = []
if os.path.exists(os.environ['CALLS']):
    for line in open(os.environ['CALLS']):
        pid = json.loads(line)['pid']
        try:
            os.kill(pid, 0)
            alive.append(pid)
        except ProcessLookupError:
            pass
open(os.environ['XSEL_LOG'], 'a').write(json.dumps({{'args': sys.argv[1:], 't': time.time_ns(), 'alive': alive,
                                                     'display': os.environ.get('DISPLAY')}}) + '\\n')
'''


XPROP_STUB = f'''#!{sys.executable}
import json, os, sys, time
alive = []
if os.path.exists(os.environ['CALLS']):
    for line in open(os.environ['CALLS']):
        pid = json.loads(line)['pid']
        try:
            os.kill(pid, 0)
            alive.append(pid)
        except ProcessLookupError:
            pass
open(os.environ['XPROP_LOG'], 'a').write(json.dumps({{'args': sys.argv[1:], 't': time.time_ns(), 'alive': alive,
                                                      'display': os.environ.get('DISPLAY')}}) + '\\n')
'''


def wait_until(check, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.05)
    return check()


class Supervisor:
    """Runs a copy of the supervisor with stubs for X, openbox, socat and Chromium, in a temp HOME."""

    def __init__(self, tmp, merge_timeout=None, blocked_retry=None, merge_script=None, **env):
        self.root = Path(tmp)
        self.home = self.root / 'home'
        self.bindir = self.root / 'bin'
        self.x11 = self.root / 'x11'
        self.home.mkdir(exist_ok=True)
        self.bindir.mkdir(exist_ok=True)
        self.calls = self.root / 'calls.jsonl'
        self.stack = self.root / 'stack'
        self.pauses = self.root / 'pauses'
        self.error_log = self.root / 'error.log'
        self.xsel_log = self.root / 'xsel.jsonl'
        self.xprop_log = self.root / 'xprop.jsonl'
        self.proc = None
        self.extra_env = env
        self._write_stubs()
        source = (ROOT / 'chromium-supervisor.sh').read_text()
        source = source.replace('if [[ $(id -u) != 1001 || "$HOME" != /home/browser ]]; then', 'if false; then')
        source = source.replace('/tmp/.X11-unix', str(self.x11)).replace('[[ -S "$socket" ]]', '[[ -e "$socket" ]]')
        source = source.replace('PYTHON3=/usr/bin/python3', f'PYTHON3={sys.executable}')
        source = source.replace('COOKIE_MERGE=/usr/local/libexec/cookie-merge.py',
                                f'COOKIE_MERGE={merge_script or ROOT / "cookie_merge.py"}')
        if merge_timeout is not None:
            source = source.replace('readonly MERGE_TIMEOUT=20', f'readonly MERGE_TIMEOUT={merge_timeout}')
        if blocked_retry is not None:
            source = source.replace('readonly BLOCKED_RETRY=5', f'readonly BLOCKED_RETRY={blocked_retry}')
        self.script = self.root / 'supervisor.sh'
        self.script.write_text(source)

    def _write_stubs(self):
        def stub(name, text):
            path = self.bindir / name
            path.write_text(text)
            path.chmod(0o755)
        stub('flock', f'#!{sys.executable}\nimport fcntl, sys\ntry:\n'
                      '    fcntl.flock(int(sys.argv[-1]), fcntl.LOCK_EX | fcntl.LOCK_NB)\n'
                      'except BlockingIOError:\n    raise SystemExit(1)\n')
        for name in ('pkill', 'pgrep'):
            stub(name, '#!/bin/sh\nexit 1\n')
        for name in ('openbox', 'socat'):
            stub(name, f'#!/bin/sh\necho "{name}" >> "$STACK"\nexec /bin/sleep 60\n')
        stub('Xvfb', f'#!{sys.executable}\nimport os, time\nopen(os.environ["X11"] + "/X99", "w").close()\n'
                     'open(os.environ["STACK"], "a").write("xvfb\\n")\nwhile True: time.sleep(0.1)\n')
        stub('xauth', '#!/bin/sh\ntouch "$2"\n')
        stub('chromium', CHROMIUM_STUB)
        stub('xsel', XSEL_STUB)
        stub('xprop', XPROP_STUB)
        stub('sleep', '#!/bin/sh\ncase "$1" in 1|2|4|8|16|30) echo "$1" >> "$PAUSES"; exec /bin/sleep 0.01;; esac\n'
                      'exec /bin/sleep "$@"\n')

    def start(self):
        env = {**os.environ, 'HOME': str(self.home), 'PATH': f'{self.bindir}:{os.environ["PATH"]}',
               'CALLS': str(self.calls), 'STACK': str(self.stack), 'PAUSES': str(self.pauses), 'X11': str(self.x11),
               'XSEL_LOG': str(self.xsel_log), 'XPROP_LOG': str(self.xprop_log),
               **self.extra_env}
        env.pop('BOT_BROWSER_SCREEN_SIZE', None)
        with self.error_log.open('a') as errors:
            self.proc = subprocess.Popen(['bash', str(self.script)], env=env, start_new_session=True,
                                         stdout=subprocess.DEVNULL, stderr=errors)
        return self

    def stop(self):
        if self.proc is None:
            return
        if self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
                self.proc.wait(timeout=5)
        # stray stub processes (Xvfb, chromium) die with the session
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.proc = None

    # --- state helpers ---
    def set_mode(self, mode, url=None, token=SESSION_TOKEN):
        """Same order as the launcher: the URL file first, then the mode file, both by rename. human carries the
        session token the launcher generates (`human <32 hex>`); token=None writes the old token-less form."""
        if url is not None:
            self._atomic(self.home / '.browser-human-url', url + '\n')
        line = f'{mode} {token}' if mode == 'human' and token else mode
        self._atomic(self.home / '.browser-mode', line + '\n')

    def _atomic(self, path, text):
        tmp = path.with_name(path.name + '.tmp')
        tmp.write_text(text)
        os.replace(tmp, path)

    def records(self):
        if not self.calls.exists():
            return []
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def wait_calls(self, count, timeout=10.0):
        wait_until(lambda: len(self.records()) >= count, timeout)
        records = self.records()
        assert len(records) >= count, (records, self.error_log.read_text())
        return records

    def xsel_calls(self):
        if not self.xsel_log.exists():
            return []
        return [json.loads(line) for line in self.xsel_log.read_text().splitlines()]

    def xprop_calls(self):
        if not self.xprop_log.exists():
            return []
        return [json.loads(line) for line in self.xprop_log.read_text().splitlines()]

    def stack_lines(self):
        return self.stack.read_text().splitlines() if self.stack.exists() else []

    def wait_ready_stack(self):
        wait_until(lambda: sorted(self.stack_lines()) == ['openbox', 'socat', 'xvfb'], 5)

    def active(self):
        path = self.home / '.browser-active'
        return path.read_text().split() if path.exists() else None


def has_debugging(args):
    return any(a.startswith('--remote-debugging') for a in args)


class BrowserModeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.sup = Supervisor(self._tmp.name)
        self.addCleanup(self.sup.stop)

    def test_default_mode_is_bot_with_cdp_and_basic_password_store(self):
        self.sup.start()
        args = self.sup.wait_calls(1)[0]['args']
        self.assertIn('--remote-debugging-port=9222', args)
        self.assertIn('--remote-debugging-address=127.0.0.1', args)
        self.assertIn('--password-store=basic', args)
        self.assertIn(f'--user-data-dir={self.sup.home}/.config/botstead-browser', args)
        wait_until(lambda: self.sup.active() and self.sup.active()[0] == 'bot')
        self.assertEqual(self.sup.active()[0], 'bot')

    def test_human_mode_is_clean_chromium_without_debugging_and_one_tab(self):
        self.sup.set_mode('human', 'https://example.com/login?next=1')
        self.sup.start()
        record = self.sup.wait_calls(1)[0]
        args = record['args']
        self.assertFalse(has_debugging(args), args)
        self.assertNotIn('--remote-debugging-pipe', args)
        self.assertIn(f'--user-data-dir={self.sup.home}/.config/botstead-browser-human', args)
        self.assertIn('--password-store=basic', args)
        self.assertNotIn('--no-sandbox', args)
        self.assertEqual(args[-2:], ['--', 'https://example.com/login?next=1'])
        self.assertEqual([a for a in args if not a.startswith('-')], ['https://example.com/login?next=1'])
        self.assertEqual((self.sup.home / '.config/botstead-browser-human').stat().st_mode & 0o777, 0o700)
        wait_until(lambda: self.sup.active() and self.sup.active()[0] == 'human')
        mode, pid, started = self.sup.active()
        self.assertEqual(int(pid), record['pid'])
        self.assertLessEqual(abs(int(started) - int(time.time())), 5)
        time.sleep(1)
        self.assertEqual(len(self.sup.records()), 1)  # nothing started with CDP next to it

    def test_human_url_is_revalidated_and_never_becomes_a_flag(self):
        bad = ['javascript:alert(1)', '--remote-debugging-port=9222', 'file:///etc/passwd', 'https://a b/',
               'http://x\\y/', 'chrome://settings', 'https://e.com/' + 'a' * 2100, 'data:text/html,x', '']
        for url in bad:
            with self.subTest(url=url[:40]), tempfile.TemporaryDirectory() as tmp:
                sup = Supervisor(tmp)
                try:
                    sup.set_mode('human', url)
                    sup.start()
                    args = sup.wait_calls(1)[0]['args']
                    self.assertEqual(args[-2:], ['--', 'about:blank'])
                    self.assertFalse(has_debugging(args))
                finally:
                    sup.stop()

    def test_missing_url_file_opens_about_blank(self):
        self.sup.set_mode('human')
        self.sup.start()
        self.assertEqual(self.sup.wait_calls(1)[0]['args'][-2:], ['--', 'about:blank'])

    def test_switch_to_human_and_back_restarts_only_chromium(self):
        profile = self.sup.home / '.config/botstead-browser'
        cookies = profile / 'Default/Network/Cookies'
        cookies.parent.mkdir(parents=True)
        con = sqlite3.connect(cookies)
        con.execute(SCHEMA)
        con.execute("INSERT INTO cookies VALUES (0, '.example.com', '', 'sid', 'bot-value', '/', 0, -1)")
        con.execute("INSERT INTO cookies VALUES (0, '.bot.test', '', 'only-bot', 'b', '/', 0, -1)")
        con.commit()
        con.close()
        self.sup.start()
        first = self.sup.wait_calls(1)[0]
        self.assertTrue(has_debugging(first['args']))
        self.sup.wait_ready_stack()
        (profile / 'planted-by-bot').write_text('service worker')

        self.sup.set_mode('human', 'https://example.com/')
        second = self.sup.wait_calls(2)[1]
        self.assertFalse(has_debugging(second['args']))
        self.assertEqual(second['others_alive'], [], 'old Chromium must be gone before the clean one starts')
        # Заглушка Chromium пишет marker уже после записи о вызове: под нагрузкой проверка без ожидания даёт гонку.
        wait_until(lambda: (self.sup.home / '.config/botstead-browser-human/marker').exists())
        self.assertFalse((self.sup.home / '.config/botstead-browser-human/planted-by-bot').exists())
        wait_until(lambda: self.sup.active() and self.sup.active()[0] == 'human')

        self.sup.set_mode('bot')
        third = self.sup.wait_calls(3)[2]
        self.assertTrue(has_debugging(third['args']))
        self.assertEqual(third['others_alive'], [], 'the human Chromium must be gone before the bot one starts')
        wait_until(lambda: self.sup.active() and self.sup.active()[0] == 'bot')
        self.assertFalse((self.sup.home / '.config/botstead-browser-human').exists())
        self.assertEqual((self.sup.home / '.browser-merge-status').read_text().strip(), 'merged 2')
        rows = {(h, n): v for h, n, v in sqlite3.connect(cookies).execute('SELECT host_key, name, value FROM cookies')}
        self.assertEqual(rows, {('.example.com', 'sid'): 'human-value', ('.bot.test', 'only-bot'): 'b',
                                ('.new.test', 't'): 'v'})
        self.assertTrue((profile / 'planted-by-bot').exists(), 'the bot profile itself is not touched')
        self.assertEqual(sorted(self.sup.stack_lines()), ['openbox', 'socat', 'xvfb'])  # stack started once
        self.assertEqual(len(self.sup.records()), 3)

    def test_same_mode_again_does_not_restart_chromium(self):
        self.sup.set_mode('human', 'https://example.com/')
        self.sup.start()
        self.sup.wait_calls(1)
        for _ in range(3):
            self.sup.set_mode('human', 'https://other.example/')
            time.sleep(0.5)
        self.assertEqual(len(self.sup.records()), 1)
        self.assertEqual(self.sup.records()[0]['args'][-1], 'https://example.com/')

    def test_human_mode_survives_supervisor_restart_and_keeps_the_session_profile(self):
        self.sup.set_mode('human', 'https://example.com/')
        self.sup.start()
        self.sup.wait_calls(1)
        self.sup.stop()
        (self.sup.home / '.config/botstead-browser-human/session-file').write_text('x')
        self.sup.start()
        records = self.sup.wait_calls(2)
        self.assertEqual(len(records), 2)
        for record in records:
            self.assertFalse(has_debugging(record['args']))
        self.assertTrue((self.sup.home / '.config/botstead-browser-human/session-file').exists())

    def test_chromium_crash_in_human_mode_never_brings_back_cdp(self):
        with tempfile.TemporaryDirectory() as tmp:
            crashing = Supervisor(tmp, CHROMIUM_EXIT='1')
            try:
                crashing.set_mode('human', 'https://example.com/')
                crashing.start()
                records = crashing.wait_calls(4)
                for record in records:
                    self.assertFalse(has_debugging(record['args']), record)
                self.assertEqual(crashing.pauses.read_text().splitlines()[:3], ['1', '2', '4'])
                self.assertEqual(sorted(crashing.stack_lines()), ['openbox', 'socat', 'xvfb'])  # X is not restarted
            finally:
                crashing.stop()

    def test_unknown_mode_content_starts_no_browser(self):
        self.sup.set_mode('garbage')
        self.sup.start()
        self.sup.wait_ready_stack()
        wait_until(lambda: 'unknown content' in self.sup.error_log.read_text())
        time.sleep(0.7)
        self.assertEqual(self.sup.records(), [])
        self.assertFalse((self.sup.home / '.browser-ready').exists())
        self.assertIn('unknown content', self.sup.error_log.read_text())
        self.sup.set_mode('bot')
        self.assertTrue(has_debugging(self.sup.wait_calls(1)[0]['args']))

    def test_mode_file_as_symlink_is_not_followed(self):
        target = self.sup.root / 'elsewhere'
        target.write_text('bot\n')
        (self.sup.home / '.browser-mode').symlink_to(target)
        self.sup.start()
        self.sup.wait_ready_stack()
        time.sleep(0.7)
        self.assertEqual(self.sup.records(), [])

    def test_leftover_human_profile_is_merged_and_removed_when_starting_in_bot_mode(self):
        human = self.sup.home / '.config/botstead-browser-human/Default/Network'
        human.mkdir(parents=True)
        con = sqlite3.connect(human / 'Cookies')
        con.execute(SCHEMA)
        con.execute("INSERT INTO cookies VALUES (1, '.left.test', '', 'x', 'y', '/', 0, -1)")
        con.commit()
        con.close()
        (self.sup.home / '.browser-human-session').write_text('session\n')  # the session was live when the container died
        self.sup.start()
        self.assertTrue(has_debugging(self.sup.wait_calls(1)[0]['args']))
        self.assertFalse((self.sup.home / '.config/botstead-browser-human').exists())
        bot = self.sup.home / '.config/botstead-browser/Default/Network/Cookies'
        self.assertEqual(sqlite3.connect(bot).execute('SELECT host_key, name FROM cookies').fetchall(),
                         [('.left.test', 'x')])

    def test_failed_merge_still_removes_the_human_profile_and_reports_it(self):
        human = self.sup.home / '.config/botstead-browser-human/Default/Network'
        human.mkdir(parents=True)
        (human / 'Cookies').write_text('not a database')
        (self.sup.home / '.browser-human-session').write_text('session\n')
        self.sup.start()
        self.assertTrue(has_debugging(self.sup.wait_calls(1)[0]['args']))
        self.assertFalse((self.sup.home / '.config/botstead-browser-human').exists())
        self.assertEqual((self.sup.home / '.browser-merge-status').read_text().strip(), 'failed')
        self.assertIn('cookie merge failed', self.sup.error_log.read_text())

    def test_stale_bot_stack_files_are_cleared_on_start(self):
        (self.sup.home / '.browser-active').write_text('bot 1 1\n')
        self.sup.set_mode('human', 'https://example.com/')
        self.sup.start()
        self.sup.wait_calls(1)
        wait_until(lambda: self.sup.active() and self.sup.active()[0] == 'human')
        self.assertEqual(self.sup.active()[0], 'human')


class SupervisorSourceTests(unittest.TestCase):
    def test_human_branch_has_no_debugging_flags(self):
        source = (ROOT / 'chromium-supervisor.sh').read_text()
        human = source[source.index('if [[ "$mode" == human ]]; then\n        read_human_url'):source.index('    else\n        dir=$PROFILE')]
        self.assertNotIn('remote-debugging', human)
        self.assertIn('--user-data-dir="$dir"', human)
        self.assertIn('-- "$human_url"', human)
        bot = source[source.index('    else\n        dir=$PROFILE'):source.index('    chromium_pid=$!')]
        self.assertIn('--remote-debugging-port=9222', bot)
        # Первая страница бота about:blank явно: без адреса Chromium открывает chrome://newtab/, а chrome://* закрыт политикой.
        self.assertIn('"${common[@]}" -- about:blank 9>&- &', bot)
        common = source[source.index('local -a common=('):source.index('if [[ "$mode" == human ]]; then\n        read_human_url')]
        self.assertIn('--password-store=basic', common)  # one shared flag list for both modes
        self.assertNotIn('remote-debugging', common)

    def test_cookie_merge_is_installed_root_owned_and_not_writable(self):
        dockerfile = (ROOT / 'Dockerfile').read_text()
        self.assertIn('COPY bot-image/cookie_merge.py /usr/local/libexec/cookie-merge.py', dockerfile)
        self.assertIn('chmod 0644 /usr/local/libexec/cookie-merge.py', dockerfile)


if __name__ == '__main__':
    unittest.main()

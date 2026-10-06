"""The human's Chromium instance: one directory tree for everything it writes, wiped before and after the session,
a bot instance that refuses to start while the tree cannot be removed, a cookie merge with a time limit, and a cleared
X clipboard on both edges of the session."""
from pathlib import Path
import json
import os
import sqlite3
import stat
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_browser_modes import ROOT, SCHEMA, SESSION_TOKEN, Supervisor, has_debugging, wait_until  # noqa: E402

ENV_DIRS = ('HOME', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME', 'TMPDIR')
SESSION_MARKER = '.browser-human-session'


def within(path, root):
    path, root = Path(path), Path(root)
    return root in path.parents


class HumanInstanceCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = self._tmp.name

    def make(self, **kwargs):
        sup = Supervisor(self.tmp, **kwargs)
        self.addCleanup(sup.stop)
        self.root = sup.home / '.config/botstead-browser-human'
        return sup

    def plant_cookies(self, directory, host, name='x'):
        db = Path(directory) / 'Default/Network/Cookies'
        db.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(db)
        con.execute(SCHEMA)
        con.execute("INSERT INTO cookies VALUES (1, ?, '', ?, 'y', '/', 0, -1)", (host, name))
        con.commit()
        con.close()


class OneDirectoryTreeTests(HumanInstanceCase):
    def test_every_path_of_the_human_instance_is_inside_the_instance_directory(self):
        sup = self.make()
        sup.set_mode('human', 'https://example.com/')
        sup.start()
        record = sup.wait_calls(1)[0]
        args, env = record['args'], record['env']
        self.assertIn(f'--user-data-dir={self.root}', args)
        self.assertIn(f'--disk-cache-dir={self.root}/cache', args)
        self.assertIn(f'--crash-dumps-dir={self.root}/crash', args)
        for flag in [a for a in args if a.startswith('--') and a.split('=')[0].endswith('-dir')]:
            value = flag.split('=', 1)[1]
            self.assertTrue(value == str(self.root) or within(value, self.root), flag)
        for name in ENV_DIRS:
            self.assertIsNotNone(env[name], name)
            self.assertTrue(within(env[name], self.root), (name, env[name]))
        for name in ENV_DIRS:
            self.assertEqual(stat.S_IMODE(Path(env[name]).stat().st_mode), 0o700, name)
        for flag in ('--disk-cache-dir', '--crash-dumps-dir'):
            value = next(a for a in args if a.startswith(flag + '=')).split('=', 1)[1]
            self.assertEqual(stat.S_IMODE(Path(value).stat().st_mode), 0o700, flag)

    def test_the_bot_instance_does_not_use_the_human_tree(self):
        sup = self.make()
        sup.start()
        record = sup.wait_calls(1)[0]
        for name in ENV_DIRS:
            value = record['env'][name]
            self.assertTrue(value is None or not within(value, self.root), (name, value))
        self.assertFalse(any(a.startswith(('--disk-cache-dir=', '--crash-dumps-dir=')) for a in record['args']))

    def test_return_removes_the_whole_tree_and_nothing_of_the_instance_survives(self):
        sup = self.make()
        sup.set_mode('human', 'https://example.com/')
        sup.start()
        sup.wait_calls(1)
        wait_until(lambda: len(list(sup.home.rglob('human-instance-file'))) >= 7)
        found = {str(p.relative_to(sup.home)) for p in sup.home.rglob('human-instance-file')}
        self.assertGreaterEqual(len(found), 7, found)  # cache, crash, 4 XDG, tmp, ~/.pki all got a file
        sup.set_mode('bot')
        sup.wait_calls(2)
        wait_until(lambda: sup.active() and sup.active()[0] == 'bot')
        self.assertFalse(self.root.exists())
        self.assertEqual(list(sup.home.rglob('human-instance-file')), [])
        self.assertEqual([p for p in sup.home.rglob('*') if 'botstead-browser-human' in str(p)], [])


class WipeBeforeAndAfterTests(HumanInstanceCase):
    def test_a_directory_planted_during_a_crash_pause_is_wiped_before_the_human_starts(self):
        """Probe: the bot's Chromium crashes, the mode flips to human during the pause, and a file sits in the human
        directory (the bot can make its browser write anywhere uid 1001 can)."""
        sup = self.make()
        home = sup.home
        (sup.bindir / 'chromium').write_text(f'''#!{sys.executable}
import json, os, sys, time
args = sys.argv[1:]
profile = next(a.split('=', 1)[1] for a in args if a.startswith('--user-data-dir='))
planted = os.path.exists(profile + '/Default/Preferences')
open(os.environ['CALLS'], 'a').write(json.dumps({{'args': args, 'pid': os.getpid(), 'planted_seen': planted}}) + '\\n')
if any(a.startswith('--remote-debugging') for a in args):
    raise SystemExit(1)
while True: time.sleep(0.05)
''')
        (sup.bindir / 'sleep').write_text(f'''#!/bin/sh
case "$1" in 1|2|4|8|16|30)
  mkdir -p "{home}/.config/botstead-browser-human/Default"
  echo '{{"planted":1}}' > "{home}/.config/botstead-browser-human/Default/Preferences"
  printf 'human\\n' > "{home}/.browser-mode"
  exec /bin/sleep 0.01;;
esac
exec /bin/sleep "$@"
''')
        sup.start()
        records = sup.wait_calls(2)
        self.assertTrue(has_debugging(records[0]['args']))
        self.assertFalse(has_debugging(records[1]['args']))
        self.assertFalse(records[1]['planted_seen'], 'the planted file must be gone when the human Chromium starts')
        self.assertTrue((home / SESSION_MARKER).exists())

    def test_a_directory_without_a_session_marker_is_wiped_before_entering_human(self):
        sup = self.make()
        (self.root / 'Default').mkdir(parents=True)
        (self.root / 'Default/Preferences').write_text('planted')
        (self.root / 'cache').mkdir()
        (self.root / 'cache/old').write_text('old cache')
        sup.set_mode('human', 'https://example.com/')
        sup.start()
        sup.wait_calls(1)
        self.assertFalse((self.root / 'Default/Preferences').exists())
        self.assertFalse((self.root / 'cache/old').exists())

    def test_switching_from_bot_to_human_wipes_a_directory_even_with_a_stale_marker(self):
        sup = self.make()
        sup.start()
        sup.wait_calls(1)
        sup.wait_ready_stack()
        wait_until(lambda: sup.active() and sup.active()[0] == 'bot')
        (self.root / 'cache').mkdir(parents=True)
        (self.root / 'cache/old').write_text('x')
        (sup.home / SESSION_MARKER).write_text('stale\n')
        sup.set_mode('human', 'https://example.com/')
        sup.wait_calls(2)
        self.assertFalse((self.root / 'cache/old').exists())

    def test_the_marker_lives_for_the_session_only(self):
        sup = self.make()
        marker = sup.home / SESSION_MARKER
        sup.set_mode('human', 'https://example.com/')
        sup.start()
        sup.wait_calls(1)
        self.assertTrue(marker.is_file() and not marker.is_symlink())
        self.assertEqual(stat.S_IMODE(marker.stat().st_mode), 0o600)
        sup.set_mode('bot')
        sup.wait_calls(2)
        wait_until(lambda: sup.active() and sup.active()[0] == 'bot')
        self.assertFalse(marker.exists())
        self.assertFalse(self.root.exists())

    def test_a_restart_in_the_middle_of_a_session_keeps_the_directory(self):
        sup = self.make()
        sup.set_mode('human', 'https://example.com/')
        sup.start()
        sup.wait_calls(1)
        sup.stop()
        (self.root / 'session-file').write_text('x')
        (self.root / 'cache').mkdir(exist_ok=True)
        (self.root / 'cache/page').write_text('x')
        self.assertTrue((sup.home / SESSION_MARKER).exists())
        sup.start()
        sup.wait_calls(2)
        self.assertTrue((self.root / 'session-file').exists())
        self.assertTrue((self.root / 'cache/page').exists())

    def test_a_crash_of_the_human_chromium_keeps_the_session_directory(self):
        sup = self.make(CHROMIUM_EXIT='1')
        sup.set_mode('human', 'https://example.com/')
        sup.start()
        sup.wait_calls(2)
        self.assertTrue((sup.home / SESSION_MARKER).exists())
        self.assertTrue(self.root.is_dir())

    def test_leftover_directory_with_a_marker_is_merged_then_removed_in_bot_mode(self):
        sup = self.make()
        self.plant_cookies(self.root, '.left.test')
        (sup.home / SESSION_MARKER).write_text('session\n')
        sup.start()
        self.assertTrue(has_debugging(sup.wait_calls(1)[0]['args']))
        self.assertFalse(self.root.exists())
        self.assertFalse((sup.home / SESSION_MARKER).exists())
        bot = sup.home / '.config/botstead-browser/Default/Network/Cookies'
        self.assertEqual(sqlite3.connect(bot).execute('SELECT host_key, name FROM cookies').fetchall(),
                         [('.left.test', 'x')])
        self.assertEqual((sup.home / '.browser-merge-status').read_text().strip(), 'merged 1')

    def test_leftover_directory_without_a_marker_is_wiped_and_never_merged(self):
        sup = self.make()
        self.plant_cookies(self.root, '.planted.test')
        sup.start()
        self.assertTrue(has_debugging(sup.wait_calls(1)[0]['args']))
        self.assertFalse(self.root.exists())
        bot = sup.home / '.config/botstead-browser/Default/Network/Cookies'
        self.assertFalse(bot.exists(), 'untrusted cookies must not reach the bot profile')
        self.assertFalse((sup.home / '.browser-merge-status').exists())

    def test_a_symlink_in_place_of_the_directory_is_removed_and_its_target_left_alone(self):
        sup = self.make()
        target = sup.root / 'elsewhere'
        target.mkdir()
        (target / 'keep').write_text('x')
        self.root.parent.mkdir(parents=True, exist_ok=True)
        self.root.symlink_to(target)
        (sup.home / SESSION_MARKER).write_text('session\n')
        sup.start()
        sup.wait_calls(1)
        self.assertFalse(self.root.exists() or self.root.is_symlink())
        self.assertTrue((target / 'keep').exists())


@unittest.skipIf(os.geteuid() == 0, 'root can remove a read-only tree')
class RemovalFailureTests(HumanInstanceCase):
    def lock_tree(self, sup):
        (self.root / 'locked').mkdir(parents=True)
        (self.root / 'locked/file').write_text('x')
        (self.root / 'locked').chmod(0o500)
        self.addCleanup(lambda: (self.root / 'locked').exists() and (self.root / 'locked').chmod(0o700))
        (sup.home / SESSION_MARKER).write_text('session\n')

    def test_the_bot_instance_does_not_start_while_the_human_tree_cannot_be_removed(self):
        sup = self.make(blocked_retry=1)
        self.lock_tree(sup)
        sup.start()
        sup.wait_ready_stack()
        wait_until(lambda: 'could not be removed' in sup.error_log.read_text(), 10)
        time.sleep(1.5)
        self.assertEqual(sup.records(), [], 'no Chromium of any kind may start')
        self.assertFalse((sup.home / '.browser-ready').exists())
        self.assertFalse((sup.home / '.browser-active').exists())
        self.assertTrue((sup.home / SESSION_MARKER).exists(), 'the session marker stays until the tree is gone')

    def test_the_blocked_start_is_retried_with_a_pause_and_finishes_once_the_tree_can_go(self):
        sup = self.make(blocked_retry=1)
        self.lock_tree(sup)
        sup.start()
        wait_until(lambda: 'could not be removed' in sup.error_log.read_text(), 10)
        self.assertEqual(sup.records(), [])
        (self.root / 'locked').chmod(0o700)
        records = sup.wait_calls(1, timeout=15)
        self.assertTrue(has_debugging(records[0]['args']))
        self.assertFalse(self.root.exists())
        self.assertFalse((sup.home / SESSION_MARKER).exists())
        errors = sup.error_log.read_text()
        self.assertLess(errors.count('could not be removed'), 12, 'the retry is paced, not a busy loop')

    def test_the_merge_result_of_the_first_attempt_survives_the_retries(self):
        """The first attempt merges the cookies but the tree cannot go; the retries find nothing new ("merged 0") and must
        not replace the first result: the cookies are in the bot profile."""
        sup = self.make(blocked_retry=1)
        self.plant_cookies(self.root, '.left.test')
        self.lock_tree(sup)
        status = sup.home / '.browser-merge-status'
        sup.start()
        wait_until(lambda: sup.error_log.read_text().count('could not be removed') >= 3, 15)
        self.assertEqual(status.read_text().strip(), 'merged 1', 'retries must keep the first result')
        (self.root / 'locked').chmod(0o700)
        records = sup.wait_calls(1, timeout=15)
        self.assertTrue(has_debugging(records[0]['args']))
        self.assertEqual(status.read_text().strip(), 'merged 1')

    def test_a_failed_first_attempt_is_replaced_by_a_successful_retry(self):
        flaky = Path(self.tmp) / 'flaky-merge.py'
        flaky.write_text('import os, sys\nflag = os.environ["HOME"] + "/merge-tried"\n'
                         'if not os.path.exists(flag):\n    open(flag, "w").close()\n    sys.exit(1)\nprint("merged 4")\n')
        sup = self.make(blocked_retry=1, merge_script=flaky)
        self.plant_cookies(self.root, '.left.test')
        self.lock_tree(sup)
        sup.start()
        wait_until(lambda: sup.error_log.read_text().count('could not be removed') >= 2, 15)
        (self.root / 'locked').chmod(0o700)
        sup.wait_calls(1, timeout=15)
        self.assertEqual((sup.home / '.browser-merge-status').read_text().strip(), 'merged 4')

    def test_a_new_human_session_starts_without_the_old_merge_result(self):
        sup = self.make()
        (sup.home / '.browser-merge-status').write_text('merged 9\n')
        sup.set_mode('human', 'https://example.com/')
        sup.start()
        sup.wait_calls(1)
        self.assertFalse((sup.home / '.browser-merge-status').exists())

    def test_entering_human_with_a_tree_that_cannot_be_removed_starts_nothing(self):
        sup = self.make(blocked_retry=1)
        (self.root / 'locked').mkdir(parents=True)
        (self.root / 'locked/file').write_text('x')
        (self.root / 'locked').chmod(0o500)
        self.addCleanup(lambda: (self.root / 'locked').chmod(0o700))
        sup.set_mode('human', 'https://example.com/')
        sup.start()
        wait_until(lambda: 'could not be removed' in sup.error_log.read_text(), 10)
        time.sleep(1.2)
        self.assertEqual(sup.records(), [], 'an old tree must never be reused by a new human session')


class CookieMergeLimitTests(HumanInstanceCase):
    def test_merge_is_limited_to_twenty_seconds_and_runs_isolated(self):
        source = (ROOT / 'chromium-supervisor.sh').read_text()
        self.assertIn('readonly MERGE_TIMEOUT=20', source)
        self.assertIn('-I "$COOKIE_MERGE"', source)

    def test_a_merge_that_hangs_is_killed_and_does_not_stop_the_removal_or_the_start(self):
        stuck = Path(self.tmp) / 'stuck-merge.py'
        stuck.write_text('import time\nopen(__import__("os").environ["HOME"] + "/merge-started", "w").close()\n'
                         'time.sleep(120)\n')
        sup = self.make(merge_timeout=1, merge_script=stuck)
        self.plant_cookies(self.root, '.left.test')
        (sup.home / SESSION_MARKER).write_text('session\n')
        started = time.monotonic()
        sup.start()
        records = sup.wait_calls(1, timeout=15)
        self.assertLess(time.monotonic() - started, 12)
        self.assertTrue(has_debugging(records[0]['args']))
        self.assertTrue((sup.home / 'merge-started').exists())
        self.assertFalse(self.root.exists(), 'the human directory goes even though the merge hung')
        self.assertEqual((sup.home / '.browser-merge-status').read_text().strip(), 'failed')
        self.assertIn('timed out', sup.error_log.read_text())

    def test_the_merge_script_runs_under_python_dash_i(self):
        probe = Path(self.tmp) / 'probe-merge.py'
        probe.write_text('import os, sys\nopen(os.environ["HOME"] + "/isolated", "w").write(str(sys.flags.isolated))\n'
                         'print("merged 0")\n')
        sup = self.make(merge_script=probe)
        (self.root).mkdir(parents=True)
        (sup.home / SESSION_MARKER).write_text('session\n')
        sup.start()
        sup.wait_calls(1)
        self.assertEqual((sup.home / 'isolated').read_text(), '1')

    def test_the_merge_gets_the_human_tree_and_the_bot_profile(self):
        probe = Path(self.tmp) / 'args-merge.py'
        probe.write_text('import os, sys, json\nopen(os.environ["HOME"] + "/merge-args", "w").write(json.dumps(sys.argv[1:]))\n'
                         'print("merged 0")\n')
        sup = self.make(merge_script=probe)
        self.root.mkdir(parents=True)
        (sup.home / SESSION_MARKER).write_text('session\n')
        sup.start()
        sup.wait_calls(1)
        self.assertEqual(json.loads((sup.home / 'merge-args').read_text()),
                         [str(self.root), str(sup.home / '.config/botstead-browser')])


class ClipboardTests(HumanInstanceCase):
    def cleared(self, call):
        args = call['args']
        return [name for name, flags in (('primary', ('--primary', '-p')), ('clipboard', ('--clipboard', '-b')))
                if any(flag in args for flag in flags)]

    def test_both_selections_are_cleared_before_the_human_chromium_starts(self):
        sup = self.make()
        sup.set_mode('human', 'https://example.com/')
        sup.start()
        record = sup.wait_calls(1)[0]
        wait_until(lambda: len(sup.xsel_calls()) >= 2)
        before = [c for c in sup.xsel_calls() if c['t'] < record['t']]
        self.assertEqual(sorted(name for c in before for name in self.cleared(c)), ['clipboard', 'primary'])
        for call in before:
            self.assertTrue(any(flag in call['args'] for flag in ('--clear', '-c')), call)
            self.assertEqual(call['display'], ':99')

    def test_both_selections_are_cleared_after_the_human_chromium_is_gone_and_before_the_bot_one(self):
        sup = self.make()
        sup.set_mode('human', 'https://example.com/')
        sup.start()
        human = sup.wait_calls(1)[0]
        wait_until(lambda: len(sup.xsel_calls()) >= 2)
        sup.set_mode('bot')
        bot = sup.wait_calls(2)[1]
        self.assertTrue(has_debugging(bot['args']))
        after = [c for c in sup.xsel_calls() if human['t'] < c['t'] < bot['t']]
        self.assertEqual(sorted(name for c in after for name in self.cleared(c)), ['clipboard', 'primary'])
        for call in after:
            self.assertEqual(call['alive'], [], 'cleared only after the human Chromium has stopped')

    def test_a_missing_xsel_does_not_stop_the_browser(self):
        sup = self.make()
        (sup.bindir / 'xsel').unlink()
        sup.set_mode('human', 'https://example.com/')
        sup.start()
        self.assertFalse(has_debugging(sup.wait_calls(1)[0]['args']))

    def test_a_hanging_xsel_does_not_stop_the_browser(self):
        sup = self.make()
        (sup.bindir / 'xsel').write_text('#!/bin/sh\nexec /bin/sleep 120\n')
        sup.set_mode('human', 'https://example.com/')
        sup.start()
        self.assertFalse(has_debugging(sup.wait_calls(1, timeout=20)[0]['args']))

    def test_the_bot_start_without_a_human_session_does_not_touch_the_clipboard(self):
        sup = self.make()
        sup.start()
        sup.wait_calls(1)
        time.sleep(0.5)
        self.assertEqual(sup.xsel_calls(), [])


class CutBufferTests(HumanInstanceCase):
    """x11vnc copies the client's clipboard text into CUT_BUFFER0 of the root window; `xsel --clear` does not touch it."""

    def removed(self, calls):
        return sorted(call['args'][-1] for call in calls if call['args'][:2] == ['-root', '-remove'])

    def test_every_cut_buffer_is_removed_before_the_human_chromium_starts(self):
        sup = self.make()
        sup.set_mode('human', 'https://example.com/')
        sup.start()
        record = sup.wait_calls(1)[0]
        wait_until(lambda: len(sup.xprop_calls()) >= 8)
        before = [c for c in sup.xprop_calls() if c['t'] < record['t']]
        self.assertEqual(self.removed(before), [f'CUT_BUFFER{n}' for n in range(8)])
        for call in before:
            self.assertEqual(call['display'], ':99')

    def test_every_cut_buffer_is_removed_after_the_human_chromium_is_gone_and_before_the_bot_one(self):
        sup = self.make()
        sup.set_mode('human', 'https://example.com/')
        sup.start()
        human = sup.wait_calls(1)[0]
        wait_until(lambda: len(sup.xprop_calls()) >= 8)
        sup.set_mode('bot')
        bot = sup.wait_calls(2)[1]
        self.assertTrue(has_debugging(bot['args']))
        after = [c for c in sup.xprop_calls() if human['t'] < c['t'] < bot['t']]
        self.assertEqual(self.removed(after), [f'CUT_BUFFER{n}' for n in range(8)])
        for call in after:
            self.assertEqual(call['alive'], [], 'removed only after the human Chromium has stopped')

    def test_cut_buffers_are_removed_even_when_xsel_is_missing(self):
        sup = self.make()
        (sup.bindir / 'xsel').unlink()
        sup.set_mode('human', 'https://example.com/')
        sup.start()
        sup.wait_calls(1)
        wait_until(lambda: len(sup.xprop_calls()) >= 8)
        self.assertEqual(self.removed(sup.xprop_calls()), [f'CUT_BUFFER{n}' for n in range(8)])

    def test_a_missing_xprop_does_not_stop_the_browser(self):
        sup = self.make()
        (sup.bindir / 'xprop').unlink()
        sup.set_mode('human', 'https://example.com/')
        sup.start()
        self.assertFalse(has_debugging(sup.wait_calls(1)[0]['args']))
        self.assertIn('xprop not found', sup.error_log.read_text())

    def test_a_hanging_xprop_does_not_stop_the_browser(self):
        sup = self.make()
        (sup.bindir / 'xprop').write_text('#!/bin/sh\nexec /bin/sleep 120\n')
        sup.set_mode('human', 'https://example.com/')
        sup.start()
        self.assertFalse(has_debugging(sup.wait_calls(1, timeout=20)[0]['args']))

    def test_the_bot_start_without_a_human_session_does_not_touch_the_cut_buffers(self):
        sup = self.make()
        sup.start()
        sup.wait_calls(1)
        time.sleep(0.5)
        self.assertEqual(sup.xprop_calls(), [])


OTHER_TOKEN = 'fedcba9876543210fedcba9876543210'


class SessionTokenTests(HumanInstanceCase):
    """The launcher writes `human <token>` to the mode file when the human takes over. A human directory is resumed only
    when its marker carries the same token: whatever the bot's Chromium (same uid) plants cannot match it."""

    def marker(self, sup):
        return (sup.home / SESSION_MARKER).read_text()

    def test_the_marker_carries_the_token_of_the_mode_file(self):
        sup = self.make()
        sup.set_mode('human', 'https://example.com/')
        sup.start()
        sup.wait_calls(1)
        self.assertEqual(self.marker(sup), f'session {SESSION_TOKEN}\n')

    def test_a_restart_with_the_same_token_resumes_the_session(self):
        sup = self.make()
        sup.set_mode('human', 'https://example.com/')
        sup.start()
        sup.wait_calls(1)
        sup.stop()
        (self.root / 'session-file').write_text('x')
        sup.start()
        sup.wait_calls(2)
        self.assertTrue((self.root / 'session-file').exists())
        self.assertEqual(self.marker(sup), f'session {SESSION_TOKEN}\n')

    def test_a_restart_with_another_token_starts_a_fresh_session(self):
        sup = self.make()
        sup.set_mode('human', 'https://example.com/')
        sup.start()
        sup.wait_calls(1)
        sup.stop()
        (self.root / 'session-file').write_text('x')
        sup.set_mode('human', token=OTHER_TOKEN)
        sup.start()
        sup.wait_calls(2)
        self.assertFalse((self.root / 'session-file').exists())
        self.assertEqual(self.marker(sup), f'session {OTHER_TOKEN}\n')

    def test_a_token_less_human_mode_never_resumes(self):
        sup = self.make()
        sup.set_mode('human', 'https://example.com/', token=None)
        sup.start()
        sup.wait_calls(1)
        sup.stop()
        (self.root / 'session-file').write_text('x')
        sup.start()
        sup.wait_calls(2)
        self.assertFalse((self.root / 'session-file').exists())

    def test_a_planted_directory_and_marker_are_not_resumed_after_a_container_restart(self):
        """The bot's Chromium plants a marker and a directory, the container restarts with the mode file already human."""
        for planted in ('session\n', f'session {OTHER_TOKEN}\n', 'session\n' + 'x' * 100, ''):
            with self.subTest(marker=planted[:20]):
                with tempfile.TemporaryDirectory() as tmp:
                    sup = Supervisor(tmp)
                    self.addCleanup(sup.stop)
                    root = sup.home / '.config/botstead-browser-human'
                    (root / 'Default').mkdir(parents=True)
                    (root / 'Default/Preferences').write_text('planted')
                    (sup.home / SESSION_MARKER).write_text(planted)
                    sup.set_mode('human', 'https://example.com/')
                    sup.start()
                    sup.wait_calls(1)
                    self.assertFalse((root / 'Default/Preferences').exists())
                    self.assertEqual((sup.home / SESSION_MARKER).read_text(), f'session {SESSION_TOKEN}\n')

    def test_a_marker_planted_in_bot_mode_is_not_resumed_at_takeover(self):
        sup = self.make()
        sup.start()
        sup.wait_calls(1)
        wait_until(lambda: sup.active() and sup.active()[0] == 'bot')
        (self.root / 'Default').mkdir(parents=True)
        (self.root / 'Default/Preferences').write_text('planted')
        (sup.home / SESSION_MARKER).write_text(f'session {SESSION_TOKEN}\n')  # even the right token: previous was bot
        sup.set_mode('human', 'https://example.com/')
        sup.wait_calls(2)
        self.assertFalse((self.root / 'Default/Preferences').exists())

    def test_the_bot_crashes_its_chromium_and_the_human_takes_over_gets_a_fresh_directory(self):
        """Scenario of the review: the bot's Chromium dies, plants the directory and a matching marker during the crash
        pause, the mode flips to human. The previous mode (bot) must survive the pause, so the start is fresh."""
        sup = self.make()
        home = sup.home
        (sup.bindir / 'chromium').write_text(f'''#!{sys.executable}
import json, os, sys, time
args = sys.argv[1:]
profile = next(a.split('=', 1)[1] for a in args if a.startswith('--user-data-dir='))
planted = os.path.exists(profile + '/Default/Preferences')
open(os.environ['CALLS'], 'a').write(json.dumps({{'args': args, 'pid': os.getpid(), 'planted_seen': planted}}) + '\\n')
if any(a.startswith('--remote-debugging') for a in args):
    raise SystemExit(1)
while True: time.sleep(0.05)
''')
        (sup.bindir / 'sleep').write_text(f'''#!/bin/sh
case "$1" in 1|2|4|8|16|30)
  mkdir -p "{home}/.config/botstead-browser-human/Default"
  echo '{{"planted":1}}' > "{home}/.config/botstead-browser-human/Default/Preferences"
  printf 'session {SESSION_TOKEN}\\n' > "{home}/.browser-human-session"
  printf 'human {SESSION_TOKEN}\\n' > "{home}/.browser-mode"
  exec /bin/sleep 0.01;;
esac
exec /bin/sleep "$@"
''')
        sup.start()
        records = sup.wait_calls(2)
        self.assertTrue(has_debugging(records[0]['args']))
        self.assertFalse(has_debugging(records[1]['args']))
        self.assertFalse(records[1]['planted_seen'], 'a directory planted before the takeover must not be resumed')

    def test_the_session_survives_a_crash_of_the_human_chromium_itself(self):
        sup = self.make(CHROMIUM_EXIT='1')
        sup.set_mode('human', 'https://example.com/')
        sup.start()
        sup.wait_calls(1)
        (self.root / 'session-file').write_text('x')
        sup.wait_calls(3)
        self.assertTrue((self.root / 'session-file').exists())

    def test_a_malformed_mode_line_starts_no_browser(self):
        for line in ('human nothex', 'human ' + 'A' * 32, 'human ' + SESSION_TOKEN + ' extra', 'bot ' + SESSION_TOKEN,
                     'human ' + SESSION_TOKEN[:31]):
            with self.subTest(line=line):
                with tempfile.TemporaryDirectory() as tmp:
                    sup = Supervisor(tmp)
                    self.addCleanup(sup.stop)
                    sup._atomic(sup.home / '.browser-mode', line + '\n')
                    sup.start()
                    sup.wait_ready_stack()
                    wait_until(lambda: 'unknown content' in sup.error_log.read_text())
                    time.sleep(0.5)
                    self.assertEqual(sup.records(), [])


class ImageTests(unittest.TestCase):
    def test_xsel_is_installed_in_the_image(self):
        dockerfile = (ROOT / 'Dockerfile').read_text()
        packages = dockerfile[dockerfile.index('apt-get install -y --no-install-recommends \\\n    software-properties-common'):
                              dockerfile.index('# Add NodeSource repository')]
        self.assertIn('xsel', packages.split())

    def test_xprop_comes_with_x11_utils_in_the_image(self):
        dockerfile = (ROOT / 'Dockerfile').read_text()
        packages = dockerfile[dockerfile.index('apt-get install -y --no-install-recommends \\\n    software-properties-common'):
                              dockerfile.index('# Add NodeSource repository')]
        self.assertIn('x11-utils', packages.split())


if __name__ == '__main__':
    unittest.main()

"""cookie_merge.py on two real SQLite databases: the human cookies replace the bot's by key, nothing else moves."""
from pathlib import Path
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cookie_merge  # noqa: E402

COLUMNS = ('creation_utc INTEGER NOT NULL, host_key TEXT NOT NULL, top_frame_site_key TEXT NOT NULL, '
           'name TEXT NOT NULL, value TEXT NOT NULL, encrypted_value BLOB NOT NULL DEFAULT x\'\', path TEXT NOT NULL, '
           'source_scheme INTEGER NOT NULL DEFAULT 0, source_port INTEGER NOT NULL DEFAULT -1')
UNIQUE = 'UNIQUE (host_key, top_frame_site_key, name, path, source_scheme, source_port)'
NEW = 'Default/Network/Cookies'
OLD = 'Default/Cookies'


def make_db(profile, rows, rel=NEW, columns=COLUMNS, unique=UNIQUE, insert=None):
    path = Path(profile) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute(f'CREATE TABLE cookies ({columns}, {unique})')
    for row in rows:
        con.execute(insert or 'INSERT INTO cookies (creation_utc, host_key, top_frame_site_key, name, value, '
                    'encrypted_value, path) VALUES (?, ?, ?, ?, ?, ?, ?)', row)
    con.commit()
    con.close()
    return path


def dump(path):
    con = sqlite3.connect(path)
    try:
        return sorted(con.execute('SELECT host_key, top_frame_site_key, name, path, value, encrypted_value '
                                  'FROM cookies'))
    finally:
        con.close()


class CookieMergeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.human = Path(tmp.name) / 'human'
        self.bot = Path(tmp.name) / 'bot'

    def test_human_row_replaces_bot_row_with_the_same_key(self):
        bot = make_db(self.bot, [(1, '.a.com', '', 'sid', 'bot', b'old', '/')])
        make_db(self.human, [(2, '.a.com', '', 'sid', 'human', b'new', '/')])
        self.assertEqual(cookie_merge.merge_cookies(self.human, self.bot), 1)
        self.assertEqual(dump(bot), [('.a.com', '', 'sid', '/', 'human', b'new')])

    def test_bot_only_cookies_stay_and_human_only_cookies_arrive(self):
        bot = make_db(self.bot, [(1, '.bot.com', '', 'keep', 'k', b'', '/')])
        make_db(self.human, [(2, '.human.com', '', 'new', 'n', b'', '/')])
        self.assertEqual(cookie_merge.merge_cookies(self.human, self.bot), 1)
        self.assertEqual([(r[0], r[2]) for r in dump(bot)], [('.bot.com', 'keep'), ('.human.com', 'new')])

    def test_key_is_host_top_frame_name_path_so_near_misses_are_kept(self):
        bot = make_db(self.bot, [
            (1, '.a.com', '', 'sid', 'other-path', b'', '/app'),
            (1, '.a.com', 'https://top.test', 'sid', 'other-top-frame', b'', '/'),
            (1, '.a.com', '', 'sid2', 'other-name', b'', '/'),
            (1, '.b.com', '', 'sid', 'other-host', b'', '/'),
            (1, '.a.com', '', 'sid', 'replaced', b'', '/'),
        ])
        make_db(self.human, [(2, '.a.com', '', 'sid', 'human', b'', '/')])
        cookie_merge.merge_cookies(self.human, self.bot)
        values = {r[4] for r in dump(bot)}
        self.assertEqual(values, {'other-path', 'other-top-frame', 'other-name', 'other-host', 'human'})

    def test_every_row_with_the_key_goes_even_when_source_scheme_differs(self):
        bot = make_db(self.bot, [(1, '.a.com', '', 'sid', 'old', b'', '/')])
        con = sqlite3.connect(bot)
        con.execute("INSERT INTO cookies (creation_utc, host_key, top_frame_site_key, name, value, path, "
                    "source_scheme, source_port) VALUES (1, '.a.com', '', 'sid', 'old-https', '/', 2, 443)")
        con.commit()
        con.close()
        make_db(self.human, [(2, '.a.com', '', 'sid', 'human', b'', '/')])
        cookie_merge.merge_cookies(self.human, self.bot)
        self.assertEqual([r[4] for r in dump(bot)], ['human'])

    def test_legacy_cookies_location_is_used_on_both_sides(self):
        bot = make_db(self.bot, [(1, '.a.com', '', 'sid', 'bot', b'', '/')], rel=OLD)
        make_db(self.human, [(2, '.a.com', '', 'sid', 'human', b'', '/')], rel=OLD)
        cookie_merge.merge_cookies(self.human, self.bot)
        self.assertEqual([r[4] for r in dump(bot)], ['human'])

    def test_no_human_database_changes_nothing(self):
        bot = make_db(self.bot, [(1, '.a.com', '', 'sid', 'bot', b'', '/')])
        (self.human / 'Default').mkdir(parents=True)
        self.assertEqual(cookie_merge.merge_cookies(self.human, self.bot), 0)
        self.assertEqual([r[4] for r in dump(bot)], ['bot'])

    def test_missing_bot_database_takes_the_human_one(self):
        make_db(self.human, [(2, '.a.com', '', 'sid', 'human', b'', '/')])
        self.assertEqual(cookie_merge.merge_cookies(self.human, self.bot), 1)
        target = self.bot / NEW
        self.assertEqual([r[4] for r in dump(target)], ['human'])
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_only_the_cookies_table_moves(self):
        bot = make_db(self.bot, [])
        human = make_db(self.human, [(2, '.a.com', '', 'sid', 'human', b'', '/')])
        for path, value in ((bot, 'bot'), (human, 'human')):
            con = sqlite3.connect(path)
            con.execute('CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)')
            con.execute('INSERT INTO meta VALUES (?, ?)', ('version', value))
            con.commit()
            con.close()
        (self.human / 'Default' / 'Preferences').write_text('{"human": true}')
        (self.bot / 'Default').mkdir(exist_ok=True)
        (self.bot / 'Default' / 'Preferences').write_text('{"bot": true}')
        cookie_merge.merge_cookies(self.human, self.bot)
        self.assertEqual(sqlite3.connect(bot).execute("SELECT value FROM meta").fetchall(), [('bot',)])
        self.assertEqual((self.bot / 'Default' / 'Preferences').read_text(), '{"bot": true}')
        self.assertEqual(sorted(p.name for p in (self.bot / 'Default').iterdir()), ['Network', 'Preferences'])

    def test_columns_missing_on_one_side_are_tolerated_when_they_have_defaults(self):
        # the bot database is newer: one more column with a default
        bot = make_db(self.bot, [(1, '.a.com', '', 'old', 'o', b'', '/')],
                      columns=COLUMNS + ', has_cross_site_ancestor INTEGER NOT NULL DEFAULT 0')
        make_db(self.human, [(2, '.a.com', '', 'sid', 'human', b'', '/')])
        self.assertEqual(cookie_merge.merge_cookies(self.human, self.bot), 1)
        self.assertEqual([(r[2], r[4]) for r in dump(bot)], [('old', 'o'), ('sid', 'human')])

    def test_failure_rolls_back_and_leaves_the_bot_database_untouched(self):
        # the bot table has a NOT NULL column without default that the human table lacks: the INSERT fails
        bot = make_db(self.bot, [(1, '.a.com', '', 'sid', 'bot', b'', '/')],
                      columns=COLUMNS + ', required_extra TEXT NOT NULL DEFAULT \'\'')
        con = sqlite3.connect(bot)
        con.execute('DROP TABLE cookies')
        con.execute(f'CREATE TABLE cookies ({COLUMNS}, human_missing TEXT NOT NULL, {UNIQUE})')
        con.execute("INSERT INTO cookies (creation_utc, host_key, top_frame_site_key, name, value, path, human_missing) "
                    "VALUES (1, '.a.com', '', 'sid', 'bot', '/', 'x')")
        con.commit()
        con.close()
        make_db(self.human, [(2, '.a.com', '', 'sid', 'human', b'', '/')])
        with self.assertRaises(sqlite3.IntegrityError):
            cookie_merge.merge_cookies(self.human, self.bot)
        self.assertEqual([r[4] for r in dump(bot)], ['bot'])

    def test_table_without_a_usable_key_is_refused(self):
        make_db(self.bot, [], columns='name TEXT NOT NULL, value TEXT NOT NULL', unique='UNIQUE (name)')
        make_db(self.human, [], columns='name TEXT NOT NULL, value TEXT NOT NULL', unique='UNIQUE (name)')
        with self.assertRaises(cookie_merge.MergeError):
            cookie_merge.merge_cookies(self.human, self.bot)

    def test_symlinked_human_database_is_refused(self):
        make_db(self.human, [(2, '.a.com', '', 'sid', 'human', b'', '/')], rel='real/Cookies')
        link = self.human / NEW
        link.parent.mkdir(parents=True)
        os.symlink(self.human / 'real/Cookies', link)
        make_db(self.bot, [])
        with self.assertRaises(cookie_merge.MergeError):
            cookie_merge.merge_cookies(self.human, self.bot)

    def test_cli_reports_count_and_failure_code(self):
        make_db(self.bot, [])
        make_db(self.human, [(2, '.a.com', '', 'sid', 'human', b'', '/')])
        script = ROOT / 'cookie_merge.py'
        ok = subprocess.run([sys.executable, '-I', str(script), str(self.human), str(self.bot)],
                            capture_output=True, text=True)
        self.assertEqual((ok.returncode, ok.stdout.strip()), (0, 'merged 1'))
        (self.human / NEW).write_text('not a database')
        bad = subprocess.run([sys.executable, '-I', str(script), str(self.human), str(self.bot)],
                             capture_output=True, text=True)
        self.assertEqual(bad.returncode, 1)
        self.assertEqual(bad.stdout, '')
        usage = subprocess.run([sys.executable, '-I', str(script)], capture_output=True, text=True)
        self.assertEqual(usage.returncode, 2)


TRIGGER = ("CREATE TRIGGER wipe AFTER INSERT ON cookies BEGIN DELETE FROM cookies WHERE host_key = '.bot.com'; END")


class Recording(sqlite3.Connection):
    log = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.set_trace_callback(Recording.log.append)


def recorded_merge(human, bot):
    Recording.log = []
    real = sqlite3.connect

    def connect(*args, **kwargs):
        return real(*args, factory=Recording, **kwargs)
    with mock.patch.object(cookie_merge.sqlite3, 'connect', connect):
        count = cookie_merge.merge_cookies(human, bot)
    return count, list(Recording.log)


class CookieMergeHardeningTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.human = self.tmp / 'human'
        self.bot = self.tmp / 'bot'

    def add_trigger(self, path):
        con = sqlite3.connect(path)
        con.execute(TRIGGER)
        con.commit()
        con.close()

    def test_a_trigger_in_the_bot_database_is_refused_before_anything_is_written(self):
        bot = make_db(self.bot, [(1, '.bot.com', '', 'keep', 'k', b'', '/')])
        self.add_trigger(bot)
        make_db(self.human, [(2, '.a.com', '', 'sid', 'human', b'', '/')])
        with self.assertRaisesRegex(cookie_merge.MergeError, 'trigger'):
            cookie_merge.merge_cookies(self.human, self.bot)
        self.assertEqual([(r[0], r[2]) for r in dump(bot)], [('.bot.com', 'keep')])

    def test_a_trigger_in_the_human_database_is_refused(self):
        bot = make_db(self.bot, [(1, '.bot.com', '', 'keep', 'k', b'', '/')])
        human = make_db(self.human, [(2, '.a.com', '', 'sid', 'human', b'', '/')])
        self.add_trigger(human)
        with self.assertRaisesRegex(cookie_merge.MergeError, 'trigger'):
            cookie_merge.merge_cookies(self.human, self.bot)
        self.assertEqual([r[2] for r in dump(bot)], ['keep'])

    def test_a_trigger_in_the_human_database_is_refused_when_the_bot_has_none_yet(self):
        human = make_db(self.human, [(2, '.a.com', '', 'sid', 'human', b'', '/')])
        self.add_trigger(human)
        with self.assertRaisesRegex(cookie_merge.MergeError, 'trigger'):
            cookie_merge.merge_cookies(self.human, self.bot)
        self.assertFalse((self.bot / NEW).exists())
        self.assertEqual(list((self.bot / 'Default/Network').glob('*')) if (self.bot / 'Default/Network').exists() else [], [])

    def test_copy_for_a_missing_bot_database_leaves_no_temporary_file_and_replaces_atomically(self):
        make_db(self.human, [(2, '.a.com', '', 'sid', 'human', b'', '/'), (2, '.b.com', '', 't', 'v', b'', '/')])
        self.assertEqual(cookie_merge.merge_cookies(self.human, self.bot), 2)
        self.assertEqual(sorted(p.name for p in (self.bot / 'Default/Network').iterdir()), ['Cookies'])
        self.assertEqual((self.bot / NEW).stat().st_mode & 0o777, 0o600)

    def test_the_bot_database_as_a_symlink_is_refused_and_its_target_untouched(self):
        target = make_db(self.tmp / 'elsewhere', [(1, '.x.com', '', 'x', 'x', b'', '/')])
        link = self.bot / NEW
        link.parent.mkdir(parents=True)
        os.symlink(target, link)
        make_db(self.human, [(2, '.a.com', '', 'sid', 'human', b'', '/')])
        with self.assertRaises(cookie_merge.MergeError):
            cookie_merge.merge_cookies(self.human, self.bot)
        self.assertEqual([r[2] for r in dump(target)], ['x'])
        self.assertTrue(link.is_symlink())

    def test_a_symlinked_directory_on_either_side_is_refused(self):
        make_db(self.tmp / 'elsewhere', [(1, '.x.com', '', 'x', 'x', b'', '/')], rel='Network/Cookies')
        for side in ('bot', 'human'):
            with self.subTest(side=side):
                bot, human = self.tmp / f'b-{side}', self.tmp / f'h-{side}'
                make_db(bot if side == 'human' else human, [(2, '.a.com', '', 'sid', 'v', b'', '/')])
                broken = human if side == 'human' else bot
                (broken / 'Default').mkdir(parents=True, exist_ok=True)
                os.symlink(self.tmp / 'elsewhere', broken / 'Default/Network')
                with self.assertRaises(cookie_merge.MergeError):
                    cookie_merge.merge_cookies(human, bot)

    def test_a_symlinked_profile_root_is_refused(self):
        real = self.tmp / 'real-human'
        make_db(real, [(2, '.a.com', '', 'sid', 'human', b'', '/')])
        os.symlink(real, self.human)
        make_db(self.bot, [])
        with self.assertRaises(cookie_merge.MergeError):
            cookie_merge.merge_cookies(self.human, self.bot)

    def test_a_symlinked_sidecar_file_is_refused(self):
        bot = make_db(self.bot, [(1, '.a.com', '', 'sid', 'bot', b'', '/')])
        make_db(self.human, [(2, '.a.com', '', 'sid', 'human', b'', '/')])
        victim = self.tmp / 'victim'
        victim.write_text('precious')
        os.symlink(victim, str(bot) + '-journal')
        with self.assertRaises(cookie_merge.MergeError):
            cookie_merge.merge_cookies(self.human, self.bot)
        self.assertEqual(victim.read_text(), 'precious')

    def test_a_fifo_in_place_of_the_database_is_refused_without_blocking(self):
        make_db(self.human, [(2, '.a.com', '', 'sid', 'human', b'', '/')])
        fifo = self.bot / NEW
        fifo.parent.mkdir(parents=True)
        os.mkfifo(fifo)
        with self.assertRaises(cookie_merge.MergeError):
            cookie_merge.merge_cookies(self.human, self.bot)

    def test_a_hard_linked_database_is_refused(self):
        bot = make_db(self.bot, [(1, '.a.com', '', 'sid', 'bot', b'', '/')])
        os.link(bot, self.tmp / 'second-name')
        make_db(self.human, [(2, '.a.com', '', 'sid', 'human', b'', '/')])
        with self.assertRaises(cookie_merge.MergeError):
            cookie_merge.merge_cookies(self.human, self.bot)
        self.assertEqual([r[4] for r in dump(bot)], ['bot'])

    def test_trusted_schema_is_switched_off_before_any_other_statement(self):
        make_db(self.bot, [(1, '.bot.com', '', 'keep', 'k', b'', '/')])
        make_db(self.human, [(2, '.a.com', '', 'sid', 'human', b'', '/')])
        count, log = recorded_merge(self.human, self.bot)
        self.assertEqual(count, 1)
        self.assertEqual(log[0].replace(' ', '').upper(), 'PRAGMATRUSTED_SCHEMA=OFF')

    def test_the_whole_replacement_is_one_transaction(self):
        make_db(self.bot, [(1, '.a.com', '', 'sid', 'bot', b'', '/')])
        make_db(self.human, [(2, '.a.com', '', 'sid', 'human', b'', '/')])
        _, log = recorded_merge(self.human, self.bot)
        begins = [i for i, line in enumerate(log) if line.upper().startswith('BEGIN')]
        commits = [i for i, line in enumerate(log) if line.upper().startswith('COMMIT')]
        writes = [i for i, line in enumerate(log) if line.upper().startswith(('DELETE', 'INSERT'))]
        self.assertEqual((len(begins), len(commits)), (1, 1))
        self.assertEqual(len(writes), 2)
        self.assertTrue(all(begins[0] < i < commits[0] for i in writes), log)

    def test_cli_refuses_triggers_with_exit_code_one_and_an_empty_stdout(self):
        bot = make_db(self.bot, [])
        self.add_trigger(bot)
        make_db(self.human, [(2, '.a.com', '', 'sid', 'human', b'', '/')])
        done = subprocess.run([sys.executable, '-I', str(ROOT / 'cookie_merge.py'), str(self.human), str(self.bot)],
                              capture_output=True, text=True)
        self.assertEqual((done.returncode, done.stdout), (1, ''))
        self.assertIn('trigger', done.stderr)


if __name__ == '__main__':
    unittest.main()

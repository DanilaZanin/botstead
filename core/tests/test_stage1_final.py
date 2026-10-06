"""Этап 1, финальная доводка: git в контейнере, инструкции для Write/Edit, правила без op_hash,
Read не читает креды, читающие команды запоминаются, ложные метки не ставятся (п. 1-4, 8, 9).

Чистая логика без БД. Запуск без Postgres: pytest --noconftest tests/test_stage1_final.py
Пункты 5-7 (лимит при выходе из waiting, outbox, расход одобрения) в test_stage1_final_flow.py.
"""
import pytest

from bothub import risk
from bothub.builder import BASE_AUTO_ALLOW
from bothub.main import decide_permission
from bothub.risk import classify, op_hash, permission_class, remember_rule, rule_matches


def _bot(**over):
    return {"auto_allow": [], "mac_full_control": False} | over


def _base_bot(**over):
    return _bot(auto_allow=[{"tool": t} for t in BASE_AUTO_ALLOW]) | over


def _pin(tool, args):
    """Правило, как его сохраняет «запомнить»: точные аргументы плюс хэш операции."""
    return {"tool": tool, "match": {k: v for k, v in args.items()}, "op_hash": op_hash(tool, args)}


def _decide_with_rule(tool, args):
    return decide_permission(_bot(auto_allow=[_pin(tool, args)]), tool, args)


# --- 1. git в контейнере ----------------------------------------------------------------

def test_git_is_not_a_container_read_command():
    assert "git" not in risk._CONTAINER_READ_COMMANDS


@pytest.mark.parametrize("cmd", ["git status", "git status -s", "git log", "git diff", "git -C /home/bot/x status"])
def test_container_git_always_needs_confirmation_and_is_not_remembered(cmd):
    args = {"command": cmd}
    assert permission_class("Bash", args) == "blocked"
    assert remember_rule("Bash", args) is None
    # даже если правило как-то оказалось в БД (старое «запомнить»), авто не проходит
    assert _decide_with_rule("Bash", args)[1] is False
    assert decide_permission(_bot(auto_allow=[{"tool": "Bash", "match": args}]), "Bash", args)[1] is False


def test_git_status_after_remember_does_not_pass_auto():
    args = {"command": "git status"}
    assert decide_permission(_bot(), "Bash", args)[1] is False
    rule = remember_rule("Bash", args)  # то, что main.decide() сохранил бы после «запомнить»
    bot = _bot(auto_allow=[rule] if rule else [])
    assert decide_permission(bot, "Bash", args)[1] is False


# --- 2. Write/Edit для инструкционных файлов ---------------------------------------------

INSTRUCTION_NAMES = [
    "CLAUDE.md", "claude.md", "Claude.MD", "CLAUDE.local.md", "claude.LOCAL.md",
    "AGENTS.md", "agents.md", "Agents.Md", "GEMINI.md", "gemini.md",
    "Makefile", "makefile", "MAKEFILE", "GNUmakefile", "gnumakefile", "GNUMAKEFILE",
    "package.json", "Package.JSON", "conftest.py", "CONFTEST.PY", "pytest.ini", "Pytest.INI",
    "pyproject.toml", "PyProject.toml", "setup.py", "SETUP.PY", "setup.cfg", "Setup.Cfg",
    "sitecustomize.py", "SiteCustomize.py", "usercustomize.py", "UserCustomize.PY",
    "evil.pth", "EVIL.PTH", "a.b.Pth",
]
INSTRUCTION_DIR_FILES = [
    "node_modules/pkg/index.js", "NODE_MODULES/pkg/index.js", "Node_Modules/x.js",
    "proj/node_modules/.bin/tool", "bin/run.sh", "BIN/run.sh", "proj/Bin/tool", "a/b/bin/x",
    ".local/bin/tool", ".LOCAL/BIN/tool", "x/.local/bin/y",
]


@pytest.mark.parametrize("tool", ["Write", "Edit"])
@pytest.mark.parametrize("rel", INSTRUCTION_NAMES)
@pytest.mark.parametrize("prefix", ["/home/bot/", "/home/bot/proj/sub/"])
def test_instruction_files_need_confirmation(tool, rel, prefix):
    args = {"file_path": prefix + rel, "content": "x"}
    assert permission_class(tool, args) == "blocked"
    assert decide_permission(_base_bot(), tool, args)[1] is False
    assert decide_permission(_base_bot(auto_allow=[{"tool": "*"}, _pin(tool, args)]), tool, args)[1] is False
    assert remember_rule(tool, args) is None


@pytest.mark.parametrize("tool", ["Write", "Edit"])
@pytest.mark.parametrize("rel", INSTRUCTION_DIR_FILES)
def test_instruction_directories_need_confirmation(tool, rel):
    args = {"file_path": "/home/bot/" + rel, "content": "x"}
    assert decide_permission(_base_bot(), tool, args)[1] is False
    assert remember_rule(tool, args) is None


@pytest.mark.parametrize("path", [
    "/home/bot/work/../CLAUDE.md", "/home/bot/a/./AGENTS.md", "/home/bot/x/../node_modules/y.js",
    "/home/bot//Makefile",
])
def test_instruction_paths_are_normalized_before_check(path):
    assert decide_permission(_base_bot(), "Write", {"file_path": path, "content": "x"})[1] is False


@pytest.mark.parametrize("tool", ["Write", "Edit"])
@pytest.mark.parametrize("path", [
    "/home/bot/notes.md", "/home/bot/work/a/b.py", "/home/bot/report.json", "/home/bot/package.json.bak",
    "/home/bot/my_conftest.py", "/home/bot/setup.pyc", "/home/bot/binary/x.txt", "/home/bot/robin/x.txt",
    "/home/bot/node_modules_old/x.js", "/home/bot/pth/x.txt", "/home/bot/CLAUDE.md.txt", "/home/bot/mypytest.ini",
])
def test_ordinary_files_still_auto_allowed(tool, path):
    assert decide_permission(_base_bot(), tool, {"file_path": path, "content": "x"})[1] is True


# --- 3. правило с match без op_hash не совпадает -----------------------------------------

def test_rule_with_match_but_without_op_hash_never_matches():
    rule = {"tool": "send_message", "match": {"channel": "ops"}}
    assert rule_matches(rule, "send_message", {"channel": "ops"}) is False
    assert decide_permission(_bot(auto_allow=[rule]), "send_message", {"channel": "ops"})[1] is False


def test_rule_with_match_and_op_hash_matches_exactly():
    args = {"channel": "ops"}
    rule = _pin("send_message", args)
    assert rule_matches(rule, "send_message", args) is True
    assert decide_permission(_bot(auto_allow=[rule]), "send_message", args)[1] is True
    assert decide_permission(_bot(auto_allow=[rule]), "send_message", args | {"x": 1})[1] is False


@pytest.mark.parametrize("op", [None, "", 0])
def test_rule_with_empty_op_hash_counts_as_missing(op):
    rule = {"tool": "send_message", "match": {"channel": "ops"}, "op_hash": op}
    assert decide_permission(_bot(auto_allow=[rule]), "send_message", {"channel": "ops"})[1] is False


@pytest.mark.parametrize("tool,args", [
    ("Bash", {"command": "ls"}),
    ("mcp__bothub__mac_shell", {"cmd": "ls"}),
    ("mcp__bothub__mac_find_files", {"path": "/safe/a"}),
    ("read_file", {"path": "/safe/a"}),
])
def test_legacy_pinned_rule_without_op_hash_is_dead_for_safe_and_unknown_tools(tool, args):
    legacy = {"tool": tool, "match": dict(args)}
    assert decide_permission(_bot(auto_allow=[legacy]), tool, args)[1] is False
    assert decide_permission(_bot(auto_allow=[_pin(tool, args)]), tool, args)[1] is True


@pytest.mark.parametrize("rule", [{"tool": "Read"}, {"tool": "Read", "match": {}}, {"tool": "Read", "match": None}])
def test_tool_wide_rules_without_match_still_work(rule):
    assert decide_permission(_bot(auto_allow=[rule]), "Read", {"file_path": "/home/bot/a.txt"})[1] is True


def test_remember_rule_now_carries_op_hash():
    args = {"channel": "ops", "text": "hi"}
    rule = remember_rule("send_message", args)
    assert rule == {"tool": "send_message", "match": {"channel": "ops", "text": "hi"}, "op_hash": op_hash("send_message", args)}
    assert decide_permission(_bot(auto_allow=[rule]), "send_message", args)[1] is True


# --- 4. Read и attach_file не читают креды без подтверждения ------------------------------

CRED_PATHS = [
    "/home/bot/.claude/.credentials.json", "/home/bot/.claude/settings.json", "/home/bot/.claude/projects/x/y.jsonl",
    "/home/bot/.codex/auth.json", "/home/bot/.codex/config.toml",
    "/home/bot/.gemini/oauth_creds.json", "/home/bot/.gemini/settings.json",
    "/home/bot/.auth/token", "/home/bot/.auth/claude/x",
    "/home/bot/.ssh/id_rsa", "/home/bot/.ssh/id_ed25519", "/home/bot/.ssh/config", "/home/bot/.ssh/known_hosts",
    "/home/bot/.aws/credentials", "/home/bot/.aws/config",
    "/home/bot/.netrc", "/home/bot/proj/.netrc",
    "/home/bot/.env", "/home/bot/.env.local", "/home/bot/.env.production", "/home/bot/proj/sub/.env", "/home/bot/.envrc",
    "/proc/self/environ", "/proc/1/environ", "/proc/12345/environ", "/proc/self/task/1/environ",
    "/run/secrets/db_password", "/run/secrets/a/b", "/run/secrets",
    # нормализация: `..`, `.`, повторные слэши, регистр, ~, относительные пути
    "/home/bot/work/../.ssh/id_rsa", "/home/bot/./.claude/x", "/home/bot//.ssh//id_rsa", "/home/bot/.SSH/id_rsa",
    "/home/bot/.Claude/.credentials.json", "/home/bot/.ENV", "/proc/self/../self/environ", "/proc//self/environ",
    "/run/secrets/../secrets/x", "/home/bot/a/b/../../.aws/credentials",
    "~/.ssh/id_rsa", "~/.claude/.credentials.json", ".claude/.credentials.json", ".ssh/id_rsa", ".env",
    "/home/bot/.claude", "/home/bot/.ssh", "/home/bot/.claude.json", ".claude.json", "/home/bot/.claude.json.bak", "/home/bot/.codex.old/auth.json",
    # glob не раскрыть: проверить нечего
    "/home/bot/.cl*/.cred*", "/home/bot/.c?aude/x", "/home/bot/.[cs]sh/id_rsa", "/home/bot/.*/token", "/home/bot/*", "/proc/*/environ",
    "/run/sec*/x",
]
PLAIN_PATHS = [
    "/home/bot/a.txt", "/home/bot/work/env.md", "/home/bot/netrc.txt", "/home/bot/work/environ",
    "/home/bot/envelope.txt", "/home/bot/notes/ssh.md", "/home/bot/claude.txt", "/home/bot/work/.gitignore",
    "/tmp/report.csv", "/home/bot/run/secretsauce.txt", "work/a.txt", "~/a.txt",
]


@pytest.mark.parametrize("path", CRED_PATHS)
def test_read_of_credential_path_needs_confirmation(path):
    args = {"file_path": path}
    assert permission_class("Read", args) == "blocked"
    assert decide_permission(_base_bot(), "Read", args)[1] is False
    assert decide_permission(_bot(auto_allow=[{"tool": "*"}, _pin("Read", args)]), "Read", args)[1] is False
    assert remember_rule("Read", args) is None


@pytest.mark.parametrize("path", CRED_PATHS)
def test_attach_file_of_credential_path_needs_confirmation(path):
    tool, args = "mcp__bothub__attach_file", {"path": path}
    assert permission_class(tool, args) == "blocked"
    assert decide_permission(_base_bot(), tool, args)[1] is False
    assert decide_permission(_bot(auto_allow=[{"tool": "*"}, _pin(tool, args)]), tool, args)[1] is False
    assert remember_rule(tool, args) is None


@pytest.mark.parametrize("path", PLAIN_PATHS)
def test_read_and_attach_file_of_ordinary_paths_stay_auto(path):
    assert decide_permission(_base_bot(), "Read", {"file_path": path})[1] is True
    assert decide_permission(_base_bot(), "mcp__bothub__attach_file", {"path": path})[1] is True


@pytest.mark.parametrize("tool,args", [
    ("Read", {}), ("Read", {"file_path": None}), ("Read", {"file_path": ["/home/bot/a"]}), ("Read", {"file_path": 5}),
    ("Read", {"file_path": ""}), ("Read", {"file_path": "/home/bot/a\x00/.ssh/x"}),
    ("mcp__bothub__attach_file", {}), ("mcp__bothub__attach_file", {"path": None}), ("mcp__bothub__attach_file", {"path": ["/x"]}),
])
def test_read_and_attach_file_without_usable_path_need_confirmation(tool, args):
    assert permission_class(tool, args) == "blocked"
    assert decide_permission(_base_bot(), tool, args)[1] is False


def test_grep_path_argument_is_checked_too():
    assert decide_permission(_base_bot(), "Grep", {"pattern": "x", "path": "/home/bot/.ssh"})[1] is False
    assert decide_permission(_base_bot(), "Grep", {"pattern": "x", "path": "/home/bot/.claude/projects"})[1] is False
    assert decide_permission(_base_bot(), "Grep", {"pattern": "x", "path": "/home/bot/src"})[1] is True
    assert decide_permission(_base_bot(), "Grep", {"pattern": "x"})[1] is True


@pytest.mark.parametrize("cmd", [
    "cat /home/bot/.cl*/.cred*",
    "cat /home/bot/.claude/.credentials.json",
    "cat /home/bot/.codex/auth.json",
    "cat /home/bot/.gemini/oauth_creds.json",
    "cat /home/bot/.auth/token",
    "cat /home/bot/.ssh/id_rsa",
    "cat /home/bot/.aws/credentials",
    "cat /home/bot/.netrc",
    "cat /home/bot/.env",
    "cat /home/bot/.env.local",
    "cat /proc/self/environ",
    "cat /proc/1/environ",
    "cat /proc/*/environ",
    "cat /run/secrets/db_password",
    "cat .ssh/id_rsa",
    "cat .claude.json",
    "stat .claude.json",
    "cat .claude/.credentials.json",
    "cat ~/.ssh/id_rsa",
    "cat ~/.netrc",
    "cat /home/bot/work/../.ssh/id_rsa",
    "cat /home/bot//.ssh/id_rsa",
    "cat /home/bot/.SSH/id_rsa",
    "cat /home/bot/.c?aude/x",
    "cat /home/bot/.[cs]sh/*",
    "cat .cl*/.cred*",
    "ls /home/bot/.claude",
    "ls /home/bot/.ssh",
    "grep token /home/bot/.codex/auth.json",
    "grep -r x /home/bot/.gemini",
    "head -n 3 /home/bot/.ssh/id_rsa",
    "tail /home/bot/.aws/credentials",
    "wc -c /home/bot/.env",
    "stat /home/bot/.ssh/id_rsa",
    "file /home/bot/.netrc",
    "diff /home/bot/a /home/bot/.env",
    "sort /home/bot/.netrc",
    "uniq /home/bot/.auth/token",
    "du /home/bot/.ssh",
    "tree /home/bot/.claude",
    "find /home/bot/.ssh -type f",
    "find /home/bot/.cl* -type f",
    "echo /home/bot/.ssh/id_rsa",
    "wc --files0-from=/home/bot/.env",
])
def test_container_read_command_touching_credentials_is_not_remembered_or_auto(cmd):
    args = {"command": cmd}
    assert permission_class("Bash", args) == "blocked"
    assert remember_rule("Bash", args) is None
    assert _decide_with_rule("Bash", args)[1] is False


# --- 8. читающие команды запоминаются ----------------------------------------------------

READ_REMEMBERABLE = [
    "head a.txt", "head -n 5 a.txt", "head -c 100 /home/bot/work/a.txt",
    "tail a.txt", "tail -n 20 log.txt",
    "wc a.txt", "wc -l a.txt", "wc -lw /home/bot/work/a.txt",
    "find .", "find . -name x", "find /home/bot/work -type f -name '*.py'", "find . -maxdepth 2 -type d", "find . -iname '*.md' -mtime -3",
    "du work", "du -sh work", "du -h --max-depth=1 .",
    "sort a.txt", "sort -r a.txt", "sort -u -n a.txt", "sort -k2 a.txt",
    "uniq a.txt", "uniq -c a.txt", "uniq -d a.txt",
    "which python3", "which -a ls", "which git",
    "echo hello", "echo 'hello world'", "echo -n hi",
    "stat a.txt", "stat -c %s a.txt", "stat work",
    "file a.txt", "file -b a.txt", "file work/a.bin",
    "tree", "tree work", "tree -L 2 work", "tree -a -d .",
    "diff a.txt b.txt", "diff -u a.txt b.txt", "diff -q work other",
    # уже было
    "ls", "ls -la work", "cat a.txt", "grep x a.txt", "pwd",
]


@pytest.mark.parametrize("cmd", READ_REMEMBERABLE)
def test_single_read_command_is_rememberable_and_passes_auto_with_pinned_rule(cmd):
    args = {"command": cmd}
    assert permission_class("Bash", args) == "unknown"
    rule = remember_rule("Bash", args)
    assert rule is not None and rule["op_hash"] == op_hash("Bash", args)
    assert decide_permission(_bot(auto_allow=[rule]), "Bash", args) == ("other", True)
    # без правила и с чужим правилом: подтверждение
    assert decide_permission(_bot(), "Bash", args) == ("other", False)
    other = {"command": cmd + " other.txt"}
    assert decide_permission(_bot(auto_allow=[rule]), "Bash", other)[1] is False


@pytest.mark.parametrize("cmd", [
    "head", "tail", "wc", "find", "du", "sort", "uniq", "which", "echo", "stat", "file", "tree", "diff",
])
def test_each_new_container_read_program_is_in_the_whitelist(cmd):
    assert cmd in risk._CONTAINER_READ_COMMANDS


NOT_REMEMBERABLE = [
    # перенаправления
    "head a.txt > out.txt", "echo hi > out.txt", "echo hi >> out.txt", "sort a.txt < b.txt", "wc -l a.txt 2> err.txt",
    "tail a.txt >out",
    # пайпы и составные
    "sort a.txt | uniq", "head a.txt | wc -l", "find . -name x | head", "echo hi | tee out", "echo hi; echo bye",
    "diff a b && echo same", "sort a || echo x", "tail a.txt &",
    # подстановки и раскрытия
    "echo $(id)", "echo `id`", "echo $HOME", "wc -l $(ls)", "head $(echo a)", "echo ${HOME}", "echo <(ls)", "sort <(ls)",
    "echo {a,b}", "ls {a,b}",
    # find, который пишет или запускает
    "find . -exec rm {} ;", "find . -exec cat {} +", "find . -execdir sh x ;", "find . -ok rm {} ;", "find . -okdir rm {} ;",
    "find . -delete", "find . -name x -delete", "find . -fprint out.txt", "find . -fprintf out %p", "find . -fls out",
    # запись и запуск через флаги
    "sort -o out.txt a.txt", "sort --output=out.txt a.txt", "sort -oout.txt a.txt", "tree -o out.txt", "tree --output=out.txt",
    "sort --compress-program=sh a.txt", "uniq a.txt out.txt", "file -C", "file --compile",
    "tail -f log.txt", "tail -F log.txt", "tail --follow log.txt",
    # обёртки и пути к программам
    "env head a", "/usr/bin/head a.txt", "./head a.txt", "sudo head a", "xargs head", "nohup tail a",
    # glob вместо пути
    "cat *.txt", "head ?.txt", "ls [a-c]*", "wc -l *", "sort a*.txt", "diff a* b", "tree w*", "du -sh *", "stat *", "file *",
    # git и прочее не из списка
    "git status", "git log", "date", "env", "printenv", "cut -d, -f1 a.txt", "tr a b", "df", "python3 x.py", "make",
    "stat -f %z a.txt && rm a",
]


@pytest.mark.parametrize("cmd", NOT_REMEMBERABLE)
def test_non_plain_read_commands_are_not_remembered_and_never_auto(cmd):
    args = {"command": cmd}
    assert remember_rule("Bash", args) is None
    assert _decide_with_rule("Bash", args)[1] is False
    assert decide_permission(_bot(auto_allow=[{"tool": "Bash"}, {"tool": "*"}]), "Bash", args)[1] is False


@pytest.mark.parametrize("cmd", [
    "find . -name '*.py'", "find . -iname '*.MD'", "find . -path '*/src/*' -name x", "find . -regex '.*x'",
    "grep 'a*b' file.txt", "grep -e 'x*' a.txt", "grep -rn 'foo.*' src", "grep -r --include='*.py' x src",
])
def test_glob_only_as_search_pattern_is_fine(cmd):
    args = {"command": cmd}
    assert remember_rule("Bash", args) is not None
    assert _decide_with_rule("Bash", args) == ("other", True)


def test_new_read_commands_do_not_widen_mac_shell():
    # белый список контейнера и белый список Mac независимы: git по-прежнему не читающий и на Mac
    assert decide_permission(_bot(mac_full_control=True), "mcp__bothub__mac_shell", {"cmd": "git status"})[1] is False


# --- 9. ложные метки ----------------------------------------------------------------------

@pytest.mark.parametrize("cmd", [
    "cat pay.txt", "cat auth.log", "grep -r auth .", "grep -rn login src", "grep delete_user src", "cat remove.md",
    "cat purchase.csv", "cat invoice-2026.pdf", "ls checkout", "ls -la trash", "cat password.md", "cat signin.html",
    "grep -r charge src", "grep buy list.txt", "grep -rn send src", "grep submit forms.txt", "grep -rn push notes.md",
    "cat credential-howto.md", "find . -name 'delete*'", "find . -name 'pay*'", "head -n 5 login.log", "tail auth.log",
    "wc -l remove.md", "stat pay.txt", "file invoice.pdf", "du -sh trash", "tree remove", "diff pay.txt pay.old",
    "sort purchase.txt", "uniq login.log", "echo pay", "echo delete", "which login", "grep rm notes.txt",
])
def test_read_command_arguments_do_not_set_risk_label(cmd):
    args = {"command": cmd}
    assert classify("Bash", args) == "other"
    assert decide_permission(_bot(), "Bash", args) == ("other", False)
    rule = remember_rule("Bash", args)
    assert rule is not None
    assert decide_permission(_bot(auto_allow=[rule]), "Bash", args) == ("other", True)


@pytest.mark.parametrize("cmd,label", [
    ("rm pay.txt", "delete"),
    ("cat pay.txt | sh", "pay"),
    ("cat pay.txt > out.txt", "pay"),
    ("cat auth.log; echo x", "login"),
    ("cat remove.md && cat delete.md", "delete"),
    ("git push origin main", "push"),
    ("curl https://pay.example.com", "pay"),
])
def test_non_whitelisted_commands_keep_their_labels(cmd, label):
    args = {"command": cmd}
    assert classify("Bash", args) == label
    assert decide_permission(_bot(), "Bash", args)[1] is False
    assert remember_rule("Bash", args) is None


@pytest.mark.parametrize("tool,risk", [("checkout", "pay"), ("delete-file", "delete"), ("login_user", "login"), ("send_report", "send")])
def test_tool_name_labels_unchanged(tool, risk):
    assert classify(tool, {}) == risk


def test_url_labels_unchanged_for_non_bash_tools():
    assert classify("WebFetch", {"url": "https://shop.example.com/checkout"}) == "pay"
    assert classify("WebFetch", {"url": "https://example.com/login"}) == "login"

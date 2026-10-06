"""Exercise the dev bootstrap sequence without Docker or PostgreSQL."""

import json
import os
import subprocess
from pathlib import Path

import pytest


pytestmark = pytest.mark.pure
ROOT = Path(__file__).resolve().parents[2]


def _run_dev_script(tmp_path, *, fail_bot=False):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name in ("docker", "uv"):
        executable = fake_bin / name
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)
    curl = fake_bin / "curl"
    curl.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "args=sys.argv[1:]\n"
        "url=next((a for a in args if a.startswith('http://')), '')\n"
        "path=url.split('127.0.0.1:8000')[-1]\n"
        "body=json.loads(args[args.index('-d')+1]) if '-d' in args else {}\n"
        "with Path(os.environ['DEV_CALL_LOG']).open('a') as out: out.write(json.dumps({'path':path,'body':body,'args':args})+'\\n')\n"
        "if path=='/api/health': print('{\"ok\":true}')\n"
        "elif path=='/api/setup': print('{\"id\":\"setup-admin\"}')\n"
        "elif path=='/api/bots':\n"
        "  if os.environ.get('DEV_FAIL_BOT')=='1':\n"
        "    print('{\"error\":\"invalid\"}')\n"
        "    sys.exit(22 if '--fail-with-body' in args or '-f' in args else 0)\n"
        "  print(json.dumps({'id':'generated-'+body.get('id', body.get('name', 'bot'))}))\n"
        "else: print('{}')\n"
    )
    curl.chmod(0o755)
    log = tmp_path / "calls.jsonl"
    env = os.environ | {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "DEV_CALL_LOG": str(log),
        "DEV_FAIL_BOT": "1" if fail_bot else "0",
        "FILES_DIR": str(tmp_path / "files"),
        "OWNER_TOKEN": "dev-test-token",
    }
    result = subprocess.run(["bash", str(ROOT / "scripts/dev.sh")], cwd=ROOT, env=env, capture_output=True, text=True, timeout=20)
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    return result, calls


def test_dev_script_sets_up_before_seeding_and_uses_api_bot_ids(tmp_path):
    result, calls = _run_dev_script(tmp_path)
    assert result.returncode == 0, result.stderr
    posts = [call for call in calls if call["path"] != "/api/health"]
    assert posts[0]["path"] == "/api/setup"
    assert any("Authorization: Bearer dev-test-token" in arg for arg in posts[0]["args"])
    assert all(call["path"] == "/api/setup" for call in posts[:1])
    assert any(call["path"] == "/api/bots" for call in posts)
    assert next(call for call in posts if call["path"] == "/api/schedules")["body"]["bot_id"] == "generated-sre"
    assert next(call for call in posts if call["path"] == "/api/memory" and call["body"].get("bot_id"))["body"]["bot_id"] == "generated-sre"


def test_dev_script_reports_seed_failure(tmp_path):
    result, calls = _run_dev_script(tmp_path, fail_bot=True)
    assert result.returncode != 0
    assert any(call["path"] == "/api/bots" for call in calls)
    assert "invalid" in result.stderr or "invalid" in result.stdout

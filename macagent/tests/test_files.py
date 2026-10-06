import base64
import os

import pytest

from bothub_mac.tools import files
from bothub_mac.tools.errors import ToolError


def test_build_predicate_plain():
    predicate = files.build_predicate("report", kind="any")
    assert predicate == 'kMDItemFSName == "*report*"cd'


def test_build_predicate_kind_and_since():
    predicate = files.build_predicate("invoice", kind="pdf", since="2026-01-01")
    assert 'kMDItemFSName == "*invoice*"cd' in predicate
    assert 'kMDItemContentType == "com.adobe.pdf"' in predicate
    assert '$time.iso("2026-01-01")' in predicate


def test_build_predicate_content_search():
    predicate = files.build_predicate("term", content=True)
    assert predicate.startswith("kMDItemTextContent")


def test_build_predicate_escapes_quotes():
    predicate = files.build_predicate('a"b')
    assert 'a\\"b' in predicate


def test_build_predicate_unknown_kind():
    with pytest.raises(ToolError):
        files.build_predicate("x", kind="spreadsheet")


def test_find_files_uses_mdfind_and_stats(tmp_path, monkeypatch):
    f = tmp_path / "report.pdf"
    f.write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(files, "_run_mdfind", lambda predicate: [str(f)])
    result = files.find_files({"query": "report"})
    assert len(result) == 1
    assert result[0]["name"] == "report.pdf"
    assert result[0]["kind"] == "pdf"
    assert result[0]["size"] == f.stat().st_size


def test_find_files_skips_missing_paths(monkeypatch):
    monkeypatch.setattr(files, "_run_mdfind", lambda predicate: ["/no/such/file"])
    assert files.find_files({"query": "x"}) == []


def test_find_files_requires_query():
    with pytest.raises(ToolError):
        files.find_files({})


def test_find_files_respects_limit(tmp_path, monkeypatch):
    paths = []
    for i in range(5):
        p = tmp_path / f"f{i}.txt"
        p.write_text("x")
        paths.append(str(p))
    monkeypatch.setattr(files, "_run_mdfind", lambda predicate: paths)
    result = files.find_files({"query": "f", "limit": 2})
    assert len(result) == 2


def test_read_file_roundtrip(tmp_path):
    p = tmp_path / "note.txt"
    p.write_text("hello")
    data = files.read_file({"path": str(p)})
    assert data["size"] == 5
    assert base64.b64decode(data["content_b64"]) == b"hello"
    assert data["mime"].startswith("text/")


def test_read_file_missing_raises():
    with pytest.raises(ToolError):
        files.read_file({"path": "/no/such/file.txt"})


def test_read_file_truncates_to_max_bytes(tmp_path):
    p = tmp_path / "big.bin"
    p.write_bytes(b"x" * 100)
    data = files.read_file({"path": str(p), "max_bytes": 10})
    assert len(base64.b64decode(data["content_b64"])) == 10
    assert data["size"] == 100


def test_move_to_trash_missing_raises():
    with pytest.raises(ToolError):
        files.move_to_trash({"path": "/no/such/file"})


def test_move_to_trash_passes_quoted_path_as_argv_not_script_text(tmp_path, monkeypatch):
    # Путь с кавычкой и обратным слэшем не должен ломать/дополнять AppleScript:
    # он обязан прийти как отдельный argv-элемент после "--", а не быть частью
    # текста -e скрипта.
    evil = tmp_path / 'a"b\\c'
    evil.write_text("x")
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd

        class Result:
            returncode = 0
            stderr = ""

        return Result()

    monkeypatch.setattr(files.subprocess, "run", fake_run)
    files.move_to_trash({"path": str(evil)})

    cmd = captured["cmd"]
    assert cmd[0] == "osascript"
    assert cmd[-2] == "--"
    assert cmd[-1] == str(evil)
    script_args = [a for i, a in enumerate(cmd) if cmd[i - 1] == "-e"]
    assert all(str(evil) not in a for a in script_args)

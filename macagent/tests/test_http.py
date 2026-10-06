import json

import pytest

from bothub_mac.config import Config
from bothub_mac.tools import http
from bothub_mac.tools.errors import ToolError


def test_build_multipart_contains_fields_and_file():
    body, boundary = http.build_multipart(
        {"thread_id": "t1", "origin": "mac:/x/y.png"}, ("file", "y.png", "image/png", b"\x89PNG")
    )
    text = body.decode("latin-1")
    assert boundary in text
    assert 'name="thread_id"' in text
    assert "t1" in text
    assert 'name="file"; filename="y.png"' in text
    assert "Content-Type: image/png" in text
    assert body.endswith(f"--{boundary}--\r\n".encode())


def test_upload_file_missing_args_raises(tmp_path):
    config = Config(bothub_url="https://x", mac_agent_token="t")
    with pytest.raises(ToolError):
        http.upload_file({"path": str(tmp_path / "missing.png")}, config)


def test_upload_file_posts_multipart(tmp_path, monkeypatch):
    p = tmp_path / "a.png"
    p.write_bytes(b"\x89PNG")
    config = Config(bothub_url="https://bots.example.com", mac_agent_token="secret-token")

    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({"file_id": "abc"}).encode()

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.header_items())
        captured["method"] = req.get_method()
        return FakeResponse()

    monkeypatch.setattr(http.urllib.request, "urlopen", fake_urlopen)
    result = http.upload_file({"path": str(p), "thread_id": "t1"}, config)

    assert result == {"file_id": "abc"}
    assert captured["url"] == "https://bots.example.com/api/files"
    assert captured["method"] == "POST"
    assert captured["headers"]["Authorization"] == "Bearer secret-token"


def test_upload_file_uses_current_connected_address(tmp_path, monkeypatch):
    """При переключении на запасной адрес upload_file шлёт файл туда же, куда ходит WS."""
    p = tmp_path / "a.png"
    p.write_bytes(b"\x89PNG")
    config = Config(
        bothub_url="https://primary.example.com", mac_agent_token="secret-token",
        bothub_urls=("https://primary.example.com", "https://fallback.example.com"),
    )
    config.current.url = "https://fallback.example.com"

    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({"file_id": "abc"}).encode()

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        return FakeResponse()

    monkeypatch.setattr(http.urllib.request, "urlopen", fake_urlopen)
    http.upload_file({"path": str(p), "thread_id": "t1"}, config)

    assert captured["url"] == "https://fallback.example.com/api/files"

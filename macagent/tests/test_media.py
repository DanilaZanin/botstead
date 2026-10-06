"""preview/screenshot ужимаются через sips в jpeg; подмена subprocess."""
import base64
import os
import subprocess
from pathlib import Path

import pytest

from bothub_mac.tools import media
from bothub_mac.tools.errors import ToolError


def _fake_run(pixel_w=1440, pixel_h=900, sips_resize_fails=False, capture=None):
    def run(cmd, **kwargs):
        if capture is not None:
            capture.append(cmd)
        if cmd[0] == "screencapture":
            Path(cmd[-1]).write_bytes(b"RAWPNG")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[0] == "qlmanage":
            outdir = cmd[cmd.index("-o") + 1]
            stem = os.path.basename(cmd[-1])
            Path(outdir, f"{stem}.png").write_bytes(b"RAWPNG")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[0] == "sips" and "-g" in cmd:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=f"pixelWidth: {pixel_w}\npixelHeight: {pixel_h}\n", stderr=""
            )
        if cmd[0] == "sips":
            if sips_resize_fails:
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="sips: bad format")
            out_path = cmd[cmd.index("-o") + 1]
            Path(out_path).write_bytes(b"JPEGDATA")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {cmd}")

    return run


def test_screenshot_returns_jpeg_with_dimensions(monkeypatch):
    monkeypatch.setattr(media.subprocess, "run", _fake_run())
    result = media.screenshot({})
    assert base64.b64decode(result["jpeg_b64"]) == b"JPEGDATA"
    assert result["width"] == 1440
    assert result["height"] == 900
    assert result["bytes"] == len(b"JPEGDATA")


def test_screenshot_uses_default_max_side_1440(monkeypatch):
    captured = []
    monkeypatch.setattr(media.subprocess, "run", _fake_run(capture=captured))
    media.screenshot({})
    resize_cmd = next(c for c in captured if c[0] == "sips" and "-Z" in c)
    assert resize_cmd[resize_cmd.index("-Z") + 1] == "1440"
    assert "format" in resize_cmd and "jpeg" in resize_cmd


def test_screenshot_respects_custom_max_side_and_format(monkeypatch):
    captured = []
    monkeypatch.setattr(media.subprocess, "run", _fake_run(capture=captured))
    media.screenshot({"max_side": 640, "format": "png"})
    resize_cmd = next(c for c in captured if c[0] == "sips" and "-Z" in c)
    assert resize_cmd[resize_cmd.index("-Z") + 1] == "640"
    assert "png" in resize_cmd


def test_screenshot_raises_on_capture_failure(monkeypatch):
    def failing_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="no permission")
    monkeypatch.setattr(media.subprocess, "run", failing_run)
    with pytest.raises(ToolError):
        media.screenshot({})


def test_screenshot_raises_on_sips_resize_failure(monkeypatch):
    monkeypatch.setattr(media.subprocess, "run", _fake_run(sips_resize_fails=True))
    with pytest.raises(ToolError):
        media.screenshot({})


def test_preview_uses_default_max_side_800(monkeypatch, tmp_path):
    p = tmp_path / "doc.pdf"
    p.write_bytes(b"x")
    captured = []
    monkeypatch.setattr(media.subprocess, "run", _fake_run(capture=captured))
    result = media.preview({"path": str(p)})
    resize_cmd = next(c for c in captured if c[0] == "sips" and "-Z" in c)
    assert resize_cmd[resize_cmd.index("-Z") + 1] == "800"
    assert base64.b64decode(result["jpeg_b64"]) == b"JPEGDATA"


def test_preview_missing_path_raises():
    with pytest.raises(ToolError):
        media.preview({})


def test_preview_nonexistent_path_raises(tmp_path):
    with pytest.raises(ToolError):
        media.preview({"path": str(tmp_path / "missing.pdf")})

"""`agent-bus quota record|show`."""

from __future__ import annotations

import io
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from agent_bus import cli, config
from agent_bus.dispatch import settings

MARKER = "marker-path-do-not-store"
PAYLOAD = {
    "cwd": MARKER,
    "workspace": {"current_dir": MARKER},
    "rate_limits": {
        "five_hour": {"used_percentage": 40.0, "resets_at": 1790000000},
        "seven_day": {"used_percentage": 10, "resets_at": 1790500000},
    },
}


class FakeStdin:
    def __init__(self, data: bytes):
        self.buffer = io.BytesIO(data)


class FakeStdout:
    def __init__(self):
        self.buffer = io.BytesIO()

    def write(self, text):
        self.buffer.write(text.encode())

    def flush(self):
        pass


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


def _record(monkeypatch, data: bytes, *extra: str):
    out = FakeStdout()
    monkeypatch.setattr(sys, "stdin", FakeStdin(data))
    monkeypatch.setattr(sys, "stdout", out)
    code = cli.main(["quota", "record", *extra])
    return code, out.buffer.getvalue()


def _snapshot() -> Path:
    return settings.quota_dir(config.defaults()["dispatch"]) / "claude.json"


def test_record_echoes_arbitrary_bytes(home, monkeypatch, capsys):
    for data in (b"", b"not json", b"\xff\xfe\x00 bytes", b"[1, 2]", b'{"rate_limits": 3}'):
        code, out = _record(monkeypatch, data)
        assert code == 0 and out == data
    assert capsys.readouterr().err == ""
    assert not _snapshot().exists()


def test_record_stores_a_private_snapshot(home, monkeypatch, capsys):
    raw = json.dumps(PAYLOAD).encode()
    code, out = _record(monkeypatch, raw)
    assert code == 0 and out == raw
    path = _snapshot()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    body = json.loads(path.read_text())
    assert set(body) == {"client", "recorded_at", "windows"}
    assert set(body["windows"]) == {"five_hour", "seven_day"}
    assert MARKER not in path.read_text()
    assert capsys.readouterr().err == ""


def test_record_survives_an_unwritable_folder(home, monkeypatch, capsys):
    folder = _snapshot().parent
    folder.parent.mkdir(parents=True, exist_ok=True)
    folder.write_text("a file where the folder should be")
    raw = json.dumps(PAYLOAD).encode()
    code, out = _record(monkeypatch, raw)
    assert code == 0 and out == raw
    assert capsys.readouterr().err == ""


def test_record_survives_an_invalid_config(home, monkeypatch, capsys):
    config.config_path().parent.mkdir(parents=True, exist_ok=True)
    config.config_path().write_text("{ not json", encoding="utf-8")
    raw = json.dumps(PAYLOAD).encode()
    code, out = _record(monkeypatch, raw)
    assert code == 0 and out == raw
    assert capsys.readouterr().err == ""
    assert _snapshot().exists()


def test_record_survives_an_unexpected_error(home, monkeypatch, capsys):
    from agent_bus.dispatch import quota_cli

    def boom(data, client):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(quota_cli, "_record", boom)
    raw = json.dumps(PAYLOAD).encode()
    code, out = _record(monkeypatch, raw)
    assert code == 0 and out == raw
    assert capsys.readouterr().err == ""


def test_record_survives_a_closed_stdout(home, monkeypatch, capsys):
    class Broken:
        class buffer:
            @staticmethod
            def write(data):
                raise BrokenPipeError

            @staticmethod
            def flush():
                raise BrokenPipeError

    monkeypatch.setattr(sys, "stdin", FakeStdin(json.dumps(PAYLOAD).encode()))
    monkeypatch.setattr(sys, "stdout", Broken)
    assert cli.main(["quota", "record"]) == 0


def test_show_text_and_json(home, monkeypatch, capsys):
    real_stdin, real_stdout = sys.stdin, sys.stdout
    _record(monkeypatch, json.dumps(PAYLOAD).encode())
    monkeypatch.setattr(sys, "stdin", real_stdin)
    monkeypatch.setattr(sys, "stdout", real_stdout)
    capsys.readouterr()
    config.config_path().parent.mkdir(parents=True, exist_ok=True)
    config.config_path().write_text(
        json.dumps({"dispatch": {"routing": {"quota": {"enabled": True}}}}), encoding="utf-8"
    )
    assert cli.main(["quota", "show"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "quota routing: on"
    assert "factor=" in out
    assert cli.main(["quota", "show", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["enabled"] is True
    assert all("factor" in row for row in data["clients"])


def test_show_reports_off_by_default(home, capsys):
    assert cli.main(["quota", "show"]) == 0
    assert capsys.readouterr().out.splitlines()[0] == "quota routing: off"


def test_show_with_a_broken_config_exits_1(home, capsys):
    config.config_path().parent.mkdir(parents=True, exist_ok=True)
    config.config_path().write_text("{ not json", encoding="utf-8")
    assert cli.main(["quota", "show"]) == 1
    assert "agent-bus:" in capsys.readouterr().err

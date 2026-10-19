"""Claude Code wiring of the background wake hooks."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from agent_bus import clients
from agent_bus.clients import claude
from agent_bus.clients.base import WAKE_EXIT_CODE

# the real check, from before the autouse fixture stands it in
real_rewake_unsupported_reason = claude.rewake_unsupported_reason
BIN = "/opt/agent-bus/bin/agent-bus"


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "acme"
    (repo / ".git").mkdir(parents=True)
    return repo


def _settings(repo: Path) -> dict:
    return json.loads((repo / ".claude" / "settings.json").read_text())


def _watch_hooks(repo: Path, event: str) -> list[dict]:
    return [
        h for block in _settings(repo)["hooks"].get(event, [])
        for h in block["hooks"] if h.get("asyncRewake")
    ]


def _wire(repo: Path) -> None:
    clients.wiring("claude").apply(repo, name="acme/claude", bin_path=BIN)


def test_a_watcher_starts_with_each_session_and_after_each_turn(tmp_path):
    repo = _repo(tmp_path)
    _wire(repo)
    for event in ("SessionStart", "Stop"):
        (hook,) = _watch_hooks(repo, event)
        assert hook["asyncRewake"] is True
        assert hook["rewakeSummary"] == claude.REWAKE_SUMMARY
        assert "hook-watch --client claude" in hook["command"]
        assert hook["command"].startswith(f"AGENT_BUS_NAME=acme/claude {BIN} ")
    assert _watch_hooks(repo, "UserPromptSubmit") == []


def test_claude_code_kills_the_watcher_only_after_it_gave_up(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BUS_WATCH_HOURS", "2")
    repo = _repo(tmp_path)
    _wire(repo)
    (hook,) = _watch_hooks(repo, "Stop")
    assert "--max-seconds 7200 " in hook["command"]
    assert hook["timeout"] == 7200 + claude.WATCH_TIMEOUT_MARGIN_SECONDS


def test_the_watch_time_stays_within_what_claude_code_can_time(monkeypatch):
    monkeypatch.setenv("AGENT_BUS_WATCH_HOURS", str(24 * 365))
    seconds = claude.watch_seconds()
    assert (seconds + claude.WATCH_TIMEOUT_MARGIN_SECONDS) * 1000 <= 2**31 - 1


def test_turning_native_wake_off_removes_the_watchers(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _wire(repo)
    monkeypatch.setenv("AGENT_BUS_NATIVE_WAKE", "0")
    _wire(repo)
    assert _watch_hooks(repo, "Stop") == []
    assert "SessionStart" not in _settings(repo)["hooks"]
    assert any("hook-stop" in h["command"]
               for b in _settings(repo)["hooks"]["Stop"] for h in b["hooks"])
    assert "AGENT_BUS_NATIVE_WAKE" in clients.wiring("claude").limitations[0]


def test_a_users_own_session_start_hook_is_kept(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    mine = {"hooks": [{"type": "command", "command": "echo hello"}]}
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"SessionStart": [mine]}}))
    _wire(repo)
    monkeypatch.setenv("AGENT_BUS_NATIVE_WAKE", "0")
    _wire(repo)
    assert _settings(repo)["hooks"]["SessionStart"] == [mine]


def test_no_watchers_for_a_claude_code_that_cannot_wake(tmp_path, monkeypatch):
    monkeypatch.setattr(claude, "rewake_unsupported_reason", lambda: "Claude Code 2.0.1 is older")
    repo = _repo(tmp_path)
    _wire(repo)
    assert _watch_hooks(repo, "Stop") == []
    assert "older" in clients.wiring("claude").limitations[0]


# ----------------------------- version check --------------------------------


def _fake_claude(tmp_path: Path, monkeypatch, script: str) -> None:
    path = tmp_path / "claude"
    path.write_text("#!/bin/sh\n" + script + "\n")
    path.chmod(0o755)
    monkeypatch.setenv("AGENT_BUS_CLAUDE_BIN", str(path))
    real_rewake_unsupported_reason.cache_clear()


@pytest.fixture(autouse=True)
def _forget_versions():
    yield
    real_rewake_unsupported_reason.cache_clear()


def test_a_recent_claude_code_can_wake(tmp_path, monkeypatch):
    _fake_claude(tmp_path, monkeypatch, 'echo "2.1.303 (Claude Code)"')
    assert real_rewake_unsupported_reason() is None


def test_the_first_version_that_can_wake(tmp_path, monkeypatch):
    version = ".".join(map(str, claude.MIN_REWAKE_VERSION))
    _fake_claude(tmp_path, monkeypatch, f'echo "{version} (Claude Code)"')
    assert real_rewake_unsupported_reason() is None


def test_an_older_claude_code_cannot(tmp_path, monkeypatch):
    _fake_claude(tmp_path, monkeypatch, 'echo "2.1.200 (Claude Code)"')
    assert "2.1.200 is older" in real_rewake_unsupported_reason()


def test_a_missing_claude_code_cannot(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BUS_CLAUDE_BIN", str(tmp_path / "nowhere"))
    real_rewake_unsupported_reason.cache_clear()
    assert "could not ask" in real_rewake_unsupported_reason()


def test_a_claude_code_that_does_not_say_its_version_cannot(tmp_path, monkeypatch):
    _fake_claude(tmp_path, monkeypatch, "echo hello")
    assert "did not report" in real_rewake_unsupported_reason()


# ----------------------------- the command, run for real --------------------


def _run_watch_command(tmp_path: Path, exit_code: int | None) -> subprocess.CompletedProcess:
    """Run the written hook command with a stand-in binary that exits
    `exit_code`; None means no binary at all."""
    binary = tmp_path / "agent-bus"
    if exit_code is not None:
        binary.write_text(f"#!/bin/sh\necho 'mail for you' >&2\nexit {exit_code}\n")
        binary.chmod(0o755)
    repo = _repo(tmp_path)
    clients.wiring("claude").apply(repo, name="acme/claude", bin_path=str(binary))
    (hook,) = _watch_hooks(repo, "Stop")
    return subprocess.run(
        ["sh", "-c", hook["command"]], capture_output=True, text=True, timeout=30,
        stdin=subprocess.DEVNULL, env={**os.environ},
    )


def test_a_wake_reaches_claude_code_as_exit_2(tmp_path):
    done = _run_watch_command(tmp_path, WAKE_EXIT_CODE)
    assert done.returncode == 2
    assert "mail for you" in done.stderr


@pytest.mark.parametrize("exit_code", [0, 1, 2, 3])
def test_anything_else_is_silent_success(tmp_path, exit_code):
    """In particular 2: an agent-bus too old to know `hook-watch` fails in
    its argument parser with exit 2, which must not wake the model."""
    assert _run_watch_command(tmp_path, exit_code).returncode == 0


def test_a_missing_binary_does_not_wake(tmp_path):
    assert _run_watch_command(tmp_path, None).returncode == 0

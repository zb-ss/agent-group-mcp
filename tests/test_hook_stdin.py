"""A hook must never hold the agent's turn open waiting for input.

A hook runs inside a turn: the client waits for it before making its next
model request. These tests run the real CLI with the awkward kinds of stdin
a client can leave it holding.
"""

from __future__ import annotations

import io
import json
import os
import pty
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_bus import hooks

HOOKS = ("hook-user-prompt", "hook-stop")


def _env(bus_paths, **extra) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_BUS_")}
    root = Path(__file__).resolve().parents[1] / "src"
    env.update(
        AGENT_BUS_DB=str(bus_paths["db"]),
        AGENT_BUS_AUDIT_LOG=str(bus_paths["log"]),
        PYTHONPATH=os.pathsep.join(filter(None, (str(root), env.get("PYTHONPATH")))),
        **extra,
    )
    return env


def _run(subcommand: str, *, env: dict, stdin, timeout: float):
    proc = subprocess.Popen(
        [sys.executable, "-m", "agent_bus.cli", subcommand, "--client", "claude"],
        stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, text=True,
    )
    began = time.monotonic()
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        pytest.fail(f"{subcommand} was still running after {timeout}s — it hung")
    return out, err, time.monotonic() - began


@pytest.mark.parametrize("subcommand", HOOKS)
def test_a_terminal_for_stdin_does_not_hang_the_hook(subcommand, bus_paths, tmp_path):
    """Regression: a plugin that runs the hook without redirecting input
    leaves it holding the client's terminal, which never sends end-of-input.
    Every turn then hung before reaching the model."""
    env = _env(bus_paths, AGENT_BUS_NAME="repo-a/claude", AGENT_BUS_REPO=str(tmp_path))
    parent, child = pty.openpty()
    try:
        out, err, took = _run(subcommand, env=env, stdin=child, timeout=15)
    finally:
        os.close(child)
        os.close(parent)
    assert took < 10, f"took {took:.1f}s"
    assert err == "", err


@pytest.mark.parametrize("subcommand", HOOKS)
def test_a_pipe_nobody_writes_to_does_not_hang_the_hook(subcommand, bus_paths, tmp_path):
    """The other way a client can leave a hook waiting: an inherited pipe it
    keeps open. The hook gives up on the payload rather than on the turn."""
    env = _env(bus_paths, AGENT_BUS_NAME="repo-a/claude", AGENT_BUS_REPO=str(tmp_path),
               AGENT_BUS_HOOK_PAYLOAD_TIMEOUT="0.5")
    read_end, write_end = os.pipe()
    try:
        out, err, took = _run(subcommand, env=env, stdin=read_end, timeout=15)
    finally:
        os.close(read_end)
        os.close(write_end)  # deliberately closed only after the hook gave up
    assert took < 10, f"took {took:.1f}s"
    assert err == "", err


@pytest.mark.parametrize(
    "raw,expected",
    [(None, 5.0), ("", 5.0), ("0.25", 0.25), ("0", 0.0), ("-1", 5.0), ("soon", 5.0)],
)
def test_the_payload_timeout_is_configurable(raw, expected, monkeypatch):
    from agent_bus import settings

    if raw is None:
        monkeypatch.delenv(settings.HOOK_PAYLOAD_TIMEOUT_ENV, raising=False)
    else:
        monkeypatch.setenv(settings.HOOK_PAYLOAD_TIMEOUT_ENV, raw)
    assert settings.hook_payload_timeout_seconds() == expected


@pytest.mark.parametrize("subcommand", HOOKS)
def test_a_normal_piped_payload_is_still_read_in_full(subcommand, bus_paths, tmp_path):
    """The fix must not cost us the payload clients actually send."""
    from agent_bus.storage import Storage

    repo = tmp_path / "repo-a"
    (repo / ".git").mkdir(parents=True)
    store = Storage()
    store.upsert_agent("repo-a/claude", str(repo))
    store.ensure_agent("human", "/home")
    store.send_message(from_agent="human", to="repo-a/claude", body="payload arrived")

    # no AGENT_BUS_NAME: the agent can only be found via the payload's cwd,
    # so this fails unless the payload was read
    env = _env(bus_paths)
    payload = json.dumps({"session_id": "s", "cwd": str(repo / "src"),
                          "hook_event_name": "Stop", "padding": "x" * 20_000})
    proc = subprocess.Popen(
        [sys.executable, "-m", "agent_bus.cli", subcommand, "--client", "claude"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, text=True,
    )
    out, err = proc.communicate(payload, timeout=20)
    assert err == "", err
    assert "payload arrived" in out, out


def test_an_in_memory_stream_is_read_directly():
    """Tests and library callers pass a StringIO, which has no file
    descriptor to select on."""
    assert hooks._read_available(io.StringIO('{"cwd": "/x"}')) == '{"cwd": "/x"}'


def test_a_terminal_reads_as_no_payload():
    class FakeTerminal(io.StringIO):
        def isatty(self) -> bool:
            return True

        def read(self, *args):  # pragma: no cover - must never be called
            raise AssertionError("a terminal must not be read from")

    assert hooks._read_available(FakeTerminal()) == ""

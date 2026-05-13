"""CLI subcommands: send, inbox, agents, tail (incl. follow), hook entrypoints."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


def _run_cli(args, env_extra=None, input_text=None, timeout=15):
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "agent_bus.cli", *args],
        env=env,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_cli_send_then_inbox(bus_paths):
    env = {
        "AGENT_BUS_DB": str(bus_paths["db"]),
        "AGENT_BUS_AUDIT_LOG": str(bus_paths["log"]),
    }
    # register beta first so a broadcast has a target
    from agent_bus.storage import Storage
    s = Storage()
    s.upsert_agent("beta", "/repo/beta")

    # use --json so the assertion is independent of cosmetic formatting
    r = _run_cli(["send", "--name", "human", "--to", "beta",
                  "hello from cli", "--json"],
                 env_extra=env)
    assert r.returncode == 0, r.stderr
    sent = json.loads(r.stdout)
    assert sent["recipients"] == ["beta"]
    assert sent["message_id"]

    r = _run_cli(["inbox", "--name", "beta", "--json"], env_extra=env)
    assert r.returncode == 0, r.stderr
    msgs = json.loads(r.stdout)
    assert len(msgs) == 1
    assert msgs[0]["body"] == "hello from cli"
    assert msgs[0]["from"] == "human"


def test_cli_agents_lists_registered(bus_paths):
    env = {
        "AGENT_BUS_DB": str(bus_paths["db"]),
        "AGENT_BUS_AUDIT_LOG": str(bus_paths["log"]),
    }
    from agent_bus.storage import Storage
    s = Storage()
    s.upsert_agent("alpha", "/repo/a")
    s.upsert_agent("beta", "/repo/b")

    r = _run_cli(["agents", "--json"], env_extra=env)
    assert r.returncode == 0, r.stderr
    rows = json.loads(r.stdout)
    names = sorted(r["name"] for r in rows)
    assert names == ["alpha", "beta"]


def test_cli_tail_static_json(bus_paths):
    env = {
        "AGENT_BUS_DB": str(bus_paths["db"]),
        "AGENT_BUS_AUDIT_LOG": str(bus_paths["log"]),
    }
    from agent_bus.storage import Storage
    s = Storage()
    s.upsert_agent("a", "/repo/a")
    s.upsert_agent("b", "/repo/b")
    s.send_message(from_agent="a", to="b", body="audit-this")

    r = _run_cli(["tail", "--limit", "10", "--json"], env_extra=env)
    assert r.returncode == 0
    rows = [json.loads(l) for l in r.stdout.splitlines() if l.strip()]
    assert any(row["op"] == "send" and row["from"] == "a" for row in rows)


def test_cli_tail_static_plain(bus_paths):
    """Default tail output is a one-line human summary, not JSON."""
    env = {
        "AGENT_BUS_DB": str(bus_paths["db"]),
        "AGENT_BUS_AUDIT_LOG": str(bus_paths["log"]),
    }
    from agent_bus.storage import Storage
    s = Storage()
    s.upsert_agent("a", "/repo/a")
    s.upsert_agent("b", "/repo/b")
    s.send_message(from_agent="a", to="b", body="audit-plain-line")

    r = _run_cli(["tail", "--limit", "10"], env_extra=env)
    assert r.returncode == 0
    # the body preview and agent names should be in the rendered line,
    # but the line should NOT be parseable as JSON (it's human output)
    assert "audit-plain-line" in r.stdout
    assert "send" in r.stdout
    for line in r.stdout.splitlines():
        if line.strip():
            try:
                json.loads(line)
                raise AssertionError(f"non-json line parsed as JSON: {line!r}")
            except json.JSONDecodeError:
                pass


def test_cli_tail_follow_picks_up_new_send(bus_paths):
    env = os.environ.copy()
    env["AGENT_BUS_DB"] = str(bus_paths["db"])
    env["AGENT_BUS_AUDIT_LOG"] = str(bus_paths["log"])

    from agent_bus.storage import Storage
    s = Storage()
    s.upsert_agent("a", "/repo/a")
    s.upsert_agent("b", "/repo/b")

    # start tail -f in background
    proc = subprocess.Popen(
        [sys.executable, "-m", "agent_bus.cli", "tail", "-f"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # give it a moment to start its read loop
        time.sleep(0.4)
        # send via CLI from another process
        r = _run_cli(["send", "--name", "a", "--to", "b", "follow-me"],
                     env_extra={"AGENT_BUS_DB": env["AGENT_BUS_DB"],
                                "AGENT_BUS_AUDIT_LOG": env["AGENT_BUS_AUDIT_LOG"]})
        assert r.returncode == 0

        # wait up to 2 seconds for the message to appear in tail output
        deadline = time.time() + 2.0
        seen = ""
        while time.time() < deadline:
            assert proc.stdout is not None
            # non-blocking read attempt
            proc.stdout.flush() if hasattr(proc.stdout, "flush") else None
            try:
                line = proc.stdout.readline()
            except Exception:
                line = ""
            if line:
                seen += line
                if "follow-me" in seen:
                    break
            else:
                time.sleep(0.1)
        assert "follow-me" in seen, f"tail -f did not emit new audit row in time. Got: {seen!r}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_cli_hook_stop_empty_silent(bus_paths):
    env = {
        "AGENT_BUS_DB": str(bus_paths["db"]),
        "AGENT_BUS_AUDIT_LOG": str(bus_paths["log"]),
        "AGENT_BUS_NAME": "alpha",
    }
    from agent_bus.storage import Storage
    Storage().upsert_agent("alpha", "/repo/a")

    r = _run_cli(["hook-stop"], env_extra=env, input_text="{}")
    assert r.returncode == 0
    assert r.stdout.strip() == ""


def test_cli_hook_stop_blocks_when_pending(bus_paths):
    env = {
        "AGENT_BUS_DB": str(bus_paths["db"]),
        "AGENT_BUS_AUDIT_LOG": str(bus_paths["log"]),
        "AGENT_BUS_NAME": "alpha",
    }
    from agent_bus.storage import Storage
    s = Storage()
    s.upsert_agent("alpha", "/repo/a")
    s.upsert_agent("beta", "/repo/b")
    s.send_message(from_agent="beta", to="alpha", body="urgent question")

    r = _run_cli(["hook-stop"], env_extra=env, input_text="{}")
    assert r.returncode == 0
    payload = json.loads(r.stdout.strip().splitlines()[-1])
    assert payload["decision"] == "block"
    assert "urgent question" in payload["reason"]


def test_cli_hook_user_prompt_outputs_pending(bus_paths):
    env = {
        "AGENT_BUS_DB": str(bus_paths["db"]),
        "AGENT_BUS_AUDIT_LOG": str(bus_paths["log"]),
        "AGENT_BUS_NAME": "alpha",
    }
    from agent_bus.storage import Storage
    s = Storage()
    s.upsert_agent("alpha", "/repo/a")
    s.upsert_agent("beta", "/repo/b")
    s.send_message(from_agent="beta", to="alpha", body="hello alpha")

    r = _run_cli(["hook-user-prompt"], env_extra=env, input_text="{}")
    assert r.returncode == 0
    assert "1 new message(s)" in r.stdout
    assert "hello alpha" in r.stdout

    # inbox now empty
    r = _run_cli(["inbox", "--name", "alpha", "--json"], env_extra=env)
    assert json.loads(r.stdout) == []

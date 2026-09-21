"""Hook commands: Stop / UserPromptSubmit behavior."""

from __future__ import annotations

import io
import json


def test_hook_stop_empty_inbox_silent(two_agents):
    from agent_bus.hooks import run_hook_stop

    out = io.StringIO()
    rc = run_hook_stop(
        storage=two_agents,
        stdin=io.StringIO("{}"),
        stdout=out,
        actor="alpha",
    )
    assert rc == 0
    assert out.getvalue() == ""


def test_hook_stop_blocks_when_pending(two_agents):
    from agent_bus.hooks import run_hook_stop

    two_agents.send_message(from_agent="beta", to="alpha", body="please reply")

    out = io.StringIO()
    rc = run_hook_stop(
        storage=two_agents,
        stdin=io.StringIO("{}"),
        stdout=out,
        actor="alpha",
    )
    assert rc == 0
    payload = json.loads(out.getvalue().strip().splitlines()[-1])
    assert payload["decision"] == "block"
    assert "please reply" in payload["reason"]


def test_hook_user_prompt_surfaces_messages(two_agents):
    from agent_bus.hooks import run_hook_user_prompt

    two_agents.send_message(from_agent="beta", to="alpha", body="ping 1")
    two_agents.send_message(from_agent="beta", to="alpha", body="ping 2")

    out = io.StringIO()
    rc = run_hook_user_prompt(
        storage=two_agents,
        stdin=io.StringIO("{}"),
        stdout=out,
        actor="alpha",
    )
    assert rc == 0
    text = out.getvalue()
    assert "2 new message(s)" in text
    assert "ping 1" in text
    assert "ping 2" in text

    # inbox is now empty
    assert two_agents.read_inbox(agent="alpha") == []


def test_hook_user_prompt_no_op_on_empty(two_agents):
    from agent_bus.hooks import run_hook_user_prompt

    out = io.StringIO()
    rc = run_hook_user_prompt(
        storage=two_agents,
        stdin=io.StringIO("{}"),
        stdout=out,
        actor="alpha",
    )
    assert rc == 0
    assert out.getvalue() == ""


def test_hook_writes_deliver_audit_row(two_agents, bus_paths):
    from agent_bus.hooks import run_hook_user_prompt

    two_agents.send_message(from_agent="beta", to="alpha", body="trace me")
    run_hook_user_prompt(
        storage=two_agents,
        stdin=io.StringIO("{}"),
        stdout=io.StringIO(),
        actor="alpha",
    )
    rows = [
        json.loads(line)
        for line in bus_paths["log"].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    ops = [r["op"] for r in rows]
    # send, then read+deliver
    assert ops.count("send") == 1
    assert ops.count("read") == 1
    assert ops.count("deliver") == 1


def test_hooks_count_as_a_sign_of_life(two_agents):
    """A client that only ever talks to the bus through its hooks must not
    look idle, or group fan-out would start skipping it."""
    import sqlite3

    from agent_bus.hooks import run_hook_stop, run_hook_user_prompt

    stale = "2020-01-01T00:00:00.000000Z"
    for hook in (run_hook_user_prompt, run_hook_stop):
        with sqlite3.connect(two_agents.path) as conn:
            conn.execute("UPDATE agents SET last_seen = ? WHERE name = 'alpha'", (stale,))
        hook(storage=two_agents, stdin=io.StringIO("{}"),
             stdout=io.StringIO(), actor="alpha")
        assert two_agents.get_agent("alpha").last_seen > stale

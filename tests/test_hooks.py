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


# --------------------------- identity from --client ----------------------


def _wired_repo(tmp_path, storage, name: str):
    repo = tmp_path / "repo-a"
    (repo / ".git").mkdir(parents=True)
    storage.upsert_agent(name, str(repo))
    storage.ensure_agent("human", "/home")
    return repo


def _run_hook(hook, storage, *, payload: dict, client: str | None = None) -> str:
    out = io.StringIO()
    rc = hook(storage=storage, stdin=io.StringIO(json.dumps(payload)),
              stdout=out, client=client)
    assert rc == 0
    return out.getvalue()


def test_client_hook_finds_its_agent_from_the_payload_cwd(
    storage, tmp_path, monkeypatch
):
    """One hook command per client, shared by every repo: the payload says
    which repo this session is in."""
    from agent_bus.hooks import run_hook_stop

    monkeypatch.delenv("AGENT_BUS_NAME", raising=False)
    repo = _wired_repo(tmp_path, storage, "repo-a/codex")
    storage.send_message(from_agent="human", to="repo-a/codex", body="for codex")

    text = _run_hook(run_hook_stop, storage, client="codex",
                     payload={"cwd": str(repo / "src"), "hook_event_name": "Stop"})
    assert "for codex" in json.loads(text)["reason"]


def test_client_hook_is_silent_in_a_repo_that_is_not_on_the_bus(
    storage, tmp_path, monkeypatch
):
    from agent_bus.hooks import run_hook_stop, run_hook_user_prompt

    monkeypatch.delenv("AGENT_BUS_NAME", raising=False)
    unwired = tmp_path / "unwired"
    (unwired / ".git").mkdir(parents=True)

    for hook in (run_hook_stop, run_hook_user_prompt):
        assert _run_hook(hook, storage, client="codex",
                         payload={"cwd": str(unwired)}) == ""
    assert storage.list_agents() == []


def test_hook_without_any_identity_fails_loudly(storage, monkeypatch):
    import pytest

    from agent_bus.hooks import run_hook_stop

    monkeypatch.delenv("AGENT_BUS_NAME", raising=False)
    monkeypatch.delenv("AGENT_BUS_CLIENT", raising=False)
    with pytest.raises(SystemExit) as exc_info:
        run_hook_stop(storage=storage, stdin=io.StringIO("{}"), stdout=io.StringIO())
    assert exc_info.value.code == 2


# --------------------------- agy dialect ---------------------------------


def test_agy_hooks_always_answer_in_json(storage, tmp_path, monkeypatch):
    from agent_bus.hooks import run_hook_stop, run_hook_user_prompt

    monkeypatch.delenv("AGENT_BUS_NAME", raising=False)
    repo = _wired_repo(tmp_path, storage, "repo-a/agy")
    payload = {"workspacePaths": [str(repo)], "conversationId": "c-1"}

    # nothing pending: still a JSON object made only of fields agy knows
    assert json.loads(_run_hook(run_hook_user_prompt, storage, client="agy",
                                payload=payload)) == {"injectSteps": []}
    assert json.loads(_run_hook(run_hook_stop, storage, client="agy",
                                payload=payload)) == {"decision": "stop"}

    storage.send_message(from_agent="human", to="repo-a/agy", body="first")
    injected = json.loads(_run_hook(run_hook_user_prompt, storage, client="agy",
                                    payload=payload))
    (step,) = injected["injectSteps"]
    assert "first" in step["ephemeralMessage"]

    storage.send_message(from_agent="human", to="repo-a/agy", body="second")
    decision = json.loads(_run_hook(run_hook_stop, storage, client="agy",
                                    payload=payload))
    assert decision["decision"] == "continue"
    assert "second" in decision["reason"]


def test_dialect_follows_the_client_part_of_an_explicit_name(
    storage, tmp_path, monkeypatch
):
    """Per-repo agy wiring names the agent outright; the hook still has to
    answer in agy's JSON."""
    from agent_bus.hooks import run_hook_stop

    _wired_repo(tmp_path, storage, "repo-a/agy")
    monkeypatch.setenv("AGENT_BUS_NAME", "repo-a/agy")
    assert json.loads(_run_hook(run_hook_stop, storage, payload={})) == {
        "decision": "stop"
    }

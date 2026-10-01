"""MCP tool surface: whoami, send/read via the FastMCP wrapper."""

from __future__ import annotations

import asyncio
import json

import pytest


@pytest.fixture
def mcp_alpha(bus_paths, storage):
    from agent_bus.server import build_server

    return build_server(name="alpha", repo_path="/repo/alpha", storage=storage)


@pytest.fixture
def mcp_beta(bus_paths, storage):
    from agent_bus.server import build_server

    return build_server(name="beta", repo_path="/repo/beta", storage=storage)


# Two clients sharing one repo, plus a client in another repo — all over
# the same Storage, the way real MCP server processes share one database.
SHARED_REPO = "/repo/shared"


@pytest.fixture
def mcp_shared_claude(bus_paths, storage):
    from agent_bus.server import build_server

    return build_server(name="shared/claude", repo_path=SHARED_REPO, storage=storage)


@pytest.fixture
def mcp_shared_codex(bus_paths, storage):
    from agent_bus.server import build_server

    return build_server(name="shared/codex", repo_path=SHARED_REPO, storage=storage)


@pytest.fixture
def mcp_other(bus_paths, storage):
    from agent_bus.server import build_server

    return build_server(name="other/claude", repo_path="/repo/other", storage=storage)


def _session(fake_procs) -> dict:
    """build_server arguments for a session of its own: a fresh client
    process and its server."""
    server, lineage = fake_procs.session()
    return {"process": server, "lineage": lineage}


def _unwrap(out):
    """FastMCP wraps list results as {"result": [...]}."""
    if isinstance(out, dict) and "result" in out:
        return out["result"]
    return out


def _call(mcp, tool: str, args: dict) -> object:
    """Synchronously invoke a FastMCP tool and return parsed JSON output."""
    raw = asyncio.run(mcp.call_tool(tool, args))
    # FastMCP returns (content_list, structured_dict) tuple — unwrap.
    if isinstance(raw, tuple):
        _content, structured = raw
        return structured
    # older API: list[TextContent]
    text = "".join(getattr(c, "text", "") for c in raw)
    return json.loads(text) if text else None


def test_whoami(mcp_alpha):
    out = _call(mcp_alpha, "whoami", {})
    assert out["name"] == "alpha"
    assert out["repo_path"] == "/repo/alpha"
    assert out["registered_at"]


def test_list_agents_includes_pending_counts(mcp_alpha, mcp_beta):
    _call(mcp_alpha, "send_message", {"to": "beta", "body": "yo"})
    out = _call(mcp_beta, "list_agents", {})
    agents = out.get("result", out)
    if isinstance(agents, dict) and "result" in agents:
        agents = agents["result"]
    by_name = {a["name"]: a for a in agents}
    assert by_name["alpha"]["pending_count"] == 0
    assert by_name["beta"]["pending_count"] == 1


def test_send_then_read_inbox(mcp_alpha, mcp_beta):
    sent = _call(mcp_alpha, "send_message", {"to": "beta", "body": "hi beta"})
    assert sent["recipients"] == ["beta"]
    inbox = _call(mcp_beta, "read_inbox", {})
    msgs = inbox.get("result", inbox) if isinstance(inbox, dict) else inbox
    if isinstance(msgs, dict) and "result" in msgs:
        msgs = msgs["result"]
    assert len(msgs) == 1
    assert msgs[0]["body"] == "hi beta"
    assert msgs[0]["from"] == "alpha"


def test_broadcast_skips_sender(mcp_alpha, mcp_beta, storage):
    # add a third agent so we can verify fan-out
    storage.upsert_agent("gamma", "/repo/gamma")
    result = _call(mcp_alpha, "send_message", {"to": "*", "body": "all hands"})
    assert "message_ids" in result
    assert sorted(result["recipients"]) == ["beta", "gamma"]


def test_read_thread_returns_full_conversation(mcp_alpha, mcp_beta):
    sent = _call(mcp_alpha, "send_message", {"to": "beta", "body": "q?"})
    thread = sent["thread_id"]
    _call(mcp_beta, "send_message",
          {"to": "alpha", "body": "a!", "thread_id": thread})
    thread_view = _call(mcp_alpha, "read_thread", {"thread_id": thread})
    msgs = thread_view.get("result", thread_view) if isinstance(thread_view, dict) else thread_view
    if isinstance(msgs, dict) and "result" in msgs:
        msgs = msgs["result"]
    assert [m["body"] for m in msgs] == ["q?", "a!"]


def test_tail_audit_after_activity(mcp_alpha, mcp_beta):
    _call(mcp_alpha, "send_message", {"to": "beta", "body": "audit me"})
    _call(mcp_beta, "read_inbox", {})
    out = _call(mcp_alpha, "tail_audit", {"limit": 10})
    rows = out.get("result", out) if isinstance(out, dict) else out
    if isinstance(rows, dict) and "result" in rows:
        rows = rows["result"]
    ops = [r["op"] for r in rows]
    # we should see at least one send and one read
    assert "send" in ops
    assert "read" in ops


# --------------------------- two identities, one repo --------------------


def test_two_clients_in_one_repo_keep_separate_inboxes(
    mcp_shared_claude, mcp_shared_codex, mcp_other
):
    _call(mcp_other, "send_message", {"to": "shared/codex", "body": "codex only"})

    assert _unwrap(_call(mcp_shared_claude, "read_inbox", {})) == []
    (msg,) = _unwrap(_call(mcp_shared_codex, "read_inbox", {}))
    assert (msg["body"], msg["to"], msg["kind"]) == ("codex only", "shared/codex", "direct")


def test_clients_in_one_repo_can_message_each_other(
    mcp_shared_claude, mcp_shared_codex
):
    sent = _call(mcp_shared_claude, "send_message",
                 {"to": "shared/codex", "body": "are you touching storage.py?"})
    assert sent["recipients"] == ["shared/codex"]
    (msg,) = _unwrap(_call(mcp_shared_codex, "read_inbox", {}))
    # sent from the session, so a reply reaches exactly that session
    assert msg["from"] == "shared/claude-1"


def test_bare_repo_name_reaches_both_clients(
    mcp_shared_claude, mcp_shared_codex, mcp_other
):
    sent = _call(mcp_other, "send_message", {"to": "shared", "body": "hello repo"})
    assert sent["kind"] == "group"
    assert sent["recipients"] == ["shared/claude", "shared/codex"]
    assert len(sent["message_ids"]) == 2

    for member in (mcp_shared_claude, mcp_shared_codex):
        (msg,) = _unwrap(_call(member, "read_inbox", {}))
        assert (msg["body"], msg["addressed_to"], msg["kind"]) == (
            "hello repo", "shared", "group",
        )


def test_group_send_from_a_member_skips_itself(
    mcp_shared_claude, mcp_shared_codex
):
    sent = _call(mcp_shared_claude, "send_message", {"to": "shared", "body": "team?"})
    # no other claude session is running, so its client address is skipped too
    assert sent["recipients"] == ["shared/codex"]
    assert _unwrap(_call(mcp_shared_claude, "read_inbox", {})) == []


def test_both_clients_report_the_same_repo_path(mcp_shared_claude, mcp_shared_codex):
    paths = {
        _call(m, "whoami", {})["repo_path"]
        for m in (mcp_shared_claude, mcp_shared_codex)
    }
    assert paths == {SHARED_REPO}


def test_unknown_recipient_is_a_tool_error(mcp_shared_claude, mcp_shared_codex):
    from mcp.server.fastmcp.exceptions import ToolError

    with pytest.raises(ToolError) as exc_info:
        _call(mcp_shared_claude, "send_message", {"to": "shared/cdx", "body": "hi"})
    assert "shared/codex" in str(exc_info.value)


# --------------------------- identity on the server ----------------------


def test_whoami_describes_the_identity_and_its_repo_mates(
    mcp_shared_claude, mcp_shared_codex
):
    out = _call(mcp_shared_claude, "whoami", {})
    assert (out["name"], out["group"], out["client"], out["instance"]) == (
        "shared/claude-1", "shared", "claude", 1,
    )
    assert (out["client_address"], out["session"]) == ("shared/claude", "1")
    assert [(m["name"], m["kind"], m["live"]) for m in out["group_members"]] == [
        ("shared/claude", "client", None),
        ("shared/claude-1", "session", True),
        ("shared/codex", "client", None),
        ("shared/codex-1", "session", True),
    ]
    assert out["warnings"] == []


def test_whoami_for_a_name_without_a_client(mcp_alpha):
    out = _call(mcp_alpha, "whoami", {})
    assert (out["group"], out["client"], out["instance"]) == ("alpha", None, None)
    assert [m["name"] for m in out["group_members"]] == ["alpha"]


def test_list_agents_can_be_narrowed_to_one_repo(
    mcp_shared_claude, mcp_shared_codex, mcp_other
):
    everyone = _unwrap(_call(mcp_other, "list_agents", {}))
    assert [a["name"] for a in everyone] == [
        "other/claude", "other/claude-1", "shared/claude", "shared/claude-1",
        "shared/codex", "shared/codex-1",
    ]
    shared = _unwrap(_call(mcp_other, "list_agents", {"group": "shared"}))
    assert [(a["name"], a["client"], a["kind"]) for a in shared] == [
        ("shared/claude", "claude", "client"),
        ("shared/claude-1", "claude", "session"),
        ("shared/codex", "codex", "client"),
        ("shared/codex-1", "codex", "session"),
    ]


def test_server_derives_its_name_from_the_client_and_repo(
    bus_paths, storage, tmp_path, monkeypatch
):
    from agent_bus.server import build_server

    for var in ("AGENT_BUS_NAME", "AGENT_BUS_CLIENT", "AGENT_BUS_INSTANCE"):
        monkeypatch.delenv(var, raising=False)
    repo = tmp_path / "Repo_A"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setenv("AGENT_BUS_REPO", str(repo))

    mcp = build_server(client="codex", storage=storage)
    out = _call(mcp, "whoami", {})
    assert (out["name"], out["repo_path"]) == ("repo-a/codex-1", str(repo))


def test_a_pinned_instance_is_the_session_address_asked_for(
    bus_paths, storage, fake_procs, monkeypatch
):
    from agent_bus.server import build_server

    monkeypatch.setenv("AGENT_BUS_NAME", "shared/claude")
    monkeypatch.setenv("AGENT_BUS_REPO", SHARED_REPO)
    first = build_server(storage=storage, **_session(fake_procs))
    monkeypatch.setenv("AGENT_BUS_INSTANCE", "5")
    second = build_server(storage=storage, **_session(fake_procs))

    assert _call(first, "whoami", {})["name"] == "shared/claude-1"
    out = _call(second, "whoami", {})
    assert (out["name"], out["instance"]) == ("shared/claude-5", 5)

    _call(first, "send_message", {"to": "shared/claude-5", "body": "hi 5"})
    assert [m["body"] for m in _unwrap(_call(second, "read_inbox", {}))] == ["hi 5"]
    assert _unwrap(_call(first, "read_inbox", {})) == []


def test_whoami_warns_when_the_name_belongs_to_another_repo(bus_paths, storage):
    """Two repos wired under one name share one mailbox; say so."""
    from agent_bus.server import build_server

    storage.upsert_agent("foo/claude", "/code/websites/foo")
    mcp = build_server(name="foo/claude", repo_path="/code/projects/foo", storage=storage)
    (warning,) = _call(mcp, "whoami", {})["warnings"]
    assert "/code/websites/foo" in warning


def test_server_without_any_identity_exits(bus_paths, storage, monkeypatch, tmp_path):
    from agent_bus.server import build_server

    for var in ("AGENT_BUS_NAME", "AGENT_BUS_CLIENT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        build_server(storage=storage)
    assert exc_info.value.code == 2

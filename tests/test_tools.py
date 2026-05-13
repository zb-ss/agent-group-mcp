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

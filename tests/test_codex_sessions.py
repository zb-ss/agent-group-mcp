"""Codex threads share a parent process, but must never share a private inbox."""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
from contextlib import AsyncExitStack

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from mcp.shared.context import RequestContext
from mcp.types import RequestParams

from agent_bus import clients, identity
from agent_bus.hooks import run_hook_user_prompt
from agent_bus.server import build_mcp, start
from agent_bus.sessions import SessionError, SessionRegistry
from agent_bus.storage import Storage

ADDRESS = "shared/codex"
REPO = "/repo/shared"


def _server(storage, fake_procs, daemon, *, env=None, name=ADDRESS):
    me = start(
        store=storage, name=name, repo_path=REPO,
        process=fake_procs.proc(), lineage=[daemon], env=env or {},
    )
    return me, build_mcp(me, storage)


def _call(mcp, tool, args=None, *, thread="thread-a", session="run-a"):
    # Exercise FastMCP's Context injection with the metadata Codex supplies.
    meta = {"threadId": thread, "sessionId": session} if thread is not None else {}
    ctx = Context(request_context=RequestContext(
        request_id=1, meta=RequestParams.Meta(**meta), session=None, lifespan_context={},
    ), fastmcp=mcp)
    original = mcp.get_context
    mcp.get_context = lambda: ctx
    try:
        result = asyncio.run(mcp.call_tool(tool, args or {}))
    finally:
        mcp.get_context = original
    data = result[1] if isinstance(result, tuple) else json.loads(result[0].text)
    return data.get("result", data) if isinstance(data, dict) else data


@pytest.mark.parametrize("value", [None, "", "  ", 7, {}, []])
def test_unusable_metadata_does_not_identify_a_thread(value):
    assert clients.session_key_from_meta("codex", {"threadId": value}) is None


def test_thread_id_is_used_instead_of_the_ephemeral_session_id():
    assert clients.session_key_from_meta(
        "codex", {"threadId": " thread-a ", "sessionId": "run-a"}
    ) == "thread-a"
    assert clients.session_key("codex", {"CODEX_THREAD_ID": " thread-a "}) == "thread-a"
    assert clients.session_key_from_meta("claude", {"threadId": "thread-a"}) is None


def test_four_servers_under_one_parent_have_independent_inboxes(storage, fake_procs):
    daemon = fake_procs.proc("codex")
    servers = [_server(storage, fake_procs, daemon) for _ in range(4)]
    names = [me.name for me, _ in servers]
    assert names == [f"{ADDRESS}-{n}" for n in range(1, 5)]
    for i, (me, mcp) in enumerate(servers):
        assert _call(mcp, "whoami", thread=f"thread-{i}")["name"] == me.name
        assert me.registry.get(me.process, ADDRESS).session_key == f"thread-{i}"
    storage.upsert_agent("human", REPO)
    storage.send_message(from_agent="human", to=names[3], body="four only")
    for i, (_, mcp) in enumerate(servers):
        inbox = _call(mcp, "read_inbox", thread=f"thread-{i}")
        assert [m["body"] for m in inbox] == (["four only"] if i == 3 else [])
        out = _call(mcp, "set_session", {"label": f"task-{i}"}, thread=f"thread-{i}")
        assert out["name"] == f"{ADDRESS}-task-{i}"


@pytest.mark.parametrize("key", [None, "thread-b"])
def test_codex_hook_never_falls_back_to_a_sibling_process(storage, fake_procs, key):
    daemon = fake_procs.proc("codex")
    me, mcp = _server(storage, fake_procs, daemon)
    _call(mcp, "whoami")
    assert me.registry.for_hook(ADDRESS, session_key=key, ancestry=[daemon]) is None
    assert me.registry.for_hook(ADDRESS, session_key="thread-a", ancestry=[daemon]) == me.name


def test_hooks_use_payload_thread_id_and_leave_sibling_mail(
    storage, fake_procs, monkeypatch,
):
    daemon = fake_procs.proc("codex")
    first, a = _server(storage, fake_procs, daemon)
    second, b = _server(storage, fake_procs, daemon)
    _call(a, "whoami", thread="thread-a")
    _call(b, "whoami", thread="thread-b")
    storage.upsert_agent("human", REPO)
    storage.send_message(from_agent="human", to=first.name, body="for first")
    storage.send_message(from_agent="human", to=second.name, body="for second")
    monkeypatch.setenv("AGENT_BUS_NAME", ADDRESS)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-a")
    out = io.StringIO()
    run_hook_user_prompt(
        storage=storage, client="codex", stdout=out,
        stdin=io.StringIO(json.dumps({"session_id": "thread-b", "cwd": REPO})),
    )
    assert "for second" in out.getvalue() and "for first" not in out.getvalue()
    assert [m["body"] for m in _call(a, "read_inbox")] == ["for first"]


def test_new_codex_threads_do_not_inherit_ended_sibling_addresses(storage, fake_procs):
    daemon = fake_procs.proc("codex")
    first, a = _server(storage, fake_procs, daemon, env={"CODEX_THREAD_ID": "thread-a"})
    _call(a, "set_session", {"label": "frontend"})
    first.release()
    second, _ = _server(storage, fake_procs, daemon, env={"CODEX_THREAD_ID": "thread-b"})
    assert second.name != first.name


def test_per_call_metadata_overrides_an_inherited_thread_environment(storage, fake_procs):
    daemon = fake_procs.proc("codex")
    first, a = _server(storage, fake_procs, daemon, env={"CODEX_THREAD_ID": "parent-thread"})
    second, b = _server(storage, fake_procs, daemon, env={"CODEX_THREAD_ID": "parent-thread"})
    assert first.name != second.name
    _call(a, "whoami", thread="thread-a")
    _call(b, "whoami", thread="thread-b")
    assert {r.session_key for r in first.registry.live(ADDRESS)} == {"thread-a", "thread-b"}


def test_keyed_claim_does_not_resume_another_thread_under_the_same_parent(storage, fake_procs):
    registry = SessionRegistry(storage)
    daemon = fake_procs.proc("codex")
    server = fake_procs.proc()
    old = registry.claim(client_address=ADDRESS, repo_path=REPO, server=server,
                         lineage=[daemon], session_key="thread-a", pinned="frontend")
    registry.release(server, ADDRESS)
    new = registry.claim(client_address=ADDRESS, repo_path=REPO, server=fake_procs.proc(),
                         lineage=[daemon], session_key="thread-b")
    assert new.name != old.name


@pytest.mark.parametrize("label", [None, "frontend"])
def test_first_call_recovers_a_resumed_threads_address(storage, fake_procs, label):
    daemon = fake_procs.proc("codex")
    first, a = _server(storage, fake_procs, daemon)
    _call(a, "set_session", {"label": label, "topic": "ui work"})
    first.release()
    resumed, b = _server(storage, fake_procs, daemon)
    provisional = resumed.name
    assert provisional != first.name
    storage.upsert_agent("human", REPO)
    storage.send_message(from_agent="human", to=provisional, body="before binding")
    out = _call(b, "whoami", session="new-run")
    assert out["name"] == first.name
    assert [m["body"] for m in _call(b, "read_inbox")] == ["before binding"]
    assert storage.get_agent(provisional) is None


def test_an_old_unkeyed_server_does_not_absorb_new_codex_threads(storage, fake_procs):
    daemon = fake_procs.proc("codex")
    old, _ = _server(storage, fake_procs, daemon)
    new, mcp = _server(storage, fake_procs, daemon)
    _call(mcp, "whoami")
    assert old.name != new.name
    assert new.registry.for_hook(ADDRESS, session_key="thread-a", ancestry=[daemon]) == new.name


@pytest.mark.parametrize("tool,args", [
    ("whoami", {}), ("list_agents", {}),
    ("send_message", {"to": "human", "body": "hello"}),
    ("read_inbox", {}), ("read_thread", {"thread_id": "mail-thread"}),
    ("tail_audit", {}), ("set_session", {"label": "frontend"}),
])
def test_every_tool_binds_before_access_and_rejects_a_different_thread(
    storage, fake_procs, tool, args,
):
    storage.upsert_agent("human", REPO)
    me, mcp = _server(storage, fake_procs, fake_procs.proc("codex"))
    _call(mcp, tool, args)
    assert me.registry.get(me.process, ADDRESS).session_key == "thread-a"
    with pytest.raises(ToolError, match="already bound to another thread"):
        _call(mcp, tool, args, thread="thread-b")


def test_same_thread_cannot_bind_two_live_servers(storage, fake_procs):
    daemon = fake_procs.proc("codex")
    _, a = _server(storage, fake_procs, daemon)
    second, b = _server(storage, fake_procs, daemon)
    _call(a, "whoami")
    with pytest.raises(ToolError, match="already has a running server"):
        _call(b, "whoami")
    assert second.registry.get(second.process, ADDRESS).session_key is None


def test_crashed_thread_recovers_its_label_and_waiting_mail(storage, fake_procs):
    daemon = fake_procs.proc("codex")
    first, a = _server(storage, fake_procs, daemon)
    _call(a, "set_session", {"label": "frontend", "topic": "ui work"})
    fake_procs.kill(first.process)
    storage.upsert_agent("human", REPO)
    storage.send_message(from_agent="human", to=first.name, body="waiting")
    _, b = _server(storage, fake_procs, daemon)
    assert _call(b, "whoami")["topic"] == "ui work"
    assert [m["body"] for m in _call(b, "read_inbox")] == ["waiting"]


def test_resume_cannot_take_a_label_another_thread_has_claimed(storage, fake_procs):
    daemon = fake_procs.proc("codex")
    first, a = _server(storage, fake_procs, daemon)
    _call(a, "set_session", {"label": "frontend"})
    first.release()
    second, b = _server(storage, fake_procs, daemon)
    _call(b, "set_session", {"label": "frontend"}, thread="thread-b")
    resumed, c = _server(storage, fake_procs, daemon)
    assert _call(c, "whoami")["name"] == resumed.name != second.name


@pytest.mark.parametrize("handle", ["7", "backend"])
def test_binding_honours_an_explicit_startup_address(storage, fake_procs, handle):
    daemon = fake_procs.proc("codex")
    first, a = _server(storage, fake_procs, daemon)
    _call(a, "set_session", {"label": "frontend"})
    first.release()
    _, b = _server(storage, fake_procs, daemon, name=f"{ADDRESS}-{handle}")
    assert _call(b, "whoami")["name"] == f"{ADDRESS}-{handle}"


def test_expired_thread_reservations_are_not_resurrected(storage, fake_procs, monkeypatch):
    daemon = fake_procs.proc("codex")
    first, a = _server(storage, fake_procs, daemon)
    _call(a, "set_session", {"label": "frontend"})
    first.release()
    monkeypatch.setenv("AGENT_BUS_SESSION_RESUME_HOURS", "0")
    _, b = _server(storage, fake_procs, daemon)
    assert _call(b, "whoami")["name"] != first.name


def test_codex_never_falls_back_to_a_shared_inbox_when_numbers_run_out(
    storage, fake_procs, monkeypatch,
):
    monkeypatch.setattr(identity, "MAX_NUMBER", 1)
    daemon = fake_procs.proc("codex")
    _server(storage, fake_procs, daemon)
    with pytest.raises(SessionError, match="every session number"):
        _server(storage, fake_procs, daemon)


def test_older_codex_without_metadata_keeps_distinct_inboxes(storage, fake_procs):
    daemon = fake_procs.proc("codex")
    first, a = _server(storage, fake_procs, daemon)
    second, b = _server(storage, fake_procs, daemon)
    _call(a, "send_message", {"to": second.name, "body": "private"}, thread=None)
    assert _call(a, "read_inbox", thread=None) == []
    assert [m["body"] for m in _call(b, "read_inbox", thread=None)] == ["private"]
    assert first.registry.for_hook(ADDRESS, session_key=None, ancestry=[daemon]) is None


def test_real_stdio_servers_preserve_thread_isolation(bus_paths):
    """Real subprocesses and MCP wire metadata, with one shared client PID."""
    async def exercise() -> None:
        env = {
            "AGENT_BUS_DB": str(bus_paths["db"]),
            "AGENT_BUS_AUDIT_LOG": str(bus_paths["log"]),
            "AGENT_BUS_NAME": ADDRESS, "AGENT_BUS_REPO": REPO,
            "AGENT_BUS_NATIVE_WAKE": "0",
        }
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "agent_bus.cli", "serve", "--client", "codex"], env=env,
        )
        async with AsyncExitStack() as stack:
            sessions = []
            for _ in range(4):
                read, write = await stack.enter_async_context(stdio_client(params))
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                sessions.append(session)
            rows = SessionRegistry(Storage()).live(ADDRESS)
            assert len({r.name for r in rows}) == 4
            assert {r.client_process.pid for r in rows} == {os.getpid()}

            async def call(i, tool, args=None):
                result = await sessions[i].call_tool(
                    tool, args or {}, meta={"threadId": f"thread-{i}", "sessionId": "run"},
                )
                assert not result.isError, result.content
                value = result.structuredContent or json.loads(result.content[0].text)
                return value.get("result", value) if isinstance(value, dict) else value

            names = [(await call(i, "whoami"))["name"] for i in range(4)]
            await call(0, "send_message", {"to": names[3], "body": "four only"})
            for i in range(3):
                assert await call(i, "read_inbox") == []
            assert [m["body"] for m in await call(3, "read_inbox")] == ["four only"]
            assert (await call(3, "set_session", {"label": "frontend"}))["name"] == f"{ADDRESS}-frontend"
            wrong = await sessions[3].call_tool("read_inbox", {}, meta={"threadId": "wrong"})
            assert wrong.isError
    asyncio.run(exercise())

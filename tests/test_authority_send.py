"""Sending with the dispatcher token: MCP tool and CLI paths."""

from __future__ import annotations

import asyncio
import json

from agent_bus import authority, resolution

TOKEN = "unit-test-token"


def _register(storage):
    storage.register_authority(authority.ROLE_DISPATCHER, authority.digest(TOKEN))
    storage.upsert_agent("peer", "/repo/peer")


def _server(storage, fake_procs, env):
    from agent_bus import server

    proc, lineage = fake_procs.session()
    me = server.start(
        store=storage, name="alpha/claude", repo_path="/repo/alpha", client="claude",
        process=proc, lineage=lineage, env=env,
    )
    return me, server.build_mcp(me, storage)


def _send(mcp, body="hello"):
    raw = asyncio.run(mcp.call_tool("send_message", {"to": "peer", "body": body}))
    return raw


def _stored(storage):
    return storage.read_inbox(agent="peer")


def test_tool_send_carries_authority_with_token(storage, fake_procs):
    _register(storage)
    me, mcp = _server(storage, fake_procs, {resolution.AUTHORITY_TOKEN_ENV: TOKEN})
    assert me.authority_token == TOKEN
    _send(mcp)
    msgs = _stored(storage)
    assert [m.authority for m in msgs] == ["dispatcher"]
    assert all(d["authority"] == "dispatcher" for d in (m.to_dict() for m in msgs))
    assert TOKEN not in json.dumps(asyncio.run(mcp.call_tool("whoami", {})), default=str)


def test_tool_send_without_token_has_no_authority(storage, fake_procs):
    _register(storage)
    me, mcp = _server(storage, fake_procs, {})
    assert me.authority_token is None
    _send(mcp)
    msgs = _stored(storage)
    assert [m.authority for m in msgs] == [None]
    assert "authority" in msgs[0].to_dict()


def test_instructions_state_the_rule(storage, fake_procs):
    _register(storage)
    me, _ = _server(storage, fake_procs, {})
    from agent_bus import server

    text = server._instructions(me)
    assert authority.VERIFIED_MARK in text and "no such authority" in text


def _cli_send(monkeypatch, capsys, name="dispatcher/claude-x"):
    from agent_bus import cli

    rc = cli.main(["send", "--name", name, "--to", "peer", "hi", "--json"])
    assert rc == 0
    capsys.readouterr()


def test_cli_send_with_env_token_stores_authority(storage, monkeypatch, capsys):
    _register(storage)
    monkeypatch.setenv(resolution.AUTHORITY_TOKEN_ENV, TOKEN)
    _cli_send(monkeypatch, capsys)
    assert [m.authority for m in _stored(storage)] == ["dispatcher"]


def test_cli_send_without_token_has_no_authority(storage, monkeypatch, capsys):
    _register(storage)
    monkeypatch.delenv(resolution.AUTHORITY_TOKEN_ENV, raising=False)
    _cli_send(monkeypatch, capsys)
    assert [m.authority for m in _stored(storage)] == [None]

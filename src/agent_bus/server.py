"""MCP stdio server. One instance per Claude Code session.

Identity is taken from the environment (AGENT_BUS_NAME / AGENT_BUS_REPO),
upserted on startup, and silently attached to every tool call. There is
no register_agent tool — identity is config, not data.
"""

from __future__ import annotations

import os
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import audit
from .storage import BROADCAST, Storage


def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        sys.stderr.write(
            f"agent-bus: missing required env var {name}. "
            "Set it in the .mcp.json `env` block.\n"
        )
        raise SystemExit(2)
    return val


def build_server(
    *,
    name: str | None = None,
    repo_path: str | None = None,
    storage: Storage | None = None,
) -> FastMCP:
    """Construct (but do not run) the MCP server.

    Pulled out of `main()` so tests can wire a Storage with a temp DB.
    """
    agent_name = name if name is not None else _require_env("AGENT_BUS_NAME")
    agent_repo = repo_path if repo_path is not None else _require_env("AGENT_BUS_REPO")
    store = storage or Storage()
    store.upsert_agent(agent_name, agent_repo)

    mcp = FastMCP(
        "agent-bus",
        instructions=(
            "Local multi-agent message bus. You are identified as "
            f"`{agent_name}`. Use `read_inbox` at the start of a turn if "
            "you suspect pending messages; the Stop/UserPromptSubmit hooks "
            "will surface them automatically when configured. "
            "Address a message to a full agent name (`repo-a/claude`) for "
            "one agent, to a bare repo name (`repo-a`) for every agent "
            "working in that repo, or to `*` for every peer except you."
        ),
    )

    @mcp.tool()
    def whoami() -> dict:
        """Return this server's bound identity (set in .mcp.json env)."""
        store.touch_agent(agent_name)
        row = store.get_agent(agent_name)
        if row is None:
            # shouldn't happen — upsert was called at boot
            return {"name": agent_name, "repo_path": agent_repo, "registered_at": None}
        return {
            "name": row.name,
            "repo_path": row.repo_path,
            "registered_at": row.registered_at,
            "last_seen": row.last_seen,
        }

    @mcp.tool()
    def list_agents() -> list[dict]:
        """Every agent that has ever connected, plus its current unread count."""
        store.touch_agent(agent_name)
        return [a.to_dict(pending_count=c) for a, c in store.list_agents_with_counts()]

    @mcp.tool()
    def send_message(to: str, body: str, thread_id: str | None = None) -> dict:
        """Send `body` to one agent, to every agent in a repo, or to everyone.

        `to` is a full agent name such as `repo-a/claude` (exactly that
        agent), a bare repo name such as `repo-a` (every agent working in
        that repo, except you), or `*` (every agent on the bus, except
        you). `list_agents` shows the names. An unknown `to` is an error
        and nothing is sent.

        Returns {"to", "kind", "message_ids", "recipients", "thread_id",
        "sent_at"}, plus "message_id" when exactly one agent received it.
        `kind` is "direct", "group" or "broadcast". Each recipient gets its
        own row and message_id, so read state is tracked per agent.
        """
        store.touch_agent(agent_name)
        return store.send_message(
            from_agent=agent_name,
            to=to,
            body=body,
            thread_id=thread_id,
            actor=agent_name,
        )

    @mcp.tool()
    def read_inbox(mark_read: bool = True, limit: int = 50) -> list[dict]:
        """Unread messages for this agent, oldest first.

        Set mark_read=False to peek without flipping read_at.
        """
        store.touch_agent(agent_name)
        msgs = store.read_inbox(
            agent=agent_name,
            mark_read=mark_read,
            limit=limit,
            actor=agent_name,
        )
        return [m.to_dict() for m in msgs]

    @mcp.tool()
    def read_thread(thread_id: str, limit: int = 100) -> list[dict]:
        """Full thread across all participants, ordered by sent_at."""
        store.touch_agent(agent_name)
        return [m.to_dict() for m in store.read_thread(thread_id=thread_id, limit=limit)]

    @mcp.tool()
    def tail_audit(limit: int = 50) -> list[dict]:
        """Last N audit-log entries across the whole bus.

        Audit rows are the recovery-truth source; messages.body is the
        only place the full body lives, but the audit log records its
        sha256 so you can reconstruct provenance.
        """
        store.touch_agent(agent_name)
        return audit.tail(limit=limit)

    return mcp


def main() -> None:  # pragma: no cover (entrypoint)
    mcp = build_server()
    mcp.run()  # stdio transport by default


if __name__ == "__main__":  # pragma: no cover
    main()

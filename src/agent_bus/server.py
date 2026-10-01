"""MCP stdio server. One instance per client session.

Identity comes from the environment — AGENT_BUS_NAME, or a client id plus
the repo (see `resolution.py`). A server speaking for a client then claims
a session address of its own, `<repo>/<client>-<handle>` (see
`sessions.py`), so that two sessions of one client in one repo keep
separate inboxes. That name is silently attached to every tool call; there
is no register_agent tool — identity is config, not data. A session may
rename itself with `set_session`.
"""

from __future__ import annotations

import os
import signal
import sys
from dataclasses import dataclass, field
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from . import audit, clients, identity, procs, resolution, settings
from .procs import Proc
from .sessions import SessionError, SessionRegistry
from .storage import KIND_SESSION, Storage

MAX_TOPIC_LEN = audit.PREVIEW_LIMIT


@dataclass
class Me:
    """Who this server speaks for. `name` changes when the session is
    renamed; everything else is fixed for the life of the process."""

    name: str
    repo_path: str
    registry: SessionRegistry | None = None
    process: Proc | None = None
    client_address: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def holds_session(self) -> bool:
        return self.registry is not None and self.process is not None

    def release(self) -> None:
        """Give up the session address, if this server holds one."""
        if self.registry is not None and self.process is not None and self.client_address:
            self.registry.release(self.process, self.client_address)


def _who_am_i(
    store: Storage, name: str | None, repo_path: str | None, client: str | None
) -> resolution.ResolvedIdentity:
    if name is not None and repo_path is not None:
        return resolution.ResolvedIdentity(name, repo_path)
    try:
        who = resolution.resolve(storage=store, client=client)
    except resolution.RepoOptedOutError as e:
        # a deliberate choice, not a misconfiguration: say so and nothing more
        sys.stderr.write(f"agent-bus: {e}\n")
        raise SystemExit(2) from None
    except resolution.IdentityError as e:
        sys.stderr.write(
            f"agent-bus: {e}. Set it in the `env` block of this client's "
            "MCP server config.\n"
        )
        raise SystemExit(2) from None
    assert who is not None  # only None with registered_only=True
    return who


def _mcp_client_name(ctx: Context) -> str | None:
    """What the connected client calls itself in the MCP handshake. Purely
    diagnostic: it shows a config file being read by a client other than the
    one the agent is named after."""
    try:
        params = ctx.session.client_params
    except (ValueError, LookupError, AttributeError):
        return None  # no live session, e.g. a tool called directly in tests
    return params.clientInfo.name if params else None


def _startup_warnings(store: Storage, who: resolution.ResolvedIdentity) -> list[str]:
    """Things worth telling the agent about its own wiring. Computed before
    this process registers, so the roster still shows the other claimant."""
    parsed = identity.parse_or_none(who.name)
    address = parsed.client_address if parsed and parsed.client_address else who.name
    existing = store.get_agent(address)
    if existing is None or existing.repo_path == who.repo_path:
        return []
    return [
        f"the name {address!r} was last registered for {existing.repo_path}, "
        f"not {who.repo_path}. If both repos are in use they share one "
        "inbox and will take each other's mail — give one a different name "
        "(a `.agent-bus-name` file, then re-run `agent-bus init`)."
    ]


def start(
    *,
    store: Storage,
    name: str | None = None,
    repo_path: str | None = None,
    client: str | None = None,
    process: Proc | None = None,
    lineage: list[Proc] | None = None,
    env: dict[str, str] | None = None,
) -> Me:
    """Work out who this server is and register it: as a session of its
    client when it speaks for one, else under its name as given.

    `process` and `lineage` default to this process and its ancestry; tests
    pass stand-ins to play several sessions from one process."""
    env = dict(os.environ) if env is None else env
    who = _who_am_i(store, name, repo_path, client)
    warnings = _startup_warnings(store, who)
    parsed = identity.parse_or_none(who.name)
    if not settings.sessions_enabled() or parsed is None or parsed.client is None:
        store.upsert_agent(who.name, who.repo_path)
        return Me(who.name, who.repo_path, warnings=warnings)

    process = process or procs.describe(os.getpid())
    if process is None:
        store.upsert_agent(who.name, who.repo_path)
        warnings.append(
            "this system does not say which process started this server, so "
            "it cannot hold a session address and shares the client address"
        )
        return Me(who.name, who.repo_path, warnings=warnings)

    registry = SessionRegistry(store)
    address = parsed.client_address
    assert address is not None  # parsed.client is set
    claim = registry.claim(
        client_address=address,
        repo_path=who.repo_path,
        server=process,
        lineage=procs.ancestry() if lineage is None else lineage,
        session_key=clients.session_key(parsed.client, env),
        pinned=parsed.handle,
    )
    return Me(
        claim.name, who.repo_path, registry=registry, process=process,
        client_address=address, warnings=warnings + claim.warnings,
    )


def _instructions(me: Me) -> str:
    text = (
        "Local multi-agent message bus. You are identified as "
        f"`{me.name}`. Use `read_inbox` at the start of a turn if "
        "you suspect pending messages; the Stop/UserPromptSubmit hooks "
        "will surface them automatically when configured. "
        "Address a message to a full agent name (`repo-a/claude-1`) for "
        "one session, to a client (`repo-a/claude`) for whichever of its "
        "sessions reads first, to a bare repo name (`repo-a`) for every "
        "agent working in that repo, or to `*` for every peer except you."
    )
    if me.holds_session:
        text += (
            f" `{me.client_address}` is shared by every session of this "
            "client here. Once you know what this session is working on, "
            "call `set_session` with a short label and topic so peers can "
            "reach you by name; `whoami` shows your current address."
        )
    return text


def _other_live_sessions(me: Me, live_names: set[str]) -> list[str]:
    return [
        n for n in live_names
        if n != me.name and identity.parse(n).client_address == me.client_address
    ]


def _own_address_unless_shared(me: Me, live_names: set[str]) -> frozenset[str]:
    """A repo-wide or broadcast send reaches the sender's sibling sessions
    through their shared client address — but only while one is running;
    otherwise the next session to start would get the sender's own
    message back."""
    if me.client_address is None or _other_live_sessions(me, live_names):
        return frozenset()
    return frozenset({me.client_address})


def _session_note(me: Me, to: str, live_names: set[str]) -> str | None:
    """A word for the sender when nobody is running to read the message."""
    parsed = identity.parse_or_none(to)
    if parsed is None or parsed.client is None:
        return None
    if parsed.is_session and to not in live_names:
        return (
            f"no running session holds {to!r}; the message waits until a "
            f"session takes that name again. To reach whichever session is "
            f"running, send to {parsed.client_address!r}."
        )
    if not parsed.is_session and me.client_address == to:
        if not _other_live_sessions(me, live_names):
            return (
                f"no other session of {to!r} is running; the next one to "
                "start will pick this up"
            )
    return None


def build_mcp(me: Me, store: Storage) -> FastMCP:
    """The FastMCP app for an identity `start` has settled."""
    mcp = FastMCP("agent-bus", instructions=_instructions(me))

    def live_names() -> set[str]:
        return me.registry.live_names() if me.registry else set()

    def describe(row, *, pending: int | None = None, live: set[str]) -> dict:
        out = row.to_dict(pending_count=pending)
        out["live"] = (row.name in live) if row.kind == KIND_SESSION else None
        return out

    @mcp.tool()
    def whoami(ctx: Context) -> dict:
        """This server's identity: its name (its session address, when it
        holds one), its repo's group, the client address its sessions
        share, the other agents working in the same repo, plus any wiring
        warnings."""
        store.touch_agent(me.name)
        row = store.get_agent(me.name)
        parsed = identity.parse_or_none(me.name)
        group = row.group if row else me.name
        live = live_names()
        return {
            "name": me.name,
            "group": group,
            "client": parsed.client if parsed else None,
            "client_address": parsed.client_address if parsed else None,
            "session": parsed.handle if parsed else None,
            "instance": parsed.number if parsed else None,
            "topic": row.topic if row else None,
            "repo_path": row.repo_path if row else me.repo_path,
            "registered_at": row.registered_at if row else None,
            "last_seen": row.last_seen if row else None,
            "mcp_client": _mcp_client_name(ctx),
            "group_members": [
                {
                    "name": a.name, "kind": a.kind, "client": a.client_id,
                    "topic": a.topic, "last_seen": a.last_seen,
                    "live": (a.name in live) if a.kind == KIND_SESSION else None,
                }
                for a in store.list_agents()
                if a.group == group
            ],
            "warnings": me.warnings,
        }

    @mcp.tool()
    def list_agents(group: str | None = None) -> list[dict]:
        """Every agent that has ever connected, plus its current unread
        count. Pass `group` (a bare repo name) to see one repo's agents.
        Sessions say whether a running server holds them (`live`)."""
        store.touch_agent(me.name)
        live = live_names()
        return [
            describe(a, pending=c, live=live)
            for a, c in store.list_agents_with_counts()
            if group is None or a.group == group
        ]

    @mcp.tool()
    def send_message(to: str, body: str, thread_id: str | None = None) -> dict:
        """Send `body` to one session, to a client, to every agent in a
        repo, or to everyone.

        `to` is a session address such as `repo-a/claude-frontend` (exactly
        that session), a client address such as `repo-a/claude` (whichever
        of that client's sessions reads it first; a session never receives
        its own), a bare repo name such as `repo-a` (every agent working in
        that repo, except you), or `*` (every agent on the bus, except
        you). `list_agents` shows the names. An unknown `to` is an error
        and nothing is sent.

        Returns {"to", "kind", "message_ids", "recipients", "thread_id",
        "sent_at"}, plus "message_id" when exactly one agent received it
        and "note" when no running session is there to read it.
        `kind` is "direct", "group" or "broadcast". Each recipient gets its
        own row and message_id, so read state is tracked per agent.
        """
        store.touch_agent(me.name)
        live = live_names() if me.registry is not None else set()
        result = store.send_message(
            from_agent=me.name, to=to, body=body, thread_id=thread_id,
            actor=me.name, skip=_own_address_unless_shared(me, live),
        )
        if me.registry is not None:
            note = _session_note(me, to, live)
            if note:
                result["note"] = note
        return result

    @mcp.tool()
    def read_inbox(mark_read: bool = True, limit: int = 50) -> list[dict]:
        """Unread messages for this session, oldest first — its own, plus
        any sent to its client address that no other session has taken.

        Set mark_read=False to peek without flipping read_at.
        """
        store.touch_agent(me.name)
        msgs = store.read_inbox(
            agent=me.name, mark_read=mark_read, limit=limit, actor=me.name,
        )
        return [m.to_dict() for m in msgs]

    @mcp.tool()
    def read_thread(thread_id: str, limit: int = 100) -> list[dict]:
        """Full thread across all participants, ordered by sent_at."""
        store.touch_agent(me.name)
        return [m.to_dict() for m in store.read_thread(thread_id=thread_id, limit=limit)]

    @mcp.tool()
    def tail_audit(limit: int = 50) -> list[dict]:
        """Last N audit-log entries across the whole bus.

        Audit rows are the recovery-truth source; messages.body is the
        only place the full body lives, but the audit log records its
        sha256 so you can reconstruct provenance.
        """
        store.touch_agent(me.name)
        return audit.tail(limit=limit)

    @mcp.tool()
    def set_session(label: str | None = None, topic: str | None = None) -> dict:
        """Name this session after what it works on, so peers can reach it.

        `label` renames the session to `<repo>/<client>-<label>` (for
        example "frontend" or "release-notes"): unread mail moves with it, and a
        label an ended session left behind is taken over along with its
        waiting mail. `topic` is a short free-text line shown next to the
        name in `list_agents`; an empty string clears it.
        Returns {"name", "client_address", "topic"}.
        """
        if not me.holds_session or me.client_address is None:
            raise SessionError(
                f"{me.name!r} is not a session of a client, so it has no "
                "session address to name"
            )
        if label is not None:
            assert me.registry is not None and me.process is not None
            me.name = me.registry.rename(me.process, me.client_address, label)
        if topic is not None:
            store.set_topic(me.name, topic.strip()[:MAX_TOPIC_LEN] or None)
        store.touch_agent(me.name)
        row = store.get_agent(me.name)
        return {
            "name": me.name,
            "client_address": me.client_address,
            "topic": row.topic if row else None,
        }

    return mcp


def build_server(
    *,
    name: str | None = None,
    repo_path: str | None = None,
    storage: Storage | None = None,
    client: str | None = None,
    process: Proc | None = None,
    lineage: list[Proc] | None = None,
) -> FastMCP:
    """Construct (but do not run) the MCP server.

    Pulled out of `main()` so tests can wire a Storage with a temp DB.
    """
    store = storage or Storage()
    me = start(
        store=store, name=name, repo_path=repo_path, client=client,
        process=process, lineage=lineage,
    )
    return build_mcp(me, store)


def _exit_on_signal(signum: int, _frame: Any) -> None:
    raise SystemExit(128 + signum)


def main(client: str | None = None) -> None:  # pragma: no cover (entrypoint)
    store = Storage()
    me = start(store=store, client=client)
    # a client closing its session usually just closes our stdin, which
    # ends `run()`; a signal must end it the same way, so that the session
    # address is given up rather than left for the next server to clean up
    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, _exit_on_signal)
    try:
        build_mcp(me, store).run()  # stdio transport by default
    finally:
        me.release()


if __name__ == "__main__":  # pragma: no cover
    main()

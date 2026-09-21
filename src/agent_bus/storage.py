"""SQLite-backed storage for the agent bus.

WAL mode lets multiple processes write concurrently. Each call opens a
short-lived connection so the module is safe from any thread, and so the
OS reclaims file descriptors promptly when callers go away.

Schema (created and versioned by `migrations.py`):
  agents(name PK, repo_path, registered_at, last_seen, group_name, client)
  messages(message_id PK, from_agent, to_agent, body, thread_id,
           sent_at, read_at, delivered_at,
           addressed_to, fanout_id, to_group, claimed_by)

An agent named `<group>/<client>` belongs to `<group>`; any other name is
a group of one (see `identity.py`).

Addressing: `to` is "*" (everyone but the sender), a full agent name
(exactly that agent), or a bare name (every agent in that group but the
sender). Group and broadcast sends fan out into N message rows, one per
recipient, each with its own message_id, sharing one `fanout_id`. We never
store a single row for many recipients — that would require per-recipient
read state on the same row, which the schema deliberately rejects.
"""

from __future__ import annotations

import difflib
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from . import audit, identity, migrations, settings, wake
from .identity import BROADCAST, KIND_BROADCAST, KIND_DIRECT, KIND_GROUP
from .paths import db_path, ensure_parents

TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
MAX_SUGGESTIONS = 3


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime(TIMESTAMP_FORMAT)


class UnknownRecipientError(ValueError):
    """`to` names neither a registered agent nor a group with members."""

    def __init__(self, to: str, *, members: list[str], close: list[str]) -> None:
        self.to = to
        hint = ""
        if members:
            hint = f" Agents in that repo: {', '.join(members)}."
        elif close:
            hint = f" Did you mean: {', '.join(close)}?"
        super().__init__(
            f"no agent or group named {to!r} is on the bus.{hint} "
            "Nothing was sent; list the agents to see who is reachable."
        )


MESSAGE_COLUMNS = (
    "message_id, from_agent, to_agent, body, thread_id, sent_at, read_at, "
    "delivered_at, addressed_to, fanout_id, to_group, claimed_by"
)


def _inbox_predicate(agent_expr: str, group_expr: str) -> str:
    """WHERE clause for the unread rows an agent may drain.

    Its own rows, plus rows addressed to its bare group name while no agent
    is registered under exactly that name. Those are mail for a repo from
    before it had per-client identities, or written by an older agent-bus
    that does not expand groups; the first member to read takes them. Rows
    from a fan-out are excluded — every member already has its own copy.

    Both arguments are SQL expressions chosen by this module, never input.
    """
    return f"""
        read_at IS NULL AND (
            to_agent = {agent_expr}
            OR (
                to_agent = {group_expr}
                AND {group_expr} != {agent_expr}
                AND fanout_id IS NULL
                AND NOT EXISTS (
                    SELECT 1 FROM agents AS namesake
                    WHERE namesake.name = {group_expr}
                )
            )
        )
    """


INBOX_PREDICATE = _inbox_predicate(":agent", ":group")
ROSTER_INBOX_PREDICATE = _inbox_predicate(
    "agents.name", "COALESCE(agents.group_name, agents.name)"
)


def _inbox_params(agent: str) -> dict[str, str]:
    parsed = identity.parse_or_none(agent)
    has_client = parsed is not None and parsed.client is not None
    return {"agent": agent, "group": parsed.group if has_client else agent}


@dataclass
class Message:
    message_id: str
    from_agent: str
    to_agent: str
    body: str
    thread_id: str
    sent_at: str
    read_at: str | None
    delivered_at: str | None
    # All NULL on rows written before group addressing, or by an older
    # agent-bus sharing this database.
    addressed_to: str | None = None
    fanout_id: str | None = None
    to_group: str | None = None
    claimed_by: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Message":
        return cls(**{column: row[column] for column in row.keys()})

    @property
    def kind(self) -> str:
        if self.fanout_id is None:
            return KIND_DIRECT
        if self.addressed_to == BROADCAST:
            return KIND_BROADCAST
        return KIND_GROUP

    def to_dict(self) -> dict:
        return {
            "message_id": self.message_id,
            "from": self.from_agent,
            "to": self.to_agent,
            "addressed_to": self.addressed_to,
            "kind": self.kind,
            "body": self.body,
            "thread_id": self.thread_id,
            "sent_at": self.sent_at,
            "read_at": self.read_at,
            "delivered_at": self.delivered_at,
        }


AGENT_COLUMNS = "name, repo_path, registered_at, last_seen, group_name, client"


@dataclass
class AgentRow:
    name: str
    repo_path: str
    registered_at: str
    last_seen: str
    # NULL for a name with no client part, and for any row written by an
    # agent-bus that predates these columns.
    group_name: str | None = None
    client: str | None = None

    @property
    def group(self) -> str:
        """The group this agent answers to. An agent without a client
        part is a group of one, named after itself."""
        return self.group_name or self.name

    def to_dict(self, *, pending_count: int | None = None) -> dict:
        out = {
            "name": self.name,
            "group": self.group,
            "client": self.client,
            "repo_path": self.repo_path,
            "registered_at": self.registered_at,
            "last_seen": self.last_seen,
        }
        if pending_count is not None:
            out["pending_count"] = pending_count
        return out


@dataclass(frozen=True)
class _Delivery:
    """What one send turned into: who got it, under which ids."""

    kind: str
    thread_id: str | None
    recipients: list[str]
    message_ids: list[str]


class Storage:
    """Thin wrapper around the SQLite DB.

    Construct once per process and reuse — `connect()` opens a fresh
    connection per call so the instance is safe across threads.
    """

    def __init__(self, path: Path | None = None):
        self.path = path or db_path()
        ensure_parents(self.path)
        self._initialised = False

    # ----------------------------- plumbing ---------------------------------

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            yield conn
        finally:
            conn.close()

    def init_schema(self) -> None:
        if self._initialised:
            return
        with self.connect() as conn:
            migrations.migrate(conn, self.path)
        self._initialised = True

    # ----------------------------- agents -----------------------------------

    def upsert_agent(self, name: str, repo_path: str) -> AgentRow:
        """Register `name` as living in `repo_path`. Authoritative: call it
        only from the process that owns the identity (its MCP server).
        Anything merely speaking as a name uses `ensure_agent`."""
        return self._register(name, repo_path, overwrite_repo=True)

    def ensure_agent(self, name: str, repo_path: str) -> AgentRow:
        """Put `name` on the roster if it is missing; otherwise only bump
        `last_seen`. Never rewrites where an existing agent lives."""
        return self._register(name, repo_path, overwrite_repo=False)

    def _register(self, name: str, repo_path: str, *, overwrite_repo: bool) -> AgentRow:
        self.init_schema()
        now = _utc_now_iso()
        parsed = identity.parse_or_none(name)
        has_client = parsed is not None and parsed.client is not None
        group_name = parsed.group if has_client else None
        client = parsed.client if has_client else None
        repo_update = "repo_path = excluded.repo_path," if overwrite_repo else ""
        with self.connect() as conn:
            conn.execute(
                f"""
                INSERT INTO agents
                    (name, repo_path, registered_at, last_seen, group_name, client)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    {repo_update}
                    group_name = excluded.group_name,
                    client = excluded.client,
                    last_seen = excluded.last_seen
                """,
                (name, repo_path, now, now, group_name, client),
            )
            row = conn.execute(
                f"SELECT {AGENT_COLUMNS} FROM agents WHERE name = ?",
                (name,),
            ).fetchone()
        return AgentRow(**dict(row))

    def get_agent(self, name: str) -> AgentRow | None:
        self.init_schema()
        with self.connect() as conn:
            row = conn.execute(
                f"SELECT {AGENT_COLUMNS} FROM agents WHERE name = ?",
                (name,),
            ).fetchone()
        return AgentRow(**dict(row)) if row else None

    def list_agents(self) -> list[AgentRow]:
        self.init_schema()
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT {AGENT_COLUMNS} FROM agents ORDER BY name"
            ).fetchall()
        return [AgentRow(**dict(r)) for r in rows]

    def list_agents_with_counts(self) -> list[tuple[AgentRow, int]]:
        self.init_schema()
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT {AGENT_COLUMNS},
                       (SELECT COUNT(*) FROM messages
                        WHERE {ROSTER_INBOX_PREDICATE}) AS pending_count
                FROM agents ORDER BY name
                """
            ).fetchall()
        agents: list[tuple[AgentRow, int]] = []
        for r in rows:
            fields = dict(r)
            pending = int(fields.pop("pending_count"))
            agents.append((AgentRow(**fields), pending))
        return agents

    def touch_agent(self, name: str) -> None:
        self.init_schema()
        with self.connect() as conn:
            conn.execute(
                "UPDATE agents SET last_seen = ? WHERE name = ?",
                (_utc_now_iso(), name),
            )

    def forget_agent(self, name: str) -> bool:
        """Remove an agent from the roster. Returns True if a row was deleted.

        Does NOT touch the `messages` table: history is preserved, and if
        the agent ever reconnects (e.g. `agent-bus chat --name <same>`)
        the upsert recreates the row and any unread messages still
        surface in the next `read_inbox`. Forgetting is therefore
        reversible — it just means "stop showing this name on the
        roster and stop fanning broadcasts out to it for now".
        """
        self.init_schema()
        with self.connect() as conn:
            cur = conn.execute("DELETE FROM agents WHERE name = ?", (name,))
            return cur.rowcount > 0

    # ----------------------------- messages ---------------------------------

    @staticmethod
    def _drop_idle_members(candidates: list[AgentRow]) -> list[AgentRow]:
        """Trim clients nobody has seen for a while from a fan-out.

        A stale client is dropped only when a fresher one shares its group,
        so the cutoff thins out a repo's roster but never silences a repo.
        """
        max_idle_days = settings.fanout_max_idle_days()
        if max_idle_days <= 0:
            return candidates
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=max_idle_days)
        ).strftime(TIMESTAMP_FORMAT)

        by_group: dict[str, list[AgentRow]] = {}
        for agent in candidates:
            by_group.setdefault(agent.group, []).append(agent)
        kept: list[AgentRow] = []
        for members in by_group.values():
            fresh = [m for m in members if m.last_seen >= cutoff]
            kept.extend(fresh or members)
        return sorted(kept, key=lambda a: a.name)

    @staticmethod
    def _unknown_recipient(to: str, agents: list[AgentRow]) -> UnknownRecipientError:
        group = to.partition(identity.SEPARATOR)[0]
        members = [a.name for a in agents if a.group == group]
        known = sorted({a.name for a in agents} | {a.group for a in agents})
        close = difflib.get_close_matches(to, known, n=MAX_SUGGESTIONS)
        return UnknownRecipientError(to, members=members, close=close)

    def _resolve_recipients(
        self, conn: sqlite3.Connection, *, from_agent: str, to: str
    ) -> tuple[str, list[AgentRow]]:
        """Turn `to` into (kind, recipients). Runs inside the send
        transaction so the roster it reads is the roster it writes for."""
        agents = [
            AgentRow(**dict(r))
            for r in conn.execute(f"SELECT {AGENT_COLUMNS} FROM agents ORDER BY name")
        ]
        others = [a for a in agents if a.name != from_agent]

        if to == BROADCAST:
            return KIND_BROADCAST, self._drop_idle_members(others)

        if identity.SEPARATOR in to:
            exact = [a for a in agents if a.name == to]
            if not exact:
                raise self._unknown_recipient(to, agents)
            return KIND_DIRECT, exact

        members = [a for a in agents if a.group == to]
        if not members:
            raise self._unknown_recipient(to, agents)
        if len(members) == 1 and members[0].name == to:
            return KIND_DIRECT, members
        return KIND_GROUP, self._drop_idle_members(
            [m for m in members if m.name != from_agent]
        )

    def send_message(
        self,
        *,
        from_agent: str,
        to: str,
        body: str,
        thread_id: str | None = None,
        actor: str | None = None,
    ) -> dict:
        """Insert one row per recipient. Writes audit rows BEFORE returning.

        Always returns {"to", "kind", "message_ids", "recipients",
        "thread_id", "sent_at"}; "message_id" is added whenever exactly one
        agent received it and `to` was not a broadcast. `kind` is "direct",
        "group" or "broadcast". A group or broadcast with nobody to receive
        it is not an error — the lists are simply empty.

        Raises UnknownRecipientError when `to` names nobody on the bus.
        """
        if not body:
            raise ValueError("body must be non-empty")
        if not to:
            raise ValueError("to must be non-empty (use '*' to broadcast)")

        self.init_schema()
        sent_at = _utc_now_iso()
        actor_name = actor or from_agent

        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                delivery = self._deliver(
                    conn, from_agent=from_agent, to=to, body=body,
                    thread_id=thread_id, sent_at=sent_at,
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

        result = {
            "to": to,
            "kind": delivery.kind,
            "message_ids": delivery.message_ids,
            "recipients": delivery.recipients,
            "thread_id": delivery.thread_id,
            "sent_at": sent_at,
        }
        if not delivery.recipients:
            return result
        if len(delivery.message_ids) == 1 and to != BROADCAST:
            result["message_id"] = delivery.message_ids[0]

        audit.append_many(
            {
                "ts": sent_at,
                "op": "send",
                "actor": actor_name,
                "message_id": mid,
                "from": from_agent,
                "to": name,
                "addressed_to": to,
                "thread_id": delivery.thread_id,
                "body_preview": audit.body_preview(body),
                "body_sha256": audit.body_sha256(body),
            }
            for mid, name in zip(delivery.message_ids, delivery.recipients)
        )

        self._fire_wakes(
            recipients=delivery.recipients,
            message_ids=delivery.message_ids,
            from_agent=from_agent,
            actor_name=actor_name,
            body=body,
            thread_id=delivery.thread_id,
        )
        return result

    def _deliver(
        self,
        conn: sqlite3.Connection,
        *,
        from_agent: str,
        to: str,
        body: str,
        thread_id: str | None,
        sent_at: str,
    ) -> "_Delivery":
        """Resolve `to` and insert one row per recipient, inside the
        caller's transaction."""
        kind, recipients = self._resolve_recipients(conn, from_agent=from_agent, to=to)
        thread = thread_id or (str(uuid.uuid4()) if recipients else None)
        is_fan_out = kind != KIND_DIRECT
        fanout_id = str(uuid.uuid4()) if is_fan_out else None
        message_ids = [str(uuid.uuid4()) for _ in recipients]
        conn.executemany(
            """
            INSERT INTO messages
                (message_id, from_agent, to_agent, body, thread_id,
                 sent_at, addressed_to, fanout_id, to_group)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    mid, from_agent, recipient.name, body, thread, sent_at,
                    to, fanout_id, recipient.group if is_fan_out else None,
                )
                for mid, recipient in zip(message_ids, recipients)
            ],
        )
        return _Delivery(
            kind=kind,
            thread_id=thread,
            recipients=[r.name for r in recipients],
            message_ids=message_ids,
        )

    def read_inbox(
        self,
        *,
        agent: str,
        mark_read: bool = True,
        limit: int = 50,
        actor: str | None = None,
        also_deliver: bool = False,
    ) -> list[Message]:
        """Return unread messages for `agent`, oldest first.

        Writes one op="read" audit row per delivered message. If
        `also_deliver` is True (hook callers set this), also writes a
        twin op="deliver" row so the audit trail shows the hook path.

        When `mark_read=False`, returns the next batch of unread messages
        without flipping read_at — useful for previews and tests.
        """
        if limit <= 0:
            return []
        self.init_schema()
        now = _utc_now_iso()
        actor_name = actor or agent

        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = conn.execute(
                    f"""
                    SELECT {MESSAGE_COLUMNS}
                    FROM messages
                    WHERE {INBOX_PREDICATE}
                    ORDER BY sent_at ASC, message_id ASC
                    LIMIT :limit
                    """,
                    {**_inbox_params(agent), "limit": limit},
                ).fetchall()

                ids = [r["message_id"] for r in rows]
                if ids and mark_read:
                    placeholders = ",".join("?" * len(ids))
                    conn.execute(
                        f"UPDATE messages SET read_at = ?, "
                        f"delivered_at = COALESCE(delivered_at, ?) "
                        f"WHERE message_id IN ({placeholders})",
                        (now, now, *ids),
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

        messages = [Message.from_row(r) for r in rows]
        if mark_read:
            messages = [
                replace(m, read_at=now, delivered_at=m.delivered_at or now)
                for m in messages
            ]

        audit_rows: list[dict] = []
        for m in messages:
            audit_rows.append(
                {
                    "ts": now,
                    "op": "read",
                    "actor": actor_name,
                    "message_id": m.message_id,
                    "from": m.from_agent,
                    "to": m.to_agent,
                    "thread_id": m.thread_id,
                    "body_preview": audit.body_preview(m.body),
                    "body_sha256": audit.body_sha256(m.body),
                }
            )
            if also_deliver:
                audit_rows.append(
                    {
                        "ts": now,
                        "op": "deliver",
                        "actor": actor_name,
                        "message_id": m.message_id,
                        "from": m.from_agent,
                        "to": m.to_agent,
                        "thread_id": m.thread_id,
                        "body_preview": audit.body_preview(m.body),
                        "body_sha256": audit.body_sha256(m.body),
                    }
                )
        if audit_rows:
            audit.append_many(audit_rows)

        return messages

    def read_thread(self, *, thread_id: str, limit: int = 100) -> list[Message]:
        if limit <= 0:
            return []
        self.init_schema()
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT {MESSAGE_COLUMNS}
                FROM messages
                WHERE thread_id = ?
                ORDER BY sent_at ASC, message_id ASC
                LIMIT ?
                """,
                (thread_id, limit),
            ).fetchall()
        return [Message.from_row(r) for r in rows]

    def recent_messages(self, *, limit: int = 10) -> list[Message]:
        """Return the N most-recent messages across the bus, oldest first.

        Used by the chat TUI to print connect-time context. Goes through
        the `messages` table (not the audit log) so the full body is
        available — audit rows only store the 200-char preview.
        """
        if limit <= 0:
            return []
        self.init_schema()
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT {MESSAGE_COLUMNS}
                FROM messages
                ORDER BY sent_at DESC, message_id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        msgs = [Message.from_row(r) for r in rows]
        msgs.reverse()  # oldest first for display
        return msgs

    def _fire_wakes(
        self,
        *,
        recipients: list[str],
        message_ids: list[str],
        from_agent: str,
        actor_name: str,
        body: str,
        thread_id: str,
    ) -> None:
        """Run each recipient's configured wake command. Fire-and-forget.

        Wake config is loaded once per send (cheap: file is ~hundreds of
        bytes typically) so per-call edits to wake.json take effect on
        the very next message — no daemon restart needed.

        Failures are absorbed: a misconfigured wake must never break a
        send. The op='wake' audit row captures launch status either way
        so users can debug from the log.
        """
        try:
            cfg = wake.load_wake_config()
        except Exception:
            cfg = {}
        if not cfg:
            return

        wake_rows: list[dict] = []
        for mid, recipient in zip(message_ids, recipients):
            try:
                fired, status = wake.fire_wake(
                    recipient,
                    from_agent=from_agent,
                    to_agent=recipient,
                    body=body,
                    thread_id=thread_id,
                    message_id=mid,
                    config=cfg,
                )
            except Exception as e:
                fired, status = False, f"fired:ERR:{type(e).__name__}"
            if fired or status.startswith("fired:"):
                wake_rows.append(
                    {
                        "ts": _utc_now_iso(),
                        "op": "wake",
                        "actor": actor_name,
                        "message_id": mid,
                        "from": from_agent,
                        "to": recipient,
                        "thread_id": thread_id,
                        "body_preview": audit.body_preview(body),
                        "body_sha256": audit.body_sha256(body),
                        "wake_status": status,
                    }
                )
        if wake_rows:
            audit.append_many(wake_rows)

    def pending_count(self, *, agent: str) -> int:
        self.init_schema()
        with self.connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS c FROM messages WHERE {INBOX_PREDICATE}",
                _inbox_params(agent),
            ).fetchone()
        return int(row["c"]) if row else 0

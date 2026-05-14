"""SQLite-backed storage for the agent bus.

WAL mode lets multiple processes write concurrently. Each call opens a
short-lived connection so the module is safe from any thread, and so the
OS reclaims file descriptors promptly when callers go away.

Schema (declared in SCHEMA_SQL below):
  agents(name PK, repo_path, registered_at, last_seen)
  messages(message_id PK, from_agent, to_agent, body, thread_id,
           sent_at, read_at, delivered_at)

A "broadcast" send (to="*") fans out into N message rows, one per
non-sender peer, each with its own unique message_id. We never expand
to="*" into a single row — that would require per-recipient read state
on the same row, which the schema deliberately rejects.
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from . import audit
from .paths import db_path, ensure_parents

BROADCAST = "*"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS agents (
    name           TEXT PRIMARY KEY,
    repo_path      TEXT NOT NULL,
    registered_at  TEXT NOT NULL,
    last_seen      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    message_id     TEXT PRIMARY KEY,
    from_agent     TEXT NOT NULL,
    to_agent       TEXT NOT NULL,
    body           TEXT NOT NULL,
    thread_id      TEXT NOT NULL,
    sent_at        TEXT NOT NULL,
    read_at        TEXT,
    delivered_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_messages_inbox
    ON messages (to_agent, read_at);

CREATE INDEX IF NOT EXISTS idx_messages_thread
    ON messages (thread_id, sent_at);
"""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


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

    def to_dict(self) -> dict:
        return {
            "message_id": self.message_id,
            "from": self.from_agent,
            "to": self.to_agent,
            "body": self.body,
            "thread_id": self.thread_id,
            "sent_at": self.sent_at,
            "read_at": self.read_at,
            "delivered_at": self.delivered_at,
        }


@dataclass
class AgentRow:
    name: str
    repo_path: str
    registered_at: str
    last_seen: str

    def to_dict(self, *, pending_count: int | None = None) -> dict:
        out = {
            "name": self.name,
            "repo_path": self.repo_path,
            "registered_at": self.registered_at,
            "last_seen": self.last_seen,
        }
        if pending_count is not None:
            out["pending_count"] = pending_count
        return out


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
            conn.executescript(SCHEMA_SQL)
        self._initialised = True

    # ----------------------------- agents -----------------------------------

    def upsert_agent(self, name: str, repo_path: str) -> AgentRow:
        self.init_schema()
        now = _utc_now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO agents (name, repo_path, registered_at, last_seen)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    repo_path = excluded.repo_path,
                    last_seen = excluded.last_seen
                """,
                (name, repo_path, now, now),
            )
            row = conn.execute(
                "SELECT name, repo_path, registered_at, last_seen FROM agents WHERE name = ?",
                (name,),
            ).fetchone()
        return AgentRow(**dict(row))

    def get_agent(self, name: str) -> AgentRow | None:
        self.init_schema()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT name, repo_path, registered_at, last_seen FROM agents WHERE name = ?",
                (name,),
            ).fetchone()
        return AgentRow(**dict(row)) if row else None

    def list_agents(self) -> list[AgentRow]:
        self.init_schema()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT name, repo_path, registered_at, last_seen
                FROM agents ORDER BY name
                """
            ).fetchall()
        return [AgentRow(**dict(r)) for r in rows]

    def list_agents_with_counts(self) -> list[tuple[AgentRow, int]]:
        self.init_schema()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT a.name, a.repo_path, a.registered_at, a.last_seen,
                       COALESCE((
                         SELECT COUNT(*) FROM messages m
                         WHERE m.to_agent = a.name AND m.read_at IS NULL
                       ), 0) AS pending_count
                FROM agents a ORDER BY a.name
                """
            ).fetchall()
        return [
            (
                AgentRow(
                    name=r["name"],
                    repo_path=r["repo_path"],
                    registered_at=r["registered_at"],
                    last_seen=r["last_seen"],
                ),
                int(r["pending_count"]),
            )
            for r in rows
        ]

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

    def _expand_recipients(self, from_agent: str, to: str) -> list[str]:
        if to != BROADCAST:
            return [to]
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT name FROM agents WHERE name != ? ORDER BY name",
                (from_agent,),
            ).fetchall()
        return [r["name"] for r in rows]

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

        Return shape:
          - unicast: {"message_id": "...", "sent_at": ts,
                      "thread_id": ..., "recipients": [name]}
          - broadcast (to="*"): {"message_ids": [...], "sent_at": ts,
                                 "thread_id": ..., "recipients": [...]}
        """
        if not body:
            raise ValueError("body must be non-empty")
        if not to:
            raise ValueError("to must be non-empty (use '*' to broadcast)")

        self.init_schema()
        recipients = self._expand_recipients(from_agent, to)
        if not recipients:
            # broadcast with no peers — nothing to send, but not an error
            return {
                "message_ids": [],
                "sent_at": _utc_now_iso(),
                "thread_id": thread_id,
                "recipients": [],
            }

        sent_at = _utc_now_iso()
        thread = thread_id or str(uuid.uuid4())
        actor_name = actor or from_agent

        rows: list[tuple[str, str, str, str, str, str]] = []
        message_ids: list[str] = []
        audit_rows: list[dict] = []
        for recipient in recipients:
            mid = str(uuid.uuid4())
            message_ids.append(mid)
            rows.append((mid, from_agent, recipient, body, thread, sent_at))
            audit_rows.append(
                {
                    "ts": sent_at,
                    "op": "send",
                    "actor": actor_name,
                    "message_id": mid,
                    "from": from_agent,
                    "to": recipient,
                    "thread_id": thread,
                    "body_preview": audit.body_preview(body),
                    "body_sha256": audit.body_sha256(body),
                }
            )

        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.executemany(
                    """
                    INSERT INTO messages
                        (message_id, from_agent, to_agent, body, thread_id, sent_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

        audit.append_many(audit_rows)

        if to == BROADCAST:
            return {
                "message_ids": message_ids,
                "sent_at": sent_at,
                "thread_id": thread,
                "recipients": recipients,
            }
        return {
            "message_id": message_ids[0],
            "sent_at": sent_at,
            "thread_id": thread,
            "recipients": recipients,
        }

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
                    """
                    SELECT message_id, from_agent, to_agent, body, thread_id,
                           sent_at, read_at, delivered_at
                    FROM messages
                    WHERE to_agent = ? AND read_at IS NULL
                    ORDER BY sent_at ASC, message_id ASC
                    LIMIT ?
                    """,
                    (agent, limit),
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

        messages = [
            Message(
                message_id=r["message_id"],
                from_agent=r["from_agent"],
                to_agent=r["to_agent"],
                body=r["body"],
                thread_id=r["thread_id"],
                sent_at=r["sent_at"],
                read_at=now if mark_read else r["read_at"],
                delivered_at=(r["delivered_at"] or now) if mark_read else r["delivered_at"],
            )
            for r in rows
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
                """
                SELECT message_id, from_agent, to_agent, body, thread_id,
                       sent_at, read_at, delivered_at
                FROM messages
                WHERE thread_id = ?
                ORDER BY sent_at ASC, message_id ASC
                LIMIT ?
                """,
                (thread_id, limit),
            ).fetchall()
        return [
            Message(
                message_id=r["message_id"],
                from_agent=r["from_agent"],
                to_agent=r["to_agent"],
                body=r["body"],
                thread_id=r["thread_id"],
                sent_at=r["sent_at"],
                read_at=r["read_at"],
                delivered_at=r["delivered_at"],
            )
            for r in rows
        ]

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
                """
                SELECT message_id, from_agent, to_agent, body, thread_id,
                       sent_at, read_at, delivered_at
                FROM messages
                ORDER BY sent_at DESC, message_id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        msgs = [
            Message(
                message_id=r["message_id"],
                from_agent=r["from_agent"],
                to_agent=r["to_agent"],
                body=r["body"],
                thread_id=r["thread_id"],
                sent_at=r["sent_at"],
                read_at=r["read_at"],
                delivered_at=r["delivered_at"],
            )
            for r in rows
        ]
        msgs.reverse()  # oldest first for display
        return msgs

    def pending_count(self, *, agent: str) -> int:
        self.init_schema()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM messages WHERE to_agent = ? AND read_at IS NULL",
                (agent,),
            ).fetchone()
        return int(row["c"]) if row else 0

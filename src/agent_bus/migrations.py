"""Forward-only schema migrations, versioned through ``PRAGMA user_version``.

``user_version = epoch * EPOCH_SIZE + step``

  * A **step** is an additive change: new nullable columns, new tables,
    new indexes. Older agent-bus binaries name their columns explicitly,
    so they keep working against a database that is a few steps ahead —
    which matters because long-running MCP servers outlive an upgrade.
    New code must therefore read a NULL in a newer column as "written by
    an older binary".
  * The **epoch** changes only for a change older binaries cannot survive.
    A binary refuses a database from a newer epoch instead of corrupting it.

A pre-versioning (0.4.x) database and a brand-new file both report
``user_version = 0``; step 1 is written to be safe for either.

There is no downgrade path. The first time an existing database is
migrated, a copy is saved next to it as ``<db>.pre-migrate-<version>``.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import identity

EPOCH_SIZE = 1000
SCHEMA_EPOCH = 1

BACKUP_SUFFIX = ".pre-migrate-"


class SchemaTooNewError(RuntimeError):
    """The database belongs to a newer, incompatible agent-bus."""

    def __init__(self, db_epoch: int) -> None:
        self.db_epoch = db_epoch
        super().__init__(
            f"this database uses schema epoch {db_epoch}, but this agent-bus "
            f"only understands epoch {SCHEMA_EPOCH} — upgrade agent-bus"
        )


@dataclass(frozen=True)
class Migration:
    step: int
    description: str
    apply: Callable[[sqlite3.Connection], None]


# --------------------------- steps ---------------------------------------


def _create_base_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS agents (
            name           TEXT PRIMARY KEY,
            repo_path      TEXT NOT NULL,
            registered_at  TEXT NOT NULL,
            last_seen      TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            message_id     TEXT PRIMARY KEY,
            from_agent     TEXT NOT NULL,
            to_agent       TEXT NOT NULL,
            body           TEXT NOT NULL,
            thread_id      TEXT NOT NULL,
            sent_at        TEXT NOT NULL,
            read_at        TEXT,
            delivered_at   TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_inbox "
        "ON messages (to_agent, read_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_thread "
        "ON messages (thread_id, sent_at)"
    )


def _add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, declaration: str
) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def _add_group_addressing_columns(conn: sqlite3.Connection) -> None:
    _add_column_if_missing(conn, "agents", "group_name", "TEXT")
    _add_column_if_missing(conn, "agents", "client", "TEXT")
    _add_column_if_missing(conn, "messages", "addressed_to", "TEXT")
    _add_column_if_missing(conn, "messages", "fanout_id", "TEXT")
    _add_column_if_missing(conn, "messages", "to_group", "TEXT")
    _add_column_if_missing(conn, "messages", "claimed_by", "TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agents_group ON agents (group_name)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_fanout ON messages (fanout_id)"
    )


def _add_sessions(conn: sqlite3.Connection) -> None:
    """One row per MCP server that holds (or recently held) a session
    address, plus a free-text topic per agent. Also files rows whose names
    only became valid with session handles (`repo/codex-docs`) under their
    repo. Their `client` stays NULL, as on every session row: 0.5.x parses
    the names of a client's rows strictly and would reject theirs."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            server_pid      INTEGER NOT NULL,
            server_started  TEXT NOT NULL,
            name            TEXT NOT NULL,
            client_address  TEXT NOT NULL,
            repo_path       TEXT NOT NULL,
            lineage         TEXT NOT NULL,
            session_key     TEXT,
            boot_id         TEXT,
            pid_ns          TEXT,
            started_at      TEXT NOT NULL,
            last_seen       TEXT NOT NULL,
            ended_at        TEXT,
            PRIMARY KEY (server_pid, server_started, client_address)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sessions_address "
        "ON sessions (client_address)"
    )
    _add_column_if_missing(conn, "agents", "topic", "TEXT")
    unfiled = conn.execute(
        "SELECT name FROM agents WHERE client IS NULL"
    ).fetchall()
    for (name,) in unfiled:
        parsed = identity.parse_or_none(name)
        if parsed is not None and parsed.is_session:
            conn.execute(
                "UPDATE agents SET group_name = ? WHERE name = ?",
                (parsed.group, name),
            )


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "base tables (0.4.x schema)", _create_base_tables),
    Migration(2, "group addressing columns", _add_group_addressing_columns),
    Migration(3, "sessions table and agent topics", _add_sessions),
)


# --------------------------- runner --------------------------------------


def target_version() -> int:
    return SCHEMA_EPOCH * EPOCH_SIZE + MIGRATIONS[-1].step


def _read_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _pending(version: int) -> list[Migration]:
    """Steps still to apply. Raises if the database is from a newer epoch."""
    db_epoch, db_step = divmod(version, EPOCH_SIZE)
    if db_epoch > SCHEMA_EPOCH:
        raise SchemaTooNewError(db_epoch)
    return [m for m in MIGRATIONS if m.step > db_step]


def _has_tables(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'agents'"
    ).fetchone()
    return row is not None


def _backup(db_path: Path, version: int) -> None:
    """Copy the database aside before its first migration.

    Called by the process holding the write lock, so concurrent starters
    cannot race on the backup file. A second connection is used because
    WAL readers are not blocked by our own pending write transaction.
    """
    target = db_path.with_name(f"{db_path.name}{BACKUP_SUFFIX}{version}")
    if target.exists():
        return
    partial = target.with_name(target.name + ".partial")
    source = sqlite3.connect(db_path, timeout=30.0)
    try:
        destination = sqlite3.connect(partial)
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()
    os.replace(partial, target)


def migrate(conn: sqlite3.Connection, db_path: Path) -> None:
    """Bring the database up to `target_version()`.

    `conn` must be in autocommit mode (``isolation_level=None``). The
    version is first read without a lock so an up-to-date database costs
    one PRAGMA; it is read again inside the write transaction because
    several freshly upgraded processes may race here, and only the first
    may apply the steps.
    """
    if not _pending(_read_version(conn)):
        return

    conn.execute("BEGIN IMMEDIATE")
    try:
        version = _read_version(conn)
        pending = _pending(version)
        if pending:
            if _has_tables(conn):
                _backup(db_path, version)
            for migration in pending:
                migration.apply(conn)
            conn.execute(f"PRAGMA user_version = {target_version()}")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

"""Task-state store: dispatch.db beside the bus DB, with its own migrations."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..migrations import Migration, _add_column_if_missing
from . import settings
from .model import Task


def _create_tasks(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            source TEXT NOT NULL,
            task_id TEXT NOT NULL,
            title TEXT NOT NULL,
            url TEXT NOT NULL,
            project_id TEXT,
            type TEXT,
            handled_types TEXT NOT NULL DEFAULT '[]',
            repo TEXT,
            label TEXT,
            session_name TEXT,
            first_seen_at TEXT NOT NULL,
            last_spawned_at TEXT,
            handled_at TEXT,
            last_comment_handled_at TEXT,
            last_notified_state TEXT,
            outcome TEXT,
            PRIMARY KEY (source, task_id)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_label ON tasks (label)")


# Where the task's session was last started and what became of it:
# `work_dir`/`clone_path` are the session's work folder and its clone;
# `worktree_removed_at` is when dispatch removed that worktree; `held_at` is
# when the needs-approval alert for the current hold was delivered;
# `approved_at` is when the owner approved starting past the soft limit.
# Both hold fields are cleared when the session starts.
_M1_COLUMNS = (
    "work_dir",
    "clone_path",
    "worktree_removed_at",
    "held_at",
    "approved_at",
)


def _add_m1_columns(conn: sqlite3.Connection) -> None:
    for column in _M1_COLUMNS:
        _add_column_if_missing(conn, "tasks", column, "TEXT")


# The commit the session's branch started from: a branch still at it has made
# no commits, so being "merged" into the base proves nothing.
def _add_start_commit(conn: sqlite3.Connection) -> None:
    _add_column_if_missing(conn, "tasks", "start_commit", "TEXT")


# Milestone 2: the route a task's session took (`client`, `model`), the triage
# hand-off to the dispatcher (`candidate_*`) and its decision (`decision*`).
_M2_COLUMNS = (
    "client",
    "model",
    "candidate_type",
    "candidate_at",
    "decision",
    "decision_at",
    "decision_reason",
    "decision_repo",
    "decision_client",
    "decision_model",
)
# Milestone 3: the clone choice the dispatcher made for a task.
_M3_COLUMNS = ("decision_clone",)
TRIAGE_FIELDS = (
    "candidate_type",
    "candidate_at",
    "decision",
    "decision_at",
    "decision_reason",
    "decision_repo",
    "decision_client",
    "decision_model",
    "decision_clone",
)
EVENT_CANDIDATE = "candidate"
EVENT_DECIDE = "decide"
EVENT_APPROVE = "approve"
EVENT_APPROVAL_REQUEST = "approval_request"
EVENT_EXECUTED = "executed"
EVENT_ROUTING = "routing"
EVENT_SCAN = "scan"
EVENT_DRAFT = "draft"
EVENT_SEND = "send"
EVENT_INCIDENT = "incident"

DRAFT_PENDING = "pending"
DRAFT_SENDING = "sending"
DRAFT_SENT = "sent"
DRAFT_PLACED = "placed"
DRAFT_DISCARDED = "discarded"

INCIDENT_OPEN = "open"
INCIDENT_CLOSED = "closed"

# Passed to save_source_state to leave the stored cursor as it is.
KEEP: Any = object()


def _add_m2(conn: sqlite3.Connection) -> None:
    for column in _M2_COLUMNS:
        _add_column_if_missing(conn, "tasks", column, "TEXT")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS decision_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            at TEXT NOT NULL,
            source TEXT,
            task_id TEXT,
            event TEXT NOT NULL,
            actor TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_decision_log_event ON decision_log (event, at)"
    )


def _add_m3(conn: sqlite3.Connection) -> None:
    _add_column_if_missing(conn, "tasks", "decision_clone", "TEXT")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS source_state (
            source TEXT PRIMARY KEY,
            cursor TEXT,
            last_run_at TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS message_items (
            source TEXT NOT NULL,
            item_id TEXT NOT NULL,
            container TEXT,
            rule TEXT,
            action TEXT,
            result TEXT NOT NULL,
            url TEXT,
            seen_at TEXT NOT NULL,
            alert TEXT,
            PRIMARY KEY (source, item_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS drafts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            item_id TEXT,
            kind TEXT NOT NULL,
            origin TEXT NOT NULL,
            rule TEXT,
            url TEXT,
            target TEXT NOT NULL DEFAULT '{}',
            text TEXT NOT NULL,
            status TEXT NOT NULL,
            incident_id INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            approved_at TEXT,
            approved_by TEXT,
            delivered_at TEXT,
            error TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_drafts_status ON drafts (status, id)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS incidents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT NOT NULL,
            source TEXT NOT NULL,
            container TEXT,
            rule TEXT NOT NULL,
            action TEXT NOT NULL,
            state TEXT NOT NULL,
            item_id TEXT,
            thread_ref TEXT,
            url TEXT,
            label TEXT,
            session_name TEXT,
            client TEXT,
            model TEXT,
            result TEXT,
            repeats INTEGER NOT NULL DEFAULT 0,
            opened_at TEXT NOT NULL,
            last_seen_at TEXT,
            closed_at TEXT,
            close_reason TEXT,
            report_at TEXT,
            spawn_attempted_at TEXT
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_incidents_open "
        "ON incidents (key) WHERE state = 'open'"
    )


def _add_spawn_attempts(conn: sqlite3.Connection) -> None:
    """Every incident session attempt, started or failed, and the last one on
    the incident: the hourly spawn limit counts attempts by when they were
    made, not by when the incident opened. A database step 5 created kept no
    attempt times; its earlier attempts count from when the incident opened,
    the best time it has."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(incidents)")}
    _add_column_if_missing(conn, "incidents", "spawn_attempted_at", "TEXT")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS incident_spawn_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            incident_id INTEGER NOT NULL,
            at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_incident_spawn_attempts_at "
        "ON incident_spawn_attempts (at)"
    )
    if "spawn_attempted_at" in existing:
        return
    attempted = "label IS NOT NULL OR result = 'spawn_failed'"
    conn.execute(f"UPDATE incidents SET spawn_attempted_at = opened_at WHERE {attempted}")
    conn.execute(
        "INSERT INTO incident_spawn_attempts (incident_id, at) "
        f"SELECT id, opened_at FROM incidents WHERE {attempted}"
    )


MIGRATIONS = (
    Migration(1, "tasks table", _create_tasks),
    Migration(2, "worktree, hold and approval columns", _add_m1_columns),
    Migration(3, "start commit column", _add_start_commit),
    Migration(4, "triage, routing and decision log", _add_m2),
    Migration(5, "message sources, drafts and incidents", _add_m3),
    Migration(6, "incident spawn attempt time", _add_spawn_attempts),
)
# The step that adds the triage columns and the decision log.
TRIAGE_STEP = next(m.step for m in MIGRATIONS if m.apply is _add_m2)
# The step that adds message sources, drafts and incidents.
MESSAGES_STEP = next(m.step for m in MIGRATIONS if m.apply is _add_m3)
# The step that records when each incident session was last tried.
SPAWN_ATTEMPT_STEP = next(m.step for m in MIGRATIONS if m.apply is _add_spawn_attempts)

_DRAFT_UPDATABLE = frozenset(
    {"text", "status", "updated_at", "approved_at", "approved_by", "delivered_at", "error"}
)
_INCIDENT_UPDATABLE = frozenset(
    {
        "state", "label", "session_name", "client", "model", "result", "repeats",
        "last_seen_at", "closed_at", "close_reason", "report_at", "spawn_attempted_at",
    }
)

_UPDATABLE = frozenset(
    {
        "project_id",
        "type",
        "handled_types",
        "repo",
        "label",
        "session_name",
        "last_spawned_at",
        "handled_at",
        "last_comment_handled_at",
        "last_notified_state",
        "outcome",
        *_M1_COLUMNS,
        "start_commit",
        *_M2_COLUMNS,
        *_M3_COLUMNS,
    }
)


def _migrate(conn: sqlite3.Connection) -> None:
    def version() -> int:
        return conn.execute("PRAGMA user_version").fetchone()[0]

    def pending(current: int) -> list[Migration]:
        return [m for m in MIGRATIONS if m.step > current]

    if not pending(version()):
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        steps = pending(version())
        if steps:
            for migration in steps:
                migration.apply(conn)
            conn.execute(f"PRAGMA user_version = {int(steps[-1].step)}")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _has_triage(conn: sqlite3.Connection) -> bool:
    """Whether the triage columns and the decision log exist yet. A read never
    migrates (dry runs and status must not write); a database no run has
    migrated cannot hold candidates or decisions."""
    return conn.execute("PRAGMA user_version").fetchone()[0] >= TRIAGE_STEP


def _version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _has_messages(conn: sqlite3.Connection) -> bool:
    """Whether the message-source tables exist yet; reads never migrate."""
    return _version(conn) >= MESSAGES_STEP


@dataclass(frozen=True)
class TaskRecord:
    source: str
    task_id: str
    title: str
    url: str
    first_seen_at: str
    handled_types: tuple[str, ...]
    project_id: str | None = None
    type: str | None = None
    repo: str | None = None
    label: str | None = None
    session_name: str | None = None
    last_spawned_at: str | None = None
    handled_at: str | None = None
    last_comment_handled_at: str | None = None
    last_notified_state: str | None = None
    outcome: str | None = None
    work_dir: str | None = None
    clone_path: str | None = None
    worktree_removed_at: str | None = None
    held_at: str | None = None
    approved_at: str | None = None
    start_commit: str | None = None
    client: str | None = None
    model: str | None = None
    candidate_type: str | None = None
    candidate_at: str | None = None
    decision: str | None = None
    decision_at: str | None = None
    decision_reason: str | None = None
    decision_repo: str | None = None
    decision_client: str | None = None
    decision_model: str | None = None
    decision_clone: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SourceState:
    source: str
    cursor: str | None = None
    last_run_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ItemRecord:
    source: str
    item_id: str
    result: str
    seen_at: str
    container: str | None = None
    rule: str | None = None
    action: str | None = None
    url: str | None = None
    alert: dict | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DraftRecord:
    id: int
    source: str
    kind: str
    origin: str
    text: str
    status: str
    created_at: str
    updated_at: str
    target: dict = field(default_factory=dict)
    item_id: str | None = None
    rule: str | None = None
    url: str | None = None
    incident_id: int | None = None
    approved_at: str | None = None
    approved_by: str | None = None
    delivered_at: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class IncidentRecord:
    id: int
    key: str
    source: str
    rule: str
    action: str
    state: str
    opened_at: str
    container: str | None = None
    item_id: str | None = None
    thread_ref: str | None = None
    url: str | None = None
    label: str | None = None
    session_name: str | None = None
    client: str | None = None
    model: str | None = None
    result: str | None = None
    repeats: int = 0
    last_seen_at: str | None = None
    closed_at: str | None = None
    close_reason: str | None = None
    report_at: str | None = None
    spawn_attempted_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _build(cls: type, row: sqlite3.Row, **overrides: Any) -> Any:
    # Columns a later build added must not break reading.
    data = {k: v for k, v in dict(row).items() if k in cls.__dataclass_fields__}
    data.update(overrides)
    return cls(**data)


def _json_object(value: str | None) -> dict | None:
    return json.loads(value) if value else None


def _item(row: sqlite3.Row) -> ItemRecord:
    return _build(ItemRecord, row, alert=_json_object(row["alert"]))


def _draft(row: sqlite3.Row) -> DraftRecord:
    return _build(DraftRecord, row, target=_json_object(row["target"]) or {})


def _record(row: sqlite3.Row) -> TaskRecord:
    # A database a later build migrated may carry columns this build does not
    # know; it must stay readable.
    data = {k: v for k, v in dict(row).items() if k in TaskRecord.__dataclass_fields__}
    data["handled_types"] = tuple(json.loads(data["handled_types"] or "[]"))
    return TaskRecord(**data)


class DispatchStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path if path is not None else settings.dispatch_db_path()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        # The DB holds task titles and links: create it (and the -wal/-shm
        # SQLite derives from it) owner-only, whatever the caller's umask.
        old_umask = os.umask(0o077)
        try:
            conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        finally:
            os.umask(old_umask)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=30000")
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError:
                pass
            conn.execute("PRAGMA synchronous=NORMAL")
            yield conn
        finally:
            conn.close()

    def init_schema(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as conn:
            _migrate(conn)

    # ------------------------------ reads ------------------------------

    def get(self, source: str, task_id: str) -> TaskRecord | None:
        if not self.path.exists():
            return None
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE source = ? AND task_id = ?",
                (source, task_id),
            ).fetchone()
        return _record(row) if row else None

    def list_tasks(self) -> list[TaskRecord]:
        if not self.path.exists():
            return []
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks ORDER BY first_seen_at, task_id"
            ).fetchall()
        return [_record(row) for row in rows]

    def by_label(self, label: str) -> list[TaskRecord]:
        if not self.path.exists():
            return []
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE label = ? ORDER BY first_seen_at, task_id",
                (label,),
            ).fetchall()
        return [_record(row) for row in rows]

    def pending_candidates(self) -> list[TaskRecord]:
        """Tasks handed to the dispatcher that still await its decision."""
        if not self.path.exists():
            return []
        with self.connect() as conn:
            if not _has_triage(conn):
                return []
            rows = conn.execute(
                "SELECT * FROM tasks WHERE candidate_at IS NOT NULL "
                "AND (decision IS NULL OR decision = 'ask') "
                "ORDER BY candidate_at, task_id"
            ).fetchall()
        return [_record(row) for row in rows]

    def list_events(self, *, event: str | None = None, limit: int | None = None) -> list[dict]:
        """Decision log rows, newest last; with `limit`, the newest `limit`."""
        if not self.path.exists():
            return []
        query = "SELECT * FROM decision_log"
        params: list[Any] = []
        if event is not None:
            query += " WHERE event = ?"
            params.append(event)
        query += " ORDER BY id DESC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(int(limit))
        with self.connect() as conn:
            if not _has_triage(conn):
                return []
            rows = conn.execute(query, params).fetchall()
        events = []
        for row in reversed(rows):
            item = dict(row)
            item["detail"] = json.loads(item["detail"] or "{}")
            events.append(item)
        return events

    def source_state(self, name: str) -> SourceState | None:
        row = self._read_one("SELECT * FROM source_state WHERE source = ?", (name,))
        return _build(SourceState, row) if row else None

    def has_item(self, source: str, item_id: str) -> bool:
        row = self._read_one(
            "SELECT 1 FROM message_items WHERE source = ? AND item_id = ?",
            (source, item_id),
        )
        return row is not None

    def pending_alerts(self) -> list[ItemRecord]:
        rows = self._read_all(
            "SELECT * FROM message_items WHERE alert IS NOT NULL ORDER BY seen_at, item_id"
        )
        return [_item(row) for row in rows]

    def get_draft(self, draft_id: int) -> DraftRecord | None:
        row = self._read_one("SELECT * FROM drafts WHERE id = ?", (draft_id,))
        return _draft(row) if row else None

    def list_drafts(self, status: str | None = None) -> list[DraftRecord]:
        if status is None:
            rows = self._read_all("SELECT * FROM drafts ORDER BY id")
        else:
            rows = self._read_all("SELECT * FROM drafts WHERE status = ? ORDER BY id", (status,))
        return [_draft(row) for row in rows]

    def get_incident(self, incident_id: int) -> IncidentRecord | None:
        row = self._read_one("SELECT * FROM incidents WHERE id = ?", (incident_id,))
        return _build(IncidentRecord, row) if row else None

    def open_incident(self, key: str) -> IncidentRecord | None:
        row = self._read_one(
            "SELECT * FROM incidents WHERE key = ? AND state = ?", (key, INCIDENT_OPEN)
        )
        return _build(IncidentRecord, row) if row else None

    def list_incidents(self, state: str | None = None) -> list[IncidentRecord]:
        if state is None:
            rows = self._read_all("SELECT * FROM incidents ORDER BY id")
        else:
            rows = self._read_all("SELECT * FROM incidents WHERE state = ? ORDER BY id", (state,))
        return [_build(IncidentRecord, row) for row in rows]

    def incident_spawns_since(self, stamp: str) -> int:
        """Incident session attempts (started or failed) at or after `stamp`."""
        row = self._read_one(
            "SELECT COUNT(*) AS n FROM incident_spawn_attempts WHERE at >= ?",
            (stamp,), step=SPAWN_ATTEMPT_STEP,
        )
        return int(row["n"]) if row else 0

    def unreported_incidents(self) -> list[IncidentRecord]:
        rows = self._read_all(
            "SELECT * FROM incidents WHERE label IS NOT NULL AND report_at IS NULL ORDER BY id"
        )
        return [_build(IncidentRecord, row) for row in rows]

    def _read_all(
        self, query: str, params: tuple = (), *, step: int = MESSAGES_STEP
    ) -> list[sqlite3.Row]:
        """Rows of a message-source table; empty on a database no run has
        migrated to `step` (a read never migrates)."""
        if not self.path.exists():
            return []
        with self.connect() as conn:
            if _version(conn) < step:
                return []
            return conn.execute(query, params).fetchall()

    def _read_one(
        self, query: str, params: tuple = (), *, step: int = MESSAGES_STEP
    ) -> sqlite3.Row | None:
        rows = self._read_all(query, params, step=step)
        return rows[0] if rows else None

    # ------------------------------ writes -----------------------------

    def observe(self, task: Task, *, project_id: str | None, now: str) -> TaskRecord:
        self.init_schema()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO tasks (source, task_id, title, url, project_id, first_seen_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (source, task_id) DO UPDATE SET
                    title = excluded.title,
                    url = excluded.url,
                    project_id = excluded.project_id
                """,
                (task.source, task.task_id, task.title, task.url, project_id, now),
            )
        record = self.get(task.source, task.task_id)
        assert record is not None
        return record

    def update(self, source: str, task_id: str, **fields: Any) -> TaskRecord:
        unknown = sorted(set(fields) - _UPDATABLE)
        if unknown:
            raise ValueError(f"cannot update column(s): {', '.join(unknown)}")
        if "handled_types" in fields:
            fields["handled_types"] = json.dumps(list(fields["handled_types"]))
        self.init_schema()
        if fields:
            assignments = ", ".join(f"{column} = ?" for column in fields)
            with self.connect() as conn:
                cursor = conn.execute(
                    f"UPDATE tasks SET {assignments} WHERE source = ? AND task_id = ?",
                    (*fields.values(), source, task_id),
                )
            if cursor.rowcount == 0:
                raise KeyError(f"unknown task {source}:{task_id}")
        record = self.get(source, task_id)
        if record is None:
            raise KeyError(f"unknown task {source}:{task_id}")
        return record

    def log_event(
        self,
        *,
        at: str,
        event: str,
        actor: str,
        source: str | None = None,
        task_id: str | None = None,
        detail: Mapping | None = None,
    ) -> None:
        self.init_schema()
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO decision_log (at, source, task_id, event, actor, detail) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (at, source, task_id, event, actor, json.dumps(dict(detail or {}), sort_keys=True)),
            )

    def save_source_state(
        self, name: str, *, now: str, last_run_at: str | None = None, cursor: Any = KEEP
    ) -> None:
        self.init_schema()
        sets = ["updated_at = excluded.updated_at"]
        if cursor is not KEEP:
            sets.append("cursor = excluded.cursor")
        if last_run_at is not None:
            sets.append("last_run_at = excluded.last_run_at")
        initial_cursor = None if cursor is KEEP else cursor
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO source_state (source, cursor, last_run_at, updated_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT (source) DO UPDATE SET " + ", ".join(sets),
                (name, initial_cursor, last_run_at, now),
            )

    def record_item(
        self,
        source: str,
        item_id: str,
        *,
        container: str | None,
        rule: str | None,
        action: str | None,
        result: str,
        url: str | None,
        now: str,
        alert: Mapping | None = None,
    ) -> None:
        self.init_schema()
        alert_json = json.dumps(dict(alert), sort_keys=True) if alert is not None else None
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO message_items "
                "(source, item_id, container, rule, action, result, url, seen_at, alert) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (source, item_id, container, rule, action, result, url, now, alert_json),
            )

    def prune_message_items(self, older_than: str) -> int:
        """Forget message items first seen before `older_than`; returns how many.

        An item a pending or sending draft, or an open incident, still points
        at is kept. Incident spawn attempts made before `older_than` go too:
        the hourly spawn limit only reads the last hour, and the retention is
        at least a day. A database no run has migrated to the message-source
        step has nothing to prune and is left as it is."""
        if not self.path.exists():
            return 0
        with self.connect() as conn:
            if not _has_messages(conn):
                return 0
            cursor = conn.execute(
                """
                DELETE FROM message_items
                WHERE seen_at < ?
                AND NOT EXISTS (
                    SELECT 1 FROM drafts d
                    WHERE d.source = message_items.source AND d.item_id = message_items.item_id
                    AND d.status IN (?, ?)
                )
                AND NOT EXISTS (
                    SELECT 1 FROM incidents i
                    WHERE i.source = message_items.source AND i.item_id = message_items.item_id
                    AND i.state = ?
                )
                """,
                (older_than, DRAFT_PENDING, DRAFT_SENDING, INCIDENT_OPEN),
            )
            pruned = int(cursor.rowcount)
            if _version(conn) >= SPAWN_ATTEMPT_STEP:
                conn.execute("DELETE FROM incident_spawn_attempts WHERE at < ?", (older_than,))
        return pruned

    def clear_alert(self, source: str, item_id: str) -> None:
        self.init_schema()
        with self.connect() as conn:
            conn.execute(
                "UPDATE message_items SET alert = NULL WHERE source = ? AND item_id = ?",
                (source, item_id),
            )

    def add_draft(
        self,
        *,
        source: str,
        item_id: str | None,
        kind: str,
        origin: str,
        rule: str | None,
        url: str | None,
        target: Mapping,
        text: str,
        now: str,
        incident_id: int | None = None,
    ) -> DraftRecord:
        self.init_schema()
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO drafts (source, item_id, kind, origin, rule, url, target, text, "
                "status, incident_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    source, item_id, kind, origin, rule, url,
                    json.dumps(dict(target), sort_keys=True), text, DRAFT_PENDING,
                    incident_id, now, now,
                ),
            )
            draft_id = int(cursor.lastrowid)
        draft = self.get_draft(draft_id)
        assert draft is not None
        return draft

    def update_draft(self, draft_id: int, **fields: Any) -> DraftRecord:
        self._update_row("drafts", "draft", _DRAFT_UPDATABLE, draft_id, fields)
        draft = self.get_draft(draft_id)
        assert draft is not None
        return draft

    def claim_draft(self, draft_id: int, *, from_status: str, to_status: str, now: str) -> bool:
        """Move a draft between statuses atomically; True for the one caller
        whose update changed the row."""
        self.init_schema()
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE drafts SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
                (to_status, now, draft_id, from_status),
            )
        return cursor.rowcount == 1

    def create_incident(
        self,
        *,
        key: str,
        source: str,
        container: str | None,
        rule: str,
        action: str,
        item_id: str | None,
        thread_ref: str | None,
        url: str | None,
        now: str,
    ) -> IncidentRecord | None:
        """Open an incident; None when one is already open for the key."""
        self.init_schema()
        with self.connect() as conn:
            try:
                cursor = conn.execute(
                    "INSERT INTO incidents (key, source, container, rule, action, state, "
                    "item_id, thread_ref, url, opened_at, last_seen_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (key, source, container, rule, action, INCIDENT_OPEN, item_id,
                     thread_ref, url, now, now),
                )
            except sqlite3.IntegrityError:
                return None
            incident_id = int(cursor.lastrowid)
        return self.get_incident(incident_id)

    def record_spawn_attempt(self, incident_id: int, *, now: str) -> IncidentRecord:
        """Count one session attempt for the incident toward the hourly limit."""
        self.init_schema()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = conn.execute(
                    "UPDATE incidents SET spawn_attempted_at = ? WHERE id = ?",
                    (now, incident_id),
                )
                if cursor.rowcount != 1:
                    raise KeyError(f"no incident {incident_id}")
                conn.execute(
                    "INSERT INTO incident_spawn_attempts (incident_id, at) VALUES (?, ?)",
                    (incident_id, now),
                )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        incident = self.get_incident(incident_id)
        assert incident is not None
        return incident

    def update_incident(self, incident_id: int, **fields: Any) -> IncidentRecord:
        self._update_row("incidents", "incident", _INCIDENT_UPDATABLE, incident_id, fields)
        incident = self.get_incident(incident_id)
        assert incident is not None
        return incident

    def _update_row(
        self, table: str, noun: str, allowed: frozenset, row_id: int, fields: dict
    ) -> None:
        unknown = sorted(set(fields) - allowed)
        if unknown:
            raise ValueError(f"cannot update column(s): {', '.join(unknown)}")
        self.init_schema()
        if not fields:
            exists = self._read_one(f"SELECT 1 FROM {table} WHERE id = ?", (row_id,))
            if exists is None:
                raise KeyError(f"unknown {noun} {row_id}")
            return
        assignments = ", ".join(f"{column} = ?" for column in fields)
        with self.connect() as conn:
            cursor = conn.execute(
                f"UPDATE {table} SET {assignments} WHERE id = ?", (*fields.values(), row_id)
            )
        if cursor.rowcount == 0:
            raise KeyError(f"unknown {noun} {row_id}")

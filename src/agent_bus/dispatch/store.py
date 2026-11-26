"""Task-state store: dispatch.db beside the bus DB, with its own migrations."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
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


MIGRATIONS = (
    Migration(1, "tasks table", _create_tasks),
    Migration(2, "worktree, hold and approval columns", _add_m1_columns),
    Migration(3, "start commit column", _add_start_commit),
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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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

    # ------------------------------ writes -----------------------------

    def observe(self, task: Task, *, project_id: str, now: str) -> TaskRecord:
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

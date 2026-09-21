"""Schema migrations: versioning, forward-only steps, backup, guards."""

from __future__ import annotations

import multiprocessing as mp
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from agent_bus import migrations

NEW_AGENT_COLUMNS = {"group_name", "client"}
NEW_MESSAGE_COLUMNS = {"addressed_to", "fanout_id", "to_group", "claimed_by"}


def _columns(db: Path, table: str) -> set[str]:
    with sqlite3.connect(db) as conn:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _user_version(db: Path) -> int:
    with sqlite3.connect(db) as conn:
        return conn.execute("PRAGMA user_version").fetchone()[0]


def _set_user_version(db: Path, version: int) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute(f"PRAGMA user_version = {int(version)}")


def _backups(db: Path) -> list[Path]:
    return sorted(db.parent.glob(db.name + ".pre-migrate-*"))


def test_fresh_db_lands_on_target_version(bus_paths, storage):
    assert _user_version(bus_paths["db"]) == migrations.target_version()
    assert NEW_AGENT_COLUMNS <= _columns(bus_paths["db"], "agents")
    assert NEW_MESSAGE_COLUMNS <= _columns(bus_paths["db"], "messages")


def test_fresh_db_is_not_backed_up(bus_paths, storage):
    assert _backups(bus_paths["db"]) == []


def test_legacy_db_is_migrated_in_place(legacy_db):
    from agent_bus.storage import Storage

    Storage().init_schema()

    assert _user_version(legacy_db) == migrations.target_version()
    assert NEW_AGENT_COLUMNS <= _columns(legacy_db, "agents")
    assert NEW_MESSAGE_COLUMNS <= _columns(legacy_db, "messages")
    with sqlite3.connect(legacy_db) as conn:
        agents = conn.execute(
            "SELECT name, group_name, client FROM agents ORDER BY name"
        ).fetchall()
        unread = conn.execute(
            "SELECT message_id, addressed_to, fanout_id, to_group, claimed_by "
            "FROM messages WHERE read_at IS NULL"
        ).fetchall()
    # additive only: nothing is backfilled, NULL means "legacy"
    assert agents == [
        ("human", None, None), ("repo-a", None, None), ("repo-b", None, None),
    ]
    assert unread == [("m-unread", None, None, None, None)]


def test_legacy_db_is_backed_up_once_before_migrating(legacy_db):
    from agent_bus.storage import Storage

    Storage().init_schema()
    Storage().init_schema()  # a second process start must not back up again

    (backup,) = _backups(legacy_db)
    assert backup.name == legacy_db.name + ".pre-migrate-0"
    assert _user_version(backup) == 0
    assert _columns(backup, "agents") == {
        "name", "repo_path", "registered_at", "last_seen",
    }
    with sqlite3.connect(backup) as conn:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2


def test_migration_tolerates_columns_that_already_exist(legacy_db):
    """A database someone half-migrated by hand must not wedge startup."""
    from agent_bus.storage import Storage

    with sqlite3.connect(legacy_db) as conn:
        conn.execute("ALTER TABLE agents ADD COLUMN client TEXT")

    Storage().init_schema()
    assert _user_version(legacy_db) == migrations.target_version()
    assert NEW_AGENT_COLUMNS <= _columns(legacy_db, "agents")


def test_newer_step_in_same_epoch_is_tolerated(bus_paths, storage):
    """Steps are additive, so an older binary keeps working on them."""
    from agent_bus.storage import Storage

    newer = migrations.target_version() + 5
    _set_user_version(bus_paths["db"], newer)

    store = Storage()
    store.upsert_agent("alpha", "/repo/alpha")
    assert [a.name for a in store.list_agents()] == ["alpha"]
    assert _user_version(bus_paths["db"]) == newer


def test_newer_epoch_is_refused(bus_paths, storage):
    from agent_bus.storage import Storage

    _set_user_version(
        bus_paths["db"], (migrations.SCHEMA_EPOCH + 1) * migrations.EPOCH_SIZE
    )
    with pytest.raises(migrations.SchemaTooNewError) as exc_info:
        Storage().init_schema()
    assert "upgrade agent-bus" in str(exc_info.value)


def test_cli_reports_newer_epoch_without_a_traceback(bus_paths, storage):
    _set_user_version(
        bus_paths["db"], (migrations.SCHEMA_EPOCH + 1) * migrations.EPOCH_SIZE
    )
    env = os.environ.copy()
    env["AGENT_BUS_DB"] = str(bus_paths["db"])
    env["AGENT_BUS_AUDIT_LOG"] = str(bus_paths["log"])
    r = subprocess.run(
        [sys.executable, "-m", "agent_bus.cli", "agents"],
        env=env, capture_output=True, text=True, timeout=15,
    )
    assert r.returncode == 3
    assert "upgrade agent-bus" in r.stderr
    assert "Traceback" not in r.stderr


# --------- concurrent starters -------------------------------------------


def _migrate_in_subprocess(db_path: str, audit_log: str) -> None:
    os.environ["AGENT_BUS_DB"] = db_path
    os.environ["AGENT_BUS_AUDIT_LOG"] = audit_log
    from agent_bus.storage import Storage

    store = Storage()
    store.init_schema()
    store.upsert_agent(f"starter-{os.getpid()}", "/repo/starter")


def test_concurrent_starters_migrate_exactly_once(legacy_db, bus_paths):
    """After an upgrade every client restarts at once; all of them race
    to migrate the same 0.4.x database."""
    ctx = mp.get_context("spawn")
    procs = [
        ctx.Process(
            target=_migrate_in_subprocess,
            args=(str(legacy_db), str(bus_paths["log"])),
        )
        for _ in range(4)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)
    assert [p.exitcode for p in procs] == [0, 0, 0, 0]

    assert _user_version(legacy_db) == migrations.target_version()
    assert len(_backups(legacy_db)) == 1
    with sqlite3.connect(legacy_db) as conn:
        starters = conn.execute(
            "SELECT COUNT(*) FROM agents WHERE name LIKE 'starter-%'"
        ).fetchone()[0]
    assert starters == 4


def _open_in_subprocess(db_path: str, audit_log: str) -> None:
    os.environ["AGENT_BUS_DB"] = db_path
    os.environ["AGENT_BUS_AUDIT_LOG"] = audit_log
    from agent_bus.storage import Storage

    store = Storage()
    for i in range(20):
        store.upsert_agent(f"agent-{os.getpid()}-{i}", "/repo")


def test_many_clients_opening_a_pre_wal_database_at_once(legacy_db, bus_paths):
    """Converting a database to WAL takes an exclusive lock. Every client
    restarting at once after an upgrade is exactly that moment, and it used
    to fail outright with 'database is locked'."""
    ctx = mp.get_context("spawn")
    procs = [
        ctx.Process(target=_open_in_subprocess,
                    args=(str(legacy_db), str(bus_paths["log"])))
        for _ in range(6)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
    assert [p.exitcode for p in procs] == [0] * 6

    with sqlite3.connect(legacy_db) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        written = conn.execute(
            "SELECT COUNT(*) FROM agents WHERE name LIKE 'agent-%'"
        ).fetchone()[0]
    assert written == 6 * 20

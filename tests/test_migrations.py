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


# ----------------------- step 5: state, repos, spawns -----------------------

STEP_5_AGENT_COLUMNS = {"state", "state_at"}
STEP_5_TABLES = {"repos", "spawns"}


def _tables(db: Path) -> set[str]:
    with sqlite3.connect(db) as conn:
        return {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }


def test_target_version_is_step_seven():
    assert migrations.target_version() == 1008


def test_fresh_db_has_step_five_objects(bus_paths, storage):
    assert _user_version(bus_paths["db"]) == migrations.target_version()
    assert STEP_5_AGENT_COLUMNS <= _columns(bus_paths["db"], "agents")
    assert STEP_5_TABLES <= _tables(bus_paths["db"])


def test_legacy_db_gets_step_five_objects(legacy_db):
    from agent_bus.storage import Storage

    Storage().init_schema()

    assert _user_version(legacy_db) == migrations.target_version()
    assert STEP_5_AGENT_COLUMNS <= _columns(legacy_db, "agents")
    assert STEP_5_TABLES <= _tables(legacy_db)


def _step_four_db(db: Path) -> None:
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        for migration in migrations.MIGRATIONS[:4]:
            migration.apply(conn)
        conn.execute(
            "INSERT INTO agents (name, repo_path, registered_at, last_seen, "
            "group_name, client) VALUES "
            "('repo-a/claude', '/code/repo-a', 't', 't', 'repo-a', 'claude'), "
            "('repo-a/claude-docs', '/code/repo-a', 't', 't', 'repo-a', NULL), "
            "('solo', '/code/solo', 't', 't', NULL, NULL)"
        )
        conn.execute("PRAGMA user_version = 1004")
    finally:
        conn.close()


def test_step_four_db_migrates_without_renaming_anything(bus_paths):
    from agent_bus.storage import Storage

    db = bus_paths["db"]
    _step_four_db(db)
    with sqlite3.connect(db) as conn:
        before = conn.execute(
            "SELECT name, group_name FROM agents ORDER BY name"
        ).fetchall()

    Storage().init_schema()

    assert _user_version(db) == migrations.target_version()
    assert STEP_5_AGENT_COLUMNS <= _columns(db, "agents")
    assert STEP_5_TABLES <= _tables(db)
    with sqlite3.connect(db) as conn:
        after = conn.execute(
            "SELECT name, group_name FROM agents ORDER BY name"
        ).fetchall()
        states = conn.execute("SELECT state, state_at FROM agents").fetchall()
    assert after == before
    assert set(states) == {(None, None)}
    assert [b.name for b in _backups(db)] == [db.name + ".pre-migrate-1004"]


def test_pre_step_five_statements_still_work(bus_paths):
    from agent_bus.storage import Storage

    Storage().init_schema()
    with sqlite3.connect(bus_paths["db"]) as conn:
        conn.execute(
            "INSERT INTO agents (name, repo_path, registered_at, last_seen, "
            "group_name, client, topic) "
            "VALUES ('x/claude', '/code/x', 't', 't', 'x', 'claude', 'hello')"
        )
        row = conn.execute(
            "SELECT name, repo_path, registered_at, last_seen, group_name, "
            "client, topic FROM agents WHERE name = 'x/claude'"
        ).fetchone()
    assert row == ("x/claude", "/code/x", "t", "t", "x", "claude", "hello")


def test_repos_table_has_no_unused_index(bus_paths, storage):
    with sqlite3.connect(bus_paths["db"]) as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert "idx_repos_id" not in names


def test_step_five_db_gains_the_links_column(bus_paths):
    from agent_bus.storage import Storage

    db = bus_paths["db"]
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        for migration in migrations.MIGRATIONS[:5]:
            migration.apply(conn)
        conn.execute(
            "INSERT INTO agents (name, repo_path, registered_at, last_seen) "
            "VALUES ('solo', '/code/solo', 't', 't')"
        )
        conn.execute("PRAGMA user_version = 1005")
    finally:
        conn.close()
    assert "links" not in _columns(db, "agents")

    Storage().init_schema()

    assert _user_version(db) == migrations.target_version()
    assert "links" in _columns(db, "agents")
    assert Storage().get_agent("solo").link_list == []


def test_the_links_step_is_idempotent(bus_paths, storage):
    conn = sqlite3.connect(bus_paths["db"], isolation_level=None)
    try:
        migrations.MIGRATIONS[5].apply(conn)
    finally:
        conn.close()
    assert "links" in _columns(bus_paths["db"], "agents")


def _step_six_db(db: Path) -> None:
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        for migration in migrations.MIGRATIONS[:6]:
            migration.apply(conn)
        conn.execute(
            "INSERT INTO agents (name, repo_path, registered_at, last_seen) "
            "VALUES ('example-org/claude-a', '/code/a', 't', 't')"
        )
        conn.execute(
            "INSERT INTO messages (message_id, from_agent, to_agent, body, "
            "thread_id, sent_at) VALUES ('m1', 'x', 'example-org/claude-a', "
            "'hi', 't1', '2030-01-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO spawns (name, client, repo_id, repo_path, work_dir, "
            "model, prompt_file, tmux_session, tmux_window, remote, spawned_at) "
            "VALUES ('example-org/claude-a', 'claude', 'example-org/repo-a', "
            "'/code/a', '/code/a', NULL, '/p', 's', 'w', 0, '2030-01-01T00:00:00Z')"
        )
        conn.execute("PRAGMA user_version = 1006")
    finally:
        conn.close()


def test_step_six_db_upgrades_to_authority_columns(bus_paths):
    from agent_bus.storage import Storage

    db = bus_paths["db"]
    _step_six_db(db)
    assert "authority" not in _columns(db, "messages")

    storage = Storage()
    storage.init_schema()

    assert _user_version(db) == 1008
    assert "authority" in _columns(db, "messages")
    assert "role" in _columns(db, "spawns")
    assert "authorities" in _tables(db)
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT body, authority FROM messages WHERE message_id = 'm1'"
        ).fetchone() == ("hi", None)
    record = storage.get_spawn("example-org/claude-a")
    assert record is not None and record.role is None


def test_pre_step_seven_statements_still_work(bus_paths, storage):
    with sqlite3.connect(bus_paths["db"]) as conn:
        conn.execute(
            "INSERT INTO agents (name, repo_path, registered_at, last_seen) "
            "VALUES ('example-org/claude-b', '/code/b', 't', 't')"
        )
        conn.execute(
            "INSERT INTO messages (message_id, from_agent, to_agent, body, "
            "thread_id, sent_at, addressed_to, fanout_id, to_group) "
            "VALUES ('m2', 'x', 'example-org/claude-b', 'yo', 't2', "
            "'2030-01-01T00:00:00Z', NULL, NULL, NULL)"
        )
        conn.execute(
            "INSERT INTO spawns (name, client, repo_id, repo_path, work_dir, "
            "model, prompt_file, tmux_session, tmux_window, remote, spawned_at) "
            "VALUES ('example-org/claude-b', 'claude', NULL, '/code/b', "
            "'/code/b', NULL, '/p', 's', 'w', 1, '2030-01-01T00:00:00Z')"
        )
    msgs = storage.read_inbox(agent="example-org/claude-b", mark_read=False)
    assert [(m.body, m.authority) for m in msgs] == [("yo", None)]
    assert storage.get_spawn("example-org/claude-b").role is None

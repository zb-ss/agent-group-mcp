"""dispatch.db store, task model helpers."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agent_bus.dispatch import model, settings
from agent_bus.dispatch.store import MIGRATIONS, DispatchStore
from agent_bus import migrations

NOW = "2030-01-07T10:15:00.000000Z"
LATER = "2030-01-08T10:15:00.000000Z"


@pytest.fixture
def bus_db(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "bus.db"
    monkeypatch.setenv("AGENT_BUS_DB", str(path))
    return path


def _task(title: str = "Fix login") -> model.Task:
    return model.Task(
        source="asana",
        task_id="1000000000000001",
        title=title,
        url="https://example.com/task/1",
        due_on="2030-01-07",
        modified_at=None,
        newest_comment_at=None,
    )


def test_path_beside_bus_db(bus_db):
    assert DispatchStore().path == bus_db.with_name("dispatch.db")
    assert settings.dispatch_db_path() == bus_db.with_name("dispatch.db")


def test_reads_on_missing_file_create_nothing(bus_db):
    store = DispatchStore()
    assert store.get("asana", "1") is None
    assert store.list_tasks() == []
    assert store.by_label("x") == []
    assert not store.path.exists()
    assert not bus_db.exists()


def test_observe_inserts_once_and_refreshes(bus_db):
    store = DispatchStore()
    first = store.observe(_task(), project_id="p1", now=NOW)
    second = store.observe(_task("Fix logout"), project_id="p2", now=LATER)
    assert first.first_seen_at == NOW
    assert second.first_seen_at == NOW
    assert (second.title, second.project_id) == ("Fix logout", "p2")
    assert second.handled_types == ()
    assert len(store.list_tasks()) == 1


def test_update_round_trips_every_column(bus_db):
    store = DispatchStore()
    store.observe(_task(), project_id="p1", now=NOW)
    values = {
        "project_id": "p9",
        "type": "refinement",
        "handled_types": ("refinement", "follow-up"),
        "repo": "repo-a",
        "label": "t0001-fix-login",
        "session_name": "repo-a/claude-1",
        "last_spawned_at": NOW,
        "handled_at": NOW,
        "last_comment_handled_at": LATER,
        "last_notified_state": "waiting",
        "outcome": model.OUTCOME_SPAWNED,
    }
    record = store.update("asana", "1000000000000001", **values)
    for key, value in values.items():
        assert getattr(record, key) == value
    assert store.by_label("t0001-fix-login") == [record]
    assert record.to_dict()["outcome"] == "spawned"
    store.update("asana", "1000000000000001", handled_types=["a"])
    assert store.get("asana", "1000000000000001").handled_types == ("a",)


def test_update_rejects_unknown_column(bus_db):
    store = DispatchStore()
    store.observe(_task(), project_id="p1", now=NOW)
    with pytest.raises(ValueError):
        store.update("asana", "1000000000000001", title="x")
    with pytest.raises(ValueError):
        store.update("asana", "1000000000000001", **{"outcome = 'x' --": 1})


def test_update_unknown_task(bus_db):
    with pytest.raises(KeyError):
        DispatchStore().update("asana", "nope", outcome="done")


def test_user_version_and_idempotent_init(bus_db):
    store = DispatchStore()
    store.init_schema()
    store.init_schema()
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    assert not bus_db.exists()


def test_bus_migrations_untouched():
    assert MIGRATIONS not in (migrations.MIGRATIONS,)
    assert all(m.description != "tasks table" for m in migrations.MIGRATIONS)


def test_to_timestamp_normalises():
    expected = "2030-01-07T10:15:00.000000Z"
    assert model.to_timestamp("2030-01-07T10:15:00.000Z") == expected
    assert model.to_timestamp("2030-01-07T12:15:00+02:00") == expected
    assert model.to_timestamp(None) is None
    assert model.to_timestamp("garbage") is None


def test_clean_text():
    assert model.clean_text("a\nb\x1b[0m\t  c", 50) == "a b [0m c"
    assert model.clean_text("abcdefghij", 5) == "abcde..."
    assert model.format_now(__import__("datetime").datetime(2030, 1, 7, 10, 15)) == NOW


def test_db_files_are_owner_only_under_a_permissive_umask(tmp_path):
    import os
    import stat

    old = os.umask(0o022)
    try:
        store = DispatchStore(tmp_path / "state" / "dispatch.db")
        store.observe(_task(), project_id="1000000000000001", now="2030-01-01T00:00:00.000000Z")
        files = [p for p in (tmp_path / "state").iterdir()]
    finally:
        os.umask(old)
    assert any(p.name == "dispatch.db" for p in files)
    for path in files:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path.name
    assert stat.S_IMODE((tmp_path / "state").stat().st_mode) == 0o700

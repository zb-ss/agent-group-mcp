"""Pytest fixtures: every test gets a private DB + audit log via env vars."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _never_the_real_bus(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Point every test at a throwaway database and audit log, including
    the ones that do not ask for `bus_paths`. A test that reaches the
    default paths opens the user's real bus — and runs any pending schema
    migration on it."""
    root = tmp_path_factory.mktemp("bus")
    monkeypatch.setenv("AGENT_BUS_DB", str(root / "bus.db"))
    monkeypatch.setenv("AGENT_BUS_AUDIT_LOG", str(root / "audit.log"))


@pytest.fixture
def bus_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    db = tmp_path / "bus.db"
    log = tmp_path / "audit.log"
    monkeypatch.setenv("AGENT_BUS_DB", str(db))
    monkeypatch.setenv("AGENT_BUS_AUDIT_LOG", str(log))
    return {"db": db, "log": log, "root": tmp_path}


@pytest.fixture
def storage(bus_paths):
    from agent_bus.storage import Storage

    s = Storage()
    s.init_schema()
    return s


# Frozen copy of the 0.4.x schema. Pinned here (not imported) so the
# fixture keeps describing an OLD database even after storage.py moves on.
LEGACY_SCHEMA_SQL = """
CREATE TABLE agents (
    name           TEXT PRIMARY KEY,
    repo_path      TEXT NOT NULL,
    registered_at  TEXT NOT NULL,
    last_seen      TEXT NOT NULL
);
CREATE TABLE messages (
    message_id     TEXT PRIMARY KEY,
    from_agent     TEXT NOT NULL,
    to_agent       TEXT NOT NULL,
    body           TEXT NOT NULL,
    thread_id      TEXT NOT NULL,
    sent_at        TEXT NOT NULL,
    read_at        TEXT,
    delivered_at   TEXT
);
CREATE INDEX idx_messages_inbox ON messages (to_agent, read_at);
CREATE INDEX idx_messages_thread ON messages (thread_id, sent_at);
"""

LEGACY_TS = "2026-01-01T00:00:00.000000Z"


@pytest.fixture
def legacy_db(bus_paths) -> Path:
    """A 0.4.x-format database: repo-slug agent names, one read and one
    unread message, `user_version` still 0."""
    conn = sqlite3.connect(bus_paths["db"])
    try:
        conn.executescript(LEGACY_SCHEMA_SQL)
        conn.executemany(
            "INSERT INTO agents VALUES (?, ?, ?, ?)",
            [
                ("repo-a", "/code/repo-a", LEGACY_TS, LEGACY_TS),
                ("repo-b", "/code/repo-b", LEGACY_TS, LEGACY_TS),
                ("human", "/code/repo-a", LEGACY_TS, LEGACY_TS),
            ],
        )
        conn.executemany(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("m-read", "repo-b", "repo-a", "old and read", "t-1",
                 LEGACY_TS, LEGACY_TS, LEGACY_TS),
                ("m-unread", "repo-b", "repo-a", "old and unread", "t-1",
                 LEGACY_TS, None, None),
            ],
        )
        conn.commit()
    finally:
        conn.close()
    return bus_paths["db"]


@pytest.fixture
def two_agents(storage):
    storage.upsert_agent("alpha", "/repo/alpha")
    storage.upsert_agent("beta", "/repo/beta")
    return storage


@pytest.fixture
def three_agents(storage):
    storage.upsert_agent("alpha", "/repo/alpha")
    storage.upsert_agent("beta", "/repo/beta")
    storage.upsert_agent("gamma", "/repo/gamma")
    return storage


class FakeProcesses:
    """Stand-in processes, so one test process can play several sessions.

    A session is a client process that started an MCP server. `session()`
    makes both; `kill()` ends one. Real processes are still looked up for
    real, so a server built without stand-ins keeps working alongside."""

    def __init__(self, alive: set) -> None:
        import itertools

        self._alive = alive
        self._pids = itertools.count(900_001)

    def proc(self, name: str = "client"):
        from agent_bus.procs import Proc

        pid = next(self._pids)
        p = Proc(pid, f"fake-{pid}", name)
        self._alive.add(p)
        return p

    def session(self):
        """(server process, its ancestry): a fresh client and its server."""
        client = self.proc()
        return self.proc(), [client]

    def kill(self, *procs) -> None:
        for p in procs:
            self._alive.discard(p)


@pytest.fixture
def fake_procs(monkeypatch: pytest.MonkeyPatch) -> FakeProcesses:
    from agent_bus import procs

    alive: set = set()
    real_gone = procs.gone

    def gone(candidates):
        fakes = {p for p in candidates if p.started.startswith("fake-")}
        real = [p for p in candidates if p not in fakes]
        return real_gone(real) | (fakes - alive)

    monkeypatch.setattr(procs, "gone", gone)
    return FakeProcesses(alive)

"""Pytest fixtures: every test gets a private DB + audit log via env vars."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest


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

"""Pytest fixtures: every test gets a private DB + audit log via env vars."""

from __future__ import annotations

import os
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

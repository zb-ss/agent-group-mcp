"""Resolve filesystem paths for the bus DB and audit log.

Both can be overridden via env (AGENT_BUS_DB, AGENT_BUS_AUDIT_LOG) so tests
and the live install never collide.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import DEFAULT_AUDIT_PATH, DEFAULT_DB_PATH


def db_path() -> Path:
    raw = os.environ.get("AGENT_BUS_DB") or DEFAULT_DB_PATH
    return Path(raw).expanduser()


def audit_path() -> Path:
    raw = os.environ.get("AGENT_BUS_AUDIT_LOG") or DEFAULT_AUDIT_PATH
    return Path(raw).expanduser()


def ensure_parents(*paths: Path) -> None:
    for p in paths:
        p.parent.mkdir(parents=True, exist_ok=True)

"""Append-only JSON-lines audit log.

The audit log is the source of truth for recovery. Bodies live in SQLite —
the log keeps a 200-char preview plus a full sha256 hash so it stays
grep-friendly while remaining tamper-evident.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Literal

from .paths import audit_path, ensure_parents

AuditOp = Literal["send", "read", "deliver"]

PREVIEW_LIMIT = 200


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def body_sha256(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def body_preview(body: str) -> str:
    return body if len(body) <= PREVIEW_LIMIT else body[:PREVIEW_LIMIT]


def append(
    op: AuditOp,
    *,
    actor: str,
    message_id: str,
    from_agent: str,
    to_agent: str,
    thread_id: str | None,
    body: str,
    ts: str | None = None,
    log_path: Path | None = None,
) -> dict:
    """Append one audit row. Returns the row dict (useful for tests).

    Writes are O_APPEND so multiple processes can write concurrently
    without locking on POSIX.
    """
    row = {
        "ts": ts or _utc_now_iso(),
        "op": op,
        "actor": actor,
        "message_id": message_id,
        "from": from_agent,
        "to": to_agent,
        "thread_id": thread_id,
        "body_preview": body_preview(body),
        "body_sha256": body_sha256(body),
    }
    target = log_path or audit_path()
    ensure_parents(target)
    line = json.dumps(row, ensure_ascii=False) + "\n"
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)
    return row


def tail(limit: int = 50, *, log_path: Path | None = None) -> list[dict]:
    """Return up to `limit` most-recent audit rows, oldest first.

    Skips malformed lines silently so a partial write can't poison reads.
    """
    target = log_path or audit_path()
    if not target.exists():
        return []
    with target.open("r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    out: list[dict] = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def iter_all(*, log_path: Path | None = None) -> Iterator[dict]:
    target = log_path or audit_path()
    if not target.exists():
        return iter([])
    def _gen() -> Iterator[dict]:
        with target.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    return _gen()


def append_many(rows: Iterable[dict], *, log_path: Path | None = None) -> None:
    """Atomically append a batch of pre-built rows. Used for fan-out sends."""
    target = log_path or audit_path()
    ensure_parents(target)
    payload = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    if not payload:
        return
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, payload.encode("utf-8"))
    finally:
        os.close(fd)

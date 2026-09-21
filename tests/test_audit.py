"""Audit log writes."""

from __future__ import annotations

import json
import os

from agent_bus import audit


def test_a_short_write_does_not_truncate_the_log(bus_paths, monkeypatch):
    """os.write may write fewer bytes than asked (a full disk, a signal).
    Ignoring that would leave half a JSON line, which readers skip — the
    row would vanish without anyone noticing."""
    real_write = os.write
    calls = []

    def stingy_write(fd: int, data: bytes) -> int:
        calls.append(len(data))
        return real_write(fd, data[:7])  # never more than 7 bytes at a time

    rows = [{"op": "send", "message_id": f"m{i}", "body_preview": "x" * 40} for i in range(3)]
    # scoped: undoing the whole fixture would also drop the temp log path
    with monkeypatch.context() as patched:
        patched.setattr(os, "write", stingy_write)
        audit.append_many(rows)

    assert len(calls) > 3  # it kept going until everything was out
    lines = bus_paths["log"].read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["message_id"] for line in lines] == ["m0", "m1", "m2"]


def test_single_append_survives_a_short_write_too(bus_paths, monkeypatch):
    real_write = os.write
    with monkeypatch.context() as patched:
        patched.setattr(os, "write", lambda fd, data: real_write(fd, data[:5]))
        audit.append("send", actor="a", message_id="m1", from_agent="a", to_agent="b",
                     thread_id="t", body="hello")
    (row,) = audit.tail()
    assert row["message_id"] == "m1"

"""Storage layer: round-trips, broadcasts, audit hashing, threads, concurrency."""

from __future__ import annotations

import hashlib
import json
import multiprocessing as mp
import os
import sqlite3
from pathlib import Path


def test_upsert_agent_then_list(storage):
    a = storage.upsert_agent("alpha", "/repo/a")
    storage.upsert_agent("beta", "/repo/b")
    assert a.name == "alpha"
    assert a.repo_path == "/repo/a"

    agents = [r.name for r in storage.list_agents()]
    assert agents == ["alpha", "beta"]


def test_upsert_is_idempotent_and_updates_repo(storage):
    storage.upsert_agent("alpha", "/repo/old")
    again = storage.upsert_agent("alpha", "/repo/new")
    assert again.repo_path == "/repo/new"
    assert len(storage.list_agents()) == 1


def test_send_read_round_trip(two_agents, bus_paths):
    result = two_agents.send_message(
        from_agent="alpha", to="beta", body="hello beta"
    )
    assert "message_id" in result
    assert result["recipients"] == ["beta"]

    inbox = two_agents.read_inbox(agent="beta")
    assert len(inbox) == 1
    m = inbox[0]
    assert m.from_agent == "alpha"
    assert m.to_agent == "beta"
    assert m.body == "hello beta"
    assert m.read_at is not None

    # second read returns empty (already marked)
    assert two_agents.read_inbox(agent="beta") == []


def test_peek_does_not_mark_read(two_agents):
    two_agents.send_message(from_agent="alpha", to="beta", body="peek me")
    peek = two_agents.read_inbox(agent="beta", mark_read=False)
    assert len(peek) == 1
    # second read still sees it
    assert len(two_agents.read_inbox(agent="beta")) == 1


def test_broadcast_hits_all_peers_not_sender(three_agents):
    result = three_agents.send_message(
        from_agent="alpha", to="*", body="broadcast hi"
    )
    assert "message_ids" in result
    assert sorted(result["recipients"]) == ["beta", "gamma"]
    assert len(result["message_ids"]) == 2

    # message_ids must be unique
    assert len(set(result["message_ids"])) == 2

    beta_inbox = three_agents.read_inbox(agent="beta")
    gamma_inbox = three_agents.read_inbox(agent="gamma")
    alpha_inbox = three_agents.read_inbox(agent="alpha")
    assert [m.body for m in beta_inbox] == ["broadcast hi"]
    assert [m.body for m in gamma_inbox] == ["broadcast hi"]
    assert alpha_inbox == []


def test_broadcast_with_no_peers_is_noop(storage):
    storage.upsert_agent("solo", "/repo/solo")
    result = storage.send_message(from_agent="solo", to="*", body="anyone?")
    assert result["recipients"] == []
    assert result["message_ids"] == []


def test_thread_id_propagates_and_orders(two_agents):
    r1 = two_agents.send_message(from_agent="alpha", to="beta", body="first")
    thread = r1["thread_id"]
    two_agents.send_message(
        from_agent="beta", to="alpha", body="reply", thread_id=thread
    )
    two_agents.send_message(
        from_agent="alpha", to="beta", body="more", thread_id=thread
    )

    msgs = two_agents.read_thread(thread_id=thread)
    bodies = [m.body for m in msgs]
    assert bodies == ["first", "reply", "more"]


def test_audit_log_one_line_per_op_with_valid_json(two_agents, bus_paths):
    two_agents.send_message(from_agent="alpha", to="beta", body="payload-A")
    two_agents.read_inbox(agent="beta")

    raw_lines = bus_paths["log"].read_text(encoding="utf-8").splitlines()
    rows = [json.loads(line) for line in raw_lines if line.strip()]
    # one send + one read
    assert [r["op"] for r in rows] == ["send", "read"]
    for r in rows:
        assert set(r.keys()) >= {
            "ts", "op", "actor", "message_id",
            "from", "to", "thread_id", "body_preview", "body_sha256",
        }


def test_audit_log_sha256_matches_body(two_agents, bus_paths):
    body = "the brown fox " * 30
    two_agents.send_message(from_agent="alpha", to="beta", body=body)
    raw = bus_paths["log"].read_text(encoding="utf-8").splitlines()
    row = json.loads(raw[0])
    expected = hashlib.sha256(body.encode("utf-8")).hexdigest()
    assert row["body_sha256"] == expected
    assert row["body_preview"] == body[:200]


def test_audit_preview_truncated_to_200(two_agents, bus_paths):
    body = "x" * 500
    two_agents.send_message(from_agent="alpha", to="beta", body=body)
    raw = bus_paths["log"].read_text(encoding="utf-8").splitlines()
    row = json.loads(raw[0])
    assert len(row["body_preview"]) == 200


def test_list_agents_with_counts(two_agents):
    two_agents.send_message(from_agent="alpha", to="beta", body="m1")
    two_agents.send_message(from_agent="alpha", to="beta", body="m2")
    rows = dict((a.name, c) for a, c in two_agents.list_agents_with_counts())
    assert rows == {"alpha": 0, "beta": 2}


def test_forget_agent_removes_from_roster(two_agents):
    assert two_agents.forget_agent("alpha") is True
    names = [a.name for a in two_agents.list_agents()]
    assert names == ["beta"]


def test_forget_agent_noop_on_unknown(two_agents):
    assert two_agents.forget_agent("nobody") is False


def test_forget_preserves_messages(two_agents):
    """Forgetting is reversible: when the agent reconnects, their unread
    messages still surface."""
    two_agents.send_message(from_agent="alpha", to="beta", body="held for you")
    assert two_agents.forget_agent("beta") is True
    assert "beta" not in {a.name for a in two_agents.list_agents()}

    two_agents.upsert_agent("beta", "/repo/b")
    inbox = two_agents.read_inbox(agent="beta")
    assert [m.body for m in inbox] == ["held for you"]


def test_recent_messages_returns_oldest_first(two_agents):
    two_agents.send_message(from_agent="alpha", to="beta", body="first")
    two_agents.send_message(from_agent="beta", to="alpha", body="second")
    two_agents.send_message(from_agent="alpha", to="beta", body="third")
    msgs = two_agents.recent_messages(limit=10)
    assert [m.body for m in msgs] == ["first", "second", "third"]


def test_recent_messages_caps_at_limit(two_agents):
    for i in range(5):
        two_agents.send_message(from_agent="alpha", to="beta", body=f"m{i}")
    msgs = two_agents.recent_messages(limit=3)
    # the three most recent, oldest first
    assert [m.body for m in msgs] == ["m2", "m3", "m4"]


def test_recent_messages_includes_full_body(two_agents):
    big_body = "x" * 500  # longer than the audit preview limit
    two_agents.send_message(from_agent="alpha", to="beta", body=big_body)
    msgs = two_agents.recent_messages(limit=1)
    assert msgs[0].body == big_body


def test_forget_filters_subsequent_broadcasts(three_agents):
    """A broadcast goes only to currently-registered peers, so forgetting
    silences future fan-out without touching past messages."""
    three_agents.forget_agent("gamma")
    result = three_agents.send_message(
        from_agent="alpha", to="*", body="post-forget"
    )
    assert result["recipients"] == ["beta"]
    assert three_agents.pending_count(agent="gamma") == 0


# --------- concurrent writers --------------------------------------------

def _writer(db_path: str, audit_log: str, name: str, peer: str, n: int) -> None:
    # subprocess entrypoint — must reimport
    os.environ["AGENT_BUS_DB"] = db_path
    os.environ["AGENT_BUS_AUDIT_LOG"] = audit_log
    from agent_bus.storage import Storage

    store = Storage()
    store.upsert_agent(name, f"/repo/{name}")
    store.upsert_agent(peer, f"/repo/{peer}")
    for i in range(n):
        store.send_message(from_agent=name, to=peer, body=f"{name}-msg-{i}")


def test_concurrent_writers_no_loss_no_dup(bus_paths):
    db = str(bus_paths["db"])
    log = str(bus_paths["log"])
    # warm-up: pre-create schema + agents so neither child races on it
    from agent_bus.storage import Storage

    s = Storage()
    s.upsert_agent("alpha", "/repo/alpha")
    s.upsert_agent("beta", "/repo/beta")

    ctx = mp.get_context("spawn")
    p1 = ctx.Process(target=_writer, args=(db, log, "alpha", "beta", 50))
    p2 = ctx.Process(target=_writer, args=(db, log, "beta", "alpha", 50))
    p1.start(); p2.start()
    p1.join(timeout=30); p2.join(timeout=30)
    assert p1.exitcode == 0, "alpha writer crashed"
    assert p2.exitcode == 0, "beta writer crashed"

    # 50 messages each direction
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        ids = [r["message_id"] for r in conn.execute("SELECT message_id FROM messages")]
    assert total == 100
    assert len(set(ids)) == 100, "duplicate message_id observed"

    # audit log has 100 send rows
    sends = [
        json.loads(line)
        for line in Path(log).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert sum(1 for r in sends if r["op"] == "send") == 100

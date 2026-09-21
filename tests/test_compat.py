"""Backwards compatibility: behaviour and data that existing installs rely on.

These tests pin what an upgrade must NOT change — the public return
shapes and the ability to open a 0.4.x database with its repo-slug
agent names and unread mail intact.
"""

from __future__ import annotations

import sqlite3


def test_unicast_return_shape_is_stable(two_agents):
    result = two_agents.send_message(from_agent="alpha", to="beta", body="hi")
    assert {"message_id", "sent_at", "thread_id", "recipients"} <= set(result)
    assert result["recipients"] == ["beta"]


def test_broadcast_return_shape_is_stable(three_agents):
    result = three_agents.send_message(from_agent="alpha", to="*", body="hi")
    assert {"message_ids", "sent_at", "thread_id", "recipients"} <= set(result)
    assert "message_id" not in result
    assert len(result["message_ids"]) == len(result["recipients"]) == 2


def test_message_dict_keeps_its_keys(two_agents):
    two_agents.send_message(from_agent="alpha", to="beta", body="hi")
    (msg,) = two_agents.read_inbox(agent="beta")
    assert {
        "message_id", "from", "to", "body", "thread_id",
        "sent_at", "read_at", "delivered_at",
    } <= set(msg.to_dict())


def test_agent_dict_keeps_its_keys(two_agents):
    (row, count), *_ = two_agents.list_agents_with_counts()
    assert {
        "name", "repo_path", "registered_at", "last_seen", "pending_count",
    } <= set(row.to_dict(pending_count=count))


# --------------------------- 0.4.x database ------------------------------


def test_legacy_db_opens_with_roster_intact(legacy_db):
    from agent_bus.storage import Storage

    store = Storage()
    assert [a.name for a in store.list_agents()] == ["human", "repo-a", "repo-b"]
    assert store.get_agent("repo-a").repo_path == "/code/repo-a"


def test_legacy_db_unread_mail_still_reaches_its_agent(legacy_db):
    from agent_bus.storage import Storage

    store = Storage()
    assert store.pending_count(agent="repo-a") == 1
    inbox = store.read_inbox(agent="repo-a")
    assert [m.body for m in inbox] == ["old and unread"]
    assert inbox[0].from_agent == "repo-b"


def test_legacy_db_history_is_not_rewritten(legacy_db):
    from agent_bus.storage import Storage

    store = Storage()
    store.init_schema()
    with sqlite3.connect(legacy_db) as conn:
        rows = conn.execute(
            "SELECT message_id, from_agent, to_agent, body, read_at "
            "FROM messages ORDER BY message_id"
        ).fetchall()
    assert rows == [
        ("m-read", "repo-b", "repo-a", "old and read", "2026-01-01T00:00:00.000000Z"),
        ("m-unread", "repo-b", "repo-a", "old and unread", None),
    ]


def test_legacy_agent_names_keep_working_end_to_end(legacy_db):
    """A repo-slug name with no client part is still a first-class agent."""
    from agent_bus.storage import Storage

    store = Storage()
    sent = store.send_message(from_agent="repo-a", to="repo-b", body="still here")
    assert sent["recipients"] == ["repo-b"]
    assert [m.body for m in store.read_inbox(agent="repo-b")] == ["still here"]

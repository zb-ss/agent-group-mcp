"""Repo-scoped group addressing: several identities behind one repo name."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from agent_bus.storage import UnknownRecipientError

REPO_A = "/code/repo-a"
REPO_B = "/code/repo-b"


@pytest.fixture
def shared_repo(storage):
    """Two clients in repo-a, one in repo-b, and a human."""
    storage.upsert_agent("repo-a/claude", REPO_A)
    storage.upsert_agent("repo-a/codex", REPO_A)
    storage.upsert_agent("repo-b/claude", REPO_B)
    storage.ensure_agent("human", "/home")
    return storage


def _set_last_seen(storage, name: str, *, days_ago: int) -> None:
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    with sqlite3.connect(storage.path) as conn:
        conn.execute("UPDATE agents SET last_seen = ? WHERE name = ?", (ts, name))


# --------------------------- resolution ----------------------------------


def test_bare_repo_name_reaches_every_client_in_that_repo(shared_repo):
    result = shared_repo.send_message(
        from_agent="repo-b/claude", to="repo-a", body="hello repo-a"
    )
    assert result["kind"] == "group"
    assert result["to"] == "repo-a"
    assert result["recipients"] == ["repo-a/claude", "repo-a/codex"]
    assert len(set(result["message_ids"])) == 2
    assert "message_id" not in result

    for member in ("repo-a/claude", "repo-a/codex"):
        (msg,) = shared_repo.read_inbox(agent=member)
        assert (msg.body, msg.to_agent, msg.kind) == ("hello repo-a", member, "group")
        assert msg.to_dict()["addressed_to"] == "repo-a"
    assert shared_repo.read_inbox(agent="repo-b/claude") == []


def test_group_send_from_inside_the_group_skips_the_sender(shared_repo):
    result = shared_repo.send_message(
        from_agent="repo-a/claude", to="repo-a", body="repo-mates?"
    )
    assert result["recipients"] == ["repo-a/codex"]
    assert result["kind"] == "group"
    assert result["message_id"] == result["message_ids"][0]


def test_group_send_with_nobody_else_in_it_is_a_noop(storage):
    storage.upsert_agent("repo-a/claude", REPO_A)
    result = storage.send_message(from_agent="repo-a/claude", to="repo-a", body="hi")
    assert (result["recipients"], result["message_ids"]) == ([], [])
    assert result["kind"] == "group"


def test_full_name_reaches_exactly_one_client(shared_repo):
    result = shared_repo.send_message(
        from_agent="repo-a/claude", to="repo-a/codex", body="just you"
    )
    assert result["kind"] == "direct"
    assert result["recipients"] == ["repo-a/codex"]
    assert result["message_id"] and result["message_ids"] == [result["message_id"]]

    (msg,) = shared_repo.read_inbox(agent="repo-a/codex")
    assert msg.kind == "direct"
    assert shared_repo.read_inbox(agent="repo-a/claude") == []


def test_name_without_a_client_is_still_a_direct_message(shared_repo):
    result = shared_repo.send_message(
        from_agent="repo-a/claude", to="human", body="hi human"
    )
    assert result["kind"] == "direct"
    assert result["recipients"] == ["human"]
    assert "message_id" in result


def test_legacy_agent_is_a_member_of_the_group_named_after_it(shared_repo):
    """An agent still wired under the bare repo name keeps getting mail
    addressed to that name once clients join the repo."""
    shared_repo.upsert_agent("repo-a", REPO_A)
    result = shared_repo.send_message(
        from_agent="repo-b/claude", to="repo-a", body="all of repo-a"
    )
    assert result["kind"] == "group"
    assert result["recipients"] == ["repo-a", "repo-a/claude", "repo-a/codex"]


def test_broadcast_reaches_every_identity_but_the_sender(shared_repo):
    result = shared_repo.send_message(from_agent="human", to="*", body="all hands")
    assert result["kind"] == "broadcast"
    assert result["recipients"] == ["repo-a/claude", "repo-a/codex", "repo-b/claude"]
    assert "message_id" not in result
    (msg,) = shared_repo.read_inbox(agent="repo-a/codex")
    assert msg.kind == "broadcast"


def test_messages_written_before_group_addressing_read_as_direct(legacy_db):
    from agent_bus.storage import Storage

    (msg,) = Storage().read_inbox(agent="repo-a")
    assert msg.kind == "direct"
    assert msg.to_dict()["addressed_to"] is None


# --------------------------- unknown recipients --------------------------


def test_unknown_recipient_is_an_error_not_a_dead_letter(shared_repo):
    with pytest.raises(UnknownRecipientError):
        shared_repo.send_message(from_agent="human", to="nobody-here", body="hi")
    with sqlite3.connect(shared_repo.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_unknown_client_error_lists_the_repo_members(shared_repo):
    with pytest.raises(UnknownRecipientError) as exc_info:
        shared_repo.send_message(from_agent="human", to="repo-a/cluade", body="hi")
    message = str(exc_info.value)
    assert "repo-a/claude" in message and "repo-a/codex" in message


def test_unknown_group_error_suggests_a_close_match(shared_repo):
    with pytest.raises(UnknownRecipientError) as exc_info:
        shared_repo.send_message(from_agent="human", to="repo-aa", body="hi")
    assert "repo-a" in str(exc_info.value)


def test_unknown_recipient_writes_no_audit_row(shared_repo, bus_paths):
    with pytest.raises(UnknownRecipientError):
        shared_repo.send_message(from_agent="human", to="nobody-here", body="hi")
    assert not bus_paths["log"].exists() or bus_paths["log"].read_text() == ""


# --------------------------- mail addressed to a retired name -----------


def test_members_drain_mail_addressed_to_their_bare_group(shared_repo):
    """Rows addressed to the bare repo name — left over from before the
    repo had clients, or written by an older agent-bus that does not
    expand groups — go to the first member that reads."""
    with sqlite3.connect(shared_repo.path) as conn:
        conn.execute(
            "INSERT INTO messages (message_id, from_agent, to_agent, body, "
            "thread_id, sent_at) VALUES ('m-old', 'repo-b/claude', 'repo-a', "
            "'for whoever is in repo-a', 't-old', '2026-01-01T00:00:00.000000Z')"
        )
    assert shared_repo.pending_count(agent="repo-a/claude") == 1
    assert shared_repo.pending_count(agent="repo-a/codex") == 1

    (msg,) = shared_repo.read_inbox(agent="repo-a/codex")
    assert (msg.message_id, msg.to_agent) == ("m-old", "repo-a")
    assert shared_repo.read_inbox(agent="repo-a/claude") == []
    assert shared_repo.read_inbox(agent="repo-b/claude") == []


def test_members_leave_bare_group_mail_alone_while_that_agent_exists(shared_repo):
    """If an agent is still registered under the bare name, the mail is its."""
    shared_repo.upsert_agent("repo-a", REPO_A)
    with sqlite3.connect(shared_repo.path) as conn:
        conn.execute(
            "INSERT INTO messages (message_id, from_agent, to_agent, body, "
            "thread_id, sent_at) VALUES ('m-old', 'repo-b/claude', 'repo-a', "
            "'for the legacy agent', 't-old', '2026-01-01T00:00:00.000000Z')"
        )
    assert shared_repo.read_inbox(agent="repo-a/claude") == []
    assert shared_repo.pending_count(agent="repo-a/claude") == 0
    (msg,) = shared_repo.read_inbox(agent="repo-a")
    assert msg.message_id == "m-old"


def test_roster_counts_match_what_read_inbox_would_return(shared_repo):
    with sqlite3.connect(shared_repo.path) as conn:
        conn.execute(
            "INSERT INTO messages (message_id, from_agent, to_agent, body, "
            "thread_id, sent_at) VALUES ('m-old', 'human', 'repo-a', 'x', "
            "'t', '2026-01-01T00:00:00.000000Z')"
        )
    shared_repo.send_message(from_agent="human", to="repo-a/claude", body="direct")
    counts = {a.name: n for a, n in shared_repo.list_agents_with_counts()}
    assert counts == {
        "human": 0, "repo-a/claude": 2, "repo-a/codex": 1, "repo-b/claude": 0,
    }


# --------------------------- idle members --------------------------------


def test_fan_out_skips_a_member_that_has_been_idle_too_long(shared_repo, monkeypatch):
    monkeypatch.setenv("AGENT_BUS_FANOUT_MAX_IDLE_DAYS", "14")
    _set_last_seen(shared_repo, "repo-a/codex", days_ago=30)

    group = shared_repo.send_message(from_agent="human", to="repo-a", body="g")
    assert group["recipients"] == ["repo-a/claude"]
    everyone = shared_repo.send_message(from_agent="human", to="*", body="b")
    assert everyone["recipients"] == ["repo-a/claude", "repo-b/claude"]


def test_a_fully_idle_group_still_gets_its_mail(shared_repo, monkeypatch):
    """The cutoff trims stale clients; it must never silence a repo."""
    monkeypatch.setenv("AGENT_BUS_FANOUT_MAX_IDLE_DAYS", "14")
    _set_last_seen(shared_repo, "repo-a/claude", days_ago=30)
    _set_last_seen(shared_repo, "repo-a/codex", days_ago=40)

    result = shared_repo.send_message(from_agent="human", to="repo-a", body="g")
    assert result["recipients"] == ["repo-a/claude", "repo-a/codex"]


def test_idle_member_is_still_reachable_by_its_full_name(shared_repo, monkeypatch):
    monkeypatch.setenv("AGENT_BUS_FANOUT_MAX_IDLE_DAYS", "14")
    _set_last_seen(shared_repo, "repo-a/codex", days_ago=30)
    result = shared_repo.send_message(from_agent="human", to="repo-a/codex", body="d")
    assert result["recipients"] == ["repo-a/codex"]


def test_idle_cutoff_can_be_switched_off(shared_repo, monkeypatch):
    monkeypatch.setenv("AGENT_BUS_FANOUT_MAX_IDLE_DAYS", "0")
    _set_last_seen(shared_repo, "repo-a/codex", days_ago=300)
    result = shared_repo.send_message(from_agent="human", to="repo-a", body="g")
    assert result["recipients"] == ["repo-a/claude", "repo-a/codex"]


# --------------------------- audit ---------------------------------------


def test_group_send_audits_one_row_per_recipient(shared_repo, bus_paths):
    shared_repo.send_message(from_agent="human", to="repo-a", body="audited")
    rows = [json.loads(line) for line in bus_paths["log"].read_text().splitlines()]
    sends = [r for r in rows if r["op"] == "send"]
    assert sorted(r["to"] for r in sends) == ["repo-a/claude", "repo-a/codex"]
    assert {r["addressed_to"] for r in sends} == {"repo-a"}

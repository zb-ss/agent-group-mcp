"""Claiming: one agent per repo takes responsibility for a fanned-out message.

A message addressed to a repo (or to everyone) lands in every client's
inbox, but only the first client of each repo to read it is asked to act.
The others still see it, marked as already picked up.
"""

from __future__ import annotations

import io
import json

import pytest

REPO_A = "/code/repo-a"


@pytest.fixture
def shared_repo(storage):
    storage.upsert_agent("repo-a/claude", REPO_A)
    storage.upsert_agent("repo-a/codex", REPO_A)
    storage.upsert_agent("repo-b/claude", "/code/repo-b")
    storage.ensure_agent("human", "/home")
    return storage


def _audit_ops(bus_paths) -> list[dict]:
    return [
        json.loads(line)
        for line in bus_paths["log"].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# --------------------------- storage -------------------------------------


def test_first_reader_in_a_repo_claims_a_group_message(shared_repo):
    shared_repo.send_message(from_agent="human", to="repo-a", body="someone look")

    (first,) = shared_repo.read_inbox(agent="repo-a/codex")
    assert first.claimed_by == "repo-a/codex"
    assert first.to_dict()["claimed_by"] == "repo-a/codex"

    (second,) = shared_repo.read_inbox(agent="repo-a/claude")
    assert second.claimed_by == "repo-a/codex"
    assert second.body == "someone look"


def test_direct_messages_are_never_claimed(shared_repo):
    shared_repo.send_message(from_agent="human", to="repo-a/claude", body="you")
    (msg,) = shared_repo.read_inbox(agent="repo-a/claude")
    assert msg.claimed_by is None


def test_peeking_does_not_claim(shared_repo):
    shared_repo.send_message(from_agent="human", to="repo-a", body="someone look")
    (peeked,) = shared_repo.read_inbox(agent="repo-a/codex", mark_read=False)
    assert peeked.claimed_by is None
    (claimed,) = shared_repo.read_inbox(agent="repo-a/claude")
    assert claimed.claimed_by == "repo-a/claude"


def test_a_broadcast_is_claimed_once_per_repo(shared_repo):
    shared_repo.send_message(from_agent="human", to="*", body="all hands")

    (a_first,) = shared_repo.read_inbox(agent="repo-a/claude")
    (b_only,) = shared_repo.read_inbox(agent="repo-b/claude")
    (a_second,) = shared_repo.read_inbox(agent="repo-a/codex")

    assert a_first.claimed_by == "repo-a/claude"
    assert b_only.claimed_by == "repo-b/claude"   # its own repo, its own claim
    assert a_second.claimed_by == "repo-a/claude"


def test_actionable_only_leaves_what_a_repo_mate_claimed(shared_repo):
    """What the Stop hook reads: messages this agent must act on."""
    shared_repo.send_message(from_agent="human", to="repo-a", body="someone look")
    shared_repo.send_message(from_agent="human", to="repo-a/claude", body="just you")
    shared_repo.read_inbox(agent="repo-a/codex")  # codex claims the group message

    actionable = shared_repo.read_inbox(agent="repo-a/claude", actionable_only=True)
    assert [m.body for m in actionable] == ["just you"]

    # the claimed copy is still there for the next full read
    (fyi,) = shared_repo.read_inbox(agent="repo-a/claude")
    assert (fyi.body, fyi.claimed_by) == ("someone look", "repo-a/codex")


def test_actionable_only_claims_what_nobody_has_taken(shared_repo):
    shared_repo.send_message(from_agent="human", to="repo-a", body="someone look")
    (msg,) = shared_repo.read_inbox(agent="repo-a/claude", actionable_only=True)
    assert msg.claimed_by == "repo-a/claude"


def test_a_lone_agent_claims_silently(two_agents, bus_paths):
    """Single-identity repos must see no difference, in behaviour or audit."""
    two_agents.send_message(from_agent="alpha", to="*", body="hi")
    (msg,) = two_agents.read_inbox(agent="beta")
    assert msg.claimed_by == "beta"
    assert [r["op"] for r in _audit_ops(bus_paths)] == ["send", "read"]


def test_claim_is_audited_when_repo_mates_got_a_copy(shared_repo, bus_paths):
    shared_repo.send_message(from_agent="human", to="repo-a", body="someone look")
    shared_repo.read_inbox(agent="repo-a/codex")
    shared_repo.read_inbox(agent="repo-a/claude")

    claims = [r for r in _audit_ops(bus_paths) if r["op"] == "claim"]
    assert [(c["actor"], c["to"], c["addressed_to"]) for c in claims] == [
        ("repo-a/codex", "repo-a/codex", "repo-a"),
    ]


# --------------------------- hooks ---------------------------------------


def _run(hook, storage, actor: str) -> str:
    out = io.StringIO()
    assert hook(storage=storage, stdin=io.StringIO("{}"), stdout=out, actor=actor) == 0
    return out.getvalue()


def test_stop_hook_blocks_only_the_agent_that_claims(shared_repo):
    from agent_bus.hooks import run_hook_stop

    shared_repo.send_message(from_agent="human", to="repo-a", body="someone look")

    first = json.loads(_run(run_hook_stop, shared_repo, "repo-a/claude"))
    assert first["decision"] == "block"
    assert "someone look" in first["reason"]

    assert _run(run_hook_stop, shared_repo, "repo-a/codex") == ""
    assert shared_repo.pending_count(agent="repo-a/codex") == 1


def test_stop_hook_still_blocks_for_direct_mail(shared_repo):
    from agent_bus.hooks import run_hook_stop

    shared_repo.send_message(from_agent="human", to="repo-a", body="group")
    shared_repo.read_inbox(agent="repo-a/claude")  # claude claims it
    shared_repo.send_message(from_agent="human", to="repo-a/codex", body="direct")

    payload = json.loads(_run(run_hook_stop, shared_repo, "repo-a/codex"))
    assert "direct" in payload["reason"]
    assert "group" not in payload["reason"]


def test_prompt_hook_shows_a_claimed_message_as_already_picked_up(shared_repo):
    from agent_bus.hooks import run_hook_user_prompt

    shared_repo.send_message(from_agent="human", to="repo-a", body="someone look")
    shared_repo.read_inbox(agent="repo-a/claude")

    text = _run(run_hook_user_prompt, shared_repo, "repo-a/codex")
    assert "someone look" in text
    assert "already picked up by repo-a/claude" in text
    assert shared_repo.pending_count(agent="repo-a/codex") == 0


def test_prompt_hook_tells_the_claimer_the_message_is_theirs(shared_repo):
    from agent_bus.hooks import run_hook_user_prompt

    shared_repo.send_message(from_agent="human", to="repo-a", body="someone look")
    text = _run(run_hook_user_prompt, shared_repo, "repo-a/codex")
    assert "1 new message(s)" in text
    assert "everyone in repo-a" in text
    assert "already picked up" not in text


# --------------------------- concurrent readers --------------------------


def _drain_in_subprocess(db_path: str, audit_log: str, agent: str) -> None:
    import os

    os.environ["AGENT_BUS_DB"] = db_path
    os.environ["AGENT_BUS_AUDIT_LOG"] = audit_log
    from agent_bus.storage import Storage

    store = Storage()
    while store.read_inbox(agent=agent, limit=3):
        pass


def test_concurrent_repo_mates_never_both_claim(shared_repo, bus_paths):
    import multiprocessing as mp
    import sqlite3

    for i in range(40):
        shared_repo.send_message(from_agent="human", to="repo-a", body=f"m{i}")

    ctx = mp.get_context("spawn")
    procs = [
        ctx.Process(
            target=_drain_in_subprocess,
            args=(str(bus_paths["db"]), str(bus_paths["log"]), agent),
        )
        for agent in ("repo-a/claude", "repo-a/codex")
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)
    assert [p.exitcode for p in procs] == [0, 0]

    with sqlite3.connect(bus_paths["db"]) as conn:
        per_fanout = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT claimed_by), "
            "SUM(claimed_by IS NULL), SUM(read_at IS NULL) "
            "FROM messages GROUP BY fanout_id"
        ).fetchall()
    assert len(per_fanout) == 40
    # both copies read, both naming the same single claimer
    assert set(per_fanout) == {(2, 1, 0, 0)}

    claims = [r for r in _audit_ops(bus_paths) if r["op"] == "claim"]
    assert len(claims) == 40

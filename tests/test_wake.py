"""Wake-on-send: per-recipient shell command fires when mail arrives.

We never wait on the wake subprocess in production (fire-and-forget),
so to verify it actually ran in tests we use commands that write a
file. The presence of the file plus its contents = proof of fire.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from agent_bus import wake


def _wait_for(path: Path, *, timeout: float = 5.0) -> bool:
    """Poll for an external subprocess to finish writing a marker file.

    `touch` creates a zero-byte file, so we cannot require size > 0.
    Existence alone is enough — the test only cares that the wake
    command ran.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return True
        time.sleep(0.02)
    return False


# --------------------------- config IO ----------------------------------


def test_load_wake_config_missing_returns_empty(bus_paths):
    assert wake.load_wake_config() == {}


def test_save_and_load_round_trip(bus_paths):
    cfg = {"alpha": "echo alpha", "beta": {"command": "echo beta"}}
    wake.save_wake_config(cfg)
    again = wake.load_wake_config()
    assert again == cfg


def test_load_wake_config_handles_garbage(bus_paths):
    wake.wake_config_path().write_text("not json {{{", encoding="utf-8")
    # graceful degradation — wake is opt-in, malformed config must not crash
    assert wake.load_wake_config() == {}


def test_load_wake_config_handles_non_dict_root(bus_paths):
    wake.wake_config_path().write_text("[1, 2, 3]", encoding="utf-8")
    assert wake.load_wake_config() == {}


# --------------------------- fire_wake ---------------------------------


def test_fire_wake_no_config_returns_noop(bus_paths):
    fired, status = wake.fire_wake(
        "alpha",
        from_agent="beta",
        to_agent="alpha",
        body="hi",
        thread_id="t",
        message_id="m",
    )
    assert fired is False
    assert status == "no-config"


def test_fire_wake_disabled_entry(bus_paths):
    wake.save_wake_config({"alpha": False, "beta": None, "gamma": ""})
    for name in ("alpha", "beta", "gamma"):
        fired, status = wake.fire_wake(
            name,
            from_agent="x",
            to_agent=name,
            body="hi",
            thread_id="t",
            message_id="m",
        )
        assert fired is False, name
        assert status == "disabled", (name, status)


def test_fire_wake_runs_string_command(bus_paths, tmp_path):
    marker = tmp_path / "wake-fired"
    wake.save_wake_config({"alpha": f'touch "{marker}"'})
    fired, status = wake.fire_wake(
        "alpha",
        from_agent="beta",
        to_agent="alpha",
        body="hi",
        thread_id="t",
        message_id="m",
    )
    assert fired is True
    assert status == "fired:OK"
    assert _wait_for(marker), "wake command did not produce its marker file"


def test_fire_wake_runs_dict_command_with_metadata(bus_paths, tmp_path):
    marker = tmp_path / "wake-dict"
    wake.save_wake_config({"alpha": {"command": f'touch "{marker}"', "note": "ignored"}})
    fired, status = wake.fire_wake(
        "alpha",
        from_agent="beta",
        to_agent="alpha",
        body="hi",
        thread_id="t",
        message_id="m",
    )
    assert fired is True
    assert _wait_for(marker)


def test_fire_wake_passes_env_and_stdin(bus_paths, tmp_path):
    marker = tmp_path / "wake-env.json"
    cmd = (
        f'(echo "from=$AGENT_BUS_FROM"; '
        f'echo "to=$AGENT_BUS_TO"; '
        f'echo "thread=$AGENT_BUS_THREAD_ID"; '
        f'echo "preview=$AGENT_BUS_BODY_PREVIEW"; '
        f'cat) > "{marker}"'
    )
    wake.save_wake_config({"alpha": cmd})
    fired, _status = wake.fire_wake(
        "alpha",
        from_agent="beta",
        to_agent="alpha",
        body="hello world",
        thread_id="thread-xyz",
        message_id="msg-1",
    )
    assert fired is True
    assert _wait_for(marker)
    content = marker.read_text(encoding="utf-8")
    assert "from=beta" in content
    assert "to=alpha" in content
    assert "thread=thread-xyz" in content
    assert "preview=hello world" in content
    # stdin payload is JSON
    json_part = content.split("preview=hello world\n", 1)[1].strip()
    payload = json.loads(json_part)
    assert payload["from"] == "beta"
    assert payload["body"] == "hello world"


# --------------------------- send_message integration -------------------


def test_send_message_fires_wake_and_audits_it(bus_paths, two_agents, tmp_path):
    marker = tmp_path / "wake-send.flag"
    wake.save_wake_config({"beta": f'touch "{marker}"'})
    result = two_agents.send_message(from_agent="alpha", to="beta", body="ring")
    assert "message_id" in result
    assert _wait_for(marker), "wake did not fire from send_message"

    # audit log should have a send row AND a wake row
    rows = [
        json.loads(l)
        for l in bus_paths["log"].read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]
    ops = [r["op"] for r in rows]
    assert ops.count("send") == 1
    assert ops.count("wake") == 1
    wake_row = next(r for r in rows if r["op"] == "wake")
    assert wake_row["to"] == "beta"
    assert wake_row["wake_status"] == "fired:OK"


def test_broadcast_fires_one_wake_per_recipient(bus_paths, three_agents, tmp_path):
    marker_dir = tmp_path / "wakes"
    marker_dir.mkdir()
    wake.save_wake_config({
        "beta": f'touch "{marker_dir}/beta"',
        "gamma": f'touch "{marker_dir}/gamma"',
    })
    three_agents.send_message(from_agent="alpha", to="*", body="bell")
    assert _wait_for(marker_dir / "beta")
    assert _wait_for(marker_dir / "gamma")

    rows = [
        json.loads(l)
        for l in bus_paths["log"].read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]
    wake_rows = [r for r in rows if r["op"] == "wake"]
    targets = sorted(r["to"] for r in wake_rows)
    assert targets == ["beta", "gamma"]


def test_send_without_wake_config_is_a_noop(bus_paths, two_agents):
    """No wake.json → send still works, no wake rows in the audit log."""
    two_agents.send_message(from_agent="alpha", to="beta", body="no-wake")
    rows = [
        json.loads(l)
        for l in bus_paths["log"].read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]
    assert all(r["op"] != "wake" for r in rows)


def test_recipient_without_config_skipped_but_others_fire(
    bus_paths, three_agents, tmp_path
):
    """If only some recipients have wake commands, only those fire."""
    marker = tmp_path / "only-beta"
    wake.save_wake_config({"beta": f'touch "{marker}"'})
    three_agents.send_message(from_agent="alpha", to="*", body="ping")
    assert _wait_for(marker)

    rows = [
        json.loads(l)
        for l in bus_paths["log"].read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]
    wake_rows = [r for r in rows if r["op"] == "wake"]
    assert [r["to"] for r in wake_rows] == ["beta"]


# --------------------------- groups -------------------------------------


def _wake_rows(bus_paths) -> list[dict]:
    return [
        row
        for row in (
            json.loads(line)
            for line in bus_paths["log"].read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
        if row["op"] == "wake"
    ]


def _make_older(storage, name: str) -> None:
    """Seen an hour ago: less recent than its repo-mate, but nowhere near
    idle enough for fan-out to skip it."""
    import sqlite3
    from datetime import datetime, timedelta, timezone

    an_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    with sqlite3.connect(storage.path) as conn:
        conn.execute(
            "UPDATE agents SET last_seen = ? WHERE name = ?", (an_hour_ago, name)
        )


@pytest.fixture
def shared_repo(storage):
    storage.upsert_agent("repo-a/claude", "/code/repo-a")
    storage.upsert_agent("repo-a/codex", "/code/repo-a")
    storage.ensure_agent("human", "/home")
    return storage


def test_group_send_wakes_one_agent_per_repo(bus_paths, shared_repo, tmp_path):
    """Waking every client of a repo would start four turns for one message;
    the most recently seen client is the one most likely to be listening."""
    wake.save_wake_config({
        "repo-a/claude": f'touch "{tmp_path}/claude"',
        "repo-a/codex": f'touch "{tmp_path}/codex"',
    })
    _make_older(shared_repo, "repo-a/claude")

    shared_repo.send_message(from_agent="human", to="repo-a", body="ring")
    assert _wait_for(tmp_path / "codex")
    assert [r["to"] for r in _wake_rows(bus_paths)] == ["repo-a/codex"]
    assert not (tmp_path / "claude").exists()


def test_group_wake_falls_to_a_client_that_has_a_command(
    bus_paths, shared_repo, tmp_path
):
    wake.save_wake_config({"repo-a/claude": f'touch "{tmp_path}/claude"'})
    _make_older(shared_repo, "repo-a/claude")  # codex is fresher but unwired

    shared_repo.send_message(from_agent="human", to="repo-a", body="ring")
    assert _wait_for(tmp_path / "claude")
    assert [r["to"] for r in _wake_rows(bus_paths)] == ["repo-a/claude"]


def test_wake_command_keyed_by_repo_serves_every_client_in_it(
    bus_paths, shared_repo, tmp_path
):
    """A wake.json written before the repo had clients is keyed by the bare
    repo name; it keeps working and learns which client it is waking."""
    out = tmp_path / "who"
    wake.save_wake_config({"repo-a": f'printf "%s" "$AGENT_BUS_TO" > "{out}"'})

    shared_repo.send_message(from_agent="human", to="repo-a/claude", body="ring")
    assert _wait_for(out)
    time.sleep(0.1)
    assert out.read_text() == "repo-a/claude"


def test_own_wake_command_wins_over_the_repo_one(bus_paths, shared_repo, tmp_path):
    wake.save_wake_config({
        "repo-a": f'touch "{tmp_path}/repo"',
        "repo-a/claude": f'touch "{tmp_path}/own"',
    })
    shared_repo.send_message(from_agent="human", to="repo-a/claude", body="ring")
    assert _wait_for(tmp_path / "own")
    assert not (tmp_path / "repo").exists()


def test_disabling_one_client_does_not_fall_back_to_the_repo_command(
    bus_paths, shared_repo, tmp_path
):
    wake.save_wake_config({
        "repo-a": f'touch "{tmp_path}/repo"',
        "repo-a/claude": False,
    })
    shared_repo.send_message(from_agent="human", to="repo-a/claude", body="ring")
    time.sleep(0.3)
    assert not (tmp_path / "repo").exists()
    assert _wake_rows(bus_paths) == []

"""The cross-session status list: effective state and row collection."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent_bus import formatting as fmt
from agent_bus import status
from agent_bus.sessions import SessionRegistry
from agent_bus.storage import TIMESTAMP_FORMAT, SpawnRecord

NOW = datetime(2030, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
GRACE = 60.0
REPO = "/repo/shared"


def _ts(seconds_ago: float) -> str:
    return (NOW - timedelta(seconds=seconds_ago)).strftime(TIMESTAMP_FORMAT)


def _state(reported, *, live, spawned_at=None):
    return status.effective_state(
        reported, live=live, spawned_at=spawned_at, now=NOW, grace_seconds=GRACE
    )


@pytest.mark.parametrize(
    "reported, expected",
    [
        ("working", "working"),
        ("waiting", "waiting"),
        ("done", "done"),
        (None, "running"),
        ("starting", "running"),
    ],
)
def test_live_session_shows_what_it_reported(reported, expected):
    assert _state(reported, live=True) == expected


def test_not_live_done_stays_done():
    assert _state("done", live=False) == "done"


def test_not_live_inside_grace_is_starting():
    assert _state("starting", live=False, spawned_at=_ts(10)) == "starting"


def test_not_live_outside_grace_is_ended():
    assert _state("starting", live=False, spawned_at=_ts(600)) == "ended"


def test_not_live_without_spawn_record_is_ended():
    assert _state("working", live=False) == "ended"


def test_unparseable_spawn_time_counts_as_old():
    assert _state(None, live=False, spawned_at="garbage") == "ended"


def _record(name="shared/claude-fix", spawned_at=None, **kw):
    fields = dict(
        name=name, client="claude", repo_id="example-org/shared",
        repo_path=REPO, work_dir=REPO, model=None, prompt_file="/tmp/p.txt",
        tmux_session="agents", tmux_window="fix", remote=True,
        spawned_at=spawned_at or _ts(1),
    )
    fields.update(kw)
    return SpawnRecord(**fields)


def _collect(storage, **kw):
    return status.collect(storage, grace_seconds=GRACE, now=NOW, **kw)


def _start(storage, fake_procs, pinned=None):
    """Start a session; one that has a spawn record presents its token, as a
    spawned client's server does."""
    server, lineage = fake_procs.session()
    record = storage.get_spawn(f"shared/claude-{pinned}") if pinned else None
    claim = SessionRegistry(storage).claim(
        client_address="shared/claude", repo_path=REPO, server=server,
        lineage=lineage, pinned=pinned,
        spawn_token=record.spawned_at if record else None,
    )
    return claim.name, server


def test_live_unspawned_session_is_running(storage, fake_procs):
    name, _ = _start(storage, fake_procs)
    (row,) = _collect(storage)
    assert (row.name, row.state, row.live, row.spawned) == (name, "running", True, False)
    assert row.client == "claude"
    assert row.tmux_session is None
    assert row.work_dir == REPO


def test_spawned_not_yet_started_is_starting_with_tmux(storage):
    storage.ensure_agent("shared/claude-fix", REPO)
    storage.set_state("shared/claude-fix", "starting")
    storage.record_spawn(_record(spawned_at=_ts(1)))
    (row,) = _collect(storage)
    assert row.state == "starting"
    assert (row.live, row.spawned, row.label) == (False, True, "fix")
    assert (row.tmux_session, row.tmux_window) == ("agents", "fix")
    assert row.repo_id == "example-org/shared"


def test_spawn_record_without_roster_entry_still_shows(storage):
    storage.record_spawn(_record(spawned_at=_ts(1)))
    (row,) = _collect(storage)
    assert (row.name, row.state, row.group) == ("shared/claude-fix", "starting", "shared")


def test_session_goes_live_once_its_server_claims_the_address(storage, fake_procs):
    storage.ensure_agent("shared/claude-fix", REPO)
    storage.record_spawn(_record(spawned_at=_ts(1)))
    name, _ = _start(storage, fake_procs, pinned="fix")
    assert name == "shared/claude-fix"
    (row,) = _collect(storage)
    assert (row.live, row.state, row.tmux_window) == (True, "running", "fix")


def test_killed_session_hidden_unless_ended_requested(storage, fake_procs):
    storage.record_spawn(_record(spawned_at=_ts(1)))
    name, server = _start(storage, fake_procs, pinned="fix")
    fake_procs.kill(server)
    storage.record_spawn(_record(spawned_at=_ts(600)))
    assert _collect(storage) == []
    (row,) = _collect(storage, include_ended=True)
    assert (row.name, row.state, row.live) == (name, "ended", False)


def test_done_session_stays_visible_after_server_ends(storage, fake_procs):
    storage.record_spawn(_record(spawned_at=_ts(600)))
    name, server = _start(storage, fake_procs, pinned="fix")
    storage.set_state(name, "done")
    fake_procs.kill(server)
    (row,) = _collect(storage)
    assert (row.state, row.live) == ("done", False)


def test_to_dict_has_exactly_the_documented_keys(storage, fake_procs):
    _start(storage, fake_procs)
    (row,) = _collect(storage)
    assert sorted(row.to_dict()) == sorted([
        "name", "group", "client", "label", "state", "reported_state",
        "state_at", "live", "spawned", "last_seen", "topic", "pending_count",
        "repo_path", "repo_id", "work_dir", "tmux_session", "tmux_window",
        "model", "remote", "links",
    ])


def test_fragments_show_tmux_location_and_state(storage):
    storage.record_spawn(_record(spawned_at=_ts(1), work_dir="/wt/fix"))
    text = "\n".join(fmt.render_plain(line) for line in status.fragments(_collect(storage)))
    assert "tmux agents:fix" in text
    assert "starting" in text
    assert "dir=/wt/fix" in text
    assert "repo=/repo/shared" in text


# ----------------------------- retention of done ----------------------------


def _finished(storage, fake_procs, *, reported_seconds_ago):
    """A spawned session that reported `done` and whose server is gone."""
    storage.record_spawn(_record(spawned_at=_ts(10_000)))
    name, server = _start(storage, fake_procs, pinned="fix")
    storage.set_state(name, "done")
    with storage.connect() as conn:
        conn.execute(
            "UPDATE agents SET state_at = ? WHERE name = ?",
            (_ts(reported_seconds_ago), name),
        )
    fake_procs.kill(server)
    return name


def test_a_long_finished_session_is_hidden_after_the_retention(storage, fake_procs):
    name = _finished(storage, fake_procs, reported_seconds_ago=5000)
    assert _collect(storage, hide_done_after_seconds=1000) == []
    (row,) = _collect(storage, hide_done_after_seconds=1000, include_ended=True)
    assert (row.name, row.state) == (name, "done")
    assert storage.get_agent(name) is not None  # nothing is deleted


def test_a_recently_finished_session_stays_listed(storage, fake_procs):
    _finished(storage, fake_procs, reported_seconds_ago=100)
    (row,) = _collect(storage, hide_done_after_seconds=1000)
    assert row.state == "done"


def test_without_a_retention_finished_sessions_stay_listed(storage, fake_procs):
    _finished(storage, fake_procs, reported_seconds_ago=10**7)
    assert len(_collect(storage)) == 1


def test_a_live_done_session_is_never_hidden(storage, fake_procs):
    storage.record_spawn(_record(spawned_at=_ts(10_000)))
    name, _server = _start(storage, fake_procs, pinned="fix")
    storage.set_state(name, "done")
    with storage.connect() as conn:
        conn.execute("UPDATE agents SET state_at = ? WHERE name = ?", (_ts(10**6), name))
    (row,) = _collect(storage, hide_done_after_seconds=1000)
    assert (row.live, row.state) == (True, "done")


def test_spawn_then_claim_goes_from_starting_to_live(storage, fake_procs):
    storage.ensure_agent("shared/claude-fix", REPO)
    storage.set_state("shared/claude-fix", "starting")
    storage.record_spawn(_record(spawned_at=_ts(1)))
    (before,) = _collect(storage)
    assert (before.state, before.live) == ("starting", False)

    _start(storage, fake_procs, pinned="fix")

    (after,) = _collect(storage)
    assert (after.state, after.live, after.reported_state) == ("running", True, "starting")


@pytest.mark.parametrize("reported", ["working", "waiting"])
def test_dead_session_that_reported_is_ended_inside_the_grace(reported):
    assert _state(reported, live=False, spawned_at=_ts(10)) == "ended"


def test_dead_session_that_never_reported_is_starting_inside_the_grace():
    assert _state(None, live=False, spawned_at=_ts(10)) == "starting"


def test_the_row_carries_the_session_links(storage, fake_procs):
    name, _ = _start(storage, fake_procs)
    links = [{"label": "PR", "url": "https://example.com/pr/1"}]
    storage.set_links(name, links)
    (row,) = _collect(storage)
    assert row.links == tuple(links)
    assert row.to_dict()["links"] == tuple(links)

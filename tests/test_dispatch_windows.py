"""The idle-window sweep, with a fake Runner: it must close only a done,
idle, unwatched, single-pane window whose prompt is empty."""

from __future__ import annotations

import dataclasses
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from agent_bus import config, status
from agent_bus.dispatch import windows
from agent_bus.storage import TIMESTAMP_FORMAT

NOW = datetime(2030, 1, 7, 10, 0, 0, tzinfo=timezone.utc)
DONE_AT = (NOW - timedelta(hours=5)).strftime(TIMESTAMP_FORMAT)
IDLE_PANE = "some output\n> \n"
TYPED_PANE = "some output\n> half typed reply\n"
PROMPT = {"pattern": r"^> $", "tail_lines": 5, "escapes": False}


def _row(name="repo-a/claude-one", **kw):
    base = dict(
        name=name, group="repo-a", client="claude", label="one", state="done",
        reported_state="done", state_at=DONE_AT, live=True, spawned=True, last_seen=None,
        topic=None, pending_count=0, repo_path="/x", repo_id=None, work_dir="/x",
        tmux_session="agents", tmux_window="one", model=None, remote=None, role=None,
    )
    base.update(kw)
    return status.StatusRow(**base)


class FakeTmux:
    def __init__(self, listing="@1\tone\t1\t0\t0\n", pane=IDLE_PANE, capture_rc=0):
        self.listing, self.pane, self.capture_rc = listing, pane, capture_rc
        self.calls: list[list[str]] = []

    def __call__(self, argv, *, timeout, env=None, cwd=None):
        self.calls.append(list(argv))
        if argv[1] == "list-windows":
            return subprocess.CompletedProcess(argv, 0, self.listing, "")
        if argv[1] == "capture-pane":
            return subprocess.CompletedProcess(argv, self.capture_rc, self.pane, "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def kills(self):
        return [c for c in self.calls if c[1] == "kill-window"]


@pytest.fixture
def cfg():
    c = config.defaults()
    c["spawn"]["clients"]["claude"]["idle_prompt"] = dict(PROMPT)
    c["dispatch"]["window_cleanup"] = {"enabled": True, "idle_minutes": 240, "max_per_run": 5}
    return c


def _sweep(cfg, rows, fake, *, dry_run=False, recheck=None):
    by_name = {r.name: r for r in rows}
    return windows.sweep(
        rows, cfg=cfg, now=NOW, run=fake, dry_run=dry_run,
        recheck=recheck or by_name.get,
    )


def test_closes_exactly_once_when_every_condition_holds(cfg):
    fake = FakeTmux()
    result = _sweep(cfg, [_row()], fake)
    assert result == [{"session": "repo-a/claude-one", "window": "one", "result": "closed"}]
    assert fake.kills() == [["tmux", "kill-window", "-t", "@1"]]


def _kept(cfg, row, fake=None, **kw):
    fake = fake or FakeTmux()
    result = _sweep(cfg, [row], fake, **kw)
    assert len(result) == 1 and result[0]["result"] == "kept", result
    assert fake.kills() == []
    return result[0]


@pytest.mark.parametrize("state", ["waiting", "working", "starting", "running"])
def test_live_states_are_never_closed(cfg, state):
    fake = FakeTmux()
    assert _sweep(cfg, [_row(state=state, reported_state=state)], fake) == []
    assert fake.calls == []


@pytest.mark.parametrize("change", [
    {"live": False}, {"role": "dispatcher"}, {"spawned": False},
    {"state_at": (NOW - timedelta(minutes=10)).strftime(TIMESTAMP_FORMAT)},
    {"state_at": None}, {"tmux_window": None}, {"tmux_session": None},
])
def test_rows_that_are_not_candidates_are_ignored(cfg, change):
    fake = FakeTmux()
    assert _sweep(cfg, [_row(**change)], fake) == []
    assert fake.calls == []


def test_a_client_without_an_idle_prompt_is_kept(cfg):
    cfg["spawn"]["clients"]["claude"]["idle_prompt"] = None
    entry = _kept(cfg, _row())
    assert entry["reason"] == "no idle prompt pattern for claude"


def test_two_windows_of_the_name_are_kept(cfg):
    _kept(cfg, _row(), FakeTmux(listing="@1\tone\t1\t0\t0\n@2\tone\t1\t0\t0\n"))


def test_several_panes_are_kept(cfg):
    _kept(cfg, _row(), FakeTmux(listing="@1\tone\t2\t0\t0\n"))


def test_the_active_window_of_an_attached_session_is_kept(cfg):
    _kept(cfg, _row(), FakeTmux(listing="@1\tone\t1\t1\t1\n"))


def test_an_active_window_of_a_detached_session_may_close(cfg):
    fake = FakeTmux(listing="@1\tone\t1\t1\t0\n")
    assert _sweep(cfg, [_row()], fake)[0]["result"] == "closed"


def test_a_missing_window_is_kept(cfg):
    _kept(cfg, _row(), FakeTmux(listing=""))


@pytest.mark.parametrize("fresh", [
    {"state": "working", "reported_state": "working"},
    {"state_at": (NOW - timedelta(hours=9)).strftime(TIMESTAMP_FORMAT)},
])
def test_a_changed_recheck_keeps_the_window(cfg, fresh):
    row = _row()
    _kept(cfg, row, recheck=lambda name: dataclasses.replace(row, **fresh))


def test_a_vanished_recheck_keeps_the_window(cfg):
    _kept(cfg, _row(), recheck=lambda name: None)


def test_typed_text_keeps_the_window(cfg):
    _kept(cfg, _row(), FakeTmux(pane=TYPED_PANE))


def test_a_failed_capture_keeps_the_window_and_names_only_the_class(cfg):
    entry = _kept(cfg, _row(), FakeTmux(capture_rc=1))
    assert entry["reason"] == "TmuxError"


def test_capture_uses_the_configured_tail_and_escapes(cfg):
    cfg["spawn"]["clients"]["claude"]["idle_prompt"] = {**PROMPT, "tail_lines": 7, "escapes": True}
    fake = FakeTmux()
    _sweep(cfg, [_row()], fake)
    capture = next(c for c in fake.calls if c[1] == "capture-pane")
    assert capture[-3:] == ["-S", "-7", "-e"]


def test_max_per_run_stops_closing(cfg):
    cfg["dispatch"]["window_cleanup"]["max_per_run"] = 2
    rows = [_row(name=f"repo-a/claude-{i}", tmux_window=f"w{i}") for i in range(4)]
    fake = FakeTmux(listing="".join(f"@{i}\tw{i}\t1\t0\t0\n" for i in range(4)))
    result = _sweep(cfg, rows, fake)
    assert [r["result"] for r in result] == ["closed", "closed"]
    assert len(fake.kills()) == 2


def test_dry_run_never_kills(cfg):
    fake = FakeTmux()
    result = _sweep(cfg, [_row()], fake, dry_run=True)
    assert result[0]["result"] == "would_close"
    assert fake.kills() == []


def test_no_pane_text_appears_in_any_result(cfg):
    secret_pane = "SECRET-PANE-TEXT\n> half typed\n"
    results = _sweep(cfg, [_row()], FakeTmux(pane=secret_pane))
    assert "SECRET-PANE-TEXT" not in repr(results)
    results = _sweep(cfg, [_row()], FakeTmux(pane="SECRET-PANE-TEXT\n> \n"), dry_run=True)
    assert "SECRET-PANE-TEXT" not in repr(results)


# Synthetic reproductions of the Claude Code prompt chrome (neutral text).
_EMPTY = "\x1b[39m❯\xa0\x1b[2mTry \"example suggestion\"\x1b[0m\n"
_BARE = "\x1b[39m❯\xa0\n"
_TYPED = "\x1b[39m❯\xa0some unsent words\n"
_MENU = " ❯ No, exit\n"


@pytest.mark.parametrize("pane, idle", [(_EMPTY, True), (_BARE, True), (_TYPED, False),
                                        (_MENU, False)])
def test_the_packaged_claude_idle_pattern_tells_typed_text_apart(pane, idle):
    prompt = config.defaults()["spawn"]["clients"]["claude"]["idle_prompt"]
    assert prompt["escapes"] is True
    import re

    assert bool(re.search(prompt["pattern"], "output\n" + pane, re.MULTILINE)) is idle

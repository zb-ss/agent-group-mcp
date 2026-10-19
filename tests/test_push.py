"""Waking an idle Codex session from the sender's side (`codex queue`)."""

from __future__ import annotations

import io
import json
import time
from pathlib import Path

import pytest

from agent_bus import audit, procs, wake
from agent_bus.clients import codex
from agent_bus.hooks import run_hook_stop, run_hook_user_prompt
from agent_bus.routes import DRIVER_PUSH, STATE_BUSY, STATE_IDLE, STATE_PUSHED, WakeRoutes

CODEX = "repo/codex"
THREAD = "019a0000-0000-7000-8000-000000000001"


@pytest.fixture
def codex_session(storage, fake_procs, monkeypatch, tmp_path):
    """A Codex session in `repo` whose hooks run under a stand-in client
    process, and a stand-in `codex` that records how it was called."""
    storage.upsert_agent(CODEX, "/repo")
    storage.upsert_agent("peer/claude", "/peer")
    client = fake_procs.proc("codex")
    monkeypatch.setenv("AGENT_BUS_NAME", CODEX)
    monkeypatch.setattr(procs, "ancestry", lambda pid=None: [client])
    monkeypatch.setattr(procs, "executable", lambda pid: "/opt/codex/bin/codex")

    calls = tmp_path / "codex-calls"
    fake = tmp_path / "codex"
    fake.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{calls}"\n')
    fake.chmod(0o755)
    monkeypatch.setenv("AGENT_BUS_CODEX_BIN", str(fake))
    return {"store": storage, "client": client, "calls": calls, "fake_procs": fake_procs}


def _hook(store, *, stop: bool, thread: str = THREAD) -> str:
    out = io.StringIO()
    payload = json.dumps({"session_id": thread, "cwd": "/repo"})
    run = run_hook_stop if stop else run_hook_user_prompt
    run(storage=store, stdin=io.StringIO(payload), stdout=out, client="codex")
    return out.getvalue()


def _route(store):
    routes = WakeRoutes(store).covering([CODEX])[CODEX]
    return routes[0] if routes else None


def _pushed_args(calls: Path, timeout: float = 5.0) -> list[str] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if calls.exists() and calls.read_text():
            return calls.read_text().splitlines()
        time.sleep(0.02)
    return None


def _wake_rows() -> list[dict]:
    return [r for r in audit.tail(500) if r["op"] == "wake"]


# ----------------------------- the hooks keep the route --------------------


def test_a_turn_marks_the_session_busy_and_its_end_idle(codex_session):
    store = codex_session["store"]
    _hook(store, stop=False)
    route = _route(store)
    assert (route.route_key, route.driver, route.state) == (THREAD, DRIVER_PUSH, STATE_BUSY)
    assert route.command == "/opt/codex/bin/codex"
    assert route.process == codex_session["client"]

    _hook(store, stop=True)
    assert _route(store).state == STATE_IDLE


def test_a_turn_kept_open_for_mail_stays_busy(codex_session):
    store = codex_session["store"]
    store.send_message(from_agent="peer/claude", to=CODEX, body="arrived mid-turn")
    assert "arrived mid-turn" in _hook(store, stop=True)
    assert _route(store).state == STATE_BUSY


def test_no_route_without_a_thread_id(codex_session):
    out = io.StringIO()
    run_hook_stop(
        storage=codex_session["store"], stdin=io.StringIO('{"cwd": "/repo"}'),
        stdout=out, client="codex",
    )
    assert _route(codex_session["store"]) is None


def test_no_route_with_native_wake_off(codex_session, monkeypatch):
    monkeypatch.setenv("AGENT_BUS_NATIVE_WAKE", "0")
    _hook(codex_session["store"], stop=True)
    assert _route(codex_session["store"]) is None


# ----------------------------- the sender pushes ---------------------------


def test_mail_for_an_idle_session_queues_a_turn(codex_session):
    store = codex_session["store"]
    _hook(store, stop=True)

    store.send_message(from_agent="peer/claude", to=CODEX, body="wake up")

    assert _pushed_args(codex_session["calls"]) == [
        "queue", "--thread", THREAD, "--message", codex.QUEUE_NOTICE,
    ]
    assert _route(store).state == STATE_PUSHED
    (row,) = _wake_rows()
    assert (row["wake_status"], row["wake_driver"], row["to"]) == ("fired:OK", DRIVER_PUSH, CODEX)
    assert store.pending_count(agent=CODEX) == 1  # the woken turn's hook delivers it


def test_one_queued_turn_until_it_starts(codex_session):
    store = codex_session["store"]
    _hook(store, stop=True)
    store.send_message(from_agent="peer/claude", to=CODEX, body="first")
    assert _pushed_args(codex_session["calls"])
    codex_session["calls"].unlink()

    store.send_message(from_agent="peer/claude", to=CODEX, body="second")
    assert _pushed_args(codex_session["calls"], timeout=0.5) is None

    _hook(store, stop=False)  # the queued turn starts and takes both
    _hook(store, stop=True)
    store.send_message(from_agent="peer/claude", to=CODEX, body="third")
    assert _pushed_args(codex_session["calls"])


def test_a_busy_session_is_not_pushed_to(codex_session):
    store = codex_session["store"]
    _hook(store, stop=False)
    store.send_message(from_agent="peer/claude", to=CODEX, body="later")
    assert _pushed_args(codex_session["calls"], timeout=0.5) is None
    assert _wake_rows() == []


def test_a_session_whose_client_exited_is_forgotten(codex_session):
    store = codex_session["store"]
    _hook(store, stop=True)
    codex_session["fake_procs"].kill(codex_session["client"])

    store.send_message(from_agent="peer/claude", to=CODEX, body="nobody home")

    assert _pushed_args(codex_session["calls"], timeout=0.5) is None
    assert _route(store) is None


def test_the_recorded_executable_is_used_by_default(codex_session, monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_BUS_CODEX_BIN")
    recorded = tmp_path / "codex-native"
    recorded.write_text("")
    assert codex.queue_command(thread="t", executable=str(recorded))[0] == str(recorded)
    assert codex.queue_command(thread="t", executable=None)[0] == "codex"
    # gone, say uninstalled since: whatever `codex` is on PATH
    assert codex.queue_command(thread="t", executable="/nonexistent/codex")[0] == "codex"


def test_a_replaced_binary_reads_as_its_path(monkeypatch, tmp_path):
    """Linux shows a running binary replaced by an upgrade as
    "<path> (deleted)"; the path itself holds the new one."""
    proc_root = tmp_path / "proc"
    (proc_root / "self").mkdir(parents=True)
    (proc_root / "self" / "stat").write_text("")
    (proc_root / "4242").mkdir()
    (proc_root / "4242" / "exe").symlink_to("/opt/codex/bin/codex (deleted)")
    monkeypatch.setattr(procs, "PROC_ROOT", proc_root)
    assert procs.executable(4242) == "/opt/codex/bin/codex"


def test_a_command_that_cannot_start_is_audited_and_retried(codex_session, monkeypatch):
    store = codex_session["store"]
    monkeypatch.setenv("AGENT_BUS_CODEX_BIN", "/nonexistent/codex")
    _hook(store, stop=True)

    store.send_message(from_agent="peer/claude", to=CODEX, body="try")

    (row,) = _wake_rows()
    assert row["wake_status"].startswith("fired:ERR:")
    assert _route(store).state == STATE_IDLE  # so the next message tries again


def test_past_the_hourly_limit_the_session_is_not_pushed_to(codex_session, monkeypatch):
    store = codex_session["store"]
    monkeypatch.setenv("AGENT_BUS_WAKES_PER_HOUR", "1")
    WakeRoutes(store).record_wake(CODEX, DRIVER_PUSH)
    _hook(store, stop=True)

    store.send_message(from_agent="peer/claude", to=CODEX, body="too many")

    assert _pushed_args(codex_session["calls"], timeout=0.5) is None
    (row,) = _wake_rows()
    assert row["wake_status"] == "rate-limited"


def test_a_wake_json_entry_wins_over_the_push(codex_session, tmp_path):
    store = codex_session["store"]
    marker = tmp_path / "command-ran"
    wake.save_wake_config({CODEX: f"touch {marker}"})
    _hook(store, stop=True)

    store.send_message(from_agent="peer/claude", to=CODEX, body="configured")

    assert _pushed_args(codex_session["calls"], timeout=0.5) is None
    deadline = time.monotonic() + 5
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert marker.exists()


def test_native_wake_off_means_no_push(codex_session, monkeypatch):
    store = codex_session["store"]
    _hook(store, stop=True)
    monkeypatch.setenv("AGENT_BUS_NATIVE_WAKE", "0")
    store.send_message(from_agent="peer/claude", to=CODEX, body="quiet")
    assert _pushed_args(codex_session["calls"], timeout=0.5) is None


# ----------------------------- repo-wide sends ------------------------------


def test_a_repo_send_is_left_to_a_session_that_watches_itself(codex_session):
    store = codex_session["store"]
    store.upsert_agent("repo/claude", "/repo")
    _hook(store, stop=True)
    WakeRoutes(store).claim_watch(
        route_key="claude-session", client="claude", address="repo/claude",
        owner=codex_session["fake_procs"].proc("claude"),
        watcher=codex_session["fake_procs"].proc("agent-bus"), token="t",
    )

    store.send_message(from_agent="peer/claude", to="repo", body="anyone?")

    assert _pushed_args(codex_session["calls"], timeout=0.5) is None


def test_a_repo_send_pushes_when_nobody_there_watches(codex_session):
    store = codex_session["store"]
    store.upsert_agent("repo/claude", "/repo")
    _hook(store, stop=True)

    store.send_message(from_agent="peer/claude", to="repo", body="anyone?")

    assert _pushed_args(codex_session["calls"])


def test_mail_for_a_session_address_reaches_its_route(codex_session):
    """Codex's hooks may run for a session address: mail sent to the
    client address still reaches it."""
    store = codex_session["store"]
    WakeRoutes(store).note_push(
        route_key=THREAD, client="codex", address=f"{CODEX}-1",
        owner=codex_session["client"], command=None, busy=False,
    )
    store.send_message(from_agent="peer/claude", to=CODEX, body="for any session")
    assert _pushed_args(codex_session["calls"])


def test_a_watcher_that_died_does_not_keep_the_repo_to_itself(codex_session):
    """A watch route left behind by a watcher killed outright must not stop
    the sender from waking somebody else in the repo."""
    store = codex_session["store"]
    store.upsert_agent("repo/claude", "/repo")
    _hook(store, stop=True)
    dead_watcher = codex_session["fake_procs"].proc("agent-bus")
    WakeRoutes(store).claim_watch(
        route_key="claude-session", client="claude", address="repo/claude",
        owner=codex_session["fake_procs"].proc("claude"), watcher=dead_watcher, token="t",
    )
    codex_session["fake_procs"].kill(dead_watcher)

    store.send_message(from_agent="peer/claude", to="repo", body="anyone?")

    assert _pushed_args(codex_session["calls"])
    assert [r.driver for r in WakeRoutes(store).covering(["repo/claude"])["repo/claude"]] == []


def test_mail_that_arrives_as_a_turn_ends_still_queues_a_turn(codex_session, monkeypatch):
    """A send between the Stop hook's drain and its marking the session
    idle sees the session busy and queues nothing; the hook catches it."""
    from agent_bus import hooks

    store = codex_session["store"]
    _hook(store, stop=False)  # busy
    real_drain = hooks.drain

    def drain_then_mail_arrives(*args, **kwargs):
        taken = real_drain(*args, **kwargs)
        store.send_message(from_agent="peer/claude", to=CODEX, body="just too late")
        return taken

    monkeypatch.setattr(hooks, "drain", drain_then_mail_arrives)
    assert _hook(store, stop=True) == ""  # nothing to act on when it looked

    assert _pushed_args(codex_session["calls"])
    assert _route(store).state == STATE_PUSHED


def test_mail_sent_as_a_turn_starts_waits_for_its_end(codex_session, monkeypatch):
    """The prompt hook marks the session busy before it drains, so a send
    in between queues no second turn: the Stop hook delivers it."""
    from agent_bus import hooks

    store = codex_session["store"]
    _hook(store, stop=True)  # idle
    real_drain = hooks.drain

    def drain_then_mail_arrives(*args, **kwargs):
        taken = real_drain(*args, **kwargs)
        store.send_message(from_agent="peer/claude", to=CODEX, body="mid-turn")
        return taken

    monkeypatch.setattr(hooks, "drain", drain_then_mail_arrives)
    _hook(store, stop=False)
    assert _pushed_args(codex_session["calls"], timeout=0.5) is None
    monkeypatch.setattr(hooks, "drain", real_drain)
    assert "mid-turn" in _hook(store, stop=True)


def _route_from(store, **scope):
    """A push route for a process that does not exist here, recorded in
    the given boot / PID namespace."""
    from agent_bus.procs import Proc

    WakeRoutes(store).note_push(
        route_key="t-scope", client="codex", address=CODEX,
        owner=Proc(999_999, "Mon Jan  1 00:00:00 2026"), command=None, busy=False,
    )
    with store.connect() as conn:
        conn.execute(
            "UPDATE wake_routes SET boot_id = ?, pid_ns = ? WHERE route_key = 't-scope'",
            (scope.get("boot_id"), scope.get("pid_ns")),
        )
    return WakeRoutes(store).covering([CODEX])[CODEX][0]


def test_a_route_from_another_pid_namespace_is_not_judged_from_here(storage, monkeypatch):
    storage.upsert_agent(CODEX, "/repo")
    monkeypatch.setattr(procs, "scope", lambda: procs.Scope("boot-a", "pid:[1]"))
    route = _route_from(storage, boot_id="boot-a", pid_ns="pid:[2]")
    assert WakeRoutes(storage).alive(route)


def test_a_route_from_before_a_reboot_is_dead(storage, monkeypatch):
    storage.upsert_agent(CODEX, "/repo")
    monkeypatch.setattr(procs, "scope", lambda: procs.Scope("boot-b", "pid:[1]"))
    route = _route_from(storage, boot_id="boot-a", pid_ns="pid:[1]")
    assert not WakeRoutes(storage).alive(route)
    assert WakeRoutes(storage).covering([CODEX])[CODEX] == []

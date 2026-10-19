"""`hook-watch`: wait for mail in the background, then wake the session."""

from __future__ import annotations

import io
import json

import pytest

from agent_bus import audit, procs, wake
from agent_bus.clients.base import WAKE_EXIT_CODE
from agent_bus.procs import Proc
from agent_bus.routes import DRIVER_WATCH, WakeRoutes
from agent_bus.watch import run_hook_watch

PAYLOAD = json.dumps({"session_id": "session-1", "hook_event_name": "Stop"})


class FakeTime:
    """A clock that only moves when the watcher sleeps; `on_sleep` runs
    before each sleep, to change the world while the watcher waits."""

    def __init__(self, on_sleep=None) -> None:
        self.now = 0.0
        self.sleeps = 0
        self.on_sleep = on_sleep

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps += 1
        if self.on_sleep:
            self.on_sleep(self.sleeps)
        self.now += seconds


@pytest.fixture
def alpha(two_agents, monkeypatch):
    """Hooks resolve to `alpha`; the watcher has no client process to
    keep an eye on unless a test gives it one."""
    monkeypatch.setenv("AGENT_BUS_NAME", "alpha")
    monkeypatch.setattr(procs, "ancestry", lambda pid=None: [])
    return two_agents


def _watch(
    store, fake: FakeTime, *, max_seconds: float = 60.0, payload: str = PAYLOAD,
    client: str | None = None,
):
    err = io.StringIO()
    rc = run_hook_watch(
        storage=store, stdin=io.StringIO(payload), stderr=err, client=client,
        max_seconds=max_seconds, sleep=fake.sleep, clock=fake.clock,
    )
    return rc, err.getvalue()


def _wake_rows() -> list[dict]:
    return [r for r in audit.tail(500) if r["op"] == "wake"]


def test_mail_already_waiting_wakes_at_once(alpha):
    alpha.send_message(from_agent="beta", to="alpha", body="please look at the build")

    rc, err = _watch(alpha, FakeTime())

    assert rc == WAKE_EXIT_CODE
    assert "1 new message(s)" in err
    assert "please look at the build" in err
    assert alpha.pending_count(agent="alpha") == 0  # taken, not shown twice


def test_mail_arriving_while_waiting_wakes(alpha):
    def arrive(n):
        if n == 3:
            alpha.send_message(from_agent="beta", to="alpha", body="late news")

    fake = FakeTime(on_sleep=arrive)
    rc, err = _watch(alpha, fake)

    assert rc == WAKE_EXIT_CODE
    assert "late news" in err
    assert fake.sleeps == 3


def test_no_mail_gives_up_quietly_when_time_is_up(alpha):
    fake = FakeTime()
    rc, err = _watch(alpha, fake, max_seconds=10)

    assert rc == 0
    assert err == ""
    assert fake.now == pytest.approx(10)


def test_the_route_is_released_on_exit(alpha):
    _watch(alpha, FakeTime(), max_seconds=1)
    assert WakeRoutes(alpha).covering(["alpha"]) == {"alpha": []}


def test_a_newer_watcher_of_the_session_takes_over(alpha):
    def newer_watcher_starts(n):
        if n == 1:
            WakeRoutes(alpha).claim_watch(
                route_key="session-1", client="", address="alpha", owner=None,
                watcher=None, token="newer",
            )
            alpha.send_message(from_agent="beta", to="alpha", body="for the newer one")

    rc, err = _watch(alpha, FakeTime(on_sleep=newer_watcher_starts))

    assert rc == 0
    assert err == ""
    assert alpha.pending_count(agent="alpha") == 1  # left for the newer watcher
    # and the older watcher did not take the newer one's route with it
    (route,) = WakeRoutes(alpha).covering(["alpha"])["alpha"]
    assert route.token == "newer"


def test_wake_json_entry_means_the_watcher_stands_down(alpha):
    wake.save_wake_config({"alpha": "notify-send hi"})
    alpha.send_message(from_agent="beta", to="alpha", body="hello")

    fake = FakeTime()
    rc, _ = _watch(alpha, fake)

    assert rc == 0
    assert fake.sleeps == 0
    assert alpha.pending_count(agent="alpha") == 1


def test_wake_json_false_also_stands_down(alpha):
    wake.save_wake_config({"alpha": False})
    alpha.send_message(from_agent="beta", to="alpha", body="hello")
    assert _watch(alpha, FakeTime())[0] == 0


def test_native_wake_turned_off(alpha, monkeypatch):
    monkeypatch.setenv("AGENT_BUS_NATIVE_WAKE", "0")
    alpha.send_message(from_agent="beta", to="alpha", body="hello")
    assert _watch(alpha, FakeTime())[0] == 0
    assert alpha.pending_count(agent="alpha") == 1


def test_a_repo_with_no_agent_for_this_client_is_left_alone(storage, monkeypatch):
    monkeypatch.delenv("AGENT_BUS_NAME", raising=False)
    payload = json.dumps({"session_id": "s", "cwd": "/not/on/the/bus"})
    rc, err = _watch(storage, FakeTime(), payload=payload, client="claude")
    assert (rc, err) == (0, "")


def test_wakes_are_audited_and_counted(alpha):
    alpha.send_message(from_agent="beta", to="alpha", body="one")
    alpha.send_message(from_agent="beta", to="alpha", body="two")

    _watch(alpha, FakeTime())

    rows = _wake_rows()
    assert [r["body_preview"] for r in rows] == ["one", "two"]
    assert {r["wake_status"] for r in rows} == {"woke"}
    assert {r["wake_driver"] for r in rows} == {DRIVER_WATCH}
    with alpha.connect() as conn:
        (count,) = conn.execute(
            "SELECT COUNT(*) FROM wakes WHERE address = 'alpha'"
        ).fetchone()
    assert count == 1  # one wake, however many messages it carried


def test_past_the_hourly_limit_mail_waits(alpha, monkeypatch):
    monkeypatch.setenv("AGENT_BUS_WAKES_PER_HOUR", "2")
    routes = WakeRoutes(alpha)
    for _ in range(2):
        routes.record_wake("alpha", DRIVER_WATCH)
    alpha.send_message(from_agent="beta", to="alpha", body="not now")

    rc, err = _watch(alpha, FakeTime(), max_seconds=10)

    assert (rc, err) == (0, "")
    assert alpha.pending_count(agent="alpha") == 1
    limited = [r for r in _wake_rows() if r["wake_status"] == "rate-limited"]
    assert len(limited) == 1  # said once, not on every poll


def test_a_limit_of_zero_means_no_limit(alpha, monkeypatch):
    monkeypatch.setenv("AGENT_BUS_WAKES_PER_HOUR", "0")
    routes = WakeRoutes(alpha)
    for _ in range(50):
        routes.record_wake("alpha", DRIVER_WATCH)
    alpha.send_message(from_agent="beta", to="alpha", body="still")
    assert _watch(alpha, FakeTime())[0] == WAKE_EXIT_CODE


def test_a_copy_a_repo_mate_already_took_does_not_wake(storage, monkeypatch):
    storage.upsert_agent("repo/claude", "/repo")
    storage.upsert_agent("repo/codex", "/repo")
    storage.upsert_agent("other/claude", "/other")
    storage.send_message(from_agent="other/claude", to="repo", body="anyone?")
    storage.read_inbox(agent="repo/codex")  # codex claims it for the repo

    monkeypatch.setenv("AGENT_BUS_NAME", "repo/claude")
    monkeypatch.setenv("AGENT_BUS_SESSIONS", "0")
    monkeypatch.setattr(procs, "ancestry", lambda pid=None: [])
    rc, _ = _watch(storage, FakeTime(), max_seconds=5)
    assert rc == 0


def test_the_watcher_ends_when_its_client_exits(alpha, monkeypatch):
    client = Proc(4242, "Mon Jan  1 00:00:00 2026", "claude")
    monkeypatch.setattr(procs, "ancestry", lambda pid=None: [client])
    exited = {"yes": False}

    def kill(pid, sig):
        if exited["yes"]:
            raise ProcessLookupError(pid)

    monkeypatch.setattr("agent_bus.watch.os.kill", kill)

    def client_exits(n):
        if n == 2:
            exited["yes"] = True

    fake = FakeTime(on_sleep=client_exits)
    rc, _ = _watch(alpha, fake)
    assert rc == 0
    assert fake.sleeps == 2


def test_a_reused_pid_counts_as_the_client_gone(alpha, monkeypatch):
    client = Proc(4242, "Mon Jan  1 00:00:00 2026", "claude")
    monkeypatch.setattr(procs, "ancestry", lambda pid=None: [client])
    monkeypatch.setattr("agent_bus.watch.os.kill", lambda pid, sig: None)
    monkeypatch.setattr(procs, "gone", lambda candidates: set(candidates))

    rc, _ = _watch(alpha, FakeTime(), max_seconds=600)
    assert rc == 0


def test_a_cleared_conversation_replaces_its_predecessors_watcher(alpha, monkeypatch):
    """Same client process, same address, a new session id (`/clear`):
    only the new conversation's watcher may keep watching."""
    client = Proc(4242, "Mon Jan  1 00:00:00 2026", "claude")
    routes = WakeRoutes(alpha)
    routes.claim_watch(
        route_key="before-clear", client="claude", address="alpha", owner=client,
        watcher=None, token="old",
    )
    routes.claim_watch(
        route_key="after-clear", client="claude", address="alpha", owner=client,
        watcher=None, token="new",
    )
    assert not routes.owns("before-clear", "old")
    assert routes.owns("after-clear", "new")


# ----------------------------- sessions -------------------------------------

SHARED = "shared/claude"


def _session(storage, fake_procs, *, key: str):
    """A running Claude Code session: its client process and the address
    its MCP server claimed."""
    from agent_bus.sessions import SessionRegistry

    server, lineage = fake_procs.session()
    claim = SessionRegistry(storage).claim(
        client_address=SHARED, repo_path="/repo/shared", server=server,
        lineage=lineage, session_key=key,
    )
    return claim.name, lineage[0]


def _watch_as(storage, monkeypatch, client_proc, fake, *, key: str, max_seconds=60.0):
    monkeypatch.setenv("AGENT_BUS_NAME", SHARED)
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    monkeypatch.setattr(procs, "ancestry", lambda pid=None: [client_proc])
    monkeypatch.setattr("agent_bus.watch.os.kill", lambda pid, sig: None)
    payload = json.dumps({"session_id": key, "hook_event_name": "SessionStart"})
    return _watch(storage, fake, payload=payload, client="claude", max_seconds=max_seconds)


def test_a_session_is_woken_for_its_own_mail_only(storage, fake_procs, monkeypatch):
    storage.upsert_agent("peer/codex", "/repo/peer")
    first, first_client = _session(storage, fake_procs, key="a")
    second, _ = _session(storage, fake_procs, key="b")
    storage.send_message(from_agent="peer/codex", to=second, body="for two")

    fake = FakeTime()
    rc, _ = _watch_as(storage, monkeypatch, first_client, fake, key="a", max_seconds=10)
    assert rc == 0
    assert storage.pending_count(agent=second) == 1

    storage.send_message(from_agent="peer/codex", to=first, body="for one")
    rc, err = _watch_as(storage, monkeypatch, first_client, FakeTime(), key="a")
    assert rc == WAKE_EXIT_CODE
    assert "for one" in err and "for two" not in err


def test_mail_for_any_session_wakes_one(storage, fake_procs, monkeypatch):
    storage.upsert_agent("peer/codex", "/repo/peer")
    _, first_client = _session(storage, fake_procs, key="a")
    storage.send_message(from_agent="peer/codex", to=SHARED, body="whoever")

    rc, err = _watch_as(storage, monkeypatch, first_client, FakeTime(), key="a")
    assert rc == WAKE_EXIT_CODE
    assert "to any shared/claude session" in err


def test_a_watcher_started_before_its_server_finds_the_session(
    storage, fake_procs, monkeypatch
):
    """SessionStart can run before the session's MCP server has claimed
    an address: the watcher starts on the shared address and moves to the
    session's own once there is one."""
    storage.upsert_agent("peer/codex", "/repo/peer")
    storage.upsert_agent(SHARED, "/repo/shared")
    client_proc = fake_procs.proc("claude")
    claimed: dict = {}

    def server_starts_then_mail_arrives(n):
        from agent_bus.sessions import SessionRegistry

        if n == 1:
            claimed["name"] = SessionRegistry(storage).claim(
                client_address=SHARED, repo_path="/repo/shared",
                server=fake_procs.proc("agent-bus"), lineage=[client_proc],
                session_key="a",
            ).name
        if n == 2:
            storage.send_message(from_agent="peer/codex", to=claimed["name"], body="direct")

    fake = FakeTime(on_sleep=server_starts_then_mail_arrives)
    rc, err = _watch_as(storage, monkeypatch, client_proc, fake, key="a")
    assert rc == WAKE_EXIT_CODE
    assert "direct" in err


# ----------------------------- the real process -----------------------------


def _spawn_watch(bus_paths, *, max_seconds: float):
    import os
    import subprocess
    import sys

    env = {
        **os.environ,
        "AGENT_BUS_NAME": "alpha",
        "AGENT_BUS_DB": str(bus_paths["db"]),
        "AGENT_BUS_AUDIT_LOG": str(bus_paths["log"]),
        "AGENT_BUS_WATCH_POLL_SECONDS": "0.1",
        "CLAUDE_CODE_ENTRYPOINT": "cli",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "agent_bus.cli", "hook-watch", "--client", "claude",
         "--max-seconds", str(max_seconds)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, text=True,
    )
    proc.stdin.write(PAYLOAD)
    proc.stdin.close()
    return proc


def _wait_for_route(store, timeout: float = 10.0) -> None:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if WakeRoutes(store).covering(["alpha"])["alpha"]:
            return
        time.sleep(0.05)
    raise AssertionError("the watcher never registered")


def test_the_command_wakes_for_mail_that_arrives(two_agents, bus_paths):
    proc = _spawn_watch(bus_paths, max_seconds=30)
    _wait_for_route(two_agents)
    two_agents.send_message(from_agent="beta", to="alpha", body="from another process")
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == WAKE_EXIT_CODE
    assert "from another process" in err


def test_a_stop_signal_ends_the_watch_quietly(two_agents, bus_paths):
    import signal

    proc = _spawn_watch(bus_paths, max_seconds=30)
    _wait_for_route(two_agents)
    proc.send_signal(signal.SIGTERM)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == 0
    assert err == ""
    assert WakeRoutes(two_agents).covering(["alpha"]) == {"alpha": []}


def test_a_wake_json_entry_for_the_session_itself_counts(storage, fake_procs, monkeypatch):
    storage.upsert_agent("peer/codex", "/repo/peer")
    name, client_proc = _session(storage, fake_procs, key="a")
    wake.save_wake_config({name: False})
    storage.send_message(from_agent="peer/codex", to=name, body="hush")

    fake = FakeTime()
    rc, _ = _watch_as(storage, monkeypatch, client_proc, fake, key="a")
    assert rc == 0
    assert fake.sleeps == 0


@pytest.mark.parametrize("entrypoint", ["sdk-cli", "sdk-py", None])
def test_a_non_interactive_claude_code_session_is_not_watched(
    storage, fake_procs, monkeypatch, entrypoint
):
    """`claude -p` waits for a background hook in the foreground: a watcher
    there would hold the run until mail arrived."""
    storage.upsert_agent("peer/codex", "/repo/peer")
    name, client_proc = _session(storage, fake_procs, key="a")
    storage.send_message(from_agent="peer/codex", to=name, body="not for a script")
    monkeypatch.setenv("AGENT_BUS_NAME", SHARED)
    monkeypatch.setattr(procs, "ancestry", lambda pid=None: [client_proc])
    if entrypoint is None:
        monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    else:
        monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", entrypoint)

    fake = FakeTime()
    payload = json.dumps({"session_id": "a", "hook_event_name": "Stop"})
    rc, err = _watch(storage, fake, payload=payload, client="claude")

    assert (rc, err, fake.sleeps) == (0, "", 0)
    assert storage.pending_count(agent=name) == 1


@pytest.mark.skipif(not hasattr(__import__("signal"), "pthread_sigmask"), reason="POSIX only")
def test_stop_signals_are_let_through_again_when_the_mail_was_gone(alpha, monkeypatch):
    """Another hook can take the mail between the watcher's look and its
    drain. With nothing taken, a stop signal must work again — not wait
    for the watcher to give up."""
    import signal

    from agent_bus import hooks

    alpha.send_message(from_agent="beta", to="alpha", body="someone else's now")
    monkeypatch.setattr(hooks, "drain", lambda *args, **kwargs: [])
    masks: list[set] = []
    fake = FakeTime(on_sleep=lambda n: masks.append(signal.pthread_sigmask(signal.SIG_BLOCK, [])))
    saved = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGHUP)}
    try:
        err = io.StringIO()
        rc = run_hook_watch(
            storage=alpha, stdin=io.StringIO(PAYLOAD), stderr=err, max_seconds=5,
            handle_signals=True, sleep=fake.sleep, clock=fake.clock,
        )
    finally:
        for sig, handler in saved.items():
            signal.signal(sig, handler)
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM, signal.SIGHUP})
    assert rc == 0
    assert masks and all(signal.SIGTERM not in mask for mask in masks)


def test_a_session_entry_in_wake_json_counts_once_the_session_is_found(
    storage, fake_procs, monkeypatch
):
    """The watcher began on the shared address; the session's own entry
    must still decide once the watcher moves to the session's address."""
    from agent_bus.sessions import SessionRegistry

    storage.upsert_agent("peer/codex", "/repo/peer")
    storage.upsert_agent(SHARED, "/repo/shared")
    client_proc = fake_procs.proc("claude")
    wake.save_wake_config({f"{SHARED}-1": False})

    def server_starts_then_mail_arrives(n):
        if n == 1:
            name = SessionRegistry(storage).claim(
                client_address=SHARED, repo_path="/repo/shared",
                server=fake_procs.proc("agent-bus"), lineage=[client_proc], session_key="a",
            ).name
            assert name == f"{SHARED}-1"
            storage.send_message(from_agent="peer/codex", to=name, body="hush")

    fake = FakeTime(on_sleep=server_starts_then_mail_arrives)
    rc, err = _watch_as(storage, monkeypatch, client_proc, fake, key="a")
    assert (rc, err) == (0, "")
    assert storage.pending_count(agent=f"{SHARED}-1") == 1

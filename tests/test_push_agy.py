"""Waking an idle agy session from the sender's side (`agy agentapi
send-message`), with the credentials its agent registers."""

from __future__ import annotations

import io
import json
import stat
import time
from pathlib import Path

import pytest

from agent_bus import audit, procs, push_credentials
from agent_bus.clients import agy
from agent_bus.clients.base import PushCredentials
from agent_bus.hooks import run_hook_stop, run_hook_user_prompt, run_wake_register
from agent_bus.routes import DRIVER_PUSH, STATE_IDLE, STATE_PUSHED, WakeRoutes

AGY = "repo/agy"
THREAD = "c0ffee00-0000-4000-8000-000000000001"
ENDPOINT = "127.0.0.1:41000"
TOKEN = "test-csrf-token-value"
AGENT_BUS = "/opt/agent-bus/bin/agent-bus"


@pytest.fixture
def agy_session(storage, fake_procs, monkeypatch, tmp_path):
    """An agy session in `repo` whose hooks run under a stand-in client
    process, and a stand-in `agy` that records its arguments and the API
    credentials it was given."""
    storage.upsert_agent(AGY, "/repo")
    storage.upsert_agent("peer/claude", "/peer")
    client = fake_procs.proc("agy")
    monkeypatch.setenv("AGENT_BUS_NAME", AGY)
    monkeypatch.setenv("AGENT_BUS_BIN", AGENT_BUS)
    monkeypatch.setattr(procs, "ancestry", lambda pid=None: [client])
    monkeypatch.setattr(procs, "executable", lambda pid: "/opt/agy/bin/agy")

    calls = tmp_path / "agy-calls"
    fake = tmp_path / "agy"
    fake.write_text(
        "#!/bin/sh\n"
        f'{{ printf "%s\\n" "$@"; echo "address=$ANTIGRAVITY_LS_ADDRESS"; '
        f'echo "token=$ANTIGRAVITY_CSRF_TOKEN"; }} > "{calls}"\n'
    )
    fake.chmod(0o755)
    monkeypatch.setenv("AGENT_BUS_AGY_BIN", str(fake))
    return {"store": storage, "client": client, "calls": calls, "fake_procs": fake_procs,
            "monkeypatch": monkeypatch}


def _hook(store, *, stop: bool, thread: str = THREAD) -> dict:
    out = io.StringIO()
    payload = json.dumps({"conversationId": thread, "workspacePaths": ["/repo"]})
    run = run_hook_stop if stop else run_hook_user_prompt
    run(storage=store, stdin=io.StringIO(payload), stdout=out, client="agy")
    return json.loads(out.getvalue())


def _injected(output: dict) -> str:
    return "\n".join(step["ephemeralMessage"] for step in output["injectSteps"])


def _agents_env(thread: str = THREAD, secret: str = TOKEN) -> dict:
    """What agy puts in the environment of a command its agent runs."""
    return {
        agy.CONVERSATION_ENV: thread,
        agy.API_ADDRESS_ENV: ENDPOINT,
        agy.API_TOKEN_ENV: secret,
    }


def _register(store, env: dict | None = None) -> tuple[int, str]:
    out = io.StringIO()
    code = run_wake_register(
        storage=store, env=_agents_env() if env is None else env, stdout=out, client="agy"
    )
    return code, out.getvalue()


def _route(store):
    routes = WakeRoutes(store).covering([AGY])[AGY]
    return routes[0] if routes else None


def _sent(calls: Path, timeout: float = 5.0) -> list[str] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if calls.exists() and calls.read_text().endswith("\n") and "token=" in calls.read_text():
            return calls.read_text().splitlines()
        time.sleep(0.02)
    return None


def _wake_rows() -> list[dict]:
    return [r for r in audit.tail(500) if r["op"] == "wake"]


def _registered_and_idle(session) -> None:
    store = session["store"]
    _hook(store, stop=False)
    assert _register(store)[0] == 0
    _hook(store, stop=True)


# ----------------------------- the hook asks the agent ---------------------


def test_the_first_model_call_asks_the_agent_to_register(agy_session):
    store = agy_session["store"]
    first = _injected(_hook(store, stop=False))
    assert f"{AGENT_BUS} wake-register --client agy" in first

    assert _hook(store, stop=False) == {"injectSteps": []}  # asked once


def test_a_session_in_a_new_client_process_is_asked_again(agy_session):
    store = agy_session["store"]
    _hook(store, stop=False)
    resumed = agy_session["fake_procs"].proc("agy")
    agy_session["monkeypatch"].setattr(procs, "ancestry", lambda pid=None: [resumed])

    assert "wake-register" in _injected(_hook(store, stop=False))


def test_mail_and_the_request_arrive_together(agy_session):
    store = agy_session["store"]
    store.send_message(from_agent="peer/claude", to=AGY, body="hello there")

    (step,) = _hook(store, stop=False)["injectSteps"]
    assert "hello there" in step["ephemeralMessage"]
    assert "wake-register" in step["ephemeralMessage"]


def test_no_request_with_native_wake_off(agy_session, monkeypatch):
    monkeypatch.setenv("AGENT_BUS_NATIVE_WAKE", "0")
    assert _hook(agy_session["store"], stop=False) == {"injectSteps": []}
    assert _route(agy_session["store"]) is None


# ----------------------------- the agent registers -------------------------


def test_registering_keeps_the_credentials_where_only_this_user_can_read_them(
    agy_session, bus_paths
):
    store = agy_session["store"]
    _hook(store, stop=False)

    code, said = _register(store)

    assert code == 0 and "registered" in said
    assert TOKEN not in said
    (record,) = push_credentials.store_dir().glob("*.json")
    assert stat.S_IMODE(push_credentials.store_dir().stat().st_mode) == 0o700
    assert stat.S_IMODE(record.stat().st_mode) == 0o600
    assert THREAD not in record.name  # named after a hash of the session id
    # the database (with its WAL) and the audit log may be readable by others
    shared = [p for p in bus_paths["root"].iterdir() if p.is_file()]
    assert bus_paths["db"] in shared
    for path in shared:
        assert TOKEN.encode() not in path.read_bytes(), path.name


def test_a_store_directory_open_to_others_is_closed(agy_session):
    store = agy_session["store"]
    push_credentials.store_dir().mkdir(mode=0o777)
    push_credentials.store_dir().chmod(0o777)
    _hook(store, stop=False)
    _register(store)
    assert stat.S_IMODE(push_credentials.store_dir().stat().st_mode) == 0o700


def test_a_store_directory_that_is_a_link_is_refused(agy_session, tmp_path):
    store = agy_session["store"]
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    push_credentials.store_dir().symlink_to(elsewhere)

    assert _hook(store, stop=False) == {"injectSteps": []}  # not asked, not broken
    code, said = _register(store)

    assert code == 1 and "not a directory of this user's own" in said
    assert list(elsewhere.iterdir()) == []


def test_credentials_never_show_in_their_repr():
    shown = repr(PushCredentials(thread="t", endpoint=ENDPOINT, secret=TOKEN))
    assert TOKEN not in shown and ENDPOINT in shown


def test_registering_outside_the_agents_shell_keeps_nothing(agy_session):
    store = agy_session["store"]
    _hook(store, stop=False)
    code, said = _register(store, env={agy.CONVERSATION_ENV: THREAD})

    assert code == 1 and "shell command of the agent" in said
    assert list(push_credentials.store_dir().glob("*.json")) == [
        push_credentials._path(THREAD)
    ]  # only the note that the agent was asked
    assert push_credentials.for_route(_route(store)) is None


def test_registering_a_session_that_is_not_on_the_bus_keeps_nothing(agy_session):
    code, said = _register(agy_session["store"])
    assert code == 0 and "not on the bus" in said
    assert not push_credentials._path(THREAD).exists()


def test_a_client_that_needs_no_registration_is_refused(agy_session):
    out = io.StringIO()
    code = run_wake_register(
        storage=agy_session["store"], env=_agents_env(), stdout=out, client="codex"
    )
    assert code == 2 and "needs no wake registration" in out.getvalue()


def test_registering_does_not_ask_again(agy_session):
    store = agy_session["store"]
    _hook(store, stop=False)
    _register(store)
    assert _hook(store, stop=False) == {"injectSteps": []}


# ----------------------------- the sender pushes ---------------------------


def test_mail_for_a_registered_idle_session_starts_a_turn(agy_session):
    store = agy_session["store"]
    _registered_and_idle(agy_session)

    store.send_message(from_agent="peer/claude", to=AGY, body="wake up")

    sent = _sent(agy_session["calls"])
    assert sent == [
        "agentapi", "send-message", THREAD, agy.WAKE_NOTICE,
        f"address={ENDPOINT}", f"token={TOKEN}",
    ]
    assert _route(store).state == STATE_PUSHED
    (row,) = _wake_rows()
    assert (row["wake_status"], row["wake_driver"], row["to"]) == ("fired:OK", DRIVER_PUSH, AGY)
    assert TOKEN not in json.dumps(row)
    assert store.pending_count(agent=AGY) == 1  # the woken turn's hook delivers it


def test_an_unregistered_session_is_not_pushed_to_and_the_audit_says_why(agy_session):
    store = agy_session["store"]
    _hook(store, stop=False)
    _hook(store, stop=True)

    store.send_message(from_agent="peer/claude", to=AGY, body="anyone?")

    assert _sent(agy_session["calls"], timeout=0.5) is None
    assert _route(store).state == STATE_IDLE
    (row,) = _wake_rows()
    assert row["wake_status"] == "unregistered"


def test_credentials_from_an_earlier_client_process_are_not_used(agy_session):
    store = agy_session["store"]
    _registered_and_idle(agy_session)
    resumed = agy_session["fake_procs"].proc("agy")  # `agy --continue`
    agy_session["monkeypatch"].setattr(procs, "ancestry", lambda pid=None: [resumed])
    _hook(store, stop=False)
    _hook(store, stop=True)

    store.send_message(from_agent="peer/claude", to=AGY, body="stale?")

    assert _sent(agy_session["calls"], timeout=0.5) is None
    assert _wake_rows()[-1]["wake_status"] == "unregistered"


def test_a_busy_session_is_pushed_to_once_per_model_call(agy_session):
    """agy takes the message into the running turn, and ends a turn the
    user interrupts without its Stop hook — so "busy" may be stale."""
    store = agy_session["store"]
    _hook(store, stop=False)
    _register(store)

    store.send_message(from_agent="peer/claude", to=AGY, body="first")
    assert _sent(agy_session["calls"])
    agy_session["calls"].unlink()

    store.send_message(from_agent="peer/claude", to=AGY, body="second")
    assert _sent(agy_session["calls"], timeout=0.5) is None

    _hook(store, stop=False)  # the next model call
    store.send_message(from_agent="peer/claude", to=AGY, body="third")
    assert _sent(agy_session["calls"])


def test_registering_says_there_is_nothing_more_to_do(agy_session):
    store = agy_session["store"]
    _hook(store, stop=False)
    assert "Nothing else to do" in _register(store)[1]


def test_records_of_ended_client_processes_are_deleted(agy_session):
    store = agy_session["store"]
    _registered_and_idle(agy_session)
    agy_session["fake_procs"].kill(agy_session["client"])
    other = agy_session["fake_procs"].proc("agy")

    push_credentials.request_once("another-conversation", other)

    assert not push_credentials._path(THREAD).exists()
    assert push_credentials._path("another-conversation").exists()


def test_the_recorded_executable_is_used_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_BUS_AGY_BIN", raising=False)
    recorded = tmp_path / "agy-native"
    recorded.write_text("")
    assert agy.send_command(thread="t", executable=str(recorded))[0] == str(recorded)
    assert agy.send_command(thread="t", executable=str(tmp_path / "gone"))[0] == "agy"


def test_credentials_are_read_only_from_a_complete_environment():
    assert agy.api_credentials(_agents_env()) == PushCredentials(
        thread=THREAD, endpoint=ENDPOINT, secret=TOKEN
    )
    for missing in (agy.CONVERSATION_ENV, agy.API_ADDRESS_ENV, agy.API_TOKEN_ENV):
        env = _agents_env()
        env[missing] = " "
        assert agy.api_credentials(env) is None

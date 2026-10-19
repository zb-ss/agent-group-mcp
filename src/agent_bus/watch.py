"""`agent-bus hook-watch`: wake an idle session when mail arrives.

A client that can run a hook in the background and start a turn when it
finishes — Claude Code's `asyncRewake` hooks, the generated opencode
plugin — runs this when a session starts and at the end of every turn.
It polls the bus every `settings.watch_poll_seconds()` until there is
mail this session must act on, takes that mail from the inbox, prints it
on stderr and exits with WAKE_EXIT_CODE; the client turns that into a
new turn. It exits 0, waking nothing:

  * when a newer watcher of the same session has taken over (`routes`):
    every turn starts one, and only the newest may wake the session;
  * when the client process that started it is gone;
  * after `max_seconds`, before the client's own timeout would kill it;
  * at once when wake.json has an entry for the agent — a command there,
    or `false`, is the user's explicit choice, and the sender acts on it;
  * at once when the client cannot take a wake from this session
    (`clients.watch_allowed`): Claude Code waits for a background hook
    in the foreground when it runs non-interactively (`claude -p`), and
    would hang there until mail arrived.

Past `settings.wakes_per_hour()` wakes the mail is left for the agent's
next turn instead.

WAKE_EXIT_CODE is deliberately not 2, the code Claude Code wakes on: an
agent-bus too old to know `hook-watch` exits 2 from its argument parser,
and that usage error would wake the model at the end of every turn. The
hook command turns WAKE_EXIT_CODE into 2 itself (`clients.claude`).
"""

from __future__ import annotations

import os
import signal
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, TextIO

from . import audit, clients, hooks, identity, procs, settings, wake
from .clients.base import WAKE_EXIT_CODE, HookDialect
from .procs import Proc
from .routes import DRIVER_WATCH, WakeRoutes
from .storage import Message, Storage

# Telling whether the client is still the same process, and which address
# its session has now, costs a `ps` call on macOS. Both are checked this
# often rather than on every poll; between checks a signal-0 probe
# catches a client that has exited. Bounds cost, not wake latency.
RECHECK_SECONDS = 30.0
# A session's MCP server may claim its address only after the session's
# first hook has run. Until it has, the watcher listens at the client
# address and looks for the session's own on every poll, for this long.
SETTLE_SECONDS = 60.0
# The signals a client sends its hooks when it exits or gives up on them.
STOP_SIGNALS = tuple(
    s for s in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGHUP", None)) if s
)

WAKE_HEADER = "[agent-bus] {count} new message(s) from peers — handle them:"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass
class _Watch:
    store: Storage
    routes: WakeRoutes
    base: str  # the agent the payload resolves to, before narrowing to a session
    name: str  # the address being watched
    payload: dict
    dialect: HookDialect
    chain: list[Proc]
    owner: Proc | None
    me: Proc | None
    client: str
    route_key: str
    token: str
    hold_signals: bool
    said_limited: bool = False


def run_hook_watch(
    *,
    storage: Storage | None = None,
    stdin: TextIO | None = None,
    stderr: TextIO | None = None,
    client: str | None = None,
    max_seconds: float | None = None,
    handle_signals: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Wait for mail and wake the session; see the module docstring.
    `handle_signals` is for the CLI process only: it makes a stop signal
    end the watch quietly, and holds it off while mail is being handed
    over."""
    payload = hooks.read_payload(stdin or sys.stdin)
    if not settings.native_wake_enabled():
        return 0
    if handle_signals:
        for sig in STOP_SIGNALS:
            signal.signal(sig, _stop)
    watch = _start(storage or Storage(), payload, client, hold_signals=handle_signals)
    if watch is None:
        return 0
    try:
        # claimed inside the try, so that no stop signal leaves it behind
        watch.routes.claim_watch(
            route_key=watch.route_key, client=watch.client, address=watch.name,
            owner=watch.owner, watcher=watch.me, token=watch.token,
        )
        budget = max_seconds if max_seconds is not None else settings.watch_hours() * 3600
        return _wait(watch, out=stderr or sys.stderr, budget=budget, sleep=sleep, clock=clock)
    finally:
        watch.routes.release(watch.route_key, watch.token)


def _stop(_signum, _frame) -> None:
    raise SystemExit(0)


def _start(
    store: Storage, payload: dict, client: str | None, *, hold_signals: bool
) -> _Watch | None:
    base, dialect = hooks.resolve_agent(store, payload, client)
    if base is None:
        return None
    parsed = identity.parse_or_none(base)
    client_id = client or (parsed.client if parsed else None) or ""
    if not clients.watch_allowed(client_id, os.environ):
        return None
    chain = procs.ancestry()
    owner = procs.client_of(chain)
    name = hooks.session_of(store, base, payload, dialect, ancestry=chain)
    if _wake_json_decides(name):
        return None
    key = (
        dialect.session_from_payload(payload)
        or clients.session_key(client_id, os.environ)
        or (f"{client_id}:{owner.pid}:{owner.started}" if owner else name)
    )
    return _Watch(
        store=store, routes=WakeRoutes(store), base=base, name=name,
        payload=payload, dialect=dialect, chain=chain, owner=owner,
        me=procs.describe(os.getpid()), client=client_id,
        route_key=key, token=uuid.uuid4().hex, hold_signals=hold_signals,
    )


def _wake_json_decides(name: str) -> bool:
    """True when wake.json has an entry for `name`, its client or its
    repo — a command or an explicit `false`."""
    parsed = identity.parse_or_none(name)
    group = parsed.group if parsed and parsed.client else None
    _command, status = wake.wake_command(wake.load_wake_config(), name, group)
    return status != "no-config"


def _wait(
    watch: _Watch, *, out: TextIO, budget: float,
    sleep: Callable[[float], None], clock: Callable[[], float],
) -> int:
    started = clock()
    deadline = started + budget
    next_check = started + RECHECK_SECONDS
    while watch.routes.owns(watch.route_key, watch.token):
        now = clock()
        if now >= next_check:
            next_check = now + RECHECK_SECONDS
            if not _still_ours(watch):
                return 0
        elif not _owner_running(watch.owner):
            return 0
        elif not _is_session(watch.name) and now - started < SETTLE_SECONDS:
            if not _follow_address(watch):
                return 0
        if _wake_if_mail(watch, out):
            return WAKE_EXIT_CODE
        remaining = deadline - clock()
        if remaining <= 0:
            return 0
        sleep(min(settings.watch_poll_seconds(), remaining))
    return 0  # a newer watcher took over


def _is_session(name: str) -> bool:
    parsed = identity.parse_or_none(name)
    return parsed is not None and parsed.is_session


def _owner_running(owner: Proc | None) -> bool:
    """Cheap probe between full checks: is the client's pid still there?"""
    if owner is None:
        return True
    try:
        os.kill(owner.pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # it exists; we may just not be allowed to signal it
    return True


def _still_ours(watch: _Watch) -> bool:
    """The full check: the client is the same process it was (not a
    reused pid), and the watched address is still the session's."""
    if watch.owner is not None and watch.owner in procs.gone([watch.owner]):
        return False
    return _follow_address(watch)


def _follow_address(watch: _Watch) -> bool:
    """Pick up the session's current address: its server may have claimed
    one since the watch began, or the session may have been renamed.
    False when wake.json has an entry for the new address, which then
    decides instead."""
    current = hooks.session_of(
        watch.store, watch.base, watch.payload, watch.dialect, ancestry=watch.chain
    )
    if current == watch.name:
        return True
    if _wake_json_decides(current):
        return False
    watch.name = current
    watch.routes.move(watch.route_key, watch.token, current)
    return True


def _wake_if_mail(watch: _Watch, out: TextIO) -> bool:
    if not watch.store.pending_count(agent=watch.name, actionable_only=True):
        return False
    if not watch.routes.may_wake(watch.name):
        if not watch.said_limited:
            watch.said_limited = True
            _audit_limited(watch)
        return False
    holding = watch.hold_signals and hasattr(signal, "pthread_sigmask")
    if holding:
        # From here on the mail is out of the inbox: a stop signal now
        # would lose it, so it waits until the process has exited.
        signal.pthread_sigmask(signal.SIG_BLOCK, set(STOP_SIGNALS))
    msgs = hooks.drain(watch.store, watch.name, actionable_only=True)
    if not msgs:
        # another session or hook took it first: nothing to protect, and
        # a stop that came meanwhile is delivered now
        if holding:
            signal.pthread_sigmask(signal.SIG_UNBLOCK, set(STOP_SIGNALS))
        return False
    out.write(_wake_text(msgs, watch.name))
    out.flush()
    watch.routes.record_wake(watch.name, DRIVER_WATCH)
    _audit_woke(watch, msgs)
    return True


def _wake_text(msgs: list[Message], reader: str) -> str:
    header = WAKE_HEADER.format(count=len(msgs))
    return header + "\n" + hooks.format_messages(msgs, reader) + "\n"


def _audit_woke(watch: _Watch, msgs: list[Message]) -> None:
    audit.append_many(
        {
            "ts": _utc_now_iso(),
            "op": "wake",
            "actor": watch.name,
            "message_id": m.message_id,
            "from": m.from_agent,
            "to": m.to_agent,
            "thread_id": m.thread_id,
            "body_preview": audit.body_preview(m.body),
            "body_sha256": audit.body_sha256(m.body),
            "wake_status": "woke",
            "wake_driver": DRIVER_WATCH,
        }
        for m in msgs
    )


def _audit_limited(watch: _Watch) -> None:
    audit.append_many([{
        "ts": _utc_now_iso(),
        "op": "wake",
        "actor": watch.name,
        "message_id": None,
        "from": None,
        "to": watch.name,
        "thread_id": None,
        "body_preview": (
            f"not woken: {settings.wakes_per_hour()} wakes in the last hour; "
            "mail waits for the next turn"
        ),
        "body_sha256": None,
        "wake_status": "rate-limited",
        "wake_driver": DRIVER_WATCH,
    }])

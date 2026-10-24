"""Waking an idle session from the sender's side.

A client that offers a command to start a turn in one of its sessions
(`clients.push_command`: `codex queue` for Codex, `agy agentapi
send-message` for agy) is woken by whoever sends it mail. The command runs
detached, like a wake.json command, so a send never waits on it — and only
for a session that can take it: one whose client is still running, that is
idle (a busy session gets its mail from its own hooks; agy is the exception,
`clients.pushed_while_busy`), and that has not already been woken
`settings.wakes_per_hour()` times in the last hour.

agy's command also needs credentials that agy gives only to its agent, so
an agy session is pushed to only once its agent has registered them
(`push_credentials.py`). They reach the command through its environment,
never its arguments, which other users of the machine can see.

A queued turn whose command failed after it started is not noticed here;
the session then stays marked as pushed to, and is not pushed to again
until its next hook — exactly when it is next active.
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from . import audit, clients, push_credentials
from .routes import DRIVER_PUSH, Route

if TYPE_CHECKING:  # routes are built over storage, which calls this module
    from .routes import WakeRoutes

STATUS_RATE_LIMITED = "rate-limited"
# an agy session whose agent has not registered its credentials yet
STATUS_UNREGISTERED = "unregistered"


def push_wake(
    routes: "WakeRoutes", recipient: str, candidates: tuple[Route, ...]
) -> tuple[bool, str]:
    """Start a turn in the most recently active of `candidates` that is
    still running, for mail sent to `recipient`.

    Returns ``(fired, status)`` as `wake.fire_wake` does: ``"fired:OK"``
    once the command is launched, ``"fired:ERR:…"`` when it could not be,
    STATUS_RATE_LIMITED, ``"busy"`` for a session that will see the mail
    anyway, ``"no-session"`` when no candidate's client is running, and
    STATUS_UNREGISTERED for a session whose credentials are missing.
    """
    route = next((r for r in candidates if routes.alive(r)), None)
    if route is None:
        return False, "no-session"
    env = _push_env(route)
    if env is None:
        return False, STATUS_UNREGISTERED
    if not routes.may_wake(route.address):
        return False, STATUS_RATE_LIMITED
    if not routes.take_idle(route, busy_too=clients.pushed_while_busy(route.client)):
        return False, "busy"
    argv = clients.push_command(
        route.client, thread=route.route_key, executable=route.command
    )
    try:
        subprocess.Popen(
            argv,
            env=env or None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,  # detach from the sender's process group
        )
    except (OSError, ValueError) as e:
        routes.put_back(route)
        return False, f"fired:ERR:{type(e).__name__}"
    routes.record_wake(route.address, DRIVER_PUSH)
    return True, "fired:OK"


def _push_env(route: Route) -> dict[str, str] | None:
    """The environment the push command for `route` needs: `{}` for the
    sender's own, None when it needs credentials nobody registered."""
    registration = clients.push_registration(route.client)
    if registration is None:
        return {}
    credentials = push_credentials.for_route(route)
    if credentials is None:
        return None
    return {**os.environ, **registration.to_env(credentials)}


def audit_push(address: str, status: str, *, note: str) -> None:
    """An `op="wake"` row for a push that no single send asked for."""
    audit.append_many([{
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "op": "wake",
        "actor": address,
        "message_id": None,
        "from": None,
        "to": address,
        "thread_id": None,
        "body_preview": note,
        "body_sha256": None,
        "wake_status": status,
        "wake_driver": DRIVER_PUSH,
    }])

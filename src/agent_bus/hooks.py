"""Hook handlers: surface pending mail at the two turn boundaries.

Exposed as `agent-bus hook-user-prompt` (before the model sees a prompt)
and `agent-bus hook-stop` (when the agent is about to stop). Whose inbox to
drain comes from `resolution.resolve` — $AGENT_BUS_NAME, or `--client` plus
the repo the hook payload reports. What to print is the client's business
(`clients.hook_dialect`); draining and formatting are shared. Each delivered
message gets a sibling `deliver` audit row so the log shows the hook path.
"""

from __future__ import annotations

import os
import select
import sys
import time
from typing import Iterable, TextIO

from . import clients, identity, resolution, settings
from .clients.base import HookDialect
from .identity import KIND_BROADCAST, KIND_GROUP
from .storage import Message, Storage


def _read_payload(stdin: TextIO) -> dict:
    """Consume the hook payload. Always read it, even when the name comes
    from the environment, so the client's pipe never blocks."""
    try:
        return resolution.parse_hook_payload(_read_available(stdin))
    except Exception:
        # Hook input is best-effort; never block on a bad pipe.
        return {}


def _read_available(stdin: TextIO) -> str:
    """Whatever payload is actually coming, and never more than a moment's wait.

    A hook runs inside the agent's turn: a client waits for it before making
    its next model request. Reading to end-of-input is therefore only safe
    when end-of-input is certain to arrive. It is not when the hook inherited
    a terminal, which never sends one, and not when it inherited a pipe the
    client keeps open — either way `read()` would hold the turn open forever.

    So: nothing at all from a terminal, and otherwise only what arrives
    within `settings.hook_payload_timeout_seconds()`. Losing a slow payload
    costs at worst the repo hint, which resolution recovers from the working
    directory; hanging the agent costs the whole session.
    """
    try:
        if stdin.isatty():
            return ""
    except (AttributeError, ValueError, OSError):
        pass

    fd = None
    if os.name == "posix":  # select() on a pipe is POSIX-only
        try:
            fd = stdin.fileno()
        except (AttributeError, ValueError, OSError):
            fd = None
    if fd is None:
        return stdin.read()  # an in-memory stream, as the tests use

    deadline = time.monotonic() + settings.hook_payload_timeout_seconds()
    chunks: list[bytes] = []
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            ready, _, _ = select.select([fd], [], [], remaining)
        except (OSError, ValueError):
            break
        if not ready:
            break  # nobody is writing; take what we have
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            break
        if not chunk:
            break  # end of input, the normal case
        chunks.append(chunk)
    return b"".join(chunks).decode("utf-8", "replace")


def _client_of(name: str | None) -> str | None:
    parsed = identity.parse_or_none(name) if name else None
    return parsed.client if parsed else None


def _identify(
    store: Storage, payload: dict, client: str | None, actor: str | None = None
) -> tuple[str | None, HookDialect]:
    """(agent name, dialect). The name is None when this client has no
    agent in the payload's repo — a user-wide hook firing in a repo that is
    not on the bus, which is not an error. `actor` names the agent outright
    and skips resolution."""
    if actor:
        return actor, clients.hook_dialect(client or _client_of(actor))
    client = client or os.environ.get(resolution.CLIENT_ENV)
    explicit = os.environ.get(resolution.NAME_ENV)
    dialect = clients.hook_dialect(client or _client_of(explicit))
    try:
        who = resolution.resolve(
            storage=store,
            client=client,
            repo_hint=dialect.repo_from_payload(payload),
            registered_only=True,
        )
    except resolution.IdentityError as e:
        sys.stderr.write(f"agent-bus hook: {e}\n")
        raise SystemExit(2) from None
    return (who.name if who else None), dialect


GROUP_LEGEND = (
    "(A message to everyone in a repo is handled by the first agent there "
    "to see it. You saw these first, so they are yours.)"
)
FYI_HEADER = (
    "For your information only — a repo-mate already has these; act only "
    "if one concerns your own work:"
)


def _audience(m: Message) -> str:
    if m.kind == KIND_GROUP:
        return f" to everyone in {m.addressed_to}"
    if m.kind == KIND_BROADCAST:
        return " to everyone on the bus"
    return ""


def _format_message(m: Message) -> str:
    return (
        f"- from {m.from_agent}{_audience(m)} at {m.sent_at} "
        f"(thread {m.thread_id}): {m.body}"
    )


def _format_messages(msgs: Iterable[Message], reader: str) -> str:
    """Messages the reader must handle first, then the ones a repo-mate
    already claimed."""
    mine: list[Message] = []
    taken: list[Message] = []
    for m in msgs:
        (taken if m.is_claimed_by_other(reader) else mine).append(m)

    lines = [_format_message(m) for m in mine]
    if any(m.kind == KIND_GROUP for m in mine):
        lines.append(GROUP_LEGEND)
    if taken:
        lines.append(FYI_HEADER)
        lines.extend(
            f"{_format_message(m)} [already picked up by {m.claimed_by}]"
            for m in taken
        )
    return "\n".join(lines)


def _drain(store: Storage, name: str, *, actionable_only: bool = False) -> list[Message]:
    # a hook firing is a sign of life: some clients never call an MCP tool
    # between turns, and fan-out skips agents that look long gone
    store.touch_agent(name)
    return store.read_inbox(
        agent=name, mark_read=True, actor=name, also_deliver=True,
        actionable_only=actionable_only,
    )


def run_hook_user_prompt(
    *,
    storage: Storage | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    actor: str | None = None,
    client: str | None = None,
) -> int:
    """Surface pending peer messages as extra context for the next turn.

    Exit code is always 0 — this hook must never block a user submission.
    """
    store = storage or Storage()
    out = stdout or sys.stdout
    name, dialect = _identify(store, _read_payload(stdin or sys.stdin), client, actor)

    msgs = _drain(store, name) if name else []
    context = None
    if msgs:
        header = f"[agent-bus] {len(msgs)} new message(s) since last turn:"
        context = header + "\n" + _format_messages(msgs, name)
    out.write(dialect.prompt_output(context))
    out.flush()
    return 0


def run_hook_stop(
    *,
    storage: Storage | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    actor: str | None = None,
    client: str | None = None,
) -> int:
    """Keep the agent going if it has mail it must act on.

    Nothing to act on → the client is told to stop normally (for most
    clients: no output at all). Otherwise the messages become the reason
    the turn stays open.
    """
    store = storage or Storage()
    out = stdout or sys.stdout
    name, dialect = _identify(store, _read_payload(stdin or sys.stdin), client, actor)

    # only what this agent must act on: a copy a repo-mate already claimed
    # must not keep a second agent's turn open
    msgs = _drain(store, name, actionable_only=True) if name else []
    reason = None
    if msgs:
        reason = (
            "Pending messages from peers — handle them before stopping:\n"
            + _format_messages(msgs, name)
        )
    out.write(dialect.stop_output(reason))
    out.flush()
    return 0

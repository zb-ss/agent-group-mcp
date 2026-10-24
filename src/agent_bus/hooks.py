"""Hook handlers: surface pending mail at the two turn boundaries.

Exposed as `agent-bus hook-user-prompt` (before the model sees a prompt)
and `agent-bus hook-stop` (when the agent is about to stop). Whose inbox to
drain comes from `resolution.resolve` — $AGENT_BUS_NAME, or `--client` plus
the repo the hook payload reports — narrowed to the session this hook runs
for (`sessions.SessionRegistry.for_hook`). A hook that cannot tell which
session it belongs to drains only the address the client's sessions share,
never another session's own mail. What to print is the client's business
(`clients.hook_dialect`); draining and formatting are shared. Each delivered
message gets a sibling `deliver` audit row so the log shows the hook path.

For a client whose idle sessions are woken by their sender (Codex, agy),
the hooks also keep that session's wake route current: busy from the start
of a turn, idle once it ends with nothing to act on (`routes.py`). agy's
sender also needs credentials that agy gives only to its agent: the
prompt hook asks the agent once to run `agent-bus wake-register`
(`run_wake_register`), which keeps them for senders.
"""

from __future__ import annotations

import os
import select
import shlex
import sqlite3
import sys
import time
from dataclasses import dataclass
from typing import Iterable, Mapping, TextIO

from . import clients, identity, procs, push, push_credentials, resolution, settings
from .clients.base import HookDialect
from .identity import KIND_BROADCAST, KIND_GROUP
from .init_cmd import detect_agent_bus_bin
from .procs import Proc
from .routes import WakeRoutes
from .sessions import SessionRegistry
from .storage import Message, Storage


def read_payload(stdin: TextIO) -> dict:
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
    store: Storage, payload: dict, client: str | None, actor: str | None = None,
    *, ancestry: list[Proc] | None = None,
) -> tuple[str | None, HookDialect]:
    """(agent name, dialect). The name is None when this client has no
    agent in the payload's repo — a user-wide hook firing in a repo that is
    not on the bus, which is not an error. `actor` names the agent outright
    and skips resolution."""
    if actor:
        return actor, clients.hook_dialect(client or _client_of(actor))
    name, dialect = resolve_agent(store, payload, client)
    if name is None:
        return None, dialect
    return session_of(store, name, payload, dialect, ancestry=ancestry), dialect


@dataclass(frozen=True)
class _Turn:
    """One hook run: who it is for, and what the client said about it."""

    name: str | None
    dialect: HookDialect
    payload: dict
    client: str | None
    # this process's ancestry, looked up only for a client whose sessions
    # are woken by their sender (see `_note_route`)
    ancestry: list[Proc] | None


def _wake_for_late_mail(store: Storage, turn: _Turn) -> None:
    """Mail sent between this hook's drain and its marking the session
    idle found the session busy, so nobody queued a turn for it. Queue
    one now; a sender that saw the session idle already has, and then
    this finds the route taken."""
    if turn.ancestry is None or turn.name is None:
        return
    thread = turn.dialect.session_from_payload(turn.payload)
    try:
        if not thread or not store.pending_count(agent=turn.name, actionable_only=True):
            return
        routes = WakeRoutes(store)
        mine = tuple(
            r for r in routes.covering([turn.name])[turn.name] if r.route_key == thread
        )
        if mine:
            fired, status = push.push_wake(routes, turn.name, mine)
            if fired or status.startswith("fired:"):
                push.audit_push(turn.name, status, note="mail arrived as the turn ended")
    except sqlite3.Error as e:
        sys.stderr.write(f"agent-bus hook: could not wake this session for late mail: {e}\n")


def _begin(
    store: Storage, stdin: TextIO | None, client: str | None, actor: str | None
) -> _Turn:
    payload = read_payload(stdin or sys.stdin)
    client_id = (
        client or os.environ.get(resolution.CLIENT_ENV)
        or _client_of(actor or os.environ.get(resolution.NAME_ENV))
    )
    pushed_to = clients.can_push(client_id) and settings.native_wake_enabled()
    ancestry = procs.ancestry() if pushed_to else None
    name, dialect = _identify(store, payload, client, actor, ancestry=ancestry)
    return _Turn(name, dialect, payload, client_id, ancestry)


def _note_route(store: Storage, turn: _Turn, *, busy: bool) -> None:
    """For a client whose idle sessions the sender wakes: how to reach
    this session, and whether it is busy now. Best-effort — a hook that
    has delivered its mail must not fail over bookkeeping."""
    if turn.ancestry is None or turn.name is None or turn.client is None:
        return
    thread = turn.dialect.session_from_payload(turn.payload)
    if not thread:
        return
    owner = procs.client_of(turn.ancestry)
    try:
        WakeRoutes(store).note_push(
            route_key=thread, client=turn.client, address=turn.name, owner=owner,
            command=procs.executable(owner.pid) if owner else None, busy=busy,
        )
    except sqlite3.Error as e:
        sys.stderr.write(f"agent-bus hook: could not record how to wake this session: {e}\n")


def _registration_request(turn: _Turn) -> str | None:
    """For a client that gives the credentials to wake a session only to
    its agent's commands (agy): what to ask the agent so that it registers
    them — once per conversation and client process. Best-effort, like
    `_note_route`."""
    registration = clients.push_registration(turn.client)
    if registration is None or turn.ancestry is None or turn.name is None:
        return None
    thread = turn.dialect.session_from_payload(turn.payload)
    if not thread:
        return None
    try:
        if not push_credentials.request_once(thread, procs.client_of(turn.ancestry)):
            return None
    except OSError as e:
        sys.stderr.write(f"agent-bus hook: could not ask for the wake registration: {e}\n")
        return None
    command = f"{shlex.quote(_own_command())} wake-register --client {turn.client}"
    return registration.request.format(command=command)


def _own_command() -> str:
    """This agent-bus, by the path the client runs its hooks with: the
    same command in every repo, so that allowing it once allows it
    everywhere."""
    argv0 = sys.argv[0] if sys.argv else ""
    if os.path.isabs(argv0) and os.path.basename(argv0) == "agent-bus":
        return argv0
    return detect_agent_bus_bin()


def resolve_agent(
    store: Storage, payload: dict, client: str | None
) -> tuple[str | None, HookDialect]:
    """(agent name, dialect) for the payload's repo, before narrowing it
    to the session the hook runs for (`session_of`)."""
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


def session_of(
    store: Storage, name: str, payload: dict, dialect: HookDialect,
    *, ancestry: list[Proc] | None = None,
) -> str:
    """The session address this hook runs for, when this one can be told;
    otherwise the client address all its sessions share. Never a session
    address merely asked for (`AGENT_BUS_SESSION`): another session may
    hold it. `ancestry` saves looking this process's up again."""
    parsed = identity.parse_or_none(name)
    if not settings.sessions_enabled() or parsed is None or parsed.client_address is None:
        return name
    key = dialect.session_from_payload(payload) or clients.session_key(
        parsed.client, os.environ
    )
    found = SessionRegistry(store).for_hook(
        parsed.client_address, session_key=key, ancestry=ancestry
    )
    return found or parsed.client_address


GROUP_LEGEND = (
    "(A message to everyone in a repo is handled by the first agent there "
    "to see it. You saw these first, so they are yours.)"
)
FYI_HEADER = (
    "For your information only — a repo-mate already has these; act only "
    "if one concerns your own work:"
)


def _audience(m: Message, reader: str) -> str:
    if m.kind == KIND_GROUP:
        return f" to everyone in {m.addressed_to}"
    if m.kind == KIND_BROADCAST:
        return " to everyone on the bus"
    parsed = identity.parse_or_none(reader)
    if parsed is not None and parsed.is_session and m.to_agent == parsed.client_address:
        return f" to any {m.to_agent} session"
    return ""


def _format_message(m: Message, reader: str) -> str:
    return (
        f"- from {m.from_agent}{_audience(m, reader)} at {m.sent_at} "
        f"(thread {m.thread_id}): {m.body}"
    )


def format_messages(msgs: Iterable[Message], reader: str) -> str:
    """Messages the reader must handle first, then the ones a repo-mate
    already claimed."""
    mine: list[Message] = []
    taken: list[Message] = []
    for m in msgs:
        (taken if m.is_claimed_by_other(reader) else mine).append(m)

    lines = [_format_message(m, reader) for m in mine]
    if any(m.kind == KIND_GROUP for m in mine):
        lines.append(GROUP_LEGEND)
    if taken:
        lines.append(FYI_HEADER)
        lines.extend(
            f"{_format_message(m, reader)} [already picked up by {m.claimed_by}]"
            for m in taken
        )
    return "\n".join(lines)


def drain(store: Storage, name: str, *, actionable_only: bool = False) -> list[Message]:
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
    turn = _begin(store, stdin, client, actor)
    name = turn.name
    # busy before the drain: mail sent after it reaches this turn's end,
    # rather than queueing another turn
    _note_route(store, turn, busy=True)

    msgs = drain(store, name) if name else []
    context = None
    if msgs:
        header = f"[agent-bus] {len(msgs)} new message(s) since last turn:"
        context = header + "\n" + format_messages(msgs, name)
    request = _registration_request(turn)
    if request:
        context = f"{context}\n\n{request}" if context else request
    out.write(turn.dialect.prompt_output(context))
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
    turn = _begin(store, stdin, client, actor)
    name = turn.name

    # only what this agent must act on: a copy a repo-mate already claimed
    # must not keep a second agent's turn open
    msgs = drain(store, name, actionable_only=True) if name else []
    reason = None
    if msgs:
        reason = (
            "Pending messages from peers — handle them before stopping:\n"
            + format_messages(msgs, name)
        )
    out.write(turn.dialect.stop_output(reason))
    out.flush()
    # the turn goes on when there was mail; otherwise the session is idle
    _note_route(store, turn, busy=reason is not None)
    if reason is None:
        _wake_for_late_mail(store, turn)
    return 0


def run_wake_register(
    *,
    storage: Storage | None = None,
    env: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    client: str | None = None,
) -> int:
    """`agent-bus wake-register`, run by the agent itself when the prompt
    hook asks it to: keep the credentials its client gives only to the
    agent's commands, so that senders can wake the session
    (`push_credentials.py`). Prints nothing secret.

    Exit code 0 also when there is nothing to keep (the session is not on
    the bus, native wake is off); 1 when the credentials are missing from
    the environment; 2 for a client that needs no registration.
    """
    out = stdout or sys.stdout
    env = os.environ if env is None else env
    client = client or env.get(resolution.CLIENT_ENV)
    registration = clients.push_registration(client)
    if registration is None:
        out.write(f"agent-bus: {client or 'this client'} needs no wake registration.\n")
        return 2
    credentials = registration.from_env(env)
    if credentials is None:
        out.write(
            "agent-bus: nothing to register here: run this as a shell command "
            "of the agent itself.\n"
        )
        return 1
    if not settings.native_wake_enabled():
        out.write(f"agent-bus: native wake is off ({settings.NATIVE_WAKE_ENV}); nothing kept.\n")
        return 0
    try:
        route = WakeRoutes(storage or Storage()).get(credentials.thread)
        if route is None or route.client != client:
            out.write("agent-bus: this session is not on the bus; nothing kept.\n")
            return 0
        push_credentials.register(
            credentials.thread, route.process, credentials.endpoint, credentials.secret
        )
    except (OSError, sqlite3.Error) as e:
        out.write(f"agent-bus: could not keep the wake registration: {e}\n")
        return 1
    out.write(
        "agent-bus: registered for the rest of this conversation; do not run "
        "this again. Nothing else to do; carry on.\n"
    )
    return 0

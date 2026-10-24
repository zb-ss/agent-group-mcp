"""Per-client adapters.

The core (storage, server, hooks) knows nothing about any particular MCP
client; what differs between clients is looked up here by client id.
Supporting another client means adding a module and its registry lines —
and a client that follows the standard hook contract needs no dialect.
"""

from __future__ import annotations

from typing import Callable, Mapping

from . import agy, claude, codex, opencode
from .base import ClientWiring, HookDialect, PushRegistration, StandardHookDialect

_HOOK_DIALECTS: dict[str, HookDialect] = {
    agy.CLIENT_ID: agy.AgyHookDialect(),
    codex.CLIENT_ID: codex.CodexHookDialect(),
}
_STANDARD = StandardHookDialect()

# Where a client tells the processes it starts which of its sessions they
# serve. Codex publishes this to shells/hooks; its MCP calls use _meta below.
_SESSION_KEY_ENV: dict[str, str] = {
    claude.CLIENT_ID: claude.SESSION_KEY_ENV,
    codex.CLIENT_ID: codex.SESSION_KEY_ENV,
}

# These clients multiplex threads in one process. Ancestry cannot identify
# a conversation, even when only one of its servers is currently registered.
_SESSION_KEY_META: dict[str, str] = {
    codex.CLIENT_ID: codex.SESSION_KEY_META,
}

# Clients whose idle sessions the sender wakes, by running a command that
# starts a turn: (thread id, executable the session runs) → argv.
_PUSH_COMMANDS: dict[str, Callable[..., list[str]]] = {
    agy.CLIENT_ID: agy.send_command,
    codex.CLIENT_ID: codex.queue_command,
}

# Of those, the clients whose busy sessions are pushed to as well: the
# message joins the running turn rather than starting an empty one after it.
_PUSHED_WHILE_BUSY: frozenset[str] = frozenset({agy.CLIENT_ID})

# Of those, the clients whose command also needs credentials that only the
# commands their agent runs receive: the agent registers them first.
_PUSH_REGISTRATIONS: dict[str, PushRegistration] = {
    agy.CLIENT_ID: agy.REGISTRATION,
}

# Clients whose sessions can take a wake from a watcher only under some
# conditions, told by the environment the watcher runs in.
_WATCH_ALLOWED: dict[str, Callable[[Mapping[str, str]], bool]] = {
    claude.CLIENT_ID: claude.can_take_wake,
}

_WIRINGS: dict[str, ClientWiring] = {
    agy.CLIENT_ID: agy.AgyWiring(),
    claude.CLIENT_ID: claude.ClaudeWiring(),
    codex.CLIENT_ID: codex.CodexWiring(),
    opencode.CLIENT_ID: opencode.OpencodeWiring(),
}


def hook_dialect(client: str | None) -> HookDialect:
    """The hook protocol `client` speaks; the standard one if unknown."""
    if client is None:
        return _STANDARD
    return _HOOK_DIALECTS.get(client, _STANDARD)


def session_key(client: str | None, env: Mapping[str, str]) -> str | None:
    """The client's own id for the session this process serves, if it
    publishes one in the environment."""
    variable = _SESSION_KEY_ENV.get(client or "")
    value = env.get(variable, "").strip() if variable else ""
    return value or None


def requires_session_key(client: str | None) -> bool:
    """Whether hooks must identify a thread rather than its parent process."""
    return client in _SESSION_KEY_META


def session_key_from_meta(client: str | None, meta: Mapping[str, object]) -> str | None:
    """The conversation id supplied by the MCP client on a tool call."""
    field = _SESSION_KEY_META.get(client or "")
    value = meta.get(field) if field else None
    if not isinstance(value, str):
        return None
    return value.strip() or None


def watch_allowed(client: str | None, env: Mapping[str, str]) -> bool:
    """Whether a watcher may run for the session `client` started it in."""
    check = _WATCH_ALLOWED.get(client or "")
    return check(env) if check else True


def can_push(client: str | None) -> bool:
    """Whether `client`'s idle sessions are woken by their sender."""
    return (client or "") in _PUSH_COMMANDS


def push_command(client: str, *, thread: str, executable: str | None) -> list[str]:
    """The argv that starts a turn in `client`'s session `thread`.
    Raises KeyError for a client that cannot be pushed to."""
    return _PUSH_COMMANDS[client](thread=thread, executable=executable)


def pushed_while_busy(client: str | None) -> bool:
    """Whether a busy session of `client` is pushed to as well as an idle one."""
    return (client or "") in _PUSHED_WHILE_BUSY


def push_registration(client: str | None) -> PushRegistration | None:
    """How `client`'s agent registers the credentials a push needs, for a
    client that gives them to nobody else; None for every other client."""
    return _PUSH_REGISTRATIONS.get(client or "")


def wirable_clients() -> tuple[str, ...]:
    """Client ids `agent-bus init` knows how to wire."""
    return tuple(sorted(_WIRINGS))


def wiring(client: str) -> ClientWiring:
    """Raises KeyError for a client `init` cannot wire."""
    return _WIRINGS[client]

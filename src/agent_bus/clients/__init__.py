"""Per-client adapters.

The core (storage, server, hooks) knows nothing about any particular MCP
client; what differs between clients is looked up here by client id.
Supporting another client means adding a module and its registry lines —
and a client that follows the standard hook contract needs no dialect.
"""

from __future__ import annotations

from typing import Mapping

from . import agy, claude, codex, opencode
from .base import ClientWiring, HookDialect, StandardHookDialect

_HOOK_DIALECTS: dict[str, HookDialect] = {
    agy.CLIENT_ID: agy.AgyHookDialect(),
    codex.CLIENT_ID: codex.CodexHookDialect(),
}
_STANDARD = StandardHookDialect()

# Where a client tells the processes it starts which of its sessions they
# serve. Only a client listed here can have its sessions told apart by id;
# the others rely on process ancestry alone.
_SESSION_KEY_ENV: dict[str, str] = {
    claude.CLIENT_ID: claude.SESSION_KEY_ENV,
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


def wirable_clients() -> tuple[str, ...]:
    """Client ids `agent-bus init` knows how to wire."""
    return tuple(sorted(_WIRINGS))


def wiring(client: str) -> ClientWiring:
    """Raises KeyError for a client `init` cannot wire."""
    return _WIRINGS[client]

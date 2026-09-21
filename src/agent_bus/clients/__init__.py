"""Per-client adapters.

The core (storage, server, hooks) knows nothing about any particular MCP
client; what differs between clients is looked up here by client id.
Supporting another client means adding a module and one registry line —
and a client that follows the standard hook contract needs neither.
"""

from __future__ import annotations

from . import agy
from .base import HookDialect, StandardHookDialect

_HOOK_DIALECTS: dict[str, HookDialect] = {
    agy.CLIENT_ID: agy.AgyHookDialect(),
}
_STANDARD = StandardHookDialect()


def hook_dialect(client: str | None) -> HookDialect:
    """The hook protocol `client` speaks; the standard one if unknown."""
    if client is None:
        return _STANDARD
    return _HOOK_DIALECTS.get(client, _STANDARD)

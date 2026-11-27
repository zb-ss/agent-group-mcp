"""A session's own remote URL as an automatic session link.

Claude Code sets a bridge session id in the environment of a session that
runs with Remote Control. The configured template
(`spawn.clients.<client>.session_link`) turns that id into a URL, which is
stored as a link of the session so alerts and `agent-bus status` carry it.

Which process sees the variable (checked live in a Remote Control session):
the shell the Bash tool starts, this session's agent-bus MCP server and the
processes that run the bus hooks all see it, so the MCP server computes the
link once at start and the hooks need no code of their own.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from . import config, links
from .storage import Storage

_ID = re.compile(r"[A-Za-z0-9_-]{1,200}")  # what a URL-safe id may hold


def from_env(client_cfg: Mapping[str, Any] | None, env: Mapping[str, str]) -> dict[str, str] | None:
    """The link for this environment, or None when the client has no
    session_link, the variable is unset or empty, or its value is not a
    plain id."""
    spec = (client_cfg or {}).get("session_link")
    if not isinstance(spec, dict):
        return None
    value = env.get(spec.get("env") or "")
    if not value or _ID.fullmatch(value) is None:
        return None
    try:
        normalized = links.normalize(
            [{"label": spec["label"], "url": spec["url"].format(id=value)}]
        )
    except (links.LinkError, KeyError, IndexError, ValueError):
        return None
    return normalized[0]


def ensure(store: Storage, agent_name: str, link: dict[str, str]) -> bool:
    """Put `link` first in the agent's links unless its url is already
    there. The oldest trailing entries go when the list would exceed the
    bound. Returns whether it wrote."""
    row = store.get_agent(agent_name)
    existing = row.link_list if row else []
    if any(entry.get("url") == link["url"] for entry in existing):
        return False
    store.set_links(agent_name, ([link] + existing)[: links.MAX_LINKS])
    return True


def for_client(client: str | None, env: Mapping[str, str]) -> dict[str, str] | None:
    """The link for `client` from the loaded config (defaults when the
    user's config is broken)."""
    if not client:
        return None
    try:
        cfg = config.load()
    except config.ConfigError:
        cfg = config.defaults()
    return from_env(cfg.get("spawn", {}).get("clients", {}).get(client), env)

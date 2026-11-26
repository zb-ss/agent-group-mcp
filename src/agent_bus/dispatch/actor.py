"""Who is acting: the verified dispatcher or the owner's own shell.

A leaf module so that drafts and decisions can share it without importing
the runner.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .. import authority, resolution

ACTOR_DISPATCHER = "dispatcher"
ACTOR_CLI = "cli"


def actor_for(storage: Any, env: Mapping[str, str]) -> str:
    role = storage.authority_role(env.get(resolution.AUTHORITY_TOKEN_ENV))
    return ACTOR_DISPATCHER if role == authority.ROLE_DISPATCHER else ACTOR_CLI

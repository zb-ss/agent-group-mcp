"""What the bus needs to know about an MCP client's hook protocol.

Clients disagree on two things: where the hook payload says which repo
the session is in, and what a hook must print to inject context or to keep
the agent from stopping. Everything else about draining an inbox is shared
and lives in `hooks.py`.
"""

from __future__ import annotations

import json
from typing import Protocol


class HookDialect(Protocol):
    def repo_from_payload(self, payload: dict) -> str | None:
        """The session's working directory, if the payload carries one."""

    def prompt_output(self, context: str | None) -> str:
        """Stdout for the hook that runs before the model sees a prompt.
        `context` is the text to inject, or None when the inbox was empty."""

    def stop_output(self, reason: str | None) -> str:
        """Stdout for the hook that runs when the agent is about to stop.
        `reason` is why it must keep going, or None to let it stop."""


class StandardHookDialect:
    """The contract Claude Code introduced and several clients adopted:
    plain stdout becomes context, and a `{"decision": "block"}` object keeps
    the turn open. Also the fallback for a client we have no adapter for."""

    def repo_from_payload(self, payload: dict) -> str | None:
        cwd = payload.get("cwd")
        return cwd if isinstance(cwd, str) and cwd else None

    def prompt_output(self, context: str | None) -> str:
        return f"{context}\n" if context else ""

    def stop_output(self, reason: str | None) -> str:
        if not reason:
            return ""
        return json.dumps({"decision": "block", "reason": reason}) + "\n"

"""Antigravity CLI (`agy`).

Its hooks speak JSON in both directions: the payload is camelCase and has
no `cwd`, only the list of workspace folders; stdout must be a JSON object
made of fields agy knows, even when there is nothing to say.

Events used: `PreInvocation` (before each model call) to inject context,
and `Stop` to keep the agent going.
"""

from __future__ import annotations

import json

CLIENT_ID = "agy"


class AgyHookDialect:
    def repo_from_payload(self, payload: dict) -> str | None:
        paths = payload.get("workspacePaths")
        if not isinstance(paths, list) or not paths:
            return None
        first = paths[0]
        return first if isinstance(first, str) and first else None

    def prompt_output(self, context: str | None) -> str:
        steps = [{"ephemeralMessage": context}] if context else []
        return json.dumps({"injectSteps": steps}) + "\n"

    def stop_output(self, reason: str | None) -> str:
        if not reason:
            # `decision` is required; anything but "continue" lets it stop
            return json.dumps({"decision": "stop"}) + "\n"
        return json.dumps({"decision": "continue", "reason": reason}) + "\n"

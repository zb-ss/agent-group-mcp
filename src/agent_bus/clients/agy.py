"""Antigravity CLI (`agy`).

agy only starts MCP servers that are configured per user or that come
with a plugin, so a repo is wired as a workspace plugin of its own:
`.agents/plugins/agent-bus/` holding `plugin.json`, `mcp_config.json` and
`hooks.json`. The directory is entirely ours, which means nothing of the
user's is ever merged or rewritten.

Its hooks speak JSON in both directions: the payload is camelCase and has
no `cwd`, only the list of workspace folders; stdout must be a JSON object
made of fields agy knows, even when there is nothing to say. There is no
prompt-submit event. Events used: `PreInvocation` (before each model call)
to inject context, and `Stop` to keep the agent going.

An idle agy session is woken from the sender's side, as Codex is: `agy
agentapi send-message <conversation id> <text>` starts a turn in a running
session at once (and reaches a busy one at its next model call), after
which the PreInvocation hook adds the mail. agentapi talks to the
session's local API, which wants the API's address and a CSRF token. agy
gives those to the commands its agent runs, but not to hooks or MCP
servers, so the PreInvocation hook asks the agent once to run `agent-bus
wake-register`, which keeps them for senders (`push_credentials.py`).

Unlike Codex, agy is pushed to while busy too: the message joins the
running turn instead of queueing another one, and agy ends a turn the user
interrupts without running its Stop hook, so "busy" may be stale.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Mapping

from .. import settings
from . import base
from .base import (
    PushCredentials,
    PushRegistration,
    UnreadableConfigError,
    WiringState,
    WiringStatus,
)

CLIENT_ID = "agy"

HOOK_EVENTS = (
    ("PreInvocation", "hook-user-prompt"),
    ("Stop", "hook-stop"),
)

# What agy puts in the environment of the commands its agent runs.
CONVERSATION_ENV = "ANTIGRAVITY_CONVERSATION_ID"
API_ADDRESS_ENV = "ANTIGRAVITY_LS_ADDRESS"
API_TOKEN_ENV = "ANTIGRAVITY_CSRF_TOKEN"
# The message a woken session starts its turn with; the mail itself follows
# from the PreInvocation hook.
WAKE_NOTICE = "[agent-bus] New mail from peers has arrived; it is shown below."
REGISTER_REQUEST = (
    "[agent-bus] Before anything else, run this shell command, once for this "
    "whole conversation: {command}\n"
    "It only lets mail from other agents wake this session while it is idle, "
    "and it stays in effect: never run it again in this conversation, even "
    "when later messages come from agent-bus. It needs no follow-up: do not "
    "mention it or look for mail afterwards, just carry on with the user's "
    "request."
)


def send_command(*, thread: str, executable: str | None) -> list[str]:
    """The command that starts a turn in agy conversation `thread`: with the
    executable the session runs, while it is still there. It also needs
    the session's API credentials in its environment (`api_env`)."""
    recorded = executable if executable and os.path.isfile(executable) else None
    program = settings.agy_bin() or recorded or "agy"
    return [program, "agentapi", "send-message", thread, WAKE_NOTICE]


def api_credentials(env: Mapping[str, str]) -> PushCredentials | None:
    """The conversation and its API credentials, from the environment of a
    command the agent runs; None anywhere else."""
    thread, endpoint, secret = (
        env.get(key, "").strip() for key in (CONVERSATION_ENV, API_ADDRESS_ENV, API_TOKEN_ENV)
    )
    if not (thread and endpoint and secret):
        return None
    return PushCredentials(thread=thread, endpoint=endpoint, secret=secret)


def api_env(credentials: PushCredentials) -> dict[str, str]:
    return {API_ADDRESS_ENV: credentials.endpoint, API_TOKEN_ENV: credentials.secret}


REGISTRATION = PushRegistration(
    from_env=api_credentials, to_env=api_env, request=REGISTER_REQUEST
)


class AgyHookDialect:
    def repo_from_payload(self, payload: dict) -> str | None:
        paths = payload.get("workspacePaths")
        if not isinstance(paths, list) or not paths:
            return None
        first = paths[0]
        return first if isinstance(first, str) and first else None

    def session_from_payload(self, payload: dict) -> str | None:
        session = payload.get("conversationId")
        return session if isinstance(session, str) and session else None

    def prompt_output(self, context: str | None) -> str:
        steps = [{"ephemeralMessage": context}] if context else []
        return json.dumps({"injectSteps": steps}) + "\n"

    def stop_output(self, reason: str | None) -> str:
        if not reason:
            # `decision` is required; anything but "continue" lets it stop
            return json.dumps({"decision": "stop"}) + "\n"
        return json.dumps({"decision": "continue", "reason": reason}) + "\n"


PLUGIN_DIR = Path(".agents") / "plugins" / base.SERVER_KEY


class AgyWiring:
    client_id = CLIENT_ID
    limitations = (
        "agy has no prompt-submit hook: pending mail is injected before each "
        "model call as a transient system message.",
        "agy loads a repo's .agents/ plugins only for a workspace it trusts.",
        "An idle agy session can be woken by mail once its agent has run "
        "`agent-bus wake-register`, which it is asked to do when the session "
        "starts. agy asks you to allow that command: allow it always, and it "
        "stops asking.",
    )

    def files(self, repo: Path) -> list[Path]:
        plugin = repo / PLUGIN_DIR
        return [plugin / "plugin.json", plugin / "mcp_config.json", plugin / "hooks.json"]

    def inspect(self, repo: Path) -> WiringState:
        _manifest, mcp_path, _hooks = self.files(repo)
        if not (repo / PLUGIN_DIR).exists():
            return WiringState(WiringStatus.ABSENT)
        try:
            mcp = base.load_json_object(mcp_path)
        except UnreadableConfigError:
            return WiringState(WiringStatus.UNREADABLE)
        servers = mcp.get("mcpServers")
        entry = servers.get(base.SERVER_KEY) if isinstance(servers, dict) else None
        if not isinstance(entry, dict) or not base.is_managed_server(
            entry.get("command"), entry.get("args")
        ):
            # a plugin called agent-bus that does not look like our output
            return WiringState(WiringStatus.HANDWRITTEN)
        env = entry.get("env")
        name = env.get("AGENT_BUS_NAME") if isinstance(env, dict) else None
        return WiringState(WiringStatus.MANAGED, name)

    def apply(self, repo: Path, *, name: str, bin_path: str) -> None:
        manifest_path, mcp_path, hooks_path = self.files(repo)
        base.write_json_object(manifest_path, {"name": base.SERVER_KEY})
        # only keys agy documents: it may reject a config with unknown ones
        base.write_json_object(mcp_path, {"mcpServers": {base.SERVER_KEY: {
            "command": bin_path,
            "args": ["serve"],
            "env": {"AGENT_BUS_NAME": name, "AGENT_BUS_REPO": str(repo)},
        }}})
        base.write_json_object(hooks_path, {base.SERVER_KEY: {
            event: [{
                "type": "command",
                "command": base.hook_command(
                    name=name, bin_path=bin_path, subcommand=subcommand, client=CLIENT_ID,
                ),
            }]
            for event, subcommand in HOOK_EVENTS
        }})

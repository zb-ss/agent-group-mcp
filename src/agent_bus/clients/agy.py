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
"""

from __future__ import annotations

import json
from pathlib import Path

from . import base
from .base import UnreadableConfigError, WiringState, WiringStatus

CLIENT_ID = "agy"

HOOK_EVENTS = (
    ("PreInvocation", "hook-user-prompt"),
    ("Stop", "hook-stop"),
)


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


PLUGIN_DIR = Path(".agents") / "plugins" / base.SERVER_KEY


class AgyWiring:
    client_id = CLIENT_ID
    limitations = (
        "agy has no prompt-submit hook: pending mail is injected before each "
        "model call as a transient system message.",
        "agy loads a repo's .agents/ plugins only for a workspace it trusts.",
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

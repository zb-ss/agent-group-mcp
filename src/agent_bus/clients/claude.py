"""Claude Code.

MCP servers live in the repo's `.mcp.json`; hooks and tool permissions in
`.claude/settings.json`. Hooks follow the standard contract, fire on
`UserPromptSubmit` and `Stop`, and inherit Claude Code's environment.
"""

from __future__ import annotations

from pathlib import Path

from . import base
from .base import UnreadableConfigError, WiringState, WiringStatus

CLIENT_ID = "claude"

HOOK_EVENTS = (
    ("UserPromptSubmit", "hook-user-prompt"),
    ("Stop", "hook-stop"),
)

ALLOW_TOOLS = (
    "mcp__agent-bus__whoami",
    "mcp__agent-bus__list_agents",
    "mcp__agent-bus__send_message",
    "mcp__agent-bus__read_inbox",
    "mcp__agent-bus__read_thread",
    "mcp__agent-bus__tail_audit",
)


def build_mcp_entry(*, name: str, repo: Path, bin_path: str) -> dict:
    """Canonical shape — kept in one place so `is_managed_mcp_entry()` can
    recognise our previous output on re-runs."""
    return {
        "command": bin_path,
        "args": ["serve"],
        "env": {
            "AGENT_BUS_NAME": name,
            "AGENT_BUS_REPO": str(repo),
        },
        "description": base.MANAGED_DESCRIPTION,
    }


def is_managed_mcp_entry(entry: dict | None) -> bool:
    if not isinstance(entry, dict):
        return False
    return base.is_managed_server(entry.get("command", ""), entry.get("args", []))


def build_hook_block(*, name: str, bin_path: str, subcommand: str) -> dict:
    return {
        "matcher": "",
        "hooks": [
            {
                "type": "command",
                "command": base.hook_command(
                    name=name, bin_path=bin_path,
                    subcommand=subcommand, client=CLIENT_ID,
                ),
            }
        ],
    }


def _block_owners(block: object) -> set[str]:
    if not isinstance(block, dict) or not isinstance(block.get("hooks"), list):
        return set()
    owners = (
        base.hook_owner(h.get("command", ""))
        for h in block["hooks"] if isinstance(h, dict)
    )
    return {o for o in owners if o is not None}


def is_managed_hook_block(block: object) -> bool:
    """Any hook block `agent-bus init` wrote, for whichever client."""
    return bool(_block_owners(block))


def _is_ours(block: object) -> bool:
    """Blocks this adapter may replace: tagged for Claude Code, or untagged
    ones from before hooks carried a client tag. A block tagged for another
    client that shares this file is left alone."""
    return bool(_block_owners(block) & {CLIENT_ID, ""})


class ClaudeWiring:
    client_id = CLIENT_ID
    limitations: tuple[str, ...] = ()

    def files(self, repo: Path) -> list[Path]:
        return [repo / ".mcp.json", repo / ".claude" / "settings.json"]

    def inspect(self, repo: Path) -> WiringState:
        try:
            for path in self.files(repo):
                base.load_json_object(path)
            data = base.load_json_object(repo / ".mcp.json")
        except UnreadableConfigError:
            return WiringState(WiringStatus.UNREADABLE)
        servers = data.get("mcpServers")
        entry = servers.get(base.SERVER_KEY) if isinstance(servers, dict) else None
        if entry is None:
            return WiringState(WiringStatus.ABSENT)
        env = entry.get("env") if isinstance(entry, dict) else None
        name = env.get("AGENT_BUS_NAME") if isinstance(env, dict) else None
        status = (
            WiringStatus.MANAGED if is_managed_mcp_entry(entry)
            else WiringStatus.HANDWRITTEN
        )
        return WiringState(status, name)

    def apply(self, repo: Path, *, name: str, bin_path: str) -> None:
        mcp_path, settings_path = self.files(repo)
        # parse both before writing either: never leave a repo half-wired
        mcp = base.load_json_object(mcp_path)
        settings = base.load_json_object(settings_path)

        servers = mcp.get("mcpServers")
        if not isinstance(servers, dict):
            servers = {}
        servers[base.SERVER_KEY] = build_mcp_entry(name=name, repo=repo, bin_path=bin_path)
        mcp["mcpServers"] = servers

        self._merge_permissions(settings)
        self._merge_hooks(settings, name=name, bin_path=bin_path)

        base.write_json_object(mcp_path, mcp)
        base.write_json_object(settings_path, settings)

    @staticmethod
    def _merge_permissions(settings: dict) -> None:
        perms = settings.get("permissions")
        if not isinstance(perms, dict):
            perms = {}
        allow = perms.get("allow")
        if not isinstance(allow, list):
            allow = []
        allow.extend(tool for tool in ALLOW_TOOLS if tool not in allow)
        perms["allow"] = allow
        settings["permissions"] = perms

    @staticmethod
    def _merge_hooks(settings: dict, *, name: str, bin_path: str) -> None:
        hooks = settings.get("hooks")
        if not isinstance(hooks, dict):
            hooks = {}
        for event, subcommand in HOOK_EVENTS:
            existing = hooks.get(event)
            if not isinstance(existing, list):
                existing = []
            kept = [b for b in existing if not _is_ours(b)]
            kept.append(
                build_hook_block(name=name, bin_path=bin_path, subcommand=subcommand)
            )
            hooks[event] = kept
        settings["hooks"] = hooks

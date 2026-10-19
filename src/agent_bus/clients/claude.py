"""Claude Code.

MCP servers live in the repo's `.mcp.json`; hooks and tool permissions in
`.claude/settings.json`. Hooks follow the standard contract, fire on
`UserPromptSubmit` and `Stop`, and inherit Claude Code's environment.
Claude Code tells its MCP servers and hooks which session they serve in
`CLAUDE_CODE_SESSION_ID`.

Waking an idle session uses an `asyncRewake` hook: Claude Code runs it in
the background and, when it exits 2, starts a turn with its stderr. Each
session start and each turn end launches `hook-watch` that way. Before
Claude Code knew `asyncRewake` it ran such a hook in the foreground,
which would hold every turn open until mail arrived — so the watch hooks
are written only for a Claude Code that reports MIN_REWAKE_VERSION or
later.
"""

from __future__ import annotations

import functools
import re
import subprocess
from pathlib import Path
from typing import Mapping

from .. import settings
from . import base
from .base import UnreadableConfigError, WiringState, WiringStatus

CLIENT_ID = "claude"
SESSION_KEY_ENV = "CLAUDE_CODE_SESSION_ID"

HOOK_EVENTS = (
    ("UserPromptSubmit", "hook-user-prompt"),
    ("Stop", "hook-stop"),
)
# Where a watcher starts: a new (or resumed, cleared, compacted) session,
# and the end of every turn.
WATCH_EVENTS = ("SessionStart", "Stop")

# Claude Code names how it was started in its children's environment;
# `cli` is an interactive session. Run otherwise (`claude -p`, the SDK) it
# waits for an asyncRewake hook in the foreground, or discards what the
# hook found when the run ends.
ENTRYPOINT_ENV = "CLAUDE_CODE_ENTRYPOINT"
INTERACTIVE_ENTRYPOINT = "cli"

# Changelog facts, not tunables: `asyncRewake` hooks exist from 2.1.287,
# and 2.1.290 fixed one waking Claude in an endless loop.
MIN_REWAKE_VERSION = (2, 1, 290)
# The exit code on which Claude Code wakes the model from an asyncRewake hook.
REWAKE_EXIT_CODE = 2
# Shown to the user in place of "Stop hook feedback" when mail wakes a session.
REWAKE_SUMMARY = "agent-bus: new mail"
# Claude Code waits on a hook with a JavaScript timer, which cannot count
# past 2**31 - 1 milliseconds.
MAX_HOOK_TIMEOUT_SECONDS = (2**31 - 1) // 1000
# The watcher gives up this much before Claude Code's timeout, so that it
# ends on its own rather than being killed.
WATCH_TIMEOUT_MARGIN_SECONDS = 30

ALLOW_TOOLS = (
    "mcp__agent-bus__whoami",
    "mcp__agent-bus__list_agents",
    "mcp__agent-bus__send_message",
    "mcp__agent-bus__read_inbox",
    "mcp__agent-bus__read_thread",
    "mcp__agent-bus__tail_audit",
    "mcp__agent-bus__set_session",
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


def build_watch_block(*, name: str, bin_path: str, max_seconds: int) -> dict:
    return {
        "matcher": "",
        "hooks": [
            {
                "type": "command",
                "command": base.watch_command(
                    name=name, bin_path=bin_path, client=CLIENT_ID,
                    max_seconds=max_seconds, wake_exit=REWAKE_EXIT_CODE,
                ),
                "asyncRewake": True,
                "timeout": max_seconds + WATCH_TIMEOUT_MARGIN_SECONDS,
                "rewakeSummary": REWAKE_SUMMARY,
            }
        ],
    }


def watch_seconds() -> int:
    """How long each watcher waits, within what Claude Code can time."""
    wanted = int(settings.watch_hours() * 3600)
    return max(1, min(wanted, MAX_HOOK_TIMEOUT_SECONDS - WATCH_TIMEOUT_MARGIN_SECONDS))


def can_take_wake(env: Mapping[str, str]) -> bool:
    """Whether the session this process serves can be woken by a
    background hook: only an interactive one."""
    return env.get(ENTRYPOINT_ENV) == INTERACTIVE_ENTRYPOINT


def parse_version(text: str) -> tuple[int, ...] | None:
    """`2.1.303 (Claude Code)` → (2, 1, 303)."""
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    return tuple(int(part) for part in match.groups()) if match else None


@functools.lru_cache(maxsize=1)
def rewake_unsupported_reason() -> str | None:
    """Why this machine's Claude Code cannot be trusted with an
    asyncRewake hook, or None when it can. Asked once per `init` run."""
    command = settings.claude_bin()
    try:
        out = subprocess.run(
            [command, "--version"], capture_output=True, text=True, check=True,
            stdin=subprocess.DEVNULL, timeout=settings.client_version_timeout_seconds(),
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return f"could not ask `{command} --version` (set ${settings.CLAUDE_BIN_ENV})"
    version = parse_version(out)
    if version is None:
        return f"`{command} --version` did not report a version"
    if version < MIN_REWAKE_VERSION:
        found = ".".join(map(str, version))
        wanted = ".".join(map(str, MIN_REWAKE_VERSION))
        return f"Claude Code {found} is older than {wanted}"
    return None


def native_wake_reason() -> str | None:
    """Why `init` leaves the watch hooks out, or None when it writes them."""
    if not settings.native_wake_enabled():
        return f"turned off with ${settings.NATIVE_WAKE_ENV}"
    return rewake_unsupported_reason()


def is_managed_hook_block(block: object) -> bool:
    """Any hook block `agent-bus init` wrote, for whichever client."""
    return bool(base.group_owners(block))


def _is_ours(block: object) -> bool:
    """Blocks this adapter may replace: tagged for Claude Code, or untagged
    ones from before hooks carried a client tag. A block tagged for another
    client that shares this file is left alone."""
    return bool(base.group_owners(block) & {CLIENT_ID, ""})


class ClaudeWiring:
    client_id = CLIENT_ID

    @property
    def limitations(self) -> tuple[str, ...]:
        reason = native_wake_reason()
        if reason is None:
            return ()
        return (
            f"idle sessions are not woken when mail arrives ({reason}); mail "
            "waits for the next turn, or for a wake.json command.",
        )

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
        config = base.load_json_object(settings_path)

        servers = mcp.get("mcpServers")
        if not isinstance(servers, dict):
            servers = {}
        servers[base.SERVER_KEY] = build_mcp_entry(name=name, repo=repo, bin_path=bin_path)
        mcp["mcpServers"] = servers

        self._merge_permissions(config)
        self._merge_hooks(config, name=name, bin_path=bin_path)

        base.write_json_object(mcp_path, mcp)
        base.write_json_object(settings_path, config)

    @staticmethod
    def _merge_permissions(config: dict) -> None:
        perms = config.get("permissions")
        if not isinstance(perms, dict):
            perms = {}
        allow = perms.get("allow")
        if not isinstance(allow, list):
            allow = []
        allow.extend(tool for tool in ALLOW_TOOLS if tool not in allow)
        perms["allow"] = allow
        config["permissions"] = perms

    @staticmethod
    def _merge_hooks(config: dict, *, name: str, bin_path: str) -> None:
        hooks = config.get("hooks")
        if not isinstance(hooks, dict):
            hooks = {}
        ours = _blocks_by_event(name=name, bin_path=bin_path)
        for event in dict.fromkeys([*ours, *WATCH_EVENTS]):
            existing = hooks.get(event)
            if not isinstance(existing, list):
                existing = []
            kept = [b for b in existing if not _is_ours(b)] + ours.get(event, [])
            if kept:
                hooks[event] = kept
            else:
                hooks.pop(event, None)
        config["hooks"] = hooks


def _blocks_by_event(*, name: str, bin_path: str) -> dict[str, list[dict]]:
    """Every hook block this adapter writes, by event."""
    blocks: dict[str, list[dict]] = {
        event: [build_hook_block(name=name, bin_path=bin_path, subcommand=subcommand)]
        for event, subcommand in HOOK_EVENTS
    }
    if native_wake_reason() is None:
        seconds = watch_seconds()
        for event in WATCH_EVENTS:
            blocks.setdefault(event, []).append(
                build_watch_block(name=name, bin_path=bin_path, max_seconds=seconds)
            )
    return blocks

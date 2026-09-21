"""Codex CLI.

MCP servers are TOML tables in the repo's `.codex/config.toml`; hooks live
in `.codex/hooks.json`. Hooks follow the standard contract and fire on
`UserPromptSubmit` and `Stop`.

Three things shape this adapter:

  * Python can read TOML but not write it, so our server table sits between
    two marker comments and only that span is ever rewritten. A table named
    `agent-bus` outside the markers is somebody else's and stays untouched.
  * Codex starts MCP servers with a minimal environment, so the variables a
    second session needs are listed in `env_vars` to be passed through.
  * Codex asks the user to approve each hook and ties the approval to the
    hook's definition. The hook commands therefore carry no agent name —
    the hook finds its agent from the payload's `cwd` — and come out
    identical in every repo.

Its hooks follow the standard contract with one trap: stdout that starts
with `[` or `{` is parsed as JSON, and a hook whose output fails to parse
is discarded. Context for the prompt therefore goes out as JSON, never as
the plain text other clients take.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from . import base
from .base import UnreadableConfigError, WiringState, WiringStatus

CLIENT_ID = "codex"

HOOK_EVENTS = (
    ("UserPromptSubmit", "hook-user-prompt"),
    ("Stop", "hook-stop"),
)
PASSED_THROUGH_ENV = ("AGENT_BUS_INSTANCE", "AGENT_BUS_DB", "AGENT_BUS_AUDIT_LOG")

BLOCK_START = "# >>> agent-bus: managed by `agent-bus init`, regenerated on every run >>>"
BLOCK_END = "# <<< agent-bus <<<"


class CodexHookDialect(base.StandardHookDialect):
    def prompt_output(self, context: str | None) -> str:
        if not context:
            return ""
        return json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": context,
            }
        }) + "\n"


def _toml_string(value: str) -> str:
    # a JSON string literal is a valid TOML basic string
    return json.dumps(value, ensure_ascii=False)


def _server_block(*, name: str, repo: Path, bin_path: str) -> str:
    env_vars = ", ".join(_toml_string(v) for v in PASSED_THROUGH_ENV)
    return "\n".join([
        BLOCK_START,
        f"[mcp_servers.{base.SERVER_KEY}]",
        f"command = {_toml_string(bin_path)}",
        'args = ["serve"]',
        f"env_vars = [{env_vars}]",
        "",
        f"[mcp_servers.{base.SERVER_KEY}.env]",
        f"AGENT_BUS_NAME = {_toml_string(name)}",
        f"AGENT_BUS_REPO = {_toml_string(str(repo))}",
        BLOCK_END,
    ]) + "\n"


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError as e:
        raise UnreadableConfigError(path, str(e)) from None


def _parse(path: Path, text: str) -> dict:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise UnreadableConfigError(path, f"not valid TOML ({e})") from None


def _server_table(config: dict) -> dict | None:
    servers = config.get("mcp_servers")
    table = servers.get(base.SERVER_KEY) if isinstance(servers, dict) else None
    return table if isinstance(table, dict) else None


def _has_block(text: str) -> bool:
    return BLOCK_START in text and BLOCK_END in text


def _splice(text: str, block: str) -> str:
    """`text` with our block replaced in place, or appended."""
    if _has_block(text):
        before, _, rest = text.partition(BLOCK_START)
        _, _, after = rest.partition(BLOCK_END)
        return before + block + after.removeprefix("\n")
    if text and not text.endswith("\n"):
        text += "\n"
    return text + ("\n" if text else "") + block


class CodexWiring:
    client_id = CLIENT_ID
    limitations = (
        "Codex reads a repo's .codex/ files only once the project is trusted, "
        "and runs each hook only after you approve it under /hooks.",
    )

    def files(self, repo: Path) -> list[Path]:
        return [repo / ".codex" / "config.toml", repo / ".codex" / "hooks.json"]

    def inspect(self, repo: Path) -> WiringState:
        config_path, hooks_path = self.files(repo)
        try:
            text = _read_text(config_path)
            table = _server_table(_parse(config_path, text))
            base.load_json_object(hooks_path)
        except UnreadableConfigError:
            return WiringState(WiringStatus.UNREADABLE)
        if table is None:
            return WiringState(WiringStatus.ABSENT)
        env = table.get("env")
        name = env.get("AGENT_BUS_NAME") if isinstance(env, dict) else None
        if _has_block(text):
            return WiringState(WiringStatus.MANAGED, name)
        return WiringState(WiringStatus.HANDWRITTEN, name, can_force=False)

    def apply(self, repo: Path, *, name: str, bin_path: str) -> None:
        config_path, hooks_path = self.files(repo)
        # work everything out before writing anything: never half-wire a repo
        new_config = self._new_config(config_path, name=name, repo=repo, bin_path=bin_path)
        hooks = base.load_json_object(hooks_path)
        self._merge_hooks(hooks, bin_path=bin_path)

        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(new_config, encoding="utf-8")
        base.write_json_object(hooks_path, hooks)

    @staticmethod
    def _new_config(path: Path, *, name: str, repo: Path, bin_path: str) -> str:
        text = _read_text(path)
        if _server_table(_parse(path, text)) is not None and not _has_block(text):
            raise UnreadableConfigError(
                path, f"has its own [mcp_servers.{base.SERVER_KEY}] table; remove it "
                "or set AGENT_BUS_NAME there yourself"
            )
        new_text = _splice(text, _server_block(name=name, repo=repo, bin_path=bin_path))
        # the block is text, the file is the user's: prove the result still
        # parses and says what we meant before it replaces anything
        table = _server_table(_parse(path, new_text))
        if not table or table.get("env", {}).get("AGENT_BUS_NAME") != name:
            raise UnreadableConfigError(path, "could not add the agent-bus server table")
        return new_text

    @staticmethod
    def _merge_hooks(data: dict, *, bin_path: str) -> None:
        hooks = data.get("hooks")
        if not isinstance(hooks, dict):
            hooks = {}
        for event, subcommand in HOOK_EVENTS:
            groups = hooks.get(event)
            if not isinstance(groups, list):
                groups = []
            kept = [g for g in groups if CLIENT_ID not in base.group_owners(g)]
            kept.append({"hooks": [{
                "type": "command",
                "command": base.hook_command(
                    name=None, bin_path=bin_path, subcommand=subcommand, client=CLIENT_ID,
                ),
            }]})
            hooks[event] = kept
        data["hooks"] = hooks

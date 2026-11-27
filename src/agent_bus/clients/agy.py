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
import shutil
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
    WorktreePrep,
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
        "agy loads a repo's .agents/ plugins only for a workspace it trusts; "
        "a spawned worktree is a new folder, so it is trusted only with the "
        "opt-in spawn.clients.agy.worktree_trust option, which copies the "
        "clone's trust over. agy also ignores plugin files that are "
        "symlinks, so spawn gives a worktree real copies of them.",
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

    def prepare_worktree(
        self, clone: Path, worktree: Path, *, name: str, bin_path: str,
        dry_run: bool = False, options: Mapping | None = None,
    ) -> WorktreePrep:
        """Make the plugin load in a worktree, and (opt-in) trust the worktree.

        agy ignores a workspace plugin whose files are symlinks (checked with
        agy 1.5.18: the same files as real files load, as links they do not,
        and `agy plugin validate` accepts both), so the links spawn made to
        the clone's files are replaced by copies. The copy names the clone,
        as the clone's file does: the session's repo stays the clone.

        `options["worktree_trust"]` names agy's settings file and the key
        holding its list of trusted folders; only that list is appended to."""
        prep = self._copy_plugin_files(clone, worktree, dry_run=dry_run)
        trusted = self._trust_worktree(clone, worktree, dry_run=dry_run, options=options)
        return WorktreePrep(
            [*prep.notes, *trusted.notes], [*prep.written, *trusted.written],
            [*prep.covered, *trusted.covered],
        )

    def _copy_plugin_files(self, clone: Path, worktree: Path, *, dry_run: bool) -> WorktreePrep:
        notes: list[str] = []
        written: list[Path] = []
        covered: list[Path] = []
        for source in self.files(clone):
            rel = source.relative_to(clone)
            dest = worktree / rel
            if not source.is_file():
                continue
            if not dest.is_symlink() and dest.exists():
                # a file the repo tracks: left alone, and only called covered when
                # it is the same wiring
                if dest.is_file() and dest.read_bytes() == source.read_bytes():
                    covered.append(rel)
                continue
            if dry_run:
                notes.append(f"would copy {rel} into {worktree} as a real file (agy ignores links)")
                covered.append(rel)
                continue
            if not dest.parent.resolve().is_relative_to(worktree.resolve()):
                notes.append(f"not writing {dest}: its folder resolves outside the worktree")
                continue
            if dest.is_symlink():
                dest.unlink()
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, dest)
            written.append(rel)
            covered.append(rel)
        return WorktreePrep(notes, written, covered)

    def _trust_worktree(
        self, clone: Path, worktree: Path, *, dry_run: bool, options: Mapping | None,
    ) -> WorktreePrep:
        trust = (options or {}).get("worktree_trust") or {}
        if not trust.get("enabled"):
            return WorktreePrep()
        path = Path(trust["file"]).expanduser()
        try:
            data = base.load_json_object(path)
        except UnreadableConfigError as exc:
            return WorktreePrep([f"not carrying agy folder trust: {exc}"])
        trusted = data.get(trust["key"])
        if not isinstance(trusted, list):
            return WorktreePrep([
                f"agy has no trusted folders recorded in {path}; trust the clone in agy first"
            ])
        real = {os.path.realpath(item) for item in trusted if isinstance(item, str)}
        if os.path.realpath(clone) not in real:
            return WorktreePrep([
                f"{clone} is not trusted in agy, so the worktree is not trusted either"
            ])
        if os.path.realpath(worktree) in real:
            return WorktreePrep()
        if dry_run:
            return WorktreePrep([f"would trust {worktree} in agy ({path})"])
        data[trust["key"]] = [*trusted, str(worktree)]
        base.write_json_object(path, data)
        return WorktreePrep([
            f"trusted {worktree} in agy, carried over from the clone; a running agy "
            f"may rewrite {path} and drop it"
        ])

    def release_worktree(self, worktree: Path, *, options: Mapping | None = None) -> list[str]:
        """Opt-in counterpart of `prepare_worktree`, run after the worktree is
        removed: drop its path from agy's trusted folders. Only that list is
        touched; a missing or unreadable file is a note, not an error."""
        trust = (options or {}).get("worktree_trust") or {}
        if not trust.get("enabled"):
            return []
        path = Path(trust["file"]).expanduser()
        if not path.exists():
            return [f"agy settings {path} not found; no folder trust to drop"]
        try:
            data = base.load_json_object(path)
            trusted = data.get(trust["key"])
            if not isinstance(trusted, list):
                return []
            target = os.path.realpath(worktree)
            kept = [
                item for item in trusted
                if not (isinstance(item, str) and os.path.realpath(item) == target)
            ]
            if len(kept) == len(trusted):
                return []
            data[trust["key"]] = kept
            base.write_json_object(path, data)
        except (UnreadableConfigError, OSError) as exc:
            return [f"not dropping agy folder trust: {exc}"]
        return [f"dropped agy folder trust for {worktree}"]

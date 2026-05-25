"""Push-style wake-on-send.

When a peer sends a message, the bus looks up the recipient's wake
command (if any) and fires it as a detached fire-and-forget subprocess.
The wake command is whatever the user's setup makes feasible:

  - emacs/vterm:    emacsclient -e "(with-current-buffer ...)"
  - desktop:        notify-send agent-bus "$AGENT_BUS_BODY_PREVIEW"
  - tmux:           tmux send-keys -t main:agents.0 "check inbox" Enter
  - screen:         screen -S agent -X stuff "check inbox\\n"
  - anything else:  webhook curl, custom script, ntfy push…

Design constraints (see [[agent-bus-client-neutral]]):
  - Generic MCP, no Claude-Code-only features. The wake fires from the
    sender's MCP server process (or from `agent-bus send` in a shell)
    regardless of which client the *recipient* uses.
  - Opt-in per agent. No wake.json entry → no command runs → existing
    behaviour unchanged.
  - Latency-sensitive: subprocess.Popen returns immediately, so the
    sender never blocks on the recipient's wake.

The wake command is invoked through `/bin/sh -c`, so any shell features
work. The full message body goes on stdin as JSON; a 200-char preview
plus the routing metadata go on environment variables for ergonomic
templates. Wake commands run as the local user — treat wake.json as
sensitive in the same way you'd treat ~/.bashrc.

If a wake fires (or fails to fire), the audit log records an `op="wake"`
row alongside the `send` rows, so every push attempt is traceable.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from .paths import db_path, ensure_parents

WAKE_CONFIG_FILENAME = "wake.json"
BODY_PREVIEW_LIMIT = 200


def wake_config_path() -> Path:
    """Lives alongside the bus.db so test fixtures pick up the override
    via AGENT_BUS_DB automatically."""
    return db_path().parent / WAKE_CONFIG_FILENAME


def load_wake_config(*, path: Path | None = None) -> dict[str, Any]:
    """Parse wake.json. Returns {} on any failure (missing, malformed,
    permission denied) — wake is opt-in and a broken config should not
    break the bus."""
    p = path or wake_config_path()
    if not p.exists():
        return {}
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError:
        return {}
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def save_wake_config(cfg: dict[str, Any], *, path: Path | None = None) -> None:
    p = path or wake_config_path()
    ensure_parents(p)
    p.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _resolve_command(entry: Any) -> str | None:
    """A wake.json value may be a plain command string, a dict with a
    `command` field (and optional metadata for future use), or null/false
    to explicitly disable. Anything else is ignored."""
    if entry is None or entry is False:
        return None
    if isinstance(entry, str):
        return entry.strip() or None
    if isinstance(entry, dict):
        cmd = entry.get("command")
        if isinstance(cmd, str) and cmd.strip():
            return cmd.strip()
    return None


def fire_wake(
    agent: str,
    *,
    from_agent: str,
    to_agent: str,
    body: str,
    thread_id: str,
    message_id: str,
    config: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """Look up `agent` in wake.json and run the configured command.

    Returns ``(fired, status)``:
      - ``(False, "no-config")``  : agent has no entry in wake.json
      - ``(False, "disabled")``   : entry is explicitly null/false/empty
      - ``(True,  "fired:OK")``   : subprocess launched
      - ``(False, "fired:ERR…")`` : Popen raised before launch

    Note: ``"fired:OK"`` only means the process was launched, not that
    the wake command succeeded. Exit code is not awaited — that would
    defeat the fire-and-forget contract. Callers can inspect the audit
    log to confirm wakes were attempted; debugging a wake command's
    output is the user's job (we redirect stdout/stderr to /dev/null
    on purpose so a slow or noisy wake never floods the MCP transport).
    """
    cfg = config if config is not None else load_wake_config()
    if agent not in cfg:
        return False, "no-config"
    cmd = _resolve_command(cfg[agent])
    if cmd is None:
        return False, "disabled"

    env = os.environ.copy()
    env.update(
        {
            "AGENT_BUS_FROM": from_agent,
            "AGENT_BUS_TO": to_agent,
            "AGENT_BUS_THREAD_ID": thread_id,
            "AGENT_BUS_MESSAGE_ID": message_id,
            "AGENT_BUS_BODY_PREVIEW": body[:BODY_PREVIEW_LIMIT],
        }
    )
    payload = json.dumps(
        {
            "from": from_agent,
            "to": to_agent,
            "thread_id": thread_id,
            "message_id": message_id,
            "body": body,
        },
        ensure_ascii=False,
    ).encode("utf-8")

    try:
        proc = subprocess.Popen(
            cmd,
            shell=True,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,  # detach from agent-bus's process group
        )
    except (OSError, ValueError) as e:
        return False, f"fired:ERR:{type(e).__name__}"

    # write the JSON payload to stdin, then immediately close. We don't
    # wait for the process — fire-and-forget.
    try:
        if proc.stdin is not None:
            proc.stdin.write(payload)
            proc.stdin.close()
    except (BrokenPipeError, OSError):
        pass

    return True, "fired:OK"

"""Claude Code hook handlers.

Both hooks are exposed as `agent-bus hook-stop` and
`agent-bus hook-user-prompt`. They read $AGENT_BUS_NAME from env to know
whose inbox to drain, and they tag the corresponding `read` audit row
with a sibling `deliver` row so the log shows the hook path.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Iterable, TextIO

from .identity import KIND_BROADCAST, KIND_GROUP
from .storage import Message, Storage


def _agent_name() -> str:
    name = os.environ.get("AGENT_BUS_NAME")
    if not name:
        sys.stderr.write(
            "agent-bus hook: AGENT_BUS_NAME is unset. The Stop / "
            "UserPromptSubmit hook needs to know which agent to drain.\n"
        )
        raise SystemExit(2)
    return name


def _drain_stdin(stdin: TextIO) -> None:
    """Consume the hook payload so Claude Code's pipe doesn't block.

    We don't actually need the payload contents today — the agent name
    comes from env — but reading it keeps the contract clean.
    """
    try:
        stdin.read()
    except Exception:
        # Hook input is best-effort; never block on a bad pipe.
        pass


GROUP_LEGEND = (
    "(A message to everyone in a repo is handled by the first agent there "
    "to see it. You saw these first, so they are yours.)"
)
FYI_HEADER = (
    "For your information only — a repo-mate already has these; act only "
    "if one concerns your own work:"
)


def _audience(m: Message) -> str:
    if m.kind == KIND_GROUP:
        return f" to everyone in {m.addressed_to}"
    if m.kind == KIND_BROADCAST:
        return " to everyone on the bus"
    return ""


def _format_message(m: Message) -> str:
    return (
        f"- from {m.from_agent}{_audience(m)} at {m.sent_at} "
        f"(thread {m.thread_id}): {m.body}"
    )


def _format_messages(msgs: Iterable[Message], reader: str) -> str:
    """Messages the reader must handle first, then the ones a repo-mate
    already claimed."""
    mine: list[Message] = []
    taken: list[Message] = []
    for m in msgs:
        (taken if m.is_claimed_by_other(reader) else mine).append(m)

    lines = [_format_message(m) for m in mine]
    if any(m.kind == KIND_GROUP for m in mine):
        lines.append(GROUP_LEGEND)
    if taken:
        lines.append(FYI_HEADER)
        lines.extend(
            f"{_format_message(m)} [already picked up by {m.claimed_by}]"
            for m in taken
        )
    return "\n".join(lines)


def _drain(store: Storage, name: str, *, actionable_only: bool = False) -> list[Message]:
    # a hook firing is a sign of life: some clients never call an MCP tool
    # between turns, and fan-out skips agents that look long gone
    store.touch_agent(name)
    return store.read_inbox(
        agent=name, mark_read=True, actor=name, also_deliver=True,
        actionable_only=actionable_only,
    )


def run_hook_user_prompt(
    *,
    storage: Storage | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    actor: str | None = None,
) -> int:
    """Surface pending peer messages as extra context for the next turn.

    Output is appended to the user's prompt by Claude Code. Exit code is
    always 0 — we never want this hook to block a user submission.
    """
    name = actor or _agent_name()
    store = storage or Storage()
    out = stdout or sys.stdout
    _drain_stdin(stdin or sys.stdin)

    msgs = _drain(store, name)
    if not msgs:
        return 0

    header = f"[agent-bus] {len(msgs)} new message(s) since last turn:"
    out.write(header + "\n" + _format_messages(msgs, name) + "\n")
    out.flush()
    return 0


def run_hook_stop(
    *,
    storage: Storage | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    actor: str | None = None,
) -> int:
    """Block the Stop event if there are pending peer messages.

    Empty inbox → exit 0 silently (allow the stop). Non-empty inbox →
    emit a JSON decision payload so Claude keeps the turn open and
    handles the messages.
    """
    name = actor or _agent_name()
    store = storage or Storage()
    out = stdout or sys.stdout
    _drain_stdin(stdin or sys.stdin)

    # only what this agent must act on: a copy a repo-mate already claimed
    # must not keep a second agent's turn open
    msgs = _drain(store, name, actionable_only=True)
    if not msgs:
        return 0

    reason = (
        "Pending messages from peers — handle them before stopping:\n"
        + _format_messages(msgs, name)
    )
    payload = {"decision": "block", "reason": reason}
    out.write(json.dumps(payload) + "\n")
    out.flush()
    return 0

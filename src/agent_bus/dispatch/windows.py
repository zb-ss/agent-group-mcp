"""Close the tmux window of a dispatch-started session that finished long ago.

The rule: fail closed. A window is closed only when the session is live and
reported `done` for the configured time, the window is the only one of its
name with a single pane, nobody is looking at it, and the pane's last lines
still show the client's own idle prompt (so no unsent input is lost). Anything
unclear keeps the window. The session's `done` stamp is never touched, and
pane text is never returned, logged or stored: it only meets the pattern.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

from .. import status, tmux
from ..commands import Runner

Recheck = Callable[[str], Any]


def _is_candidate(row: Any, *, now: datetime, idle_minutes: float) -> bool:
    if not (row.spawned and row.role is None and row.live):
        return False
    if row.state != "done" or row.reported_state != "done":
        return False
    if not (row.tmux_session and row.tmux_window):
        return False
    if getattr(row, "pending_count", 0):
        return False  # unread messages: the session has work it has not seen
    reported = status._parse_ts(row.state_at)
    return reported is not None and (now - reported).total_seconds() >= idle_minutes * 60


def _kept(row: Any, reason: str) -> dict:
    return {
        "session": row.name, "window": row.tmux_window, "result": "kept", "reason": reason,
    }


def _window_reason(found: Sequence[tmux.WindowInfo]) -> str | None:
    if not found:
        return "no such window"
    if len(found) > 1:
        return "the window name is shared by several windows"
    if found[0].panes != 1:
        return "the window has several panes"
    if found[0].active and found[0].session_attached:
        return "the window is shown in an attached session"
    return None


def _still_candidate(row: Any, fresh: Any, *, now: datetime, idle_minutes: float) -> bool:
    return (
        fresh is not None
        and fresh.state_at == row.state_at
        and _is_candidate(fresh, now=now, idle_minutes=idle_minutes)
    )


def _check(
    row: Any, *, cfg: Mapping, now: datetime, run: Runner, recheck: Recheck,
    idle_minutes: float,
) -> tuple[str | None, str | None]:
    """(reason to keep, id of the window to close); exactly one is set."""
    template = cfg["spawn"]["clients"].get(row.client) or {}
    idle = template.get("idle_prompt")
    if not idle:
        return f"no idle prompt pattern for {row.client}", None
    common = {"tmux_bin": cfg["spawn"]["tmux_bin"], "timeout": cfg["spawn"]["tmux_timeout_seconds"],
              "run": run}
    found = tmux.windows(row.tmux_session, row.tmux_window, **common)
    reason = _window_reason(found)
    if reason:
        return reason, None
    if not _still_candidate(row, recheck(row.name), now=now, idle_minutes=idle_minutes):
        return "the session changed state during the check", None
    text = tmux.capture_pane(
        found[0].id, lines=idle["tail_lines"], escapes=idle["escapes"], **common
    )
    if not re.search(idle["pattern"], text, re.MULTILINE):
        return "the prompt is not idle or holds input", None
    return None, found[0].id


def sweep(
    rows: Sequence[Any], *, cfg: Mapping, now: datetime, run: Runner, dry_run: bool,
    recheck: Recheck,
) -> list[dict]:
    """One result per candidate row: closed, would_close or kept (with why)."""
    limits = cfg["dispatch"]["window_cleanup"]
    common = {"tmux_bin": cfg["spawn"]["tmux_bin"], "timeout": cfg["spawn"]["tmux_timeout_seconds"],
              "run": run}
    results: list[dict] = []
    closed = 0
    for row in rows:
        if closed >= limits["max_per_run"]:
            break
        if not _is_candidate(row, now=now, idle_minutes=limits["idle_minutes"]):
            continue
        entry = {"session": row.name, "window": row.tmux_window}
        try:
            reason, window_id = _check(
                row, cfg=cfg, now=now, run=run, recheck=recheck,
                idle_minutes=limits["idle_minutes"],
            )
            if reason:
                results.append(_kept(row, reason))
                continue
            if dry_run:
                results.append({**entry, "result": "would_close"})
                continue
            tmux.kill_window(window_id, **common)
        except tmux.TmuxError as exc:
            results.append(_kept(row, type(exc).__name__))
            continue
        closed += 1
        results.append({**entry, "result": "closed"})
    return results

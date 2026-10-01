"""Tunables read from the environment, with their defaults in one place."""

from __future__ import annotations

import os

FANOUT_MAX_IDLE_DAYS_ENV = "AGENT_BUS_FANOUT_MAX_IDLE_DAYS"
DEFAULT_FANOUT_MAX_IDLE_DAYS = 14


def fanout_max_idle_days() -> int:
    """Group and broadcast sends skip a client that has not been seen for
    this many days, as long as a more recently seen client shares its repo.
    0 disables the cutoff. An unparseable value falls back to the default
    rather than breaking a send."""
    raw = os.environ.get(FANOUT_MAX_IDLE_DAYS_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_FANOUT_MAX_IDLE_DAYS
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_FANOUT_MAX_IDLE_DAYS


HOOK_PAYLOAD_TIMEOUT_ENV = "AGENT_BUS_HOOK_PAYLOAD_TIMEOUT"
DEFAULT_HOOK_PAYLOAD_TIMEOUT_SECONDS = 5.0


def hook_payload_timeout_seconds() -> float:
    """How long a hook waits for its input payload before going on without
    it. The agent's turn is blocked while a hook runs, so this stays short;
    a lost payload costs only the repo hint, which resolution recovers from
    the working directory. An unparseable or negative value falls back to
    the default rather than breaking the hook."""
    raw = os.environ.get(HOOK_PAYLOAD_TIMEOUT_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_HOOK_PAYLOAD_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_HOOK_PAYLOAD_TIMEOUT_SECONDS
    return value if value >= 0 else DEFAULT_HOOK_PAYLOAD_TIMEOUT_SECONDS


SESSIONS_ENV = "AGENT_BUS_SESSIONS"


def sessions_enabled() -> bool:
    """Whether each MCP server claims a session address of its own
    (`<repo>/<client>-<n>`). Set to 0 to go back to one shared identity
    per client and repo."""
    raw = os.environ.get(SESSIONS_ENV)
    if raw is None or not raw.strip():
        return True
    return raw.strip().lower() not in {"0", "false", "no", "off"}


SESSION_RESUME_HOURS_ENV = "AGENT_BUS_SESSION_RESUME_HOURS"
DEFAULT_SESSION_RESUME_HOURS = 1.0


def session_resume_hours() -> float:
    """How long an ended session's address stays reserved for the same
    client process or session id coming back — a restarted MCP server, a
    resumed conversation. A new session takes a reserved number only when
    every other one is taken. Mail is never held back meanwhile: a
    numbered session's mail goes to the shared address at once, and a
    labelled one keeps its address regardless. 0 turns the reservation
    off."""
    raw = os.environ.get(SESSION_RESUME_HOURS_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_SESSION_RESUME_HOURS
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_SESSION_RESUME_HOURS


PROCESS_LOOKUP_TIMEOUT_ENV = "AGENT_BUS_PROCESS_LOOKUP_TIMEOUT"
DEFAULT_PROCESS_LOOKUP_TIMEOUT_SECONDS = 2.0


def process_lookup_timeout_seconds() -> float:
    """How long to wait for `ps` where there is no /proc (macOS). A hook
    runs inside the agent's turn, so a stuck `ps` must not hold it; when
    the lookup gives up, the hook drains the shared client address only."""
    raw = os.environ.get(PROCESS_LOOKUP_TIMEOUT_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_PROCESS_LOOKUP_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_PROCESS_LOOKUP_TIMEOUT_SECONDS
    return value if value > 0 else DEFAULT_PROCESS_LOOKUP_TIMEOUT_SECONDS

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

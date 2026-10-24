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


NATIVE_WAKE_ENV = "AGENT_BUS_NATIVE_WAKE"


def native_wake_enabled() -> bool:
    """Whether an idle agent is woken through its client's own mechanism
    when mail arrives — a background hook for Claude Code, a queued turn
    for Codex, a plugin for opencode. Read by `init`, which then leaves
    the wake hooks out, and by the wake paths themselves, so setting it
    to 0 also stops wiring already written."""
    raw = os.environ.get(NATIVE_WAKE_ENV)
    if raw is None or not raw.strip():
        return True
    return raw.strip().lower() not in {"0", "false", "no", "off"}


WATCH_POLL_ENV = "AGENT_BUS_WATCH_POLL_SECONDS"
DEFAULT_WATCH_POLL_SECONDS = 2.0


def watch_poll_seconds() -> float:
    """How often a waiting watcher looks for new mail: the longest a
    message waits before it wakes an idle agent. One indexed read per
    look, so a short interval costs little."""
    raw = os.environ.get(WATCH_POLL_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_WATCH_POLL_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_WATCH_POLL_SECONDS
    return value if value > 0 else DEFAULT_WATCH_POLL_SECONDS


WATCH_HOURS_ENV = "AGENT_BUS_WATCH_HOURS"
DEFAULT_WATCH_HOURS = 24.0


def watch_hours() -> float:
    """How long one watcher waits before giving up. An agent idle for
    longer than this is not woken until its next turn starts a new
    watcher. Written into the client's config by `init` (Claude Code
    kills a background hook at its configured timeout), so a change
    takes effect on the next `init`."""
    raw = os.environ.get(WATCH_HOURS_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_WATCH_HOURS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_WATCH_HOURS
    return value if value > 0 else DEFAULT_WATCH_HOURS


WAKES_PER_HOUR_ENV = "AGENT_BUS_WAKES_PER_HOUR"
DEFAULT_WAKES_PER_HOUR = 20


def wakes_per_hour() -> int:
    """At most this many native wakes per agent in any hour. Two agents
    that answer each other on every wake would otherwise keep each other
    busy indefinitely; past the limit, mail waits for the agent's next
    turn instead. 0 removes the limit. Commands in wake.json are not
    counted: they are configured by hand."""
    raw = os.environ.get(WAKES_PER_HOUR_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_WAKES_PER_HOUR
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_WAKES_PER_HOUR


CLAUDE_BIN_ENV = "AGENT_BUS_CLAUDE_BIN"
DEFAULT_CLAUDE_BIN = "claude"


def claude_bin() -> str:
    """The Claude Code executable `init` asks for its version, to decide
    whether its hooks can wake a session."""
    raw = os.environ.get(CLAUDE_BIN_ENV, "").strip()
    return raw or DEFAULT_CLAUDE_BIN


CLIENT_VERSION_TIMEOUT_ENV = "AGENT_BUS_CLIENT_VERSION_TIMEOUT"
DEFAULT_CLIENT_VERSION_TIMEOUT_SECONDS = 10.0


def client_version_timeout_seconds() -> float:
    """How long `init` waits for a client to report its version. A client
    that does not answer in time is treated as too old for native wake."""
    raw = os.environ.get(CLIENT_VERSION_TIMEOUT_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_CLIENT_VERSION_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_CLIENT_VERSION_TIMEOUT_SECONDS
    return value if value > 0 else DEFAULT_CLIENT_VERSION_TIMEOUT_SECONDS


CODEX_BIN_ENV = "AGENT_BUS_CODEX_BIN"


def codex_bin() -> str | None:
    """The Codex executable a sender runs to queue a turn for an idle
    Codex session. Unset, the executable the session itself runs is used,
    as its hooks recorded it — which works even where the `codex` on PATH
    is a wrapper script needing an interpreter the sender lacks."""
    raw = os.environ.get(CODEX_BIN_ENV, "").strip()
    return raw or None


AGY_BIN_ENV = "AGENT_BUS_AGY_BIN"


def agy_bin() -> str | None:
    """The agy executable a sender runs to start a turn in an idle agy
    session. Unset, the executable the session itself runs is used, as its
    hooks recorded it."""
    raw = os.environ.get(AGY_BIN_ENV, "").strip()
    return raw or None

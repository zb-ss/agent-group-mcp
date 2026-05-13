"""Shared formatting helpers used by the CLI subcommands and the chat TUI.

All public functions here are pure (no I/O, no side effects) so they're
trivial to unit-test. The chat TUI converts the FormattedText results
into prompt_toolkit's rendering tree; the plain CLI subcommands convert
them to ANSI escapes for stdout.
"""

from __future__ import annotations

import hashlib
import sys
from datetime import datetime, timezone
from typing import Iterable

# Curated palette for per-agent colors. Avoids red (= errors) and white
# (= system messages). Mapped via hashlib so the same agent name always
# renders in the same color across all subcommands and processes.
AGENT_PALETTE: tuple[str, ...] = (
    "ansicyan",
    "ansigreen",
    "ansiyellow",
    "ansibrightblue",
    "ansibrightmagenta",
    "ansibrightcyan",
    "ansibrightgreen",
    "ansibrightyellow",
)

# 8-bit ANSI fallbacks for plain-stdout rendering (CLI subcommands).
_ANSI_FALLBACK: dict[str, str] = {
    "ansicyan": "\033[36m",
    "ansigreen": "\033[32m",
    "ansiyellow": "\033[33m",
    "ansibrightblue": "\033[94m",
    "ansibrightmagenta": "\033[95m",
    "ansibrightcyan": "\033[96m",
    "ansibrightgreen": "\033[92m",
    "ansibrightyellow": "\033[93m",
    "ansired": "\033[31m",
    "ansibrightred": "\033[91m",
    "ansiwhite": "\033[97m",
    "ansibrightblack": "\033[90m",  # used as "dim"
}
_ANSI_RESET = "\033[0m"
_ANSI_DIM = _ANSI_FALLBACK["ansibrightblack"]
_ANSI_BOLD = "\033[1m"


THREAD_ID_LEN = 8
SUBJECT_LIMIT = 72


def color_for(name: str) -> str:
    """Deterministic palette pick for an agent name.

    Uses sha1 so the choice is stable across runs (Python's built-in
    `hash()` is randomized per process).
    """
    digest = hashlib.sha1(name.encode("utf-8")).digest()
    return AGENT_PALETTE[digest[0] % len(AGENT_PALETTE)]


def short_thread(thread_id: str | None) -> str:
    if not thread_id:
        return ""
    return thread_id[:THREAD_ID_LEN]


def truncate(s: str, limit: int = SUBJECT_LIMIT) -> str:
    s = s.replace("\n", " ").replace("\r", " ")
    if len(s) <= limit:
        return s
    return s[: limit - 1].rstrip() + "…"


# ----------------------------- timestamps --------------------------------


def parse_iso(ts: str) -> datetime | None:
    """Parse the ISO timestamps storage writes (`...Z`). Returns None on bad input."""
    if not ts:
        return None
    raw = ts
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def humanize_clock(ts: str, *, tz: timezone | None = None) -> str:
    """HH:MM:SS in the given timezone (default: local)."""
    dt = parse_iso(ts)
    if dt is None:
        return ts
    if tz is None:
        return dt.astimezone().strftime("%H:%M:%S")
    return dt.astimezone(tz).strftime("%H:%M:%S")


def humanize_relative(ts: str, *, now: datetime | None = None) -> str:
    """e.g. `just now`, `3m ago`, `2h ago`, `1d ago`."""
    dt = parse_iso(ts)
    if dt is None:
        return ts
    if now is None:
        now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = (now - dt).total_seconds()
    if delta < 0:
        return "just now"  # clock skew → treat as now
    if delta < 5:
        return "just now"
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    days = int(delta // 86400)
    if days < 7:
        return f"{days}d ago"
    if days < 30:
        return f"{days // 7}w ago"
    return dt.astimezone().strftime("%Y-%m-%d")


# ----------------------------- prompt_toolkit fragments ------------------


Fragment = tuple[str, str]  # (style_class, text)


def fragments_for_message(
    *,
    sent_at: str,
    from_agent: str,
    to_agent: str,
    body: str,
    thread_id: str | None,
    name_col: int = 18,
    target_col: int = 18,
    show_thread: bool = True,
) -> list[Fragment]:
    """Build a one-line FormattedText representation of a message.

    name_col and target_col pad the columns so multi-message output
    aligns regardless of name length.
    """
    out: list[Fragment] = []
    out.append(("class:ts", f"{humanize_clock(sent_at)}  "))
    out.append((f"class:agent-{from_agent}", from_agent.ljust(name_col)))
    out.append(("class:arrow", " → "))
    out.append((f"class:target-{to_agent}", to_agent.ljust(target_col)))
    out.append(("", "  "))
    out.append(("", truncate(body, SUBJECT_LIMIT)))
    if show_thread and thread_id:
        out.append(("class:thread", f"  [{short_thread(thread_id)}]"))
    return out


def fragments_for_audit_row(row: dict, *, name_col: int = 18) -> list[Fragment]:
    op = row.get("op", "?")
    sent_at = row.get("ts", "")
    actor = row.get("actor") or row.get("from") or "?"
    src = row.get("from") or actor
    dst = row.get("to") or "*"
    body = row.get("body_preview") or ""
    thread_id = row.get("thread_id")

    op_class = {
        "send": "class:op-send",
        "read": "class:op-read",
        "deliver": "class:op-deliver",
    }.get(op, "class:op-other")

    out: list[Fragment] = []
    out.append(("class:ts", f"{humanize_clock(sent_at)}  "))
    out.append((op_class, f"{op:<7}"))
    out.append((f"class:agent-{src}", src.ljust(name_col)))
    out.append(("class:arrow", " → "))
    out.append((f"class:target-{dst}", dst.ljust(name_col)))
    out.append(("", "  "))
    out.append(("class:body", truncate(body, SUBJECT_LIMIT)))
    if thread_id:
        out.append(("class:thread", f"  [{short_thread(thread_id)}]"))
    return out


def style_map(names: Iterable[str]) -> dict[str, str]:
    """Build a prompt_toolkit style dict that paints each agent name
    in its deterministic palette color."""
    style: dict[str, str] = {
        "ts": "ansibrightblack",
        "arrow": "ansibrightblack",
        "thread": "ansibrightblack",
        "body": "",
        "op-send": "ansibrightyellow bold",
        "op-read": "ansibrightblack",
        "op-deliver": "ansibrightgreen",
        "op-other": "ansibrightmagenta",
        "system": "ansibrightblack italic",
        "prompt": "ansibrightcyan bold",
        "bottom-toolbar": "bg:#222222 ansiwhite",
    }
    seen: set[str] = set()
    for n in names:
        if n in seen or not n:
            continue
        seen.add(n)
        color = color_for(n)
        style[f"agent-{n}"] = f"{color} bold"
        style[f"target-{n}"] = color
    # broadcast target gets a neutral hue
    style["target-*"] = "ansibrightmagenta bold"
    return style


# ----------------------------- plain ANSI rendering ---------------------


def _supports_color(stream=None) -> bool:
    stream = stream or sys.stdout
    return hasattr(stream, "isatty") and stream.isatty()


def render_plain(
    fragments: list[Fragment], *, use_color: bool | None = None, stream=None
) -> str:
    """Render FormattedText fragments to a plain string with ANSI escapes
    when the destination is a TTY (otherwise: text only)."""
    use_color = _supports_color(stream) if use_color is None else use_color
    out: list[str] = []
    for style_class, text in fragments:
        if not text:
            continue
        if not use_color or not style_class:
            out.append(text)
            continue
        # very small style→ANSI mapper. Mirrors what style_map() emits.
        if style_class.startswith("class:agent-"):
            name = style_class[len("class:agent-"):]
            out.append(f"{_ANSI_FALLBACK[color_for(name)]}{_ANSI_BOLD}{text}{_ANSI_RESET}")
        elif style_class.startswith("class:target-"):
            name = style_class[len("class:target-"):]
            if name == "*":
                out.append(f"{_ANSI_FALLBACK['ansibrightmagenta']}{_ANSI_BOLD}{text}{_ANSI_RESET}")
            else:
                out.append(f"{_ANSI_FALLBACK[color_for(name)]}{text}{_ANSI_RESET}")
        elif style_class in ("class:ts", "class:thread", "class:arrow"):
            out.append(f"{_ANSI_DIM}{text}{_ANSI_RESET}")
        elif style_class == "class:op-send":
            out.append(f"{_ANSI_FALLBACK['ansibrightyellow']}{_ANSI_BOLD}{text}{_ANSI_RESET}")
        elif style_class == "class:op-read":
            out.append(f"{_ANSI_DIM}{text}{_ANSI_RESET}")
        elif style_class == "class:op-deliver":
            out.append(f"{_ANSI_FALLBACK['ansibrightgreen']}{text}{_ANSI_RESET}")
        elif style_class == "class:op-other":
            out.append(f"{_ANSI_FALLBACK['ansibrightmagenta']}{text}{_ANSI_RESET}")
        elif style_class == "class:system":
            out.append(f"{_ANSI_DIM}{text}{_ANSI_RESET}")
        elif style_class == "class:prompt":
            out.append(f"{_ANSI_FALLBACK['ansibrightcyan']}{_ANSI_BOLD}{text}{_ANSI_RESET}")
        elif style_class == "class:body":
            out.append(text)
        else:
            out.append(text)
    return "".join(out)

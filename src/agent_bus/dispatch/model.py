"""Tracker-neutral task model and small pure helpers."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone

from ..storage import TIMESTAMP_FORMAT

OUTCOME_SPAWNED = "spawned"
OUTCOME_SPAWN_FAILED = "spawn_failed"
OUTCOME_NOTIFIED = "notified"
OUTCOME_DONE = "done"
OUTCOME_ENDED = "ended"

# Stored in handled_types of an item whose author the source does not trust.
# Agent-facing surfaces (overlap lists, status output) show a placeholder
# instead of its title; no schema change, and not a task type.
UNTRUSTED_MARKER = "untrusted_author"
UNTRUSTED_TITLE = "(untrusted author)"


def is_untrusted(handled_types) -> bool:
    return UNTRUSTED_MARKER in handled_types


@dataclass(frozen=True)
class Membership:
    project_id: str
    project_name: str
    section_name: str | None


@dataclass(frozen=True)
class Task:
    """A tracker task. Both timestamps are UTC strings in TIMESTAMP_FORMAT,
    so plain string comparison orders them."""

    source: str
    task_id: str
    title: str
    url: str
    due_on: str | None  # YYYY-MM-DD
    modified_at: str | None
    newest_comment_at: str | None
    memberships: tuple[Membership, ...] = ()
    type_hint: str | None = None  # a dispatch type the source chose for the item
    spawn_allowed: bool = True  # False when the source's author gate forbids a session


def to_timestamp(value: str | None) -> str | None:
    """Normalise an ISO-8601 string to a UTC TIMESTAMP_FORMAT string."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime(TIMESTAMP_FORMAT)


def format_now(now: datetime) -> str:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc).strftime(TIMESTAMP_FORMAT)


def _flatten(text: str) -> str:
    flattened = "".join(
        " " if unicodedata.category(ch).startswith("C") else ch for ch in text
    )
    return " ".join(flattened.split())


def _truncate(text: str, max_chars: int) -> str:
    if len(text) > max_chars:
        return text[:max_chars].rstrip() + "..."
    return text


def clean_text(text: str, max_chars: int) -> str:
    """Flatten untrusted tracker text to one printable line of bounded size."""
    return _truncate(_flatten(text), max_chars)


_DASH_RUN = re.compile(r"-{3,}")


def clean_untrusted(text: str, max_chars: int) -> str:
    """`clean_text` for values that go into a prompt: runs of three or more
    dashes also collapse to one, so a value cannot spell the template's
    `--- begin/end untrusted data ---` markers inline."""
    return _truncate(_DASH_RUN.sub("-", _flatten(text)), max_chars)


_NEWLINE_RUN = re.compile(r"\n{3,}")


def clean_multiline(text: str, max_chars: int) -> str:
    """Keep the line structure of untrusted text but nothing that can forge
    markup: control and line/paragraph separators become spaces, newline runs
    shrink to one blank line, and dash runs collapse like `clean_untrusted`."""
    unified = text.replace("\r\n", "\n").replace("\r", "\n")
    printable = "".join(
        " "
        if ch != "\n"
        and (unicodedata.category(ch).startswith("C") or unicodedata.category(ch) in ("Zl", "Zp"))
        else ch
        for ch in unified
    )
    printable = "\n".join(line.rstrip(" ") for line in printable.split("\n"))
    printable = _NEWLINE_RUN.sub("\n\n", printable)
    printable = _DASH_RUN.sub("-", printable)
    return _truncate(printable.strip(), max_chars)

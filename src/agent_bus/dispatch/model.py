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

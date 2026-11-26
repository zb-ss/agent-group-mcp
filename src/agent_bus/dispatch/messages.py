"""Message model, source protocol and the per-pass context.

Message text is untrusted: it reaches a prompt file only inside an
untrusted-data block and never an argv or an alert. Decisions and alerts carry
ids, rule names and links only."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from . import notify
from .model import format_now
from .notify import Notification


@dataclass(frozen=True)
class Message:
    source: str  # source instance name
    item_id: str
    container: str  # Slack channel id, mail folder, later a repository
    author: str
    author_kind: str  # "user" or "bot"
    text: str
    at: str  # UTC, TIMESTAMP_FORMAT
    url: str = ""
    title: str = ""
    thread: str | None = None  # parent id when the item is a reply
    mentions: tuple[str, ...] = ()  # from: owner, channel, here
    # What a reply needs: Slack channel/thread_ts; email to/subject/in_reply_to/references.
    reply: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Batch:
    items: tuple[Message, ...]
    cursor: str | None
    errors: tuple[str, ...] = ()  # non-fatal, id-only lines for the run report


class MessageSource(Protocol):
    name: str
    kind: str
    schedule: Mapping | None

    def fetch(self, cursor: str | None, *, now: datetime) -> Batch:
        """Read-only on the remote side and on local mail."""
        ...

    def rules_for(self, message: Message) -> tuple[Sequence[Mapping], str]:
        """The rule list and the default action for that message."""
        ...


@dataclass
class PassCtx:
    cfg: Any
    dispatch: Any
    storage: Any
    store: Any
    notifiers: Any
    now: datetime
    dry_run: bool
    env: Any
    run: Any
    spawn_fn: Any
    send: Any
    rows: Any
    dispatcher: Any
    available: Any
    quota: Any
    http_send: Any | None = None
    scanner_fn: Any | None = None
    decisions: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    spawns: int = 0

    @property
    def stamp(self) -> str:
        return format_now(self.now)


def decision(
    ctx: PassCtx, message: Message, *, rule: str | None, action: str,
    result: str, reason: str | None = None,
) -> dict:
    """Record what happened to a message. No text, title or author."""
    entry = {
        "source": message.source,
        "item_id": message.item_id,
        "container": message.container,
        "rule": rule,
        "action": action,
        "result": result,
        "reason": reason,
        "url": message.url,
    }
    ctx.decisions.append(entry)
    return entry


STATES = {
    "message": "message",
    "draft_ready": "draft ready",
    "needs_owner": "needs you",
    "incident_opened": "incident",
    "incident_closed": "incident closed",
    "incident_report": "incident report",
}


def alert(
    ctx: PassCtx, event: str, *, source: str, title: str, url: str,
    session: str | None = None,
) -> bool:
    """Thin alert: source, state, a rule name or id, and a link. Callers pass
    only a rule name (or `mention`/`default`) and an id as `title`."""
    if event not in ctx.dispatch["notify_on"]:
        return True
    delivered, errors = notify.deliver(
        ctx.notifiers,
        Notification(session or source, STATES[event], title, url),
        max_chars=ctx.dispatch["title_max_chars"],
    )
    ctx.errors.extend(errors)
    return delivered

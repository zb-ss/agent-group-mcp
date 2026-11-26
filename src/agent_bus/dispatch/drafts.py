"""Drafts: text a model wrote from untrusted messages, waiting for review.

Nothing leaves the machine before an authorised approval (the verified
dispatcher or the owner's own shell). Every delivery attempt is audited, and
the audit rows never hold the text or a token. Email is placed in the Drafts
folder for the owner to send himself; this module never sends mail.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Any

from .. import resolution
from . import maildir, settings, slack
from .actor import ACTOR_CLI, ACTOR_DISPATCHER, actor_for
from .model import clean_multiline, clean_text
from .source import HttpSend, SourceError
from .store import (
    DRAFT_PENDING,
    DRAFT_PLACED,
    DRAFT_SENDING,
    DRAFT_SENT,
    DRAFT_DISCARDED,
    EVENT_DRAFT,
    EVENT_SEND,
    DispatchStore,
    DraftRecord,
)

KIND_SLACK = "slack"
KIND_EMAIL = "email"
ORIGIN_SCANNER = "scanner"
ORIGIN_INCIDENT = "incident"
ACTOR_RUNNER = "runner"

ACTOR_AGENT_SESSION = "agent-session"
# A refused caller's claimed session name is attribution only; it is cleaned
# and bounded like an address so it cannot forge a log line.
_ACTOR_MAX_CHARS = 200

_VIEW_FIELDS = ("id", "status", "kind", "source", "rule", "url", "origin", "created_at")
# Target fields the source itself assigns (a channel id, a message timestamp).
# Every other target field (an email's To, Subject, In-Reply-To, References)
# comes from the sender and is shown only as untrusted data.
_TRUSTED_TARGET_KEYS = ("channel", "thread_ts")
# Untrusted lines on a terminal carry this prefix, so no line of them can pass
# for the command's own output.
UNTRUSTED_PREFIX = "| "


class DraftError(Exception):
    """A draft operation the rules refuse, or a delivery that failed."""


class DraftRefused(DraftError):
    """The caller may not change drafts: an agent session without the dispatcher token."""


def kind_for(source_kind: str) -> str:
    if source_kind == "slack":
        return KIND_SLACK
    if source_kind in ("maildir", "imap"):
        return KIND_EMAIL
    raise DraftError(f"source type {source_kind} cannot hold drafts")


def _clean(text: str, max_chars: int) -> str:
    cleaned = clean_multiline(text, max_chars)
    if not cleaned:
        raise DraftError("draft text is empty")
    return cleaned


def _get(store: DispatchStore, draft_id: int) -> DraftRecord:
    draft = store.get_draft(draft_id)
    if draft is None:
        raise DraftError(f"unknown draft {draft_id}")
    return draft


def _pending(store: DispatchStore, draft_id: int) -> DraftRecord:
    draft = _get(store, draft_id)
    if draft.status != DRAFT_PENDING:
        raise DraftError(f"draft {draft_id} is not pending")
    return draft


def create(
    store: DispatchStore,
    *,
    source: str,
    item_id: str | None,
    kind: str,
    origin: str,
    rule: str | None,
    url: str | None,
    target: Mapping[str, Any],
    text: str,
    now: str,
    max_chars: int,
    incident_id: int | None = None,
) -> DraftRecord:
    draft = store.add_draft(
        source=source, item_id=item_id, kind=kind, origin=origin, rule=rule, url=url,
        target=target, text=_clean(text, max_chars), now=now, incident_id=incident_id,
    )
    store.log_event(
        at=now, event=EVENT_DRAFT, actor=ACTOR_RUNNER, source=source, task_id=item_id,
        detail={
            "op": "created", "draft_id": draft.id, "kind": kind, "origin": origin, "rule": rule,
        },
    )
    return draft


def _view(draft: DraftRecord) -> dict:
    return {name: getattr(draft, name) for name in _VIEW_FIELDS}


def _split_target(target: Mapping[str, Any]) -> tuple[dict, dict]:
    """(source-assigned fields, sender-controlled fields) of a draft target."""
    trusted = {k: v for k, v in target.items() if k in _TRUSTED_TARGET_KEYS}
    sender = {k: v for k, v in target.items() if k not in _TRUSTED_TARGET_KEYS}
    return trusted, sender


def _parse_stamp(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def is_stale(draft: DraftRecord, *, now: str, stale_minutes: int) -> bool:
    """A draft stuck in `sending`: its delivery never recorded a result."""
    if draft.status != DRAFT_SENDING:
        return False
    return _parse_stamp(now) - _parse_stamp(draft.updated_at) > timedelta(minutes=stale_minutes)


def listing(
    store: DispatchStore, *, now: str, stale_minutes: int, include_closed: bool = False
) -> list[dict]:
    """Open drafts (pending and sending), or every draft with include_closed.
    Each row says whether it is a stale `sending` draft."""
    if include_closed:
        rows = store.list_drafts()
    else:
        rows = [d for d in store.list_drafts() if d.status in (DRAFT_PENDING, DRAFT_SENDING)]
    return [
        {**_view(d), "stale": is_stale(d, now=now, stale_minutes=stale_minutes)} for d in rows
    ]


def show(store: DispatchStore, draft_id: int) -> dict:
    draft = _get(store, draft_id)
    trusted, sender = _split_target(draft.target)
    untrusted: dict[str, Any] = {"text": draft.text}
    if sender:
        untrusted["target"] = sender
    return {**_view(draft), "target": trusted, "untrusted": untrusted}


def untrusted_lines(draft: DraftRecord) -> list[str]:
    """The sender's header fields and the model-written text, each line
    prefixed so it cannot pass for the command's own output."""
    _, sender = _split_target(draft.target)
    lines = [f"{key}: {clean_text(str(value), len(str(value)) or 1)}"
             for key, value in sorted(sender.items())]
    if lines:
        lines.append("")
    lines.extend(draft.text.split("\n"))
    return [UNTRUSTED_PREFIX + line for line in lines]


def owner_actor(storage: Any, env: Mapping[str, str]) -> str:
    who = actor_for(storage, env)
    if who == ACTOR_DISPATCHER:
        return who
    if (env.get(resolution.SESSION_ENV) or "").strip() or (
        env.get(resolution.SPAWN_TOKEN_ENV) or ""
    ).strip():
        raise DraftRefused(
            "only the dispatcher or the owner's own shell may edit, approve, discard or reset "
            "a draft (this shell belongs to an agent session)"
        )
    return ACTOR_CLI


def refused_actor(env: Mapping[str, str]) -> str:
    """Who a refused change came from, for the decision log: the session the
    caller claims to be (cleaned, prefixed so it never reads as `dispatcher` or
    `cli`), else a generic agent session."""
    name = clean_text(env.get(resolution.SESSION_ENV) or "", _ACTOR_MAX_CHARS)
    return f"session:{name}" if name else ACTOR_AGENT_SESSION


def log_refused(
    store: DispatchStore, draft_id: int, *, operation: str, env: Mapping[str, str], now: str
) -> None:
    """Audit a refused draft change: the draft id, the requested operation and
    the caller. Never the draft text."""
    draft = store.get_draft(draft_id)
    store.log_event(
        at=now, event=EVENT_DRAFT, actor=refused_actor(env),
        source=draft.source if draft else None, task_id=draft.item_id if draft else None,
        detail={"op": "refused", "draft_id": draft_id, "operation": operation},
    )


def max_chars_for(draft: DraftRecord, dispatch_cfg: Mapping[str, Any]) -> int:
    """The length cap a draft was created under: incident reports keep the
    report limit, scanner replies the scanner's draft limit."""
    if draft.origin == ORIGIN_INCIDENT:
        return int(dispatch_cfg["incident"]["max_report_chars"])
    return int(dispatch_cfg["scanner"]["max_draft_chars"])


def edit(
    store: DispatchStore, draft_id: int, text: str, *, actor: str, now: str, max_chars: int
) -> DraftRecord:
    _pending(store, draft_id)
    cleaned = _clean(text, max_chars)
    draft = store.update_draft(draft_id, text=cleaned, updated_at=now)
    store.log_event(
        at=now, event=EVENT_DRAFT, actor=actor, source=draft.source, task_id=draft.item_id,
        detail={"op": "edited", "draft_id": draft_id, "chars": len(cleaned)},
    )
    return draft


def discard(store: DispatchStore, draft_id: int, *, actor: str, now: str) -> DraftRecord:
    _pending(store, draft_id)
    draft = store.update_draft(draft_id, status=DRAFT_DISCARDED, updated_at=now)
    store.log_event(
        at=now, event=EVENT_DRAFT, actor=actor, source=draft.source, task_id=draft.item_id,
        detail={"op": "discarded", "draft_id": draft_id},
    )
    return draft


def confirmation_summary(draft: DraftRecord, cfg: Mapping[str, Any]) -> list[str]:
    """What an approval would do: kind, destination, link, size."""
    target = draft.target
    if draft.kind == KIND_SLACK:
        destination = f"Slack channel {target.get('channel') or '-'}"
        if target.get("thread_ts"):
            destination += " (thread reply)"
    else:
        destination = f"mailbox folder {source_folder(draft, cfg) or '-'} (not sent)"
    return [
        f"draft {draft.id}: {draft.kind} from {draft.source}",
        f"  target: {destination}",
        f"  link: {draft.url or '-'}",
        f"  length: {len(draft.text)} characters",
    ]


def confirm_owner_approval(
    draft: DraftRecord,
    cfg: Mapping[str, Any],
    *,
    actor: str,
    assume_yes: bool,
    is_tty: Callable[[], bool],
    ask: Callable[[str], str],
    show: Callable[[str], None],
) -> None:
    """Gate an approval by the owner's shell behind a terminal and a typed yes.

    The verified dispatcher already carries the owner's authority and may pass
    assume_yes; for any other actor assume_yes is refused, so a script cannot
    skip the prompt.
    """
    lines = confirmation_summary(draft, cfg)
    lines.append(
        "  text below: written by a model from untrusted message data; "
        "check it before you approve"
    )
    lines.extend(untrusted_lines(draft))
    _confirm(
        lines, verb="approve", gerund="approving", noun="approval", actor=actor,
        assume_yes=assume_yes, is_tty=is_tty, ask=ask, show=show,
    )


def _confirm(
    lines: list[str],
    *,
    verb: str,
    gerund: str,
    noun: str,
    actor: str,
    assume_yes: bool,
    is_tty: Callable[[], bool],
    ask: Callable[[str], str],
    show: Callable[[str], None],
) -> None:
    if actor == ACTOR_DISPATCHER:
        return
    if assume_yes:
        raise DraftError("--yes is accepted only for the verified dispatcher")
    if not is_tty():
        raise DraftError(f"{gerund} a draft needs an interactive terminal")
    for line in lines:
        show(line)
    try:
        answer = ask(f"type yes to {verb}: ")
    except EOFError:
        raise DraftError(f"{noun} cancelled") from None
    if answer.strip().lower() != "yes":
        raise DraftError(f"{noun} cancelled")


def confirm_owner_reset(
    draft: DraftRecord,
    *,
    actor: str,
    assume_yes: bool,
    is_tty: Callable[[], bool],
    ask: Callable[[str], str],
    show: Callable[[str], None],
) -> None:
    """Gate a reset by the owner's shell like an approval: a reset draft can be
    approved again, so a delivery that did go through could be repeated."""
    lines = [
        f"draft {draft.id}: {draft.kind} from {draft.source}, sending since {draft.updated_at}",
        f"  link: {draft.url or '-'}",
        "  its delivery may have gone through: check the destination before approving it again",
    ]
    _confirm(
        lines, verb="reset", gerund="resetting", noun="reset", actor=actor,
        assume_yes=assume_yes, is_tty=is_tty, ask=ask, show=show,
    )


def pending_draft(store: DispatchStore, draft_id: int) -> DraftRecord:
    return _pending(store, draft_id)


def stale_draft(
    store: DispatchStore, draft_id: int, *, now: str, stale_minutes: int
) -> DraftRecord:
    """The draft, when it is stuck in `sending` past the stale limit."""
    draft = _get(store, draft_id)
    if draft.status != DRAFT_SENDING:
        raise DraftError(f"draft {draft_id} is not sending")
    if not is_stale(draft, now=now, stale_minutes=stale_minutes):
        raise DraftError(
            f"draft {draft_id} has been sending for less than {stale_minutes} minutes"
        )
    return draft


def reset(
    store: DispatchStore, draft_id: int, *, actor: str, now: str, stale_minutes: int
) -> DraftRecord:
    """Move a draft stuck in `sending` back to `pending`."""
    draft = stale_draft(store, draft_id, now=now, stale_minutes=stale_minutes)
    if not store.claim_draft(draft_id, from_status=DRAFT_SENDING, to_status=DRAFT_PENDING, now=now):
        raise DraftError(f"draft {draft_id} is not sending")
    store.log_event(
        at=now, event=EVENT_DRAFT, actor=actor, source=draft.source, task_id=draft.item_id,
        detail={"op": "reset", "draft_id": draft_id},
    )
    return _get(store, draft_id)


def approve(
    store: DispatchStore,
    cfg: Mapping[str, Any],
    draft_id: int,
    *,
    actor: str,
    now: str,
    env: Mapping[str, str],
    http_send: HttpSend | None = None,
) -> DraftRecord:
    draft = _pending(store, draft_id)
    store.update_draft(draft_id, approved_at=now, approved_by=actor, updated_at=now)
    store.log_event(
        at=now, event=EVENT_DRAFT, actor=actor, source=draft.source, task_id=draft.item_id,
        detail={"op": "approved", "draft_id": draft_id},
    )
    return deliver(store, cfg, _get(store, draft_id), actor=actor, now=now, env=env, http_send=http_send)


def _send(
    draft: DraftRecord, source_cfg: Mapping, *, env: Mapping[str, str], now: str,
    http_send: HttpSend | None,
) -> tuple[str, str]:
    """Deliver; returns (final status, audit result)."""
    if draft.kind == KIND_SLACK:
        channel = draft.target.get("channel")
        if not channel:
            raise SourceError("draft has no target channel")
        slack.post_message(
            source_cfg, env=env, channel=channel, thread_ts=draft.target.get("thread_ts"),
            text=slack.escape_text(draft.text), http_send=http_send,
        )
        return DRAFT_SENT, "sent"
    if draft.kind == KIND_EMAIL:
        maildir.place_draft(source_cfg, target=draft.target, text=draft.text, now=_parse_stamp(now))
        return DRAFT_PLACED, "placed"
    raise SourceError(f"unknown draft kind {draft.kind}")


def deliver(
    store: DispatchStore,
    cfg: Mapping[str, Any],
    draft: DraftRecord,
    *,
    actor: str,
    now: str,
    env: Mapping[str, str],
    http_send: HttpSend | None = None,
) -> DraftRecord:
    claimed = store.claim_draft(
        draft.id, from_status=DRAFT_PENDING, to_status=DRAFT_SENDING, now=now
    )
    if not claimed:
        raise DraftError(f"draft {draft.id} is not pending")
    final, result, error = DRAFT_PENDING, "failed", None
    try:
        try:
            source_cfg = settings.source_settings(cfg["dispatch"], draft.source)
        except KeyError:
            raise SourceError(f"source {draft.source} is no longer configured") from None
        final, result = _send(draft, source_cfg, env=env, now=now, http_send=http_send)
    except SourceError as exc:
        error = str(exc)
    except Exception as exc:  # leave the draft retryable and never leak details
        error = f"delivery failed: {type(exc).__name__}"
    fields: dict[str, Any] = {"status": final, "error": error, "updated_at": now}
    if error is None:
        fields["delivered_at"] = now
    updated = store.update_draft(draft.id, **fields)
    target = draft.target
    store.log_event(
        at=now, event=EVENT_SEND, actor=actor, source=draft.source, task_id=draft.item_id,
        detail={
            "draft_id": draft.id, "kind": draft.kind, "origin": draft.origin,
            "channel": target.get("channel"), "folder": source_folder(draft, cfg),
            "thread_ts": target.get("thread_ts"), "chars": len(draft.text),
            "result": result, "error": error,
        },
    )
    if error is not None:
        raise DraftError(error)
    return updated


def source_folder(draft: DraftRecord, cfg: Mapping[str, Any]) -> str | None:
    """The Drafts folder an email draft goes to; None for other kinds."""
    if draft.kind != KIND_EMAIL:
        return None
    try:
        return settings.source_settings(cfg["dispatch"], draft.source).get("drafts_folder")
    except KeyError:
        return None

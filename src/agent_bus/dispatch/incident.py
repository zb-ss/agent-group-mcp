"""Incidents: one open incident per alert key, an optional incident session.

The alert text is untrusted. It reaches only the prompt file's untrusted block
(cleaned and capped); alerts, the bus message that closes an incident, log rows
and decisions carry ids and rule names only. The session's bounds are the
packaged playbook plus the owner's server-tool policy; this module adds the
limits on how many sessions start. One incident is open per key, so a repeat of
an alert starts nothing, and spawns are rate-limited.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from datetime import timedelta
from pathlib import Path
from typing import Any

from .. import resolution, status
from ..config import ConfigError
from ..spawn import SpawnError, SpawnRequest
from ..storage import UnknownRecipientError
from . import dispatcher, drafts, messages, playbook, routing, settings, slack
from .dispatcher import DispatcherError
from .drafts import DraftError
from .messages import Message, PassCtx
from .model import clean_multiline, clean_untrusted, format_now
from .playbook import PlaybookError
from .rules import Hit
from .store import EVENT_INCIDENT, INCIDENT_CLOSED, IncidentRecord

ROLE = "incident"
ACTOR = "runner"
PLACEHOLDERS = (
    "label", "incident_id", "rule", "source", "alert_at", "alert_url", "alert_text",
    "tools", "dir", "dispatcher", "report_file",
)
_FAILURES = (SpawnError, ConfigError, PlaybookError, DispatcherError, OSError, subprocess.SubprocessError)
_NO_DISPATCHER = "none is running"
# Results of an incident whose session never started; the next open or repeat
# for the key tries again (still under the spawn limits).
RETRY_RESULTS = frozenset({"rate_limited", "spawn_failed"})
_NOT_STARTED = "session could not start"
_UTF8_MAX_BYTES = 4  # the widest UTF-8 character: bounds the bytes of a char cap


def close_body(incident: IncidentRecord, stamp: str) -> str:
    """The bus message that tells a session its alert resolved. No alert text."""
    return (
        "agent-bus dispatch incident update. The alert source reported this incident as "
        "resolved. Stop applying fixes, finish your report, then report done.\n"
        + json.dumps(
            {
                "kind": "incident_closed", "incident_id": incident.id,
                "rule": incident.rule, "closed_at": stamp,
            }
        )
    )


def _log(ctx: PassCtx, incident: IncidentRecord, **detail: Any) -> None:
    ctx.store.log_event(
        at=ctx.stamp, event=EVENT_INCIDENT, actor=ACTOR, source=incident.source,
        task_id=incident.item_id, detail={"incident_id": incident.id, **detail},
    )


def _session_row(rows, incident: IncidentRecord):
    for row in rows or ():
        if getattr(row, "role", None) != ROLE:
            continue
        if incident.session_name and row.name == incident.session_name:
            return row
        if incident.label and row.tmux_window == incident.label:
            return row
    return None


def handle(ctx: PassCtx, message: Message, hit: Hit) -> dict:
    existing = ctx.store.open_incident(hit.key)
    if hit.phase == "close":
        return _close(ctx, message, hit, existing)
    if existing is not None:
        if _should_retry(hit, existing):
            return _retry(ctx, message, hit, existing)
        return _repeat(ctx, message, hit, existing)
    if hit.phase == "repeat":
        return messages.decision(
            ctx, message, rule=hit.rule, action=hit.action, result="ignored",
            reason="repeat without an open incident",
        )
    return _open(ctx, message, hit)


def _close(ctx: PassCtx, message: Message, hit: Hit, incident: IncidentRecord | None) -> dict:
    if incident is None:
        return messages.decision(
            ctx, message, rule=hit.rule, action=hit.action, result="ignored",
            reason="no open incident",
        )
    if ctx.dry_run:
        return messages.decision(
            ctx, message, rule=hit.rule, action=hit.action, result="would_close"
        )
    ctx.store.update_incident(
        incident.id, state=INCIDENT_CLOSED, closed_at=ctx.stamp, close_reason="resolved"
    )
    told = _tell_session(ctx, incident)
    _log(ctx, incident, op="closed", told_session=told)
    messages.alert(
        ctx, "incident_closed", source=message.source, title=hit.rule or "",
        url=incident.url or "", session=incident.session_name,
    )
    return messages.decision(
        ctx, message, rule=hit.rule, action=hit.action, result="incident_closed"
    )


def _tell_session(ctx: PassCtx, incident: IncidentRecord) -> bool:
    return tell_session(
        ctx.rows, incident, send=ctx.send, sender=ctx.dispatch["bus_sender"], stamp=ctx.stamp
    )


def tell_session(rows, incident: IncidentRecord, *, send, sender: str, stamp: str) -> bool:
    """Send the close message to the incident's live session; False when none."""
    row = _session_row(rows, incident)
    if row is None or not row.live:
        return False
    try:
        send(from_agent=sender, to=row.name, body=close_body(incident, stamp))
    except (UnknownRecipientError, ValueError):
        return False
    return True


class IncidentError(Exception):
    """A manual incident command could not run; the message names no alert text."""


def listing(store, *, include_closed: bool = False) -> list[dict]:
    records = store.list_incidents() if include_closed else store.list_incidents("open")
    return [
        {
            "id": r.id, "state": r.state, "source": r.source, "rule": r.rule,
            "opened_at": r.opened_at, "repeats": r.repeats, "session": r.session_name,
            "url": r.url, "closed_at": r.closed_at, "close_reason": r.close_reason,
        }
        for r in records
    ]


def manual_actor(storage, env) -> str:
    """Same actors as draft approval: the verified dispatcher or the owner's shell."""
    try:
        return drafts.owner_actor(storage, env)
    except DraftError:
        raise IncidentError(
            "only the dispatcher or the owner's own shell may close an incident "
            "(this shell belongs to an agent session)"
        ) from None


def close_manual(
    store, incident_id: int, *, actor: str, stamp: str, rows, send, sender: str
) -> bool:
    """Close an open incident by hand and tell a live session. Returns whether a
    session was told."""
    incident = store.get_incident(incident_id)
    if incident is None:
        raise IncidentError(f"no incident {incident_id}")
    if incident.state != "open":
        raise IncidentError(f"incident {incident_id} is already {incident.state}")
    store.update_incident(
        incident_id, state=INCIDENT_CLOSED, closed_at=stamp, close_reason="manual"
    )
    told = tell_session(rows, incident, send=send, sender=sender, stamp=stamp)
    store.log_event(
        at=stamp, event=EVENT_INCIDENT, actor=actor, source=incident.source,
        task_id=incident.item_id,
        detail={"incident_id": incident_id, "op": "closed", "reason": "manual", "told_session": told},
    )
    return told


def _repeat(ctx: PassCtx, message: Message, hit: Hit, incident: IncidentRecord) -> dict:
    if not ctx.dry_run:
        ctx.store.update_incident(
            incident.id, repeats=incident.repeats + 1, last_seen_at=ctx.stamp
        )
    return messages.decision(
        ctx, message, rule=hit.rule, action=hit.action, result="incident_repeat"
    )


def _should_retry(hit: Hit, incident: IncidentRecord) -> bool:
    return (
        hit.action == "respond" and incident.action == "respond"
        and incident.result in RETRY_RESULTS and not incident.session_name
    )


def _hour_ago(ctx: PassCtx) -> str:
    return format_now(ctx.now - timedelta(hours=1))


def _retry(ctx: PassCtx, message: Message, hit: Hit, incident: IncidentRecord) -> dict:
    """An open incident whose session never started: count the repeat, then
    try the spawn again. The owner was already told it could not start."""
    if ctx.dry_run:
        return messages.decision(
            ctx, message, rule=hit.rule, action=hit.action, result="would_respond"
        )
    incident = ctx.store.update_incident(
        incident.id, repeats=incident.repeats + 1, last_seen_at=ctx.stamp
    )
    return _respond(ctx, message, hit, incident)


def _open(ctx: PassCtx, message: Message, hit: Hit) -> dict:
    if ctx.dry_run:
        result = "would_respond" if hit.action == "respond" else "would_notify"
        return messages.decision(ctx, message, rule=hit.rule, action=hit.action, result=result)
    incident = ctx.store.create_incident(
        key=hit.key, source=message.source, container=message.container, rule=hit.rule,
        action=hit.action, item_id=message.item_id,
        thread_ref=message.reply.get("thread_ts"), url=message.url, now=ctx.stamp,
    )
    if incident is None:  # lost a race: another pass opened it first
        return messages.decision(
            ctx, message, rule=hit.rule, action=hit.action, result="incident_repeat"
        )
    if hit.action != "respond":
        messages.alert(
            ctx, "incident_opened", source=message.source, title=hit.rule or "",
            url=message.url,
        )
        return messages.decision(
            ctx, message, rule=hit.rule, action=hit.action, result="incident_opened"
        )
    return _respond(ctx, message, hit, incident)


def _limited(ctx: PassCtx, cfg: dict) -> bool:
    if ctx.store.incident_spawns_since(_hour_ago(ctx)) >= cfg["max_spawns_per_hour"]:
        return True
    running = sum(
        1 for row in ctx.rows or ()
        if getattr(row, "role", None) == ROLE and row.state in status.ACTIVE_STATES
    )
    return running + ctx.spawns >= cfg["max_running"]


def _not_started(
    ctx: PassCtx, message: Message, hit: Hit, incident: IncidentRecord, result: str, **detail: Any
) -> None:
    """Record a session that did not start. Only the first failure for an
    incident alerts the owner; retries that fail again only log."""
    ctx.store.update_incident(incident.id, result=result)
    _log(ctx, incident, op=result, **detail)
    if incident.result in RETRY_RESULTS:
        return
    messages.alert(
        ctx, "incident_opened", source=message.source,
        title=f"{hit.rule or ''}: {_NOT_STARTED}", url=message.url,
    )


def _respond(ctx: PassCtx, message: Message, hit: Hit, incident: IncidentRecord) -> dict:
    cfg = ctx.dispatch["incident"]
    if _limited(ctx, cfg):
        _not_started(ctx, message, hit, incident, "rate_limited")
        return messages.decision(
            ctx, message, rule=hit.rule, action=hit.action, result="rate_limited"
        )
    # Recorded before the attempt, so a spawn that fails (or crashes the
    # pass) still counts toward the hourly limit and a spawn that keeps
    # failing is not retried on every repeat once the limit is reached.
    ctx.store.record_spawn_attempt(incident.id, now=ctx.stamp)
    try:
        spawned, route = _spawn(ctx, message, hit, incident, cfg)
    except _FAILURES as exc:
        _not_started(ctx, message, hit, incident, "spawn_failed", error=type(exc).__name__)
        return messages.decision(
            ctx, message, rule=hit.rule, action=hit.action, result="failed",
            reason=type(exc).__name__,
        )
    ctx.store.update_incident(
        incident.id, label=f"{cfg['label_prefix']}-{incident.id}", session_name=spawned.name,
        client=route.client, model=route.model, result="spawned",
    )
    ctx.spawns += 1
    _log(
        ctx, incident, op="opened", session=spawned.name, client=route.client,
        model=route.model,
    )
    messages.alert(
        ctx, "incident_opened", source=message.source, title=hit.rule or "",
        url=message.url, session=spawned.name,
    )
    return messages.decision(
        ctx, message, rule=hit.rule, action=hit.action, result="incident_opened"
    )


def _spawn(ctx: PassCtx, message: Message, hit: Hit, incident: IncidentRecord, cfg: dict):
    label = f"{cfg['label_prefix']}-{incident.id}"
    group = cfg["group"]
    work_dir = settings.incident_dir(ctx.dispatch)
    if (resolution.find_repo_root(work_dir.parent) / ".git").exists():
        raise DispatcherError(
            f"{work_dir} lies inside another repository; the incident folder must be "
            f"outside every repo (set dispatch.incident.dir in config.json)"
        )
    route = routing.choose(
        ctx.dispatch["routing"],
        routing.Signals(
            type="incident", repo=group, repo_id=None, title=hit.rule or "", sections=()
        ),
        static_client=cfg["client"], static_model=cfg["model"],
        available=ctx.available, quota=ctx.quota,
    )
    dispatcher.prepare_folder(
        ctx.cfg, work_dir, group=group, client=route.client, storage=ctx.storage,
        run=ctx.run, notes=[],
    )
    (work_dir / "reports").mkdir(mode=0o700, exist_ok=True)
    live = dispatcher_name(ctx)
    values = {
        "label": label,
        "dir": str(work_dir),
        "incident_id": str(incident.id),
        "rule": clean_untrusted(hit.rule or "", cfg["max_name_chars"]),
        "source": clean_untrusted(message.source, cfg["max_name_chars"]),
        "alert_at": clean_untrusted(message.at, cfg["max_time_chars"]),
        "alert_url": clean_untrusted(message.url, cfg["max_url_chars"]),
        "alert_text": clean_untrusted(message.text, cfg["max_alert_chars"]),
        "tools": cfg["tools_hint"],
        "dispatcher": live,
        "report_file": str(work_dir / "reports" / f"{label}.md"),
    }
    text = playbook.render(
        hit.playbook or "incident", values,
        templates_dir=ctx.dispatch["templates_dir"], placeholders=PLACEHOLDERS,
    )
    prompt = playbook.write_prompt(
        text, label=label, prompts_dir=settings.prompts_dir(ctx.dispatch)
    )
    request = SpawnRequest(
        repo=group, client=route.client, name=label, prompt_file=prompt, model=route.model,
        remote=cfg["remote"], role=ROLE,
    )
    result = ctx.spawn_fn(request, storage=ctx.storage, cfg=ctx.cfg, run=ctx.run, now=ctx.now)
    return result, route


def dispatcher_name(ctx: PassCtx) -> str:
    return ctx.dispatcher.name if ctx.dispatcher is not None else _NO_DISPATCHER


# --- sweep: expiry and reports -------------------------------------------------


def sweep(ctx: PassCtx) -> None:
    _expire(ctx)
    _reports(ctx)


def _expire(ctx: PassCtx) -> None:
    if ctx.dry_run:
        return
    cfg = ctx.dispatch["incident"]
    cutoff = format_now(ctx.now - timedelta(hours=cfg["max_open_hours"]))
    for incident in ctx.store.list_incidents("open"):
        if incident.opened_at >= cutoff:
            continue
        ctx.store.update_incident(
            incident.id, state=INCIDENT_CLOSED, closed_at=ctx.stamp, close_reason="expired"
        )
        _log(ctx, incident, op="expired")


class ReportRefused(Exception):
    """A report file that is not a plain file directly in the reports folder."""


def _reports_folder(dispatch_cfg) -> Path:
    return settings.incident_dir(dispatch_cfg).resolve() / "reports"


def _read_report(folder: Path, name: str, cfg: dict) -> str:
    """Read a report the incident session wrote. The session is untrusted:
    the file must be a regular file directly in the resolved reports folder,
    never a symlink or a hard link (either could reach a secrets file that
    would then be posted), a device, a FIFO or a directory, owned by this
    user, and no larger than the cap. The folder is opened once without
    following a symlink and the report is opened relative to that handle, so
    the folder cannot be swapped between the check and the read.
    Raises FileNotFoundError while there is no report yet."""
    if Path(name).name != name or name in (".", ".."):
        raise ReportRefused("report is not directly in the reports folder")
    folder_fd = _open_reports_folder(folder)
    try:
        raw = _read_capped(folder_fd, name, cfg["max_report_chars"] * _UTF8_MAX_BYTES)
    finally:
        os.close(folder_fd)
    text = raw.decode("utf-8", errors="replace")
    return slack.neutralise(clean_multiline(text, cfg["max_report_chars"]))


def _open_reports_folder(folder: Path) -> int:
    try:
        return os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise
    except OSError:
        # ELOOP for a symlink, ENOTDIR for anything else that is no directory
        raise ReportRefused("reports folder is not a plain directory") from None


def _read_capped(folder_fd: int, name: str, max_bytes: int) -> bytes:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder_fd)
    except OSError as exc:
        if isinstance(exc, FileNotFoundError):
            raise
        if stat.S_ISLNK(os.lstat(name, dir_fd=folder_fd).st_mode):
            raise ReportRefused("report is a symlink") from None
        raise
    try:
        _check_report(os.fstat(fd), max_bytes)
        with os.fdopen(fd, "rb") as handle:
            fd = -1  # the file object owns it now
            raw = handle.read(max_bytes + 1)
    finally:
        if fd >= 0:
            os.close(fd)
    if len(raw) > max_bytes:
        raise ReportRefused("report is larger than the cap")
    return raw


def _check_report(info: os.stat_result, max_bytes: int) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise ReportRefused("report is not a regular file")
    if info.st_nlink != 1:
        # A hard link to a secrets file passes O_NOFOLLOW and S_ISREG.
        raise ReportRefused("report is a hard link")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise ReportRefused("report is not owned by this user")
    if info.st_size > max_bytes:
        raise ReportRefused("report is larger than the cap")


def _ready(ctx: PassCtx, incident: IncidentRecord) -> bool:
    row = _session_row(ctx.rows, incident)
    return row is None or not row.live or row.state in ("waiting", "done")


def _reports(ctx: PassCtx) -> None:
    cfg = ctx.dispatch["incident"]
    folder = _reports_folder(ctx.dispatch)
    for incident in ctx.store.unreported_incidents():
        name = f"{incident.label}.md"
        if not os.path.lexists(folder / name) or not _ready(ctx, incident):
            continue
        try:
            text = _read_report(folder, name, cfg)
        except FileNotFoundError:
            continue
        except ReportRefused as exc:
            _refuse_report(ctx, incident, str(exc))
            continue
        except OSError as exc:
            ctx.errors.append(f"incident {incident.id} report unreadable: {type(exc).__name__}")
            continue
        if text:
            _report(ctx, incident, text, cfg)


def _refuse_report(ctx: PassCtx, incident: IncidentRecord, reason: str) -> None:
    """Nothing is drafted or posted. The incident counts as reported so the
    refusal is logged once, not on every pass."""
    ctx.errors.append(f"incident {incident.id} report refused: {reason}")
    if ctx.dry_run:
        return
    ctx.store.update_incident(incident.id, report_at=ctx.stamp)
    _log(ctx, incident, op="report_refused", reason=reason)


def _source_type(ctx: PassCtx, incident: IncidentRecord) -> str:
    entry = ctx.dispatch["sources"].get(incident.source, {})
    return settings.source_type(incident.source, entry)


def _rule(ctx: PassCtx, incident: IncidentRecord) -> dict | None:
    try:
        source_cfg = settings.source_settings(ctx.dispatch, incident.source)
        rules = source_cfg["channels"][incident.container]["rules"]
    except (KeyError, TypeError):
        return None
    for rule in rules:
        if rule.get("name") == incident.rule:
            return rule
    return None


def _report(ctx: PassCtx, incident: IncidentRecord, text: str, cfg: dict) -> None:
    source_type = _source_type(ctx, incident)
    if ctx.dry_run:
        messages.decision(
            ctx,
            Message(
                source=incident.source, item_id=incident.item_id or "",
                container=incident.container or "", author="", author_kind="bot",
                text="", at=ctx.stamp, url=incident.url or "",
            ),
            rule=incident.rule, action="report", result="would_report",
        )
        return
    try:
        draft = drafts.create(
            ctx.store, source=incident.source, item_id=incident.item_id,
            kind=drafts.kind_for(source_type), origin=drafts.ORIGIN_INCIDENT,
            rule=incident.rule, url=incident.url,
            target={"channel": incident.container, "thread_ts": incident.thread_ref},
            text=text, now=ctx.stamp, max_chars=cfg["max_report_chars"],
            incident_id=incident.id,
        )
    except DraftError as exc:
        ctx.errors.append(f"incident report not stored: {exc}")
        return
    ctx.store.update_incident(incident.id, report_at=ctx.stamp)
    rule = _rule(ctx, incident)
    if rule is not None and rule.get("auto_post_report") and source_type == "slack":
        _auto_post(ctx, incident, draft)
        return
    messages.alert(
        ctx, "draft_ready", source=incident.source, title=f"{incident.rule} #{draft.id}",
        url=incident.url or "",
    )


def _auto_post(ctx: PassCtx, incident: IncidentRecord, draft) -> None:
    try:
        drafts.deliver(
            ctx.store, ctx.cfg, draft, actor=drafts.ACTOR_RUNNER, now=ctx.stamp,
            env=ctx.env, http_send=ctx.http_send,
        )
    except DraftError as exc:
        ctx.errors.append(f"incident report not posted: {exc}")
        messages.alert(  # the draft stays pending: tell the owner it needs them
            ctx, "draft_ready", source=incident.source,
            title=f"{incident.rule} #{draft.id}: auto-post failed", url=incident.url or "",
        )
        return
    messages.alert(
        ctx, "incident_report", source=incident.source, title=incident.rule,
        url=incident.url or "", session=incident.session_name,
    )

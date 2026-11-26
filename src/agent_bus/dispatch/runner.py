"""The dispatch run: lock, working hours, session transitions, then for every
task classify and either spawn a session, notify, or skip.

Shaped like `spawn.py`: frozen result dataclasses, keyword-only seams for
everything that touches the outside (sources, notifiers, git, spawn, status).
Task text is untrusted: it reaches the rendered prompt file and the store,
never an argv.
"""

from __future__ import annotations

import fcntl
import shutil
import subprocess
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from typing import Any

from .. import authority, commands, identity, repo_identity, spawn, status
from ..config import ConfigError
from ..spawn import SpawnError, SpawnRequest
from ..clients import agy as agy_client
from ..clients.base import ResumableSession
from ..storage import TIMESTAMP_FORMAT, Storage, UnknownRecipientError
from . import classify, cleanup, decision_model, playbook, routing, settings, triage
from .asana import AsanaSource
from .model import (
    OUTCOME_DONE,
    OUTCOME_ENDED,
    OUTCOME_NOTIFIED,
    OUTCOME_SPAWN_FAILED,
    OUTCOME_SPAWNED,
    Task,
    clean_text,
    clean_untrusted,
    format_now,
)
from .notify import Notification, Notifier, deliver
from .playbook import PlaybookError
from .source import HttpGet, SourceError, TaskSource
from .store import (
    EVENT_APPROVAL_REQUEST,
    EVENT_CANDIDATE,
    EVENT_EXECUTED,
    EVENT_ROUTING,
    TRIAGE_FIELDS,
    DispatchStore,
    TaskRecord,
)

SOURCES: dict[str, Callable[..., TaskSource]] = {"asana": AsanaSource}

RESULTS = (
    "spawned", "would_spawn", "held", "would_hold", "notified", "would_notify",
    "messaged", "would_message", "resumed", "would_resume", "delegated", "would_delegate",
    "skipped", "failed", "ignored",
)
UNKNOWN = "unknown"
HEAD = "HEAD"
STATE_LABELS = {"waiting": "needs review", "done": "done", "ended": "ended without done"}
_OUTCOME_FOR_STATE = {"done": OUTCOME_DONE, "ended": OUTCOME_ENDED}
_WATCHED_OUTCOMES = (OUTCOME_SPAWNED, OUTCOME_DONE, OUTCOME_ENDED)
_SPAWN_FAILURES = (SpawnError, ConfigError, PlaybookError, OSError)
_CLEAR_TRIAGE: dict[str, None] = dict.fromkeys(TRIAGE_FIELDS)
_REOPEN: dict[str, None] = dict.fromkeys(
    ("decision", "decision_at", "decision_reason", "decision_repo", "decision_client", "decision_model")
)
_DECISIONS_TO_EXECUTE = ("start", "resume")


@dataclass(frozen=True)
class TaskDecision:
    source: str
    task_id: str
    title: str
    url: str
    type: str
    action: str
    result: str
    reason: str | None = None
    label: str | None = None
    session: str | None = None


@dataclass(frozen=True)
class RunReport:
    dry_run: bool = False
    skipped: str | None = None
    decisions: list[TaskDecision] = field(default_factory=list)
    transitions: list[dict] = field(default_factory=list)
    cleanups: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def exit_code(self) -> int:
        failed = any(d.result == "failed" for d in self.decisions)
        return 1 if self.errors or failed else 0


@dataclass
class _Ctx:
    cfg: Mapping
    dispatch: Mapping
    storage: Storage
    store: DispatchStore
    notifiers: Sequence[Notifier]
    now: datetime
    dry_run: bool
    run: commands.Runner
    spawn_fn: Callable[..., Any]
    resume_fn: Callable[..., Any]
    find_session: Callable[..., Any]
    send: Callable[..., dict]
    rows: list
    report: RunReport
    available: Callable[[str], bool]
    http_post: Callable[..., bytes]
    quota: Callable[[str], float]
    dispatcher: Any | None = None
    repo_ids: dict[str, str | None] = field(default_factory=dict)
    spawns: int = 0
    fetched: set[str] = field(default_factory=set)

    @property
    def stamp(self) -> str:
        return format_now(self.now)


# ------------------------------ sources / lock ------------------------------


def build_sources(
    dispatch_cfg: Mapping, *, env: Mapping[str, str], http_get: HttpGet | None = None
) -> list[TaskSource]:
    built: list[TaskSource] = []
    for name, source_cfg in dispatch_cfg["sources"].items():
        if not source_cfg.get("enabled"):
            continue
        factory = SOURCES.get(name)
        if factory is None:
            raise ConfigError(f"dispatch.sources.{name} is enabled but there is no such source")
        built.append(factory(source_cfg, env=env, http_get=http_get))
    return built


@contextmanager
def run_lock(path: Path) -> Iterator[bool]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


# ------------------------------- transitions --------------------------------


def _notify(ctx: _Ctx, notification: Notification) -> bool:
    """Send a notification; True when an enabled notifier took it. When none
    did, the caller leaves the state unrecorded so the next run retries."""
    delivered, errors = deliver(
        ctx.notifiers, notification, max_chars=ctx.dispatch["title_max_chars"]
    )
    ctx.report.errors.extend(errors)
    return delivered


def find_row(rows: Sequence[Any], record: TaskRecord) -> Any | None:
    """The status row of a record's session. A session may rename itself, which
    moves its row to a new name; the spawn's tmux window keeps the dispatch label."""
    for row in rows:
        if row.name == record.session_name:
            return row
    if not record.label:
        return None
    # Follow-ups reuse the label, so several renamed rows can share the window:
    # prefer a live or active one, then the most recently seen.
    shared = [r for r in rows if r.spawned and r.tmux_window == record.label]
    if not shared:
        return None
    return max(
        shared,
        key=lambda r: (
            bool(r.live or r.state in status.ACTIVE_STATES),
            r.state_at or "",
            r.last_seen or "",
        ),
    )


def _answered(record: TaskRecord, row: Any | None) -> bool:
    """True when the session reported a state after the follow-up was messaged."""
    return bool(row is not None and row.state_at and row.state_at > (record.handled_at or ""))


def _not_yet_answered(record: TaskRecord, row: Any | None) -> bool:
    """After a follow-up was messaged, the session's state is only news once it
    was reported after that message; an unchanged row is the state notified
    before the follow-up."""
    if row is None or row.state == "ended":
        # A dead session can no longer answer; its end is news.
        return False
    return not _answered(record, row)


def _transition(ctx: _Ctx, record: TaskRecord, rows: Sequence[Any]) -> None:
    row = find_row(rows, record)
    state = row.state if row is not None else "ended"
    if row is not None and row.name != record.session_name and not ctx.dry_run:
        record = ctx.store.update(record.source, record.task_id, session_name=row.name)
    if state == record.last_notified_state:
        return
    if record.last_notified_state == "messaged" and _not_yet_answered(record, row):
        return
    notified = state in STATE_LABELS and state in ctx.dispatch["notify_on"]
    delivered = False
    if not ctx.dry_run:
        links = tuple(
            (entry.get("label") or "", entry["url"])
            for entry in (row.links if row is not None else ())
        )
        delivered = notified and _notify(
            ctx,
            Notification(record.session_name, STATE_LABELS[state], record.title, record.url,
                         links=links),
        )
        fields: dict[str, Any] = {"outcome": _OUTCOME_FOR_STATE.get(state, OUTCOME_SPAWNED)}
        if delivered or not notified:
            fields["last_notified_state"] = state
        if (record.last_notified_state == "messaged" and state == "ended"
                and not _answered(record, row)):
            # The messaged session died unanswered: reopen the follow-up so
            # the next run resumes or re-spawns for that comment. A session
            # that answered and then ended keeps the comment handled.
            fields["last_comment_handled_at"] = None
        ctx.store.update(record.source, record.task_id, **fields)
    ctx.report.transitions.append(
        {"task_id": record.task_id, "session": record.session_name, "state": state,
         "notified": delivered}
    )


def _transitions(ctx: _Ctx) -> None:
    for record in ctx.store.list_tasks():
        if record.session_name and record.outcome in _WATCHED_OUTCOMES:
            _transition(ctx, record, ctx.rows)


def _cleanup_paths(record: TaskRecord, row: Any | None) -> tuple[str, str] | None:
    spawned = row is not None and row.spawned
    work_dir = record.work_dir or (row.work_dir if spawned else None)
    clone = record.clone_path or (row.repo_path if spawned else None)
    if not work_dir or not clone or work_dir == clone or not Path(work_dir).is_dir():
        return None
    return work_dir, clone


def _cleanup_project(ctx: _Ctx, record: TaskRecord) -> dict | None:
    """The project settings of a record; the defaults for a repo that was
    started for an unmapped task."""
    project = settings.project_settings(ctx.dispatch, record.source, record.project_id)
    if project is None and record.repo:
        return settings.default_project(ctx.dispatch, record.repo)
    return project


def _cleanup(ctx: _Ctx) -> None:
    if not ctx.dispatch["worktree_cleanup"]["enabled"]:
        return
    timeout = ctx.cfg["repos"]["git_timeout_seconds"]
    remote = ctx.cfg["repos"]["remote"]
    for record in ctx.store.list_tasks():
        if not record.label or record.worktree_removed_at:
            continue
        row = find_row(ctx.rows, record) if record.session_name else None
        paths = _cleanup_paths(record, row)
        if paths is None:
            continue
        if row is not None and (row.live or row.state in status.ACTIVE_STATES):
            continue
        project = _cleanup_project(ctx, record)
        if project is None or not project.get("worktree"):
            continue
        work_dir, clone = paths
        if project.get("fetch") and not ctx.dry_run and clone not in ctx.fetched:
            _git_out(ctx, ["git", "-C", clone, "fetch", "--quiet", remote],
                     ctx.dispatch["fetch_timeout_seconds"])
            ctx.fetched.add(clone)
        entry = {"task_id": record.task_id, "work_dir": work_dir}
        reason = cleanup.keep_reason(
            Path(work_dir), Path(clone), base=project["worktree"], remote=remote,
            timeout=timeout, run=ctx.run, start_commit=record.start_commit,
        )
        if reason:
            ctx.report.cleanups.append({**entry, "result": "kept", "reason": reason})
        elif ctx.dry_run:
            ctx.report.cleanups.append({**entry, "result": "would_remove"})
        else:
            _remove_worktree(ctx, record, entry, Path(work_dir), Path(clone), timeout)


def _drop_agy_trust(ctx: _Ctx, record: TaskRecord, work_dir: Path) -> dict:
    project = _cleanup_project(ctx, record) or {}
    if (record.client or project.get("client")) != agy_client.CLIENT_ID:
        return {}
    options = ctx.cfg["spawn"]["clients"].get(agy_client.CLIENT_ID)
    notes = agy_client.AgyWiring().release_worktree(work_dir, options=options)
    return {"notes": notes} if notes else {}


def _remove_worktree(
    ctx: _Ctx, record: TaskRecord, entry: dict, work_dir: Path, clone: Path, timeout: float
) -> None:
    error = cleanup.remove(work_dir, clone, timeout=timeout, run=ctx.run)
    if error:
        ctx.report.cleanups.append({**entry, "result": "failed", "reason": error})
        return
    ctx.store.update(record.source, record.task_id, worktree_removed_at=ctx.stamp)
    entry = {**entry, **_drop_agy_trust(ctx, record, work_dir)}
    if "worktree_removed" in ctx.dispatch["notify_on"]:
        _notify(ctx, Notification(record.session_name, "worktree removed",
                                  record.title, record.url))
    ctx.report.cleanups.append({**entry, "result": "removed"})


# --------------------------------- tasks ------------------------------------


def _decision(
    ctx: _Ctx, task: Task, type_: str, action: str, result: str,
    *, reason: str | None = None, label: str | None = None, session: str | None = None,
) -> TaskDecision:
    title = clean_text(task.title, ctx.dispatch["title_max_chars"])
    return TaskDecision(task.source, task.task_id, title, task.url, type_, action,
                        result, reason, label, session)


def _mapped_project(ctx: _Ctx, task: Task) -> tuple[str, dict] | None:
    for membership in task.memberships:
        project = settings.project_settings(ctx.dispatch, task.source, membership.project_id)
        if project is not None:
            return membership.project_id, project
    return None


def _with_type(record: TaskRecord, type_: str) -> tuple[str, ...]:
    return record.handled_types + ((type_,) if type_ not in record.handled_types else ())


def _handle_task(ctx: _Ctx, task: Task) -> None:
    mapped = _mapped_project(ctx, task)
    if mapped is None:
        _handle_unmapped(ctx, task)
        return
    project_id, project = mapped
    record = ctx.store.get(task.source, task.task_id)
    if not ctx.dry_run:
        record = ctx.store.observe(task, project_id=project_id, now=ctx.stamp)
    type_ = classify.classify(task, record, rules=ctx.dispatch["rules"])
    action = settings.type_action(ctx.dispatch, type_)
    if action == "spawn":
        patterns = settings.type_repos(ctx.dispatch, type_)
        if patterns is not None and not settings.repo_allowed(
            patterns, _repo_id(ctx, project["repo"])
        ):
            action = "notify"
    if type_ == classify.TYPE_IGNORE or action == settings.TYPE_IGNORE:
        ctx.report.decisions.append(_decision(ctx, task, type_, "ignore", "ignored"))
    elif action == "notify":
        ctx.report.decisions.append(_notify_only(ctx, task, record, type_, project))
    else:
        delegated = _triage(ctx, task, record, type_, project_id, project)
        ctx.report.decisions.append(
            delegated
            or _spawn_task(ctx, task, record, type_, project_id, project)
        )


def _triage_active(ctx: _Ctx) -> bool:
    return bool(ctx.dispatch["triage"]["enabled"] and ctx.dispatcher is not None)


def _minutes_since(ctx: _Ctx, stamp: str) -> float | None:
    try:
        then = datetime.strptime(stamp, TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (ctx.now - then).total_seconds() / 60


def _fallback_due(ctx: _Ctx, candidate_at: str) -> bool:
    minutes = ctx.dispatch["triage"]["fallback_after_minutes"]
    if minutes is None:
        return False
    elapsed = _minutes_since(ctx, candidate_at)
    return elapsed is not None and elapsed >= minutes


def _skip_by_decision(
    ctx: _Ctx, task: Task, record: TaskRecord | None, type_: str,
) -> TaskDecision:
    reason = f"dispatcher: {(record.decision_reason if record else None) or 'no reason given'}"
    if not ctx.dry_run and record is not None:
        ctx.store.update(
            task.source, task.task_id, type=type_, handled_types=_with_type(record, type_),
            handled_at=ctx.stamp, last_comment_handled_at=task.newest_comment_at,
            **_CLEAR_TRIAGE,
        )
        ctx.store.log_event(
            at=ctx.stamp, event=EVENT_EXECUTED, actor="runner", source=task.source,
            task_id=task.task_id, detail={"result": "skipped", "by": "dispatcher"},
        )
    return _decision(ctx, task, type_, "skip", "skipped", reason=reason)


def _existing_session(ctx: _Ctx, label: str, record: TaskRecord | None) -> dict | None:
    rows = _session_rows(ctx, label, record)
    if not rows:
        return None
    row = _latest(rows)
    return {"name": row.name, "state": row.state, "live": bool(row.live)}


def _triage(
    ctx: _Ctx, task: Task, record: TaskRecord | None, type_: str, project_id: str | None,
    project: Mapping, *, unmapped: bool = False,
) -> TaskDecision | None:
    """Hand a spawn-action task to the dispatcher. None means the deterministic
    path goes on (spawn, resume or message as before)."""
    chosen = record.decision if record else None
    if chosen in _DECISIONS_TO_EXECUTE:
        return None
    if chosen == "skip":
        return _skip_by_decision(ctx, task, record, type_)
    if not _triage_active(ctx):
        return None
    if chosen == "ask":
        return _decision(ctx, task, type_, "triage", "delegated",
                         reason="the dispatcher is asking the owner")
    if record is not None and record.candidate_at:
        if _fallback_due(ctx, record.candidate_at):
            return None
        return _decision(ctx, task, type_, "triage", "delegated",
                         reason="waiting for the dispatcher")
    label, failure = _label_for(ctx, task, type_)
    if failure is not None:
        return _decision(ctx, task, type_, "spawn", "failed", reason=failure, label=label)
    if ctx.dry_run:
        return _decision(ctx, task, type_, "triage", "would_delegate", label=label)
    return _send_candidate(ctx, task, record, type_, project, label, unmapped)


def _send_candidate(
    ctx: _Ctx, task: Task, record: TaskRecord | None, type_: str, project: Mapping,
    label: str, unmapped: bool,
) -> TaskDecision | None:
    if unmapped:
        why = "unmapped_project"
    else:
        why = "follow_up" if settings.resume_playbook(ctx.dispatch, type_) else "new_task"
    repo = None if unmapped else project["repo"]
    body = triage.candidate_body(
        task=task, type_=type_, reason=why, repo=repo, label=label,
        session=_existing_session(ctx, label, record),
        over_soft_limit=_over_soft_limit(ctx, record),
        max_chars=ctx.dispatch["title_max_chars"],
    )
    try:
        ctx.send(from_agent=ctx.dispatch["bus_sender"], to=ctx.dispatcher.name, body=body)
    except (UnknownRecipientError, ValueError):
        return None
    fields: dict[str, Any] = {"candidate_type": type_, "candidate_at": ctx.stamp, "label": label}
    if not unmapped:
        fields["repo"] = project["repo"]
    ctx.store.update(task.source, task.task_id, **fields)
    ctx.store.log_event(
        at=ctx.stamp, event=EVENT_CANDIDATE, actor="runner", source=task.source,
        task_id=task.task_id, detail={"type": type_, "reason": why, "to": ctx.dispatcher.name},
    )
    return _decision(ctx, task, type_, "triage", "delegated", label=label)


def _handle_unmapped(ctx: _Ctx, task: Task) -> None:
    """A task of a project without a mapping: only an offered candidate, and only
    for sources that list repo patterns. Otherwise it is dropped, as before."""
    if not settings.unmapped_repos(ctx.dispatch, task.source):
        return
    record = ctx.store.get(task.source, task.task_id)
    type_ = classify.classify(task, record, rules=ctx.dispatch["rules"])
    if settings.type_action(ctx.dispatch, type_) != "spawn":
        return
    # a follow-up on a task that already has a repo keeps it: decide() stores no
    # decision_repo for it
    chosen = record and (record.decision_repo or record.repo)
    if record is not None and record.decision in _DECISIONS_TO_EXECUTE and chosen:
        _spawn_unmapped(ctx, task, record, type_, chosen)
        return
    if not _triage_active(ctx) and not (record is not None and record.decision == "skip"):
        return
    if not ctx.dry_run:
        first = task.memberships[0].project_id if task.memberships else None
        record = ctx.store.observe(task, project_id=first, now=ctx.stamp)
    decision = _triage(ctx, task, record, type_, None, {"repo": None}, unmapped=True)
    if decision is not None:
        ctx.report.decisions.append(decision)


def _spawn_unmapped(
    ctx: _Ctx, task: Task, record: TaskRecord, type_: str, repo: str
) -> None:
    project = settings.default_project(ctx.dispatch, repo)
    patterns = settings.type_repos(ctx.dispatch, type_)
    if patterns is not None and not settings.repo_allowed(
        patterns, _repo_id(ctx, project["repo"])
    ):
        ctx.report.decisions.append(
            _decision(ctx, task, type_, "spawn", "failed",
                      reason=f"repo not allowed for {type_}")
        )
        return
    ctx.report.decisions.append(
        _spawn_task(ctx, task, record, type_, record.project_id or "", project)
    )


def _repo_id(ctx: _Ctx, repo: str) -> str | None:
    """The repo's id (`<host>/<path>`), cached for the run; None when unknown."""
    if repo in ctx.repo_ids:
        return ctx.repo_ids[repo]
    try:
        found: str | None = repo_identity.resolve(
            repo, storage=ctx.storage, cfg=ctx.cfg["repos"], run=ctx.run, persist=False,
        ).repo_id
    except LookupError:
        found = repo_identity.normalize_remote(repo, ctx.cfg["repos"].get("host_aliases"))
    ctx.repo_ids[repo] = found
    return found


def _notify_only(
    ctx: _Ctx, task: Task, record: TaskRecord | None, type_: str, project: Mapping
) -> TaskDecision:
    if ctx.dry_run:
        return _decision(ctx, task, type_, "notify", "would_notify")
    wanted = "notify" in ctx.dispatch["notify_on"]
    if wanted and not _notify(
        ctx, Notification(None, f"{type_} (notify-only)", task.title, task.url)
    ):
        # Nothing recorded: the task stays unhandled and the next run retries.
        return _decision(ctx, task, type_, "notify", "failed",
                         reason="no notifier delivered; will retry")
    fields: dict[str, Any] = {}
    if not (record and record.session_name and record.outcome in _WATCHED_OUTCOMES):
        # A tracked session keeps its outcome so its later transitions still notify.
        fields = {"outcome": OUTCOME_NOTIFIED, "last_notified_state": "notified"}
    ctx.store.update(
        task.source, task.task_id, type=type_, handled_types=_with_type(record, type_),
        repo=project["repo"], handled_at=ctx.stamp,
        last_comment_handled_at=task.newest_comment_at, **fields,
    )
    return _decision(ctx, task, type_, "notify", "notified")


def _running(ctx: _Ctx) -> int:
    """Spawned sessions in an active state plus the ones started in this run."""
    active = sum(1 for r in ctx.rows if r.spawned and getattr(r, "role", None) is None
        and r.state in status.ACTIVE_STATES)
    return active + ctx.spawns


def _over_soft_limit(ctx: _Ctx, record: TaskRecord | None) -> bool:
    soft = ctx.dispatch["soft_limit"]
    if soft is None or (record is not None and record.approved_at):
        return False
    return _running(ctx) >= soft


def _ask_dispatcher(
    ctx: _Ctx, task: Task, record: TaskRecord | None, type_: str, label: str,
) -> bool:
    """Send the approval request to the dispatcher; True when it took it."""
    body = triage.approval_body(
        task=task, type_=type_, label=label, running=_running(ctx),
        soft_limit=ctx.dispatch["soft_limit"], max_chars=ctx.dispatch["title_max_chars"],
    )
    try:
        ctx.send(from_agent=ctx.dispatch["bus_sender"], to=ctx.dispatcher.name, body=body)
    except (UnknownRecipientError, ValueError):
        return False
    ctx.store.update(task.source, task.task_id, held_at=ctx.stamp)
    ctx.store.log_event(
        at=ctx.stamp, event=EVENT_APPROVAL_REQUEST, actor="runner", source=task.source,
        task_id=task.task_id, detail={"type": type_, "to": ctx.dispatcher.name},
    )
    return True


def _hold(
    ctx: _Ctx, task: Task, record: TaskRecord | None, type_: str, label: str,
) -> TaskDecision:
    """Hold a spawn past the soft limit until the owner approves it. Nothing but
    `held_at` is recorded, so the task is classified the same way next run. With
    a live dispatcher the approval request goes to it; else the owner is alerted."""
    via_dispatcher = ctx.dispatcher is not None
    if not ctx.dry_run and (record is None or record.held_at is None):
        if via_dispatcher and _ask_dispatcher(ctx, task, record, type_, label):
            pass
        else:
            via_dispatcher = False
            wanted = "needs_approval" in ctx.dispatch["notify_on"]
            # An undelivered alert is retried next run, like the other notifications.
            if not wanted or _notify(
                ctx, Notification(label, "needs approval", task.title, task.url)
            ):
                ctx.store.update(task.source, task.task_id, held_at=ctx.stamp)
    hint = (
        "the dispatcher was asked" if via_dispatcher
        else f"run `agent-bus dispatch approve {task.task_id}`"
    )
    reason = (
        f"needs approval: {_running(ctx)} sessions running, soft limit "
        f"{ctx.dispatch['soft_limit']}; {hint}"
    )
    result = "would_hold" if ctx.dry_run else "held"
    return _decision(ctx, task, type_, "spawn", result, reason=reason, label=label)


def _session_rows(ctx: _Ctx, label: str, record: TaskRecord | None) -> list:
    """The status rows of this task's session (a renamed session has moved)."""
    return [
        row for row in ctx.rows
        if row.name == (record.session_name if record else None)
        or row.label == label
        or (row.spawned and row.tmux_window == label)
        or (record is not None and find_row([row], record) is not None)
    ]


def _skip_reason(ctx: _Ctx, label: str, record: TaskRecord | None) -> str | None:
    """Why a spawn must not happen now, or None."""
    for row in _session_rows(ctx, label, record):
        if row.live or row.state in status.ACTIVE_STATES:
            return "session active"
    if _running(ctx) >= ctx.cfg["spawn"]["max_sessions"]:
        return "spawn cap reached"
    if ctx.spawns >= ctx.dispatch["max_spawns_per_run"]:
        return "per-run limit reached"
    return None


def _git_out(ctx: _Ctx, argv: list[str], timeout: float) -> str | None:
    try:
        done = ctx.run(argv, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def _head_sha(ctx: _Ctx, project: Mapping) -> str:
    try:
        local = repo_identity.resolve(
            project["repo"], storage=ctx.storage, cfg=ctx.cfg["repos"],
            run=ctx.run, persist=False,
        )
    except LookupError:
        return UNKNOWN
    path = str(local.path)
    if project.get("fetch") and not ctx.dry_run:
        _git_out(ctx, ["git", "-C", path, "fetch", "--quiet", ctx.cfg["repos"]["remote"]],
                 ctx.dispatch["fetch_timeout_seconds"])
    ref = project.get("worktree") or HEAD
    sha = _git_out(ctx, ["git", "-C", path, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
                   ctx.cfg["repos"]["git_timeout_seconds"])
    return sha or UNKNOWN


def _in_flight(ctx: _Ctx, record: TaskRecord) -> bool:
    row = find_row(ctx.rows, record) if record.session_name else None
    return row is not None and bool(row.live or row.state in status.ACTIVE_STATES)


def _overlap(ctx: _Ctx, task: Task, project_id: str) -> str:
    """Same-project tasks: sessions in flight first, then the most recently
    handled, so the capped list never pins the oldest tasks forever."""
    others = [
        r for r in ctx.store.list_tasks()
        if r.source == task.source and r.project_id == project_id
        and r.task_id != task.task_id and (r.handled_at or r.outcome == OUTCOME_SPAWNED)
    ]
    others.sort(
        key=lambda r: (r.last_spawned_at or r.handled_at or "", r.task_id), reverse=True
    )
    others.sort(key=lambda r: not _in_flight(ctx, r))  # stable: in flight first
    return playbook.overlap_text(
        others, limit=ctx.dispatch["overlap_max_items"],
        max_chars=ctx.dispatch["title_max_chars"],
    )


def _values(
    ctx: _Ctx, task: Task, type_: str, label: str, project_id: str, project: Mapping,
) -> dict[str, str]:
    d, cap = ctx.dispatch, ctx.dispatch["title_max_chars"]
    in_project = [m for m in task.memberships if m.project_id == project_id]

    def untrusted(value: str | None) -> str:
        return clean_untrusted(value or "-", cap)

    return {
        "task_url": untrusted(task.url),
        "task_gid": untrusted(task.task_id),
        "task_title": clean_untrusted(task.title, cap),
        "task_type": type_,
        "source": untrusted(task.source),
        "project_name": clean_untrusted(in_project[0].project_name if in_project else "", cap),
        "project_gid": untrusted(project_id),
        "section_names": clean_untrusted(
            ", ".join(m.section_name for m in in_project if m.section_name), cap),
        "due_on": untrusted(task.due_on),
        "modified_at": untrusted(task.modified_at),
        "newest_comment_at": untrusted(task.newest_comment_at),
        "repo": project["repo"],
        "label": label,
        "handoff_channel": d["handoff_channel"].format(
            label=label, repo=project["repo"], gid=untrusted(task.task_id), type=type_),
        "overlap": _overlap(ctx, task, project_id),
        "head_sha": _head_sha(ctx, project),
        "base_ref": project.get("worktree") or HEAD,
    }


def _request(project: Mapping, label: str, prompt: Path) -> SpawnRequest:
    return SpawnRequest(
        repo=project["repo"], client=project["client"], name=label, prompt_file=prompt,
        model=project["model"], worktree=project["worktree"],
        clone=project["clone"], remote=project["remote"],
    )


def _label_for(ctx: _Ctx, task: Task, type_: str) -> tuple[str | None, str | None]:
    """(label, failure reason)."""
    try:
        label = classify.build_label(task, type_, ctx.dispatch["label"])
    except identity.InvalidNameError as exc:
        return None, f"invalid label: {exc}"
    for other in ctx.store.by_label(label):
        if (other.source, other.task_id) != (task.source, task.task_id):
            return label, "label collision; raise dispatch.label.gid_suffix_length"
    return label, None


def _spawn_task(
    ctx: _Ctx, task: Task, record: TaskRecord, type_: str, project_id: str, project: Mapping,
) -> TaskDecision:
    label, failure = _label_for(ctx, task, type_)
    if failure is not None:
        return _decision(ctx, task, type_, "spawn", "failed", reason=failure, label=label)
    resume_playbook = settings.resume_playbook(ctx.dispatch, type_)
    rows = _session_rows(ctx, label, record)
    if record is not None and record.decision == "start":
        resume_playbook = None  # the dispatcher chose a new session
    if resume_playbook:
        live = [row for row in rows if row.live]
        if live:
            return _message_live(ctx, task, record, type_, project_id, project, label,
                                 _latest(live), resume_playbook)
    reason = _skip_reason(ctx, label, record)
    if reason is not None:
        if (reason == "session active" and not ctx.dry_run and record is not None
                and record.decision == "start"):
            # A new session cannot start beside a live one: make the candidate
            # pending again so the dispatcher can pick resume.
            ctx.store.update(task.source, task.task_id, **_REOPEN)
            reason = "session active; start decision reopened, decide again (resume messages it)"
        return _decision(ctx, task, type_, "spawn", "skipped", reason=reason, label=label)
    if _over_soft_limit(ctx, record):
        return _hold(ctx, task, record, type_, label)
    new_reason = None
    if resume_playbook and record.session_name:
        prior = find_row(ctx.rows, record) or (_latest(rows) if rows else None)
        found, why = _resumable(ctx, record, prior, project, label)
        if found is not None:
            return _resume_task(ctx, task, record, type_, project_id, project, label,
                                prior, found, resume_playbook)
        new_reason = f"new session: {why}"
    route, answer = _route(ctx, task, record, type_, project_id, project)
    project = {**project, "client": route.client, "model": route.model}
    try:
        values = _values(ctx, task, type_, label, project_id, project)
        text = playbook.render(
            settings.playbook_name(ctx.dispatch, project, type_), values,
            templates_dir=ctx.dispatch["templates_dir"],
        )
        if ctx.dry_run:
            # Count it so the per-run limit and session cap read like a real run.
            ctx.spawns += 1
            return _decision(ctx, task, type_, "spawn", "would_spawn",
                             reason=new_reason, label=label)
        prompt = playbook.write_prompt(
            text, label=label, prompts_dir=settings.prompts_dir(ctx.dispatch))
        result = ctx.spawn_fn(
            _request(project, label, prompt),
            storage=ctx.storage, cfg=ctx.cfg, run=ctx.run, now=ctx.now,
        )
    except SpawnError as exc:
        if exc.exit_code == spawn.EXIT_CAP_REACHED:
            return _decision(ctx, task, type_, "spawn", "skipped",
                             reason="spawn cap reached", label=label)
        return _spawn_failed(ctx, task, record, type_, project, label, exc)
    except _SPAWN_FAILURES as exc:
        return _spawn_failed(ctx, task, record, type_, project, label, exc)
    return _spawned(ctx, task, record, type_, project, label, result, reason=new_reason,
                    start_commit=values["head_sha"], routed=(type_, route, answer))


def _route(
    ctx: _Ctx, task: Task, record: TaskRecord | None, type_: str, project_id: str,
    project: Mapping,
) -> tuple[routing.Route, decision_model.ShadowAnswer | None]:
    """The client and model for a new session, and the decision model's answer
    when it was consulted (never in a dry run)."""
    if record is not None and record.decision_client:
        route = routing.Route(record.decision_client, record.decision_model,
                              routing.BY_DISPATCHER)
        return route, None
    routing_cfg = ctx.dispatch["routing"]
    dm_cfg = ctx.dispatch["decision_model"]
    needs_id = bool(routing_cfg["enabled"] or dm_cfg["enabled"])
    signals = routing.Signals(
        type=type_, repo=project["repo"],
        repo_id=_repo_id(ctx, project["repo"]) if needs_id else None,
        title=clean_text(task.title, ctx.dispatch["title_max_chars"]),
        sections=tuple(
            m.section_name for m in task.memberships
            if m.project_id == project_id and m.section_name
        ),
    )
    route = routing.choose(
        routing_cfg, signals, static_client=project["client"], static_model=project["model"],
        available=ctx.available, quota=ctx.quota,
    )
    if not dm_cfg["enabled"] or ctx.dry_run:
        return route, None
    clients = [c for c in routing_cfg["clients"] if ctx.available(c)]
    answer = decision_model.consult(
        dm_cfg, signals, clients, complexity_max=routing_cfg["complexity"]["max"],
        http_post=ctx.http_post,
    )
    route = decision_model.apply(
        dm_cfg, route, answer, routing_cfg=routing_cfg, available=ctx.available,
    )
    return route, answer


def _log_routing(
    ctx: _Ctx, task: Task, type_: str, route: routing.Route,
    answer: decision_model.ShadowAnswer | None,
) -> None:
    dm_cfg = ctx.dispatch["decision_model"]
    if not (ctx.dispatch["routing"]["enabled"] or dm_cfg["enabled"]):
        return
    agree = answer.client == route.client if answer is not None and answer.client else None
    ctx.store.log_event(
        at=ctx.stamp, event=EVENT_ROUTING, actor="runner", source=task.source,
        task_id=task.task_id,
        detail={
            "type": type_, "actual": route.to_dict(),
            "shadow": answer.to_dict() if answer is not None else None,
            "mode": dm_cfg["mode"] if dm_cfg["enabled"] else None,
            "agree": agree,
        },
    )


def _executed(
    ctx: _Ctx, task: Task, record: TaskRecord | None, result: str, session: str,
) -> None:
    ctx.store.log_event(
        at=ctx.stamp, event=EVENT_EXECUTED, actor="runner", source=task.source,
        task_id=task.task_id,
        detail={"result": result, "session": session,
                "by": "dispatcher" if record is not None and record.decision else "runner"},
    )


def _latest(rows: Sequence[Any]) -> Any:
    return max(rows, key=lambda r: (r.state_at or "", r.last_seen or ""))


def _message_live(
    ctx: _Ctx, task: Task, record: TaskRecord, type_: str, project_id: str,
    project: Mapping, label: str, row: Any, playbook_name: str,
) -> TaskDecision:
    """A live session gets the follow-up through the bus. It counts against no
    limit: nothing is started."""
    try:
        text = playbook.render(
            playbook_name, _values(ctx, task, type_, label, project_id, project),
            templates_dir=ctx.dispatch["templates_dir"],
        )
    except PlaybookError as exc:
        return _decision(ctx, task, type_, "message", "failed", reason=str(exc),
                         label=label, session=row.name)
    if ctx.dry_run:
        return _decision(ctx, task, type_, "message", "would_message",
                         label=label, session=row.name)
    try:
        ctx.send(from_agent=ctx.dispatch["bus_sender"], to=row.name, body=text)
    except (UnknownRecipientError, ValueError):
        return _decision(ctx, task, type_, "message", "failed",
                         reason=f"could not message {row.name}; will retry",
                         label=label, session=row.name)
    # The marker state differs from every reported state; _transition holds
    # back until the session reports a state after handled_at, so that report
    # notifies even when it equals the one notified before.
    ctx.store.update(
        task.source, task.task_id, type=type_, handled_types=_with_type(record, type_),
        repo=project["repo"], label=label, session_name=row.name,
        handled_at=ctx.stamp, last_comment_handled_at=task.newest_comment_at,
        outcome=OUTCOME_SPAWNED, last_notified_state="messaged", **_CLEAR_TRIAGE,
    )
    _executed(ctx, task, record, "messaged", row.name)
    return _decision(ctx, task, type_, "message", "messaged", label=label, session=row.name)


def _resumable(
    ctx: _Ctx, record: TaskRecord, row: Any | None, project: Mapping, label: str,
) -> tuple[ResumableSession | None, str]:
    """(the session to resume, or None and why it cannot be)."""
    client = record.client or project["client"]
    if not (ctx.cfg["spawn"]["clients"].get(client) or {}).get("resume_prompt"):
        return None, f"no resume template for {client}"
    work_dir = record.work_dir or (row.work_dir if row else None)
    clone = record.clone_path or (row.repo_path if row else None)
    if not work_dir or not clone or not Path(work_dir).is_dir():
        return None, "its working folder is gone"
    found = ctx.find_session(
        client, cfg=ctx.cfg, clone=Path(clone), work_dir=Path(work_dir),
        label=record.label or label,
    )
    if found is None:
        return None, "no resumable session"
    limit = ctx.dispatch["resume_max_transcript_mb"]
    if found.size_bytes > limit * 1024 * 1024:
        return None, f"transcript over {limit} MB"
    return found, ""


def _resume_task(
    ctx: _Ctx, task: Task, record: TaskRecord, type_: str, project_id: str,
    project: Mapping, label: str, row: Any | None, found: ResumableSession,
    playbook_name: str,
) -> TaskDecision:
    address = row.name if row else record.session_name
    work_dir = record.work_dir or row.work_dir
    clone = record.clone_path or row.repo_path
    try:
        text = playbook.render(
            playbook_name, _values(ctx, task, type_, label, project_id, project),
            templates_dir=ctx.dispatch["templates_dir"],
        )
        if ctx.dry_run:
            ctx.spawns += 1
            return _decision(ctx, task, type_, "resume", "would_resume",
                             label=label, session=address)
        prompt = playbook.write_prompt(
            text, label=label, prompts_dir=settings.prompts_dir(ctx.dispatch))
        result = ctx.resume_fn(
            spawn.ResumeRequest(
                address=address, client=record.client or project["client"], label=label,
                work_dir=Path(work_dir), repo_path=Path(clone), prompt_file=prompt,
                session_id=found.session_id,
                model=record.model if record.client else project["model"],
                remote=project["remote"],
            ),
            storage=ctx.storage, cfg=ctx.cfg, run=ctx.run, now=ctx.now,
        )
    except SpawnError as exc:
        if exc.exit_code == spawn.EXIT_CAP_REACHED:
            return _decision(ctx, task, type_, "resume", "skipped",
                             reason="spawn cap reached", label=label)
        return _spawn_failed(ctx, task, record, type_, project, label, exc)
    except _SPAWN_FAILURES as exc:
        return _spawn_failed(ctx, task, record, type_, project, label, exc)
    return _spawned(ctx, task, record, type_, project, label, result,
                    action="resume", decision_result="resumed")


def _spawned(
    ctx: _Ctx, task: Task, record: TaskRecord, type_: str, project: Mapping,
    label: str, result: Any, *, reason: str | None = None, action: str = "spawn",
    decision_result: str = "spawned", start_commit: str | None = None,
    routed: tuple[str, routing.Route, decision_model.ShadowAnswer | None] | None = None,
) -> TaskDecision:
    session = result.name
    extra: dict[str, Any] = {}
    if action == "spawn":  # a resume keeps the folder, so keeps its start commit
        extra["start_commit"] = start_commit if start_commit != UNKNOWN else None
        extra["client"], extra["model"] = project["client"], project["model"]
    ctx.store.update(
        task.source, task.task_id, type=type_, handled_types=_with_type(record, type_),
        repo=project["repo"], label=label, session_name=session,
        last_spawned_at=ctx.stamp, handled_at=ctx.stamp,
        last_comment_handled_at=task.newest_comment_at,
        outcome=OUTCOME_SPAWNED, last_notified_state="starting",
        work_dir=result.work_dir, clone_path=result.repo_path,
        worktree_removed_at=None, held_at=None, approved_at=None, **_CLEAR_TRIAGE, **extra,
    )
    _executed(ctx, task, record, decision_result, session)
    if action == "spawn" and routed is not None:
        _log_routing(ctx, task, routed[0], routed[1], routed[2])
    ctx.spawns += 1
    return _decision(ctx, task, type_, action, decision_result, reason=reason,
                     label=label, session=session)


def _spawn_failed(
    ctx: _Ctx, task: Task, record: TaskRecord, type_: str, project: Mapping,
    label: str, exc: Exception,
) -> TaskDecision:
    if not ctx.dry_run:
        ctx.store.update(task.source, task.task_id, repo=project["repo"], label=label,
                         outcome=OUTCOME_SPAWN_FAILED)
        if record.last_notified_state != "spawn_failed":
            wanted = "spawn_failed" in ctx.dispatch["notify_on"]
            if not wanted or _notify(
                ctx, Notification(label, "spawn failed", task.title, task.url)
            ):
                ctx.store.update(task.source, task.task_id, last_notified_state="spawn_failed")
    return _decision(ctx, task, type_, "spawn", "failed", reason=str(exc), label=label)


# ---------------------------------- run -------------------------------------


def _fetch_all(ctx: _Ctx, sources: Sequence[TaskSource], today) -> None:
    for source in sources:
        try:
            tasks = source.fetch(today=today)
        except SourceError as exc:
            ctx.report.errors.append(f"{source.name}: {exc}")
            continue
        for task in tasks:
            _handle_task(ctx, task)


def _client_available(cfg: Mapping) -> Callable[[str], bool]:
    """A configured client whose launch command is on PATH."""
    def available(client: str) -> bool:
        command = (cfg["spawn"]["clients"].get(client) or {}).get("command")
        return bool(command) and shutil.which(command[0]) is not None

    return available


def run(
    *,
    cfg: Mapping,
    storage: Storage,
    store: DispatchStore,
    sources: Sequence[TaskSource],
    notifiers: Sequence[Notifier],
    now: datetime | None = None,
    tz: tzinfo | None = None,
    dry_run: bool = False,
    ignore_hours: bool = False,
    run: commands.Runner = commands.run_command,
    spawn_fn: Callable[..., Any] = spawn.spawn,
    resume_fn: Callable[..., Any] = spawn.resume,
    find_session: Callable[..., Any] = spawn.find_resumable,
    send: Callable[..., dict] | None = None,
    collect: Callable[..., list] = status.collect,
    available: Callable[[str], bool] | None = None,
    http_post: Callable[..., bytes] = decision_model.default_http_post,
    quota: Callable[[str], float] = routing.no_quota,
) -> RunReport:
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(tz)
    d = cfg["dispatch"]
    if not (dry_run or ignore_hours or settings.within_working_hours(d["working_hours"], local)):
        return RunReport(skipped="outside working hours")
    if available is None:
        available = _client_available(cfg)
    ctx = _Ctx(
        cfg=cfg, dispatch=d, storage=storage, store=store, notifiers=notifiers, now=now,
        dry_run=dry_run, run=run, spawn_fn=spawn_fn, resume_fn=resume_fn,
        find_session=find_session, send=send or storage.send_message, rows=[],
        report=RunReport(dry_run=dry_run), available=available, http_post=http_post,
        quota=quota,
    )
    if dry_run:
        return _execute(ctx, sources, local.date(), collect)
    with run_lock(settings.lock_path()) as acquired:
        if not acquired:
            return RunReport(skipped="another run is in progress")
        return _execute(ctx, sources, local.date(), collect)


def _live_dispatcher(ctx: _Ctx) -> Any | None:
    """The dispatcher's status row when it is live and holds a valid authority."""
    if not ctx.storage.has_authority(authority.ROLE_DISPATCHER):
        return None
    for row in ctx.rows:
        if getattr(row, "role", None) == authority.ROLE_DISPATCHER and row.live:
            return row
    return None


def _execute(ctx: _Ctx, sources: Sequence[TaskSource], today, collect) -> RunReport:
    ctx.rows = collect(
        ctx.storage, grace_seconds=ctx.cfg["spawn"]["startup_grace_seconds"],
        now=ctx.now, include_ended=True,
    )
    ctx.dispatcher = _live_dispatcher(ctx)
    _transitions(ctx)
    _cleanup(ctx)
    _fetch_all(ctx, sources, today)
    return ctx.report

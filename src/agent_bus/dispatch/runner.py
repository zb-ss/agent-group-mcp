"""The dispatch run: lock, working hours, session transitions, then for every
task classify and either spawn a session, notify, or skip.

Shaped like `spawn.py`: frozen result dataclasses, keyword-only seams for
everything that touches the outside (sources, notifiers, git, spawn, status).
Task text is untrusted: it reaches the rendered prompt file and the store,
never an argv.
"""

from __future__ import annotations

import fcntl
import subprocess
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from typing import Any

from .. import commands, identity, repo_identity, spawn, status
from ..config import ConfigError
from ..spawn import SpawnError, SpawnRequest
from ..clients import agy as agy_client
from ..clients.base import ResumableSession
from ..storage import Storage, UnknownRecipientError
from . import classify, cleanup, playbook, settings
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
from .store import DispatchStore, TaskRecord

SOURCES: dict[str, Callable[..., TaskSource]] = {"asana": AsanaSource}

RESULTS = (
    "spawned", "would_spawn", "held", "would_hold", "notified", "would_notify",
    "messaged", "would_message", "resumed", "would_resume", "skipped", "failed", "ignored",
)
UNKNOWN = "unknown"
HEAD = "HEAD"
STATE_LABELS = {"waiting": "needs review", "done": "done", "ended": "ended without done"}
_OUTCOME_FOR_STATE = {"done": OUTCOME_DONE, "ended": OUTCOME_ENDED}
_WATCHED_OUTCOMES = (OUTCOME_SPAWNED, OUTCOME_DONE, OUTCOME_ENDED)
_SPAWN_FAILURES = (SpawnError, ConfigError, PlaybookError, OSError)


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
        project = settings.project_settings(ctx.dispatch, record.source, record.project_id)
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
    project = settings.project_settings(ctx.dispatch, record.source, record.project_id) or {}
    if project.get("client") != agy_client.CLIENT_ID:
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
        return
    project_id, project = mapped
    record = ctx.store.get(task.source, task.task_id)
    if not ctx.dry_run:
        record = ctx.store.observe(task, project_id=project_id, now=ctx.stamp)
    type_ = classify.classify(task, record, rules=ctx.dispatch["rules"])
    action = settings.type_action(ctx.dispatch, type_)
    if type_ == classify.TYPE_IGNORE or action == settings.TYPE_IGNORE:
        ctx.report.decisions.append(_decision(ctx, task, type_, "ignore", "ignored"))
    elif action == "notify":
        ctx.report.decisions.append(_notify_only(ctx, task, record, type_, project))
    else:
        ctx.report.decisions.append(
            _spawn_task(ctx, task, record, type_, project_id, project)
        )


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
    active = sum(1 for r in ctx.rows if r.spawned and r.state in status.ACTIVE_STATES)
    return active + ctx.spawns


def _over_soft_limit(ctx: _Ctx, record: TaskRecord | None) -> bool:
    soft = ctx.dispatch["soft_limit"]
    if soft is None or (record is not None and record.approved_at):
        return False
    return _running(ctx) >= soft


def _hold(
    ctx: _Ctx, task: Task, record: TaskRecord | None, type_: str, label: str,
) -> TaskDecision:
    """Hold a spawn past the soft limit until the owner approves it. Nothing but
    `held_at` is recorded, so the task is classified the same way next run."""
    reason = (
        f"needs approval: {_running(ctx)} sessions running, soft limit "
        f"{ctx.dispatch['soft_limit']}; run `agent-bus dispatch approve {task.task_id}`"
    )
    if ctx.dry_run:
        return _decision(ctx, task, type_, "spawn", "would_hold", reason=reason, label=label)
    if record is None or record.held_at is None:
        wanted = "needs_approval" in ctx.dispatch["notify_on"]
        # An undelivered alert is retried next run, like the other notifications.
        if not wanted or _notify(
            ctx, Notification(label, "needs approval", task.title, task.url)
        ):
            ctx.store.update(task.source, task.task_id, held_at=ctx.stamp)
    return _decision(ctx, task, type_, "spawn", "held", reason=reason, label=label)


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
    if resume_playbook:
        live = [row for row in rows if row.live]
        if live:
            return _message_live(ctx, task, record, type_, project_id, project, label,
                                 _latest(live), resume_playbook)
    reason = _skip_reason(ctx, label, record)
    if reason is not None:
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
                    start_commit=values["head_sha"])


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
        outcome=OUTCOME_SPAWNED, last_notified_state="messaged",
    )
    return _decision(ctx, task, type_, "message", "messaged", label=label, session=row.name)


def _resumable(
    ctx: _Ctx, record: TaskRecord, row: Any | None, project: Mapping, label: str,
) -> tuple[ResumableSession | None, str]:
    """(the session to resume, or None and why it cannot be)."""
    client = project["client"]
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
                address=address, client=project["client"], label=label,
                work_dir=Path(work_dir), repo_path=Path(clone), prompt_file=prompt,
                session_id=found.session_id, model=project["model"],
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
) -> TaskDecision:
    session = result.name
    extra: dict[str, Any] = {}
    if action == "spawn":  # a resume keeps the folder, so keeps its start commit
        extra["start_commit"] = start_commit if start_commit != UNKNOWN else None
    ctx.store.update(
        task.source, task.task_id, type=type_, handled_types=_with_type(record, type_),
        repo=project["repo"], label=label, session_name=session,
        last_spawned_at=ctx.stamp, handled_at=ctx.stamp,
        last_comment_handled_at=task.newest_comment_at,
        outcome=OUTCOME_SPAWNED, last_notified_state="starting",
        work_dir=result.work_dir, clone_path=result.repo_path,
        worktree_removed_at=None, held_at=None, approved_at=None, **extra,
    )
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
) -> RunReport:
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(tz)
    d = cfg["dispatch"]
    if not (dry_run or ignore_hours or settings.within_working_hours(d["working_hours"], local)):
        return RunReport(skipped="outside working hours")
    ctx = _Ctx(
        cfg=cfg, dispatch=d, storage=storage, store=store, notifiers=notifiers, now=now,
        dry_run=dry_run, run=run, spawn_fn=spawn_fn, resume_fn=resume_fn,
        find_session=find_session, send=send or storage.send_message, rows=[],
        report=RunReport(dry_run=dry_run),
    )
    if dry_run:
        return _execute(ctx, sources, local.date(), collect)
    with run_lock(settings.lock_path()) as acquired:
        if not acquired:
            return RunReport(skipped="another run is in progress")
        return _execute(ctx, sources, local.date(), collect)


def _execute(ctx: _Ctx, sources: Sequence[TaskSource], today, collect) -> RunReport:
    ctx.rows = collect(
        ctx.storage, grace_seconds=ctx.cfg["spawn"]["startup_grace_seconds"],
        now=ctx.now, include_ended=True,
    )
    _transitions(ctx)
    _cleanup(ctx)
    _fetch_all(ctx, sources, today)
    return ctx.report

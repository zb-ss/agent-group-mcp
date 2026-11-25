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
from ..storage import Storage
from . import classify, playbook, settings
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
    "spawned", "would_spawn", "notified", "would_notify", "skipped", "failed", "ignored",
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
    rows: list
    report: RunReport
    spawns: int = 0

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


def _transition(ctx: _Ctx, record: TaskRecord, rows: Sequence[Any]) -> None:
    row = find_row(rows, record)
    state = row.state if row is not None else "ended"
    if row is not None and row.name != record.session_name and not ctx.dry_run:
        record = ctx.store.update(record.source, record.task_id, session_name=row.name)
    if state == record.last_notified_state:
        return
    notified = state in STATE_LABELS and state in ctx.dispatch["notify_on"]
    delivered = False
    if not ctx.dry_run:
        delivered = notified and _notify(
            ctx, Notification(record.session_name, STATE_LABELS[state], record.title, record.url)
        )
        fields: dict[str, Any] = {"outcome": _OUTCOME_FOR_STATE.get(state, OUTCOME_SPAWNED)}
        if delivered or not notified:
            fields["last_notified_state"] = state
        ctx.store.update(record.source, record.task_id, **fields)
    ctx.report.transitions.append(
        {"task_id": record.task_id, "session": record.session_name, "state": state,
         "notified": delivered}
    )


def _transitions(ctx: _Ctx) -> None:
    for record in ctx.store.list_tasks():
        if record.session_name and record.outcome in _WATCHED_OUTCOMES:
            _transition(ctx, record, ctx.rows)


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


def _skip_reason(ctx: _Ctx, label: str, record: TaskRecord | None) -> str | None:
    """Why a spawn must not happen now, or None."""
    for row in ctx.rows:
        same = (
            row.name == (record.session_name if record else None)
            or row.label == label
            or (row.spawned and row.tmux_window == label)
            or (record is not None and find_row([row], record) is not None)
        )
        if same and (row.live or row.state in status.ACTIVE_STATES):
            return "session active"
    active = sum(1 for r in ctx.rows if r.spawned and r.state in status.ACTIVE_STATES)
    if active + ctx.spawns >= ctx.cfg["spawn"]["max_sessions"]:
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
    reason = _skip_reason(ctx, label, record)
    if reason is not None:
        return _decision(ctx, task, type_, "spawn", "skipped", reason=reason, label=label)
    try:
        text = playbook.render(
            settings.playbook_name(ctx.dispatch, project, type_),
            _values(ctx, task, type_, label, project_id, project),
            templates_dir=ctx.dispatch["templates_dir"],
        )
        if ctx.dry_run:
            # Count it so the per-run limit and session cap read like a real run.
            ctx.spawns += 1
            return _decision(ctx, task, type_, "spawn", "would_spawn", label=label)
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
    return _spawned(ctx, task, record, type_, project, label, result.name)


def _spawned(
    ctx: _Ctx, task: Task, record: TaskRecord, type_: str, project: Mapping,
    label: str, session: str,
) -> TaskDecision:
    ctx.store.update(
        task.source, task.task_id, type=type_, handled_types=_with_type(record, type_),
        repo=project["repo"], label=label, session_name=session,
        last_spawned_at=ctx.stamp, handled_at=ctx.stamp,
        last_comment_handled_at=task.newest_comment_at,
        outcome=OUTCOME_SPAWNED, last_notified_state="starting",
    )
    ctx.spawns += 1
    return _decision(ctx, task, type_, "spawn", "spawned", label=label, session=session)


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
    collect: Callable[..., list] = status.collect,
) -> RunReport:
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(tz)
    d = cfg["dispatch"]
    if not (dry_run or ignore_hours or settings.within_working_hours(d["working_hours"], local)):
        return RunReport(skipped="outside working hours")
    ctx = _Ctx(
        cfg=cfg, dispatch=d, storage=storage, store=store, notifiers=notifiers, now=now,
        dry_run=dry_run, run=run, spawn_fn=spawn_fn, rows=[],
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
    _fetch_all(ctx, sources, today)
    return ctx.report

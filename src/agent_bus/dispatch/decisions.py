"""Dispatcher decisions on candidates, and approvals, with their audit rows.

Pure store operations: the CLI and the runner call these; nothing here talks
to the bus except `actor_for`, which only reads the authority table.
"""

from __future__ import annotations

from collections.abc import Mapping

from .. import repo_identity, resolution, status
from . import runner, settings, store as store_mod
from .actor import ACTOR_CLI, ACTOR_DISPATCHER, actor_for  # noqa: F401
from .model import clean_text, clean_untrusted, is_untrusted
from .store import DispatchStore, TaskRecord

_SPAWNING = ("start", "resume")


class DecisionError(Exception):
    """A decision or approval the rules refuse."""


def find(store: DispatchStore, task_id: str, source: str | None = None) -> TaskRecord:
    matches = [
        r for r in store.list_tasks()
        if r.task_id == task_id and (source is None or r.source == source)
    ]
    if not matches:
        raise DecisionError(f"unknown task {task_id}")
    if len(matches) > 1:
        sources = ", ".join(sorted(r.source for r in matches))
        raise DecisionError(
            f"task {task_id} exists in several sources ({sources}); pass --source"
        )
    return matches[0]


def approve(
    store: DispatchStore, task_id: str, *, source: str | None = None, now: str, actor: str
) -> TaskRecord:
    record = find(store, task_id, source)
    updated = store.update(record.source, record.task_id, approved_at=now)
    store.log_event(
        at=now, event=store_mod.EVENT_APPROVE, actor=actor,
        source=record.source, task_id=record.task_id,
    )
    return updated


def candidates(store: DispatchStore, rows: list, dispatch_cfg: Mapping) -> list[dict]:
    max_chars = dispatch_cfg["title_max_chars"]
    out = []
    for record in store.pending_candidates():
        if is_untrusted(record.handled_types):
            # The author was removed from trusted_authors: never show their text.
            continue
        row = runner.find_row(rows, record) if record.session_name else None
        out.append(
            {
                "source": record.source,
                "task_id": record.task_id,
                "type": record.candidate_type,
                "state": "asked" if record.decision == "ask" else "pending",
                "candidate_at": record.candidate_at,
                "repo": record.repo,
                "label": record.label,
                "held": bool(record.held_at and not record.approved_at),
                "session": (
                    {"name": row.name, "state": row.state, "live": bool(row.live)}
                    if row is not None
                    else None
                ),
                "untrusted": {
                    "title": clean_untrusted(record.title, max_chars),
                    "url": clean_untrusted(record.url, max_chars),
                },
            }
        )
    return out


def _chosen_repo(
    record: TaskRecord, cfg: Mapping, repo: str | None
) -> str | None:
    dispatch = cfg["dispatch"]
    if record.repo:
        if repo is not None:
            raise DecisionError(f"task {record.task_id} already has repo {record.repo}; omit --repo")
        return None
    if repo is None:
        raise DecisionError(
            f"task {record.task_id} has no repo (unmapped project); pass --repo"
        )
    repo_id = repo_identity.normalize_remote(repo, cfg["repos"].get("host_aliases"))
    if repo_id is None:
        raise DecisionError(f"--repo {repo!r} does not name a repository remote")
    patterns = settings.unmapped_repos(dispatch, record.source)
    if not settings.repo_allowed(patterns, repo_id):
        raise DecisionError(
            f"repo {repo_id} does not match dispatch.sources.{record.source}.unmapped_repos"
        )
    type_patterns = settings.type_repos(dispatch, record.candidate_type or "")
    if not settings.repo_allowed(type_patterns, repo_id):
        raise DecisionError(
            f"repo {repo_id} does not match dispatch.types.{record.candidate_type}.repos"
        )
    return repo_id


def _check_routing(cfg: Mapping, client: str | None, model_name: str | None) -> None:
    if client is not None:
        routing_clients = cfg["dispatch"]["routing"]["clients"]
        if client not in routing_clients or client not in cfg["spawn"]["clients"]:
            raise DecisionError(
                f"client {client!r} is not a configured routing and spawn client"
            )
    if model_name is not None:
        if client is None:
            raise DecisionError("--model needs --client")
        if model_name.startswith("-"):
            raise DecisionError("--model must not start with '-'")


def decide(
    store: DispatchStore,
    cfg: Mapping,
    task_id: str,
    *,
    action: str,
    source: str | None = None,
    reason: str | None = None,
    repo: str | None = None,
    client: str | None = None,
    model: str | None = None,
    clone: bool = False,
    now: str,
    actor: str,
    rows: list | None = None,
) -> TaskRecord:
    record = find(store, task_id, source)
    if not record.candidate_at or record.decision not in (None, "ask"):
        raise DecisionError(f"task {task_id} is not a pending candidate")
    if is_untrusted(record.handled_types):
        raise DecisionError(
            f"task {task_id} is from an author outside trusted_authors; it takes no decision"
        )
    if action == "start" and rows is not None and record.session_name:
        row = runner.find_row(rows, record)
        if row is not None and (row.live or row.state in status.ACTIVE_STATES):
            raise DecisionError(
                f"task {task_id} has a live session ({row.name}); use resume to message it"
            )
    if action not in settings.DECISIONS:
        raise DecisionError(f"unknown action {action!r}; use one of {', '.join(settings.DECISIONS)}")
    if action not in _SPAWNING:
        if repo is not None or client is not None or model is not None or clone:
            raise DecisionError(
                "--repo, --client, --model and --clone only apply to start and resume"
            )
        decision_repo = None
    else:
        decision_repo = _chosen_repo(record, cfg, repo)
        _check_routing(cfg, client, model)
    stored_reason = (
        clean_text(reason, cfg["dispatch"]["title_max_chars"]) if reason else None
    )
    updated = store.update(
        record.source, record.task_id,
        decision=action, decision_at=now, decision_reason=stored_reason,
        decision_repo=decision_repo, decision_client=client, decision_model=model,
        decision_clone="1" if clone else None,
    )
    store.log_event(
        at=now, event=store_mod.EVENT_DECIDE, actor=actor,
        source=record.source, task_id=record.task_id,
        detail={
            "action": action, "reason": stored_reason, "repo": decision_repo,
            "client": client, "model": model, "clone": clone,
        },
    )
    return updated


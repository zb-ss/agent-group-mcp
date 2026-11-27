"""Rule-based task classification and session label derivation.

Pure functions, no I/O and no LLM: the first configured rule that matches
wins. Rules come from ``dispatch.rules`` (validated by the settings loader).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from .. import identity
from .model import Membership, Task
from .store import TaskRecord

TYPE_IGNORE = "ignore"


def has_new_comment(task: Task, record: TaskRecord | None) -> bool:
    """True when the task got a comment after the one last handled."""
    if record is None or record.handled_at is None:
        return False
    if task.newest_comment_at is None:
        return False
    return (
        record.last_comment_handled_at is None
        or task.newest_comment_at > record.last_comment_handled_at
    )


def _any_search(patterns: Sequence[str], texts: Sequence[str]) -> bool:
    return any(re.search(p, t) for p in patterns for t in texts)


def _scoped_memberships(
    rule: Mapping, memberships: Sequence[Membership]
) -> Sequence[Membership]:
    projects = rule.get("project")
    if not projects:
        return memberships
    wanted = {str(p) for p in projects}
    return [m for m in memberships if m.project_id in wanted]


def _matches(
    rule: Mapping,
    task: Task,
    record: TaskRecord | None,
    memberships: Sequence[Membership],
) -> bool:
    task_type = rule.get("type")
    comment_driven = rule.get("new_comment") is True
    if (
        record is not None
        and not comment_driven
        and task_type in record.handled_types
    ):
        return False

    scoped = _scoped_memberships(rule, memberships)
    if rule.get("project") and not scoped:
        return False
    if "section" in rule:
        sections = [m.section_name for m in scoped if m.section_name]
        if not _any_search(rule["section"], sections):
            return False
    if "title" in rule and not _any_search(rule["title"], [task.title]):
        return False
    if "handled_as" in rule:
        if record is None or not set(rule["handled_as"]) & set(
            record.handled_types
        ):
            return False
    if "new_comment" in rule and bool(rule["new_comment"]) != has_new_comment(
        task, record
    ):
        return False
    return True


def classify(
    task: Task,
    record: TaskRecord | None,
    *,
    rules: Sequence[Mapping],
    memberships: Sequence[Membership] | None = None,
) -> str:
    """Return the type of the first matching rule, else the task's ``type_hint``
    (a type a source chose, unless already handled), else ``ignore``.

    ``memberships`` defaults to the task's own project/section pairs."""
    pairs = task.memberships if memberships is None else memberships
    for rule in rules:
        if _matches(rule, task, record, pairs):
            return str(rule["type"])
    hint = task.type_hint
    if hint and (record is None or hint not in record.handled_types):
        return hint
    return TYPE_IGNORE


def slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def build_label(task: Task, task_type: str, label_cfg: Mapping) -> str:
    """Derive the session label; only this slug ever reaches a command line."""
    gid = re.sub(r"[^a-z0-9]", "", task.task_id.lower())
    length = int(label_cfg["gid_suffix_length"])
    rendered = str(label_cfg["template"]).format(
        gid=gid,
        gid_suffix=gid[-length:],
        slug=slug(task.title),
        type=task_type,
    )
    return identity.normalize_label(rendered)

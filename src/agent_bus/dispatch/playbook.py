"""Prompt templates (playbooks): loading, rendering and writing prompt files."""

from __future__ import annotations

import os
import re
import string
from collections.abc import Mapping, Sequence
from importlib import resources
from pathlib import Path

from .model import clean_untrusted
from .store import TaskRecord

PLACEHOLDERS = (
    "task_url",
    "task_gid",
    "task_title",
    "task_type",
    "source",
    "project_name",
    "project_gid",
    "section_names",
    "due_on",
    "modified_at",
    "newest_comment_at",
    "repo",
    "label",
    "handoff_channel",
    "overlap",
    "head_sha",
    "base_ref",
)

_NAME = re.compile(r"[a-z0-9][a-z0-9-]*")


class PlaybookError(Exception):
    """A playbook template is missing, invalid or cannot be rendered."""


def load_template(name: str, *, templates_dir: str | None) -> str:
    if not _NAME.fullmatch(name):
        raise PlaybookError(f"invalid playbook name {name!r}: use [a-z0-9][a-z0-9-]*")
    private = None
    if templates_dir:
        private = Path(templates_dir).expanduser() / f"{name}.md"
        if private.is_file():
            return private.read_text(encoding="utf-8")
    packaged = resources.files("agent_bus.dispatch").joinpath("templates", f"{name}.md")
    if packaged.is_file():
        return packaged.read_text(encoding="utf-8")
    raise PlaybookError(
        f"playbook {name!r} not found in templates_dir ({private or 'not set'}) "
        f"nor in the packaged templates ({name}.md)"
    )


def render(
    name: str, values: Mapping[str, str], *, templates_dir: str | None,
    placeholders: Sequence[str] = PLACEHOLDERS,
) -> str:
    text = load_template(name, templates_dir=templates_dir)
    try:
        result = string.Template(text).substitute(values)
    except (KeyError, ValueError) as exc:
        raise PlaybookError(
            f"playbook {name!r} cannot be rendered ({exc!r}); "
            f"placeholders: {', '.join(placeholders)}"
        ) from exc
    if not result.strip():
        raise PlaybookError(f"playbook {name!r} rendered empty")
    if result.startswith("-"):
        raise PlaybookError(f"playbook {name!r} renders to text starting with '-'")
    return result


def overlap_text(records: Sequence[TaskRecord], *, limit: int, max_chars: int) -> str:
    """One bullet per record, every tracker-derived field cleaned like the
    other prompt values so none can forge the untrusted-data markers."""
    def clean(value: str | None) -> str:
        return clean_untrusted(value or "-", max_chars)

    lines = [
        f"- {clean(r.title)} ({clean(r.url)}) "
        f"[{clean(r.type)}, {clean(r.outcome)}, {clean(r.session_name)}]"
        for r in records[: max(limit, 0)]
    ]
    return "\n".join(lines) if lines else "(none)"


def write_prompt(text: str, *, label: str, prompts_dir: Path) -> Path:
    prompts_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = prompts_dir / f"{label}.md"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(path, 0o600)
    return path

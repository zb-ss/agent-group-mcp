"""Validation and small pure helpers for the `dispatch` config section.

Everything tunable lives under `dispatch` in config.json; this module checks
the merged value at load time (so a typo fails before a timer run does) and
holds the helpers that read it, so other modules never poke at raw keys.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import identity, paths
from ..config import (
    ConfigError,
    _check_section,
    _is_int,
    _is_non_negative_number,
    _is_number,
    _is_str,
    _is_str_list,
    _is_str_map,
)

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
ACTIONS = ("spawn", "notify", "ignore")
TYPE_IGNORE = "ignore"
PLAYBOOK_NAME = re.compile(r"[a-z0-9][a-z0-9-]*")
TIMER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

_SAMPLE_GID = "1000000012345678"
_SAMPLE_SUFFIX = "12345678"
_SAMPLE_SLUG = "sample-title"


def _is_bool(value: Any) -> bool:
    return isinstance(value, bool)


def _is_obj(value: Any) -> bool:
    return isinstance(value, dict)


_TOP_SCHEMA = {
    "env_file": (_is_str, "a string or null", True),
    "max_spawns_per_run": (lambda v: _is_int(v) and v >= 0, "an integer >= 0", False),
    "soft_limit": (lambda v: _is_int(v) and v >= 0, "an integer >= 0 or null", True),
    "resume_max_transcript_mb": (_is_non_negative_number, "a number >= 0", False),
    "bus_sender": (lambda v: _is_str(v) and bool(v), "a non-empty string", False),
    "title_max_chars": (lambda v: _is_int(v) and v >= 0, "an integer >= 0", False),
    "overlap_max_items": (lambda v: _is_int(v) and v >= 0, "an integer >= 0", False),
    "fetch_timeout_seconds": (_is_number, "a number", False),
    "handoff_channel": (_is_str, "a string", False),
    "templates_dir": (_is_str, "a string or null", True),
    "prompts_dir": (_is_str, "a string or null", True),
    "notify_on": (_is_str_list, "a list of strings", False),
}
_DEFAULTS_SCHEMA = {
    "client": (_is_str, "a string", False),
    "model": (_is_str, "a string or null", True),
    "worktree": (_is_str, "a string or null", True),
    "fetch": (_is_bool, "a boolean", False),
    "clone": (_is_bool, "a boolean", False),
    "remote": (_is_bool, "a boolean", False),
}
_ASANA_SCHEMA = {
    "enabled": (_is_bool, "a boolean", False),
    "ignore_own_comments": (_is_bool, "a boolean", False),
    "comment_authors": (
        lambda v: _is_str_list(v) and all(a.isascii() and a.isdigit() for a in v),
        "a list of user gids (digit strings); empty allows any author",
        False,
    ),
    "token_env": (lambda v: _is_str(v) and bool(v), "a non-empty string", False),
    "workspace": (_is_str, "a string or null", True),
    "lookahead_days": (lambda v: _is_int(v) and v >= 0, "an integer >= 0", False),
    "page_size": (lambda v: _is_int(v) and 1 <= v <= 100, "an integer from 1 to 100", False),
    "max_pages": (lambda v: _is_int(v) and v >= 1, "an integer >= 1", False),
    "timeout_seconds": (_is_number, "a number", False),
    "max_retry_wait_seconds": (lambda v: _is_number(v) and v >= 0, "a number >= 0", False),
}
_PROJECT_SCHEMA = {
    "client": (_is_str, "a string", False),
    "model": (_is_str, "a string or null", True),
    "worktree": (_is_str, "a string or null", True),
    "fetch": (_is_bool, "a boolean", False),
    "clone": (_is_bool, "a boolean", False),
    "remote": (_is_bool, "a boolean", False),
}
_LOG_SCHEMA = {"file": (_is_str, "a string or null", True)}
_MACOS_SCHEMA = {
    "enabled": (_is_bool, "a boolean", False),
    "osascript_bin": (_is_str, "a string", False),
    "title": (_is_str, "a string", False),
    "timeout_seconds": (_is_number, "a number", False),
}
_EMAIL_SCHEMA = {
    "enabled": (_is_bool, "a boolean", False),
    "host": (_is_str, "a string or null", True),
    "port": (_is_int, "an integer", False),
    "username": (_is_str, "a string or null", True),
    "password_env": (_is_str, "a string or null", True),
    "from": (_is_str, "a string or null", True),
    "to": (_is_str_list, "a list of strings", False),
    "subject_prefix": (_is_str, "a string", False),
    "timeout_seconds": (_is_number, "a number", False),
}
_TIMER_SCHEMA = {
    "interval_minutes": (lambda v: _is_int(v) and 1 <= v <= 720, "an integer from 1 to 720", False),
    "environment": (_is_str_map, "an object of strings", False),
    "output_file": (_is_str, "a string or null", True),
    "launchctl_bin": (_is_str, "a string", False),
    "systemctl_bin": (_is_str, "a string", False),
    "timeout_seconds": (_is_number, "a number", False),
}


def parse_hhmm(value: str) -> tuple[int, int]:
    """`09:30` -> (9, 30). ValueError on anything else."""
    if not isinstance(value, str):
        raise ValueError(f"{value!r} is not an HH:MM time")
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", value)
    if not match:
        raise ValueError(f"{value!r} is not an HH:MM time")
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        raise ValueError(f"{value!r} is not an HH:MM time")
    return hour, minute


def _require(cond: bool, key: str, wanted: str) -> None:
    if not cond:
        raise ConfigError(f"dispatch.{key} must be {wanted}")


def _check_working_hours(section: Any) -> None:
    _require(_is_obj(section), "working_hours", "an object")
    days = section.get("days")
    _require(
        _is_str_list(days) and all(d in DAYS for d in days),
        "working_hours.days",
        f"a list of {', '.join(DAYS)}",
    )
    parsed = {}
    for key in ("start", "end"):
        try:
            parsed[key] = parse_hhmm(section.get(key))
        except ValueError:
            raise ConfigError(f"dispatch.working_hours.{key} must be an HH:MM time") from None
    _require(
        parsed["start"] <= parsed["end"],
        "working_hours.start",
        "not later than working_hours.end",
    )


def _check_label(section: Any) -> None:
    _require(_is_obj(section), "label", "an object")
    template = section.get("template")
    _require(_is_str(template), "label.template", "a string")
    length = section.get("gid_suffix_length")
    _require(_is_int(length) and 1 <= length <= 16, "label.gid_suffix_length", "an integer from 1 to 16")
    suffix = _SAMPLE_SUFFIX[-length:]
    placeholders = "{gid}, {gid_suffix}, {slug} and {type}"
    try:
        rendered = template.format(
            gid=_SAMPLE_GID, gid_suffix=suffix, slug=_SAMPLE_SLUG, type="refinement"
        )
        label = identity.normalize_label(rendered)
    except (KeyError, IndexError, ValueError) as exc:
        # InvalidNameError is a ValueError, so it lands here too.
        raise ConfigError(
            f"dispatch.label.template must render a valid session label from the "
            f"placeholders {placeholders} ({type(exc).__name__}: {exc})"
        ) from exc
    if suffix not in label:
        raise ConfigError(
            f"dispatch.label.template must keep the task id suffix {{gid_suffix}} "
            f"(or {{gid}}) inside the label; placeholders: {placeholders}"
        )


def _check_handoff_channel(template: str) -> None:
    try:
        template.format(label="label", repo="repo", gid=_SAMPLE_GID, type="refinement")
    except (KeyError, IndexError, ValueError) as exc:
        raise ConfigError(
            f"dispatch.handoff_channel may only use the placeholders {{label}}, "
            f"{{repo}}, {{gid}} and {{type}} ({type(exc).__name__}: {exc})"
        ) from exc


def _check_worktree(value: Any, key: str) -> None:
    if isinstance(value, str) and value.startswith("-"):
        raise ConfigError(f"dispatch.{key} must not start with '-'")


def _check_defaults(section: Any) -> None:
    _check_section(section, _DEFAULTS_SCHEMA, "dispatch.defaults")
    _check_worktree(section.get("worktree"), "defaults.worktree")


def _check_types(section: Any) -> None:
    _require(_is_obj(section), "types", "an object")
    for name, entry in section.items():
        key = f"types.{name}"
        _require(_is_obj(entry), key, "an object")
        _require(entry.get("action") in ACTIONS, f"{key}.action", f"one of {', '.join(ACTIONS)}")
        playbook = entry.get("playbook")
        _require(
            _is_str(playbook) and PLAYBOOK_NAME.fullmatch(playbook) is not None,
            f"{key}.playbook",
            "a playbook name (lowercase letters, digits and dashes)",
        )
        resume = entry.get("resume_playbook")
        _require(
            resume is None or (_is_str(resume) and PLAYBOOK_NAME.fullmatch(resume) is not None),
            f"{key}.resume_playbook",
            "null or a playbook name (lowercase letters, digits and dashes)",
        )


def _check_worktree_cleanup(section: Any) -> None:
    _require(_is_obj(section), "worktree_cleanup", "an object")
    _require(_is_bool(section.get("enabled")), "worktree_cleanup.enabled", "a boolean")


def _check_patterns(value: Any, key: str) -> None:
    _require(_is_str_list(value), key, "a list of regular expressions")
    for pattern in value:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ConfigError(f"dispatch.{key} has an invalid regular expression ({exc})") from exc


RULE_KEYS = frozenset({"type", "section", "title", "project", "handled_as", "new_comment"})
NOTIFY_EVENTS = (
    "waiting", "done", "ended", "spawn_failed", "notify", "needs_approval", "worktree_removed",
)


def _check_notify_on(value: Any) -> None:
    for event in value:
        _require(event in NOTIFY_EVENTS, "notify_on", f"a list of: {', '.join(NOTIFY_EVENTS)}")


def _check_rules(rules: Any, types: Mapping) -> None:
    _require(isinstance(rules, list), "rules", "a list")
    for index, rule in enumerate(rules):
        key = f"rules[{index}]"
        _require(_is_obj(rule), key, "an object")
        _require(
            rule.get("type") in types or rule.get("type") == TYPE_IGNORE,
            f"{key}.type",
            "a key of dispatch.types or 'ignore'",
        )
        unknown = sorted(set(rule) - RULE_KEYS)
        _require(not unknown, key, f"free of unknown keys ({', '.join(unknown)}); "
                 f"allowed: {', '.join(sorted(RULE_KEYS))}")
        if "project" in rule:
            _require(
                _is_str_list(rule["project"]) and bool(rule["project"])
                and all(p.strip() for p in rule["project"]),
                f"{key}.project", "a non-empty list of non-empty project gids",
            )
        for pattern_key in ("section", "title"):
            if pattern_key in rule:
                _check_patterns(rule[pattern_key], f"{key}.{pattern_key}")
        if "handled_as" in rule:
            _require(_is_str_list(rule["handled_as"]), f"{key}.handled_as", "a list of strings")
            for handled in rule["handled_as"]:
                _require(handled in types, f"{key}.handled_as", "keys of dispatch.types")
        if "new_comment" in rule:
            _require(_is_bool(rule["new_comment"]), f"{key}.new_comment", "a boolean")


def _check_project(project_id: str, entry: Any) -> None:
    key = f"sources.asana.projects.{project_id}"
    if isinstance(entry, str):
        _require(bool(entry), key, "a non-empty repo string or an object")
        return
    _require(_is_obj(entry), key, "a non-empty repo string or an object")
    _require(_is_str(entry.get("repo")) and bool(entry["repo"]), f"{key}.repo", "a non-empty string")
    _check_section(entry, _PROJECT_SCHEMA, f"dispatch.{key}")
    _check_worktree(entry.get("worktree"), f"{key}.worktree")
    if "playbooks" not in entry:
        return
    playbooks = entry["playbooks"]
    _require(_is_str_map(playbooks), f"{key}.playbooks", "an object of playbook names")
    for task_type, name in playbooks.items():
        _require(
            PLAYBOOK_NAME.fullmatch(name) is not None,
            f"{key}.playbooks.{task_type}",
            "a playbook name (lowercase letters, digits and dashes)",
        )


def _check_asana(section: Any) -> None:
    _check_section(section, _ASANA_SCHEMA, "dispatch.sources.asana")
    base_url = section.get("base_url")
    _require(
        _is_str(base_url) and base_url.startswith("https://"),
        "sources.asana.base_url",
        "a string starting with https://",
    )
    projects = section.get("projects")
    _require(_is_obj(projects), "sources.asana.projects", "an object")
    for project_id, entry in projects.items():
        _check_project(project_id, entry)


def _check_sources(section: Any) -> None:
    _require(_is_obj(section), "sources", "an object")
    if "asana" in section:
        _check_asana(section["asana"])


def _check_email(section: Any) -> None:
    _check_section(section, _EMAIL_SCHEMA, "dispatch.notifiers.email")
    _require(
        section.get("security") in ("tls", "starttls"),
        "notifiers.email.security",
        "'tls' or 'starttls'",
    )
    if not section.get("enabled"):
        return
    _require(bool(section.get("host")), "notifiers.email.host", "set when email is enabled")
    _require(bool(section.get("from")), "notifiers.email.from", "set when email is enabled")
    _require(bool(section.get("to")), "notifiers.email.to", "non-empty when email is enabled")
    _require(
        not section.get("username") or bool(section.get("password_env")),
        "notifiers.email.password_env", "set when notifiers.email.username is set",
    )
    _require(
        not section.get("password_env") or bool(section.get("username")),
        "notifiers.email.username", "set when notifiers.email.password_env is set",
    )


def _check_notifiers(section: Any) -> None:
    _require(_is_obj(section), "notifiers", "an object")
    if "log" in section:
        _check_section(section["log"], _LOG_SCHEMA, "dispatch.notifiers.log")
    if "macos" in section:
        _check_section(section["macos"], _MACOS_SCHEMA, "dispatch.notifiers.macos")
    if "email" in section:
        _check_email(section["email"])


def _check_timer(section: Any) -> None:
    _check_section(section, _TIMER_SCHEMA, "dispatch.timer")
    name = section.get("name")
    _require(
        _is_str(name) and TIMER_NAME.fullmatch(name) is not None,
        "timer.name",
        "letters, digits, dots, dashes and underscores only",
    )
    command = section.get("command")
    _require(
        _is_str_list(command) and bool(command),
        "timer.command",
        "a non-empty list of strings",
    )
    for part in command:
        try:
            part.format(agent_bus_bin="x")
        except (KeyError, IndexError, ValueError) as exc:
            raise ConfigError(
                f"dispatch.timer.command may only use the placeholder {{agent_bus_bin}} "
                f"({type(exc).__name__}: {exc})"
            ) from exc


def validate(section: Any) -> None:
    """Raise ConfigError naming the dotted key of the first bad value."""
    _check_section(section, _TOP_SCHEMA, "dispatch")
    _check_notify_on(section["notify_on"])
    _check_worktree_cleanup(section.get("worktree_cleanup"))
    _check_working_hours(section.get("working_hours"))
    _check_label(section.get("label"))
    _check_handoff_channel(section["handoff_channel"])
    _check_defaults(section.get("defaults"))
    _check_types(section.get("types"))
    _check_rules(section.get("rules"), section["types"])
    _check_sources(section.get("sources"))
    _check_notifiers(section.get("notifiers"))
    _check_timer(section.get("timer"))


def within_working_hours(working_hours: Mapping, local_now: datetime) -> bool:
    if DAYS[local_now.weekday()] not in working_hours["days"]:
        return False
    start = parse_hhmm(working_hours["start"])
    end = parse_hhmm(working_hours["end"])
    return start <= (local_now.hour, local_now.minute) <= end


def project_settings(dispatch_cfg: Mapping, source: str, project_id: str) -> dict | None:
    """The settings for one mapped project: `dispatch.defaults` overlaid by
    the project entry. None when the project is not configured."""
    projects = dispatch_cfg["sources"].get(source, {}).get("projects", {})
    if project_id not in projects:
        return None
    entry = projects[project_id]
    if isinstance(entry, str):
        entry = {"repo": entry}
    merged = {**copy.deepcopy(dict(dispatch_cfg["defaults"])), **copy.deepcopy(entry)}
    merged.setdefault("playbooks", {})
    return merged


def playbook_name(dispatch_cfg: Mapping, project: Mapping, task_type: str) -> str:
    override = project.get("playbooks", {}).get(task_type)
    return override or dispatch_cfg["types"][task_type]["playbook"]


def resume_playbook(dispatch_cfg: Mapping, task_type: str) -> str | None:
    return dispatch_cfg["types"].get(task_type, {}).get("resume_playbook") or None


def type_action(dispatch_cfg: Mapping, task_type: str) -> str:
    entry = dispatch_cfg["types"].get(task_type)
    return entry["action"] if entry else TYPE_IGNORE


def dispatch_db_path() -> Path:
    return paths.db_path().with_name("dispatch.db")


def lock_path() -> Path:
    return paths.db_path().with_name("dispatch.lock")


def _configured_or(value: str | None, fallback: str) -> Path:
    if value:
        return Path(value).expanduser()
    return paths.db_path().with_name(fallback)


def log_path(dispatch_cfg: Mapping) -> Path:
    return _configured_or(dispatch_cfg["notifiers"]["log"].get("file"), "dispatch.log")


def prompts_dir(dispatch_cfg: Mapping) -> Path:
    return _configured_or(dispatch_cfg.get("prompts_dir"), "dispatch-prompts")


def timer_output_path(dispatch_cfg: Mapping) -> Path:
    return _configured_or(dispatch_cfg["timer"].get("output_file"), "dispatch-timer.log")

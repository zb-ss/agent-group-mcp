"""Validation and small pure helpers for the `dispatch` config section.

Everything tunable lives under `dispatch` in config.json; this module checks
the merged value at load time (so a typo fails before a timer run does) and
holds the helpers that read it, so other modules never poke at raw keys.
"""

from __future__ import annotations

import copy
import fnmatch
import re
import urllib.parse
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import identity, paths, storage
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

DECISIONS = ("start", "resume", "skip", "ask")
DECISION_MODES = ("shadow", "active")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
ACTIONS = ("spawn", "notify", "ignore")
TYPE_IGNORE = "ignore"
PLAYBOOK_NAME = re.compile(r"[a-z0-9][a-z0-9-]*")
TIMER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
MESSAGE_ACTIONS = ("ignore", "notify", "draft", "respond")
MESSAGE_SOURCE_TYPES = ("slack", "maildir")
MENTION_KINDS = ("owner", "channel", "here")
THREAD_KINDS = ("any", "top", "reply")
SCHEDULE_HOURS = ("working", "always")
SOURCE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")
SLACK_CHANNEL_ID = re.compile(r"[A-Z0-9]{2,}")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

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
    "schedule_tolerance_seconds": (_is_non_negative_number, "a number >= 0", False),
    "message_retention_days": (lambda v: _is_int(v) and v >= 1, "an integer >= 1", False),
}
_DRAFTS_SCHEMA = {
    "sending_stale_minutes": (lambda v: _is_int(v) and v >= 1, "an integer >= 1", False),
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
    "unmapped_repos": (
        lambda v: _is_str_list(v) and all(p.strip() for p in v),
        "a list of non-empty repo id patterns",
        False,
    ),
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
    "always": (_is_bool, "a boolean", False),
}


def _is_nonempty_str(value: Any) -> bool:
    return _is_str(value) and bool(value)


def _is_nonneg_int(value: Any) -> bool:
    return _is_int(value) and value >= 0


_SLACK_SCHEMA = {
    "enabled": (_is_bool, "a boolean", False),
    "base_url": (lambda v: _is_str(v) and v.startswith("https://"), "a string starting with https://", False),
    "token_env": (_is_nonempty_str, "a non-empty string", False),
    "owner_id": (_is_str, "a string or null", True),
    "timeout_seconds": (_is_non_negative_number, "a number >= 0", False),
    "max_retry_wait_seconds": (_is_non_negative_number, "a number >= 0", False),
    "min_interval_seconds": (_is_non_negative_number, "a number >= 0", False),
    "max_calls_per_run": (lambda v: _is_int(v) and v >= 1, "an integer >= 1", False),
    "page_size": (lambda v: _is_int(v) and 1 <= v <= 1000, "an integer from 1 to 1000", False),
    "max_pages": (lambda v: _is_int(v) and v >= 1, "an integer >= 1", False),
    "initial_lookback_minutes": (_is_non_negative_number, "a number >= 0", False),
    "thread_lookback_hours": (_is_non_negative_number, "a number >= 0", False),
    "max_text_chars": (lambda v: _is_int(v) and v >= 1, "an integer >= 1", False),
}
_SLACK_MENTIONS_SCHEMA = {
    "enabled": (_is_bool, "a boolean", False),
    "use_search": (_is_bool, "a boolean", False),
    "include_direct_messages": (_is_bool, "a boolean", False),
    "exclude_channels": (_is_str_list, "a list of strings", False),
    "conversations_refresh_minutes": (_is_non_negative_number, "a number >= 0", False),
}
_MAILDIR_SCHEMA = {
    "enabled": (_is_bool, "a boolean", False),
    "root": (_is_str, "a string or null", True),
    "address": (_is_str, "a string or null", True),
    "skip_bulk": (_is_bool, "a boolean", False),
    "initial_lookback_minutes": (_is_non_negative_number, "a number >= 0", False),
    "max_items_per_run": (lambda v: _is_int(v) and v >= 1, "an integer >= 1", False),
    "max_body_chars": (lambda v: _is_int(v) and v >= 1, "an integer >= 1", False),
}
_SCANNER_SCHEMA = {
    "enabled": (_is_bool, "a boolean", False),
    "batch_size": (lambda v: _is_int(v) and v >= 1, "an integer >= 1", False),
    "max_batches_per_run": (lambda v: _is_int(v) and v >= 1, "an integer >= 1", False),
    "max_item_chars": (lambda v: _is_int(v) and v >= 1, "an integer >= 1", False),
    "max_draft_chars": (lambda v: _is_int(v) and v >= 1, "an integer >= 1", False),
    "max_question_chars": (lambda v: _is_int(v) and v >= 1, "an integer >= 1", False),
    "timeout_seconds": (lambda v: _is_number(v) and v > 0, "a number > 0", False),
    "dir": (_is_str, "a string or null", True),
}
_INCIDENT_SCHEMA = {
    "remote": (_is_bool, "a boolean", False),
    "max_spawns_per_hour": (_is_nonneg_int, "an integer >= 0", False),
    "max_running": (_is_nonneg_int, "an integer >= 0", False),
    "max_open_hours": (lambda v: _is_number(v) and v > 0, "a number > 0", False),
    "max_alert_chars": (lambda v: _is_int(v) and v >= 1, "an integer >= 1", False),
    "max_report_chars": (lambda v: _is_int(v) and v >= 1, "an integer >= 1", False),
    "max_key_chars": (lambda v: _is_int(v) and v >= 1, "an integer >= 1", False),
    "max_name_chars": (lambda v: _is_int(v) and v >= 1, "an integer >= 1", False),
    "max_time_chars": (lambda v: _is_int(v) and v >= 1, "an integer >= 1", False),
    "max_url_chars": (lambda v: _is_int(v) and v >= 1, "an integer >= 1", False),
    "tools_hint": (_is_nonempty_str, "a non-empty string", False),
    "dir": (_is_str, "a string or null", True),
}
_SLACK_RULE_KEYS = frozenset(
    {"name", "action", "bot_ids", "user_ids", "text", "mention", "thread", "playbook", "lifecycle",
     "auto_post_report"}
)
_MAILDIR_RULE_KEYS = frozenset({"name", "action", "from", "folder", "subject", "text"})
_LIFECYCLE_KEYS = frozenset({"open", "repeat", "close", "key"})


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


def _check_working_hours(section: Any, key: str = "working_hours") -> None:
    _require(_is_obj(section), key, "an object")
    days = section.get("days")
    _require(
        _is_str_list(days) and all(d in DAYS for d in days),
        f"{key}.days",
        f"a list of {', '.join(DAYS)}",
    )
    parsed = {}
    for part in ("start", "end"):
        try:
            parsed[part] = parse_hhmm(section.get(part))
        except ValueError:
            raise ConfigError(f"dispatch.{key}.{part} must be an HH:MM time") from None
    _require(
        parsed["start"] <= parsed["end"],
        f"{key}.start",
        f"not later than {key}.end",
    )


def _check_schedule(value: Any, key: str) -> None:
    if value is None:
        return
    _require(_is_obj(value), key, "null or an object")
    interval = value.get("interval_minutes")
    _require(
        _is_int(interval) and 1 <= interval <= 1440,
        f"{key}.interval_minutes",
        "an integer from 1 to 1440",
    )
    hours = value.get("hours")
    if _is_obj(hours):
        _check_working_hours(hours, f"{key}.hours")
        return
    _require(hours in SCHEDULE_HOURS, f"{key}.hours", f"one of {', '.join(SCHEDULE_HOURS)} or a working-hours object")


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
        repos = entry.get("repos")
        _require(
            repos is None or (_is_str_list(repos) and all(p.strip() for p in repos)),
            f"{key}.repos",
            "null or a list of non-empty repo id patterns",
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
    "message", "draft_ready", "needs_owner", "incident_opened", "incident_closed", "incident_report",
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


def _format_only(template: str, key: str, **allowed: str) -> None:
    names = ", ".join("{" + n + "}" for n in allowed)
    try:
        template.format(**allowed)
    except (KeyError, IndexError, ValueError) as exc:
        raise ConfigError(
            f"dispatch.{key} may only use the placeholders {names} ({type(exc).__name__}: {exc})"
        ) from exc


def _is_relative_path(value: Any) -> bool:
    if not (_is_nonempty_str(value) and not value.startswith(("/", "\\"))):
        return False
    parts = re.split(r"[\\/]", value)
    return ".." not in parts and not re.match(r"^[A-Za-z]:", value)


def _check_message_rules(rules: Any, key: str, kind: str) -> None:
    _require(isinstance(rules, list), key, "a list")
    allowed = _SLACK_RULE_KEYS if kind == "slack" else _MAILDIR_RULE_KEYS
    seen: set[str] = set()
    for index, rule in enumerate(rules):
        rkey = f"{key}[{index}]"
        _require(_is_obj(rule), rkey, "an object")
        unknown = sorted(set(rule) - allowed)
        _require(not unknown, rkey, f"free of unknown keys ({', '.join(unknown)}); allowed: {', '.join(sorted(allowed))}")
        name = rule.get("name")
        _require(
            _is_str(name) and PLAYBOOK_NAME.fullmatch(name) is not None,
            f"{rkey}.name",
            "a rule name (lowercase letters, digits and dashes)",
        )
        _require(name not in seen, f"{rkey}.name", "unique within the list")
        seen.add(name)
        action = rule.get("action")
        _require(action in MESSAGE_ACTIONS, f"{rkey}.action", f"one of {', '.join(MESSAGE_ACTIONS)}")
        _require(
            not (kind == "maildir" and action == "respond"),
            f"{rkey}.action",
            "ignore, notify or draft: email never starts a session",
        )
        _check_message_rule_matchers(rule, rkey, kind)
        if kind == "slack":
            _check_slack_rule_extras(rule, rkey, action)


def _check_message_rule_matchers(rule: Mapping, rkey: str, kind: str) -> None:
    list_keys = ("bot_ids", "user_ids") if kind == "slack" else ("folder",)
    for name in list_keys:
        if name in rule:
            _require(
                _is_str_list(rule[name]) and all(rule[name]),
                f"{rkey}.{name}",
                "a list of non-empty strings",
            )
    for name in ("text",) if kind == "slack" else ("from", "subject", "text"):
        if name in rule:
            _check_patterns(rule[name], f"{rkey}.{name}")


def _check_slack_rule_extras(rule: Mapping, rkey: str, action: str) -> None:
    if "mention" in rule:
        _require(
            _is_str_list(rule["mention"]) and all(m in MENTION_KINDS for m in rule["mention"]),
            f"{rkey}.mention",
            f"a list drawn from {', '.join(MENTION_KINDS)}",
        )
    if "thread" in rule:
        _require(rule["thread"] in THREAD_KINDS, f"{rkey}.thread", f"one of {', '.join(THREAD_KINDS)}")
    playbook = rule.get("playbook")
    if action == "respond":
        _require(
            _is_str(playbook) and PLAYBOOK_NAME.fullmatch(playbook) is not None,
            f"{rkey}.playbook",
            "a playbook name (lowercase letters, digits and dashes), required for respond",
        )
    else:
        _require("playbook" not in rule, f"{rkey}.playbook", "set only with action respond")
    if "lifecycle" in rule:
        _require(action in ("notify", "respond"), f"{rkey}.lifecycle", "set only with action notify or respond")
        _check_lifecycle(rule["lifecycle"], f"{rkey}.lifecycle")
    if "auto_post_report" in rule:
        _require(_is_bool(rule["auto_post_report"]), f"{rkey}.auto_post_report", "a boolean")
        _require(action == "respond", f"{rkey}.auto_post_report", "set only with action respond")
    _check_rule_author(rule, rkey, action)


def _check_rule_author(rule: Mapping, rkey: str, action: str) -> None:
    """A rule that starts an incident session, or whose close pattern ends an
    incident, must name its author. Otherwise any channel member could start
    or stop sessions by posting the pattern. Open and close go through the
    same rule, so both must come from the same author."""
    closes = bool((rule.get("lifecycle") or {}).get("close"))
    if action != "respond" and not closes:
        return
    has_author = bool(rule.get("bot_ids")) or bool(rule.get("user_ids"))
    what = "start an incident session" if action == "respond" else "close an incident"
    _require(
        has_author,
        f"{rkey}.bot_ids",
        f"a non-empty list (or set user_ids): a rule that can {what} needs an author condition",
    )


def _check_lifecycle(life: Any, key: str) -> None:
    _require(_is_obj(life), key, "an object")
    unknown = sorted(set(life) - _LIFECYCLE_KEYS)
    _require(not unknown, key, f"free of unknown keys ({', '.join(unknown)})")
    for name in ("open", "repeat", "close"):
        if name in life:
            _check_patterns(life[name], f"{key}.{name}")
    if life.get("key") is not None:
        pattern = life["key"]
        _require(_is_str(pattern), f"{key}.key", "null or one regular expression")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ConfigError(f"dispatch.{key}.key has an invalid regular expression ({exc})") from exc


def _check_slack(name: str, section: Any) -> None:
    prefix = f"sources.{name}"
    _check_section(section, _SLACK_SCHEMA, f"dispatch.{prefix}")
    _check_schedule(section.get("schedule"), f"{prefix}.schedule")
    mentions = section.get("mentions")
    _require(_is_obj(mentions), f"{prefix}.mentions", "an object")
    _check_section(mentions, _SLACK_MENTIONS_SCHEMA, f"dispatch.{prefix}.mentions")
    _require(
        mentions.get("action") in ("ignore", "notify", "draft"),
        f"{prefix}.mentions.action",
        "one of ignore, notify, draft",
    )
    query = mentions.get("search_query")
    _require(_is_str(query), f"{prefix}.mentions.search_query", "a string")
    _format_only(query, f"{prefix}.mentions.search_query", owner_id="U0000000001")
    types = mentions.get("conversation_types")
    _require(
        _is_str_list(types) and bool(types), f"{prefix}.mentions.conversation_types", "a non-empty list of strings"
    )
    channels = section.get("channels")
    _require(_is_obj(channels), f"{prefix}.channels", "an object")
    for channel_id, entry in channels.items():
        ckey = f"{prefix}.channels.{channel_id}"
        _require(
            SLACK_CHANNEL_ID.fullmatch(channel_id) is not None,
            ckey,
            "keyed by channel ids (capital letters and digits)",
        )
        _require(_is_obj(entry), ckey, "an object")
        _check_message_rules(entry.get("rules", []), f"{ckey}.rules", "slack")


def _check_maildir(name: str, section: Any) -> None:
    prefix = f"sources.{name}"
    _check_section(section, _MAILDIR_SCHEMA, f"dispatch.{prefix}")
    _check_schedule(section.get("schedule"), f"{prefix}.schedule")
    folders = section.get("folders")
    _require(
        isinstance(folders, list) and bool(folders) and all(_is_relative_path(f) for f in folders),
        f"{prefix}.folders",
        "a non-empty list of relative folder names without '..'",
    )
    drafts = section.get("drafts_folder")
    _require(
        drafts is None or _is_relative_path(drafts),
        f"{prefix}.drafts_folder",
        "null or a relative folder name without '..'",
    )
    _require(
        section.get("default_action") in ("ignore", "notify", "draft"),
        f"{prefix}.default_action",
        "one of ignore, notify, draft",
    )
    link = section.get("link_template")
    if link is not None:
        _require(
            _is_str(link) and link.startswith("https://"),
            f"{prefix}.link_template",
            "null or a string starting with https://",
        )
        _format_only(link, f"{prefix}.link_template", message_id="id")
    _require(
        not section.get("enabled") or bool(section.get("root")),
        f"{prefix}.root",
        "set when the source is enabled",
    )
    _check_message_rules(section.get("rules"), f"{prefix}.rules", "maildir")


def _check_sources(section: Any) -> None:
    _require(_is_obj(section), "sources", "an object")
    if "asana" in section:
        _check_asana(section["asana"])
        _check_schedule(section["asana"].get("schedule"), "sources.asana.schedule")
    for name, entry in section.items():
        if name == "asana":
            continue
        _require(
            _is_str(name) and SOURCE_NAME.fullmatch(name) is not None,
            f"sources.{name}",
            "named with lowercase letters, digits, dashes and underscores",
        )
        _require(_is_obj(entry), f"sources.{name}", "an object")
        kind = source_type(name, entry)
        _require(kind != "asana", f"sources.{name}.type", "asana only under the key asana")
        merged = source_settings({"sources": section}, name)
        if kind == "slack":
            _check_slack(name, merged)
        elif kind == "maildir":
            _check_maildir(name, merged)


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


def _is_model_name(value: Any) -> bool:
    return value is None or (_is_str(value) and not value.startswith("-"))


_LABEL_WANTED = "a valid session label not starting with '-'"


def _is_valid_label(label: Any) -> bool:
    if not _is_str(label) or label.startswith("-"):
        return False
    try:
        identity.normalize_label(label)
    except ValueError:
        return False
    return True


def _check_dispatcher(section: Any) -> None:
    _require(_is_obj(section), "dispatcher", "an object")
    client = section.get("client")
    _require(
        _is_str(client) and identity.is_valid_client(client),
        "dispatcher.client",
        "a valid client name",
    )
    _require(_is_valid_label(section.get("label")), "dispatcher.label", _LABEL_WANTED)
    group = section.get("group")
    _require(
        _is_str(group) and bool(group) and identity.slugify(group) == group,
        "dispatcher.group",
        "a non-empty slug (lowercase letters, digits and dashes)",
    )
    _require(
        section.get("dir") is None or _is_str(section.get("dir")),
        "dispatcher.dir",
        "a string or null",
    )
    playbook = section.get("playbook")
    _require(
        _is_str(playbook) and PLAYBOOK_NAME.fullmatch(playbook) is not None,
        "dispatcher.playbook",
        "a playbook name (lowercase letters, digits and dashes)",
    )
    _require(
        _is_model_name(section.get("model")),
        "dispatcher.model",
        "a string not starting with '-' or null",
    )
    _require(_is_bool(section.get("remote")), "dispatcher.remote", "a boolean")
    _require(_is_bool(section.get("keep_alive")), "dispatcher.keep_alive", "a boolean")


def _check_triage(section: Any) -> None:
    _require(_is_obj(section), "triage", "an object")
    _require(_is_bool(section.get("enabled")), "triage.enabled", "a boolean")
    minutes = section.get("fallback_after_minutes")
    _require(
        minutes is None or (_is_number(minutes) and minutes > 0),
        "triage.fallback_after_minutes",
        "a number > 0 or null",
    )


def _check_routing_client(name: Any, entry: Any) -> None:
    key = f"routing.clients.{name}"
    _require(_is_str(name) and identity.is_valid_client(name), "routing.clients", "keyed by valid client names")
    _require(_is_obj(entry), key, "an object")
    _require(_is_number(entry.get("weight")), f"{key}.weight", "a number")
    types = entry.get("types")
    _require(
        _is_obj(types) and all(_is_number(v) for v in types.values()),
        f"{key}.types",
        "an object of numbers",
    )
    _require(_is_number(entry.get("per_complexity")), f"{key}.per_complexity", "a number")
    models = entry.get("models")
    _require(isinstance(models, list), f"{key}.models", "a list of {min_complexity, model} objects")
    for index, item in enumerate(models):
        item_key = f"{key}.models[{index}]"
        _require(_is_obj(item), item_key, "an object")
        minimum = item.get("min_complexity")
        _require(_is_int(minimum) and minimum >= 0, f"{item_key}.min_complexity", "an integer >= 0")
        _require(
            "model" in item and _is_model_name(item["model"]),
            f"{item_key}.model",
            "a string not starting with '-' or null",
        )


def _check_complexity(section: Any) -> None:
    _require(_is_obj(section), "routing.complexity", "an object")
    maximum = section.get("max")
    _require(
        _is_int(maximum) and 1 <= maximum <= 25, "routing.complexity.max", "an integer from 1 to 25"
    )
    signals = section.get("signals")
    _require(isinstance(signals, list), "routing.complexity.signals", "a list")
    for index, signal in enumerate(signals):
        key = f"routing.complexity.signals[{index}]"
        _require(_is_obj(signal), key, "an object")
        unknown = sorted(set(signal) - {"score", "type", "title", "section"})
        _require(not unknown, key, f"free of unknown keys ({', '.join(unknown)})")
        _require(_is_int(signal.get("score")), f"{key}.score", "an integer")
        if "type" in signal:
            _require(_is_str_list(signal["type"]), f"{key}.type", "a list of strings")
        for pattern_key in ("title", "section"):
            if pattern_key in signal:
                _check_patterns(signal[pattern_key], f"{key}.{pattern_key}")


def _check_overrides(overrides: Any) -> None:
    _require(isinstance(overrides, list), "routing.overrides", "a list")
    for index, item in enumerate(overrides):
        key = f"routing.overrides[{index}]"
        _require(_is_obj(item), key, "an object")
        unknown = sorted(set(item) - {"client", "repo", "type", "model"})
        _require(not unknown, key, f"free of unknown keys ({', '.join(unknown)})")
        client = item.get("client")
        _require(
            _is_str(client) and identity.is_valid_client(client),
            f"{key}.client",
            "a valid client name",
        )
        if "repo" in item:
            _require(_is_str(item["repo"]) and bool(item["repo"]), f"{key}.repo", "a non-empty string")
        if "type" in item:
            _require(_is_str(item["type"]), f"{key}.type", "a string")
        if "model" in item:
            _require(
                _is_model_name(item["model"]), f"{key}.model", "a string not starting with '-' or null"
            )


def _check_routing(section: Any) -> None:
    _require(_is_obj(section), "routing", "an object")
    _require(_is_bool(section.get("enabled")), "routing.enabled", "a boolean")
    clients = section.get("clients")
    _require(_is_obj(clients), "routing.clients", "an object")
    for name, entry in clients.items():
        _check_routing_client(name, entry)
    _check_complexity(section.get("complexity"))
    _check_overrides(section.get("overrides"))


LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


def endpoint_problem(endpoint: str, allow_remote: bool) -> str | None:
    """Why a decision-model endpoint may not be used, or None when it may."""
    parts = urllib.parse.urlsplit(endpoint)
    host = (parts.hostname or "").lower()
    if host in LOCAL_HOSTS:
        return None
    if not allow_remote:
        return "a local host (localhost, 127.0.0.1 or ::1) unless allow_remote is true"
    if parts.scheme != "https":
        return "https:// for a host other than the local machine"
    return None


def _check_decision_model(section: Any) -> None:
    _require(_is_obj(section), "decision_model", "an object")
    _require(_is_bool(section.get("enabled")), "decision_model.enabled", "a boolean")
    _require(
        section.get("mode") in DECISION_MODES,
        "decision_model.mode",
        f"one of {', '.join(DECISION_MODES)}",
    )
    endpoint = section.get("endpoint")
    _require(
        _is_str(endpoint) and endpoint.startswith(("http://", "https://")),
        "decision_model.endpoint",
        "a string starting with http:// or https://",
    )
    allow_remote = section.get("allow_remote")
    _require(_is_bool(allow_remote), "decision_model.allow_remote", "a boolean")
    problem = endpoint_problem(endpoint, allow_remote)
    _require(problem is None, "decision_model.endpoint", problem or "")
    model = section.get("model")
    _require(model is None or _is_str(model), "decision_model.model", "a string or null")
    _require(
        not section["enabled"] or bool(model),
        "decision_model.model",
        "set when decision_model is enabled",
    )
    timeout = section.get("timeout_seconds")
    _require(_is_number(timeout) and timeout > 0, "decision_model.timeout_seconds", "a number > 0")
    chars = section.get("max_state_chars")
    _require(_is_int(chars) and chars >= 1, "decision_model.max_state_chars", "an integer >= 1")


def _check_scanner_client(name: Any, entry: Any) -> None:
    key = f"scanner.clients.{name}"
    _require(_is_obj(entry), key, "an object")
    command = entry.get("command")
    _require(_is_str_list(command) and bool(command), f"{key}.command", "a non-empty list of strings")
    for part in command:
        _format_only(part, f"{key}.command", prompt_file="x", instruction="x", model="x")
    _require(
        any("{prompt_file}" in part for part in command),
        f"{key}.command",
        "containing {prompt_file} in some element",
    )
    _require(_is_str(entry.get("instruction")), f"{key}.instruction", "a string")
    pass_env = entry.get("pass_env")
    _require(
        _is_str_list(pass_env)
        and all(ENV_NAME.fullmatch(n) and not n.startswith("AGENT_BUS_") for n in pass_env),
        f"{key}.pass_env",
        "a list of environment variable names not starting with AGENT_BUS_",
    )
    for field in ("env", "model_env"):
        _require(entry.get(field) is None or _is_obj(entry[field]), f"{key}.{field}", "an object or null")


def _check_scanner(section: Any) -> None:
    _check_section(section, _SCANNER_SCHEMA, "dispatch.scanner")
    clients = section.get("clients")
    _require(_is_obj(clients), "scanner.clients", "an object")
    _require(section.get("client") in clients, "scanner.client", "a key of dispatch.scanner.clients")
    _require(_is_model_name(section.get("model")), "scanner.model", "a string not starting with '-' or null")
    playbook = section.get("playbook")
    _require(
        _is_str(playbook) and PLAYBOOK_NAME.fullmatch(playbook) is not None,
        "scanner.playbook",
        "a playbook name (lowercase letters, digits and dashes)",
    )
    for name, entry in clients.items():
        _check_scanner_client(name, entry)


def _check_incident(section: Any) -> None:
    _check_section(section, _INCIDENT_SCHEMA, "dispatch.incident")
    client = section.get("client")
    _require(_is_str(client) and identity.is_valid_client(client), "incident.client", "a valid client name")
    _require(
        _is_model_name(section.get("model")), "incident.model", "a string not starting with '-' or null"
    )
    group = section.get("group")
    _require(
        _is_str(group) and bool(group) and identity.slugify(group) == group,
        "incident.group",
        "a non-empty slug (lowercase letters, digits and dashes)",
    )
    _require(_is_valid_label(section.get("label_prefix")), "incident.label_prefix", _LABEL_WANTED)


def _check_drafts(section: Any) -> None:
    _require(_is_obj(section), "drafts", "an object")
    _check_section(section, _DRAFTS_SCHEMA, "dispatch.drafts")


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
    _check_dispatcher(section.get("dispatcher"))
    _check_triage(section.get("triage"))
    _check_routing(section.get("routing"))
    _check_decision_model(section.get("decision_model"))
    _check_scanner(section.get("scanner"))
    _check_incident(section.get("incident"))
    _check_drafts(section.get("drafts"))


def within_working_hours(working_hours: Mapping, local_now: datetime) -> bool:
    if DAYS[local_now.weekday()] not in working_hours["days"]:
        return False
    start = parse_hhmm(working_hours["start"])
    end = parse_hhmm(working_hours["end"])
    return start <= (local_now.hour, local_now.minute) <= end


def source_type(name: str, entry: Mapping) -> str:
    return entry.get("type") or name


def source_settings(dispatch_cfg: Mapping, name: str) -> dict:
    """The settings of one source instance. An instance whose name differs
    from its type starts from the packaged defaults of that type."""
    from ..config import _merge, defaults

    entry = copy.deepcopy(dict(dispatch_cfg["sources"][name]))
    kind = source_type(name, entry)
    if kind == name:
        return entry
    packaged = defaults()["dispatch"]["sources"].get(kind)
    if packaged is None:
        return entry
    return _merge(copy.deepcopy(packaged), entry)


def schedule_due(
    schedule: Mapping | None,
    *,
    working_hours: Mapping,
    last_run_at: str | None,
    now: datetime,
    local: datetime,
    tolerance_seconds: float,
) -> bool:
    """Whether a source with this schedule should run now."""
    if schedule is None:
        return within_working_hours(working_hours, local)
    hours = schedule["hours"]
    window = working_hours if hours == "working" else None if hours == "always" else hours
    if window is not None and not within_working_hours(window, local):
        return False
    if not last_run_at:
        return True
    try:
        last = datetime.strptime(last_run_at, storage.TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    needed = schedule["interval_minutes"] * 60 - tolerance_seconds
    return (now - last).total_seconds() >= needed


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


def default_project(dispatch_cfg: Mapping, repo: str) -> dict:
    """Settings for a repo that is not mapped to a project: the defaults."""
    merged = copy.deepcopy(dict(dispatch_cfg["defaults"]))
    merged["repo"] = repo
    merged["playbooks"] = {}
    return merged


def type_repos(dispatch_cfg: Mapping, task_type: str) -> list[str] | None:
    """The repo id patterns a type may spawn for; None when unrestricted."""
    repos = dispatch_cfg["types"].get(task_type, {}).get("repos")
    return None if repos is None else list(repos)


def repo_allowed(patterns: list[str] | None, repo_id: str | None) -> bool:
    if patterns is None:
        return True
    if repo_id is None:
        return False
    return any(fnmatch.fnmatchcase(repo_id, pattern) for pattern in patterns)


def unmapped_repos(dispatch_cfg: Mapping, source: str) -> list[str]:
    return list(dispatch_cfg["sources"].get(source, {}).get("unmapped_repos", []))


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


def dispatcher_dir(dispatch_cfg: Mapping) -> Path:
    return _configured_or(dispatch_cfg["dispatcher"].get("dir"), "dispatcher")


def incident_dir(dispatch_cfg: Mapping) -> Path:
    return _configured_or(dispatch_cfg["incident"].get("dir"), "incidents")


def scanner_dir(dispatch_cfg: Mapping) -> Path:
    return _configured_or(dispatch_cfg["scanner"].get("dir"), "dispatch-scanner")


def scan_lock_path() -> Path:
    return paths.db_path().with_name("dispatch-scan.lock")


def dispatcher_token_path() -> Path:
    return paths.db_path().with_name("dispatcher.token")

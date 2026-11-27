"""File-based configuration for agent dispatch (spawn, repo resolution).

`config.json` lives beside the bus database. Shipped defaults come from the
packaged `default_config.json`; the user file is deep-merged over them.

This differs from settings.py, which holds scalar env-var tunables: this
file carries structured data (per-client command templates, lists of
folders) that does not fit an environment variable.

Security: command templates run as the local user inside tmux, so this
file is as sensitive as a shell rc file. Keep it writable by you only.
"""

from __future__ import annotations

import copy
import json
import os
import re
import string
from importlib import resources
from pathlib import Path
from typing import Any

from .paths import db_path

CONFIG_FILENAME = "config.json"
DEFAULTS_RESOURCE = "default_config.json"


class ConfigError(ValueError):
    """The user config file cannot be used; the message names its path."""


def config_path() -> Path:
    """Lives alongside the bus.db so AGENT_BUS_DB relocates it in tests."""
    return db_path().parent / CONFIG_FILENAME


def defaults() -> dict[str, Any]:
    """Packaged defaults, parsed fresh so callers may mutate the result."""
    raw = resources.files("agent_bus").joinpath(DEFAULTS_RESOURCE).read_text(encoding="utf-8")
    return json.loads(raw)


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    # Only dicts merge; lists, scalars and null replace, so a user can
    # disable a template fragment with null or swap a whole list.
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = _merge(base[key], value)
        else:
            base[key] = copy.deepcopy(value)
    return base


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_non_negative_number(value: Any) -> bool:
    return _is_number(value) and value >= 0


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_str(value: Any) -> bool:
    return isinstance(value, str)


def _is_process_name(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 15 and "/" not in value


def _is_str_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _is_str_map(value: Any) -> bool:
    return isinstance(value, dict) and all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    )


# key -> (checker, description, nullable)
_REPOS_SCHEMA = {
    "remote": (_is_str, "a string", False),
    "host_aliases": (_is_str_map, "an object of strings", False),
    "roots": (_is_str_list, "a list of strings", False),
    "scan_depth": (_is_int, "an integer", False),
    "clone_root": (_is_str, "a string or null", True),
    "clone_url": (_is_str, "a string", False),
    "git_timeout_seconds": (_is_number, "a number", False),
    "id_recheck_seconds": (_is_number, "a number", False),
    "status_budget_seconds": (_is_non_negative_number, "a non-negative number", False),
    "clone_timeout_seconds": (_is_number, "a number", False),
}
_SPAWN_SCHEMA = {
    "max_sessions": (_is_int, "an integer", False),
    "startup_grace_seconds": (_is_number, "a number", False),
    "tmux_bin": (_is_str, "a string", False),
    "tmux_session": (_is_str, "a string", False),
    "tmux_timeout_seconds": (_is_number, "a number", False),
    "after_exit": (_is_str, "a string", False),
    "strip_env": (_is_str_list, "a list of strings", False),
    "worktree_root": (_is_str, "a string or null", True),
    "carry_over": (_is_str_list, "a list of strings", False),
}
_STATUS_SCHEMA = {
    "hide_done_after_seconds": (_is_non_negative_number, "a non-negative number", False),
}
_TEMPLATE_SCHEMA = {
    "command": (_is_str_list, "a list of strings", False),
    "model": (_is_str_list, "a list of strings or null", True),
    "remote": (_is_str_list, "a list of strings or null", True),
    "prompt": (_is_str_list, "a list of strings", False),
    "resume": (_is_str_list, "a list of strings or null", True),
    "resume_prompt": (_is_str_list, "a list of strings or null", True),
    "user_wired": (_is_str_list, "a list of strings or null", True),
    "transcripts": (_is_str, "a string or null", True),
    "prompt_delivery": (lambda v: v in ("argv", "paste"), '"argv" or "paste"', False),
    "prompt_ready_seconds": (_is_non_negative_number, "a non-negative number", False),
    "process_name": (_is_process_name, "a non-empty string of at most 15 characters without a slash, or null", True),
}
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _check_model_env(client: str, value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, dict) or not all(
        isinstance(k, str)
        and _ENV_NAME.fullmatch(k) is not None
        and isinstance(v, (str, dict, list))
        for k, v in value.items()
    ):
        raise ConfigError(
            f"spawn.clients.{client}.model_env must be an object of environment "
            f"variable names to a string, object or list"
        )


def _check_worktree_trust(client: str, value: Any) -> None:
    if value is None:
        return
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("enabled"), bool)
        or not all(isinstance(value.get(k), str) and value[k] for k in ("file", "key"))
    ):
        raise ConfigError(
            f"spawn.clients.{client}.worktree_trust must be an object with a boolean "
            f"enabled and non-empty string file and key"
        )


def _check_session_link(client: str, value: Any) -> None:
    if value is None:
        return
    key = f"spawn.clients.{client}.session_link"
    wanted = (
        f"{key} must be null or an object with an env variable name, an https:// url "
        f"with a literal host and exactly one {{id}} placeholder in its path or query, "
        f"and a label of 1 to 60 characters"
    )
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("env"), str)
        or _ENV_NAME.fullmatch(value["env"]) is None
        or not isinstance(value.get("url"), str)
        or not value["url"].startswith("https://")
        or not isinstance(value.get("label"), str)
        or not 1 <= len(value["label"]) <= 60
    ):
        raise ConfigError(wanted)
    _check_link_template(value["url"], wanted)


# The part of a session link template before its {id}: the scheme and a
# literal host (no user info), then a path or query. The id can never become
# part of the host.
_LINK_PREFIX = re.compile(r"https://[^/?#{}@\s]+[/?][^#{}]*")


def _check_link_template(url: str, wanted: str) -> None:
    from .links import is_web_url  # lazy: keep module import order simple

    try:
        fields = [
            (name, spec, conv)
            for _, name, spec, conv in string.Formatter().parse(url)
            if name is not None
        ]
    except ValueError as exc:
        raise ConfigError(f"{wanted} ({type(exc).__name__}: {exc})") from exc
    # Exactly one bare {id}: no other field, no conversion (!r), no format
    # spec, and no other brace (an escaped {{ or }} included).
    prefix, _, rest = url.partition("{id}")
    if fields != [("id", "", None)] or any(c in prefix + rest for c in "{}"):
        raise ConfigError(wanted)
    if _LINK_PREFIX.fullmatch(prefix) is None:
        raise ConfigError(wanted)
    if not is_web_url(url.replace("{id}", "sample-id_1")):
        raise ConfigError(wanted)


def _check_idle_prompt(client: str, value: Any) -> None:
    if value is None:
        return
    key = f"spawn.clients.{client}.idle_prompt"
    wanted = (
        f"{key} must be null or an object with a regular expression pattern, an "
        f"integer tail_lines from 1 to 200 and a boolean escapes"
    )
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("pattern"), str)
        or not _is_int(value.get("tail_lines"))
        or not 1 <= value["tail_lines"] <= 200
        or not isinstance(value.get("escapes"), bool)
    ):
        raise ConfigError(wanted)
    try:
        re.compile(value["pattern"])
    except re.error as exc:
        raise ConfigError(f"{key}.pattern is not a valid regular expression ({exc})") from exc


def _check_section(section: Any, schema: dict, prefix: str) -> None:
    if not isinstance(section, dict):
        raise ConfigError(f"{prefix} must be an object")
    for key, (check, wanted, nullable) in schema.items():
        if key not in section:
            continue
        value = section[key]
        if value is None and nullable:
            continue
        if not check(value):
            raise ConfigError(f"{prefix}.{key} must be {wanted}")


def _check_clone_url(template: str) -> None:
    """Format the template with sample values so a stray brace or an unknown
    placeholder fails at load time, not in the middle of a spawn."""
    try:
        template.format(host="host.example", path="owner/repo")
    except (KeyError, IndexError, ValueError) as exc:
        raise ConfigError(
            f"repos.clone_url may only use the placeholders {{host}} and {{path}} "
            f"({type(exc).__name__}: {exc})"
        ) from exc


def _validate(cfg: dict[str, Any]) -> None:
    _check_section(cfg.get("repos"), _REPOS_SCHEMA, "repos")
    _check_clone_url(cfg["repos"]["clone_url"])
    _check_section(cfg.get("status"), _STATUS_SCHEMA, "status")
    _check_section(cfg.get("spawn"), _SPAWN_SCHEMA, "spawn")
    clients = cfg["spawn"].get("clients")
    if not isinstance(clients, dict):
        raise ConfigError("spawn.clients must be an object")
    for client, template in clients.items():
        _check_section(template, _TEMPLATE_SCHEMA, f"spawn.clients.{client}")
        _check_model_env(client, template.get("model_env"))
        _check_worktree_trust(client, template.get("worktree_trust"))
        _check_session_link(client, template.get("session_link"))
        _check_idle_prompt(client, template.get("idle_prompt"))
        if template.get("worktree_links") is None:
            continue
        links = template["worktree_links"]
        if not isinstance(links, list) or not all(
            isinstance(e, dict)
            and isinstance(e.get("source"), str)
            and isinstance(e.get("target"), str)
            for e in links
        ):
            raise ConfigError(
                f"spawn.clients.{client}.worktree_links must be a list of "
                f'objects {{"source": "...", "target": "..."}} with string values'
            )
    from .dispatch import settings as dispatch_settings  # lazy: import cycle

    dispatch_settings.validate(cfg.get("dispatch"))


def load_user(path: Path | None = None) -> dict[str, Any]:
    """The raw user config object, without defaults. A missing or blank file
    gives {}; an unreadable, broken or non-object file raises."""
    p = path or config_path()
    if not p.exists():
        return {}
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config file {p}: {exc}") from exc
    if not raw.strip():
        return {}
    try:
        user = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config file {p} is not valid JSON: {exc}") from exc
    if not isinstance(user, dict):
        raise ConfigError(f"config file {p} must contain a JSON object")
    return user


def validate_user(user: dict[str, Any]) -> dict[str, Any]:
    """Defaults merged with `user`, validated. Raises ConfigError."""
    merged = _merge(defaults(), user)
    _validate(merged)
    return merged


def load(*, path: Path | None = None) -> dict[str, Any]:
    """Defaults merged with the user file. Missing or blank file gives the
    defaults. Unlike wake.json, a broken file raises: spawn must not run
    with a config the user believes is in effect but is not."""
    p = path or config_path()
    user = load_user(p)
    try:
        return validate_user(user)
    except ConfigError as exc:
        raise ConfigError(f"{exc} (config file {p})") from exc


def save_user(user: dict[str, Any], path: Path | None = None) -> Path:
    """Validate `user` merged over the defaults, then write it atomically.
    Nothing is written when validation fails. A symlinked config file is
    written through to its target; a new file is 0600, an existing one keeps
    its permission bits."""
    validate_user(user)
    target = Path(os.path.realpath(path or config_path()))
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        mode = target.stat().st_mode & 0o777
    except FileNotFoundError:
        mode = 0o600
    tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(user, indent=2) + "\n")
        os.chmod(tmp, mode)  # the umask may have narrowed the creation mode
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    return target

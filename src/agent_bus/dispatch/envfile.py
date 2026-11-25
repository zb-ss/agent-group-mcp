"""Optional owner-only env file: secrets for timer runs.

A launchd or systemd job has no login environment, so the token and mail
password can come from a file the owner keeps at mode 600. Keys already in
the process environment win. Error messages never contain a value.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from ..config import ConfigError


class EnvFileError(ConfigError):
    """The env file is missing, unreadable or readable by others."""


def _parse_value(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def load(path: str | None, *, base: Mapping[str, str]) -> dict[str, str]:
    env = dict(base)
    if path is None:
        return env
    file = Path(path).expanduser()
    try:
        mode = file.stat().st_mode
    except OSError as exc:
        raise EnvFileError(f"env file {file} cannot be read ({type(exc).__name__})") from exc
    if mode & 0o077:
        raise EnvFileError(f"env file {file} must not be readable by others: chmod 600 {file}")
    try:
        text = file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise EnvFileError(f"env file {file} cannot be read ({type(exc).__name__})") from exc
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key or key in base:
            continue
        env[key] = _parse_value(value)
    return env

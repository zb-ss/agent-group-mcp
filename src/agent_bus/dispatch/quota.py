"""Subscription quota as a routing input.

Reads only usage numbers the clients already keep on this machine: a snapshot
written by `agent-bus quota record` from a status line payload, and the newest
rate-limit event in the Codex session logs. No network, no credential files.
Everything here degrades to a factor of 1.0 (no effect) when data is missing,
stale or malformed.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .. import storage
from . import routing

MAX_WINDOWS = 8
MAX_SNAPSHOT_BYTES = 16384
DIR_MODE = 0o700
FILE_MODE = 0o600
_WINDOW_NAME = re.compile(r"[a-z0-9_]{1,40}")
_CLIENT_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")
_MARKER = "rate_limits"


@dataclass(frozen=True)
class Window:
    name: str
    used_percent: float
    resets_at: float | None  # epoch seconds


@dataclass(frozen=True)
class Reading:
    client: str
    at: datetime  # UTC
    windows: tuple[Window, ...]


def _percent(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number or not 0 <= number <= 100:
        return None
    return number


def _epoch(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if number == number and abs(number) < 1e12 else None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    return None


def _window(name: Any, raw: Any, percent_keys: tuple[str, ...]) -> Window | None:
    if not isinstance(name, str) or not _WINDOW_NAME.fullmatch(name):
        return None
    if not isinstance(raw, Mapping):
        return None
    used = None
    for key in percent_keys:
        used = _percent(raw.get(key))
        if used is not None:
            break
    if used is None:
        return None
    return Window(name, used, _epoch(raw.get("resets_at")))


def parse_statusline(payload: Any) -> tuple[Window, ...]:
    """The usage windows in a status line payload's `rate_limits`."""
    try:
        limits = payload.get("rate_limits") if isinstance(payload, Mapping) else None
        if not isinstance(limits, Mapping):
            return ()
        windows: list[Window] = []
        for name, raw in limits.items():
            window = _window(name, raw, ("used_percentage", "used_percent"))
            if window is not None:
                windows.append(window)
            if len(windows) >= MAX_WINDOWS:
                break
        return tuple(windows)
    except (TypeError, ValueError, AttributeError):
        return ()


def _utc(at: datetime) -> datetime:
    return at.replace(tzinfo=timezone.utc) if at.tzinfo is None else at.astimezone(timezone.utc)


def write_snapshot(
    directory: Path,
    client: str,
    windows: tuple[Window, ...],
    *,
    now: datetime,
    min_interval_seconds: float,
) -> bool:
    """Store `windows` for `client`; False when skipped (nothing to store, a
    snapshot younger than the minimum interval, or a bad client name)."""
    if not windows or not _CLIENT_NAME.fullmatch(client):
        return False
    directory = Path(directory)
    target = directory / f"{client}.json"
    try:
        age = _utc(now).timestamp() - target.stat().st_mtime
        if 0 <= age < min_interval_seconds:
            return False
    except OSError:
        pass
    directory.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    body = {
        "client": client,
        "recorded_at": _utc(now).strftime(storage.TIMESTAMP_FORMAT),
        "windows": {
            w.name: {"used_percent": w.used_percent, "resets_at": w.resets_at}
            for w in windows[:MAX_WINDOWS]
        },
    }
    tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(body, fh)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    return True


def _parse_recorded(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return _utc(datetime.strptime(value, storage.TIMESTAMP_FORMAT))
    except ValueError:
        return None


def read_snapshot(directory: Path, client: str) -> Reading | None:
    if not _CLIENT_NAME.fullmatch(client):
        return None
    path = Path(directory) / f"{client}.json"
    try:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            return None
        if info.st_size > MAX_SNAPSHOT_BYTES:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        at = _parse_recorded(data.get("recorded_at"))
        raw_windows = data.get("windows")
        if at is None or not isinstance(raw_windows, Mapping):
            return None
        windows = []
        for name, raw in raw_windows.items():
            window = _window(name, raw, ("used_percent",))
            if window is not None:
                windows.append(window)
            if len(windows) >= MAX_WINDOWS:
                break
        return Reading(client, at, tuple(windows)) if windows else None
    except (OSError, ValueError, AttributeError, TypeError):
        return None


def _newest(path: Path, count: int) -> list[Path]:
    try:
        entries = [p for p in path.iterdir() if p.name.isdigit() and p.is_dir()]
    except OSError:
        return []
    return sorted(entries, key=lambda p: p.name, reverse=True)[: max(count, 0)]


def _codex_files(root: Path, max_days: int, max_files: int) -> list[Path]:
    days: list[Path] = []
    for year in _newest(root, 10_000):
        for month in _newest(year, 12):
            for day in _newest(month, 31):
                days.append(day)
                if len(days) >= max_days:
                    break
            if len(days) >= max_days:
                break
        if len(days) >= max_days:
            break
    files: list[tuple[float, Path]] = []
    for day in days:
        try:
            for path in day.glob("rollout-*.jsonl"):
                info = os.lstat(path)
                if stat.S_ISREG(info.st_mode):
                    files.append((info.st_mtime, path))
        except OSError:
            continue
    files.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in files[: max(max_files, 0)]]


def _tail_lines(path: Path, max_bytes: int) -> list[str]:
    with open(path, "rb") as fh:
        size = os.fstat(fh.fileno()).st_size
        fh.seek(max(size - max_bytes, 0))
        data = fh.read(max_bytes)
    return data.decode("utf-8", errors="replace").splitlines()


def _codex_event(path: Path, max_bytes: int) -> Reading | None:
    try:
        lines = _tail_lines(path, max_bytes)
    except OSError:
        return None
    for line in reversed(lines):
        if _MARKER not in line:
            continue
        try:
            event = json.loads(line)
            limits = event["payload"]["rate_limits"]
            at = _parse_recorded_iso(event.get("timestamp"))
        except (ValueError, KeyError, TypeError):
            continue
        if at is None or not isinstance(limits, Mapping):
            continue
        windows = []
        for name in ("primary", "secondary"):
            window = _window(name, limits.get(name), ("used_percent",))
            if window is not None:
                windows.append(window)
        if windows:
            return Reading("codex", at, tuple(windows))
    return None


def _parse_recorded_iso(value: Any) -> datetime | None:
    epoch = _epoch(value) if isinstance(value, str) else None
    return datetime.fromtimestamp(epoch, timezone.utc) if epoch is not None else None


def read_codex(client_cfg: Mapping, *, home: Path | None = None) -> Reading | None:
    try:
        raw = str(client_cfg["sessions_dir"])
        root = Path(raw).expanduser() if home is None or not raw.startswith("~") else Path(home) / raw[2:]
        for path in _codex_files(
            root, int(client_cfg["max_days"]), int(client_cfg["max_files"])
        ):
            reading = _codex_event(path, int(client_cfg["max_tail_bytes"]))
            if reading is not None:
                return reading
    except (OSError, KeyError, TypeError, ValueError):
        return None
    return None


def factor(reading: Reading | None, quota_cfg: Mapping, *, now: datetime) -> float:
    """Multiplier in 0..1 for a client's routing score; 1.0 = no effect.

    A reading older than `max_age_minutes` no longer says how much is used,
    except for an exhausted window with a known reset: that still proves the
    client is out until `resets_at`, so it keeps counting until then."""
    if reading is None:
        return 1.0
    now_ts = _utc(now).timestamp()
    max_age = float(quota_cfg["max_age_minutes"]) * 60
    age = now_ts - reading.at.timestamp()
    if age < -max_age:
        return 1.0  # recorded in the future: a wrong clock, not a reading
    stale = age > max_age
    weight = float(quota_cfg["weight"])
    floor = float(quota_cfg["min_remaining_percent"])
    result = 1.0
    for window in reading.windows:
        if window.resets_at is not None and window.resets_at <= now_ts:
            continue
        used = min(max(window.used_percent, 0.0), 100.0)
        exhausted = 100.0 - used < floor
        if stale and not (exhausted and window.resets_at is not None):
            continue
        if exhausted:
            value = _exhausted_factor(window, quota_cfg, now_ts)
        else:
            value = 1.0 - weight * used / 100.0
        result = min(result, value)
    return min(max(result, 0.0), 1.0)


def _exhausted_factor(window: Window, quota_cfg: Mapping, now_ts: float) -> float:
    soon = float(quota_cfg["reset_soon_minutes"]) * 60
    if window.resets_at is not None and window.resets_at - now_ts <= soon:
        return float(quota_cfg["reset_soon_factor"])
    return float(quota_cfg["exhausted_factor"])


def _read(
    client: str, routing_cfg: Mapping, snapshot_dir: Path, home: Path | None
) -> tuple[str, Reading | None]:
    entry = (routing_cfg["quota"].get("clients") or {}).get(client)
    kind = entry.get("reader") if isinstance(entry, Mapping) else None
    if kind == "snapshot":
        return kind, read_snapshot(snapshot_dir, client)
    if kind == "codex_sessions":
        return kind, read_codex(entry, home=home)
    return "", None


def reader(
    routing_cfg: Mapping, *, snapshot_dir: Path, now: datetime, home: Path | None = None
) -> Callable[[str], float]:
    """The `quota` hook for routing.choose, memoised per client for the run."""
    quota_cfg = routing_cfg["quota"]
    if not quota_cfg["enabled"]:
        return routing.no_quota
    cache: dict[str, float] = {}

    def lookup(client: str) -> float:
        if client not in cache:
            kind, reading = _read(client, routing_cfg, snapshot_dir, home)
            cache[client] = factor(reading, quota_cfg, now=now) if kind else 1.0
        return cache[client]

    return lookup


def report(
    routing_cfg: Mapping, *, snapshot_dir: Path, now: datetime, home: Path | None = None
) -> list[dict]:
    """One row per routing or quota client, for `agent-bus quota show`."""
    quota_cfg = routing_cfg["quota"]
    names = list(dict.fromkeys(
        [*(routing_cfg.get("clients") or {}), *(quota_cfg.get("clients") or {})]
    ))
    rows = []
    for client in names:
        kind, reading = _read(client, routing_cfg, snapshot_dir, home)
        rows.append({
            "client": client,
            "enabled": bool(quota_cfg["enabled"]),
            "reader": kind or None,
            "age_seconds": (
                round(_utc(now).timestamp() - reading.at.timestamp(), 1) if reading else None
            ),
            "windows": [
                {"name": w.name, "used_percent": w.used_percent, "resets_at": w.resets_at}
                for w in (reading.windows if reading else ())
            ],
            "factor": factor(reading, quota_cfg, now=now) if kind else 1.0,
        })
    return rows

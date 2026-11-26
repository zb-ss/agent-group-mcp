"""The always-on dispatcher session: start, stop, status.

The dispatcher is an ordinary spawned session with a distinct role. Its
authority token reaches it only through a 0600 file that the launch command
reads and deletes; the bus keeps only the sha256 (see authority.py).
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from .. import authority, commands, resolution, spawn, tmux
from .. import status as status_mod
from ..config import ConfigError
from ..spawn import SpawnError, SpawnRequest
from .. import storage as storage_mod
from ..storage import Storage
from . import playbook, settings
from .store import DispatchStore

NAME_FILE = ".agent-bus-name"


class DispatcherError(Exception):
    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


@dataclass(frozen=True)
class StartResult:
    name: str
    work_dir: str
    tmux_session: str
    tmux_window: str
    command: str
    launched: bool
    already_running: bool
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class StatusResult:
    running: bool
    name: str | None
    state: str | None
    client: str | None
    tmux: str | None
    authority: bool
    pending_candidates: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def find_row(rows):
    """The dispatcher's own status row when it is live or in an active state."""
    for row in rows:
        if row.role == authority.ROLE_DISPATCHER and (
            row.live or row.state in status_mod.ACTIVE_STATES
        ):
            return row
    return None


def _rows(cfg: Mapping, storage: Storage, now: datetime | None):
    return status_mod.collect(
        storage, grace_seconds=cfg["spawn"]["startup_grace_seconds"], now=now
    )


def _timeouts(cfg: Mapping) -> dict[str, Any]:
    spawn_cfg = cfg["spawn"]
    return {"tmux_bin": spawn_cfg["tmux_bin"], "timeout": spawn_cfg["tmux_timeout_seconds"]}


def _prepare(
    cfg: Mapping, work_dir: Path, *, storage: Storage, run: commands.Runner, notes: list[str]
) -> None:
    d = cfg["dispatch"]["dispatcher"]
    work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not (work_dir / ".git").exists():
        # A local git folder with no remote: only so the existing wiring and
        # spawn code, which expect a repository, can be reused unchanged.
        done = run(
            ["git", "init", "--quiet", str(work_dir)],
            timeout=cfg["repos"]["git_timeout_seconds"],
        )
        if done.returncode != 0:
            raise DispatcherError(
                f"git init {work_dir} failed: {(done.stderr or '').strip() or done.returncode}"
            )
    name_file = work_dir / NAME_FILE
    if not name_file.exists():
        name_file.write_text(d["group"] + "\n", encoding="utf-8")
    spawn.wire_repo(d["client"], work_dir, storage=storage, notes=notes)
    # spawn finds a repo by alias through the roster; this folder has no
    # remote and lies outside the scan roots, so put its client address there
    storage.ensure_agent(f"{d['group']}/{d['client']}", str(work_dir))


PLACEHOLDERS = ("label", "dir", "soft_limit", "max_sessions", "clients")


def _render_prompt(cfg: Mapping, work_dir: Path) -> Path:
    d = cfg["dispatch"]["dispatcher"]
    text = playbook.render(
        d["playbook"],
        {
            "label": d["label"],
            "dir": str(work_dir),
            "soft_limit": str(cfg["dispatch"]["soft_limit"]),
            "max_sessions": str(cfg["spawn"]["max_sessions"]),
            "clients": ", ".join(cfg["dispatch"]["routing"]["clients"]),
        },
        templates_dir=cfg["dispatch"]["templates_dir"],
        placeholders=PLACEHOLDERS,
    )
    return playbook.write_prompt(
        text, label=d["label"], prompts_dir=settings.prompts_dir(cfg["dispatch"])
    )


def _close_stale_window(cfg: Mapping, storage: Storage, run: commands.Runner) -> None:
    label = cfg["dispatch"]["dispatcher"]["label"]
    stale = any(
        s.role == authority.ROLE_DISPATCHER and s.tmux_window == label
        for s in storage.list_spawns()
    )
    if not stale:
        return
    try:
        tmux.kill_windows(cfg["spawn"]["tmux_session"], label, run=run, **_timeouts(cfg))
    except tmux.TmuxError as exc:
        raise DispatcherError(str(exc)) from exc


def _write_token(token: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(token)
    os.chmod(path, 0o600)


def _discard_token(storage: Storage, path: Path) -> None:
    storage.revoke_authority(authority.ROLE_DISPATCHER)
    path.unlink(missing_ok=True)


def _result(spawned: spawn.SpawnResult, notes: list[str]) -> StartResult:
    return StartResult(
        name=spawned.name, work_dir=spawned.work_dir, tmux_session=spawned.tmux_session,
        tmux_window=spawned.tmux_window, command=spawned.command,
        launched=spawned.launched, already_running=False,
        notes=[*notes, *spawned.notes],
    )


def start(
    cfg: Mapping,
    *,
    storage: Storage,
    run: commands.Runner = commands.run_command,
    env: Mapping[str, str] | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
) -> StartResult:
    d = cfg["dispatch"]["dispatcher"]
    work_dir = settings.dispatcher_dir(cfg["dispatch"])
    row = find_row(_rows(cfg, storage, now))
    if row is not None:
        return StartResult(
            name=row.name, work_dir=row.work_dir, tmux_session=row.tmux_session or "",
            tmux_window=row.tmux_window or "", command="", launched=False,
            already_running=True, notes=[],
        )
    if (resolution.find_repo_root(work_dir.parent) / ".git").exists():
        raise DispatcherError(
            f"{work_dir} lies inside another repository; the dispatcher must run outside "
            f"every repo (set dispatch.dispatcher.dir in config.json)"
        )
    notes: list[str] = []
    if dry_run and not (work_dir / ".git").exists():
        return StartResult(
            name="", work_dir=str(work_dir), tmux_session=cfg["spawn"]["tmux_session"],
            tmux_window=d["label"], command="", launched=False, already_running=False,
            notes=[f"would prepare {work_dir}"],
        )
    token_path = settings.dispatcher_token_path()
    secret_env = ((resolution.AUTHORITY_TOKEN_ENV, token_path),)
    if dry_run:
        return _launch(
            cfg, work_dir, secret_env, storage=storage, run=run, env=env, now=now,
            notes=notes, dry_run=True, prompt=_dry_prompt(cfg),
        )
    try:
        _prepare(cfg, work_dir, storage=storage, run=run, notes=notes)
        prompt = _render_prompt(cfg, work_dir)
        _close_stale_window(cfg, storage, run)
        token = authority.new_token()
        try:
            _write_token(token, token_path)
            storage.register_authority(authority.ROLE_DISPATCHER, authority.digest(token))
        except BaseException:
            _discard_token(storage, token_path)
            raise
    except (playbook.PlaybookError, OSError, subprocess.SubprocessError) as exc:
        raise DispatcherError(f"dispatcher could not be prepared: {exc}") from exc
    return _launch(
        cfg, work_dir, secret_env, storage=storage, run=run, env=env, now=now,
        notes=notes, dry_run=False, prompt=prompt,
    )


def _dry_prompt(cfg: Mapping) -> Path:
    """Where the prompt would be written; a dry run writes nothing."""
    label = cfg["dispatch"]["dispatcher"]["label"]
    return settings.prompts_dir(cfg["dispatch"]) / f"{label}.md"


def _launch(
    cfg: Mapping, work_dir: Path, secret_env, *, storage: Storage, run, env, now,
    notes: list[str], dry_run: bool, prompt: Path,
) -> StartResult:
    d = cfg["dispatch"]["dispatcher"]
    request = SpawnRequest(
        repo=d["group"], client=d["client"], name=d["label"], prompt_file=prompt,
        model=d["model"], remote=d["remote"], dry_run=dry_run,
        role=authority.ROLE_DISPATCHER, secret_env=secret_env,
    )
    try:
        spawned = spawn.spawn(request, storage=storage, cfg=cfg, run=run, env=env, now=now)
    except (SpawnError, ConfigError) as exc:
        if not dry_run:
            _discard_token(storage, secret_env[0][1])
        raise DispatcherError(str(exc), getattr(exc, "hint", None)) from exc
    if not dry_run:
        storage.set_authority_name(authority.ROLE_DISPATCHER, spawned.name)
    return _result(spawned, notes)


def stop(
    cfg: Mapping, *, storage: Storage, run: commands.Runner = commands.run_command
) -> int:
    """Close the dispatcher's window and revoke its authority; returns the
    windows closed (0 is not an error)."""
    try:
        closed = tmux.kill_windows(
            cfg["spawn"]["tmux_session"], cfg["dispatch"]["dispatcher"]["label"],
            run=run, **_timeouts(cfg),
        )
    except tmux.TmuxError as exc:
        raise DispatcherError(str(exc)) from exc
    _discard_token(storage, settings.dispatcher_token_path())
    # the closed window's session must not read as running until the startup
    # grace runs out, or an immediate `start` would report it already running
    for record in storage.list_spawns():
        if record.role == authority.ROLE_DISPATCHER:
            storage.set_state(record.name, storage_mod.STATE_DONE)
    return closed


def status(
    cfg: Mapping, *, storage: Storage, store: DispatchStore, now: datetime | None = None
) -> StatusResult:
    row = find_row(_rows(cfg, storage, now))
    tmux_target = (
        f"{row.tmux_session}:{row.tmux_window}" if row and row.tmux_session else None
    )
    return StatusResult(
        running=row is not None,
        name=row.name if row else None,
        state=row.state if row else None,
        client=row.client if row else None,
        tmux=tmux_target,
        authority=storage.has_authority(authority.ROLE_DISPATCHER),
        pending_candidates=len(store.pending_candidates()),
    )

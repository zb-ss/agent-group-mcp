"""The tmux calls spawn needs: is it installed, does a session exist, open a
window. Every call goes through a `Runner` so tests never run a real tmux."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Mapping

from .commands import Runner, run_command


class TmuxError(RuntimeError):
    """tmux could not be run, or refused the request."""


def available(tmux_bin: str) -> bool:
    return shutil.which(tmux_bin) is not None


def _run(
    argv: list[str],
    *,
    timeout: float,
    run: Runner,
    env: Mapping[str, str] | None = None,
) -> "subprocess.CompletedProcess[str]":
    try:
        return run(argv, timeout=timeout, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        raise TmuxError(f"{argv[0]} {argv[1]} failed: {exc}") from exc


def has_session(
    session: str, *, tmux_bin: str, timeout: float, run: Runner = run_command
) -> bool:
    # The "=" prefix makes tmux match the name exactly, not as a prefix.
    done = _run([tmux_bin, "has-session", "-t", f"={session}"], timeout=timeout, run=run)
    return done.returncode == 0


def launch(
    session: str,
    window: str,
    work_dir: Path,
    command: str,
    *,
    env: Mapping[str, str],
    tmux_bin: str,
    timeout: float,
    run: Runner = run_command,
) -> None:
    """Open `command` in a new detached window of `session`, creating the
    session when it does not exist yet."""
    # tmux format-expands the start directory, so `#` must be doubled
    start_dir = str(work_dir).replace("#", "##")
    if has_session(session, tmux_bin=tmux_bin, timeout=timeout, run=run):
        argv = [
            tmux_bin, "new-window", "-d", "-t", f"={session}:",
            "-n", window, "-c", start_dir, command,
        ]
    else:
        argv = [
            tmux_bin, "new-session", "-d", "-s", session,
            "-n", window, "-c", start_dir, command,
        ]
    done = _run(argv, timeout=timeout, run=run, env=env)
    if done.returncode != 0:
        detail = (done.stderr or "").strip() or f"exit {done.returncode}"
        raise TmuxError(f"tmux {argv[1]} failed: {detail}")

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


def window_ids(
    session: str, window: str, *, tmux_bin: str, timeout: float, run: Runner = run_command
) -> list[str]:
    """The ids of the windows of `session` named exactly `window`."""
    done = _run(
        [tmux_bin, "list-windows", "-t", f"={session}", "-F", "#{window_id}\t#{window_name}"],
        timeout=timeout, run=run,
    )
    if done.returncode != 0:
        return []
    ids = []
    for line in (done.stdout or "").splitlines():
        window_id, _, name = line.partition("\t")
        if window_id and name == window:
            ids.append(window_id)
    return ids


def kill_windows(
    session: str, window: str, *, tmux_bin: str, timeout: float, run: Runner = run_command
) -> int:
    """Close every window of `session` named `window`; returns how many."""
    closed = 0
    for window_id in window_ids(session, window, tmux_bin=tmux_bin, timeout=timeout, run=run):
        done = _run([tmux_bin, "kill-window", "-t", window_id], timeout=timeout, run=run)
        if done.returncode != 0:
            detail = (done.stderr or "").strip() or f"exit {done.returncode}"
            raise TmuxError(f"tmux kill-window failed: {detail}")
        closed += 1
    return closed


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
) -> str:
    """Open `command` in a new detached window of `session`, creating the
    session when it does not exist yet. Returns the id of the new window, so
    a caller never has to find it again by name."""
    # tmux format-expands the start directory, so `#` must be doubled
    start_dir = str(work_dir).replace("#", "##")
    if has_session(session, tmux_bin=tmux_bin, timeout=timeout, run=run):
        head = [tmux_bin, "new-window", "-d", "-t", f"={session}:", "-n", window]
    else:
        head = [tmux_bin, "new-session", "-d", "-s", session, "-n", window]
    argv = [*head, "-P", "-F", "#{window_id}", "-c", start_dir, command]
    done = _run(argv, timeout=timeout, run=run, env=env)
    if done.returncode != 0:
        detail = (done.stderr or "").strip() or f"exit {done.returncode}"
        raise TmuxError(f"tmux {argv[1]} failed: {detail}")
    return (done.stdout or "").strip()


# The kernel keeps 15 characters of a process name (16 with the NUL).
_COMM_LIMIT = 15


def _require_client_running(
    target: str, client: str, *, tmux_bin: str, timeout: float, run: Runner,
    env: Mapping[str, str] | None,
) -> None:
    """A first prompt must never reach a login shell: tracker text in it would
    run. The window's command is `<client>; <after_exit>`, run by a shell
    without job control, so tmux reports that shell as the current command for
    the whole life of the client. Any child of the pane's shell is no proof (a
    helper of the login shell is one too): require a direct child whose process
    name is the client binary's. Once the client has exited the after_exit
    shell replaces the wrapper (same pid) and has no such child."""
    name = Path(client).name[:_COMM_LIMIT]
    if not name:
        raise TmuxError(f"window {target}: no client name to recognise its process by")
    probe = _run(
        [tmux_bin, "display-message", "-p", "-t", target, "#{pane_pid}"],
        timeout=timeout, run=run, env=env,
    )
    if probe.returncode != 0:
        raise TmuxError(f"window {target} is gone")
    pane_pid = (probe.stdout or "").strip()
    if not pane_pid.isdigit():
        raise TmuxError(f"window {target} has no readable process id")
    found = _run(["pgrep", "-P", pane_pid, "-x", name], timeout=timeout, run=run, env=env)
    if found.returncode != 0 or not (found.stdout or "").strip():
        raise TmuxError(f"window {target} is at a shell, not the client {name}")


def paste_file(
    target: str,
    file: Path,
    *,
    client: str,
    tmux_bin: str,
    timeout: float,
    run: Runner = run_command,
    env: Mapping[str, str] | None = None,
) -> None:
    """Type the contents of `file` into the window `target` (a window id from
    `launch`), then press Enter. tmux reads the file itself, so the text never
    travels in an argument. `client` is the client's command (its binary name
    identifies the process). Refuses a window that has fallen back to a shell,
    and checks again right before Enter, so text pasted just as the client
    exits is left unsent. Uses a named buffer so a user's own is untouched."""
    if not target:
        raise TmuxError("no window id to paste into")
    check = lambda: _require_client_running(  # noqa: E731
        target, client, tmux_bin=tmux_bin, timeout=timeout, run=run, env=env)
    check()
    buffer = f"agent-bus-{target.lstrip('@')}"
    steps = (
        [tmux_bin, "load-buffer", "-b", buffer, str(file)],
        [tmux_bin, "paste-buffer", "-p", "-d", "-b", buffer, "-t", target],
        [tmux_bin, "send-keys", "-t", target, "Enter"],
    )
    for argv in steps:
        if argv[1] == "send-keys":
            check()
        done = _run(argv, timeout=timeout, run=run, env=env)
        if done.returncode != 0:
            detail = (done.stderr or "").strip() or f"exit {done.returncode}"
            raise TmuxError(f"tmux {argv[1]} failed: {detail}")

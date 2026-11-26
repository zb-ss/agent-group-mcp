"""Git checks and removal for a dispatched worktree that has been merged.

The rule: fail closed, never force the removal, never touch the clone itself.
Any git failure, timeout or unexpected output means the worktree stays.
Every git call goes through the injected `commands.Runner` as an argv list
(never a shell).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ..commands import Runner


def _out(run: Runner, argv: list[str], timeout: float) -> tuple[int | None, str]:
    """(returncode, stripped stdout); (None, "") when git could not run."""
    try:
        done = run(argv, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None, ""
    return done.returncode, (done.stdout or "").strip()


def _listed_worktrees(clone: Path, timeout: float, run: Runner) -> list[str] | None:
    code, out = _out(run, ["git", "-C", str(clone), "worktree", "list", "--porcelain"], timeout)
    if code != 0:
        return None
    return [
        os.path.realpath(line[len("worktree "):])
        for line in out.splitlines()
        if line.startswith("worktree ")
    ]


def _patch_equivalent(run: Runner, clone: Path, branch: str, base: str, timeout: float) -> bool:
    """Every commit of the branch has a patch-equivalent in base (a rebase
    merge or a single-commit squash). Multi-commit squashes stay undetected."""
    code, out = _out(
        run, ["git", "-C", str(clone), "cherry", base, f"refs/heads/{branch}"], timeout
    )
    if code != 0:
        return False
    return not any(line.startswith("+") for line in out.splitlines())


def keep_reason(
    work_dir: Path, clone: Path, *, base: str, remote: str, timeout: float, run: Runner,
    start_commit: str | None = None,
) -> str | None:
    """Why the worktree must stay, or None when it may be removed."""
    real_dir = os.path.realpath(work_dir)
    if real_dir == os.path.realpath(clone):
        return "it is the clone itself"

    code, branch = _out(run, ["git", "-C", str(work_dir), "branch", "--show-current"], timeout)
    if code != 0:
        return "git failed (branch)"
    if not branch:
        return "detached HEAD (no branch): kept for follow-up resumes"

    listed = _listed_worktrees(clone, timeout, run)
    if listed is None:
        return "git failed (worktree list)"
    if real_dir not in listed or listed[0] == real_dir:
        return "not a linked worktree of the clone"

    if base.startswith("-"):
        return "invalid base ref"
    # A branch still at its starting commit is trivially an ancestor of the
    # base; only a branch that moved can have been merged.
    if not start_commit:
        return "start commit unknown"
    code, tip = _out(run, ["git", "-C", str(clone), "rev-parse", "--verify", "--quiet",
                           f"refs/heads/{branch}^{{commit}}"], timeout)
    if code != 0 or not tip:
        return "git failed (rev-parse)"
    if tip == start_commit:
        return f"branch {branch} has no commits of its own"
    code, _ = _out(
        run,
        ["git", "-C", str(clone), "merge-base", "--is-ancestor", f"refs/heads/{branch}", base],
        timeout,
    )
    if code == 1:
        if not _patch_equivalent(run, clone, branch, base, timeout):
            return f"branch {branch} is not merged into {base}"
    elif code != 0:
        return f"cannot tell whether {branch} is merged into {base}"

    code, status = _out(run, ["git", "-C", str(work_dir), "status", "--porcelain"], timeout)
    if code != 0:
        return "git failed (status)"
    if status:
        return "uncommitted changes"

    code, count = _out(
        run,
        ["git", "-C", str(work_dir), "rev-list", "--count", "HEAD", "--not", f"--remotes={remote}"],
        timeout,
    )
    if code != 0:
        return "git failed (rev-list)"
    if count != "0":
        return f"commits not pushed to {remote}"
    return None


def remove(work_dir: Path, clone: Path, *, timeout: float, run: Runner) -> str | None:
    """Remove the worktree; None on success, else why it failed."""
    argv = ["git", "-C", str(clone), "worktree", "remove", "--", str(work_dir)]
    try:
        done = run(argv, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return type(exc).__name__
    if done.returncode == 0:
        return None
    return (done.stderr or "").strip() or f"exit {done.returncode}"

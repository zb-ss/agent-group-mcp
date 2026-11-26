"""Worktree cleanup checks: fake-runner reasons plus one real-git round trip."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from agent_bus import commands
from agent_bus.dispatch import cleanup

CLONE = Path("/work/clone")
TREE = Path("/work/tree")


def _done(out: str = "", code: int = 0, err: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], code, stdout=out, stderr=err)


class Fake:
    """Answers by git subcommand; records every argv."""

    def __init__(self, **answers):
        self.calls: list[list[str]] = []
        self.answers = {
            "branch": _done("feat\n"),
            "worktree": _done(f"worktree {CLONE}\nHEAD x\n\nworktree {TREE}\nHEAD y\n"),
            "rev-parse": _done("tip\n"),
            "merge-base": _done(),
            "cherry": _done("+ abc\n"),
            "status": _done(""),
            "rev-list": _done("0\n"),
        }
        self.answers.update(answers)

    def __call__(self, argv, *, timeout):
        self.calls.append(argv)
        answer = self.answers[argv[3]]
        if isinstance(answer, Exception):
            raise answer
        return answer


def _keep(fake, **kw):
    args = dict(base="origin/main", remote="origin", timeout=5, run=fake, start_commit="start")
    args.update(kw)
    return cleanup.keep_reason(TREE, CLONE, **args)


def test_removable_and_argv_order():
    fake = Fake()
    assert _keep(fake) is None
    assert [c[3] for c in fake.calls] == ["branch", "worktree", "rev-parse", "merge-base", "status", "rev-list"]
    assert fake.calls[3][-2:] == ["refs/heads/feat", "origin/main"]
    assert fake.calls[5][-4:] == ["--count", "HEAD", "--not", "--remotes=origin"]


def test_clone_itself():
    assert cleanup.keep_reason(
        CLONE, CLONE, base="b", remote="origin", timeout=5, run=Fake()
    ) == "it is the clone itself"


@pytest.mark.parametrize("answers, reason", [
    ({"branch": _done("", 1)}, "git failed (branch)"),
    ({"branch": OSError("x")}, "git failed (branch)"),
    ({"branch": _done("")}, "detached HEAD (no branch): kept for follow-up resumes"),
    ({"worktree": _done("", 1)}, "git failed (worktree list)"),
    ({"worktree": _done("worktree /work/clone\n")}, "not a linked worktree of the clone"),
    ({"worktree": _done(f"worktree {TREE}\n\nworktree {CLONE}\n")}, "not a linked worktree of the clone"),
    ({"rev-parse": _done("start\n")}, "branch feat has no commits of its own"),
    ({"rev-parse": _done("", 1)}, "git failed (rev-parse)"),
    ({"merge-base": _done("", 1)}, "branch feat is not merged into origin/main"),
    ({"merge-base": _done("", 128)}, "cannot tell whether feat is merged into origin/main"),
    ({"merge-base": subprocess.TimeoutExpired("git", 1)}, "cannot tell whether feat is merged into origin/main"),
    ({"status": _done("", 1)}, "git failed (status)"),
    ({"status": _done(" M a\n")}, "uncommitted changes"),
    ({"rev-list": _done("", 1)}, "git failed (rev-list)"),
    ({"rev-list": _done("2\n")}, "commits not pushed to origin"),
    ({"rev-list": _done("")}, "commits not pushed to origin"),
])
def test_keep_reasons(answers, reason):
    assert _keep(Fake(**answers)) == reason


def test_unknown_start_commit_keeps_the_worktree():
    assert _keep(Fake(), start_commit=None) == "start commit unknown"


def test_rebase_merged_branch_is_accepted_via_cherry():
    fake = Fake(**{"merge-base": _done("", 1), "cherry": _done("- a1\n- b2\n")})
    assert _keep(fake) is None
    assert fake.calls[4][3:] == ["cherry", "origin/main", "refs/heads/feat"]


@pytest.mark.parametrize("cherry", [
    _done("- a1\n+ b2\n"), _done("", 1), OSError("x"),
])
def test_cherry_unmerged_or_failing_keeps_the_branch(cherry):
    fake = Fake(**{"merge-base": _done("", 1), "cherry": cherry})
    assert _keep(fake) == "branch feat is not merged into origin/main"


def test_cherry_is_not_asked_when_ancestor_or_unknown():
    fake = Fake(**{"merge-base": _done("", 128)})
    assert _keep(fake) == "cannot tell whether feat is merged into origin/main"
    assert all(c[3] != "cherry" for c in fake.calls)


def test_invalid_base():
    assert _keep(Fake(), base="-x") == "invalid base ref"


def test_remove_argv_has_no_force():
    calls = []

    def run(argv, *, timeout):
        calls.append(argv)
        return _done()

    assert cleanup.remove(TREE, CLONE, timeout=5, run=run) is None
    assert calls == [["git", "-C", str(CLONE), "worktree", "remove", "--", str(TREE)]]
    assert "--force" not in calls[0] and "-f" not in calls[0]


def test_remove_failures():
    assert cleanup.remove(TREE, CLONE, timeout=5, run=lambda a, timeout: _done("", 1, " busy \n")) == "busy"
    assert cleanup.remove(TREE, CLONE, timeout=5, run=lambda a, timeout: _done("", 3)) == "exit 3"

    def boom(argv, *, timeout):
        raise subprocess.TimeoutExpired("git", 1)

    assert cleanup.remove(TREE, CLONE, timeout=5, run=boom) == "TimeoutExpired"


def test_real_git_round_trip(tmp_path, monkeypatch):
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    for key, value in {
        "GIT_AUTHOR_NAME": "Example", "GIT_AUTHOR_EMAIL": "example@example.com",
        "GIT_COMMITTER_NAME": "Example", "GIT_COMMITTER_EMAIL": "example@example.com",
    }.items():
        monkeypatch.setenv(key, value)

    def git(cwd, *args):
        subprocess.run(["git", "-c", "init.defaultBranch=main", *args], cwd=cwd, check=True,
                       capture_output=True, text=True)

    remote = tmp_path / "remote.git"
    clone = tmp_path / "clone"
    tree = tmp_path / "tree"
    git(tmp_path, "init", "--bare", str(remote))
    git(tmp_path, "clone", str(remote), str(clone))
    (clone / "a.txt").write_text("a\n")
    git(clone, "add", "a.txt")
    git(clone, "commit", "-m", "first")
    git(clone, "push", "origin", "HEAD:main")
    git(clone, "fetch", "origin")
    git(clone, "branch", "--set-upstream-to=origin/main")
    git(clone, "worktree", "add", "--detach", str(tree))

    assert cleanup.keep_reason(tree, clone, base="origin/main", remote="origin", timeout=30,
                               run=commands.run_command) == "detached HEAD (no branch): kept for follow-up resumes"

    start = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tree, check=True,
                           capture_output=True, text=True).stdout.strip()

    def keep():
        return cleanup.keep_reason(tree, clone, base="origin/main", remote="origin",
                                   timeout=30, run=commands.run_command, start_commit=start)

    git(tree, "checkout", "-b", "feat")
    (tree / "ignored.env").write_text("x\n")
    assert keep() == "branch feat has no commits of its own"
    (tree / "ignored.env").unlink()
    (tree / "b.txt").write_text("b\n")
    git(tree, "add", "b.txt")
    git(tree, "commit", "-m", "second")
    assert keep() == "branch feat is not merged into origin/main"
    # Merged into the local main but never pushed from the worktree.
    git(clone, "merge", "--ff-only", "feat")
    assert cleanup.keep_reason(tree, clone, base="main", remote="origin", timeout=30,
                               run=commands.run_command, start_commit=start) == "commits not pushed to origin"

    git(tree, "push", "origin", "feat")
    assert keep() == "branch feat is not merged into origin/main"

    git(clone, "push", "origin", "main")
    git(clone, "fetch", "origin")
    assert keep() is None

    (tree / "junk.txt").write_text("x\n")
    assert keep() == "uncommitted changes"

    (tree / "junk.txt").unlink()

    assert cleanup.remove(tree, clone, timeout=30, run=commands.run_command) is None
    assert not tree.exists()
    assert (clone / "a.txt").exists() and (clone / "b.txt").exists()


def test_real_git_rebase_merge_is_detected(tmp_path, monkeypatch):
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    for key, value in {
        "GIT_AUTHOR_NAME": "Example", "GIT_AUTHOR_EMAIL": "example@example.com",
        "GIT_COMMITTER_NAME": "Example", "GIT_COMMITTER_EMAIL": "example@example.com",
    }.items():
        monkeypatch.setenv(key, value)

    def git(cwd, *args):
        subprocess.run(["git", "-c", "init.defaultBranch=main", *args], cwd=cwd, check=True,
                       capture_output=True, text=True)

    clone = tmp_path / "clone"
    tree = tmp_path / "tree"
    git(tmp_path, "init", str(clone))
    (clone / "a.txt").write_text("a\n")
    git(clone, "add", "a.txt")
    git(clone, "commit", "-m", "first")
    git(clone, "worktree", "add", "-b", "feat", str(tree))
    start = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tree, check=True,
                           capture_output=True, text=True).stdout.strip()
    (tree / "b.txt").write_text("b\n")
    git(tree, "add", "b.txt")
    git(tree, "commit", "-m", "feature")
    # Main moves on, then receives the feature as a rebased copy.
    (clone / "c.txt").write_text("c\n")
    git(clone, "add", "c.txt")
    git(clone, "commit", "-m", "other")
    git(clone, "cherry-pick", "feat")

    def keep():
        return cleanup.keep_reason(tree, clone, base="main", remote="origin",
                                   timeout=30, run=commands.run_command, start_commit=start)

    assert "is not merged" not in (keep() or "")
    git(tree, "commit", "--allow-empty", "-m", "unmerged extra")
    assert keep() == "branch feat is not merged into main"

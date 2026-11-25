"""`agent-bus dispatch ...`: the CLI wiring, with a fake source and fake runner."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_bus import cli, commands, config
from agent_bus.dispatch import runner, settings
from agent_bus.dispatch.model import Membership, Task
from agent_bus.dispatch.store import DispatchStore

PROJECT = "1000000000000001"
GID = "1000000000000101"


def _task() -> Task:
    return Task(
        source="asana", task_id=GID, title="Fix the login form",
        url=f"https://example.com/task/{GID}", due_on="2030-01-07",
        modified_at="2030-01-05T09:00:00.000000Z",
        newest_comment_at="2030-01-05T09:00:00.000000Z",
        memberships=(Membership(PROJECT, "Project A", "Refine"),),
    )


class FakeSource:
    name = "asana"

    def fetch(self, *, today):
        return [_task()]


def _write_config(data: dict) -> None:
    path = config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _enable_asana() -> None:
    _write_config({"dispatch": {"sources": {"asana": {
        "enabled": True,
        "projects": {PROJECT: {"repo": "example-org/repo-a"}},
    }}}})


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


def test_run_dry_run_json_changes_nothing(capsys, monkeypatch):
    _enable_asana()
    monkeypatch.setattr(runner, "build_sources", lambda d, env, **k: [FakeSource()])
    assert cli.main(["dispatch", "run", "--dry-run", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["dry_run"] is True
    assert [d["result"] for d in report["decisions"]] == ["would_spawn"]
    assert not settings.dispatch_db_path().exists()


def test_run_plain_dry_run_prints_a_line_per_decision(capsys, monkeypatch):
    _enable_asana()
    monkeypatch.setattr(runner, "build_sources", lambda d, env, **k: [FakeSource()])
    assert cli.main(["dispatch", "run", "--dry-run"]) == 0
    assert "would_spawn refinement" in capsys.readouterr().out


def test_run_without_a_source_says_so(capsys):
    assert cli.main(["dispatch", "run"]) == 0
    assert "no dispatch source is enabled" in capsys.readouterr().err


def test_run_with_a_broken_config_names_the_file(capsys):
    path = config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert cli.main(["dispatch", "run"]) == 1
    assert "config.json" in capsys.readouterr().err


def test_status_empty(capsys):
    assert cli.main(["dispatch", "status"]) == 0
    assert "(no dispatched tasks)" in capsys.readouterr().out


def test_status_json_after_seeding(capsys):
    store = DispatchStore()
    store.observe(_task(), project_id=PROJECT, now="2030-01-07T10:00:00.000000Z")
    store.update("asana", GID, type="refinement", session_name="repo-a/claude-x")
    assert cli.main(["dispatch", "status", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["task_id"] == GID
    assert rows[0]["session_state"] == "ended"
    assert cli.main(["dispatch", "status"]) == 0
    assert GID in capsys.readouterr().out


def test_status_follows_a_session_that_renamed_itself(capsys, monkeypatch):
    from agent_bus import status

    store = DispatchStore()
    store.observe(_task(), project_id=PROJECT, now="2030-01-07T10:00:00.000000Z")
    store.update("asana", GID, type="refinement", label="t0000101-fix-the-login",
                 session_name="repo-a/claude-t0000101-fix-the-login")
    renamed = status.StatusRow(
        name="repo-a/claude-login-form", group="repo-a", client="claude",
        label="login-form", state="working", reported_state="working", state_at=None,
        live=True, spawned=True, last_seen=None, topic=None, pending_count=0,
        repo_path="/x", repo_id=None, work_dir="/x", tmux_session="agents",
        tmux_window="t0000101-fix-the-login", model=None, remote=None,
    )
    monkeypatch.setattr(status, "collect", lambda *a, **k: [renamed])
    assert cli.main(["dispatch", "status", "--json"]) == 0
    row = json.loads(capsys.readouterr().out)[0]
    assert row["current_session"] == "repo-a/claude-login-form"
    assert row["session_state"] == "working"
    assert cli.main(["dispatch", "status"]) == 0
    line = capsys.readouterr().out
    assert "repo-a/claude-login-form  working" in line


def _fail_if_called(*args, **kwargs):
    raise AssertionError("the timer must not be loaded")


def test_install_and_uninstall_timer(home, capsys, monkeypatch):
    monkeypatch.setattr(commands, "run_command", _fail_if_called)
    assert cli.main(["dispatch", "install-timer", "--bin-path", "/usr/local/bin/agent-bus"]) == 0
    out = capsys.readouterr().out
    paths = [Path(line.split(" ", 1)[1]) for line in out.splitlines() if line.startswith("wrote ")]
    assert paths and all(p.exists() and str(home) in str(p) for p in paths)
    assert "not loaded; to load it run:" in out

    assert cli.main(["dispatch", "uninstall-timer", "--bin-path", "/usr/local/bin/agent-bus"]) == 0
    assert "removed" in capsys.readouterr().out
    assert not any(p.exists() for p in paths)


def test_install_timer_load_uses_the_runner(home, capsys, monkeypatch):
    import subprocess

    calls = []

    def fake_run(argv, *, timeout, env=None):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(commands, "run_command", fake_run)
    assert cli.main(
        ["dispatch", "install-timer", "--load", "--bin-path", "/usr/local/bin/agent-bus"]
    ) == 0
    assert calls
    assert "loaded" in capsys.readouterr().out


def test_install_timer_rejects_relative_bin_path(home, capsys):
    assert cli.main(["dispatch", "install-timer", "--bin-path", "agent-bus"]) == 1
    assert "not absolute" in capsys.readouterr().err


def test_dispatch_needs_a_subcommand():
    with pytest.raises(SystemExit) as exc:
        cli.main(["dispatch"])
    assert exc.value.code == 2


def test_version_still_works(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0


def test_uninstall_timer_works_without_an_absolute_binary(home, capsys, monkeypatch):
    monkeypatch.setattr(commands, "run_command", _fail_if_called)
    monkeypatch.delenv("AGENT_BUS_BIN", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert cli.main(["dispatch", "uninstall-timer"]) == 0
    assert "not absolute" not in capsys.readouterr().err

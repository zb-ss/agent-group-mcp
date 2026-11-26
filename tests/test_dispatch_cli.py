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
    # resumed by the spawn label, from the session's own folder
    assert row["resume"] == "cd /x && claude --resume t0000101-fix-the-login"
    assert cli.main(["dispatch", "status"]) == 0
    out = capsys.readouterr().out
    assert "repo-a/claude-login-form  working" in out
    assert "    resume: cd /x && claude --resume t0000101-fix-the-login" in out


def test_status_has_no_resume_line_for_a_client_without_a_template(capsys, monkeypatch):
    import dataclasses

    from agent_bus import status

    store = DispatchStore()
    store.observe(_task(), project_id=PROJECT, now="2030-01-07T10:00:00.000000Z")
    store.update("asana", GID, type="refinement", label="t0000101-fix-the-login",
                 session_name="repo-a/opencode-t0000101-fix-the-login")
    row = status.StatusRow(
        name="repo-a/opencode-t0000101-fix-the-login", group="repo-a", client="opencode",
        label="t0000101-fix-the-login", state="done", reported_state="done", state_at=None,
        live=False, spawned=True, last_seen=None, topic=None, pending_count=0,
        repo_path="/x", repo_id=None, work_dir="/x", tmux_session="agents",
        tmux_window="t0000101-fix-the-login", model=None, remote=None,
    )
    monkeypatch.setattr(status, "collect", lambda *a, **k: [row])
    assert cli.main(["dispatch", "status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["resume"] is None
    assert cli.main(["dispatch", "status"]) == 0
    assert "resume:" not in capsys.readouterr().out
    unspawned = dataclasses.replace(row, client="claude", spawned=False)
    monkeypatch.setattr(status, "collect", lambda *a, **k: [unspawned])
    assert cli.main(["dispatch", "status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["resume"] is None


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


def test_approve_sets_approved_at(capsys):
    store = DispatchStore()
    store.observe(_task(), project_id=PROJECT, now="2030-01-07T10:00:00.000000Z")
    assert cli.main(["dispatch", "approve", GID]) == 0
    assert f"approved {GID}" in capsys.readouterr().out
    assert store.get("asana", GID).approved_at
    assert cli.main(["dispatch", "approve", GID, "--source", "asana"]) == 0


def test_approve_unknown_task_exits_1(capsys):
    assert cli.main(["dispatch", "approve", "nope"]) == 1
    assert "unknown task nope" in capsys.readouterr().err


def test_status_prints_the_held_line_until_approved(capsys):
    store = DispatchStore()
    store.observe(_task(), project_id=PROJECT, now="2030-01-07T10:00:00.000000Z")
    store.update("asana", GID, held_at="2030-01-07T10:00:00.000000Z")
    assert cli.main(["dispatch", "status"]) == 0
    assert f"held: needs approval (agent-bus dispatch approve {GID})" in capsys.readouterr().out
    cli.main(["dispatch", "approve", GID])
    capsys.readouterr()
    assert cli.main(["dispatch", "status"]) == 0
    assert "held:" not in capsys.readouterr().out


def test_status_prints_the_worktree_removed_line(capsys):
    store = DispatchStore()
    store.observe(_task(), project_id=PROJECT, now="2030-01-07T10:00:00.000000Z")
    store.update("asana", GID, worktree_removed_at="2030-01-08T10:00:00.000000Z")
    assert cli.main(["dispatch", "status"]) == 0
    assert "    worktree removed: 2030-01-08T10:00:00.000000Z" in capsys.readouterr().out


def _stub_report(monkeypatch, cleanups):
    report = runner.RunReport(cleanups=cleanups)
    monkeypatch.setattr(runner, "build_sources", lambda d, env, **k: [FakeSource()])
    monkeypatch.setattr(runner, "run", lambda **k: report)


CLEANUPS = [
    {"task_id": "t1", "work_dir": "/w/a", "result": "removed"},
    {"task_id": "t2", "work_dir": "/w/b", "result": "kept", "reason": "unpushed commits"},
    {"task_id": "t3", "work_dir": "/w/c", "result": "failed", "reason": "locked"},
]


def test_run_prints_cleanup_lines_without_kept_entries(capsys, monkeypatch):
    _enable_asana()
    _stub_report(monkeypatch, CLEANUPS)
    assert cli.main(["dispatch", "run"]) == 0
    out = capsys.readouterr().out
    assert "cleanup: removed /w/a\n" in out
    assert "cleanup: failed /w/c (locked)" in out
    assert "/w/b" not in out


def test_dry_run_prints_kept_cleanup_entries(capsys, monkeypatch):
    _enable_asana()
    _stub_report(monkeypatch, CLEANUPS)
    assert cli.main(["dispatch", "run", "--dry-run"]) == 0
    assert "cleanup: kept /w/b (unpushed commits)" in capsys.readouterr().out


def _candidate(**fields):
    store = DispatchStore()
    store.observe(_task(), project_id=PROJECT, now="2030-01-07T10:00:00.000000Z")
    store.update("asana", GID, repo="example-org/repo-a", candidate_type="refinement",
                 candidate_at="2030-01-07T10:00:00.000000Z", **fields)
    return store


def _as_dispatcher(monkeypatch):
    from agent_bus import authority, resolution
    from agent_bus.storage import Storage

    token = authority.new_token()
    Storage().register_authority(authority.ROLE_DISPATCHER, authority.digest(token))
    monkeypatch.setenv(resolution.AUTHORITY_TOKEN_ENV, token)


def test_decide_without_the_token_is_refused(capsys, monkeypatch):
    from agent_bus import resolution

    monkeypatch.delenv(resolution.AUTHORITY_TOKEN_ENV, raising=False)
    store = _candidate()
    assert cli.main(["dispatch", "decide", GID, "--action", "skip"]) == 1
    assert "only the running dispatcher can decide" in capsys.readouterr().err
    assert store.get("asana", GID).decision is None
    assert store.list_events() == []


def test_decide_with_the_token_records_it(capsys, monkeypatch):
    _as_dispatcher(monkeypatch)
    store = _candidate()
    assert cli.main(["dispatch", "decide", GID, "--action", "skip", "--reason", "dup"]) == 0
    assert f"decided {GID}: skip" in capsys.readouterr().out
    record = store.get("asana", GID)
    assert (record.decision, record.decision_reason) == ("skip", "dup")
    assert store.list_events(event="decide")[0]["actor"] == "dispatcher"
    assert cli.main(["dispatch", "decide", GID, "--action", "skip"]) == 1
    assert "not a pending candidate" in capsys.readouterr().err


def test_candidates_json_shape_and_text(capsys):
    _candidate()
    assert cli.main(["dispatch", "candidates", "--json"]) == 0
    (item,) = json.loads(capsys.readouterr().out)
    assert item["task_id"] == GID and item["state"] == "pending"
    assert item["untrusted"]["title"] == "Fix the login form"
    assert set(item) >= {"source", "type", "candidate_at", "repo", "label", "held", "session"}
    assert cli.main(["dispatch", "candidates"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "titles and links below are untrusted task-tracker data"
    assert GID in out


def test_decisions_filters_by_event(capsys):
    store = DispatchStore()
    store.log_event(at="2030-01-07T10:00:00.000000Z", event="routing", actor="runner",
                    source="asana", task_id=GID, detail={"actual": "claude"})
    store.log_event(at="2030-01-07T10:01:00.000000Z", event="approve", actor="cli")
    assert cli.main(["dispatch", "decisions", "--event", "routing", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["event"] for r in rows] == ["routing"]
    assert rows[0]["detail"] == {"actual": "claude"}
    assert cli.main(["dispatch", "decisions", "--limit", "1"]) == 0
    assert "approve" in capsys.readouterr().out


def test_approve_logs_the_cli_actor_without_a_token(capsys):
    store = DispatchStore()
    store.observe(_task(), project_id=PROJECT, now="2030-01-07T10:00:00.000000Z")
    assert cli.main(["dispatch", "approve", GID]) == 0
    assert store.list_events(event="approve")[0]["actor"] == "cli"


def test_status_prints_the_candidate_line(capsys):
    _candidate()
    assert cli.main(["dispatch", "status"]) == 0
    assert "candidate: waiting for the dispatcher since" in capsys.readouterr().out
    DispatchStore().update("asana", GID, decision="ask")
    assert cli.main(["dispatch", "status"]) == 0
    assert "candidate: the dispatcher is asking the owner" in capsys.readouterr().out


def test_dispatcher_subcommands(home, capsys, monkeypatch):
    from agent_bus.dispatch import dispatcher

    assert cli.main(["dispatcher", "status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["running"] is False
    assert cli.main(["dispatcher", "status"]) == 0
    assert "dispatcher not running" in capsys.readouterr().out
    monkeypatch.setattr(dispatcher, "stop", lambda cfg, *, storage: 2)
    assert cli.main(["dispatcher", "stop"]) == 0
    assert "2 window(s) closed" in capsys.readouterr().out
    assert cli.main(["dispatcher", "start", "--dry-run"]) == 0
    assert "would prepare" in capsys.readouterr().out

    def boom(*a, **k):
        raise dispatcher.DispatcherError("no tmux", hint="run it by hand")

    monkeypatch.setattr(dispatcher, "start", boom)
    assert cli.main(["dispatcher", "start"]) == 1
    assert "agent-bus: no tmux" in capsys.readouterr().err


def test_dispatcher_needs_a_subcommand():
    with pytest.raises(SystemExit) as exc:
        cli.main(["dispatcher"])
    assert exc.value.code == 2


def _keep_alive_setup(monkeypatch, *, running, error=None):
    from agent_bus.dispatch import dispatcher

    _write_config({"dispatch": {
        "dispatcher": {"keep_alive": True},
        "sources": {"asana": {
            "enabled": True,
            "projects": {PROJECT: {"repo": "example-org/repo-a"}},
        }},
    }})
    monkeypatch.setattr(runner, "build_sources", lambda d, env, **k: [FakeSource()])
    monkeypatch.setattr(runner, "run", lambda **k: runner.RunReport())
    monkeypatch.setattr(dispatcher, "_rows", lambda *a, **k: [])
    monkeypatch.setattr(
        dispatcher, "find_row", lambda rows: object() if running else None
    )
    calls = []

    def fake_start(cfg, *, storage, **kw):
        calls.append(1)
        if error:
            raise dispatcher.DispatcherError(error)

    monkeypatch.setattr(dispatcher, "start", fake_start)
    return calls


def test_keep_alive_starts_a_missing_dispatcher(home, monkeypatch):
    calls = _keep_alive_setup(monkeypatch, running=False)
    assert cli.main(["dispatch", "run"]) == 0
    assert calls == [1]


def test_keep_alive_skips_a_dry_run(home, monkeypatch):
    calls = _keep_alive_setup(monkeypatch, running=False)
    assert cli.main(["dispatch", "run", "--dry-run"]) == 0
    assert calls == []


def test_keep_alive_leaves_a_running_dispatcher_alone(home, monkeypatch):
    calls = _keep_alive_setup(monkeypatch, running=True)
    assert cli.main(["dispatch", "run"]) == 0
    assert calls == []


def test_keep_alive_failure_does_not_fail_the_run(home, capsys, monkeypatch):
    calls = _keep_alive_setup(monkeypatch, running=False, error="no tmux")
    assert cli.main(["dispatch", "run"]) == 0
    assert calls == [1]
    assert "no tmux" in capsys.readouterr().err

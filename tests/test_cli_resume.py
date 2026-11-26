"""`agent-bus resume`: reopen an ended spawned session."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_bus import cli, spawn
from agent_bus.clients.base import ResumableSession
from agent_bus.storage import SpawnRecord


def _record(**over) -> SpawnRecord:
    fields = dict(
        name="grp/claude-fix",
        client="claude",
        repo_id="host/owner/repo",
        repo_path="/work/repo",
        work_dir="/work/repo-wt",
        model="model-a",
        prompt_file="/tmp/p.md",
        tmux_session="agents",
        tmux_window="fix",
        remote=True,
        spawned_at="2026-01-01T00:00:00Z",
    )
    fields.update(over)
    return SpawnRecord(**fields)


def _result(launched: bool = False) -> spawn.SpawnResult:
    return spawn.SpawnResult(
        name="grp/claude-fix", client="claude", label="fix", repo_id=None,
        repo_path="/work/repo", work_dir="/work/repo-wt", tmux_session="agents",
        tmux_window="fix", command="run it", launched=launched,
    )


@pytest.fixture
def seen(storage, monkeypatch):
    storage.record_spawn(_record())
    calls: dict = {}
    monkeypatch.setattr(
        spawn, "find_resumable",
        lambda client, **kw: calls.update(find=(client, kw)) or ResumableSession("sid-1", Path("/t/s.jsonl"), 1),
    )

    def fake_resume(request, **kw):
        calls["request"] = request
        return _result()

    monkeypatch.setattr(spawn, "resume", fake_resume)
    return calls


def _run(*argv: str) -> int:
    return cli.main(["resume", "grp/claude-fix", "--prompt-file", "/tmp/next.md", *argv])


def test_unknown_address_returns_1(storage, capsys):
    assert cli.main(["resume", "nope/x", "--prompt-file", "f"]) == 1
    assert "is not a spawned session" in capsys.readouterr().err


def test_no_resumable_session_returns_1(seen, monkeypatch, capsys):
    monkeypatch.setattr(spawn, "find_resumable", lambda client, **kw: None)
    assert _run() == 1
    assert "no resumable claude session found" in capsys.readouterr().err
    assert "request" not in seen


def test_request_is_built_from_the_record(seen, capsys):
    assert _run() == 0
    req = seen["request"]
    assert (req.address, req.client, req.label) == ("grp/claude-fix", "claude", "fix")
    assert str(req.work_dir) == "/work/repo-wt"
    assert str(req.repo_path) == "/work/repo"
    assert str(req.prompt_file) == "/tmp/next.md"
    assert (req.session_id, req.model, req.remote, req.dry_run) == (
        "sid-1", "model-a", True, False,
    )
    assert seen["find"][1]["label"] == "fix"
    assert "would launch" in capsys.readouterr().out


def test_model_flag_overrides_the_recorded_model(seen):
    assert _run("--model", "model-b", "--no-remote") == 0
    assert seen["request"].model == "model-b"
    assert seen["request"].remote is False


def test_dry_run_json_prints_the_result(seen, capsys):
    assert _run("--dry-run", "--json") == 0
    assert seen["request"].dry_run is True
    assert json.loads(capsys.readouterr().out)["name"] == "grp/claude-fix"


def test_spawn_error_maps_to_its_exit_code(seen, monkeypatch, capsys):
    def boom(request, **kw):
        raise spawn.SpawnError("no tmux", exit_code=3, hint="install tmux")

    monkeypatch.setattr(spawn, "resume", boom)
    assert _run() == 3
    captured = capsys.readouterr()
    assert "no tmux" in captured.err
    assert "install tmux" in captured.out

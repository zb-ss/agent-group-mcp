"""The dispatcher lifecycle: start, stop, status, with a recording fake runner."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from agent_bus import authority, config
from agent_bus.dispatch import dispatcher, settings
from agent_bus.dispatch.store import DispatchStore
from agent_bus.storage import Storage

TOKEN_ENV = "AGENT_BUS_AUTHORITY_TOKEN"


class FakeRun:
    """Answers git and tmux; records every argv and env."""

    def __init__(self, *, tmux_fails=False, windows=()):
        self.calls: list[tuple[list[str], dict]] = []
        self.tmux_fails = tmux_fails
        self.windows = list(windows)  # (id, name) pairs
        self.sessions: set[str] = set()

    def __call__(self, argv, *, timeout, env=None, cwd=None):
        self.calls.append((list(argv), {"env": env}))
        out, rc, err = "", 0, ""
        if argv[:2] == ["git", "init"]:
            Path(argv[-1], ".git").mkdir(parents=True)
        elif "--git-common-dir" in argv:
            out = ".git\n"
        elif argv[:1] == ["tmux"] and argv[1] == "has-session":
            rc = 0 if argv[3].lstrip("=") in self.sessions else 1
        elif argv[:1] == ["tmux"] and argv[1] == "list-windows":
            out = "".join(f"{i}\t{n}\n" for i, n in self.windows)
        elif argv[:1] == ["tmux"] and argv[1] in ("new-session", "new-window"):
            if self.tmux_fails:
                rc, err = 1, "no server running"
        return subprocess.CompletedProcess(argv, rc, out, err)

    def tmux(self, verb):
        return [a for a, _ in self.calls if a[0] == "tmux" and a[1] == verb]


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BUS_DB", str(tmp_path / "state" / "bus.db"))
    monkeypatch.setenv("AGENT_BUS_AUDIT_LOG", str(tmp_path / "state" / "audit.log"))
    (tmp_path / "state").mkdir()
    monkeypatch.setattr("agent_bus.spawn.tmux.available", lambda _bin: True)
    c = config.defaults()
    c["repos"]["roots"] = []
    return c


@pytest.fixture
def storage(cfg):
    return Storage()


def _snapshot(root: Path) -> dict:
    return {
        str(p.relative_to(root)): (p.read_bytes() if p.is_file() else None)
        for p in sorted(root.rglob("*"))
    }


def test_start_prepares_registers_and_spawns(cfg, storage, tmp_path):
    run = FakeRun()
    result = dispatcher.start(cfg, storage=storage, run=run, env={"PATH": "/usr/bin"})
    work_dir = settings.dispatcher_dir(cfg["dispatch"])
    assert result.launched and not result.already_running
    assert Path(result.work_dir) == work_dir
    assert run.calls[0][0][:3] == ["git", "init", "--quiet"]
    assert (work_dir / ".git").exists()
    assert (work_dir / ".agent-bus-name").read_text().strip() == cfg["dispatch"]["dispatcher"]["group"]
    assert storage.has_authority(authority.ROLE_DISPATCHER)
    record = storage.get_spawn(result.name)
    assert record.role == authority.ROLE_DISPATCHER
    assert result.tmux_window == cfg["dispatch"]["dispatcher"]["label"]


def test_token_never_leaves_its_file(cfg, storage):
    run = FakeRun()
    result = dispatcher.start(cfg, storage=storage, run=run, env={"PATH": "/usr/bin"})
    path = settings.dispatcher_token_path()
    token = path.read_text()
    assert token and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert str(path) in result.command and "rm -f" in result.command and "cat" in result.command
    assert TOKEN_ENV in result.command
    blob = json.dumps(result.to_dict()) + json.dumps(run.calls, default=str)
    assert token not in blob
    for _, meta in run.calls:
        assert token not in (meta["env"] or {}).values()
    db_bytes = (path.parent / "bus.db").read_bytes()
    assert token.encode() not in db_bytes
    assert authority.digest(token).encode() in db_bytes


def test_second_start_changes_nothing(cfg, storage):
    run = FakeRun()
    first = dispatcher.start(cfg, storage=storage, run=run, env={})
    before = len(run.calls)
    token = settings.dispatcher_token_path().read_text()
    again = dispatcher.start(cfg, storage=storage, run=run, env={})
    assert again.already_running and again.name == first.name
    assert len(run.calls) == before
    assert settings.dispatcher_token_path().read_text() == token


def test_folder_inside_a_repo_is_refused(cfg, storage, tmp_path):
    repo = tmp_path / "somerepo"
    (repo / ".git").mkdir(parents=True)
    cfg["dispatch"]["dispatcher"]["dir"] = str(repo / "dispatcher")
    run = FakeRun()
    with pytest.raises(dispatcher.DispatcherError, match="outside"):
        dispatcher.start(cfg, storage=storage, run=run, env={})
    assert not run.calls and not (repo / "dispatcher").exists()


def test_spawn_failure_cleans_up_the_token(cfg, storage):
    run = FakeRun(tmux_fails=True)
    with pytest.raises(dispatcher.DispatcherError):
        dispatcher.start(cfg, storage=storage, run=run, env={})
    assert not settings.dispatcher_token_path().exists()
    assert not storage.has_authority(authority.ROLE_DISPATCHER)


def test_dry_run_on_an_unprepared_folder_writes_nothing(cfg, storage, tmp_path):
    storage.init_schema()
    root = tmp_path / "state"
    before = _snapshot(root)
    run = FakeRun()
    result = dispatcher.start(cfg, storage=storage, run=run, env={}, dry_run=True)
    assert not result.launched
    assert any(n.startswith("would prepare") for n in result.notes)
    assert _snapshot(root) == before
    assert not run.calls


def test_dry_run_on_a_prepared_folder_shows_only_the_token_path(cfg, storage, tmp_path):
    dispatcher.start(cfg, storage=storage, run=FakeRun(), env={})
    dispatcher.stop(cfg, storage=storage, run=FakeRun())
    root = tmp_path / "state"
    before = _snapshot(root)
    run = FakeRun()
    result = dispatcher.start(cfg, storage=storage, run=run, env={}, dry_run=True)
    assert not result.already_running and not result.launched and str(settings.dispatcher_token_path()) in result.command
    assert not run.tmux("new-session") and not run.tmux("new-window")
    assert _snapshot(root) == before


def test_stop_kills_revokes_and_removes(cfg, storage):
    dispatcher.start(cfg, storage=storage, run=FakeRun(), env={})
    label = cfg["dispatch"]["dispatcher"]["label"]
    run = FakeRun(windows=[("@4", label), ("@5", "other")])
    assert dispatcher.stop(cfg, storage=storage, run=run) == 1
    assert [a[-1] for a in run.tmux("kill-window")] == ["@4"]
    assert not storage.has_authority(authority.ROLE_DISPATCHER)
    assert not settings.dispatcher_token_path().exists()


def test_stop_without_a_window_is_not_an_error(cfg, storage):
    assert dispatcher.stop(cfg, storage=storage, run=FakeRun()) == 0


def test_status_counts_pending_candidates(cfg, storage):
    store = DispatchStore()
    idle = dispatcher.status(cfg, storage=storage, store=store)
    assert not idle.running and not idle.authority and idle.pending_candidates == 0
    first = dispatcher.start(cfg, storage=storage, run=FakeRun(), env={})
    live = dispatcher.status(cfg, storage=storage, store=store)
    assert live.running and live.name == first.name and live.authority
    assert live.to_dict()["pending_candidates"] == 0


def test_a_bad_private_playbook_is_a_dispatcher_error_not_a_traceback(cfg, storage, tmp_path):
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "dispatcher.md").write_text("costs $5 and $HOME")
    cfg["dispatch"]["templates_dir"] = str(templates)
    with pytest.raises(dispatcher.DispatcherError, match="could not be prepared"):
        dispatcher.start(cfg, storage=storage, run=FakeRun(), env={})
    assert not storage.has_authority(authority.ROLE_DISPATCHER)
    assert not settings.dispatcher_token_path().exists()


def test_playbook_error_lists_the_dispatcher_placeholders(cfg, storage, tmp_path):
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "dispatcher.md").write_text("$nope")
    cfg["dispatch"]["templates_dir"] = str(templates)
    with pytest.raises(dispatcher.DispatcherError, match="soft_limit") as caught:
        dispatcher.start(cfg, storage=storage, run=FakeRun(), env={})
    assert "task_url" not in str(caught.value)


def test_keep_alive_never_stops_the_run_on_any_error(cfg, storage, monkeypatch, capsys):
    from agent_bus.dispatch import cli

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(dispatcher, "start", boom)
    monkeypatch.setattr(dispatcher, "find_row", lambda _rows: None)
    cli._keep_dispatcher_alive(cfg, storage)
    assert "not kept alive" in capsys.readouterr().err

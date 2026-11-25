"""Spawning a session in tmux: command rendering, launch, registration.

No real tmux, git clone or client ever runs: a recording fake stands in for
the command runner."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_bus import clients, config, server, spawn, status, tmux
from agent_bus.spawn import (
    EXIT_CAP_REACHED, EXIT_FAILED, EXIT_USAGE, SpawnError, SpawnRequest,
)
from agent_bus.storage import STATE_DONE, STATE_STARTING, TIMESTAMP_FORMAT, SpawnRecord

NOW = datetime(2030, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
URL = "git@github.com:example-org/repo-a.git"
GOLDEN = (
    "env -u CLAUDECODE -u CLAUDE_CODE_CHILD_SESSION -u CLAUDE_CODE_SESSION_ID "
    "-u CLAUDE_PID -u CLAUDE_CODE_MESSAGING_SOCKET -u CLAUDE_CODE_MESSAGING_TOKEN "
    "-u CLAUDE_CODE_CHILD_NOTES_DIR -u CLAUDE_CODE_SESSION_ATTENDED "
    "-u CLAUDE_CODE_ENTRYPOINT -u CLAUDE_EFFORT -u CLAUDE_PLUGIN_DATA "
    "AGENT_BUS_SESSION=fix-login claude -n fix-login --permission-mode auto "
    '--remote-control fix-login "$(cat /tmp/p.txt)"; exec $SHELL -l'
)
LOCAL_FILE = "local.cfg"


class FakeRun:
    """Records every call and answers like git and tmux would."""

    def __init__(self, *, sessions=(), tmux_fails=False, url=URL):
        self.calls: list[tuple[list[str], dict]] = []
        self.sessions = set(sessions)
        self.tmux_fails = tmux_fails
        self.url = url
        # checkouts that are worktrees: path -> the git dir they report
        self.common_dirs: dict[str, str] = {}

    def __call__(self, argv, *, timeout, env=None, cwd=None):
        self.calls.append((list(argv), {"env": env, "timeout": timeout}))
        out, rc, err = "", 0, ""
        if argv[:2] == ["git", "clone"]:
            Path(argv[-1], ".git").mkdir(parents=True)
        elif "worktree" in argv:
            Path(argv[argv.index("--") + 1]).mkdir(parents=True)
        elif "--git-common-dir" in argv:
            out = self.common_dirs.get(argv[2], ".git") + "\n"
        elif "get-url" in argv:
            out = self.url + "\n"
        elif argv[1] == "has-session":
            rc = 0 if argv[3].lstrip("=") in self.sessions else 1
        elif argv[1] in ("new-session", "new-window") and self.tmux_fails:
            rc, err = 1, "no server running"
        return subprocess.CompletedProcess(argv, rc, out, err)

    def argvs(self, *head):
        return [a for a, _ in self.calls if tuple(a[: len(head)]) == head]

    def tmux_calls(self):
        return [(a, m) for a, m in self.calls if a[0] == "tmux"]


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "code" / "repo-a"
    (path / ".git").mkdir(parents=True)
    return path


@pytest.fixture
def cfg(tmp_path, repo):
    c = config.defaults()
    c["repos"]["roots"] = [str(tmp_path / "code")]
    c["repos"]["clone_root"] = str(tmp_path / "clones")
    c["spawn"]["worktree_root"] = str(tmp_path / "worktrees")
    return c


@pytest.fixture
def prompt(tmp_path):
    p = tmp_path / "prompt.txt"
    p.write_text("do the thing")
    return p


@pytest.fixture(autouse=True)
def tmux_present(monkeypatch):
    monkeypatch.setattr(tmux, "available", lambda _bin: True)


def _env(**extra):
    return {"PATH": "/usr/bin", "CLAUDECODE": "1", "CLAUDE_EFFORT": "high", **extra}


def _go(storage, cfg, prompt, fake=None, *, env=None, now=NOW, **fields):
    fields = {"repo": "repo-a", "client": "claude", "name": "fix-login", **fields}
    fake = fake or FakeRun()
    result = spawn.spawn(
        SpawnRequest(prompt_file=prompt, **fields),
        storage=storage, cfg=cfg, run=fake, env=_env() if env is None else env, now=now,
    )
    return result, fake


def _fail(storage, cfg, prompt, fake=None, **fields) -> SpawnError:
    with pytest.raises(SpawnError) as err:
        _go(storage, cfg, prompt, fake, **fields)
    return err.value


# ------------------------------ rendering -----------------------------------


def _render(client="claude", *, model=None, remote=True, name="fix-login",
            prompt_file="/tmp/p.txt", cfg=None, set_env=None):
    cfg = cfg or config.defaults()
    sc = cfg["spawn"]
    values = {"name": name, "label": name, "address": "x", "repo": "r",
              "dir": "/d", "prompt_file": prompt_file}
    if model:
        values["model"] = model
    return spawn.render_command(
        sc["clients"][client], values=values,
        prompt_file=Path(prompt_file), model=model, remote=remote,
        strip_env=sc["strip_env"], set_env=set_env or {"AGENT_BUS_SESSION": name},
        after_exit=sc["after_exit"], client=client,
    )


def test_claude_command_is_exactly_the_prototype_line():
    assert _render() == GOLDEN


def test_claude_model_and_no_remote():
    line = _render(model="m1", remote=False)
    assert "--model m1" in line
    assert "--remote-control" not in line


@pytest.mark.parametrize(
    "client, model, tail",
    [
        ("codex", None, 'codex "$(cat /tmp/p.txt)"; exec'),
        ("codex", "m1", 'codex -m m1 "$(cat /tmp/p.txt)"; exec'),
        ("agy", None, 'agy --mode auto --remote-control -i "$(cat /tmp/p.txt)"; exec'),
        ("agy", "m1", 'agy --mode auto --model m1 --remote-control -i "$(cat /tmp/p.txt)"; exec'),
        ("opencode", None, 'opencode --standalone --auto --prompt "$(cat /tmp/p.txt)"; exec'),
    ],
)
def test_other_clients(client, model, tail):
    assert f" AGENT_BUS_SESSION=fix-login {tail}" in _render(client, model=model)


def test_name_and_prompt_path_with_spaces_and_quotes_are_quoted():
    line = _render(name="a b'c", prompt_file="/tmp/my 'p'.txt")
    assert "-n 'a b'\"'\"'c'" in line
    assert '"$(cat \'/tmp/my \'"\'"\'p\'"\'"\'.txt\')"' in line


def test_unknown_placeholder_is_a_config_error():
    cfg = config.defaults()
    cfg["spawn"]["clients"]["codex"]["command"] = ["codex", "{nope}"]
    with pytest.raises(config.ConfigError, match="codex"):
        _render("codex", cfg=cfg)


def test_fragment_that_is_not_a_list_is_a_config_error():
    cfg = config.defaults()
    cfg["spawn"]["clients"]["codex"]["command"] = "codex"
    with pytest.raises(config.ConfigError, match="spawn.clients.codex.command"):
        _render("codex", cfg=cfg)


# ------------------------------- launch -------------------------------------


def test_new_session_when_missing(storage, cfg, prompt, repo):
    result, fake = _go(storage, cfg, prompt)
    assert fake.tmux_calls()[1][0] == [
        "tmux", "new-session", "-d", "-s", "agents", "-n", "fix-login",
        "-c", str(repo), result.command,
    ]
    assert result.launched and result.command.startswith("env -u CLAUDECODE")


def test_new_window_when_session_exists(storage, cfg, prompt, repo):
    result, fake = _go(storage, cfg, prompt, FakeRun(sessions={"agents"}))
    assert fake.tmux_calls()[1][0] == [
        "tmux", "new-window", "-d", "-t", "=agents:", "-n", "fix-login",
        "-c", str(repo), result.command,
    ]


def test_tmux_environment_lacks_stripped_variables(storage, cfg, prompt):
    _, fake = _go(storage, cfg, prompt)
    assert fake.tmux_calls()[1][1]["env"] == {"PATH": "/usr/bin"}


def test_caller_session_identity_never_reaches_tmux(storage, cfg, prompt):
    # A spawned session that spawns again outside tmux must not hand its own
    # label, number or token to a tmux server this call creates.
    caller = _env(AGENT_BUS_SESSION="parent", AGENT_BUS_INSTANCE="3",
                  AGENT_BUS_SPAWN_TOKEN="2026-01-01T00:00:00Z")
    result, fake = _go(storage, cfg, prompt, env=caller)
    assert fake.tmux_calls()[1][1]["env"] == {"PATH": "/usr/bin"}
    assert "AGENT_BUS_SESSION=fix-login AGENT_BUS_SPAWN_TOKEN=" in result.command
    assert "parent" not in result.command


def test_bus_paths_travel_inside_the_command(storage, cfg, prompt):
    result, _ = _go(storage, cfg, prompt, env=_env(AGENT_BUS_DB="/x/bus.db"))
    assert "AGENT_BUS_DB=/x/bus.db claude" in result.command
    assert "AGENT_BUS_SESSION=fix-login AGENT_BUS_SPAWN_TOKEN=" in result.command
    assert "AGENT_BUS_AUDIT_LOG" not in result.command


# ------------------------------- usage errors -------------------------------


def test_unknown_client(storage, cfg, prompt):
    err = _fail(storage, cfg, prompt, client="nope")
    assert err.exit_code == EXIT_USAGE and "claude" in str(err)


def test_bad_name(storage, cfg, prompt):
    assert _fail(storage, cfg, prompt, name="123").exit_code == EXIT_USAGE


def test_missing_prompt_file(storage, cfg, tmp_path):
    assert _fail(storage, cfg, tmp_path / "none.txt").exit_code == EXIT_USAGE


def test_model_without_model_fragment(storage, cfg, prompt):
    err = _fail(storage, cfg, prompt, client="opencode", model="m1")
    assert err.exit_code == EXIT_USAGE
    assert "spawn.clients.opencode.model" in str(err)


# ------------------------------ missing clone -------------------------------


def test_missing_clone_without_flag_prints_clone_command(storage, cfg, prompt, tmp_path):
    cfg["repos"]["roots"] = []
    fake = FakeRun()
    err = _fail(storage, cfg, prompt, fake, repo=URL)
    assert err.exit_code == EXIT_FAILED
    assert err.hint == f"git clone -- {URL} {tmp_path / 'clones' / 'repo-a'}"
    assert not fake.argvs("git", "clone")


def test_missing_clone_with_flag_clones_then_continues(storage, cfg, prompt, tmp_path):
    cfg["repos"]["roots"] = []
    result, fake = _go(storage, cfg, prompt, repo=URL, clone=True)
    dest = tmp_path / "clones" / "repo-a"
    (argv,) = fake.argvs("git", "clone")
    # cloned beside the destination, renamed into place only on success
    assert argv[:4] == ["git", "clone", "--", URL]
    assert Path(argv[4]).parent == dest.parent and Path(argv[4]) != dest
    assert dest.is_dir() and [p.name for p in dest.parent.iterdir()] == ["repo-a"]
    assert result.repo_path == str(dest)
    assert result.repo_id == "github.com/example-org/repo-a"
    assert result.launched


def test_fresh_clone_is_wired_for_the_client(storage, cfg, prompt, tmp_path):
    cfg["repos"]["roots"] = []
    result, _ = _go(storage, cfg, prompt, repo=URL, clone=True)
    dest = tmp_path / "clones" / "repo-a"
    assert (dest / ".mcp.json").exists()
    assert any("wired the new clone" in n for n in result.notes)


def test_clone_without_any_root_names_the_config_key(storage, cfg, prompt):
    cfg["repos"]["roots"] = []
    cfg["repos"]["clone_root"] = None
    err = _fail(storage, cfg, prompt, repo=URL, clone=True)
    assert err.exit_code == EXIT_USAGE and "repos.clone_root" in str(err)


def test_clone_of_an_alias_needs_a_url(storage, cfg, prompt):
    err = _fail(storage, cfg, prompt, repo="unknown-alias", clone=True)
    assert err.exit_code == EXIT_FAILED and "--repo" in str(err)


# --------------------------------- cap --------------------------------------


def _record(storage, address, *, state=STATE_STARTING, repo="/code/other"):
    storage.ensure_agent(address, repo)
    storage.set_state(address, state)
    storage.record_spawn(SpawnRecord(
        name=address, client="claude", repo_id=None, repo_path=repo, work_dir=repo,
        model=None, prompt_file="/p", tmux_session="agents",
        tmux_window=address.split("-")[-1], remote=True,
        spawned_at=NOW.strftime(TIMESTAMP_FORMAT),
    ))


def _fill(storage, count, **kw):
    for i in range(count):
        _record(storage, f"other/claude-job-{chr(97 + i)}", **kw)


def test_cap_reached_launches_nothing(storage, cfg, prompt):
    _fill(storage, 5)
    fake = FakeRun()
    err = _fail(storage, cfg, prompt, fake)
    assert err.exit_code == EXIT_CAP_REACHED
    assert "spawn.max_sessions" in str(err) and "other/claude-job-a" in str(err)
    assert not fake.tmux_calls()


def test_higher_cap_lets_it_through(storage, cfg, prompt):
    _fill(storage, 5)
    cfg["spawn"]["max_sessions"] = 6
    assert _go(storage, cfg, prompt)[0].launched


def test_done_session_does_not_count(storage, cfg, prompt):
    _fill(storage, 4)
    _record(storage, "other/claude-job-z", state=STATE_DONE)
    assert _go(storage, cfg, prompt)[0].launched


def test_active_address_is_refused(storage, cfg, prompt):
    _go(storage, cfg, prompt)
    err = _fail(storage, cfg, prompt)
    assert err.exit_code == EXIT_FAILED and "already running" in str(err)


def test_live_done_session_still_holds_its_address(storage, cfg, prompt, monkeypatch):
    _go(storage, cfg, prompt)
    storage.set_state("repo-a/claude-fix-login", STATE_DONE)
    row = type("Row", (), {"name": "repo-a/claude-fix-login", "live": True,
                           "state": STATE_DONE, "spawned": True})()
    monkeypatch.setattr(status, "collect", lambda *a, **k: [row])
    err = _fail(storage, cfg, prompt)
    assert err.exit_code == EXIT_FAILED and "already running" in str(err)


def test_cap_refusal_happens_before_any_clone(storage, cfg, prompt):
    cfg["repos"]["roots"] = []
    _fill(storage, 5)
    fake = FakeRun()
    err = _fail(storage, cfg, prompt, fake, repo=URL, clone=True)
    assert err.exit_code == EXIT_CAP_REACHED
    assert not fake.argvs("git", "clone")


def test_clone_left_by_a_refused_spawn_is_reused(storage, cfg, prompt, tmp_path):
    cfg["repos"]["roots"] = []
    dest = tmp_path / "clones" / "repo-a"
    (dest / ".git").mkdir(parents=True)
    result, fake = _go(storage, cfg, prompt, repo=URL, clone=True)
    assert not fake.argvs("git", "clone")
    assert result.repo_path == str(dest)


def test_clone_destination_of_another_repo_falls_back_to_owner_name(storage, cfg, prompt, tmp_path):
    cfg["repos"]["roots"] = []
    (tmp_path / "clones" / "repo-a" / ".git").mkdir(parents=True)
    fake = FakeRun(url="git@github.com:example-org/other.git")
    result, _ = _go(storage, cfg, prompt, fake, repo=URL, clone=True)
    # the other repo keeps its folder; this one is cloned beside it
    assert result.repo_path == str(tmp_path / "clones" / "example-org-repo-a")
    assert any("belongs to another repo" in n for n in result.notes)


@pytest.mark.parametrize("repo", [
    "--upload-pack=touch x;://a/h:r", "-c.example.com/o/r", "ext::sh -c id/a/b",
])
def test_option_like_repo_never_reaches_git(storage, cfg, prompt, repo):
    cfg["repos"]["roots"] = []
    fake = FakeRun()
    err = _fail(storage, cfg, prompt, fake, repo=repo, clone=True)
    assert not fake.argvs("git", "clone")
    assert err.exit_code == EXIT_FAILED


def test_worktree_base_ref_cannot_be_an_option(storage, cfg, prompt):
    err = _fail(storage, cfg, prompt, worktree="--force")
    assert err.exit_code == EXIT_USAGE


def test_worktree_session_points_hooks_at_the_clone(storage, cfg, prompt, repo):
    result, _fake = _go(storage, cfg, prompt, worktree="origin/main", dry_run=True)
    assert f"AGENT_BUS_REPO={repo}" in result.command


# ------------------------------- worktree -----------------------------------


def _wire(repo):
    clients.wiring("claude").apply(repo, name="repo-a/claude", bin_path="agent-bus")


def test_worktree_is_added_and_linked(storage, cfg, prompt, repo, tmp_path):
    _wire(repo)
    (repo / LOCAL_FILE).write_text("x")
    cfg["spawn"]["carry_over"] = [LOCAL_FILE, "missing-file"]
    result, fake = _go(storage, cfg, prompt, worktree="origin/main")
    target = tmp_path / "worktrees" / "repo-a" / "fix-login"
    assert [
        "git", "-C", str(repo), "worktree", "add", "--detach", "--", str(target), "origin/main",
    ] in fake.argvs("git", "-C")
    assert result.work_dir == str(target)
    for rel in (".mcp.json", ".claude/settings.json", LOCAL_FILE):
        assert (target / rel).is_symlink()
        assert (target / rel).resolve() == (repo / rel).resolve()
    assert not (target / "missing-file").is_symlink()
    assert fake.tmux_calls()[1][0][-2] == str(target)


def test_existing_destination_is_untouched(storage, cfg, prompt, repo, tmp_path):
    (repo / LOCAL_FILE).write_text("x")
    cfg["spawn"]["carry_over"] = [LOCAL_FILE]
    result, _ = _go(storage, cfg, prompt, worktree="main")
    target = Path(result.work_dir)
    # a second session reusing the directory leaves what is there alone
    (target / LOCAL_FILE).unlink()
    (target / LOCAL_FILE).write_text("mine")
    _go(storage, cfg, prompt, name="other-job", worktree="main")
    assert (target / LOCAL_FILE).read_text() == "mine"
    assert not (target / LOCAL_FILE).is_symlink()


def test_existing_worktree_is_reused(storage, cfg, prompt, repo, tmp_path):
    target = tmp_path / "worktrees" / "repo-a" / "fix-login"
    target.mkdir(parents=True)
    fake = FakeRun()
    fake.common_dirs[str(target)] = str(repo / ".git")
    result, fake = _go(storage, cfg, prompt, fake, worktree="main")
    assert not any("worktree" in a for a, _ in fake.calls)
    assert any("reusing" in n for n in result.notes)


def test_carry_over_cannot_leave_the_repo(storage, cfg, prompt):
    cfg["spawn"]["carry_over"] = ["../outside"]
    with pytest.raises(config.ConfigError, match="outside"):
        _go(storage, cfg, prompt, worktree="main")


def test_carry_over_cannot_be_absolute(storage, cfg, prompt):
    cfg["spawn"]["carry_over"] = ["/etc/passwd"]
    with pytest.raises(config.ConfigError):
        _go(storage, cfg, prompt, worktree="main")


# --------------------------- worktree_links ----------------------------------


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    return h


def _memory(home, folder):
    return home / ".claude" / "projects" / spawn.path_key(folder) / "memory"


def test_path_key_replaces_non_alphanumerics():
    key = spawn.path_key(Path("/srv/code/repo-a/.wt/fix_login"))
    assert key.endswith("-srv-code-repo-a--wt-fix-login")


def test_memory_is_linked_into_the_worktree(storage, cfg, prompt, repo, home):
    source = _memory(home, repo)
    source.mkdir(parents=True)
    result, _ = _go(storage, cfg, prompt, worktree="main")
    target = _memory(home, Path(result.work_dir))
    assert target.is_symlink() and target.resolve() == source.resolve()
    assert result.to_dict()["links"] == [
        {"source": str(source), "target": str(target), "created": True}
    ]
    assert any(n.startswith("linked ") for n in result.notes)


def test_no_worktree_no_link(storage, cfg, prompt, repo, home):
    _memory(home, repo).mkdir(parents=True)
    result, _ = _go(storage, cfg, prompt)
    assert result.links == []


def test_missing_source_is_noted_not_linked(storage, cfg, prompt, repo, home):
    result, _ = _go(storage, cfg, prompt, worktree="main")
    target = _memory(home, Path(result.work_dir))
    assert not target.exists() and not target.is_symlink()
    assert any("does not exist" in n for n in result.notes)


def test_existing_target_is_untouched(storage, cfg, prompt, repo, home, tmp_path):
    _memory(home, repo).mkdir(parents=True)
    target = _memory(home, tmp_path / "worktrees" / "repo-a" / "fix-login")
    target.mkdir(parents=True)
    (target / "keep.md").write_text("mine")
    result, _ = _go(storage, cfg, prompt, worktree="main")
    assert not target.is_symlink() and (target / "keep.md").read_text() == "mine"
    assert result.links[0]["created"] is False
    assert any("already exists" in n for n in result.notes)


def test_dry_run_plans_the_link_only(storage, cfg, prompt, repo, home):
    _memory(home, repo).mkdir(parents=True)
    result, _ = _go(storage, cfg, prompt, worktree="main", dry_run=True)
    assert any(n.startswith("would link ") for n in result.notes)
    assert not Path(result.links[0]["target"]).exists()
    assert result.links[0]["created"] is False


def test_empty_worktree_links_turns_it_off(storage, cfg, prompt, repo, home):
    _memory(home, repo).mkdir(parents=True)
    cfg["spawn"]["clients"]["claude"]["worktree_links"] = []
    result, _ = _go(storage, cfg, prompt, worktree="main")
    assert result.links == []
    assert not _memory(home, Path(result.work_dir)).exists()


def test_unknown_placeholder_names_client_and_key(storage, cfg, prompt, repo, home):
    cfg["spawn"]["clients"]["claude"]["worktree_links"] = [
        {"source": "~/{nope}", "target": "~/x"}
    ]
    with pytest.raises(config.ConfigError, match="claude.worktree_links"):
        _go(storage, cfg, prompt, worktree="main")


# ------------------------- tmux missing, dry run -----------------------------


def test_tmux_missing_writes_nothing_and_prints_the_command(
    storage, cfg, prompt, repo, monkeypatch
):
    monkeypatch.setattr(tmux, "available", lambda _bin: False)
    err = _fail(storage, cfg, prompt)
    assert err.exit_code == EXIT_FAILED and "tmux" in str(err) and "spawn.tmux_bin" in str(err)
    assert err.hint.startswith(f"cd {repo} && env -u CLAUDECODE")
    assert storage.list_agents() == [] and storage.list_spawns() == []


def test_dry_run_changes_nothing(storage, cfg, prompt, tmp_path):
    before = storage.list_agents(), storage.list_spawns()
    result, fake = _go(storage, cfg, prompt, worktree="main", dry_run=True)
    assert not result.launched and result.command.endswith("exec $SHELL -l")
    assert not fake.tmux_calls() and not any("worktree" in a for a, _ in fake.calls)
    assert not (tmp_path / "worktrees").exists()
    assert (storage.list_agents(), storage.list_spawns()) == before
    assert storage.repo_ids() == {}


def test_dry_run_clone_runs_no_git_clone(storage, cfg, prompt, tmp_path):
    cfg["repos"]["roots"] = []
    result, fake = _go(storage, cfg, prompt, repo=URL, clone=True, dry_run=True)
    assert not fake.argvs("git", "clone") and not result.launched
    assert not (tmp_path / "clones").exists()
    assert storage.repo_ids() == {}


# ----------------------------- registration ----------------------------------


def test_tmux_failure_removes_record_and_state(storage, cfg, prompt):
    err = _fail(storage, cfg, prompt, FakeRun(tmux_fails=True))
    assert err.exit_code == EXIT_FAILED and "no server running" in str(err)
    assert storage.list_spawns() == []
    assert storage.get_agent("repo-a/claude-fix-login") is None
    assert storage.get_agent("repo-a/claude") is None


def test_registration_end_to_end(storage, cfg, prompt, repo, monkeypatch, fake_procs):
    _wire(repo)
    result, _ = _go(storage, cfg, prompt, now=None)
    assert result.name == "repo-a/claude-fix-login"
    assert storage.get_agent(result.name).state == STATE_STARTING
    record = storage.get_spawn(result.name)
    assert (record.tmux_session, record.tmux_window) == ("agents", "fix-login")

    monkeypatch.setenv("AGENT_BUS_NAME", "repo-a/claude")
    monkeypatch.setenv("AGENT_BUS_REPO", str(repo))
    monkeypatch.setenv("AGENT_BUS_SESSION", "fix-login")
    monkeypatch.setenv("AGENT_BUS_SPAWN_TOKEN", record.spawned_at)
    process, lineage = fake_procs.session()
    me = server.start(store=storage, process=process, lineage=lineage)
    assert me.name == result.name

    (row,) = status.collect(storage, grace_seconds=120)
    assert (row.name, row.live, row.spawned) == (result.name, True, True)
    assert (row.tmux_session, row.tmux_window) == ("agents", "fix-login")


def test_the_launched_command_carries_the_spawn_token(storage, cfg, prompt):
    result, _ = _go(storage, cfg, prompt)
    token = storage.get_spawn(result.name).spawned_at
    assert f"AGENT_BUS_SPAWN_TOKEN={token}" in result.command


def test_a_session_joining_after_the_grace_is_live_and_spawned_again(
    storage, cfg, prompt, repo, monkeypatch, fake_procs
):
    _wire(repo)
    long_ago = datetime.now(timezone.utc) - timedelta(seconds=300)
    result, _ = _go(storage, cfg, prompt, now=long_ago)
    grace = cfg["spawn"]["startup_grace_seconds"]
    (waiting,) = status.collect(storage, grace_seconds=grace, include_ended=True)
    assert waiting.state == "ended"  # a client stuck on a trust prompt

    monkeypatch.setenv("AGENT_BUS_NAME", "repo-a/claude")
    monkeypatch.setenv("AGENT_BUS_REPO", str(repo))
    monkeypatch.setenv("AGENT_BUS_SESSION", "fix-login")
    monkeypatch.setenv("AGENT_BUS_SPAWN_TOKEN", storage.get_spawn(result.name).spawned_at)
    process, lineage = fake_procs.session()
    server.start(store=storage, process=process, lineage=lineage)

    (row,) = status.collect(storage, grace_seconds=grace)
    assert (row.live, row.spawned, row.state) == (True, True, "running")
    assert (row.tmux_session, row.tmux_window) == ("agents", "fix-login")
    active = [r for r in status.collect(storage, grace_seconds=grace) if r.spawned and r.live]
    assert len(active) == 1  # it counts against spawn.max_sessions again


# ------------------------- failed launches and clones ------------------------


class _FailingClone(FakeRun):
    """A clone that leaves a partial directory behind and then fails."""

    def __call__(self, argv, **kw):
        if argv[:2] == ["git", "clone"]:
            self.calls.append((list(argv), {}))
            Path(argv[-1], ".git").mkdir(parents=True)
            return subprocess.CompletedProcess(argv, 128, "", "fatal: early EOF")
        return super().__call__(argv, **kw)


def test_failed_clone_leaves_nothing_behind(storage, cfg, prompt, tmp_path):
    cfg["repos"]["roots"] = []
    err = _fail(storage, cfg, prompt, _FailingClone(), repo=URL, clone=True)
    assert "early EOF" in str(err)
    assert list((tmp_path / "clones").iterdir()) == []


def test_timed_out_clone_leaves_nothing_behind(storage, cfg, prompt, tmp_path):
    cfg["repos"]["roots"] = []

    def runner(argv, **kw):
        if argv[:2] == ["git", "clone"]:
            Path(argv[-1], ".git").mkdir(parents=True)
            raise subprocess.TimeoutExpired(argv, 1)
        return FakeRun()(argv, **kw)

    err = _fail(storage, cfg, prompt, runner, repo=URL, clone=True)
    assert "git clone" in str(err)
    assert list((tmp_path / "clones").iterdir()) == []


class _NoHead(FakeRun):
    def __call__(self, argv, **kw):
        if "rev-parse" in argv:
            self.calls.append((list(argv), {}))
            return subprocess.CompletedProcess(argv, 128, "", "fatal: bad revision 'HEAD'")
        return super().__call__(argv, **kw)


def test_clone_without_head_is_refused_naming_the_directory(storage, cfg, prompt, tmp_path):
    cfg["repos"]["roots"] = []
    dest = tmp_path / "clones" / "repo-a"
    (dest / ".git").mkdir(parents=True)
    fake = _NoHead()
    err = _fail(storage, cfg, prompt, fake, repo=URL, clone=True)
    assert str(dest) in str(err) and "incomplete" in str(err)
    assert not fake.tmux_calls() and dest.exists()


def test_complete_clone_is_checked_with_rev_parse(storage, cfg, prompt, tmp_path):
    cfg["repos"]["roots"] = []
    (tmp_path / "clones" / "repo-a" / ".git").mkdir(parents=True)
    _, fake = _go(storage, cfg, prompt, repo=URL, clone=True)
    assert fake.argvs("git", "-C", str(tmp_path / "clones" / "repo-a"), "rev-parse", "--verify", "HEAD")


@pytest.mark.parametrize("url", [
    "https://host.example/x/.git.git", "https://host.example/x/.cargo",
])
def test_unsafe_clone_folder_never_reaches_git(storage, cfg, prompt, tmp_path, url):
    cfg["repos"]["roots"] = []
    fake = FakeRun()
    err = _fail(storage, cfg, prompt, fake, repo=url, clone=True)
    assert "folder name" in str(err)
    assert not fake.argvs("git", "clone") and not (tmp_path / "clones").exists()


def test_opted_out_repo_is_refused(storage, cfg, prompt, repo):
    (repo / ".agent-bus-ignore").write_text("")
    fake = FakeRun()
    err = _fail(storage, cfg, prompt, fake)
    assert ".agent-bus-ignore" in str(err)
    assert not fake.tmux_calls() and storage.list_agents() == []


def test_template_typo_fails_before_any_side_effect(storage, cfg, prompt, tmp_path):
    cfg["repos"]["roots"] = []
    cfg["spawn"]["clients"]["claude"]["command"] = ["claude", "{nmae}"]
    fake = FakeRun()
    with pytest.raises(config.ConfigError, match="nmae"):
        _go(storage, cfg, prompt, fake, repo=URL, clone=True, worktree="main")
    assert not fake.argvs("git", "clone") and not any("worktree" in a for a, _ in fake.calls)
    assert not (tmp_path / "clones").exists() and not (tmp_path / "worktrees").exists()


def test_null_prompt_fragment_fails_before_any_side_effect(storage, cfg, prompt, tmp_path):
    cfg["spawn"]["clients"]["claude"]["prompt"] = None
    with pytest.raises(config.ConfigError, match="prompt"):
        _go(storage, cfg, prompt, worktree="main")
    assert not (tmp_path / "worktrees").exists()


def test_link_typo_fails_before_the_worktree_exists(storage, cfg, prompt, tmp_path):
    cfg["spawn"]["clients"]["claude"]["worktree_links"] = [{"source": "~/{nope}", "target": "~/x"}]
    fake = FakeRun()
    with pytest.raises(config.ConfigError, match="worktree_links"):
        _go(storage, cfg, prompt, fake, worktree="main")
    assert not any("worktree" in a for a, _ in fake.calls)


def test_invalid_client_key_is_a_spawn_error(storage, cfg, prompt):
    cfg["spawn"]["clients"]["claude-yolo"] = dict(cfg["spawn"]["clients"]["claude"])
    err = _fail(storage, cfg, prompt, client="claude-yolo")
    assert err.exit_code == EXIT_USAGE and "claude-yolo" in str(err)


def test_filesystem_error_in_the_worktree_step_is_a_spawn_error(storage, cfg, prompt, tmp_path):
    (tmp_path / "worktrees").write_text("a file, not a directory")
    err = _fail(storage, cfg, prompt, worktree="main")
    assert "working directory" in str(err)
    assert storage.list_agents() == []


def test_spawn_error_carries_the_notes_so_far(storage, cfg, prompt, repo):
    err = _fail(storage, cfg, prompt, FakeRun(tmux_fails=True))
    assert any("not wired" in n for n in err.notes)


def test_failed_launch_restores_the_previous_record_and_state(storage, cfg, prompt):
    _go(storage, cfg, prompt)
    storage.set_state("repo-a/claude-fix-login", STATE_DONE)
    before = storage.get_spawn("repo-a/claude-fix-login")
    err = _fail(storage, cfg, prompt, FakeRun(tmux_fails=True))
    after = storage.get_agent("repo-a/claude-fix-login")
    assert (err.exit_code, after.state, storage.get_spawn(after.name)) == (
        EXIT_FAILED, STATE_DONE, before
    )


def test_tmux_start_directory_escapes_hash():
    for sessions in (set(), {"agents"}):
        fake = FakeRun(sessions=sessions)
        tmux.launch(
            "agents", "w", Path("/srv/code/#(touch x)/repo-a"), "true",
            env={}, tmux_bin="tmux", timeout=1, run=fake,
        )
        assert fake.tmux_calls()[-1][0][-2] == "/srv/code/##(touch x)/repo-a"


# ------------------------- worktrees and request checks ----------------------


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / ".git").mkdir(exist_ok=True)
    return path


def test_symlinked_carry_over_entry_is_carried(storage, cfg, prompt, repo, tmp_path):
    private = tmp_path / "private"
    private.mkdir()
    (private / "notes.md").write_text("n")
    (repo / "NOTES.md").symlink_to(private / "notes.md")
    cfg["spawn"]["carry_over"] = ["NOTES.md"]
    result, _ = _go(storage, cfg, prompt, worktree="main")
    assert (Path(result.work_dir) / "NOTES.md").is_symlink()


def test_symlinked_wiring_file_is_carried_and_never_blamed_on_carry_over(
    storage, cfg, prompt, repo, tmp_path
):
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "mcp.json").write_text("{}")
    (repo / ".mcp.json").symlink_to(tmp_path / "elsewhere" / "mcp.json")
    result, _ = _go(storage, cfg, prompt, worktree="main", dry_run=True)
    assert any("would create worktree" in n for n in result.notes)


def test_wiring_folder_outside_the_repo_names_the_wiring_file(storage, cfg, prompt, repo, tmp_path):
    (tmp_path / "outside").mkdir()
    (repo / ".claude").symlink_to(tmp_path / "outside")
    err = _fail(storage, cfg, prompt, worktree="main")
    assert "wiring file" in str(err) and "carry_over" not in str(err)


def test_carry_over_parent_part_is_refused_even_when_it_stays_inside(storage, cfg, prompt):
    cfg["spawn"]["carry_over"] = ["sub/../x"]
    with pytest.raises(config.ConfigError, match="carry_over"):
        _go(storage, cfg, prompt, worktree="main")


def test_tracked_settings_file_gets_the_hooks_through_local_settings(
    storage, cfg, prompt, repo, tmp_path
):
    _wire(repo)
    tracked = tmp_path / "worktrees" / "repo-a" / "fix-login" / ".claude" / "settings.json"

    class _Tracked(FakeRun):
        def __call__(self, argv, **kw):
            done = super().__call__(argv, **kw)
            if "worktree" in argv:  # the checkout carries a committed copy
                tracked.parent.mkdir(parents=True)
                tracked.write_text("{}")
            return done

    result, _ = _go(storage, cfg, prompt, _Tracked(), worktree="main")
    assert any("hooks added through .claude/settings.local.json" in n for n in result.notes)
    assert not [n for n in result.notes if "tracks it" in n]
    assert not tracked.is_symlink() and tracked.read_text() == "{}"
    local = tracked.with_name("settings.local.json")
    assert local.is_file() and not local.is_symlink()
    data = json.loads(local.read_text())
    assert data["enabledMcpjsonServers"] == ["agent-bus"]
    assert "UserPromptSubmit" in data["hooks"] and "Stop" in data["hooks"]
    assert "mcp__agent-bus__whoami" in data["permissions"]["allow"]
    assert "AGENT_BUS_NAME=repo-a/claude agent-bus hook-stop" in json.dumps(data["hooks"])


def test_tracked_mcp_file_is_still_noted(storage, cfg, prompt, repo, tmp_path):
    _wire(repo)
    tracked = tmp_path / "worktrees" / "repo-a" / "fix-login" / ".mcp.json"

    class _Tracked(FakeRun):
        def __call__(self, argv, **kw):
            done = super().__call__(argv, **kw)
            if "worktree" in argv:
                tracked.parent.mkdir(parents=True, exist_ok=True)
                tracked.write_text("{}")
            return done

    result, _ = _go(storage, cfg, prompt, _Tracked(), worktree="main")
    (note,) = [n for n in result.notes if "tracks it" in n]
    assert str(tracked) in note


def test_worktree_local_settings_start_from_the_clones_and_keep_its_keys(
    storage, cfg, prompt, repo, tmp_path
):
    _wire(repo)
    local = repo / ".claude" / "settings.local.json"
    local.write_text(json.dumps({"model": "m", "enabledMcpjsonServers": ["other"]}))
    result, _ = _go(storage, cfg, prompt, worktree="main")
    mine = Path(result.work_dir) / ".claude" / "settings.local.json"
    data = json.loads(mine.read_text())
    assert data["model"] == "m" and data["enabledMcpjsonServers"] == ["other", "agent-bus"]
    assert "hooks" not in data  # the carried-over settings.json already has them
    assert json.loads(local.read_text())["enabledMcpjsonServers"] == ["other"]  # clone untouched


def test_worktree_local_settings_replace_a_link_into_the_clone(storage, cfg, prompt, repo, tmp_path):
    _wire(repo)
    clone_local = repo / ".claude" / "settings.local.json"
    clone_local.write_text('{"model": "m"}')
    cfg["spawn"]["carry_over"] = [".claude/settings.local.json"]
    result, _ = _go(storage, cfg, prompt, worktree="main")
    mine = Path(result.work_dir) / ".claude" / "settings.local.json"
    assert mine.is_file() and not mine.is_symlink()
    assert json.loads(clone_local.read_text()) == {"model": "m"}  # the clone's file is untouched


def test_unreadable_worktree_local_settings_are_left_alone(storage, cfg, prompt, repo, tmp_path):
    _wire(repo)
    target = tmp_path / "worktrees" / "repo-a" / "fix-login"
    (target / ".claude").mkdir(parents=True)
    (target / ".claude" / "settings.local.json").write_text("{not json")
    fake = FakeRun()
    fake.common_dirs[str(target)] = str(repo / ".git")
    result, _ = _go(storage, cfg, prompt, fake, worktree="main")
    assert (target / ".claude" / "settings.local.json").read_text() == "{not json"
    assert any("not writing" in n for n in result.notes)


def test_dry_run_reports_the_local_settings_and_writes_nothing(storage, cfg, prompt, repo, tmp_path):
    _wire(repo)
    result, _ = _go(storage, cfg, prompt, worktree="main", dry_run=True)
    assert any("would write" in n and "settings.local.json" in n for n in result.notes)
    assert not (tmp_path / "worktrees").exists()


def test_local_settings_are_excluded_only_when_git_would_show_them(
    storage, cfg, prompt, repo, tmp_path
):
    _wire(repo)

    class _Visible(FakeRun):
        def __call__(self, argv, **kw):
            if "check-ignore" in argv:
                self.calls.append((list(argv), {}))
                return subprocess.CompletedProcess(argv, 1, "", "")
            return super().__call__(argv, **kw)

    _go(storage, cfg, prompt, _Visible(), worktree="main")
    exclude = (repo / ".git" / "info" / "exclude").read_text().splitlines()
    assert exclude == ["/.claude/settings.local.json"]


def test_local_settings_already_ignored_are_not_excluded_again(storage, cfg, prompt, repo):
    _wire(repo)
    _go(storage, cfg, prompt, worktree="main")  # the fake answers "ignored"
    assert not (repo / ".git" / "info" / "exclude").exists()


def test_tracked_wiring_file_is_noted_for_a_reused_worktree(storage, cfg, prompt, repo, tmp_path):
    _wire(repo)
    target = tmp_path / "worktrees" / "repo-a" / "fix-login"
    target.mkdir(parents=True)
    (target / ".mcp.json").write_text("{}")
    fake = FakeRun()
    fake.common_dirs[str(target)] = str(repo / ".git")
    result, _ = _go(storage, cfg, prompt, fake, worktree="main")
    assert any("tracks it" in n for n in result.notes)


# --------------------------- default worktree place --------------------------


def test_default_worktree_is_inside_the_clone_and_excluded(storage, cfg, prompt, repo):
    cfg["spawn"]["worktree_root"] = None
    result, fake = _go(storage, cfg, prompt, worktree="main")
    target = repo / ".worktrees" / "fix-login"
    assert result.work_dir == str(target)
    assert ["git", "-C", str(repo), "worktree", "add", "--detach", "--", str(target), "main"] in (
        fake.argvs("git", "-C")
    )
    assert (repo / ".git" / "info" / "exclude").read_text().splitlines() == ["/.worktrees/"]


def test_the_exclude_entry_is_added_once_and_keeps_other_lines(storage, cfg, prompt, repo):
    cfg["spawn"]["worktree_root"] = None
    exclude = repo / ".git" / "info" / "exclude"
    exclude.parent.mkdir()
    exclude.write_text("*.swp")  # no trailing newline
    _go(storage, cfg, prompt, worktree="main")
    _go(storage, cfg, prompt, name="other-job", worktree="main")
    assert exclude.read_text().splitlines() == ["*.swp", "/.worktrees/"]


def test_default_worktree_dry_run_writes_nothing(storage, cfg, prompt, repo):
    cfg["spawn"]["worktree_root"] = None
    result, _ = _go(storage, cfg, prompt, worktree="main", dry_run=True)
    assert result.work_dir == str(repo / ".worktrees" / "fix-login")
    assert not (repo / ".worktrees").exists() and not (repo / ".git" / "info").exists()


def test_default_worktree_folder_of_another_repo_is_refused(storage, cfg, prompt, repo):
    cfg["spawn"]["worktree_root"] = None
    target = repo / ".worktrees" / "fix-login"
    target.mkdir(parents=True)
    fake = FakeRun()
    fake.common_dirs[str(target)] = str(repo.parent / "elsewhere" / ".git")
    assert "not a git worktree" in str(_fail(storage, cfg, prompt, fake, worktree="main"))


def test_default_worktree_cannot_leave_the_clone_through_a_link(storage, cfg, prompt, repo, tmp_path):
    cfg["spawn"]["worktree_root"] = None
    (tmp_path / "outside").mkdir()
    (repo / ".worktrees").symlink_to(tmp_path / "outside")
    assert "resolves outside" in str(_fail(storage, cfg, prompt, worktree="main"))


def test_a_configured_root_keeps_the_group_layout_and_no_exclude(storage, cfg, prompt, repo, tmp_path):
    result, _ = _go(storage, cfg, prompt, worktree="main")
    assert result.work_dir == str(tmp_path / "worktrees" / "repo-a" / "fix-login")
    assert not (repo / ".git" / "info" / "exclude").exists()


# ----------------------------- user-level wiring -----------------------------


def test_codex_wired_for_the_user_gets_the_folder_name_note(
    storage, cfg, prompt, repo, tmp_path, monkeypatch
):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text('[mcp_servers.agent-bus]\ncommand = "agent-bus"\n')
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    result, _ = _go(storage, cfg, prompt, client="codex", dry_run=True)
    assert not any("is not wired" in n for n in result.notes)
    (note,) = [n for n in result.notes if "AGENT_BUS_SESSION" in n]
    assert "user account" in note and "will not link" in note


def test_codex_without_any_wiring_keeps_the_not_wired_note(
    storage, cfg, prompt, repo, tmp_path, monkeypatch
):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "empty"))
    result, _ = _go(storage, cfg, prompt, client="codex", dry_run=True)
    assert any("is not wired for codex" in n for n in result.notes)


def test_name_starting_with_a_dash_is_refused(storage, cfg, prompt):
    err = _fail(storage, cfg, prompt, name="--dangerously-skip-permissions")
    assert err.exit_code == EXIT_USAGE and "'-'" in str(err)


def test_model_starting_with_a_dash_is_refused(storage, cfg, prompt):
    err = _fail(storage, cfg, prompt, model="--dangerously-skip-permissions")
    assert err.exit_code == EXIT_USAGE and "'-'" in str(err)


def test_an_empty_prompt_file_is_refused(storage, cfg, tmp_path):
    path = tmp_path / "empty.txt"
    path.write_text("")
    fake = FakeRun()
    err = _fail(storage, cfg, path, fake)
    assert err.exit_code == EXIT_USAGE and "empty" in str(err)
    assert not fake.tmux_calls() and storage.list_agents() == []


@pytest.mark.parametrize("text", ["- fix the loop", "---\ntitle: x\n---", "--settings={}"])
def test_prompt_starting_with_a_dash_is_refused(storage, cfg, tmp_path, text):
    path = tmp_path / "dash.txt"
    path.write_text(text)
    fake = FakeRun()
    err = _fail(storage, cfg, path, fake)
    assert err.exit_code == EXIT_USAGE and "leading space" in str(err)
    assert not fake.tmux_calls() and storage.list_agents() == []


def test_prompt_with_a_leading_space_is_accepted(storage, cfg, tmp_path):
    path = tmp_path / "space.txt"
    path.write_text(" - fix the loop")
    assert _go(storage, cfg, path)[0].launched


def test_failed_respawn_keeps_an_old_done_session_hidden(storage, cfg, prompt):
    _go(storage, cfg, prompt)
    address = "repo-a/claude-fix-login"
    storage.set_state(address, STATE_DONE)
    with storage.connect() as conn:
        conn.execute(
            "UPDATE agents SET state_at = ?, last_seen = ? WHERE name = ?",
            ("2020-01-01T00:00:00Z", "2020-01-02T00:00:00Z", address),
        )
    before = storage.get_agent(address)
    _fail(storage, cfg, prompt, FakeRun(tmux_fails=True))
    after = storage.get_agent(address)
    assert (after.state, after.state_at, after.last_seen) == (
        STATE_DONE, "2020-01-01T00:00:00Z", "2020-01-02T00:00:00Z"
    )
    assert after == before


def test_clone_name_collision_with_two_taken_folders_names_both(storage, cfg, prompt, tmp_path):
    cfg["repos"]["roots"] = []
    for folder in ("repo-a", "example-org-repo-a"):
        (tmp_path / "clones" / folder / ".git").mkdir(parents=True)
    fake = FakeRun(url="git@github.com:example-org/other.git")
    err = _fail(storage, cfg, prompt, fake, repo=URL, clone=True)
    assert str(tmp_path / "clones" / "repo-a") in str(err)
    assert str(tmp_path / "clones" / "example-org-repo-a") in str(err)
    assert not fake.argvs("git", "clone")


def test_clone_fallback_folder_is_reused_when_it_is_the_repo(storage, cfg, prompt, tmp_path):
    cfg["repos"]["roots"] = []
    (tmp_path / "clones" / "repo-a" / ".git").mkdir(parents=True)
    (tmp_path / "clones" / "example-org-repo-a" / ".git").mkdir(parents=True)

    class _Two(FakeRun):
        def __call__(self, argv, **kw):
            if "get-url" in argv and "example-org-repo-a" not in argv[2]:
                self.calls.append((list(argv), {}))
                return subprocess.CompletedProcess(argv, 0, "git@github.com:example-org/x.git\n", "")
            return super().__call__(argv, **kw)

    result, fake = _go(storage, cfg, prompt, _Two(), repo=URL, clone=True)
    assert result.repo_path == str(tmp_path / "clones" / "example-org-repo-a")
    assert not fake.argvs("git", "clone")


def _wired_as(repo, name):
    (repo / ".mcp.json").write_text(
        '{"mcpServers": {"agent-bus": {"command": "agent-bus", "args": ["serve"],'
        f' "env": {{"AGENT_BUS_NAME": "{name}"}}}}}}}}'
    )


@pytest.mark.parametrize("name", ["../claude", "./claude", "Repo-A/claude"])
def test_wired_group_that_is_not_a_slug_never_becomes_a_path(
    storage, cfg, prompt, repo, tmp_path, name
):
    _wired_as(repo, name)
    fake = FakeRun()
    err = _fail(storage, cfg, prompt, fake, worktree="main")
    assert "plain name" in str(err)
    assert not any("worktree" in a for a, _ in fake.calls)
    assert not (tmp_path / "worktrees").exists() and storage.list_agents() == []


def test_wired_name_that_is_not_a_string_is_a_spawn_error(storage, cfg, prompt, repo):
    (repo / ".mcp.json").write_text(
        '{"mcpServers": {"agent-bus": {"env": {"AGENT_BUS_NAME": 5}}}}'
    )
    err = _fail(storage, cfg, prompt)
    assert "no client part" in str(err)


def test_worktree_folder_of_another_repo_is_refused(storage, cfg, prompt, repo, tmp_path):
    target = tmp_path / "worktrees" / "repo-a" / "fix-login"
    target.mkdir(parents=True)
    fake = FakeRun()
    fake.common_dirs[str(target)] = str(tmp_path / "other" / ".git")
    err = _fail(storage, cfg, prompt, fake, worktree="main")
    assert str(target) in str(err) and "not a git worktree" in str(err)
    assert not fake.tmux_calls() and storage.list_agents() == []


def test_carry_over_never_links_through_a_symlinked_folder(storage, cfg, prompt, repo, tmp_path):
    (repo / "tools").mkdir()
    (repo / "tools" / "local.cfg").write_text("x")
    cfg["spawn"]["carry_over"] = ["tools/local.cfg"]
    outside = tmp_path / "outside"
    outside.mkdir()

    class _LinkedFolder(FakeRun):
        def __call__(self, argv, **kw):
            done = super().__call__(argv, **kw)
            if "worktree" in argv:  # the ref tracks `tools` as a link out of the tree
                (Path(argv[argv.index("--") + 1]) / "tools").symlink_to(outside)
            return done

    result, _ = _go(storage, cfg, prompt, _LinkedFolder(), worktree="main")
    assert list(outside.iterdir()) == []
    assert any("outside the worktree" in n for n in result.notes)


def test_fresh_clone_with_a_linked_wiring_folder_is_not_wired(storage, cfg, prompt, tmp_path):
    cfg["repos"]["roots"] = []
    outside = tmp_path / "outside"
    outside.mkdir()

    class _Linked(FakeRun):
        def __call__(self, argv, **kw):
            done = super().__call__(argv, **kw)
            if argv[:2] == ["git", "clone"]:
                Path(argv[-1], ".claude").symlink_to(outside)
            return done

    result, _ = _go(storage, cfg, prompt, _Linked(), repo=URL, clone=True)
    assert list(outside.iterdir()) == []
    note = next(n for n in result.notes if "not wiring" in n)
    assert "agent-bus init" in note
    assert not (tmp_path / "clones" / "repo-a" / ".mcp.json").exists()

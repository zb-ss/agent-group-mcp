"""Bulk init: slug, scan, merge, idempotency, .agent-bus-{name,ignore}."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_bus import init_cmd


# --------------------------- slugify ---------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("acme.dev", "acme-dev"),
        ("acme", "acme"),
        ("My Cool Thing", "my-cool-thing"),
        ("a_b_c", "a-b-c"),
        ("UPPER", "upper"),
        ("a---b", "a-b"),
        ("___", "repo"),
        ("repo.with.lots.of.dots", "repo-with-lots-of-dots"),
        ("é-tienne!", "tienne"),
    ],
)
def test_slugify(raw, expected):
    assert init_cmd.slugify(raw) == expected


def test_slugify_truncates_long_names():
    out = init_cmd.slugify("a" * 200)
    assert len(out) <= init_cmd.MAX_NAME_LEN


# --------------------------- resolve_name ----------------------------


def test_resolve_name_uses_basename(tmp_path):
    repo = tmp_path / "acme.dev"
    repo.mkdir()
    assert init_cmd.resolve_name(repo) == "acme-dev"


def test_resolve_name_prefix(tmp_path):
    repo = tmp_path / "thing"
    repo.mkdir()
    assert init_cmd.resolve_name(repo, prefix="work") == "work-thing"


def test_resolve_name_respects_per_repo_file(tmp_path):
    repo = tmp_path / "acme.dev"
    repo.mkdir()
    (repo / init_cmd.NAME_FILE).write_text("legacy-agent-name\n")
    assert init_cmd.resolve_name(repo) == "legacy-agent-name"


def test_resolve_name_explicit_override_wins(tmp_path):
    repo = tmp_path / "anything"
    repo.mkdir()
    (repo / init_cmd.NAME_FILE).write_text("ignored\n")
    assert init_cmd.resolve_name(repo, override="custom") == "custom"


# --------------------------- bin detection ---------------------------


def test_detect_agent_bus_bin_explicit():
    assert init_cmd.detect_agent_bus_bin(explicit="/some/path") == "/some/path"


def test_detect_agent_bus_bin_env_var(monkeypatch):
    monkeypatch.setenv("AGENT_BUS_BIN", "/from/env/agent-bus")
    assert init_cmd.detect_agent_bus_bin() == "/from/env/agent-bus"


def test_detect_agent_bus_bin_returns_unresolved_shim(tmp_path, monkeypatch):
    """Regression: the pipx shim is a symlink into a venv; the venv path
    changes on `pipx reinstall` or package rename, while the shim is stable.
    We must NOT resolve the symlink — wirings would break on every reinstall.
    """
    venv_bin = tmp_path / "venvs" / "agent-bus" / "bin" / "agent-bus"
    venv_bin.parent.mkdir(parents=True)
    venv_bin.write_text("#!/bin/sh\nexec true\n")
    venv_bin.chmod(0o755)

    shim_dir = tmp_path / ".local" / "bin"
    shim_dir.mkdir(parents=True)
    shim = shim_dir / "agent-bus"
    shim.symlink_to(venv_bin)

    monkeypatch.delenv("AGENT_BUS_BIN", raising=False)
    monkeypatch.setenv("PATH", str(shim_dir))

    result = init_cmd.detect_agent_bus_bin()
    assert result == str(shim), (
        f"detect should return the stable shim path, not the resolved venv "
        f"target (got {result!r}, expected {str(shim)!r})"
    )


# --------------------------- repo detection --------------------------


def test_find_git_repos_basic(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "subdir" / "b"
    c = tmp_path / "not-a-repo"
    for r in (a, b, c):
        r.mkdir(parents=True)
    (a / ".git").mkdir()
    (b / ".git").mkdir()

    repos = init_cmd.find_git_repos(tmp_path)
    repo_names = sorted(r.name for r in repos)
    assert repo_names == ["a", "b"]


def test_find_git_repos_skips_nested(tmp_path):
    outer = tmp_path / "outer"
    inner = outer / "inner"
    inner.mkdir(parents=True)
    (outer / ".git").mkdir()
    (inner / ".git").mkdir()
    repos = init_cmd.find_git_repos(tmp_path)
    assert repos == [outer.resolve()]


def test_find_git_repos_skips_noisy_dirs(tmp_path):
    repo = tmp_path / "ok"
    repo.mkdir()
    (repo / ".git").mkdir()
    # node_modules masquerading as something with a .git inside should be skipped
    nm = tmp_path / "node_modules" / "fakedep"
    nm.mkdir(parents=True)
    (nm / ".git").mkdir()
    repos = init_cmd.find_git_repos(tmp_path)
    assert repos == [repo.resolve()]


# --------------------------- plan + apply ----------------------------


def _make_repo(tmp_path: Path, name: str, *, name_file: str | None = None,
               ignore: bool = False) -> Path:
    repo = tmp_path / name
    repo.mkdir(parents=True, exist_ok=True)
    (repo / ".git").mkdir(exist_ok=True)
    if name_file is not None:
        (repo / init_cmd.NAME_FILE).write_text(name_file + "\n")
    if ignore:
        (repo / init_cmd.IGNORE_FILE).touch()
    return repo


def test_plan_write_for_fresh_repo(tmp_path):
    repo = _make_repo(tmp_path, "acme.dev")
    plan = init_cmd.plan_for_repo(repo)
    assert plan.action == init_cmd.Action.WRITE
    assert plan.name == "acme-dev"


def test_plan_skips_ignored(tmp_path):
    repo = _make_repo(tmp_path, "ignored", ignore=True)
    plan = init_cmd.plan_for_repo(repo)
    assert plan.action == init_cmd.Action.SKIP_IGNORED


def test_plan_skips_non_repo(tmp_path):
    repo = tmp_path / "not-a-repo"
    repo.mkdir()
    plan = init_cmd.plan_for_repo(repo)
    assert plan.action == init_cmd.Action.SKIP_NOT_REPO


def test_apply_writes_mcp_and_settings(tmp_path):
    repo = _make_repo(tmp_path, "acme.dev")
    plan = init_cmd.plan_for_repo(repo)
    init_cmd.apply_plan(plan, bin_path="/usr/local/bin/agent-bus")

    mcp = json.loads((repo / ".mcp.json").read_text())
    assert mcp["mcpServers"]["agent-bus"]["env"]["AGENT_BUS_NAME"] == "acme-dev"
    assert mcp["mcpServers"]["agent-bus"]["env"]["AGENT_BUS_REPO"] == str(repo)
    assert mcp["mcpServers"]["agent-bus"]["args"] == ["serve"]

    settings = json.loads((repo / ".claude" / "settings.json").read_text())
    allow = settings["permissions"]["allow"]
    for tool in init_cmd.ALLOW_TOOLS:
        assert tool in allow
    hook_cmds = " ".join(
        h["command"]
        for ev in ("UserPromptSubmit", "Stop")
        for block in settings["hooks"][ev]
        for h in block["hooks"]
    )
    assert "hook-user-prompt" in hook_cmds
    assert "hook-stop" in hook_cmds
    assert "AGENT_BUS_NAME=acme-dev" in hook_cmds


def test_apply_is_idempotent(tmp_path):
    """Running init twice should produce identical files (no duplicate
    hooks, no duplicate allow-list entries)."""
    repo = _make_repo(tmp_path, "acme.dev")
    for _ in range(2):
        plan = init_cmd.plan_for_repo(repo)
        init_cmd.apply_plan(plan, bin_path="/usr/local/bin/agent-bus")
    settings = json.loads((repo / ".claude" / "settings.json").read_text())
    # exactly one matcher per event for our hook
    for event in ("UserPromptSubmit", "Stop"):
        managed = [
            b for b in settings["hooks"][event] if init_cmd.is_managed_hook_block(b)
        ]
        assert len(managed) == 1, f"duplicate hooks for {event}"
    # allow-list has each agent-bus tool exactly once
    allow = settings["permissions"]["allow"]
    for tool in init_cmd.ALLOW_TOOLS:
        assert allow.count(tool) == 1


def test_apply_preserves_unrelated_mcp_servers(tmp_path):
    repo = _make_repo(tmp_path, "ok")
    (repo / ".mcp.json").write_text(json.dumps({
        "mcpServers": {
            "github": {"command": "gh-mcp", "args": []},
        }
    }))
    plan = init_cmd.plan_for_repo(repo)
    init_cmd.apply_plan(plan, bin_path="/usr/local/bin/agent-bus")
    mcp = json.loads((repo / ".mcp.json").read_text())
    assert mcp["mcpServers"]["github"]["command"] == "gh-mcp"
    assert "agent-bus" in mcp["mcpServers"]


def test_apply_preserves_unrelated_hooks_and_perms(tmp_path):
    repo = _make_repo(tmp_path, "ok")
    settings_path = repo / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps({
        "permissions": {"allow": ["mcp__github__list_branches"]},
        "hooks": {
            "UserPromptSubmit": [
                {"matcher": "", "hooks": [{"type": "command", "command": "echo unrelated"}]}
            ]
        },
    }))
    plan = init_cmd.plan_for_repo(repo)
    init_cmd.apply_plan(plan, bin_path="/usr/local/bin/agent-bus")
    settings = json.loads(settings_path.read_text())
    # foreign allow entry survives
    assert "mcp__github__list_branches" in settings["permissions"]["allow"]
    # foreign hook block survives
    user_prompt_hooks = settings["hooks"]["UserPromptSubmit"]
    foreign = [b for b in user_prompt_hooks if not init_cmd.is_managed_hook_block(b)]
    assert any("echo unrelated" in h["command"]
               for b in foreign for h in b["hooks"])


def test_refresh_renames_existing_managed_entry(tmp_path):
    """Migration case: a managed entry whose custom name no longer
    matches the slug should refresh to the slug-derived name."""
    repo = _make_repo(tmp_path, "acme.dev")
    (repo / ".mcp.json").write_text(json.dumps({
        "mcpServers": {
            "agent-bus": init_cmd.build_mcp_entry(
                name="legacy-agent-name",
                repo=repo,
                bin_path="/old/path/agent-bus",
            ),
        }
    }))
    plan = init_cmd.plan_for_repo(repo)
    assert plan.action == init_cmd.Action.REFRESH
    assert plan.previous_name == "legacy-agent-name"
    assert plan.name == "acme-dev"

    init_cmd.apply_plan(plan, bin_path="/new/path/agent-bus")
    mcp = json.loads((repo / ".mcp.json").read_text())
    entry = mcp["mcpServers"]["agent-bus"]
    assert entry["env"]["AGENT_BUS_NAME"] == "acme-dev"
    assert entry["command"] == "/new/path/agent-bus"


def test_skip_handwritten_without_force(tmp_path):
    repo = _make_repo(tmp_path, "ok")
    (repo / ".mcp.json").write_text(json.dumps({
        "mcpServers": {
            "agent-bus": {
                "command": "some-other-thing",
                "args": ["--weird"],
            }
        }
    }))
    plan = init_cmd.plan_for_repo(repo)
    assert plan.action == init_cmd.Action.SKIP_HANDWRITTEN

    # with --force the same input becomes a refresh
    plan_forced = init_cmd.plan_for_repo(repo, force=True)
    assert plan_forced.action == init_cmd.Action.REFRESH


def test_deconflict_handles_collisions(tmp_path):
    a = _make_repo(tmp_path / "websites", "foo")
    b = _make_repo(tmp_path / "projects", "foo")
    plans = init_cmd.plan_for_paths(
        [tmp_path / "websites", tmp_path / "projects"],
        scan=True,
    )
    changes = [p for p in plans if p.is_change]
    names = sorted(p.name for p in changes)
    # first gets bare slug, second gets parent-prefixed
    assert "foo" in names
    assert any(n in {"projects-foo", "websites-foo"} for n in names)
    assert len(set(names)) == len(names), "deconflict failed to make names unique"


def test_per_repo_name_file_respected(tmp_path):
    repo = _make_repo(tmp_path, "acme.dev", name_file="legacy-agent-name")
    plan = init_cmd.plan_for_repo(repo)
    assert plan.name == "legacy-agent-name"

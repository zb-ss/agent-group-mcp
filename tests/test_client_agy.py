"""agy wiring: a workspace plugin under `.agents/plugins/agent-bus/`."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from agent_bus import clients, init_cmd
from agent_bus.clients.base import WiringState, WiringStatus

BIN = "/usr/local/bin/agent-bus"


def _repo(tmp_path: Path, name: str = "repo-a") -> Path:
    repo = tmp_path / name
    (repo / ".git").mkdir(parents=True)
    return repo


PLUGIN = Path(".agents") / "plugins" / "agent-bus"


def _load(repo: Path, filename: str) -> dict:
    return json.loads((repo / PLUGIN / filename).read_text())


@pytest.fixture
def agy():
    return clients.wiring("agy")


def test_fresh_repo_gets_a_server_and_a_named_hook(tmp_path, agy):
    repo = _repo(tmp_path)
    assert agy.inspect(repo).status is WiringStatus.ABSENT

    agy.apply(repo, name="repo-a/agy", bin_path=BIN)

    assert _load(repo, "plugin.json") == {"name": "agent-bus"}
    server = _load(repo, "mcp_config.json")["mcpServers"]["agent-bus"]
    # only keys agy documents
    assert server == {
        "command": BIN,
        "args": ["serve"],
        "env": {"AGENT_BUS_NAME": "repo-a/agy", "AGENT_BUS_REPO": str(repo)},
    }
    hook = _load(repo, "hooks.json")["agent-bus"]
    assert sorted(hook) == ["PreInvocation", "Stop"]
    # PreInvocation and Stop take a flat list of handlers, not matcher groups
    (pre,) = hook["PreInvocation"]
    (stop,) = hook["Stop"]
    assert pre == {
        "type": "command",
        "command": f"AGENT_BUS_NAME=repo-a/agy {BIN} hook-user-prompt --client agy || true",
    }
    assert stop["command"] == (
        f"AGENT_BUS_NAME=repo-a/agy {BIN} hook-stop --client agy || true")
    assert agy.inspect(repo) == WiringState(WiringStatus.MANAGED, "repo-a/agy")


def test_the_users_own_agents_files_are_never_touched(tmp_path, agy):
    """Everything we write sits inside our plugin directory."""
    repo = _repo(tmp_path)
    (repo / ".agents").mkdir()
    theirs = {
        "hooks.json": json.dumps({"lint-checker": {"PostToolUse": []}}),
        "mcp_config.json": "{ // their own, with a comment\n}",
    }
    for filename, text in theirs.items():
        (repo / ".agents" / filename).write_text(text)

    for _ in range(2):
        agy.apply(repo, name="repo-a/agy", bin_path=BIN)

    for filename, text in theirs.items():
        assert (repo / ".agents" / filename).read_text() == text
    assert sorted(p.name for p in (repo / PLUGIN).iterdir()) == [
        "hooks.json", "mcp_config.json", "plugin.json",
    ]


def test_renaming_rewrites_the_plugin_in_place(tmp_path, agy):
    repo = _repo(tmp_path)
    agy.apply(repo, name="repo-a/agy", bin_path="/old/agent-bus")
    agy.apply(repo, name="renamed/agy", bin_path=BIN)
    assert agy.inspect(repo) == WiringState(WiringStatus.MANAGED, "renamed/agy")
    assert len(_load(repo, "hooks.json")["agent-bus"]["Stop"]) == 1


def test_a_plugin_of_that_name_we_did_not_write_is_left_alone(tmp_path, agy):
    repo = _repo(tmp_path)
    (repo / PLUGIN).mkdir(parents=True)
    (repo / PLUGIN / "plugin.json").write_text(json.dumps({"name": "agent-bus"}))
    (repo / PLUGIN / "mcp_config.json").write_text(json.dumps({
        "mcpServers": {"agent-bus": {"command": "something-else", "args": []}},
    }))
    assert agy.inspect(repo).status is WiringStatus.HANDWRITTEN
    plan = init_cmd.plan_for_paths([repo], scan=False, client_ids=["agy"])[0]
    assert plan.action is init_cmd.Action.SKIP_HANDWRITTEN

    (repo / PLUGIN / "mcp_config.json").write_text("{ // comments\n}")
    assert agy.inspect(repo).status is WiringStatus.UNREADABLE


def test_the_wired_hooks_answer_agy_in_its_own_json(
    tmp_path, agy, storage, monkeypatch, capsys
):
    """Run the exact commands that were written, the way agy would: from the
    directory holding hooks.json, payload on stdin with `workspacePaths`
    and no cwd."""
    from agent_bus import cli

    repo = _repo(tmp_path)
    agy.apply(repo, name="repo-a/agy", bin_path=BIN)
    storage.upsert_agent("repo-a/agy", str(repo))
    storage.ensure_agent("human", "/home")
    storage.send_message(from_agent="human", to="repo-a", body="hello agy")

    payload = json.dumps({"conversationId": "c-1", "workspacePaths": [str(repo)]})
    monkeypatch.chdir(repo / PLUGIN)

    def run(command: str) -> dict:
        assignment, _binary, *argv = command.removesuffix(" || true").split()
        key, _, value = assignment.partition("=")
        monkeypatch.setenv(key, value)
        monkeypatch.setattr("sys.stdin", io.StringIO(payload))
        assert cli.main(argv) == 0
        return json.loads(capsys.readouterr().out)

    hook = _load(repo, "hooks.json")["agent-bus"]
    injected = run(hook["PreInvocation"][0]["command"])
    assert "hello agy" in injected["injectSteps"][0]["ephemeralMessage"]
    assert run(hook["Stop"][0]["command"]) == {"decision": "stop"}


# ---------------------------- folder trust carry-over -----------------------


def _trust_opts(settings: Path, **extra) -> dict:
    return {"worktree_trust": {
        "enabled": True, "file": str(settings), "key": "trusted", **extra,
    }}


def _prep(agy, tmp_path, options, *, dry_run=False):
    clone = tmp_path / "clone"
    clone.mkdir(exist_ok=True)
    return agy.prepare_worktree(
        clone, tmp_path / "wt", name="repo-a/agy", bin_path=BIN, dry_run=dry_run,
        options=options,
    )


def test_trusted_clone_passes_its_trust_to_the_worktree_once(tmp_path, agy):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"trusted": [str(tmp_path / "clone")], "other": {"a": 1}}))

    prep = _prep(agy, tmp_path, _trust_opts(settings))
    assert prep.notes and prep.notes[0].startswith(f"trusted {tmp_path / 'wt'} in agy")
    data = json.loads(settings.read_text())
    assert data["trusted"] == [str(tmp_path / "clone"), str(tmp_path / "wt")]
    assert data["other"] == {"a": 1}

    assert _prep(agy, tmp_path, _trust_opts(settings)).notes == []
    assert json.loads(settings.read_text()) == data


def test_disabled_option_writes_nothing(tmp_path, agy):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"trusted": [str(tmp_path / "clone")]}))
    before = settings.read_text()
    options = _trust_opts(settings)
    options["worktree_trust"]["enabled"] = False
    assert _prep(agy, tmp_path, options).notes == []
    assert _prep(agy, tmp_path, None).notes == []
    assert settings.read_text() == before


def test_untrusted_clone_is_not_carried(tmp_path, agy):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"trusted": ["/elsewhere"]}))
    before = settings.read_text()
    prep = _prep(agy, tmp_path, _trust_opts(settings))
    assert prep.notes == [
        f"{tmp_path / 'clone'} is not trusted in agy, so the worktree is not trusted either"
    ]
    assert settings.read_text() == before


def test_missing_list_asks_to_trust_the_clone_first(tmp_path, agy):
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    prep = _prep(agy, tmp_path, _trust_opts(settings))
    assert prep.notes == [
        f"agy has no trusted folders recorded in {settings}; trust the clone in agy first"
    ]
    assert settings.read_text() == "{}"


def test_unreadable_settings_file_is_left_alone(tmp_path, agy):
    settings = tmp_path / "settings.json"
    settings.write_text("{not json")
    prep = _prep(agy, tmp_path, _trust_opts(settings))
    assert prep.notes[0].startswith("not carrying agy folder trust: ")
    assert settings.read_text() == "{not json"


def test_dry_run_reports_and_writes_nothing(tmp_path, agy):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"trusted": [str(tmp_path / "clone")]}))
    before = settings.read_text()
    prep = _prep(agy, tmp_path, _trust_opts(settings), dry_run=True)
    assert prep.notes == [f"would trust {tmp_path / 'wt'} in agy ({settings})"]
    assert settings.read_text() == before


# ------------------------- trust cleanup after removal ----------------------


def _release(agy, tmp_path, options):
    return agy.release_worktree(tmp_path / "wt", options=options)


def test_release_drops_only_the_worktree_entry(tmp_path, agy):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps(
        {"trusted": [str(tmp_path / "clone"), str(tmp_path / "wt")], "other": {"a": 1}}
    ))
    notes = _release(agy, tmp_path, _trust_opts(settings))
    assert notes == [f"dropped agy folder trust for {tmp_path / 'wt'}"]
    assert json.loads(settings.read_text()) == {
        "trusted": [str(tmp_path / "clone")], "other": {"a": 1},
    }
    assert _release(agy, tmp_path, _trust_opts(settings)) == []


def test_release_is_off_unless_enabled(tmp_path, agy):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"trusted": [str(tmp_path / "wt")]}))
    before = settings.read_text()
    options = _trust_opts(settings)
    options["worktree_trust"]["enabled"] = False
    assert _release(agy, tmp_path, options) == []
    assert _release(agy, tmp_path, None) == []
    assert settings.read_text() == before


def test_release_missing_or_unreadable_file_is_a_note(tmp_path, agy):
    settings = tmp_path / "settings.json"
    assert "not found" in _release(agy, tmp_path, _trust_opts(settings))[0]
    settings.write_text("{not json")
    notes = _release(agy, tmp_path, _trust_opts(settings))
    assert notes[0].startswith("not dropping agy folder trust: ")
    assert settings.read_text() == "{not json"


def test_release_without_a_list_changes_nothing(tmp_path, agy):
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    assert _release(agy, tmp_path, _trust_opts(settings)) == []
    assert settings.read_text() == "{}"


# ------------------------- plugin files in a worktree -----------------------


def _wired_clone(tmp_path, agy):
    clone = _repo(tmp_path, "clone")
    agy.apply(clone, name="repo-a/agy", bin_path=BIN)
    return clone


def test_worktree_gets_real_copies_in_place_of_links(tmp_path, agy):
    clone = _wired_clone(tmp_path, agy)
    wt = tmp_path / "wt"
    (wt / PLUGIN).mkdir(parents=True)
    (wt / PLUGIN / "plugin.json").symlink_to(clone / PLUGIN / "plugin.json")
    prep = agy.prepare_worktree(clone, wt, name="repo-a/agy", bin_path=BIN)
    names = ["plugin.json", "mcp_config.json", "hooks.json"]
    assert sorted(prep.written) == sorted(PLUGIN / n for n in names)
    assert sorted(prep.covered) == sorted(PLUGIN / n for n in names)
    for n in names:
        assert not (wt / PLUGIN / n).is_symlink()
        assert (wt / PLUGIN / n).read_bytes() == (clone / PLUGIN / n).read_bytes()
    env = json.loads((wt / PLUGIN / "mcp_config.json").read_text())["mcpServers"]["agent-bus"]["env"]
    assert env == {"AGENT_BUS_NAME": "repo-a/agy", "AGENT_BUS_REPO": str(clone)}
    # a second run has nothing left to write
    assert agy.prepare_worktree(clone, wt, name="repo-a/agy", bin_path=BIN).written == []


def test_worktree_dry_run_writes_nothing(tmp_path, agy):
    clone = _wired_clone(tmp_path, agy)
    wt = tmp_path / "wt"
    prep = agy.prepare_worktree(clone, wt, name="repo-a/agy", bin_path=BIN, dry_run=True)
    assert prep.written == [] and prep.notes and not wt.exists()


def test_tracked_plugin_file_is_kept_and_only_same_wiring_is_covered(tmp_path, agy):
    clone = _wired_clone(tmp_path, agy)
    wt = tmp_path / "wt"
    (wt / PLUGIN).mkdir(parents=True)
    (wt / PLUGIN / "hooks.json").write_text("{}")
    prep = agy.prepare_worktree(clone, wt, name="repo-a/agy", bin_path=BIN)
    assert (wt / PLUGIN / "hooks.json").read_text() == "{}"
    assert PLUGIN / "hooks.json" not in prep.written
    assert PLUGIN / "hooks.json" not in prep.covered


def test_trust_and_files_are_both_done_and_release_still_drops_trust(tmp_path, agy):
    clone = _wired_clone(tmp_path, agy)
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"trusted": [str(clone)]}))
    wt = tmp_path / "wt"
    prep = agy.prepare_worktree(
        clone, wt, name="repo-a/agy", bin_path=BIN, options=_trust_opts(settings),
    )
    assert (wt / PLUGIN / "plugin.json").is_file()
    assert any(n.startswith(f"trusted {wt} in agy") for n in prep.notes)
    notes = agy.release_worktree(wt, options=_trust_opts(settings))
    assert notes == [f"dropped agy folder trust for {wt}"]
    assert json.loads(settings.read_text())["trusted"] == [str(clone)]

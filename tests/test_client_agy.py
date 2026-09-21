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
        "command": f"AGENT_BUS_NAME=repo-a/agy {BIN} hook-user-prompt --client agy",
    }
    assert stop["command"] == f"AGENT_BUS_NAME=repo-a/agy {BIN} hook-stop --client agy"
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
        assignment, _binary, *argv = command.split()
        key, _, value = assignment.partition("=")
        monkeypatch.setenv(key, value)
        monkeypatch.setattr("sys.stdin", io.StringIO(payload))
        assert cli.main(argv) == 0
        return json.loads(capsys.readouterr().out)

    hook = _load(repo, "hooks.json")["agent-bus"]
    injected = run(hook["PreInvocation"][0]["command"])
    assert "hello agy" in injected["injectSteps"][0]["ephemeralMessage"]
    assert run(hook["Stop"][0]["command"]) == {"decision": "stop"}

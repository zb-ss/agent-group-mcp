"""Codex wiring: `.codex/config.toml` (marker-delimited block) + `.codex/hooks.json`."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from agent_bus import clients, init_cmd
from agent_bus.clients.base import WiringStatus

BIN = "/usr/local/bin/agent-bus"


def _repo(tmp_path: Path, name: str = "repo-a") -> Path:
    repo = tmp_path / name
    (repo / ".git").mkdir(parents=True)
    return repo


def _config(repo: Path) -> dict:
    return tomllib.loads((repo / ".codex" / "config.toml").read_text())


def _hook_commands(repo: Path, event: str) -> list[str]:
    data = json.loads((repo / ".codex" / "hooks.json").read_text())
    return [h["command"] for group in data["hooks"][event] for h in group["hooks"]]


@pytest.fixture
def codex():
    return clients.wiring("codex")


def test_fresh_repo_gets_a_server_entry_and_both_hooks(tmp_path, codex):
    repo = _repo(tmp_path)
    assert codex.inspect(repo).status is WiringStatus.ABSENT

    codex.apply(repo, name="repo-a/codex", bin_path=BIN)

    server = _config(repo)["mcp_servers"]["agent-bus"]
    assert server["command"] == BIN
    assert server["args"][0] == "serve"
    assert server["env"] == {"AGENT_BUS_NAME": "repo-a/codex", "AGENT_BUS_REPO": str(repo)}
    # Codex does not pass its environment on to MCP servers
    assert "AGENT_BUS_INSTANCE" in server["env_vars"]

    assert _hook_commands(repo, "UserPromptSubmit") == [
        f"{BIN} hook-user-prompt --client codex || true"
    ]
    assert _hook_commands(repo, "Stop") == [f"{BIN} hook-stop --client codex || true"]
    assert codex.inspect(repo) == clients.base.WiringState(
        WiringStatus.MANAGED, "repo-a/codex"
    )


def test_hook_command_is_the_same_in_every_repo(tmp_path, codex):
    """Codex trusts a hook by its definition; a name baked into the command
    would make every repo a new thing to approve."""
    a, b = _repo(tmp_path, "repo-a"), _repo(tmp_path, "repo-b")
    codex.apply(a, name="repo-a/codex", bin_path=BIN)
    codex.apply(b, name="repo-b/codex", bin_path=BIN)
    assert (a / ".codex" / "hooks.json").read_text() == (
        b / ".codex" / "hooks.json"
    ).read_text()


def test_apply_keeps_the_rest_of_the_config(tmp_path, codex):
    repo = _repo(tmp_path)
    (repo / ".codex").mkdir()
    (repo / ".codex" / "config.toml").write_text(
        '# my settings\nmodel = "some-model"\n\n'
        '[mcp_servers.other]\ncommand = "other-mcp"  # keep me\n'
    )
    (repo / ".codex" / "hooks.json").write_text(json.dumps({"hooks": {"Stop": [
        {"hooks": [{"type": "command", "command": "echo unrelated"}]},
    ]}}))

    codex.apply(repo, name="repo-a/codex", bin_path=BIN)

    text = (repo / ".codex" / "config.toml").read_text()
    assert "# my settings" in text and "# keep me" in text
    config = _config(repo)
    assert config["model"] == "some-model"
    assert sorted(config["mcp_servers"]) == ["agent-bus", "other"]
    assert _hook_commands(repo, "Stop") == [
        "echo unrelated", f"{BIN} hook-stop --client codex || true",
    ]


def test_apply_is_idempotent_and_renames_in_place(tmp_path, codex):
    repo = _repo(tmp_path)
    codex.apply(repo, name="repo-a/codex", bin_path="/old/agent-bus")
    codex.apply(repo, name="renamed/codex", bin_path=BIN)
    first = (repo / ".codex" / "config.toml").read_text()
    codex.apply(repo, name="renamed/codex", bin_path=BIN)

    assert (repo / ".codex" / "config.toml").read_text() == first
    assert first.count("[mcp_servers.agent-bus]") == 1
    assert _config(repo)["mcp_servers"]["agent-bus"]["env"]["AGENT_BUS_NAME"] == "renamed/codex"
    assert len(_hook_commands(repo, "Stop")) == 1


def test_a_table_we_did_not_write_is_left_alone(tmp_path, codex):
    """Without our markers we cannot rewrite a TOML table safely, so even
    --force does not touch it."""
    repo = _repo(tmp_path)
    (repo / ".codex").mkdir()
    original = (
        '[mcp_servers.agent-bus]\ncommand = "/some/agent-bus"\nargs = ["serve"]\n'
        'startup_timeout_sec = 30\n\n[mcp_servers.agent-bus.env]\n'
        'AGENT_BUS_NAME = "repo-a"\n'
    )
    (repo / ".codex" / "config.toml").write_text(original)

    state = codex.inspect(repo)
    assert (state.status, state.name, state.can_force) == (
        WiringStatus.HANDWRITTEN, "repo-a", False,
    )
    for force in (False, True):
        plan = init_cmd.plan_for_paths(
            [repo], scan=False, force=force, client_ids=["codex"]
        )[0]
        assert plan.action is init_cmd.Action.SKIP_HANDWRITTEN
    assert (repo / ".codex" / "config.toml").read_text() == original


def test_broken_toml_is_never_overwritten(tmp_path, codex):
    repo = _repo(tmp_path)
    (repo / ".codex").mkdir()
    (repo / ".codex" / "config.toml").write_text("this is = = not toml\n")
    assert codex.inspect(repo).status is WiringStatus.UNREADABLE
    with pytest.raises(clients.base.UnreadableConfigError):
        codex.apply(repo, name="repo-a/codex", bin_path=BIN)
    assert (repo / ".codex" / "config.toml").read_text() == "this is = = not toml\n"


def test_a_config_we_cannot_extend_is_refused_before_writing(tmp_path, codex):
    """An inline `mcp_servers` table cannot take another entry."""
    repo = _repo(tmp_path)
    (repo / ".codex").mkdir()
    original = 'mcp_servers = { other = { command = "other-mcp" } }\n'
    (repo / ".codex" / "config.toml").write_text(original)
    with pytest.raises(clients.base.UnreadableConfigError):
        codex.apply(repo, name="repo-a/codex", bin_path=BIN)
    assert (repo / ".codex" / "config.toml").read_text() == original
    assert not (repo / ".codex" / "hooks.json").exists()


def test_paths_with_awkward_characters_survive_the_toml_round_trip(tmp_path, codex):
    repo = _repo(tmp_path, 'we"ird \\ repo')
    codex.apply(repo, name="weird/codex", bin_path='/opt/my tools/agent-bus')
    server = _config(repo)["mcp_servers"]["agent-bus"]
    assert server["command"] == "/opt/my tools/agent-bus"
    assert server["env"]["AGENT_BUS_REPO"] == str(repo)
    assert _hook_commands(repo, "Stop") == [
        "'/opt/my tools/agent-bus' hook-stop --client codex || true"
    ]


def test_init_wires_two_clients_into_one_repo(tmp_path):
    repo = _repo(tmp_path)
    plan = init_cmd.plan_for_paths([repo], scan=False, client_ids=["claude", "codex"])[0]
    init_cmd.apply_plan(plan, bin_path=BIN)

    claude_name = json.loads((repo / ".mcp.json").read_text())[
        "mcpServers"]["agent-bus"]["env"]["AGENT_BUS_NAME"]
    codex_name = _config(repo)["mcp_servers"]["agent-bus"]["env"]["AGENT_BUS_NAME"]
    assert (claude_name, codex_name) == ("repo-a/claude", "repo-a/codex")


def test_codex_hook_drains_the_inbox_its_server_registered(tmp_path, storage, monkeypatch):
    """The whole loop: init wires it, serve registers it, the name-less
    hook finds it again from the payload's cwd."""
    import io

    from agent_bus.hooks import run_hook_stop
    from agent_bus.server import build_server

    for var in ("AGENT_BUS_NAME", "AGENT_BUS_REPO", "AGENT_BUS_CLIENT"):
        monkeypatch.delenv(var, raising=False)
    repo = _repo(tmp_path)
    clients.wiring("codex").apply(repo, name="repo-a/codex", bin_path=BIN)
    env = _config(repo)["mcp_servers"]["agent-bus"]["env"]
    build_server(name=env["AGENT_BUS_NAME"], repo_path=env["AGENT_BUS_REPO"], storage=storage)
    storage.ensure_agent("human", "/home")
    storage.send_message(from_agent="human", to="repo-a", body="hello codex")

    out = io.StringIO()
    run_hook_stop(storage=storage, stdout=out, client="codex",
                  stdin=io.StringIO(json.dumps({"cwd": str(repo / "src")})))
    assert "hello codex" in json.loads(out.getvalue())["reason"]


# --------------------------- hook dialect --------------------------------


def test_prompt_hook_answers_codex_in_json(tmp_path, storage, monkeypatch):
    """Codex reads stdout that starts with `[` or `{` as JSON and fails the
    hook when it does not parse — and our plain-text header starts with
    `[agent-bus]`. The mail would be marked read and never shown."""
    import io

    from agent_bus.hooks import run_hook_user_prompt

    for var in ("AGENT_BUS_NAME", "AGENT_BUS_REPO", "AGENT_BUS_CLIENT"):
        monkeypatch.delenv(var, raising=False)
    repo = _repo(tmp_path)
    storage.upsert_agent("repo-a/codex", str(repo))
    storage.ensure_agent("human", "/home")
    storage.send_message(from_agent="human", to="repo-a/codex", body="hello codex")

    out = io.StringIO()
    run_hook_user_prompt(storage=storage, stdout=out, client="codex",
                         stdin=io.StringIO(json.dumps({"cwd": str(repo)})))
    answer = json.loads(out.getvalue())
    assert set(answer) == {"hookSpecificOutput"}
    specific = answer["hookSpecificOutput"]
    assert specific["hookEventName"] == "UserPromptSubmit"
    assert "hello codex" in specific["additionalContext"]
    assert specific["additionalContext"].startswith("[agent-bus] 1 new message(s)")


def test_prompt_hook_says_nothing_to_codex_when_the_inbox_is_empty():
    assert clients.hook_dialect("codex").prompt_output(None) == ""


def test_stop_hook_uses_the_block_decision_for_codex():
    out = clients.hook_dialect("codex").stop_output("handle your mail")
    assert json.loads(out) == {"decision": "block", "reason": "handle your mail"}
    assert clients.hook_dialect("codex").stop_output(None) == ""


# --------------------------- surprising config files ---------------------


def test_a_non_utf8_config_is_reported_not_crashed(tmp_path, codex):
    """It is somebody else's file if we cannot even read it as text."""
    repo = _repo(tmp_path)
    (repo / ".codex").mkdir()
    original = b'command = "caf\xe9"\n'
    (repo / ".codex" / "config.toml").write_bytes(original)

    assert codex.inspect(repo).status is WiringStatus.UNREADABLE
    plan = init_cmd.plan_for_paths([repo], scan=False, client_ids=["codex"])[0]
    assert plan.action is init_cmd.Action.SKIP_UNREADABLE
    assert (repo / ".codex" / "config.toml").read_bytes() == original


def test_one_unreadable_repo_does_not_abandon_the_others(tmp_path, capsys, bus_paths):
    """A --scan --apply run must not stop partway and leave the rest unwired."""
    from agent_bus import cli

    broken = _repo(tmp_path, "broken")
    (broken / ".codex").mkdir()
    (broken / ".codex" / "config.toml").write_bytes(b'x = "\xe9"\n')
    healthy = _repo(tmp_path, "healthy")

    rc = cli.main(["init", "--scan", str(tmp_path), "--clients", "codex",
                   "--apply", "--bin-path", BIN])
    capsys.readouterr()
    assert rc == 0
    assert clients.wiring("codex").inspect(healthy).name == "healthy/codex"
    assert (broken / ".codex" / "config.toml").read_bytes() == b'x = "\xe9"\n'


@pytest.mark.parametrize(
    "corruption",
    [
        "stray start above the block",
        "duplicated whole block",
        "end before start",
    ],
)
def test_confused_markers_never_swallow_user_config(tmp_path, codex, corruption):
    """The span between markers is only meaningful when there is exactly one
    well-formed block; otherwise replacing it would delete what is between."""
    from agent_bus.clients.codex import BLOCK_END, BLOCK_START

    repo = _repo(tmp_path)
    codex.apply(repo, name="repo-a/codex", bin_path=BIN)
    config = repo / ".codex" / "config.toml"
    theirs = '[mcp_servers.myserver]\ncommand = "mine"\n'
    text = config.read_text()
    if corruption == "stray start above the block":
        text = f"{BLOCK_START}\n{theirs}{text}"
    elif corruption == "duplicated whole block":
        text = text + "\n" + text
    else:
        text = f"{BLOCK_END}\n{theirs}{text}"
    config.write_text(text)

    assert codex.inspect(repo).status is WiringStatus.UNREADABLE
    with pytest.raises(clients.base.UnreadableConfigError):
        codex.apply(repo, name="repo-a/codex", bin_path=BIN)
    assert config.read_text() == text
    if corruption != "duplicated whole block":
        assert "myserver" in config.read_text()


def test_a_user_table_outside_our_block_is_left_alone(tmp_path, codex):
    """Our markers being present does not make an agent-bus table ours."""
    from agent_bus.clients.codex import BLOCK_END, BLOCK_START

    repo = _repo(tmp_path)
    (repo / ".codex").mkdir()
    original = (
        '[mcp_servers.agent-bus]\ncommand = "their-own-thing"\n\n'
        f"{BLOCK_START}\n# nothing of ours here yet\n{BLOCK_END}\n"
    )
    (repo / ".codex" / "config.toml").write_text(original)

    state = codex.inspect(repo)
    assert (state.status, state.can_force) == (WiringStatus.HANDWRITTEN, False)
    with pytest.raises(clients.base.UnreadableConfigError):
        codex.apply(repo, name="repo-a/codex", bin_path=BIN)
    assert (repo / ".codex" / "config.toml").read_text() == original


def test_config_writes_leave_no_half_written_file(tmp_path, codex):
    repo = _repo(tmp_path)
    codex.apply(repo, name="repo-a/codex", bin_path=BIN)
    leftovers = [p.name for p in (repo / ".codex").iterdir() if "tmp" in p.name]
    assert leftovers == []

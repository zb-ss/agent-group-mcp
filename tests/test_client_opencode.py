"""opencode wiring: `.opencode/opencode.json` + a generated plugin."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_bus import clients, init_cmd
from agent_bus.clients.base import WiringState, WiringStatus

BIN = "/usr/local/bin/agent-bus"


def _repo(tmp_path: Path, name: str = "repo-a") -> Path:
    repo = tmp_path / name
    (repo / ".git").mkdir(parents=True)
    return repo


@pytest.fixture
def opencode():
    return clients.wiring("opencode")


def test_fresh_repo_gets_a_server_entry_and_a_plugin(tmp_path, opencode):
    repo = _repo(tmp_path)
    assert opencode.inspect(repo).status is WiringStatus.ABSENT

    opencode.apply(repo, name="repo-a/opencode", bin_path=BIN)

    config = json.loads((repo / ".opencode" / "opencode.json").read_text())
    assert config["mcp"]["agent-bus"] == {
        "type": "local",
        "command": [BIN, "serve"],
        "environment": {"AGENT_BUS_NAME": "repo-a/opencode", "AGENT_BUS_REPO": str(repo)},
        "enabled": True,
    }
    plugin = (repo / ".opencode" / "plugins" / "agent-bus.js").read_text()
    assert f'const BIN = "{BIN}";' in plugin
    assert 'const NAME = "repo-a/opencode";' in plugin
    assert opencode.inspect(repo) == WiringState(WiringStatus.MANAGED, "repo-a/opencode")


def test_apply_keeps_the_rest_of_the_config_and_is_idempotent(tmp_path, opencode):
    repo = _repo(tmp_path)
    (repo / ".opencode").mkdir()
    (repo / ".opencode" / "opencode.json").write_text(json.dumps({
        "model": "some/model",
        "mcp": {"other": {"type": "remote", "url": "https://mcp.example.invalid"}},
    }))
    opencode.apply(repo, name="repo-a/opencode", bin_path="/old/agent-bus")
    opencode.apply(repo, name="renamed/opencode", bin_path=BIN)

    config = json.loads((repo / ".opencode" / "opencode.json").read_text())
    assert config["model"] == "some/model"
    assert sorted(config["mcp"]) == ["agent-bus", "other"]
    assert opencode.inspect(repo).name == "renamed/opencode"
    assert 'const NAME = "renamed/opencode";' in (
        repo / ".opencode" / "plugins" / "agent-bus.js"
    ).read_text()


def test_config_with_comments_is_never_overwritten(tmp_path, opencode):
    repo = _repo(tmp_path)
    (repo / ".opencode").mkdir()
    original = '{\n  // my opencode settings\n  "model": "some/model"\n}\n'
    (repo / ".opencode" / "opencode.json").write_text(original)

    plan = init_cmd.plan_for_paths([repo], scan=False, client_ids=["opencode"])[0]
    assert plan.action is init_cmd.Action.SKIP_UNREADABLE
    assert (repo / ".opencode" / "opencode.json").read_text() == original


def test_a_plugin_file_we_did_not_write_is_left_alone(tmp_path, opencode):
    repo = _repo(tmp_path)
    plugin = repo / ".opencode" / "plugins" / "agent-bus.js"
    plugin.parent.mkdir(parents=True)
    plugin.write_text("export const Mine = async () => ({});\n")
    assert opencode.inspect(repo).status is WiringStatus.HANDWRITTEN
    plan = init_cmd.plan_for_paths([repo], scan=False, client_ids=["opencode"])[0]
    assert plan.action is init_cmd.Action.SKIP_HANDWRITTEN


def test_names_and_paths_are_escaped_into_the_plugin(tmp_path, opencode):
    repo = _repo(tmp_path)
    opencode.apply(repo, name="repo-a/opencode", bin_path='/opt/my "tools"/agent-bus')
    plugin = (repo / ".opencode" / "plugins" / "agent-bus.js").read_text()
    assert 'const BIN = "/opt/my \\"tools\\"/agent-bus";' in plugin


def test_all_four_clients_in_one_repo(tmp_path):
    repo = _repo(tmp_path)
    everyone = ["claude", "codex", "agy", "opencode"]
    assert sorted(clients.wirable_clients()) == sorted(everyone)

    plan = init_cmd.plan_for_paths([repo], scan=False, client_ids=everyone)[0]
    init_cmd.apply_plan(plan, bin_path=BIN)

    wired = {c: clients.wiring(c).inspect(repo) for c in everyone}
    assert {c: s.name for c, s in wired.items()} == {
        c: f"repo-a/{c}" for c in everyone
    }
    assert all(s.status is WiringStatus.MANAGED for s in wired.values())
    # and a second run finds nothing to rename
    again = init_cmd.plan_for_paths([repo], scan=False, client_ids=everyone)[0]
    assert again.renames() == []


# --------------------------- the plugin's behaviour ----------------------

HARNESS = r"""
import { AgentBus } from "./plugin.mjs";

const calls = [];
const replies = JSON.parse(process.argv[2]);        // subcommand -> [stdout, stdout, ...]
const session = JSON.parse(process.argv[3]);        // what client.session.get returns
const prompts = [];

const $ = (strings, ...values) => {
  const command = strings.reduce((acc, s, i) => acc + s + (values[i] ?? ""), "");
  const call = { command };
  calls.push(call);
  const chain = {
    cwd(dir) { call.cwd = dir; return chain; },
    env(env) { call.name = env.AGENT_BUS_NAME; return chain; },
    quiet() { return chain; },
    nothrow() { return chain; },
    async text() {
      const sub = command.split(" ")[1];
      const reply = (replies[sub] ?? []).shift() ?? "";
      // stands in for a hook that never returns
      if (reply === "__HANG__") return new Promise(() => {});
      return reply;
    },
  };
  return chain;
};
const client = { session: {
  get: async () => ({ data: session }),
  prompt: async (req) => { prompts.push(req); },
} };

const hooks = await AgentBus({ client, $, directory: "/code/repo-a" });
const systems = [];
for (let i = 0; i < 3; i++) {
  const output = { system: ["base prompt"] };
  await hooks["experimental.chat.system.transform"]({ sessionID: "s1" }, output);
  systems.push(output.system);
}
await hooks.event({ event: { type: "message.updated", properties: {} } });
await hooks.event({ event: { type: "session.idle", properties: { sessionID: "s1" } } });
await hooks.event({ event: { type: "session.status",
                             properties: { sessionID: "s1", status: { type: "idle" } } } });
const afterIdle = { system: ["base prompt"] };
await hooks["experimental.chat.system.transform"]({ sessionID: "s1" }, afterIdle);

console.log(JSON.stringify({ calls, systems, prompts, afterIdle: afterIdle.system }));
"""


def _drive_plugin(tmp_path: Path, *, replies: dict, session: dict) -> dict:
    plugin = clients.opencode.render_plugin(name="repo-a/opencode", bin_path=BIN)
    (tmp_path / "plugin.mjs").write_text(plugin)
    (tmp_path / "harness.mjs").write_text(HARNESS)
    result = subprocess.run(
        ["node", "harness.mjs", json.dumps(replies), json.dumps(session)],
        cwd=tmp_path, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")


@needs_node
def test_plugin_repeats_mail_for_the_rest_of_the_turn(tmp_path):
    """The system-prompt addition is not kept in the transcript, and the
    inbox hands mail out once — so the plugin has to remember it."""
    out = _drive_plugin(
        tmp_path,
        replies={"hook-user-prompt": ["[agent-bus] first", "", "[agent-bus] second"]},
        session={"id": "s1"},
    )
    assert out["systems"] == [
        ["base prompt", "[agent-bus] first"],
        ["base prompt", "[agent-bus] first"],
        ["base prompt", "[agent-bus] first\n[agent-bus] second"],
    ]
    assert out["afterIdle"] == ["base prompt"]  # a new turn starts clean

    first = out["calls"][0]
    assert first == {
        "command": f"{BIN} hook-user-prompt --client opencode",
        "cwd": "/code/repo-a",
        "name": "repo-a/opencode",
    }


@needs_node
def test_plugin_starts_a_follow_up_turn_when_mail_is_waiting_at_idle(tmp_path):
    reason = "Pending messages from peers — handle them before stopping:\n- from human: hi"
    out = _drive_plugin(
        tmp_path,
        replies={"hook-stop": [json.dumps({"decision": "block", "reason": reason}), ""]},
        session={"id": "s1"},
    )
    assert out["prompts"] == [{
        "path": {"id": "s1"},
        "body": {"parts": [{"type": "text", "text": reason}]},
    }]
    stops = [c for c in out["calls"] if "hook-stop" in c["command"]]
    assert len(stops) == 2  # both idle events asked; only the first had mail


@needs_node
def test_plugin_ignores_an_idle_subagent_and_an_empty_inbox(tmp_path):
    sub = _drive_plugin(
        tmp_path,
        replies={"hook-stop": [json.dumps({"decision": "block", "reason": "mail"})]},
        session={"id": "s1", "parentID": "parent"},
    )
    assert sub["prompts"] == []
    assert not any("hook-stop" in c["command"] for c in sub["calls"])

    empty = _drive_plugin(tmp_path, replies={}, session={"id": "s1"})
    assert empty["prompts"] == []
    assert empty["systems"] == [["base prompt"]] * 3


def test_a_bin_path_containing_a_placeholder_cannot_corrupt_the_plugin(tmp_path):
    """The template is filled in one pass, so a value that happens to look
    like another placeholder is not rewritten by a later substitution."""
    plugin = clients.opencode.render_plugin(
        name="repo-a/opencode", bin_path="/usr/__NAME__/agent-bus"
    )
    (tmp_path / "p.mjs").write_text(plugin)
    done = subprocess.run(["node", "--check", str(tmp_path / "p.mjs")],
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    assert 'const BIN = "/usr/__NAME__/agent-bus";' in plugin
    assert 'const NAME = "repo-a/opencode";' in plugin


@needs_node
def test_a_hook_that_never_returns_does_not_hold_up_the_request(tmp_path):
    """Regression: a hook waiting on input it will never get used to stall
    every model request. The plugin now gives up on the hook, not the turn."""
    import os
    import time

    (tmp_path / "plugin.mjs").write_text(
        clients.opencode.render_plugin(name="repo-a/opencode", bin_path=BIN)
    )
    (tmp_path / "harness.mjs").write_text(HARNESS)
    began = time.monotonic()
    result = subprocess.run(
        ["node", "harness.mjs", json.dumps({"hook-user-prompt": ["__HANG__"]}),
         json.dumps({"id": "s1"})],
        cwd=tmp_path, capture_output=True, text=True, timeout=30,
        env={**os.environ, "AGENT_BUS_HOOK_TIMEOUT_MS": "300"},
    )
    took = time.monotonic() - began
    assert result.returncode == 0, result.stderr
    assert took < 15, f"the harness took {took:.1f}s — the hang was not bounded"
    # the request still went ahead, just without the mail
    assert json.loads(result.stdout)["systems"][0] == ["base prompt"]


@needs_node
def test_a_stuck_hook_is_not_started_again_on_every_request(tmp_path):
    """A turn makes several model requests. Re-running a hook that is still
    stuck from the last one would pay the full timeout each time and leave
    one more process hanging per request."""
    import os
    import time

    (tmp_path / "plugin.mjs").write_text(
        clients.opencode.render_plugin(name="repo-a/opencode", bin_path=BIN)
    )
    (tmp_path / "harness.mjs").write_text(HARNESS)
    # every call would hang: a binary that is broken for good, not just slow
    replies = {"hook-user-prompt": ["__HANG__"] * 4, "hook-stop": ["__HANG__"] * 2}
    began = time.monotonic()
    result = subprocess.run(
        ["node", "harness.mjs", json.dumps(replies), json.dumps({"id": "s1"})],
        cwd=tmp_path, capture_output=True, text=True, timeout=30,
        env={**os.environ, "AGENT_BUS_HOOK_TIMEOUT_MS": "1000"},
    )
    took = time.monotonic() - began
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)

    spawned = [c["command"].split()[1] for c in out["calls"]]
    assert spawned.count("hook-user-prompt") == 1, "the stuck hook was started again"
    assert out["systems"] == [["base prompt"]] * 3
    # the harness makes four model requests and two idle checks; waiting out
    # the timeout on each would take six seconds or more
    assert took < 4.5, f"took {took:.1f}s"

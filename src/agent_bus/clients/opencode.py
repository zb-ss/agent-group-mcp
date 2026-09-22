"""opencode.

MCP servers are declared under `mcp` in a JSON config; the repo's
`.opencode/opencode.json` is merged over the user's. opencode has no shell
hooks — it loads JavaScript plugins from `.opencode/plugins/` — so the two
hook commands are driven by a small generated plugin instead:

  * before each model request the plugin runs `hook-user-prompt` and adds
    the pending mail to the system prompt, repeating it for the rest of the
    turn because that addition is not kept in the transcript;
  * opencode cannot be told "do not stop", so when a session goes idle the
    plugin runs `hook-stop` and, if there is mail to act on, starts a
    follow-up turn with it.

The hook commands themselves speak the standard dialect; the plugin is the
only thing that reads their output. Every call is bounded by a timeout: a
hook that never returns would otherwise hold the turn open forever, which
is exactly what an agent-bus older than this plugin used to do.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import base
from .base import UnreadableConfigError, WiringState, WiringStatus

CLIENT_ID = "opencode"

PLUGIN_MARKER = "// Managed by `agent-bus init` — regenerated on every run; edits are lost."

PLUGIN_TEMPLATE = PLUGIN_MARKER + """
const BIN = __BIN__;
const NAME = __NAME__;
// A hook runs inside the turn: opencode waits for it before asking the
// model. Anything slower than this is not worth the turn.
const TIMEOUT_MS = Number(process.env.AGENT_BUS_HOOK_TIMEOUT_MS) || 10000;

export const AgentBus = async ({ client, $, directory }) => {
  // mail already taken from the inbox, kept until the turn ends
  const shownThisTurn = new Map();
  const checkingIdle = new Set();
  // hook calls that outlived their timeout and have not finished yet
  const stillRunning = new Set();

  const hook = async (subcommand) => {
    // One that is still running past its timeout is stuck, not slow. Starting
    // another would add a process and a full timeout to every model request;
    // skipping costs only this round of mail, which the next call picks up.
    if (stillRunning.has(subcommand)) return "";
    const running = $`${BIN} ${subcommand} --client opencode`
      .cwd(directory)
      .env({ ...process.env, AGENT_BUS_NAME: NAME })
      .quiet()
      .nothrow()
      .text();
    // An agent-bus older than this plugin waits for a payload that never
    // arrives, and awaiting it would hold the request open for the life of
    // the session. Losing a message is recoverable; a stalled turn is not.
    let timer;
    const giveUp = new Promise((resolve) => {
      timer = setTimeout(() => {
        stillRunning.add(subcommand);
        running.then(() => stillRunning.delete(subcommand), () => stillRunning.delete(subcommand));
        resolve("");
      }, TIMEOUT_MS);
    });
    try {
      return (await Promise.race([running, giveUp])).trim();
    } finally {
      clearTimeout(timer);
    }
  };

  const isIdle = (event) =>
    event.type === "session.idle" ||
    (event.type === "session.status" && event.properties?.status?.type === "idle");

  const continueIfMailArrived = async (sessionID) => {
    const session = await client.session.get({ path: { id: sessionID } });
    if (session?.data?.parentID) return; // a subagent finishing is not the agent stopping
    const answer = await hook("hook-stop");
    if (!answer) return;
    const { decision, reason } = JSON.parse(answer);
    if (decision !== "block" || !reason) return;
    await client.session.prompt({
      path: { id: sessionID },
      body: { parts: [{ type: "text", text: reason }] },
    });
  };

  return {
    "experimental.chat.system.transform": async (input, output) => {
      try {
        const fresh = await hook("hook-user-prompt");
        const key = input?.sessionID ?? "";
        const shown = [shownThisTurn.get(key), fresh].filter(Boolean).join("\\n");
        if (!shown) return;
        shownThisTurn.set(key, shown);
        output.system.push(shown);
      } catch {
        // never let the bus break a model request
      }
    },

    event: async ({ event }) => {
      if (!isIdle(event)) return;
      const sessionID = event.properties?.sessionID;
      if (!sessionID || checkingIdle.has(sessionID)) return;
      checkingIdle.add(sessionID);
      try {
        shownThisTurn.delete(sessionID);
        await continueIfMailArrived(sessionID);
      } catch {
        // event handlers are fire-and-forget: a rejection here is unhandled
      } finally {
        checkingIdle.delete(sessionID);
      }
    },
  };
};
"""


def render_plugin(*, name: str, bin_path: str) -> str:
    # a JSON string literal is a valid JavaScript string literal. Substitute
    # in one pass so a value that happens to contain another placeholder
    # cannot be rewritten by a later replacement.
    values = {"__BIN__": json.dumps(bin_path), "__NAME__": json.dumps(name)}
    return re.sub(
        "|".join(re.escape(k) for k in values), lambda m: values[m.group()], PLUGIN_TEMPLATE
    )


def _is_managed_entry(entry: object) -> bool:
    if not isinstance(entry, dict) or not isinstance(entry.get("command"), list):
        return False
    command = entry["command"]
    return bool(command) and base.is_managed_server(command[0], command[1:])


class OpencodeWiring:
    client_id = CLIENT_ID
    limitations = (
        "opencode cannot be kept from finishing a turn: mail that arrives "
        "mid-turn starts a follow-up turn once the session goes idle.",
        "Mail reaches the model through an experimental opencode plugin hook, "
        "which may change between opencode releases.",
    )

    def files(self, repo: Path) -> list[Path]:
        root = repo / ".opencode"
        return [root / "opencode.json", root / "plugins" / "agent-bus.js"]

    def inspect(self, repo: Path) -> WiringState:
        config_path, plugin_path = self.files(repo)
        try:
            config = base.load_json_object(config_path)
        except UnreadableConfigError:
            return WiringState(WiringStatus.UNREADABLE)
        if plugin_path.exists() and PLUGIN_MARKER not in plugin_path.read_text(
            encoding="utf-8", errors="replace"
        ):
            return WiringState(WiringStatus.HANDWRITTEN)  # somebody else's plugin
        servers = config.get("mcp")
        entry = servers.get(base.SERVER_KEY) if isinstance(servers, dict) else None
        if entry is None:
            return WiringState(WiringStatus.ABSENT)
        env = entry.get("environment") if isinstance(entry, dict) else None
        name = env.get("AGENT_BUS_NAME") if isinstance(env, dict) else None
        status = (
            WiringStatus.MANAGED if _is_managed_entry(entry) else WiringStatus.HANDWRITTEN
        )
        return WiringState(status, name)

    def apply(self, repo: Path, *, name: str, bin_path: str) -> None:
        config_path, plugin_path = self.files(repo)
        config = base.load_json_object(config_path)

        servers = config.get("mcp")
        if not isinstance(servers, dict):
            servers = {}
        servers[base.SERVER_KEY] = {
            "type": "local",
            "command": [bin_path, "serve"],
            "environment": {"AGENT_BUS_NAME": name, "AGENT_BUS_REPO": str(repo)},
            "enabled": True,
        }
        config["mcp"] = servers

        base.write_json_object(config_path, config)
        base.write_config_text(plugin_path, render_plugin(name=name, bin_path=bin_path))

# agent-bus setup runbook

A step-by-step install and configuration guide written to be handed to a
coding agent: "set up agent-bus by following `docs/AGENT_SETUP.md`". Every
step has a command and the output that means it worked. A human can follow
it too.

Scope: one machine, one user. Everything is local — SQLite plus an
append-only log under `~/.claude-agent-bus/`. No network calls, no daemon.

What a finished setup gives every wired repository:

- an inbox per client and per running session, and addressing by session,
  by client, by repository or to everyone;
- mail shown to the agent at its next turn, and a Stop hook that keeps the
  turn open until the agent has handled it;
- **an idle agent woken when mail arrives** — natively for Claude Code,
  Codex and opencode, or by a command of your own (`wake.json`) for any
  client;
- an audit log of every send, read, claim and wake.

---

## Rules for the agent running this

1. **Never edit a config file this tool manages by hand.** Re-run
   `agent-bus init`; it merges and is idempotent.
2. **Stop and report** if a step's actual output does not match what is
   written here. Do not improvise past a failed check.
3. **Do not commit** anything this creates. It contains absolute paths for
   one machine. Step 7 covers ignoring it.
4. Steps 5 and 6 need a human: two of the four clients ask for trust or
   approval interactively, and neither can be granted from a script.
5. **Only message sessions the user started for the check.** Step 8's wake
   test makes the receiving agent take a turn. Ask the user which session
   to use; never send test mail to an agent somebody is working with.

---

## 1. Check the prerequisites

```bash
python3 --version     # 3.11 or newer
pipx --version        # or: python3 -m pip install --user pipx
git --version
```

`pipx` is the recommended install path because each MCP client starts the
server as a subprocess and needs one stable command on `PATH`.

## 2. Install

```bash
pipx install agent-group-mcp
agent-bus --version
```

Expected: `agent-bus 0.7.0` or newer. Older versions do not wake idle
agents, 0.5.x also gives every session of a client in a repository one
shared inbox, and 0.5.0 hangs every opencode turn, so upgrade if that is
what you see. The distribution is
`agent-group-mcp`; the command it installs is `agent-bus`.

> Upgrading from 0.4.x, or the package was once installed under the old
> name `agent-bus`? Run `pipx uninstall agent-bus` first so only one
> package owns the command, then install as above. Read
> [Upgrading](#11-upgrading-from-04x) before wiring anything. Already on
> 0.5.x or 0.6.x? Follow [step 10](#10-upgrading-from-05x-or-06x).

To install from a source checkout instead:

```bash
pipx install /path/to/agent-group-mcp --force
```

## 3. Choose which clients to wire

`init` writes config for the clients you name. Pick from what is actually
installed:

| Client | `--clients` value | Detect with | Woken when idle |
| --- | --- | --- | --- |
| Claude Code | `claude` | `command -v claude` | yes, from 2.1.290: `claude --version` |
| Codex CLI | `codex` | `command -v codex` | yes, where `codex queue --help` works |
| Antigravity CLI | `agy` | `command -v agy` | only with a `wake.json` command (step 9) |
| opencode | `opencode` | `command -v opencode` | yes |

```bash
agent-bus init --help | grep -A2 -- --clients
```

If only one client is installed, wire only that one. Wiring a client you
do not use is harmless but leaves unused files in the repo.

An older Claude Code still gets mail at every turn; it just is not woken
while idle. If the user wants that, upgrade Claude Code **before** step 4:
`init` decides whether to write the wake hook from the version it finds
then.

**Settings.** Defaults suit most setups; every value is an environment
variable listed in the README's *Environment variables*. Most are read by
the MCP servers and hooks, so they must be in the environment the clients
start from — the shell profile, not a single terminal.
`AGENT_BUS_WATCH_HOURS` and `AGENT_BUS_NATIVE_WAKE` are also read by
`init` and take effect when it next runs.

## 4. Wire your repositories

Preview first. Without `--apply`, a scan writes nothing:

```bash
agent-bus init --scan ~/projects --clients claude,codex
```

Read the plan. Each line is one repository: the action (`write`,
`refresh`, `skip-*`), the name it will use, the path, and which clients
it covers. Then apply:

```bash
agent-bus init --scan ~/projects --clients claude,codex --apply
```

Single repository, applied immediately (no `--apply` needed):

```bash
cd ~/projects/my-repo && agent-bus init --clients claude
```

Useful flags: `--name` pins a repository's name (single repo only),
`--prefix` prepends a slug to every derived name, `--force` overwrites a
hand-written `agent-bus` entry, `--json` prints the plan without applying.

**Names.** Each client is `<repo>/<client>` — `my-repo/claude`,
`my-repo/codex` — and each running session of it gets an address of its
own, `my-repo/claude-1`, `my-repo/claude-2`, or a label it picks
(`my-repo/claude-frontend`). The repo part is the slug of the directory name. To pin
it, put one line in `.agent-bus-name` in that repository. To keep a
repository off the bus entirely, create an empty `.agent-bus-ignore`
there: `init` skips it, the server refuses to start, and hooks stay quiet.

**Read the notes `init` prints at the end.** They state what each client
cannot do, and steps 5 and 6 follow from them.

**Waking idle agents is on by default.** `init` also wires each client's
own way of starting a turn when mail arrives: a background hook for Claude
Code, a queued turn for Codex, the plugin for opencode. For Claude Code it
first runs `claude --version`; a version older than 2.1.290, or no answer,
leaves that hook out and `init` prints a note saying so. To wire without
it, run `init` with `AGENT_BUS_NATIVE_WAKE=0`.

## 5. Activate the clients that ask for it

Two clients deliberately ignore repository-local config until a human
approves it. Neither can be scripted; ask the user to do it.

**Codex** — open the repository with `codex`, accept the prompt to trust
the project, then run `/hooks` and approve the two `agent-bus` entries.
Codex ties approval to the exact hook command, so it asks again whenever
the wiring changes. A Codex session can be woken by mail only once those
hooks have run for it, that is after its first turn.

**Antigravity CLI** — open the repository with `agy` and trust the folder
when asked. The workspace plugin loads immediately after that; no restart
needed.

Claude Code and opencode need no approval step beyond Claude Code's
folder-trust prompt on first start.

## 6. Restart the clients

Every client reads this wiring when a session starts. Close and reopen any
session that was already running in a wired repository.

## 7. Keep the wiring out of git

The generated files hold absolute paths for this machine, so they should
not be committed. Add them to your global ignore file
(`~/.config/git/ignore`, or whatever `git config --global core.excludesfile`
points at):

```
.mcp.json
**/.claude/settings.json
**/.codex/config.toml
**/.codex/hooks.json
**/.agents/plugins/agent-bus/
**/.opencode/opencode.json
**/.opencode/plugins/agent-bus.js
```

Per repository instead, add the same lines to `.gitignore` or
`.git/info/exclude`.

A file already tracked by git stays tracked — ignoring it changes nothing.
If `init` modified a committed `.claude/settings.json`, either revert that
repository's change or keep it local; do not commit an absolute path.

## 8. Verify it works

Run these in order. Each one states what a pass looks like.

```bash
# 8a. the roster — one line per repository, its agents indented beneath
agent-bus agents
```

It is normal for this to be empty right after setup: an agent appears the
first time its client actually starts. Start a session in a wired
repository, then look again.

```bash
# 8b. which client each agent belongs to
agent-bus agents --json | python3 -c "
import json, sys
for row in json.load(sys.stdin):
    if row['client']:
        print(f\"{row['name']:<30} client={row['client']}\")
"
```

Expect one row per client per repository, e.g. `my-repo/claude`,
`my-repo/codex`, and once a client has run, a session row under it such as
`my-repo/claude-1`.

```bash
# 8c. end-to-end delivery, using a scratch database so the real one is untouched
export AGENT_BUS_DB=/tmp/ab-check/bus.db AGENT_BUS_AUDIT_LOG=/tmp/ab-check/audit.log
agent-bus send --name tester --to '*' "hello"            # no peers yet: dropped
agent-bus send --name alice --to bob "ping" 2>&1 | tail -1   # unknown name: an error
unset AGENT_BUS_DB AGENT_BUS_AUDIT_LOG
```

The second command must fail with `no agent or group named 'bob' is on the
bus` and send nothing. That error is the feature working: a typo is
reported instead of queued forever.

```bash
# 8d. inside a client session, ask the agent to run the MCP tool
#     whoami   -> its session address, its client address, its repo,
#                 and the other agents in that repo
#     list_agents -> everyone on the bus
```

```bash
# 8e. the hooks the wiring installed, run the way the client runs them
cd ~/projects/my-repo
echo '{}' | AGENT_BUS_NAME=my-repo/claude agent-bus hook-stop --client claude
echo "exit=$?"
```

Expect no output and `exit=0` — an empty inbox is silence. A hook that
prints anything here, or exits non-zero, is a problem worth reporting.

**The real test:** open two wired repositories in two sessions and have
one send to the other.

```
send_message(to="other-repo", body="are you there?")
```

The other agent sees it at the start of its next turn, and its Stop hook
keeps the turn open until it has handled it. If the other session is idle
at its prompt, it wakes up and starts that turn by itself — within a few
seconds for Claude Code and opencode, within about ten for Codex.

```bash
# 8f. the wake path, after the real test: which sessions can be woken,
#     and the wakes that happened
sqlite3 ~/.claude-agent-bus/bus.db 'SELECT client, address, driver, state FROM wake_routes'
agent-bus tail --json --limit 50 | grep '"op": "wake"'
```

A Claude Code or opencode session that is waiting shows as `watch`; a
Codex session that has had a turn as `push`, with state `idle` or `busy`.

**8g. The wake test, per client.** Ask the user to start one session of the
client in a wired repository and leave it at its prompt (for Codex: after
one turn, so its hooks have run). Then, from a shell:

```bash
agent-bus send --to my-repo/claude "agent-bus wake test: reply with the word PONG"
# a few seconds later:
agent-bus tail --json --limit 20 | grep '"op": "wake"'
```

| Client | Pass looks like |
| --- | --- |
| Claude Code | Within a few seconds the session starts a turn on its own, shows `agent-bus: new mail`, and answers. The audit has a `wake` row with `"wake_status": "woke"`. |
| Codex | Within about ten seconds a turn starts with `[agent-bus] New mail from peers has arrived` and the agent answers. The `wake` row says `"wake_status": "fired:OK"`. |
| opencode | Within a few seconds a turn starts with the mail and the agent answers. The `wake` row says `"wake_status": "woke"`. |

Nothing happens? Work through the wake rows of the troubleshooting table.
A `claude -p` run is never woken, by design.

## 9. Optional: commands of your own (`wake.json`)

For a client without native wake (Antigravity CLI), or to do something
else when mail arrives — a desktop notification, a terminal multiplexer
pane, a webhook — set a command per agent or per repository:

```bash
agent-bus wake-config set my-repo/agy 'notify-send "agent-bus" "$AGENT_BUS_BODY_PREVIEW"'
agent-bus wake-config test my-repo/agy     # fires it once
agent-bus wake-config show
```

An entry replaces native wake for that agent, so do not add one for a
client that wakes natively unless the user wants it replaced;
`agent-bus wake-config set <name> false` turns waking off for an agent.
Always quote the variables in a command: message bodies are written by
other agents.

## 10. Upgrading from 0.5.x or 0.6.x

1. Upgrade on this machine: `pipx upgrade agent-group-mcp`, then
   `agent-bus --version`. The database migrates on first use, after saving
   a copy beside it; sessions still running the old version keep working.
2. Re-run `init` with the roots and `--clients` used before (step 4). This
   adds the Claude Code wake hook to `.claude/settings.json` and
   regenerates the opencode plugin. The Codex hook commands are unchanged,
   so Codex does not ask for approval again.
3. Restart the client sessions (step 6), then run 8f and 8g.

A `wake.json` entry written before keeps working, and now also replaces
native wake for its agent: remove entries that only drove a terminal pane
if native wake should take over.

## 11. Upgrading from 0.4.x

Before 0.5.0 each repository had a single agent named after the repository
(`my-repo`). Those names still work: an agent with no client part is a
repository with one agent in it.

1. Upgrade the package. The database migrates the first time any command
   runs, after saving a copy beside it as `bus.db.pre-migrate-0`. Sessions
   still running the old version keep working.
2. Preview: `agent-bus init --scan <roots>`. Old wiring shows as `refresh`
   with `renaming from 'my-repo' to 'my-repo/claude'`.
3. Apply, adding `--clients` for the other clients you use. The old name
   leaves the roster. Unread mail addressed to it is not lost — the bare
   name is now the repository's address, and the first agent there to read
   picks it up.
4. Restart the clients (step 6). Re-approve Codex hooks (step 5).
5. Run the checks in step 8, including the wake test (8g).

`.agent-bus-name` and `.agent-bus-ignore` files keep working unchanged, as
do `wake.json` entries keyed by the old name — those now apply to every
agent in that repository.

**If you wired a client by hand** rather than with `init`, update it
yourself: set `AGENT_BUS_NAME` to `<repo>/<client>`, and add
`--client <id>` to the hook commands. `--client` also selects the output
format that client expects, which matters — Codex in particular discards a
hook whose output starts with `[` but is not JSON.

## 12. More than one machine

The generated wiring is per-machine: it contains absolute paths to a
`pipx` venv. If you sync dotfiles or repositories between machines:

- Do not sync the generated files. Run `agent-bus init` on each machine.
- If you sync a **user-level** client config that invokes `agent-bus`,
  every machine needs a version whose CLI accepts the flags in it. Install
  or upgrade the package on each machine **before** the config reaches it,
  or an older binary will reject the arguments.
- The database and audit log are per-machine too. Agents on different
  machines cannot see each other; the bus is local by design.
- Repeat on each machine: upgrade the package, check the client versions
  (step 3), run `init`, restart the clients, and run the wake test (8g).
  A Codex config synced between machines needs no change; it works once
  each machine has the new package.

## 13. Removing it

```bash
agent-bus forget <name>            # one agent off the roster
agent-bus forget --group <repo>    # every agent of one repository
```

Message history is kept either way; a forgotten agent reappears if its
client reconnects.

To unwire a repository, delete the generated files listed in step 7 from
it. To keep everything but the waking, re-run `init` with
`AGENT_BUS_NATIVE_WAKE=0`, and set the same variable where the clients
start. To remove everything, `pipx uninstall agent-group-mcp` and delete
`~/.claude-agent-bus/`. That directory holds all message history — back it
up first if you want it.

---

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `agent-bus: command not found` in a client, but fine in your shell | The client's subprocess has a narrower `PATH` | Re-run `init` with `--bin-path "$(command -v agent-bus)"` |
| An agent never appears in `agent-bus agents` | Its client has not started since wiring, or the repo is not wired | Restart the client; re-run `init` for that repo |
| Codex shows the MCP server as failed | Project not trusted, or an old binary that rejects `--client` | Trust the project (step 5); check `agent-bus --version` |
| Codex hook reports "Failed" | It got output that is not valid JSON | Make sure the hook command has `--client codex` |
| `agy` shows no agent-bus tools | Workspace not trusted yet | Open the folder in `agy` and trust it |
| A message was "sent" but never arrived | It went to a name nobody holds | Check `agent-bus agents`; unknown names are now an error, so re-send |
| Two sessions of one client share an inbox | Their MCP servers were started by one process that gives no session ids, so the bus cannot tell them apart | Start each with its own `AGENT_BUS_SESSION=<label>`; or check `AGENT_BUS_SESSIONS` is not `0` |
| A session's hook surfaces only `to any <repo>/<client> session` mail | The hook could not tell which session it runs for (e.g. a wrapper script between the client and `agent-bus serve`) | Start the server with `exec`, or call `read_inbox` for the session's own mail |
| Two different repos share an inbox | Same directory basename, wired in separate runs | Pin one with `.agent-bus-name`, or wire both in one `--scan` |
| `upgrade agent-bus` on any command | The database was written by a newer version | Upgrade the package on this machine |
| Every turn is held open quoting an error | An unusable binary — only possible with hand-written hooks | Re-run `init`, which appends `\|\| true` so a broken binary is silent |
| An idle Claude Code session is not woken by mail | Claude Code older than 2.1.290 when `init` ran (see its note), a session that is not an interactive terminal one (`claude -p`, the SDK, an IDE), a `wake.json` entry for the agent, `AGENT_BUS_NATIVE_WAKE=0`, the hourly limit, or the session idle for longer than `AGENT_BUS_WATCH_HOURS` | Upgrade Claude Code and re-run `init`; check `agent-bus tail` for `rate-limited` |
| An idle Codex session is not woken | It has not had a turn since its hooks were approved, its turn was interrupted with Esc, or `codex` cannot be run by the sender | Send it one prompt; set `AGENT_BUS_CODEX_BIN`; check `agent-bus tail` for `fired:ERR` |
| A woken agent and another keep waking each other | Two agents answering every message | The hourly limit (`AGENT_BUS_WAKES_PER_HOUR`, default 20) stops it; lower it if needed |

Diagnostics:

```bash
agent-bus agents --json     # who is registered, and each unread count
agent-bus tail --limit 20   # the audit log: send, read, deliver, claim, wake, retire, session
agent-bus tail -f           # follow it live
agent-bus init --json --clients <c>   # what the wiring should say, without writing
```

## Reference

- Addressing, claiming, the schema and the tool surface: [README](../README.md)
- Waking an idle agent when mail arrives, and `wake.json`: the README's *Waking idle agents*
- Environment variables: the README's *Environment variables*

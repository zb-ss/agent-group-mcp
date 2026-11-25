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
  Codex, Antigravity CLI and opencode, or by a command of your own
  (`wake.json`) for any client;
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

Expected: `agent-bus 0.7.2` or newer. 0.7.0 and 0.7.1 do not wake idle
Antigravity CLI sessions, older versions wake no idle agents at all,
0.5.x also gives every session of a client in a repository one
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
| Antigravity CLI | `agy` | `command -v agy` | yes, where `agy agentapi --help` lists `send-message`, once its agent has registered (step 5) |
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

Each Codex thread has its own inbox, including threads in one app-server
process. Ask it to call the agent-bus MCP tool `whoami` once to bind that
inbox to its thread id; any bus tool call does this. For example: "Call
`whoami` on the agent-bus MCP server and show its `name` and `client_address`."
If your global MCP config uses `enabled_tools`,
include `set_session` so it can name its inbox. No thread id needs to be
hardcoded in the config or added to the MCP environment. After upgrading,
restart the existing Codex MCP connections so they load the fix.

**Claude Code** — needs no step from you for the MCP server: `init` also
lists `agent-bus` in `enabledMcpjsonServers` of the untracked
`.claude/settings.local.json`, which approves the `.mcp.json` server. Without
that entry Claude Code never starts the server and the session never joins
the bus. If that file (or its folder) points outside the repo, `init` leaves
it alone and says so; approve the server in Claude Code yourself.

**Antigravity CLI** — open the repository with `agy` and trust the folder
when asked. The workspace plugin loads immediately after that; no restart
needed.

To be woken by mail while idle, each agy session registers once: at its
first model call the agent is asked to run `agent-bus wake-register
--client agy`, and agy asks the user to allow that command. Tell the user
to pick the option that always allows it and saves it to `settings.json`;
the command is the same in every repository, so agy then stops asking on
this machine. Declining is harmless: that session still gets its mail at
every turn, it is just not woken while idle.

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
**/.claude/settings.local.json
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
seconds for Claude Code and opencode, within about ten for Codex, and in
under a second for agy.

```bash
# 8f. the wake path, after the real test: which sessions can be woken,
#     and the wakes that happened
sqlite3 ~/.claude-agent-bus/bus.db 'SELECT client, address, driver, state FROM wake_routes'
agent-bus tail --json --limit 50 | grep '"op": "wake"'
```

A Claude Code or opencode session that is waiting shows as `watch`; a
Codex or agy session that has had a turn as `push`, with state `idle` or
`busy`.

**8g. The wake test, per client.** Ask the user to start one session of the
client in a wired repository and leave it at its prompt (for Codex and
agy: after one turn, so its hooks have run — and for agy, after its agent
has run `agent-bus wake-register` as asked). Then, from a shell:

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
| Antigravity CLI | Within a second a turn starts with `[agent-bus] New mail from peers has arrived` and the agent answers. The `wake` row says `"wake_status": "fired:OK"`; `unregistered` means its agent has not run `agent-bus wake-register` yet. |

Nothing happens? Work through the wake rows of the troubleshooting table.
A `claude -p` run is never woken, by design.

## 9. Optional: commands of your own (`wake.json`)

To do something else when mail arrives — a desktop notification, a
terminal multiplexer pane, a webhook — or to wake a client agent-bus has
no native wake for, set a command per agent or per repository:

```bash
agent-bus wake-config set my-repo/claude 'notify-send "agent-bus" "$AGENT_BUS_BODY_PREVIEW"'
agent-bus wake-config test my-repo/claude     # fires it once
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

From 0.7.x, agy sessions are woken natively with the wiring they already
have: upgrading the package is enough, and `init` needs no re-run. Each
running agy session asks for its registration at its next model call
(step 5). Remove a `wake.json` entry for an agy agent if native wake
should take over from it.

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
- A repo's identity is its normalised remote URL (`host/path`, lowercase,
  no scheme, user, port or `.git`), the same on every machine. Each machine
  finds its own clone from the roster, or by scanning the folders listed in
  `repos.roots` in `~/.claude-agent-bus/config.json`; `repos.clone_root` is
  where `spawn --clone` puts a clone that is missing. Group names stay the
  local alias (`.agent-bus-name`), so nothing is renamed.
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

## 14. Optional: spawning sessions and watching them

Prerequisites: `tmux` installed, and the repository wired for the client
with `agent-bus init` (steps 3 and 4). Put the first message in a file and
look before starting anything:

```bash
echo "Fix the login redirect loop and open a pull request." > /tmp/prompt.txt
agent-bus spawn --repo repo-a --client claude --name fix-login \
    --prompt-file /tmp/prompt.txt --dry-run
```

It prints the address (`repo-a/claude-fix-login`), the folder and the exact
command, and changes nothing. If it looks right, run it again without
`--dry-run`, then:

```bash
agent-bus status          # state, client, tmux window; --all adds ended and long-finished ones
tmux attach -t agents     # one window per session
```

`--worktree <base-ref>` puts the session in a git worktree at
`<clone>/.worktrees/<label>` (excluded through the repository's
`info/exclude`; `spawn.worktree_root` moves it elsewhere). Inside the clone it
inherits the clone's folder trust in Claude Code and Codex, so no trust prompt
blocks an unattended session; Antigravity CLI asks again, and opencode never
asks. IDEs, containers and file watchers that mount the main folder also see
`.worktrees/`. For Claude Code, spawn writes a worktree-local
`.claude/settings.local.json` that approves the server and, if the worktree's
committed `.claude/settings.json` lacks the agent-bus hooks, carries them.

Spawned sessions are started in the client's auto permission mode, with
Remote Control on where the client has it (`--no-remote` turns it off). To
change the commands, the cap of five sessions, or the folders searched for
clones, create `~/.claude-agent-bus/config.json` (see the README's
*Configuration*). Ask the agent in its prompt to call
`set_session(state="done")` when it has finished; `status` then stops
counting it against the cap. A finished session is hidden from `status`
after `status.hide_done_after_seconds` (default one day) once it has ended;
`--all` shows it. A mail wake sets a session back to `working`, and a
reconnect of the same session keeps its reported state.

To close everything: `tmux kill-session -t agents`. Sessions that were
running show as `ended` in `status --all`.

## 15. Upgrading from 0.7.x

Nothing to re-wire. The database migrates forward the first time any
`agent-bus` command opens it, and a `bus.db.pre-migrate-<version>` copy is
kept next to it. Group names and addresses do not change. Restart
long-running clients to get state reporting: a running MCP server and its
hooks only learn `set_session(state=...)` and the `working` / `waiting`
updates after a restart.

---

## 16. Optional: dispatching tasks on a timer

`agent-bus dispatch` reads your due tasks from Asana, starts a session for
the ones your rules select, and notifies you. Do this only when the owner
wants it, and never load the timer without their say-so.

1. Provide the token. Export it under the name you will set as `token_env` (default `ASANA_TOKEN`), or, for timer runs, create an env file with `KEY=VALUE` lines and `chmod 600` it (for example `~/.claude-agent-bus/dispatch.env`). Never paste the value into `config.json`.
2. Add a `dispatch` block to `~/.claude-agent-bus/config.json`: enable `sources.asana`, map each project id to a repo under `projects`, and set `env_file` and `templates_dir` if you use them. See the README section *Dispatching tasks* for the keys. Map only projects that belong to a single repo: the repo comes from the first of a task's projects found in `projects`, so a shared process project (one board for every repo) stays unmapped and is used in rules instead, through a rule's `project` scope.
For an unattended timer, also set `sources.asana.comment_authors` to the user gids whose comments may start a follow-up session (empty means any author, including guests and collaborators). Also give `rules[].project` a non-empty list; an empty list is rejected.
3. Run `agent-bus dispatch run --dry-run` and read the decisions. It makes read-only requests and changes nothing, and applies the per-run and session limits as a real run would.
4. Run once for real: `agent-bus dispatch run --ignore-hours`.
5. Check the result with `agent-bus dispatch status` and `agent-bus status`.
6. Run `agent-bus dispatch install-timer` from a shell whose `PATH` has `tmux` and your clients. It prints the files it wrote and loads nothing. Only when the owner says so, run `agent-bus dispatch install-timer --load`.

To remove it: `agent-bus dispatch uninstall-timer --unload`.

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
| Two sessions of one client share an inbox | Session identities are disabled, an old server is still running, or the client supplies no usable thread identity | Check `AGENT_BUS_SESSIONS` is not `0`; upgrade and restart the MCP connections. For Codex, call `whoami` in each thread and compare addresses. For other clients without thread ids, use separate client processes |
| A session's hook surfaces only `to any <repo>/<client> session` mail | The hook could not tell which session it runs for (e.g. a wrapper script between the client and `agent-bus serve`) | Start the server with `exec`, or call `read_inbox` for the session's own mail |
| Two different repos share an inbox | Same directory basename, wired in separate runs | Pin one with `.agent-bus-name`, or wire both in one `--scan` |
| `upgrade agent-bus` on any command | The database was written by a newer version | Upgrade the package on this machine |
| Every turn is held open quoting an error | An unusable binary — only possible with hand-written hooks | Re-run `init`, which appends `\|\| true` so a broken binary is silent |
| An idle Claude Code session is not woken by mail | Claude Code older than 2.1.290 when `init` ran (see its note), a session that is not an interactive terminal one (`claude -p`, the SDK, an IDE), a `wake.json` entry for the agent, `AGENT_BUS_NATIVE_WAKE=0`, the hourly limit, or the session idle for longer than `AGENT_BUS_WATCH_HOURS` | Upgrade Claude Code and re-run `init`; check `agent-bus tail` for `rate-limited` |
| An idle Codex session is not woken | It has not had a turn since its hooks were approved, its turn was interrupted with Esc, or `codex` cannot be run by the sender | Send it one prompt; set `AGENT_BUS_CODEX_BIN`; check `agent-bus tail` for `fired:ERR` |
| An idle agy session is not woken | Its agent has not run `agent-bus wake-register` (the user declined, or refused the permission), an agy whose `agentapi` has no `send-message`, a `wake.json` entry for the agent, `AGENT_BUS_NATIVE_WAKE=0`, or the hourly limit | Check `agent-bus tail` for `unregistered`; ask the agent to run `agent-bus wake-register --client agy` and allow it; set `AGENT_BUS_AGY_BIN` if the sender cannot run `agy` |
| `spawn` says tmux was not found | tmux is not installed or not on `PATH` | Install tmux, or set `spawn.tmux_bin`; spawn printed the command to run by hand |
| A spawned worktree session lacks the project memory | Memory lives outside the repo, per folder | Claude links it by default through `spawn.clients.claude.worktree_links` (source `~/.claude/projects/{clone_key}/memory`, target `~/.claude/projects/{work_dir_key}/memory`); the source must exist. Set the list to `[]` in `config.json` to switch it off |
| A spawned session stays `starting`, then disappears | The repo is not wired for that client, or the worktree lacks the wiring; or the client is waiting on a folder-trust or MCP-approval prompt: it shows `starting`, then `ended` after `spawn.startup_grace_seconds`, and becomes live again once it joins | Run `agent-bus init` for the repo and client (for Claude Code this also approves the server). In a worktree, untracked wiring files are linked in, and for Claude Code spawn writes a worktree-local `.claude/settings.local.json` (server approval, plus the hooks when the committed `.claude/settings.json` lacks them); other tracked wiring files keep their committed copy and spawn prints a note naming them. Answer a folder-trust prompt in the tmux window (Antigravity CLI asks in a new folder; Claude Code and Codex do not inside `<clone>/.worktrees`), or raise `spawn.startup_grace_seconds` in `config.json` |
| A spawned Codex session is not linked to the spawn in `status` | Codex is wired once per user and does not pass `AGENT_BUS_SESSION` to MCP servers, so it registers under the folder's name | Wire the repo with `agent-bus init --clients codex`, which passes the variable through |
| A spawned opencode session joins as a stranger | opencode's shared background service gave its MCP server the environment of an earlier launch | The default template uses `opencode --standalone`; keep it in custom templates |
| `claude --resume` in the repo does not list a spawned session | The session ran in a worktree, and the picker shows the current folder only | Run the `resume:` command `agent-bus dispatch status` prints, `claude --resume <label>` from the repo, or press Ctrl+W in the picker to list every worktree |
| `environment variable ... is not set` when the timer runs | launchd and systemd do not inherit your shell environment | Set `dispatch.env_file` to a `chmod 600` file with the `KEY=VALUE` line |
| The timer runs but nothing happens | Outside `dispatch.working_hours`, the source is not `enabled`, or another run holds the lock | Read `dispatch.log` and `dispatch-timer.log` beside the database; try `agent-bus dispatch run --dry-run --ignore-hours` |
| `dispatch` reports `spawn cap reached` | `spawn.max_sessions` or `dispatch.max_spawns_per_run` is reached | Wait for sessions to report `done`, or raise the limit in `config.json` |
| A task is not picked up | Its project is not in `projects`, it has no due date or is due after the lookahead, it is not assigned to the token's user, or no rule matches | Check with `dispatch run --dry-run --json`; adjust `projects`, `lookahead_days` or `rules` |
| The timer cannot find tmux or the client | The unit's `PATH` is the one of the shell that ran `install-timer` | Re-run `install-timer` from a shell whose `PATH` has them |
| `spawn` exits 4 | The cap (`spawn.max_sessions`, default 5) is reached | Wait, have a finished session report `done`, or raise the cap in `config.json` |
| `spawn --repo` says the repo is ambiguous | Two different clones match | Give the full id or the bus name, or remove the stray clone from `repos.roots` |
| A woken agent and another keep waking each other | Two agents answering every message | The hourly limit (`AGENT_BUS_WAKES_PER_HOUR`, default 20) stops it; lower it if needed |

Diagnostics:

```bash
agent-bus agents --json     # who is registered, and each unread count
agent-bus status --json     # sessions with state, client, tmux window, folder
agent-bus tail --limit 20   # the audit log: send, read, deliver, claim, wake, retire, session
agent-bus tail -f           # follow it live
agent-bus init --json --clients <c>   # what the wiring should say, without writing
```

## Reference

- Addressing, claiming, the schema and the tool surface: [README](../README.md)
- Waking an idle agent when mail arrives, and `wake.json`: the README's *Waking idle agents*
- Environment variables: the README's *Environment variables*

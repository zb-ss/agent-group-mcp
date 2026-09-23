# agent-bus setup runbook

A step-by-step install and configuration guide written to be handed to a
coding agent: "set up agent-bus by following `docs/AGENT_SETUP.md`". Every
step has a command and the output that means it worked. A human can follow
it too.

Scope: one machine, one user. Everything is local — SQLite plus an
append-only log under `~/.claude-agent-bus/`. No network calls, no daemon.

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

Expected: `agent-bus 0.5.1` or newer — 0.5.0 hangs every opencode turn,
so upgrade it if that is what you see. The distribution is
`agent-group-mcp`; the command it installs is `agent-bus`.

> Upgrading from 0.4.x, or the package was once installed under the old
> name `agent-bus`? Run `pipx uninstall agent-bus` first so only one
> package owns the command, then install as above. Read
> [Upgrading](#9-upgrading-from-04x) before wiring anything.

To install from a source checkout instead:

```bash
pipx install /path/to/agent-group-mcp --force
```

## 3. Choose which clients to wire

`init` writes config for the clients you name. Pick from what is actually
installed:

| Client | `--clients` value | Detect with |
| --- | --- | --- |
| Claude Code | `claude` | `command -v claude` |
| Codex CLI | `codex` | `command -v codex` |
| Antigravity CLI | `agy` | `command -v agy` |
| opencode | `opencode` | `command -v opencode` |

```bash
agent-bus init --help | grep -A2 -- --clients
```

If only one client is installed, wire only that one. Wiring a client you
do not use is harmless but leaves unused files in the repo.

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

**Names.** Each agent is `<repo>/<client>` — `my-repo/claude`,
`my-repo/codex`. The repo part is the slug of the directory name. To pin
it, put one line in `.agent-bus-name` in that repository. To keep a
repository off the bus entirely, create an empty `.agent-bus-ignore`
there: `init` skips it, the server refuses to start, and hooks stay quiet.

**Read the notes `init` prints at the end.** They state what each client
cannot do, and steps 5 and 6 follow from them.

## 5. Activate the clients that ask for it

Two clients deliberately ignore repository-local config until a human
approves it. Neither can be scripted; ask the user to do it.

**Codex** — open the repository with `codex`, accept the prompt to trust
the project, then run `/hooks` and approve the two `agent-bus` entries.
Codex ties approval to the exact hook command, so it asks again whenever
the wiring changes.

**Antigravity CLI** — open the repository with `agy` and trust the folder
when asked. The workspace plugin loads immediately after that; no restart
needed.

Claude Code and opencode need no approval step.

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
`my-repo/codex`.

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
#     whoami   -> its own name, its repo, and the other agents in that repo
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
keeps the turn open until it has handled it.

## 9. Upgrading from 0.4.x

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

`.agent-bus-name` and `.agent-bus-ignore` files keep working unchanged, as
do `wake.json` entries keyed by the old name — those now apply to every
agent in that repository.

**If you wired a client by hand** rather than with `init`, update it
yourself: set `AGENT_BUS_NAME` to `<repo>/<client>`, and add
`--client <id>` to the hook commands. `--client` also selects the output
format that client expects, which matters — Codex in particular discards a
hook whose output starts with `[` but is not JSON.

## 10. More than one machine

The generated wiring is per-machine: it contains absolute paths to a
`pipx` venv. If you sync dotfiles or repositories between machines:

- Do not sync the generated files. Run `agent-bus init` on each machine.
- If you sync a **user-level** client config that invokes `agent-bus`,
  every machine needs a version whose CLI accepts the flags in it. Install
  or upgrade the package on each machine **before** the config reaches it,
  or an older binary will reject the arguments.
- The database and audit log are per-machine too. Agents on different
  machines cannot see each other; the bus is local by design.

## 11. Removing it

```bash
agent-bus forget <name>            # one agent off the roster
agent-bus forget --group <repo>    # every agent of one repository
```

Message history is kept either way; a forgotten agent reappears if its
client reconnects.

To unwire a repository, delete the generated files listed in step 7 from
it. To remove everything, `pipx uninstall agent-group-mcp` and delete
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
| Two sessions of one client share an inbox | Both resolve to the same name | Start the second with `AGENT_BUS_INSTANCE=2` |
| Two different repos share an inbox | Same directory basename, wired in separate runs | Pin one with `.agent-bus-name`, or wire both in one `--scan` |
| `upgrade agent-bus` on any command | The database was written by a newer version | Upgrade the package on this machine |
| Every turn is held open quoting an error | An unusable binary — only possible with hand-written hooks | Re-run `init`, which appends `\|\| true` so a broken binary is silent |

Diagnostics:

```bash
agent-bus agents --json     # who is registered, and each unread count
agent-bus tail --limit 20   # the audit log: send, read, deliver, claim, wake, retire
agent-bus tail -f           # follow it live
agent-bus init --json --clients <c>   # what the wiring should say, without writing
```

## Reference

- Addressing, claiming, the schema and the tool surface: [README](../README.md)
- Waking an idle agent when mail arrives: the README's *Waking idle agents*
- Environment variables: the README's *Environment variables*

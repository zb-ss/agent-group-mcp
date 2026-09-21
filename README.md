# agent-bus

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python: 3.11+](https://img.shields.io/badge/Python-3.11+-blue.svg)](pyproject.toml)

Local MCP server + companion CLI that lets coding agents on one machine
**exchange messages through a shared, persistent bus** — with full audit
logging and hooks for instant reaction at every turn boundary. Agents can
live in different repos, and several different MCP clients (Claude Code,
Codex, Antigravity CLI, opencode) can work in the **same** repo at once,
each with an inbox of its own.

- **Zero external services.** Local-only. Filesystem + SQLite (WAL mode).
- **One MCP server process per client session**, all sharing one DB.
- **One identity per client per repo** — `repo-a/claude`, `repo-a/codex` —
  and the bare repo name `repo-a` reaches everyone working in it.
- **Humans are first-class participants** via the `agent-bus` CLI
  (`send`, `inbox`, `tail -f`, `chat` with a colored TUI).
- **Hook-driven reactivity** — peers see new messages at the start of
  their next turn, and a Stop hook keeps an agent on the line until it
  has handled its inbox.
- **Open-source friendly:** MIT, no telemetry, no network calls, all
  state lives under `~/.claude-agent-bus/` (gitignored by default).

---

## Install

### Recommended: pipx (global)

```bash
pipx install agent-group-mcp
```

This puts the `agent-bus` CLI on your `$PATH` (typically
`~/.local/bin/agent-bus`) in an isolated venv that pipx manages. Every
MCP client session, every repo, and every shell can call it without
sourcing anything. To upgrade later: `pipx upgrade agent-group-mcp`.

### Alternative: from source (development)

```bash
git clone git@github.com:zb-ss/agent-group-mcp.git
cd agent-group-mcp
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Use this when you want to hack on the source — `pytest` runs from the
same venv. **Don't** wire this venv's paths into your `.mcp.json` /
hooks; use the pipx install for that. The two installs coexist fine.

---

## Architecture

- **Language:** Python 3.11+
- **MCP transport:** stdio. Each `claude` spawns its own MCP subprocess.
  No long-lived daemon, no port management.
- **SDK:** official `mcp` Python package (FastMCP).
- **Storage:** SQLite at `$AGENT_BUS_DB`
  (default `~/.claude-agent-bus/bus.db`), WAL mode + `synchronous=NORMAL`.
- **Audit log:** append-only JSON-lines at `$AGENT_BUS_AUDIT_LOG`
  (default `~/.claude-agent-bus/audit.log`).

### Identity from config (not from arguments)

Each MCP server works out who it is at startup, upserts the `agents` row,
and silently attaches its name to every tool call. **`send_message` /
`read_inbox` take no `from` or `name` argument** — the server already
knows its identity. The hook commands use the same rule:

1. **`AGENT_BUS_NAME`** is used as given (with `AGENT_BUS_REPO` as the
   repo path). This is what `agent-bus init` writes into per-repo configs.
2. Otherwise a **client id** — `--client codex` or `AGENT_BUS_CLIENT` —
   plus the repo: `AGENT_BUS_REPO`, else the directory the hook payload
   reports, else the working directory, walked up to the repository root.
   The name is `<repo>/<client>`. This lets a client whose hooks are
   configured once per user drain the right inbox in every repo, and stay
   silent in repos that are not on the bus.
3. **`AGENT_BUS_INSTANCE=2`** appends `-2`, for a second session of the
   same client in the same repo (see below).

The server exits with an error if none of this identifies it.

### Names

| Form | Meaning |
| --- | --- |
| `repo-a/claude` | one client working in `repo-a` |
| `repo-a/claude-2` | a second concurrent session of that client |
| `repo-a` | the repo: every agent working in it (or an agent wired under the bare name, see [Upgrading](#upgrading-an-existing-install)) |
| `human`, `alex` | anyone without a client part — the CLI, the chat TUI |

The repo part is the slug of the repo's basename (`~/websites/acme.dev` →
`acme-dev`, at most 40 characters). A client id is lowercase letters and
digits, at most 12 characters. `/` can never appear in a repo slug, so a
repo literally called `tools-claude` cannot collide with client `claude`
in repo `tools`.

---

## MCP tools

All tools are auto-attributed to the server's identity.

| Tool                                         | Returns                                                                                                                                |
| -------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| `whoami()`                                   | `{name, group, client, instance, repo_path, registered_at, last_seen, mcp_client, group_members, warnings}`                             |
| `list_agents(group=None)`                    | `[{name, group, client, repo_path, last_seen, pending_count}, …]`; pass a bare repo name as `group` to see one repo                     |
| `send_message(to, body, thread_id=None)`     | `{to, kind, message_ids, recipients, thread_id, sent_at}`, plus `message_id` when exactly one agent received it                        |
| `read_inbox(mark_read=True, limit=50)`       | Unread messages for self, oldest first: `[{message_id, from, to, addressed_to, kind, claimed_by, body, sent_at, thread_id, read_at, delivered_at}]` |
| `read_thread(thread_id, limit=100)`          | Full conversation across participants, ordered by `sent_at`.                                                                           |
| `tail_audit(limit=50)`                       | Last N audit-log entries (any agent).                                                                                                  |

### Addressing

| `to`            | Reaches                                         | `kind`      |
| --------------- | ----------------------------------------------- | ----------- |
| `repo-a/codex`  | exactly that agent                              | `direct`    |
| `repo-a`        | every agent in `repo-a`, except the sender      | `group`     |
| `*`             | every agent on the bus, except the sender       | `broadcast` |

A name nobody holds is an **error**, not a dead letter: nothing is sent and
the message lists the agents of that repo (or close matches). A group or
broadcast with nobody else to receive it is not an error — `recipients` is
simply empty.

> **One row per recipient.** The schema keeps `message_id` as the row PK
> and tracks `read_at` per recipient, so a group or broadcast send fans out
> into **N rows with N distinct `message_id`s**. The audit log shows one
> `send` row per recipient, which makes per-peer delivery easy to grep.

### Several agents in one repo

A message to a repo — and most mail is addressed that way, because the
repo name is what peers know — lands in the inbox of every client there.
To keep four clients from all acting on "please bump the dependency", one
of them **claims** it:

- The **first agent of a repo to read** a group or broadcast message
  becomes its `claimed_by`, for every copy in that repo. This happens
  inside the read transaction, so two agents can never both claim.
- The claimer is asked to act: its Stop hook keeps the turn open, exactly
  as for a direct message.
- Its repo-mates still get their copy, but only as information: it does
  not block their Stop, and shows up at their next prompt marked
  `already picked up by repo-a/claude`.
- A broadcast is claimed once **per repo**, so the attention it costs
  grows with the number of repos, not the number of clients.
- To make a specific agent act, address it by its full name.

Fan-out also skips a client nobody has seen for
`AGENT_BUS_FANOUT_MAX_IDLE_DAYS` days (default 14; `0` disables) — but only
when a more recently seen client shares its repo, so a repo where everyone
has been away still gets its mail. Direct messages are never skipped. Use
`agent-bus forget` for a client you have stopped using.

A **second session of the same client** in the same repo reads the same
config file, so it would share a name — and an inbox — with the first.
Start it with `AGENT_BUS_INSTANCE=2` in its environment and it becomes
`repo-a/claude-2`. `whoami` warns when a name was last registered for a
different repo path, which is the other way two processes end up sharing
one mailbox.

---

## SQLite schema

```sql
agents(name PK, repo_path, registered_at, last_seen,
       group_name NULL, client NULL)
messages(message_id PK, from_agent, to_agent, body, thread_id,
         sent_at, read_at NULL, delivered_at NULL,
         addressed_to NULL, fanout_id NULL, to_group NULL, claimed_by NULL)
-- Indices on (to_agent, read_at), (thread_id, sent_at),
-- agents(group_name) and messages(fanout_id).
-- Startup: PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;
```

`messages` is the durable history. **Rows are never deleted** — `read_at`
just flips when a peer pulls a message. The audit log is the recovery
source of truth.

### Migrations

The schema is versioned with `PRAGMA user_version` and migrated forward
automatically the first time any `agent-bus` process opens the database;
processes starting at the same moment serialize on the write lock and the
steps run once. The first migration of an existing database saves a copy
next to it as `bus.db.pre-migrate-<version>`. There is no downgrade.

Migrations only ever **add** nullable columns, tables and indexes, so an
older `agent-bus` still works against a newer database — which matters
because MCP servers started before an upgrade keep running. What an older
version cannot do is expand a repo name into its agents; it writes one row
to the bare name, and the first agent of that repo to read picks it up.
A change older versions could not survive would bump the schema *epoch*,
which they refuse with a message to upgrade.

---

## Audit logging

Every `send_message`, `read_inbox`, hook delivery, and CLI `send` writes
one JSON-lines record to `audit.log` **before returning**. Row shape:

```json
{
  "ts": "<ISO8601 UTC>",
  "op": "send" | "read" | "deliver" | "claim" | "wake" | "retire",
  "actor": "<agent or 'human'>",
  "message_id": "...",
  "from": "...",
  "to": "<the agent this row is about>",
  "addressed_to": "<what the sender typed: a name, a repo, or *>",
  "thread_id": "...",
  "body_preview": "<first 200 chars>",
  "body_sha256": "<sha256 of full body>"
}
```

The full body lives only in SQLite — the log keeps a preview + hash so
it stays grep-friendly while remaining tamper-evident. Hook deliveries
write a paired `read` + `deliver` row so the log shows the hook path.
`claim` marks the agent that took a repo-wide message on, `wake` a wake
command that was launched, and `retire` a name that `agent-bus init`
replaced.

---

## Companion CLI

All subcommands hit the same SQLite store and audit log — no MCP round
trip — so a human can drive the bus from any shell even when no Claude
session is running.

```text
agent-bus init [PATHS...] [--clients a,b] [--scan] [--apply] [--force] [--prefix STR] [--name NAME]
agent-bus send BODY [--to NAME] [--thread ID] [--name NAME] [--json]
agent-bus inbox [--name NAME] [--limit N] [--peek] [--json]
agent-bus tail [-f] [--limit N] [--full] [--json]    # follow audit.log live
agent-bus chat [--name NAME]                 # colored TUI
agent-bus agents [--json]
agent-bus forget NAME                        # remove stale agent from roster
agent-bus forget --group REPO                # ... or every agent of a repo
agent-bus wake-config {show,set,clear,test} [NAME] [COMMAND]   # push-style alerts
agent-bus hook-stop [--client ID]            # "about to stop" hook
agent-bus hook-user-prompt [--client ID]     # "before the prompt" hook
agent-bus serve [--client ID]                # same as python -m agent_bus.server
```

**Identity defaults** for the CLI: `$AGENT_BUS_NAME` → `human`. That
identity is auto-registered in the `agents` table the first time it
sends, so peers can address you directly. Speaking as somebody else
(`--name repo-a/claude`) never changes where that agent is registered.

`agent-bus agents` lists a repo's clients under one line for the repo:

```
repo-a              repo=~/code/repo-a  2 agents
  repo-a/claude     seen 2m ago  pending=0
  repo-a/codex      seen 3h ago  pending=2
human               repo=~  seen just now  pending=0
```

If you end up with stale identities on the roster (e.g. you sent once
as the default `human`, then started using `--name alex` and now both
show up in `/agents`), run `agent-bus forget human` to drop the stale
row. Message history is preserved — forgetting only removes the agent
from the broadcast fan-out and the roster.

### Chat TUI (`agent-bus chat`)

Built on [prompt_toolkit](https://github.com/prompt-toolkit/python-prompt-toolkit)
so incoming messages never clobber the line you're typing on. Each agent
gets a stable color (sha1 of name → curated palette), timestamps are
shown in local `HH:MM:SS`, and thread IDs are truncated to 8 chars in the
display (full UUIDs still live in the DB and in `--json` output).

On connect you get:

```
agent-bus chat — connected as 'alex' (default → *). /help for commands.
agents on the bus (3):
  api-service  last seen 1m ago     pending=0
  web-app      last seen 4s ago     pending=2
  alex         last seen just now (you)  pending=0
recent activity (last 10 sends):
  14:30:51  web-app     → api-service  can you check the dashboard?
  14:31:02  api-service → web-app      on it [c7a3b2f0]
  ...

[alex → *] ▌
                                                 alex → *   /help · /quit
```

Commands inside the TUI:

```
@<name> <body>     direct message to one peer
/to <name|*|all>   set default target
/agents            list known agents + unread counts
/thread <id>       reprint a thread (8-char prefix is enough)
/history [N]       reprint last N 'send' rows
/clear             clear screen
/help, /quit, /exit
plain text         send to current default target (default '*')
```

Tab-completes slash commands. Command history persists at
`~/.claude-agent-bus/chat_history`.

### Non-chat subcommands

`inbox`, `agents`, and `tail` use the same per-agent colors and column
alignment by default. Add `--json` to any of them for raw,
machine-readable output (shape stable across releases):

```bash
agent-bus inbox --json
agent-bus agents --json
agent-bus tail --json | jq '.[] | select(.op=="send")'
```

By default `tail -f` renders one-line summaries instead of raw JSON, so
it stays readable while still being scriptable via `--json`.

---

## Wiring with `agent-bus init`

Hand-editing every client's config in every repo is a chore.
`agent-bus init` does it for you, once per client you ask for:

```bash
# preview every git repo under these roots (default: dry-run)
agent-bus init --scan ~/websites ~/projects

# review the plan, then write it
agent-bus init --scan ~/websites ~/projects --apply

# init just the current directory (writes immediately)
agent-bus init

# wire several clients into one repo
agent-bus init ~/projects/foo --clients claude,codex,opencode

# pin the repo's name
agent-bus init ~/projects/foo --name myname
```

`--clients` defaults to `claude`. Each client is wired as
`<repo>/<client>`; the MCP server is called `agent-bus` in every client, so
tool names do not depend on which client you are in.

| Client          | `--clients` | Files written in the repo                                   | Mail shown                      | Kept from stopping             |
| --------------- | ----------- | ----------------------------------------------------------- | ------------------------------- | ------------------------------ |
| Claude Code     | `claude`    | `.mcp.json`, `.claude/settings.json`                        | `UserPromptSubmit` hook         | `Stop` hook                    |
| Codex           | `codex`     | `.codex/config.toml`, `.codex/hooks.json`                   | `UserPromptSubmit` hook         | `Stop` hook                    |
| Antigravity CLI | `agy`       | `.agents/plugins/agent-bus/` (a workspace plugin)           | `PreInvocation` hook            | `Stop` hook                    |
| opencode        | `opencode`  | `.opencode/opencode.json`, `.opencode/plugins/agent-bus.js` | plugin, before each model call  | not possible — see below       |

What differs per client:

- **Claude Code** — also gets the six `mcp__agent-bus__*` entries added to
  `permissions.allow` (deduped if already present).
- **Codex** — loads a repo's `.codex/` files only once the project is
  trusted, and runs each hook only after you approve it under `/hooks`.
  Approval is tied to the hook's definition, so the hook commands carry no
  agent name (the hook finds its agent from the payload's `cwd`) and are
  identical in every repo. Python can read TOML but not write it, so the
  server table sits between two marker comments and only that span is ever
  rewritten; a `[mcp_servers.agent-bus]` table of your own is left alone,
  even with `--force`. Codex starts MCP servers with a minimal
  environment, so `AGENT_BUS_INSTANCE`, `AGENT_BUS_DB` and
  `AGENT_BUS_AUDIT_LOG` are passed through with `env_vars`.
- **Antigravity CLI** — only starts MCP servers configured per user or
  bundled with a plugin, so the repo is wired as a workspace plugin of its
  own; nothing of yours under `.agents/` is touched. The plugin loads once
  you have trusted the workspace in `agy`. There is no prompt-submit
  event: mail is injected before each model call as a transient system
  message. Check the result with `agy plugin validate
  .agents/plugins/agent-bus`.
- **opencode** — has no shell hooks; a small generated plugin runs the two
  hook commands. It adds pending mail to the system prompt before each
  model request (repeating it for the rest of the turn, since that
  addition is not kept in the transcript). opencode **cannot be kept from
  finishing a turn**: when mail arrives mid-turn, the plugin starts a
  follow-up turn as soon as the session goes idle. The injection uses an
  `experimental.*` plugin hook that may change between opencode releases.
  A `.opencode/opencode.json` containing comments cannot be merged and is
  skipped.

Everything `init` writes contains absolute paths for this machine — add the
files above to your `.gitignore` rather than committing them.

**A client that is not in the list** works too, as long as it can start a
stdio MCP server: give the server `AGENT_BUS_NAME=<repo>/<client>` and
`AGENT_BUS_REPO`, or start it as `agent-bus serve --client <id>` from
inside the repo. If its hooks follow the common contract (plain stdout
becomes context; `{"decision": "block", "reason": …}` keeps the turn
open), point them at `agent-bus hook-user-prompt --client <id>` and
`agent-bus hook-stop --client <id>`.

**Name derivation.** The repo's name is the slug of its basename:
`~/websites/acme.dev` → `acme-dev`, `~/projects/my_thing` → `my-thing`.
Collisions within one scan are resolved by prefixing the parent dir
(`projects-foo` vs `websites-foo`), and the plan warns when a name is
already registered for a different repo path.

**Per-repo overrides.**

- Drop a `.agent-bus-name` file in any repo containing a single line
  with the desired repo name. `agent-bus init` will use that instead of
  the slug. Useful for keeping a name other agents already know.
- Drop a `.agent-bus-ignore` file (empty) in any repo to opt it out of
  bulk init entirely.

**Idempotency.** Re-running `init` is safe: it detects its own previous
output and refreshes it without duplicating hooks or allow-list
entries. Hand-written `agent-bus` entries are left alone unless you
pass `--force`, and a config file that cannot be parsed is reported and
never overwritten.

**Useful flags.**

```
--clients a,b   which clients to wire: claude, codex, agy, opencode (default: claude)
--scan          treat paths as roots; walk for git repos
--apply         actually write (required for --scan; single-repo is implicit)
--force         overwrite hand-written agent-bus entries
--prefix STR    prepend a slug to every derived name (e.g. `--prefix work-`)
--name NAME     explicit repo name (single-repo init only)
--bin-path PATH override the agent-bus binary path written into the configs
--json          emit the plan as JSON without applying (includes each client's name)
```

### Upgrading an existing install

Before per-client identities, each repo had one agent named after the
repo (`acme-dev`). Those names keep working: an agent without a client
part is simply a repo with one agent in it, and mail to `acme-dev`
reaches it as before. Upgrading the package alone changes nothing.

1. Upgrade (`pipx upgrade agent-group-mcp`). The database is migrated
   the first time any command runs, after a copy is saved next to it.
   Sessions that are still running the old version keep working.
2. Preview the new wiring: `agent-bus init --scan <roots>`. A repo wired
   the old way shows as `refresh` with
   `renaming from 'acme-dev' to 'acme-dev/claude'`.
3. Apply it (`--apply`), adding `--clients …` for the other clients you
   use. The old name is retired from the roster. Unread mail addressed to
   it is not lost: `acme-dev` is now the repo's address, and the first
   agent there to read picks it up. A `.agent-bus-name` file keeps naming
   the repo, and `wake.json` entries keyed by the old name now apply to
   the whole repo — neither needs editing.
4. Restart the clients so they start the server under the new name.

If you wired a client yourself — a hand-written config, or one produced
by your own tooling — it keeps its old name until you change it: set
`AGENT_BUS_NAME` to `<repo>/<client>` there (`agent-bus init --json
--clients <client>` prints the exact name), and add `--client <id>` to its
hook commands. For Codex the second part is not optional: Codex parses
hook output that starts with `[` as JSON and discards the hook when that
fails, which is what the plain-text `[agent-bus] …` header looks like.
`--client codex` switches the prompt hook to the JSON form Codex expects.

---

## Wiring it up manually (single repo, no `init`)

Shown for Claude Code; for the other clients the quickest reference is to
run `agent-bus init --clients <client>` in a scratch repo and read what it
wrote.

### 1. `.mcp.json` (one per repo)

Each repo gets its own MCP server with its own identity. Assuming the
pipx install above, `agent-bus serve` is on `$PATH`:

```jsonc
{
  "mcpServers": {
    "agent-bus": {
      "command": "agent-bus",
      "args": ["serve"],
      "env": {
        "AGENT_BUS_NAME": "repo-a/claude",
        "AGENT_BUS_REPO": "/path/to/repo-a"
      }
    }
  }
}
```

If Claude Code's subprocess environment doesn't inherit `~/.local/bin`,
use the absolute pipx path instead, e.g.
`"command": "/home/<you>/.local/bin/agent-bus"`.

In repo B, the same file uses `"AGENT_BUS_NAME": "repo-b/claude"` and the
repo B path. Same `~/.claude-agent-bus/bus.db` is shared automatically.

### 2. `.claude/settings.json` (one per repo)

```jsonc
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "AGENT_BUS_NAME=repo-a/claude agent-bus hook-user-prompt --client claude"
          }
        ]
      }
    ],
    "Stop": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "AGENT_BUS_NAME=repo-a/claude agent-bus hook-stop --client claude"
          }
        ]
      }
    ]
  }
}
```

- `hook-user-prompt` reads the inbox, prints any pending messages, and
  exits 0 — so they show up as extra context for the next response.
- `hook-stop` reads the inbox; if there is nothing this agent must act on
  it exits 0 (allow stop). Otherwise it emits
  `{"decision":"block","reason":"…"}` so the agent keeps the turn open and
  replies via `send_message`.
- `--client` selects the output format the client expects and marks the
  hook as belonging to that client, so `init` can refresh it later without
  touching another client's hooks in the same file.

If you run the CLI from a different `$PATH`, replace `agent-bus` with the
absolute path to the script.

**Hooks configured once per user.** A client whose hooks live in a
user-level file cannot carry a per-repo name. Leave the name out —
`agent-bus hook-stop --client codex` — and the hook reads the repo from its
payload, looks up the agent that client registered there, and stays silent
in repos that are not on the bus.

### 3. Pre-approve the MCP tools (optional but recommended)

By default Claude Code will prompt you the first time it calls each
`mcp__agent-bus__*` tool. To skip those prompts, drop this allowlist
into the same `.claude/settings.json`:

```jsonc
{
  "permissions": {
    "allow": [
      "mcp__agent-bus__whoami",
      "mcp__agent-bus__list_agents",
      "mcp__agent-bus__send_message",
      "mcp__agent-bus__read_inbox",
      "mcp__agent-bus__read_thread",
      "mcp__agent-bus__tail_audit"
    ]
  }
}
```

---

## Worked example: three-way exchange

Open three terminals.

**Terminal A — Claude session in repo A** (`AGENT_BUS_NAME=alpha`):

```text
> Use the agent-bus MCP to greet beta and the human.
[claude calls send_message(to="*", body="hi everyone, alpha here")]
```

Audit log gains:

```json
{"op":"send","actor":"alpha","from":"alpha","to":"beta","body_preview":"hi everyone, alpha here", ...}
{"op":"send","actor":"alpha","from":"alpha","to":"human","body_preview":"hi everyone, alpha here", ...}
```

**Terminal B — Claude session in repo B** (`AGENT_BUS_NAME=beta`):

The next user prompt fires `hook-user-prompt`, which prints:

```
[agent-bus] 1 new message(s) since last turn:
- from alpha to everyone on the bus at 2026-05-13T... (thread <id>): hi everyone, alpha here
```

Claude replies with `send_message(to="alpha", body="hey alpha, beta here", thread_id="<same>")`. If Claude tries to stop without responding, the Stop hook blocks the turn with that exact reason payload, forcing it to handle the message first.

**Terminal C — human** running `agent-bus chat`:

```text
$ agent-bus chat
agent-bus chat: connected as 'human' (default target *).
[human → *] hello both
[chat] broadcast → alpha, beta (2 msg)

<alpha → human> [thread …] hi everyone, alpha here
[human → *] @alpha thanks for the ping!
[chat] sent → alpha (id 7c2a…)
```

All three participants see each other's messages and can chain replies
on the same `thread_id`.

---

## Recovery: reconstructing a thread from the audit log

`messages.body` is the only place full bodies live, but every send is
hashed and previewed in `audit.log`. To grep a thread:

```bash
# every event in thread <id>, in order
grep '"thread_id":"<id>"' ~/.claude-agent-bus/audit.log

# every send by alpha
grep '"op":"send".*"actor":"alpha"' ~/.claude-agent-bus/audit.log

# verify a message body's integrity (preview + hash)
sqlite3 ~/.claude-agent-bus/bus.db \
  "SELECT body FROM messages WHERE message_id='<id>'" \
  | sha256sum
# compare against body_sha256 in the matching audit row
```

If the SQLite file is lost, the audit log gives you sender, recipient,
timing, thread, body preview (first 200 chars), and a hash. Full body
recovery requires SQLite + audit cross-reference.

---

## Environment variables

| Variable                          | Used by            | Meaning                                                                                  |
| --------------------------------- | ------------------ | ---------------------------------------------------------------------------------------- |
| `AGENT_BUS_NAME`                  | server, hooks, CLI | The agent's full name. Wins over everything else.                                        |
| `AGENT_BUS_REPO`                  | server, hooks      | The repo this agent works in.                                                            |
| `AGENT_BUS_CLIENT`                | server, hooks      | Client id, same as `--client`; used to derive the name when `AGENT_BUS_NAME` is unset.   |
| `AGENT_BUS_INSTANCE`              | server, hooks      | `2`..`99`: this is another session of the same client in the same repo.                  |
| `AGENT_BUS_FANOUT_MAX_IDLE_DAYS`  | senders            | Skip long-idle clients in repo-wide and broadcast sends. Default `14`, `0` disables.     |
| `AGENT_BUS_DB`                    | everything         | Database path. Default `~/.claude-agent-bus/bus.db`.                                     |
| `AGENT_BUS_AUDIT_LOG`             | everything         | Audit log path. Default `~/.claude-agent-bus/audit.log`.                                 |
| `AGENT_BUS_BIN`                   | `init`             | The `agent-bus` path to write into configs (same as `--bin-path`).                       |

---

## Running the tests

```bash
pip install -e ".[dev]"
pytest
```

The suite covers:

- whoami / env-derived identity, and identity derived from `--client` + repo
- send → read round trip
- broadcast hits all peers, not the sender
- repo-wide addressing, unknown recipients, claiming (incl. two processes racing to claim)
- schema migrations: fresh and 0.4.x databases, concurrent starters, backup, newer-epoch refusal
- `init` per client: written files, idempotency, upgrade of old wiring, unparseable configs left alone
- the generated opencode plugin, driven with fakes under Node (skipped without `node`)
- audit log: one line per op, valid JSON, sha256 matches body, preview ≤ 200 chars
- thread retrieval ordering
- concurrent writers (2 subprocesses × 50 messages each, no loss, no dup `message_id`)
- `hook-stop` empty → exit 0, no stdout
- `hook-stop` with pending → exit 0, valid JSON with `decision: "block"` and bodies in `reason`
- `hook-user-prompt` with pending → bodies in stdout, inbox empty after
- `agent-bus tail -f` integration: send a message in a subprocess, assert it appears within 1s

---

## Contributing

PRs welcome. The codebase is small and intentionally stays that way.

- Install for development: `pip install -e ".[dev]"` (use a venv).
- Run the test suite: `pytest`.
- Style: no formatter pinned, but match what's already there. Type hints
  are encouraged but not enforced.
- All `messages` data and the audit log live under `~/.claude-agent-bus/`
  by default. Pytest fixtures redirect to a per-test `tmp_path` via the
  `AGENT_BUS_DB` / `AGENT_BUS_AUDIT_LOG` env vars — please use them in
  new tests so they never touch a developer's real bus.
- Never commit personal data, real agent names tied to private projects,
  or absolute filesystem paths. The `.gitignore` already excludes
  `*.db`, `audit.log`, `chat_history`, and `.claude-agent-bus/`.

## Waking idle agents

Agent CLIs (Claude Code, OpenCode, Gemini CLI) block on stdin when
idle, so a peer message landing in SQLite does **not** start a new
turn on its own. There is no MCP transport — stdio, SSE, or
Streamable HTTP — that fixes this, because the constraint is in the
client REPL, not the wire protocol.

agent-bus's answer is **wake-on-send**: a per-agent shell command runs
the instant a message arrives for that agent. The command is whatever
your environment makes feasible — drive emacs/vterm, drive a tmux
pane, fire a desktop notification, hit a webhook, ring a bell.

### How it works

1. You drop a `wake.json` next to `bus.db` (default:
   `~/.claude-agent-bus/wake.json`) mapping `agent_name → shell command`.
2. On every `send_message` (MCP tool *or* CLI), agent-bus looks up the
   recipient's entry and fires the command as a detached
   fire-and-forget subprocess. An entry keyed by a bare repo name
   (`repo-a`) covers every agent in that repo without an entry of its
   own; an agent's own entry always wins, including `false` to opt out.
   A message to a repo or to everyone wakes **at most one agent per
   repo** — the most recently seen one that has a command — because
   whoever wakes claims the message, and waking its repo-mates too would
   start a turn in each of them for nothing.
3. Every fire writes an `op="wake"` audit row alongside the `send`
   row, with the launch status (`fired:OK`, `fired:ERR:…`, `disabled`,
   `no-config`) so the bus log shows what happened.
4. The command receives routing info on **env vars**:
   `AGENT_BUS_FROM`, `AGENT_BUS_TO`, `AGENT_BUS_THREAD_ID`,
   `AGENT_BUS_MESSAGE_ID`, `AGENT_BUS_BODY_PREVIEW` (≤ 200 chars).
   The full message body is piped to **stdin as JSON**.
5. **Always quote env-var expansions in your wake command** — bodies
   are user-controlled. `notify-send "$AGENT_BUS_BODY_PREVIEW"` is
   safe; `notify-send $AGENT_BUS_BODY_PREVIEW` is shell-injection-prone.

### Managing wake.json

Edit it directly, or use the CLI helpers:

```bash
agent-bus wake-config show
agent-bus wake-config set web-app \
  'emacsclient -e "(with-current-buffer (get-buffer \"*vterm: web-app*\") (vterm-send-string \"check inbox\") (vterm-send-return))"'
agent-bus wake-config test web-app   # fire a synthetic wake to verify
agent-bus wake-config clear web-app
```

### Example wake commands

**emacs / vterm.** Requires `emacs --daemon` (or `M-x server-start`)
and a vterm buffer named per agent, e.g. `*vterm: web-app*`.
The command types into that buffer and submits, which fires
UserPromptSubmit in Claude Code (or the equivalent in OpenCode /
Gemini CLI), which lets the hook drain the inbox.

```bash
agent-bus wake-config set web-app \
  'emacsclient -e "(with-current-buffer (get-buffer \"*vterm: web-app*\") (vterm-send-string \"check inbox\") (vterm-send-return))"'
```

**Plain terminals (desktop notification).** When no multiplexer is in
the picture and you're at the desk, notify yourself and switch tabs:

```bash
agent-bus wake-config set web-app \
  'notify-send -a agent-bus "agent-bus → web-app" "$AGENT_BUS_FROM: $AGENT_BUS_BODY_PREVIEW"'
```

**tmux** (only if you do use it):

```bash
agent-bus wake-config set web-app \
  'tmux send-keys -t main:agents.0 "check inbox" Enter'
```

**Disable for a specific agent** (e.g. the human):

```bash
agent-bus wake-config set alex false   # or just omit the entry
```

### Failure modes

- Wake command crashes / exits nonzero: agent-bus doesn't notice
  (we don't await the process). The `op="wake"` audit row says
  `fired:OK` because the launch succeeded. Debug your wake command
  separately by running it yourself.
- `wake.json` is missing or malformed: silently no-op for every
  recipient. `agent-bus wake-config show` reports
  `(no wake commands configured)`.
- `emacsclient` can't reach an emacs server: command exits nonzero
  out-of-band. Run `emacsclient -e '(message "ping")'` once to verify
  before wiring it.

## Privacy & security

- All state is local. No network calls leave your machine.
- The audit log records message **previews** (first 200 chars) and
  **sha256 hashes** of bodies. Treat both the DB and the audit log as
  containing message content, and back them up / protect them
  accordingly.
- agent names + repo paths are recorded in the `agents` table and the
  audit log. Don't put secrets in agent names. Don't broadcast
  credentials over the bus.

## Out of scope (future work)

- **Online / cross-machine mode.** A Streamable HTTP MCP transport
  (`mcp.run_streamable_http_async()` — the successor to SSE) would
  let agents on different machines join one bus, expose a web/mobile
  UI for the human, and accept webhooks from external integrations as
  first-class senders. Not yet built; the SQLite + stdio design is
  intentional for now to keep the install lightweight and the trust
  story simple. `wake.json` is forward-compatible — wake commands
  fire from wherever `send_message` runs, so an HTTP mode later
  reuses the same config.
- Authentication (local-only, single-user assumed).
- Message expiry / log rotation.
- Synchronous request/reply within a single tool call (would require
  the peer's session to be actively running).
- **Push wake without `wake.json`.** Anthropic ships
  `notifications/claude/channel` for exactly this, but (a) it's
  Claude-Code-only and we stay client-neutral (OpenCode and Gemini
  CLI need to participate too), and (b) the upstream wake path has
  known open bugs (`#44380` and dozens of duplicates). `wake.json`
  is the portable answer until the spec + clients converge.

---

## License

MIT.

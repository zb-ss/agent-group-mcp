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
- **Idle agents wake up** when mail arrives — through Claude Code's,
  Codex's, Antigravity CLI's and opencode's own mechanisms, with no
  terminal multiplexer involved — or through a command of your own
  (`wake.json`).
- **Open-source friendly:** MIT, no telemetry, no network calls, all
  state lives under `~/.claude-agent-bus/` (gitignored by default).

> **Setting this up?** [`docs/AGENT_SETUP.md`](docs/AGENT_SETUP.md) is a
> step-by-step install and configuration runbook, written so you can hand it
> to a coding agent: *"set up agent-bus by following docs/AGENT_SETUP.md"*.
> Every step says what output means it worked. This README explains how the
> bus behaves and why.

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

Check what you have with `agent-bus --version`. If the package was once
installed under its older name, run `pipx uninstall agent-bus` first so a
single package owns the command.

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
  No long-lived daemon, no port management. Waking an idle session takes
  one small background hook process per session, which ends with it.
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
3. **`AGENT_BUS_SESSION=<label>`** (or **`AGENT_BUS_INSTANCE=<n>`**) asks
   for a particular session address (see [Sessions](#several-sessions-of-one-client)).

The server exits with an error if none of this identifies it. A server that
speaks for a client then claims a **session address** of its own, so that
two sessions of the same client in the same repo never share an inbox.

### Names

| Form | Meaning |
| --- | --- |
| `repo-a/claude-1`, `repo-a/claude-frontend` | one session of a client in `repo-a`: numbered by the bus, or labelled |
| `repo-a/claude` | that client in `repo-a`: the address all of its sessions share |
| `repo-a` | the repo: every agent working in it (or an agent wired under the bare name, see [Upgrading](#upgrading-an-existing-install)) |
| `human`, `alex` | anyone without a client part — the CLI, the chat TUI |

The repo part is the slug of the repo's basename (`~/websites/acme.dev` →
`acme-dev`, at most 40 characters). A client id is lowercase letters and
digits, at most 12 characters. `/` can never appear in a repo slug, so a
repo literally called `tools-claude` cannot collide with client `claude`
in repo `tools`. A session handle is a number from 1 to 99, or a label of
lowercase letters, digits and single dashes that starts with a letter, at
most 24 characters.

**Repo identity.** Besides its name, a repo has an id: its normalised git
remote URL, written `host/path` — lowercase, with no scheme, user, port,
trailing `/` or `.git`, and with host aliases applied (`repos.host_aliases`
in [`config.json`](#configuration-configjson); `ssh.github.com` counts as
`github.com`). `git@github.com:Example-Org/Repo-A.git` and
`https://github.com/example-org/repo-a` are both `github.com/example-org/repo-a`,
on every machine. Group names are unchanged: they stay the local alias
(`--name`, then `.agent-bus-name`, then the folder name). The id is an extra
key, cached in the `repos` table, that `agent-bus spawn --repo` and
`agent-bus status` use to find a repo's clone on the machine they run on:
first from the roster, then by scanning `repos.roots`.

---

## MCP tools

All tools are auto-attributed to the server's identity.

| Tool                                         | Returns                                                                                                                                |
| -------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| `whoami()`                                   | `{name, group, client, client_address, session, instance, topic, state, state_at, repo_path, registered_at, last_seen, mcp_client, group_members, warnings}` |
| `list_agents(group=None)`                    | `[{name, kind, group, client, topic, state, state_at, repo_path, last_seen, pending_count, live}, …]`; pass a bare repo name as `group` to see one repo; `kind` is `session`, `client` or `agent`, and `live` says whether a running server holds a session |
| `send_message(to, body, thread_id=None)`     | `{to, kind, message_ids, recipients, thread_id, sent_at}`, plus `message_id` when exactly one agent received it and `note` when no running session is there to read it |
| `read_inbox(mark_read=True, limit=50)`       | Unread messages for this session — its own, plus any sent to its client address that no other session took — oldest first: `[{message_id, from, to, addressed_to, kind, claimed_by, body, sent_at, thread_id, read_at, delivered_at}]` |
| `set_session(label=None, topic=None, state=None)` | Renames this session to `<repo>/<client>-<label>` and/or sets the topic shown in `list_agents`, and/or reports its state (`working`, `waiting` or `done`; anything else is rejected): `{name, client_address, topic, state}` |
| `read_thread(thread_id, limit=100)`          | Full conversation across participants, ordered by `sent_at`.                                                                           |
| `tail_audit(limit=50)`                       | Last N audit-log entries (any agent).                                                                                                  |

### Addressing

| `to`                 | Reaches                                                                 | `kind`      |
| -------------------- | ----------------------------------------------------------------------- | ----------- |
| `repo-a/codex-frontend` | exactly that session                                                    | `direct`    |
| `repo-a/codex`       | whichever session of that client reads it first (never the sender)     | `direct`    |
| `repo-a`             | every client in `repo-a`, except the sender                            | `group`     |
| `*`                  | every agent on the bus, except the sender                              | `broadcast` |

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

`whoami` warns when a name was last registered for a different repo path,
which is the way two repos end up sharing one mailbox.

### Several sessions of one client

Every session of a client in a repo reads the same config file, so the
config can only say *which client* it is. The session address comes from
the bus: each MCP server claims one when it starts.

- The first session is `repo-a/claude-1`, the next `repo-a/claude-2`, and
  so on — the lowest number no running session holds.
- A session can be named after its work instead: `set_session(label=
  "frontend", topic="login form")` makes it `repo-a/claude-frontend`, and its
  unread mail moves with it. To start with a name, set
  `AGENT_BUS_SESSION=frontend` in the client's environment.
- `repo-a/claude` is shared by all of them. A message sent there is taken
  by whichever session reads it first, so it suits "any Claude in this
  repo, please"; a session never receives what it sent there itself.
  Address a session by its own name when it has to be that one.
- A repo-wide or broadcast message costs one copy per client, delivered
  at its shared address, and the claiming rules above apply unchanged.
- Codex threads get separate addresses even when one app-server process
  starts all their MCP servers. The first tool call binds the address to
  Codex's thread id. Call the agent-bus MCP tool `whoami` to discover it,
  and include `set_session` in `enabled_tools` if your Codex MCP
  configuration uses an allowlist.
- The hooks find their session through the client process that started
  both them and its MCP server (and, for Claude Code, its session id).
  Codex hooks require the matching thread id; before the first MCP call
  they can deliver shared mail only. A hook that cannot tell which
  session it runs for surfaces only the shared
  address's mail — never another session's own.
- A session ends with its server process. A numbered address is then given
  up and its unread mail moves to the shared address; a labelled one is
  kept, mail included, for whichever session takes that label next. For
  `AGENT_BUS_SESSION_RESUME_HOURS` (default 1) the address also stays
  reserved for the same client process or session id, so a restarted MCP
  server or a resumed conversation gets its old address back.
- Whether a session's process has ended is decided on evidence only. If
  the process table cannot be read, or the process lives in another PID
  namespace, the session counts as running.
- For clients without per-call thread ids, servers that cannot be told
  apart — one process starting several with no session ids to separate
  them — share one session address, which is
  how every session of a client behaved before. Such an address can carry
  a topic but not a label, since a label would rename all of them.

`list_agents` and `agent-bus agents` show each session under its client,
with whether it is running and its topic. Set `AGENT_BUS_SESSIONS=0` to go
back to one shared identity per client and repo.

---

## SQLite schema

```sql
agents(name PK, repo_path, registered_at, last_seen,
       group_name NULL, client NULL, topic NULL,
       state NULL, state_at NULL)
messages(message_id PK, from_agent, to_agent, body, thread_id,
         sent_at, read_at NULL, delivered_at NULL,
         addressed_to NULL, fanout_id NULL, to_group NULL, claimed_by NULL)
sessions(server_pid, server_started, client_address,   -- PK
         name, repo_path, lineage, session_key NULL, started_at, last_seen)
repos(repo_path PK, repo_id NULL, checked_at)       -- cached remote ids
spawns(name PK, client, repo_id NULL, repo_path, work_dir, model NULL,
       prompt_file, tmux_session, tmux_window, remote, spawned_at)
-- Indices on (to_agent, read_at), (thread_id, sent_at),
-- agents(group_name), messages(fanout_id) and sessions(client_address).
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
  "op": "send" | "read" | "deliver" | "claim" | "wake" | "retire" | "session",
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
command that was launched, `retire` a name that `agent-bus init`
replaced, and `session` a session address being claimed, renamed or given
up (with how much unread mail moved).

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
agent-bus wake-config {show,set,clear,test} [NAME] [COMMAND]   # your own wake commands
agent-bus hook-stop [--client ID]            # "about to stop" hook
agent-bus hook-user-prompt [--client ID]     # "before the prompt" hook
agent-bus hook-watch [--client ID] [--max-seconds N]   # background hook: wake on new mail
agent-bus wake-register [--client ID]        # run by an agy agent when asked: let mail wake it
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

`inbox`, `agents`, `status` and `tail` use the same per-agent colors and column
alignment by default. Add `--json` to any of them for raw,
machine-readable output (shape stable across releases):

```bash
agent-bus inbox --json
agent-bus agents --json
agent-bus status --json
agent-bus tail --json | jq '.[] | select(.op=="send")'
```

Task dispatch has its own commands, described in
[Dispatching tasks](#dispatching-tasks-agent-bus-dispatch):
`dispatch run`, `dispatch status`, `dispatch install-timer` and
`dispatch uninstall-timer`.

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

| Client          | `--clients` | Files written in the repo                                   | Mail shown                      | Kept from stopping             | Woken when idle                    |
| --------------- | ----------- | ----------------------------------------------------------- | ------------------------------- | ------------------------------ | ---------------------------------- |
| Claude Code     | `claude`    | `.mcp.json`, `.claude/settings.json`, `.claude/settings.local.json` | `UserPromptSubmit` hook         | `Stop` hook                    | background `asyncRewake` hook      |
| Codex           | `codex`     | `.codex/config.toml`, `.codex/hooks.json`                   | `UserPromptSubmit` hook         | `Stop` hook                    | the sender runs `codex queue`      |
| Antigravity CLI | `agy`       | `.agents/plugins/agent-bus/` (a workspace plugin)           | `PreInvocation` hook            | `Stop` hook                    | the sender runs `agy agentapi`     |
| opencode        | `opencode`  | `.opencode/opencode.json`, `.opencode/plugins/agent-bus.js` | plugin, before each model call  | not possible — see below       | plugin                             |

How idle agents are woken is described in [Waking idle agents](#waking-idle-agents).

What differs per client:

- **Claude Code** — also gets the `mcp__agent-bus__*` entries added to
  `permissions.allow` (deduped if already present), and a background
  `hook-watch` hook on `SessionStart` and `Stop` that wakes an idle session.
  That hook is written only for Claude Code 2.1.290 or later: `init` asks
  `claude --version` (`AGENT_BUS_CLAUDE_BIN` names another executable)
  and, when the answer is older or missing, leaves it out and says so.
  Claude Code starts a repo's `.mcp.json` server only once it is approved,
  and an unattended (spawned) session cannot answer that prompt. `init`
  therefore also adds `"agent-bus"` to `enabledMcpjsonServers` in the
  untracked `.claude/settings.local.json`, keeping every other key and
  entry and rewriting nothing when it is already there. A file that is not
  a JSON object is never overwritten (the repo is skipped like any
  unreadable config). When that file, or its folder, resolves outside the
  repo, it is not written: the plan says so and you approve the server
  yourself. Keep it out of git like the other wiring files.
- **Codex** — loads a repo's `.codex/` files only once the project is
  trusted, and runs each hook only after you approve it under `/hooks`.
  Approval is tied to the hook's definition, so the hook commands carry no
  agent name (the hook finds its agent from the payload's `cwd`) and are
  identical in every repo. Python can read TOML but not write it, so the
  server table sits between two marker comments and only that span is ever
  rewritten; a `[mcp_servers.agent-bus]` table of your own is left alone,
  even with `--force`. Codex starts MCP servers with a minimal
  environment, so `AGENT_BUS_SESSION`, `AGENT_BUS_INSTANCE`, `AGENT_BUS_DB`
  and `AGENT_BUS_AUDIT_LOG` are passed through with `env_vars`. When one
  Codex process starts the MCP servers of several sessions, the bus cannot
  tell those sessions apart and they share one session address. Its hooks
  also record each session's thread id, so that a sender can wake it with
  `codex queue`.
- **Antigravity CLI** — only starts MCP servers configured per user or
  bundled with a plugin, so the repo is wired as a workspace plugin of its
  own; nothing of yours under `.agents/` is touched. The plugin loads once
  you have trusted the workspace in `agy`. There is no prompt-submit
  event: mail is injected before each model call as a transient system
  message. Check the result with `agy plugin validate
  .agents/plugins/agent-bus`. A sender wakes an idle session with `agy
  agentapi send-message`, which needs credentials agy gives only to the
  commands its agent runs; so the first model call of each session asks
  the agent to run `agent-bus wake-register` once, and agy asks you to
  allow that command — allow it always, and it stops asking.
- **opencode** — has no shell hooks; a small generated plugin runs the two
  hook commands. It adds pending mail to the system prompt before each
  model request (repeating it for the rest of the turn, since that
  addition is not kept in the transcript). opencode **cannot be kept from
  finishing a turn**: when mail arrives mid-turn, the plugin asks it to
  start a follow-up turn once the session goes idle. That follow-up is
  best-effort — if it does not happen, the message is simply waiting at
  the next prompt. The injection uses an `experimental.*` plugin hook that
  may change between opencode releases, and a `.opencode/opencode.json`
  containing comments cannot be merged and is skipped. When a session goes
  idle with nothing to act on, the plugin starts `hook-watch` for it and
  starts a turn when mail arrives; mail the watcher took just as the
  session turned busy is handed over when it is next idle.

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
Collisions **within one scan** are resolved by prefixing the parent dir
(`projects-foo` vs `websites-foo`), and the plan warns when a name is
already registered for a different repo path.

Two repos with the same basename wired in **separate** `init` runs are the
gap: neither run can see the other, and the warning only appears once one
of them has actually connected. They would then share a name, and so an
inbox. `whoami` reports it, and `agent-bus agents` shows one name against
the wrong path. Wire such repos in a single `--scan`, or pin one with a
`.agent-bus-name` file.

**Per-repo overrides.**

- Drop a `.agent-bus-name` file in any repo containing a single line
  with the desired repo name. `agent-bus init` will use that instead of
  the slug. Useful for keeping a name other agents already know.
- Drop a `.agent-bus-ignore` file (empty) in any repo to opt it out
  entirely. `init` skips the repo, and a client whose MCP server or hooks
  are configured once per user — so they follow you into every repo —
  refuses to join the bus there: the server exits with a message, and the
  hooks stay silent.

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

**Waking an idle session** (Claude Code 2.1.290 or later) takes one more
hook, on both `SessionStart` and `Stop`:

```jsonc
{
  "matcher": "",
  "hooks": [
    {
      "type": "command",
      "command": "AGENT_BUS_NAME=repo-a/claude agent-bus hook-watch --client claude --max-seconds 86400 ; [ $? -ne 10 ] || exit 2",
      "asyncRewake": true,
      "timeout": 86430,
      "rewakeSummary": "agent-bus: new mail"
    }
  ]
}
```

Claude Code runs it in the background and starts a turn when it exits 2.
`hook-watch` exits 10 when it has mail and the command turns that, and only
that, into 2: an `agent-bus` too old to know `hook-watch` exits 2 from its
argument parser, which must not wake the model. Do not end this command in
`|| true` — that would swallow the wake too. Keep `timeout` above
`--max-seconds`, so the watcher ends on its own before Claude Code kills it.

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
      "mcp__agent-bus__tail_audit",
      "mcp__agent-bus__set_session"
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
| `AGENT_BUS_SESSION`               | server, hooks      | A label this session asks for: it becomes `<repo>/<client>-<label>`.                    |
| `AGENT_BUS_INSTANCE`              | server, hooks      | `1`..`99`: the session number this session asks for.                                     |
| `AGENT_BUS_SPAWN_TOKEN`           | server             | Set by `agent-bus spawn` in the command it launches; proves the session is the one that spawn started, however late it joins. Not for hand use. |
| `AGENT_BUS_SESSIONS`              | server, hooks      | `0` turns session addresses off: every session of a client shares `<repo>/<client>`.     |
| `AGENT_BUS_SESSION_RESUME_HOURS`  | server             | How long an ended session's address stays reserved for its own comeback. Default `1`, `0` disables. |
| `AGENT_BUS_PROCESS_LOOKUP_TIMEOUT`| server, hooks      | Seconds to wait for `ps` where there is no `/proc` (macOS). Default `2`.                 |
| `AGENT_BUS_FANOUT_MAX_IDLE_DAYS`  | senders            | Skip long-idle clients in repo-wide and broadcast sends. Default `14`, `0` disables.     |
| `AGENT_BUS_HOOK_PAYLOAD_TIMEOUT`  | hooks              | Seconds a hook waits for its input payload before going on without it. Default `5`.     |
| `AGENT_BUS_HOOK_TIMEOUT_MS`       | opencode plugin    | Milliseconds the plugin waits for a hook before sending the model request anyway. Default `10000`. |
| `AGENT_BUS_DB`                    | everything         | Database path. Default `~/.claude-agent-bus/bus.db`.                                     |
| `AGENT_BUS_AUDIT_LOG`             | everything         | Audit log path. Default `~/.claude-agent-bus/audit.log`.                                 |
| `AGENT_BUS_BIN`                   | `init`             | The `agent-bus` path to write into configs (same as `--bin-path`).                       |
| `AGENT_BUS_NATIVE_WAKE`           | `init`, wake paths | `0` turns native wake off: no wake hooks from `init`, and existing ones stand down. Default on. |
| `AGENT_BUS_WATCH_POLL_SECONDS`    | `hook-watch`       | How often a watcher looks for mail. Default `2`.                                         |
| `AGENT_BUS_WATCH_HOURS`           | `init`             | How long each watcher waits before giving up. Default `24`.                              |
| `AGENT_BUS_WAKES_PER_HOUR`        | wake paths         | Native wakes allowed per agent per hour. Default `20`, `0` removes the limit.            |
| `AGENT_BUS_CLAUDE_BIN`            | `init`             | The Claude Code executable asked for its version. Default `claude`.                      |
| `AGENT_BUS_CLIENT_VERSION_TIMEOUT`| `init`             | Seconds to wait for that answer. Default `10`.                                           |
| `AGENT_BUS_CODEX_BIN`             | senders            | The Codex executable that queues a turn. Default: the one the session runs.              |
| `AGENT_BUS_AGY_BIN`               | senders            | The agy executable that starts a turn. Default: the one the session runs.                |

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
- config: shipped defaults, deep merge of a user file, unreadable or malformed files
- repo identity: URL normalisation (ssh, https, ports, host aliases), resolving a repo from the roster and from scanned roots
- spawn: command rendering per client, the tmux calls (with a stand-in `tmux`), the cap, cloning, worktrees and their carried-over files, dry runs, registration on the bus
- dispatch: the Asana source, rules, playbooks, state file, notifiers and timer files, driven with fake HTTP, SMTP, tmux and timers (no network)
- state reporting: `set_session(state=...)`, the hooks' `working` / `waiting`, and renames carrying the state
- status: states, liveness, `--all`, the `--json` keys
- native wake: `hook-watch` waking, giving up, handing over to a newer watcher, following a session's address, standing down for `wake.json`, the rate limit, a stop signal; the Claude Code hook command's exit codes run for real; Codex routes kept by the hooks and `codex queue` run by senders (with a stand-in `codex`); agy's registration request, private credential store and `agy agentapi send-message` run by senders (with a stand-in `agy`); the opencode plugin's watcher under Node

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

## Spawning sessions (`agent-bus spawn`)

```bash
agent-bus spawn --repo <name> --client <claude|codex|agy|opencode> [--model M] \
    --name <session> --prompt-file F [--worktree <base-ref>] [--clone] \
    [--no-remote] [--dry-run] [--json]
```

`spawn` opens one tmux window per session, all in one tmux session
(`agents` by default), named after the session. The client starts
interactively in the repo's folder with the contents of the prompt file as
its first message, and the window stays open with a login shell after the
client exits, so you can read what happened. Attach with
`tmux attach -t agents`.

```bash
echo "Fix the login redirect loop and open a pull request." > /tmp/prompt.txt
agent-bus spawn --repo repo-a --client claude --name fix-login --prompt-file /tmp/prompt.txt --dry-run
agent-bus spawn --repo repo-a --client claude --name fix-login --prompt-file /tmp/prompt.txt
agent-bus status
```

- **`--repo`** takes a repo's bus name, its id (`github.com/example-org/repo-a`),
  a remote URL, or a unique ending of the id (`example-org/repo-a`,
  `repo-a`). It is resolved to a clone on this machine from the roster,
  then by scanning `repos.roots`. A git worktree is never chosen over its
  main clone, and two different clones that match give exit 2 with both
  paths listed. A repo that is not on this machine exits 1 (see Missing
  clone).
- **Address.** The session becomes `<repo>/<client>-<label>`, for example
  `repo-a/claude-fix-login`; the label is `--name`, at most 24 characters
  (lowercase letters, digits and single dashes, starting with a letter; a
  `--name` that starts with `-` is refused with exit 2).
  Spawn registers it on the roster in state `starting` and starts the
  client with `AGENT_BUS_SESSION=<label>`, so the client's MCP server
  claims exactly that address. The repo must be wired for that client
  (`agent-bus init`). The prompt file is passed to the client as one
  argument, so a file whose first character is `-` (a list item, `---` front
  matter) is refused with exit 2: start it with text, a space or a blank
  line.
- **tmux** is needed for this command only; it is not a dependency of the
  package, and `status` never uses it. Without tmux, spawn exits 1, writes
  nothing to the bus and prints the `cd <dir> && <command>` line to run by
  hand.
- **Remote Control** is on by default for the clients that have it
  (`claude`, `agy`). `--no-remote` turns it off.
- **Cap.** At most `spawn.max_sessions` (default 5) spawned sessions can
  be active (`starting`, `running`, `working` or `waiting`). Another spawn
  exits 4 and names the running ones. A session that reported `done`, or
  has ended, does not count. Spawning a name whose address is still active
  exits 1.
- **Missing clone.** A repo that is not on this machine is cloned only
  with `--clone`, into `repos.clone_root`. Without it, spawn exits 1 and
  prints the `git clone -- <url> <dir>` command. The clone runs as
  `git clone -- <url> <temporary sibling>` and the folder is renamed into
  place only when git succeeds, so a failed or timed-out clone leaves
  nothing behind. A folder that already exists at the destination is reused
  only when it is a complete clone of that repo (`git rev-parse --verify
  HEAD` succeeds); a partial one is refused, naming the directory. The
  folder name is the last part of the repo id and must start with a letter,
  digit or underscore (`.git.git` or `.cargo` are refused; repeated `.git`
  suffixes are dropped). When that folder already holds a different repo,
  the clone goes to `<owner>-<name>` beside it (spawn says so); if that is
  taken too, spawn refuses and names both paths. A repo cloned this way is
  wired for the spawned client, so the new session can join the bus, except
  when a wiring file's folder is a symlink leading outside the clone: spawn
  then writes nothing and says how to wire it by hand. With neither
  `repos.clone_root` nor `repos.roots` set, `--clone` exits 2 naming the
  key.
- **Opted-out repos.** A repo with a `.agent-bus-ignore` file is refused
  (exit 1): a session there can never join the bus.
- **Early checks.** The command template and the `worktree_links` entries
  are rendered with sample values first, so a typo in `config.json` fails
  before anything is cloned, created or linked. A client key that is not a
  valid client id exits 2. Expected failures print `agent-bus: <message>`
  and the notes gathered so far, never a traceback.
- **Registration.** The roster rows, the `starting` state and the spawn
  record are written just before the tmux window opens. If tmux refuses,
  exactly what the spawn wrote is removed and any earlier record or state
  is restored.
- **`--worktree <base-ref>`** runs the session in a new detached git
  worktree (`git worktree add --detach -- <dir> <base-ref>`). By default
  (`spawn.worktree_root` is `null`) it is `<clone>/.worktrees/<label>`, and
  `/.worktrees/` is added to the repository's shared `info/exclude` (never
  to a tracked file), once, so git does not list it. With
  `spawn.worktree_root` set, it is `<root>/<group>/<label>` as before and
  nothing is excluded. No fetch is done, so the ref is used as it is
  locally. An existing worktree directory is reused only when it is
  a git worktree of that clone; anything else at that path is refused.
  Why inside the clone: Claude Code, Codex and Antigravity CLI ask whether
  to trust any new folder, which an unattended session cannot answer.
  Claude Code and Codex treat a folder inside a trusted clone as trusted,
  so the worktree starts without a prompt; Antigravity CLI asks again
  (answer it in the tmux window); opencode never asks. The trade-off: tools
  that scan or mount the main folder (IDEs, containers, file watchers) also
  see `.worktrees/`. Repo discovery (`init --scan`, `repos.roots`) skips
  `.worktrees` folders, so they are never taken for repos.
  Worktrees lose untracked files, so the client's wiring files and every
  existing entry of `spawn.carry_over` are symlinked in from the clone
  (an entry must be a relative path without `..`; the entry itself may be a
  symlink). A path that already exists in the worktree is left alone and
  nothing is deleted. For Claude Code, spawn also writes a worktree-local
  `.claude/settings.local.json` (a regular file, never a link into the
  clone). It starts from the clone's own file when that is a JSON object,
  lists `agent-bus` in `enabledMcpjsonServers` so the server starts without
  an approval prompt, and, when the worktree's `.claude/settings.json` (for
  example a committed copy) lacks the agent-bus hooks, adds those hooks and
  the agent-bus tool permissions, with the clone's wiring name and binary.
  Spawn says "hooks added through .claude/settings.local.json" when it did
  that, and adds the file to the shared `info/exclude` only if
  `git check-ignore` says git would list it. A wiring file the repo tracks
  that this does not make up for (for example `.mcp.json`) keeps its
  committed copy; spawn prints a note naming it.
  Client state kept outside the repo is linked by the client's
  `worktree_links` list (`source` and `target` templates with
  `{clone_key}`, `{work_dir_key}`, `{clone}`, `{work_dir}`). The Claude
  default links `~/.claude/projects/<clone key>/memory` to the worktree's
  key, so project memory carries over; a key is the absolute path with every
  non-alphanumeric character replaced by `-`. Nothing is linked if the source
  is missing or the target exists. Set
  `spawn.clients.claude.worktree_links` to `[]` to switch it off.
- **`--dry-run`** prints the address, folder and command, and changes
  nothing (no tmux, no clone, no worktree, no link, no database write,
  not even the repo id cache). `--json`
  prints the same as an object.

Exit codes: `0` started (or dry run), `1` could not start (no tmux, no
local clone of the repo, name already active, a malformed `config.json` or
a template typo), `2` bad usage (ambiguous repo, unknown client, a model
for a client that has no model option, a `--name` or prompt file that
starts with `-`, `--clone` with no clone folder configured), `4` the cap is
reached.

### Configuration (`config.json`)

Settings live in `config.json` beside the database
(`~/.claude-agent-bus/config.json`). It is optional: the shipped defaults
below apply, and your file is merged over them key by key. Objects merge,
lists and scalars replace, and `null` removes a fragment. A malformed file
stops `spawn` with an error naming it; `status` warns and carries on.
`agent-bus config show [--json]` prints the file path and the effective merged config.

```json
{
  "repos": {
    "remote": "origin",
    "host_aliases": {"ssh.github.com": "github.com", "altssh.gitlab.com": "gitlab.com"},
    "roots": [],
    "scan_depth": 6,
    "clone_root": null,
    "clone_url": "git@{host}:{path}.git",
    "git_timeout_seconds": 30,
    "id_recheck_seconds": 3600,
    "status_budget_seconds": 5,
    "clone_timeout_seconds": 600
  },
  "status": {
    "hide_done_after_seconds": 86400
  },
  "spawn": {
    "max_sessions": 5,
    "startup_grace_seconds": 120,
    "tmux_bin": "tmux",
    "tmux_session": "agents",
    "tmux_timeout_seconds": 10,
    "after_exit": "exec $SHELL -l",
    "strip_env": ["CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SESSION_ID", "CLAUDE_PID", "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_CHILD_NOTES_DIR", "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_EFFORT", "CLAUDE_PLUGIN_DATA"],
    "worktree_root": null,
    "carry_over": [],
    "clients": {
      "claude": {"command": ["claude", "-n", "{name}", "--permission-mode", "auto"], "model": ["--model", "{model}"], "remote": ["--remote-control", "{name}"], "prompt": ["{prompt}"],
        "resume": ["claude", "--resume", "{label}"],
        "worktree_links": [{"source": "~/.claude/projects/{clone_key}/memory", "target": "~/.claude/projects/{work_dir_key}/memory"}]},
      "codex": {"command": ["codex"], "model": ["-m", "{model}"], "remote": null, "prompt": ["{prompt}"], "resume": ["codex", "resume"], "worktree_links": []},
      "agy": {"command": ["agy", "--mode", "auto"], "model": ["--model", "{model}"], "remote": ["--remote-control"], "prompt": ["-i", "{prompt}"], "resume": null, "worktree_links": []},
      "opencode": {"command": ["opencode", "--standalone", "--auto"], "model": null, "remote": null, "prompt": ["--prompt", "{prompt}"], "resume": null, "worktree_links": []}
    }
  },
  "dispatch": {
    "env_file": null,
    "max_spawns_per_run": 2,
    "working_hours": {"days": ["mon", "tue", "wed", "thu", "fri"], "start": "09:00", "end": "19:00"},
    "label": {"template": "t{gid_suffix}-{slug}", "gid_suffix_length": 8},
    "handoff_channel": "{label}",
    "templates_dir": null,
    "prompts_dir": null,
    "title_max_chars": 120,
    "overlap_max_items": 10,
    "fetch_timeout_seconds": 120,
    "notify_on": ["waiting", "done", "ended", "spawn_failed", "notify"],
    "defaults": {"client": "claude", "model": null, "worktree": null, "fetch": false, "clone": false, "remote": true},
    "types": {
      "refinement": {"action": "spawn", "playbook": "refinement"},
      "follow-up": {"action": "spawn", "playbook": "follow-up"},
      "implementation": {"action": "notify", "playbook": "implementation"}
    },
    "rules": [
      {"type": "refinement", "section": ["(?i)refine"]},
      {"type": "follow-up", "handled_as": ["refinement", "follow-up"], "new_comment": true},
      {"type": "implementation", "section": ["(?i)^(to ?do|ready|in progress)"]}
    ],
    "sources": {
      "asana": {"enabled": false, "base_url": "https://app.asana.com/api/1.0", "token_env": "ASANA_TOKEN", "workspace": null, "lookahead_days": 0, "page_size": 100, "max_pages": 10, "timeout_seconds": 30, "max_retry_wait_seconds": 30, "ignore_own_comments": true, "comment_authors": [], "projects": {}}
    },
    "notifiers": {
      "log": {"file": null},
      "macos": {"enabled": true, "osascript_bin": "osascript", "title": "agent-bus", "timeout_seconds": 10},
      "email": {"enabled": false, "host": null, "port": 465, "security": "tls", "username": null, "password_env": null, "from": null, "to": [], "subject_prefix": "[agent-bus]", "timeout_seconds": 30}
    },
    "timer": {"name": "agent-bus-dispatch", "interval_minutes": 15, "command": ["{agent_bus_bin}", "dispatch", "run"], "environment": {}, "output_file": null, "launchctl_bin": "launchctl", "systemctl_bin": "systemctl", "timeout_seconds": 30}
  }
}
```

- `repos.roots`: folders scanned (to `scan_depth`) for clones the roster
  does not know, for example `["~/code"]`. `repos.clone_root` is where
  `--clone` clones to; `clone_url` builds the URL from `{host}` and
  `{path}` of the repo id, and any other placeholder or stray brace is
  rejected when the config is loaded. `id_recheck_seconds` is how long a
  cached repo id is trusted before `status` asks git again; an id is never
  cached from a failed or timed-out lookup, and a repo whose remote was
  added or changed later is found again. A repo with no `origin` is
  remembered as such for `repos.id_recheck_seconds`, so `status` and `spawn`
  do not ask git about it each time; `status` spends at most
  `repos.status_budget_seconds` (default 5) on git lookups per call.
- `--repo` matches any group name the roster holds for a clone. Repo URLs
  with a query, a fragment, or an ambiguous authority (`#`, `?`, `\`,
  whitespace, a userinfo or host starting with `-`) are not accepted as
  remotes; the clone URL passed to git is rebuilt from the parsed parts.
- Spawn puts a per-spawn token, `AGENT_BUS_SPAWN_TOKEN`, in the launched
  client's environment; the session's MCP server presents it when it joins.
  The session that carries the token keeps its `starting` state and spawn
  record however long it takes to join (a client may wait on a folder-trust
  or MCP-approval prompt). A session that claims a spawned address without
  the matching token is not the session spawn started: it never inherits the
  state, and the spawn record is removed, so it shows as a normal session.
  Until it joins, a spawned session shows `starting`, then `ended` once
  `spawn.startup_grace_seconds` has passed; it is live again, with its tmux
  location and counted against `spawn.max_sessions`, as soon as it joins. A
  dead spawned session that already reported `working` or `waiting` shows as
  `ended` even inside the startup
  grace (only a session that never reported shows `starting`).
- `spawn.strip_env` lists variables removed from the launched client and
  from the tmux session, so a spawned client does not think it is a child
  of the session that started it (the tmux server keeps the environment of
  whoever started it).
- `spawn.worktree_root` is `null` by default: worktrees go to
  `<clone>/.worktrees/<label>` (see `--worktree`). A path puts them under
  `<root>/<group>/<label>` instead.
- The opencode template starts `opencode --standalone --auto`. By default
  opencode runs its sessions through a shared background service, whose MCP
  servers keep the environment of the launch that started the service: a
  later spawn would join the bus with an earlier spawn's token and be taken
  for a stranger. `--standalone` runs a private server with the environment
  of the launching process.
- Codex is usually wired once per user (`[mcp_servers.agent-bus]` in
  `~/.codex/config.toml`, or under `$CODEX_HOME`). When spawn finds that, it
  does not claim the repo is unwired; it notes that Codex does not pass
  `AGENT_BUS_SESSION` to its MCP servers, so the session is named after the
  folder rather than the label and `agent-bus status` will not link it to the
  spawn. A per-repo Codex wiring (`agent-bus init --clients codex`) passes
  the variable through.
- `spawn.carry_over` lists untracked files or folders of the repo to
  symlink into a worktree, for example `[".tool-config", "local-notes"]`.
- Each client has four fragments, lists of arguments, plus `worktree_links`:
  `command` always
  runs, `model` is added with `--model`, `remote` unless `--no-remote`, and
  `prompt` last. A fragment set to `null` is not available: `--model` for a
  client with `"model": null` exits 2. Placeholders: `{name}` (the
  `--name`), `{label}`, `{address}`, `{model}`, `{repo}`, `{dir}` (the work
  folder), `{prompt_file}`, and `{prompt}`. Every argument is shell-quoted
  except `{prompt}`, which becomes `"$(cat <prompt file>)"`, so the prompt
  never appears in the tmux command line. `worktree_links` is described
  under `--worktree` above (`[]` turns it off).
- A client's `resume` list is how `dispatch status` tells you to reopen a
  spawned session; it is printed after `cd` into the session's folder,
  because clients list and look up sessions by working directory.
  Placeholders: `{label}` (the spawn label) and `{dir}`. The defaults are
  `claude --resume {label}` (spawn names a Claude session after its label)
  and `codex resume` (the picker, filtered to that folder). agy and
  opencode have `null`, so no command is shown for them.

For example, to lower the cap and give Codex its working folder:

```json
{
  "spawn": {
    "max_sessions": 3,
    "clients": {"codex": {"command": ["codex", "-C", "{dir}"]}}
  }
}
```

These commands run as your user, so the file is as sensitive as a shell rc
file. The default templates start clients in their auto permission mode.

## Session status (`agent-bus status`)

`agent-bus status` shows, per repo, every live session and every spawned
session: its client, state, when it was last seen, the tmux
`session:window` it runs in, its folder when that differs from the repo's,
and its topic. Ended sessions are hidden, and so are finished ones (no
running server, state `done`) once their last report is older than
`status.hide_done_after_seconds` (default one day; see *Configuration*).
`--all` shows both; nothing is deleted from the database. With nothing to
show it prints `(no sessions running)`.

Sessions report their state over the bus; nothing is read from terminal
panes.

| State | Where it comes from |
| --- | --- |
| `starting` | `spawn` registered the session; the client has not connected yet |
| `running` | the session is live but has not reported a state |
| `working` | the prompt hook, at the start of a turn, `set_session(state="working")`, or a wake: when the background watcher or a push hands mail over and wakes a session it is `working` (also when it was `done`; a follow-up is new work) |
| `waiting` | the stop hook when a turn really ends (the session waits for you), or `set_session(state="waiting")` |
| `done` | the agent called `set_session(state="done")`; it stays `done` until the next prompt |
| `ended` | the session registry: no running server holds the session, whatever state it last reported |

A stop hook that holds the turn open to deliver mail changes nothing, and a
hook that cannot tell which session it serves writes no state. Renaming a
session with `set_session(label=...)` carries its state and spawn record to
the new name, replacing whatever a previous holder of that label left
behind. A session that takes over a kept label starts without a state, and
a reconnect or restart of the same session's MCP server (same client
process or session id) keeps the state it reported, `done` included; the
`starting` state `spawn` wrote lasts until the session's first report. Tell spawned agents in the prompt to call
`set_session(state="done")` when their task is finished.

`agent-bus status --json` emits one object per session with the keys
`name`, `group`, `client`, `label`, `state`, `reported_state`, `state_at`,
`live`, `spawned`, `last_seen`, `topic`, `pending_count`, `repo_path`,
`repo_id`, `work_dir`, `tmux_session`, `tmux_window`, `model` and
`remote`.

---

## Dispatching tasks (`agent-bus dispatch`)

`agent-bus dispatch run` reads your tasks from a task source (first source:
Asana), decides per task what to do with fixed rules from `config.json`, and
either starts a session for it with `agent-bus spawn` or only notifies you.
There is no model in the decision loop. It is meant to run from a timer, and
it is off until you enable a source.

One run does, in order:

1. Takes a run lock. When another run holds it, the run exits 0 and does nothing.
2. Checks `dispatch.working_hours`. Outside them it stops (`--ignore-hours` skips the check).
3. Sends notifications for sessions it spawned earlier whose state changed (needs review, done, ended without done).
4. Fetches the incomplete tasks assigned to the token's user that are due on or before today plus `lookahead_days`.
5. Maps each task to a repo through `projects`: the repo comes from the first of the task's memberships whose project id is a key of `projects`. A task can sit in several projects at once, so map only the projects that belong to one repo and leave shared process projects (a board every repo uses, for example) unmapped; a rule's `project` scope is separate from this mapping and can name such a shared project. A task in no configured project is ignored and not stored.
6. Classifies the task: rules are tried in order and the first match wins.
7. Spawns a session with a prompt rendered from a playbook, or notifies only, according to the action of the task's type.

```bash
agent-bus dispatch run [--dry-run] [--json] [--ignore-hours]
agent-bus dispatch status [--json]
agent-bus dispatch install-timer [--load] [--bin-path PATH]
agent-bus dispatch uninstall-timer [--unload] [--bin-path PATH]
```

`run --dry-run` prints one decision per mapped task (type, action, result,
reason, label) and changes nothing. It applies `max_spawns_per_run` and the
session cap as a real run would, counting each would-be spawn, so the
report matches what a run does: it makes read-only HTTP requests, but
spawns nothing, sends no notification and creates no state file, prompt file
or lock. Start there. `--json` prints the decisions as JSON.

### Setup

Export the token under the variable name you configure, or put it in an
env file (see below), then add a `dispatch` block to
`~/.claude-agent-bus/config.json`:

```json
{
  "dispatch": {
    "env_file": "~/.claude-agent-bus/dispatch.env",
    "templates_dir": "~/.claude-agent-bus/playbooks",
    "sources": {
      "asana": {
        "enabled": true,
        "token_env": "ASANA_TOKEN",
        "projects": {
          "1000000000000001": {"repo": "example-org/repo-a", "worktree": "origin/main", "fetch": true}
        }
      }
    }
  }
}
```

The token is read from the environment variable named by `token_env`. Only
the name lives in the config; the value is never stored in the config, the
log or `dispatch.db`, and error messages name the variable only.

Launchd and systemd do not inherit your shell environment, so for timer runs
put the token (and the mail password) in `dispatch.env`, as `KEY=VALUE`
lines. The file must be owner-only (`chmod 600`); agent-bus refuses a file
that others can read. A variable already set in the process environment wins
over the file.

### Configuration reference

All keys sit under `dispatch` and deep-merge over the shipped defaults.
Lists replace on merge, so a custom `rules` replaces the shipped list.
An invalid value makes the config fail to load, naming the dotted key.

| Key | Meaning (default) |
| --- | --- |
| `env_file` | Optional `KEY=VALUE` file read before each run, mode 600 (`null`) |
| `max_spawns_per_run` | Spawn limit per run (`2`). The bus-wide `spawn.max_sessions` applies too |
| `working_hours.days` / `start` / `end` | Days and local time window in which runs act (`mon`-`fri`, `09:00`-`19:00`) |
| `label.template` | Session label template (`t{gid_suffix}-{slug}`) |
| `label.gid_suffix_length` | Characters of the task id used for `{gid_suffix}` (`8`) |
| `handoff_channel` | Handoff channel name given to the session (`{label}`) |
| `templates_dir` | Directory of playbook files that override the built-ins (`null`) |
| `prompts_dir` | Where rendered prompt files are written (`dispatch-prompts` beside the database) |
| `title_max_chars` | Cap on task text copied into a prompt (`120`) |
| `overlap_max_items` | Cap on the list of other in-flight or handled tasks of the same project (`10`) |
| `fetch_timeout_seconds` | Timeout for `git fetch` before a spawn (`120`) |
| `notify_on` | Which events notify: `waiting`, `done`, `ended`, `spawn_failed`, `notify` (all five) |
| `defaults` | Per-project defaults: `client` (`claude`), `model`, `worktree`, `fetch`, `clone`, `remote` |
| `types.<type>.action` | `spawn`, `notify` or `ignore` per type |
| `types.<type>.playbook` | Playbook name for the type (same as the type) |
| `rules` | Ordered classification rules, see below |
| `sources.asana.enabled` | Turn the source on (`false`) |
| `sources.asana.base_url` | API base URL (`https://app.asana.com/api/1.0`) |
| `sources.asana.token_env` | Name of the environment variable holding the token (`ASANA_TOKEN`) |
| `sources.asana.workspace` | Optional workspace id to restrict the query (`null`) |
| `sources.asana.lookahead_days` | Include tasks due up to this many days ahead (`0`: due today or overdue) |
| `sources.asana.page_size` / `max_pages` | Paging of the API reads (`100`, `10`) |
| `sources.asana.timeout_seconds` | HTTP timeout (`30`) |
| `sources.asana.max_retry_wait_seconds` | Longest wait before the single retry after HTTP 429, whatever `Retry-After` asks (`30`). Redirects are never followed, so the token is never sent to another host. |
| `sources.asana.ignore_own_comments` | Comments by the token's user do not count as new comments (`true`) |
| `sources.asana.comment_authors` | Allowlist of user gids (digit strings). When non-empty, only comments by these users count as new comments; empty (`[]`) means any author. Recommended for unattended setups: otherwise anyone who can comment on a task can start a follow-up session |
| `sources.asana.projects` | Project id to repo: a string, or an object with `repo` and optional `client`, `model`, `worktree`, `fetch`, `clone`, `remote`, `playbooks` |
| `notifiers.log.file` | Log file (`dispatch.log` beside the database) |
| `notifiers.macos.*` | `enabled`, `osascript_bin`, `title`, `timeout_seconds` |
| `notifiers.email.*` | `enabled` (`false`), `host`, `port` (`465`), `security`, `username`, `password_env`, `from`, `to`, `subject_prefix`, `timeout_seconds` |
| `timer.name` | Name of the launchd job or systemd unit (`agent-bus-dispatch`) |
| `timer.interval_minutes` | Minutes between runs (`15`) |
| `timer.command` | Command the timer runs; `{agent_bus_bin}` is the resolved binary |
| `timer.environment` | Extra environment variables written into the unit (`{}`) |
| `timer.output_file` | Timer stdout/stderr file (`dispatch-timer.log` beside the database) |
| `timer.launchctl_bin` / `systemctl_bin` / `timeout_seconds` | Binaries and timeout used by `--load` and `--unload` |

### Rules, types and actions

A rule has a `type` and conditions; all conditions of a rule must hold.

- `section`: list of regular expressions matched against the task's section names.
- `title`: list of regular expressions matched against the title.
- `project`: list of project ids; the rule then matches only through the task's memberships in those projects, and its `section` patterns are tested only against those memberships' sections.
- `handled_as`: the task was already handled as one of these types.
- `new_comment`: `true` when the newest comment is newer than the last handled one.

The first matching rule wins; no match means `ignore`. A rule without
`new_comment: true` never matches a type the task was already handled as, so
a task is not handled twice as the same type. The shipped rules send
sections matching `refine` to `refinement`, handled tasks with a new comment
to `follow-up`, and `to do` / `ready` / `in progress` sections to
`implementation`.

Each type has an action: `spawn` starts a session, `notify` only notifies
(once per task), `ignore` does nothing. `refinement` and `follow-up` spawn;
`implementation` is notify-only by default, and
`dispatch.types.implementation.action` set to `spawn` makes it spawn.
A task whose session is live or in an active state is skipped.

The label is built from `label.template` with `{gid}`, `{gid_suffix}`,
`{slug}` (from the title) and `{type}`. It is at most 24 characters and
starts with a letter. The full task id goes into the prompt and the state
file.

### Playbooks

A playbook is a prompt template named after the type (`refinement`,
`follow-up`, `implementation`). Generic ones ship with the package. A file
`<name>.md` in `dispatch.templates_dir` replaces the built-in of that name,
so your real prompts can live outside any repo. A project may pick other
names with `playbooks`, e.g. `{"refinement": "my-refinement"}`.

Placeholders are written `$name`: `task_url`, `task_gid`, `task_title`,
`task_type`, `source`, `project_name`, `project_gid`, `section_names`,
`due_on`, `modified_at`, `newest_comment_at`, `repo`, `label`,
`handoff_channel`, `overlap`, `head_sha` and `base_ref`. A literal dollar is
`$$`. A prompt must not start with `-`. The rendered prompt is written to a
private file and passed to `spawn` as `--prompt-file`.

### Notifiers

- `log` always writes, to `dispatch.log` beside the database.
- `macos` runs `osascript` with the text as an argument on macOS and does nothing elsewhere.
- `email` needs `host`, `from`, `to` and `security`, and `username` and `password_env` together or not at all: `tls` (port 465, implicit TLS) or `starttls` (port 587). The certificate is verified and there is no plaintext fallback: when TLS cannot be established nothing is sent. The password is read from the variable named by `password_env`.

Messages carry only the session name, the state, the task title and the
link. Each transition (needs review, done, ended without done, spawn
failed) and each notify-only task is notified once. A notification is
recorded as sent only when at least one enabled notifier succeeded; when
none did, it stays pending and the next run retries it (the failure is
reported on stderr and the run exits 1, without secrets).

The state database, the log and the prompt files hold task titles and links,
so dispatch creates them owner-only (files `0600`, directories it creates
`0700`). The timer output file (`timer.output_file`) also holds task titles;
keep it in a private directory.

### Timer

`install-timer` writes a launchd plist (macOS, in `~/Library/LaunchAgents`)
or a systemd user service and timer (Linux, in `~/.config/systemd/user`)
that run `agent-bus dispatch run`, and prints each path it wrote. The
schedule comes from `working_hours` and `timer.interval_minutes` (default:
Monday to Friday, every 15 minutes, 09:00 to 19:00). Nothing is loaded or
enabled without `--load`. The `PATH` of the shell that runs `install-timer`
is written into the unit, so run it from a shell where `tmux` and your
clients resolve. `uninstall-timer` removes the files and unloads them only
with `--unload`.

### State and limits

State lives in `dispatch.db` beside the bus database (it follows
`AGENT_BUS_DB`), with its own migrations; the bus database is untouched.
`dispatch status` lists every known task with its type, outcome, session
address and that session's current state. A session that renamed itself is
shown under its new name. Below a spawned session it prints the command that
reopens it (`resume: cd <folder> && claude --resume <label>`, from the
client's `spawn.clients.<client>.resume`); `--json` gives the same as
`resume`, and `current_session` for the current name. Follow-ups reuse the
label, so Claude Code may show a picker filtered to that name.

- A task is only seen while it is inside the due window, so follow-up only works on tasks still due today or overdue (or within `lookahead_days`).
- A follow-up waits while the earlier session for the task is still open.
- Runs stop spawning at `dispatch.max_spawns_per_run` or when `spawn.max_sessions` is reached.

---

## Waking idle agents

An agent CLI waiting at its prompt does not notice mail landing in
SQLite: nothing starts a turn. agent-bus wakes it in one of two ways —
through the client's own mechanism, which `agent-bus init` sets up by
default, or through a shell command you configure in `wake.json`.

### Native wake (the default)

| Client          | How an idle session is woken                                                                                                                                                                             | Delay                         |
| --------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------- |
| Claude Code     | A background hook (`asyncRewake`) runs `agent-bus hook-watch` when a session starts and after every turn. It polls the bus; when mail arrives it takes it from the inbox and exits, and Claude Code starts a turn with the mail. Interactive sessions only — see below. | `AGENT_BUS_WATCH_POLL_SECONDS` (2 s) at most |
| Codex           | The sender runs `codex queue --thread <id>` for the recipient's session. Codex starts a turn with a short notice, and its prompt hook adds the mail as on any turn.                                     | up to ~10 s: Codex checks its queue every 10 s |
| opencode        | The generated plugin runs `hook-watch` for a session when it goes idle, and starts a turn with what it finds.                                                                                            | 2 s at most                   |
| Antigravity CLI | The sender runs `agy agentapi send-message <conversation id>` for the recipient's session, which starts a turn with a short notice; the PreInvocation hook adds the mail. The session's agent must have run `agent-bus wake-register` once — see below. | under a second                |

- **Mail that arrives mid-turn** reaches Claude Code inside the running
  turn, and agy at its next model call. Codex and opencode see it when
  the turn ends, through the Stop hook and the plugin's idle check, as
  before — a busy Codex session is never queued to, so it does not get an
  extra, empty turn. agy is pushed to while busy as well, at most once per
  model call: its message joins the running turn, and agy ends a turn you
  interrupt without running its Stop hook, so a session that looks busy
  may be idle.
- **Claude Code: interactive sessions only.** The watcher runs only where
  `CLAUDE_CODE_ENTRYPOINT` is `cli` — a session started with `claude` in a
  terminal. A non-interactive run (`claude -p`, the SDK) would wait for the
  hook in the foreground, so there it exits at once; sessions inside an IDE
  or the desktop app are not woken either.
- **One watcher per session.** Each turn starts a new one and the older
  one stops. A watcher stops when its client exits, and gives up after
  `AGENT_BUS_WATCH_HOURS` (24): a session idle for longer is not woken
  until its next turn. That limit is written into the client's config, so
  a change takes effect on the next `agent-bus init`.
- **Codex** is queued to only while its process is running and the session
  is idle, and only once until that queued turn has started. The sender
  runs the Codex executable the session's hooks recorded, which works
  where the `codex` on `PATH` is a wrapper script; `AGENT_BUS_CODEX_BIN`
  names another one.
- **Antigravity CLI** guards the API that starts a turn with a token, and
  gives it only to the commands its agent runs — not to hooks or MCP
  servers. So the first model call of each session (and of a conversation
  resumed in a new `agy` process) asks the agent to run `agent-bus
  wake-register --client agy` once; agy asks you to allow that command,
  and choosing to always allow it stops the question for good. The token
  lets whoever holds it drive that session, so it is kept apart from the
  bus database, in `push-credentials/` beside it (a directory only you
  can open, one file per session readable only by you), passed to `agy`
  through its environment rather than its arguments, and deleted once
  the session's `agy` process has ended. Until a session has registered,
  mail waits for its next turn and the audit says `unregistered`.
- **A repo-wide or broadcast message** still wakes at most one agent per
  repo. A Claude Code or opencode session watching in that repo takes it;
  otherwise the sender wakes the most recently seen agent it can.
- **Rate limit.** No agent is woken natively more than
  `AGENT_BUS_WAKES_PER_HOUR` (20) times in an hour, so two agents that
  answer each other cannot keep each other busy indefinitely. Past the
  limit, mail waits for the agent's next turn. `0` removes the limit.
- **Audit.** Every native wake writes an `op="wake"` row with
  `wake_driver` (`watch` or `push`) and `wake_status` (`woke`,
  `fired:OK`, `fired:ERR:…`, `rate-limited`, `unregistered`).
- **Turning it off.** `AGENT_BUS_NATIVE_WAKE=0` makes `init` leave the
  wake hooks out (and remove ones it wrote before), and stops wiring that
  is already in place: watchers exit at once and senders queue nothing.
- **`wake.json` wins.** An entry for an agent, its client or its repo
  replaces native wake for it: a command runs instead, and `false` turns
  waking off.

Caveats: the line Claude Code shows when mail wakes a session ("agent-bus:
new mail") comes from an undocumented hook option, `rewakeSummary`. Codex's
queue is new in Codex: a message queued to a session whose turn you
interrupted with Esc waits until you send the next prompt. agy's
`agentapi` is barely documented and may change between agy releases, and
the registration depends on the agent running the command it is asked to.

### Commands of your own (`wake.json`)

A per-agent shell command can run the instant a message arrives for that
agent instead — for a client without native wake, or to do something else
entirely: drive emacs/vterm, drive a tmux pane, fire a desktop
notification, hit a webhook, ring a bell.

#### How it works

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

#### Managing wake.json

Edit it directly, or use the CLI helpers:

```bash
agent-bus wake-config show
agent-bus wake-config set web-app \
  'emacsclient -e "(with-current-buffer (get-buffer \"*vterm: web-app*\") (vterm-send-string \"check inbox\") (vterm-send-return))"'
agent-bus wake-config test web-app   # fire a synthetic wake to verify
agent-bus wake-config clear web-app
```

#### Example wake commands

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

#### Failure modes

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

## Using more than one machine

The bus is local: the database, the audit log and the wiring all live on
one machine, and agents on different machines cannot see each other.

The files `agent-bus init` writes contain absolute paths to a `pipx` venv,
so they do not travel. If you sync dotfiles or repositories between
machines, run `agent-bus init` on each one rather than syncing its output,
and keep the generated files out of git (see the list in
[`docs/AGENT_SETUP.md`](docs/AGENT_SETUP.md#7-keep-the-wiring-out-of-git)).

One trap is worth calling out. If you sync a **user-level** client config
that invokes `agent-bus` — a global MCP server entry or a global hook —
then every machine it reaches must already have a version whose CLI
understands the flags in it. Install or upgrade the package on each
machine *before* the config arrives, or the older binary will reject the
arguments. Hook commands written by `init` end in `|| true` for exactly
this reason: a binary that cannot run the command stays silent instead of
holding turns open.

---

## Privacy & security

- All state is local, and nothing leaves your machine unless dispatch is
  enabled. Then agent-bus calls the configured task source over HTTPS
  (read-only GET requests) and, when the email notifier is on, the
  configured SMTP server over TLS.
- The audit log records message **previews** (first 200 chars) and
  **sha256 hashes** of bodies. Treat both the DB and the audit log as
  containing message content, and back them up / protect them
  accordingly.
- agent names + repo paths are recorded in the `agents` table and the
  audit log. Don't put secrets in agent names. Don't broadcast
  credentials over the bus.
- `config.json` holds commands that `agent-bus spawn` runs as you, so
  treat it like a shell rc file. The default templates start clients in
  their auto permission mode, with Remote Control on for the clients that
  have it (`--no-remote` turns it off), so a spawned session can act, and
  be reached from other devices, without asking first. Spawn only on repos
  and prompts you would run unattended.
- Task titles and comments are untrusted data. Dispatch writes them only
  into the prompt file, labelled as such, never into a command line.
  Dispatched sessions run in the client's auto permission mode, so map only
  repos and playbooks you would run unattended. The built-in playbooks tell
  the session to do nothing outward-facing without your approval. Every
  tracker value in a prompt is flattened to one line, truncated to
  `title_max_chars` and stripped of runs of three or more dashes, so it
  cannot forge the untrusted-data markers. Anyone who can comment on a
  handled task can start a follow-up session; set
  `sources.asana.comment_authors` to your own gid (and trusted colleagues)
  for unattended setups.
- The one secret agent-bus keeps itself — the token an agy session
  registers so that it can be woken — is never written to the database or
  the audit log; see [Native wake](#native-wake-the-default).

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
- Claude Code's MCP channels (`notifications/claude/channel`) as a way to
  wake a session: they need a command-line flag and organisation
  settings, while a background hook works in any session.

---

## License

MIT.

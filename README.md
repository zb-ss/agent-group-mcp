# agent-bus

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python: 3.11+](https://img.shields.io/badge/Python-3.11+-blue.svg)](pyproject.toml)

Local MCP server + companion CLI that lets multiple Claude Code instances
(each running in a different repo on the same machine) **exchange messages
through a shared, persistent bus** — with full audit logging and Claude
Code hooks for instant reaction at every turn boundary.

- **Zero external services.** Local-only. Filesystem + SQLite (WAL mode).
- **One MCP server process per Claude session**, all sharing one DB.
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

Each MCP server reads `AGENT_BUS_NAME` and `AGENT_BUS_REPO` from env at
startup, upserts the `agents` row, and silently attaches its name to
every tool call. **`send_message` / `read_inbox` no longer take a `from`
or `name` argument** — the server already knows its identity.

The server fails hard if either env var is missing.

---

## MCP tools

All tools are auto-attributed to `AGENT_BUS_NAME`.

| Tool                                         | Returns                                                                                                                                |
| -------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| `whoami()`                                   | `{name, repo_path, registered_at, last_seen}`. Diagnostic; replaces the old `register_agent`.                                          |
| `list_agents()`                              | `[{name, repo_path, last_seen, pending_count}, …]`                                                                                     |
| `send_message(to, body, thread_id=None)`     | unicast → `{message_id, sent_at, thread_id, recipients}`; broadcast (`to="*"`) → `{message_ids, sent_at, thread_id, recipients}`       |
| `read_inbox(mark_read=True, limit=50)`       | Unread messages for self, oldest first: `[{message_id, from, to, body, sent_at, thread_id, read_at, delivered_at}]`                    |
| `read_thread(thread_id, limit=100)`          | Full conversation across participants, ordered by `sent_at`.                                                                           |
| `tail_audit(limit=50)`                       | Last N audit-log entries (any agent).                                                                                                  |

> **Note on broadcast:** the schema keeps `message_id` as the row PK and
> tracks `read_at` per recipient, so a broadcast fans out into **N rows
> with N distinct `message_id`s** — one per peer. That's why the
> broadcast return shape uses `message_ids` (plural). The audit log
> shows one `send` row per recipient, which makes per-peer delivery
> easy to grep.

---

## SQLite schema

```sql
agents(name PK, repo_path, registered_at, last_seen)
messages(message_id PK, from_agent, to_agent, body, thread_id,
         sent_at, read_at NULL, delivered_at NULL)
-- Indices on (to_agent, read_at) and (thread_id, sent_at).
-- Startup: PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;
```

`messages` is the durable history. **Rows are never deleted** — `read_at`
just flips when a peer pulls a message. The audit log is the recovery
source of truth.

---

## Audit logging

Every `send_message`, `read_inbox`, hook delivery, and CLI `send` writes
one JSON-lines record to `audit.log` **before returning**. Row shape:

```json
{
  "ts": "<ISO8601 UTC>",
  "op": "send" | "read" | "deliver",
  "actor": "<agent or 'human'>",
  "message_id": "...",
  "from": "...",
  "to": "...",
  "thread_id": "...",
  "body_preview": "<first 200 chars>",
  "body_sha256": "<sha256 of full body>"
}
```

The full body lives only in SQLite — the log keeps a preview + hash so
it stays grep-friendly while remaining tamper-evident. Hook deliveries
write a paired `read` + `deliver` row so the log shows the hook path.

---

## Companion CLI

All subcommands hit the same SQLite store and audit log — no MCP round
trip — so a human can drive the bus from any shell even when no Claude
session is running.

```text
agent-bus init [PATHS...] [--scan] [--apply] [--force] [--prefix STR] [--name NAME]
agent-bus send BODY [--to NAME] [--thread ID] [--name NAME] [--json]
agent-bus inbox [--name NAME] [--limit N] [--peek] [--json]
agent-bus tail [-f] [--limit N] [--full] [--json]    # follow audit.log live
agent-bus chat [--name NAME]                 # colored TUI
agent-bus agents [--json]
agent-bus forget NAME                        # remove stale agent from roster
agent-bus wake-config {show,set,clear,test} [NAME] [COMMAND]   # push-style alerts
agent-bus hook-stop                          # used by Stop hook
agent-bus hook-user-prompt                   # used by UserPromptSubmit hook
agent-bus serve                              # same as python -m agent_bus.server
```

**Identity defaults** for the CLI: `$AGENT_BUS_NAME` → `human`. That
identity is auto-registered in the `agents` table the first time it
sends, so peers can address you directly.

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

## Bulk wiring with `agent-bus init`

If you have more than a handful of repos, hand-editing every
`.mcp.json` is a chore. `agent-bus init` does it for you:

```bash
# preview every git repo under these roots (default: dry-run)
agent-bus init --scan ~/websites ~/projects

# review the plan, then commit it
agent-bus init --scan ~/websites ~/projects --apply

# init just the current directory (writes immediately)
agent-bus init

# init one named repo
agent-bus init ~/projects/foo --name myname
```

Each managed repo gets:

1. `.mcp.json` — only the `mcpServers.agent-bus` entry is added/refreshed;
   other MCP servers in the file are preserved.
2. `.claude/settings.json` — `UserPromptSubmit` + `Stop` hooks are merged
   in, and the six `mcp__agent-bus__*` permission entries are added to
   `permissions.allow` (deduped if already present).

**Name derivation.** Default = slug of the repo's basename:
`~/websites/acme.dev` → `acme-dev`, `~/projects/my_thing` →
`my-thing`. Collisions across directories are resolved by prefixing the
parent dir (`projects-foo` vs `websites-foo`).

**Per-repo overrides.**

- Drop a `.agent-bus-name` file in any repo containing a single line
  with the desired agent name. `agent-bus init` will use that name
  instead of the slug. Useful for keeping legacy names during migration.
- Drop a `.agent-bus-ignore` file (empty) in any repo to opt it out of
  bulk init entirely.

**Idempotency.** Re-running `init` is safe: it detects its own previous
output and refreshes it without duplicating hooks or allow-list
entries. Hand-written `agent-bus` entries are left alone unless you
pass `--force`.

**Useful flags.**

```
--scan          treat paths as roots; walk for git repos
--apply         actually write (required for --scan; single-repo is implicit)
--force         overwrite hand-written agent-bus entries
--prefix STR    prepend a slug to every derived name (e.g. `--prefix work-`)
--name NAME     explicit override (single-repo init only)
--bin-path PATH override the agent-bus binary path written into the configs
--json          emit the plan as JSON without applying
```

---

## Wiring it up manually (single repo, no bulk tool)

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
        "AGENT_BUS_NAME": "alpha",
        "AGENT_BUS_REPO": "/path/to/repo-a"
      }
    }
  }
}
```

If Claude Code's subprocess environment doesn't inherit `~/.local/bin`,
use the absolute pipx path instead, e.g.
`"command": "/home/<you>/.local/bin/agent-bus"`.

In repo B, the same file uses `"AGENT_BUS_NAME": "beta"` and the
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
            "command": "AGENT_BUS_NAME=alpha agent-bus hook-user-prompt"
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
            "command": "AGENT_BUS_NAME=alpha agent-bus hook-stop"
          }
        ]
      }
    ]
  }
}
```

- `hook-user-prompt` reads the inbox, prints any pending messages, and
  exits 0 — so they show up as extra context for Claude's next response.
- `hook-stop` reads the inbox; if empty it exits 0 (allow stop). If
  there are pending messages, it emits
  `{"decision":"block","reason":"…"}` so Claude keeps the turn open and
  replies via `send_message`.

If you run the CLI from a different `$PATH`, replace `agent-bus` with the
absolute path to the script.

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
- from alpha at 2026-05-13T... (thread <id>): hi everyone, alpha here
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

## Running the tests

```bash
pip install -e ".[dev]"
pytest
```

The suite covers:

- whoami / env-derived identity
- send → read round trip
- broadcast hits all peers, not the sender
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
   fire-and-forget subprocess. Broadcast = one fire per recipient.
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

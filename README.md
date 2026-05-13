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
git clone git@github.com:zb-ss/agent-group-mcp.git agent-bus
cd agent-bus
pipx install .
```

This puts `agent-bus` on your `$PATH` (typically `~/.local/bin/agent-bus`)
in an isolated venv that pipx manages. Every Claude Code session, every
repo, and every shell can call it without sourcing anything.

To upgrade later: `pipx upgrade agent-bus` (from anywhere) or
`pipx install --force .` from a fresh clone.

### Alternative: editable install (development)

```bash
git clone git@github.com:zb-ss/agent-group-mcp.git agent-bus
cd agent-bus
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
agent-bus send BODY [--to NAME] [--thread ID] [--name NAME] [--json]
agent-bus inbox [--name NAME] [--limit N] [--peek] [--json]
agent-bus tail [-f] [--limit N]              # follow audit.log live
agent-bus chat                               # line-buffered TUI
agent-bus agents [--json]
agent-bus hook-stop                          # used by Stop hook
agent-bus hook-user-prompt                   # used by UserPromptSubmit hook
agent-bus serve                              # same as python -m agent_bus.server
```

**Identity defaults** for the CLI: `$AGENT_BUS_NAME` → `human`. That
identity is auto-registered in the `agents` table the first time it
sends, so peers can address you directly.

### Chat TUI (`agent-bus chat`)

Built on [prompt_toolkit](https://github.com/prompt-toolkit/python-prompt-toolkit)
so incoming messages never clobber the line you're typing on. Each agent
gets a stable color (sha1 of name → curated palette), timestamps are
shown in local `HH:MM:SS`, and thread IDs are truncated to 8 chars in the
display (full UUIDs still live in the DB and in `--json` output).

On connect you get:

```
agent-bus chat — connected as 'zoltan' (default → *). /help for commands.
agents on the bus (3):
  servonaut-cli         last seen 1m ago     pending=0
  servonaut-web-backend last seen 4s ago     pending=2
  zoltan                last seen just now (you)  pending=0
recent activity (last 10 sends):
  14:30:51  servonaut-web-backend → servonaut-cli    can you check the dashboard?
  14:31:02  servonaut-cli         → servonaut-web    on it [c7a3b2f0]
  ...

[zoltan → *] ▌
                                                 zoltan → *   /help · /quit
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

## Wiring it up in a repo

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

- HTTP / SSE transport.
- Authentication (local-only, single-user assumed).
- Message expiry / log rotation.
- Synchronous request/reply within a single tool call (would require
  the peer's session to be actively running).
- **Idle-session wake-up.** Claude Code can't be externally poked into a
  new turn while idle — the user must type something, or the session
  must use `/loop` for hands-free polling. Hooks only fire when Claude
  is already taking a turn.

---

## License

MIT.

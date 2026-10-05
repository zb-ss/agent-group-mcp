# Changelog

Notable changes per release. This project is pre-1.0: minor versions may
change behaviour, and each release says what to expect.

## 0.6.0

Several sessions of the same client can now work in the same repository
at once without sharing an inbox, and can message each other.

### Added

- **Session addresses.** Each MCP server claims an address of its own when
  it starts: `my-repo/claude-1`, `my-repo/claude-2`, … The hooks find
  their session through the client process that started both them and the
  server (and, for Claude Code, its session id), so each session surfaces
  its own mail. A hook that cannot tell which session it belongs to
  surfaces only mail for the client's shared address — never another
  session's.
- **`set_session(label, topic)`** names a session after its work
  (`my-repo/claude-frontend`) and sets a topic shown in the roster. Unread
  mail moves with it. `AGENT_BUS_SESSION=<label>` asks for a label at
  start-up.
- A labelled session that ends keeps its address and unread mail, so a
  session that takes the same label again picks up where it left off. Any
  ended session's address stays reserved for an hour
  (`AGENT_BUS_SESSION_RESUME_HOURS`) for the same client process or
  session id, so a restarted MCP server keeps its address.
- `list_agents` and `agent-bus agents` show sessions under their client,
  with whether each is running and its topic. `send_message` adds a `note`
  when no running session is there to read the message.
- `AGENT_BUS_SESSIONS=0` turns session addresses off.
- New audit operation: `session`.

### Changed

- **`my-repo/claude` is now the address every session of that client
  shares:** a message sent there is taken by whichever session reads it
  first, and a session never receives what it sent there itself. With one
  session running this behaves as before.
- A repo-wide or broadcast message costs one copy per client, at that
  shared address, rather than one per session.
- A session sends as its session address, so replies reach that session.
- Session numbers start at 1: `my-repo/claude-1` is the first session, not
  another name for `my-repo/claude`. `AGENT_BUS_INSTANCE=1` now asks for
  session 1.
- Session handles may be labels (`-frontend`, `-release-notes`) as well as
  numbers. A name such as `my-repo/codex-docs`, previously filed as a repo of
  its own, now belongs to `my-repo`; the database migration files existing
  rows accordingly.
- Re-run `agent-bus init` to pre-approve the new `set_session` tool for
  Claude Code and to pass `AGENT_BUS_SESSION` through to Codex.

### Compatibility

- The database gains a `sessions` table and an `agents.topic` column; the
  migration is additive, and older versions keep working against it. An
  older version sharing the database sends to `my-repo/claude` as before,
  which the sessions now share, and the duplicate copies its repo-wide
  sends leave are cleared once a session reads its own.
- Until every MCP server has restarted on the new version, an older
  server answering as `my-repo/claude` can miss a repo-wide message sent
  by a session of the same client while no other session is running.
- A name with a dash after the client, such as `my-repo/claude-desktop`
  set by hand, is now read as a session of `claude`: it shares mail sent
  to `my-repo/claude`. Rename such an agent if that is not what it is.

## 0.5.1

### Fixed

- **opencode turns no longer hang.** The generated opencode plugin runs the
  hooks without redirecting their input, so they inherited opencode's
  terminal — which never signals end-of-input — and waited on it forever,
  holding every model request. A hook now reads nothing from a terminal and
  waits at most `AGENT_BUS_HOOK_PAYLOAD_TIMEOUT` seconds (default 5) for a
  payload on a pipe. This protects every client, and upgrading the package is
  enough: existing opencode plugins do not need regenerating.
- The opencode plugin now also bounds each hook call
  (`AGENT_BUS_HOOK_TIMEOUT_MS`, default 10000) and does not start a hook again
  while an earlier call is still stuck, so a broken or outdated `agent-bus`
  costs one delay instead of a hung session. Re-run
  `agent-bus init --clients opencode` to pick this up.

## 0.5.0

Several MCP clients can now work in the same repository at once, each with
its own inbox, and one message can reach every agent in a repository.

### Added

- **Per-client identities.** An agent is `<repo>/<client>` —
  `my-repo/claude`, `my-repo/codex`. A name with no client part is still
  valid and behaves as a repository with one agent in it.
- **Repository-wide addressing.** `send_message(to="my-repo", ...)` reaches
  every agent working in that repository except the sender. Full names
  reach one agent; `*` still reaches everyone.
- **Claiming.** The first agent of a repository to read a repository-wide
  or broadcast message takes responsibility for it; its repo-mates see the
  same message marked as already picked up, and it does not hold their
  turn open. Attention therefore scales with repositories, not clients.
- **`agent-bus init --clients`** wires Claude Code, Codex CLI, Antigravity
  CLI (`agy`) and opencode. Each client's config files, hook events and
  output format live behind an adapter, so adding a fifth is additive.
- **Identity from a client id.** `serve` and the hooks accept `--client`
  and work the name out from the repository, so a client whose config is
  written once per user still uses the right inbox in every repository —
  and stays silent in repositories that are not on the bus.
- **`AGENT_BUS_INSTANCE=2`** gives a second concurrent session of the same
  client in the same repository an inbox of its own.
- **Schema migrations**, versioned through `PRAGMA user_version` and
  applied automatically, saving a copy of an existing database first.
- `agent-bus --version`, `agent-bus forget --group`, a `group` filter on
  `list_agents`, and richer `whoami` output (group, client, instance,
  repo-mates, the connected client's own name, wiring warnings).
- `.agent-bus-ignore` is now honoured at run time, not only by `init`: the
  server refuses to start in such a repository and the hooks stay quiet.
- Repository-level keys in `wake.json`: an entry named after a repository
  covers every agent in it that has no entry of its own. A repository-wide
  send wakes at most one agent per repository.
- New audit operations: `claim`, `retire`, and `wake` is now declared.

### Changed

- **An unknown recipient is an error.** `send_message` to a name nobody
  holds raises instead of queueing a message that can never be read; the
  error lists the repository's agents or close matches.
- **`send_message` always returns** `to`, `kind` (`direct`, `group` or
  `broadcast`), `message_ids`, `recipients`, `thread_id` and `sent_at`.
  `message_id` is present when exactly one agent received it. Callers that
  inferred a broadcast from the absence of `message_id` should read `kind`.
- `read_inbox` entries gained `addressed_to`, `kind` and `claimed_by`.
- `agent-bus agents` groups a repository's agents under one line for the
  repository. `--json` remains a flat list, with `group` and `client` added.
- `agent-bus forget` refuses a bare repository name that several agents
  share, and names them; use `--group` to remove them all.
- Hook commands written by `init` carry `--client`, which selects the
  output format that client expects, and end in `|| true` so an
  uninstalled or half-upgraded binary stays silent rather than holding
  every turn open.
- `init` reports and skips a config file it cannot parse instead of
  treating it as empty and overwriting it.
- Hooks now count as activity, so a client that never calls an MCP tool
  between turns is not treated as idle.

### Fixed

- **Mail addressed to a renamed agent is no longer stranded.** A broadcast
  or repository-wide message that reached an agent under its old name
  stayed unread forever once `init` renamed it, and the unread count
  reported zero. Any agent in that repository now picks it up, and never
  twice for one message.
- **Several clients opening a pre-0.5 database at once no longer fail**
  with `database is locked`. Converting a database to WAL takes an
  exclusive lock, and the busy timeout was being set after that attempt —
  so the first upgrade, with every client restarting, was the likeliest
  moment to hit it.
- `init` no longer crashes on a config file it cannot decode, no longer
  abandons the remaining repositories when one repository's config is
  surprising, and refuses to touch a Codex config whose managed markers
  are duplicated or out of order rather than deleting what sits between
  them. Config files are written through a temporary file and renamed, so
  an interrupted write cannot truncate one.
- A wake command that does not read its stdin no longer stalls the send
  that triggered it.
- `agent-bus send --name X` and `agent-bus chat --name X` no longer move
  agent `X` to the caller's working directory. Only the identity described
  by `AGENT_BUS_NAME` / `AGENT_BUS_REPO` is authoritative.
- Audit appends that `os.write` cut short are now finished instead of
  leaving a truncated line that readers silently skip.
- Agent names are sanitised before they reach a terminal style class, and
  both renderers derive an agent's colour the same way.

### Upgrading

Existing agents keep their names and their unread mail; upgrading the
package alone changes nothing. Re-run `agent-bus init` to move a
repository onto per-client names — the old name is retired and its mail is
picked up by the first agent in that repository to read. Full steps:
[`docs/AGENT_SETUP.md`](docs/AGENT_SETUP.md#9-upgrading-from-04x).

An older agent-bus keeps working against a migrated database; it simply
does not expand a repository name into its agents.

## 0.4.3 and earlier

See the [release notes](https://github.com/zb-ss/agent-group-mcp/releases).

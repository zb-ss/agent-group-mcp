# Changelog

Notable changes per release. This project is pre-1.0: minor versions may
change behaviour, and each release says what to expect.

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

"""Work out which agent this process is speaking for.

The MCP server and the hook commands share one rule:

  1. ``AGENT_BUS_NAME`` — used as given. Wiring that names the agent
     outright (everything `agent-bus init` wrote before per-client
     identities, and per-repo configs today) needs nothing else.
  2. Otherwise a client id (``--client`` or ``AGENT_BUS_CLIENT``) plus a
     repo: ``AGENT_BUS_REPO``, else the directory a hook payload reports,
     else the working directory — walked up to the repository root. The
     name is whatever that client registered for that repo, or
     ``<group>/<client>`` derived from the repo when nothing is registered.
  3. ``AGENT_BUS_SESSION=<label>`` or ``AGENT_BUS_INSTANCE=<n>`` appends a
     session handle: the session address this process asks for.

The result is a client address (``<repo>/<client>``), or a session address
when a handle was asked for. Which session address a server actually holds
is settled by `sessions.SessionRegistry.claim`, and a hook finds it with
`sessions.SessionRegistry.for_hook`; this module only says whose they are.

Rule 2 is what lets one hook command serve every repo: a client whose hooks
are configured once per user still drains the right inbox in each repo, and
stays silent in repos that are not on the bus (`registered_only`).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from . import identity
from .storage import Storage

NAME_ENV = "AGENT_BUS_NAME"
REPO_ENV = "AGENT_BUS_REPO"
CLIENT_ENV = "AGENT_BUS_CLIENT"
INSTANCE_ENV = "AGENT_BUS_INSTANCE"
SESSION_ENV = "AGENT_BUS_SESSION"

NAME_FILE = ".agent-bus-name"
IGNORE_FILE = ".agent-bus-ignore"


class IdentityError(ValueError):
    """The environment does not say, or contradicts, who this process is."""


class RepoOptedOutError(IdentityError):
    """The repo carries an `.agent-bus-ignore` marker, so no agent runs here."""

    def __init__(self, repo: Path) -> None:
        self.repo = repo
        super().__init__(
            f"{repo} is opted out of the bus by its {IGNORE_FILE} file. "
            f"Remove that file to let an agent run here."
        )


@dataclass(frozen=True)
class ResolvedIdentity:
    name: str
    repo_path: str


def parse_hook_payload(raw: str) -> dict:
    """A hook's stdin as a dict. Anything unusable reads as empty — a hook
    must never fail because a client changed its payload."""
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def find_repo_root(start: Path) -> Path:
    """The nearest ancestor holding `.git` (a directory, or a worktree's
    file); `start` itself when there is none."""
    start = start.expanduser().resolve()
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            return candidate
    return start


def is_opted_out(repo: Path) -> bool:
    """True when `repo` carries the marker that keeps agents out of it.

    `init` skips such a repo when writing wiring; this keeps a client whose
    config is shared across repos — a user-level MCP server or hook — from
    joining the bus there anyway.
    """
    return (repo / IGNORE_FILE).exists()


def pinned_group(repo: Path) -> str | None:
    """The group name a `.agent-bus-name` file pins for `repo`, if any."""
    name_file = repo / NAME_FILE
    if not name_file.exists():
        return None
    try:
        raw = name_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return identity.slugify(raw) if raw else None


def group_for_repo(repo: Path) -> str:
    """The repo's group name: its `.agent-bus-name` file, else its slug."""
    return pinned_group(repo) or identity.slugify(repo.name)


def _handle(env: Mapping[str, str]) -> tuple[str, str] | None:
    """(variable, handle) for the session handle the environment asks for:
    a label from AGENT_BUS_SESSION, else a number from AGENT_BUS_INSTANCE."""
    label = env.get(SESSION_ENV, "").strip()
    if label:
        try:
            return SESSION_ENV, identity.normalize_label(label)
        except identity.InvalidNameError as e:
            raise IdentityError(f"{SESSION_ENV}: {e}") from None
    raw = env.get(INSTANCE_ENV, "").strip()
    if not raw:
        return None
    try:
        return INSTANCE_ENV, identity.check_handle(str(int(raw)))
    except (ValueError, identity.InvalidNameError):
        raise IdentityError(
            f"{INSTANCE_ENV} must be a number {identity.MIN_NUMBER}.."
            f"{identity.MAX_NUMBER}, got {raw!r}"
        ) from None


def _with_handle(name: str, handle: tuple[str, str] | None) -> str:
    if handle is None:
        return name
    variable, value = handle
    parsed = identity.parse_or_none(name)
    if parsed is None or parsed.client is None:
        raise IdentityError(
            f"{variable} is set, but {name!r} has no client part to add a "
            f"session to. Name the agent <group>{identity.SEPARATOR}<client> first."
        )
    return identity.compose(parsed.group, parsed.client, value)


def _registered_name(storage: Storage, repo_path: str, client: str) -> str | None:
    """What `client` is called in this repo according to the roster. `init`
    may have renamed the group to tell two same-named repos apart, which a
    path-derived name would not know about."""
    parsed = [
        identity.parse_or_none(a.name)
        for a in storage.list_agents()
        if a.repo_path == repo_path and a.client == client
    ]
    addresses = {p.client_address for p in parsed if p is not None}
    return min((a for a in addresses if a), default=None)


def resolve(
    *,
    storage: Storage,
    env: Mapping[str, str] | None = None,
    client: str | None = None,
    repo_hint: str | None = None,
    cwd: Path | None = None,
    registered_only: bool = False,
) -> ResolvedIdentity | None:
    """Apply the module's rule. Returns None only when `registered_only`
    is set and this client has no agent in this repo."""
    env = os.environ if env is None else env
    cwd = cwd or Path.cwd()
    handle = _handle(env)

    explicit = env.get(NAME_ENV)
    if explicit:
        repo_path = env.get(REPO_ENV) or str(find_repo_root(Path(repo_hint or cwd)))
        if is_opted_out(Path(repo_path)):
            if registered_only:
                return None
            raise RepoOptedOutError(Path(repo_path))
        return ResolvedIdentity(_with_handle(explicit, handle), repo_path)

    client = client or env.get(CLIENT_ENV)
    if not client:
        raise IdentityError(
            f"cannot tell which agent this is: set {NAME_ENV}, or pass --client "
            f"(or {CLIENT_ENV}) so it can be derived from the repository"
        )
    if not identity.is_valid_client(client):
        raise IdentityError(f"invalid client id {client!r}")

    repo = find_repo_root(Path(env.get(REPO_ENV) or repo_hint or cwd))
    if is_opted_out(repo):
        if registered_only:
            return None
        raise RepoOptedOutError(repo)
    name = _registered_name(storage, str(repo), client)
    if name is None:
        if registered_only:
            return None
        name = identity.compose(group_for_repo(repo), client)
    return ResolvedIdentity(_with_handle(name, handle), str(repo))

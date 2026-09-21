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
  3. ``AGENT_BUS_INSTANCE=n`` appends ``-n``, so a second session of the
     same client in the same repo gets an inbox of its own.

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


def _instance(env: Mapping[str, str]) -> int | None:
    raw = env.get(INSTANCE_ENV, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise IdentityError(f"{INSTANCE_ENV} must be a number, got {raw!r}") from None


def _with_instance(name: str, instance: int | None) -> str:
    if instance is None:
        return name
    parsed = identity.parse_or_none(name)
    if parsed is None or parsed.client is None:
        raise IdentityError(
            f"{INSTANCE_ENV} is set, but {name!r} has no client part to number. "
            f"Name the agent <group>{identity.SEPARATOR}<client> first."
        )
    try:
        return identity.compose(parsed.group, parsed.client, instance)
    except identity.InvalidNameError as e:
        raise IdentityError(str(e)) from None


def _registered_name(storage: Storage, repo_path: str, client: str) -> str | None:
    """What `client` is called in this repo according to the roster. `init`
    may have renamed the group to tell two same-named repos apart, which a
    path-derived name would not know about."""
    names = [
        a.name for a in storage.list_agents()
        if a.repo_path == repo_path and a.client == client
    ]
    first_sessions = [n for n in names if identity.parse(n).instance is None]
    return min(first_sessions or names, default=None)


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
    instance = _instance(env)

    explicit = env.get(NAME_ENV)
    if explicit:
        repo_path = env.get(REPO_ENV) or str(find_repo_root(Path(repo_hint or cwd)))
        if is_opted_out(Path(repo_path)):
            if registered_only:
                return None
            raise RepoOptedOutError(Path(repo_path))
        return ResolvedIdentity(_with_instance(explicit, instance), repo_path)

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
    return ResolvedIdentity(_with_instance(name, instance), str(repo))

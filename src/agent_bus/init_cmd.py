"""Bulk wire `agent-bus` into many git repos at once.

Each repo is wired once per MCP client you ask for (`--clients`, default
Claude Code only). What "wired" means — which files, which hook events —
is the client adapter's business (`clients/`); this module decides names
and what to do, and never touches a config file itself.

A repo's agents are named ``<group>/<client>``. The group is derived from
the repo's basename by default (e.g. ``~/websites/acme.dev`` →
``acme-dev``). Two overrides:

  * A per-repo ``.agent-bus-name`` file (one line, the desired group).
  * The ``--name`` CLI flag (single-repo init only).

A ``.agent-bus-ignore`` file in any repo causes that repo to be
skipped during bulk scans.

Idempotency: re-running ``agent-bus init`` against the same repo
produces no change because each adapter recognises its own structural
fingerprint (``command`` references ``agent-bus`` and ``args[0] ==
"serve"``). Hand-written entries with a different shape are left alone
unless the user passes ``--force``, and a config file that cannot be
parsed is never overwritten. Wiring written before per-client identities
(a bare group as the agent name) is upgraded in place.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterable

from . import clients, identity
from .clients.base import WiringStatus
from .clients.claude import (  # noqa: F401  (public names that lived here)
    ALLOW_TOOLS,
    build_mcp_entry,
    is_managed_hook_block,
    is_managed_mcp_entry,
)
from .identity import MAX_GROUP_LEN, slugify
from .resolution import NAME_FILE, pinned_group
from .storage import Storage

IGNORE_FILE = ".agent-bus-ignore"
MAX_NAME_LEN = MAX_GROUP_LEN  # kept for callers that imported it from here
DEFAULT_SCAN_DEPTH = 6
DEFAULT_CLIENTS = ("claude",)


# --------------------------- naming ------------------------------------


def resolve_name(
    repo: Path,
    *,
    prefix: str | None = None,
    override: str | None = None,
) -> str:
    """Determine the group name for `repo`.

    Priority: explicit override > per-repo .agent-bus-name file > slug.
    Every source is slugified, so users can't accidentally pin an invalid
    name. The prefix only applies to a name derived from the basename.
    """
    if override:
        return slugify(override)
    pinned = pinned_group(repo)
    if pinned:
        return pinned
    derived = slugify(repo.name)
    if prefix:
        derived = slugify(f"{prefix}-{derived}")
    return derived


def deconflict_names(plans: list["InitPlan"]) -> None:
    """In-place: rename plans whose slugs collide so each one is unique.

    The first occurrence keeps the bare slug; subsequent collisions
    get the parent-dir prefixed.
    """
    by_name: dict[str, list[InitPlan]] = {}
    for p in plans:
        by_name.setdefault(p.name, []).append(p)
    for name, group in by_name.items():
        if len(group) <= 1:
            continue
        # keep first as-is, rename the rest
        for p in group[1:]:
            parent = slugify(p.repo.parent.name)
            p.name = slugify(f"{parent}-{name}")
            p.notes.append(f"renamed to deconflict with {group[0].repo}")


# --------------------------- repo detection ----------------------------


def is_git_repo(path: Path) -> bool:
    """A directory with .git (file or directory) is a git working copy."""
    return (path / ".git").exists()


def has_ignore_marker(path: Path) -> bool:
    return (path / IGNORE_FILE).exists()


def find_git_repos(
    root: Path, *, max_depth: int = DEFAULT_SCAN_DEPTH
) -> list[Path]:
    """Return git repos under `root`, skipping nested repos and a few
    obvious dirs (node_modules / .venv / vendor) for speed."""
    root = root.expanduser().resolve()
    if not root.exists():
        return []
    if is_git_repo(root):
        return [root]

    skip_dirs = {
        ".git", "node_modules", ".venv", "venv", "__pycache__",
        ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
        "vendor", "build", "dist", ".next", ".nuxt",
    }
    out: list[Path] = []
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        dirpath, depth = stack.pop()
        try:
            entries = list(dirpath.iterdir())
        except (OSError, PermissionError):
            continue
        if any(e.name == ".git" for e in entries):
            out.append(dirpath)
            continue  # do not descend into a git repo (skip submodules)
        if depth >= max_depth:
            continue
        for e in entries:
            if not e.is_dir():
                continue
            if e.is_symlink():
                continue
            if e.name in skip_dirs:
                continue
            if e.name.startswith("."):
                # hidden dirs other than .git are usually editor/config
                # state — don't recurse into them.
                continue
            stack.append((e, depth + 1))
    out.sort()
    return out


# --------------------------- agent-bus binary path ---------------------


def detect_agent_bus_bin(explicit: str | None = None) -> str:
    """Best-effort: explicit > $AGENT_BUS_BIN env > shutil.which > bare command.

    Returns the path as PATH gives it to us (typically the pipx shim at
    ``~/.local/bin/agent-bus``). We deliberately do **not** resolve symlinks:
    pipx shims survive `pipx upgrade` / `pipx reinstall` and even package
    renames, while the underlying venv path moves and breaks every wiring
    that baked in the resolved target.
    """
    if explicit:
        return explicit
    import os

    env_path = os.environ.get("AGENT_BUS_BIN")
    if env_path:
        return env_path
    found = shutil.which("agent-bus")
    if found:
        return found
    return "agent-bus"


# --------------------------- planning + dry-run ------------------------


class Action(Enum):
    WRITE = "write"          # no agent-bus entry yet → create
    REFRESH = "refresh"      # existing managed entry → update (e.g. name change)
    SKIP_HANDWRITTEN = "skip-handwritten"   # foreign entry, no --force
    SKIP_UNREADABLE = "skip-unreadable"     # config we cannot parse; never overwritten
    SKIP_IGNORED = "skip-ignored"
    SKIP_NOT_REPO = "skip-not-repo"


CHANGES = (Action.WRITE, Action.REFRESH)


@dataclass
class ClientPlan:
    client: str
    action: Action
    previous_name: str | None = None

    @property
    def is_change(self) -> bool:
        return self.action in CHANGES


@dataclass
class InitPlan:
    repo: Path
    name: str  # the repo's group; each client is wired as <name>/<client>
    action: Action
    previous_name: str | None = None
    notes: list[str] = field(default_factory=list)
    clients: list[ClientPlan] = field(default_factory=list)

    @property
    def is_change(self) -> bool:
        return self.action in CHANGES

    def agent_name(self, client: str) -> str:
        return identity.compose(self.name, client)

    def renames(self) -> list[tuple[str, str]]:
        """(old, new) for each client whose wired name is about to change."""
        return [
            (c.previous_name, self.agent_name(c.client))
            for c in self.clients
            if c.is_change and c.previous_name
            and c.previous_name != self.agent_name(c.client)
        ]


def _plan_client(repo: Path, client: str, *, force: bool) -> ClientPlan:
    state = clients.wiring(client).inspect(repo)
    if state.status is WiringStatus.UNREADABLE:
        return ClientPlan(client, Action.SKIP_UNREADABLE)
    if state.status is WiringStatus.ABSENT:
        return ClientPlan(client, Action.WRITE)
    if state.status is WiringStatus.HANDWRITTEN and not (force and state.can_force):
        return ClientPlan(client, Action.SKIP_HANDWRITTEN)
    return ClientPlan(client, Action.REFRESH, previous_name=state.name)


def _repo_action(client_plans: list[ClientPlan]) -> Action:
    """One verdict per repo: a change if any client changes, else why not."""
    actions = [c.action for c in client_plans]
    if Action.REFRESH in actions:
        return Action.REFRESH
    if Action.WRITE in actions:
        return Action.WRITE
    if Action.SKIP_UNREADABLE in actions:
        return Action.SKIP_UNREADABLE
    return Action.SKIP_HANDWRITTEN


def plan_for_repo(
    repo: Path,
    *,
    prefix: str | None = None,
    override: str | None = None,
    force: bool = False,
    bin_path: str | None = None,
    client_ids: Iterable[str] = DEFAULT_CLIENTS,
) -> InitPlan:
    repo = repo.expanduser().resolve()
    if not is_git_repo(repo):
        return InitPlan(repo=repo, name="", action=Action.SKIP_NOT_REPO)
    if has_ignore_marker(repo):
        return InitPlan(repo=repo, name="", action=Action.SKIP_IGNORED)

    client_plans = [_plan_client(repo, c, force=force) for c in client_ids]
    previous = next((c.previous_name for c in client_plans if c.previous_name), None)
    return InitPlan(
        repo=repo,
        name=resolve_name(repo, prefix=prefix, override=override),
        action=_repo_action(client_plans),
        previous_name=previous,
        clients=client_plans,
    )


def _describe(plan: InitPlan, storage: Storage | None) -> None:
    """Fill in the notes a person needs before saying yes. Runs after
    deconfliction, because the names it talks about are final only then."""
    for old, new in plan.renames():
        plan.notes.append(f"renaming from '{old}' to '{new}'")
    for c in plan.clients:
        if c.action is Action.SKIP_UNREADABLE:
            plan.notes.append(f"{c.client}: config file is not valid JSON, left alone")
        elif c.action is Action.SKIP_HANDWRITTEN and plan.is_change:
            plan.notes.append(f"{c.client}: hand-written entry left alone")
    if storage is None or not plan.is_change:
        return
    others = sorted({
        a.repo_path for a in storage.list_agents()
        if a.group == plan.name and a.repo_path != str(plan.repo)
    })
    if others:
        plan.notes.append(
            f"'{plan.name}' is also registered for {', '.join(others)} — if that "
            f"repo is live too they share an inbox; pin a name with {NAME_FILE}"
        )


def plan_for_paths(
    paths: Iterable[Path],
    *,
    scan: bool,
    prefix: str | None = None,
    override: str | None = None,
    force: bool = False,
    bin_path: str | None = None,
    client_ids: Iterable[str] = DEFAULT_CLIENTS,
    storage: Storage | None = None,
) -> list[InitPlan]:
    client_ids = tuple(client_ids)
    repos: list[Path] = []
    for p in paths:
        p = p.expanduser().resolve()
        if scan:
            repos.extend(find_git_repos(p))
        else:
            repos.append(p)
    plans = [
        plan_for_repo(
            r,
            prefix=prefix,
            override=override if not scan else None,
            force=force,
            bin_path=bin_path,
            client_ids=client_ids,
        )
        for r in repos
    ]
    deconflict_names([p for p in plans if p.is_change])
    for plan in plans:
        _describe(plan, storage)
    return plans


# --------------------------- apply ------------------------------------


def apply_plan(
    plan: InitPlan, *, bin_path: str, storage: Storage | None = None
) -> None:
    """Wire each client of one repo.

    With a `storage`, a name the wiring no longer uses is also retired from
    the roster, so it neither lingers as a stale repo-mate nor strands its
    unread mail. Assumes `plan.is_change`; callers should filter SKIP_*.
    """
    if not plan.is_change:
        return
    for client_plan in plan.clients:
        if not client_plan.is_change:
            continue
        name = plan.agent_name(client_plan.client)
        clients.wiring(client_plan.client).apply(
            plan.repo, name=name, bin_path=bin_path
        )
        if storage is not None and client_plan.previous_name:
            storage.retire_agent(client_plan.previous_name, successor=name)


def summarise(plans: list[InitPlan]) -> dict[str, int]:
    counts: dict[str, int] = {a.value: 0 for a in Action}
    counts["rename"] = 0
    for p in plans:
        counts[p.action.value] += 1
        if p.renames():
            counts["rename"] += 1
    return counts

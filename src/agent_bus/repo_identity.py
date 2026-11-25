"""Repo identity by normalised git remote URL, and local clone resolution.

The id is ``<host>/<path>``, lowercase, with scheme, user info, port, trailing
slashes and one trailing ``.git`` dropped and host aliases applied. The same
repo therefore has the same id on every machine, whatever URL form each
clone uses.

The id is a second key beside the group alias (``--name`` >
``.agent-bus-name`` > folder slug), which stays the per-machine human name.
Nothing here renames a group. Each machine resolves its own clone: from the
roster first, then by scanning the configured root folders.
"""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

from . import init_cmd, resolution
from .commands import Runner, run_command
from .config import ConfigError
from .storage import TIMESTAMP_FORMAT

_SCHEME_RE = re.compile(r"^(?:git\+ssh|ssh|https?|git)://", re.IGNORECASE)
_HOST_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*$")
# A path segment: letters, digits, `_ . - ~` and percent-escapes (hosts use
# `~user` project owners and `My%20Project`); never `-` first, never `.`/`..`.
_SEGMENT_RE = re.compile(
    r"^(?!\.\.?$)(?:[a-z0-9_.~]|%[0-9a-f]{2})(?:[a-z0-9._~-]|%[0-9a-f]{2})*$"
)
# A folder name safe to create inside the clone root: no leading dot or other
# punctuation, so a remote's name can never become `.git` or a dot-directory.
_FOLDER_RE = re.compile(r"^[a-z0-9_][a-z0-9._-]*$")
# User info of a scheme URL: never starts with `-`; the password part cannot
# hold `@` or `/` (the authority check already refuses `\\ # ?` and whitespace).
_USERINFO_RE = re.compile(r"^[A-Za-z0-9._~%][A-Za-z0-9._~%-]*(:[^@/]*)?$")
_STRICT_SCP_RE = re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[A-Za-z0-9._/~-]+$")
_SCP_RE = re.compile(r"^(?:[^@/\s:]+@)?([^/\s:@]+):(?!//)(.+)$")


@dataclass(frozen=True)
class _SchemeUrl:
    """A scheme URL taken apart the way git and curl read it."""

    host: str
    path: str
    clone_url: str


def _parse_scheme_url(value: str) -> _SchemeUrl | None:
    """Parse a `scheme://` URL strictly (optional user info, host, optional
    port, path), or None.

    The host must be the one git and curl will contact, so anything those
    tools read differently from a naive split is refused: a query or fragment
    (`https://evil.example#@github.com/o/r` contacts evil.example), a
    backslash, whitespace, an odd user info, a host starting with `-`. The
    returned `clone_url` is rebuilt from the parsed parts, so what git is
    asked to fetch is exactly what was validated."""
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return None
    authority = parts.netloc
    if parts.query or parts.fragment or not authority:
        return None
    if re.search(r"[\\\s#?]", authority):
        return None
    userinfo, at, hostport = authority.rpartition("@")
    if at and not _USERINFO_RE.match(userinfo):
        return None
    host = hostport.rsplit(":", 1)[0] if ":" in hostport else hostport
    if port is None and ":" in hostport and hostport.rsplit(":", 1)[1]:
        return None
    if host.startswith("-") or not host:
        return None
    clone_url = urlunsplit((parts.scheme, authority, parts.path, "", ""))
    return _SchemeUrl(host=host, path=parts.path.lstrip("/"), clone_url=clone_url)


def normalize_remote(url: str, host_aliases: Mapping[str, str] | None = None) -> str | None:
    """`<host>/<path>` for a remote URL, or None when it names no remote."""
    value = (url or "").strip()
    if not value or value[0] in "/.~-" or value.lower().startswith("file:"):
        return None

    if _SCHEME_RE.match(value):
        parsed = _parse_scheme_url(value)
        if parsed is None:
            return None
        host, path = parsed.host, parsed.path
    else:
        scp = _SCP_RE.match(value)
        if scp:
            host, path = scp.group(1), scp.group(2)
        else:
            host, _, path = value.partition("/")
            if "." not in host:
                return None

    host = host.strip().lower()
    if not _HOST_RE.match(host):
        return None
    path = path.strip().strip("/")
    if path.lower().endswith(".git"):
        path = path[: -len(".git")]
    path = path.strip("/").lower()
    if not host or not path or not all(_SEGMENT_RE.match(p) for p in path.split("/")):
        return None
    host = (host_aliases or {}).get(host, host)
    return f"{host}/{path}"


def is_worktree(repo: Path) -> bool:
    return (repo / ".git").is_file()


# `git remote get-url` exits with this code for "No such remote"; any other
# failure (not a repository, permissions, a timeout) is an error, not an answer.
NO_SUCH_REMOTE_EXIT = 2


@dataclass(frozen=True)
class Lookup:
    """What asking git for a clone's remote produced. `definite` is False for
    errors and timeouts: those say nothing about the remote and are never
    cached."""

    definite: bool
    repo_id: str | None = None


def lookup_repo_id(repo: Path, *, cfg: Mapping[str, Any], run: Runner = run_command) -> Lookup:
    argv = ["git", "-C", str(repo), "remote", "get-url", cfg["remote"]]
    try:
        done = run(argv, timeout=cfg["git_timeout_seconds"])
    except (OSError, subprocess.SubprocessError):
        return Lookup(False)
    if done.returncode == NO_SUCH_REMOTE_EXIT:
        return Lookup(True)
    if done.returncode != 0:
        return Lookup(False)
    return Lookup(True, normalize_remote(done.stdout, cfg.get("host_aliases")))


def repo_id(repo: Path, *, cfg: Mapping[str, Any], run: Runner = run_command) -> str | None:
    """The repo's id from its configured remote; None when it has none or
    the lookup failed."""
    return lookup_repo_id(repo, cfg=cfg, run=run).repo_id


@dataclass(frozen=True)
class LocalRepo:
    path: Path
    repo_id: str | None
    alias: str


class RepoNotFoundError(LookupError):
    def __init__(self, name: str, clone_url: str | None, clone_dest: Path | None) -> None:
        self.name = name
        self.clone_url = clone_url
        self.clone_dest = clone_dest
        super().__init__(f"no local clone of {name!r} found")


class AmbiguousRepoError(LookupError):
    def __init__(self, name: str, candidates: list[LocalRepo]) -> None:
        self.name = name
        self.candidates = candidates
        listing = ", ".join(str(c.path) for c in candidates)
        super().__init__(f"{name!r} matches more than one clone: {listing}")


def _roster_paths(storage) -> list[tuple[Path, list[str]]]:
    """Distinct existing repo paths of roster rows with a client part, each
    with every group registered for it. A repo renamed since its labelled
    rows were written has several; the group the repo's own wiring gives
    comes first so it wins ordering ties."""
    groups: dict[str, list[str]] = {}
    for row in storage.list_agents():
        if row.client_id is None:
            continue
        known = groups.setdefault(row.repo_path, [])
        if row.group not in known:
            known.append(row.group)
    out = []
    for raw, names in groups.items():
        path = Path(raw)
        if not path.exists():
            continue
        current = resolution.group_for_repo(path)
        names.sort(key=lambda g: g != current)
        out.append((path, names))
    return out


def _is_stale(checked_at: str, ttl_seconds: float) -> bool:
    try:
        checked = datetime.strptime(checked_at, TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return (datetime.now(timezone.utc) - checked).total_seconds() >= ttl_seconds


def _fresh_answer(entry: tuple[str | None, str] | None, ttl_seconds: float) -> bool:
    """True when the cache holds a definite answer, an id or a checked
    `no remote`, that is still inside the recheck period."""
    return entry is not None and not _is_stale(entry[1], ttl_seconds)


def refresh_missing(
    storage, cfg: Mapping[str, Any], *, run: Runner = run_command,
    budget_seconds: float | None = None,
) -> None:
    """Fill the id cache for roster repos not asked yet, and re-ask the ones
    whose answer (an id or a definite `no remote`) is older than
    `repos.id_recheck_seconds`. A failed lookup changes nothing.

    `budget_seconds` bounds the whole pass, because failed lookups are never
    cached and a hanging path would otherwise cost a full timeout on every
    call: each lookup gets what is left of it, and none starts once it is
    spent. None means no bound."""
    known = storage.repo_id_entries()
    deadline = None if budget_seconds is None else time.monotonic() + budget_seconds
    for path, _groups in _roster_paths(storage):
        if _fresh_answer(known.get(str(path)), cfg["id_recheck_seconds"]):
            continue
        limited = _within_budget(cfg, deadline)
        if limited is None:
            return
        found = lookup_repo_id(path, cfg=limited, run=run)
        if found.definite:
            storage.set_repo_id(str(path), found.repo_id)


def _within_budget(cfg: Mapping[str, Any], deadline: float | None) -> Mapping[str, Any] | None:
    """`cfg` with the git timeout cut to the time left, or None when none is."""
    if deadline is None:
        return cfg
    left = deadline - time.monotonic()
    if left <= 0:
        return None
    return {**cfg, "git_timeout_seconds": min(cfg["git_timeout_seconds"], left)}


def folder_name(rid: str) -> str | None:
    """The clone folder for an id, or None when its last segment is unsafe.

    Repeated `.git` suffixes are dropped first, so a remote named `.git.git`
    cannot turn the clone root itself into a repository."""
    name = rid.rsplit("/", 1)[-1]
    while name.endswith(".git"):
        name = name[: -len(".git")]
    return name if _FOLDER_RE.match(name) else None


def clone_target(name: str, cfg: Mapping[str, Any]) -> tuple[str, Path | None] | None:
    """(clone URL, destination) for a name that identifies a remote, or None
    when it names none or its folder name would be unsafe."""
    aliases = cfg.get("host_aliases")
    rid = normalize_remote(name, aliases)
    if rid is None:
        return None
    folder = folder_name(rid)
    if folder is None:
        return None
    # A raw name is only passed through as the clone URL for a known scheme or
    # a strict user@host:path form; anything else is rebuilt from the id.
    raw = name.strip()
    scheme_url = _parse_scheme_url(raw) if _SCHEME_RE.match(raw) else None
    if scheme_url is not None:
        url = scheme_url.clone_url
    elif _STRICT_SCP_RE.match(raw):
        url = raw
    else:
        host, _, path = rid.partition("/")
        try:
            url = cfg["clone_url"].format(host=host, path=path)
        except (KeyError, IndexError, ValueError) as exc:
            raise ConfigError(
                f"repos.clone_url {cfg['clone_url']!r} may only use the placeholders "
                f"{{host}} and {{path}} ({type(exc).__name__}: {exc})"
            ) from exc
    root = cfg.get("clone_root") or (cfg.get("roots") or [None])[0]
    dest = Path(root).expanduser() / folder if root else None
    return url, dest


def _candidates(
    storage, cfg, run: Runner, *, scan: bool, refresh: bool = False, persist: bool = True
) -> list[LocalRepo]:
    """Clones to match against. `persist=False` keeps the id cache read-only
    (a dry run must not write to the bus database). A cached `no remote` is
    an answer like any other: it is asked again only when it is old or when
    `refresh` asks for everything."""
    if not scan:
        entries = storage.repo_id_entries()
        out = []
        for path, groups in _roster_paths(storage):
            key = str(path)
            cached = entries.get(key)
            rid = cached[0] if cached else None
            if refresh or not _fresh_answer(cached, cfg["id_recheck_seconds"]):
                found = lookup_repo_id(path, cfg=cfg, run=run)
                if found.definite:
                    rid = found.repo_id
                    if persist:
                        storage.set_repo_id(key, rid)
            out.extend(LocalRepo(path, rid, group) for group in groups)
        return out
    out = []
    for root in cfg.get("roots") or []:
        for path in init_cmd.find_git_repos(Path(root), max_depth=cfg["scan_depth"]):
            out.append(LocalRepo(path, repo_id(path, cfg=cfg, run=run), resolution.group_for_repo(path)))
    return out


def _match(name: str, cands: list[LocalRepo], aliases) -> list[LocalRepo]:
    lowered = name.strip().lower()
    bare = lowered[: -len(".git")] if lowered.endswith(".git") else lowered
    wanted = normalize_remote(name, aliases)
    rules = (
        lambda c: c.alias == name,
        lambda c: c.repo_id is not None and c.repo_id in {wanted, bare},
        lambda c: c.repo_id is not None and c.repo_id.endswith("/" + bare),
    )
    for rule in rules:
        hits = [c for c in cands if rule(c)]
        if hits:
            return hits
    return []


def resolve(
    name: str, *, storage, cfg: Mapping[str, Any], run: Runner = run_command,
    persist: bool = True,
) -> LocalRepo:
    """The one local clone `name` (alias, id, URL or id suffix) refers to.
    With `persist=False` nothing is written to the id cache."""
    aliases = cfg.get("host_aliases")
    hits: list[LocalRepo] = []
    # cached ids first; then the roster re-asked (a remote may have been
    # added or changed since the cache was filled); then the scan roots
    for scan, refresh in ((False, False), (False, True), (True, False)):
        hits = _match(
            name,
            _candidates(storage, cfg, run, scan=scan, refresh=refresh, persist=persist),
            aliases,
        )
        if hits:
            break
    if hits:
        mains = [h for h in hits if not is_worktree(h.path)]
        hits = mains or hits
        unique: dict[Path, LocalRepo] = {}
        for h in hits:
            unique.setdefault(h.path.resolve(), h)
        hits = list(unique.values())
    if not hits:
        target = clone_target(name, cfg)
        url, dest = target if target else (None, None)
        raise RepoNotFoundError(name, url, dest)
    if len(hits) > 1:
        raise AmbiguousRepoError(name, hits)
    found = hits[0]
    latest = lookup_repo_id(found.path, cfg=cfg, run=run)
    if latest.definite and persist:
        storage.set_repo_id(str(found.path), latest.repo_id)
    # a failed lookup keeps the id we already had
    fresh = latest.repo_id if latest.definite else found.repo_id
    return LocalRepo(found.path, fresh, found.alias)

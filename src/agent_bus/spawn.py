"""Start an agent client in a tmux window, registered on the bus.

`spawn()` is an importable API; the `agent-bus spawn` command is a thin
layer over it. Every tunable (client command templates, cap, tmux names,
env to strip, carry-over list) comes from the loaded config.

Order matters: nothing is written to the bus until every check that can
refuse the spawn has passed and tmux is known to be present.
"""

from __future__ import annotations

import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import clients, commands, identity, paths, repo_identity, resolution, status, tmux
from . import storage as storage_mod
from .clients.base import ResumableSession, UnreadableConfigError
from .config import ConfigError
from .storage import SpawnRecord, Storage

EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_CAP_REACHED = 4

SECRET_NAME_RE = re.compile(r"[A-Z_][A-Z0-9_]*")
PROMPT_TOKEN = "{prompt}"
SESSION_ENV = resolution.SESSION_ENV
SPAWN_TOKEN_ENV = resolution.SPAWN_TOKEN_ENV
REPO_ENV = resolution.REPO_ENV
# A tmux window inherits the tmux server's environment, so the bus paths a
# test or a relocated install uses must travel inside the command.
PASSTHROUGH_ENV = ("AGENT_BUS_DB", "AGENT_BUS_AUDIT_LOG")


class SpawnError(Exception):
    def __init__(self, message: str, exit_code: int = EXIT_FAILED, hint: str | None = None) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.hint = hint
        # what spawn had noted before it failed; filled in by `spawn()`
        self.notes: list[str] = []


@dataclass(frozen=True)
class SpawnRequest:
    repo: str
    client: str
    name: str
    prompt_file: Path
    model: str | None = None
    worktree: str | None = None
    clone: bool = False
    remote: bool = True
    dry_run: bool = False
    role: str | None = None
    # (variable name, 0600 file holding its value) pairs, see render_command
    secret_env: tuple[tuple[str, Path], ...] = ()


@dataclass(frozen=True)
class SpawnResult:
    name: str
    client: str
    label: str
    repo_id: str | None
    repo_path: str
    work_dir: str
    tmux_session: str
    tmux_window: str
    command: str
    launched: bool
    notes: list[str] = field(default_factory=list)
    links: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ------------------------------ command ------------------------------------


def _fragment(template: Mapping, key: str, client: str | None) -> list[str] | None:
    value = template.get(key)
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(t, str) for t in value):
        raise ConfigError(
            f"spawn.clients.{client or '?'}.{key} must be a list of strings"
        )
    return value


PROMPT_DELIVERIES = ("argv", "paste")
DEFAULT_PROMPT_READY_SECONDS_KEY = "prompt_ready_seconds"


def uses_paste(template: Mapping, client: str | None = None) -> bool:
    """True when the client's first prompt is pasted into its window after
    launch (`prompt_delivery: "paste"`) instead of passed on its command line."""
    mode = template.get("prompt_delivery", "argv")
    if mode not in PROMPT_DELIVERIES:
        raise ConfigError(
            f"spawn.clients.{client or '?'}.prompt_delivery must be one of "
            f"{', '.join(PROMPT_DELIVERIES)}"
        )
    return mode == "paste"


def render_command(
    template: Mapping,
    *,
    values: Mapping[str, str],
    prompt_file: Path,
    model: str | None,
    remote: bool,
    strip_env: Sequence[str],
    set_env: Mapping[str, str],
    after_exit: str,
    client: str | None = None,
    user_wired: bool = False,
    command_key: str = "command",
    secret_env: Sequence[tuple[str, Path]] = (),
) -> str:
    """The shell line a tmux window runs. Every token is quoted except
    `{prompt}`, which reads the prompt file at run time so the prompt never
    travels inside the tmux command string. `user_wired` adds the client's
    `user_wired` fragment (session variables for a client wired per user)
    right after the command."""
    prefix, suffix = "", ""
    for name, secret_file in secret_env:
        if not SECRET_NAME_RE.fullmatch(name):
            raise ConfigError(f"secret environment name {name!r} must match [A-Z_][A-Z0-9_]*")
        quoted = shlex.quote(str(secret_file))
        # the value is read at run time and the file removed at once, so the
        # secret never enters the command string
        prefix += f'export {name}="$(cat {quoted})"; rm -f {quoted}; '
        suffix += f"unset {name}; "
    model_env = _model_env(template, values, model, client)
    fragments: list[list[str]] = []
    for key in (command_key, "user_wired", "model", "remote", "prompt"):
        if key == "user_wired" and not user_wired:
            continue
        if key == "model" and (model is None or (model_env and template.get("model") is None)):
            continue
        if key == "remote" and not remote:
            continue
        if key == "prompt" and command_key == "command" and uses_paste(template, client):
            continue  # pasted into the window after launch instead
        fragment = _fragment(template, key, client)
        if fragment is None:
            if key in ("remote", "user_wired"):
                continue
            raise ConfigError(
                f"spawn.clients.{client or '?'}.{key} must be a list of strings"
            )
        fragments.append(fragment)

    parts = ["env"]
    for var in strip_env:
        parts += ["-u", shlex.quote(var)]
    env_values = {**model_env, **set_env}
    parts += [f"{key}={shlex.quote(value)}" for key, value in env_values.items()]
    for token in (t for fragment in fragments for t in fragment):
        if token == PROMPT_TOKEN:
            parts.append(f'"$(cat {shlex.quote(str(prompt_file))})"')
            continue
        try:
            parts.append(shlex.quote(token.format_map(values)))
        except (KeyError, IndexError, ValueError, AttributeError) as exc:
            raise ConfigError(
                f"spawn.clients.{client or '?'}: cannot fill {token!r} "
                f"({type(exc).__name__}: {exc}); placeholders are "
                f"{', '.join('{' + k + '}' for k in values)}"
            ) from exc
    return prefix + " ".join(parts) + f"; {suffix}{after_exit}"


def _fill_leaves(node: Any, values: Mapping[str, str]) -> Any:
    if isinstance(node, str):
        return node.format_map(values)
    if isinstance(node, list):
        return [_fill_leaves(item, values) for item in node]
    if isinstance(node, dict):
        return {key: _fill_leaves(item, values) for key, item in node.items()}
    return node


def env_from_template(
    spec: Mapping, values: Mapping[str, str], *, where: str
) -> dict[str, str]:
    """Environment variables from a config template: strings are filled,
    objects and lists are filled leaf by leaf and sent as compact JSON."""
    out: dict[str, str] = {}
    for variable, value in spec.items():
        try:
            filled = _fill_leaves(value, values)
        except (KeyError, IndexError, ValueError, AttributeError) as exc:
            raise ConfigError(
                f"{where}: cannot fill {variable!r} "
                f"({type(exc).__name__}: {exc}); placeholders are "
                f"{', '.join('{' + k + '}' for k in values)}"
            ) from exc
        out[variable] = (
            filled if isinstance(filled, str) else json.dumps(filled, separators=(",", ":"))
        )
    return out


def _model_env(
    template: Mapping, values: Mapping[str, str], model: str | None, client: str | None
) -> dict[str, str]:
    """The variables that carry `--model` for a client without a model flag."""
    spec = template.get("model_env")
    if model is None or not spec:
        return {}
    if not isinstance(spec, dict):
        raise ConfigError(f"spawn.clients.{client or '?'}.model_env must be an object")
    return env_from_template(spec, values, where=f"spawn.clients.{client or '?'}.model_env")


def resume_command(
    template: Mapping, *, label: str, work_dir: str, client: str | None = None
) -> str | None:
    """The shell line that reopens a spawned session from its own folder, built
    from the client's `resume` template; None when the client has none. The
    folder matters: clients list and resolve sessions by working directory."""
    fragment = _fragment(template, "resume", client)
    if not fragment:
        return None
    values = {"label": label, "dir": work_dir}
    try:
        argv = [token.format_map(values) for token in fragment]
    except (KeyError, IndexError, ValueError, AttributeError) as exc:
        raise ConfigError(
            f"spawn.clients.{client or '?'}.resume: cannot fill the template "
            f"({type(exc).__name__}: {exc}); placeholders are {{label}}, {{dir}}"
        ) from exc
    return f"cd {shlex.quote(work_dir)} && {shlex.join(argv)}"


# ------------------------------- helpers -----------------------------------


def _git(argv: list[str], *, timeout: float, run: commands.Runner, what: str) -> None:
    try:
        done = run(argv, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SpawnError(f"{what} failed: {exc}") from exc
    if done.returncode != 0:
        detail = (done.stderr or "").strip() or f"exit {done.returncode}"
        raise SpawnError(f"{what} failed: {detail}")


def _resolve_clone(
    request: SpawnRequest, *, storage: Storage, cfg: Mapping, run: commands.Runner,
    notes: list[str],
) -> repo_identity.LocalRepo:
    repos_cfg = cfg["repos"]
    try:
        # a dry run must not write to the bus database, not even the id cache
        return repo_identity.resolve(
            request.repo, storage=storage, cfg=repos_cfg, run=run,
            persist=not request.dry_run,
        )
    except repo_identity.AmbiguousRepoError as exc:
        raise SpawnError(str(exc), EXIT_USAGE) from exc
    except repo_identity.RepoNotFoundError as exc:
        return _clone_missing(request, exc, storage=storage, cfg=cfg, run=run, notes=notes)


def _clone_missing(
    request: SpawnRequest, missing: repo_identity.RepoNotFoundError, *,
    storage: Storage, cfg: Mapping, run: commands.Runner, notes: list[str],
) -> repo_identity.LocalRepo:
    repos_cfg = cfg["repos"]
    url, dest = missing.clone_url, missing.clone_dest
    if not request.clone:
        hint = f"git clone -- {shlex.quote(url)} {shlex.quote(str(dest))}" if url and dest else None
        raise SpawnError(
            f"no local clone of {request.repo!r} found; pass --clone to clone it, "
            f"or add the folder that holds an existing clone to repos.roots in config.json",
            EXIT_FAILED, hint,
        ) from missing
    if url is None:
        raise SpawnError(
            f"cannot clone {request.repo!r}: pass a repo id host/owner/name or a URL "
            f"to --repo, whose last part is a plain folder name (it must start with "
            f"a letter, digit or underscore)", EXIT_FAILED,
        ) from missing
    if dest is None:
        raise SpawnError(
            "cannot clone: set repos.clone_root (or repos.roots) in config.json",
            EXIT_USAGE,
        ) from missing
    rid = repo_identity.normalize_remote(url, repos_cfg.get("host_aliases"))
    dest = _clone_destination(dest, rid, cfg=repos_cfg, run=run, notes=notes)
    found = repo_identity.LocalRepo(dest, rid, resolution.group_for_repo(dest))
    if dest.exists():
        _check_reusable_clone(dest, rid, cfg=repos_cfg, run=run)
        notes.append(f"reusing the existing clone {dest}")
    elif request.dry_run:
        notes.append(f"would clone {url} into {dest}")
        return found
    else:
        _clone_into(url, dest, cfg=repos_cfg, run=run)
    if not request.dry_run:
        _wire_fresh_clone(request.client, dest, storage=storage, notes=notes)
    return found


def _is_clone_of(path: Path, rid: str | None, *, cfg: Mapping, run: commands.Runner) -> bool:
    return rid is not None and repo_identity.repo_id(path, cfg=cfg, run=run) == rid


def _clone_destination(
    dest: Path, rid: str | None, *, cfg: Mapping, run: commands.Runner, notes: list[str]
) -> Path:
    """`dest`, or `<owner>-<name>` beside it when another repo with the same
    name already sits there. The fallback folder follows the same name rule
    as the first choice; when it is taken too, both paths are named."""
    if not dest.exists() or _is_clone_of(dest, rid, cfg=cfg, run=run):
        return dest
    owner_and_name = rid.split("/")[-2:] if rid else []
    folder = (
        repo_identity.folder_name("-".join(owner_and_name)) if len(owner_and_name) == 2 else None
    )
    if folder is None or dest.with_name(folder) == dest:
        raise SpawnError(f"cannot clone: {dest} already exists and is not a clone of {rid}")
    alt = dest.with_name(folder)
    if alt.exists() and not _is_clone_of(alt, rid, cfg=cfg, run=run):
        raise SpawnError(
            f"cannot clone: {dest} and {alt} both already exist and neither is a clone of {rid}"
        )
    notes.append(f"{dest} belongs to another repo; using {alt}")
    return alt


def _check_reusable_clone(
    dest: Path, rid: str | None, *, cfg: Mapping, run: commands.Runner
) -> None:
    """A folder left by an earlier, refused attempt is reused only when it is
    a complete clone of the wanted repo. git writes `origin` before it
    fetches, so a killed clone passes the remote test: HEAD must resolve."""
    existing = repo_identity.repo_id(dest, cfg=cfg, run=run)
    if rid is None or existing != rid:
        raise SpawnError(f"cannot clone: {dest} already exists and is not a clone of {rid}")
    try:
        head = run(
            ["git", "-C", str(dest), "rev-parse", "--verify", "HEAD"],
            timeout=cfg["git_timeout_seconds"],
        )
        complete = head.returncode == 0
    except (OSError, subprocess.SubprocessError):
        complete = False
    if not complete:
        raise SpawnError(
            f"cannot reuse {dest}: it is an incomplete clone (HEAD does not resolve); "
            f"remove that directory and run again"
        )


def _clone_into(url: str, dest: Path, *, cfg: Mapping, run: commands.Runner) -> None:
    """Clone into a temporary sibling and rename into place, so a failed or
    timed-out clone leaves nothing at `dest`. Only the temporary directory,
    which this call names, is ever removed."""
    temp = dest.with_name(f".{dest.name}.cloning-{uuid.uuid4().hex[:8]}")
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        _git(
            ["git", "clone", "--", url, str(temp)],
            timeout=cfg["clone_timeout_seconds"], run=run, what=f"git clone {url}",
        )
        if dest.exists():
            raise SpawnError(f"cannot clone: {dest} appeared while cloning")
        temp.rename(dest)
    except OSError as exc:
        raise SpawnError(f"cannot clone into {dest}: {exc}") from exc
    finally:
        shutil.rmtree(temp, ignore_errors=True)


def wire_repo(client: str, path: Path, *, storage: Storage, notes: list[str]) -> None:
    """Wire a folder for `client` so a session started there can join the bus."""
    _wire_fresh_clone(client, path, storage=storage, notes=notes)


def _wire_fresh_clone(
    client: str, dest: Path, *, storage: Storage, notes: list[str]
) -> None:
    """Wire a repo cloned just now, so the spawned session can join the bus."""
    if client not in clients.wirable_clients():
        return
    from . import init_cmd  # lazy: init_cmd is a heavier module than spawn needs

    for file in clients.wiring(client).files(dest):
        if not _parent_inside(dest, file):
            # a tracked symlink such as `.claude -> ../../.claude` would send the
            # write into a folder that is not part of this repo
            notes.append(
                f"not wiring {dest} for {client}: {file} would be written outside the "
                f"clone, through a symlink; review the link, then wire it by hand with "
                f"`agent-bus init {dest} --clients {client}` if you trust it"
            )
            return
    try:
        plan = init_cmd.plan_for_repo(dest, client_ids=[client])
        if not plan.is_change:
            return
        init_cmd.apply_plan(plan, bin_path=init_cmd.detect_agent_bus_bin(), storage=storage)
    except UnreadableConfigError as exc:
        notes.append(f"could not wire {dest} for {client}: {exc}")
        return
    notes.append(f"wired the new clone {dest} for {client}")


def _is_user_wired(client: str, clone: Path) -> bool:
    """Whether the client reaches the bus through the user's own wiring: it
    is wirable, the clone exists and has no wiring of its own."""
    return (
        client in clients.wirable_clients()
        and clone.exists()
        and clients.wiring(client).inspect(clone).name is None
        and clients.wired_for_user(client)
    )


def _address(
    request: SpawnRequest, clone: Path, *, storage: Storage, notes: list[str],
    template: Mapping | None = None,
) -> tuple[str, str]:
    """(group, client part) the session joins the bus under."""
    client = request.client
    wired_name = None
    if client in clients.wirable_clients() and clone.exists():
        state = clients.wiring(client).inspect(clone)
        wired_name = state.name
        if wired_name is None:
            notes.append(_unwired_note(client, clone, template))
    if wired_name is not None:
        parsed = identity.parse_or_none(wired_name) if isinstance(wired_name, str) else None
        if parsed is None or parsed.client is None:
            raise SpawnError(
                f"{clone} is wired for {client} as {wired_name!r}, which has no client "
                f"part; re-run `agent-bus init {clone}` for this repo", EXIT_FAILED,
            )
        return parsed.group, parsed.client
    parsed = identity.parse(resolution.client_address(storage, clone, client))
    return parsed.group, parsed.client or client


def _unwired_note(client: str, clone: Path, template: Mapping | None = None) -> str:
    """Why a clone with no wiring of its own may still join the bus."""
    if clients.wired_for_user(client):
        if (template or {}).get("user_wired") is not None:
            return (
                f"{client} is wired for your user account, not for {clone}: the spawn "
                f"passes the session variables through spawn.clients.{client}.user_wired"
            )
        return (
            f"{client} is wired for your user account, not for {clone}: it does not "
            f"pass AGENT_BUS_SESSION to MCP servers, so the session is named after "
            f"the folder instead of its label, and `agent-bus status` will not link "
            f"it to this spawn"
        )
    return (
        f"{clone} is not wired for {client}: the session will not appear on "
        f"the bus until `agent-bus init {clone} --clients {client}` is run"
    )


def _wiring_rels(request: SpawnRequest, clone: Path) -> list[Path]:
    """The client's wiring files, relative to the clone."""
    if request.client not in clients.wirable_clients():
        return []
    rels: list[Path] = []
    for file in clients.wiring(request.client).files(clone):
        try:
            rels.append(file.relative_to(clone))
        except ValueError:
            continue
    return rels


def _parent_inside(root: Path, path: Path) -> bool:
    """Whether the folder holding `path` stays under `root` once symlinks are
    followed. Only the parent is resolved: the entry itself may be a symlink
    (to a private tree, say), and the link made for it points at `path`, which
    exposes nothing `root` does not already expose."""
    return path.parent.resolve().is_relative_to(root.resolve())


def _carry_over_paths(
    request: SpawnRequest, clone: Path, cfg: Mapping
) -> list[Path]:
    """Wiring files plus `spawn.carry_over`, validated lexically (no absolute
    path, no `..` part) and by where their parent folder really lives."""
    entries = _wiring_rels(request, clone)
    for rel in entries:
        if not _parent_inside(clone, clone / rel):
            raise SpawnError(
                f"the {request.client} wiring file {str(rel)!r} sits in a folder that "
                f"resolves outside {clone}; it cannot be carried into a worktree"
            )
    for configured in cfg["spawn"]["carry_over"]:
        rel = Path(configured)
        if rel.is_absolute() or ".." in rel.parts or not _parent_inside(clone, clone / rel):
            raise ConfigError(
                f"spawn.carry_over entry {configured!r} must be a path inside the repository"
            )
        entries.append(rel)
    return entries


def _worktree_target(root: Path, group: str, label: str) -> Path:
    """`<root>/<group>/<label>`. The group can come from a repo's own wiring
    file, so it must already be a plain slug; names never become paths."""
    if group != identity.slugify(group):
        raise SpawnError(
            f"cannot build a worktree folder from the group {group!r}: it is not a plain "
            f"name (lowercase letters, digits and dashes); fix the repo's agent-bus wiring"
        )
    target = root / group / label
    if not target.resolve().is_relative_to(root.resolve()):
        raise SpawnError(f"the worktree folder {target} resolves outside {root}")
    return target


def _git_common_dir(path: Path, *, timeout: float, run: commands.Runner) -> Path | None:
    """The shared git directory of a checkout, absolute and resolved."""
    try:
        done = run(["git", "-C", str(path), "rev-parse", "--git-common-dir"], timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    out = (done.stdout or "").strip()
    if done.returncode != 0 or not out:
        return None
    return (path / out).resolve()


def _check_worktree_of(
    target: Path, clone: Path, *, timeout: float, run: commands.Runner
) -> None:
    """A folder at the worktree path is reused only when it is a worktree of
    this clone. The group is a per-machine alias, so two repos can share it."""
    wanted = _git_common_dir(clone, timeout=timeout, run=run)
    found = _git_common_dir(target, timeout=timeout, run=run)
    if wanted is None or found != wanted:
        raise SpawnError(
            f"cannot reuse {target}: it is not a git worktree of {clone}; "
            f"choose another --name or remove that folder"
        )


def _note_tracked_wiring(
    request: SpawnRequest, clone: Path, target: Path, notes: list[str],
    covered: Sequence[Path] = (),
) -> None:
    """A repo that tracks a wiring file keeps its committed copy in the
    worktree, and nothing is rewritten: say so, since the session then lacks
    the agent-bus hooks (or its server entry). Files the client's own
    worktree step compensates for (`covered`) are not worth a warning."""
    for rel in _wiring_rels(request, clone):
        if rel in covered:
            continue
        mine, theirs = target / rel, clone / rel
        if mine.is_symlink() or not mine.is_file() or not theirs.is_file():
            continue
        try:
            same = mine.read_bytes() == theirs.read_bytes()
        except OSError:
            same = False
        if not same:
            notes.append(
                f"{mine} is a different file from the clone's {rel}, because the repo "
                f"tracks it; the session will lack agent-bus hooks (no state reports, "
                f"no hook-delivered mail) unless that file carries the wiring"
            )


def _carry_over_link(source: Path, dest: Path, target: Path, notes: list[str]) -> None:
    if not source.exists() or dest.exists() or dest.is_symlink():
        return
    if not _parent_inside(target, dest):
        notes.append(f"not linking {dest}: its folder resolves outside the worktree")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(source, dest)


def _default_worktree_target(clone: Path, label: str) -> Path:
    """`<clone>/.worktrees/<label>`. Folders inside a trusted clone inherit
    its trust in most clients, which an unattended session cannot grant."""
    target = clone / paths.WORKTREES_DIR / label
    # a `.worktrees` link to elsewhere would put the worktree outside the
    # clone, where no trust is inherited
    if not target.resolve().is_relative_to(clone.resolve()):
        raise SpawnError(f"the worktree folder {target} resolves outside {clone}")
    return target


def _exclude_worktrees_dir(clone: Path, *, timeout: float, run: commands.Runner) -> None:
    """Keep git from listing `.worktrees/` as untracked, through the shared
    `info/exclude` of the repository: it is never tracked, so no tracked
    file changes. Idempotent."""
    common = _git_common_dir(clone, timeout=timeout, run=run)
    if common is None:
        raise SpawnError(f"cannot find the git directory of {clone}")
    _append_exclude(common, f"/{paths.WORKTREES_DIR}/")


def _append_exclude(common_dir: Path, pattern: str) -> None:
    """Add `pattern` as a line of `<common_dir>/info/exclude` unless it is there."""
    exclude = common_dir / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    text = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    if pattern in text.splitlines():
        return
    separator = "" if not text or text.endswith("\n") else "\n"
    exclude.write_text(f"{text}{separator}{pattern}\n", encoding="utf-8")


def _exclude_unless_ignored(
    clone: Path, target: Path, written: Sequence[Path], *, timeout: float, run: commands.Runner
) -> None:
    """Exclude files a worktree step created, but only those git would
    otherwise list (`git check-ignore`); a tracked `.gitignore` is never edited."""
    for rel in written:
        try:
            done = run(["git", "-C", str(target), "check-ignore", "-q", "--", str(rel)], timeout=timeout)
        except (OSError, subprocess.SubprocessError):
            continue
        common = _git_common_dir(clone, timeout=timeout, run=run)
        if done.returncode == 1 and common is not None:
            _append_exclude(common, f"/{rel.as_posix()}")


def _prepare_client_worktree(
    request: SpawnRequest, clone: Path, target: Path, *,
    cfg: Mapping, timeout: float, run: commands.Runner, notes: list[str],
) -> None:
    """The client's own worktree step (if any), then the note for wiring files
    the repo tracks that the step did not make up for."""
    covered: Sequence[Path] = ()
    if request.client in clients.wirable_clients():
        state = clients.wiring(request.client).inspect(clone)
        if state.name is not None:
            prep = clients.prepare_worktree(
                request.client, clone, target, name=state.name,
                bin_path=state.bin_path or _default_bin(), dry_run=request.dry_run,
                options=cfg["spawn"]["clients"][request.client],
            )
            notes.extend(prep.notes)
            covered = prep.covered
            _exclude_unless_ignored(clone, target, prep.written, timeout=timeout, run=run)
    _note_tracked_wiring(request, clone, target, notes, covered)


def _default_bin() -> str:
    from . import init_cmd  # lazy, as in _wire_fresh_clone

    return init_cmd.detect_agent_bus_bin()


def _work_dir(
    request: SpawnRequest, clone: Path, group: str, label: str, *,
    cfg: Mapping, run: commands.Runner, notes: list[str],
) -> Path:
    if request.worktree is None:
        return clone
    configured = cfg["spawn"]["worktree_root"]
    timeout = cfg["repos"]["git_timeout_seconds"]
    if configured:
        target = _worktree_target(Path(configured).expanduser(), group, label)
    else:
        target = _default_worktree_target(clone, label)
    entries = _carry_over_paths(request, clone, cfg)
    if target.exists():
        _check_worktree_of(target, clone, timeout=timeout, run=run)
        notes.append(f"reusing existing worktree {target}")
    elif request.dry_run:
        notes.append(f"would create worktree {target} at {request.worktree}")
    else:
        _create_worktree(request, clone, target, entries, default=not configured,
                         timeout=timeout, run=run, notes=notes)
    _prepare_client_worktree(
        request, clone, target, cfg=cfg, timeout=timeout, run=run, notes=notes,
    )
    return target


def _create_worktree(
    request: SpawnRequest, clone: Path, target: Path, entries: Sequence[Path], *,
    default: bool, timeout: float, run: commands.Runner, notes: list[str],
) -> None:
    if default:
        _exclude_worktrees_dir(clone, timeout=timeout, run=run)
    target.parent.mkdir(parents=True, exist_ok=True)
    _git(
        ["git", "-C", str(clone), "worktree", "add", "--detach", "--", str(target), request.worktree],
        timeout=timeout, run=run, what="git worktree add",
    )
    for rel in entries:
        _carry_over_link(clone / rel, target / rel, target, notes)


def path_key(path: Path) -> str:
    """Claude Code's per-folder key: the absolute path with every character
    that is not an ASCII letter or digit replaced by `-`."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(Path(path).resolve()))


def _link_values(clone: Path, work_dir: Path) -> dict[str, str]:
    return {
        "clone_key": path_key(clone), "work_dir_key": path_key(work_dir),
        "clone": str(clone), "work_dir": str(work_dir),
    }


def find_resumable(
    client: str, *, cfg: Mapping, clone: Path, work_dir: Path, label: str
) -> ResumableSession | None:
    """The client's session named `label` that ran in `work_dir`, found through
    the client's `transcripts` pattern; None when the client has none."""
    pattern = (cfg["spawn"]["clients"].get(client) or {}).get("transcripts")
    if not pattern:
        return None
    values = _link_values(clone, work_dir)
    try:
        filled = os.path.expanduser(pattern.format_map(values))
    except (KeyError, IndexError, ValueError, AttributeError) as exc:
        raise ConfigError(
            f"spawn.clients.{client}.transcripts: cannot fill {pattern!r} "
            f"({type(exc).__name__}: {exc}); placeholders are "
            f"{', '.join('{' + k + '}' for k in values)}"
        ) from exc
    return clients.find_session(client, [Path(p) for p in glob.glob(filled)], label=label)


def _render_link(client: str, entry: Mapping, values: Mapping[str, str]) -> tuple[Path, Path]:
    """(source, target) of one `worktree_links` entry; ConfigError when a
    placeholder cannot be filled."""
    try:
        source, target = (
            os.path.expanduser(entry[k].format_map(values)) for k in ("source", "target")
        )
    except (KeyError, IndexError, ValueError, AttributeError) as exc:
        raise ConfigError(
            f"spawn.clients.{client}.worktree_links: cannot fill {entry!r} "
            f"({type(exc).__name__}: {exc}); placeholders are "
            f"{', '.join('{' + k + '}' for k in values)}"
        ) from exc
    return Path(source), Path(target)


def _relative_link_text(source: Path, target: Path) -> str:
    """What a link at `target` stores to reach `source`: a relative path, so the
    link survives when the home folder is synced to a machine whose home path
    differs (`/Users/me` and `/home/me`). It is computed between the physical
    folders, because the system resolves a relative link from the folder the
    link really sits in, which a symlinked parent can move."""
    real_source = Path(os.path.realpath(source.parent)) / source.name
    return os.path.relpath(real_source, os.path.realpath(target.parent))


def _link_outside_state(
    request: SpawnRequest, clone: Path, work_dir: Path, *, cfg: Mapping, notes: list[str]
) -> list[dict[str, Any]]:
    """Link client state kept outside the repo (per `worktree_links`) into a
    worktree. Never deletes or overwrites anything."""
    if request.worktree is None or work_dir == clone:
        return []
    entries = cfg["spawn"]["clients"][request.client].get("worktree_links") or []
    values = _link_values(clone, work_dir)
    links: list[dict[str, Any]] = []
    for entry in entries:
        source, target = _render_link(request.client, entry, values)
        created = False
        if not source.exists():
            notes.append(f"not linking {target}: {source} does not exist")
        elif target.exists() or target.is_symlink():
            notes.append(f"not linking {target}: it already exists")
        elif request.dry_run:
            notes.append(f"would link {target} -> {source}")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(_relative_link_text(source, target), target)
            created = True
            notes.append(f"linked {target} -> {source}")
        links.append({"source": str(source), "target": str(target), "created": created})
    return links


# ------------------------------ validation ---------------------------------


@dataclass(frozen=True)
class _Checked:
    template: Mapping
    label: str
    prompt_file: Path


def _validate_request(request: SpawnRequest, spawn_cfg: Mapping) -> _Checked:
    """Everything about the request that needs neither the bus nor git."""
    templates = spawn_cfg["clients"]
    if request.client not in templates:
        raise SpawnError(
            f"unknown client {request.client!r}; configured clients: "
            f"{', '.join(sorted(templates))}", EXIT_USAGE,
        )
    if not identity.is_valid_client(request.client):
        raise SpawnError(
            f"spawn.clients.{request.client} is not usable: a client id is lowercase "
            f"letters and digits, starting with a letter", EXIT_USAGE,
        )
    template = templates[request.client]
    if request.name.startswith("-"):
        # `{name}` is rendered raw into the client's command line
        raise SpawnError(f"--name {request.name!r} must not start with '-'", EXIT_USAGE)
    try:
        label = identity.normalize_label(request.name)
    except identity.InvalidNameError as exc:
        raise SpawnError(str(exc), EXIT_USAGE) from exc
    prompt_file = Path(request.prompt_file).expanduser()
    if not prompt_file.is_file():
        raise SpawnError(f"prompt file {request.prompt_file} is not a file", EXIT_USAGE)
    _check_prompt_start(prompt_file)
    if request.model is not None and not template.get("model") and not template.get("model_env"):
        raise SpawnError(
            f"the {request.client} template has neither a model fragment nor model_env, "
            f"so --model cannot be used; set spawn.clients.{request.client}.model or "
            f"spawn.clients.{request.client}.model_env in config.json",
            EXIT_USAGE,
        )
    if request.model is not None and request.model.startswith("-"):
        # `{model}` is rendered raw into the client's command line
        raise SpawnError(f"--model {request.model!r} must not start with '-'", EXIT_USAGE)
    if request.worktree is not None and request.worktree.startswith("-"):
        raise SpawnError(
            f"--worktree base ref {request.worktree!r} must not start with '-'", EXIT_USAGE
        )
    return _Checked(template, label, prompt_file.resolve())


def _check_prompt_start(prompt_file: Path) -> None:
    """The prompt reaches the client as one bare argument, and a client's
    option parser may take a leading `-` for an option (the client's own
    `--` handling is not verified), so such a prompt is refused."""
    try:
        with prompt_file.open("rb") as handle:
            first = handle.read(1)
    except OSError as exc:
        raise SpawnError(f"cannot read the prompt file {prompt_file}: {exc}", EXIT_USAGE) from exc
    if not first:
        raise SpawnError(f"the prompt file {prompt_file} is empty", EXIT_USAGE)
    if first == b"-":
        raise SpawnError(
            f"the prompt file {prompt_file} starts with '-', which the client could "
            f"read as an option; start the prompt with text (a leading space or "
            f"blank line also works)", EXIT_USAGE,
        )


def _command_values(
    request: SpawnRequest, checked: _Checked, *, address: str, repo: str, work_dir: str
) -> dict[str, str]:
    values = {
        "name": request.name, "label": checked.label, "address": address,
        "repo": repo, "dir": work_dir, "prompt_file": str(checked.prompt_file),
    }
    if request.model is not None:
        values["model"] = request.model
    return values


def _session_env(
    label: str, clone: Path, work_dir: Path, env: Mapping[str, str], token: str
) -> dict[str, str]:
    """The variables the launched client gets. The token is the spawn
    record's stamp: the session's MCP server presents it to prove it is the
    session spawn started, however late it joins."""
    set_env = {SESSION_ENV: label, SPAWN_TOKEN_ENV: token}
    if work_dir != clone:
        # hooks find their session by repo: point them at the clone, which is
        # the path the session is registered under, not at the worktree
        set_env[REPO_ENV] = str(clone)
    set_env.update({k: env[k] for k in PASSTHROUGH_ENV if env.get(k)})
    return set_env


def _render(
    request: SpawnRequest, checked: _Checked, spawn_cfg: Mapping, *,
    values: Mapping[str, str], set_env: Mapping[str, str], user_wired: bool = False,
) -> str:
    secret_names = {name for name, _ in request.secret_env}
    strip_env = list(spawn_cfg["strip_env"])
    if resolution.AUTHORITY_TOKEN_ENV not in secret_names:
        strip_env.append(resolution.AUTHORITY_TOKEN_ENV)
    return render_command(
        checked.template, values=values, prompt_file=checked.prompt_file,
        model=request.model, remote=request.remote, strip_env=strip_env,
        set_env=set_env, after_exit=spawn_cfg["after_exit"], client=request.client,
        user_wired=user_wired, secret_env=request.secret_env,
    )


def _check_templates(
    request: SpawnRequest, checked: _Checked, spawn_cfg: Mapping, env: Mapping[str, str]
) -> None:
    """Render the command and the link entries with provisional values, so a
    template typo is reported before any clone, worktree or link exists."""
    provisional = Path("<clone>")
    values = _command_values(
        request, checked, address="<address>", repo="<repo>", work_dir="<dir>"
    )
    _render(
        request, checked, spawn_cfg, values=values,
        set_env=_session_env(checked.label, provisional, provisional, env, "<token>"),
        user_wired=True,
    )
    link_values = _link_values(provisional, Path("<work_dir>"))
    for entry in checked.template.get("worktree_links") or []:
        _render_link(request.client, entry, link_values)


def _check_capacity(
    storage: Storage, spawn_cfg: Mapping, now: datetime | None, *, exempt: bool = False
) -> list[status.StatusRow]:
    """The status rows, after refusing when the spawn cap is reached. Role
    sessions (the dispatcher) never count, and `exempt` skips the check."""
    rows = status.collect(storage, grace_seconds=spawn_cfg["startup_grace_seconds"], now=now)
    active = [
        r for r in rows if r.spawned and r.role is None and r.state in status.ACTIVE_STATES
    ]
    if not exempt and len(active) >= spawn_cfg["max_sessions"]:
        raise SpawnError(
            f"{len(active)} spawned sessions are active, the cap is "
            f"{spawn_cfg['max_sessions']} (spawn.max_sessions in config.json): "
            f"{', '.join(r.name for r in active)}", EXIT_CAP_REACHED,
        )
    return rows


def _compose_address(
    request: SpawnRequest, clone: Path, label: str, *, storage: Storage, notes: list[str],
    template: Mapping | None = None,
) -> str:
    try:
        group, client_part = _address(
            request, clone, storage=storage, notes=notes, template=template
        )
        return identity.compose(group, client_part, label)
    except identity.InvalidNameError as exc:
        raise SpawnError(f"cannot address the session: {exc}", EXIT_FAILED) from exc


def _prepare_work_dir(
    request: SpawnRequest, clone: Path, group: str, label: str, *,
    cfg: Mapping, run: commands.Runner, notes: list[str],
) -> tuple[Path, list[dict[str, Any]]]:
    try:
        work_dir = _work_dir(request, clone, group, label, cfg=cfg, run=run, notes=notes)
        links = _link_outside_state(request, clone, work_dir, cfg=cfg, notes=notes)
    except OSError as exc:
        raise SpawnError(f"cannot prepare the working directory: {exc}") from exc
    return work_dir, links


# ------------------------------ registration --------------------------------


_CALLER_SESSION_ENV = frozenset(
    {
        resolution.SESSION_ENV, resolution.INSTANCE_ENV, resolution.SPAWN_TOKEN_ENV,
        resolution.AUTHORITY_TOKEN_ENV,
    }
)


def _register_and_launch(
    request: SpawnRequest, result: SpawnResult, *, storage: Storage, spawn_cfg: Mapping,
    run: commands.Runner, env: Mapping[str, str], checked: _Checked, stamp: str,
    sleep: Callable[[float], None] = time.sleep, paste: bool = False,
) -> SpawnResult:
    """Put the session on the roster, then open its window. When tmux refuses,
    exactly what this call wrote is undone, restoring any earlier row. With
    `paste`, the prompt file is pasted into the window once the client had
    `prompt_ready_seconds` to start."""
    address = result.name
    # registering a session also registers its client address
    client_address = identity.parse(address).client_address
    before_agent, before_spawn = storage.get_agent(address), storage.get_spawn(address)
    client_existed = client_address is None or storage.get_agent(client_address) is not None
    storage.ensure_agent(address, result.repo_path)
    storage.set_state(address, storage_mod.STATE_STARTING)
    storage.record_spawn(SpawnRecord(
        name=address, client=request.client, repo_id=result.repo_id,
        repo_path=result.repo_path, work_dir=result.work_dir, model=request.model,
        prompt_file=str(checked.prompt_file),
        tmux_session=result.tmux_session, tmux_window=result.tmux_window,
        remote=request.remote and checked.template.get("remote") is not None, spawned_at=stamp,
        role=request.role,
    ))
    # The caller's own bus identity must not reach a tmux server this call may
    # create: its login shells would inherit it. The command sets these itself.
    strip = set(spawn_cfg["strip_env"]) | _CALLER_SESSION_ENV
    window_env = {k: v for k, v in env.items() if k not in strip}
    try:
        window_id = tmux.launch(
            result.tmux_session, result.tmux_window, Path(result.work_dir), result.command,
            env=window_env,
            tmux_bin=spawn_cfg["tmux_bin"], timeout=spawn_cfg["tmux_timeout_seconds"], run=run,
        )
    except tmux.TmuxError as exc:
        _roll_back(storage, address, before_agent, before_spawn)
        if not client_existed:
            storage.forget_agent(client_address)
        raise SpawnError(str(exc), EXIT_FAILED) from exc
    if paste:
        _paste_prompt(
            result, checked, spawn_cfg, window_id, sleep=sleep, run=run, env=window_env)
    return SpawnResult(**{**asdict(result), "launched": True})


def _paste_prompt(
    result: SpawnResult, checked: _Checked, spawn_cfg: Mapping, window_id: str, *,
    sleep: Callable[[float], None], run: commands.Runner, env: Mapping[str, str],
) -> None:
    """Deliver the first prompt after launch. The session is already running,
    so a failure is a note on the result, not a refusal."""
    wait = checked.template.get(DEFAULT_PROMPT_READY_SECONDS_KEY)
    sleep(wait if isinstance(wait, (int, float)) and wait >= 0 else 0)
    command = checked.template.get("command")
    try:
        tmux.paste_file(
            window_id, checked.prompt_file,
            client=command[0] if isinstance(command, list) and command else "",
            tmux_bin=spawn_cfg["tmux_bin"], timeout=spawn_cfg["tmux_timeout_seconds"],
            run=run, env=env,
        )
    except tmux.TmuxError as exc:
        result.notes.append(
            f"the window is open but its first prompt was not delivered ({exc}); "
            f"paste {checked.prompt_file} into it by hand"
        )


def _roll_back(storage: Storage, address: str, agent, record: SpawnRecord | None) -> None:
    storage.delete_spawn(address)
    if agent is None:
        storage.forget_agent(address)
    else:
        storage.restore_agent_state(address, agent.state, agent.state_at, agent.last_seen)
    if record is not None:
        storage.record_spawn(record)


# -------------------------------- spawn ------------------------------------


def spawn(
    request: SpawnRequest,
    *,
    storage: Storage,
    cfg: Mapping,
    run: commands.Runner = commands.run_command,
    env: Mapping[str, str] | None = None,
    now: datetime | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> SpawnResult:
    """Start the session, or raise SpawnError / ConfigError (never anything
    else for an expected failure). The error carries the notes gathered so far."""
    notes: list[str] = []
    try:
        return _spawn(
            request, storage=storage, cfg=cfg, run=run,
            env=os.environ if env is None else env, now=now, notes=notes, sleep=sleep,
        )
    except (SpawnError, ConfigError) as exc:
        exc.notes = notes  # type: ignore[attr-defined]
        raise


def _spawn(
    request: SpawnRequest, *, storage: Storage, cfg: Mapping, run: commands.Runner,
    env: Mapping[str, str], now: datetime | None, notes: list[str],
    sleep: Callable[[float], None] = time.sleep,
) -> SpawnResult:
    spawn_cfg = cfg["spawn"]
    checked = _validate_request(request, spawn_cfg)
    _check_templates(request, checked, spawn_cfg, env)
    # Refusals that need no clone come first, so a refused spawn never leaves
    # a fresh clone behind.
    rows = _check_capacity(storage, spawn_cfg, now, exempt=request.role is not None)

    found = _resolve_clone(request, storage=storage, cfg=cfg, run=run, notes=notes)
    clone = found.path
    if resolution.is_opted_out(clone):
        raise SpawnError(
            f"{clone} opted out of the bus with {resolution.IGNORE_FILE}; remove that "
            f"file to spawn a session there"
        )
    address = _compose_address(
        request, clone, checked.label, storage=storage, notes=notes,
        template=checked.template,
    )
    # a live session holds its address whatever state it reported (a finished
    # session stays open in its window)
    if any(r.name == address and (r.live or r.state in status.ACTIVE_STATES) for r in rows):
        raise SpawnError(f"{address} is already running", EXIT_FAILED)

    stamp = (now or datetime.now(timezone.utc)).strftime(storage_mod.TIMESTAMP_FORMAT)
    work_dir, links = _prepare_work_dir(
        request, clone, identity.parse(address).group, checked.label,
        cfg=cfg, run=run, notes=notes,
    )
    values = _command_values(
        request, checked, address=address, repo=found.alias, work_dir=str(work_dir)
    )
    result = SpawnResult(
        name=address, client=request.client, label=checked.label, repo_id=found.repo_id,
        repo_path=str(clone), work_dir=str(work_dir), tmux_session=spawn_cfg["tmux_session"],
        tmux_window=checked.label, launched=False, notes=notes, links=links,
        command=_render(
            request, checked, spawn_cfg, values=values,
            set_env=_session_env(checked.label, clone, work_dir, env, stamp),
            user_wired=_is_user_wired(request.client, clone),
        ),
    )
    if request.dry_run:
        return result
    tmux_bin = spawn_cfg["tmux_bin"]
    if not tmux.available(tmux_bin):
        raise SpawnError(
            f"tmux is required to spawn a session but {tmux_bin!r} was not found "
            f"(spawn.tmux_bin in config.json); run the command by hand instead",
            EXIT_FAILED, hint=f"cd {shlex.quote(str(work_dir))} && {result.command}",
        )
    return _register_and_launch(
        request, result, storage=storage, spawn_cfg=spawn_cfg, run=run, env=env,
        checked=checked, stamp=stamp, sleep=sleep,
        paste=uses_paste(checked.template, request.client),
    )


# -------------------------------- resume -----------------------------------


@dataclass(frozen=True)
class ResumeRequest:
    """`address` is the session's current bus address (it may have been
    renamed); `label` is the dispatch label: the tmux window name and the
    name the client knows the session by."""

    address: str
    client: str
    label: str
    work_dir: Path
    repo_path: Path
    prompt_file: Path
    session_id: str | None = None
    model: str | None = None
    remote: bool = True
    dry_run: bool = False


def resume(
    request: ResumeRequest,
    *,
    storage: Storage,
    cfg: Mapping,
    run: commands.Runner = commands.run_command,
    env: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> SpawnResult:
    """Reopen an ended session in a new tmux window with a follow-up prompt, or
    raise SpawnError / ConfigError (never anything else for an expected failure)."""
    spawn_cfg = cfg["spawn"]
    env = os.environ if env is None else env
    checked, handle = _validate_resume(request, spawn_cfg)
    work_dir, repo_path = Path(request.work_dir), Path(request.repo_path)
    rows = _check_capacity(storage, spawn_cfg, now)
    if any(r.name == request.address and (r.live or r.state in status.ACTIVE_STATES) for r in rows):
        raise SpawnError(f"{request.address} is already running", EXIT_FAILED)
    stamp = (now or datetime.now(timezone.utc)).strftime(storage_mod.TIMESTAMP_FORMAT)
    values = {
        "name": checked.label, "label": checked.label, "address": request.address,
        "repo": identity.parse(request.address).group, "dir": str(work_dir),
        "prompt_file": str(checked.prompt_file),
    }
    if request.session_id is not None:
        values["session_id"] = request.session_id
    if request.model is not None:
        values["model"] = request.model
    command = render_command(
        checked.template, values=values, prompt_file=checked.prompt_file,
        model=request.model, remote=request.remote,
        strip_env=[*spawn_cfg["strip_env"], resolution.AUTHORITY_TOKEN_ENV],
        set_env=_session_env(handle, repo_path, work_dir, env, stamp),
        after_exit=spawn_cfg["after_exit"], client=request.client, command_key="resume_prompt",
    )
    existing = storage.get_spawn(request.address)
    result = SpawnResult(
        name=request.address, client=request.client, label=checked.label,
        repo_id=existing.repo_id if existing else None, repo_path=str(repo_path),
        work_dir=str(work_dir), tmux_session=spawn_cfg["tmux_session"],
        tmux_window=checked.label, command=command, launched=False,
    )
    if request.dry_run:
        return result
    tmux_bin = spawn_cfg["tmux_bin"]
    if not tmux.available(tmux_bin):
        raise SpawnError(
            f"tmux is required to resume a session but {tmux_bin!r} was not found "
            f"(spawn.tmux_bin in config.json); run the command by hand instead",
            EXIT_FAILED, hint=f"cd {shlex.quote(str(work_dir))} && {command}",
        )
    spawn_request = SpawnRequest(
        repo=str(repo_path), client=request.client, name=checked.label,
        prompt_file=checked.prompt_file, model=request.model, worktree=None,
        remote=request.remote,
    )
    return _register_and_launch(
        spawn_request, result, storage=storage, spawn_cfg=spawn_cfg, run=run, env=env,
        checked=checked, stamp=stamp,
    )


def _validate_resume(request: ResumeRequest, spawn_cfg: Mapping) -> tuple[_Checked, str]:
    """(the checked template and prompt, the session handle of the address)."""
    templates = spawn_cfg["clients"]
    if request.client not in templates:
        raise SpawnError(
            f"unknown client {request.client!r}; configured clients: "
            f"{', '.join(sorted(templates))}", EXIT_USAGE,
        )
    template = templates[request.client]
    if not template.get("resume_prompt"):
        raise SpawnError(
            f"the {request.client} template has no resume_prompt, so its sessions "
            f"cannot be resumed with a prompt", EXIT_USAGE,
        )
    try:
        label = identity.normalize_label(request.label)
        handle = identity.parse(request.address).handle
        handle = identity.normalize_label(handle) if handle else None
    except identity.InvalidNameError as exc:
        raise SpawnError(str(exc), EXIT_USAGE) from exc
    if handle is None:
        raise SpawnError(f"{request.address} is not a session address", EXIT_USAGE)
    prompt_file = Path(request.prompt_file).expanduser()
    if not prompt_file.is_file():
        raise SpawnError(f"prompt file {request.prompt_file} is not a file", EXIT_USAGE)
    _check_prompt_start(prompt_file)
    if request.model is not None and request.model.startswith("-"):
        raise SpawnError(f"--model {request.model!r} must not start with '-'", EXIT_USAGE)
    if not Path(request.work_dir).is_dir():
        raise SpawnError(
            f"cannot resume {request.address}: its working folder {request.work_dir} is gone"
        )
    return _Checked(template, label, prompt_file.resolve()), handle

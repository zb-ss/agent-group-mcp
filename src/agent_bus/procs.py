"""Which processes this one descends from, and whether one has ended.

A session's MCP server and its hooks are both started by the same client
process. The server records its ancestry when it claims a session; a hook
later finds that session through its own client process.

A pid alone does not identify a process — the kernel reuses them — so each
one is recorded with its start time, and a process has *ended* when its
pid is gone, carries a different start time, or is a zombie. Linux answers
from /proc; elsewhere (macOS) one `ps` call lists every process.

Not knowing is never read as "ended": when the process table cannot be
read, or a process belongs to another machine boot or another PID
namespace, nothing is declared gone. Callers treat such sessions as still
running, so the worst outcome of a failed lookup is a number left unused,
never an address handed to a second session.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import settings

PROC_ROOT = Path("/proc")
# a loop guard, not a tunable: real process trees are a dozen levels deep
MAX_DEPTH = 64
# Programs that sit between a client and the commands it runs without being
# the client: a hook command runs under `sh -c`, and an MCP server may be
# started through a shell. A protocol fact about how commands are spawned,
# not a setting.
SHELLS = frozenset({"sh", "bash", "dash", "zsh", "fish", "ksh", "mksh", "busybox"})
# `ps` output must not depend on the caller's locale or time zone: start
# times are compared across processes, and a client launched from the GUI
# may not share the terminal's TZ.
_PS_LOCALE = {"LC_ALL": "C", "TZ": "UTC0"}

Entry = tuple[int, str, str, str]  # (parent pid, start time, state, command name)
Lookup = Callable[[int], "Entry | None"]


@dataclass(frozen=True)
class Proc:
    pid: int
    started: str
    # what the process calls itself; informative, not part of its identity
    name: str = field(default="", compare=False)

    @property
    def is_shell(self) -> bool:
        return os.path.basename(self.name).lstrip("-") in SHELLS

    def to_json(self) -> list:
        return [self.pid, self.started, self.name]

    @classmethod
    def from_json(cls, raw: object) -> "Proc | None":
        if not isinstance(raw, list) or len(raw) not in (2, 3):
            return None
        pid, started, *rest = raw
        name = rest[0] if rest else ""
        if isinstance(pid, int) and isinstance(started, str) and isinstance(name, str):
            return cls(pid, started, name)
        return None


@dataclass(frozen=True)
class Scope:
    """Which processes a pid can name: those of one machine boot, in one
    PID namespace. Either part is None where the platform cannot say."""

    boot_id: str | None
    pid_ns: str | None


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _read_proc_stat(pid: int) -> Entry | None:
    """(parent pid, start time, state, command name) from /proc/<pid>/stat."""
    raw = _read_text(PROC_ROOT / str(pid) / "stat")
    if raw is None:
        return None
    # the command name is in parentheses and may itself contain spaces and
    # parentheses, so the fixed fields start after the LAST closing one
    close = raw.rfind(")")
    name = raw[raw.find("(") + 1:close]
    fields = raw[close + 2:].split()
    try:
        # fields 3 (state), 4 (ppid) and 22 (starttime)
        return int(fields[1]), fields[19], fields[0], name
    except (IndexError, ValueError):
        return None


def _ps_table() -> dict[int, Entry] | None:
    """pid → entry for every process, from one `ps` call; None when `ps`
    cannot be run or says nothing."""
    try:
        out = subprocess.run(
            ["ps", "-A", "-o", "pid=,ppid=,state=,lstart=,comm="],
            capture_output=True, text=True, check=True,
            env={**os.environ, **_PS_LOCALE},
            stdin=subprocess.DEVNULL,
            timeout=settings.process_lookup_timeout_seconds(),
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    table: dict[int, Entry] = {}
    for line in out.splitlines():
        parts = line.split()
        # pid ppid state, a five-word start time in the C locale, the name
        if len(parts) >= 8 and parts[0].isdigit() and parts[1].isdigit():
            table[int(parts[0])] = (
                int(parts[1]), " ".join(parts[3:8]), parts[2], " ".join(parts[8:]),
            )
    return table or None


def _uses_proc() -> bool:
    return (PROC_ROOT / "self" / "stat").exists()


def _lookup() -> Lookup | None:
    """A pid → entry function for this platform, or None when the process
    table cannot be read at all."""
    if _uses_proc():
        return _read_proc_stat
    table = _ps_table()
    return table.get if table is not None else None


def describe(pid: int) -> Proc | None:
    """The running process `pid`, with its start time and name."""
    lookup = _lookup()
    entry = lookup(pid) if lookup else None
    return Proc(pid, entry[1], entry[3]) if entry else None


def ancestry(pid: int | None = None) -> list[Proc]:
    """The parent of `pid` (default: this process), then its parent, and
    so on — nearest first, stopping before the init process. Empty when
    the process table cannot be read."""
    lookup = _lookup()
    if lookup is None:
        return []
    current = os.getpid() if pid is None else pid
    chain: list[Proc] = []
    for _ in range(MAX_DEPTH):
        entry = lookup(current)
        if entry is None:
            break
        parent = entry[0]
        if parent <= 1:
            break
        parent_entry = lookup(parent)
        if parent_entry is None:
            break
        chain.append(Proc(parent, parent_entry[1], parent_entry[3]))
        current = parent
    return chain


def client_of(chain: list[Proc]) -> Proc | None:
    """The process that started a command: its nearest ancestor that is
    not a shell standing in between."""
    return next((p for p in chain if not p.is_shell), None)


def gone(candidates: list[Proc]) -> set[Proc]:
    """Those of `candidates` known to have ended: pid missing, pid reused
    (a different start time), or a zombie. Empty when the process table
    cannot be read — not knowing is not the same as gone."""
    if not candidates:
        return set()
    lookup = _lookup()
    if lookup is None:
        return set()
    ended: set[Proc] = set()
    for proc in candidates:
        entry = lookup(proc.pid)
        if entry is None or entry[1] != proc.started or entry[2].startswith(("Z", "X")):
            ended.add(proc)
    return ended


def scope() -> Scope:
    """This process's boot and PID namespace, as far as the platform says.
    macOS has neither; its `ps` start times are wall-clock dates, which a
    reboot cannot repeat."""
    boot = _read_text(PROC_ROOT / "sys" / "kernel" / "random" / "boot_id")
    try:
        ns = os.readlink(PROC_ROOT / "self" / "ns" / "pid")
    except OSError:
        ns = None
    return Scope(boot.strip() if boot else None, ns)

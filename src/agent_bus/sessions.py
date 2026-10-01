"""Session addresses: one per running session of a client in a repo.

Every session of a client in a repo reads the same config file, so without
help they would all answer to `<repo>/<client>`, drain one inbox between
them and have no way to address each other. Instead each MCP server claims
a session address when it starts, `<repo>/<client>-<handle>`:

  * the handle is the lowest free number (`-1`, `-2`, …), or one pinned in
    advance (`AGENT_BUS_SESSION=frontend`, `AGENT_BUS_INSTANCE=2`, or a handle
    already in `AGENT_BUS_NAME`), or a label the session picks later with
    `rename`;
  * `<repo>/<client>` stays on the roster as the address the sessions
    share: a message sent there is taken by whichever of them reads first.

A `sessions` row records which server holds which address, with the
server's process ancestry. The *client process* of a session is the
nearest of those ancestors that is not a shell: the client that started
the server. A hook belongs to the session whose client process is its own
client process; failing that, to the session with the same client session
id; failing that, to none — and then it drains only the shared client
address, so it never takes another session's own mail.

Two servers a hook could not tell apart — one client process, no session
ids to separate them, as one Codex app server does for all its threads —
share one address, which is how every session of a client behaved before
session addresses existed.

A session ends when its server process does: on a clean exit (`release`),
or when the next claim finds the process gone. A numbered address is then
given up at once — its unread mail moves to the client address and its
roster row goes — and a labelled one is kept, mail and all, for whoever
takes that label next. Either way the ended row stays behind for a while
(`settings.session_resume_hours`) so that the same client process or the
same session id coming back gets its old address again.

Whether a process is gone is only ever decided on evidence (`procs.gone`):
a session whose process cannot be looked up — the table unreadable, or a
process from another PID namespace — counts as still running.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import audit, identity, procs, settings
from .procs import Proc
from .storage import Storage, register_in

TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
SESSION_COLUMNS = (
    "server_pid, server_started, name, client_address, repo_path, lineage, "
    "session_key, boot_id, pid_ns, started_at, last_seen, ended_at"
)

Key = tuple[int, str, str]  # (server pid, server start time, client address)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime(TIMESTAMP_FORMAT)


class SessionError(ValueError):
    """A session operation that cannot be carried out as asked."""


@dataclass(frozen=True)
class SessionRow:
    server: Proc
    name: str
    client_address: str
    repo_path: str
    lineage: tuple[Proc, ...]
    session_key: str | None
    boot_id: str | None
    pid_ns: str | None
    started_at: str
    last_seen: str
    ended_at: str | None

    @property
    def key(self) -> Key:
        return (self.server.pid, self.server.started, self.client_address)

    @property
    def client_process(self) -> Proc | None:
        """The client that started this session's server."""
        return procs.client_of(list(self.lineage))

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "SessionRow":
        try:
            raw_lineage = json.loads(row["lineage"])
        except (TypeError, ValueError):
            raw_lineage = []
        if not isinstance(raw_lineage, list):
            raw_lineage = []
        lineage = tuple(p for p in (Proc.from_json(item) for item in raw_lineage) if p)
        return cls(
            server=Proc(row["server_pid"], row["server_started"]),
            name=row["name"],
            client_address=row["client_address"],
            repo_path=row["repo_path"],
            lineage=lineage,
            session_key=row["session_key"],
            boot_id=row["boot_id"],
            pid_ns=row["pid_ns"],
            started_at=row["started_at"],
            last_seen=row["last_seen"],
            ended_at=row["ended_at"],
        )


@dataclass(frozen=True)
class Claim:
    """The address a server ended up with, and anything worth telling it."""

    name: str
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _Choice:
    name: str
    # the ended session this one carries on, whose row it replaces
    predecessor: SessionRow | None = None
    # whether mail an ended holder left at `name` stays there for this one
    inherits: bool = True


def _known_ended(rows: list[SessionRow]) -> set[Key]:
    """Keys of the rows whose session is over: marked ended, from an
    earlier boot of this machine, or whose server process is gone. A row
    from another PID namespace cannot be judged and counts as running."""
    here = procs.scope()
    ended: set[Key] = set()
    to_check: list[SessionRow] = []
    for row in rows:
        if row.ended_at is not None:
            ended.add(row.key)
        elif row.boot_id and here.boot_id and row.boot_id != here.boot_id:
            ended.add(row.key)  # the machine has restarted since
        elif row.pid_ns and here.pid_ns and row.pid_ns != here.pid_ns:
            continue
        else:
            to_check.append(row)
    gone = procs.gone([r.server for r in to_check])
    ended.update(r.key for r in to_check if r.server in gone)
    return ended


def _could_be_the_same_session(
    row: SessionRow, client: Proc | None, key: str | None
) -> bool:
    """True when a hook could not tell `row` from a server started by
    `client` with session id `key`: same client process, no session ids
    that differ."""
    if client is None or row.client_process != client:
        return False
    return key is None or row.session_key is None or row.session_key == key


class SessionRegistry:
    def __init__(self, storage: Storage) -> None:
        self.storage = storage

    # ----------------------------- queries -----------------------------------

    def _rows(self, conn: sqlite3.Connection, client_address: str | None) -> list[SessionRow]:
        where, params = (
            ("WHERE client_address = ?", (client_address,)) if client_address else ("", ())
        )
        rows = conn.execute(
            f"SELECT {SESSION_COLUMNS} FROM sessions {where} ORDER BY started_at, name",
            params,
        ).fetchall()
        return [SessionRow.from_row(r) for r in rows]

    def _look(self, client_address: str | None) -> tuple[list[SessionRow], set[Key]]:
        """The rows and which of them are over, read without holding the
        write lock: looking processes up may take a `ps` call."""
        self.storage.init_schema()
        with self.storage.connect() as conn:
            rows = self._rows(conn, client_address)
        return rows, _known_ended(rows)

    def live(self, client_address: str | None = None) -> list[SessionRow]:
        """Sessions not known to be over — for one client address, or
        across the bus."""
        rows, ended = self._look(client_address)
        return [r for r in rows if r.key not in ended]

    def live_names(self) -> set[str]:
        return {r.name for r in self.live()}

    def get(self, server: Proc, client_address: str) -> SessionRow | None:
        self.storage.init_schema()
        with self.storage.connect() as conn:
            row = _own_row(conn, server, client_address)
        return SessionRow.from_row(row) if row else None

    def for_hook(
        self,
        client_address: str,
        *,
        session_key: str | None,
        ancestry: list[Proc] | None = None,
    ) -> str | None:
        """The session a hook belongs to, or None when it cannot be told.

        The hook's own client process — its nearest ancestor that is not a
        shell — decides first; a session id the client reports decides
        next. A session further up the hook's ancestry never counts: that
        is a different session, such as one whose terminal ran this
        client. `ancestry` defaults to this process's, looked up only when
        there is a session to compare it with.
        """
        candidates = self.live(client_address)
        if not candidates:
            return None
        own = procs.client_of(procs.ancestry() if ancestry is None else ancestry)
        if own is not None:
            names = {r.name for r in candidates if r.client_process == own}
            if len(names) == 1:
                return names.pop()
        if session_key:
            names = {r.name for r in candidates if r.session_key == session_key}
            if len(names) == 1:
                return names.pop()
        return None

    # ----------------------------- changes ----------------------------------

    def claim(
        self,
        *,
        client_address: str,
        repo_path: str,
        server: Proc,
        lineage: list[Proc],
        session_key: str | None = None,
        pinned: str | None = None,
    ) -> Claim:
        """Take a session address for the server `server` and register it.

        `pinned` is a handle asked for in advance. Ends the sessions of
        this client whose servers are gone, giving up their addresses."""
        parsed = identity.parse(client_address)
        if parsed.client is None or parsed.is_session:
            raise SessionError(f"{client_address!r} is not a client address")
        _before, ended_before = self._look(client_address)
        here = procs.scope()
        now = _utc_now_iso()
        warnings: list[str] = []
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                _expire(conn, client_address, now)
                rows = self._rows(conn, client_address)
                # a row that appeared after the look belongs to a server
                # that has only just started, so it counts as running
                over = [r for r in rows if r.key in ended_before or r.ended_at]
                over_keys = {r.key for r in over}
                live = [r for r in rows if r.key not in over_keys]
                crashed = [r for r in over if r.ended_at is None]
                choice = self._choose(
                    conn, client_address, live, over, procs.client_of(lineage),
                    session_key, pinned, warnings,
                )
                ended = self._end(
                    conn, crashed, keep=choice.name if choice.inherits else None,
                    live=live, now=now,
                )
                if choice.predecessor is not None:
                    _delete_own_row(conn, choice.predecessor.server, client_address)
                conn.execute(
                    f"INSERT OR REPLACE INTO sessions ({SESSION_COLUMNS}) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                    (
                        server.pid, server.started, choice.name, client_address,
                        repo_path, json.dumps([p.to_json() for p in lineage]),
                        session_key, here.boot_id, here.pid_ns, now, now,
                    ),
                )
                register_in(conn, choice.name, repo_path, overwrite_repo=True, now=now)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        self._audit("claim", choice.name, f"{choice.name!r} started", now)
        for row_name, moved in ended:
            self._audit_end(row_name, client_address, moved, now)
        return Claim(choice.name, warnings)

    @staticmethod
    def _untracked_numbers(
        conn: sqlite3.Connection, client_address: str, rows: list[SessionRow]
    ) -> set[str]:
        """Numbered names of this client on the roster that no session row
        accounts for — an older agent-bus's `AGENT_BUS_INSTANCE`, or an
        explicit `AGENT_BUS_NAME`. Something may still use them."""
        parsed = identity.parse(client_address)
        tracked = {r.name for r in rows}
        names = {
            r["name"] for r in conn.execute(
                "SELECT name FROM agents WHERE group_name = ?", (parsed.group,)
            )
        }
        untracked = set()
        for name in names - tracked:
            member = identity.parse_or_none(name)
            if member and member.client_address == client_address and member.number:
                untracked.add(name)
        return untracked

    def _choose(
        self,
        conn: sqlite3.Connection,
        client_address: str,
        live: list[SessionRow],
        over: list[SessionRow],
        client: Proc | None,
        session_key: str | None,
        pinned: str | None,
        warnings: list[str],
    ) -> _Choice:
        held = {r.name for r in live}
        unavailable = held | self._untracked_numbers(conn, client_address, live + over)
        for row in live:
            if _could_be_the_same_session(row, client, session_key):
                return _Choice(row.name)

        parsed = identity.parse(client_address)
        if pinned is not None:
            wanted = identity.compose(parsed.group, parsed.client, pinned)
            if wanted not in unavailable:
                return _Choice(wanted)
            warnings.append(
                f"{wanted!r} is held by another running session, so this one "
                "was given a number instead"
            )

        # the same client process restarting its server, or a resumed
        # session with the same id, gets its old address back
        for row in sorted(over, key=lambda r: r.ended_at or r.last_seen, reverse=True):
            same_process = client is not None and row.client_process == client
            same_key = session_key is not None and row.session_key == session_key
            if (same_process or same_key) and row.name not in unavailable:
                return _Choice(row.name, predecessor=row)

        # a fresh start: mail an ended holder left at a number goes to the
        # client address, not to the newcomer. Numbers kept for a session
        # that may come back are used only once every other one is taken.
        free = [
            name for name in (
                identity.compose(parsed.group, parsed.client, number)
                for number in range(identity.MIN_NUMBER, identity.MAX_NUMBER + 1)
            )
            if name not in unavailable
        ]
        kept = {r.name for r in over}
        for candidate in sorted(free, key=lambda name: name in kept):
            return _Choice(candidate, inherits=False)
        warnings.append(
            f"every session number of {client_address!r} is taken, so this "
            "session shares the client address"
        )
        return _Choice(client_address)

    def _end(
        self,
        conn: sqlite3.Connection,
        crashed: list[SessionRow],
        *,
        keep: str | None,
        live: list[SessionRow],
        now: str,
    ) -> list[tuple[str, int]]:
        """Mark the sessions whose servers are gone as ended, giving up the
        addresses no running session holds — all but `keep`. Returns
        (name, unread moved) for each address given up."""
        held = {r.name for r in live} | ({keep} if keep else set())
        ended: list[tuple[str, int]] = []
        for row in crashed:
            _mark_ended(conn, row, now)
            if row.name in held or row.name in {n for n, _ in ended}:
                continue
            moved = _give_up(conn, row.name, row.client_address)
            if moved is not None:
                ended.append((row.name, moved))
        return ended

    def release(self, server: Proc, client_address: str) -> None:
        """End the session server `server` holds for `client_address`, on
        its way out."""
        _rows, over = self._look(client_address)
        now = _utc_now_iso()
        moved = None
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                mine = _own_row(conn, server, client_address)
                if mine is None or mine["ended_at"] is not None:
                    conn.execute("COMMIT")
                    return
                row = SessionRow.from_row(mine)
                _mark_ended(conn, row, now)
                still_held = any(
                    r.name == row.name and r.key != row.key and r.ended_at is None
                    and r.key not in over
                    for r in self._rows(conn, client_address)
                )
                if not still_held:
                    moved = _give_up(conn, row.name, row.client_address)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        if moved is not None:
            self._audit_end(row.name, row.client_address, moved, now)

    def rename(self, server: Proc, client_address: str, label: str) -> str:
        """Give the session server `server` holds for `client_address` the
        address `<repo>/<client>-<label>`. Its unread mail moves with it,
        and unread mail it sent now says it came from the new name; a label
        an ended session left behind is taken over, mail and all."""
        handle = identity.normalize_label(label)
        _rows, over = self._look(client_address)
        now = _utc_now_iso()
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                mine = _own_row(conn, server, client_address)
                if mine is None or mine["ended_at"] is not None:
                    raise SessionError("this server holds no session address to rename")
                row = SessionRow.from_row(mine)
                parsed = identity.parse(client_address)
                new = identity.compose(parsed.group, parsed.client, handle)
                if new == row.name:
                    conn.execute("COMMIT")
                    return new
                rows = self._rows(conn, client_address)
                live = [r for r in rows if r.ended_at is None and r.key not in over]
                if any(r.name == row.name and r.key != row.key for r in live):
                    raise SessionError(
                        f"{row.name!r} is shared with other sessions the bus cannot "
                        "tell apart (one client process started their servers), so "
                        "a label would rename them all; set only a topic instead"
                    )
                if any(r.name == new for r in live):
                    raise SessionError(
                        f"{new!r} is held by another running session; pick another label"
                    )
                conn.execute(
                    "DELETE FROM sessions WHERE name = ? AND client_address = ? "
                    "AND ended_at IS NOT NULL",
                    (new, client_address),
                )
                topic = conn.execute(
                    "SELECT topic FROM agents WHERE name = ?", (row.name,)
                ).fetchone()
                register_in(conn, new, row.repo_path, overwrite_repo=True, now=now)
                if topic is not None and topic["topic"] is not None:
                    conn.execute(
                        "UPDATE agents SET topic = COALESCE(topic, ?) WHERE name = ?",
                        (topic["topic"], new),
                    )
                # mail at the shared client address belongs to every session
                moved = 0 if row.name == client_address else _move_unread(conn, row.name, new)
                _rewrite_sender(conn, row.name, new)
                conn.execute(
                    "UPDATE sessions SET name = ?, last_seen = ? WHERE server_pid = ? "
                    "AND server_started = ? AND client_address = ?",
                    (new, now, server.pid, server.started, client_address),
                )
                if row.name != client_address:
                    conn.execute("DELETE FROM agents WHERE name = ?", (row.name,))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        self._audit(
            "rename", new, f"{row.name!r} is now {new!r}; {moved} unread message(s) moved",
            now, previous=row.name,
        )
        return new

    # ----------------------------- audit ------------------------------------

    @staticmethod
    def _audit(event: str, name: str, text: str, ts: str, previous: str | None = None) -> None:
        audit.append_many([{
            "ts": ts,
            "op": "session",
            "actor": name,
            "message_id": None,
            "from": previous or name,
            "to": name,
            "thread_id": None,
            "body_preview": f"session {event}: {text}",
            "body_sha256": None,
        }])

    def _audit_end(self, name: str, client_address: str, moved: int, ts: str) -> None:
        if identity.parse(name).label:
            text = f"{name!r} ended; its address and mail wait for that label to be taken again"
        else:
            text = (
                f"{name!r} ended; {moved} unread message(s) moved to "
                f"{client_address!r} for its other sessions"
            )
        self._audit("end", name, text, ts)


def _own_row(conn: sqlite3.Connection, server: Proc, client_address: str) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT {SESSION_COLUMNS} FROM sessions WHERE server_pid = ? "
        "AND server_started = ? AND client_address = ?",
        (server.pid, server.started, client_address),
    ).fetchone()


def _delete_own_row(conn: sqlite3.Connection, server: Proc, client_address: str) -> None:
    conn.execute(
        "DELETE FROM sessions WHERE server_pid = ? AND server_started = ? "
        "AND client_address = ?",
        (server.pid, server.started, client_address),
    )


def _mark_ended(conn: sqlite3.Connection, row: SessionRow, now: str) -> None:
    conn.execute(
        "UPDATE sessions SET ended_at = ? WHERE server_pid = ? "
        "AND server_started = ? AND client_address = ?",
        (now, row.server.pid, row.server.started, row.client_address),
    )


def _expire(conn: sqlite3.Connection, client_address: str, now: str) -> None:
    """Forget ended sessions too old to be resumed."""
    hours = settings.session_resume_hours()
    cutoff = (
        datetime.strptime(now, TIMESTAMP_FORMAT) - timedelta(hours=hours)
    ).strftime(TIMESTAMP_FORMAT)
    conn.execute(
        "DELETE FROM sessions WHERE client_address = ? AND ended_at IS NOT NULL "
        "AND ended_at <= ?",
        (client_address, cutoff),
    )


def _move_unread(conn: sqlite3.Connection, old: str, new: str) -> int:
    """Re-address `old`'s unread mail to `new`, except copies of a fan-out
    `new` already holds — moving those would deliver it twice."""
    return conn.execute(
        """
        UPDATE messages SET to_agent = :new
        WHERE to_agent = :old AND read_at IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM messages AS sibling
              WHERE sibling.fanout_id = messages.fanout_id
                AND sibling.to_agent = :new
          )
        """,
        {"old": old, "new": new},
    ).rowcount


def _rewrite_sender(conn: sqlite3.Connection, old: str, new: str) -> None:
    """Unread mail sent as `old` now says it came from `new`, so that a
    reply reaches the session where it is now — and so that a session
    still does not read back what it sent its own client address."""
    conn.execute(
        "UPDATE messages SET from_agent = ? WHERE from_agent = ? AND read_at IS NULL",
        (new, old),
    )


def _give_up(conn: sqlite3.Connection, name: str, client_address: str) -> int | None:
    """Release the address `name` now that no running session holds it.
    A numbered address is removed: its unread mail moves to the client
    address, and unread mail it sent says it came from there. A labelled
    one stays as it is. Returns how many messages moved, or None when
    nothing was given up."""
    parsed = identity.parse_or_none(name)
    if parsed is None or not parsed.is_session:
        return None
    if parsed.label:
        return 0
    moved = _move_unread(conn, name, client_address)
    _rewrite_sender(conn, name, client_address)
    conn.execute("DELETE FROM agents WHERE name = ?", (name,))
    return moved

"""How each running session can be woken, and how often it has been.

A *route* says how to start a turn in one session when mail arrives at
its address. There are two kinds:

  * `watch` — the session runs a watcher (`watch.py`) that looks for mail
    itself and wakes its own session from inside. Claude Code and
    opencode. The newest watcher of a session owns its route, and an
    older one that finds the route taken gives up — so a session never
    has more than one watcher working, however many it started.
  * `push` — the sender starts the turn with the client's own command
    (`codex queue`). The route keeps the client's id for the session and
    the client's executable, which is all the command needs. Codex. Its
    hooks keep the route's `state` current: `busy` from the start of a
    turn, `idle` once a turn ends with nothing left to act on. Only an
    idle session is pushed to — a busy one is given its mail by its own
    end-of-turn hook — and pushing marks it `pushed`, so that a second
    message does not queue a second turn before the first has started.

A route records the processes it depends on: the client, and for a watch
route the watcher itself. A route one of them has left is dead. As with
sessions that is decided on evidence (`procs.gone`), and a process from
another PID namespace, which cannot be looked up from here, counts as
running.

`wakes` records each native wake so that no agent is woken more often
than `settings.wakes_per_hour` allows.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Iterable

from . import identity, procs, settings
from .procs import Proc

if TYPE_CHECKING:  # storage sends through this module
    from .storage import Storage

TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
ROUTE_COLUMNS = (
    "route_key, client, address, driver, pid, started, watcher_pid, "
    "watcher_started, token, command, state, boot_id, pid_ns, updated_at"
)

DRIVER_WATCH = "watch"
DRIVER_PUSH = "push"

STATE_IDLE = "idle"
STATE_BUSY = "busy"
STATE_PUSHED = "pushed"

# the rate limit's unit, part of the setting's name
RATE_WINDOW = timedelta(hours=1)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.strftime(TIMESTAMP_FORMAT)


def _proc(pid: object, started: object) -> Proc | None:
    if isinstance(pid, int) and isinstance(started, str) and started:
        return Proc(pid, started)
    return None


@dataclass(frozen=True)
class Route:
    route_key: str
    client: str
    address: str
    driver: str
    process: Proc | None  # the client
    watcher: Proc | None
    token: str | None
    command: str | None
    state: str | None
    boot_id: str | None
    pid_ns: str | None
    updated_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Route":
        return cls(
            route_key=row["route_key"],
            client=row["client"],
            address=row["address"],
            driver=row["driver"],
            process=_proc(row["pid"], row["started"]),
            watcher=_proc(row["watcher_pid"], row["watcher_started"]),
            token=row["token"],
            command=row["command"],
            state=row["state"],
            boot_id=row["boot_id"],
            pid_ns=row["pid_ns"],
            updated_at=row["updated_at"],
        )

    def covers(self, recipient: str) -> bool:
        """Whether this route's session reads mail sent to `recipient`:
        its own address, and for a session the address its client's
        sessions share."""
        if self.address == recipient:
            return True
        parsed = identity.parse_or_none(self.address)
        return parsed is not None and parsed.is_session and parsed.client_address == recipient

    @property
    def processes(self) -> list[Proc]:
        return [p for p in (self.process, self.watcher) if p is not None]


def _route_values(
    *, route_key: str, client: str, address: str, driver: str,
    owner: Proc | None, watcher: Proc | None = None, token: str | None = None,
    command: str | None = None, state: str | None = None, now: str,
) -> tuple:
    """One row's values, in ROUTE_COLUMNS order."""
    here = procs.scope()
    return (
        route_key, client, address, driver,
        owner.pid if owner else None, owner.started if owner else None,
        watcher.pid if watcher else None, watcher.started if watcher else None,
        token, command, state, here.boot_id, here.pid_ns, now,
    )


_PLACEHOLDERS = ", ".join("?" * len(ROUTE_COLUMNS.split(",")))


class WakeRoutes:
    def __init__(self, storage: "Storage") -> None:
        self.storage = storage

    def _connect(self):
        self.storage.init_schema()
        return self.storage.connect()

    # ----------------------------- watchers ---------------------------------

    def claim_watch(
        self, *, route_key: str, client: str, address: str,
        owner: Proc | None, watcher: Proc | None, token: str,
    ) -> None:
        """Make the watcher holding `token` the one for this session,
        displacing any earlier watcher of it — and of any other session
        of the same client process at the same address: a client that
        reuses its process for a fresh conversation (`/clear`) is still
        one session as far as the bus can tell."""
        values = _route_values(
            route_key=route_key, client=client, address=address, driver=DRIVER_WATCH,
            owner=owner, watcher=watcher, token=token, now=_iso(_utc_now()),
        )
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if owner is not None:
                    conn.execute(
                        "DELETE FROM wake_routes WHERE driver = ? AND address = ? "
                        "AND pid = ? AND started = ? AND route_key != ?",
                        (DRIVER_WATCH, address, owner.pid, owner.started, route_key),
                    )
                conn.execute(
                    f"INSERT OR REPLACE INTO wake_routes ({ROUTE_COLUMNS}) "
                    f"VALUES ({_PLACEHOLDERS})",
                    values,
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def owns(self, route_key: str, token: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT token FROM wake_routes WHERE route_key = ?", (route_key,)
            ).fetchone()
        return row is not None and row["token"] == token

    def move(self, route_key: str, token: str, address: str) -> None:
        """The watcher's session has a new address (it was renamed, or
        its server registered after the watcher started)."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE wake_routes SET address = ?, updated_at = ? "
                "WHERE route_key = ? AND token = ?",
                (address, _iso(_utc_now()), route_key, token),
            )

    def release(self, route_key: str, token: str) -> None:
        """Drop the route, if it is still this watcher's."""
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM wake_routes WHERE route_key = ? AND token = ?",
                (route_key, token),
            )

    # ----------------------------- push routes ------------------------------

    def note_push(
        self, *, route_key: str, client: str, address: str,
        owner: Proc | None, command: str | None, busy: bool,
    ) -> None:
        """Record, from one of its hooks, that session `route_key` of
        `client` answers at `address`, can be woken by running `command`,
        and is now busy or idle. Refreshed on every hook, so the session
        most recently active at an address is the one woken."""
        values = _route_values(
            route_key=route_key, client=client, address=address, driver=DRIVER_PUSH,
            owner=owner, command=command, state=STATE_BUSY if busy else STATE_IDLE,
            now=_iso(_utc_now()),
        )
        with self._connect() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO wake_routes ({ROUTE_COLUMNS}) "
                f"VALUES ({_PLACEHOLDERS})",
                values,
            )

    def take_idle(self, route: Route) -> bool:
        """Mark an idle push route as pushed to. False when it was not idle
        any more — busy, or another sender got there first."""
        with self._connect() as conn:
            changed = conn.execute(
                "UPDATE wake_routes SET state = ? WHERE route_key = ? AND state = ?",
                (STATE_PUSHED, route.route_key, STATE_IDLE),
            ).rowcount
        return changed == 1

    def put_back(self, route: Route) -> None:
        """Undo `take_idle` for a push that could not be started."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE wake_routes SET state = ? WHERE route_key = ? AND state = ?",
                (STATE_IDLE, route.route_key, STATE_PUSHED),
            )

    # ----------------------------- queries ----------------------------------

    def covering(self, recipients: Iterable[str]) -> dict[str, list[Route]]:
        """recipient → the routes of the sessions that read its mail,
        most recently active first. Whether their processes still run is
        not checked here: that costs a process lookup, worth paying only
        for a route the answer matters for (`alive`)."""
        wanted = list(dict.fromkeys(recipients))
        if not wanted:
            return {}
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {ROUTE_COLUMNS} FROM wake_routes ORDER BY updated_at DESC"
            ).fetchall()
        routes = [Route.from_row(r) for r in rows]
        return {name: [r for r in routes if r.covers(name)] for name in wanted}

    def alive(self, route: Route) -> bool:
        """Whether every process the route depends on still runs; a dead
        route is deleted."""
        if not _is_dead(route):
            return True
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM wake_routes WHERE route_key = ? AND updated_at = ?",
                (route.route_key, route.updated_at),
            )
        return False

    # ----------------------------- rate limit -------------------------------

    def may_wake(self, address: str) -> bool:
        """Whether `address` is below its hourly wake allowance."""
        limit = settings.wakes_per_hour()
        if limit == 0:
            return True
        since = _iso(_utc_now() - RATE_WINDOW)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM wakes WHERE address = ? AND woken_at > ?",
                (address, since),
            ).fetchone()
        return int(row["n"]) < limit

    def record_wake(self, address: str, driver: str) -> None:
        now = _utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "DELETE FROM wakes WHERE woken_at <= ?", (_iso(now - RATE_WINDOW),)
                )
                conn.execute(
                    "INSERT INTO wakes (address, woken_at, driver) VALUES (?, ?, ?)",
                    (address, _iso(now), driver),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise


def _is_dead(route: Route) -> bool:
    """Known to be over: recorded before this machine last booted, or one
    of its processes gone. A route from another PID namespace cannot be
    judged from here and counts as running."""
    here = procs.scope()
    if route.boot_id and here.boot_id and route.boot_id != here.boot_id:
        return True
    if route.pid_ns and here.pid_ns and route.pid_ns != here.pid_ns:
        return False
    return bool(procs.gone(route.processes))

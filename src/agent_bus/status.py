"""What every session is doing, in one list: the data behind `agent-bus status`.

Liveness comes from the session registry and what a session is doing comes
from the state it reported, so a crashed session reads `ended` rather than a
stale `working`. Spawn reads the same list for its concurrency cap."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from . import formatting as fmt
from . import identity, storage as storage_mod
from .sessions import SessionRegistry
from .storage import KIND_SESSION, SpawnRecord, Storage

STATE_RUNNING = "running"
STATE_ENDED = "ended"

# States that count against the spawn cap.
ACTIVE_STATES = (
    storage_mod.STATE_STARTING,
    STATE_RUNNING,
    storage_mod.STATE_WORKING,
    storage_mod.STATE_WAITING,
)


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.strptime(ts, storage_mod.TIMESTAMP_FORMAT).replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def effective_state(
    reported: str | None,
    *,
    live: bool,
    spawned_at: str | None,
    now: datetime,
    grace_seconds: float,
) -> str:
    """The state to show: what a live session reported, else what the
    spawn record and the grace period say. The grace covers only a session
    that never reported: one that said `working` or `waiting` and then died
    is `ended`, so it neither blocks a re-spawn nor counts against the cap."""
    if live:
        return reported if reported in storage_mod.REPORTED_STATES else STATE_RUNNING
    if reported == storage_mod.STATE_DONE:
        return storage_mod.STATE_DONE
    if reported not in (None, storage_mod.STATE_STARTING):
        return STATE_ENDED
    spawned = _parse_ts(spawned_at)
    if spawned is not None and (now - spawned).total_seconds() < grace_seconds:
        return storage_mod.STATE_STARTING
    return STATE_ENDED


@dataclass(frozen=True)
class StatusRow:
    name: str
    group: str
    client: str | None
    label: str | None
    state: str
    reported_state: str | None
    state_at: str | None
    live: bool
    spawned: bool
    last_seen: str | None
    topic: str | None
    pending_count: int
    repo_path: str
    repo_id: str | None
    work_dir: str
    tmux_session: str | None
    tmux_window: str | None
    model: str | None
    remote: bool | None
    # tuple of {"label", "url"} dicts the session attached
    links: tuple = ()
    role: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _label(name: str) -> str | None:
    parsed = identity.parse_or_none(name)
    return parsed.handle if parsed is not None else None


def _group(name: str) -> str:
    parsed = identity.parse_or_none(name)
    return parsed.group if parsed is not None else name


def collect(
    storage: Storage,
    *,
    grace_seconds: float,
    now: datetime | None = None,
    include_ended: bool = False,
    hide_done_after_seconds: float | None = None,
) -> list[StatusRow]:
    """Every session row. Unless `include_ended`, sessions that are gone
    are left out, and so are finished ones (dead, `done`) whose last report
    is older than `hide_done_after_seconds` (None: never; callers read it
    from config.json). Nothing is deleted from the database."""
    now = now or datetime.now(timezone.utc)
    roster = storage.list_agents_with_counts()
    live_names = SessionRegistry(storage).live_names()
    spawns: dict[str, SpawnRecord] = {s.name: s for s in storage.list_spawns()}
    cached_ids = storage.repo_ids()
    rows: list[StatusRow] = []
    seen: set[str] = set()

    for agent, pending in roster:
        if agent.kind != KIND_SESSION:
            continue
        record = spawns.get(agent.name)
        live = agent.name in live_names
        if not live and record is None:
            continue
        seen.add(agent.name)
        rows.append(StatusRow(
            name=agent.name,
            group=agent.group,
            client=agent.client_id or (record.client if record else None),
            label=_label(agent.name),
            state=effective_state(
                agent.state, live=live,
                spawned_at=record.spawned_at if record else None,
                now=now, grace_seconds=grace_seconds,
            ),
            reported_state=agent.state,
            state_at=agent.state_at,
            live=live,
            spawned=record is not None,
            last_seen=agent.last_seen,
            topic=agent.topic,
            pending_count=pending,
            repo_path=agent.repo_path,
            repo_id=(record.repo_id if record else None)
            or cached_ids.get(agent.repo_path),
            work_dir=(record.work_dir if record else None) or agent.repo_path,
            tmux_session=record.tmux_session if record else None,
            tmux_window=record.tmux_window if record else None,
            model=record.model if record else None,
            remote=record.remote if record else None,
            links=tuple(agent.link_list),
            role=record.role if record else None,
        ))

    for name, record in spawns.items():
        if name in seen:
            continue
        rows.append(StatusRow(
            name=name,
            group=_group(name),
            client=record.client,
            label=_label(name),
            state=effective_state(
                None, live=False, spawned_at=record.spawned_at,
                now=now, grace_seconds=grace_seconds,
            ),
            reported_state=None,
            state_at=None,
            live=False,
            spawned=True,
            last_seen=record.spawned_at,
            topic=None,
            pending_count=0,
            repo_path=record.repo_path,
            repo_id=record.repo_id or cached_ids.get(record.repo_path),
            work_dir=record.work_dir or record.repo_path,
            tmux_session=record.tmux_session,
            tmux_window=record.tmux_window,
            model=record.model,
            remote=record.remote,
            role=record.role,
        ))

    if not include_ended:
        rows = [
            r for r in rows
            if r.state != STATE_ENDED
            and not _is_stale_done(r, now, hide_done_after_seconds)
        ]
    rows.sort(key=lambda r: (r.group, r.name))
    return rows


def _is_stale_done(row: StatusRow, now: datetime, retention: float | None) -> bool:
    """A finished session that is gone and reported `done` longer ago than
    the retention: it would only clutter the default list."""
    if retention is None or row.live or row.state != storage_mod.STATE_DONE:
        return False
    reported = _parse_ts(row.state_at)
    return reported is not None and (now - reported).total_seconds() > retention


def _state_class(state: str) -> str:
    return "class:system" if state in (STATE_RUNNING, storage_mod.STATE_WORKING) else "class:ts"


def fragments(rows: list[StatusRow]) -> list[list[fmt.Fragment]]:
    """Styled lines: one header per repo group, one indented line per session."""
    by_group: dict[str, list[StatusRow]] = {}
    for row in rows:
        by_group.setdefault(row.group, []).append(row)
    name_col = max((len(r.name) for r in rows), default=0) + 4
    lines: list[list[fmt.Fragment]] = []
    for group, members in by_group.items():
        first = members[0]
        header: list[fmt.Fragment] = [
            (f"class:agent-{fmt.safe_class(group)}", group),
            ("", "  "),
            ("class:system", f"repo={first.repo_path}"),
        ]
        if first.repo_id:
            header += [("", "  "), ("class:ts", first.repo_id)]
        lines.append(header)
        for r in members:
            tmux = (
                f"tmux {r.tmux_session}:{r.tmux_window}"
                if r.tmux_session and r.tmux_window else "-"
            )
            seen_at = fmt.humanize_relative(r.last_seen) if r.last_seen else "-"
            line: list[fmt.Fragment] = [
                ("", "    "),
                (f"class:agent-{fmt.safe_class(r.name)}", r.name.ljust(name_col - 4)),
                ("", "  "),
                ("class:ts", (r.client or "-").ljust(8)),
                ("", "  "),
                (_state_class(r.state), r.state.ljust(8)),
                ("", "  "),
                ("class:ts", f"seen {seen_at}"),
                ("", "  "),
                ("class:system", tmux),
            ]
            if r.work_dir != r.repo_path:
                line += [("", "  "), ("class:system", f"dir={r.work_dir}")]
            line += [
                ("", "  "),
                ("class:thread" if r.pending_count == 0 else "class:op-send",
                 f"pending={r.pending_count}"),
            ]
            if r.topic:
                line += [("", "  "), ("class:system", r.topic)]
            lines.append(line)
    return lines

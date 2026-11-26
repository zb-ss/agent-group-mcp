"""dispatch.db store, task model helpers."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agent_bus.dispatch import model, settings
from agent_bus.dispatch.store import MIGRATIONS, DispatchStore
from agent_bus import migrations

NOW = "2030-01-07T10:15:00.000000Z"
LATER = "2030-01-08T10:15:00.000000Z"


@pytest.fixture
def bus_db(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "bus.db"
    monkeypatch.setenv("AGENT_BUS_DB", str(path))
    return path


def _task(title: str = "Fix login") -> model.Task:
    return model.Task(
        source="asana",
        task_id="1000000000000001",
        title=title,
        url="https://example.com/task/1",
        due_on="2030-01-07",
        modified_at=None,
        newest_comment_at=None,
    )


def test_path_beside_bus_db(bus_db):
    assert DispatchStore().path == bus_db.with_name("dispatch.db")
    assert settings.dispatch_db_path() == bus_db.with_name("dispatch.db")


def test_reads_on_missing_file_create_nothing(bus_db):
    store = DispatchStore()
    assert store.get("asana", "1") is None
    assert store.list_tasks() == []
    assert store.by_label("x") == []
    assert not store.path.exists()
    assert not bus_db.exists()


def test_observe_inserts_once_and_refreshes(bus_db):
    store = DispatchStore()
    first = store.observe(_task(), project_id="p1", now=NOW)
    second = store.observe(_task("Fix logout"), project_id="p2", now=LATER)
    assert first.first_seen_at == NOW
    assert second.first_seen_at == NOW
    assert (second.title, second.project_id) == ("Fix logout", "p2")
    assert second.handled_types == ()
    assert len(store.list_tasks()) == 1


def test_update_round_trips_every_column(bus_db):
    store = DispatchStore()
    store.observe(_task(), project_id="p1", now=NOW)
    values = {
        "project_id": "p9",
        "type": "refinement",
        "handled_types": ("refinement", "follow-up"),
        "repo": "repo-a",
        "label": "t0001-fix-login",
        "session_name": "repo-a/claude-1",
        "last_spawned_at": NOW,
        "handled_at": NOW,
        "last_comment_handled_at": LATER,
        "last_notified_state": "waiting",
        "outcome": model.OUTCOME_SPAWNED,
    }
    record = store.update("asana", "1000000000000001", **values)
    for key, value in values.items():
        assert getattr(record, key) == value
    assert store.by_label("t0001-fix-login") == [record]
    assert record.to_dict()["outcome"] == "spawned"
    store.update("asana", "1000000000000001", handled_types=["a"])
    assert store.get("asana", "1000000000000001").handled_types == ("a",)


def test_update_rejects_unknown_column(bus_db):
    store = DispatchStore()
    store.observe(_task(), project_id="p1", now=NOW)
    with pytest.raises(ValueError):
        store.update("asana", "1000000000000001", title="x")
    with pytest.raises(ValueError):
        store.update("asana", "1000000000000001", **{"outcome = 'x' --": 1})


def test_update_unknown_task(bus_db):
    with pytest.raises(KeyError):
        DispatchStore().update("asana", "nope", outcome="done")


def test_user_version_and_idempotent_init(bus_db):
    store = DispatchStore()
    store.init_schema()
    store.init_schema()
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == MIGRATIONS[-1].step
    assert not bus_db.exists()


def test_bus_migrations_untouched():
    assert MIGRATIONS not in (migrations.MIGRATIONS,)
    assert all(m.description != "tasks table" for m in migrations.MIGRATIONS)


def test_to_timestamp_normalises():
    expected = "2030-01-07T10:15:00.000000Z"
    assert model.to_timestamp("2030-01-07T10:15:00.000Z") == expected
    assert model.to_timestamp("2030-01-07T12:15:00+02:00") == expected
    assert model.to_timestamp(None) is None
    assert model.to_timestamp("garbage") is None


def test_clean_text():
    assert model.clean_text("a\nb\x1b[0m\t  c", 50) == "a b [0m c"
    assert model.clean_text("abcdefghij", 5) == "abcde..."
    assert model.format_now(__import__("datetime").datetime(2030, 1, 7, 10, 15)) == NOW


def test_db_files_are_owner_only_under_a_permissive_umask(tmp_path):
    import os
    import stat

    old = os.umask(0o022)
    try:
        store = DispatchStore(tmp_path / "state" / "dispatch.db")
        store.observe(_task(), project_id="1000000000000001", now="2030-01-01T00:00:00.000000Z")
        files = [p for p in (tmp_path / "state").iterdir()]
    finally:
        os.umask(old)
    assert any(p.name == "dispatch.db" for p in files)
    for path in files:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path.name
    assert stat.S_IMODE((tmp_path / "state").stat().st_mode) == 0o700


NEW_FIELDS = ("work_dir", "clone_path", "worktree_removed_at", "held_at", "approved_at", "start_commit")


def _version(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def test_step_one_database_migrates_to_three(bus_db):
    store = DispatchStore()
    store.path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(store.path)
    MIGRATIONS[0].apply(conn)
    conn.execute("PRAGMA user_version = 1")
    conn.execute(
        "INSERT INTO tasks (source, task_id, title, url, first_seen_at) "
        "VALUES ('asana', '1', 'T', 'https://example.com/t', ?)",
        (NOW,),
    )
    conn.commit()
    conn.close()

    store.init_schema()
    assert _version(store.path) == MIGRATIONS[-1].step
    record = store.get("asana", "1")
    assert record is not None
    assert all(getattr(record, name) is None for name in NEW_FIELDS)


def test_new_fields_round_trip_including_none(bus_db):
    store = DispatchStore()
    store.observe(_task(), project_id="p1", now=NOW)
    values = {name: f"value-{name}" for name in NEW_FIELDS}
    record = store.update("asana", "1000000000000001", **values)
    assert all(getattr(record, name) == values[name] for name in NEW_FIELDS)
    cleared = store.update("asana", "1000000000000001", **{n: None for n in NEW_FIELDS})
    assert all(getattr(cleared, name) is None for name in NEW_FIELDS)


def test_unknown_extra_column_still_loads(bus_db):
    store = DispatchStore()
    store.observe(_task(), project_id="p1", now=NOW)
    conn = sqlite3.connect(store.path)
    conn.execute("ALTER TABLE tasks ADD COLUMN from_a_later_build TEXT")
    conn.execute("UPDATE tasks SET from_a_later_build = 'x'")
    conn.commit()
    conn.close()
    record = store.get("asana", "1000000000000001")
    assert record is not None and record.title == "Fix login"


M2_FIELDS = (
    "client", "model", "candidate_type", "candidate_at", "decision", "decision_at",
    "decision_reason", "decision_repo", "decision_client", "decision_model",
)


def test_step_three_database_migrates_forward_keeping_records(bus_db):
    store = DispatchStore()
    store.path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(store.path)
    for migration in MIGRATIONS[:3]:
        migration.apply(conn)
    conn.execute("PRAGMA user_version = 3")
    conn.execute(
        "INSERT INTO tasks (source, task_id, title, url, first_seen_at) "
        "VALUES ('asana', '1', 'T', 'https://example.com/t', ?)",
        (NOW,),
    )
    conn.commit()
    conn.close()

    store.init_schema()
    assert _version(store.path) == MIGRATIONS[-1].step
    record = store.get("asana", "1")
    assert record is not None and record.title == "T"
    assert all(getattr(record, name) is None for name in M2_FIELDS)
    assert store.list_events() == []


def test_m2_fields_round_trip(bus_db):
    store = DispatchStore()
    store.observe(_task(), project_id=None, now=NOW)
    values = {name: f"value-{name}" for name in M2_FIELDS}
    record = store.update("asana", "1000000000000001", **values)
    assert all(getattr(record, name) == values[name] for name in M2_FIELDS)
    assert record.project_id is None


def test_log_event_round_trip_filter_and_limit(bus_db):
    store = DispatchStore()
    assert store.list_events() == []
    for index, event in enumerate(("candidate", "decide", "candidate")):
        store.log_event(
            at=f"2026-01-01T00:00:0{index}Z", event=event, actor="dispatch",
            source="asana", task_id=str(index), detail={"n": index},
        )
    store.log_event(at=NOW, event="routing", actor="dispatch")
    everything = store.list_events()
    assert [e["event"] for e in everything] == ["candidate", "decide", "candidate", "routing"]
    assert everything[0]["detail"] == {"n": 0}
    assert everything[-1]["detail"] == {} and everything[-1]["task_id"] is None
    assert [e["task_id"] for e in store.list_events(event="candidate")] == ["0", "2"]
    assert [e["task_id"] for e in store.list_events(event="candidate", limit=1)] == ["2"]


def test_pending_candidates(bus_db):
    store = DispatchStore()
    assert store.pending_candidates() == []
    for gid in ("1", "2", "3", "4"):
        store.observe(
            model.Task(source="asana", task_id=gid, title=gid, url="https://example.com/" + gid,
                       due_on=None, modified_at=None, newest_comment_at=None),
            project_id="p", now=NOW,
        )
    store.update("asana", "1", candidate_at=NOW, candidate_type="refinement")
    store.update("asana", "2", candidate_at=NOW, decision="ask")
    store.update("asana", "3", candidate_at=NOW, decision="skip")
    assert [r.task_id for r in store.pending_candidates()] == ["1", "2"]


def test_triage_reads_on_an_unmigrated_database_are_empty_and_write_nothing(bus_db):
    store = DispatchStore()
    store.path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(store.path)
    MIGRATIONS[0].apply(conn)
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()

    # no run has migrated this database yet (only dry runs, which never write)
    assert store.pending_candidates() == []
    assert store.list_events() == []
    assert _version(store.path) == 1


# ------------------------- milestone 3: step 5 -------------------------


def _step_four_db(store: DispatchStore) -> None:
    store.path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(store.path)
    for migration in MIGRATIONS[:4]:
        migration.apply(conn)
    conn.execute("PRAGMA user_version = 4")
    conn.execute(
        "INSERT INTO tasks (source, task_id, title, url, first_seen_at) "
        "VALUES ('asana', '1', 'T', 'https://example.com/t', ?)",
        (NOW,),
    )
    conn.commit()
    conn.close()


def _incident(store: DispatchStore, key: str = "alerts:1"):
    return store.create_incident(
        key=key, source="slack", container="C0000000001", rule="infra", action="respond",
        item_id="1", thread_ref="2030-01-07T10:00:00", url="https://example.slack.com/x", now=NOW,
    )


def _add_draft(store: DispatchStore):
    return store.add_draft(
        source="slack", item_id="1", kind="slack_reply", origin="scanner", rule="r",
        url="https://example.slack.com/x", target={"channel": "C0000000001"}, text="hi", now=NOW,
    )


def test_step_four_database_migrates_to_five_keeping_rows(bus_db):
    store = DispatchStore()
    _step_four_db(store)
    store.init_schema()
    assert _version(store.path) == MIGRATIONS[-1].step
    record = store.get("asana", "1")
    assert record is not None and record.title == "T" and record.decision_clone is None
    assert store.list_drafts() == [] and store.list_incidents() == []


def test_message_reads_on_missing_or_step_four_database_are_empty(bus_db):
    store = DispatchStore()

    def reads() -> None:
        assert store.source_state("slack") is None
        assert store.has_item("slack", "1") is False
        assert store.pending_alerts() == []
        assert store.get_draft(1) is None
        assert store.list_drafts() == [] and store.list_drafts("pending") == []
        assert store.get_incident(1) is None
        assert store.open_incident("k") is None
        assert store.list_incidents() == [] and store.list_incidents("open") == []
        assert store.incident_spawns_since(NOW) == 0
        assert store.unreported_incidents() == []

    reads()
    assert not store.path.exists()
    _step_four_db(store)
    reads()
    assert _version(store.path) == 4


def test_second_open_incident_is_refused_but_a_closed_one_does_not_block(bus_db):
    store = DispatchStore()
    first = _incident(store)
    assert first is not None and first.state == "open"
    assert _incident(store) is None
    assert _incident(store, "alerts:2") is not None
    store.update_incident(first.id, state="closed", closed_at=LATER, close_reason="resolved")
    again = _incident(store)
    assert again is not None and again.id != first.id
    assert store.open_incident("alerts:1").id == again.id
    assert [i.id for i in store.list_incidents("closed")] == [first.id]


def test_incident_updates_spawns_and_unreported(bus_db):
    store = DispatchStore()
    inc = _incident(store)
    assert store.unreported_incidents() == []
    store.update_incident(inc.id, label="inc-1", session_name="x/claude-1", repeats=2)
    assert store.incident_spawns_since(NOW) == 0  # a label alone is not an attempt
    attempted = store.record_spawn_attempt(inc.id, now=NOW)
    assert attempted.spawn_attempted_at == NOW
    assert store.incident_spawns_since(NOW) == 1
    assert store.incident_spawns_since(LATER) == 0
    assert [i.id for i in store.unreported_incidents()] == [inc.id]
    store.update_incident(inc.id, report_at=LATER)
    assert store.unreported_incidents() == []
    with pytest.raises(ValueError):
        store.update_incident(inc.id, key="other")
    with pytest.raises(KeyError):
        store.update_incident(999, state="closed")
    with pytest.raises(KeyError):
        store.record_spawn_attempt(999, now=NOW)
    assert store.incident_spawns_since(NOW) == 1  # the refused attempt left no row


def test_every_spawn_attempt_counts_by_when_it_was_made(bus_db):
    store = DispatchStore()
    old = _incident(store)  # opened at NOW
    store.record_spawn_attempt(old.id, now=NOW)
    store.record_spawn_attempt(old.id, now=LATER)  # a retry of the same incident
    assert store.incident_spawns_since(NOW) == 2
    assert store.incident_spawns_since(LATER) == 1  # counts the retry, not the open time
    assert store.get_incident(old.id).spawn_attempted_at == LATER


def _step_five_db(store: DispatchStore) -> None:
    """A database a step-5 build created: the incidents table has no attempt column."""
    _step_four_db(store)
    conn = sqlite3.connect(store.path)
    # the incidents table as step 5 first shipped it; step 5 then skips it
    conn.execute(
        "CREATE TABLE incidents (id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL, "
        "source TEXT NOT NULL, container TEXT, rule TEXT NOT NULL, action TEXT NOT NULL, "
        "state TEXT NOT NULL, item_id TEXT, thread_ref TEXT, url TEXT, label TEXT, "
        "session_name TEXT, client TEXT, model TEXT, result TEXT, "
        "repeats INTEGER NOT NULL DEFAULT 0, opened_at TEXT NOT NULL, last_seen_at TEXT, "
        "closed_at TEXT, close_reason TEXT, report_at TEXT)"
    )
    MIGRATIONS[4].apply(conn)
    conn.execute("PRAGMA user_version = 5")
    insert = (
        "INSERT INTO incidents (key, source, rule, action, state, opened_at, label, result) "
        "VALUES (?, 'slack', 'infra', 'respond', 'open', ?, ?, ?)"
    )
    conn.execute(insert, ("k1", NOW, "incident-1", "spawned"))
    conn.execute(insert, ("k2", NOW, None, "spawn_failed"))
    conn.execute(insert, ("k3", NOW, None, "rate_limited"))
    conn.commit()
    conn.close()


def test_step_five_database_without_the_attempt_column_migrates_additively(bus_db):
    store = DispatchStore()
    _step_five_db(store)
    # reads never migrate: the attempt count is empty below the needed step
    assert store.incident_spawns_since(NOW) == 0
    assert [i.key for i in store.list_incidents()] == ["k1", "k2", "k3"]
    assert _version(store.path) == 5
    store.init_schema()
    assert _version(store.path) == MIGRATIONS[-1].step
    incidents = {i.key: i for i in store.list_incidents()}
    assert incidents["k1"].label == "incident-1" and incidents["k1"].spawn_attempted_at == NOW
    assert incidents["k2"].spawn_attempted_at == NOW  # a failed attempt counts too
    assert incidents["k3"].spawn_attempted_at is None  # rate-limited: never attempted
    assert store.incident_spawns_since(NOW) == 2
    store.init_schema()  # idempotent: the backfill does not run twice
    assert store.incident_spawns_since(NOW) == 2


def test_claim_draft_succeeds_once_and_update_is_whitelisted(bus_db):
    store = DispatchStore()
    draft = _add_draft(store)
    assert draft.status == "pending" and draft.target == {"channel": "C0000000001"}
    assert store.claim_draft(draft.id, from_status="pending", to_status="sending", now=LATER)
    assert not store.claim_draft(draft.id, from_status="pending", to_status="sending", now=LATER)
    updated = store.update_draft(draft.id, text="new", status="sent", delivered_at=LATER)
    assert (updated.text, updated.status, updated.delivered_at) == ("new", "sent", LATER)
    assert [d.id for d in store.list_drafts("sent")] == [draft.id]
    assert store.list_drafts("pending") == []
    with pytest.raises(ValueError):
        store.update_draft(draft.id, source="x")
    with pytest.raises(KeyError):
        store.update_draft(999, text="x")


def test_save_source_state_keeps_cursor_when_only_last_run_is_given(bus_db):
    store = DispatchStore()
    store.save_source_state("slack", now=NOW, cursor="100")
    store.save_source_state("slack", now=LATER, last_run_at=LATER)
    state = store.source_state("slack")
    assert (state.cursor, state.last_run_at) == ("100", LATER)
    store.save_source_state("slack", now=LATER, cursor="200")
    state = store.source_state("slack")
    assert (state.cursor, state.last_run_at) == ("200", LATER)
    store.save_source_state("slack", now=LATER, cursor=None)
    assert store.source_state("slack").cursor is None
    store.save_source_state("maildir", now=NOW, last_run_at=NOW)
    assert store.source_state("maildir").cursor is None


def test_items_dedupe_and_alert_queue(bus_db):
    store = DispatchStore()
    store.record_item(
        "slack", "1", container="C0000000001", rule="r", action="notify", result="notified",
        url="https://example.slack.com/x", now=NOW, alert={"state": "open"},
    )
    store.record_item("slack", "2", container=None, rule=None, action=None, result="ignored",
                      url=None, now=LATER)
    assert store.has_item("slack", "1") and not store.has_item("maildir", "1")
    pending = store.pending_alerts()
    assert [(i.item_id, i.alert) for i in pending] == [("1", {"state": "open"})]
    store.clear_alert("slack", "1")
    assert store.pending_alerts() == []
    assert store.has_item("slack", "1")


def test_decision_clone_round_trips_and_clears_with_triage_fields(bus_db):
    from agent_bus.dispatch.store import TRIAGE_FIELDS

    store = DispatchStore()
    store.observe(_task(), project_id="p", now=NOW)
    record = store.update("asana", "1000000000000001", decision_clone="yes")
    assert record.decision_clone == "yes"
    assert "decision_clone" in TRIAGE_FIELDS
    cleared = store.update("asana", "1000000000000001", **{f: None for f in TRIAGE_FIELDS})
    assert cleared.decision_clone is None


def test_message_tables_stay_owner_only(tmp_path):
    import os
    import stat

    old = os.umask(0o022)
    try:
        store = DispatchStore(tmp_path / "state" / "dispatch.db")
        _add_draft(store)
        _incident(store)
        files = list((tmp_path / "state").iterdir())
    finally:
        os.umask(old)
    for path in files:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path.name


def _item(store: DispatchStore, item_id: str, now: str) -> None:
    store.record_item("slack", item_id, container="C0000000001", rule="r", action="draft",
                      result="drafted", url="https://example.slack.com/x", now=now)


def test_prune_drops_spawn_attempts_older_than_the_cutoff(bus_db):
    store = DispatchStore()
    inc = _incident(store)
    store.record_spawn_attempt(inc.id, now=NOW)
    store.record_spawn_attempt(inc.id, now=LATER)
    store.prune_message_items(LATER)
    assert store.incident_spawns_since(NOW) == 1  # only the attempt at LATER is left
    assert store.get_incident(inc.id).spawn_attempted_at == LATER


def test_prune_on_a_step_five_database_leaves_it_unmigrated(bus_db):
    store = DispatchStore()
    _step_five_db(store)
    assert store.prune_message_items(LATER) == 0
    assert _version(store.path) == 5


def test_prune_message_items_keeps_new_and_referenced_items(bus_db):
    store = DispatchStore()
    old = "2030-01-01T00:00:00.000000Z"
    for item_id in ("1", "2", "3", "4", "5"):
        _item(store, item_id, old)
    _item(store, "new", LATER)
    pending = _add_draft(store)  # item "1", pending
    sending = store.add_draft(
        source="slack", item_id="2", kind="slack", origin="scanner", rule="r", url=None,
        target={}, text="hi", now=NOW,
    )
    assert store.claim_draft(sending.id, from_status="pending", to_status="sending", now=NOW)
    sent = store.add_draft(
        source="slack", item_id="3", kind="slack", origin="scanner", rule="r", url=None,
        target={}, text="hi", now=NOW,
    )
    store.update_draft(sent.id, status="sent")
    store.create_incident(
        key="alerts:4", source="slack", container="C0000000001", rule="infra",
        action="respond", item_id="4", thread_ref=None, url=None, now=NOW,
    )
    closed = store.create_incident(
        key="alerts:5", source="slack", container="C0000000001", rule="infra",
        action="respond", item_id="5", thread_ref=None, url=None, now=NOW,
    )
    store.update_incident(closed.id, state="closed")
    assert pending.status == "pending"
    assert store.prune_message_items(NOW) == 2  # "3" (draft sent) and "5" (incident closed)
    kept = {i for i in ("1", "2", "3", "4", "5", "new") if store.has_item("slack", i)}
    assert kept == {"1", "2", "4", "new"}
    assert store.prune_message_items(NOW) == 0


def test_prune_on_a_missing_or_step_four_database_writes_nothing(bus_db):
    store = DispatchStore()
    assert store.prune_message_items(LATER) == 0
    assert not store.path.exists()
    _step_four_db(store)
    assert store.prune_message_items(LATER) == 0
    assert _version(store.path) == 4  # not migrated
    assert store.get("asana", "1") is not None


def test_message_items_hold_no_message_text(bus_db):
    store = DispatchStore()
    store.init_schema()
    with store.connect() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(message_items)")}
    assert columns == {
        "source", "item_id", "container", "rule", "action", "result", "url", "seen_at", "alert",
    }

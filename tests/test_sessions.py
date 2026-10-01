"""Session addresses: several sessions of one client in one repo."""

from __future__ import annotations

import asyncio
import io
import json
import sqlite3

import pytest

from agent_bus import procs
from agent_bus.hooks import run_hook_stop, run_hook_user_prompt
from agent_bus.procs import Proc
from agent_bus.sessions import SessionError, SessionRegistry

REPO = "/repo/shared"
CLAUDE = "shared/claude"


@pytest.fixture
def registry(storage, fake_procs):
    return SessionRegistry(storage)


def _start(registry, fake_procs, address=CLAUDE, *, key=None, pinned=None, client=None):
    """Start a session: a client process and its server. Returns
    (name, server, client process)."""
    server, lineage = fake_procs.session()
    if client is not None:
        lineage = [client]
    claim = registry.claim(
        client_address=address, repo_path=REPO, server=server,
        lineage=lineage, session_key=key, pinned=pinned,
    )
    return claim.name, server, lineage[0]


def _bodies(storage, name):
    return [m.body for m in storage.read_inbox(agent=name)]


# ----------------------------- addresses ------------------------------------


def test_each_session_gets_an_address_of_its_own(storage, registry, fake_procs):
    first, *_ = _start(registry, fake_procs)
    second, *_ = _start(registry, fake_procs)
    assert (first, second) == ("shared/claude-1", "shared/claude-2")

    storage.upsert_agent("peer/codex", "/repo/peer")
    storage.send_message(from_agent="peer/codex", to=second, body="for two only")
    assert _bodies(storage, first) == []
    assert _bodies(storage, second) == ["for two only"]


def test_sessions_of_one_client_can_message_each_other(storage, registry, fake_procs):
    first, *_ = _start(registry, fake_procs)
    second, *_ = _start(registry, fake_procs)
    storage.send_message(from_agent=first, to=second, body="ping")
    storage.send_message(from_agent=second, to=first, body="pong")
    assert _bodies(storage, second) == ["ping"]
    assert _bodies(storage, first) == ["pong"]


def test_the_client_address_goes_to_whichever_session_reads_first(
    storage, registry, fake_procs
):
    first, *_ = _start(registry, fake_procs)
    second, *_ = _start(registry, fake_procs)
    storage.upsert_agent("peer/codex", "/repo/peer")
    sent = storage.send_message(from_agent="peer/codex", to=CLAUDE, body="any claude")
    assert (sent["kind"], sent["recipients"]) == ("direct", [CLAUDE])

    assert _bodies(storage, second) == ["any claude"]
    assert _bodies(storage, first) == []


def test_a_session_never_receives_what_it_sent_to_its_client_address(
    storage, registry, fake_procs
):
    first, *_ = _start(registry, fake_procs)
    second, *_ = _start(registry, fake_procs)
    storage.send_message(from_agent=first, to=CLAUDE, body="anyone else?")
    assert _bodies(storage, first) == []
    assert _bodies(storage, second) == ["anyone else?"]


def test_a_repo_send_costs_one_copy_per_client(storage, registry, fake_procs):
    first, *_ = _start(registry, fake_procs)
    second, *_ = _start(registry, fake_procs)
    storage.upsert_agent("shared/codex", REPO)
    sent = storage.send_message(from_agent="shared/codex", to="shared", body="repo news")
    assert sent["recipients"] == [CLAUDE]

    (msg,) = storage.read_inbox(agent=first)
    assert msg.claimed_by == first
    assert _bodies(storage, second) == []


def test_a_repo_send_from_a_session_reaches_its_sibling(storage, registry, fake_procs):
    first, *_ = _start(registry, fake_procs)
    second, *_ = _start(registry, fake_procs)
    sent = storage.send_message(from_agent=first, to="shared", body="CI starting")
    assert sent["recipients"] == [CLAUDE]
    assert _bodies(storage, first) == []
    assert _bodies(storage, second) == ["CI starting"]


# ----------------------------- endings --------------------------------------


def test_a_numbered_session_that_ends_hands_its_mail_to_the_client_address(
    storage, registry, fake_procs
):
    first, server, _ = _start(registry, fake_procs)
    storage.upsert_agent("peer/codex", "/repo/peer")
    storage.send_message(from_agent="peer/codex", to=first, body="left behind")

    registry.release(server, CLAUDE)

    assert storage.get_agent(first) is None
    newcomer, *_ = _start(registry, fake_procs)
    assert newcomer == "shared/claude-2"  # -1 is kept a while for a comeback
    assert _bodies(storage, newcomer) == ["left behind"]


def test_a_crashed_session_is_cleaned_up_by_the_next_one(storage, registry, fake_procs):
    first, server, client = _start(registry, fake_procs)
    second, *_ = _start(registry, fake_procs)
    storage.upsert_agent("peer/codex", "/repo/peer")
    storage.send_message(from_agent="peer/codex", to=first, body="orphaned")
    fake_procs.kill(server, client)

    third, *_ = _start(registry, fake_procs)

    assert third == "shared/claude-3"
    assert [r.name for r in registry.live(CLAUDE)] == [second, third]
    # the mail went to the client address first, so the oldest reader gets it
    assert _bodies(storage, second) == ["orphaned"]


def test_a_labelled_session_keeps_its_address_and_mail_for_a_comeback(
    storage, registry, fake_procs
):
    name, server, _ = _start(registry, fake_procs, pinned="frontend")
    assert name == "shared/claude-frontend"
    storage.upsert_agent("peer/codex", "/repo/peer")
    registry.release(server, CLAUDE)
    storage.send_message(from_agent="peer/codex", to=name, body="while you were out")

    resumed, *_ = _start(registry, fake_procs, pinned="frontend")
    assert resumed == name
    assert _bodies(storage, resumed) == ["while you were out"]


def test_a_restarted_server_gets_its_old_address_back(storage, registry, fake_procs):
    _start(registry, fake_procs)
    second, server, client = _start(registry, fake_procs)
    fake_procs.kill(server)  # the server dies, its client lives on

    again, *_ = _start(registry, fake_procs, client=client)
    assert again == second


def test_a_resumed_session_with_the_same_id_gets_its_old_address_back(
    storage, registry, fake_procs
):
    _start(registry, fake_procs, key="session-a")
    second, server, client = _start(registry, fake_procs, key="session-b")
    fake_procs.kill(server, client)

    resumed, *_ = _start(registry, fake_procs, key="session-b")
    assert resumed == second


# ----------------------------- telling sessions apart ------------------------


def test_servers_a_hook_cannot_tell_apart_share_one_address(
    storage, registry, fake_procs
):
    """One process starting several servers with no session ids — one Codex
    app server for all its threads — gets one address, as before."""
    daemon = fake_procs.proc()
    first, *_ = _start(registry, fake_procs, "shared/codex", client=daemon)
    second, *_ = _start(registry, fake_procs, "shared/codex", client=daemon)
    assert first == second == "shared/codex-1"


def test_session_ids_tell_apart_servers_of_one_process(storage, registry, fake_procs):
    daemon = fake_procs.proc()
    first, *_ = _start(registry, fake_procs, client=daemon, key="a")
    second, *_ = _start(registry, fake_procs, client=daemon, key="b")
    assert (first, second) == ("shared/claude-1", "shared/claude-2")


def test_a_label_another_running_session_holds_gives_a_number_and_a_warning(
    registry, fake_procs
):
    _start(registry, fake_procs, pinned="frontend")
    server, lineage = fake_procs.session()
    claim = registry.claim(
        client_address=CLAUDE, repo_path=REPO, server=server, lineage=lineage,
        pinned="frontend",
    )
    assert claim.name == "shared/claude-1"
    assert "held by another running session" in claim.warnings[0]


def test_numbering_leaves_alone_a_name_it_does_not_track(storage, registry, fake_procs):
    """`repo/claude-1` registered by an older agent-bus has no session row;
    something may still be using it."""
    storage.upsert_agent("shared/claude-1", REPO)
    name, *_ = _start(registry, fake_procs)
    assert name == "shared/claude-2"


def test_claim_rejects_anything_but_a_client_address(registry, fake_procs):
    server, lineage = fake_procs.session()
    for address in ("shared", "shared/claude-2"):
        with pytest.raises(SessionError):
            registry.claim(
                client_address=address, repo_path=REPO, server=server, lineage=lineage,
            )


# ----------------------------- hooks find their session ----------------------


def test_a_hook_finds_its_session_through_the_client_process(registry, fake_procs):
    first, _, first_client = _start(registry, fake_procs)
    second, _, second_client = _start(registry, fake_procs)
    shell = fake_procs.proc("sh")
    terminal = fake_procs.proc("bash")

    assert registry.for_hook(
        CLAUDE, session_key=None, ancestry=[shell, second_client, terminal]
    ) == second
    assert registry.for_hook(
        CLAUDE, session_key=None, ancestry=[first_client, terminal]
    ) == first


def test_a_hook_that_cannot_tell_finds_nothing(registry, fake_procs):
    _start(registry, fake_procs)
    _start(registry, fake_procs)
    stranger = fake_procs.proc()
    assert registry.for_hook(CLAUDE, session_key=None, ancestry=[stranger]) is None


def test_a_hook_falls_back_to_the_session_id(registry, fake_procs):
    _start(registry, fake_procs, key="a")
    second, *_ = _start(registry, fake_procs, key="b")
    stranger = fake_procs.proc()
    assert registry.for_hook(CLAUDE, session_key="b", ancestry=[stranger]) == second


def test_a_hook_ignores_sessions_that_ended(registry, fake_procs):
    """A dead session sharing a far ancestor (the terminal) must not be
    mistaken for this hook's session."""
    terminal = fake_procs.proc()
    _, server, client = _start(registry, fake_procs)
    # the dead session's client ran in the same terminal
    fake_procs.kill(server, client)
    me = fake_procs.proc()
    assert registry.for_hook(CLAUDE, session_key=None, ancestry=[me, terminal]) is None


def _hook(storage, monkeypatch, ancestry, *, stop=False):
    monkeypatch.setenv("AGENT_BUS_NAME", CLAUDE)
    monkeypatch.setattr(procs, "ancestry", lambda pid=None: ancestry)
    out = io.StringIO()
    run = run_hook_stop if stop else run_hook_user_prompt
    run(storage=storage, stdin=io.StringIO("{}"), stdout=out, client="claude")
    return out.getvalue()


def test_the_prompt_hook_drains_its_own_session(storage, registry, fake_procs, monkeypatch):
    first, _, first_client = _start(registry, fake_procs)
    second, _, second_client = _start(registry, fake_procs)
    storage.upsert_agent("peer/codex", "/repo/peer")
    storage.send_message(from_agent="peer/codex", to=first, body="for one")
    storage.send_message(from_agent="peer/codex", to=second, body="for two")
    storage.send_message(from_agent="peer/codex", to=CLAUDE, body="for anyone")

    out = _hook(storage, monkeypatch, [first_client])
    assert "for one" in out and "for two" not in out
    assert "to any shared/claude session" in out
    assert _bodies(storage, second) == ["for two"]


def test_a_hook_that_cannot_tell_takes_only_shared_mail(
    storage, registry, fake_procs, monkeypatch
):
    first, *_ = _start(registry, fake_procs)
    _start(registry, fake_procs)
    storage.upsert_agent("peer/codex", "/repo/peer")
    storage.send_message(from_agent="peer/codex", to=first, body="private")
    storage.send_message(from_agent="peer/codex", to=CLAUDE, body="shared")

    out = _hook(storage, monkeypatch, [fake_procs.proc()], stop=True)
    assert "shared" in out and "private" not in out
    assert _bodies(storage, first) == ["private"]


# ----------------------------- renaming -------------------------------------


def test_rename_moves_the_session_and_its_mail(storage, registry, fake_procs):
    name, server, _ = _start(registry, fake_procs)
    storage.upsert_agent("peer/codex", "/repo/peer")
    storage.send_message(from_agent="peer/codex", to=name, body="before the rename")
    storage.set_topic(name, "Frontend login form")

    new = registry.rename(server, CLAUDE, "Frontend")

    assert new == "shared/claude-frontend"
    assert storage.get_agent(name) is None
    assert storage.get_agent(new).topic == "Frontend login form"
    assert registry.get(server, CLAUDE).name == new
    assert _bodies(storage, new) == ["before the rename"]


def test_rename_to_a_label_a_running_session_holds_fails(registry, fake_procs):
    _start(registry, fake_procs, pinned="frontend")
    _, server, _ = _start(registry, fake_procs)
    with pytest.raises(SessionError):
        registry.rename(server, CLAUDE, "frontend")


def test_rename_takes_over_a_label_an_ended_session_left(storage, registry, fake_procs):
    old, old_server, _ = _start(registry, fake_procs, pinned="frontend")
    registry.release(old_server, CLAUDE)
    storage.upsert_agent("peer/codex", "/repo/peer")
    storage.send_message(from_agent="peer/codex", to=old, body="waiting for frontend")

    _, server, _ = _start(registry, fake_procs)
    assert registry.rename(server, CLAUDE, "frontend") == old
    assert _bodies(storage, old) == ["waiting for frontend"]


# ----------------------------- the MCP surface -------------------------------


def _server(storage, fake_procs, name=CLAUDE):
    from agent_bus.server import build_server

    server, lineage = fake_procs.session()
    return build_server(
        name=name, repo_path=REPO, storage=storage, process=server, lineage=lineage,
    )


def _call(mcp, tool, args):
    """Invoke a FastMCP tool and return its result, unwrapped."""
    raw = asyncio.run(mcp.call_tool(tool, args))
    if isinstance(raw, tuple):
        out = raw[1]
    else:  # older FastMCP: a list of text content
        text = "".join(getattr(c, "text", "") for c in raw)
        out = json.loads(text) if text else None
    return out["result"] if isinstance(out, dict) and "result" in out else out


def test_set_session_names_the_session_for_its_peers(storage, fake_procs):
    first = _server(storage, fake_procs)
    second = _server(storage, fake_procs)

    out = _call(second, "set_session", {"label": "release-notes", "topic": "changelog draft"})
    assert out == {
        "name": "shared/claude-release-notes", "client_address": CLAUDE, "topic": "changelog draft",
    }
    _call(first, "send_message", {"to": "shared/claude-release-notes", "body": "review?"})
    assert [m["body"] for m in _call(second, "read_inbox", {})] == ["review?"]

    roster = {a["name"]: a for a in _call(first, "list_agents", {"group": "shared"})}
    assert roster["shared/claude-release-notes"]["live"] is True
    assert roster["shared/claude-release-notes"]["topic"] == "changelog draft"
    assert roster[CLAUDE]["kind"] == "client"


def test_set_session_needs_a_session(storage, fake_procs):
    from mcp.server.fastmcp.exceptions import ToolError

    loner = _server(storage, fake_procs, name="human")
    with pytest.raises(ToolError):
        _call(loner, "set_session", {"label": "x"})


def test_sending_to_an_ended_session_says_so(storage, fake_procs):
    first = _server(storage, fake_procs)
    storage.upsert_agent("shared/claude-frontend", REPO)  # a label nobody holds now
    out = _call(first, "send_message", {"to": "shared/claude-frontend", "body": "hi"})
    assert "no running session holds" in out["note"]


def test_sessions_can_be_switched_off(storage, fake_procs, monkeypatch):
    monkeypatch.setenv("AGENT_BUS_SESSIONS", "0")
    mcp = _server(storage, fake_procs)
    assert _call(mcp, "whoami", {})["name"] == CLAUDE


# ----------------------------- compatibility --------------------------------


def test_mail_an_older_agent_bus_fanned_out_to_a_session_arrives_once(
    storage, registry, fake_procs
):
    """0.5.x expands a repo send to every row in the group, sessions and
    client address alike. The session must not read it twice."""
    first, *_ = _start(registry, fake_procs)
    with storage.connect() as conn:
        for mid, to in (("m1", first), ("m2", CLAUDE)):
            conn.execute(
                "INSERT INTO messages (message_id, from_agent, to_agent, body, "
                "thread_id, sent_at, addressed_to, fanout_id, to_group) "
                "VALUES (?, 'old/agent', ?, 'old fan-out', 't', "
                "'2026-01-01T00:00:00.000000Z', 'shared', 'f1', 'shared')",
                (mid, to),
            )
    assert _bodies(storage, first) == ["old fan-out"]


def test_migration_files_names_that_only_now_parse(bus_paths):
    from agent_bus import migrations
    from agent_bus.storage import Storage

    store = Storage()
    store.init_schema()
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO agents (name, repo_path, registered_at, last_seen) "
            "VALUES ('shared/codex-docs', ?, 'x', 'x')", (REPO,),
        )
        conn.execute("PRAGMA user_version = 1002")
    with store.connect() as conn:
        migrations.migrate(conn, bus_paths["db"])
    row = store.get_agent("shared/codex-docs")
    assert (row.group, row.client_id, row.kind) == ("shared", "codex", "session")
    assert row.client is None  # 0.5.x parses the names in that column strictly


# ----------------------------- processes ------------------------------------


def test_proc_stat_survives_a_command_name_with_parentheses(tmp_path, monkeypatch):
    (tmp_path / "42").mkdir()
    fields = ["S", "7"] + ["0"] * 17 + ["123456"] + ["0"] * 10
    (tmp_path / "42" / "stat").write_text(f"42 (odd (name) x) {' '.join(fields)}\n")
    monkeypatch.setattr(procs, "PROC_ROOT", tmp_path)
    assert procs._read_proc_stat(42) == (7, "123456", "S", "odd (name) x")


def test_this_process_and_its_ancestry_are_running():
    chain = procs.ancestry()
    assert chain, "the test runner has a parent"
    assert procs.gone(chain) == set()
    assert procs.gone([Proc(chain[0].pid, "a different start")])


def test_ps_lookup_agrees_with_proc(monkeypatch, tmp_path):
    """The macOS path, exercised wherever `ps` exists."""
    if not procs._ps_table():
        pytest.skip("no ps here")
    expected = [p.pid for p in procs.ancestry()]
    monkeypatch.setattr(procs, "PROC_ROOT", tmp_path / "nothing")
    assert [p.pid for p in procs.ancestry()] == expected


def test_session_rows_survive_a_bad_lineage(storage, registry, fake_procs):
    _start(registry, fake_procs)
    with storage.connect() as conn:
        conn.execute("UPDATE sessions SET lineage = 'not json'")
    (row,) = registry.live(CLAUDE)
    assert row.lineage == ()


def test_sessions_table_exists_after_migration(storage):
    with storage.connect() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
    assert {"server_pid", "name", "client_address", "lineage"} <= cols
    with pytest.raises(sqlite3.IntegrityError):
        with storage.connect() as conn:
            conn.execute(
                "INSERT INTO sessions (server_pid, server_started, name, "
                "client_address, repo_path, lineage, started_at, last_seen) "
                "VALUES (1, 's', NULL, 'a/b', '/r', '[]', 't', 't')"
            )


# ----------------------------- review regressions ---------------------------


def test_a_shared_address_cannot_be_relabelled(storage, registry, fake_procs):
    """Two threads of one Codex process share an address; relabelling it
    for one would leave the other on a name that no longer exists."""
    daemon = fake_procs.proc("codex")
    name, server, _ = _start(registry, fake_procs, "shared/codex", client=daemon)
    _start(registry, fake_procs, "shared/codex", client=daemon)
    with pytest.raises(SessionError, match="shared with other sessions"):
        registry.rename(server, "shared/codex", "frontend")
    assert storage.get_agent(name) is not None


def test_not_knowing_whether_a_process_runs_is_not_the_same_as_gone(
    storage, registry, fake_procs, monkeypatch
):
    first, server, client = _start(registry, fake_procs)
    storage.upsert_agent("peer/codex", "/repo/peer")
    storage.send_message(from_agent="peer/codex", to=first, body="private for one")
    fake_procs.kill(server, client)
    # the process table cannot be read: nothing may be declared gone
    monkeypatch.setattr(procs, "gone", lambda candidates: set())

    newcomer, *_ = _start(registry, fake_procs)
    assert newcomer == "shared/claude-2"
    assert _bodies(storage, newcomer) == []
    assert _bodies(storage, first) == ["private for one"]


def test_an_unreadable_process_table_declares_nothing_gone(monkeypatch):
    monkeypatch.setattr(procs, "_uses_proc", lambda: False)
    monkeypatch.setattr(procs, "_ps_table", lambda: None)
    assert procs.gone([Proc(1, "whenever")]) == set()
    assert procs.ancestry() == []


def test_ps_runs_in_a_fixed_locale_and_time_zone(monkeypatch):
    seen = {}

    def fake_run(args, **kwargs):
        seen.update(kwargs)
        raise OSError("no ps here")

    monkeypatch.setattr(procs.subprocess, "run", fake_run)
    assert procs._ps_table() is None
    assert (seen["env"]["LC_ALL"], seen["env"]["TZ"]) == ("C", "UTC0")
    assert seen["stdin"] is procs.subprocess.DEVNULL


def test_ps_output_is_parsed_with_its_five_word_start_time(monkeypatch):
    out = (
        "  1     0 S Thu Oct  1 19:12:50 2026 systemd\n"
        "777     1 Z Thu Oct  1 20:00:00 2026 my app\n"
    )

    class Done:
        stdout = out

    monkeypatch.setattr(procs.subprocess, "run", lambda *a, **k: Done())
    table = procs._ps_table()
    assert table[777] == (1, "Thu Oct 1 20:00:00 2026", "Z", "my app")
    monkeypatch.setattr(procs, "_uses_proc", lambda: False)
    assert procs.gone([Proc(777, "Thu Oct 1 20:00:00 2026")])  # a zombie


def test_a_zombie_server_has_ended(tmp_path, monkeypatch):
    (tmp_path / "42").mkdir()
    fields = ["Z", "7"] + ["0"] * 17 + ["123456"] + ["0"] * 10
    (tmp_path / "42" / "stat").write_text(f"42 (python) {' '.join(fields)}\n")
    (tmp_path / "self").mkdir()
    (tmp_path / "self" / "stat").write_text("")
    monkeypatch.setattr(procs, "PROC_ROOT", tmp_path)
    assert procs.gone([Proc(42, "123456")]) == {Proc(42, "123456")}


def test_sessions_from_another_pid_namespace_count_as_running(
    storage, registry, fake_procs
):
    first, server, client = _start(registry, fake_procs)
    fake_procs.kill(server, client)
    with storage.connect() as conn:
        conn.execute("UPDATE sessions SET pid_ns = 'pid:[elsewhere]'")
    if procs.scope().pid_ns is None:
        pytest.skip("no PID namespaces here")
    assert [r.name for r in registry.live(CLAUDE)] == [first]


def test_sessions_from_before_a_reboot_have_ended(storage, registry, fake_procs):
    if procs.scope().boot_id is None:
        pytest.skip("no boot id here")
    _start(registry, fake_procs)
    with storage.connect() as conn:
        conn.execute("UPDATE sessions SET boot_id = 'an-earlier-boot'")
    assert registry.live(CLAUDE) == []


def test_a_session_further_up_the_ancestry_is_not_this_hooks(registry, fake_procs):
    """`claude -p` run from another session's terminal: the outer session's
    client is an ancestor of the inner hook, but it is not the inner
    session."""
    outer, _, outer_client = _start(registry, fake_procs, key="outer-id")
    inner_client = fake_procs.proc("claude")
    hook_ancestry = [fake_procs.proc("sh"), inner_client, fake_procs.proc("bash"), outer_client]
    assert registry.for_hook(CLAUDE, session_key="inner-id", ancestry=hook_ancestry) is None
    assert registry.for_hook(
        CLAUDE, session_key="outer-id", ancestry=hook_ancestry
    ) == outer  # the id still settles it


def test_the_own_client_process_wins_over_a_stale_session_id(registry, fake_procs):
    """After /clear the client may report a new id while its server keeps
    the old one; the process is the same, so is the session."""
    name, _, client = _start(registry, fake_procs, key="before-clear")
    assert registry.for_hook(
        CLAUDE, session_key="after-clear", ancestry=[fake_procs.proc("sh"), client],
    ) == name


def test_a_hook_never_falls_back_to_a_label_someone_else_holds(
    storage, registry, fake_procs, monkeypatch
):
    holder, *_ = _start(registry, fake_procs, pinned="frontend")
    storage.upsert_agent("peer/codex", "/repo/peer")
    storage.send_message(from_agent="peer/codex", to=holder, body="for the holder")
    storage.send_message(from_agent="peer/codex", to=CLAUDE, body="for anyone")

    monkeypatch.setenv("AGENT_BUS_SESSION", "frontend")
    out = _hook(storage, monkeypatch, [fake_procs.proc()], stop=True)
    assert "for anyone" in out and "for the holder" not in out


def test_a_cleanly_restarted_server_gets_its_number_back(storage, registry, fake_procs):
    _start(registry, fake_procs)
    second, server, client = _start(registry, fake_procs)
    registry.release(server, CLAUDE)
    fake_procs.kill(server)
    third, *_ = _start(registry, fake_procs)  # someone else starts meanwhile
    assert third == "shared/claude-3"  # -2 is reserved for its comeback

    again, *_ = _start(registry, fake_procs, client=client)
    assert again == second


def test_a_cleanly_restarted_labelled_session_keeps_its_label_and_mail(
    storage, registry, fake_procs
):
    name, server, _ = _start(registry, fake_procs, key="conv-1", pinned="frontend")
    registry.release(server, CLAUDE)
    storage.upsert_agent("peer/codex", "/repo/peer")
    storage.send_message(from_agent="peer/codex", to=name, body="while restarting")

    resumed, *_ = _start(registry, fake_procs, key="conv-1")  # no label pinned
    assert resumed == name
    assert _bodies(storage, resumed) == ["while restarting"]


def test_a_reservation_runs_out(storage, registry, fake_procs, monkeypatch):
    monkeypatch.setenv("AGENT_BUS_SESSION_RESUME_HOURS", "0")
    _, server, client = _start(registry, fake_procs, key="conv-1")
    registry.release(server, CLAUDE)
    _start(registry, fake_procs)  # expires the ended row
    again, *_ = _start(registry, fake_procs, key="conv-1")
    assert again == "shared/claude-2"


def test_an_older_agent_bus_can_still_parse_every_client_on_the_roster(
    storage, registry, fake_procs
):
    """0.5.x parses, strictly, the name of every roster row whose `client`
    column matches; a session name there would crash it."""
    _start(registry, fake_procs)
    _, server, _ = _start(registry, fake_procs)
    registry.rename(server, CLAUDE, "frontend")
    with storage.connect() as conn:
        named = [r[0] for r in conn.execute("SELECT name FROM agents WHERE client IS NOT NULL")]
    assert named == [CLAUDE]


def test_a_renamed_session_still_does_not_read_back_its_own_shared_send(
    storage, registry, fake_procs
):
    first, server, _ = _start(registry, fake_procs)
    second, *_ = _start(registry, fake_procs)
    storage.send_message(from_agent=first, to=CLAUDE, body="anyone?")
    new = registry.rename(server, CLAUDE, "frontend")
    assert _bodies(storage, new) == []
    (msg,) = storage.read_inbox(agent=second)
    assert msg.from_agent == new  # a reply reaches the session where it is now


def test_mail_left_for_the_next_session_reaches_the_one_reusing_the_number(
    storage, registry, fake_procs
):
    first, server, client = _start(registry, fake_procs)
    storage.send_message(from_agent=first, to=CLAUDE, body="whoever comes next")
    registry.release(server, CLAUDE)
    fake_procs.kill(server, client)

    newcomer, *_ = _start(registry, fake_procs)
    (msg,) = storage.read_inbox(agent=newcomer)
    assert (msg.body, msg.from_agent) == ("whoever comes next", CLAUDE)


def test_reading_shared_fan_out_alone_logs_no_claim(storage, registry, fake_procs, bus_paths):
    import json as _json

    first, *_ = _start(registry, fake_procs)
    storage.upsert_agent("shared/codex", REPO)
    storage.send_message(from_agent="shared/codex", to="shared", body="news")
    storage.read_inbox(agent=first)
    ops = [_json.loads(line)["op"] for line in bus_paths["log"].read_text().splitlines()]
    assert "claim" not in ops


def test_an_older_fan_out_copy_at_the_client_address_is_settled(
    storage, registry, fake_procs
):
    first, *_ = _start(registry, fake_procs)
    with storage.connect() as conn:
        for mid, to in (("m1", first), ("m2", CLAUDE)):
            conn.execute(
                "INSERT INTO messages (message_id, from_agent, to_agent, body, "
                "thread_id, sent_at, addressed_to, fanout_id, to_group) "
                "VALUES (?, 'old/agent', ?, 'old fan-out', 't', "
                "'2026-01-01T00:00:00.000000Z', 'shared', 'f1', 'shared')",
                (mid, to),
            )
    assert _bodies(storage, first) == ["old fan-out"]
    assert storage.pending_count(agent=CLAUDE) == 0


def test_a_session_ancestry_through_a_shell_finds_the_client(registry, fake_procs):
    """A server started through `sh -c` without exec still belongs to the
    client above the shell."""
    client = fake_procs.proc("claude")
    server = fake_procs.proc("python")
    registry.claim(
        client_address=CLAUDE, repo_path=REPO, server=server,
        lineage=[fake_procs.proc("sh"), client],
    )
    assert registry.for_hook(
        CLAUDE, session_key=None, ancestry=[fake_procs.proc("bash"), client],
    ) == "shared/claude-1"


def test_numbers_kept_for_a_comeback_are_used_once_all_others_are_taken(
    storage, registry, fake_procs, monkeypatch
):
    monkeypatch.setattr(identity_module(), "MAX_NUMBER", 2)
    _, server, _ = _start(registry, fake_procs)
    registry.release(server, CLAUDE)
    second, *_ = _start(registry, fake_procs)
    third, *_ = _start(registry, fake_procs)
    assert (second, third) == ("shared/claude-2", "shared/claude-1")


def identity_module():
    from agent_bus import identity

    return identity

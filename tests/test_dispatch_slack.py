import json
import urllib.parse

import pytest

from agent_bus.dispatch import slack, source
from agent_bus.dispatch.slack import SlackBudget, SlackClient
from agent_bus.dispatch.source import HttpResponse, SourceError

TOKEN = "fake-slack-token-value"
CFG = {
    "base_url": "https://slack.example.com/api/",
    "token_env": "FAKE_SLACK_TOKEN",
    "timeout_seconds": 7,
    "max_retry_wait_seconds": 5,
    "min_interval_seconds": 1.0,
}
ENV = {"FAKE_SLACK_TOKEN": TOKEN}


class FakeSend:
    """Scripted by method name; each entry is a payload or (payload, headers)."""

    def __init__(self, script):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls = []

    def __call__(self, url, body, headers, timeout):
        method = url.rsplit("/", 1)[1]
        self.calls.append((url, body, dict(headers), timeout))
        item = self.script[method].pop(0)
        payload, hdrs = item if isinstance(item, tuple) else (item, {})
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return HttpResponse(body=raw, headers=hdrs)


class Clock:
    def __init__(self):
        self.now = 100.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def client(send, clock=None, cfg=None, env=ENV, budget=None):
    clock = clock or Clock()
    return SlackClient(
        cfg or CFG, env=env, http_send=send, sleep=clock.sleep, clock=clock, budget=budget
    )


def test_call_posts_form_with_token_only_in_header():
    send = FakeSend({"auth.test": [({"ok": True, "user_id": "U0000000001"}, {"x-oauth-scopes": "a"})]})
    payload, headers = client(send).call("auth.test", {"x": "1"})
    url, body, hdrs, timeout = send.calls[0]
    assert url == "https://slack.example.com/api/auth.test"
    assert body == b"x=1"
    assert hdrs["Authorization"] == f"Bearer {TOKEN}"
    assert hdrs["Content-Type"] == "application/x-www-form-urlencoded; charset=utf-8"
    assert timeout == 7.0
    assert TOKEN not in url and TOKEN.encode() not in body
    assert payload["user_id"] == "U0000000001"
    assert headers["x-oauth-scopes"] == "a"


def test_missing_token_names_only_the_variable():
    with pytest.raises(SourceError) as info:
        client(FakeSend({}), env={}).call("auth.test", {})
    assert str(info.value) == "environment variable FAKE_SLACK_TOKEN is not set"


def test_invalid_json_and_error_code_sanitised_without_token():
    with pytest.raises(SourceError) as info:
        client(FakeSend({"m": [b"<html>" + TOKEN.encode()]})).call("m", {})
    assert str(info.value) == "slack m: invalid response"
    bad = {"ok": False, "error": "Bad Token! " + "x" * 100}
    with pytest.raises(SourceError) as info:
        client(FakeSend({"m": [bad]})).call("m", {})
    code = str(info.value).split(": ", 1)[1]
    assert code.startswith("badtoken") and len(code) == 60
    assert TOKEN not in str(info.value)


def test_pacing_waits_between_calls():
    clock = Clock()
    send = FakeSend({"m": [{"ok": True}, {"ok": True}]})
    c = client(send, clock)
    c.call("m", {})
    c.call("m", {})
    assert clock.sleeps == [1.0]


def test_budget_is_enforced():
    c = client(FakeSend({"m": [{"ok": True}, {"ok": True}]}), budget=1)
    c.call("m", {})
    with pytest.raises(SlackBudget):
        c.call("m", {})
    assert issubclass(SlackBudget, SourceError)


def test_ratelimited_retried_once_with_capped_wait():
    clock = Clock()
    limited = ({"ok": False, "error": "ratelimited"}, {"retry-after": "900"})
    send = FakeSend({"m": [limited, {"ok": True, "v": 1}]})
    payload, _ = client(send, clock).call("m", {})
    assert payload["v"] == 1
    assert 5.0 in clock.sleeps
    assert len(send.calls) == 2

    send = FakeSend({"m": [limited, limited]})
    with pytest.raises(SourceError) as info:
        client(send, Clock()).call("m", {})
    assert str(info.value) == "slack m failed: ratelimited"
    assert len(send.calls) == 2


def test_default_send_gets_cap_at_call_time(monkeypatch):
    seen = {}

    def fake(url, body, headers, timeout, **kw):
        seen.update(kw)
        return HttpResponse(body=b'{"ok": true}', headers={})

    monkeypatch.setattr(source, "default_http_send", fake)
    SlackClient(CFG, env=ENV).call("m", {})
    assert seen == {"max_retry_wait": 5.0}


def test_post_message_parameters():
    send = FakeSend({"chat.postMessage": [{"ok": True, "ts": "1893456000.000200"}]})
    ts = slack.post_message(
        CFG, env=ENV, channel="C0000000001", thread_ts="1893456000.000100",
        text="hi", http_send=send,
    )
    assert ts == "1893456000.000200"
    params = urllib.parse.parse_qs(send.calls[0][1].decode())
    assert params == {"channel": ["C0000000001"], "text": ["hi"],
                      "thread_ts": ["1893456000.000100"]}
    send = FakeSend({"chat.postMessage": [{"ok": True, "ts": "1.2"}]})
    slack.post_message(CFG, env=ENV, channel="C0000000001", thread_ts=None, text="x", http_send=send)
    assert "thread_ts" not in urllib.parse.parse_qs(send.calls[0][1].decode())


@pytest.mark.parametrize("text,expected", [
    ("<!channel> go", "channel go"),
    ("<!here>", "here"),
    ("<!everyone>", "everyone"),
    ("hi <@U0000000001>!", "hi U0000000001!"),
    ("<!subteam^S0000000001>", "subteam^S0000000001"),
    ("<!subteam^S0000000001|@team>", "subteam^S0000000001|@team"),
    ("plain <https://example.com|x>", "plain <https://example.com|x>"),
    ("<!<!channel>>", "channel"),
    ("<@<@U0000000001>>", "U0000000001"),
    ("<!<!<!here>>>", "here"),
])
def test_neutralise(text, expected):
    assert slack.neutralise(text) == expected


def test_permalink():
    assert slack.permalink("https://example.slack.com/", "C0000000001", "1893456000.000100") == \
        "https://example.slack.com/archives/C0000000001/p1893456000000100"
    assert slack.permalink(
        "https://example.slack.com", "C0000000001", "1893456000.000200", "1893456000.000100"
    ).endswith("/p1893456000000200?thread_ts=1893456000.000100&cid=C0000000001")


# --- SlackSource ---------------------------------------------------------

from datetime import datetime, timezone  # noqa: E402

OWNER = "U0000000001"
OTHER = "U0000000002"
BOT = "B0000000001"
CHAN = "C0000000001"
CHAN2 = "C0000000002"
DM = "D0000000001"
WS = "https://example.slack.com/"
NOW = datetime(2030, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
NOW_TS = NOW.timestamp()


def ts(offset_minutes: float, n: int = 0) -> str:
    return f"{NOW_TS + offset_minutes * 60:.0f}.{n:06d}"


def source_cfg(**over):
    cfg = {
        "type": "slack", "enabled": True, "base_url": "https://slack.example.com/api",
        "token_env": "FAKE_SLACK_TOKEN", "owner_id": None, "timeout_seconds": 7,
        "max_retry_wait_seconds": 5, "min_interval_seconds": 0, "max_calls_per_run": 40,
        "page_size": 200, "max_pages": 3, "initial_lookback_minutes": 60,
        "thread_lookback_hours": 0, "max_text_chars": 50,
        "schedule": {"interval_minutes": 15, "hours": "working"},
        "mentions": {
            "enabled": True, "action": "draft", "use_search": True, "search_query": "<@{owner_id}>",
            "include_direct_messages": False,
            "conversation_types": ["public_channel", "im"], "exclude_channels": [],
            "conversations_refresh_minutes": 720,
        },
        "channels": {},
    }
    for key, value in over.items():
        if key == "mentions":
            cfg["mentions"].update(value)
        else:
            cfg[key] = value
    return cfg


def make(send, **over):
    clock = Clock()
    return slack.SlackSource(
        "slack", source_cfg(**over), env=ENV, http_send=send, sleep=clock.sleep, clock=clock
    )


def auth(scopes="search:read,channels:history"):
    return ({"ok": True, "user_id": OWNER, "url": WS}, {"x-oauth-scopes": scopes})


def msg(offset, text="hello", **extra):
    return {"ts": ts(offset), "user": OTHER, "text": text, **extra}


def methods(send):
    return [c[0].rsplit("/", 1)[1] for c in send.calls]


def test_search_path_with_scope_header():
    match = {**msg(-5, f"hi <@{OWNER}>"), "channel": {"id": CHAN}, "permalink": f"{WS}archives/{CHAN}/p1"}
    send = FakeSend({
        "auth.test": [auth()],
        "search.messages": [{"ok": True, "messages": {"matches": [match], "paging": {"pages": 1}}}],
    })
    batch = make(send).fetch(None, now=NOW)
    assert methods(send) == ["auth.test", "search.messages"]
    (item,) = batch.items
    assert item.url == f"{WS}archives/{CHAN}/p1" and item.mentions == ("owner",)
    body = urllib.parse.parse_qs(send.calls[1][1].decode())
    assert body["query"] == [f"<@{OWNER}>"] and body["sort"] == ["timestamp"] and body["sort_dir"] == ["desc"]


def test_member_scan_without_search_scope_and_cursor_round_trip():
    hist = {"ok": True, "messages": [msg(-5, f"yo <@{OWNER}>"), msg(-4, "no mention")]}
    send = FakeSend({
        "auth.test": [auth("channels:history"), auth("channels:history")],
        "users.conversations": [{"ok": True, "channels": [{"id": CHAN}]}],
        "conversations.history": [hist, {"ok": True, "messages": []}],
    })
    src = make(send)
    first = src.fetch(None, now=NOW)
    assert [i.item_id for i in first.items] == [f"{CHAN}:{ts(-5)}"]
    assert "search.messages" not in methods(send)
    later = datetime.fromtimestamp(NOW_TS + 60, timezone.utc)
    second = src.fetch(first.cursor, now=later)
    assert second.items == ()
    assert methods(send).count("users.conversations") == 1  # cached
    hist_call = urllib.parse.parse_qs(send.calls[-1][1].decode())
    assert hist_call["oldest"] == [ts(-4)]


def test_first_run_respects_initial_lookback():
    send = FakeSend({
        "auth.test": [auth()],
        "conversations.history": [{"ok": True, "messages": []}],
    })
    make(send, mentions={"enabled": False}, channels={CHAN: {"rules": []}}).fetch(None, now=NOW)
    oldest = urllib.parse.parse_qs(send.calls[1][1].decode())["oldest"][0]
    assert oldest == f"{NOW_TS - 3600:.6f}"


def test_thread_replies_found_through_latest_reply():
    parent = {**msg(-120, "parent"), "latest_reply": ts(-5), "thread_ts": ts(-120)}
    reply = {"ts": ts(-5), "user": OTHER, "text": "reply", "thread_ts": ts(-120)}
    send = FakeSend({
        "auth.test": [auth()],
        "conversations.history": [{"ok": True, "messages": [parent]}],
        "conversations.replies": [{"ok": True, "messages": [parent, reply]}],
    })
    src = make(send, mentions={"enabled": False}, channels={CHAN: {"rules": []}}, thread_lookback_hours=24)
    cursor = json.dumps({"channels": {CHAN: ts(-60)}})
    batch = src.fetch(cursor, now=NOW)
    (item,) = batch.items
    assert item.thread == ts(-120) and item.reply == {"channel": CHAN, "thread_ts": ts(-120)}
    assert item.url == f"{WS}archives/{CHAN}/p{ts(-5).replace('.', '')}?thread_ts={ts(-120)}&cid={CHAN}"
    history = urllib.parse.parse_qs(send.calls[1][1].decode())
    assert float(history["oldest"][0]) < NOW_TS - 3600  # reaches back to find the thread


def test_owner_messages_skipped_and_author_kinds():
    send = FakeSend({
        "auth.test": [auth()],
        "conversations.history": [{"ok": True, "messages": [
            {"ts": ts(-1), "user": OWNER, "text": "mine"},
            {"ts": ts(-2), "bot_id": BOT, "text": "<!channel> down", "subtype": "bot_message"},
            {"ts": ts(-3), "user": OTHER, "text": "<!here> look"},
            {"ts": ts(-4), "user": OTHER, "text": "joined", "subtype": "channel_join"},
        ]}],
    })
    src = make(send, mentions={"enabled": False}, channels={CHAN: {"rules": []}})
    items = src.fetch(None, now=NOW).items
    assert [i.author for i in items] == [OTHER, BOT]
    assert [i.author_kind for i in items] == ["user", "bot"]
    assert [i.mentions for i in items] == [("here",), ("channel",)]
    assert items[0].at.endswith("Z") and items[0].container == CHAN


def test_text_is_cut():
    send = FakeSend({
        "auth.test": [auth()],
        "conversations.history": [{"ok": True, "messages": [msg(-1, "x" * 200)]}],
    })
    src = make(send, mentions={"enabled": False}, channels={CHAN: {"rules": []}})
    assert len(src.fetch(None, now=NOW).items[0].text) == 50


def test_search_pagination_stops_at_max_pages():
    matches = lambda n: {"ok": True, "messages": {"matches": [  # noqa: E731
        {**msg(-n, f"<@{OWNER}>"), "channel": {"id": CHAN}, "permalink": "p"}], "paging": {"pages": 9}}}
    send = FakeSend({"auth.test": [auth()], "search.messages": [matches(1), matches(2), matches(3)]})
    batch = make(send, max_pages=3).fetch(None, now=NOW)
    assert methods(send).count("search.messages") == 3 and len(batch.items) == 3


def test_budget_partial_batch_and_resume_least_recently_checked():
    def hist(offset):
        return {"ok": True, "messages": [msg(offset, f"<@{OWNER}>")]}

    send = FakeSend({
        "auth.test": [auth("x"), auth("x")],
        "users.conversations": [{"ok": True, "channels": [{"id": CHAN}, {"id": CHAN2}]}],
        "conversations.history": [hist(-5), hist(-4)],
    })
    # auth + users.conversations + one history = 3 calls
    first = make(send, max_calls_per_run=3).fetch(None, now=NOW)
    assert [i.container for i in first.items] == [CHAN]
    state = json.loads(first.cursor)
    assert CHAN in state["channels"] and CHAN2 not in state["channels"]
    assert list(state["member"]["checked"]) == [CHAN]
    second = make(send, max_calls_per_run=2).fetch(first.cursor, now=NOW)
    assert [i.container for i in second.items] == [CHAN2]
    assert urllib.parse.parse_qs(send.calls[-1][1].decode())["channel"] == [CHAN2]


def test_fetch_never_writes():
    send = FakeSend({
        "auth.test": [auth()],
        "search.messages": [{"ok": True, "messages": {"matches": []}}],
    })
    make(send).fetch(None, now=NOW)
    assert "chat.postMessage" not in methods(send)


def test_other_slack_error_propagates():
    send = FakeSend({"auth.test": [{"ok": False, "error": "invalid_auth"}]})
    with pytest.raises(SourceError):
        make(send).fetch(None, now=NOW)


def dm_script(scopes):
    return {
        "auth.test": [auth(scopes)],
        "search.messages": [{"ok": True, "messages": {"matches": []}}],
        "users.conversations": [{"ok": True, "channels": [{"id": DM, "is_im": True}]}],
        "conversations.history": [{"ok": True, "messages": [msg(-3, "plain hello")]}],
    }


@pytest.mark.parametrize("scopes", ["search:read", "channels:history"])
def test_direct_messages_count_as_mentions_when_enabled(scopes):
    send = FakeSend(dm_script(scopes))
    (item,) = make(send, mentions={"include_direct_messages": True}).fetch(None, now=NOW).items
    assert item.container == DM and "direct" in item.mentions


@pytest.mark.parametrize("scopes", ["search:read", "channels:history"])
def test_direct_messages_ignored_by_default(scopes):
    send = FakeSend(dm_script(scopes))
    assert make(send).fetch(None, now=NOW).items == ()


def test_rules_for():
    rules = [{"name": "r"}]
    src = make(FakeSend({}), channels={CHAN: {"rules": rules}})
    base = dict(source="slack", author=OTHER, author_kind="user", text="", at="")

    def message(container, mentions=()):
        return slack.Message(item_id=f"{container}:1", container=container, mentions=mentions, **base)

    assert src.rules_for(message(CHAN)) == (rules, "ignore")
    assert src.rules_for(message(CHAN, ("owner",))) == (rules, "draft")
    assert src.rules_for(message(CHAN2, ("owner",))) == ([], "draft")


def test_escape_text_leaves_no_markup():
    out = slack.escape_text("<!<!channel>> & <@U0000000001>")
    assert "<" not in out and ">" not in out and out.startswith("&lt;!&lt;!channel&gt;&gt; &amp;")


def test_searched_reply_takes_its_thread_from_the_permalink():
    parent = ts(-10)
    link = f"{WS}archives/{CHAN}/p1?thread_ts={parent}&cid={CHAN}"
    match = {**msg(-5, f"hi <@{OWNER}>"), "channel": {"id": CHAN}, "permalink": link}
    send = FakeSend({
        "auth.test": [auth()],
        "search.messages": [{"ok": True, "messages": {"matches": [match], "paging": {"pages": 1}}}],
    })
    item = make(send).fetch(None, now=NOW).items[0]
    assert item.thread == parent and item.reply["thread_ts"] == parent


def test_one_failing_conversation_does_not_hide_the_rest():
    send = FakeSend({
        "auth.test": [auth("x")],
        "users.conversations": [{"ok": True, "channels": [{"id": CHAN}, {"id": CHAN2}]}],
        "conversations.history": [
            {"ok": False, "error": "channel_not_found"},
            {"ok": True, "messages": [msg(-4, f"<@{OWNER}>")]},
        ],
    })
    batch = make(send).fetch(None, now=NOW)
    assert [i.container for i in batch.items] == [CHAN2]
    assert CHAN not in json.loads(batch.cursor)["member"]["ids"]


def two_conv_script(history):
    return {
        "auth.test": [auth("channels:history")],
        "users.conversations": [{"ok": True, "channels": [{"id": CHAN}, {"id": CHAN2}]}],
        "conversations.history": history,
    }


def test_member_scan_drops_only_membership_errors_and_reports_them():
    send = FakeSend(two_conv_script([
        {"ok": False, "error": "not_in_channel"},
        {"ok": True, "messages": [msg(-5, f"yo <@{OWNER}>")]},
    ]))
    batch = make(send).fetch(None, now=NOW)
    assert [i.container for i in batch.items] == [CHAN2]
    assert len(batch.errors) == 1 and CHAN in batch.errors[0]
    state = json.loads(batch.cursor)
    assert CHAN not in state["member"]["ids"] and CHAN in state["member"]["checked"]
    assert state["member"]["dropped"] == [CHAN]


def test_a_dropped_conversation_is_reported_once_per_conversations_refresh():
    send = FakeSend({
        "auth.test": [auth("channels:history")] * 3,
        "users.conversations": [{"ok": True, "channels": [{"id": CHAN}]}] * 2,
        "conversations.history": [{"ok": False, "error": "is_archived"}] * 2,
    })
    src = make(send, mentions={"conversations_refresh_minutes": 60})
    first = src.fetch(None, now=NOW)
    assert len(first.errors) == 1 and CHAN in first.errors[0]
    soon = datetime.fromtimestamp(NOW_TS + 60, timezone.utc)
    second = src.fetch(first.cursor, now=soon)
    assert second.errors == ()
    assert methods(send).count("users.conversations") == 1  # no forced relist
    assert methods(send).count("conversations.history") == 1
    after_refresh = datetime.fromtimestamp(NOW_TS + 2 * 3600, timezone.utc)
    third = src.fetch(second.cursor, now=after_refresh)
    assert len(third.errors) == 1 and CHAN in third.errors[0]
    assert methods(send).count("users.conversations") == 2


@pytest.mark.parametrize("code", ["missing_scope", "access_denied", "ratelimited"])
def test_member_scan_stops_on_other_slack_errors_and_keeps_the_batch(code):
    send = FakeSend(two_conv_script([
        {"ok": False, "error": code}, {"ok": False, "error": code}, {"ok": False, "error": code},
    ]))
    batch = make(send).fetch(None, now=NOW)
    assert batch.items == ()
    assert batch.errors == (f"slack: conversations.history failed for {CHAN}: {code}",)
    assert methods(send).count("conversations.history") <= 2  # stopped at the first failure
    state = json.loads(batch.cursor)
    assert CHAN in state["member"]["ids"] and CHAN in state["member"]["checked"]
    assert CHAN not in state["channels"]


def test_a_failing_member_conversation_keeps_the_ruled_channels_items():
    ruled = {"ok": True, "messages": [msg(-5, "deploy failed"), msg(-4, "deploy fixed")]}
    send = FakeSend({
        "auth.test": [auth("channels:history")],
        "users.conversations": [{"ok": True, "channels": [{"id": CHAN2}]}],
        "conversations.history": [ruled, {"ok": False, "error": "missing_scope", "detail": "secret text"}],
    })
    batch = make(send, channels={CHAN: {"rules": []}}).fetch(None, now=NOW)
    assert [i.container for i in batch.items] == [CHAN, CHAN]
    state = json.loads(batch.cursor)
    assert state["channels"][CHAN] == ts(-4)  # the ruled channel's cursor advanced
    assert CHAN2 not in state["channels"] and CHAN2 in state["member"]["checked"]
    assert batch.errors == (f"slack: conversations.history failed for {CHAN2}: missing_scope",)
    assert not any("deploy" in e or "secret" in e for e in batch.errors)


def test_a_network_error_on_one_member_conversation_keeps_the_batch():
    good = {"ok": True, "messages": [msg(-5, f"yo <@{OWNER}>")]}
    script = FakeSend(two_conv_script([good]))

    def send(url, body, headers, timeout):
        if url.endswith("conversations.history") and not script.script["conversations.history"]:
            raise SourceError("request to slack.example.com failed")
        return script(url, body, headers, timeout)

    batch = make(send).fetch(None, now=NOW)
    assert [i.container for i in batch.items] == [CHAN]
    assert batch.errors == (f"slack: conversations.history failed for {CHAN2}: request_failed",)
    state = json.loads(batch.cursor)
    assert CHAN in state["channels"] and CHAN2 in state["member"]["checked"]


def test_a_failing_conversation_sorts_last_next_run():
    good = {"ok": True, "messages": []}
    send = FakeSend({
        "auth.test": [auth("channels:history")] * 2,
        "users.conversations": [{"ok": True, "channels": [{"id": CHAN}, {"id": CHAN2}]}],
        "conversations.history": [{"ok": False, "error": "missing_scope"}, good, good],
    })
    src = make(send)
    first = src.fetch(None, now=NOW)
    later = datetime.fromtimestamp(NOW_TS + 60, timezone.utc)
    src.fetch(first.cursor, now=later)
    history = [urllib.parse.parse_qs(c[1].decode())["channel"][0] for c in send.calls if c[0].endswith("history")]
    assert history == [CHAN, CHAN2, CHAN]


def test_listing_failures_still_raise():
    send = FakeSend({
        "auth.test": [auth("channels:history")],
        "users.conversations": [{"ok": False, "error": "missing_scope"}],
    })
    with pytest.raises(SourceError) as info:
        make(send).fetch(None, now=NOW)
    assert "missing_scope" in str(info.value)


@pytest.mark.parametrize("code", ["missing_scope", "ratelimited"])
def test_a_listing_failure_keeps_the_ruled_channels_alerts(code):
    ruled = {"ok": True, "messages": [msg(-5, "CRITICAL disk full")]}
    send = FakeSend({
        "auth.test": [auth("channels:history")],
        # twice: the client retries a ratelimited answer once
        "users.conversations": [{"ok": False, "error": code, "detail": "secret text"}] * 2,
        "conversations.history": [ruled],
    })
    batch = make(send, channels={CHAN: {"rules": []}}).fetch(None, now=NOW)
    assert [i.container for i in batch.items] == [CHAN]
    state = json.loads(batch.cursor)
    assert state["channels"][CHAN] == ts(-5)  # the ruled channel's cursor advanced
    assert "member" not in state  # the scan stopped; the listing is retried next run
    assert batch.errors == (f"slack: users.conversations failed: {code}",)
    assert not any("CRITICAL" in e or "secret" in e for e in batch.errors)
    assert methods(send)[:3] == ["auth.test", "conversations.history", "users.conversations"]
    assert set(methods(send)[3:]) <= {"users.conversations"}


def test_a_search_failure_keeps_the_ruled_channels_alerts():
    ruled = {"ok": True, "messages": [msg(-5, "CRITICAL disk full")]}
    send = FakeSend({
        "auth.test": [auth()],
        "conversations.history": [ruled],
        "search.messages": [{"ok": False, "error": "missing_scope", "detail": "secret text"}],
    })
    batch = make(send, channels={CHAN: {"rules": []}}).fetch(None, now=NOW)
    assert [i.container for i in batch.items] == [CHAN]
    state = json.loads(batch.cursor)
    assert state["channels"][CHAN] == ts(-5) and "search" not in state
    assert batch.errors == ("slack: search.messages failed: missing_scope",)


def test_a_search_network_error_keeps_the_ruled_channels_alerts_and_scans_dms():
    ruled = {"ok": True, "messages": [msg(-5, "CRITICAL disk full")]}
    script = FakeSend({
        "auth.test": [auth()],
        "conversations.history": [ruled, {"ok": True, "messages": [msg(-3, "plain hello")]}],
        "users.conversations": [{"ok": True, "channels": [{"id": DM, "is_im": True}]}],
    })

    def send(url, body, headers, timeout):
        if url.endswith("search.messages"):
            raise SourceError("request to slack.example.com failed")
        return script(url, body, headers, timeout)

    src = make(send, channels={CHAN: {"rules": []}}, mentions={"include_direct_messages": True})
    batch = src.fetch(None, now=NOW)
    assert [i.container for i in batch.items] == [CHAN, DM]
    assert batch.errors == ("slack: search.messages failed: request_failed",)


def test_a_later_ruled_channel_failure_keeps_the_earlier_ones():
    ruled = {"ok": True, "messages": [msg(-5, "CRITICAL disk full")]}
    send = FakeSend({
        "auth.test": [auth("channels:history")],
        "conversations.history": [ruled, {"ok": False, "error": "not_in_channel"}],
    })
    cfg = {"channels": {CHAN: {"rules": []}, CHAN2: {"rules": []}}, "mentions": {"enabled": False}}
    batch = make(send, **cfg).fetch(None, now=NOW)
    assert [i.container for i in batch.items] == [CHAN]
    state = json.loads(batch.cursor)
    assert state["channels"][CHAN] == ts(-5) and CHAN2 not in state["channels"]
    assert batch.errors == (f"slack: conversations.history failed for {CHAN2}: not_in_channel",)


def test_a_search_failure_before_anything_was_read_still_raises():
    send = FakeSend({
        "auth.test": [auth()],
        "search.messages": [{"ok": False, "error": "missing_scope"}],
    })
    with pytest.raises(SourceError):
        make(send).fetch(None, now=NOW)


def test_dropped_conversations_sort_last_within_the_budget():
    cursor = json.dumps({"member": {
        "ids": [CHAN, CHAN2], "dms": [], "types": ["public_channel", "im"],
        "checked": {CHAN: "2030-01-01T00:00:00.000000Z"}, "listed_at": "2030-01-01T11:59:00.000000Z",
    }})
    send = FakeSend({"auth.test": [auth("channels:history")], "conversations.history": [{"ok": True, "messages": []}]})
    make(send, max_calls_per_run=2).fetch(cursor, now=NOW)
    assert urllib.parse.parse_qs(send.calls[-1][1].decode())["channel"] == [CHAN2]

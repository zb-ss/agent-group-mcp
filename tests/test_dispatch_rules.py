from __future__ import annotations

import pytest

from agent_bus import config
from agent_bus.dispatch import rules
from agent_bus.dispatch.messages import Message

# The cap callers pass: the shipped dispatch.incident.max_key_chars.
MAX_KEY_CHARS = config.defaults()["dispatch"]["incident"]["max_key_chars"]


def match(rule_list, default_action, message, *, max_key_chars=MAX_KEY_CHARS):
    return rules.match(rule_list, default_action, message, max_key_chars=max_key_chars)


def msg(**kw):
    base = dict(
        source="chat", item_id="1", container="C0000000001", author="B0000000001",
        author_kind="bot", text="ALERT disk full host-1", at="2030-01-02T03:04:05Z",
    )
    base.update(kw)
    return Message(**base)


@pytest.mark.parametrize(
    "rule, message, matched",
    [
        ({"bot_ids": ["B0000000001"]}, msg(), True),
        ({"bot_ids": ["B0000000001"]}, msg(author_kind="user"), False),
        ({"user_ids": ["U0000000001"]}, msg(author="U0000000001", author_kind="user"), True),
        ({"user_ids": ["U0000000001"]}, msg(author="U0000000001"), False),
        ({"bot_ids": ["B0000000001"], "user_ids": ["U0000000001"]},
         msg(author="U0000000001", author_kind="user"), True),
        ({"text": ["disk"]}, msg(), True),
        ({"text": ["^nope"]}, msg(), False),
        ({"mention": ["channel"]}, msg(mentions=("channel",)), True),
        ({"mention": ["here"]}, msg(mentions=("channel",)), False),
        ({"mention": ["owner", "here"]}, msg(mentions=("owner",)), True),
        ({"thread": "top"}, msg(), True),
        ({"thread": "top"}, msg(thread="1.0"), False),
        ({"thread": "reply"}, msg(thread="1.0"), True),
        ({"thread": "reply"}, msg(), False),
        ({"thread": "any"}, msg(thread="1.0"), True),
        ({"from": [r"@example\.com$"]},
         msg(author="Alice@Example.com", author_kind="user"), True),
        ({"from": ["^bob@"]}, msg(author="alice@example.com", author_kind="user"), False),
        ({"folder": ["INBOX"]}, msg(container="INBOX"), True),
        ({"folder": ["INBOX"]}, msg(container="Sent"), False),
        ({"subject": ["invoice"]}, msg(title="Your invoice"), True),
        ({"subject": ["invoice"]}, msg(title="hello"), False),
        ({"bot_ids": ["B0000000001"], "text": ["nope"]}, msg(), False),
    ],
)
def test_conditions(rule, message, matched):
    hit = match([{"name": "r", "action": "notify", **rule}], "ignore", message)
    assert (hit.rule == "r") is matched
    assert hit.action == ("notify" if matched else "ignore")
    assert hit.phase is None and hit.key is None


def test_first_match_wins_and_default():
    rules = [
        {"name": "a", "action": "notify", "text": ["disk"]},
        {"name": "b", "action": "draft"},
    ]
    assert match(rules, "ignore", msg()).rule == "a"
    assert match(rules[:1], "draft", msg(text="other")) == match([], "draft", msg())
    assert match([], "draft", msg()).rule is None


LIFE = {"open": ["^ALERT"], "repeat": ["STILL OPEN"], "close": ["RESOLVED"],
        "key": r"host-(\d+)"}


def rule(**kw):
    return {"name": "inc", "action": "respond", "bot_ids": ["B0000000001"],
            "lifecycle": LIFE, **kw}


def test_lifecycle_phases_and_order():
    assert match([rule()], "ignore", msg()).phase == "open"
    assert match([rule()], "ignore", msg(text="ALERT STILL OPEN host-1")).phase == "repeat"
    assert match([rule()], "ignore", msg(text="ALERT STILL OPEN RESOLVED host-1")).phase == "close"
    assert match([rule()], "ignore", msg(text="unrelated")).rule is None


def test_close_from_other_author_does_not_match():
    hit = match([rule()], "ignore", msg(text="RESOLVED host-1", author="B0000000002"))
    assert hit.rule is None and hit.action == "ignore"


def test_lifecycle_open_needs_what_conditions():
    hit = match([rule(mention=["channel"])], "ignore", msg())
    assert hit.rule is None
    hit = match([rule(mention=["channel"])], "ignore", msg(mentions=("channel",)))
    assert hit.phase == "open"
    # close and repeat do not need the what conditions
    assert match([rule(mention=["channel"])], "ignore", msg(text="RESOLVED")).phase == "close"


def test_key_with_and_without_group():
    assert match([rule()], "x", msg()).key == "chat:C0000000001:inc:1"
    whole = rule(lifecycle={**LIFE, "key": r"HOST-\d+"})
    assert match([whole], "x", msg(text="ALERT HOST-7")).key == "chat:C0000000001:inc:host-7"
    none = rule(lifecycle={"open": ["ALERT"]})
    assert match([none], "x", msg()).key == "chat:C0000000001:inc:"
    nomatch = rule(lifecycle={**LIFE, "key": r"zzz(\d)"})
    assert match([nomatch], "x", msg()).key == "chat:C0000000001:inc:"


def test_hit_carries_playbook_and_auto_post():
    hit = match([rule(playbook="incident", auto_post_report=True)], "x", msg())
    assert hit.playbook == "incident" and hit.auto_post_report is True


def test_the_key_cap_has_no_in_code_default():
    with pytest.raises(TypeError):
        rules.match([rule()], "x", msg())  # the caller must pass the configured cap
    assert match([rule()], "x", msg(), max_key_chars=1).key == "chat:C0000000001:inc:1"
    whole = rule(lifecycle={**LIFE, "key": r"HOST-\d+"})
    capped = match([whole], "x", msg(text="ALERT HOST-7"), max_key_chars=3).key
    assert capped.startswith("chat:C0000000001:inc:hos") and "host-7" not in capped

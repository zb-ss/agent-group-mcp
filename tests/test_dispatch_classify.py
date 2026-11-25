from __future__ import annotations

import pytest

from agent_bus import config, identity
from agent_bus.dispatch.classify import (
    build_label,
    classify,
    has_new_comment,
    slug,
)
from agent_bus.dispatch.model import Membership, Task
from agent_bus.dispatch.store import TaskRecord

DISPATCH = config.defaults()["dispatch"]
RULES = DISPATCH["rules"]
LABEL = DISPATCH["label"]
GID = "1000000012345678"
T1 = "2026-01-01T10:00:00Z"
T2 = "2026-01-01T11:00:00Z"


def make_task(section="Refinement", comment=None, title="Fix login page!!",
              project="p1", memberships=None):
    if memberships is None:
        memberships = (Membership(project, "Proj", section),)
    return Task("asana", GID, title, "https://example.test/t", None, None,
                comment, tuple(memberships))


def make_record(types=("refinement",), handled_at=T1, last_comment=None):
    return TaskRecord("asana", GID, "t", "u", T1, tuple(types),
                      handled_at=handled_at,
                      last_comment_handled_at=last_comment)


def run(task, record=None, rules=RULES):
    return classify(task, record, rules=rules)


def test_fresh_refinement():
    assert run(make_task()) == "refinement"


def test_handled_refinement_without_comment_ignored():
    assert run(make_task(), make_record()) == "ignore"


def test_newer_comment_is_follow_up():
    assert run(make_task(comment=T2), make_record(last_comment=T1)) == "follow-up"


def test_follow_up_repeats():
    rec = make_record(types=("refinement", "follow-up"), last_comment=T1)
    assert run(make_task(comment=T2), rec) == "follow-up"


def test_comment_not_newer():
    task = make_task(comment=T1)
    assert not has_new_comment(task, make_record(last_comment=T1))
    assert run(task, make_record(last_comment=T1)) == "ignore"


def test_handled_without_comment_marker_and_comment_present():
    assert run(make_task(comment=T1), make_record(last_comment=None)) == "follow-up"


def test_unhandled_record_not_follow_up():
    rec = make_record(types=(), handled_at=None)
    assert not has_new_comment(make_task(comment=T2), rec)
    assert run(make_task(section="Backlog", comment=T2), rec) == "ignore"


def test_implementation_once():
    assert run(make_task(section="To do")) == "implementation"
    rec = make_record(types=("implementation",))
    assert run(make_task(section="To do"), rec) == "ignore"


def test_no_matching_section():
    assert run(make_task(section="Backlog")) == "ignore"
    assert run(make_task(section=None)) == "ignore"


def test_first_match_wins():
    rules = [{"type": "a", "section": ["^X$"]}, {"type": "b", "section": ["X"]}]
    assert run(make_task(section="X"), rules=rules) == "a"


def test_title_condition():
    rules = [{"type": "bug", "title": ["(?i)^fix"]}]
    assert run(make_task(), rules=rules) == "bug"
    assert run(make_task(title="Add thing"), rules=rules) == "ignore"


PROJECT_RULE = [{"type": "timebox", "project": ["P1"],
                 "section": ["^Ready for estimate$"]}]


def test_project_scoped_rule_matches():
    task = make_task(memberships=(
        Membership("P1", "One", "Ready for estimate"),
        Membership("P2", "Two", "Customer Feedback"),
    ))
    assert run(task, rules=PROJECT_RULE) == "timebox"


def test_project_scoped_rule_ignores_other_project_section():
    task = make_task(memberships=(
        Membership("P1", "One", "Customer Feedback"),
        Membership("P2", "Two", "Ready for estimate"),
    ))
    assert run(task, rules=PROJECT_RULE) == "ignore"


def test_project_rule_without_membership():
    assert run(make_task(project="P3"), rules=PROJECT_RULE) == "ignore"


def test_rule_without_project_uses_any_membership():
    task = make_task(memberships=(
        Membership("P1", "One", "Backlog"),
        Membership("P2", "Two", "Refinement"),
    ))
    assert run(task) == "refinement"


def test_slug():
    assert slug("  Hello, World! ") == "hello-world"


def test_label_basic():
    assert build_label(make_task(), "refinement", LABEL) == "t12345678-fix-login-page"


def test_label_long_title():
    label = build_label(make_task(title="word " * 40), "refinement", LABEL)
    assert len(label) <= 24 and not label.endswith("-")
    assert "12345678" in label


@pytest.mark.parametrize("title", ["", "日本語のタイトル"])
def test_label_empty_or_non_latin(title):
    assert build_label(make_task(title=title), "x", LABEL) == "t12345678"


def test_label_shell_metacharacters():
    label = build_label(make_task(title="$(echo hi); `id` | a&b > c"), "x", LABEL)
    assert set(label) <= set("abcdefghijklmnopqrstuvwxyz0123456789-")


def test_label_must_start_with_letter():
    with pytest.raises(identity.InvalidNameError):
        build_label(make_task(), "x", {"template": "{gid_suffix}", "gid_suffix_length": 8})

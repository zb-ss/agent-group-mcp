"""A session's own remote URL as an automatic session link."""

from __future__ import annotations

import pytest

from agent_bus import links, session_link

BRIDGE_ENV = "CLAUDE_CODE_BRIDGE_SESSION_ID"
FAKE_ID = "example-bridge-id"
CFG = {
    "session_link": {
        "env": BRIDGE_ENV,
        "url": "https://remote.example/code/{id}",
        "label": "Remote Control",
    }
}
EXPECTED = {"label": "Remote Control", "url": f"https://remote.example/code/{FAKE_ID}"}


def test_from_env_builds_the_link_from_the_template():
    assert session_link.from_env(CFG, {BRIDGE_ENV: FAKE_ID}) == EXPECTED


@pytest.mark.parametrize("cfg", [None, {}, {"session_link": None}])
def test_from_env_is_none_without_a_session_link_config(cfg):
    assert session_link.from_env(cfg, {BRIDGE_ENV: FAKE_ID}) is None


@pytest.mark.parametrize(
    "env",
    [{}, {BRIDGE_ENV: ""}, {BRIDGE_ENV: "a/b"}, {BRIDGE_ENV: "a b"}, {BRIDGE_ENV: "x" * 201},
     {BRIDGE_ENV: "id\n"}],
)
def test_from_env_refuses_unset_empty_or_unsafe_values(env):
    assert session_link.from_env(CFG, env) is None


def _agent(storage, name="repo/claude-1"):
    storage.upsert_agent(name, "/repo")
    return name


def test_ensure_adds_once_and_is_idempotent(storage):
    name = _agent(storage)
    assert session_link.ensure(storage, name, EXPECTED) is True
    assert session_link.ensure(storage, name, EXPECTED) is False
    assert storage.get_agent(name).link_list == [EXPECTED]


def test_ensure_keeps_user_links_after_the_session_link(storage):
    name = _agent(storage)
    mine = {"label": "PR", "url": "https://example.com/pr/1"}
    storage.set_links(name, [mine])
    assert session_link.ensure(storage, name, EXPECTED) is True
    assert storage.get_agent(name).link_list == [EXPECTED, mine]


def test_ensure_trims_to_the_bound_and_keeps_the_session_link(storage):
    name = _agent(storage)
    many = [{"label": str(i), "url": f"https://example.com/{i}"} for i in range(links.MAX_LINKS)]
    storage.set_links(name, many)
    session_link.ensure(storage, name, EXPECTED)
    got = storage.get_agent(name).link_list
    assert len(got) == links.MAX_LINKS
    assert got[0] == EXPECTED
    assert got[1:] == many[: links.MAX_LINKS - 1]


def test_for_client_uses_the_loaded_config(monkeypatch):
    assert session_link.for_client("claude", {BRIDGE_ENV: FAKE_ID})["url"].endswith(FAKE_ID)
    assert session_link.for_client("codex", {BRIDGE_ENV: FAKE_ID}) is None
    assert session_link.for_client(None, {BRIDGE_ENV: FAKE_ID}) is None

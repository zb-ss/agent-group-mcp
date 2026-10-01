"""Agent names: `<group>`, `<group>/<client>`, `<group>/<client>-<handle>`."""

from __future__ import annotations

import pytest

from agent_bus import identity
from agent_bus.identity import Identity, InvalidNameError


@pytest.mark.parametrize(
    "name,expected",
    [
        ("repo-a", Identity(group="repo-a")),
        ("human", Identity(group="human")),
        ("repo-a/clientone", Identity(group="repo-a", client="clientone")),
        ("repo-a/claude", Identity(group="repo-a", client="claude")),
        ("repo-a/claude-1", Identity(group="repo-a", client="claude", handle="1")),
        ("repo-a/claude-2", Identity(group="repo-a", client="claude", handle="2")),
        ("repo-a/codex-99", Identity(group="repo-a", client="codex", handle="99")),
        ("repo-a/claude-frontend", Identity("repo-a", "claude", "frontend")),
        ("repo-a/codex-release-notes", Identity("repo-a", "codex", "release-notes")),
        ("repo-a/claude-v2", Identity("repo-a", "claude", "v2")),
    ],
)
def test_parse(name, expected):
    assert identity.parse(name) == expected
    assert identity.parse(name).name == name


@pytest.mark.parametrize(
    "name",
    [
        "",
        "/claude",             # no group
        "repo-a/",             # no client
        "repo-a/claude/2",     # one separator only
        "repo-a/Claude",       # clients are lowercase
        "repo-a/claude-0",     # numbers start at 1
        "repo-a/claude-01",
        "repo-a/claude-100",
        "repo-a/claude-2fa",   # a label starts with a letter
        "repo-a/claude-Frontend",
        "repo-a/claude-ai--spend",
        "repo-a/claude-ai-",
        "repo-a/claude-",
        "repo-a/claude-a_b",
        "repo-a/claude-" + "x" * 25,  # handle too long
        "repo-a/" + "c" * 13,  # client too long
    ],
)
def test_parse_rejects(name):
    with pytest.raises(InvalidNameError):
        identity.parse(name)


def test_parse_or_none_is_lenient_about_foreign_names():
    """Names already on someone's bus must never crash a lookup."""
    assert identity.parse_or_none("team/Alpha/1") is None
    assert identity.parse_or_none("repo-a/claude") == Identity("repo-a", "claude")


def test_compose_round_trips():
    assert identity.compose("repo-a", "claude") == "repo-a/claude"
    assert identity.compose("repo-a", "claude", 2) == "repo-a/claude-2"
    assert identity.compose("repo-a", "claude", "frontend") == "repo-a/claude-frontend"


def test_session_one_is_a_session_not_the_client_address():
    """Every session has a handle, the first one too: the plain name is the
    address all of a client's sessions share."""
    assert identity.compose("repo-a", "claude", 1) == "repo-a/claude-1"
    assert identity.compose("repo-a", "claude", None) == "repo-a/claude"


def test_identity_parts():
    session = identity.parse("repo-a/claude-frontend")
    assert session.is_session
    assert session.client_address == "repo-a/claude"
    assert session.label == "frontend" and session.number is None

    numbered = identity.parse("repo-a/claude-3")
    assert numbered.number == 3 and numbered.label is None

    client = identity.parse("repo-a/claude")
    assert not client.is_session
    assert client.client_address == "repo-a/claude"
    assert identity.parse("repo-a").client_address is None


@pytest.mark.parametrize(
    "raw,label",
    [("frontend", "frontend"), ("Release Notes", "release-notes"), ("api_v2.schema", "api-v2-schema"),
     ("  --Frontend--  ", "frontend"), ("x" * 30, "x" * 24)],
)
def test_normalize_label(raw, label):
    assert identity.normalize_label(raw) == label


@pytest.mark.parametrize("raw", ["", "   ", "2fa", "-", "42"])
def test_normalize_label_rejects(raw):
    with pytest.raises(InvalidNameError):
        identity.normalize_label(raw)


def test_compose_validates_its_parts():
    with pytest.raises(InvalidNameError):
        identity.compose("repo-a", "Not Valid")
    with pytest.raises(InvalidNameError):
        identity.compose("repo/a", "claude")
    with pytest.raises(InvalidNameError):
        identity.compose("repo-a", "claude", 100)


def test_a_repo_whose_name_ends_in_a_client_cannot_collide():
    """`slugify` can never produce the separator, so a repo literally
    called `repo-claude` stays distinct from client `claude` in `repo`."""
    slug = identity.slugify("repo-claude")
    assert identity.SEPARATOR not in slug
    assert identity.parse(slug) == Identity(group="repo-claude")
    assert identity.parse(slug) != identity.parse("repo/claude")


@pytest.mark.parametrize("raw", ["a/b", "a:b", "a@b", "a.b", "a b", "A_B"])
def test_slugify_never_emits_the_separator(raw):
    assert identity.SEPARATOR not in identity.slugify(raw)


def test_longest_identity_fits_the_documented_cap():
    longest = identity.compose("g" * identity.MAX_GROUP_LEN,
                               "c" * identity.MAX_CLIENT_LEN,
                               "h" * identity.MAX_HANDLE_LEN)
    assert len(longest) == identity.MAX_NAME_LEN
    assert identity.parse(longest).label == "h" * identity.MAX_HANDLE_LEN


def test_init_cmd_still_exports_slugify():
    """`init_cmd.slugify` / `MAX_NAME_LEN` were public before the move."""
    from agent_bus import init_cmd

    assert init_cmd.slugify("My Repo.dev") == "my-repo-dev"
    assert init_cmd.MAX_NAME_LEN == identity.MAX_GROUP_LEN

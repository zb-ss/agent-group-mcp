"""Agent names: `<group>`, `<group>/<client>`, `<group>/<client>-<n>`."""

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
        ("repo-a/claude-2", Identity(group="repo-a", client="claude", instance=2)),
        ("repo-a/codex-99", Identity(group="repo-a", client="codex", instance=99)),
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
        "repo-a/my-client",    # a dash would be ambiguous with the instance
        "repo-a/claude-1",     # the first session is plain `claude`
        "repo-a/claude-100",
        "repo-a/claude-x",
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


def test_compose_treats_first_instance_as_the_plain_name():
    assert identity.compose("repo-a", "claude", 1) == "repo-a/claude"
    assert identity.compose("repo-a", "claude", None) == "repo-a/claude"


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
                               "c" * identity.MAX_CLIENT_LEN, 99)
    assert len(longest) == identity.MAX_NAME_LEN
    assert identity.parse(longest).instance == 99


def test_init_cmd_still_exports_slugify():
    """`init_cmd.slugify` / `MAX_NAME_LEN` were public before the move."""
    from agent_bus import init_cmd

    assert init_cmd.slugify("My Repo.dev") == "my-repo-dev"
    assert init_cmd.MAX_NAME_LEN == identity.MAX_GROUP_LEN

"""Working out which agent a server or hook process is speaking for."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_bus import resolution
from agent_bus.resolution import IdentityError


def _repo(tmp_path: Path, name: str = "repo-a") -> Path:
    repo = tmp_path / name
    (repo / ".git").mkdir(parents=True)
    return repo


# --------------------------- explicit name -------------------------------


def test_explicit_name_is_used_verbatim(storage, tmp_path):
    env = {"AGENT_BUS_NAME": "legacy-agent-name", "AGENT_BUS_REPO": "/code/x"}
    who = resolution.resolve(storage=storage, env=env, cwd=tmp_path)
    assert (who.name, who.repo_path) == ("legacy-agent-name", "/code/x")


def test_explicit_name_wins_over_client(storage, tmp_path):
    env = {"AGENT_BUS_NAME": "repo-a/claude", "AGENT_BUS_REPO": "/code/repo-a"}
    who = resolution.resolve(storage=storage, env=env, client="codex", cwd=tmp_path)
    assert who.name == "repo-a/claude"


def test_instance_is_appended_to_an_explicit_client_name(storage, tmp_path):
    env = {
        "AGENT_BUS_NAME": "repo-a/claude",
        "AGENT_BUS_REPO": "/code/repo-a",
        "AGENT_BUS_INSTANCE": "2",
    }
    assert resolution.resolve(storage=storage, env=env, cwd=tmp_path).name == "repo-a/claude-2"


def test_first_instance_is_the_plain_name(storage, tmp_path):
    env = {"AGENT_BUS_NAME": "repo-a/claude", "AGENT_BUS_REPO": "/r", "AGENT_BUS_INSTANCE": "1"}
    assert resolution.resolve(storage=storage, env=env, cwd=tmp_path).name == "repo-a/claude"


@pytest.mark.parametrize("instance", ["0", "100", "two", "-3"])
def test_bad_instance_is_an_error(storage, tmp_path, instance):
    env = {"AGENT_BUS_NAME": "repo-a/claude", "AGENT_BUS_REPO": "/r",
           "AGENT_BUS_INSTANCE": instance}
    with pytest.raises(IdentityError):
        resolution.resolve(storage=storage, env=env, cwd=tmp_path)


def test_instance_needs_a_name_with_a_client_part(storage, tmp_path):
    """Silently sharing a mailbox is the failure this feature exists to stop."""
    env = {"AGENT_BUS_NAME": "repo-a", "AGENT_BUS_REPO": "/r", "AGENT_BUS_INSTANCE": "2"}
    with pytest.raises(IdentityError):
        resolution.resolve(storage=storage, env=env, cwd=tmp_path)


# --------------------------- derived from the client ---------------------


def test_client_plus_repo_derives_the_name(storage, tmp_path):
    repo = _repo(tmp_path, "My_Repo.dev")
    who = resolution.resolve(
        storage=storage, env={"AGENT_BUS_REPO": str(repo)}, client="codex", cwd=tmp_path
    )
    assert (who.name, who.repo_path) == ("my-repo-dev/codex", str(repo))


def test_client_can_come_from_the_environment(storage, tmp_path):
    repo = _repo(tmp_path)
    who = resolution.resolve(storage=storage, env={"AGENT_BUS_CLIENT": "agy"}, cwd=repo)
    assert who.name == "repo-a/agy"


def test_repo_is_found_from_a_subdirectory(storage, tmp_path):
    repo = _repo(tmp_path)
    deep = repo / "src" / "pkg"
    deep.mkdir(parents=True)
    who = resolution.resolve(storage=storage, env={}, client="codex", cwd=deep)
    assert (who.name, who.repo_path) == ("repo-a/codex", str(repo))


def test_repo_hint_from_a_hook_payload_beats_the_process_cwd(storage, tmp_path):
    repo = _repo(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    who = resolution.resolve(
        storage=storage, env={}, client="codex", cwd=elsewhere, repo_hint=str(repo)
    )
    assert who.name == "repo-a/codex"


def test_name_file_names_the_group(storage, tmp_path):
    repo = _repo(tmp_path)
    (repo / ".agent-bus-name").write_text("legacy-agent-name\n")
    who = resolution.resolve(storage=storage, env={}, client="codex", cwd=repo)
    assert who.name == "legacy-agent-name/codex"


def test_registered_identity_wins_over_derivation(storage, tmp_path):
    """`init` may have deconflicted the group (two repos called `foo`); the
    roster knows the real name, a hook deriving it from the path would not."""
    repo = _repo(tmp_path, "foo")
    storage.upsert_agent("projects-foo/codex", str(repo))
    who = resolution.resolve(storage=storage, env={}, client="codex", cwd=repo)
    assert who.name == "projects-foo/codex"


def test_lookup_prefers_the_first_session_over_a_numbered_one(storage, tmp_path):
    repo = _repo(tmp_path)
    storage.upsert_agent("repo-a/codex-2", str(repo))
    storage.upsert_agent("repo-a/codex", str(repo))
    who = resolution.resolve(storage=storage, env={}, client="codex", cwd=repo)
    assert who.name == "repo-a/codex"


def test_instance_applies_to_a_derived_name(storage, tmp_path):
    repo = _repo(tmp_path)
    who = resolution.resolve(
        storage=storage, env={"AGENT_BUS_INSTANCE": "3"}, client="codex", cwd=repo
    )
    assert who.name == "repo-a/codex-3"


def test_invalid_client_is_an_error(storage, tmp_path):
    with pytest.raises(IdentityError):
        resolution.resolve(storage=storage, env={}, client="Not Valid", cwd=_repo(tmp_path))


def test_nothing_to_go_on_is_an_error(storage, tmp_path):
    with pytest.raises(IdentityError):
        resolution.resolve(storage=storage, env={}, cwd=tmp_path)


# --------------------------- hooks: only known agents --------------------


def test_registered_only_returns_none_in_an_unwired_repo(storage, tmp_path):
    """A client-wide hook fires in every repo; most are not on the bus."""
    repo = _repo(tmp_path)
    who = resolution.resolve(
        storage=storage, env={}, client="codex", cwd=repo, registered_only=True
    )
    assert who is None


def test_registered_only_finds_a_wired_repo(storage, tmp_path):
    repo = _repo(tmp_path)
    storage.upsert_agent("repo-a/codex", str(repo))
    who = resolution.resolve(
        storage=storage, env={}, client="codex", cwd=repo, registered_only=True
    )
    assert who.name == "repo-a/codex"


def test_registered_only_still_trusts_an_explicit_name(storage, tmp_path):
    env = {"AGENT_BUS_NAME": "repo-a", "AGENT_BUS_REPO": "/r"}
    who = resolution.resolve(storage=storage, env=env, cwd=tmp_path, registered_only=True)
    assert who.name == "repo-a"


# --------------------------- hook payloads --------------------------------


@pytest.mark.parametrize(
    "client,payload,expected",
    [
        ("claude", {"cwd": "/code/repo-a", "session_id": "s"}, "/code/repo-a"),
        ("codex", {"cwd": "/code/repo-a", "hook_event_name": "Stop"}, "/code/repo-a"),
        ("agy", {"workspacePaths": ["/code/repo-a", "/code/other"]}, "/code/repo-a"),
        ("agy", {"workspacePaths": []}, None),
        ("opencode", {"cwd": "/code/repo-a"}, "/code/repo-a"),
        ("someclient", {"cwd": "/code/repo-a"}, "/code/repo-a"),
        ("claude", {}, None),
    ],
)
def test_repo_hint_from_payload(client, payload, expected):
    from agent_bus import clients

    dialect = clients.hook_dialect(client)
    assert dialect.repo_from_payload(payload) == expected


def test_payload_that_is_not_json_is_ignored():
    assert resolution.parse_hook_payload("not json") == {}
    assert resolution.parse_hook_payload("") == {}
    assert resolution.parse_hook_payload("[1, 2]") == {}
    assert resolution.parse_hook_payload(json.dumps({"cwd": "/x"})) == {"cwd": "/x"}

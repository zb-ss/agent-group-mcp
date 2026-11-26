from __future__ import annotations

import json

import pytest

from agent_bus import config

STRIP_ENV = [
    "CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_PID", "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_CHILD_NOTES_DIR", "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_EFFORT", "CLAUDE_PLUGIN_DATA",
]


def test_config_path_beside_db(bus_paths):
    assert config.config_path() == bus_paths["db"].parent / "config.json"


def test_missing_file_gives_defaults(bus_paths):
    assert config.load() == config.defaults()


def test_blank_file_gives_defaults(bus_paths):
    config.config_path().write_text("  \n", encoding="utf-8")
    assert config.load() == config.defaults()


def test_defaults_content():
    cfg = config.defaults()
    assert set(cfg["spawn"]["clients"]) == {"claude", "codex", "agy", "opencode"}
    assert cfg["spawn"]["max_sessions"] == 10
    assert cfg["spawn"]["strip_env"] == STRIP_ENV


def test_defaults_are_fresh_each_call():
    first = config.defaults()
    first["spawn"]["max_sessions"] = 99
    assert config.defaults()["spawn"]["max_sessions"] == 10


def test_override_keeps_other_defaults(bus_paths):
    config.config_path().write_text(json.dumps({"spawn": {"max_sessions": 2}}), encoding="utf-8")
    cfg = config.load()
    expected = config.defaults()
    expected["spawn"]["max_sessions"] = 2
    assert cfg == expected


def test_override_one_client_key_keeps_the_rest(bus_paths):
    user = {"spawn": {"clients": {"claude": {"model": ["-m", "{model}"]}}}}
    config.config_path().write_text(json.dumps(user), encoding="utf-8")
    claude = config.load()["spawn"]["clients"]["claude"]
    default = config.defaults()["spawn"]["clients"]["claude"]
    assert claude["model"] == ["-m", "{model}"]
    assert claude["command"] == default["command"]
    assert claude["prompt"] == default["prompt"]


def test_null_replaces_default_fragment(bus_paths):
    user = {"spawn": {"clients": {"claude": {"remote": None}}}}
    config.config_path().write_text(json.dumps(user), encoding="utf-8")
    assert config.load()["spawn"]["clients"]["claude"]["remote"] is None


def test_unknown_keys_are_kept(bus_paths):
    config.config_path().write_text(json.dumps({"extra": {"a": 1}}), encoding="utf-8")
    assert config.load()["extra"] == {"a": 1}


def test_malformed_json_raises_naming_path(bus_paths):
    p = config.config_path()
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(config.ConfigError, match=str(p)):
        config.load()


def test_json_array_raises_naming_path(bus_paths):
    p = config.config_path()
    p.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(config.ConfigError, match=str(p)):
        config.load()


def test_unreadable_path_raises_naming_path(bus_paths):
    # A directory exists but cannot be read as a file.
    p = bus_paths["root"] / "unreadable"
    p.mkdir()
    with pytest.raises(config.ConfigError, match=str(p)):
        config.load(path=p)


def test_default_worktree_links():
    clients = config.defaults()["spawn"]["clients"]
    assert len(clients["claude"]["worktree_links"]) == 1
    assert all(clients[c]["worktree_links"] == [] for c in ("codex", "agy", "opencode"))


def test_empty_worktree_links_is_accepted(bus_paths):
    config.config_path().write_text(
        json.dumps({"spawn": {"clients": {"claude": {"worktree_links": []}}}}), encoding="utf-8"
    )
    assert config.load()["spawn"]["clients"]["claude"]["worktree_links"] == []


@pytest.mark.parametrize("bad", [
    "x", {"source": "a", "target": "b"}, [{"source": "a"}], [{"source": 1, "target": "b"}], ["a"],
])
def test_bad_worktree_links_shape_raises(bus_paths, bad):
    config.config_path().write_text(
        json.dumps({"spawn": {"clients": {"codex": {"worktree_links": bad}}}}), encoding="utf-8"
    )
    with pytest.raises(config.ConfigError, match=r"spawn\.clients\.codex\.worktree_links"):
        config.load()


@pytest.mark.parametrize("key,bad", [
    ("resume_prompt", "claude"), ("resume_prompt", [1]), ("transcripts", ["a"]),
    ("user_wired", "-c"), ("model_env", "X"), ("model_env", {"1BAD": "x"}),
    ("model_env", {"OK": 5}), ("worktree_trust", "yes"),
    ("worktree_trust", {"enabled": "yes", "file": "f", "key": "k"}),
    ("worktree_trust", {"enabled": True, "file": "", "key": "k"}),
    ("worktree_trust", {"enabled": True, "file": "f"}),
])
def test_bad_new_client_keys_raise_naming_key(bus_paths, key, bad):
    config.config_path().write_text(
        json.dumps({"spawn": {"clients": {"claude": {key: bad}}}}), encoding="utf-8"
    )
    with pytest.raises(config.ConfigError, match=rf"spawn\.clients\.claude\.{key}"):
        config.load()


@pytest.mark.parametrize("section", [
    {"spawn": None}, {"repos": None}, {"spawn": {"max_sessions": "5"}},
    {"spawn": {"startup_grace_seconds": "120"}}, {"spawn": {"strip_env": "CLAUDECODE"}},
    {"repos": {"roots": "~/code"}}, {"spawn": {"clients": {"claude": None}}},
    {"spawn": {"clients": {"claude": {"command": "claude"}}}},
])
def test_wrongly_typed_values_raise_naming_file(bus_paths, section):
    p = config.config_path()
    p.write_text(json.dumps(section), encoding="utf-8")
    with pytest.raises(config.ConfigError, match=str(p)):
        config.load()


@pytest.mark.parametrize("template", [
    "https://{host}/{owner}/{repo}.git", "git@{host}:{path", "{0}/{path}",
])
def test_bad_clone_url_template_raises_at_load(bus_paths, template):
    config.config_path().write_text(json.dumps({"repos": {"clone_url": template}}), encoding="utf-8")
    with pytest.raises(config.ConfigError, match=r"repos\.clone_url"):
        config.load()


def test_good_clone_url_template_loads(bus_paths):
    config.config_path().write_text(
        json.dumps({"repos": {"clone_url": "https://{host}/{path}.git"}}), encoding="utf-8"
    )
    assert config.load()["repos"]["clone_url"] == "https://{host}/{path}.git"


def test_id_recheck_seconds_is_a_number(bus_paths):
    config.config_path().write_text(json.dumps({"repos": {"id_recheck_seconds": "1h"}}), encoding="utf-8")
    with pytest.raises(config.ConfigError, match="id_recheck_seconds"):
        config.load()


def test_readme_default_block_is_the_shipped_file():
    from pathlib import Path

    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
    shipped = json.dumps(config.defaults(), sort_keys=True)
    blocks = [b.split("```", 1)[0] for b in readme.split("```json\n")[1:]]
    assert any(
        json.loads(b) is not None and json.dumps(json.loads(b), sort_keys=True) == shipped
        for b in blocks if b.lstrip().startswith("{") and '"worktree_links"' in b
    )


def test_status_retention_default_is_one_day():
    assert config.defaults()["status"]["hide_done_after_seconds"] == 86400


def test_status_retention_can_be_zero_or_fractional(bus_paths):
    for value in (0, 12.5):
        config.config_path().write_text(
            json.dumps({"status": {"hide_done_after_seconds": value}}), encoding="utf-8"
        )
        assert config.load()["status"]["hide_done_after_seconds"] == value


@pytest.mark.parametrize("value", [-1, "86400", None, True])
def test_status_retention_must_be_a_non_negative_number(bus_paths, value):
    p = config.config_path()
    p.write_text(json.dumps({"status": {"hide_done_after_seconds": value}}), encoding="utf-8")
    with pytest.raises(config.ConfigError, match="status.hide_done_after_seconds"):
        config.load()


@pytest.mark.parametrize("value", [-1, "5", None, True])
def test_status_budget_must_be_a_non_negative_number(bus_paths, value):
    config.config_path().write_text(
        json.dumps({"repos": {"status_budget_seconds": value}}), encoding="utf-8"
    )
    with pytest.raises(config.ConfigError, match="status_budget_seconds"):
        config.load()


def test_status_budget_defaults_to_five_seconds():
    assert config.defaults()["repos"]["status_budget_seconds"] == 5


def test_client_resume_must_be_a_list_of_strings_or_null(bus_paths):
    path = config.config_path()
    path.write_text(json.dumps({"spawn": {"clients": {"claude": {"resume": "claude -r"}}}}),
                    encoding="utf-8")
    with pytest.raises(config.ConfigError, match=r"spawn\.clients\.claude\.resume"):
        config.load()
    path.write_text(json.dumps({"spawn": {"clients": {"claude": {"resume": None}}}}),
                    encoding="utf-8")
    assert config.load()["spawn"]["clients"]["claude"]["resume"] is None


@pytest.mark.parametrize(
    "key, value, wanted",
    [
        ("prompt_delivery", "typed", '"argv" or "paste"'),
        ("prompt_ready_seconds", "soon", "a non-negative number"),
        ("prompt_ready_seconds", -1, "a non-negative number"),
    ],
)
def test_prompt_delivery_keys_are_validated(bus_paths, key, value, wanted):
    path = config.config_path()
    path.write_text(json.dumps({"spawn": {"clients": {"opencode": {key: value}}}}), encoding="utf-8")
    with pytest.raises(config.ConfigError, match=f"spawn.clients.opencode.{key} must be {wanted}"):
        config.load()


def test_opencode_pastes_its_prompt_by_default():
    opencode = config.defaults()["spawn"]["clients"]["opencode"]
    assert opencode["prompt_delivery"] == "paste"
    assert opencode["prompt_ready_seconds"] > 0

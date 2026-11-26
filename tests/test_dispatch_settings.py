from __future__ import annotations

import copy
import json
import os
from datetime import datetime

import pytest

from agent_bus import config
from agent_bus.dispatch import envfile, settings

PROJECT = "1000000000000001"


def _load_with(bus_paths, override: dict) -> dict:
    config.config_path().write_text(json.dumps(override), encoding="utf-8")
    return config.load()


def test_defaults_load_and_validate(bus_paths):
    cfg = config.load()
    assert cfg["dispatch"] == config.defaults()["dispatch"]
    settings.validate(cfg["dispatch"])


BAD_VALUES = [
    ({"working_hours": {"days": ["mon", "funday"]}}, "dispatch.working_hours.days"),
    ({"working_hours": {"start": "9am"}}, "dispatch.working_hours.start"),
    ({"working_hours": {"start": "20:00", "end": "08:00"}}, "dispatch.working_hours.start"),
    ({"label": {"template": "{gid}"}}, "dispatch.label.template"),
    ({"label": {"template": "t-{slug}"}}, "dispatch.label.template"),
    ({"label": {"template": "t{nope}"}}, "dispatch.label.template"),
    ({"label": {"gid_suffix_length": 0}}, "dispatch.label.gid_suffix_length"),
    ({"handoff_channel": "{unknown}"}, "dispatch.handoff_channel"),
    ({"max_spawns_per_run": -1}, "dispatch.max_spawns_per_run"),
    ({"defaults": {"worktree": "-bad"}}, "dispatch.defaults.worktree"),
    ({"types": {"refinement": {"action": "explode"}}}, "dispatch.types.refinement.action"),
    ({"types": {"refinement": {"playbook": "a/b"}}}, "dispatch.types.refinement.playbook"),
    ({"rules": [{"type": "refinement", "section": ["("]}]}, "dispatch.rules[0].section"),
    ({"rules": [{"type": "nonsense"}]}, "dispatch.rules[0].type"),
    ({"rules": [{"type": "refinement", "sections": ["x"]}]}, "dispatch.rules[0]"),
    ({"rules": [{"type": "refinement", "project": "1000000000000001"}]}, "dispatch.rules[0].project"),
    ({"rules": [{"type": "refinement", "project": [""]}]}, "dispatch.rules[0].project"),
    ({"rules": [{"type": "refinement", "handled_as": ["nope"]}]}, "dispatch.rules[0].handled_as"),
    ({"notify_on": ["wating"]}, "dispatch.notify_on"),
    ({"rules": [{"type": "ignore", "new_comment": "yes"}]}, "dispatch.rules[0].new_comment"),
    (
        {"sources": {"asana": {"base_url": "http://asana.example.com"}}},
        "dispatch.sources.asana.base_url",
    ),
    ({"sources": {"asana": {"page_size": 0}}}, "dispatch.sources.asana.page_size"),
    ({"sources": {"asana": {"token_env": ""}}}, "dispatch.sources.asana.token_env"),
    (
        {"sources": {"asana": {"projects": {PROJECT: {"client": "claude"}}}}},
        f"dispatch.sources.asana.projects.{PROJECT}.repo",
    ),
    (
        {"sources": {"asana": {"projects": {PROJECT: {"repo": "x/y", "playbooks": {"refinement": "a/b"}}}}}},
        f"dispatch.sources.asana.projects.{PROJECT}.playbooks.refinement",
    ),
    ({"notifiers": {"email": {"security": "plain"}}}, "dispatch.notifiers.email.security"),
    ({"notifiers": {"email": {"enabled": True}}}, "dispatch.notifiers.email.host"),
    (
        {"notifiers": {"email": {"enabled": True, "host": "mail.example.com", "from": "a@example.com"}}},
        "dispatch.notifiers.email.to",
    ),
    ({"timer": {"name": "a/b"}}, "dispatch.timer.name"),
    ({"timer": {"interval_minutes": 0}}, "dispatch.timer.interval_minutes"),
    ({"timer": {"command": []}}, "dispatch.timer.command"),
    ({"timer": {"command": ["{other}"]}}, "dispatch.timer.command"),
    ({"soft_limit": -1}, "dispatch.soft_limit"),
    ({"soft_limit": "6"}, "dispatch.soft_limit"),
    ({"resume_max_transcript_mb": -1}, "dispatch.resume_max_transcript_mb"),
    ({"bus_sender": ""}, "dispatch.bus_sender"),
    ({"worktree_cleanup": {"enabled": "yes"}}, "dispatch.worktree_cleanup.enabled"),
    ({"notify_on": ["waiting", "bogus"]}, "dispatch.notify_on"),
    ({"types": {"follow-up": {"resume_playbook": "a/b"}}}, "dispatch.types.follow-up.resume_playbook"),
]


@pytest.mark.parametrize("override,key", BAD_VALUES)
def test_bad_values_name_the_dotted_key(bus_paths, override, key):
    with pytest.raises(config.ConfigError) as exc:
        _load_with(bus_paths, {"dispatch": override})
    assert key in str(exc.value)


def test_email_enabled_with_required_fields_is_valid(bus_paths):
    cfg = _load_with(
        bus_paths,
        {"dispatch": {"notifiers": {"email": {
            "enabled": True, "host": "mail.example.com", "from": "a@example.com",
            "to": ["b@example.com"],
        }}}},
    )
    assert cfg["dispatch"]["notifiers"]["email"]["port"] == 465


def test_project_string_shorthand_gets_defaults(bus_paths):
    cfg = _load_with(
        bus_paths,
        {"dispatch": {"sources": {"asana": {"projects": {PROJECT: "example-org/repo-a"}}}}},
    )["dispatch"]
    got = settings.project_settings(cfg, "asana", PROJECT)
    assert got == {
        "repo": "example-org/repo-a", "client": "claude", "model": None,
        "worktree": None, "fetch": False, "clone": False, "remote": True,
        "playbooks": {},
    }
    assert settings.project_settings(cfg, "asana", "1000000000000099") is None


def test_project_override_merges_over_defaults(bus_paths):
    entry = {"repo": "example-org/repo-a", "client": "codex", "fetch": True,
             "playbooks": {"refinement": "custom"}}
    cfg = _load_with(
        bus_paths,
        {"dispatch": {"sources": {"asana": {"projects": {PROJECT: entry}}}}},
    )["dispatch"]
    got = settings.project_settings(cfg, "asana", PROJECT)
    assert got["client"] == "codex" and got["fetch"] is True and got["remote"] is True
    assert settings.playbook_name(cfg, got, "refinement") == "custom"
    assert settings.playbook_name(cfg, got, "follow-up") == "follow-up"
    got["playbooks"]["x"] = "y"
    assert "x" not in cfg["sources"]["asana"]["projects"][PROJECT]["playbooks"]


def test_soft_limit_null_is_valid(bus_paths):
    cfg = _load_with(bus_paths, {"dispatch": {"soft_limit": None}})
    assert cfg["dispatch"]["soft_limit"] is None


def test_resume_playbook_reader(bus_paths):
    cfg = config.load()["dispatch"]
    assert settings.resume_playbook(cfg, "follow-up") == "follow-up-resume"
    assert settings.resume_playbook(cfg, "refinement") is None
    assert settings.resume_playbook(cfg, "unknown") is None


def test_type_action(bus_paths):
    cfg = config.load()["dispatch"]
    assert settings.type_action(cfg, "refinement") == "spawn"
    assert settings.type_action(cfg, "implementation") == "notify"
    assert settings.type_action(cfg, "unknown") == "ignore"


def test_working_hours_boundaries():
    wh = copy.deepcopy(config.defaults()["dispatch"]["working_hours"])
    assert settings.within_working_hours(wh, datetime(2026, 11, 25, 9, 0))
    assert settings.within_working_hours(wh, datetime(2026, 11, 25, 19, 0))
    assert not settings.within_working_hours(wh, datetime(2026, 11, 25, 19, 1))
    assert not settings.within_working_hours(wh, datetime(2026, 11, 25, 8, 59))
    assert not settings.within_working_hours(wh, datetime(2026, 11, 28, 12, 0))


def test_parse_hhmm():
    assert settings.parse_hhmm("09:30") == (9, 30)
    for bad in ("24:00", "9", "09:60", "ab:cd"):
        with pytest.raises(ValueError):
            settings.parse_hhmm(bad)


def test_paths_follow_the_bus_db(bus_paths):
    cfg = config.load()["dispatch"]
    root = bus_paths["root"]
    assert settings.dispatch_db_path() == root / "dispatch.db"
    assert settings.lock_path() == root / "dispatch.lock"
    assert settings.log_path(cfg) == root / "dispatch.log"
    assert settings.prompts_dir(cfg) == root / "dispatch-prompts"
    assert settings.timer_output_path(cfg) == root / "dispatch-timer.log"


def test_configured_paths_win(bus_paths):
    cfg = _load_with(
        bus_paths,
        {"dispatch": {"prompts_dir": "~/p", "notifiers": {"log": {"file": "/var/x.log"}}}},
    )["dispatch"]
    assert settings.prompts_dir(cfg) == settings.Path("~/p").expanduser()
    assert str(settings.log_path(cfg)) == "/var/x.log"


def _env_file(tmp_path, text, mode=0o600):
    path = tmp_path / "dispatch.env"
    path.write_text(text, encoding="utf-8")
    os.chmod(path, mode)
    return str(path)


def test_envfile_none_returns_base_copy():
    base = {"A": "1"}
    out = envfile.load(None, base=base)
    assert out == base and out is not base


def test_envfile_parses_and_keeps_base(tmp_path):
    path = _env_file(
        tmp_path,
        "# comment\n\nexport TOKEN_ONE=abc\nTOKEN_TWO=\"quoted value\"\n"
        "TOKEN_THREE='single'\nKEEP=from-file\nbad line\n",
    )
    out = envfile.load(path, base={"KEEP": "from-env"})
    assert out == {
        "KEEP": "from-env", "TOKEN_ONE": "abc",
        "TOKEN_TWO": "quoted value", "TOKEN_THREE": "single",
    }


def test_envfile_refuses_group_readable(tmp_path):
    path = _env_file(tmp_path, "SECRET_KEY=hunter2-value\n", mode=0o640)
    with pytest.raises(envfile.EnvFileError) as exc:
        envfile.load(path, base={})
    assert "chmod 600" in str(exc.value)
    assert path in str(exc.value)
    assert "hunter2-value" not in str(exc.value)


def test_envfile_missing(tmp_path):
    with pytest.raises(envfile.EnvFileError):
        envfile.load(str(tmp_path / "nope.env"), base={})


MORE_BAD_VALUES = [
    ({"rules": [{"type": "refinement", "project": []}]}, "dispatch.rules[0].project"),
    ({"notifiers": {"email": {"enabled": True, "host": "mail.example.com", "from": "a@example.com",
                              "to": ["b@example.com"], "username": "me"}}},
     "dispatch.notifiers.email.password_env"),
    ({"notifiers": {"email": {"enabled": True, "host": "mail.example.com", "from": "a@example.com",
                              "to": ["b@example.com"], "password_env": "MAIL_PASSWORD"}}},
     "dispatch.notifiers.email.username"),
    ({"sources": {"asana": {"comment_authors": "1000000000000050"}}},
     "dispatch.sources.asana.comment_authors"),
    ({"sources": {"asana": {"comment_authors": ["me"]}}}, "dispatch.sources.asana.comment_authors"),
]


@pytest.mark.parametrize("override,key", MORE_BAD_VALUES)
def test_more_bad_values_name_the_key(bus_paths, override, key):
    with pytest.raises(config.ConfigError) as exc:
        _load_with(bus_paths, {"dispatch": override})
    assert key in str(exc.value)


def test_comment_authors_default_is_empty_and_list_is_valid(bus_paths):
    assert config.load()["dispatch"]["sources"]["asana"]["comment_authors"] == []
    cfg = _load_with(bus_paths, {"dispatch": {"sources": {"asana": {
        "comment_authors": ["1000000000000050"]}}}})
    assert cfg["dispatch"]["sources"]["asana"]["comment_authors"] == ["1000000000000050"]

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
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
    ({"types": {"implementation": {"repos": "a/*"}}}, "dispatch.types.implementation.repos"),
    ({"types": {"implementation": {"repos": [""]}}}, "dispatch.types.implementation.repos"),
    ({"sources": {"asana": {"unmapped_repos": [""]}}}, "dispatch.sources.asana.unmapped_repos"),
    ({"dispatcher": {"client": "Bad Client"}}, "dispatch.dispatcher.client"),
    ({"dispatcher": {"label": "-x"}}, "dispatch.dispatcher.label"),
    ({"dispatcher": {"group": "Not A Slug"}}, "dispatch.dispatcher.group"),
    ({"dispatcher": {"dir": 5}}, "dispatch.dispatcher.dir"),
    ({"dispatcher": {"playbook": "a/b"}}, "dispatch.dispatcher.playbook"),
    ({"dispatcher": {"model": "-m"}}, "dispatch.dispatcher.model"),
    ({"dispatcher": {"remote": "yes"}}, "dispatch.dispatcher.remote"),
    ({"dispatcher": {"keep_alive": "yes"}}, "dispatch.dispatcher.keep_alive"),
    ({"triage": {"enabled": 1}}, "dispatch.triage.enabled"),
    ({"triage": {"fallback_after_minutes": 0}}, "dispatch.triage.fallback_after_minutes"),
    ({"routing": {"enabled": "on"}}, "dispatch.routing.enabled"),
    ({"routing": {"clients": {"claude": {"weight": "x"}}}}, "dispatch.routing.clients.claude.weight"),
    ({"routing": {"clients": {"claude": {"types": {"a": "x"}}}}}, "dispatch.routing.clients.claude.types"),
    ({"routing": {"clients": {"claude": {"per_complexity": None}}}}, "dispatch.routing.clients.claude.per_complexity"),
    ({"routing": {"clients": {"claude": {"models": [{"min_complexity": -1, "model": "m"}]}}}},
     "dispatch.routing.clients.claude.models[0].min_complexity"),
    ({"routing": {"clients": {"claude": {"models": [{"min_complexity": 1, "model": "-m"}]}}}},
     "dispatch.routing.clients.claude.models[0].model"),
    ({"routing": {"complexity": {"max": 26}}}, "dispatch.routing.complexity.max"),
    ({"routing": {"complexity": {"signals": [{"score": 1, "extra": 1}]}}}, "dispatch.routing.complexity.signals[0]"),
    ({"routing": {"complexity": {"signals": [{"score": "a"}]}}}, "dispatch.routing.complexity.signals[0].score"),
    ({"routing": {"complexity": {"signals": [{"score": 1, "title": ["("]}]}}},
     "dispatch.routing.complexity.signals[0].title"),
    ({"routing": {"overrides": [{"client": "Bad Client"}]}}, "dispatch.routing.overrides[0].client"),
    ({"routing": {"overrides": [{"client": "codex", "repo": ""}]}}, "dispatch.routing.overrides[0].repo"),
    ({"routing": {"overrides": [{"client": "codex", "model": "-m"}]}}, "dispatch.routing.overrides[0].model"),
    ({"routing": {"overrides": [{"client": "codex", "extra": 1}]}}, "dispatch.routing.overrides[0]"),
    ({"decision_model": {"mode": "loud"}}, "dispatch.decision_model.mode"),
    ({"decision_model": {"endpoint": "ftp://x"}}, "dispatch.decision_model.endpoint"),
    ({"decision_model": {"enabled": True}}, "dispatch.decision_model.model"),
    ({"decision_model": {"allow_remote": "yes"}}, "dispatch.decision_model.allow_remote"),
    ({"decision_model": {"endpoint": "https://models.example.test/x"}}, "dispatch.decision_model.endpoint"),
    ({"decision_model": {"allow_remote": True, "endpoint": "http://models.example.test/x"}},
     "dispatch.decision_model.endpoint"),
    ({"decision_model": {"timeout_seconds": 0}}, "dispatch.decision_model.timeout_seconds"),
    ({"decision_model": {"max_state_chars": 0}}, "dispatch.decision_model.max_state_chars"),
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
    assert settings.type_action(cfg, "implementation") == "spawn"
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


def test_repo_allowed_and_type_repos(bus_paths):
    assert settings.repo_allowed(None, "g/r")
    assert settings.repo_allowed(None, None)
    assert not settings.repo_allowed([], "g/r")
    assert not settings.repo_allowed(["g/*"], None)
    assert settings.repo_allowed(["g/*"], "g/r")
    assert not settings.repo_allowed(["g/*"], "h/r")
    cfg = config.load()["dispatch"]
    assert settings.type_repos(cfg, "implementation") == []
    assert settings.type_repos(cfg, "refinement") is None
    assert settings.type_repos(cfg, "unknown") is None


def test_default_project_and_unmapped(bus_paths):
    cfg = config.load()["dispatch"]
    project = settings.default_project(cfg, "g/r")
    assert project["repo"] == "g/r" and project["playbooks"] == {}
    assert project["client"] == cfg["defaults"]["client"]
    project["client"] = "changed"
    assert cfg["defaults"]["client"] != "changed"
    assert settings.unmapped_repos(cfg, "asana") == []
    assert settings.unmapped_repos(cfg, "other") == []


def test_dispatcher_paths(bus_paths):
    cfg = config.load()["dispatch"]
    assert settings.dispatcher_dir(cfg) == paths_db_dir() / "dispatcher"
    assert settings.dispatcher_token_path() == paths_db_dir() / "dispatcher.token"
    cfg = _load_with(bus_paths, {"dispatch": {"dispatcher": {"dir": "~/somewhere"}}})["dispatch"]
    assert settings.dispatcher_dir(cfg) == Path("~/somewhere").expanduser()


def paths_db_dir() -> Path:
    from agent_bus import paths

    return paths.db_path().parent


@pytest.mark.parametrize("endpoint,allow_remote", [
    ("http://localhost:11434/v1/x", False),
    ("http://127.0.0.1:11434/v1/x", False),
    ("http://[::1]:11434/v1/x", False),
    ("https://models.example.test/x", True),
])
def test_decision_model_endpoint_allowed(bus_paths, endpoint, allow_remote):
    _load_with(bus_paths, {"dispatch": {"decision_model": {
        "endpoint": endpoint, "allow_remote": allow_remote}}})


def _slack(**extra) -> dict:
    return {"sources": {"slack": extra}}


def _maildir(**extra) -> dict:
    return {"sources": {"maildir": extra}}


def _slack_rule(**extra) -> dict:
    return _slack(channels={"C0000000001": {"rules": [{"name": "r1", "action": "notify", **extra}]}})


def _respond(**extra) -> dict:
    return _slack_rule(action="respond", playbook="incident", **extra)


def _authored_respond(**extra) -> dict:
    return _respond(bot_ids=["B0000000001"], **extra)


MESSAGE_BAD_VALUES = [
    ({"schedule_tolerance_seconds": -1}, "dispatch.schedule_tolerance_seconds"),
    ({"timer": {"always": "yes"}}, "dispatch.timer.always"),
    ({"sources": {"asana": {"schedule": {"interval_minutes": 0, "hours": "always"}}}},
     "dispatch.sources.asana.schedule.interval_minutes"),
    ({"sources": {"asana": {"schedule": {"interval_minutes": 5, "hours": "sometimes"}}}},
     "dispatch.sources.asana.schedule.hours"),
    ({"sources": {"asana": {"schedule": {"interval_minutes": 5, "hours": {"days": ["mon"], "start": "9am", "end": "10:00"}}}}},
     "dispatch.sources.asana.schedule.hours.start"),
    ({"sources": {"Bad Name": {}}}, "dispatch.sources.Bad Name"),
    ({"sources": {"other": {"type": "asana"}}}, "dispatch.sources.other.type"),
    (_slack(base_url="http://slack.example.com"), "dispatch.sources.slack.base_url"),
    (_slack(token_env=""), "dispatch.sources.slack.token_env"),
    (_slack(page_size=1001), "dispatch.sources.slack.page_size"),
    (_slack(max_pages=0), "dispatch.sources.slack.max_pages"),
    (_slack(max_calls_per_run=0), "dispatch.sources.slack.max_calls_per_run"),
    (_slack(min_interval_seconds=-1), "dispatch.sources.slack.min_interval_seconds"),
    (_slack(owner_id=5), "dispatch.sources.slack.owner_id"),
    (_slack(mentions={"action": "respond"}), "dispatch.sources.slack.mentions.action"),
    (_slack(mentions={"search_query": "{other}"}), "dispatch.sources.slack.mentions.search_query"),
    (_slack(mentions={"conversation_types": []}), "dispatch.sources.slack.mentions.conversation_types"),
    (_slack(channels={"c1": {}}), "dispatch.sources.slack.channels.c1"),
    (_slack(channels={"C0000000001": {"rules": [{"name": "r", "action": "notify", "nope": 1}]}}),
     "dispatch.sources.slack.channels.C0000000001.rules[0]"),
    (_slack_rule(name="Bad Name"), ".rules[0].name"),
    (_slack_rule(action="explode"), ".rules[0].action"),
    (_slack_rule(bot_ids=[""]), ".rules[0].bot_ids"),
    (_slack_rule(user_ids="U0000000001"), ".rules[0].user_ids"),
    (_slack_rule(text=["("]), ".rules[0].text"),
    (_slack_rule(mention=["everyone"]), ".rules[0].mention"),
    (_slack_rule(thread="deep"), ".rules[0].thread"),
    (_slack_rule(playbook="incident"), ".rules[0].playbook"),
    (_slack_rule(action="respond"), ".rules[0].playbook"),
    (_slack_rule(action="draft", lifecycle={"open": ["x"]}), ".rules[0].lifecycle"),
    (_respond(lifecycle={"open": ["("]}), ".rules[0].lifecycle.open"),
    (_respond(lifecycle={"key": "("}), ".rules[0].lifecycle.key"),
    (_respond(lifecycle={"stop": ["x"]}), ".rules[0].lifecycle"),
    (_respond(auto_post_report="yes"), ".rules[0].auto_post_report"),
    (_slack_rule(auto_post_report=True), ".rules[0].auto_post_report"),
    (_slack(channels={"C0000000001": {"rules": [
        {"name": "r1", "action": "notify"}, {"name": "r1", "action": "notify"}]}}), ".rules[1].name"),
    (_maildir(folders=[]), "dispatch.sources.maildir.folders"),
    (_maildir(folders=["../x"]), "dispatch.sources.maildir.folders"),
    (_maildir(folders=["/abs"]), "dispatch.sources.maildir.folders"),
    (_maildir(drafts_folder="a/../b"), "dispatch.sources.maildir.drafts_folder"),
    (_maildir(default_action="respond"), "dispatch.sources.maildir.default_action"),
    (_maildir(link_template="http://mail.example.com/{message_id}"), "dispatch.sources.maildir.link_template"),
    (_maildir(link_template="https://mail.example.com/{other}"), "dispatch.sources.maildir.link_template"),
    (_maildir(address=3), "dispatch.sources.maildir.address"),
    (_maildir(enabled=True), "dispatch.sources.maildir.root"),
    (_maildir(rules=[{"name": "r", "action": "respond"}]), "email never starts a session"),
    (_maildir(rules=[{"name": "r", "action": "draft", "playbook": "x"}]), "dispatch.sources.maildir.rules[0]"),
    (_maildir(rules=[{"name": "r", "action": "draft", "from": ["("]}]), ".rules[0].from"),
    (_maildir(rules=[{"name": "r", "action": "draft", "folder": "INBOX"}]), ".rules[0].folder"),
    ({"scanner": {"enabled": "yes"}}, "dispatch.scanner.enabled"),
    ({"scanner": {"client": "nope"}}, "dispatch.scanner.client"),
    ({"scanner": {"model": "-m"}}, "dispatch.scanner.model"),
    ({"scanner": {"playbook": "a/b"}}, "dispatch.scanner.playbook"),
    ({"scanner": {"batch_size": 0}}, "dispatch.scanner.batch_size"),
    ({"scanner": {"timeout_seconds": 0}}, "dispatch.scanner.timeout_seconds"),
    ({"scanner": {"dir": 4}}, "dispatch.scanner.dir"),
    ({"scanner": {"clients": {"opencode": {"command": []}}}}, "dispatch.scanner.clients.opencode.command"),
    ({"scanner": {"clients": {"opencode": {"command": ["x", "{other}", "{prompt_file}"]}}}},
     "dispatch.scanner.clients.opencode.command"),
    ({"scanner": {"clients": {"opencode": {"command": ["x"]}}}}, "dispatch.scanner.clients.opencode.command"),
    ({"scanner": {"clients": {"opencode": {"instruction": 1}}}}, "dispatch.scanner.clients.opencode.instruction"),
    ({"scanner": {"clients": {"opencode": {"pass_env": ["AGENT_BUS_SESSION"]}}}},
     "dispatch.scanner.clients.opencode.pass_env"),
    ({"scanner": {"clients": {"opencode": {"pass_env": ["bad name"]}}}},
     "dispatch.scanner.clients.opencode.pass_env"),
    ({"scanner": {"clients": {"opencode": {"env": "x"}}}}, "dispatch.scanner.clients.opencode.env"),
    ({"incident": {"client": "Bad Client"}}, "dispatch.incident.client"),
    ({"incident": {"model": "-m"}}, "dispatch.incident.model"),
    ({"incident": {"group": "Not A Slug"}}, "dispatch.incident.group"),
    ({"incident": {"label_prefix": "-x"}}, "dispatch.incident.label_prefix"),
    ({"incident": {"remote": "yes"}}, "dispatch.incident.remote"),
    ({"incident": {"max_spawns_per_hour": -1}}, "dispatch.incident.max_spawns_per_hour"),
    ({"incident": {"max_running": -1}}, "dispatch.incident.max_running"),
    ({"incident": {"max_open_hours": 0}}, "dispatch.incident.max_open_hours"),
    ({"incident": {"max_alert_chars": 0}}, "dispatch.incident.max_alert_chars"),
    ({"incident": {"max_report_chars": 0}}, "dispatch.incident.max_report_chars"),
    ({"incident": {"tools_hint": ""}}, "dispatch.incident.tools_hint"),
    ({"incident": {"dir": 3}}, "dispatch.incident.dir"),
    ({"incident": {"max_key_chars": 0}}, "dispatch.incident.max_key_chars"),
    ({"incident": {"max_name_chars": 0}}, "dispatch.incident.max_name_chars"),
    ({"incident": {"max_time_chars": "60"}}, "dispatch.incident.max_time_chars"),
    ({"incident": {"max_url_chars": 1.5}}, "dispatch.incident.max_url_chars"),
    ({"message_retention_days": 0}, "dispatch.message_retention_days"),
    ({"message_retention_days": 1.5}, "dispatch.message_retention_days"),
    ({"drafts": {"sending_stale_minutes": 0}}, "dispatch.drafts.sending_stale_minutes"),
    ({"drafts": {"sending_stale_minutes": "30"}}, "dispatch.drafts.sending_stale_minutes"),
    ({"drafts": None}, "dispatch.drafts"),
    (_slack(mentions={"include_direct_messages": "no"}), "dispatch.sources.slack.mentions.include_direct_messages"),
    (_respond(), ".rules[0].bot_ids"),
    (_respond(bot_ids=[]), ".rules[0].bot_ids"),
    (_respond(bot_ids=[], user_ids=[]), "needs an author condition"),
    (_slack_rule(lifecycle={"open": ["ALERT"], "close": ["RESOLVED"]}), "close an incident"),
]


@pytest.mark.parametrize("override,key", MESSAGE_BAD_VALUES)
def test_message_source_bad_values(bus_paths, override, key):
    with pytest.raises(config.ConfigError) as exc:
        _load_with(bus_paths, {"dispatch": override})
    assert key in str(exc.value)


def test_message_defaults_are_off_and_valid(bus_paths):
    cfg = config.load()["dispatch"]
    settings.validate(cfg)
    assert cfg["sources"]["slack"]["enabled"] is False
    assert cfg["sources"]["maildir"]["enabled"] is False
    assert cfg["scanner"]["enabled"] is False
    assert cfg["timer"]["always"] is False
    assert cfg["sources"]["asana"]["schedule"] is None


def test_valid_slack_respond_rule_and_other_instance(bus_paths):
    cfg = _load_with(bus_paths, {"dispatch": {"sources": {"slack-alerts": {
        "type": "slack", "enabled": True,
        "channels": {"C0000000001": {"rules": [{
            "name": "critical", "action": "respond", "bot_ids": ["B0000000001"],
            "mention": ["channel"], "thread": "top", "playbook": "incident",
            "lifecycle": {"open": ["^ALERT"], "repeat": ["STILL OPEN"], "close": ["RESOLVED"], "key": "(\\w+)"},
            "auto_post_report": False,
        }]}},
    }}}})["dispatch"]
    assert cfg["sources"]["slack-alerts"]["type"] == "slack"


@pytest.mark.parametrize("author", [{"bot_ids": ["B0000000001"]}, {"user_ids": ["U0000000002"]}])
def test_respond_and_closing_rules_with_an_author_are_valid(bus_paths, author):
    _load_with(bus_paths, {"dispatch": _respond(**author)})
    _load_with(bus_paths, {"dispatch": _slack_rule(lifecycle={"open": ["A"], "close": ["R"]}, **author)})


def test_a_notify_rule_without_a_close_pattern_needs_no_author(bus_paths):
    _load_with(bus_paths, {"dispatch": _slack_rule(lifecycle={"open": ["ALERT"]})})


def test_new_message_keys_have_documented_defaults(bus_paths):
    cfg = config.load()["dispatch"]
    assert cfg["sources"]["slack"]["mentions"]["include_direct_messages"] is False
    assert cfg["drafts"]["sending_stale_minutes"] == 30
    assert cfg["message_retention_days"] == 30
    assert cfg["incident"]["max_key_chars"] == 200
    caps = {k: cfg["incident"][k] for k in ("max_name_chars", "max_time_chars", "max_url_chars")}
    assert caps == {"max_name_chars": 120, "max_time_chars": 60, "max_url_chars": 500}


def test_source_settings_inherits_packaged_type_defaults(bus_paths):
    cfg = _load_with(bus_paths, {"dispatch": {"sources": {
        "slack": {"base_url": "https://edited.example.com/api"},
        "slack-alerts": {"type": "slack", "enabled": True, "schedule": {"interval_minutes": 2, "hours": "always"}},
    }}})["dispatch"]
    merged = settings.source_settings(cfg, "slack-alerts")
    packaged = config.defaults()["dispatch"]["sources"]["slack"]
    assert merged["enabled"] is True
    assert merged["base_url"] == packaged["base_url"]
    assert merged["schedule"] == {"interval_minutes": 2, "hours": "always"}
    assert merged["mentions"] == packaged["mentions"]
    merged["mentions"]["action"] = "ignore"
    assert cfg["sources"]["slack-alerts"].get("mentions") is None
    assert settings.source_settings(cfg, "slack")["base_url"] == "https://edited.example.com/api"
    assert settings.source_type("x", {}) == "x"
    assert settings.source_type("x", {"type": "slack"}) == "slack"


def test_scan_paths(bus_paths):
    cfg = config.load()["dispatch"]
    assert settings.incident_dir(cfg) == paths_db_dir() / "incidents"
    assert settings.scanner_dir(cfg) == paths_db_dir() / "dispatch-scanner"
    assert settings.scan_lock_path() == paths_db_dir() / "dispatch-scan.lock"


WORKING = {"days": ["mon", "tue", "wed", "thu", "fri"], "start": "09:00", "end": "19:00"}
MONDAY_NOON = datetime(2030, 1, 7, 12, 0)
SUNDAY_NOON = datetime(2030, 1, 6, 12, 0)


def _due(schedule, last_run_at=None, *, now=None, local=MONDAY_NOON, tolerance=30):
    from datetime import timezone

    now = now or local.replace(tzinfo=timezone.utc)
    return settings.schedule_due(
        schedule, working_hours=WORKING, last_run_at=last_run_at, now=now, local=local,
        tolerance_seconds=tolerance,
    )


def test_schedule_due_null_follows_working_hours():
    assert _due(None) is True
    assert _due(None, local=SUNDAY_NOON) is False


def test_schedule_due_working_always_and_window():
    working = {"interval_minutes": 15, "hours": "working"}
    always = {"interval_minutes": 15, "hours": "always"}
    window = {"interval_minutes": 15, "hours": {"days": ["sun"], "start": "10:00", "end": "14:00"}}
    assert _due(working) is True
    assert _due(working, local=SUNDAY_NOON) is False
    assert _due(always, local=SUNDAY_NOON) is True
    assert _due(window, local=SUNDAY_NOON) is True
    assert _due(window) is False


def test_schedule_due_interval_and_tolerance():
    from datetime import timedelta, timezone

    schedule = {"interval_minutes": 15, "hours": "always"}
    now = MONDAY_NOON.replace(tzinfo=timezone.utc)

    def stamp(seconds_ago: float) -> str:
        return (now - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    assert _due(schedule, stamp(600), now=now) is False
    assert _due(schedule, stamp(900 - 31), now=now) is False
    assert _due(schedule, stamp(900 - 30), now=now) is True
    assert _due(schedule, stamp(1000), now=now) is True
    assert _due(schedule, "garbage", now=now) is True
    assert _due(schedule, None, now=now) is True


# ------------------------------ M4 keys ---------------------------------------

NEW_BAD_VALUES = [
    ({"window_cleanup": {"enabled": "yes"}}, "dispatch.window_cleanup.enabled"),
    ({"window_cleanup": {"idle_minutes": 0}}, "dispatch.window_cleanup.idle_minutes"),
    ({"window_cleanup": {"max_per_run": 1.5}}, "dispatch.window_cleanup.max_per_run"),
    ({"notifiers": {"linux": {"enabled": 1}}}, "dispatch.notifiers.linux.enabled"),
    ({"notifiers": {"linux": {"notify_send_bin": ""}}}, "dispatch.notifiers.linux.notify_send_bin"),
    ({"notifiers": {"linux": {"timeout_seconds": "x"}}}, "dispatch.notifiers.linux.timeout_seconds"),
    ({"routing": {"quota": {"enabled": "on"}}}, "dispatch.routing.quota.enabled"),
    ({"routing": {"quota": {"weight": 1.5}}}, "dispatch.routing.quota.weight"),
    ({"routing": {"quota": {"exhausted_factor": -0.1}}}, "dispatch.routing.quota.exhausted_factor"),
    ({"routing": {"quota": {"min_remaining_percent": 101}}}, "dispatch.routing.quota.min_remaining_percent"),
    ({"routing": {"quota": {"max_age_minutes": -1}}}, "dispatch.routing.quota.max_age_minutes"),
    ({"routing": {"quota": {"snapshot_dir": 3}}}, "dispatch.routing.quota.snapshot_dir"),
    ({"routing": {"quota": {"clients": {"claude": {"reader": "http"}}}}}, "dispatch.routing.quota.clients.claude.reader"),
    ({"routing": {"quota": {"clients": {"codex": {"max_tail_bytes": 10}}}}}, "dispatch.routing.quota.clients.codex.max_tail_bytes"),
    ({"routing": {"quota": {"clients": {"codex": {"max_files": 0}}}}}, "dispatch.routing.quota.clients.codex.max_files"),
    ({"sources": {"github": {"base_url": "http://api.example.com"}}}, "dispatch.sources.github.base_url"),
    ({"sources": {"github": {"web_base_url": "ftp://x"}}}, "dispatch.sources.github.web_base_url"),
    ({"sources": {"github": {"token_env": "not a name"}}}, "dispatch.sources.github.token_env"),
    ({"sources": {"github": {"page_size": 101}}}, "dispatch.sources.github.page_size"),
    ({"sources": {"github": {"max_pages": 0}}}, "dispatch.sources.github.max_pages"),
    ({"sources": {"github": {"max_calls_per_run": 0}}}, "dispatch.sources.github.max_calls_per_run"),
    ({"sources": {"github": {"max_untrusted_alerts_per_run": -1}}}, "dispatch.sources.github.max_untrusted_alerts_per_run"),
    ({"sources": {"github": {"max_untrusted_alerts_per_run": True}}}, "dispatch.sources.github.max_untrusted_alerts_per_run"),
    ({"sources": {"github": {"state": "merged"}}}, "dispatch.sources.github.state"),
    ({"sources": {"github": {"updated_within_days": -1}}}, "dispatch.sources.github.updated_within_days"),
    ({"sources": {"github": {"comment_lookback_days": 1.5}}}, "dispatch.sources.github.comment_lookback_days"),
    ({"sources": {"github": {"owner_login": 5}}}, "dispatch.sources.github.owner_login"),
    ({"sources": {"github": {"trusted_authors": ["a b"]}}}, "dispatch.sources.github.trusted_authors"),
    ({"sources": {"github": {"failing_conclusions": [""]}}}, "dispatch.sources.github.failing_conclusions"),
    ({"sources": {"github": {"schedule": {"interval_minutes": 0, "hours": "always"}}}}, "dispatch.sources.github.schedule.interval_minutes"),
    ({"sources": {"github": {"projects": {"no-slash": "r"}}}}, "dispatch.sources.github.projects.no-slash"),
    ({"sources": {"github": {"projects": {"owner/..": "r"}}}}, "dispatch.sources.github.projects.owner/.."),
    ({"sources": {"github": {"projects": {"../name": "r"}}}}, "dispatch.sources.github.projects.../name"),
    ({"sources": {"github": {"projects": {"example-owner/example-project": {"repo": ""}}}}}, "sources.github.projects.example-owner/example-project.repo"),
]


@pytest.mark.parametrize("override, key", NEW_BAD_VALUES)
def test_new_bad_values_name_their_key(bus_paths, override, key):
    with pytest.raises(config.ConfigError, match=key.replace(".", r"\.")):
        config.validate_user({"dispatch": override})


def test_notify_type_needs_no_playbook_but_spawn_does(bus_paths):
    config.validate_user({"dispatch": {"types": {"heads-up": {"action": "notify", "playbook": None}}}})
    config.validate_user({"dispatch": {"types": {"heads-up": {"action": "ignore"}}}})
    with pytest.raises(config.ConfigError, match="types.heads-up.playbook"):
        config.validate_user({"dispatch": {"types": {"heads-up": {"action": "spawn"}}}})
    with pytest.raises(config.ConfigError, match="types.heads-up.playbook"):
        config.validate_user({"dispatch": {"types": {"heads-up": {"action": "notify", "playbook": "A/b"}}}})


def _github(**rule):
    base = {"name": "r1", "type": "notice", "repos": ["example-owner/example-project"]}
    return {"dispatch": {"sources": {"github": {"rules": [{**base, **rule}]}}}}


def test_github_rules_valid(bus_paths):
    merged = config.validate_user(_github(
        kind="pull_request", state="open", labels=["bug"], assignee=["a"], author=["b"],
        draft=False, review_requested=["c"], failing_checks=True,
    ))
    assert merged["dispatch"]["sources"]["github"]["rules"][0]["name"] == "r1"
    config.validate_user(_github(type="ignore"))


@pytest.mark.parametrize("rule, key", [
    ({"nope": 1}, r"rules\[0\]"),
    ({"name": "Bad Name"}, r"rules\[0\]\.name"),
    ({"type": "missing"}, r"rules\[0\]\.type"),
    ({"repos": []}, r"rules\[0\]\.repos"),
    ({"repos": ["no-slash"]}, r"rules\[0\]\.repos"),
    ({"repos": ["owner/.."]}, r"rules\[0\]\.repos"),
    ({"kind": "discussion"}, r"rules\[0\]\.kind"),
    ({"state": "all"}, r"rules\[0\]\.state"),
    ({"labels": []}, r"rules\[0\]\.labels"),
    ({"author": [""]}, r"rules\[0\]\.author"),
    ({"draft": "no"}, r"rules\[0\]\.draft"),
    ({"kind": "issue", "draft": True}, r"rules\[0\]\.draft"),
    ({"kind": "issue", "review_requested": ["a"]}, r"rules\[0\]\.review_requested"),
    ({"kind": "issue", "failing_checks": True}, r"rules\[0\]\.failing_checks"),
])
def test_github_rule_errors(bus_paths, rule, key):
    with pytest.raises(config.ConfigError, match=key):
        config.validate_user(_github(**rule))


def test_github_rule_names_must_be_unique(bus_paths):
    rule = {"name": "same", "type": "notice", "repos": ["example-owner/example-project"]}
    user = {"dispatch": {"sources": {"github": {"rules": [rule, dict(rule)]}}}}
    with pytest.raises(config.ConfigError, match=r"rules\[1\]\.name"):
        config.validate_user(user)


def test_quota_dir_defaults_beside_the_db(bus_paths):
    cfg = config.defaults()["dispatch"]
    assert settings.quota_dir(cfg) == bus_paths["db"].parent / "quota"
    cfg["routing"]["quota"]["snapshot_dir"] = "/somewhere/q"
    assert settings.quota_dir(cfg) == Path("/somewhere/q")


def test_task_new_fields_default_to_inert():
    from agent_bus.dispatch.model import Task

    task = Task("s", "1", "t", "u", None, None, None)
    assert task.type_hint is None and task.spawn_allowed is True


def test_github_rule_without_repos_applies_everywhere_and_is_valid(bus_paths):
    rule = {"name": "all-repos", "type": "notice"}
    merged = config.validate_user({"dispatch": {"sources": {"github": {"rules": [rule]}}}})
    assert "repos" not in merged["dispatch"]["sources"]["github"]["rules"][0]


def test_github_untrusted_alert_cap_default_and_zero(bus_paths):
    assert config.defaults()["dispatch"]["sources"]["github"]["max_untrusted_alerts_per_run"] == 3
    config.validate_user({"dispatch": {"sources": {"github": {"max_untrusted_alerts_per_run": 0}}}})

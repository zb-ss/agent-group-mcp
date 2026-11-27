"""`agent-bus dispatch github add | remove | list`."""

from __future__ import annotations

import json
import os
import stat

from agent_bus import cli, config

SLUG = "example-owner/example-project"


def _write(data) -> None:
    path = config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")


def _user() -> dict:
    return json.loads(config.config_path().read_text(encoding="utf-8"))


def test_add_creates_a_private_file_with_only_the_new_keys(capsys):
    assert cli.main(["dispatch", "github", "add", SLUG]) == 0
    path = config.config_path()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert _user() == {"dispatch": {"sources": {"github": {"projects": {
        SLUG: {"repo": f"github.com/{SLUG}"}}}}}}
    out = capsys.readouterr().out
    assert f"watching {SLUG} (repo github.com/{SLUG})" in out
    assert "set dispatch.sources.github.enabled to true in config.json" in out
    assert "no rules yet: add dispatch.sources.github.rules" in out


def test_add_keeps_unrelated_user_keys():
    _write({"dispatch": {"sources": {"github": {"enabled": True}}}, "other": {"keep": 1}})
    assert cli.main(["dispatch", "github", "add", SLUG, "--repo", "local-name"]) == 0
    user = _user()
    assert user["other"] == {"keep": 1}
    assert user["dispatch"]["sources"]["github"]["enabled"] is True
    assert user["dispatch"]["sources"]["github"]["projects"] == {SLUG: {"repo": "local-name"}}


def test_add_twice_exits_1(capsys):
    assert cli.main(["dispatch", "github", "add", SLUG]) == 0
    before = config.config_path().read_bytes()
    assert cli.main(["dispatch", "github", "add", SLUG]) == 1
    assert "already watched" in capsys.readouterr().err
    assert config.config_path().read_bytes() == before


def test_invalid_slug_leaves_the_file_untouched():
    _write('{"dispatch": {}}')
    before = config.config_path().read_bytes()
    for bad in ("noslash", "owner/..", "../repo", "owner/my repo"):
        assert cli.main(["dispatch", "github", "add", bad]) == 1
        assert config.config_path().read_bytes() == before


def test_add_to_another_source_writes_the_type():
    assert cli.main(["dispatch", "github", "add", SLUG, "--source", "oss"]) == 0
    assert _user()["dispatch"]["sources"]["oss"]["type"] == "github"
    assert SLUG in _user()["dispatch"]["sources"]["oss"]["projects"]


def test_invalid_user_file_is_not_written(capsys):
    _write('{"dispatch": {"sources": {"github": {"max_pages": "many"}}}}')
    before = config.config_path().read_bytes()
    assert cli.main(["dispatch", "github", "add", SLUG]) == 1
    assert config.config_path().read_bytes() == before
    assert capsys.readouterr().err


def test_remove_deletes_only_that_key(capsys):
    other = "example-owner/other-project"
    _write({"dispatch": {"sources": {"github": {"projects": {
        SLUG: {"repo": "a"}, other: {"repo": "b"}}}}}})
    assert cli.main(["dispatch", "github", "remove", SLUG]) == 0
    assert _user()["dispatch"]["sources"]["github"]["projects"] == {other: {"repo": "b"}}
    before = config.config_path().read_bytes()
    assert cli.main(["dispatch", "github", "remove", SLUG]) == 1
    assert "not in config.json" in capsys.readouterr().err
    assert config.config_path().read_bytes() == before


def test_list_text_and_json(capsys):
    _write({"dispatch": {"sources": {"github": {"enabled": True, "projects": {SLUG: {"repo": "r"}}}}}})
    assert cli.main(["dispatch", "github", "list"]) == 0
    out = capsys.readouterr().out
    assert "github: enabled, 0 rule(s), 1 repo(s)" in out
    assert f"{SLUG} (repo r)" in out
    assert cli.main(["dispatch", "github", "list", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows == [{"source": "github", "enabled": True, "rules": 0,
                     "repos": [{"slug": SLUG, "repo": "r"}]}]

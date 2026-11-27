"""`agent-bus dispatch github add | remove | list`: edit the watched repositories
of a GitHub source in the user config file. No network call, no token read."""

from __future__ import annotations

import argparse
import json
from urllib.parse import urlsplit

DEFAULT_SOURCE = "github"


def _err(message: str) -> None:
    from .cli import _err as err

    err(message)


def _out(text: str) -> None:
    from .cli import _out as out

    out(text)


def _valid_slug(slug: str) -> bool:
    from . import settings

    if settings.GITHUB_SLUG.fullmatch(slug) is None:
        return False
    return all(part not in (".", "..") for part in slug.split("/"))


def _effective_source(cfg: dict, name: str) -> dict | None:
    from . import settings

    sources = cfg["dispatch"]["sources"]
    if name not in sources:
        return None
    return settings.source_settings(cfg["dispatch"], name)


def _default_repo(cfg: dict, source: dict | None, slug: str) -> str:
    # repo_identity matches a project's repo against local repo ids such as
    # github.com/owner/name, so the web host plus the slug is a valid id.
    packaged = cfg["dispatch"]["sources"][DEFAULT_SOURCE]
    base = (source or packaged)["web_base_url"]
    host = urlsplit(base).hostname or ""
    return f"{host}/{slug}"


def _user_source(user: dict, name: str, *, create: bool) -> dict | None:
    node = user
    for key in ("dispatch", "sources"):
        child = node.get(key)
        if not isinstance(child, dict):
            if not create:
                return None
            child = node[key] = {}
        node = child
    entry = node.get(name)
    if not isinstance(entry, dict):
        if not create:
            return None
        entry = node[name] = {}
        if name != DEFAULT_SOURCE:
            entry["type"] = "github"
    return entry


def cmd_add(args: argparse.Namespace) -> int:
    from .. import config

    slug, name = args.slug, args.source
    if not _valid_slug(slug):
        _err(f"invalid repository slug {slug!r}: expected owner/name")
        return 1
    try:
        cfg = config.load()
        user = config.load_user()
        source = _effective_source(cfg, name)
        if source is not None:
            if source.get("type", name) != "github":
                _err(f"source {name!r} is not a github source")
                return 1
            if slug in source.get("projects", {}):
                _err(f"{slug} is already watched by source {name!r}")
                return 1
        repo = args.repo or _default_repo(cfg, source, slug)
        entry = _user_source(user, name, create=True)
        projects = entry.get("projects")
        if not isinstance(projects, dict):
            projects = entry["projects"] = {}
        projects[slug] = {"repo": repo}
        config.save_user(user)
    except (config.ConfigError, OSError) as e:
        _err(str(e))
        return 1
    _out(f"watching {slug} (repo {repo})")
    merged = _effective_source(config.load(), name) or {}
    if not merged.get("enabled"):
        _out(f"source {name!r} is disabled: set dispatch.sources.{name}.enabled to true in config.json")
    if not merged.get("rules"):
        _out(f"no rules yet: add dispatch.sources.{name}.rules")
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    from .. import config

    slug, name = args.slug, args.source
    try:
        user = config.load_user()
        entry = _user_source(user, name, create=False)
        projects = entry.get("projects") if entry else None
        if not isinstance(projects, dict) or slug not in projects:
            _err(f"{slug} is not in config.json under dispatch.sources.{name}.projects")
            return 1
        del projects[slug]
        config.save_user(user)
    except (config.ConfigError, OSError) as e:
        _err(str(e))
        return 1
    _out(f"no longer watching {slug} (source {name!r})")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    from .. import config

    try:
        cfg = config.load()
    except (config.ConfigError, OSError) as e:
        _err(str(e))
        return 1
    rows = []
    for name in cfg["dispatch"]["sources"]:
        if args.source and name != args.source:
            continue
        source = _effective_source(cfg, name)
        if source is None or source.get("type", name) != "github":
            continue
        rows.append({
            "source": name,
            "enabled": bool(source.get("enabled")),
            "rules": len(source.get("rules") or []),
            "repos": [
                {"slug": slug, "repo": entry.get("repo")}
                for slug, entry in source.get("projects", {}).items()
            ],
        })
    if args.json:
        _out(json.dumps(rows, indent=2))
        return 0
    if not rows:
        _out("(no github sources)")
        return 0
    for row in rows:
        state = "enabled" if row["enabled"] else "disabled"
        _out(f"{row['source']}: {state}, {row['rules']} rule(s), {len(row['repos'])} repo(s)")
        for item in row["repos"]:
            _out(f"  {item['slug']} (repo {item['repo']})")
    return 0


def register(dsub) -> None:
    p = dsub.add_parser("github", help="Add, remove or list watched GitHub repositories.")
    gsub = p.add_subparsers(dest="github_command", required=True)

    s = gsub.add_parser(
        "add",
        help="Watch a repository (never enables the source or writes rules).",
        description=(
            "Add owner/name to dispatch.sources.NAME.projects in config.json. The repo id "
            "defaults to <web host>/owner/name, which matches the local clone's remote."
        ),
    )
    s.add_argument("slug", metavar="OWNER/REPO")
    s.add_argument("--source", default=DEFAULT_SOURCE, metavar="NAME", help="Source name (default github).")
    s.add_argument("--repo", metavar="REPO", help="Local repo id or name to map the repository to.")
    s.set_defaults(func=cmd_add)

    s = gsub.add_parser("remove", help="Stop watching a repository listed in config.json.")
    s.add_argument("slug", metavar="OWNER/REPO")
    s.add_argument("--source", default=DEFAULT_SOURCE, metavar="NAME", help="Source name (default github).")
    s.set_defaults(func=cmd_remove)

    s = gsub.add_parser("list", help="List GitHub sources and their watched repositories.")
    s.add_argument("--source", metavar="NAME", help="Only this source.")
    s.add_argument("--json", action="store_true", help="Emit JSON.")
    s.set_defaults(func=cmd_list)

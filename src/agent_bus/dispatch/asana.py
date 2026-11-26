"""Asana task source (REST API, stdlib only)."""

from __future__ import annotations

import json
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping

from . import source
from .model import Membership, Task, to_timestamp
from .source import HttpGet, SourceError

TASK_FIELDS = (
    "name,completed,due_on,due_at,modified_at,permalink_url,"
    "memberships.project.gid,memberships.project.name,memberships.section.name"
)
STORY_FIELDS = "created_at,resource_subtype,created_by.gid"
MAX_PAGES_KEY = "dispatch.sources.asana.max_pages"


class AsanaSource:
    name = "asana"

    def __init__(
        self,
        cfg: Mapping,
        *,
        env: Mapping[str, str],
        http_get: HttpGet | None = None,
    ) -> None:
        self._cfg = cfg
        self.schedule = cfg.get("schedule")
        self._env = env
        self._http_get = http_get
        self._headers: dict[str, str] = {}

    # -- HTTP -----------------------------------------------------------

    def _get(self, path: str, params: Mapping[str, Any]) -> dict:
        base = str(self._cfg["base_url"]).rstrip("/")
        url = f"{base}{path}?{urllib.parse.urlencode(params)}"
        timeout = float(self._cfg["timeout_seconds"])
        if self._http_get is not None:
            raw = self._http_get(url, self._headers, timeout)
        else:
            # resolved at call time so tests can patch it; the 429 wait cap
            # comes from config like every other tunable
            raw = source.default_http_get(
                url, self._headers, timeout,
                max_retry_wait=float(self._cfg["max_retry_wait_seconds"]),
            )
        try:
            body = json.loads(raw)
        except (ValueError, TypeError):
            raise SourceError("asana: response is not valid JSON") from None
        if not isinstance(body, dict) or "data" not in body:
            raise SourceError("asana: unexpected response shape")
        return body

    def _pages(self, path: str, params: dict, *, strict: bool) -> list[Any]:
        max_pages = int(self._cfg["max_pages"])
        items: list[Any] = []
        offset: str | None = None
        for _ in range(max_pages):
            page_params = dict(params)
            if offset:
                page_params["offset"] = offset
            body = self._get(path, page_params)
            data = body["data"]
            if isinstance(data, list):
                items.extend(data)
            next_page = body.get("next_page")
            offset = next_page.get("offset") if isinstance(next_page, dict) else None
            if not offset:
                return items
        if strict:
            raise SourceError(f"asana: more than {MAX_PAGES_KEY} pages of results")
        return items

    # -- fetch ----------------------------------------------------------

    def fetch(self, *, today: date) -> list[Task]:
        token_env = self._cfg["token_env"]
        token = self._env.get(token_env)
        if not token:
            raise SourceError(f"asana: environment variable {token_env} is not set")
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        me = self._get("/users/me", {"opt_fields": "gid,workspaces.gid"})["data"]
        user_gid = str(me.get("gid", "")) if isinstance(me, dict) else ""
        if self._cfg.get("workspace"):
            workspaces = [str(self._cfg["workspace"])]
        else:
            workspaces = [
                str(w["gid"])
                for w in (me.get("workspaces") or [])
                if isinstance(w, dict) and w.get("gid")
            ]

        cutoff = today + timedelta(days=int(self._cfg["lookahead_days"]))
        mapped = set(self._cfg["projects"])
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        tasks: dict[str, Task] = {}
        for workspace in workspaces:
            raw_tasks = self._pages(
                "/tasks",
                {
                    "assignee": "me",
                    "workspace": workspace,
                    "completed_since": now,
                    "limit": self._cfg["page_size"],
                    "opt_fields": TASK_FIELDS,
                },
                strict=True,
            )
            for raw in raw_tasks:
                task = self._build(raw, cutoff, mapped, user_gid)
                if task is not None:
                    tasks[task.task_id] = task
        return sorted(tasks.values(), key=lambda t: (t.due_on or "", t.task_id))

    def _build(
        self, raw: Any, cutoff: date, mapped: set, user_gid: str
    ) -> Task | None:
        if not isinstance(raw, dict):
            return None
        gid = str(raw.get("gid", ""))
        if not (gid.isascii() and gid.isdigit()) or raw.get("completed"):
            return None
        due = raw.get("due_on") or (raw.get("due_at") or "")[:10] or None
        if not due:
            return None
        try:
            if date.fromisoformat(due) > cutoff:
                return None
        except ValueError:
            return None
        memberships = []
        for entry in raw.get("memberships") or []:
            if not isinstance(entry, dict):
                continue
            project = entry.get("project") or {}
            section = entry.get("section") or {}
            project_gid = str(project.get("gid", ""))
            if not project_gid:
                continue
            memberships.append(
                Membership(
                    project_gid, project.get("name") or "", section.get("name") or None
                )
            )
        keep_unmapped = bool(self._cfg.get("unmapped_repos"))
        if not keep_unmapped and not any(m.project_id in mapped for m in memberships):
            return None
        url = raw.get("permalink_url") or ""
        return Task(
            source=self.name,
            task_id=gid,
            title=raw.get("name") or "",
            url=url if url.startswith("https://") else "",
            due_on=due,
            modified_at=to_timestamp(raw.get("modified_at")),
            newest_comment_at=self._newest_comment(gid, user_gid),
            memberships=tuple(memberships),
        )

    def _newest_comment(self, gid: str, user_gid: str) -> str | None:
        stories = self._pages(
            f"/tasks/{gid}/stories",
            {"limit": self._cfg["page_size"], "opt_fields": STORY_FIELDS},
            strict=False,
        )
        ignore_own = bool(self._cfg.get("ignore_own_comments"))
        allowed = {str(a) for a in self._cfg.get("comment_authors") or []}
        newest: str | None = None
        for story in stories:
            if not isinstance(story, dict):
                continue
            if story.get("resource_subtype") != "comment_added":
                continue
            author = story.get("created_by")
            author_gid = str(author.get("gid", "")) if isinstance(author, dict) else ""
            if allowed and author_gid not in allowed:
                continue
            if ignore_own and isinstance(author, dict) and author.get("gid") == user_gid:
                continue
            stamp = to_timestamp(story.get("created_at"))
            if stamp and (newest is None or stamp > newest):
                newest = stamp
        return newest

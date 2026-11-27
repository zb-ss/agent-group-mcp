"""GitHub task source: open issues and pull requests of watched repositories.

Read-only (GET only, stdlib only). The token is read from the environment
variable named in the config and goes only into the Authorization header.
Errors carry the host or a fixed code, never response text. No issue, pull
request or comment body is ever read: only the structured fields listed in
the data-hygiene notes below.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from . import source
from .model import Membership, Task, to_timestamp
from .source import HttpGet, SourceError

_ISSUE_NUMBER = re.compile(r"/issues/(\d+)$")
_SHA = re.compile(r"[0-9a-f]{40,64}")
_SLUG = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_PULL_ONLY = ("draft", "review_requested", "failing_checks")


class GithubError(SourceError):
    """A GitHub answer that cannot be used; `code` is a fixed code."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


class GithubBudget(SourceError):
    """The per-run GitHub call budget is used up."""


def read_token(cfg: Mapping, env: Mapping[str, str]) -> str:
    """The token from the configured environment variable. Errors name the
    variable only, never its value."""
    return source.read_token(cfg, env, "github: ")


class GithubClient:
    """GET-only client. `budget` caps the calls of the whole run; `share`
    (set per repository with `allow`) caps the calls of the current one."""

    def __init__(
        self,
        cfg: Mapping,
        *,
        env: Mapping[str, str],
        http_get: HttpGet | None = None,
        budget: int | None = None,
    ) -> None:
        self._cfg = cfg
        self._env = env
        self._http_get = http_get
        self._budget = budget
        self._share: int | None = None

    @property
    def remaining(self) -> int | None:
        return self._budget

    def allow(self, share: int | None) -> None:
        """Cap the calls until the next `allow` (None: only the run budget)."""
        self._share = share

    def get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        token = read_token(self._cfg, self._env)
        if self._share is not None:
            if self._share <= 0:
                raise GithubBudget("github: call budget for this repository is used up")
            self._share -= 1
        if self._budget is not None:
            if self._budget <= 0:
                raise GithubBudget("github: call budget for this run is used up")
            self._budget -= 1
        segments = [urllib.parse.quote(part, safe="") for part in path.strip("/").split("/")]
        url = f"{str(self._cfg['base_url']).rstrip('/')}/{'/'.join(segments)}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": str(self._cfg["accept"]),
            "X-GitHub-Api-Version": str(self._cfg["api_version"]),
            "User-Agent": str(self._cfg["user_agent"]),
        }
        timeout = float(self._cfg["timeout_seconds"])
        if self._http_get is not None:
            raw = self._http_get(url, headers, timeout)
        else:
            # resolved at call time so tests can patch it
            raw = source.default_http_get(
                url, headers, timeout,
                max_retry_wait=float(self._cfg["max_retry_wait_seconds"]),
            )
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            raise GithubError("github: response is not valid JSON", "bad_response") from None


def _lower_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    return [str(v).lower() for v in value]


def _login(entry: Any) -> str | None:
    if isinstance(entry, dict):
        login = entry.get("login")
        if isinstance(login, str) and login:
            return login.lower()
    return None


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class _Item:
    __slots__ = ("id", "number", "title", "state", "updated_at", "author", "labels",
                 "assignees", "is_pull")


class GithubSource:
    kind = "github"

    def __init__(
        self,
        name: str,
        cfg: Mapping,
        *,
        env: Mapping[str, str],
        http_get: HttpGet | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.name = name
        self._cfg = cfg
        self.schedule = cfg.get("schedule")
        self._env = env
        self._http_get = http_get
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.errors: list[str] = []
        self._client: GithubClient | None = None

    # -- helpers --------------------------------------------------------

    def _get_list(self, path: str, params: dict) -> list:
        """One page of a list endpoint."""
        assert self._client is not None
        body = self._client.get(path, params)
        if not isinstance(body, list):
            raise GithubError("github: unexpected response shape", "bad_response")
        return body

    def _paged(self, path: str, params: dict) -> list:
        page_size = int(self._cfg["page_size"])
        items: list = []
        for page in range(1, int(self._cfg["max_pages"]) + 1):
            entries = self._get_list(path, {**params, "per_page": page_size, "page": page})
            items.extend(entries)
            if len(entries) < page_size:
                break
        return items

    def _since(self, days: int) -> str:
        moment = self._now() - timedelta(days=days)
        return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _rules_for(self, slug: str) -> list[Mapping]:
        rules = []
        for rule in self._cfg.get("rules") or []:
            repos = rule.get("repos")
            if not repos or slug.lower() in _lower_list(repos):
                rules.append(rule)
        return rules

    # -- fetch ----------------------------------------------------------

    def fetch(self, *, today: date) -> list[Task]:
        self.errors = []
        read_token(self._cfg, self._env)
        budget = self._cfg.get("max_calls_per_run")
        self._client = GithubClient(
            self._cfg, env=self._env, http_get=self._http_get,
            budget=int(budget) if budget is not None else None,
        )
        self._owner: str | None = None
        self._owner_fetched = False
        trusted = _lower_list(self._cfg.get("trusted_authors"))

        watched = [
            (order, slug, rules)
            for order, slug in enumerate(self._cfg.get("projects") or {})
            if _SLUG.fullmatch(slug) and (rules := self._rules_for(slug))
        ]
        results: list[tuple[int, int, Task]] = []
        attempted = failed = 0
        first_error = ""
        for index, (order, slug, rules) in enumerate(watched):
            self._client.allow(self._share(len(watched) - index))
            attempted += 1
            try:
                tasks = self._fetch_repo(slug, rules, trusted)
            except GithubBudget:
                # Only this repository's share is spent: the others still run.
                self.errors.append(f"{self.name}: {slug}: call budget reached")
                continue
            except SourceError as exc:
                failed += 1
                line = f"{self.name}: {slug}: {exc}"
                first_error = first_error or str(exc)
                self.errors.append(line)
                continue
            results.extend((order, number, task) for number, task in tasks)
        if attempted and failed == attempted:
            raise SourceError(first_error)
        results.sort(key=lambda r: (r[0], r[1]))
        return [task for _, _, task in results]

    def _share(self, repos_left: int) -> int | None:
        """An even share of the calls left (rounded up), so one busy repository
        cannot starve the ones after it; calls it leaves unused carry over."""
        assert self._client is not None
        remaining = self._client.remaining
        if remaining is None:
            return None
        return -(-remaining // repos_left)

    def _fetch_repo(
        self, slug: str, rules: list[Mapping], trusted: list[str]
    ) -> list[tuple[int, Task]]:
        owner, repo = slug.split("/", 1)
        base = f"/repos/{owner}/{repo}"
        params: dict[str, Any] = {
            "state": self._cfg["state"], "sort": "updated", "direction": "desc",
        }
        days = int(self._cfg["updated_within_days"])
        if days > 0:
            params["since"] = self._since(days)
        items, skipped = self._read_items(self._paged(f"{base}/issues", params))
        if skipped:
            self.errors.append(f"{self.name}: {slug}: {skipped} malformed items skipped")

        # No call is ever spent on an item whose author is not trusted (pull
        # details, check runs, comments): strangers cannot drain the budget.
        # Such an item never matches a pull-only condition.
        vouched = [item for item in items if item.author is not None and item.author in trusted]
        pulls: dict[int, dict] = {}
        if any(item.is_pull for item in vouched) and any(
            key in rule for rule in rules for key in _PULL_ONLY
        ):
            pulls = self._read_pulls(self._paged(
                f"{base}/pulls",
                {"state": self._cfg["state"], "sort": "updated", "direction": "desc"},
            ))
        comments: dict[int, str] = {}
        if vouched:
            comments = self._read_comments(base, trusted)

        check_cache: dict[str, bool] = {}

        def failing(sha: str) -> bool:
            if sha not in check_cache:
                check_cache[sha] = self._has_failing_check(base, sha)
            return check_cache[sha]

        web = str(self._cfg["web_base_url"]).rstrip("/")
        built: list[tuple[int, Task]] = []
        for item in items:
            allowed = item.author is not None and item.author in trusted
            pull = pulls.get(item.number) if allowed else None
            rule = self._first_match(rules, item, pull, failing)
            if rule is None or rule.get("type") == "ignore":
                continue
            built.append((item.number, Task(
                source=self.name,
                task_id=str(item.id),
                title=item.title,
                url=f"{web}/{owner}/{repo}/issues/{item.number}",
                due_on=None,
                modified_at=to_timestamp(item.updated_at),
                newest_comment_at=comments.get(item.number) if allowed else None,
                memberships=(Membership(slug, slug, None),),
                type_hint=rule["type"],
                spawn_allowed=allowed,
            )))
        return built

    # -- readers (only the whitelisted fields) --------------------------

    def _read_items(self, raw: list) -> tuple[list[_Item], int]:
        items: list[_Item] = []
        skipped = 0
        for entry in raw:
            if not isinstance(entry, dict) or not _is_int(entry.get("id")) \
                    or not _is_int(entry.get("number")):
                skipped += 1
                continue
            item = _Item()
            item.id = entry["id"]
            item.number = entry["number"]
            item.title = str(entry.get("title") or "")
            item.state = str(entry.get("state") or "").lower()
            item.updated_at = entry.get("updated_at")
            item.author = _login(entry.get("user"))
            item.labels = [
                str(label["name"]).lower() for label in entry.get("labels") or []
                if isinstance(label, dict) and label.get("name")
            ]
            item.assignees = [
                login for login in (_login(a) for a in entry.get("assignees") or []) if login
            ]
            item.is_pull = "pull_request" in entry
            items.append(item)
        return items, skipped

    @staticmethod
    def _read_pulls(raw: list) -> dict[int, dict]:
        pulls: dict[int, dict] = {}
        for entry in raw:
            if not isinstance(entry, dict) or not _is_int(entry.get("number")):
                continue
            head = entry.get("head")
            sha = head.get("sha") if isinstance(head, dict) else None
            pulls[entry["number"]] = {
                "draft": bool(entry.get("draft")),
                "reviewers": [
                    login for login in
                    (_login(r) for r in entry.get("requested_reviewers") or []) if login
                ],
                "sha": sha if isinstance(sha, str) else "",
            }
        return pulls

    def _owner_login(self) -> str | None:
        configured = self._cfg.get("owner_login")
        if configured:
            return str(configured).lower()
        if not self._cfg.get("ignore_own_comments", True):
            return None
        if not self._owner_fetched:
            assert self._client is not None
            body = self._client.get("/user", None)
            if not isinstance(body, dict):
                raise GithubError("github: unexpected response shape", "bad_response")
            login = _login(body)
            if not login:
                # Fail closed: without the owner the own-comment filter is off.
                raise GithubError("github: the token owner is unknown", "bad_response")
            # Cached only after a successful answer, so a failure is raised
            # again for every repository instead of silently skipping the filter.
            self._owner = login
            self._owner_fetched = True
        return self._owner

    def _read_comments(self, base: str, trusted: list[str]) -> dict[int, str]:
        owner = self._owner_login() if self._cfg.get("ignore_own_comments", True) else None
        raw = self._paged(f"{base}/issues/comments", {
            "since": self._since(int(self._cfg["comment_lookback_days"])),
            "sort": "created", "direction": "desc",
        })
        newest: dict[int, str] = {}
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            match = _ISSUE_NUMBER.search(str(entry.get("issue_url") or ""))
            author = _login(entry.get("user"))
            stamp = to_timestamp(entry.get("created_at"))
            if not match or not author or not stamp or author not in trusted:
                continue
            if owner and author == owner:
                continue
            number = int(match.group(1))
            if stamp > newest.get(number, ""):
                newest[number] = stamp
        return newest

    def _has_failing_check(self, base: str, sha: str) -> bool:
        assert self._client is not None
        body = self._client.get(f"{base}/commits/{sha}/check-runs", {"per_page": 100})
        if not isinstance(body, dict) or not isinstance(body.get("check_runs"), list):
            raise GithubError("github: unexpected response shape", "bad_response")
        failing = set(_lower_list(self._cfg.get("failing_conclusions")))
        return any(
            isinstance(run, dict) and str(run.get("conclusion") or "").lower() in failing
            for run in body["check_runs"]
        )

    # -- rules ----------------------------------------------------------

    @staticmethod
    def _first_match(
        rules: list[Mapping], item: _Item, pull: dict | None, failing: Callable[[str], bool]
    ) -> Mapping | None:
        for rule in rules:
            if GithubSource._matches(rule, item, pull, failing):
                return rule
        return None

    @staticmethod
    def _matches(
        rule: Mapping, item: _Item, pull: dict | None, failing: Callable[[str], bool]
    ) -> bool:
        if "kind" in rule and rule["kind"] != ("pull_request" if item.is_pull else "issue"):
            return False
        if "state" in rule and str(rule["state"]).lower() != item.state:
            return False
        if "labels" in rule and not all(
            label in item.labels for label in _lower_list(rule["labels"])
        ):
            return False
        if "assignee" in rule and not any(
            login in item.assignees for login in _lower_list(rule["assignee"])
        ):
            return False
        if "author" in rule and item.author not in _lower_list(rule["author"]):
            return False
        if any(key in rule for key in _PULL_ONLY):
            if not item.is_pull or pull is None:
                return False
            if "draft" in rule and bool(rule["draft"]) != pull["draft"]:
                return False
            if "review_requested" in rule and not any(
                login in pull["reviewers"] for login in _lower_list(rule["review_requested"])
            ):
                return False
            if "failing_checks" in rule:
                sha = pull["sha"]
                if not _SHA.fullmatch(sha):
                    return False
                if bool(rule["failing_checks"]) != failing(sha):
                    return False
        return True

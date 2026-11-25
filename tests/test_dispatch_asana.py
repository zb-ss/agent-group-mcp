from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import date

import pytest

from agent_bus.dispatch import source
from agent_bus.dispatch.asana import AsanaSource
from agent_bus.dispatch.source import SourceError

BASE = "https://asana.example.com/api/1.0"
TODAY = date(2030, 1, 10)
USER = "1000000000000099"
TOKEN = "fake-token"


class FakeHttp:
    def __init__(self, routes):
        self.routes = routes  # path -> list of bodies (one per page)
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, headers, timeout, **kwargs):
        self.calls.append((url, dict(headers)))
        self.kwargs = kwargs
        parts = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qs(parts.query)
        path = parts.path[len("/api/1.0"):]
        if isinstance(self.routes.get(path), SourceError):
            raise self.routes[path]
        pages = self.routes[path]
        index = 1 if query.get("offset") else 0
        return json.dumps(pages[index]).encode()

    def paths(self):
        return [urllib.parse.urlsplit(u).path[len("/api/1.0"):] for u, _ in self.calls]


def cfg(**over):
    base = {
        "base_url": BASE,
        "token_env": "FAKE_TOKEN_VAR",
        "workspace": "",
        "lookahead_days": 0,
        "page_size": 50,
        "max_pages": 3,
        "timeout_seconds": 5,
        "max_retry_wait_seconds": 30,
        "ignore_own_comments": True,
        "projects": {"2000000000000001": "repo-a"},
    }
    base.update(over)
    return base


def task(gid, due="2030-01-10", completed=False, project="2000000000000001", **extra):
    entry = {
        "gid": gid,
        "name": f"Task {gid}",
        "completed": completed,
        "due_on": due,
        "modified_at": "2030-01-09T10:00:00.000Z",
        "permalink_url": f"https://example.com/t/{gid}",
        "memberships": [
            {"project": {"gid": project, "name": "Project A"}, "section": {"name": "Todo"}}
        ],
    }
    entry.update(extra)
    return entry


def story(created, subtype="comment_added", author="1000000000000050"):
    return {"created_at": created, "resource_subtype": subtype, "created_by": {"gid": author}}


def make(tasks, stories=None, workspaces=("3000000000000001",), **over):
    routes = {
        "/users/me": [{"data": {"gid": USER, "workspaces": [{"gid": w} for w in workspaces]}}],
        "/tasks": tasks if isinstance(tasks, list) and tasks and isinstance(tasks[0], dict) and "data" in tasks[0] else [{"data": tasks}],
    }
    for gid, items in (stories or {}).items():
        routes[f"/tasks/{gid}/stories"] = [{"data": items}]
    http = FakeHttp(routes)
    for t in routes["/tasks"][0]["data"]:
        routes.setdefault(f"/tasks/{t['gid']}/stories", [{"data": []}])
    src = AsanaSource(cfg(**over), env={"FAKE_TOKEN_VAR": TOKEN}, http_get=http)
    return src, http


def test_filters_tasks():
    src, _ = make([
        task("1000000000000001"),
        task("1000000000000002", due="2030-01-01"),
        task("1000000000000003", due="2030-01-11"),
        task("1000000000000004", completed=True),
        task("1000000000000005", project="2999999999999999"),
        task("1000000000000006", due=None),
        task("abc"),
    ])
    got = src.fetch(today=TODAY)
    assert [t.task_id for t in got] == ["1000000000000002", "1000000000000001"]


def test_lookahead():
    src, _ = make([task("1000000000000001", due="2030-01-12")], lookahead_days=2)
    assert len(src.fetch(today=TODAY)) == 1


def test_due_at_fallback():
    src, _ = make([task("1000000000000001", due=None, due_at="2030-01-10T08:00:00.000Z")])
    assert src.fetch(today=TODAY)[0].due_on == "2030-01-10"


def test_pagination_of_tasks():
    routes = {
        "/users/me": [{"data": {"gid": USER, "workspaces": [{"gid": "3000000000000001"}]}}],
        "/tasks": [
            {"data": [task("1000000000000001")], "next_page": {"offset": "abc"}},
            {"data": [task("1000000000000002")], "next_page": None},
        ],
        "/tasks/1000000000000001/stories": [{"data": []}],
        "/tasks/1000000000000002/stories": [{"data": []}],
    }
    src = AsanaSource(cfg(), env={"FAKE_TOKEN_VAR": TOKEN}, http_get=FakeHttp(routes))
    assert len(src.fetch(today=TODAY)) == 2


def test_too_many_pages_names_key():
    page = {"data": [], "next_page": {"offset": "x"}}
    routes = {
        "/users/me": [{"data": {"gid": USER, "workspaces": [{"gid": "3000000000000001"}]}}],
        "/tasks": [page, page],
    }
    src = AsanaSource(cfg(max_pages=2), env={"FAKE_TOKEN_VAR": TOKEN}, http_get=FakeHttp(routes))
    with pytest.raises(SourceError, match="dispatch.sources.asana.max_pages"):
        src.fetch(today=TODAY)


def test_memberships_mapped():
    src, _ = make([task("1000000000000001")])
    got = src.fetch(today=TODAY)[0]
    assert got.memberships[0].project_id == "2000000000000001"
    assert got.memberships[0].project_name == "Project A"
    assert got.memberships[0].section_name == "Todo"
    assert got.url == "https://example.com/t/1000000000000001"
    assert got.source == "asana"


def test_newest_comment_only_comment_added():
    stories = {"1000000000000001": [
        story("2030-01-05T10:00:00.000Z"),
        story("2030-01-07T10:00:00.000Z"),
        story("2030-01-09T10:00:00.000Z", subtype="assigned"),
    ]}
    src, _ = make([task("1000000000000001")], stories)
    got = src.fetch(today=TODAY)[0].newest_comment_at
    assert got.startswith("2030-01-07")


@pytest.mark.parametrize("ignore,expected", [(True, "2030-01-05"), (False, "2030-01-08")])
def test_own_comments(ignore, expected):
    stories = {"1000000000000001": [
        story("2030-01-05T10:00:00.000Z"),
        story("2030-01-08T10:00:00.000Z", author=USER),
    ]}
    src, _ = make([task("1000000000000001")], stories, ignore_own_comments=ignore)
    assert src.fetch(today=TODAY)[0].newest_comment_at.startswith(expected)


def test_configured_workspace_used():
    src, http = make([task("1000000000000001")], workspace="3000000000000777",
                     workspaces=("3000000000000001", "3000000000000002"))
    src.fetch(today=TODAY)
    task_calls = [u for u, _ in http.calls if urllib.parse.urlsplit(u).path.endswith("/tasks")]
    assert len(task_calls) == 1 and "workspace=3000000000000777" in task_calls[0]


def test_all_user_workspaces_by_default():
    src, http = make([task("1000000000000001")], workspaces=("3000000000000001", "3000000000000002"))
    src.fetch(today=TODAY)
    assert sum(p == "/tasks" for p in http.paths()) == 2


def test_missing_token_names_variable_only():
    src = AsanaSource(cfg(), env={}, http_get=FakeHttp({}))
    with pytest.raises(SourceError, match="FAKE_TOKEN_VAR"):
        src.fetch(today=TODAY)


def test_http_failure_propagates_without_token():
    http = FakeHttp({"/users/me": SourceError("HTTP 401 from asana.example.com")})
    src = AsanaSource(cfg(), env={"FAKE_TOKEN_VAR": TOKEN}, http_get=http)
    with pytest.raises(SourceError) as info:
        src.fetch(today=TODAY)
    assert TOKEN not in str(info.value)


def test_urls_and_headers():
    src, http = make([task("1000000000000001")])
    src.fetch(today=TODAY)
    assert all(u.startswith(BASE) for u, _ in http.calls)
    assert all(h["Authorization"] == f"Bearer {TOKEN}" for _, h in http.calls)


def test_bad_body_is_source_error():
    src = AsanaSource(cfg(), env={"FAKE_TOKEN_VAR": TOKEN}, http_get=lambda u, h, t: b"[]")
    with pytest.raises(SourceError) as info:
        src.fetch(today=TODAY)
    assert TOKEN not in str(info.value)


def test_default_http_get_refuses_http(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("urlopen called")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(SourceError):
        source.default_http_get("http://example.com/x?secret=1", {}, 1.0)


def test_monkeypatched_default_used(monkeypatch):
    http = FakeHttp({
        "/users/me": [{"data": {"gid": USER, "workspaces": []}}],
    })
    monkeypatch.setattr(source, "default_http_get", http)
    src = AsanaSource(cfg(), env={"FAKE_TOKEN_VAR": TOKEN})
    assert src.fetch(today=TODAY) == []


def test_the_retry_wait_cap_comes_from_config(monkeypatch):
    http = FakeHttp({
        "/users/me": [{"data": {"gid": USER, "workspaces": []}}],
    })
    monkeypatch.setattr(source, "default_http_get", http)
    src = AsanaSource(cfg(max_retry_wait_seconds=4), env={"FAKE_TOKEN_VAR": TOKEN})
    src.fetch(today=TODAY)
    assert http.kwargs == {"max_retry_wait": 4.0}


class _FakeResponse:
    def __init__(self, body=b"{}"):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def _http_error(code, headers=None):
    import email.message
    import urllib.error

    msg = email.message.Message()
    for key, value in (headers or {}).items():
        msg[key] = value
    return urllib.error.HTTPError("https://asana.example.com/x", code, "x", msg, None)


class _ScriptedOpener:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def open(self, request, timeout=None):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_redirect_is_not_followed(monkeypatch):
    import io
    import urllib.response

    opened: list[str] = []

    class FakeHttps(urllib.request.HTTPSHandler):
        def https_open(self, req):
            opened.append(req.full_url)
            headers = {"Location": "https://other.example.net/x"}
            import email.message

            msg = email.message.Message()
            msg["Location"] = headers["Location"]
            resp = urllib.response.addinfourl(io.BytesIO(b""), msg, req.full_url, 302)
            resp.msg = "Found"
            return resp

    monkeypatch.setattr(
        source,
        "_build_opener",
        lambda: urllib.request.build_opener(source._NoRedirect, FakeHttps),
    )
    with pytest.raises(SourceError) as info:
        source.default_http_get("https://asana.example.com/x", {"Authorization": "Bearer t"}, 1.0)
    assert "302" in str(info.value)
    assert opened == ["https://asana.example.com/x"]


def test_429_then_success_sleeps_once(monkeypatch):
    opener = _ScriptedOpener([_http_error(429, {"Retry-After": "7"}), _FakeResponse(b"ok")])
    monkeypatch.setattr(source, "_build_opener", lambda: opener)
    sleeps: list[float] = []
    body = source.default_http_get("https://asana.example.com/x", {}, 1.0, sleep=sleeps.append)
    assert body == b"ok"
    assert sleeps == [7.0]
    assert opener.calls == 2


def test_429_wait_is_capped(monkeypatch):
    opener = _ScriptedOpener([_http_error(429, {"Retry-After": "900"}), _FakeResponse()])
    monkeypatch.setattr(source, "_build_opener", lambda: opener)
    sleeps: list[float] = []
    source.default_http_get(
        "https://asana.example.com/x", {}, 1.0, max_retry_wait=5, sleep=sleeps.append
    )
    assert sleeps == [5.0]


def test_429_twice_raises(monkeypatch):
    opener = _ScriptedOpener([_http_error(429), _http_error(429)])
    monkeypatch.setattr(source, "_build_opener", lambda: opener)
    sleeps: list[float] = []
    with pytest.raises(SourceError) as info:
        source.default_http_get("https://asana.example.com/x", {}, 1.0, sleep=sleeps.append)
    assert "429" in str(info.value)
    assert len(sleeps) == 1
    assert opener.calls == 2


def test_comment_authors_allowlist_filters_commenters():
    stories = {"1000000000000001": [
        story("2030-01-05T10:00:00.000Z", author="1000000000000050"),
        story("2030-01-08T10:00:00.000Z", author="1000000000000099"),
    ]}
    src, _ = make([task("1000000000000001")], stories, comment_authors=["1000000000000050"])
    assert src.fetch(today=TODAY)[0].newest_comment_at.startswith("2030-01-05")


def test_empty_comment_authors_allows_any_author():
    stories = {"1000000000000001": [story("2030-01-08T10:00:00.000Z", author="1000000000000077")]}
    src, _ = make([task("1000000000000001")], stories, comment_authors=[])
    assert src.fetch(today=TODAY)[0].newest_comment_at.startswith("2030-01-08")

"""Slack Web API client (stdlib only).

The user token is read from the environment variable named in the config and
goes only into the Authorization header. Errors never contain it, nor any
response text beyond Slack's own error code.
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Callable, Mapping

from ..sessions import TIMESTAMP_FORMAT
from . import source
from .messages import Batch, Message
from .source import HttpSend, SourceError

_MENTION = re.compile(r"<[!@][^>]*>")
_CODE_CHARS = re.compile(r"[^a-z0-9_]")
MAX_CODE_CHARS = 60


class SlackError(SourceError):
    """A Slack API error answer; `code` is Slack's own sanitised error code."""

    def __init__(self, message: str, code: str, method: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.method = method


# Answers that mean the owner no longer reads a conversation.
MEMBERSHIP_CODES = frozenset({"channel_not_found", "not_in_channel", "is_archived"})


class SlackBudget(SourceError):
    """The per-run Slack call budget is used up."""


def _error_code(value: object) -> str:
    return _CODE_CHARS.sub("", str(value).lower())[:MAX_CODE_CHARS]


class SlackClient:
    def __init__(
        self,
        cfg: Mapping,
        *,
        env: Mapping[str, str],
        http_send: HttpSend | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        budget: int | None = None,
    ) -> None:
        self._cfg = cfg
        self._env = env
        self._http_send = http_send
        self._sleep = sleep
        self._clock = clock
        self._budget = budget
        self._last_call: float | None = None

    def _pace(self) -> None:
        if self._budget is not None:
            if self._budget <= 0:
                raise SlackBudget("slack: call budget for this run is used up")
            self._budget -= 1
        if self._last_call is not None:
            wait = float(self._cfg["min_interval_seconds"]) - (self._clock() - self._last_call)
            if wait > 0:
                self._sleep(wait)
        self._last_call = self._clock()

    def _post(self, url: str, body: bytes, headers: Mapping[str, str]):
        timeout = float(self._cfg["timeout_seconds"])
        if self._http_send is not None:
            return self._http_send(url, body, headers, timeout)
        # resolved at call time so tests can patch it
        return source.default_http_send(
            url, body, headers, timeout,
            max_retry_wait=float(self._cfg["max_retry_wait_seconds"]),
        )

    def call(self, method: str, params: Mapping[str, str]) -> tuple[dict, Mapping[str, str]]:
        token = source.read_token(self._cfg, self._env)
        url = f"{str(self._cfg['base_url']).rstrip('/')}/{method}"
        body = urllib.parse.urlencode(params).encode()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
        }
        for attempt in (0, 1):
            self._pace()
            response = self._post(url, body, headers)
            try:
                payload = json.loads(response.body)
            except (ValueError, TypeError):
                raise SourceError(f"slack {method}: invalid response") from None
            if not isinstance(payload, dict):
                raise SourceError(f"slack {method}: invalid response")
            if payload.get("ok"):
                return payload, response.headers
            code = _error_code(payload.get("error", ""))
            if code == "ratelimited" and attempt == 0:
                try:
                    wait = float(response.headers.get("retry-after", 1))
                except (TypeError, ValueError):
                    wait = 1.0
                self._sleep(max(0.0, min(wait, float(self._cfg["max_retry_wait_seconds"]))))
                continue
            raise SlackError(f"slack {method} failed: {code}", code, method)
        raise SourceError(f"slack {method} failed: ratelimited")  # pragma: no cover


def neutralise(text: str) -> str:
    """Make a text unable to notify people: drop the markup of mentions."""
    previous = None
    while previous != text:  # nested markup must not reassemble after one pass
        previous, text = text, _MENTION.sub(lambda m: m.group(0)[2:-1], text)
    return text


def escape_text(text: str) -> str:
    """Escape Slack control characters so no markup (mention, link) survives."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def post_message(
    cfg: Mapping,
    *,
    env: Mapping[str, str],
    channel: str,
    thread_ts: str | None,
    text: str,
    http_send: HttpSend | None = None,
) -> str:
    params = {"channel": channel, "text": text}
    if thread_ts:
        params["thread_ts"] = thread_ts
    payload, _ = SlackClient(cfg, env=env, http_send=http_send).call("chat.postMessage", params)
    ts = payload.get("ts")
    if not isinstance(ts, str) or not ts:
        raise SourceError("slack chat.postMessage: invalid response")
    return ts


def permalink(workspace_url: str, channel: str, ts: str, thread_ts: str | None = None) -> str:
    url = f"{workspace_url.rstrip('/')}/archives/{channel}/p{ts.replace('.', '')}"
    if thread_ts:
        url += f"?thread_ts={thread_ts}&cid={channel}"
    return url


ALLOWED_SUBTYPES = (None, "bot_message", "thread_broadcast")
SEARCH_MAX_COUNT = 100


def _ts_key(ts: str) -> tuple[int, int]:
    seconds, _, fraction = str(ts).partition(".")
    try:
        return int(seconds), int(fraction.ljust(6, "0")[:6] or 0)
    except ValueError:
        return 0, 0


def _newer(ts: str, than: str) -> bool:
    return _ts_key(ts) > _ts_key(than)


def _epoch_ts(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return f"{moment.timestamp():.6f}"


def _ts_to_stamp(ts: str) -> str:
    seconds, micros = _ts_key(ts)
    return datetime.fromtimestamp(seconds, timezone.utc).replace(microsecond=micros).strftime(TIMESTAMP_FORMAT)


def _scopes(headers: Mapping[str, str]) -> set[str]:
    raw = ""
    for key, value in headers.items():
        if key.lower() == "x-oauth-scopes":
            raw = str(value)
    return {part.strip() for part in raw.split(",") if part.strip()}


class SlackSource:
    """Mentions of the owner and ruled channels. Read-only: no write method."""

    kind = "slack"

    def __init__(
        self,
        name: str,
        cfg: Mapping,
        *,
        env: Mapping[str, str],
        http_send: HttpSend | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self.schedule = cfg["schedule"]
        self._cfg = cfg
        self._env = env
        self._http_send = http_send
        self._sleep = sleep
        self._clock = clock
        self._mentions = cfg["mentions"]

    # -- rules ------------------------------------------------------------

    def rules_for(self, message: Message) -> tuple[Sequence[Mapping], str]:
        default = self._mentions["action"]
        entry = self._cfg["channels"].get(message.container)
        if entry is None:
            return [], default
        counts = self._mentions["enabled"] and bool({"owner", "direct"} & set(message.mentions))
        return list(entry.get("rules", [])), default if counts else "ignore"

    # -- fetch ------------------------------------------------------------

    def fetch(self, cursor: str | None, *, now: datetime) -> Batch:
        state = self._load(cursor)
        client = SlackClient(
            self._cfg, env=self._env, http_send=self._http_send, sleep=self._sleep,
            clock=self._clock, budget=int(self._cfg["max_calls_per_run"]),
        )
        run = _Run(self, client, state, now)
        try:
            run.collect()
        except SlackBudget:
            pass
        return Batch(run.items(), json.dumps(state, sort_keys=True), run.errors())

    @staticmethod
    def _load(cursor: str | None) -> dict:
        try:
            state = json.loads(cursor) if cursor else {}
        except ValueError:
            state = {}
        return state if isinstance(state, dict) else {}


class _Run:
    def __init__(self, source_: SlackSource, client: SlackClient, state: dict, now: datetime) -> None:
        self.name = source_.name
        self.cfg = source_._cfg
        self.mentions = source_._mentions
        self.client = client
        self.state = state
        self.now = now
        self.found: dict[str, Message] = {}
        self.dropped: list[str] = []
        self.failed: list[str] = []
        self.owner = ""
        self.workspace = ""
        # Whether any conversation or search page was read this run: after
        # that, a failing call must not discard what was read (see _step).
        self.read_any = False
        self.dms: set[str] = set()
        self.ruled = sorted(self.cfg["channels"])
        self.include_dm = bool(self.mentions["include_direct_messages"])
        self.page_size = int(self.cfg["page_size"])
        self.max_pages = int(self.cfg["max_pages"])
        self.state.setdefault("channels", {})

    def items(self) -> tuple[Message, ...]:
        return tuple(sorted(self.found.values(), key=lambda m: _ts_key(m.item_id.split(":", 1)[1])))

    def errors(self) -> tuple[str, ...]:
        out = list(self.failed)
        if self.dropped:
            out.append(f"{self.name}: dropped conversations the owner no longer reads: {', '.join(self.dropped)}")
        return tuple(out)

    def _initial(self) -> str:
        minutes = float(self.cfg["initial_lookback_minutes"])
        return _epoch_ts(self.now - timedelta(minutes=minutes))

    def collect(self) -> None:
        _, headers = self._auth()
        scopes = _scopes(headers)
        for channel in self.ruled:
            self._step(lambda c=channel: self._read_channel(c, keep_all=True), "conversations.history", channel)
        if not self.mentions["enabled"]:
            return
        # Known scopes without im:read: direct messages cannot be listed, and
        # asking for them would fail the whole users.conversations call.
        dm_ok = not scopes or "im:read" in scopes
        include_dm = self.include_dm and dm_ok
        searching = bool(self.mentions["use_search"]) and "search:read" in scopes
        # Direct messages are read when asked for explicitly, or through the
        # member scan when its conversation types name `im`; the search path
        # ignores the types. Warn only when they were going to be read.
        wants_dm = self.include_dm or (
            not searching and "im" in self.mentions["conversation_types"]
        )
        if not dm_ok and wants_dm:
            self._warn_once("im_read", f"{self.name}: the token lacks im:read; direct messages are skipped")
        if searching:
            self._step(self._search, "search.messages")
            if include_dm:
                self._member_scan(["im"])
        else:
            types = list(self.mentions["conversation_types"])
            if not dm_ok:
                types = [t for t in types if t != "im"]
            elif include_dm and "im" not in types:
                types.append("im")
            if types:
                self._member_scan(types)

    def _warn_once(self, key: str, line: str) -> None:
        warned = self.state.get("warned")
        if not isinstance(warned, list):
            warned = self.state["warned"] = []
        if key not in warned:
            warned.append(key)
            self.failed.append(line)

    def _step(
        self, action: Callable[[], None], method: str, channel: str | None = None, *, reads: bool = True
    ) -> bool:
        """Run one read. Before anything was read a failure raises (the run
        has nothing to lose); after that it is reported with Slack's error
        code only and the run keeps what it read and the cursors it advanced,
        so an alert already read in a ruled channel is never dropped by a
        later failing call. A membership error (MEMBERSHIP_CODES) on a named
        channel is the exception: it is always reported by channel id and the
        run continues, even before anything was read. Returns whether the read
        succeeded."""
        try:
            action()
        except SlackBudget:
            raise  # a SourceError too, but it ends the run without an error
        except SourceError as exc:
            if channel is not None and isinstance(exc, SlackError) and exc.code in MEMBERSHIP_CODES:
                self._fail(channel, method, exc.code)
                return False
            if not self.read_any:
                raise
            code = exc.code if isinstance(exc, SlackError) else "request_failed"
            if channel is None:
                self.failed.append(f"{self.name}: {method} failed: {code}")
            else:
                self._fail(channel, method, code)
            return False
        self.read_any = self.read_any or reads
        return True

    def _auth(self) -> tuple[dict, Mapping[str, str]]:
        payload, headers = self.client.call("auth.test", {})
        self.owner = self.cfg["owner_id"] or str(payload.get("user_id") or "")
        self.workspace = str(payload.get("url") or "")
        if not self.owner:
            raise SourceError("slack auth.test: no user id")
        return payload, headers

    # -- search path ------------------------------------------------------

    def _search(self) -> None:
        since = self.state.get("search") or self._initial()
        newest = since
        query = str(self.mentions["search_query"]).format(owner_id=self.owner)
        count = min(self.page_size, SEARCH_MAX_COUNT)
        excluded = set(self.mentions["exclude_channels"])
        for page in range(1, self.max_pages + 1):
            payload, _ = self.client.call(
                "search.messages",
                {"query": query, "sort": "timestamp", "sort_dir": "desc", "count": str(count), "page": str(page)},
            )
            block = payload.get("messages") or {}
            matches = block.get("matches") or []
            reached_old = not matches
            for match in matches:
                ts = str(match.get("ts") or "")
                if not ts or not _newer(ts, since):
                    reached_old = True
                    continue
                newest = ts if _newer(ts, newest) else newest
                channel = str((match.get("channel") or {}).get("id") or "")
                if channel and channel not in excluded:
                    self._add(channel, match, url=str(match.get("permalink") or ""), keep_all=True, searched=True)
            paging = block.get("paging") or {}
            if reached_old or page >= int(paging.get("pages") or 1):
                break
        self.state["search"] = newest

    # -- member scan ------------------------------------------------------

    def _list_conversations(self, types: list[str]) -> None:
        member = self.state.get("member")
        stale = True
        if isinstance(member, dict) and member.get("types") == types and member.get("listed_at"):
            age = self.now - datetime.strptime(member["listed_at"], TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
            stale = age >= timedelta(minutes=float(self.mentions["conversations_refresh_minutes"]))
        if not stale:
            return
        checked = member.get("checked", {}) if isinstance(member, dict) else {}
        ids: list[str] = []
        dms: list[str] = []
        cursor = ""
        for _ in range(self.max_pages):
            params = {"types": ",".join(types), "limit": str(self.page_size)}
            if cursor:
                params["cursor"] = cursor
            payload, _ = self.client.call("users.conversations", params)
            for entry in payload.get("channels") or []:
                cid = str(entry.get("id") or "")
                if cid:
                    ids.append(cid)
                    if entry.get("is_im"):
                        dms.append(cid)
            cursor = str((payload.get("response_metadata") or {}).get("next_cursor") or "")
            if not cursor:
                break
        self.state["member"] = {
            "ids": ids, "dms": dms, "types": types, "checked": checked,
            "listed_at": self.now.strftime(TIMESTAMP_FORMAT),
        }

    def _member_scan(self, types: list[str]) -> None:
        """Read member conversations, least recently checked first. A failure
        on one conversation stops the scan but keeps what was collected: the
        failing one is stamped so it sorts last next run, and only its id and
        Slack's error code are reported."""
        if not self._step(lambda: self._list_conversations(types), "users.conversations", reads=False):
            return
        member = self.state["member"]
        self.dms = set(member.get("dms", []))
        skip = set(self.mentions["exclude_channels"]) | set(self.ruled)
        checked = member.setdefault("checked", {})
        order = sorted((c for c in member["ids"] if c not in skip), key=lambda c: (checked.get(c, ""), c))
        for channel in order:
            stamp = self.now.strftime(TIMESTAMP_FORMAT)
            try:
                self._read_channel(channel, keep_all=False)
                self.read_any = True
            except SlackError as exc:
                checked[channel] = stamp
                if exc.code in MEMBERSHIP_CODES:
                    self._drop(member, channel)
                    continue
                self._fail(channel, exc.method, exc.code)
                return
            except SlackBudget:
                raise  # a SourceError too, but it ends the run without an error
            except SourceError:
                checked[channel] = stamp
                self._fail(channel, "conversations.history", "request_failed")
                return
            checked[channel] = stamp

    def _drop(self, member: dict, channel: str) -> None:
        # A conversation the owner left or lost must not hide the rest. It is
        # dropped until the next conversations refresh, which reports it again
        # once if it is still listed and still unreadable.
        member["ids"] = [c for c in member["ids"] if c != channel]
        dropped = member.setdefault("dropped", [])
        if channel not in dropped:
            dropped.append(channel)
            self.dropped.append(channel)

    def _fail(self, channel: str, method: str, code: str) -> None:
        self.failed.append(f"{self.name}: {method} failed for {channel}: {code}")

    # -- history ----------------------------------------------------------

    def _read_channel(self, channel: str, *, keep_all: bool) -> None:
        since = self.state["channels"].get(channel) or self._initial()
        newest = self._history(channel, since, keep_all=keep_all)
        self.state["channels"][channel] = newest

    def _history(self, channel: str, since: str, *, keep_all: bool) -> str:
        oldest = since
        hours = float(self.cfg["thread_lookback_hours"])
        if hours > 0:
            back = _epoch_ts(self.now - timedelta(hours=hours))
            oldest = back if _newer(since, back) else since
        newest = since
        cursor = ""
        for _ in range(self.max_pages):
            params = {"channel": channel, "oldest": oldest, "limit": str(self.page_size)}
            if cursor:
                params["cursor"] = cursor
            payload, _ = self.client.call("conversations.history", params)
            for msg in payload.get("messages") or []:
                ts = str(msg.get("ts") or "")
                if not ts:
                    continue
                if _newer(ts, since):
                    newest = ts if _newer(ts, newest) else newest
                    self._add(channel, msg, url=None, keep_all=keep_all)
                latest = str(msg.get("latest_reply") or "")
                if latest and _newer(latest, since):
                    parent = str(msg.get("thread_ts") or ts)
                    newest = self._replies(channel, parent, since, newest, keep_all=keep_all)
            cursor = str((payload.get("response_metadata") or {}).get("next_cursor") or "")
            if not cursor:
                break
        return newest

    def _replies(self, channel: str, parent: str, since: str, newest: str, *, keep_all: bool) -> str:
        cursor = ""
        for _ in range(self.max_pages):
            params = {"channel": channel, "ts": parent, "oldest": since, "limit": str(self.page_size)}
            if cursor:
                params["cursor"] = cursor
            payload, _ = self.client.call("conversations.replies", params)
            for msg in payload.get("messages") or []:
                ts = str(msg.get("ts") or "")
                if not ts or ts == parent or not _newer(ts, since):
                    continue
                newest = ts if _newer(ts, newest) else newest
                self._add(channel, msg, url=None, keep_all=keep_all)
            cursor = str((payload.get("response_metadata") or {}).get("next_cursor") or "")
            if not cursor:
                break
        return newest

    # -- messages ---------------------------------------------------------

    def _mention_kinds(self, channel: str, text: str, author: str) -> tuple[str, ...]:
        kinds = []
        if "<!channel>" in text:
            kinds.append("channel")
        if "<!here>" in text:
            kinds.append("here")
        if f"<@{self.owner}>" in text or f"<@{self.owner}|" in text:
            kinds.append("owner")
        if self.include_dm and channel in self.dms and author != self.owner:
            kinds.append("direct")
        return tuple(kinds)

    def _add(self, channel: str, msg: Mapping, *, url: str | None, keep_all: bool, searched: bool = False) -> None:
        ts = str(msg.get("ts") or "")
        if not ts or msg.get("subtype") not in ALLOWED_SUBTYPES:
            return
        author = str(msg.get("user") or "")
        if author == self.owner and not msg.get("bot_id"):
            return
        item_id = f"{channel}:{ts}"
        if item_id in self.found:
            return
        full_text = str(msg.get("text") or "")
        text = full_text[: int(self.cfg["max_text_chars"])]
        kinds = self._mention_kinds(channel, full_text, author)
        if searched and "owner" not in kinds:
            kinds = (*kinds, "owner")
        if not (keep_all or {"owner", "direct"} & set(kinds)):
            return
        bot_id = str(msg.get("bot_id") or "")
        thread_ts = str(msg.get("thread_ts") or "")
        if searched and not thread_ts and url:
            # Search matches carry no thread_ts; the parent is in the permalink.
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            thread_ts = (query.get("thread_ts") or [""])[0]
        thread = thread_ts if thread_ts and thread_ts != ts else None
        self.found[item_id] = Message(
            source=self.name,
            item_id=item_id,
            container=channel,
            author=bot_id or author,
            author_kind="bot" if bot_id else "user",
            text=text,
            at=_ts_to_stamp(ts),
            url=url or permalink(self.workspace, channel, ts, thread),
            thread=thread,
            mentions=kinds,
            reply={"channel": channel, "thread_ts": thread or ts},
        )

"""Maildir message source and draft placement.

Mail text is untrusted. Reading never changes the mailbox: files are listed and
parsed, never renamed, flagged, moved or deleted. A draft is only ever written
to the Drafts folder for the owner to send himself; nothing here sends mail.
"""

from __future__ import annotations

import email.message
import email.parser
import email.policy
import email.utils
import hashlib
import html
import json
import mailbox
import os
import re
import urllib.parse
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..storage import TIMESTAMP_FORMAT
from .messages import Batch, Message
from .model import clean_text
from .source import SourceError

_TAGS = re.compile(r"<[^>]*>")
_HEADER_MAX = 998
_BULK_PRECEDENCE = {"bulk", "list", "junk"}


def _html_to_text(markup: str) -> str:
    return html.unescape(_TAGS.sub(" ", markup))


def _body(msg: email.message.EmailMessage, max_chars: int) -> str:
    try:
        part = msg.get_body(preferencelist=("plain",))
        if part is not None:
            return part.get_content()[:max_chars]
        part = msg.get_body(preferencelist=("html",))
        if part is not None:
            return _html_to_text(part.get_content())[:max_chars]
    except (LookupError, ValueError, KeyError):
        return ""
    return ""


def _is_bulk(msg: email.message.EmailMessage) -> bool:
    if msg["List-Id"]:
        return True
    if str(msg["Precedence"] or "").strip().lower() in _BULK_PRECEDENCE:
        return True
    auto = msg["Auto-Submitted"]
    return auto is not None and str(auto).strip().lower() != "no"


def _wrap(value: str) -> str:
    return clean_text(value, _HEADER_MAX)


def _item_id(raw: str) -> str:
    """A bounded, neutral id: sender-supplied text never becomes an id."""
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:32]


def _after(entry: tuple[float, str], since: tuple[float, str | None]) -> bool:
    """Whether a file sorts after the cursor. A cursor without a name (the
    initial lookback, or a cursor stored before names were kept) admits only
    strictly later mtimes."""
    mtime, key = entry
    since_mtime, since_key = since
    if mtime != since_mtime:
        return mtime > since_mtime
    return since_key is not None and key > since_key


def _parse_cursor(cursor: str | None) -> tuple[float, str | None] | None:
    """(mtime, name) from a stored cursor; a bare mtime is an older cursor."""
    if not cursor:
        return None
    try:
        data = json.loads(cursor)
    except ValueError:
        return None
    if isinstance(data, (int, float)) and not isinstance(data, bool):
        return float(data), None
    if (
        isinstance(data, list) and len(data) == 2
        and isinstance(data[0], (int, float)) and not isinstance(data[0], bool)
        and (data[1] is None or isinstance(data[1], str))
    ):
        return float(data[0]), data[1]
    return None


def _format_cursor(position: tuple[float, str | None]) -> str:
    return json.dumps([position[0], position[1]])


def _capped(last: tuple[float, str | None], now: datetime) -> tuple[float, str | None]:
    """Never move the cursor past now: a future-dated file would otherwise hide
    every later mail until the clock catches up. Such a file is fetched again on
    later runs; the item-id dedupe keeps it from being handled twice."""
    limit = now.timestamp()
    return last if last[0] <= limit else (limit, None)


class MaildirSource:
    kind = "maildir"

    def __init__(self, name: str, cfg: Mapping[str, Any], *, env=None, http_send=None) -> None:
        self.name = name
        self._cfg = cfg
        self.schedule = cfg["schedule"]

    def _folder_path(self, folder: str) -> Path:
        root = self._cfg.get("root")
        if not root:
            raise SourceError("maildir root is not configured")
        path = Path(root).expanduser() / folder
        if not (path / "cur").is_dir() or not (path / "new").is_dir():
            raise SourceError(f"maildir folder {folder!r} not found")
        return path

    def _candidates(self, since: tuple[float, str | None]) -> list[tuple[float, str, Path, str]]:
        found: list[tuple[float, str, Path, str]] = []
        for folder in self._cfg["folders"]:
            base = self._folder_path(folder)
            for sub in ("new", "cur"):
                with os.scandir(base / sub) as entries:
                    for entry in entries:
                        try:
                            if not entry.is_file():
                                continue
                            mtime = entry.stat().st_mtime
                        except OSError:
                            continue
                        key = f"{folder}/{sub}/{entry.name}"
                        if _after((mtime, key), since):
                            found.append((mtime, folder, Path(entry.path), key))
        found.sort(key=lambda item: (item[0], item[3]))
        return found

    def fetch(self, cursor: str | None, *, now: datetime) -> Batch:
        since = _parse_cursor(cursor)
        if since is None:
            since = (now.timestamp() - self._cfg["initial_lookback_minutes"] * 60, None)
        kept = self._candidates(since)[: self._cfg["max_items_per_run"]]
        parser = email.parser.BytesParser(policy=email.policy.default)
        items: list[Message] = []
        errors: list[str] = []
        last = since
        for mtime, folder, path, key in kept:
            try:
                with open(path, "rb") as handle:
                    msg = parser.parse(handle)
            except FileNotFoundError:
                # Renamed while we scanned (new -> cur, or a flag change): stop
                # before it so the next run finds it under its new name.
                break
            except Exception as exc:  # noqa: BLE001 - one unreadable file must never stop the run
                errors.append(f"maildir: cannot read {folder}/{path.name}: {type(exc).__name__}")
            else:
                try:
                    item = self._to_message(msg, mtime, folder, path)
                except Exception as exc:  # noqa: BLE001 - one hostile file must never stop the run
                    errors.append(f"maildir: cannot read {folder}/{path.name}: {type(exc).__name__}")
                else:
                    if item is not None:
                        items.append(item)
            last = (mtime, key)
        return Batch(tuple(items), _format_cursor(_capped(last, now)), tuple(errors))

    def _to_message(self, msg, mtime: float, folder: str, path: Path) -> Message | None:
        sender = email.utils.parseaddr(str(msg["From"] or ""))[1].lower()
        own = str(self._cfg.get("address") or "").lower()
        if own and sender == own:
            return None
        if self._cfg["skip_bulk"] and _is_bulk(msg):
            return None
        raw_id = str(msg["Message-ID"] or "").strip()
        message_id = raw_id.strip("<>").strip()
        item_id = _item_id(message_id or path.name.split(":", 1)[0])
        subject = str(msg["Subject"] or "")
        template = self._cfg.get("link_template")
        url = (
            template.format(message_id=urllib.parse.quote(message_id, safe=""))
            if template and message_id else ""
        )
        reply_to = email.utils.parseaddr(str(msg["Reply-To"] or ""))[1] or sender
        references = " ".join(part for part in (str(msg["References"] or "").strip(), raw_id) if part)
        reply = {
            "to": _wrap(reply_to),
            "subject": _wrap(subject if subject.lower().startswith("re:") else f"Re: {subject}"),
            "in_reply_to": _wrap(raw_id),
            "references": _wrap(references),
        }
        at = datetime.fromtimestamp(mtime, timezone.utc).strftime(TIMESTAMP_FORMAT)
        return Message(
            source=self.name, item_id=item_id, container=folder, author=sender,
            author_kind="user", text=_body(msg, self._cfg["max_body_chars"]),
            at=at, url=url, title=subject, reply=reply,
        )

    def rules_for(self, message: Message) -> tuple[Sequence[Mapping], str]:
        return self._cfg["rules"], self._cfg["default_action"]


def place_draft(cfg: Mapping[str, Any], *, target: Mapping[str, str], text: str, now: datetime) -> str:
    """Write a draft into the Drafts folder and return its Maildir key."""
    address = cfg.get("address")
    if not address:
        raise SourceError("maildir address is not configured")
    drafts = cfg.get("drafts_folder")
    if not drafts:
        raise SourceError("maildir drafts_folder is not configured")
    if not cfg.get("root"):
        raise SourceError("maildir root is not configured")
    path = Path(cfg["root"]).expanduser() / drafts
    if not (path / "cur").is_dir():
        raise SourceError("maildir drafts folder not found")
    if any("\r" in str(v) or "\n" in str(v) for v in target.values()):
        raise SourceError("draft header rejected")
    msg = email.message.EmailMessage()
    msg["From"] = address
    msg["To"] = target.get("to", "")
    msg["Subject"] = target.get("subject", "")
    for header, key in (("In-Reply-To", "in_reply_to"), ("References", "references")):
        if target.get(key):
            msg[header] = target[key]
    msg["Date"] = email.utils.format_datetime(now)
    msg["Message-ID"] = email.utils.make_msgid(domain=address.rpartition("@")[2] or None)
    msg.set_content(text)
    entry = mailbox.MaildirMessage(msg)
    entry.set_subdir("cur")
    entry.set_flags("DS")
    old_umask = os.umask(0o077)
    try:
        return mailbox.Maildir(str(path), create=False).add(entry)
    finally:
        os.umask(old_umask)

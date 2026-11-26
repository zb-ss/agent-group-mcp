"""Pure rule matching for messages. The first matching rule wins; rules with a
lifecycle map a message to an open, repeat or close phase of one incident key."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .messages import Message
from .model import clean_text


@dataclass(frozen=True)
class Hit:
    rule: str | None
    action: str
    phase: str | None  # open, repeat, close, or None without a lifecycle
    key: str | None
    playbook: str | None = None
    auto_post_report: bool = False


def _any(patterns: Sequence[str] | None, value: str, *, flags: int = 0) -> bool:
    for pattern in patterns or ():
        try:
            if re.search(pattern, value, flags):
                return True
        except re.error:
            continue  # settings validate patterns; a bad one never matches
    return False


def _who_where(rule: Mapping, message: Message) -> bool:
    bots, users = rule.get("bot_ids"), rule.get("user_ids")
    if bots or users:
        by_bot = bool(bots) and message.author in bots and message.author_kind == "bot"
        by_user = bool(users) and message.author in users and message.author_kind == "user"
        if not (by_bot or by_user):
            return False
    if rule.get("from") and not _any(rule["from"], message.author.lower()):
        return False
    folders = rule.get("folder")
    return not folders or message.container in folders


def _what(rule: Mapping, message: Message) -> bool:
    if rule.get("text") and not _any(rule["text"], message.text):
        return False
    if rule.get("subject") and not _any(rule["subject"], message.title):
        return False
    mention = rule.get("mention")
    if mention and not any(kind in message.mentions for kind in mention):
        return False
    thread = rule.get("thread", "any")
    if thread == "top":
        return message.thread is None
    if thread == "reply":
        return message.thread is not None
    return True


def _key(rule: Mapping, message: Message, name: str, max_key_chars: int) -> str:
    part = ""
    pattern = (rule.get("lifecycle") or {}).get("key")
    if pattern:
        try:
            found = re.search(pattern, message.text)
        except re.error:
            found = None
        if found:
            raw = found.group(1) if found.re.groups else found.group(0)
            part = clean_text(raw or "", max_key_chars).lower()
    return f"{message.source}:{message.container}:{name}:{part}"


def _phase(rule: Mapping, message: Message) -> str | None:
    lifecycle = rule["lifecycle"]
    if _any(lifecycle.get("close"), message.text):
        return "close"
    if _any(lifecycle.get("repeat"), message.text):
        return "repeat"
    if _what(rule, message):
        opens = lifecycle.get("open")
        if not opens or _any(opens, message.text):
            return "open"
    return None


def match(
    rules: Sequence[Mapping], default_action: str, message: Message,
    *, max_key_chars: int,
) -> Hit:
    for rule in rules:
        if not _who_where(rule, message):
            continue
        name = rule.get("name")
        action = rule.get("action", default_action)
        extra = {
            "playbook": rule.get("playbook"),
            "auto_post_report": bool(rule.get("auto_post_report", False)),
        }
        if not rule.get("lifecycle"):
            if _what(rule, message):
                return Hit(name, action, None, None, **extra)
            continue
        phase = _phase(rule, message)
        if phase:
            key = _key(rule, message, str(name), max_key_chars)
            return Hit(name, action, phase, key, **extra)
    return Hit(None, default_action, None, None)

"""Notifiers: thin messages (session, state, title, link, session links) to
log, macOS, email. The log line and the email carry the session links; the
macOS banner stays one line. The email is multipart: plain text plus HTML with
clickable, escaped anchors."""

from __future__ import annotations

import html
import os
import smtplib
import ssl
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Protocol

from .. import commands, links
from ..storage import TIMESTAMP_FORMAT
from .model import clean_text

URL_MAX_CHARS = 2048  # protocol-ish bound on a link, not a tunable


class NotifyError(Exception):
    pass


@dataclass(frozen=True)
class Notification:
    session: str | None
    state: str
    title: str
    url: str
    links: tuple[tuple[str, str], ...] = ()


class Notifier(Protocol):
    name: str

    def send(self, notification: Notification) -> None: ...


class LogNotifier:
    name = "log"
    enabled = True
    # A local record, not an alert: writing it does not mean the owner was told.
    alerts = False

    def __init__(self, path: Path, *, now: Callable[[], datetime] | None = None):
        self._path = path
        self._now = now or (lambda: datetime.now(timezone.utc))

    def send(self, notification: Notification) -> None:
        stamp = self._now().strftime(TIMESTAMP_FORMAT)
        line = (
            f"{stamp} {notification.state} session={notification.session or '-'} "
            f"title={notification.title} url={notification.url}"
        )
        if notification.links:
            line += " links=" + " ".join(url for _, url in notification.links)
        line += "\n"
        # The log holds task titles and links, so it is owner-only like the prompts.
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self._path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(line)


class MacosNotifier:
    name = "macos"

    def __init__(
        self, cfg: Mapping, *, run: commands.Runner, platform: str = sys.platform
    ):
        self._cfg = cfg
        self._run = run
        self._platform = platform

    @property
    def enabled(self) -> bool:
        return bool(self._cfg["enabled"]) and self._platform == "darwin"

    def send(self, notification: Notification) -> None:
        if not self._cfg["enabled"] or self._platform != "darwin":
            return
        argv = [
            self._cfg["osascript_bin"],
            "-e", "on run argv",
            "-e",
            "display notification (item 1 of argv) with title (item 2 of argv) "
            "subtitle (item 3 of argv)",
            "-e", "end run",
            f"{notification.state}: {notification.title}",
            self._cfg["title"],
            notification.session or "",
        ]
        try:
            result = self._run(argv, timeout=self._cfg["timeout_seconds"])
        except (OSError, subprocess.SubprocessError) as exc:
            raise NotifyError(f"osascript failed: {type(exc).__name__}") from exc
        if result.returncode != 0:
            raise NotifyError(f"osascript exited with {result.returncode}")


def connect_smtp(security: str, host: str, port: int, timeout: float) -> Any:
    """Open a TLS-protected SMTP connection; there is no plaintext path."""
    if security == "tls":
        return smtplib.SMTP_SSL(
            host, port, timeout=timeout, context=ssl.create_default_context()
        )
    if security == "starttls":
        client = smtplib.SMTP(host, port, timeout=timeout)
        try:
            client.starttls(context=ssl.create_default_context())
        except BaseException:
            try:
                client.close()
            except OSError:
                pass
            raise
        return client
    raise NotifyError(f"unsupported smtp security {security!r}")


class EmailNotifier:
    name = "email"

    def __init__(
        self,
        cfg: Mapping,
        *,
        env: Mapping[str, str],
        smtp_factory: Callable[[str, str, int, float], Any] | None = None,
    ):
        self._cfg = cfg
        self._env = env
        self._factory = smtp_factory or connect_smtp

    @property
    def enabled(self) -> bool:
        return bool(self._cfg["enabled"])

    def _message(self, n: Notification) -> EmailMessage:
        message = EmailMessage()
        message["From"] = self._cfg["from"]
        message["To"] = ", ".join(self._cfg["to"])
        message["Subject"] = f"{self._cfg['subject_prefix']} {n.state}: {n.title}"
        plain = (
            f"Session: {n.session or '-'}\nState: {n.state}\n"
            f"Task: {n.title}\nLink: {n.url}\n"
        )
        for label, url in n.links:
            plain += f"{label or 'Link'}: {url}\n"
        message.set_content(plain)
        message.add_alternative(self._html(n), subtype="html")
        return message

    @staticmethod
    def _html(n: Notification) -> str:
        esc = html.escape
        if links.is_web_url(n.url):
            task = f'<a href="{esc(n.url, quote=True)}">{esc(n.title)}</a>'
        else:
            task = f"{esc(n.title)} ({esc(n.url)})"
        items = "".join(
            # The anchor text is the URL itself, so a label cannot hide
            # where the link goes.
            f"<li>{esc(label or 'Link')}: "
            f'<a href="{esc(url, quote=True)}">{esc(url)}</a></li>'
            for label, url in n.links
        )
        listing = f"<ul>{items}</ul>" if items else ""
        return (
            f"<html><body><p>Session: {esc(n.session or '-')}<br>"
            f"State: {esc(n.state)}<br>Task: {task}</p>{listing}</body></html>"
        )

    def send(self, notification: Notification) -> None:
        cfg = self._cfg
        if not cfg["enabled"]:
            return
        smtp_password = None
        if cfg["username"]:
            smtp_password = self._env.get(cfg["password_env"])
            if not smtp_password:
                raise NotifyError(
                    f"environment variable {cfg['password_env']} is not set"
                )
        message = self._message(notification)
        try:
            client = self._factory(
                cfg["security"], cfg["host"], cfg["port"], cfg["timeout_seconds"]
            )
            try:
                if smtp_password is not None:
                    client.login(cfg["username"], smtp_password)
                client.send_message(message)
            finally:
                try:
                    client.quit()
                except (smtplib.SMTPException, OSError):
                    pass
        except (smtplib.SMTPException, ssl.SSLError, OSError) as exc:
            raise NotifyError(f"smtp failed: {type(exc).__name__}") from exc


def build(
    notifiers_cfg: Mapping,
    *,
    env: Mapping[str, str],
    run: commands.Runner,
    log_path: Path,
    platform: str = sys.platform,
    smtp_factory: Callable[[str, str, int, float], Any] | None = None,
) -> list[Notifier]:
    return [
        LogNotifier(log_path),
        MacosNotifier(notifiers_cfg["macos"], run=run, platform=platform),
        EmailNotifier(notifiers_cfg["email"], env=env, smtp_factory=smtp_factory),
    ]


def deliver(
    notifiers: Sequence[Notifier], notification: Notification, *, max_chars: int
) -> tuple[bool, list[str]]:
    """Send to every notifier. Returns (delivered, errors): delivered is True
    when at least one enabled alerting notifier succeeded (the log is a record,
    not an alert), so a caller can leave the notification pending for the next
    run when no alert got through. With no alerting notifier enabled at all
    there is nothing to retry, so it counts as delivered. Errors carry the
    exception type only, never a secret."""
    safe = Notification(
        session=notification.session,
        state=notification.state,
        title=clean_text(notification.title, max_chars),
        url=clean_text(notification.url, URL_MAX_CHARS),
        links=_safe_links(notification.links),
    )
    errors: list[str] = []
    alerting = [n for n in notifiers if _alerts(n)]
    delivered = not alerting
    for notifier in notifiers:
        try:
            notifier.send(safe)
        except (NotifyError, OSError) as exc:
            errors.append(f"{notifier.name}: {exc}")
            continue
        delivered = delivered or _alerts(notifier)
    return delivered, errors


def _safe_links(pairs: Sequence[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
    """Keep only well-formed http(s) links, with a cleaned label, capped."""
    kept: list[tuple[str, str]] = []
    for label, url in pairs:
        if not links.is_web_url(url) or len(url) > URL_MAX_CHARS:
            continue
        kept.append((clean_text(label, links.MAX_LABEL_LEN), url))
        if len(kept) == links.MAX_LINKS:
            break
    return tuple(kept)


def _alerts(notifier: Notifier) -> bool:
    """True for an enabled notifier that actually reaches the owner."""
    return bool(getattr(notifier, "enabled", True)) and bool(getattr(notifier, "alerts", True))


def send_all(
    notifiers: Sequence[Notifier], notification: Notification, *, max_chars: int
) -> list[str]:
    return deliver(notifiers, notification, max_chars=max_chars)[1]

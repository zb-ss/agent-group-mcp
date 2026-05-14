"""Chat TUI for a human participant on the bus.

Uses prompt_toolkit's PromptSession + patch_stdout() so the reader
thread can print incoming messages without ever clobbering the input
line you're typing on. A bottom toolbar always shows your identity,
the default target, and the number of unread messages in case the
reader thread is paused.

Default identity is `human` (override via AGENT_BUS_NAME or --name).
The human is registered just like any other agent so peers can address
them with `send_message(to="human", ...)`.

Commands inside the TUI:
  @<name> <body>     direct message to one peer
  /to <name>         set default target ('*' or 'all' for broadcast)
  /agents            list known agents + unread counts
  /thread <id>       reprint a thread (full or 8-char prefix)
  /history [N]       reprint last N audit `send` rows
  /help, /quit, /exit
  plain text         send to current default target
"""

from __future__ import annotations

import os
import shlex
import sys
import threading
import time
from pathlib import Path

from prompt_toolkit import HTML, PromptSession, print_formatted_text
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style

from . import audit
from . import formatting as fmt
from .paths import audit_path
from .storage import BROADCAST, Message, Storage

POLL_SECONDS = 0.4
DEFAULT_HISTORY_TAIL = 10
NAME_COL = 18
TARGET_COL = 18

HELP_LINES = (
    "agent-bus chat — commands:",
    "  @<name> <body>     direct message to one peer",
    "  /to <name|*|all>   set default target",
    "  /agents            list known agents and unread counts",
    "  /thread <id>       reprint a thread by 8-char id (or full)",
    "  /history [N]       reprint last N messages (default 10)",
    "  /clear             clear screen",
    "  /help              this help",
    "  /quit, /exit       leave",
    "  plain text         send to current default target",
    "",
    "  Note: Claude Code sessions can't be externally woken while idle.",
    "  Have each agent run `/loop 60s drain the agent-bus inbox` so peer",
    "  messages get reacted to within one poll cycle even when nobody is",
    "  typing. Alternatively: switch to the agent's terminal and press",
    "  Enter — the UserPromptSubmit hook will surface pending messages.",
)


class ChatTUI:
    def __init__(
        self,
        *,
        name: str,
        repo_path: str,
        storage: Storage | None = None,
        poll_seconds: float = POLL_SECONDS,
    ):
        self.name = name
        self.repo_path = repo_path
        self.store = storage or Storage()
        self.poll_seconds = poll_seconds
        self.default_target = BROADCAST
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._unread = 0  # only used for the bottom toolbar display

    # --------------------------- rendering helpers -------------------------

    def _style(self) -> Style:
        agents = {a.name for a in self.store.list_agents()}
        agents.add(self.name)
        return Style.from_dict(fmt.style_map(agents))

    def _emit(self, fragments: list[fmt.Fragment]) -> None:
        print_formatted_text(FormattedText(fragments), style=self._style())

    def _system(self, text: str) -> None:
        print_formatted_text(FormattedText([("class:system", text)]), style=self._style())

    # --------------------------- on-connect view ---------------------------

    def _print_connect_banner(self) -> None:
        print_formatted_text(
            FormattedText([
                ("class:prompt", "agent-bus chat "),
                ("class:system",
                 f"— connected as {self.name!r} "
                 f"(default → {self.default_target}). /help for commands."),
            ]),
            style=self._style(),
        )

    def _print_agent_roster(self) -> None:
        rows = self.store.list_agents_with_counts()
        if not rows:
            self._system("(no other agents registered yet)")
            return
        self._system(f"agents on the bus ({len(rows)}):")
        for agent, count in rows:
            tag = " (you)" if agent.name == self.name else ""
            seen = fmt.humanize_relative(agent.last_seen)
            self._emit([
                ("", "  "),
                (f"class:agent-{agent.name}", agent.name.ljust(NAME_COL)),
                ("class:system", f"  last seen {seen}{tag}"),
                ("class:thread", f"  pending={count}"),
            ])

    def _print_recent_activity(self, n: int = DEFAULT_HISTORY_TAIL) -> None:
        msgs = self.store.recent_messages(limit=n)
        if not msgs:
            return
        self._system(f"recent activity (last {len(msgs)} message(s)):")
        width = fmt.terminal_width()
        for m in msgs:
            for line in fmt.fragments_for_message_block(
                sent_at=m.sent_at,
                from_agent=m.from_agent,
                to_agent=m.to_agent,
                body=m.body,
                thread_id=m.thread_id,
                width=width,
            ):
                self._emit(line)

    # --------------------------- reader thread -----------------------------

    def _reader_loop(self) -> None:
        while not self._stop.is_set():
            try:
                msgs = self.store.read_inbox(
                    agent=self.name,
                    mark_read=True,
                    actor=self.name,
                    also_deliver=True,
                )
            except Exception as e:
                self._system(f"[reader] {e}")
                self._stop.wait(self.poll_seconds)
                continue
            for m in msgs:
                self._print_incoming(m)
            with self._lock:
                self._unread = self.store.pending_count(agent=self.name)
            self._stop.wait(self.poll_seconds)

    def _print_incoming(self, m: Message) -> None:
        for line in fmt.fragments_for_message_block(
            sent_at=m.sent_at,
            from_agent=m.from_agent,
            to_agent=m.to_agent,
            body=m.body,
            thread_id=m.thread_id,
            width=fmt.terminal_width(),
        ):
            self._emit(line)

    # --------------------------- send helpers ------------------------------

    def _send(self, to: str, body: str) -> None:
        if not body.strip():
            return
        try:
            result = self.store.send_message(
                from_agent=self.name,
                to=to,
                body=body,
                actor=self.name,
            )
        except Exception as e:
            self._system(f"[send] {e}")
            return
        ts = result.get("sent_at", "")
        thread = result.get("thread_id")
        is_broadcast = "message_ids" in result
        if is_broadcast:
            recipients = result.get("recipients", [])
            if not recipients:
                self._system("(no peers connected — message dropped)")
                return
            target_label = f"all ({len(recipients)})"
            target_class = "broadcast"
        else:
            target_label = to
            target_class = None  # use default (safe_class(to))

        for line in fmt.fragments_for_message_block(
            sent_at=ts,
            from_agent=self.name,
            to_agent=target_label,
            body=body,
            thread_id=thread,
            width=fmt.terminal_width(),
            target_class=target_class,
        ):
            self._emit(line)

    # --------------------------- command parser ---------------------------

    def _resolve_thread(self, prefix: str) -> str | None:
        """Allow `/thread abcd1234` (short form) by scanning audit log."""
        if len(prefix) == 36:  # full UUID
            return prefix
        for row in reversed(audit.tail(limit=1000)):
            tid = row.get("thread_id") or ""
            if tid.startswith(prefix):
                return tid
        return None

    def _handle_command(self, line: str) -> bool:
        try:
            parts = shlex.split(line)
        except ValueError as e:
            self._system(f"(unbalanced quoting: {e})")
            return True
        if not parts:
            return True
        cmd = parts[0].lower()
        if cmd in ("/quit", "/exit"):
            return False
        if cmd == "/help":
            for line in HELP_LINES:
                self._system(line)
            return True
        if cmd == "/clear":
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.flush()
            self._print_connect_banner()
            return True
        if cmd == "/agents":
            self._print_agent_roster()
            return True
        if cmd == "/to":
            if len(parts) < 2:
                self._system("usage: /to <name|*|all>")
                return True
            target = parts[1]
            if target.lower() in ("all", "*"):
                target = BROADCAST
            self.default_target = target
            self._system(f"default target set to {target}")
            return True
        if cmd == "/thread":
            if len(parts) < 2:
                self._system("usage: /thread <id> (8-char prefix is enough)")
                return True
            tid = self._resolve_thread(parts[1])
            if tid is None:
                self._system(f"(no thread matching {parts[1]!r})")
                return True
            msgs = self.store.read_thread(thread_id=tid)
            if not msgs:
                self._system(f"(thread {fmt.short_thread(tid)} has no messages)")
                return True
            self._system(f"thread {fmt.short_thread(tid)} — {len(msgs)} message(s):")
            width = fmt.terminal_width()
            for m in msgs:
                for line in fmt.fragments_for_message_block(
                    sent_at=m.sent_at,
                    from_agent=m.from_agent,
                    to_agent=m.to_agent,
                    body=m.body,
                    thread_id=None,  # already grouped by thread header
                    width=width,
                ):
                    self._emit(line)
            return True
        if cmd == "/history":
            n = int(parts[1]) if len(parts) >= 2 else DEFAULT_HISTORY_TAIL
            self._print_recent_activity(n)
            return True
        self._system(f"unknown command {cmd!r}; try /help")
        return True

    def _handle_line(self, line: str) -> bool:
        if not line.strip():
            return True
        if line.startswith("/"):
            return self._handle_command(line)
        if line.startswith("@"):
            head, _, body = line.partition(" ")
            target = head[1:]
            if not target or not body:
                self._system("usage: @<name> <body>")
                return True
            self._send(target, body)
            return True
        self._send(self.default_target, line)
        return True

    # --------------------------- run loop ---------------------------------

    def _bottom_toolbar(self):
        with self._lock:
            unread = self._unread
        unread_part = f" <ansired>unread:{unread}</ansired>" if unread else ""
        return HTML(
            f" <b>{self.name}</b>"
            f" → <ansicyan>{self.default_target}</ansicyan>"
            f"{unread_part}"
            f"  <ansigray>/help · /quit</ansigray>"
        )

    def _prompt_html(self):
        return HTML(
            f"<ansicyan>[</ansicyan>"
            f"<b>{self.name}</b>"
            f" → <ansicyan>{self.default_target}</ansicyan>"
            f"<ansicyan>]</ansicyan> "
        )

    def _history_path(self) -> Path:
        # Stored alongside the bus.db, NOT inside any project repo. Stays
        # user-local even if the repo is published.
        from .paths import db_path

        return db_path().parent / "chat_history"

    def run(self) -> int:
        self.store.upsert_agent(self.name, self.repo_path)

        history_path = self._history_path()
        history_path.parent.mkdir(parents=True, exist_ok=True)

        completer = WordCompleter(
            ["/agents", "/to", "/thread", "/history", "/help", "/clear", "/quit", "/exit"],
            ignore_case=True,
        )
        session: PromptSession = PromptSession(
            history=FileHistory(str(history_path)),
            completer=completer,
            complete_while_typing=False,
        )

        with patch_stdout():
            self._print_connect_banner()
            self._print_agent_roster()
            self._print_recent_activity()
            self._system("")  # blank line before live messages

            reader = threading.Thread(target=self._reader_loop, daemon=True)
            reader.start()

            try:
                while True:
                    try:
                        line = session.prompt(
                            self._prompt_html(),
                            bottom_toolbar=self._bottom_toolbar,
                            refresh_interval=1.0,
                        )
                    except (EOFError, KeyboardInterrupt):
                        break
                    if not self._handle_line(line):
                        break
            finally:
                self._stop.set()
        return 0


def main(name: str | None = None, repo_path: str | None = None) -> int:
    name = name or os.environ.get("AGENT_BUS_NAME") or "human"
    repo_path = repo_path or os.environ.get("AGENT_BUS_REPO") or str(Path.cwd())
    return ChatTUI(name=name, repo_path=repo_path).run()

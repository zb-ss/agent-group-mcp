"""Line-buffered chat TUI for a human participant.

Default identity is `human` (override via AGENT_BUS_NAME or --name). The
human is registered just like any other agent so peers can send_message
to them directly.

Reader thread polls the inbox every POLL_SECONDS and prints arrivals.
Writer is the foreground thread reading stdin. Lines starting with
`@<name> ` are direct sends; `/...` is a small command set; everything
else is a broadcast.
"""

from __future__ import annotations

import os
import shlex
import sys
import threading
import time
from pathlib import Path

from .storage import BROADCAST, Storage

POLL_SECONDS = 0.5
HELP = """\
agent-bus chat — commands:
  @<name> <body>     direct message to one peer
  /to <name>         set default target (use '*' or 'all' for broadcast)
  /agents            list known agents and unread counts
  /thread <id>       reprint a thread
  /help              this help
  /quit, /exit       leave
plain text         send to current default target (broadcast by default)
"""


def _print(line: str) -> None:
    sys.stdout.write(line.rstrip("\n") + "\n")
    sys.stdout.flush()


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
                _print(f"[chat] reader error: {e}")
                self._stop.wait(self.poll_seconds)
                continue
            for m in msgs:
                _print(
                    f"\n<{m.from_agent} → {m.to_agent}> "
                    f"[thread {m.thread_id}] {m.body}"
                )
                self._reprint_prompt()
            self._stop.wait(self.poll_seconds)

    def _reprint_prompt(self) -> None:
        sys.stdout.write(self._prompt())
        sys.stdout.flush()

    def _prompt(self) -> str:
        target = self.default_target
        return f"[{self.name} → {target}] "

    # --------------------------- send helpers ------------------------------

    def _send(self, to: str, body: str) -> None:
        if not body.strip():
            return
        result = self.store.send_message(
            from_agent=self.name,
            to=to,
            body=body,
            actor=self.name,
        )
        if "message_ids" in result:
            mids = result["message_ids"]
            recipients = result.get("recipients", [])
            if not recipients:
                _print("[chat] (no peers connected — message dropped)")
            else:
                _print(
                    f"[chat] broadcast → {', '.join(recipients)} "
                    f"({len(mids)} msg)"
                )
        else:
            _print(f"[chat] sent → {to} (id {result['message_id'][:8]})")

    # --------------------------- command parser ----------------------------

    def _handle_command(self, line: str) -> bool:
        """Return True if the chat should keep running."""
        parts = shlex.split(line)
        if not parts:
            return True
        cmd = parts[0].lower()
        if cmd in ("/quit", "/exit"):
            return False
        if cmd == "/help":
            _print(HELP)
            return True
        if cmd == "/agents":
            for agent, count in self.store.list_agents_with_counts():
                _print(
                    f"  {agent.name:<20} repo={agent.repo_path} "
                    f"last_seen={agent.last_seen} pending={count}"
                )
            return True
        if cmd == "/to":
            if len(parts) < 2:
                _print("[chat] usage: /to <name|*|all>")
                return True
            target = parts[1]
            if target.lower() in ("all", "*"):
                target = BROADCAST
            self.default_target = target
            _print(f"[chat] default target set to {target}")
            return True
        if cmd == "/thread":
            if len(parts) < 2:
                _print("[chat] usage: /thread <thread_id>")
                return True
            for m in self.store.read_thread(thread_id=parts[1]):
                _print(f"  [{m.sent_at}] {m.from_agent} → {m.to_agent}: {m.body}")
            return True
        _print(f"[chat] unknown command {cmd!r}; try /help")
        return True

    def _handle_line(self, line: str) -> bool:
        line = line.rstrip("\n")
        if not line.strip():
            return True
        if line.startswith("/"):
            return self._handle_command(line)
        if line.startswith("@"):
            head, _, body = line.partition(" ")
            target = head[1:]
            if not target or not body:
                _print("[chat] usage: @<name> <body>")
                return True
            self._send(target, body)
            return True
        self._send(self.default_target, line)
        return True

    # --------------------------- run loop ---------------------------------

    def run(self) -> int:
        self.store.upsert_agent(self.name, self.repo_path)
        _print(
            f"agent-bus chat: connected as {self.name!r} "
            f"(default target {self.default_target}). /help for commands."
        )
        reader = threading.Thread(target=self._reader_loop, daemon=True)
        reader.start()
        try:
            while True:
                self._reprint_prompt()
                try:
                    line = input()
                except EOFError:
                    break
                except KeyboardInterrupt:
                    _print("")
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

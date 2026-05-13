"""`agent-bus` command-line entrypoint.

Every subcommand talks directly to the SQLite store and the audit log —
no MCP round trip. That keeps the human first-class even when no Claude
session is running, and means humans can use the bus from any shell.

Identity for `send`/`chat` defaults to $AGENT_BUS_NAME, then "human".
The same identity is upserted into the agents table so peers can target
the human directly.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import audit
from .paths import audit_path
from .storage import BROADCAST, Storage


def _human_identity(explicit: str | None) -> str:
    return explicit or os.environ.get("AGENT_BUS_NAME") or "human"


def _repo_for(name: str) -> str:
    return os.environ.get("AGENT_BUS_REPO") or str(Path.cwd())


# --------------------------- subcommand handlers ---------------------------


def cmd_send(args: argparse.Namespace) -> int:
    store = Storage()
    identity = _human_identity(args.name)
    store.upsert_agent(identity, _repo_for(identity))

    target = args.to or BROADCAST
    body = args.body
    result = store.send_message(
        from_agent=identity,
        to=target,
        body=body,
        thread_id=args.thread,
        actor=identity,
    )
    if args.json:
        sys.stdout.write(json.dumps(result) + "\n")
    else:
        if "message_ids" in result:
            recipients = result.get("recipients", [])
            if not recipients:
                sys.stdout.write("(no peers connected — message dropped)\n")
            else:
                sys.stdout.write(
                    f"broadcast → {', '.join(recipients)} "
                    f"(thread {result['thread_id']}, "
                    f"{len(result['message_ids'])} msg)\n"
                )
        else:
            sys.stdout.write(
                f"sent → {target} (id {result['message_id']}, "
                f"thread {result['thread_id']})\n"
            )
    return 0


def cmd_inbox(args: argparse.Namespace) -> int:
    store = Storage()
    name = args.name or os.environ.get("AGENT_BUS_NAME") or "human"
    msgs = store.read_inbox(
        agent=name,
        mark_read=not args.peek,
        limit=args.limit,
        actor=name,
    )
    if args.json:
        sys.stdout.write(json.dumps([m.to_dict() for m in msgs]) + "\n")
        return 0
    if not msgs:
        sys.stdout.write(f"(no messages for {name})\n")
        return 0
    for m in msgs:
        sys.stdout.write(
            f"[{m.sent_at}] from {m.from_agent} → {m.to_agent} "
            f"(thread {m.thread_id})\n  {m.body}\n"
        )
    return 0


def cmd_agents(args: argparse.Namespace) -> int:
    store = Storage()
    rows = store.list_agents_with_counts()
    if args.json:
        sys.stdout.write(
            json.dumps([a.to_dict(pending_count=c) for a, c in rows]) + "\n"
        )
        return 0
    if not rows:
        sys.stdout.write("(no agents registered yet)\n")
        return 0
    for agent, count in rows:
        sys.stdout.write(
            f"{agent.name:<20} repo={agent.repo_path}  "
            f"last_seen={agent.last_seen}  pending={count}\n"
        )
    return 0


def cmd_tail(args: argparse.Namespace) -> int:
    log = audit_path()
    if not args.follow:
        rows = audit.tail(limit=args.limit)
        for r in rows:
            sys.stdout.write(json.dumps(r) + "\n")
        return 0

    # follow mode: print last N, then stream new lines as they arrive
    if log.exists():
        with log.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        for line in lines[-args.limit :]:
            sys.stdout.write(line if line.endswith("\n") else line + "\n")
        sys.stdout.flush()

    last_size = log.stat().st_size if log.exists() else 0
    last_inode = log.stat().st_ino if log.exists() else None
    try:
        while True:
            time.sleep(0.2)
            if not log.exists():
                last_size = 0
                last_inode = None
                continue
            st = log.stat()
            # handle rotation: inode changed → reread from start
            if last_inode is not None and st.st_ino != last_inode:
                last_size = 0
            last_inode = st.st_ino
            if st.st_size < last_size:
                # truncated
                last_size = 0
            if st.st_size > last_size:
                with log.open("r", encoding="utf-8", errors="replace") as f:
                    f.seek(last_size)
                    chunk = f.read()
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
                    last_size = st.st_size
    except KeyboardInterrupt:
        return 0


def cmd_chat(args: argparse.Namespace) -> int:
    from .chat_tui import main as chat_main

    return chat_main(name=args.name, repo_path=args.repo)


def cmd_hook_stop(args: argparse.Namespace) -> int:
    from .hooks import run_hook_stop

    return run_hook_stop()


def cmd_hook_user_prompt(args: argparse.Namespace) -> int:
    from .hooks import run_hook_user_prompt

    return run_hook_user_prompt()


def cmd_serve(args: argparse.Namespace) -> int:  # pragma: no cover
    from .server import main as server_main

    server_main()
    return 0


# --------------------------- argparse wiring -------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agent-bus",
        description=(
            "Local multi-agent message bus. Tools talk over MCP; the CLI "
            "talks directly to the SQLite store + audit log."
        ),
    )
    sub = p.add_subparsers(dest="command", required=True)

    # send
    s = sub.add_parser("send", help="Send a message (default: broadcast)")
    s.add_argument("body")
    s.add_argument("--to", help="Recipient name. Default '*' (broadcast).")
    s.add_argument("--thread", help="Continue an existing thread by ID.")
    s.add_argument("--name", help="Identity to send as. Default env or 'human'.")
    s.add_argument("--json", action="store_true", help="Emit JSON result.")
    s.set_defaults(func=cmd_send)

    # inbox
    s = sub.add_parser("inbox", help="Read this agent's unread messages.")
    s.add_argument("--name", help="Whose inbox to read. Default env or 'human'.")
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--peek", action="store_true", help="Don't mark messages read.")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_inbox)

    # agents
    s = sub.add_parser("agents", help="List registered agents.")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_agents)

    # tail
    s = sub.add_parser("tail", help="Tail the audit log.")
    s.add_argument("-f", "--follow", action="store_true")
    s.add_argument("--limit", type=int, default=50)
    s.set_defaults(func=cmd_tail)

    # chat
    s = sub.add_parser("chat", help="Line-buffered chat TUI for a human.")
    s.add_argument("--name", help="Default env or 'human'.")
    s.add_argument("--repo", help="repo_path to register. Default $PWD.")
    s.set_defaults(func=cmd_chat)

    # hook entrypoints
    s = sub.add_parser("hook-stop", help="Claude Code Stop hook handler.")
    s.set_defaults(func=cmd_hook_stop)

    s = sub.add_parser(
        "hook-user-prompt", help="Claude Code UserPromptSubmit hook handler."
    )
    s.set_defaults(func=cmd_hook_user_prompt)

    # optional serve helper (so `agent-bus serve` works besides python -m)
    s = sub.add_parser("serve", help="Run the MCP stdio server (same as python -m agent_bus.server).")
    s.set_defaults(func=cmd_serve)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

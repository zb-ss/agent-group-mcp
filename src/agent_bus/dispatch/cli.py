"""`agent-bus dispatch ...` subcommands.

Imported on every `agent-bus` call (hooks included), so only the standard
library's argparse/json/sys load here; everything else loads in the handlers.
"""

from __future__ import annotations

import argparse
import json
import sys


def _err(message: str) -> None:
    sys.stderr.write(f"agent-bus: {message}\n")


def _out(text: str) -> None:
    sys.stdout.write(text + "\n")


def _decision_line(d: dict) -> str:
    who = d["label"] or d["session"] or "-"
    line = f"{d['result']} {d['type']} {who} {d['title']}"
    return f"{line} ({d['reason']})" if d["reason"] else line


def cmd_run(args: argparse.Namespace) -> int:
    import os

    from .. import commands, config
    from ..storage import Storage
    from . import envfile, notify, runner, settings
    from .store import DispatchStore

    try:
        cfg = config.load()
        d = cfg["dispatch"]
        env = envfile.load(d["env_file"], base=os.environ)
        sources = runner.build_sources(d, env=env)
    except config.ConfigError as e:
        _err(str(e))
        return 1
    if not sources:
        _err(
            "no dispatch source is enabled "
            "(set dispatch.sources.asana.enabled in config.json)"
        )
        return 0
    notifiers = notify.build(
        d["notifiers"], env=env, run=commands.run_command, log_path=settings.log_path(d)
    )
    report = runner.run(
        cfg=cfg,
        storage=Storage(),
        store=DispatchStore(),
        sources=sources,
        notifiers=notifiers,
        dry_run=args.dry_run,
        ignore_hours=args.ignore_hours,
    )
    if args.json:
        _out(json.dumps(report.to_dict(), indent=2))
        return report.exit_code
    if report.skipped:
        _out(f"dispatch: skipped ({report.skipped})")
    else:
        for decision in report.to_dict()["decisions"]:
            _out(_decision_line(decision))
        for transition in report.transitions:
            _out("transition: " + " ".join(f"{k}={v}" for k, v in transition.items()))
        for entry in report.cleanups:
            if entry["result"] == "kept" and not args.dry_run:
                continue
            reason = f" ({entry['reason']})" if entry.get("reason") else ""
            _out(f"cleanup: {entry['result']} {entry['work_dir']}{reason}")
    for error in report.errors:
        _err(error)
    return report.exit_code


def cmd_status(args: argparse.Namespace) -> int:
    from .. import config, status
    from ..storage import Storage
    from .model import clean_text
    from .runner import find_row
    from .store import DispatchStore

    try:
        cfg = config.load()
    except config.ConfigError as e:
        sys.stderr.write(f"agent-bus: warning: {e}; using default settings\n")
        cfg = config.defaults()
    records = DispatchStore().list_tasks()
    rows = status.collect(
        Storage(), grace_seconds=cfg["spawn"]["startup_grace_seconds"], include_ended=True
    )

    clients = cfg["spawn"]["clients"]

    def session(record) -> tuple[str | None, str, str | None]:
        """(current name, state, resume command). A session may have renamed
        itself since the spawn, so resolve it the way the runner does, not by
        the stored name."""
        if not record.session_name:
            return None, "-", None
        row = find_row(rows, record)
        if row is None:
            return record.session_name, "ended", None
        return row.name, row.state, _resume_for(row, clients)

    if args.json:
        out = []
        for r in records:
            name, state, resume = session(r)
            out.append({**r.to_dict(), "current_session": name, "session_state": state,
                        "resume": resume})
        _out(json.dumps(out, indent=2))
        return 0
    if not records:
        _out("(no dispatched tasks)")
        return 0
    max_chars = cfg["dispatch"]["title_max_chars"]
    for r in records:
        name, state, resume = session(r)
        _out(
            "  ".join(
                [
                    r.task_id,
                    r.type or "-",
                    r.outcome or "-",
                    name or "-",
                    state,
                    clean_text(r.title, max_chars),
                    r.url,
                ]
            )
        )
        if resume:
            _out(f"    resume: {resume}")
        if r.worktree_removed_at:
            _out(f"    worktree removed: {r.worktree_removed_at}")
        if r.held_at and not r.approved_at:
            _out(f"    held: needs approval (agent-bus dispatch approve {r.task_id})")
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    from datetime import datetime, timezone

    from ..storage import TIMESTAMP_FORMAT
    from .store import DispatchStore

    store = DispatchStore()
    matches = [
        r for r in store.list_tasks()
        if r.task_id == args.task_id and (args.source is None or r.source == args.source)
    ]
    if not matches:
        _err(f"unknown task {args.task_id}")
        return 1
    if len(matches) > 1:
        sources = ", ".join(sorted(r.source for r in matches))
        _err(f"task {args.task_id} exists in several sources ({sources}); pass --source")
        return 1
    record = matches[0]
    store.update(
        record.source, record.task_id,
        approved_at=datetime.now(timezone.utc).strftime(TIMESTAMP_FORMAT),
    )
    _out(f"approved {record.task_id}: the next run starts it (the hard limit still applies)")
    return 0


def _resume_for(row, clients) -> str | None:
    """How to reopen a spawned session, from its client's `resume` template."""
    from ..config import ConfigError
    from ..spawn import resume_command

    template = clients.get(row.client)
    if not (row.spawned and row.work_dir and row.tmux_window and template):
        return None
    try:
        return resume_command(
            template, label=row.tmux_window, work_dir=row.work_dir, client=row.client
        )
    except ConfigError as exc:
        sys.stderr.write(f"agent-bus: warning: {exc}\n")
        return None


def _timer_plan(args: argparse.Namespace, *, uninstall: bool = False):
    from .. import config, init_cmd
    from . import timer

    cfg = config.load()
    bin_path = init_cmd.detect_agent_bus_bin(args.bin_path)
    return cfg, timer.plan(cfg, bin_path=bin_path, uninstall=uninstall)


def cmd_install_timer(args: argparse.Namespace) -> int:
    import shlex

    from .. import commands, config
    from . import timer

    try:
        cfg, plan = _timer_plan(args)
        written = timer.install(
            plan,
            load=args.load,
            run=commands.run_command,
            timeout=cfg["dispatch"]["timer"]["timeout_seconds"],
        )
    except (config.ConfigError, timer.TimerError) as e:
        _err(str(e))
        return 1
    for path in written:
        _out(f"wrote {path}")
    if args.load:
        _out("loaded")
    else:
        _out("not loaded; to load it run:")
        for command in plan.load_commands:
            _out("  " + shlex.join(command))
    return 0


def cmd_uninstall_timer(args: argparse.Namespace) -> int:
    import shlex

    from .. import commands, config
    from . import timer

    try:
        cfg, plan = _timer_plan(args, uninstall=True)
        removed = timer.uninstall(
            plan,
            unload=args.unload,
            run=commands.run_command,
            timeout=cfg["dispatch"]["timer"]["timeout_seconds"],
        )
    except (config.ConfigError, timer.TimerError) as e:
        _err(str(e))
        return 1
    if not removed:
        _out("nothing to remove")
    for path in removed:
        _out(f"removed {path}")
    if not args.unload:
        _out("not unloaded; to unload it run:")
        for command in plan.unload_commands:
            _out("  " + shlex.join(command))
    return 0


def register(sub) -> None:
    d = sub.add_parser(
        "dispatch",
        help="Start sessions for due tasks from a task source, on a timer.",
        description=(
            "Read due tasks from a task source, map them to repos, and start "
            "agent sessions with a playbook prompt (or only notify). Meant to "
            "run from a timer: see `dispatch install-timer`."
        ),
    )
    dsub = d.add_subparsers(dest="dispatch_command", required=True)

    s = dsub.add_parser("run", help="Run one dispatch pass.")
    s.add_argument("--dry-run", action="store_true", help="Show what would happen; change nothing.")
    s.add_argument("--json", action="store_true", help="Emit JSON result.")
    s.add_argument(
        "--ignore-hours", action="store_true", help="Run even outside dispatch.working_hours."
    )
    s.set_defaults(func=cmd_run)

    s = dsub.add_parser("status", help="List dispatched tasks and their session state.")
    s.add_argument("--json", action="store_true", help="Emit JSON rows.")
    s.set_defaults(func=cmd_status)

    s = dsub.add_parser(
        "approve", help="Let a held task start past the soft limit on the next run."
    )
    s.add_argument("task_id", metavar="TASK_ID", help="The held task's id.")
    s.add_argument("--source", help="Task source name, when the id exists in several.")
    s.set_defaults(func=cmd_approve)

    s = dsub.add_parser("install-timer", help="Write the launchd/systemd timer files.")
    s.add_argument("--load", action="store_true", help="Also load/enable the timer now.")
    s.add_argument(
        "--bin-path",
        help="Absolute path of the agent-bus binary the timer runs. "
        "Default: $AGENT_BUS_BIN or the one on PATH.",
    )
    s.set_defaults(func=cmd_install_timer)

    s = dsub.add_parser("uninstall-timer", help="Remove the timer files.")
    s.add_argument("--unload", action="store_true", help="Also unload/disable the timer.")
    s.add_argument(
        "--bin-path",
        help="Absolute path of the agent-bus binary the timer runs. "
        "Default: $AGENT_BUS_BIN or the one on PATH.",
    )
    s.set_defaults(func=cmd_uninstall_timer)

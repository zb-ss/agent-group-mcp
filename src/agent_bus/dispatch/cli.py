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


def _message_line(m: dict) -> str:
    """One line per message decision; never any message text."""
    line = f"message: {m['result']} {m['source']} {m.get('rule') or '-'} {m.get('url') or '-'}"
    return f"{line} ({m['reason']})" if m.get("reason") else line


def _keep_dispatcher_alive(cfg, storage) -> None:
    from . import dispatcher

    try:
        if dispatcher.find_row(dispatcher._rows(cfg, storage, None)) is None:
            dispatcher.start(cfg, storage=storage)
    except Exception as e:  # best effort: a failure here must never stop the run
        _err(f"warning: dispatcher not kept alive: {e}")


def cmd_run(args: argparse.Namespace) -> int:
    import os

    from .. import commands, config
    from ..storage import Storage
    from datetime import datetime

    from . import envfile, notify, runner, scan, settings
    from .store import DispatchStore

    try:
        cfg = config.load()
        d = cfg["dispatch"]
        env = envfile.load(d["env_file"], base=os.environ)
        sources = runner.build_sources(d, env=env)
        message_sources = scan.build_sources(d, env=env)
    except config.ConfigError as e:
        _err(str(e))
        return 1
    if not sources and not message_sources:
        _err(
            "no dispatch source is enabled "
            "(set dispatch.sources.<name>.enabled in config.json)"
        )
        return 0
    notifiers = notify.build(
        d["notifiers"], env=env, run=commands.run_command, log_path=settings.log_path(d)
    )
    storage = Storage()
    in_hours = args.ignore_hours or settings.within_working_hours(
        d["working_hours"], datetime.now().astimezone()
    )
    if d["dispatcher"]["keep_alive"] and not args.dry_run and in_hours:
        _keep_dispatcher_alive(cfg, storage)
    report = runner.run(
        cfg=cfg,
        storage=storage,
        store=DispatchStore(),
        sources=sources,
        notifiers=notifiers,
        dry_run=args.dry_run,
        ignore_hours=args.ignore_hours,
        message_sources=message_sources,
        env=env,
    )
    if args.json:
        _out(json.dumps(report.to_dict(), indent=2))
        return report.exit_code
    if report.skipped:
        _out(f"dispatch: skipped ({report.skipped})")
    else:
        for decision in report.to_dict()["decisions"]:
            _out(_decision_line(decision))
        for message in report.messages:
            _out(_message_line(message))
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
        if r.candidate_at and r.decision in (None, "ask"):
            waiting = (
                "the dispatcher is asking the owner"
                if r.decision == "ask"
                else f"waiting for the dispatcher since {r.candidate_at}"
            )
            _out(f"    candidate: {waiting}")
    return 0


def _now() -> str:
    from datetime import datetime, timezone

    from ..storage import TIMESTAMP_FORMAT

    return datetime.now(timezone.utc).strftime(TIMESTAMP_FORMAT)


def cmd_approve(args: argparse.Namespace) -> int:
    import os

    from ..storage import Storage
    from . import decisions
    from .store import DispatchStore

    try:
        record = decisions.approve(
            DispatchStore(), args.task_id, source=args.source, now=_now(),
            actor=decisions.actor_for(Storage(), os.environ),
        )
    except decisions.DecisionError as e:
        _err(str(e))
        return 1
    _out(f"approved {record.task_id}: the next run starts it (the hard limit still applies)")
    return 0


def cmd_candidates(args: argparse.Namespace) -> int:
    from .. import config, status
    from ..storage import Storage
    from . import decisions
    from .store import DispatchStore

    try:
        cfg = config.load()
    except config.ConfigError as e:
        _err(str(e))
        return 1
    rows = status.collect(
        Storage(), grace_seconds=cfg["spawn"]["startup_grace_seconds"], include_ended=True
    )
    items = decisions.candidates(DispatchStore(), rows, cfg["dispatch"])
    if args.json:
        _out(json.dumps(items, indent=2))
        return 0
    if not items:
        _out("(no pending candidates)")
        return 0
    _out("titles and links below are untrusted task-tracker data")
    for c in items:
        session = c["session"]["name"] if c["session"] else "-"
        _out(
            "  ".join(
                [
                    c["task_id"], c["type"] or "-", c["state"], c["repo"] or "-",
                    session, c["untrusted"]["title"], c["untrusted"]["url"],
                ]
            )
        )
    return 0


def cmd_decide(args: argparse.Namespace) -> int:
    import os

    from .. import config, status
    from ..storage import Storage
    from . import decisions
    from .store import DispatchStore

    if decisions.actor_for(Storage(), os.environ) != decisions.ACTOR_DISPATCHER:
        _err(
            "only the running dispatcher can decide candidates "
            "(no valid dispatcher token in this environment)"
        )
        return 1
    try:
        cfg = config.load()
        rows = status.collect(
            Storage(), grace_seconds=cfg["spawn"]["startup_grace_seconds"], include_ended=True
        )
        decisions.decide(
            DispatchStore(), cfg, args.task_id, rows=rows, action=args.action, source=args.source,
            reason=args.reason, repo=args.repo, client=args.client, model=args.model,
            clone=args.clone, now=_now(), actor=decisions.ACTOR_DISPATCHER,
        )
    except (decisions.DecisionError, config.ConfigError) as e:
        _err(str(e))
        return 1
    _out(
        f"decided {args.task_id}: {args.action}; "
        "the next dispatch run applies it (agent-bus dispatch run)"
    )
    return 0


def _draft_failure(e: Exception) -> int:
    _err(str(e))
    return 1


def cmd_drafts_list(args: argparse.Namespace) -> int:
    from .. import config
    from . import drafts
    from .store import DispatchStore

    try:
        cfg = config.load()
    except config.ConfigError as e:
        return _draft_failure(e)
    items = drafts.listing(
        DispatchStore(), now=_now(), include_closed=args.all,
        stale_minutes=cfg["dispatch"]["drafts"]["sending_stale_minutes"],
    )
    if args.json:
        _out(json.dumps(items, indent=2))
        return 0
    if not items:
        _out("(no pending drafts)")
        return 0
    _out("ID  STATUS  KIND  SOURCE  RULE  URL")
    for i in items:
        status = f"{i['status']}(stale)" if i["stale"] else i["status"]
        _out("  ".join(
            [str(i["id"]), status, i["kind"], i["source"], i["rule"] or "-", i["url"] or "-"]
        ))
    if any(i["stale"] for i in items):
        _out(
            "a stale draft's delivery never recorded a result; check its destination, then "
            "`agent-bus dispatch drafts reset ID` makes it pending again"
        )
    return 0


def cmd_drafts_show(args: argparse.Namespace) -> int:
    from .. import config
    from . import drafts
    from .store import DispatchStore

    try:
        config.load()
        store = DispatchStore()
        item = drafts.show(store, args.draft_id)
        draft = store.get_draft(args.draft_id)
    except (drafts.DraftError, config.ConfigError) as e:
        return _draft_failure(e)
    if args.json:
        _out(json.dumps(item, indent=2))
        return 0
    _out(f"draft {item['id']}  {item['status']}  {item['kind']}  {item['source']}  "
         f"{item['rule'] or '-'}  {item['url'] or '-'}")
    for key, value in sorted(item["target"].items()):
        _out(f"  {key}: {value}")
    _out(
        f"lines starting with {drafts.UNTRUSTED_PREFIX.strip()!r} are untrusted: the "
        "sender's headers and a reply drafted by a model from untrusted message data; "
        "review them before approving"
    )
    for line in drafts.untrusted_lines(draft):
        _out(line)
    return 0


def _owner(drafts, store, draft_id: int, operation: str, storage, env) -> str:
    """The acting owner; a refused agent session is written to the decision log."""
    try:
        return drafts.owner_actor(storage, env)
    except drafts.DraftRefused:
        drafts.log_refused(store, draft_id, operation=operation, env=env, now=_now())
        raise


def cmd_drafts_edit(args: argparse.Namespace) -> int:
    import os

    from .. import config
    from ..storage import Storage
    from . import drafts
    from .store import DispatchStore

    try:
        cfg = config.load()
        store = DispatchStore()
        actor = _owner(drafts, store, args.draft_id, "edit", Storage(), os.environ)
        if args.file == "-":
            text = sys.stdin.read()
        else:
            with open(args.file, encoding="utf-8") as handle:
                text = handle.read()
        drafts.edit(
            store, args.draft_id, text, actor=actor, now=_now(),
            max_chars=drafts.max_chars_for(
                drafts.pending_draft(store, args.draft_id), cfg["dispatch"]
            ),
        )
    except (drafts.DraftError, config.ConfigError, OSError, UnicodeDecodeError) as e:
        return _draft_failure(e)
    _out(f"edited draft {args.draft_id}")
    return 0


def _stdin_is_tty() -> bool:
    return sys.stdin.isatty()


def _ask(prompt: str) -> str:
    return input(prompt)


def cmd_drafts_approve(args: argparse.Namespace) -> int:
    import os
    from datetime import datetime, timezone

    from .. import config
    from ..storage import Storage
    from . import drafts, envfile
    from .store import DispatchStore

    try:
        cfg = config.load()
        env = envfile.load(cfg["dispatch"]["env_file"], base=os.environ)
        store = DispatchStore()
        actor = _owner(drafts, store, args.draft_id, "approve", Storage(), os.environ)
        drafts.confirm_owner_approval(
            drafts.pending_draft(store, args.draft_id), cfg, actor=actor,
            assume_yes=args.yes, is_tty=_stdin_is_tty, ask=_ask, show=_out,
        )
        done = drafts.approve(
            store, cfg, args.draft_id, actor=actor,
            now=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), env=env,
        )
    except (drafts.DraftError, config.ConfigError, envfile.EnvFileError) as e:
        return _draft_failure(e)
    if done.kind == drafts.KIND_SLACK:
        _out(f"sent draft {args.draft_id} to Slack")
    else:
        _out(
            f"placed draft {args.draft_id} in the mailbox Drafts folder; "
            "send it from your mail client"
        )
    return 0


def cmd_drafts_discard(args: argparse.Namespace) -> int:
    import os

    from .. import config
    from ..storage import Storage
    from . import drafts
    from .store import DispatchStore

    try:
        config.load()
        store = DispatchStore()
        actor = _owner(drafts, store, args.draft_id, "discard", Storage(), os.environ)
        drafts.discard(store, args.draft_id, actor=actor, now=_now())
    except (drafts.DraftError, config.ConfigError) as e:
        return _draft_failure(e)
    _out(f"discarded draft {args.draft_id}")
    return 0


def cmd_drafts_reset(args: argparse.Namespace) -> int:
    import os

    from .. import config
    from ..storage import Storage
    from . import drafts
    from .store import DispatchStore

    try:
        cfg = config.load()
        stale_minutes = cfg["dispatch"]["drafts"]["sending_stale_minutes"]
        store = DispatchStore()
        actor = _owner(drafts, store, args.draft_id, "reset", Storage(), os.environ)
        now = _now()
        drafts.confirm_owner_reset(
            drafts.stale_draft(store, args.draft_id, now=now, stale_minutes=stale_minutes),
            actor=actor, assume_yes=args.yes, is_tty=_stdin_is_tty, ask=_ask, show=_out,
        )
        drafts.reset(store, args.draft_id, actor=actor, now=now, stale_minutes=stale_minutes)
    except (drafts.DraftError, config.ConfigError) as e:
        return _draft_failure(e)
    _out(f"draft {args.draft_id} is pending again; approve it once you checked its destination")
    return 0


def cmd_incidents_list(args: argparse.Namespace) -> int:
    from .. import config
    from . import incident
    from .store import DispatchStore

    try:
        config.load()
    except config.ConfigError as e:
        return _draft_failure(e)
    items = incident.listing(DispatchStore(), include_closed=args.all)
    if args.json:
        _out(json.dumps(items, indent=2))
        return 0
    if not items:
        _out("(no open incidents)")
        return 0
    _out("ID  STATE  SOURCE  RULE  OPENED  REPEATS  SESSION  URL")
    for i in items:
        _out("  ".join([
            str(i["id"]), i["state"], i["source"], i["rule"] or "-", i["opened_at"],
            str(i["repeats"]), i["session"] or "-", i["url"] or "-",
        ]))
    return 0


def cmd_incidents_close(args: argparse.Namespace) -> int:
    import os

    from .. import config, status
    from ..storage import Storage
    from . import incident
    from .store import DispatchStore

    try:
        cfg = config.load()
        storage = Storage()
        actor = incident.manual_actor(storage, os.environ)
        rows = status.collect(
            storage, grace_seconds=cfg["spawn"]["startup_grace_seconds"], include_ended=True
        )
        told = incident.close_manual(
            DispatchStore(), args.incident_id, actor=actor, stamp=_now(), rows=rows,
            send=storage.send_message, sender=cfg["dispatch"]["bus_sender"],
        )
    except (incident.IncidentError, config.ConfigError) as e:
        return _draft_failure(e)
    _out(f"closed incident {args.incident_id}" + (" and told its session" if told else ""))
    return 0


def cmd_scanner_check(args: argparse.Namespace) -> int:
    import os

    from .. import config
    from . import envfile, scanner

    try:
        cfg = config.load()
        env = envfile.load(cfg["dispatch"]["env_file"], base=os.environ)
    except (config.ConfigError, envfile.EnvFileError) as e:
        return _draft_failure(e)
    result = scanner.check(cfg["dispatch"], env=env)
    if result.ok:
        _out("scanner check passed: no file created, output parses")
        return 0
    for problem in result.problems:
        _err(f"scanner check failed: {problem}")
    return 1


def cmd_decisions(args: argparse.Namespace) -> int:
    from .store import DispatchStore

    events = DispatchStore().list_events(event=args.event, limit=args.limit)
    if args.json:
        _out(json.dumps(events, indent=2))
        return 0
    if not events:
        _out("(no logged decisions)")
        return 0
    for e in events:
        _out(
            f"{e['at']}  {e['event']}  {e['actor']}  {e['source'] or '-'}:{e['task_id'] or '-'}  "
            + json.dumps(e["detail"], sort_keys=True)
        )
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


def _dispatcher_error(exc) -> None:
    hint = getattr(exc, "hint", None)
    _err(f"{exc} (hint: {hint})" if hint else str(exc))


def cmd_dispatcher_start(args: argparse.Namespace) -> int:
    from .. import config
    from ..storage import Storage
    from . import dispatcher

    try:
        cfg = config.load()
        result = dispatcher.start(cfg, storage=Storage(), dry_run=args.dry_run)
    except (config.ConfigError, dispatcher.DispatcherError) as e:
        _dispatcher_error(e)
        return 1
    if args.json:
        _out(json.dumps(result.to_dict(), indent=2))
        return 0
    if result.already_running:
        _out(f"dispatcher already running as {result.name}")
        return 0
    for note in result.notes:
        _out(note)
    if not result.command:
        return 0
    if not result.launched:
        _out(f"command: {result.command}")
        return 0
    _out(
        f"launched {result.name} in tmux {result.tmux_session}:{result.tmux_window} "
        f"({result.work_dir})"
    )
    _out(f"attach: tmux attach -t {result.tmux_session}")
    _out("if the client asks whether to trust this folder, answer once in that window")
    return 0


def cmd_dispatcher_stop(args: argparse.Namespace) -> int:
    from .. import config
    from ..storage import Storage
    from . import dispatcher

    try:
        closed = dispatcher.stop(config.load(), storage=Storage())
    except (config.ConfigError, dispatcher.DispatcherError) as e:
        _dispatcher_error(e)
        return 1
    _out(f"dispatcher stopped ({closed} window(s) closed, authority revoked)")
    return 0


def cmd_dispatcher_status(args: argparse.Namespace) -> int:
    from .. import config
    from ..storage import Storage
    from . import dispatcher
    from .store import DispatchStore

    try:
        result = dispatcher.status(config.load(), storage=Storage(), store=DispatchStore())
    except (config.ConfigError, dispatcher.DispatcherError) as e:
        _dispatcher_error(e)
        return 1
    if args.json:
        _out(json.dumps(result.to_dict(), indent=2))
        return 0
    if result.running:
        _out(f"dispatcher running as {result.name} ({result.state}) in tmux {result.tmux}")
    else:
        _out("dispatcher not running")
    _out(f"authority: {'active' if result.authority else 'none'}")
    _out(f"pending candidates: {result.pending_candidates}")
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

    s = dsub.add_parser("candidates", help="List tasks waiting for the dispatcher's decision.")
    s.add_argument("--json", action="store_true", help="Emit JSON rows.")
    s.set_defaults(func=cmd_candidates)

    s = dsub.add_parser(
        "decide", help="Record the dispatcher's decision on a candidate (dispatcher only)."
    )
    s.add_argument("task_id", metavar="TASK_ID", help="The candidate task's id.")
    s.add_argument("--action", required=True, choices=("start", "resume", "skip", "ask"))  # mirrors settings.DECISIONS; not imported here
    s.add_argument("--source", help="Task source name, when the id exists in several.")
    s.add_argument("--reason", help="Why; kept in the decision log.")
    s.add_argument("--repo", help="Repo remote for a task of an unmapped project.")
    s.add_argument("--client", help="Client to start the session with.")
    s.add_argument("--model", help="Model for that client (needs --client).")
    s.add_argument(
        "--clone", action="store_true",
        help="Clone the repository when no local clone exists (start and resume only).",
    )
    s.set_defaults(func=cmd_decide)

    p = dsub.add_parser("drafts", help="Review, edit, approve or discard reply drafts.")
    psub = p.add_subparsers(dest="drafts_command", required=True)

    s = psub.add_parser("list", help="List pending drafts and drafts still sending.")
    s.add_argument("--all", action="store_true", help="Include sent, placed and discarded drafts.")
    s.add_argument("--json", action="store_true", help="Emit JSON rows.")
    s.set_defaults(func=cmd_drafts_list)

    s = psub.add_parser("show", help="Show one draft (its text is untrusted model output).")
    s.add_argument("draft_id", type=int, metavar="ID")
    s.add_argument("--json", action="store_true", help="Emit JSON.")
    s.set_defaults(func=cmd_drafts_show)

    s = psub.add_parser("edit", help="Replace a pending draft's text from a file.")
    s.add_argument("draft_id", type=int, metavar="ID")
    s.add_argument("--file", required=True, help="File with the new text; - reads stdin.")
    s.set_defaults(func=cmd_drafts_edit)

    s = psub.add_parser(
        "approve", help="Deliver a draft: post to Slack, or place it in the mailbox Drafts folder."
    )
    s.add_argument("draft_id", type=int, metavar="ID")
    s.add_argument(
        "--yes", action="store_true",
        help="Skip the terminal confirmation; accepted only for the verified dispatcher.",
    )
    s.set_defaults(func=cmd_drafts_approve)

    s = psub.add_parser("discard", help="Discard a pending draft.")
    s.add_argument("draft_id", type=int, metavar="ID")
    s.set_defaults(func=cmd_drafts_discard)

    s = psub.add_parser(
        "reset",
        help="Make a draft stuck in sending (past dispatch.drafts.sending_stale_minutes) "
        "pending again.",
    )
    s.add_argument("draft_id", type=int, metavar="ID")
    s.add_argument(
        "--yes", action="store_true",
        help="Skip the terminal confirmation; accepted only for the verified dispatcher.",
    )
    s.set_defaults(func=cmd_drafts_reset)

    p = dsub.add_parser("incidents", help="List or close infrastructure incidents.")
    isub = p.add_subparsers(dest="incidents_command", required=True)

    s = isub.add_parser("list", help="List open incidents.")
    s.add_argument("--all", action="store_true", help="Include closed incidents.")
    s.add_argument("--json", action="store_true", help="Emit JSON rows.")
    s.set_defaults(func=cmd_incidents_list)

    s = isub.add_parser("close", help="Close an open incident and tell its session.")
    s.add_argument("incident_id", type=int, metavar="ID")
    s.set_defaults(func=cmd_incidents_close)

    p = dsub.add_parser("scanner", help="Check the message scanner.")
    ssub = p.add_subparsers(dest="scanner_command", required=True)

    s = ssub.add_parser(
        "check",
        help="Run a read-only canary with the configured scanner (exit 1 on failure).",
    )
    s.set_defaults(func=cmd_scanner_check)

    s = dsub.add_parser("decisions", help="Show the decision log (candidates, decisions, routing).")
    s.add_argument("--json", action="store_true", help="Emit JSON rows.")
    s.add_argument("--event", help="Only this event, e.g. routing.")
    s.add_argument("--limit", type=int, help="Only the newest N rows.")
    s.set_defaults(func=cmd_decisions)

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

    r = sub.add_parser(
        "dispatcher",
        help="Run the always-on dispatcher session the owner talks to.",
        description="Start, stop or inspect the dispatcher session (kept in tmux).",
    )
    rsub = r.add_subparsers(dest="dispatcher_command", required=True)

    s = rsub.add_parser("start", help="Start the dispatcher session in tmux.")
    s.add_argument("--dry-run", action="store_true", help="Show what would happen; change nothing.")
    s.add_argument("--json", action="store_true", help="Emit JSON result.")
    s.set_defaults(func=cmd_dispatcher_start)

    s = rsub.add_parser("stop", help="Close the dispatcher's window and revoke its authority.")
    s.set_defaults(func=cmd_dispatcher_stop)

    s = rsub.add_parser("status", help="Show the dispatcher's state and pending candidates.")
    s.add_argument("--json", action="store_true", help="Emit JSON result.")
    s.set_defaults(func=cmd_dispatcher_status)

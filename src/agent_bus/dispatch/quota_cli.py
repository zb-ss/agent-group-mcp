"""`agent-bus quota ...` subcommands.

Imported on every `agent-bus` call (hooks included), so only argparse/json/sys
load here; everything else loads in the handlers.
"""

from __future__ import annotations

import argparse
import json
import sys


def _err(message: str) -> None:
    sys.stderr.write(f"agent-bus: {message}\n")


def _out(text: str) -> None:
    sys.stdout.write(text + "\n")


def _echo(data: bytes) -> None:
    """Pass the status line input through untouched; a closed pipe is fine."""
    try:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
    except (BrokenPipeError, ValueError, OSError):
        pass


def _record(data: bytes, client: str) -> None:
    from datetime import datetime, timezone

    from .. import config
    from . import quota, settings

    payload = json.loads(data)
    windows = quota.parse_statusline(payload)
    if not windows:
        return
    try:
        cfg = config.load()
    except config.ConfigError:
        cfg = config.defaults()
    d = cfg["dispatch"]
    quota.write_snapshot(
        settings.quota_dir(d),
        client,
        windows,
        now=datetime.now(timezone.utc),
        min_interval_seconds=d["routing"]["quota"]["record_min_interval_seconds"],
    )


def cmd_record(args: argparse.Namespace) -> int:
    """Echo stdin to stdout first, then store the usage windows best-effort.
    Never fails: it sits inside a status line pipeline."""
    try:
        data = sys.stdin.buffer.read()
    except (OSError, ValueError):
        return 0
    _echo(data)
    try:
        _record(data, args.client)
    except Exception:  # noqa: BLE001 - best effort: a status line must never break
        # Bad JSON, a ConfigError, an unwritable folder: stay silent, exit 0.
        pass
    return 0


def _fmt_reset(epoch) -> str:
    from datetime import datetime, timezone

    if epoch is None:
        return "-"
    try:
        return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return "-"


def cmd_show(args: argparse.Namespace) -> int:
    from datetime import datetime, timezone

    from .. import config
    from . import quota, settings

    try:
        cfg = config.load()
    except config.ConfigError as e:
        _err(str(e))
        return 1
    d = cfg["dispatch"]
    enabled = bool(d["routing"]["quota"]["enabled"])
    rows = quota.report(
        d["routing"], snapshot_dir=settings.quota_dir(d), now=datetime.now(timezone.utc)
    )
    if args.json:
        _out(json.dumps({"enabled": enabled, "clients": rows}, indent=2))
        return 0
    _out(f"quota routing: {'on' if enabled else 'off'}")
    for row in rows:
        age = "-" if row["age_seconds"] is None else f"{row['age_seconds']:.0f}s"
        _out(
            f"{row['client']}: reader={row['reader'] or '-'} age={age} "
            f"factor={row['factor']:.2f}"
        )
        for w in row["windows"]:
            _out(
                f"  {w['name']}: used {w['used_percent']:.1f}% "
                f"resets {_fmt_reset(w['resets_at'])}"
            )
    return 0


def register(sub) -> None:
    q = sub.add_parser(
        "quota",
        help="Record and show subscription usage for quota-aware routing.",
        description=(
            "Feed dispatch routing with the usage a client already reports locally. "
            "`quota record` sits in a status line pipeline."
        ),
    )
    qsub = q.add_subparsers(dest="quota_command", required=True)

    s = qsub.add_parser(
        "record",
        help="Pass stdin to stdout and store its usage windows (status line pipe).",
        description=(
            "Example: agent-bus quota record | <your status line command>. "
            "Writes stdin to stdout unchanged, then stores only window names, "
            "used percentages and reset times. Never fails."
        ),
    )
    s.add_argument("--client", default="claude", help="Client the payload belongs to.")
    s.set_defaults(func=cmd_record)

    s = qsub.add_parser("show", help="Show each client's reading and routing factor.")
    s.add_argument("--json", action="store_true", help="Emit JSON.")
    s.set_defaults(func=cmd_show)

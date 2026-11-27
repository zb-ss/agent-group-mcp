"""Write (and remove) the unit files that run `agent-bus dispatch run` on a schedule.

launchd on macOS, a systemd user service + timer on Linux. Planning is pure;
`install` and `uninstall` write files and only touch launchctl/systemctl when
the caller asks to load or unload, through an injected `commands.Runner`.

With `dispatch.timer.always` false the timer fires on the working-hours slots
only. With it true the timer fires every `interval_minutes` all week
(`StartInterval` / `OnUnitActiveSec`); each message source then gates itself by
its own schedule and the task pipeline by `dispatch.working_hours`.

The environment written into a unit holds PATH, AGENT_BUS_DB and
AGENT_BUS_AUDIT_LOG (when set) plus `dispatch.timer.environment`. No secret
belongs there: unit files are plain text. Secrets go in `dispatch.env_file`.
"""

from __future__ import annotations

import plistlib
import shlex
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .. import commands
from . import settings

KIND_LAUNCHD = "launchd"
KIND_SYSTEMD = "systemd"
_LAUNCHD_WEEKDAYS = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
_LINGER_NOTE = (
    "user timers run only while you are logged in; run `loginctl enable-linger` "
    "once to keep them running after logout"
)
_PASSED_ENV = ("PATH", "AGENT_BUS_DB", "AGENT_BUS_AUDIT_LOG")


class TimerError(Exception):
    pass


@dataclass(frozen=True)
class TimerFile:
    path: Path
    content: bytes


@dataclass(frozen=True)
class TimerPlan:
    kind: str
    files: tuple[TimerFile, ...]
    load_commands: tuple[tuple[str, ...], ...]
    unload_commands: tuple[tuple[str, ...], ...]
    notes: tuple[str, ...] = ()


def slots(working_hours: Mapping, interval_minutes: int) -> list[tuple[int, int]]:
    """(hour, minute) from `start` stepping by the interval while <= `end`."""
    start_h, start_m = settings.parse_hhmm(working_hours["start"])
    end_h, end_m = settings.parse_hhmm(working_hours["end"])
    first, last = start_h * 60 + start_m, end_h * 60 + end_m
    return [divmod(t, 60) for t in range(first, last + 1, interval_minutes)]


def _environment(env: Mapping[str, str], extra: Mapping[str, str]) -> dict[str, str]:
    result = {key: env[key] for key in _PASSED_ENV if env.get(key)}
    result.update(extra)
    return result


def plan(
    cfg: Mapping,
    *,
    bin_path: str,
    platform: str = sys.platform,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
    uid: int | None = None,
    uninstall: bool = False,
) -> TimerPlan:
    """The files and commands for the timer. Removing it needs only the name
    and platform, so `uninstall` skips the absolute-binary-path check."""
    if not uninstall and not Path(bin_path).is_absolute():
        raise TimerError(
            f"the agent-bus path {bin_path!r} is not absolute; pass --bin-path /full/path/to/agent-bus"
        )
    if env is None:
        import os

        env = os.environ
    home = home if home is not None else Path.home()
    dcfg = cfg["dispatch"]
    timer = dcfg["timer"]
    hours = dcfg["working_hours"]
    name = timer["name"]
    argv = [item.format(agent_bus_bin=bin_path) for item in timer["command"]]
    environment = _environment(env, timer["environment"])
    output = settings.timer_output_path(dcfg)
    day_slots = slots(hours, timer["interval_minutes"])
    if platform == "darwin":
        return _launchd(name, argv, environment, output, hours["days"], day_slots, home, uid, timer)
    if platform.startswith("linux"):
        return _systemd(
            name, argv, environment, output, hours["days"], day_slots, home, timer, env, uninstall
        )
    raise TimerError(f"timers are supported on macOS and Linux only, not {platform!r}")


def _launchd(name, argv, environment, output, days, day_slots, home, uid, timer) -> TimerPlan:
    if uid is None:
        import os

        uid = os.getuid()
    path = home / "Library" / "LaunchAgents" / f"{name}.plist"
    document = {
        "Label": name,
        "ProgramArguments": argv,
        **(
            {"StartInterval": timer["interval_minutes"] * 60}
            if timer.get("always")
            else {
                "StartCalendarInterval": [
                    {"Weekday": _LAUNCHD_WEEKDAYS[day], "Hour": hour, "Minute": minute}
                    for day in days
                    for hour, minute in day_slots
                ]
            }
        ),
        "EnvironmentVariables": environment,
        "StandardOutPath": str(output),
        "StandardErrorPath": str(output),
        "RunAtLoad": False,
        "ProcessType": "Background",
    }
    bootout = (timer["launchctl_bin"], "bootout", f"gui/{uid}/{name}")
    bootstrap = (timer["launchctl_bin"], "bootstrap", f"gui/{uid}", str(path))
    return TimerPlan(
        kind=KIND_LAUNCHD,
        files=(TimerFile(path, plistlib.dumps(document)),),
        load_commands=(bootout, bootstrap),
        unload_commands=(bootout,),
    )


def _unit_quote(key: str, value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'Environment="{key}={escaped}"'


def _systemd_directory(home: Path, env: Mapping[str, str]) -> Path:
    base = env.get("XDG_CONFIG_HOME") or ""
    if base and Path(base).is_absolute():
        return Path(base) / "systemd" / "user"
    return home / ".config" / "systemd" / "user"


def _systemd(
    name, argv, environment, output, days, day_slots, home, timer, env, uninstall=False
) -> TimerPlan:
    directory = _systemd_directory(home, env)
    # systemd expands `$VAR`, `${VAR}` and `%x` specifiers in ExecStart. `$` is
    # doubled first so the `%` pass never touches its output (and vice versa).
    exec_start = shlex.join(argv).replace("$", "$$").replace("%", "%%")
    # No EnvironmentFile= line: `dispatch run` loads the 0600 dispatch.env_file
    # itself and refuses a group- or world-readable one, so secrets stay out of
    # the unit.
    service = [
        "[Unit]",
        f"Description={name}: run agent-bus dispatch",
        "",
        "[Service]",
        "Type=oneshot",
        f"ExecStart={exec_start}",
        *(_unit_quote(key, value) for key, value in environment.items()),
        f"StandardOutput=append:{output}",
        f"StandardError=append:{output}",
        "",
    ]
    day_list = ",".join(day.capitalize() for day in days)
    if timer.get("always"):
        triggers = ["OnActiveSec=60", f"OnUnitActiveSec={timer['interval_minutes']}min"]
    else:
        triggers = [f"OnCalendar={day_list} *-*-* {h:02d}:{m:02d}:00" for h, m in day_slots]
    timer_unit = [
        "[Unit]",
        f"Description={name}: schedule for agent-bus dispatch",
        "",
        "[Timer]",
        *triggers,
        "Persistent=false",
        "",
        "[Install]",
        "WantedBy=timers.target",
        "",
    ]
    ctl = timer["systemctl_bin"]
    return TimerPlan(
        kind=KIND_SYSTEMD,
        files=(
            TimerFile(directory / f"{name}.service", "\n".join(service).encode()),
            TimerFile(directory / f"{name}.timer", "\n".join(timer_unit).encode()),
        ),
        load_commands=(
            (ctl, "--user", "daemon-reload"),
            (ctl, "--user", "enable", "--now", f"{name}.timer"),
        ),
        unload_commands=((ctl, "--user", "disable", "--now", f"{name}.timer"),),
        notes=() if uninstall else (_LINGER_NOTE,),
    )


def _run(argv: tuple[str, ...], *, run: commands.Runner, timeout: float):
    try:
        return run(list(argv), timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise TimerError(f"{shlex.join(argv)} failed: {exc}") from exc


def install(plan: TimerPlan, *, load: bool, run: commands.Runner, timeout: float) -> list[Path]:
    written = []
    for item in plan.files:
        try:
            item.path.parent.mkdir(parents=True, exist_ok=True)
            item.path.write_bytes(item.content)
        except OSError as exc:
            raise TimerError(f"cannot write {item.path}: {exc}") from exc
        written.append(item.path)
    if not load:
        return written
    for argv in plan.load_commands:
        is_bootout = plan.kind == KIND_LAUNCHD and "bootout" in argv
        try:
            done = _run(argv, run=run, timeout=timeout)
        except TimerError:
            if is_bootout:
                continue
            raise
        if done.returncode != 0 and not is_bootout:
            detail = (done.stderr or "").strip() or f"exit {done.returncode}"
            raise TimerError(f"{shlex.join(argv)} failed: {detail}")
    return written


def uninstall(plan: TimerPlan, *, unload: bool, run: commands.Runner, timeout: float) -> list[Path]:
    if unload:
        for argv in plan.unload_commands:
            try:
                _run(argv, run=run, timeout=timeout)
            except TimerError:
                pass  # it may not be loaded
    removed = []
    for item in plan.files:
        if not item.path.exists():
            continue
        try:
            item.path.unlink()
        except OSError as exc:
            raise TimerError(f"cannot remove {item.path}: {exc}") from exc
        removed.append(item.path)
    if unload and plan.kind == KIND_SYSTEMD:
        try:
            _run(plan.load_commands[0], run=run, timeout=timeout)
        except TimerError:
            pass
    return removed

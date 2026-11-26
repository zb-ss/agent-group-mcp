"""Timer writer: plans are pure, install/uninstall use a fake runner."""

from __future__ import annotations

import copy
import plistlib
import subprocess

import pytest

from agent_bus import config
from agent_bus.dispatch import timer

BIN = "/usr/local/bin/agent-bus"
ENV = {"PATH": "/usr/bin:/bin"}


class FakeRun:
    def __init__(self, fail_on=()):
        self.calls = []
        self.fail_on = fail_on

    def __call__(self, argv, *, timeout, env=None, cwd=None):
        self.calls.append(list(argv))
        code = 1 if any(word in argv for word in self.fail_on) else 0
        return subprocess.CompletedProcess(argv, code, "", "boom" if code else "")


@pytest.fixture
def cfg():
    return copy.deepcopy(config.defaults())


def _plan(cfg, tmp_path, platform, **kw):
    return timer.plan(cfg, bin_path=BIN, platform=platform, home=tmp_path, env=ENV, uid=501, **kw)


def test_default_slots():
    got = timer.slots({"start": "09:00", "end": "19:00"}, 15)
    assert len(got) == 41 and got[0] == (9, 0) and got[-1] == (19, 0)


def test_custom_hours_change_slots(cfg, tmp_path):
    cfg["dispatch"]["working_hours"].update(start="10:00", end="11:00", days=["sat", "sun"])
    cfg["dispatch"]["timer"]["interval_minutes"] = 30
    doc = plistlib.loads(_plan(cfg, tmp_path, "darwin").files[0].content)
    entries = doc["StartCalendarInterval"]
    assert len(entries) == 6
    assert {e["Weekday"] for e in entries} == {0, 6}


def test_darwin_plan(cfg, tmp_path):
    plan = _plan(cfg, tmp_path, "darwin")
    assert plan.files[0].path == tmp_path / "Library/LaunchAgents/agent-bus-dispatch.plist"
    doc = plistlib.loads(plan.files[0].content)
    assert doc["Label"] == "agent-bus-dispatch"
    assert doc["ProgramArguments"] == [BIN, "dispatch", "run"]
    assert len(doc["StartCalendarInterval"]) == 205
    assert {e["Weekday"] for e in doc["StartCalendarInterval"]} == {1, 2, 3, 4, 5}
    assert doc["EnvironmentVariables"]["PATH"] == "/usr/bin:/bin"
    assert doc["RunAtLoad"] is False
    assert plan.load_commands[0] == ("launchctl", "bootout", "gui/501/agent-bus-dispatch")
    assert plan.load_commands[1][:3] == ("launchctl", "bootstrap", "gui/501")


def test_linux_plan(cfg, tmp_path):
    plan = _plan(cfg, tmp_path, "linux")
    service, unit = (f.content.decode() for f in plan.files)
    assert plan.files[0].path.name == "agent-bus-dispatch.service"
    assert f"ExecStart={BIN} dispatch run" in service
    lines = [l for l in unit.splitlines() if l.startswith("OnCalendar=")]
    assert len(lines) == 41
    assert lines[0] == "OnCalendar=Mon,Tue,Wed,Thu,Fri *-*-* 09:00:00"
    assert "WantedBy=timers.target" in unit
    assert plan.load_commands[1] == ("systemctl", "--user", "enable", "--now", "agent-bus-dispatch.timer")


def test_environment_entries_in_both_formats(cfg, tmp_path):
    cfg["dispatch"]["timer"]["environment"] = {"EXTRA_FLAG": "a%b"}
    doc = plistlib.loads(_plan(cfg, tmp_path, "darwin").files[0].content)
    assert doc["EnvironmentVariables"]["EXTRA_FLAG"] == "a%b"
    service = _plan(cfg, tmp_path, "linux").files[0].content.decode()
    assert 'Environment="EXTRA_FLAG=a%%b"' in service


def test_bad_inputs(cfg, tmp_path):
    with pytest.raises(timer.TimerError, match="--bin-path"):
        timer.plan(cfg, bin_path="agent-bus", platform="linux", home=tmp_path, env=ENV)
    with pytest.raises(timer.TimerError):
        _plan(cfg, tmp_path, "win32")


def test_install_without_load(cfg, tmp_path):
    fake = FakeRun()
    paths = timer.install(_plan(cfg, tmp_path, "linux"), load=False, run=fake, timeout=5)
    assert len(paths) == 2 and all(p.exists() for p in paths)
    assert fake.calls == []


def test_install_with_load_runs_commands(cfg, tmp_path):
    plan = _plan(cfg, tmp_path, "linux")
    fake = FakeRun()
    timer.install(plan, load=True, run=fake, timeout=5)
    assert fake.calls == [list(c) for c in plan.load_commands]


def test_bootout_failure_ignored_bootstrap_raises(cfg, tmp_path):
    plan = _plan(cfg, tmp_path, "darwin")
    fake = FakeRun(fail_on=("bootout",))
    timer.install(plan, load=True, run=fake, timeout=5)
    assert len(fake.calls) == 2
    with pytest.raises(timer.TimerError, match="boom"):
        timer.install(plan, load=True, run=FakeRun(fail_on=("bootstrap",)), timeout=5)


def test_uninstall(cfg, tmp_path):
    plan = _plan(cfg, tmp_path, "linux")
    timer.install(plan, load=False, run=FakeRun(), timeout=5)
    fake = FakeRun()
    removed = timer.uninstall(plan, unload=False, run=fake, timeout=5)
    assert len(removed) == 2 and fake.calls == []
    timer.install(plan, load=False, run=FakeRun(), timeout=5)
    fake = FakeRun(fail_on=("disable",))
    timer.uninstall(plan, unload=True, run=fake, timeout=5)
    assert fake.calls[0] == list(plan.unload_commands[0])
    assert fake.calls[-1] == ["systemctl", "--user", "daemon-reload"]


def test_uninstall_plan_needs_no_absolute_binary(cfg, tmp_path):
    with pytest.raises(timer.TimerError):
        timer.plan(cfg, bin_path="agent-bus", platform="linux", home=tmp_path, env=ENV)
    plan = timer.plan(cfg, bin_path="agent-bus", platform="linux", home=tmp_path, env=ENV,
                      uninstall=True)
    assert plan.unload_commands


def test_always_timer_uses_plain_intervals(cfg, tmp_path):
    cfg["dispatch"]["timer"].update(always=True, interval_minutes=5)
    doc = plistlib.loads(_plan(cfg, tmp_path, "darwin").files[0].content)
    assert doc["StartInterval"] == 300 and "StartCalendarInterval" not in doc
    unit = _plan(cfg, tmp_path, "linux").files[1].content.decode()
    assert "OnUnitActiveSec=5min" in unit and "OnActiveSec=60" in unit
    assert "OnCalendar" not in unit


def test_always_off_keeps_the_calendar_files(cfg, tmp_path):
    base = _plan(cfg, tmp_path, "linux").files[1].content
    cfg["dispatch"]["timer"]["always"] = False
    assert _plan(cfg, tmp_path, "linux").files[1].content == base
    assert b"OnUnitActiveSec" not in base

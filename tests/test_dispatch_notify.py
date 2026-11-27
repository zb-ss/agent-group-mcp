import smtplib
import ssl
import subprocess
from datetime import datetime, timezone

import pytest

from agent_bus.dispatch import notify
from agent_bus.dispatch.notify import (
    EmailNotifier,
    LinuxNotifier,
    LogNotifier,
    MacosNotifier,
    Notification,
    NotifyError,
)

PASSWORD = "placeholder"
NOTE = Notification("t123-demo", "needs review", "Fix the thing", "https://example.com/t/1")

MACOS_CFG = {
    "enabled": True,
    "osascript_bin": "osascript",
    "timeout_seconds": 5,
    "title": "Agent Bus",
}


LINUX_CFG = {
    "enabled": True,
    "notify_send_bin": "notify-send",
    "title": "agent-bus",
    "timeout_seconds": 7,
}


def _have(_name):
    return "/usr/bin/notify-send"


def _missing(_name):
    return None


def email_cfg(**over):
    cfg = {
        "enabled": True,
        "host": "smtp.example.com",
        "port": 465,
        "security": "tls",
        "username": "owner@example.com",
        "password_env": "FAKE_SMTP_PASSWORD",
        "from": "owner@example.com",
        "to": ["owner@example.com"],
        "subject_prefix": "[bus]",
        "timeout_seconds": 10,
    }
    cfg.update(over)
    return cfg


class FakeRun:
    def __init__(self, returncode=0, exc=None):
        self.calls = []
        self.returncode = returncode
        self.exc = exc

    def __call__(self, argv, *, timeout, env=None):
        self.calls.append((list(argv), timeout))
        if self.exc:
            raise self.exc
        return subprocess.CompletedProcess(argv, self.returncode, "", "")


class FakeSmtp:
    def __init__(self, calls):
        self.calls = calls

    def login(self, user, password):
        self.calls.append(("login", user))

    def send_message(self, message):
        self.calls.append(("send_message", message))

    def quit(self):
        self.calls.append(("quit",))


def factory_with(calls):
    def factory(security, host, port, timeout):
        calls.append(("connect", security, host, port, timeout))
        return FakeSmtp(calls)

    return factory


def test_log_notifier_appends(tmp_path):
    path = tmp_path / "sub" / "dispatch.log"
    fixed = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    log = LogNotifier(path, now=lambda: fixed)
    log.send(NOTE)
    log.send(Notification(None, "done", "T", "u"))
    lines = path.read_text().splitlines()
    assert lines[0] == (
        "2026-01-02T03:04:05.000000Z needs review session=t123-demo "
        "title=Fix the thing url=https://example.com/t/1"
    )
    assert "session=- " in lines[1]
    assert len(lines) == 2


def test_macos_argv_keeps_text_out_of_script():
    run = FakeRun()
    title = 'x"; do shell script "echo pwned"'
    MacosNotifier(MACOS_CFG, run=run, platform="darwin").send(
        Notification("s1", "done", title, "u")
    )
    argv, timeout = run.calls[0]
    assert timeout == 5
    assert argv == [
        "osascript",
        "-e", "on run argv",
        "-e",
        "display notification (item 1 of argv) with title (item 2 of argv) "
        "subtitle (item 3 of argv)",
        "-e", "end run",
        f"done: {title}", "Agent Bus", "s1",
    ]


def test_macos_noop_on_linux_and_disabled():
    run = FakeRun()
    MacosNotifier(MACOS_CFG, run=run, platform="linux").send(NOTE)
    MacosNotifier({**MACOS_CFG, "enabled": False}, run=run, platform="darwin").send(NOTE)
    assert run.calls == []


@pytest.mark.parametrize(
    "run",
    [
        FakeRun(returncode=1),
        FakeRun(exc=OSError("x")),
        FakeRun(exc=subprocess.TimeoutExpired("c", 1)),
    ],
)
def test_macos_failures_raise(run):
    with pytest.raises(NotifyError):
        MacosNotifier(MACOS_CFG, run=run, platform="darwin").send(NOTE)


_DESKTOP = {"DISPLAY": ":0"}


def test_linux_argv_puts_title_only_in_summary():
    run = FakeRun()
    title = "--urgency=critical <b>x</b>"
    LinuxNotifier(LINUX_CFG, run=run, platform="linux", which=_have, env=_DESKTOP).send(
        Notification("s1", "done", title, "u")
    )
    assert run.calls == [
        (["notify-send", "--app-name", "agent-bus", "--", f"done: {title}", "s1"], 7)
    ]


def test_linux_noop_on_darwin():
    run = FakeRun()
    n = LinuxNotifier(LINUX_CFG, run=run, platform="darwin", which=_have)
    n.send(NOTE)
    assert n.enabled is False
    assert run.calls == []


def test_linux_missing_binary_is_quiet():
    run = FakeRun()
    n = LinuxNotifier(LINUX_CFG, run=run, platform="linux", which=_missing)
    n.send(NOTE)
    assert n.enabled is False
    assert run.calls == []


def test_linux_without_a_session_bus_or_display_is_quiet():
    run = FakeRun(returncode=1)
    n = LinuxNotifier(LINUX_CFG, run=run, platform="linux", which=_have, env={})
    n.send(NOTE)
    assert n.enabled is False
    assert run.calls == []
    assert notify.deliver([n], NOTE, max_chars=50) == (True, [])


def test_linux_disabled_in_config_is_noop():
    run = FakeRun()
    n = LinuxNotifier({**LINUX_CFG, "enabled": False}, run=run, platform="linux", which=_have, env=_DESKTOP)
    n.send(NOTE)
    assert n.enabled is False
    assert run.calls == []


@pytest.mark.parametrize(
    "run", [FakeRun(returncode=2), FakeRun(exc=OSError("Fix the thing"))]
)
def test_linux_failures_raise_without_the_title(run):
    with pytest.raises(NotifyError) as info:
        LinuxNotifier(LINUX_CFG, run=run, platform="linux", which=_have, env=_DESKTOP).send(NOTE)
    assert "Fix the thing" not in str(info.value)


def test_deliver_with_only_a_disabled_linux_notifier_counts_as_delivered():
    n = LinuxNotifier(LINUX_CFG, run=FakeRun(), platform="darwin", which=_have)
    assert notify.deliver([n], NOTE, max_chars=50) == (True, [])


def test_linux_session_bus_alone_is_not_a_desktop():
    run = FakeRun()
    n = LinuxNotifier(
        LINUX_CFG, run=run, platform="linux", which=_have,
        env={"DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus"},
    )
    assert not n.enabled
    n.send(NOTE)
    assert run.calls == []


@pytest.mark.parametrize("name", ["DISPLAY", "WAYLAND_DISPLAY"])
def test_linux_display_enables(name):
    n = LinuxNotifier(LINUX_CFG, run=FakeRun(), platform="linux", which=_have, env={name: ":0"})
    assert n.enabled


def test_linux_runs_notify_send_with_the_checked_environment():
    seen = []

    def run(argv, *, timeout, env=None):
        seen.append(env)
        return subprocess.CompletedProcess(argv, 0, "", "")

    base = {"PATH": "/usr/bin", "DISPLAY": ":9"}
    env = {"DISPLAY": ":1", "FROM_ENV_FILE": "x", "PATH": "/usr/bin"}
    LinuxNotifier(LINUX_CFG, run=run, platform="linux", which=_have, env=env,
                  base_env=base).send(NOTE)
    assert seen == [{"PATH": "/usr/bin", "DISPLAY": ":1"}]


def test_linux_env_file_values_never_reach_notify_send():
    seen = []

    def run(argv, *, timeout, env=None):
        seen.append(env)
        return subprocess.CompletedProcess(argv, 0, "", "")

    base = {"PATH": "/usr/bin", "HOME": "/home/example"}
    merged = {
        **base,
        "WAYLAND_DISPLAY": "wayland-0",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
        "XDG_RUNTIME_DIR": "/run/user/1000",
        "EXAMPLE_API_TOKEN": "not-a-real-value",
        "FAKE_SMTP_PASSWORD": "not-a-real-value",
    }
    n = LinuxNotifier(LINUX_CFG, run=run, platform="linux", which=_have, env=merged,
                      base_env=base)
    assert n.enabled
    n.send(NOTE)
    [used] = seen
    assert "EXAMPLE_API_TOKEN" not in used and "FAKE_SMTP_PASSWORD" not in used
    assert used == {
        "PATH": "/usr/bin", "HOME": "/home/example", "WAYLAND_DISPLAY": "wayland-0",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
        "XDG_RUNTIME_DIR": "/run/user/1000",
    }


def test_linux_display_only_in_the_process_env_is_not_used_when_unchecked():
    # enabled reads the merged mapping; send must not use a display it did not see
    n = LinuxNotifier(LINUX_CFG, run=FakeRun(), platform="linux", which=_have, env={},
                      base_env={"DISPLAY": ":0"})
    assert not n.enabled


def test_macos_notifier_passes_no_env_file_mapping():
    seen = []

    def run(argv, *, timeout, env=None):
        seen.append(env)
        return subprocess.CompletedProcess(argv, 0, "", "")

    MacosNotifier(MACOS_CFG, run=run, platform="darwin").send(NOTE)
    assert seen == [None]


class _Failing:
    alerts = True
    enabled = True

    def __init__(self, name, *, desktop=False):
        self.name = name
        self.desktop = desktop
        self.sent = 0

    def send(self, notification):
        self.sent += 1
        raise NotifyError("exited with 1")


class _Ok:
    alerts = True
    enabled = True

    def __init__(self, name, *, desktop=False):
        self.name = name
        self.desktop = desktop

    def send(self, notification):
        pass


def test_failing_desktop_notifier_alone_still_delivers_with_one_warning():
    warnings = []
    linux = LinuxNotifier(
        LINUX_CFG, run=FakeRun(returncode=1), platform="linux", which=_have, env=_DESKTOP
    )
    delivered, errors = notify.deliver([linux], NOTE, max_chars=50, warn=warnings.append)
    assert (delivered, errors) == (True, [])
    assert warnings == ["linux: notify-send exited with 1"]
    assert "Fix the thing" not in warnings[0]


def test_failing_macos_notifier_is_best_effort():
    warnings = []
    macos = MacosNotifier(MACOS_CFG, run=FakeRun(returncode=1), platform="darwin")
    assert notify.deliver([macos], NOTE, max_chars=50, warn=warnings.append) == (True, [])
    assert warnings == ["macos: osascript exited with 1"]


def test_email_failure_still_retries_even_when_the_desktop_banner_worked():
    warnings = []
    delivered, errors = notify.deliver(
        [_Ok("linux", desktop=True), _Failing("email")],
        NOTE, max_chars=50, warn=warnings.append,
    )
    assert delivered is False
    assert errors == ["email: exited with 1"]
    assert warnings == []


def test_email_success_delivers_despite_a_failing_desktop_banner():
    warnings = []
    delivered, errors = notify.deliver(
        [_Failing("linux", desktop=True), _Ok("email")],
        NOTE, max_chars=50, warn=warnings.append,
    )
    assert (delivered, errors) == (True, [])
    assert warnings == ["linux: exited with 1"]


def test_default_warning_goes_to_stderr(capsys):
    notify.deliver([_Failing("linux", desktop=True)], NOTE, max_chars=50)
    assert capsys.readouterr().err == "agent-bus: warning: linux: exited with 1\n"


def test_email_tls_order_and_body():
    calls = []
    n = EmailNotifier(
        email_cfg(), env={"FAKE_SMTP_PASSWORD": PASSWORD}, smtp_factory=factory_with(calls)
    )
    n.send(NOTE)
    assert [c[0] for c in calls] == ["connect", "login", "send_message", "quit"]
    assert calls[0][1:] == ("tls", "smtp.example.com", 465, 10)
    message = calls[2][1]
    assert message["Subject"] == "[bus] needs review: Fix the thing"
    assert message["To"] == "owner@example.com"
    assert message.get_body(preferencelist=("plain",)).get_content().splitlines() == [
        "Session: t123-demo",
        "State: needs review",
        "Task: Fix the thing",
        "Link: https://example.com/t/1",
    ]


LINKS = (("PR", "https://example.com/pr/1"), ("", "https://example.com/a"))


def _mail(n):
    return EmailNotifier(email_cfg(), env={}, smtp_factory=None)._message(n)


def test_email_is_multipart_with_links():
    message = _mail(Notification("s1", "done", "T", "https://example.com/t/1", LINKS))
    assert message.get_content_type() == "multipart/alternative"
    assert [p.get_content_type() for p in message.iter_parts()] == [
        "text/plain",
        "text/html",
    ]
    plain = message.get_body(preferencelist=("plain",)).get_content().splitlines()
    assert plain == [
        "Session: s1",
        "State: done",
        "Task: T",
        "Link: https://example.com/t/1",
        "PR: https://example.com/pr/1",
        "Link: https://example.com/a",
    ]
    body = message.get_body(preferencelist=("html",)).get_content()
    assert '<a href="https://example.com/t/1">T</a>' in body
    assert '<li>PR: <a href="https://example.com/pr/1">https://example.com/pr/1</a></li>' in body
    assert "<li>Link: <a href=\"https://example.com/a\">" in body


def test_email_html_escapes_every_value():
    n = Notification(
        "s<1>", "d&ne", 'T<b>"&', "https://example.com/?a=1&b=\"2\"<x>",
        (('L<i>"&', "https://example.com/?q=1&r=\"<2>"),),
    )
    body = _mail(n).get_body(preferencelist=("html",)).get_content()
    for raw in ("<b>", "<i>", "<x>", "<2>", "s<1>", 'b="2"', "d&ne", 'T<b>'):
        assert raw not in body
    assert "&lt;b&gt;&quot;&amp;" in body
    assert "&amp;r=&quot;&lt;2&gt;" in body


def test_email_non_http_task_url_has_no_anchor():
    n = Notification("s", "done", "T", "javascript:alert(1)")
    body = _mail(n).get_body(preferencelist=("html",)).get_content()
    assert "<a " not in body
    assert "T (javascript:alert(1))" in body
    assert "<ul>" not in body


def test_deliver_drops_unsafe_links_and_caps_count():
    seen = []

    class Spy:
        name = "spy"

        def send(self, notification):
            seen.append(notification)

    pairs = (("x", "javascript:alert(1)"), ("y", "ftp://example.com/f")) + tuple(
        (f"L{i}", f"https://example.com/{i}") for i in range(12)
    )
    notify.deliver([Spy()], Notification("s", "done", "T", "u", pairs), max_chars=100)
    kept = seen[0].links
    assert len(kept) == 8
    assert all(url.startswith("https://") for _, url in kept)


def test_macos_argv_unchanged_with_links():
    plain, linked = FakeRun(), FakeRun()
    MacosNotifier(MACOS_CFG, run=plain, platform="darwin").send(NOTE)
    MacosNotifier(MACOS_CFG, run=linked, platform="darwin").send(
        Notification(NOTE.session, NOTE.state, NOTE.title, NOTE.url, LINKS)
    )
    assert plain.calls == linked.calls


def test_log_line_gains_links(tmp_path):
    path = tmp_path / "d.log"
    LogNotifier(path).send(Notification("s", "done", "T", "u", LINKS))
    lines = path.read_text().splitlines()
    assert len(lines) == 1
    assert lines[0].endswith(
        " url=u links=https://example.com/pr/1 https://example.com/a"
    )


def test_notification_takes_four_positional_arguments():
    assert Notification("s", "done", "T", "u").links == ()


def test_email_missing_password_names_variable_only():
    calls = []
    n = EmailNotifier(email_cfg(), env={}, smtp_factory=factory_with(calls))
    with pytest.raises(NotifyError) as err:
        n.send(NOTE)
    assert "FAKE_SMTP_PASSWORD" in str(err.value)
    assert calls == []


def test_email_disabled_noop():
    calls = []
    EmailNotifier(email_cfg(enabled=False), env={}, smtp_factory=factory_with(calls)).send(NOTE)
    assert calls == []


def test_email_login_failure_hides_password():
    class Failing(FakeSmtp):
        def login(self, user, password):
            raise smtplib.SMTPAuthenticationError(535, b"bad")

    n = EmailNotifier(
        email_cfg(),
        env={"FAKE_SMTP_PASSWORD": PASSWORD},
        smtp_factory=lambda *a: Failing([]),
    )
    with pytest.raises(NotifyError) as err:
        n.send(NOTE)
    assert PASSWORD not in str(err.value)


def test_subject_is_single_line_after_send_all():
    calls = []
    n = EmailNotifier(
        email_cfg(), env={"FAKE_SMTP_PASSWORD": PASSWORD}, smtp_factory=factory_with(calls)
    )
    errors = notify.send_all(
        [n], Notification("s", "done", "a\r\nBcc: evil@example.com", "u"), max_chars=100
    )
    assert errors == []
    message = calls[2][1]
    assert "\n" not in message["Subject"] and "\r" not in message["Subject"]
    assert message["Bcc"] is None


def test_connect_smtp_starttls_uses_context_before_login(monkeypatch):
    events = []

    class FakeClient:
        def __init__(self, host, port, timeout=None):
            events.append(("init", host, port, timeout))

        def starttls(self, context=None):
            events.append(("starttls", isinstance(context, ssl.SSLContext)))

        def login(self, user, password):
            events.append(("login",))

        def send_message(self, message):
            events.append(("send",))

        def quit(self):
            events.append(("quit",))

    monkeypatch.setattr(smtplib, "SMTP", FakeClient)
    n = EmailNotifier(
        email_cfg(security="starttls", port=587), env={"FAKE_SMTP_PASSWORD": PASSWORD}
    )
    n.send(NOTE)
    assert [e[0] for e in events] == ["init", "starttls", "login", "send", "quit"]
    assert events[1] == ("starttls", True)


def test_starttls_failure_sends_nothing(monkeypatch):
    events = []

    class FakeClient:
        def __init__(self, host, port, timeout=None):
            pass

        def starttls(self, context=None):
            raise smtplib.SMTPNotSupportedError("no starttls")

        def close(self):
            events.append("close")

        def login(self, *a):
            events.append("login")

        def send_message(self, *a):
            events.append("send")

    monkeypatch.setattr(smtplib, "SMTP", FakeClient)
    n = EmailNotifier(
        email_cfg(security="starttls", port=587), env={"FAKE_SMTP_PASSWORD": PASSWORD}
    )
    with pytest.raises(NotifyError):
        n.send(NOTE)
    assert events == ["close"]


def test_connect_smtp_tls_uses_smtp_ssl_only(monkeypatch):
    seen = {}

    class FakeSsl:
        def __init__(self, host, port, timeout=None, context=None):
            seen["ssl"] = (host, port, timeout, isinstance(context, ssl.SSLContext))

    def forbidden(*a, **k):
        raise AssertionError("plain SMTP must not be used")

    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSsl)
    monkeypatch.setattr(smtplib, "SMTP", forbidden)
    notify.connect_smtp("tls", "smtp.example.com", 465, 7)
    assert seen["ssl"] == ("smtp.example.com", 465, 7, True)


def test_connect_smtp_rejects_other_security():
    with pytest.raises(NotifyError):
        notify.connect_smtp("plain", "smtp.example.com", 25, 1)


def test_send_all_continues_after_failure(tmp_path):
    class Boom:
        name = "boom"

        def send(self, notification):
            raise NotifyError("broken")

    log = LogNotifier(tmp_path / "l.log")
    errors = notify.send_all([Boom(), log], NOTE, max_chars=100)
    assert errors == ["boom: broken"]
    assert (tmp_path / "l.log").exists()


def test_build_order(tmp_path):
    items = notify.build(
        {"macos": MACOS_CFG, "linux": LINUX_CFG, "email": email_cfg(enabled=False)},
        env={},
        run=FakeRun(),
        log_path=tmp_path / "l.log",
    )
    assert [i.name for i in items] == ["log", "macos", "linux", "email"]


def test_log_notifier_is_owner_only(tmp_path):
    import os
    import stat

    old = os.umask(0o022)
    try:
        path = tmp_path / "newdir" / "dispatch.log"
        LogNotifier(path).send(NOTE)
    finally:
        os.umask(old)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_deliver_counts_only_enabled_successes():
    class Off:
        name = "off"
        enabled = False

        def send(self, notification):
            return None

    class Boom:
        name = "boom"
        enabled = True

        def send(self, notification):
            raise NotifyError("nope")

    delivered, errors = notify.deliver([Off(), Boom()], NOTE, max_chars=50)
    assert (delivered, errors) == (False, ["boom: nope"])


class _Record:
    name = "record"
    enabled = True
    alerts = False

    def send(self, notification):
        return None


class _Boom:
    name = "boom"
    enabled = True

    def send(self, notification):
        raise NotifyError("nope")


def test_a_log_record_alone_does_not_count_when_the_alert_failed():
    delivered, errors = notify.deliver([_Record(), _Boom()], NOTE, max_chars=50)
    assert (delivered, errors) == (False, ["boom: nope"])


def test_with_no_alerting_notifier_there_is_nothing_to_retry():
    delivered, errors = notify.deliver([_Record()], NOTE, max_chars=50)
    assert (delivered, errors) == (True, [])


def test_the_log_notifier_is_a_record_not_an_alert():
    assert notify.LogNotifier.alerts is False

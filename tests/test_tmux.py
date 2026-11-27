"""tmux window helpers, driven by a fake Runner."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from agent_bus import tmux


class FakeTmux:
    def __init__(self, windows=(), *, session_exists=True, kill_fails=False):
        self.windows = list(windows)
        self.session_exists = session_exists
        self.kill_fails = kill_fails
        self.calls: list[list[str]] = []

    def __call__(self, argv, *, timeout, env=None, cwd=None):
        self.calls.append(list(argv))
        if argv[1] == "list-windows":
            if not self.session_exists:
                return subprocess.CompletedProcess(argv, 1, "", "can't find session")
            out = "".join(f"{wid}\t{name}\n" for wid, name in self.windows)
            return subprocess.CompletedProcess(argv, 0, out, "")
        if argv[1] == "kill-window":
            rc = 1 if self.kill_fails else 0
            return subprocess.CompletedProcess(argv, rc, "", "gone" if rc else "")
        return subprocess.CompletedProcess(argv, 0, "", "")


def _ids(fake, window="chief"):
    return tmux.window_ids("agents", window, tmux_bin="tmux", timeout=5, run=fake)


def test_window_ids_filters_by_exact_name():
    fake = FakeTmux([("@1", "chief"), ("@2", "chief-2"), ("@3", "chief")])
    assert _ids(fake) == ["@1", "@3"]
    assert fake.calls[0][:4] == ["tmux", "list-windows", "-t", "=agents"]


def test_window_ids_is_empty_without_the_session():
    assert _ids(FakeTmux(session_exists=False)) == []


def test_kill_windows_closes_each_id():
    fake = FakeTmux([("@1", "chief"), ("@2", "other"), ("@3", "chief")])
    closed = tmux.kill_windows("agents", "chief", tmux_bin="tmux", timeout=5, run=fake)
    assert closed == 2
    assert [c for c in fake.calls if c[1] == "kill-window"] == [
        ["tmux", "kill-window", "-t", "@1"], ["tmux", "kill-window", "-t", "@3"],
    ]


def test_kill_windows_without_the_session_closes_nothing():
    fake = FakeTmux(session_exists=False)
    assert tmux.kill_windows("agents", "chief", tmux_bin="tmux", timeout=5, run=fake) == 0


def test_kill_windows_raises_on_a_failed_kill():
    fake = FakeTmux([("@1", "chief")], kill_fails=True)
    with pytest.raises(tmux.TmuxError):
        tmux.kill_windows("agents", "chief", tmux_bin="tmux", timeout=5, run=fake)


class PasteTmux(FakeTmux):
    """`running` names the processes under the pane's wrapper shell. Every
    pgrep not asking for one by name finds an unrelated helper. The client
    exits after `checks` pgrep calls when that is given."""

    def __init__(self, running=("opencode",), checks=None, **kw):
        super().__init__(**kw)
        self.running = list(running)
        self.checks = checks
        self.pgreps = 0

    def __call__(self, argv, *, timeout, env=None, cwd=None):
        if argv[1] == "display-message":
            self.calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, "4242\n", "")
        if argv[0] == "pgrep":
            self.calls.append(list(argv))
            self.pgreps += 1
            if self.checks is not None and self.pgreps > self.checks:
                self.running = []
            if "-x" not in argv:
                return subprocess.CompletedProcess(argv, 0, "4299\n", "")
            hit = argv[-1] in self.running
            return subprocess.CompletedProcess(argv, 0 if hit else 1, "4243\n" if hit else "", "")
        return super().__call__(argv, timeout=timeout, env=env, cwd=cwd)


def _paste(fake, prompt, client="opencode", target="@1"):
    tmux.paste_file(target, prompt, client=client, tmux_bin="tmux", timeout=5, run=fake)


def test_paste_file_targets_the_given_id_without_the_text(tmp_path):
    prompt = tmp_path / "p.txt"
    prompt.write_text("secret words")
    # two windows share a name; the lower-index one is the stale one
    fake = PasteTmux(windows=[("@2", "chief"), ("@1", "chief")])
    _paste(fake, prompt, target="@2")
    verbs = [c[0] if c[0] == "pgrep" else c[1] for c in fake.calls]
    assert verbs == [
        "display-message", "pgrep", "load-buffer", "paste-buffer",
        "display-message", "pgrep", "send-keys",
    ]
    assert fake.calls[1] == ["pgrep", "-P", "4242", "-x", "opencode"]
    assert fake.calls[2][-1] == str(prompt)
    assert fake.calls[3][-2:] == ["-t", "@2"] and "-p" in fake.calls[3]
    assert fake.calls[-1] == ["tmux", "send-keys", "-t", "@2", "Enter"]
    assert not any("secret words" in part for call in fake.calls for part in call)


def test_paste_file_refuses_a_window_whose_client_has_exited(tmp_path):
    prompt = tmp_path / "p.txt"
    prompt.write_text("x")
    fake = PasteTmux(running=[])
    with pytest.raises(tmux.TmuxError, match="shell"):
        _paste(fake, prompt)
    assert [c[0] for c in fake.calls] == ["tmux", "pgrep"]
    with pytest.raises(tmux.TmuxError, match="no window id"):
        _paste(PasteTmux(), prompt, target="")


def test_paste_file_refuses_a_shell_that_only_has_an_unrelated_child(tmp_path):
    prompt = tmp_path / "p.txt"
    prompt.write_text("x")
    fake = PasteTmux(running=["sleep"])  # a helper of the login shell, not the client
    with pytest.raises(tmux.TmuxError, match="not the client"):
        _paste(fake, prompt)
    assert not [c for c in fake.calls if c[0] == "tmux" and c[1] in ("paste-buffer", "send-keys")]


def test_paste_file_matches_the_client_by_binary_name_only(tmp_path):
    prompt = tmp_path / "p.txt"
    prompt.write_text("x")
    _paste(PasteTmux(running=["opencode"]), prompt, client="/usr/local/bin/opencode")
    with pytest.raises(tmux.TmuxError):
        _paste(PasteTmux(running=["opencode"]), prompt, client="")


def test_paste_file_uses_the_configured_process_name(tmp_path):
    prompt = tmp_path / "p.txt"
    prompt.write_text("x")
    fake = PasteTmux(running=["node"])
    tmux.paste_file("@1", prompt, client="launcher", tmux_bin="tmux", timeout=5,
                    run=fake, process_name="node")
    assert ["pgrep", "-P", "4242", "-x", "node"] in fake.calls
    with pytest.raises(tmux.TmuxError):  # no value: the basename, still checked
        _paste(PasteTmux(running=["node"]), prompt, client="launcher")


def test_paste_file_does_not_press_enter_when_the_client_exits_after_the_paste(tmp_path):
    prompt = tmp_path / "p.txt"
    prompt.write_text("x")
    fake = PasteTmux(checks=1)
    with pytest.raises(tmux.TmuxError, match="shell"):
        _paste(fake, prompt)
    verbs = [c[1] for c in fake.calls if c[0] == "tmux"]
    assert "paste-buffer" in verbs and "send-keys" not in verbs


def test_paste_file_with_a_refusal_raises(tmp_path):
    prompt = tmp_path / "p.txt"
    prompt.write_text("x")

    def refuse(argv, *, timeout, env=None, cwd=None):
        if argv[1] == "display-message":
            return subprocess.CompletedProcess(argv, 0, "4242\n", "")
        if argv[0] == "pgrep":
            return subprocess.CompletedProcess(argv, 0, "4243\n", "")
        return subprocess.CompletedProcess(argv, 1, "", "nope")

    with pytest.raises(tmux.TmuxError, match="load-buffer failed: nope"):
        tmux.paste_file("@1", prompt, client="opencode", tmux_bin="tmux", timeout=5, run=refuse)


def test_launch_returns_the_new_window_id():
    class Launch(FakeTmux):
        def __call__(self, argv, *, timeout, env=None, cwd=None):
            if argv[1] in ("new-window", "new-session"):
                self.calls.append(list(argv))
                return subprocess.CompletedProcess(argv, 0, "@7\n", "")
            return super().__call__(argv, timeout=timeout, env=env, cwd=cwd)

    fake = Launch(session_exists=True)
    got = tmux.launch("agents", "w", Path("/x"), "cmd", env={}, tmux_bin="tmux", timeout=5, run=fake)
    assert got == "@7"
    assert fake.calls[-1][-6:] == ["-P", "-F", "#{window_id}", "-c", "/x", "cmd"]


@pytest.mark.skipif(shutil.which("tmux") is None, reason="needs tmux")
def test_paste_file_on_a_real_tmux_accepts_a_running_client_and_refuses_an_exited_one(tmp_path):
    socket = tempfile.mkdtemp(prefix="ab") + "/s"
    bin_ = ["tmux", "-S", socket]
    prompt = tmp_path / "p.txt"
    prompt.write_text("hello")

    def run(argv, *, timeout, env=None, cwd=None):
        if argv[0] == "tmux":
            argv = [*bin_, *argv[1:]]
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)

    try:
        running = tmux.launch("t", "live", tmp_path, "env A=b cat; exec $SHELL -l",
                              env={}, tmux_bin="tmux", timeout=10, run=run)
        gone = tmux.launch("t", "dead", tmp_path, "true; exec $SHELL -l",
                           env={}, tmux_bin="tmux", timeout=10, run=run)
        # the client is gone but the shell it fell back to has a helper child
        helper = tmux.launch("t", "helper", tmp_path, "true; /bin/sh -c 'true; sleep 30'",
                             env={}, tmux_bin="tmux", timeout=10, run=run)
        time.sleep(1)
        tmux.paste_file(running, prompt, client="cat", tmux_bin="tmux", timeout=10, run=run)
        for window in (gone, helper):
            with pytest.raises(tmux.TmuxError, match="shell"):
                tmux.paste_file(window, prompt, client="cat", tmux_bin="tmux", timeout=10, run=run)
    finally:
        subprocess.run([*bin_, "kill-server"], capture_output=True)


# --------------------- windows, capture_pane, kill_window --------------------


class DetailTmux:
    def __init__(self, stdout="", rc=0, stderr=""):
        self.stdout, self.rc, self.stderr = stdout, rc, stderr
        self.calls: list[list[str]] = []

    def __call__(self, argv, *, timeout, env=None, cwd=None):
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(argv, self.rc, self.stdout, self.stderr)


def _windows(fake):
    return tmux.windows("agents", "chief", tmux_bin="tmux", timeout=5, run=fake)


def test_windows_parses_details_of_the_exact_name():
    fake = DetailTmux("@1\tchief\t1\t1\t2\n@2\tchief-2\t1\t0\t0\n@3\tchief\t2\t0\t0\n")
    assert _windows(fake) == [
        tmux.WindowInfo("@1", 1, True, True), tmux.WindowInfo("@3", 2, False, False),
    ]
    argv = fake.calls[0]
    assert argv[:5] == ["tmux", "list-windows", "-t", "=agents", "-F"]
    assert argv[5] == "#{window_id}\t#{window_name}\t#{window_panes}\t#{window_active}\t#{session_attached}"


def test_windows_raises_when_tmux_fails_or_output_is_unreadable():
    with pytest.raises(tmux.TmuxError):
        _windows(DetailTmux(rc=1, stderr="no server"))
    with pytest.raises(tmux.TmuxError):
        _windows(DetailTmux("@1\tchief\tx\t1\t0\n"))


@pytest.mark.parametrize("escapes", [True, False])
def test_capture_pane_argv(escapes):
    fake = DetailTmux("pane text\n")
    text = tmux.capture_pane(
        "@1", lines=5, escapes=escapes, tmux_bin="tmux", timeout=5, run=fake
    )
    assert text == "pane text\n"
    expected = ["tmux", "capture-pane", "-p", "-t", "@1", "-S", "-5"] + (["-e"] if escapes else [])
    assert fake.calls == [expected]


def _capture(stdout, lines=3, escapes=False):
    return tmux.capture_pane(
        "@1", lines=lines, escapes=escapes, tmux_bin="tmux", timeout=5,
        run=DetailTmux(stdout),
    )


def test_capture_pane_keeps_only_the_last_lines_above_the_blank_padding():
    pane = "old output\nmore output\nstatus\n> \nhint line\n\n\n   \n"
    assert _capture(pane) == "status\n> \nhint line\n"


def test_capture_pane_ignores_output_higher_up_the_screen():
    pane = "".join(f"line {i}\n" for i in range(40)) + "> \n" + "\n" * 20
    assert _capture(pane, lines=2) == "line 39\n> \n"


def test_capture_pane_treats_escape_only_rows_as_blank():
    pane = "a\nb\n\x1b[0m\n\x1b[39m  \n"
    assert _capture(pane, lines=1, escapes=True) == "b\n"


def test_capture_pane_of_a_blank_pane_is_empty():
    assert _capture("\n\n\n") == ""


def test_capture_pane_failure_raises():
    with pytest.raises(tmux.TmuxError):
        tmux.capture_pane("@1", lines=5, escapes=False, tmux_bin="tmux", timeout=5,
                          run=DetailTmux(rc=1, stderr="gone"))


def test_kill_window_argv_and_failure():
    fake = DetailTmux()
    tmux.kill_window("@4", tmux_bin="tmux", timeout=5, run=fake)
    assert fake.calls == [["tmux", "kill-window", "-t", "@4"]]
    with pytest.raises(tmux.TmuxError):
        tmux.kill_window("@4", tmux_bin="tmux", timeout=5, run=DetailTmux(rc=1, stderr="x"))

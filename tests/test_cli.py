"""CLI subcommands: send, inbox, agents, tail (incl. follow), hook entrypoints."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


def _run_cli(args, env_extra=None, input_text=None, timeout=15, cwd=None):
    env = os.environ.copy()
    # some tests run the CLI from another directory, so the package must be
    # importable without relying on a working-directory-relative path
    import agent_bus

    package_root = str(Path(agent_bus.__file__).resolve().parents[1])
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (package_root, env.get("PYTHONPATH")) if p
    )
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "agent_bus.cli", *args],
        env=env,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=cwd,
    )


def test_cli_send_as_another_agent_keeps_its_repo_path(bus_paths, tmp_path):
    """Regression: `send --name X` rewrote X's repo_path to the caller's
    working directory, because the repo lookup ignored the name."""
    env = {
        "AGENT_BUS_DB": str(bus_paths["db"]),
        "AGENT_BUS_AUDIT_LOG": str(bus_paths["log"]),
    }
    from agent_bus.storage import Storage
    s = Storage()
    s.upsert_agent("repo-a", "/code/repo-a")
    s.upsert_agent("repo-b", "/code/repo-b")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    r = _run_cli(["send", "--name", "repo-a", "--to", "repo-b", "hi", "--json"],
                 env_extra=env, cwd=elsewhere)
    assert r.returncode == 0, r.stderr
    assert Storage().get_agent("repo-a").repo_path == "/code/repo-a"


def test_cli_send_ignores_env_repo_that_belongs_to_another_name(bus_paths):
    """AGENT_BUS_REPO describes AGENT_BUS_NAME, not whoever --name says."""
    env = {
        "AGENT_BUS_DB": str(bus_paths["db"]),
        "AGENT_BUS_AUDIT_LOG": str(bus_paths["log"]),
        "AGENT_BUS_NAME": "repo-b",
        "AGENT_BUS_REPO": "/code/repo-b",
    }
    from agent_bus.storage import Storage
    s = Storage()
    s.upsert_agent("repo-a", "/code/repo-a")
    s.upsert_agent("repo-b", "/code/repo-b")

    r = _run_cli(["send", "--name", "repo-a", "--to", "repo-b", "hi", "--json"],
                 env_extra=env)
    assert r.returncode == 0, r.stderr
    assert Storage().get_agent("repo-a").repo_path == "/code/repo-a"


def test_cli_send_registers_a_new_identity_in_the_cwd(bus_paths, tmp_path):
    env = {
        "AGENT_BUS_DB": str(bus_paths["db"]),
        "AGENT_BUS_AUDIT_LOG": str(bus_paths["log"]),
    }
    from agent_bus.storage import Storage
    Storage().upsert_agent("repo-b", "/code/repo-b")

    r = _run_cli(["send", "--name", "newcomer", "--to", "repo-b", "hi", "--json"],
                 env_extra=env, cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert Storage().get_agent("newcomer").repo_path == str(tmp_path)


def test_cli_send_as_own_env_identity_is_authoritative(bus_paths):
    env = {
        "AGENT_BUS_DB": str(bus_paths["db"]),
        "AGENT_BUS_AUDIT_LOG": str(bus_paths["log"]),
        "AGENT_BUS_NAME": "repo-a",
        "AGENT_BUS_REPO": "/code/repo-a-moved",
    }
    from agent_bus.storage import Storage
    s = Storage()
    s.upsert_agent("repo-a", "/code/repo-a")
    s.upsert_agent("repo-b", "/code/repo-b")

    r = _run_cli(["send", "--to", "repo-b", "hi", "--json"], env_extra=env)
    assert r.returncode == 0, r.stderr
    assert Storage().get_agent("repo-a").repo_path == "/code/repo-a-moved"


def test_cli_send_then_inbox(bus_paths):
    env = {
        "AGENT_BUS_DB": str(bus_paths["db"]),
        "AGENT_BUS_AUDIT_LOG": str(bus_paths["log"]),
    }
    # register beta first so a broadcast has a target
    from agent_bus.storage import Storage
    s = Storage()
    s.upsert_agent("beta", "/repo/beta")

    # use --json so the assertion is independent of cosmetic formatting
    r = _run_cli(["send", "--name", "human", "--to", "beta",
                  "hello from cli", "--json"],
                 env_extra=env)
    assert r.returncode == 0, r.stderr
    sent = json.loads(r.stdout)
    assert sent["recipients"] == ["beta"]
    assert sent["message_id"]

    r = _run_cli(["inbox", "--name", "beta", "--json"], env_extra=env)
    assert r.returncode == 0, r.stderr
    msgs = json.loads(r.stdout)
    assert len(msgs) == 1
    assert msgs[0]["body"] == "hello from cli"
    assert msgs[0]["from"] == "human"


def test_cli_send_to_a_repo_reaches_each_client_in_it(bus_paths):
    env = {
        "AGENT_BUS_DB": str(bus_paths["db"]),
        "AGENT_BUS_AUDIT_LOG": str(bus_paths["log"]),
    }
    from agent_bus.storage import Storage
    s = Storage()
    s.upsert_agent("repo-a/claude", "/code/repo-a")
    s.upsert_agent("repo-a/codex", "/code/repo-a")

    r = _run_cli(["send", "--name", "human", "--to", "repo-a", "hi both"],
                 env_extra=env)
    assert r.returncode == 0, r.stderr
    assert "repo-a (2)" in r.stdout

    for member in ("repo-a/claude", "repo-a/codex"):
        r = _run_cli(["inbox", "--name", member, "--json"], env_extra=env)
        assert [m["body"] for m in json.loads(r.stdout)] == ["hi both"]


def test_cli_send_to_unknown_recipient_fails_cleanly(bus_paths):
    env = {
        "AGENT_BUS_DB": str(bus_paths["db"]),
        "AGENT_BUS_AUDIT_LOG": str(bus_paths["log"]),
    }
    from agent_bus.storage import Storage
    Storage().upsert_agent("repo-a/claude", "/code/repo-a")

    r = _run_cli(["send", "--name", "human", "--to", "repo-a/cluade", "hi"],
                 env_extra=env)
    assert r.returncode == 1
    assert "repo-a/claude" in r.stderr
    assert "Traceback" not in r.stderr


def test_cli_forget_removes_from_roster(bus_paths):
    env = {
        "AGENT_BUS_DB": str(bus_paths["db"]),
        "AGENT_BUS_AUDIT_LOG": str(bus_paths["log"]),
    }
    from agent_bus.storage import Storage
    s = Storage()
    s.upsert_agent("alpha", "/repo/a")
    s.upsert_agent("ghost", "/repo/g")

    r = _run_cli(["forget", "ghost"], env_extra=env)
    assert r.returncode == 0
    assert "forgot agent 'ghost'" in r.stdout

    r = _run_cli(["agents", "--json"], env_extra=env)
    names = sorted(row["name"] for row in json.loads(r.stdout))
    assert names == ["alpha"]


def test_cli_forget_unknown_is_noop(bus_paths):
    env = {
        "AGENT_BUS_DB": str(bus_paths["db"]),
        "AGENT_BUS_AUDIT_LOG": str(bus_paths["log"]),
    }
    r = _run_cli(["forget", "nobody"], env_extra=env)
    assert r.returncode == 0
    assert "no agent named 'nobody'" in r.stdout


def test_cli_agents_lists_registered(bus_paths):
    env = {
        "AGENT_BUS_DB": str(bus_paths["db"]),
        "AGENT_BUS_AUDIT_LOG": str(bus_paths["log"]),
    }
    from agent_bus.storage import Storage
    s = Storage()
    s.upsert_agent("alpha", "/repo/a")
    s.upsert_agent("beta", "/repo/b")

    r = _run_cli(["agents", "--json"], env_extra=env)
    assert r.returncode == 0, r.stderr
    rows = json.loads(r.stdout)
    names = sorted(r["name"] for r in rows)
    assert names == ["alpha", "beta"]


def test_cli_tail_static_json(bus_paths):
    env = {
        "AGENT_BUS_DB": str(bus_paths["db"]),
        "AGENT_BUS_AUDIT_LOG": str(bus_paths["log"]),
    }
    from agent_bus.storage import Storage
    s = Storage()
    s.upsert_agent("a", "/repo/a")
    s.upsert_agent("b", "/repo/b")
    s.send_message(from_agent="a", to="b", body="audit-this")

    r = _run_cli(["tail", "--limit", "10", "--json"], env_extra=env)
    assert r.returncode == 0
    rows = [json.loads(l) for l in r.stdout.splitlines() if l.strip()]
    assert any(row["op"] == "send" and row["from"] == "a" for row in rows)


def test_cli_tail_static_plain(bus_paths):
    """Default tail output is a one-line human summary, not JSON."""
    env = {
        "AGENT_BUS_DB": str(bus_paths["db"]),
        "AGENT_BUS_AUDIT_LOG": str(bus_paths["log"]),
    }
    from agent_bus.storage import Storage
    s = Storage()
    s.upsert_agent("a", "/repo/a")
    s.upsert_agent("b", "/repo/b")
    s.send_message(from_agent="a", to="b", body="audit-plain-line")

    r = _run_cli(["tail", "--limit", "10"], env_extra=env)
    assert r.returncode == 0
    # the body preview and agent names should be in the rendered line,
    # but the line should NOT be parseable as JSON (it's human output)
    assert "audit-plain-line" in r.stdout
    assert "send" in r.stdout
    for line in r.stdout.splitlines():
        if line.strip():
            try:
                json.loads(line)
                raise AssertionError(f"non-json line parsed as JSON: {line!r}")
            except json.JSONDecodeError:
                pass


def test_cli_tail_follow_picks_up_new_send(bus_paths):
    env = os.environ.copy()
    env["AGENT_BUS_DB"] = str(bus_paths["db"])
    env["AGENT_BUS_AUDIT_LOG"] = str(bus_paths["log"])

    from agent_bus.storage import Storage
    s = Storage()
    s.upsert_agent("a", "/repo/a")
    s.upsert_agent("b", "/repo/b")

    # start tail -f in background
    proc = subprocess.Popen(
        [sys.executable, "-m", "agent_bus.cli", "tail", "-f"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # give it a moment to start its read loop
        time.sleep(0.4)
        # send via CLI from another process
        r = _run_cli(["send", "--name", "a", "--to", "b", "follow-me"],
                     env_extra={"AGENT_BUS_DB": env["AGENT_BUS_DB"],
                                "AGENT_BUS_AUDIT_LOG": env["AGENT_BUS_AUDIT_LOG"]})
        assert r.returncode == 0

        # wait up to 2 seconds for the message to appear in tail output
        deadline = time.time() + 2.0
        seen = ""
        while time.time() < deadline:
            assert proc.stdout is not None
            # non-blocking read attempt
            proc.stdout.flush() if hasattr(proc.stdout, "flush") else None
            try:
                line = proc.stdout.readline()
            except Exception:
                line = ""
            if line:
                seen += line
                if "follow-me" in seen:
                    break
            else:
                time.sleep(0.1)
        assert "follow-me" in seen, f"tail -f did not emit new audit row in time. Got: {seen!r}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_cli_hook_stop_empty_silent(bus_paths):
    env = {
        "AGENT_BUS_DB": str(bus_paths["db"]),
        "AGENT_BUS_AUDIT_LOG": str(bus_paths["log"]),
        "AGENT_BUS_NAME": "alpha",
    }
    from agent_bus.storage import Storage
    Storage().upsert_agent("alpha", "/repo/a")

    r = _run_cli(["hook-stop"], env_extra=env, input_text="{}")
    assert r.returncode == 0
    assert r.stdout.strip() == ""


def test_cli_hook_stop_blocks_when_pending(bus_paths):
    env = {
        "AGENT_BUS_DB": str(bus_paths["db"]),
        "AGENT_BUS_AUDIT_LOG": str(bus_paths["log"]),
        "AGENT_BUS_NAME": "alpha",
    }
    from agent_bus.storage import Storage
    s = Storage()
    s.upsert_agent("alpha", "/repo/a")
    s.upsert_agent("beta", "/repo/b")
    s.send_message(from_agent="beta", to="alpha", body="urgent question")

    r = _run_cli(["hook-stop"], env_extra=env, input_text="{}")
    assert r.returncode == 0
    payload = json.loads(r.stdout.strip().splitlines()[-1])
    assert payload["decision"] == "block"
    assert "urgent question" in payload["reason"]


def test_cli_hook_user_prompt_outputs_pending(bus_paths):
    env = {
        "AGENT_BUS_DB": str(bus_paths["db"]),
        "AGENT_BUS_AUDIT_LOG": str(bus_paths["log"]),
        "AGENT_BUS_NAME": "alpha",
    }
    from agent_bus.storage import Storage
    s = Storage()
    s.upsert_agent("alpha", "/repo/a")
    s.upsert_agent("beta", "/repo/b")
    s.send_message(from_agent="beta", to="alpha", body="hello alpha")

    r = _run_cli(["hook-user-prompt"], env_extra=env, input_text="{}")
    assert r.returncode == 0
    assert "1 new message(s)" in r.stdout
    assert "hello alpha" in r.stdout

    # inbox now empty
    r = _run_cli(["inbox", "--name", "alpha", "--json"], env_extra=env)
    assert json.loads(r.stdout) == []


def test_cli_hook_with_client_reads_the_repo_from_stdin(bus_paths, tmp_path):
    env = {
        "AGENT_BUS_DB": str(bus_paths["db"]),
        "AGENT_BUS_AUDIT_LOG": str(bus_paths["log"]),
        "AGENT_BUS_NAME": "",
    }
    repo = tmp_path / "repo-a"
    (repo / ".git").mkdir(parents=True)
    from agent_bus.storage import Storage
    s = Storage()
    s.upsert_agent("repo-a/codex", str(repo))
    s.ensure_agent("human", "/home")
    s.send_message(from_agent="human", to="repo-a/codex", body="via payload")

    r = _run_cli(["hook-user-prompt", "--client", "codex"], env_extra=env,
                 input_text=json.dumps({"cwd": str(repo)}), cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "via payload" in r.stdout


# --------------------------- init ----------------------------------------


def _scratch_repo(tmp_path, name="acme.dev"):
    repo = tmp_path / name
    (repo / ".git").mkdir(parents=True)
    return repo


def test_cli_init_json_lists_each_client(bus_paths, tmp_path):
    env = {
        "AGENT_BUS_DB": str(bus_paths["db"]),
        "AGENT_BUS_AUDIT_LOG": str(bus_paths["log"]),
    }
    repo = _scratch_repo(tmp_path)
    r = _run_cli(["init", str(repo), "--json"], env_extra=env)
    assert r.returncode == 0, r.stderr
    (plan,) = json.loads(r.stdout)
    assert plan["name"] == "acme-dev"
    assert plan["clients"] == [{
        "client": "claude", "name": "acme-dev/claude",
        "action": "write", "previous_name": None,
    }]
    assert not (repo / ".mcp.json").exists()  # --json never writes


def test_cli_init_rejects_a_client_it_cannot_wire(bus_paths, tmp_path):
    env = {
        "AGENT_BUS_DB": str(bus_paths["db"]),
        "AGENT_BUS_AUDIT_LOG": str(bus_paths["log"]),
    }
    r = _run_cli(["init", str(_scratch_repo(tmp_path)), "--clients", "nonesuch"],
                 env_extra=env)
    assert r.returncode == 2
    assert "nonesuch" in r.stderr and "claude" in r.stderr


def test_cli_init_upgrade_retires_the_old_name(bus_paths, tmp_path):
    env = {
        "AGENT_BUS_DB": str(bus_paths["db"]),
        "AGENT_BUS_AUDIT_LOG": str(bus_paths["log"]),
    }
    from agent_bus import init_cmd
    from agent_bus.storage import Storage

    repo = _scratch_repo(tmp_path)
    (repo / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "agent-bus": init_cmd.build_mcp_entry(
            name="acme-dev", repo=repo, bin_path="/old/agent-bus"),
    }}))
    Storage().upsert_agent("acme-dev", str(repo))

    r = _run_cli(["init", str(repo), "--bin-path", "/new/agent-bus"], env_extra=env)
    assert r.returncode == 0, r.stderr
    assert "renaming from 'acme-dev' to 'acme-dev/claude'" in r.stdout
    assert "[claude]" in r.stdout
    assert Storage().get_agent("acme-dev") is None


# --------------------------- several agents per repo ---------------------


def _shared_bus(bus_paths):
    from agent_bus.storage import Storage

    s = Storage()
    s.upsert_agent("repo-a/claude", "/code/repo-a")
    s.upsert_agent("repo-a/codex", "/code/repo-a")
    s.upsert_agent("repo-b", "/code/repo-b")
    s.ensure_agent("human", "/home")
    return s, {
        "AGENT_BUS_DB": str(bus_paths["db"]),
        "AGENT_BUS_AUDIT_LOG": str(bus_paths["log"]),
    }


def test_cli_agents_groups_the_clients_of_a_repo(bus_paths):
    s, env = _shared_bus(bus_paths)
    s.send_message(from_agent="human", to="repo-a/codex", body="x")

    r = _run_cli(["agents"], env_extra=env)
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    header = next(i for i, l in enumerate(lines) if l.startswith("repo-a "))
    assert "repo=/code/repo-a" in lines[header]
    assert "2 agents" in lines[header]
    assert lines[header + 1].startswith("  repo-a/claude")
    assert lines[header + 2].startswith("  repo-a/codex")
    assert "pending=1" in lines[header + 2]
    assert "repo=" not in lines[header + 1]  # said once, on the repo's line
    # an agent on its own keeps the one-line form
    solo = next(l for l in lines if l.startswith("repo-b"))
    assert "repo=/code/repo-b" in solo and "pending=0" in solo


def test_cli_agents_json_stays_a_flat_list(bus_paths):
    _s, env = _shared_bus(bus_paths)
    rows = json.loads(_run_cli(["agents", "--json"], env_extra=env).stdout)
    assert [(r["name"], r["group"], r["client"]) for r in rows] == [
        ("human", "human", None),
        ("repo-a/claude", "repo-a", "claude"),
        ("repo-a/codex", "repo-a", "codex"),
        ("repo-b", "repo-b", None),
    ]


def test_cli_forget_one_client_leaves_its_repo_mates(bus_paths):
    s, env = _shared_bus(bus_paths)
    r = _run_cli(["forget", "repo-a/codex"], env_extra=env)
    assert r.returncode == 0
    assert [a.name for a in s.list_agents() if a.group == "repo-a"] == ["repo-a/claude"]


def test_cli_forget_a_bare_repo_name_asks_for_group(bus_paths):
    s, env = _shared_bus(bus_paths)
    r = _run_cli(["forget", "repo-a"], env_extra=env)
    assert r.returncode == 1
    assert "--group" in r.stdout and "repo-a/claude" in r.stdout
    assert len([a for a in s.list_agents() if a.group == "repo-a"]) == 2


def test_cli_forget_group_removes_every_client_of_the_repo(bus_paths):
    s, env = _shared_bus(bus_paths)
    r = _run_cli(["forget", "--group", "repo-a"], env_extra=env)
    assert r.returncode == 0
    assert "repo-a/claude" in r.stdout and "repo-a/codex" in r.stdout
    assert sorted(a.name for a in s.list_agents()) == ["human", "repo-b"]


def test_cli_wake_config_test_uses_the_repo_level_command(bus_paths, tmp_path):
    _s, env = _shared_bus(bus_paths)
    marker = tmp_path / "woken"
    r = _run_cli(["wake-config", "set", "repo-a", f'touch "{marker}"'], env_extra=env)
    assert r.returncode == 0

    r = _run_cli(["wake-config", "test", "repo-a/claude"], env_extra=env)
    assert r.returncode == 0, r.stdout
    assert "fired=True" in r.stdout
    deadline = time.time() + 5
    while not marker.exists() and time.time() < deadline:
        time.sleep(0.05)
    assert marker.exists()

    shown = _run_cli(["wake-config", "show"], env_extra=env).stdout
    assert "repo-a" in shown and "every agent in the repo" in shown


def test_cli_reports_its_version(bus_paths):
    """A setup runbook needs a way to tell which build is installed."""
    from agent_bus import __version__

    r = _run_cli(["--version"])
    assert r.returncode == 0
    assert r.stdout.strip() == f"agent-bus {__version__}"


def test_version_is_the_same_in_both_places():
    """pyproject.toml and __init__.py are hand-synced; drift ships a wrong
    version to PyPI."""
    import tomllib
    from pathlib import Path

    from agent_bus import __version__

    root = Path(__file__).resolve().parents[1]
    declared = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    assert declared == __version__


def test_cli_agents_lists_sessions_under_their_client(bus_paths, fake_procs):
    from agent_bus.sessions import SessionRegistry

    s, env = _shared_bus(bus_paths)
    registry = SessionRegistry(s)
    server, lineage = fake_procs.session()
    registry.claim(client_address="repo-a/claude", repo_path="/code/repo-a",
                   server=server, lineage=lineage, pinned="frontend")
    s.set_topic("repo-a/claude-frontend", "login form")
    # the CLI runs in a subprocess that cannot see the stand-in processes,
    # so to it this session has ended
    r = _run_cli(["agents"], env_extra=env)
    assert r.returncode == 0, r.stderr
    line = next(l for l in r.stdout.splitlines() if "repo-a/claude-frontend" in l)
    assert line.startswith("    repo-a/claude-frontend")
    assert "ended" in line and "login form" in line
    assert "2 agents" in r.stdout  # sessions are not counted as agents

    rows = json.loads(_run_cli(["agents", "--json"], env_extra=env).stdout)
    by_name = {r["name"]: r for r in rows}
    assert (by_name["repo-a/claude-frontend"]["kind"], by_name["repo-a/claude-frontend"]["live"]) == (
        "session", False,
    )
    assert by_name["repo-a/claude"]["live"] is None


# ------------------------------ spawn / status ------------------------------


def _bus_env(bus_paths, **extra):
    env = {
        "AGENT_BUS_DB": str(bus_paths["db"]),
        "AGENT_BUS_AUDIT_LOG": str(bus_paths["log"]),
    }
    env.update(extra)
    return env


def _spawn_fixture(bus_paths, tmp_path):
    """A scratch repo on the roster plus a prompt file."""
    from agent_bus.storage import Storage

    repo = tmp_path / "repo-a"
    (repo / ".git").mkdir(parents=True)
    Storage().ensure_agent("repo-a/claude", str(repo))
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("do the thing\n")
    return repo, prompt


def _spawn_args(prompt, name="fix-login", *extra):
    return ["spawn", "--repo", "repo-a", "--client", "claude", "--name", name,
            "--prompt-file", str(prompt), *extra]


def _stub_tmux(tmp_path):
    """A tmux that logs its arguments; has-session fails, the rest succeeds."""
    bindir = tmp_path / "stubbin"
    bindir.mkdir()
    log = tmp_path / "tmux.log"
    exe = bindir / "tmux"
    exe.write_text(
        '#!/bin/sh\necho "$@" >> "%s"\n[ "$1" = has-session ] && exit 1\nexit 0\n' % log
    )
    exe.chmod(0o755)
    return bindir, log


def _path_with(bindir):
    return os.pathsep.join([str(bindir), os.environ.get("PATH", "")])


def test_cli_status_is_empty_on_a_fresh_bus(bus_paths):
    env = _bus_env(bus_paths)
    r = _run_cli(["status", "--json"], env_extra=env)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == []
    r = _run_cli(["status"], env_extra=env)
    assert r.returncode == 0, r.stderr
    assert "(no sessions running)" in r.stdout


def test_cli_spawn_dry_run_writes_nothing(bus_paths, tmp_path):
    from agent_bus.storage import Storage

    _, prompt = _spawn_fixture(bus_paths, tmp_path)
    r = _run_cli(_spawn_args(prompt, "fix-login", "--dry-run", "--json"),
                 env_extra=_bus_env(bus_paths))
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["name"] == "repo-a/claude-fix-login"
    assert "AGENT_BUS_SESSION=fix-login" in out["command"]
    assert out["launched"] is False
    assert Storage().list_spawns() == []
    assert Storage().repo_ids() == {}


def test_cli_spawn_launches_into_tmux_and_status_lists_it(bus_paths, tmp_path):
    _, prompt = _spawn_fixture(bus_paths, tmp_path)
    bindir, log = _stub_tmux(tmp_path)
    env = _bus_env(bus_paths, PATH=_path_with(bindir))
    r = _run_cli(_spawn_args(prompt), env_extra=env)
    assert r.returncode == 0, r.stderr
    assert "new-session -d -s agents -n fix-login" in log.read_text()
    r = _run_cli(["status", "--json"], env_extra=env)
    assert r.returncode == 0, r.stderr
    rows = {row["name"]: row for row in json.loads(r.stdout)}
    row = rows["repo-a/claude-fix-login"]
    assert row["state"] == "starting"
    assert row["tmux_session"] == "agents"
    assert row["tmux_window"] == "fix-login"


def test_cli_spawn_without_tmux_prints_the_manual_command(bus_paths, tmp_path):
    _, prompt = _spawn_fixture(bus_paths, tmp_path)
    (bus_paths["db"].parent / "config.json").write_text(
        json.dumps({"spawn": {"tmux_bin": "no-such-tmux-binary"}})
    )
    r = _run_cli(_spawn_args(prompt), env_extra=_bus_env(bus_paths))
    assert r.returncode == 1
    assert "tmux" in r.stderr
    assert r.stdout.startswith("cd ")


def test_cli_spawn_rejects_bad_usage(bus_paths, tmp_path):
    _, prompt = _spawn_fixture(bus_paths, tmp_path)
    env = _bus_env(bus_paths)
    r = _run_cli(["spawn", "--repo", "repo-a", "--client", "claude"], env_extra=env)
    assert r.returncode == 2
    bad = _spawn_args(prompt)
    bad[bad.index("claude")] = "nosuch"
    assert _run_cli(bad, env_extra=env).returncode == 2


def test_cli_spawn_refuses_option_like_name_and_prompt(bus_paths, tmp_path):
    from agent_bus.storage import Storage

    _, prompt = _spawn_fixture(bus_paths, tmp_path)
    env = _bus_env(bus_paths)
    args = _spawn_args(prompt)
    args[args.index("fix-login")] = "--name=-bad"
    args.remove("--name")
    r = _run_cli(args, env_extra=env)
    assert r.returncode == 2 and "must not start with" in r.stderr
    prompt.write_text("- item")
    r = _run_cli(_spawn_args(prompt, "fix-login", "--dry-run"), env_extra=env)
    assert r.returncode == 2 and "starts with '-'" in r.stderr
    assert Storage().list_spawns() == []


def test_cli_malformed_config_stops_spawn_but_not_status(bus_paths, tmp_path):
    _, prompt = _spawn_fixture(bus_paths, tmp_path)
    (bus_paths["db"].parent / "config.json").write_text("{not json")
    env = _bus_env(bus_paths)
    r = _run_cli(_spawn_args(prompt, "fix-login", "--dry-run"), env_extra=env)
    assert r.returncode == 1
    assert "config.json" in r.stderr
    r = _run_cli(["status"], env_extra=env)
    assert r.returncode == 0, r.stderr
    assert "config.json" in r.stderr


def test_cli_spawn_stops_at_the_concurrency_cap(bus_paths, tmp_path):
    _, prompt = _spawn_fixture(bus_paths, tmp_path)
    (bus_paths["db"].parent / "config.json").write_text(
        json.dumps({"spawn": {"max_sessions": 1}})
    )
    bindir, log = _stub_tmux(tmp_path)
    env = _bus_env(bus_paths, PATH=_path_with(bindir))
    assert _run_cli(_spawn_args(prompt, "first"), env_extra=env).returncode == 0
    r = _run_cli(_spawn_args(prompt, "second"), env_extra=env)
    assert r.returncode == 4
    assert "second" not in log.read_text()


def test_cli_config_show_prints_defaults_without_a_file(bus_paths):
    env = {"AGENT_BUS_DB": str(bus_paths["db"])}
    r = _run_cli(["config", "show", "--json"], env_extra=env)
    assert r.returncode == 0, r.stderr
    data = json.loads(r.stdout)
    assert data["exists"] is False
    assert data["path"].endswith("config.json")
    assert data["config"]["spawn"]["max_sessions"] == 10


def test_cli_config_show_reflects_a_user_override(bus_paths):
    env = {"AGENT_BUS_DB": str(bus_paths["db"])}
    (bus_paths["db"].parent / "config.json").write_text(
        json.dumps({"spawn": {"max_sessions": 2}})
    )
    r = _run_cli(["config", "show", "--json"], env_extra=env)
    assert r.returncode == 0, r.stderr
    data = json.loads(r.stdout)
    assert data["exists"] is True
    assert data["config"]["spawn"]["max_sessions"] == 2
    assert "claude" in data["config"]["spawn"]["clients"]


def test_cli_spawn_template_typo_is_an_exit_code_not_a_traceback(bus_paths, tmp_path):
    _, prompt = _spawn_fixture(bus_paths, tmp_path)
    bus_paths["db"].parent.joinpath("config.json").write_text(
        json.dumps({"spawn": {"clients": {"claude": {"command": ["claude", "{nmae}"]}}}})
    )
    r = _run_cli(_spawn_args(prompt, "fix-login", "--dry-run"), env_extra=_bus_env(bus_paths))
    assert r.returncode == 1 and "Traceback" not in r.stderr and "nmae" in r.stderr


def test_cli_spawn_bad_clone_url_template_is_a_config_error(bus_paths, tmp_path):
    _, prompt = _spawn_fixture(bus_paths, tmp_path)
    bus_paths["db"].parent.joinpath("config.json").write_text(
        json.dumps({"repos": {"clone_url": "https://{host}/{owner}.git"}})
    )
    r = _run_cli(_spawn_args(prompt, "fix-login", "--dry-run"), env_extra=_bus_env(bus_paths))
    assert r.returncode == 1 and "Traceback" not in r.stderr and "clone_url" in r.stderr


def test_cli_spawn_invalid_client_key_exits_with_usage_code(bus_paths, tmp_path):
    _, prompt = _spawn_fixture(bus_paths, tmp_path)
    bus_paths["db"].parent.joinpath("config.json").write_text(
        json.dumps({"spawn": {"clients": {"claude-yolo": {
            "command": ["x"], "prompt": ["{prompt}"]}}}})
    )
    args = _spawn_args(prompt, "fix-login", "--dry-run")
    args[args.index("claude")] = "claude-yolo"
    r = _run_cli(args, env_extra=_bus_env(bus_paths))
    assert r.returncode == 2 and "Traceback" not in r.stderr


def test_cli_spawn_refuses_an_opted_out_repo(bus_paths, tmp_path):
    repo, prompt = _spawn_fixture(bus_paths, tmp_path)
    (repo / ".agent-bus-ignore").write_text("")
    r = _run_cli(_spawn_args(prompt, "fix-login", "--dry-run"), env_extra=_bus_env(bus_paths))
    assert r.returncode == 1 and ".agent-bus-ignore" in r.stderr


def test_cli_status_hides_long_finished_sessions_unless_all(bus_paths):
    from agent_bus.storage import SpawnRecord, Storage

    store = Storage()
    store.ensure_agent("repo-a/claude-fix-login", "/srv/code/repo-a")
    store.record_spawn(SpawnRecord(
        name="repo-a/claude-fix-login", client="claude", repo_id=None,
        repo_path="/srv/code/repo-a", work_dir="/srv/code/repo-a", model=None,
        prompt_file="/tmp/p.txt", tmux_session="agents", tmux_window="fix-login",
        remote=False, spawned_at="2020-01-01T00:00:00.000000Z",
    ))
    store.set_state("repo-a/claude-fix-login", "done")
    with store.connect() as conn:
        conn.execute(
            "UPDATE agents SET state_at = '2020-01-02T00:00:00.000000Z' WHERE name = ?",
            ("repo-a/claude-fix-login",),
        )
    env = _bus_env(bus_paths)

    r = _run_cli(["status", "--json"], env_extra=env)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == []
    r = _run_cli(["status", "--json", "--all"], env_extra=env)
    assert [row["name"] for row in json.loads(r.stdout)] == ["repo-a/claude-fix-login"]


def test_status_passes_the_git_budget_from_config(bus_paths, monkeypatch, capsys):
    import argparse

    from agent_bus import cli, config, repo_identity

    config.config_path().write_text(
        json.dumps({"repos": {"status_budget_seconds": 2.5}}), encoding="utf-8"
    )
    seen = {}
    monkeypatch.setattr(
        repo_identity, "refresh_missing",
        lambda store, cfg, **kw: seen.update(kw),
    )
    cli.cmd_status(argparse.Namespace(all=False, json=True))
    assert seen == {"budget_seconds": 2.5}

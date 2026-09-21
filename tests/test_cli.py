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

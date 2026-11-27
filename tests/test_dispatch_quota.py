"""Quota readers and the routing factor."""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_bus import config
from agent_bus.dispatch import quota, routing

NOW = datetime(2026, 11, 27, 12, 0, 0, tzinfo=timezone.utc)
EPOCH = NOW.timestamp()


@pytest.fixture
def cfg():
    return json.loads(json.dumps(config.load()["dispatch"]["routing"]))


def qcfg(cfg, **over):
    out = dict(cfg["quota"], enabled=True)
    out.update(over)
    return out


def reading(*windows, at=NOW, client="claude"):
    return quota.Reading(client, at, tuple(windows))


def win(used, resets=None, name="five_hour"):
    return quota.Window(name, used, resets)


# parse_statusline

def test_parse_both_spellings_and_resets():
    iso = "2026-11-27T13:00:00Z"
    ws = quota.parse_statusline({"rate_limits": {
        "five_hour": {"used_percentage": 42.5, "resets_at": 1790000000},
        "seven_day": {"used_percent": 10, "resets_at": iso},
        "no_reset": {"used_percent": 1},
    }})
    by = {w.name: w for w in ws}
    assert by["five_hour"].used_percent == 42.5 and by["five_hour"].resets_at == 1790000000
    assert by["seven_day"].resets_at == datetime(2026, 11, 27, 13, tzinfo=timezone.utc).timestamp()
    assert by["no_reset"].resets_at is None


def test_parse_rejects_bad_values():
    ws = quota.parse_statusline({"rate_limits": {
        "a": {"used_percent": True},
        "b": {"used_percent": 101},
        "c": {"used_percent": -1},
        "Bad-Name": {"used_percent": 5},
        "d" * 41: {"used_percent": 5},
        "ok": {"used_percent": 5, "resets_at": True},
        "str": {"used_percent": "5"},
    }})
    assert [w.name for w in ws] == ["ok"] and ws[0].resets_at is None


def test_parse_limits_count_and_never_raises():
    many = {f"w{i}": {"used_percent": 1} for i in range(20)}
    assert len(quota.parse_statusline({"rate_limits": many})) == quota.MAX_WINDOWS
    for junk in (None, 5, [], {"rate_limits": 3}, {"rate_limits": {"a": 1}}):
        assert quota.parse_statusline(junk) == ()


# write_snapshot / read_snapshot

def test_write_snapshot_modes_and_content(tmp_path):
    folder = tmp_path / "q"
    assert quota.write_snapshot(folder, "claude", (win(5, 99.0),), now=NOW, min_interval_seconds=0)
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    path = folder / "claude.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    data = json.loads(path.read_text())
    assert set(data) == {"client", "recorded_at", "windows"}
    assert data["windows"] == {"five_hour": {"used_percent": 5, "resets_at": 99.0}}
    assert list(folder.iterdir()) == [path]
    got = quota.read_snapshot(folder, "claude")
    assert got.windows == (win(5, 99.0),) and got.at == NOW


def test_write_snapshot_interval_empty_and_bad_client(tmp_path):
    w = (win(5),)
    assert quota.write_snapshot(tmp_path, "claude", w, now=NOW, min_interval_seconds=0)
    os.utime(tmp_path / "claude.json", (EPOCH, EPOCH))
    assert not quota.write_snapshot(tmp_path, "claude", w, now=NOW + timedelta(seconds=5), min_interval_seconds=20)
    assert quota.write_snapshot(tmp_path, "claude", w, now=NOW + timedelta(seconds=30), min_interval_seconds=20)
    assert not quota.write_snapshot(tmp_path, "claude", (), now=NOW, min_interval_seconds=0)
    for bad in ("../x", "A", "", "a/b", "-a"):
        assert not quota.write_snapshot(tmp_path, bad, w, now=NOW, min_interval_seconds=0)


def test_read_snapshot_refusals(tmp_path):
    quota.write_snapshot(tmp_path, "claude", (win(5),), now=NOW, min_interval_seconds=0)
    link = tmp_path / "codex.json"
    link.symlink_to(tmp_path / "claude.json")
    assert quota.read_snapshot(tmp_path, "codex") is None
    big = tmp_path / "big.json"
    big.write_text(" " * (quota.MAX_SNAPSHOT_BYTES + 1))
    assert quota.read_snapshot(tmp_path, "big") is None
    (tmp_path / "broken.json").write_text("{nope")
    assert quota.read_snapshot(tmp_path, "broken") is None
    (tmp_path / "shape.json").write_text('{"recorded_at": 3, "windows": []}')
    assert quota.read_snapshot(tmp_path, "shape") is None
    assert quota.read_snapshot(tmp_path, "missing") is None


# read_codex

def event(used, ts="2026-11-27T11:59:00Z", secondary=None, resets=EPOCH + 3600):
    return json.dumps({"timestamp": ts, "type": "event_msg", "payload": {
        "type": "token_count",
        "rate_limits": {
            "primary": {"used_percent": used, "window_minutes": 300, "resets_at": resets},
            "secondary": secondary,
        }}})


def session(root, day, name, lines, mtime):
    folder = root / "2026" / "11" / day
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"rollout-2026-11-{day}T00-00-00-{name}.jsonl"
    path.write_text("\n".join(lines) + "\n")
    os.utime(path, (mtime, mtime))
    return path


def ccfg(root, **over):
    base = {"reader": "codex_sessions", "sessions_dir": str(root), "max_days": 2,
            "max_files": 5, "max_tail_bytes": 262144}
    base.update(over)
    return base


def test_codex_newest_by_mtime_and_last_event(tmp_path):
    session(tmp_path, "26", "a", [event(10)], EPOCH - 500)
    session(tmp_path, "27", "b", ['{"type":"other"}', event(20), event(30, secondary={"used_percent": 7, "resets_at": 5})], EPOCH - 100)
    r = quota.read_codex(ccfg(tmp_path))
    assert [(w.name, w.used_percent) for w in r.windows] == [("primary", 30), ("secondary", 7)]
    assert r.at == datetime(2026, 11, 27, 11, 59, tzinfo=timezone.utc)


def test_codex_skips_files_without_event_and_cut_lines(tmp_path):
    session(tmp_path, "27", "old", [event(44)], EPOCH - 500)
    session(tmp_path, "27", "new", ['{"type":"x"}'], EPOCH - 10)
    cut = event(60)
    path = session(tmp_path, "27", "cut", [cut[40:], event(11)], EPOCH - 5)
    r = quota.read_codex(ccfg(tmp_path, max_tail_bytes=len(event(11)) + 5))
    assert r.windows[0].used_percent == 11
    path.unlink()
    assert quota.read_codex(ccfg(tmp_path)).windows[0].used_percent == 44


def test_codex_null_fields_and_missing_folder(tmp_path):
    null = json.dumps({"timestamp": "2026-11-27T11:00:00Z", "payload": {"rate_limits": {"primary": None, "secondary": None}}})
    session(tmp_path, "27", "n", [null], EPOCH)
    assert quota.read_codex(ccfg(tmp_path)) is None
    assert quota.read_codex(ccfg(tmp_path / "nope")) is None
    assert quota.read_codex({}) is None


def test_codex_respects_max_days(tmp_path):
    session(tmp_path, "25", "a", [event(9)], EPOCH)
    session(tmp_path, "26", "b", ['{"x":1}'], EPOCH - 1)
    session(tmp_path, "27", "c", ['{"x":1}'], EPOCH - 2)
    assert quota.read_codex(ccfg(tmp_path, max_days=2)) is None
    assert quota.read_codex(ccfg(tmp_path, max_days=3)).windows[0].used_percent == 9


# factor

def test_factor_none_stale_future(cfg):
    q = qcfg(cfg)
    assert quota.factor(None, q, now=NOW) == 1.0
    assert quota.factor(reading(win(90), at=NOW - timedelta(minutes=31)), q, now=NOW) == 1.0
    assert quota.factor(reading(win(90), at=NOW + timedelta(minutes=31)), q, now=NOW) == 1.0
    assert quota.factor(reading(win(90), at=NOW - timedelta(minutes=29)), q, now=NOW) == pytest.approx(0.55)


def test_factor_stale_exhausted_window_counts_until_it_resets(cfg):
    q = qcfg(cfg)
    old = NOW - timedelta(hours=3)
    assert quota.factor(reading(win(100, EPOCH + 7200), at=old), q, now=NOW) == 0.0
    # Close to the reset it gets the reset-soon factor, as a fresh reading does.
    assert quota.factor(reading(win(100, EPOCH + 600), at=old), q, now=NOW) == 0.5
    # Once the reset has passed the window says nothing any more.
    assert quota.factor(reading(win(100, EPOCH - 1), at=old), q, now=NOW) == 1.0


def test_factor_stale_reading_drops_windows_that_do_not_prove_exhaustion(cfg):
    q = qcfg(cfg)
    old = NOW - timedelta(hours=3)
    # Not exhausted: expires by age.
    assert quota.factor(reading(win(90, EPOCH + 7200), at=old), q, now=NOW) == 1.0
    # Exhausted but no known reset: cannot tell when it ends, expires by age.
    assert quota.factor(reading(win(100), at=old), q, now=NOW) == 1.0
    # Mixed: only the exhausted window with a reset still counts.
    mixed = reading(win(30, EPOCH + 7200), win(100, EPOCH + 7200, name="seven_day"), at=old)
    assert quota.factor(mixed, q, now=NOW) == 0.0


def test_factor_future_reading_is_ignored_even_when_exhausted(cfg):
    future = reading(win(100, EPOCH + 7200), at=NOW + timedelta(hours=3))
    assert quota.factor(future, qcfg(cfg), now=NOW) == 1.0


def test_factor_expired_window_ignored(cfg):
    q = qcfg(cfg)
    assert quota.factor(reading(win(99, EPOCH - 1)), q, now=NOW) == 1.0
    assert quota.factor(reading(win(99, EPOCH)), q, now=NOW) == 1.0


def test_factor_below_threshold(cfg):
    q = qcfg(cfg)
    assert quota.factor(reading(win(96, EPOCH + 7200)), q, now=NOW) == 0.0
    assert quota.factor(reading(win(96)), q, now=NOW) == 0.0
    assert quota.factor(reading(win(96, EPOCH + 600)), q, now=NOW) == 0.5
    assert quota.factor(reading(win(96, EPOCH + 1800)), q, now=NOW) == 0.5
    assert quota.factor(reading(win(250, EPOCH + 7200)), q, now=NOW) == 0.0


def test_factor_weight_and_minimum(cfg):
    r = reading(win(40), win(80, name="seven_day"))
    assert quota.factor(r, qcfg(cfg, weight=0), now=NOW) == 1.0
    assert quota.factor(r, qcfg(cfg, weight=1), now=NOW) == pytest.approx(0.2)
    assert quota.factor(r, qcfg(cfg, weight=0.5), now=NOW) == pytest.approx(0.6)
    assert quota.factor(reading(), qcfg(cfg), now=NOW) == 1.0


# reader / report

def test_reader_disabled_is_no_quota(cfg, tmp_path):
    assert quota.reader(cfg, snapshot_dir=tmp_path, now=NOW) is routing.no_quota


def test_reader_enabled_memoises(cfg, tmp_path):
    cfg["quota"]["enabled"] = True
    quota.write_snapshot(tmp_path, "claude", (win(50),), now=NOW, min_interval_seconds=0)
    look = quota.reader(cfg, snapshot_dir=tmp_path, now=NOW)
    assert look("claude") == pytest.approx(0.75)
    (tmp_path / "claude.json").unlink()
    assert look("claude") == pytest.approx(0.75)
    assert look("agy") == 1.0 and look("codex") == 1.0


def test_reader_codex_and_report(cfg, tmp_path):
    root = tmp_path / "sessions"
    session(root, "27", "a", [event(50)], EPOCH)
    cfg["quota"]["clients"]["codex"]["sessions_dir"] = str(root)
    rows = {r["client"]: r for r in quota.report(cfg, snapshot_dir=tmp_path / "q", now=NOW)}
    assert not rows["codex"]["enabled"]
    assert rows["codex"]["factor"] == pytest.approx(0.75)
    assert rows["codex"]["age_seconds"] == 60.0
    assert rows["claude"]["windows"] == [] and rows["claude"]["factor"] == 1.0
    assert rows["agy"]["reader"] is None and rows["agy"]["factor"] == 1.0


# routing integration

def _signals():
    return routing.Signals(type="refinement", repo="example-project", repo_id=None, title="t")


def _route(cfg, look):
    cfg = dict(cfg, enabled=True)
    return routing.choose(cfg, _signals(), static_client="claude", static_model=None,
                          available=lambda c: True, quota=look)


def test_exhausted_higher_weight_loses(cfg, tmp_path):
    cfg["quota"]["enabled"] = True
    quota.write_snapshot(tmp_path, "claude", (win(99, EPOCH + 7200),), now=NOW, min_interval_seconds=0)
    look = quota.reader(cfg, snapshot_dir=tmp_path, now=NOW)
    assert _route(cfg, look).client != "claude"
    assert _route(cfg, routing.no_quota).client == "claude"


def test_override_beats_quota(cfg, tmp_path):
    cfg["quota"]["enabled"] = True
    cfg["overrides"] = [{"client": "claude"}]
    quota.write_snapshot(tmp_path, "claude", (win(99, EPOCH + 7200),), now=NOW, min_interval_seconds=0)
    look = quota.reader(cfg, snapshot_dir=tmp_path, now=NOW)
    route = _route(cfg, look)
    assert route.client == "claude" and route.by == routing.BY_OVERRIDE

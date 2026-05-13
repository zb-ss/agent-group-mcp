"""Pure-function tests for the formatting helpers."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent_bus import formatting as fmt


def test_color_for_is_stable():
    a = fmt.color_for("alpha")
    b = fmt.color_for("alpha")
    assert a == b
    assert a in fmt.AGENT_PALETTE


def test_color_for_varies_between_names():
    seen = {fmt.color_for(name) for name in ("alpha", "beta", "gamma", "human", "zoltan")}
    # not strictly guaranteed, but the palette has 8 colors and we picked 5 distinct names
    assert len(seen) >= 2


def test_short_thread_truncates():
    full = "abcdef01-2345-6789-abcd-ef0123456789"
    assert fmt.short_thread(full) == "abcdef01"
    assert fmt.short_thread("") == ""
    assert fmt.short_thread(None) == ""


def test_truncate_keeps_short():
    assert fmt.truncate("hi", 100) == "hi"


def test_truncate_collapses_newlines_and_adds_ellipsis():
    text = "line1\nline2\nline3"
    short = fmt.truncate(text, 10)
    assert "\n" not in short
    assert short.endswith("…")
    assert len(short) <= 10


def test_humanize_clock_handles_z_suffix():
    out = fmt.humanize_clock("2026-05-13T19:45:51.000000Z", tz=timezone.utc)
    assert out == "19:45:51"


def test_humanize_clock_falls_back_on_garbage():
    assert fmt.humanize_clock("not-a-timestamp") == "not-a-timestamp"


@pytest.mark.parametrize(
    "delta_seconds,expected",
    [
        (1, "just now"),
        (30, "30s ago"),
        (60 * 5, "5m ago"),
        (3600 * 3, "3h ago"),
        (86400 * 2, "2d ago"),
        (86400 * 14, "2w ago"),
    ],
)
def test_humanize_relative(delta_seconds, expected):
    now = datetime(2026, 5, 13, 12, 0, 0, tzinfo=timezone.utc)
    past = now.timestamp() - delta_seconds
    ts = datetime.fromtimestamp(past, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    assert fmt.humanize_relative(ts, now=now) == expected


def test_humanize_relative_clamps_future_to_just_now():
    now = datetime(2026, 5, 13, 12, 0, 0, tzinfo=timezone.utc)
    future = now.timestamp() + 30
    ts = datetime.fromtimestamp(future, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    assert fmt.humanize_relative(ts, now=now) == "just now"


def test_render_plain_with_color_off_strips_ansi():
    frags = fmt.fragments_for_message(
        sent_at="2026-05-13T19:00:00.000000Z",
        from_agent="alpha",
        to_agent="beta",
        body="hello",
        thread_id="11111111-aaaa-bbbb-cccc-dddddddddddd",
    )
    out = fmt.render_plain(frags, use_color=False)
    assert "\033[" not in out  # no ANSI escapes
    assert "alpha" in out and "beta" in out and "hello" in out


def test_render_plain_with_color_on_emits_ansi():
    frags = fmt.fragments_for_message(
        sent_at="2026-05-13T19:00:00.000000Z",
        from_agent="alpha",
        to_agent="beta",
        body="hello",
        thread_id=None,
    )
    out = fmt.render_plain(frags, use_color=True)
    assert "\033[" in out


def test_fragments_for_audit_row_has_op_first():
    row = {
        "ts": "2026-05-13T19:00:00.000000Z",
        "op": "send",
        "actor": "alpha",
        "from": "alpha",
        "to": "beta",
        "thread_id": "abcd1234-aaaa-bbbb-cccc-dddddddddddd",
        "body_preview": "hi",
        "body_sha256": "deadbeef",
    }
    frags = fmt.fragments_for_audit_row(row)
    plain = fmt.render_plain(frags, use_color=False)
    assert "send" in plain
    assert "alpha" in plain and "beta" in plain
    assert "hi" in plain
    assert "abcd1234" in plain


def test_style_map_includes_target_broadcast():
    style = fmt.style_map(["alpha", "beta"])
    assert "target-*" in style
    assert "agent-alpha" in style
    assert "agent-beta" in style

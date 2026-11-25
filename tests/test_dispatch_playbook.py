from __future__ import annotations

import stat

import pytest

from agent_bus.dispatch import playbook
from agent_bus.dispatch.playbook import PLACEHOLDERS, PlaybookError
from agent_bus.dispatch.store import TaskRecord

BUILTINS = ("refinement", "follow-up", "implementation")


def values(**over):
    data = {name: f"v-{name}" for name in PLACEHOLDERS}
    data.update(task_gid="1200000001", task_url="https://tracker.example/t/1", task_title="Fix thing")
    data.update(over)
    return data


@pytest.mark.parametrize("name", BUILTINS)
def test_builtin_renders(name):
    text = playbook.render(name, values(), templates_dir=None)
    assert text[0].isalpha()
    assert "1200000001" in text and "https://tracker.example/t/1" in text
    assert "Untrusted data" in text
    begin = text.index("--- begin untrusted data ---")
    end = text.index("--- end untrusted data ---")
    assert begin < text.index("Fix thing") < end


def test_title_is_literal():
    title = "x $HOME ${task_url} $$"
    text = playbook.render("refinement", values(task_title=title), templates_dir=None)
    assert f"title: {title}" in text


def test_private_override(tmp_path):
    (tmp_path / "refinement.md").write_text("Private $task_gid", encoding="utf-8")
    assert playbook.render("refinement", values(), templates_dir=str(tmp_path)) == "Private 1200000001"
    assert "Untrusted data" in playbook.render("follow-up", values(), templates_dir=str(tmp_path))


def test_unknown_placeholder(tmp_path):
    (tmp_path / "x.md").write_text("Hi $nope", encoding="utf-8")
    with pytest.raises(PlaybookError, match="task_url"):
        playbook.render("x", values(), templates_dir=str(tmp_path))


def test_leading_dash_and_empty(tmp_path):
    (tmp_path / "x.md").write_text("-bad $task_gid", encoding="utf-8")
    (tmp_path / "y.md").write_text("  \n", encoding="utf-8")
    with pytest.raises(PlaybookError):
        playbook.render("x", values(), templates_dir=str(tmp_path))
    with pytest.raises(PlaybookError):
        playbook.render("y", values(), templates_dir=str(tmp_path))


@pytest.mark.parametrize("name", ["../x", "", "A", "-a"])
def test_bad_name(name):
    with pytest.raises(PlaybookError):
        playbook.load_template(name, templates_dir=None)


def test_missing_template(tmp_path):
    with pytest.raises(PlaybookError, match="not found"):
        playbook.load_template("nothing", templates_dir=str(tmp_path))


def test_write_prompt(tmp_path):
    path = playbook.write_prompt("hello", label="t1-x", prompts_dir=tmp_path / "p")
    assert path.read_text() == "hello"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def record(i, **over):
    data = dict(source="s", task_id=str(i), title=f"Title\n{i}", url=f"https://t.example/{i}",
                first_seen_at="2026-01-01T00:00:00Z", handled_types=())
    data.update(over)
    return TaskRecord(**data)


def test_overlap_text():
    assert playbook.overlap_text([], limit=5, max_chars=20) == "(none)"
    recs = [record(1, type="refinement", outcome="done", session_name="a/b"), record(2), record(3)]
    lines = playbook.overlap_text(recs, limit=2, max_chars=20).splitlines()
    assert lines == [
        "- Title 1 (https://t.example/1) [refinement, done, a/b]",
        "- Title 2 (https://t.example/2) [-, -, -]",
    ]

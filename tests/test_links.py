from __future__ import annotations

import json

import pytest

from agent_bus import links

URL = "https://example.com/pr/1"


def test_normalize_accepts_and_flattens_labels():
    out = links.normalize([
        {"label": "  My\n  PR\t", "url": URL},
        {"url": "HTTP://example.com/a"},
        {"label": None, "url": URL},
    ])
    assert out[0] == {"label": "My PR", "url": URL}
    assert out[1] == {"label": "", "url": "HTTP://example.com/a"}
    assert out[2]["label"] == ""


def test_normalize_cuts_long_labels():
    out = links.normalize([{"label": "x" * 500, "url": URL}])
    assert len(out[0]["label"]) == links.MAX_LABEL_LEN


def test_normalize_accepts_an_empty_list():
    assert links.normalize([]) == []


@pytest.mark.parametrize("url", [
    "ftp://example.com/x",
    "javascript:alert(1)",
    "https://",
    "example.com/x",
    "https://example.com/a b",
    "https://example.com/a\nb",
    "https://example.com/a\x00b",
    "https://example.com/" + "a" * 2048,
    "",
    None,
    5,
])
def test_normalize_rejects_a_bad_url(url):
    with pytest.raises(links.LinkError, match=r"links\[0\]"):
        links.normalize([{"label": "x", "url": url}])


def test_normalize_rejects_a_missing_url_and_bad_shapes():
    with pytest.raises(links.LinkError):
        links.normalize([{"label": "x"}])
    with pytest.raises(links.LinkError):
        links.normalize(["https://example.com"])
    with pytest.raises(links.LinkError):
        links.normalize("https://example.com")
    with pytest.raises(links.LinkError):
        links.normalize([{"label": 3, "url": URL}])


def test_normalize_rejects_too_many_entries():
    links.normalize([{"url": URL}] * links.MAX_LINKS)
    with pytest.raises(links.LinkError):
        links.normalize([{"url": URL}] * (links.MAX_LINKS + 1))


def test_dumps_and_loads_round_trip():
    value = [{"label": "PR", "url": URL}]
    assert links.dumps([]) is None
    assert links.loads(links.dumps(value)) == value


@pytest.mark.parametrize("raw", [None, "", "not json", "{}", "5", "[1, 2]"])
def test_loads_is_tolerant(raw):
    assert links.loads(raw) == []


def test_loads_drops_invalid_entries_and_caps_the_count():
    raw = json.dumps(
        [{"url": "ftp://x.example/a"}, {"label": "ok", "url": URL}]
        + [{"url": URL}] * 20
    )
    out = links.loads(raw)
    assert len(out) == links.MAX_LINKS
    assert out[0] == {"label": "ok", "url": URL}

"""Links a session attaches to its bus state (a pull request, an artifact,
a handoff, its remote session URL), so an alert can carry them.

Pure functions, no I/O. Only http and https are accepted: a link ends up in
a notification the owner clicks.
"""

from __future__ import annotations

import json
import unicodedata
from urllib.parse import urlsplit

MAX_LINKS = 8  # protocol-ish bound, not a tunable
MAX_URL_LEN = 2048  # protocol-ish bound, not a tunable
MAX_LABEL_LEN = 60  # protocol-ish bound, not a tunable
SCHEMES = ("http", "https")  # protocol-ish bound, not a tunable


class LinkError(ValueError):
    """A links value the bus refuses to store."""


def is_web_url(url: object) -> bool:
    """True for an http(s) URL with a host, no whitespace or control
    characters, and at most MAX_URL_LEN characters."""
    if not isinstance(url, str) or not url or len(url) > MAX_URL_LEN:
        return False
    if any(ch.isspace() or unicodedata.category(ch).startswith("C") for ch in url):
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme.lower() in SCHEMES and bool(parts.netloc)


def _flatten(label: str) -> str:
    cleaned = "".join(
        " " if unicodedata.category(ch).startswith("C") else ch for ch in label
    )
    return " ".join(cleaned.split())[:MAX_LABEL_LEN].strip()


def normalize(value: object) -> list[dict[str, str]]:
    """Validate a `links` argument and return `[{"label", "url"}]`."""
    if not isinstance(value, list):
        raise LinkError("links must be a list of {label, url} objects")
    if len(value) > MAX_LINKS:
        raise LinkError(f"links holds at most {MAX_LINKS} entries, got {len(value)}")
    out: list[dict[str, str]] = []
    for index, entry in enumerate(value):
        if not isinstance(entry, dict):
            raise LinkError(f"links[{index}] must be an object with a url")
        url = entry.get("url")
        if not is_web_url(url):
            raise LinkError(
                f"links[{index}].url must be an http or https URL without "
                f"whitespace, at most {MAX_URL_LEN} characters"
            )
        label = entry.get("label")
        if label is not None and not isinstance(label, str):
            raise LinkError(f"links[{index}].label must be a string")
        out.append({"label": _flatten(label or ""), "url": url})
    return out


def dumps(links: list[dict[str, str]]) -> str | None:
    """Compact JSON for storage; None for no links."""
    if not links:
        return None
    return json.dumps(links, separators=(",", ":"), ensure_ascii=False)


def loads(raw: str | None) -> list[dict[str, str]]:
    """Tolerant reader: NULL, bad JSON or a wrong shape gives []; invalid
    entries are dropped."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    out: list[dict[str, str]] = []
    for entry in data:
        if not isinstance(entry, dict) or not is_web_url(entry.get("url")):
            continue
        label = entry.get("label")
        out.append(
            {"label": _flatten(label) if isinstance(label, str) else "", "url": entry["url"]}
        )
        if len(out) == MAX_LINKS:
            break
    return out

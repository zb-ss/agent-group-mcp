"""Client and model routing by a configurable scoring policy.

Pure rule matching: every client name, weight and model comes from the
routing config passed in, nothing is named here."""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

BY_STATIC = "static"
BY_OVERRIDE = "override"
BY_POLICY = "policy"
BY_DISPATCHER = "dispatcher"
BY_MODEL = "decision_model"


@dataclass(frozen=True)
class Signals:
    type: str
    repo: str
    repo_id: str | None
    title: str
    sections: tuple[str, ...] = ()


@dataclass(frozen=True)
class Route:
    client: str
    model: str | None
    by: str
    complexity: int | None = None
    scores: Mapping[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "client": self.client,
            "model": self.model,
            "by": self.by,
            "complexity": self.complexity,
            "scores": dict(self.scores),
        }


def no_quota(client: str) -> float:
    """Quota hook for `choose`: every client has full quota.

    M4 passes a function that reads the subscription quota of a client and
    returns a factor in 0..1; `choose` multiplies each client's score by it."""
    return 1.0


def complexity(routing_cfg: Mapping, signals: Signals) -> int:
    cfg = routing_cfg.get("complexity") or {}
    total = 0
    for entry in cfg.get("signals") or []:
        if _complexity_entry_matches(entry, signals):
            total += int(entry.get("score", 0))
    return max(0, min(int(cfg.get("max", 5)), total))


def _complexity_entry_matches(entry: Mapping, signals: Signals) -> bool:
    if "type" in entry and signals.type not in entry["type"]:
        return False
    if "title" in entry and not any(
        re.search(pattern, signals.title) for pattern in entry["title"]
    ):
        return False
    if "section" in entry and not any(
        re.search(pattern, name)
        for pattern in entry["section"]
        for name in signals.sections
    ):
        return False
    return True


def model_for(
    routing_cfg: Mapping, client: str, level: int, fallback: str | None
) -> str | None:
    client_cfg = (routing_cfg.get("clients") or {}).get(client) or {}
    best: Mapping | None = None
    for entry in client_cfg.get("models") or []:
        minimum = int(entry.get("min_complexity", 0))
        if minimum > level:
            continue
        if best is None or minimum > int(best.get("min_complexity", 0)):
            best = entry
    if best is None:
        return fallback
    return best.get("model")


def _override_matches(entry: Mapping, signals: Signals) -> bool:
    pattern = entry.get("repo")
    if pattern is not None:
        subject = signals.repo_id if signals.repo_id is not None else signals.repo
        if not fnmatch.fnmatchcase(subject, str(pattern)):
            return False
    wanted = entry.get("type")
    return wanted is None or wanted == signals.type


def choose(
    routing_cfg: Mapping,
    signals: Signals,
    *,
    static_client: str,
    static_model: str | None,
    available: Callable[[str], bool],
    quota: Callable[[str], float] = no_quota,
) -> Route:
    static = Route(static_client, static_model, BY_STATIC)
    if not routing_cfg.get("enabled"):
        return static
    level = complexity(routing_cfg, signals)
    for entry in routing_cfg.get("overrides") or []:
        if not _override_matches(entry, signals):
            continue
        client = str(entry["client"])
        if entry.get("model") is not None:
            model = entry["model"]
        else:
            model = model_for(routing_cfg, client, level, static_model)
        return Route(client, model, BY_OVERRIDE, level)
    scores: dict[str, float] = {}
    for client, client_cfg in (routing_cfg.get("clients") or {}).items():
        if not available(client):
            continue
        types = client_cfg.get("types") or {}
        raw = (
            float(client_cfg.get("weight", 0))
            + float(types.get(signals.type, 0))
            + float(client_cfg.get("per_complexity", 0)) * level
        )
        scores[client] = raw * quota(client)
    if not scores:
        return static
    winner = max(scores, key=lambda name: scores[name])  # first max wins ties
    fallback = static_model if winner == static_client else None
    return Route(
        winner, model_for(routing_cfg, winner, level, fallback), BY_POLICY, level, scores
    )

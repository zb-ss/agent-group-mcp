"""Optional decision-model adapter (Ollama System One API).

Runs in shadow mode by default: the answer is only recorded next to the
actual choice. The wire format lives in `_request_body` and `_parse`."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from .settings import endpoint_problem
from .routing import BY_MODEL, BY_POLICY, BY_STATIC, Route, Signals, model_for

HttpPost = Callable[[str, bytes, Mapping[str, str], float], bytes]

MAX_OPTIONS = 26
RATING_LEVELS = 6  # ratings 0..5
CLIENT_QUESTION = "client"
COMPLEXITY_QUESTION = "complexity"


class DecisionModelError(Exception):
    """The endpoint failed. Messages name only the host."""


def _host(url: str) -> str:
    return urllib.parse.urlsplit(url).hostname or "unknown host"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def default_http_post(
    url: str, body: bytes, headers: Mapping[str, str], timeout: float
) -> bytes:
    request = urllib.request.Request(
        url,
        data=body,
        headers={**headers, "Content-Type": "application/json"},
        method="POST",
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise DecisionModelError(
            f"HTTP {exc.code} from {_host(url)}"
        ) from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise DecisionModelError(
            f"request to {_host(url)} failed ({type(exc).__name__})"
        ) from None


@dataclass(frozen=True)
class ShadowAnswer:
    client: str | None = None
    complexity: int | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"client": self.client, "complexity": self.complexity, "error": self.error}


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", " ", text)).strip()


def _state(cfg: Mapping, signals: Signals) -> str:
    parts = [
        f"Task type: {signals.type}",
        f"Repository: {signals.repo_id or signals.repo}",
        f"Title: {_clean(signals.title)}",
    ]
    if signals.sections:
        parts.append("Sections: " + ", ".join(_clean(s) for s in signals.sections))
    return "\n".join(parts)[: int(cfg["max_state_chars"])]


def _request_body(cfg: Mapping, signals: Signals, clients: Sequence[str]) -> bytes:
    questions = {
        CLIENT_QUESTION: {
            "type": "choice",
            "instructions": "Which agent client should work on this task?",
            "criteria": {name: None for name in clients},
        },
        COMPLEXITY_QUESTION: {
            "type": "score",
            "instructions": "How complex is this task?",
            "criteria": [
                "trivial", "simple", "modest", "moderate", "hard", "very hard",
            ],
        },
    }
    if len(clients) < 2:  # the endpoint needs at least two options
        del questions[CLIENT_QUESTION]
    body = {"model": cfg["model"], "state": _state(cfg, signals), "questions": questions}
    return json.dumps(body).encode("utf-8")


def _parse(raw: bytes, clients: Sequence[str], complexity_max: int) -> ShadowAnswer:
    try:
        answers = json.loads(raw)["answers"]
        if not isinstance(answers, dict):
            raise TypeError
    except (ValueError, KeyError, TypeError):
        return ShadowAnswer(error="unreadable answer from decision model")
    client: str | None = clients[0] if len(clients) == 1 else None
    error: str | None = None
    if CLIENT_QUESTION in answers:
        chosen = answers[CLIENT_QUESTION].get("choice") if isinstance(
            answers[CLIENT_QUESTION], dict
        ) else None
        if chosen in clients:
            client = chosen
        else:
            error = "decision model answered an unknown client"
    level: int | None = None
    rating = answers.get(COMPLEXITY_QUESTION)
    score = rating.get("score") if isinstance(rating, dict) else None
    if isinstance(score, (int, float)) and not isinstance(score, bool):
        bounded = max(0.0, min(float(RATING_LEVELS - 1), float(score)))
        level = round(bounded * complexity_max / (RATING_LEVELS - 1))
    elif error is None:
        error = "decision model gave no complexity rating"
    return ShadowAnswer(client=client, complexity=level, error=error)


def consult(
    cfg: Mapping,
    signals: Signals,
    clients: Sequence[str],
    *,
    complexity_max: int,
    http_post: HttpPost = default_http_post,
) -> ShadowAnswer:
    """Ask the model; never raises."""
    if not clients or len(clients) > MAX_OPTIONS:
        return ShadowAnswer(error="client options out of range for decision model")
    problem = endpoint_problem(str(cfg["endpoint"]), bool(cfg.get("allow_remote", False)))
    if problem:
        return ShadowAnswer(error=f"decision model endpoint must be {problem}")
    try:
        body = _request_body(cfg, signals, list(clients))
        raw = http_post(
            str(cfg["endpoint"]), body, {}, float(cfg["timeout_seconds"])
        )
        return _parse(raw, list(clients), complexity_max)
    except DecisionModelError as exc:
        return ShadowAnswer(error=str(exc))
    except Exception as exc:  # noqa: BLE001 - shadow mode must never fail a spawn
        return ShadowAnswer(error=f"decision model failed ({type(exc).__name__})")


def apply(
    cfg: Mapping,
    route: Route,
    answer: ShadowAnswer,
    *,
    routing_cfg: Mapping,
    available: Callable[[str], bool],
) -> Route:
    if cfg.get("mode") != "active" or route.by not in (BY_POLICY, BY_STATIC):
        return route
    client = answer.client
    if client is None or client not in (routing_cfg.get("clients") or {}):
        return route
    if not available(client):
        return route
    level = answer.complexity if answer.complexity is not None else (route.complexity or 0)
    return Route(
        client,
        model_for(routing_cfg, client, level, None),
        BY_MODEL,
        level,
        route.scores,
    )

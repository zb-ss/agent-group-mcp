"""Task source interface and the HTTP seams shared by sources.

``default_http_get`` serves GET-only sources. ``default_http_send`` also posts a
body and returns the response headers; both refuse non-https URLs and
redirects, retry one HTTP 429 and raise errors that name only the host.
"""

from __future__ import annotations

import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date
from typing import Callable, Mapping, Protocol

from .model import Task

HttpGet = Callable[[str, Mapping[str, str], float], bytes]


@dataclass(frozen=True)
class HttpResponse:
    body: bytes
    headers: Mapping[str, str]  # names lower-cased


HttpSend = Callable[[str, "bytes | None", Mapping[str, str], float], HttpResponse]


class SourceError(Exception):
    """A source could not deliver tasks. Messages never carry secrets."""


# A bearer token is printable ASCII without spaces. Anything else (a CR or LF
# above all) would be refused by urllib with the header value in the message.
_TOKEN = re.compile(r"[\x21-\x7e]+")


def read_token(cfg: Mapping, env: Mapping[str, str], prefix: str = "") -> str:
    """The token from the environment variable named by `cfg["token_env"]`.
    Errors name the variable only, never its value; `prefix` (such as
    "github: ") starts every error message."""
    token_env = cfg["token_env"]
    token = env.get(token_env)
    if not token:
        raise SourceError(f"{prefix}environment variable {token_env} is not set")
    if _TOKEN.fullmatch(token) is None:
        raise SourceError(
            f"{prefix}environment variable {token_env} holds an invalid token "
            "(whitespace or control characters)"
        )
    return token


def _host(url: str) -> str:
    return urllib.parse.urlsplit(url).hostname or "unknown host"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects: the Authorization header must stay on one host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _build_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_NoRedirect)


def _retry_after(exc: urllib.error.HTTPError) -> float:
    try:
        return max(0.0, float(exc.headers.get("Retry-After", 0)))
    except (TypeError, ValueError, AttributeError):
        return 0.0


def default_http_get(
    url: str,
    headers: Mapping[str, str],
    timeout: float,
    *,
    max_retry_wait: float = 30.0,
    sleep: Callable[[float], None] = time.sleep,
) -> bytes:
    if not url.startswith("https://"):
        raise SourceError(f"refusing non-https url for {_host(url)}")
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    opener = _build_opener()
    for attempt in (0, 1):
        try:
            with opener.open(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt == 0:
                sleep(min(_retry_after(exc), float(max_retry_wait)))
                continue
            raise SourceError(f"HTTP {exc.code} from {_host(url)}") from None
        except (urllib.error.URLError, OSError):
            raise SourceError(f"request to {_host(url)} failed") from None
    raise SourceError(f"request to {_host(url)} failed")  # pragma: no cover


def default_http_send(
    url: str,
    body: bytes | None,
    headers: Mapping[str, str],
    timeout: float,
    *,
    max_retry_wait: float = 30.0,
    sleep: Callable[[float], None] = time.sleep,
) -> HttpResponse:
    if not url.startswith("https://"):
        raise SourceError(f"refusing non-https url for {_host(url)}")
    request = urllib.request.Request(
        url, data=body, headers=dict(headers), method="GET" if body is None else "POST"
    )
    opener = _build_opener()
    for attempt in (0, 1):
        try:
            with opener.open(request, timeout=timeout) as response:
                raw = getattr(response, "headers", None)
                lowered = {str(k).lower(): str(v) for k, v in raw.items()} if raw else {}
                return HttpResponse(body=response.read(), headers=lowered)
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt == 0:
                sleep(min(_retry_after(exc), float(max_retry_wait)))
                continue
            raise SourceError(f"HTTP {exc.code} from {_host(url)}") from None
        except (urllib.error.URLError, OSError):
            raise SourceError(f"request to {_host(url)} failed") from None
    raise SourceError(f"request to {_host(url)} failed")  # pragma: no cover


class TaskSource(Protocol):
    name: str

    def fetch(self, *, today: date) -> list[Task]: ...

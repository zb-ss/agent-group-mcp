"""Credentials for waking a session, kept where only this user can read them.

Some clients guard the local API that starts a turn with a token, and give
it only to the commands their agent runs (agy; see `clients/agy.py`). The
agent registers it with `agent-bus wake-register`; a sender reads it back
when it pushes (`push.py`).

Whoever holds the token can drive that session — run commands as this user
through its agent — so it is not kept in the bus database, which other
local users may be able to read. Each session's record is a file of its
own, readable by this user only, in a directory only this user can open,
named after a hash of the session id.

A record notes the client process the session ran in when it was written.
A conversation resumed in a new process has a new API and a new token, so
a record from another process is of no use (`for_route`), and one whose
process has ended is deleted. A record also notes that the agent was asked
to register, so that it is asked once per conversation and process rather
than before every model call.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from . import paths, procs
from .clients.base import PushCredentials
from .procs import Proc
from .routes import Route

DIR_NAME = "push-credentials"
DIR_MODE = 0o700
FILE_MODE = 0o600


def store_dir() -> Path:
    """Beside the database, so that a bus moved with AGENT_BUS_DB keeps its
    own."""
    return paths.db_path().parent / DIR_NAME


@dataclass(frozen=True)
class _Record:
    owner: Proc | None  # the client process, as the session's hooks saw it
    asked: bool
    endpoint: str | None = None
    secret: str | None = field(default=None, repr=False)


def _path(thread: str) -> Path:
    # a hash: the id comes from the client and must not pick the file
    digest = hashlib.sha256(thread.encode("utf-8")).hexdigest()[:32]
    return store_dir() / f"{digest}.json"


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _load(path: Path) -> _Record | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    owner = data.get("owner")
    proc = None
    if (
        isinstance(owner, list) and len(owner) == 2
        and isinstance(owner[0], int) and isinstance(owner[1], str)
    ):
        proc = Proc(owner[0], owner[1])
    return _Record(
        owner=proc,
        asked=data.get("asked") is True,
        endpoint=_text(data.get("endpoint")),
        secret=_text(data.get("secret")),
    )


def _private_dir() -> Path:
    """The store's directory, made if missing. Raises PermissionError for
    one that is not this user's own: the database's directory may be
    writable by others, who could have put a link or a directory of
    theirs there."""
    directory = store_dir()
    directory.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    info = os.lstat(directory)
    getuid = getattr(os, "getuid", None)  # POSIX only
    if stat.S_ISLNK(info.st_mode) or (getuid is not None and info.st_uid != getuid()):
        raise PermissionError(f"{directory} is not a directory of this user's own")
    # mkdir's mode is narrowed by the umask but never widened back, and a
    # directory made some other way may be open to others
    os.chmod(directory, DIR_MODE)
    return directory


def _save(thread: str, record: _Record) -> None:
    """Write the record beside its place and rename it into it, so a reader
    sees the old record or the new one, and the file is never readable by
    anyone else for a moment."""
    _private_dir()
    target = _path(thread)
    tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({
                "owner": [record.owner.pid, record.owner.started] if record.owner else None,
                "asked": record.asked,
                "endpoint": record.endpoint,
                "secret": record.secret,
            }, fh)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def _prune() -> None:
    """Delete the records of sessions whose client process has ended: the
    API their token opened is gone."""
    records = [(path, _load(path)) for path in store_dir().glob("*.json")]
    owners = [r.owner for _, r in records if r is not None and r.owner is not None]
    ended = procs.gone(owners)
    for path, record in records:
        if record is None or (record.owner is not None and record.owner in ended):
            path.unlink(missing_ok=True)


def request_once(thread: str, owner: Proc | None) -> bool:
    """Whether to ask the agent of session `thread`, running in client
    process `owner`, to register now: true the first time for that
    conversation and process, which is then noted. Raises OSError when
    that cannot be noted."""
    record = _load(_path(thread))
    if record is not None and record.owner == owner and record.asked:
        return False
    _save(thread, _Record(owner=owner, asked=True))
    _prune()
    return True


def register(thread: str, owner: Proc | None, endpoint: str, secret: str) -> None:
    """Keep session `thread`'s credentials, as of client process `owner`.
    Raises OSError when they cannot be written."""
    _save(thread, _Record(owner=owner, asked=True, endpoint=endpoint, secret=secret))
    _prune()


def for_route(route: Route) -> PushCredentials | None:
    """The credentials to push to `route`'s session, if its agent
    registered them in the client process the route says it runs in."""
    record = _load(_path(route.route_key))
    if record is None or record.endpoint is None or record.secret is None:
        return None
    if record.owner != route.process:
        return None
    # (thread, endpoint, secret)
    return PushCredentials(route.route_key, record.endpoint, record.secret)

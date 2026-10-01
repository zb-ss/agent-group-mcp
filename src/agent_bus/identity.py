"""Agent names and addresses.

  ``<group>``                    a repo's group — or a name with no client part,
                                 such as ``human`` or an agent wired before
                                 per-client identities existed
  ``<group>/<client>``           a client working in that repo: the address
                                 every session of that client shares
  ``<group>/<client>-<handle>``  one session of that client. The handle is a
                                 number the bus hands out (``-1``, ``-2``) or a
                                 label the session chose (``-frontend``)

The group is the repo slug, so every agent name from before per-client
identities existed is still a valid group. The separator lies outside the
slug alphabet: `slugify` can never emit it, which is what keeps a repo
literally called ``repo-claude`` distinct from client ``claude`` in
``repo``. Clients contain no dash, so everything after the first dash of
the member part is the handle.

Names never become filesystem paths.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

SEPARATOR = "/"
BROADCAST = "*"

# How a message was addressed: to one agent by name, to a repo's group by
# its bare name, or to everyone.
KIND_DIRECT = "direct"
KIND_GROUP = "group"
KIND_BROADCAST = "broadcast"

MAX_GROUP_LEN = 40
MAX_CLIENT_LEN = 12
MAX_HANDLE_LEN = 24
MIN_NUMBER = 1
MAX_NUMBER = 99
MAX_NAME_LEN = (
    MAX_GROUP_LEN + len(SEPARATOR) + MAX_CLIENT_LEN + len("-") + MAX_HANDLE_LEN
)

_NON_SLUG = re.compile(r"[^a-z0-9-]+")
_DASHES = re.compile(r"-+")
_CLIENT = re.compile(rf"[a-z][a-z0-9]{{0,{MAX_CLIENT_LEN - 1}}}")
_MEMBER = re.compile(rf"(?P<client>{_CLIENT.pattern})(?:-(?P<handle>.+))?")
_NUMBER = re.compile(r"[1-9][0-9]*")
_LABEL = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")


class InvalidNameError(ValueError):
    """The string is not a well-formed agent name."""


@dataclass(frozen=True)
class Identity:
    group: str
    client: str | None = None
    handle: str | None = None

    @property
    def name(self) -> str:
        if self.client is None:
            return self.group
        return compose(self.group, self.client, self.handle)

    @property
    def client_address(self) -> str | None:
        """The address every session of this client in this repo shares."""
        if self.client is None:
            return None
        return compose(self.group, self.client)

    @property
    def is_session(self) -> bool:
        return self.handle is not None

    @property
    def number(self) -> int | None:
        """The handle when the bus handed it out, else None."""
        return int(self.handle) if self.handle and self.handle.isdigit() else None

    @property
    def label(self) -> str | None:
        """The handle when a session chose it, else None."""
        return self.handle if self.handle and not self.handle.isdigit() else None


def slugify(name: str) -> str:
    s = name.lower()
    s = s.replace("_", "-").replace(".", "-")
    s = re.sub(r"\s+", "-", s)
    s = _NON_SLUG.sub("-", s)
    s = _DASHES.sub("-", s).strip("-")
    if not s:
        s = "repo"
    return s[:MAX_GROUP_LEN]


def is_valid_client(client: str) -> bool:
    return _CLIENT.fullmatch(client) is not None


def check_handle(handle: str) -> str:
    """`handle` if it is a session number (1..99) or a label; raises
    otherwise. A label starts with a letter so it never reads as a number."""
    if _NUMBER.fullmatch(handle):
        if not MIN_NUMBER <= int(handle) <= MAX_NUMBER:
            raise InvalidNameError(
                f"session number must be {MIN_NUMBER}..{MAX_NUMBER}, got {handle}"
            )
        return handle
    if len(handle) > MAX_HANDLE_LEN or _LABEL.fullmatch(handle) is None:
        raise InvalidNameError(
            f"invalid session handle {handle!r}: a number {MIN_NUMBER}..{MAX_NUMBER}, "
            f"or a label of lowercase letters, digits and single dashes that "
            f"starts with a letter, at most {MAX_HANDLE_LEN} characters"
        )
    return handle


def normalize_label(raw: str) -> str:
    """Turn what a person or an agent typed into a session label:
    `Release Notes` → `release-notes`. Raises when nothing usable is left."""
    s = _DASHES.sub("-", _NON_SLUG.sub("-", raw.strip().lower())).strip("-")
    s = s[:MAX_HANDLE_LEN].rstrip("-")
    if not s or not s[0].isalpha():
        raise InvalidNameError(
            f"invalid session label {raw!r}: it must start with a letter"
        )
    return check_handle(s)


def compose(group: str, client: str, handle: int | str | None = None) -> str:
    """Build ``<group>/<client>`` or ``<group>/<client>-<handle>``."""
    if not group or SEPARATOR in group:
        raise InvalidNameError(f"invalid group {group!r}")
    if not is_valid_client(client):
        raise InvalidNameError(
            f"invalid client {client!r}: lowercase letters and digits, "
            f"starting with a letter, at most {MAX_CLIENT_LEN} characters"
        )
    if handle is None:
        return f"{group}{SEPARATOR}{client}"
    return f"{group}{SEPARATOR}{client}-{check_handle(str(handle))}"


def parse(name: str) -> Identity:
    """Split a name into its parts. A name without a separator is a bare
    group and is not checked against the slug rules — names that predate
    them are still on people's buses."""
    if not name:
        raise InvalidNameError("name must be non-empty")
    if SEPARATOR not in name:
        return Identity(group=name)

    group, _, member = name.partition(SEPARATOR)
    match = _MEMBER.fullmatch(member)
    if not group or match is None:
        raise InvalidNameError(
            f"invalid agent name {name!r}: expected <group>{SEPARATOR}<client>"
            f" or <group>{SEPARATOR}<client>-<session>"
        )
    handle = match["handle"]
    if handle is None:
        return Identity(group=group, client=match["client"])
    return Identity(group=group, client=match["client"], handle=check_handle(handle))


def parse_or_none(name: str) -> Identity | None:
    try:
        return parse(name)
    except InvalidNameError:
        return None

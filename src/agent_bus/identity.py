"""Agent names and addresses.

  ``<group>``                 a repo's group — or a name with no client part,
                              such as ``human`` or an agent wired before
                              per-client identities existed
  ``<group>/<client>``        one client working in that repo
  ``<group>/<client>-<n>``    the n-th concurrent session of that client

The group is the repo slug, so every agent name from before per-client
identities existed is still a valid group. The separator lies outside the
slug alphabet: `slugify` can never emit it, which is what keeps a repo
literally called ``repo-claude`` distinct from client ``claude`` in
``repo``. Clients contain no dash so the ``-<n>`` suffix parses one way.

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
MIN_INSTANCE = 2
MAX_INSTANCE = 99
MAX_NAME_LEN = (
    MAX_GROUP_LEN + len(SEPARATOR) + MAX_CLIENT_LEN + len(f"-{MAX_INSTANCE}")
)

_NON_SLUG = re.compile(r"[^a-z0-9-]+")
_DASHES = re.compile(r"-+")
_CLIENT = re.compile(rf"[a-z][a-z0-9]{{0,{MAX_CLIENT_LEN - 1}}}")
_MEMBER = re.compile(rf"(?P<client>{_CLIENT.pattern})(?:-(?P<instance>[0-9]+))?")


class InvalidNameError(ValueError):
    """The string is not a well-formed agent name."""


@dataclass(frozen=True)
class Identity:
    group: str
    client: str | None = None
    instance: int | None = None

    @property
    def name(self) -> str:
        if self.client is None:
            return self.group
        return compose(self.group, self.client, self.instance)


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


def _check_instance(instance: int) -> None:
    if not MIN_INSTANCE <= instance <= MAX_INSTANCE:
        raise InvalidNameError(
            f"instance must be {MIN_INSTANCE}..{MAX_INSTANCE}, got {instance}"
        )


def compose(group: str, client: str, instance: int | None = None) -> str:
    """Build ``<group>/<client>[-<n>]``. Instance 1 is the plain name."""
    if not group or SEPARATOR in group:
        raise InvalidNameError(f"invalid group {group!r}")
    if not is_valid_client(client):
        raise InvalidNameError(
            f"invalid client {client!r}: lowercase letters and digits, "
            f"starting with a letter, at most {MAX_CLIENT_LEN} characters"
        )
    if instance is None or instance == 1:
        return f"{group}{SEPARATOR}{client}"
    _check_instance(instance)
    return f"{group}{SEPARATOR}{client}-{instance}"


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
            f" or <group>{SEPARATOR}<client>-<n>"
        )
    instance = match["instance"]
    if instance is None:
        return Identity(group=group, client=match["client"])
    _check_instance(int(instance))
    return Identity(group=group, client=match["client"], instance=int(instance))


def parse_or_none(name: str) -> Identity | None:
    try:
        return parse(name)
    except InvalidNameError:
        return None

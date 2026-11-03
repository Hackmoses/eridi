"""Identifiers.

Entry IDs are ULIDs: 26 uppercase Crockford base32 characters that sort by creation time and
are unique across devices without coordination. Other objects use a short prefix plus a ULID
(`root_…`, `proj_…`, `dev_…`) so an ID's kind is obvious wherever it appears.
"""

import re
import threading
from typing import Literal

from ulid import ULID

Prefix = Literal["root", "proj", "dev"]
PREFIXES: tuple[Prefix, ...] = ("root", "proj", "dev")

ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")

_lock = threading.Lock()
_last: ULID | None = None


def new_ulid() -> str:
    """Return a new ULID string, strictly increasing within this process.

    Plain ULIDs created in the same millisecond have random order. Entries must sort in the
    order they were written, so within one process we increment the previous value instead.
    """
    global _last
    with _lock:
        candidate = ULID()
        if _last is not None and int(candidate) <= int(_last):
            candidate = ULID.from_int(int(_last) + 1)
        _last = candidate
        return str(candidate)


def is_ulid(value: str) -> bool:
    """True if `value` is a canonical (uppercase) ULID string."""
    return bool(ULID_RE.match(value))


def new_id(prefix: Prefix) -> str:
    """Return a prefixed ID such as `proj_01JC8X…`."""
    if prefix not in PREFIXES:
        raise ValueError(f"Unknown ID prefix {prefix!r}. Use one of: {', '.join(PREFIXES)}.")
    return f"{prefix}_{new_ulid()}"


def split_id(value: str) -> tuple[Prefix, str]:
    """Split a prefixed ID into its prefix and ULID, validating both."""
    prefix, sep, rest = value.partition("_")
    if not sep or prefix not in PREFIXES or not is_ulid(rest):
        raise ValueError(
            f"{value!r} is not a valid ID. Expected a prefix ({', '.join(PREFIXES)}), "
            "an underscore and a 26-character ULID."
        )
    return prefix, rest

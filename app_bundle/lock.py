"""The dependency set the bundle installs, pinned by version and by content.

The file is an ordinary pip requirements file, so `pip install --require-hashes`
is what enforces it and this module never has to be right about wheel formats.
What this module adds is the refusal that comes *before* the download: a lock
with an unpinned or unhashed entry is rejected here, where the message can name
the entry, rather than by pip at the end of a hundred megabytes.

The engine's own distribution is **not** in the lock. It is not published, so it
is installed from the checkout with `--no-deps`; the lock is exactly the
third-party set, which is also exactly the set that contributes the Mach-O files
the signing plan has to enumerate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

#: How the lock is made, quoted in the refusal so nobody has to go looking.
REGENERATE = "python -m app_bundle lock"

_PINNED = re.compile(r"^(?P<name>[A-Za-z0-9._-]+)\s*==\s*(?P<version>[^\s;#]+)")
_HASH = re.compile(r"--hash=(?P<hash>sha256:[0-9a-f]{64})")


class LockError(Exception):
    """The lock is missing, or says something a hash-pinned lock may not say."""


@dataclass(frozen=True, slots=True)
class Locked:
    """One distribution, at one version, with every acceptable artefact's hash."""

    name: str
    version: str
    hashes: tuple[str, ...]


def read(path: Path) -> tuple[Locked, ...]:
    """Parse one lock file, or refuse and say what to do about it."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise LockError(
            f"no lock file at {path}. This host's architecture has none pinned; "
            f"generate one with `{REGENERATE}` and review it before it is used"
        ) from None

    locked: list[Locked] = []
    for entry in _entries(text):
        pinned = _PINNED.match(entry)
        if pinned is None:
            raise LockError(
                f"{path}: {entry.split()[0]!r} is not pinned with `==`. A lock that "
                "resolves at install time is not a lock"
            )
        hashes = tuple(found["hash"] for found in _HASH.finditer(entry))
        if not hashes:
            raise LockError(
                f"{path}: {pinned['name']} carries no --hash. The signable set of the "
                "bundle is mostly these wheels, so an unverified one is an unreviewed "
                "set of binaries to sign"
            )
        locked.append(Locked(name=pinned["name"], version=pinned["version"], hashes=hashes))

    if not locked:
        raise LockError(f"{path} pins nothing. Generate it with `{REGENERATE}`")
    return tuple(locked)


def _entries(text: str) -> list[str]:
    """One logical requirement per entry, with pip's line continuations joined."""
    joined = text.replace("\\\n", " ")
    return [
        line.strip()
        for line in joined.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

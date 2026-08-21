"""What makes a channel socket private, checked rather than assumed.

**This is the third independent copy of this discipline in the repository, and
it is deliberate.** The control plane has one and the Codex spoke has one. They
guard different wires that happen to agree today, and hoisting them into a
shared helper would make one spoke's tightening arrive silently in another's
threat model — a spoke is supposed to be able to change what it refuses without
asking anyone. The rules themselves are the ones both precedents settled: look
at the path entry rather than what it points at, refuse a symlink outright,
refuse anything another account owns, and treat the directory as the load-bearing
half.

The one rule that is this spoke's own is the length limit. The socket path is
built by the launch wrapper, travels through an environment variable, and is
bound by a process this engine does not start — so an over-long path fails
inside somebody else's process, at launch, as a bind error nobody is reading.
Refusing it here, by name and with the byte count, is what turned that into a
sentence in the reference implementation instead of two days of guessing.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

#: A directory only its owner may enter.
PRIVATE_DIRECTORY_MODE = 0o700

#: What a socket carrying a user's own speech must not be more open than.
PRIVATE_SOCKET_MODE = 0o600

#: Darwin's `sun_path` holds 104 bytes including the terminating NUL, so 103 is
#: the longest path that can ever be bound. Linux allows 107; the smaller limit
#: is used everywhere because a path that works on one machine and not another
#: is worse than one that is refused consistently.
MAX_SOCKET_PATH_BYTES = 103


class ChannelPathError(Exception):
    """A channel socket path cannot be trusted, or cannot be bound at all."""


def verify_bindable_length(path: Path) -> None:
    """Refuse a path no `AF_UNIX` socket could ever be bound to.

    The byte count is in the message because it is the only thing that separates
    "could not bind" from "could not bind because the path is too long".
    """
    used = len(str(path).encode("utf-8"))
    if used > MAX_SOCKET_PATH_BYTES:
        raise ChannelPathError(
            f"{path} is {used} bytes, and a Unix socket path may not exceed "
            f"{MAX_SOCKET_PATH_BYTES}; shorten the directory the channel sockets live in"
        )


def _own_stat(path: Path, what: str) -> os.stat_result:
    """Look at the path entry itself, never at whatever it points to."""
    try:
        found = os.lstat(path)
    except OSError as unreadable:
        raise ChannelPathError(f"cannot inspect {path}: {unreadable}") from None
    if stat.S_ISLNK(found.st_mode):
        raise ChannelPathError(
            f"{path} is a symbolic link; refusing to use a {what} reached through one"
        )
    if found.st_uid != os.geteuid():
        raise ChannelPathError(
            f"{path} belongs to uid {found.st_uid}, not to this user; refusing to use a "
            f"{what} another account owns"
        )
    return found


def verify_private_directory(directory: Path) -> None:
    """Prove nobody else can enter the directory, so nobody else can swap its contents."""
    found = _own_stat(directory, "directory")
    if not stat.S_ISDIR(found.st_mode):
        raise ChannelPathError(f"{directory} is not a directory")
    if stat.S_IMODE(found.st_mode) & ~PRIVATE_DIRECTORY_MODE:
        raise ChannelPathError(
            f"{directory} is reachable by other accounts (mode "
            f"{stat.S_IMODE(found.st_mode):04o}); refusing to keep a Session's channel "
            "socket there"
        )


def prepare_private_directory(directory: Path) -> None:
    """Make a directory only this user can enter, then check what it actually is."""
    try:
        directory.mkdir(mode=PRIVATE_DIRECTORY_MODE, parents=True, exist_ok=True)
    except OSError as refused:
        raise ChannelPathError(f"cannot create {directory}: {refused}") from None
    found = _own_stat(directory, "directory")
    if stat.S_ISDIR(found.st_mode) and stat.S_IMODE(found.st_mode) != PRIVATE_DIRECTORY_MODE:
        # It pre-existed with a wider mode, and it is ours, so narrow it.
        os.chmod(directory, PRIVATE_DIRECTORY_MODE)
    verify_private_directory(directory)


def verify_private_socket(path: Path) -> None:
    """Refuse to dial a socket this user does not own, or that others can reach."""
    verify_private_directory(path.parent)
    found = _own_stat(path, "socket")
    if not stat.S_ISSOCK(found.st_mode):
        raise ChannelPathError(f"{path} is not a socket")
    if stat.S_IMODE(found.st_mode) & ~PRIVATE_SOCKET_MODE:
        raise ChannelPathError(
            f"{path} is reachable by other accounts (mode "
            f"{stat.S_IMODE(found.st_mode):04o}); refusing to carry the user's words over it"
        )

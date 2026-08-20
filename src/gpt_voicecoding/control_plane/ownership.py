"""Who owns the socket, and whether anyone is actually behind it.

A Unix socket file is the whole authorisation story for a local interface: any
process that can reach the path can speak the protocol, so the path's owner and
mode are the only proof either side has that the peer is this user and not
another account on the same machine. Both directions check, because both are
handed a path they did not create — the engine adopts whatever is at the
configured path, and a surface dials whatever is there.

**A socket file outlives the process that bound it.** So the file answers "a
socket is here" and never "an engine is here"; only a connection tells those
apart. That distinction is why debris may be cleared while a live engine is
never displaced.

`owner_of` is a parameter rather than a call to `stat` in-line so a test can
describe a socket belonging to another account without needing a second account
— the same reason every adapter behind a seam has a fake.
"""

from __future__ import annotations

import os
import socket
import stat
from collections.abc import Callable
from pathlib import Path

#: Darwin caps `sun_path` at 104 bytes including its terminator, so a path of
#: 103 bytes is the longest one that can be bound. Checked before binding
#: because the failure otherwise arrives as an `OSError` from inside asyncio,
#: on somebody else's machine, at install time.
MAX_SOCKET_PATH_BYTES = 103

#: Owner read/write and nothing else, for the socket and its directory's leaf.
SOCKET_MODE = 0o600
DIRECTORY_MODE = 0o700

#: How the owning uid of a path is discovered. `lstat`, so a symlink planted by
#: another account is refused rather than followed.
OwnerOf = Callable[[Path], int]


def path_owner(path: Path) -> int:
    return path.lstat().st_uid


class SocketPathTooLong(ValueError):
    """The configured path cannot be bound on this platform, whoever wrote it."""

    def __init__(self, path: Path) -> None:
        super().__init__(
            f"a socket path may not exceed {MAX_SOCKET_PATH_BYTES} bytes; "
            f"{path} is {len(str(path).encode())}"
        )
        self.path = path


def verify_bindable(path: Path) -> None:
    """Refuse a path too long to bind, in words rather than in an `errno`."""
    if len(str(path).encode()) > MAX_SOCKET_PATH_BYTES:
        raise SocketPathTooLong(path)


class NotPrivate(PermissionError):
    """The path exists and belongs to somebody else, or is readable by them."""


def verify_private_directory(path: Path, *, owner_of: OwnerOf = path_owner) -> None:
    """Create or adopt one directory, only if this user exclusively owns it.

    Owning the socket proves nothing if another account owns the directory it
    can be renamed out of, so the parent is checked in its own right.
    """
    path.mkdir(mode=DIRECTORY_MODE, parents=True, exist_ok=True)
    if not stat.S_ISDIR(path.lstat().st_mode):
        raise NotPrivate(f"{path} is not a directory")
    if owner_of(path) != os.geteuid():
        raise NotPrivate(f"{path} is not owned by this user")
    os.chmod(path, DIRECTORY_MODE)


def verify_private_socket(path: Path, *, owner_of: OwnerOf = path_owner) -> None:
    """Refuse a socket this process does not exclusively own."""
    try:
        metadata = path.lstat()
    except OSError as error:
        raise NotPrivate(f"{path} is unavailable: {error}") from error
    if not stat.S_ISSOCK(metadata.st_mode):
        raise NotPrivate(f"{path} is not a Unix socket")
    if owner_of(path) != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise NotPrivate(f"{path} is not private to this user")


def is_connectable(path: Path, *, timeout: float) -> bool:
    """Whether anything is actually listening. The only question `stat` cannot answer."""
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(timeout)
    try:
        probe.connect(str(path))
    except OSError:
        return False
    finally:
        probe.close()
    return True

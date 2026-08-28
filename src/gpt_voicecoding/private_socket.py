"""Binding an `AF_UNIX` socket that is private from its first instant.

Every socket this product listens on carries something that belongs to the user:
their approval verdicts, the words they Relay into a Session, the control-plane
commands that flip their switches. None of them may exist at a mode another
account on the machine can reach — and *"may not exist"* is stronger than
*"is corrected shortly after"*.

Binding and then narrowing with `os.chmod` leaves a window. The socket is
created at `0o777 & ~umask`, which is `0o755` under the default `umask 022`, and
stays that way until the chmod lands. The window is short, and shortness is not
a property anything can rely on: a process descheduled between the two calls
holds it open for as long as the scheduler says. `tests/codex_fake.py` bound
that way, and on a loaded CI runner the codex adapter — which polls for the
socket file and checks its mode the instant it appears — landed inside the
window and refused a socket that was about to be made private
([#116](https://github.com/okqixiaobao727-design/GPT-VoiceCoding/issues/116)).
The refusal was correct. The bind was not.

So the mode comes from the umask at creation time, and the socket is never
observable at anything wider. The mask is set and restored around a *synchronous*
`bind`, with no `await` between: umask is process-global, and a coroutine that
suspended while holding it would lend its mask to whatever else the loop ran.

This is `bridge/daemon.py:699-706` of the legacy implementation — `server_bind`
wrapping `super().server_bind()` in `os.umask(0o777 & ~SOCKET_MODE)` — **ported**.
The rewrite carried it into `control_plane/server.py` and dropped it in the two
Claude-lane adapters (ADR 0010's shape); this module is where the three of them
say it once instead.

`mode` remains a parameter rather than a default because each listener passes
the policy its own verifier enforces. The Codex app-server is the one listener
bound by an external child, so its mode and forbidden-bit mask live here
together: the mask makes "no group, other or special permission bits" one rule
for its child umask and every attach, whether the server is engine-owned or the
shared daemon.

It lives at the top level, beside `locations.py`, for that module's own reason:
adapters may not import `control_plane` (ADR 0001, enforced in
`tests/test_architecture.py`), so a rule that `control_plane` and two adapters
all obey cannot live inside either of them. Like `locations`, this imports
nothing from this package.
"""

from __future__ import annotations

import asyncio
import os
import socket
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

__all__ = ["PRIVATE_SOCKET_MODE", "PRIVATE_SOCKET_UMASK", "start_private_unix_server"]

#: The normal steady-state mode for a socket carrying a live coding session.
PRIVATE_SOCKET_MODE = 0o600

#: Every group/other and setuid/setgid/sticky permission bit. Its ordinary
#: permission portion is the child umask, leaving directories owner-traversable
#: at 0700; the whole mask is the verifier's single forbidden-bits rule.
PRIVATE_SOCKET_UMASK = 0o7077

#: What `asyncio` binds a socket as when nothing narrows it: every permission
#: bit, less the umask. Named so the arithmetic below reads as the intent —
#: "everything the mode does not ask for is masked off" — rather than as a
#: number.
_ALL_PERMISSIONS = 0o777


async def start_private_unix_server(
    client_connected_cb: Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]],
    path: Path | str,
    *,
    mode: int,
    limit: int | None = None,
) -> asyncio.Server:
    """Serve on `path`, which never exists at a mode wider than `mode`.

    The drop-in shape of `asyncio.start_unix_server`, minus the window. `limit`
    is passed through only when given, so a caller that does not care keeps
    asyncio's own default rather than having one restated here.

    Raises whatever `bind` raises — an occupied path, an unreachable directory —
    with the socket closed rather than leaked, because a caller that cannot bind
    is a caller that will not be closing anything.
    """
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        previous = os.umask(_ALL_PERMISSIONS & ~mode)
        try:
            listener.bind(str(path))
        finally:
            os.umask(previous)
        # **Listening starts before the first `await`, not after it.** `bind` is
        # what makes the path appear, and a client that finds the path and
        # connects before anything is listening is refused outright — the codex
        # adapter polls for exactly this file and connects the moment it is
        # there. Leaving the two calls either side of a suspension point hands
        # the loop a window to run that client in, which is the same defect this
        # module exists to close, moved one call along. `asyncio` calls `listen`
        # again on its way to serving, which a listening socket accepts.
        listener.listen()
    except BaseException:
        listener.close()
        raise

    extra: dict[str, Any] = {} if limit is None else {"limit": limit}
    try:
        return await asyncio.start_unix_server(client_connected_cb, sock=listener, **extra)
    except BaseException:
        listener.close()
        raise

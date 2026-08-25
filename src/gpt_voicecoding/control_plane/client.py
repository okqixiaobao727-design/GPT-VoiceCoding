"""A surface's side of the wire: dial, ask once, read one line, hang up.

Bounded and timed on purpose. The engine being down is the ordinary case — the
menu-bar shell restarts it, a developer runs it by hand — so "down" must arrive
as a named failure within a known time, never as a surface that hangs waiting
for an answer that is not coming. That was the reference implementation's worst
shape: a Stop Notice route that blocked on a socket nobody was listening to.

The reply is bounded by the same limit the request is, because a surface must
not be made to hold an unbounded buffer either, and both sides read the number
from one place.

This raises rather than returning a refusal envelope. A refusal is something the
*engine* said; not reaching the engine is a different kind of news, and flattening
the two would let a surface render "no such switch" and "no engine" the same way.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from gpt_voicecoding.control_plane.ownership import OwnerOf, path_owner, verify_private_socket
from gpt_voicecoding.seams.control_plane import (
    MAX_REQUEST_BYTES,
    ErrorCode,
    MalformedRequest,
    Reply,
    Request,
)

#: Short enough that a surface which is never going to be answered says so while
#: the user is still watching. This is only ever a *slow reply* budget: an engine
#: that is not running fails immediately on `connect`, so nothing waits this long
#: to be told there is nothing there.
#:
#: One number for every action. It stopped being one when `launch` needed a
#: longer deadline than the rest (#28) — every action this engine still has
#: answers from state the hub already holds, so the per-action dispatch left
#: with the action that needed it. The part of #28's fix that has to survive is
#: not the dispatch but the default: a caller that names no deadline is given
#: one here rather than writing its own at the call site.
DEFAULT_TIMEOUT_SECONDS = 10.0


class EngineUnreachable(Exception):
    """The engine did not answer, or the path this user was told to use is not it."""

    code = ErrorCode.ENGINE_UNREACHABLE


class EngineSilent(EngineUnreachable):
    """The deadline passed with the request delivered and no reply behind it.

    Told apart from the rest of `EngineUnreachable` because the two license
    different advice. "Nothing is listening" means nothing is running and
    nothing is in flight; this means the engine took the request and may still
    be working on it. A surface that offered "it may still be running" for a
    socket nobody was listening to would be inventing the same kind of untruth
    #28 exists to remove, pointed the other way.
    """


async def ask(
    request: Request,
    *,
    path: Path,
    timeout: float | None = None,
    max_bytes: int = MAX_REQUEST_BYTES,
    owner_of: OwnerOf = path_owner,
) -> Reply:
    """Send one request to a running engine and return the one reply.

    `timeout` defaults to this engine's budget rather than being written at
    each call site, so a caller that does not think about it cannot accidentally
    hold an action to the wrong patience — which is the bug in #28.
    """
    if timeout is None:
        timeout = DEFAULT_TIMEOUT_SECONDS
    try:
        verify_private_socket(Path(path), owner_of=owner_of)
    except PermissionError as refused:
        raise EngineUnreachable(f"cannot use the engine socket: {refused}") from None

    try:
        async with asyncio.timeout(timeout):
            reader, writer = await asyncio.open_unix_connection(str(path), limit=max_bytes)
            try:
                writer.write(
                    json.dumps(request.as_document(), ensure_ascii=False).encode("utf-8") + b"\n"
                )
                await writer.drain()
                line = await reader.readuntil(b"\n")
            finally:
                writer.close()
                await _closed(writer)
    except TimeoutError:
        raise EngineSilent(f"the engine at {path} did not answer within {timeout:g}s") from None
    except asyncio.IncompleteReadError:
        raise EngineUnreachable(f"the engine at {path} closed without replying") from None
    except (asyncio.LimitOverrunError, ValueError):
        raise EngineUnreachable(
            f"the engine at {path} sent more than {max_bytes} bytes on one line"
        ) from None
    except OSError as error:
        raise EngineUnreachable(f"no engine listening on {path}: {error}") from None

    try:
        return Reply.of(json.loads(line.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError, MalformedRequest) as error:
        raise EngineUnreachable(f"the engine at {path} answered unreadably: {error}") from None


async def _closed(writer: asyncio.StreamWriter) -> None:
    """Closing is best-effort: the answer is already in hand by the time it runs."""
    try:
        await writer.wait_closed()
    except (OSError, asyncio.IncompleteReadError):
        pass

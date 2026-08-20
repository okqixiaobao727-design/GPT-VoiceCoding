"""The engine's side of the wire: one JSON object per line, over AF_UNIX.

No network socket is ever created, in either direction. The transport is
deliberately the dullest thing that works, because everything interesting is
one layer up: this reads a line, hands it to `ControlPlane`, and writes the
answer back.

Three properties it must have, each of which is an edge case #3 names:

- **One bad line costs one request, never the server.** A line that is not JSON
  and a line naming an unknown action are both answered with a refusal, and the
  connection carries on.
- **A line is bounded.** A peer that never sends a newline must not be able to
  make the engine hold an unbounded buffer, so the read has a limit and the
  connection that overran it is answered and then closed — there is no way to
  resync mid-line, and pretending otherwise would splice a peer's garbage onto
  the next request.
- **Connections do not queue behind each other.** Every connection is its own
  task with its own stream, so two surfaces can neither wedge nor read each
  other, and a reply is always written to the connection that asked.

The socket is private to this user, and a live engine is never displaced: a
socket file outlives whatever bound it, so debris is cleared while anything
still answering is refused.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Protocol

from gpt_voicecoding.control_plane.ownership import (
    SOCKET_MODE,
    OwnerOf,
    is_connectable,
    path_owner,
    verify_bindable,
    verify_private_directory,
)
from gpt_voicecoding.seams.control_plane import (
    MAX_REQUEST_BYTES,
    ErrorCode,
    MalformedRequest,
    Reply,
    Request,
)

_log = logging.getLogger(__name__)

#: How long the claim probe waits for a live engine to answer before the path is
#: judged debris. Short: it is a local socket, and a slow answer is still one.
CLAIM_PROBE_SECONDS = 1.0


class AlreadyServing(RuntimeError):
    """Another engine is listening on this path. Never displaced, only reported."""


class Answering(Protocol):
    """What the server needs of the control plane: one reply, and never a raise."""

    async def handle(self, request: Request) -> Reply: ...


class ControlPlaneServer:
    """Serves the control plane on one Unix domain socket."""

    def __init__(
        self,
        *,
        plane: Answering,
        path: Path,
        max_bytes: int = MAX_REQUEST_BYTES,
        owner_of: OwnerOf = path_owner,
    ) -> None:
        self._plane = plane
        self._path = Path(path)
        self._max_bytes = max_bytes
        self._owner_of = owner_of
        self._server: asyncio.AbstractServer | None = None

    @property
    def path(self) -> Path:
        return self._path

    async def start(self) -> None:
        """Claim the path and begin answering. Refuses a path this user cannot own."""
        verify_bindable(self._path)
        verify_private_directory(self._path.parent, owner_of=self._owner_of)
        self._claim()

        # The socket must never exist reachable by another account, not even
        # between bind and a later chmod, so the mode comes from umask at
        # creation time rather than from a second call afterwards.
        previous = os.umask(0o777 & ~SOCKET_MODE)
        try:
            self._server = await asyncio.start_unix_server(
                self._serve, path=str(self._path), limit=self._max_bytes
            )
        finally:
            os.umask(previous)

    async def aclose(self) -> None:
        """Stop answering and take the socket file with it.

        **Only the socket this server actually bound.** A start that was refused
        — because another engine is already listening — must not remove that
        engine's socket on its way out, which is the same rule as never
        displacing a live engine, applied to the failure path.
        """
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None
        self._path.unlink(missing_ok=True)

    def _claim(self) -> None:
        """Take over a stale socket file, but never displace a live engine."""
        if not self._path.exists():
            return
        if self._owner_of(self._path) != os.geteuid():
            raise PermissionError(f"{self._path} belongs to another user")
        if is_connectable(self._path, timeout=CLAIM_PROBE_SECONDS):
            raise AlreadyServing(f"another engine is already listening on {self._path}")
        self._path.unlink(missing_ok=True)  # nobody answered; the file is debris

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """One connection: read lines until the peer goes away."""
        try:
            while True:
                try:
                    line = await reader.readuntil(b"\n")
                except asyncio.IncompleteReadError:
                    return  # the peer closed, with or without a trailing line
                except (asyncio.LimitOverrunError, ValueError):
                    await self._write(
                        writer,
                        Reply.refused(
                            None,
                            ErrorCode.MALFORMED_REQUEST,
                            f"a request may not exceed {self._max_bytes} bytes",
                        ),
                    )
                    return  # there is no honest way to resync inside a line
                if len(line) > self._max_bytes:
                    await self._write(
                        writer,
                        Reply.refused(
                            None,
                            ErrorCode.MALFORMED_REQUEST,
                            f"a request may not exceed {self._max_bytes} bytes",
                        ),
                    )
                    return
                await self._write(writer, await self._answer(line))
        except (ConnectionResetError, BrokenPipeError):
            _log.info("a control-plane surface went away mid-exchange")
        finally:
            writer.close()

    async def _answer(self, line: bytes) -> Reply:
        """Read one line and answer it. Every failure here is one request's."""
        try:
            document = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            return Reply.refused(None, ErrorCode.MALFORMED_REQUEST, f"not JSON: {error}")

        try:
            request = Request.of(document)
        except MalformedRequest as unreadable:
            return Reply.refused(None, unreadable.code, str(unreadable))

        try:
            return await self._plane.handle(request)
        except Exception as unexpected:  # an engine bug, not the surface's fault
            _log.exception("the control plane raised answering %s", request.action)
            return Reply.refused(request.action, ErrorCode.REFUSED, str(unexpected) or "engine bug")

    async def _write(self, writer: asyncio.StreamWriter, reply: Reply) -> None:
        writer.write(json.dumps(reply.as_document(), ensure_ascii=False).encode("utf-8") + b"\n")
        await writer.drain()

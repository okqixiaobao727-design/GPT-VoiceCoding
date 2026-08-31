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

**A surface that is merely connected may not hold the engine open** (#96). Since
Python 3.12, `asyncio.Server.wait_closed()` waits for every live connection
handler, and this server's handler reads lines "until the peer goes away" — so
one idle peer used to pin `aclose` *forever*, and because the control plane
closes before the adapters do, the Codex app-server the engine spawned was then
never terminated. Measured: the engine survived SIGTERM indefinitely and its
app-server outlived the SIGKILL still holding its socket, which is what refused
the acceptance run after it.

The stop is therefore a **state this server enters**, not a wait on peers: a
handler parked on the *next* request returns at once, and a handler *mid-reply*
finishes the reply it is writing and then finds the same state. No reply is cut
in half and nothing is waited out, so no grace period is needed and none is
written here. A request whose bytes had not yet reached the reader when the stop
landed is the one thing that does not survive — it gets EOF rather than an
answer, which `_next_line` explains.

**Adapted from legacy, which never had this bug and is worth saying why.**
`legacy@1d32845:bridge/daemon.py:472-498` served one request per connection and
put a read deadline on the socket first (`self.request.settimeout(...)`,
`client_timeout_seconds`), on a sequential `socketserver.UnixStreamServer` whose
`server_close` waits for nobody. No handler there could be parked on a silent
peer, so no stop could be held by one. The rewrite kept several requests per
connection on purpose — the Companion Channel holds one open — which makes
legacy's one-shot shape unportable, and dropped the property it protected along
with it (ADR 0010's pattern exactly). What is restored here is that property,
by the means the multi-request shape allows.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import struct
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
from gpt_voicecoding.control_plane.progress_publication import (
    ProgressPublication,
    encode_reply,
)
from gpt_voicecoding.private_socket import start_private_unix_server
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

#: The last resort on the way out: how long `aclose` waits for the handlers to
#: notice the stop before it stops waiting and says which peers were still on.
#: It is **not** a grace period — every handler returns on its own by then — but
#: `Answering.handle` is somebody else's coroutine, and a shutdown that could be
#: held open by one slow action is the whole defect this file just fixed. Sized
#: to fit the engine's whole shutdown budget alongside the phases after it
#: (`engine/runner.py` § SHUTDOWN_SECONDS), not chosen on its own.
STOP_TIMEOUT_SECONDS = 3.0

#: Reading the pid on the far end of an `AF_UNIX` connection on Darwin.
#: `getpeername` is the empty string for a Unix client, so a connection still
#: open at stop could otherwise only be counted, never named — and "which
#: surface was still connected" is precisely what #96's artifacts could not say.
#: Measured on this machine 2026-08-26: `getsockopt(0, 0x002, 4)` returns the
#: connecting process's pid. Named here rather than imported because the socket
#: module carries neither constant.
SOL_LOCAL = 0
LOCAL_PEERPID = 0x002


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
        progress_publication: ProgressPublication | None = None,
    ) -> None:
        if progress_publication is not None and progress_publication.max_bytes != max_bytes:
            raise ValueError("the server and progress publication capacities must agree")
        self._plane = plane
        self._path = Path(path)
        self._max_bytes = max_bytes
        self._progress_publication = progress_publication or ProgressPublication(
            max_bytes=max_bytes
        )
        self._owner_of = owner_of
        self._server: asyncio.AbstractServer | None = None
        #: Set once, on the way out. Every handler reads it between requests.
        self._stopping = asyncio.Event()
        #: The connections being served right now, so the stop can say who was
        #: still on. A writer is added when its handler starts and discarded in
        #: that handler's `finally`, so this never outlives the connection.
        self._live: set[asyncio.StreamWriter] = set()

    @property
    def path(self) -> Path:
        return self._path

    async def start(self) -> None:
        """Claim the path and begin answering. Refuses a path this user cannot own."""
        verify_bindable(self._path)
        verify_private_directory(self._path.parent, owner_of=self._owner_of)
        self._claim()
        self._stopping.clear()  # a server that was stopped and started again serves

        # The socket must never exist reachable by another account, not even
        # between bind and a later chmod, so the mode comes from umask at
        # creation time rather than from a second call afterwards.
        self._server = await start_private_unix_server(
            self._serve, self._path, mode=SOCKET_MODE, limit=self._max_bytes
        )

    async def aclose(self) -> None:
        """Stop answering and take the socket file with it.

        **Only the socket this server actually bound.** A start that was refused
        — because another engine is already listening — must not remove that
        engine's socket on its way out, which is the same rule as never
        displacing a live engine, applied to the failure path.

        **The stop is announced before it is waited on**, and it names the peers
        still connected when it began. A connection open at this moment is the
        one fact #96's artifacts could not recover — the engine was killed with
        its shutdown unfinished and nothing had written down who was holding it.
        """
        if self._server is None:
            return
        server, self._server = self._server, None
        _log.info("stopping the control plane on %s; %s", self._path, self._peers())
        self._stopping.set()
        server.close()
        try:
            async with asyncio.timeout(STOP_TIMEOUT_SECONDS):
                await server.wait_closed()
        except TimeoutError:
            # Reached only if somebody's `handle` is still running, which is a
            # bug in that action rather than here — so it is named and left
            # rather than waited out, and the engine goes on stopping.
            _log.warning(
                "the control plane did not finish within %.0fs; %s",
                STOP_TIMEOUT_SECONDS,
                self._peers(),
            )
        self._path.unlink(missing_ok=True)

    def _peers(self) -> str:
        """The connections still being served, by the pid on the far end."""
        if not self._live:
            return "no surface was connected"
        named = sorted(_peer_pid(writer) for writer in self._live)
        return f"{len(named)} surface(s) still connected: {', '.join(named)}"

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
        """One connection: read lines until the peer goes away, or this server stops."""
        self._live.add(writer)
        # One waiter for the life of the connection, not one per request.
        # `asyncio.wait` does not consume a completed future, so this can be
        # raced against every read in turn — and the alternative appended and
        # removed an `Event._waiters` entry on each one.
        stopping = asyncio.ensure_future(self._stopping.wait())
        try:
            while True:
                try:
                    line = await self._next_line(reader, stopping)
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
                if line is None:
                    return  # this server is stopping and this peer was not mid-request
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
            stopping.cancel()
            self._live.discard(writer)
            writer.close()

    async def _next_line(
        self, reader: asyncio.StreamReader, stopping: asyncio.Future[bool]
    ) -> bytes | None:
        """The next request on this connection, or `None` once the server stops.

        The read is raced against the stop rather than interrupted by it, so the
        two states a handler can be in are told apart without asking: parked
        here waiting for a peer that may never speak again — which returns now —
        or somewhere below, writing a reply, which finishes and arrives here
        next. Whatever `readuntil` raised is raised here, so one bad line still
        costs one request.

        **A request already on the wire when the stop lands is answered with
        EOF, not with a refusal.** If the bytes have not reached the reader yet,
        the race is decided in the stop's favour and the peer sees the
        connection close mid-flight — which `client.ask` reports as "the engine
        closed without replying". That is honest and it is the ordinary shape of
        racing a shutdown, but it is not "nothing is truncated", so it is said
        here rather than left for somebody to find. Nothing is *consumed*:
        `readuntil` removes from its buffer only once it has found the
        separator, so a cancelled read costs no bytes.
        """
        reading = asyncio.ensure_future(reader.readuntil(b"\n"))
        abandoned = False
        try:
            await asyncio.wait({reading, stopping}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            # Cancelled here rather than after, because this `await` can also end
            # by *this* handler being cancelled — and a read left pending then
            # outlives the loop that owns it. `stopping` belongs to `_serve`,
            # whose own `finally` retires it.
            if not reading.done():
                reading.cancel()
                abandoned = True
        if abandoned:
            return None
        return reading.result()

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
        writer.write(encode_reply(self._progress_publication.final(reply)))
        await writer.drain()


def _peer_pid(writer: asyncio.StreamWriter) -> str:
    """The pid on the far end of one connection, or why it could not be read."""
    sock = writer.get_extra_info("socket")
    if sock is None:
        return "pid unknown"
    try:
        raw = sock.getsockopt(SOL_LOCAL, LOCAL_PEERPID, struct.calcsize("i"))
    except OSError as unreadable:  # a platform without it, or a socket already gone
        return f"pid unknown ({unreadable.strerror or unreadable})"
    return f"pid {struct.unpack('i', raw)[0]}"

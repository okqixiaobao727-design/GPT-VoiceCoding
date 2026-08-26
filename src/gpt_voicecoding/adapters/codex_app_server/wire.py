"""One WebSocket-framed JSON-RPC connection to a ``codex app-server``.

The app-server speaks JSON-RPC 2.0 inside WebSocket text frames, and its Unix
socket route is the only one a ``codex --remote unix://PATH`` TUI can also
attach to. The framing is hand-rolled rather than taken from a library because
this package ships with no dependencies and one client's worth of RFC 6455 is
some eighty lines — the reference implementation made the same trade and it held.

**Three kinds of inbound message, three destinations.** A response goes to the
future its request is waiting on; a notification goes to the notification
handler; and a *server request* — which is how an approval reaches us — goes to
the request handler, which is expected to answer it later, by id, possibly long
after this call returned. Anything the handler does not answer stays unanswered
on purpose: on the Codex wire, declining to answer an approval is how it is left
to the on-screen dialog, so silence has to be a thing this layer can express.

**A closed socket fails every outstanding request, loudly.** The one thing a
transport must never do is let a caller wait forever on a connection that is
gone: the caller's whole job is classifying an attempt, and a request that never
returns is classified as nothing at all.

Nothing here grades a delivery, knows what a Relay is, or retries. It moves
frames and matches ids.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import struct
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

#: RFC 6455's fixed accept-key salt. Not a secret; the handshake is a proof of
#: protocol, not of identity.
WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

#: How much of one frame this client will hold before it decides the far side is
#: not a codex app-server. Generous enough for a whole thread readback.
DEFAULT_MAX_FRAME_BYTES = 32 * 1024 * 1024

#: How long one request waits for its answer when the caller states no deadline.
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0

#: How long the closing courtesy gets. `aclose` sends a close frame and waits for
#: the transport to finish, and both of those are waits on the far side: a peer
#: that has stopped reading holds them for as long as it likes. That is a wait
#: inside the engine's shutdown, and #96 is what an unbounded one there costs —
#: the app-server this connection belongs to is stopped *after* it, so a hang
#: here is a leaked process. The frame is a courtesy; going is not optional.
#:
#: One second because this is a local `AF_UNIX` socket with an empty frame on
#: it: a write that has not left in a second is not waiting on bandwidth, it is
#: waiting on a peer that has stopped reading, and that peer is about to be
#: signalled anyway.
CLOSE_TIMEOUT_SECONDS = 1.0

_OPCODE_CONTINUATION = 0x0
_OPCODE_TEXT = 0x1
_OPCODE_BINARY = 0x2
_OPCODE_CLOSE = 0x8
_OPCODE_PING = 0x9
_OPCODE_PONG = 0xA

#: What JSON-RPC calls "method not found". Answered to any server request this
#: client was not built to handle, so the far side is told rather than stalled.
METHOD_NOT_FOUND = -32601


class WireError(Exception):
    """This connection could not carry, or could not read, one message."""


class WireClosed(WireError):
    """The connection is gone. Distinct from a request that failed on a live one."""


class RemoteError(WireError):
    """The far side answered a request with a JSON-RPC error object."""

    def __init__(self, method: str, code: int | None, message: str) -> None:
        super().__init__(f"{method} failed: {message}")
        self.method = method
        self.code = code
        self.remote_message = message


#: What a handler is handed: the decoded JSON-RPC message, whole.
Message = dict[str, Any]
NotificationHandler = Callable[[Message], None]
ServerRequestHandler = Callable[[Message], Awaitable[None] | None]
#: Called once, with the reason, when the far side goes away by itself. Not
#: called for a close this side asked for: the owner already knows about those.
ClosedHandler = Callable[[str], None]


class AppServerConnection:
    """One live connection. Open it, use it, close it — and it closes once."""

    def __init__(
        self,
        socket_path: Path | str,
        *,
        on_notification: NotificationHandler | None = None,
        on_server_request: ServerRequestHandler | None = None,
        on_closed: ClosedHandler | None = None,
        request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
    ) -> None:
        self._path = Path(socket_path)
        self._on_notification = on_notification
        self._on_server_request = on_server_request
        self._on_closed = on_closed
        self._request_timeout = request_timeout_seconds
        self._max_frame_bytes = max_frame_bytes

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._pump: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[Message]] = {}
        self._handling: set[asyncio.Task[None]] = set()
        self._next_id = 1
        self._closed = False
        #: Why the connection ended, so a caller is told the reason rather than
        #: just that there is no longer one.
        self._closed_reason = ""
        self._write_lock = asyncio.Lock()

    @property
    def socket_path(self) -> Path:
        return self._path

    @property
    def is_open(self) -> bool:
        return self._writer is not None and not self._closed

    async def connect(self) -> None:
        """Dial the socket and complete the WebSocket upgrade. Idempotent."""
        if self.is_open:
            return
        if self._closed:
            raise WireClosed("this connection was closed and cannot be reopened")
        try:
            reader, writer = await asyncio.open_unix_connection(str(self._path))
        except OSError as error:
            raise WireError(f"cannot reach the codex app-server at {self._path}: {error}") from None
        self._reader, self._writer = reader, writer
        try:
            await self._handshake()
        except BaseException:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()
            self._reader = self._writer = None
            raise
        self._pump = asyncio.create_task(self._pumping(), name=f"codex-wire-{self._path.name}")

    async def request(
        self, method: str, params: Message | None = None, *, timeout_seconds: float | None = None
    ) -> Message:
        """Call one method and wait for its answer, or raise saying why not."""
        writer = self._require_open()
        request_id = self._next_id
        self._next_id += 1
        waiting: asyncio.Future[Message] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = waiting
        try:
            await self._send({"id": request_id, "method": method, "params": params or {}}, writer)
            answer = await asyncio.wait_for(
                waiting, timeout_seconds if timeout_seconds is not None else self._request_timeout
            )
        except TimeoutError:
            raise WireError(f"{method} did not answer in time") from None
        finally:
            self._pending.pop(request_id, None)

        error = answer.get("error")
        if error is not None:
            code = error.get("code") if isinstance(error, dict) else None
            detail = error.get("message") if isinstance(error, dict) else str(error)
            raise RemoteError(method, code, str(detail))
        result = answer.get("result")
        if result is None:
            return {}
        if not isinstance(result, dict):
            raise WireError(f"{method} answered with something that is not an object")
        return result

    async def notify(self, method: str, params: Message | None = None) -> None:
        """Send one notification. Nothing answers it and nothing waits for it."""
        writer = self._require_open()
        await self._send({"method": method, "params": params or {}}, writer)

    async def respond(self, request_id: Any, result: Message) -> None:
        """Answer one server request — an approval verdict is exactly this."""
        writer = self._require_open()
        await self._send({"id": request_id, "result": result}, writer)

    async def respond_error(self, request_id: Any, code: int, message: str) -> None:
        """Refuse one server request, saying so rather than leaving it hanging."""
        writer = self._require_open()
        await self._send({"id": request_id, "error": {"code": code, "message": message}}, writer)

    async def aclose(self) -> None:
        """Close once, fail whatever was still waiting, and never raise twice."""
        if self._closed:
            return
        self._closed = True
        if not self._closed_reason:
            self._closed_reason = "this connection was closed"

        pump, self._pump = self._pump, None
        if pump is not None:
            pump.cancel()
            with suppress(asyncio.CancelledError):
                await pump

        for task in list(self._handling):
            task.cancel()
        for task in list(self._handling):
            with suppress(asyncio.CancelledError, Exception):
                await task
        self._handling.clear()

        writer, self._writer = self._writer, None
        self._reader = None
        if writer is not None:
            with suppress(Exception):
                async with asyncio.timeout(CLOSE_TIMEOUT_SECONDS):
                    writer.write(self._frame(b"", _OPCODE_CLOSE))
                    await writer.drain()
            writer.close()
            with suppress(Exception):
                async with asyncio.timeout(CLOSE_TIMEOUT_SECONDS):
                    await writer.wait_closed()
        self._fail_pending(self._closed_reason)

    # -- the reader -------------------------------------------------------

    async def _pumping(self) -> None:
        """Read frames until the far side stops. The only place messages arrive."""
        try:
            while True:
                message = await self._read_message()
                self._route(message)
        except asyncio.CancelledError:
            raise
        except (WireError, OSError, asyncio.IncompleteReadError) as ending:
            self._closed_reason = f"the codex app-server connection ended: {ending}"
            self._fail_pending(self._closed_reason)
            # Only an ending this side did not ask for is announced. A caller
            # that closed the connection itself already knows, and telling it
            # would turn every ordinary shutdown into a loss event.
            self._closed = True
            if self._on_closed is not None:
                self._on_closed(self._closed_reason)

    def _route(self, message: Message) -> None:
        """Three kinds of message, three destinations. See the module docstring."""
        has_method = isinstance(message.get("method"), str)
        has_id = message.get("id") is not None

        if has_method and has_id:
            self._dispatch_server_request(message)
            return
        if has_method:
            if self._on_notification is not None:
                self._on_notification(message)
            return

        waiting = self._pending.pop(_as_request_id(message.get("id")), None)
        if waiting is not None and not waiting.done():
            waiting.set_result(message)

    def _dispatch_server_request(self, message: Message) -> None:
        """Hand it to the handler, or answer "method not found" so nothing stalls."""
        handler = self._on_server_request
        if handler is None:
            self._spawn(self._refuse(message))
            return
        outcome = handler(message)
        if outcome is not None:
            self._spawn(outcome)

    async def _refuse(self, message: Message) -> None:
        with suppress(WireError):
            await self.respond_error(
                message["id"], METHOD_NOT_FOUND, f"unsupported server request {message['method']}"
            )

    def _spawn(self, work: Awaitable[None]) -> None:
        """Run a handler's coroutine without letting it outlive this connection."""
        task = asyncio.ensure_future(work)
        self._handling.add(task)
        task.add_done_callback(self._handling.discard)

    def _fail_pending(self, reason: str) -> None:
        for waiting in list(self._pending.values()):
            if not waiting.done():
                waiting.set_exception(WireClosed(reason))
        self._pending.clear()

    # -- framing ----------------------------------------------------------

    async def _handshake(self) -> None:
        """The client half of RFC 6455, verified rather than assumed."""
        reader, writer = self._reader, self._writer
        assert reader is not None and writer is not None
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        writer.write(
            (
                "GET / HTTP/1.1\r\n"
                "Host: localhost\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n"
                "\r\n"
            ).encode("ascii")
        )
        await writer.drain()
        try:
            header = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError) as error:
            raise WireError(f"the codex app-server closed during the handshake: {error}") from None

        lines = header.decode("iso-8859-1").split("\r\n")
        if not lines or " 101 " not in lines[0]:
            raise WireError(
                f"the codex app-server refused the WebSocket upgrade: {lines[0] if lines else ''!r}"
            )
        headers: dict[str, str] = {}
        for line in lines[1:]:
            name, separator, value = line.partition(":")
            if separator:
                headers[name.strip().casefold()] = value.strip()
        expected = base64.b64encode(
            hashlib.sha1((key + WEBSOCKET_GUID).encode("ascii")).digest()  # noqa: S324
        ).decode("ascii")
        if headers.get("sec-websocket-accept") != expected:
            raise WireError("the codex app-server's WebSocket accept key does not match")

    async def _send(self, message: Message, writer: asyncio.StreamWriter) -> None:
        payload = json.dumps(message, separators=(",", ":")).encode("utf-8")
        if len(payload) > self._max_frame_bytes:
            raise WireError("this message is larger than the configured frame limit")
        async with self._write_lock:
            try:
                writer.write(self._frame(payload, _OPCODE_TEXT))
                await writer.drain()
            except OSError as error:
                raise WireClosed(f"writing to the codex app-server failed: {error}") from None

    def _frame(self, payload: bytes, opcode: int) -> bytes:
        """One masked client frame. A client always masks; a server never does."""
        mask = os.urandom(4)
        length = len(payload)
        header = bytearray((0x80 | opcode,))
        if length <= 125:
            header.append(0x80 | length)
        elif length <= 0xFFFF:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return bytes(header) + mask + masked

    async def _read_message(self) -> Message:
        """One whole JSON-RPC message, reassembled across continuation frames."""
        fragments = bytearray()
        started = False
        while True:
            opcode, final, payload = await self._read_frame()
            if opcode == _OPCODE_CLOSE:
                raise WireError("the codex app-server closed the WebSocket")
            if opcode == _OPCODE_PING:
                await self._pong(payload)
                continue
            if opcode == _OPCODE_PONG:
                continue
            if opcode in (_OPCODE_TEXT, _OPCODE_BINARY) and not started:
                fragments.extend(payload)
                started = True
            elif opcode == _OPCODE_CONTINUATION and started:
                fragments.extend(payload)
            else:
                raise WireError(f"unexpected WebSocket opcode {opcode} from the codex app-server")
            if len(fragments) > self._max_frame_bytes:
                raise WireError("a codex app-server message exceeds the configured frame limit")
            if final:
                break
        try:
            message = json.loads(bytes(fragments))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise WireError(f"the codex app-server sent unreadable JSON: {error}") from None
        if not isinstance(message, dict):
            raise WireError("the codex app-server sent something that is not a JSON object")
        return message

    async def _pong(self, payload: bytes) -> None:
        writer = self._writer
        if writer is None:
            return
        async with self._write_lock:
            with suppress(OSError):
                writer.write(self._frame(payload, _OPCODE_PONG))
                await writer.drain()

    async def _read_frame(self) -> tuple[int, bool, bytes]:
        reader = self._reader
        if reader is None:
            raise WireClosed("this connection is not open")
        first, second = await reader.readexactly(2)
        if first & 0x70:
            raise WireError("the codex app-server set reserved WebSocket bits")
        final = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", await reader.readexactly(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", await reader.readexactly(8))[0]
        if length > self._max_frame_bytes:
            raise WireError("a codex app-server frame exceeds the configured frame limit")
        mask = await reader.readexactly(4) if masked else b""
        payload = await reader.readexactly(length) if length else b""
        if masked:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return opcode, final, payload

    def _require_open(self) -> asyncio.StreamWriter:
        writer = self._writer
        if writer is None or self._closed:
            raise WireClosed(self._closed_reason or "this connection is not open")
        return writer


def _as_request_id(value: Any) -> int:
    """JSON-RPC ids are ours and always whole numbers; anything else matches nothing."""
    return value if isinstance(value, int) and not isinstance(value, bool) else -1

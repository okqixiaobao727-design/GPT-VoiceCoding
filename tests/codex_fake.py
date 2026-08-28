"""A fake ``codex app-server``: a real socket, real frames, scripted answers.

The Codex adapter's whole job is classifying what a far side did, so its tests
need a far side that can be made to do the awkward things — answer late, answer
with a readback that contradicts the attempt, raise an approval from the server
side, or die mid-request. A mock of the adapter's own transport could not
exercise any of that, because the transport is the part being trusted.

So this is a real `asyncio` Unix server that speaks the server half of RFC 6455
and JSON-RPC. It is deliberately not a simulator of Codex: it answers exactly
what a test tells it to answer, and it records what it was asked.

Its shape is taken from the installed codex 0.148.0's own generated JSON schema
(`codex app-server generate-json-schema`), so the payloads the tests assert on
are the ones the real server uses, not ones invented here.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import struct
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gpt_voicecoding.adapters.codex_app_server.wire import WEBSOCKET_GUID
from gpt_voicecoding.private_socket import PRIVATE_SOCKET_MODE, start_private_unix_server

Message = dict[str, Any]
#: What a test installs to answer one method. Returning a dict answers it;
#: raising `FakeRemoteError` answers with a JSON-RPC error.
Responder = Callable[[Message], Awaitable[Message] | Message]


class FakeRemoteError(Exception):
    """Answer this call with a JSON-RPC error object instead of a result."""

    def __init__(self, message: str, code: int = -32600) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class Call:
    """One request the fake was asked to answer."""

    method: str
    params: Message


@dataclass
class FakeAppServer:
    """One scripted app-server on one socket. Start it, point a client at it, stop it."""

    path: Path
    #: Method name to what answers it. A method with no responder answers `{}`.
    responders: dict[str, Responder] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._server: asyncio.AbstractServer | None = None
        self._connections: list[asyncio.StreamWriter] = []
        self._sessions: list[_FakeSession] = []
        self._closed_ids: set[Any] = set()
        self._next_server_id = 1000

    # -- lifecycle --------------------------------------------------------

    async def start(self) -> FakeAppServer:
        """Bind the way real codex does: private from the socket's first instant.

        Real codex creates its socket 0600, and the adapter refuses to speak to
        one that is more open than that. A fake that left it at whatever the
        umask gave would be a fake the privacy check could never pass — and a
        fake that bound wide and narrowed it afterwards is one the check can
        catch mid-window, which is what made this a red build on a loaded runner
        (#116). Binding through the product's own helper is what keeps the
        double honest about the one property the adapter inspects.
        """
        self._server = await start_private_unix_server(
            self._serve, self.path, mode=PRIVATE_SOCKET_MODE
        )
        return self

    async def aclose(self) -> None:
        for session in list(self._sessions):
            await session.aclose()
        self._sessions.clear()
        server, self._server = self._server, None
        if server is not None:
            server.close()
            with suppress(Exception):
                await server.wait_closed()
        with suppress(FileNotFoundError):
            self.path.unlink()

    async def __aenter__(self) -> FakeAppServer:
        return await self.start()

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    # -- what a test drives it with ---------------------------------------

    def answers(self, method: str, responder: Responder | Message) -> None:
        """Install the answer for one method, as a value or as a callable."""
        if callable(responder):
            self.responders[method] = responder
        else:
            self.responders[method] = lambda _params, _r=responder: _r

    def calls_to(self, method: str) -> list[Message]:
        """Every params object this fake was sent for one method, in order."""
        return [call.params for call in self.calls if call.method == method]

    @property
    def connection_count(self) -> int:
        """How many clients are attached right now."""
        return len(self._sessions)

    async def notify_all(self, method: str, params: Message) -> None:
        """Push one notification to every attached client."""
        for session in list(self._sessions):
            await session.send({"method": method, "params": params})

    async def ask_all(self, method: str, params: Message) -> Any:
        """Raise one server request on every attached client, and return its id.

        Every client is given the *same* id, which is what makes a test able to
        assert that whichever client answers first is the one that resolved it.
        """
        request_id = self._next_server_id
        self._next_server_id += 1
        for session in list(self._sessions):
            await session.send({"id": request_id, "method": method, "params": params})
        return request_id

    def answered(self, request_id: Any) -> bool:
        """Whether some client has answered that server request."""
        return request_id in self._closed_ids

    async def drop_everyone(self) -> None:
        """Cut every live connection without a close frame — the app-server dying."""
        for session in list(self._sessions):
            await session.abort()
        self._sessions.clear()

    # -- the server half --------------------------------------------------

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        session = _FakeSession(self, reader, writer)
        self._sessions.append(session)
        try:
            await session.run()
        finally:
            if session in self._sessions:
                self._sessions.remove(session)

    async def _handle(self, message: Message, session: _FakeSession) -> None:
        method = message.get("method")
        if not isinstance(method, str):
            # A response to one of our own server requests.
            self._closed_ids.add(message.get("id"))
            return
        params = message.get("params") or {}
        self.calls.append(Call(method=method, params=params))
        if message.get("id") is None:
            return

        responder = self.responders.get(method)
        try:
            result: Message = {}
            if responder is not None:
                answer = responder(params)
                result = await answer if asyncio.iscoroutine(answer) else answer  # type: ignore[assignment]
        except FakeRemoteError as refusal:
            await session.send(
                {"id": message["id"], "error": {"code": refusal.code, "message": str(refusal)}}
            )
            return
        await session.send({"id": message["id"], "result": result})


class _FakeSession:
    """One attached client: its handshake, its frames, its lifetime."""

    def __init__(
        self, server: FakeAppServer, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._server = server
        self._reader = reader
        self._writer = writer

    async def run(self) -> None:
        try:
            await self._handshake()
            while True:
                message = await self._read_message()
                await self._server._handle(message, self)
        except (asyncio.IncompleteReadError, ConnectionResetError, OSError, ValueError):
            pass
        finally:
            await self.aclose()

    async def send(self, message: Message) -> None:
        payload = json.dumps(message, separators=(",", ":")).encode("utf-8")
        with suppress(OSError):
            self._writer.write(_server_frame(payload, 0x1))
            await self._writer.drain()

    async def aclose(self) -> None:
        with suppress(Exception):
            self._writer.close()
            await self._writer.wait_closed()

    async def abort(self) -> None:
        """Vanish without a close frame — what a killed process looks like."""
        with suppress(Exception):
            self._writer.transport.abort()

    async def _handshake(self) -> None:
        header = await self._reader.readuntil(b"\r\n\r\n")
        key = ""
        for line in header.decode("iso-8859-1").split("\r\n"):
            name, separator, value = line.partition(":")
            if separator and name.strip().casefold() == "sec-websocket-key":
                key = value.strip()
        if not key:
            raise ValueError("no Sec-WebSocket-Key in the upgrade request")
        accept = base64.b64encode(
            hashlib.sha1((key + WEBSOCKET_GUID).encode("ascii")).digest()  # noqa: S324
        ).decode("ascii")
        self._writer.write(
            (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n"
                "\r\n"
            ).encode("ascii")
        )
        await self._writer.drain()

    async def _read_message(self) -> Message:
        fragments = bytearray()
        while True:
            first, second = await self._reader.readexactly(2)
            final = bool(first & 0x80)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", await self._reader.readexactly(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", await self._reader.readexactly(8))[0]
            mask = await self._reader.readexactly(4) if masked else b""
            payload = await self._reader.readexactly(length) if length else b""
            if masked:
                payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
            if opcode == 0x8:
                raise ConnectionResetError("client closed")
            if opcode == 0x9:
                self._writer.write(_server_frame(payload, 0xA))
                await self._writer.drain()
                continue
            if opcode == 0xA:
                continue
            fragments.extend(payload)
            if final:
                break
        return json.loads(bytes(fragments))


def _server_frame(payload: bytes, opcode: int) -> bytes:
    """A server frame is never masked. Otherwise identical to the client's."""
    length = len(payload)
    header = bytearray((0x80 | opcode,))
    if length <= 125:
        header.append(length)
    elif length <= 0xFFFF:
        header.append(126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(127)
        header.extend(struct.pack("!Q", length))
    return bytes(header) + payload

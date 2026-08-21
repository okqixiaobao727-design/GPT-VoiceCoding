"""The Session Channel: one MCP server, one private socket, one receipt.

Claude Code starts this process itself, from the plugin manifest `plugin.py`
renders, when the user launches a Session. It then does two things at once:

- it is an **MCP server** on stdin/stdout, declaring the `claude/channel`
  capability, pushing `notifications/claude/channel` into the session, and
  exposing the one `acknowledge_answer` tool the session calls back;
- it **listens on a private Unix socket**, which the bridge — the only client
  that ever dials it — sends one line of JSON to per Relay.

The bridge never starts or stops this process. It connects out, and the session
that answers is the exact session this server is wired into, which is what makes
the acknowledgement proof of delivery *to that session* rather than to a route.

Per connection the bridge sends `{request_id, kind, text}` and gets back
`queued_for_claude` — accepted, and proof of nothing — and then, only when the
session really calls the tool, `acknowledged_by_claude`. The queued line is
written **before** the notification is pushed, which is the one ordering this
implementation fixes that the reference implementation left to chance: a session
fast enough to acknowledge inside the push would otherwise be acknowledged
before it was queued.

A request id this server has already seen acknowledged is answered with the same
acknowledgement again and nothing else — no second notification, no second tool
call. Bridge Core retains anything not proven delivered and sends it again, so
without that memory an UNKNOWN Relay would arrive twice as two real messages.

Nothing but MCP is ever written to stdout: diagnostics go to stderr, and a
working channel says nothing at all.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any, BinaryIO

from gpt_voicecoding import __version__
from gpt_voicecoding.adapters.agent.claude.bootstrap import (
    BootstrapError,
    ChannelBootstrap,
    read_bootstrap,
)
from gpt_voicecoding.adapters.agent.claude.privacy import (
    PRIVATE_SOCKET_MODE,
    ChannelPathError,
    prepare_private_directory,
    verify_bindable_length,
)
from gpt_voicecoding.adapters.agent.claude.protocol import (
    ACKNOWLEDGE_TOOL,
    ACKNOWLEDGE_TOOL_DESCRIPTION,
    ACKNOWLEDGED,
    CHANNEL_CAPABILITY,
    CHANNEL_ERROR,
    CHANNEL_INSTRUCTIONS,
    CHANNEL_NOTIFICATION,
    INTERNAL_ERROR,
    KIND_FIELD,
    LATEST_PROTOCOL_VERSION,
    QUEUED,
    REQUEST_ID_FIELD,
    SERVER_NAME,
    SUPPORTED_PROTOCOL_VERSIONS,
    TEXT_FIELD,
)

Message = dict[str, Any]

#: The longest a request id may be. It is a UUID minted by Bridge Core; the
#: bound is here so a malformed line cannot become a map key of any size.
MAX_REQUEST_ID_LENGTH = 128


class ChannelServer:
    """One channel: the MCP side, the socket side, and what ties them together."""

    def __init__(self, bootstrap: ChannelBootstrap, *, out: BinaryIO) -> None:
        self._bootstrap = bootstrap
        self._out = out
        #: request id -> the bridge connection still waiting on it.
        self._pending: dict[str, asyncio.StreamWriter] = {}
        #: Every request id this session has acknowledged, for this process's life.
        self._acknowledged: set[str] = set()
        self._server: asyncio.AbstractServer | None = None

    # -- the MCP side -----------------------------------------------------

    def handle(self, message: Message) -> Message | None:
        """Answer one MCP message, or `None` when it is a notification.

        Unknown methods are answered the way the reference implementation's SDK
        answers a handler that raised: a JSON-RPC error carrying the reason.
        """
        method = message.get("method")
        wire_id = message.get("id")
        if wire_id is None:
            return None  # a notification: `notifications/initialized` and friends
        params = message.get("params")
        params = params if isinstance(params, dict) else {}

        match method:
            case "initialize":
                return _result(wire_id, self._initialize(params))
            case "ping":
                return _result(wire_id, {})
            case "tools/list":
                return _result(wire_id, {"tools": [_acknowledge_tool()]})
            case "tools/call":
                return self._called(wire_id, params)
            case _:
                return _error(wire_id, f"Unknown method: {method}")

    def _initialize(self, params: Message) -> Message:
        """The handshake, negotiated exactly as the pinned SDK negotiates it."""
        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else LATEST_PROTOCOL_VERSION
        return {
            "protocolVersion": version,
            "capabilities": {"experimental": {CHANNEL_CAPABILITY: {}}, "tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": __version__},
            "instructions": CHANNEL_INSTRUCTIONS,
        }

    def _called(self, wire_id: Any, params: Message) -> Message:
        """`acknowledge_answer`, which is the only reason this server has a tool."""
        if params.get("name") != ACKNOWLEDGE_TOOL:
            return _error(wire_id, f"Unknown tool: {params.get('name')}")
        arguments = params.get("arguments")
        arguments = arguments if isinstance(arguments, dict) else {}
        request_id = arguments.get(REQUEST_ID_FIELD)
        if not isinstance(request_id, str) or not request_id.strip():
            return _error(wire_id, f"{REQUEST_ID_FIELD} must be a non-empty string")

        waiting = self._pending.pop(request_id, None)
        if waiting is None:
            return _error(wire_id, f"No pending request matches {request_id}")

        self._acknowledged.add(request_id)
        _write_line(waiting, {"type": ACKNOWLEDGED, REQUEST_ID_FIELD: request_id})
        return _result(
            wire_id,
            {"content": [{"type": "text", "text": f"Acknowledged {request_id}."}]},
        )

    def notify(self, request_id: str, kind: str, text: str) -> None:
        """Push one message into the session. A notification: nothing waits on it."""
        self.send(
            {
                "jsonrpc": "2.0",
                "method": CHANNEL_NOTIFICATION,
                "params": {
                    "content": text,
                    "meta": {REQUEST_ID_FIELD: request_id, KIND_FIELD: kind},
                },
            }
        )

    def send(self, message: Message) -> None:
        self._out.write(_line(message))
        self._out.flush()

    # -- the socket side --------------------------------------------------

    async def listen(self) -> None:
        """Bind the socket, or leave: a channel that cannot be dialled is no channel.

        The directory is made private before anything is bound, and an existing
        path is refused rather than replaced — a socket already there is either
        another live channel or something planted, and neither is ours to
        remove.
        """
        path = self._bootstrap.socket_path
        verify_bindable_length(path)
        prepare_private_directory(path.parent)
        if path.exists() or path.is_symlink():
            raise ChannelPathError(f"refusing to replace an existing path: {path}")

        # Some platforms honour the umask for `AF_UNIX` bind and some do not, so
        # this narrows the window rather than replacing the explicit chmod.
        previous = os.umask(0o077)
        try:
            self._server = await asyncio.start_unix_server(self._serve, path=str(path))
        finally:
            os.umask(previous)
        os.chmod(path, PRIVATE_SOCKET_MODE)

    async def aclose(self) -> None:
        server = self._server
        self._server = None
        if server is not None:
            server.close()
            await server.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """One bridge connection, one line at a time, until it goes away."""
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                if len(line) > self._bootstrap.max_message_bytes:
                    _write_line(
                        writer,
                        {
                            "type": CHANNEL_ERROR,
                            "message": "inbound message exceeded the size limit",
                        },
                    )
                    return
                if not line.strip():
                    continue
                self._heard(line, writer)
        except (ConnectionError, asyncio.IncompleteReadError):
            return
        finally:
            for request_id, held in list(self._pending.items()):
                if held is writer:
                    del self._pending[request_id]
            writer.close()

    def _heard(self, line: bytes, writer: asyncio.StreamWriter) -> None:
        """One inbound line: refuse it in words, or carry it into the session."""
        try:
            request_id, kind, text = self._read(line)
        except ValueError as refused:
            _write_line(writer, {"type": CHANNEL_ERROR, "message": str(refused)})
            return

        if request_id in self._acknowledged:
            # A Relay this session already acted on, sent again because the
            # bridge could not prove the first one landed. Answer with the
            # proof it missed, and do nothing else.
            _write_line(writer, {"type": ACKNOWLEDGED, REQUEST_ID_FIELD: request_id})
            return
        if request_id in self._pending:
            # The same Relay again, while the first one is still waiting to be
            # acknowledged. It was already pushed, so pushing it again would put
            # the words in front of the session twice — and refusing it would be
            # a refusal the bridge is entitled to read as proof of
            # non-delivery, which it is not: the first push may well have
            # landed. So it is answered exactly as the first one was, on
            # whichever connection is asking now.
            self._pending[request_id] = writer
            _write_line(writer, {"type": QUEUED, REQUEST_ID_FIELD: request_id})
            return

        self._pending[request_id] = writer
        _write_line(writer, {"type": QUEUED, REQUEST_ID_FIELD: request_id})
        self.notify(request_id, kind, text)

    def _read(self, line: bytes) -> tuple[str, str, str]:
        """Read one inbound message, refusing every field that is not exactly right."""
        try:
            document: Any = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as unreadable:
            raise ValueError(f"not JSON: {unreadable}") from None
        if not isinstance(document, dict):
            raise ValueError("a channel message must be a JSON object")
        unknown = sorted(set(document) - {REQUEST_ID_FIELD, KIND_FIELD, TEXT_FIELD})
        if unknown:
            raise ValueError(f"unexpected field(s): {', '.join(unknown)}")

        request_id = document.get(REQUEST_ID_FIELD)
        if (
            not isinstance(request_id, str)
            or not request_id.strip()
            or len(request_id) > MAX_REQUEST_ID_LENGTH
        ):
            raise ValueError(f"{REQUEST_ID_FIELD} must be a non-empty string")
        kind = document.get(KIND_FIELD)
        if not isinstance(kind, str) or not kind.strip():
            raise ValueError(f"{KIND_FIELD} must name what this Relay is")
        text = document.get(TEXT_FIELD)
        if not isinstance(text, str) or not text:
            raise ValueError(f"{TEXT_FIELD} must be a non-empty string")
        # A UTF-8 byte budget, because that is the unit the bridge spends.
        if len(text.encode("utf-8")) > self._bootstrap.max_text_bytes:
            raise ValueError(f"{TEXT_FIELD} exceeds {self._bootstrap.max_text_bytes} bytes")
        return request_id, kind, text


def _acknowledge_tool() -> Message:
    """The tool's declaration, transcribed from the implementation Claude accepts."""
    return {
        "name": ACKNOWLEDGE_TOOL,
        "description": ACKNOWLEDGE_TOOL_DESCRIPTION,
        "inputSchema": {
            "type": "object",
            "properties": {
                REQUEST_ID_FIELD: {
                    "type": "string",
                    "description": "The exact request_id from the channel message.",
                }
            },
            "required": [REQUEST_ID_FIELD],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    }


def _result(wire_id: Any, result: Message) -> Message:
    return {"jsonrpc": "2.0", "id": wire_id, "result": result}


def _error(wire_id: Any, message: str) -> Message:
    return {"jsonrpc": "2.0", "id": wire_id, "error": {"code": INTERNAL_ERROR, "message": message}}


def _line(message: Message) -> bytes:
    """One framed message. Real UTF-8, so both ends count the same bytes."""
    return json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"


def _write_line(writer: asyncio.StreamWriter, message: Message) -> None:
    if writer.is_closing():
        return
    writer.write(_line(message))


async def serve(bootstrap: ChannelBootstrap, *, out: BinaryIO, stdin: Any) -> None:
    """Run one channel until stdin closes, which is Claude Code letting go."""
    server = ChannelServer(bootstrap, out=out)
    await server.listen()
    try:
        reader = await _reading(stdin)
        while True:
            line = await reader.readline()
            if not line:
                return
            try:
                message = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue  # not ours to answer, and not ours to crash on
            if isinstance(message, dict):
                answer = server.handle(message)
                if answer is not None:
                    server.send(answer)
    finally:
        await server.aclose()


async def _reading(stdin: Any) -> asyncio.StreamReader:
    """An asyncio reader over a blocking pipe, with nothing but the stdlib."""
    reader = asyncio.StreamReader()
    loop = asyncio.get_running_loop()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), stdin)
    return reader


def main() -> int:
    """The entry point the plugin manifest names. Never daemonises, never forks."""
    try:
        bootstrap = read_bootstrap(os.environ)
    except BootstrapError as unreadable:
        return _fatal(str(unreadable))
    try:
        asyncio.run(serve(bootstrap, out=sys.stdout.buffer, stdin=sys.stdin))
    except (ChannelPathError, OSError) as unbound:
        # A process whose entire reason to exist is this socket must not outlive
        # its failure to create one: staying up would leave Claude Code, the
        # roster and the operator all seeing a healthy channel that nothing can
        # ever be delivered through.
        return _fatal(str(unbound))
    return 0


def _fatal(reason: str) -> int:
    """Say why, on the one stream that is not the protocol, and stop.

    `os.write` rather than `print`: writes to a pipe are asynchronous, so a
    buffered diagnostic would be discarded unsent by the exit on the next line —
    losing the one message this whole mechanism exists to deliver.
    """
    os.write(2, f"the GPT-VoiceCoding Session Channel could not start: {reason}\n".encode())
    return 1


if __name__ == "__main__":  # pragma: no cover - exercised as a real subprocess
    raise SystemExit(main())

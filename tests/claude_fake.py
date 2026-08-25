"""A fake Session Channel: a real private socket, real lines, scripted answers.

The Claude adapter's whole job is classifying what a far side did, so its tests
need a far side that can be made to do the awkward things — acknowledge late,
acknowledge somebody else's request, refuse the line, or vanish mid-Relay. A
mock of the adapter's own connection could exercise none of that, because the
connection is the part being trusted.

So this is a real `asyncio` Unix server, laid down with the same privacy the
real channel gives its socket, answering exactly what a test tells it to answer.
It is deliberately not a simulator of the real server — `test_claude_channel.py`
drives that one directly.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gpt_voicecoding.adapters.agent.claude.protocol import (
    ACKNOWLEDGED,
    CHANNEL_ERROR,
    QUEUED,
    REQUEST_ID_FIELD,
)

Message = dict[str, Any]


@dataclass
class FakeChannel:
    """One scripted channel on one socket. Start it, point an adapter at it, stop it."""

    path: Path
    #: Seconds to wait before acknowledging, or `None` never to acknowledge.
    acknowledge_after: float | None = 0.0
    #: When set, the line is refused with this reason and nothing is queued.
    refuse_with: str | None = None
    #: When set, every reply names this request id instead of the one received.
    answer_about: str | None = None
    #: When true, the connection is dropped the moment a line arrives.
    close_on_relay: bool = False
    #: When true, no `queued_for_claude` precedes the acknowledgement.
    skip_queued: bool = False
    received: list[Message] = field(default_factory=list)
    #: How many bridge connections this channel currently holds open. A channel
    #: reads until end-of-file, so this only falls when the other end really closes.
    open_connections: int = 0

    def __post_init__(self) -> None:
        self._server: asyncio.AbstractServer | None = None
        self._answering: set[asyncio.Task[None]] = set()

    async def start(self) -> FakeChannel:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        self._server = await asyncio.start_unix_server(self._serve, path=str(self.path))
        # The real channel creates its socket 0600 and the adapter refuses one
        # more open than that, so a fake left at the umask could never be dialled.
        os.chmod(self.path, 0o600)
        return self

    async def __aenter__(self) -> FakeChannel:
        return await self.start()

    async def __aexit__(self, *_error: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        for task in list(self._answering):
            task.cancel()
        self._answering.clear()
        server = self._server
        self._server = None
        if server is not None:
            server.close()
            await server.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.open_connections += 1
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                message = json.loads(line)
                self.received.append(message)
                if self.close_on_relay:
                    writer.close()
                    return
                request_id = self.answer_about or message.get(REQUEST_ID_FIELD)
                if self.refuse_with is not None:
                    _write(writer, {"type": CHANNEL_ERROR, "message": self.refuse_with})
                    continue
                if not self.skip_queued:
                    _write(writer, {"type": QUEUED, REQUEST_ID_FIELD: request_id})
                if self.acknowledge_after is not None:
                    self._spawn(self._acknowledge(writer, str(request_id)))
        except (ConnectionError, asyncio.IncompleteReadError):
            return
        finally:
            self.open_connections -= 1
            # This end of the socket has to be closed even when the bridge is the
            # one that hung up, because a server transport stays attached to its
            # `asyncio.Server` until it is. `aclose` awaits `wait_closed`, which
            # does not return while any transport is still attached, so a handler
            # that returns on end-of-file without closing leaves the fake
            # unstoppable. 3.13 stopped blocking there and 3.12 did not, which is
            # the whole of why only the 3.12 lane hung.
            writer.close()

    async def _acknowledge(self, writer: asyncio.StreamWriter, request_id: str) -> None:
        assert self.acknowledge_after is not None
        await asyncio.sleep(self.acknowledge_after)
        _write(writer, {"type": ACKNOWLEDGED, REQUEST_ID_FIELD: request_id})

    def _spawn(self, work: Any) -> None:
        task = asyncio.ensure_future(work)
        self._answering.add(task)
        task.add_done_callback(self._answering.discard)


def _write(writer: asyncio.StreamWriter, message: Message) -> None:
    if writer.is_closing():
        return
    writer.write(json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode() + b"\n")

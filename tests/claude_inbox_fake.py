"""One Session's own inbox socket, scripted — the far end of `inbox.py`.

Deliberately not a simulator of Claude Code. What it reproduces is the two facts
the receipt hangs on and nothing else: the frames a sender puts on the socket,
and the `peer_message_status` a *holding* receiver dials back to the sender's
published reply address. The other source of `DELIVERED` — the target's own
transcript — is a file, so a test that wants it writes one.

It answers on the sender's reply address rather than on the connection the
message arrived on, because that is what the real receiver does and it is the
whole reason `ReplyInbox` exists.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from gpt_voicecoding.adapters.agent.claude.inbox import ADDRESS_PREFIX, STATUS_ACTION
from gpt_voicecoding.adapters.agent.claude.privacy import PRIVATE_SOCKET_MODE


class FakeInbox:
    """A Session's inbox, listening on one path, optionally answering with statuses."""

    def __init__(
        self,
        path: Path,
        *,
        statuses: Sequence[tuple[float, str]] = (),
        close_on_relay: bool = False,
    ) -> None:
        self.path = path
        #: `(delay, status)` pairs sent back for every user frame, in order. A
        #: real hold settles this way: `held` at once, then `delivered`, `denied`
        #: or `expired` when the person answers or fails to.
        self._statuses = tuple(statuses)
        self._close_on_relay = close_on_relay
        self.received: list[dict[str, Any]] = []
        self.open_connections = 0
        self._server: asyncio.Server | None = None
        self._answering: set[asyncio.Task[None]] = set()

    @property
    def relays(self) -> list[dict[str, Any]]:
        """Only the user frames — what the Session would actually be told."""
        return [frame for frame in self.received if frame.get("type") == "user"]

    async def __aenter__(self) -> FakeInbox:
        self._server = await asyncio.start_unix_server(self._serve, path=str(self.path))
        os.chmod(self.path, PRIVATE_SOCKET_MODE)
        return self

    async def __aexit__(self, *_: object) -> None:
        for task in list(self._answering):
            task.cancel()
        for task in list(self._answering):
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.open_connections += 1
        try:
            while line := await reader.readline():
                try:
                    frame = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(frame, dict):
                    continue
                self.received.append(frame)
                if frame.get("type") != "user":
                    continue
                if self._close_on_relay:
                    return
                self._answer(frame)
        finally:
            self.open_connections -= 1
            writer.close()
            with contextlib.suppress(OSError, ConnectionError):
                await writer.wait_closed()

    def _answer(self, frame: dict[str, Any]) -> None:
        address = frame.get("from")
        msg_id = frame.get("msg_id")
        if not isinstance(address, str) or not address.startswith(ADDRESS_PREFIX):
            return
        for delay, status in self._statuses:
            task = asyncio.ensure_future(
                self._status(address[len(ADDRESS_PREFIX) :], msg_id, status, delay)
            )
            self._answering.add(task)
            task.add_done_callback(self._answering.discard)

    async def _status(self, path: str, msg_id: Any, status: str, delay: float) -> None:
        await asyncio.sleep(delay)
        with contextlib.suppress(OSError, ConnectionError):
            _, writer = await asyncio.open_unix_connection(path)
            writer.write(
                (
                    json.dumps(
                        {
                            "type": "control",
                            "action": STATUS_ACTION,
                            "orig_msg_id": msg_id,
                            "status": status,
                        }
                    )
                    + "\n"
                ).encode("utf-8")
            )
            await writer.drain()
            writer.close()
            with contextlib.suppress(OSError, ConnectionError):
                await writer.wait_closed()

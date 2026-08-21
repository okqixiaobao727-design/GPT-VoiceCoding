"""The bridge's side of the channel wire: dial, one line out, lines back.

Deliberately thin. It frames, it bounds, and it says which of the two failures
happened — nothing here decides what a reply *means*, because that is the
adapter's classification and it is the part the four-state vocabulary governs.

The one thing this module does decide is where the words are: everything up to
and including the dial happens before a byte of the user's speech is on the
wire, so a failure there is proof the Session never saw it. `send` is the line
that changes that, and after it no failure proves anything.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from gpt_voicecoding.adapters.agent.claude.privacy import (
    ChannelPathError,
    verify_bindable_length,
    verify_private_socket,
)

Message = dict[str, Any]


class ChannelError(Exception):
    """The channel could not be reached, or did not speak its own protocol."""


class ChannelClosed(ChannelError):
    """The channel went away. Its process is not this engine's to restart."""


class ChannelConnection:
    """One newline-delimited JSON connection to one Session's channel socket."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        max_message_bytes: int,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._max_message_bytes = max_message_bytes

    @classmethod
    async def dial(
        cls, path: Path, *, timeout_seconds: float, max_message_bytes: int
    ) -> ChannelConnection:
        """Prove the socket is private and this user's, then connect to it.

        The check is not a substitute for the dial and the dial is not a
        substitute for the check: a path that stats correctly may have no
        listener, and a listener may be somebody else's.
        """
        try:
            verify_bindable_length(path)
            verify_private_socket(path)
        except ChannelPathError as refused:
            raise ChannelError(str(refused)) from None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(path)), timeout=timeout_seconds
            )
        except (TimeoutError, OSError) as unreachable:
            raise ChannelError(f"could not connect to the channel socket: {unreachable}") from None
        return cls(reader, writer, max_message_bytes=max_message_bytes)

    async def send(self, message: Message) -> None:
        """Put one line on the wire. After this, nothing proves non-delivery."""
        # `ensure_ascii=False` keeps the wire in real UTF-8: escaping a Chinese
        # character to `\uXXXX` would make it six bytes where both ends' budgets
        # count three, so the limits would measure something never transmitted.
        payload = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(payload) + 1 > self._max_message_bytes:
            raise ChannelError("the outbound channel message is larger than both ends allow")
        try:
            self._writer.write(payload + b"\n")
            await self._writer.drain()
        except (OSError, ConnectionError) as broken:
            raise ChannelError(f"the channel write failed: {broken}") from None

    async def read_message(self, *, timeout_seconds: float) -> Message:
        """One reply, or a reason there was none within the budget."""
        try:
            line = await asyncio.wait_for(
                self._reader.readline(), timeout=max(timeout_seconds, 0.0)
            )
        except TimeoutError:
            raise TimeoutError("the channel did not answer within the budget") from None
        except (OSError, ConnectionError) as broken:
            raise ChannelClosed(f"the channel read failed: {broken}") from None
        if not line:
            raise ChannelClosed("the channel closed the connection")
        if len(line) > self._max_message_bytes:
            raise ChannelError("a channel reply exceeded the size limit")
        try:
            message: Any = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as unreadable:
            raise ChannelError(f"the channel sent invalid JSON: {unreadable}") from None
        if not isinstance(message, dict):
            raise ChannelError("a channel reply must be a JSON object")
        return message

    async def aclose(self) -> None:
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except (OSError, ConnectionError):
            pass

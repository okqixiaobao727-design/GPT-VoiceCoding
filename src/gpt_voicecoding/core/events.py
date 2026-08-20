"""Bridge Core's inbound event queue — the hub half of the event mechanism.

Adapters are handed the `emit` end and call it whenever something happens; every
event lands on this one queue, and one dispatch loop drains it. That is what
makes ordering, serialisation and Reply-Window queueing naturally the hub's
business rather than each adapter's — adapters deliver, they never queue.

`emit` is synchronous, unbounded and never raises, so an adapter can call it from
a socket callback, a reader task or a hook process's handler without knowing
whether the loop is running yet. Events emitted before the loop starts are simply
waiting when it does.

The loop that *interprets* events is policy and belongs to the pipelines. This
file only guarantees that every event arrives, once, in order.
"""

from __future__ import annotations

import asyncio

from gpt_voicecoding.seams.events import Event


class EventQueue:
    """One queue, one drain. Implements the `EventSink` adapters are handed."""

    def __init__(self) -> None:
        self._events: asyncio.Queue[Event] = asyncio.Queue()

    def __len__(self) -> int:
        return self._events.qsize()

    def emit(self, event: Event) -> None:
        """Take one event from an adapter. Never blocks, never raises."""
        self._events.put_nowait(event)

    async def next_event(self) -> Event:
        """Wait for the next event. The dispatch loop's one call."""
        return await self._events.get()

    def drain(self) -> tuple[Event, ...]:
        """Take everything waiting, in arrival order, without waiting for more."""
        drained: list[Event] = []
        while not self._events.empty():
            drained.append(self._events.get_nowait())
        return tuple(drained)

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
from collections import Counter

from gpt_voicecoding.seams.events import Event


class EventQueue:
    """One queue, one drain. Implements the `EventSink` adapters are handed."""

    def __init__(self) -> None:
        self._events: asyncio.Queue[Event] = asyncio.Queue()
        self._unread: Counter[type[Event]] = Counter()

    def __len__(self) -> int:
        return self._events.qsize()

    def emit(self, event: Event) -> None:
        """Take one event from an adapter. Never blocks, never raises."""
        self._unread[type(event)] += 1
        self._events.put_nowait(event)

    def unread(self, *kinds: type[Event]) -> bool:
        """Whether news of these kinds is waiting that the dispatch loop has not taken.

        Asked by anything that *measures* state the dispatch loop writes. The
        two run as separate tasks (`engine/composition.py`: `_dispatching` and
        `_ticking`), so a `VoiceSpeech` already emitted but not yet taken has
        not reached the interlock, and a ceiling measured then is measured one
        event out of date — which is a call ended while its own Voice was
        speaking (#184).

        **The caller names the kinds, and gets no answer about anything else.**
        "Is anything waiting" was the first shape of this and it was too wide:
        it let a queued `SessionStopped` — news about a Session, which no
        ceiling reads — hold a silent call open, and a lane that kept producing
        events hold one open indefinitely. A decision defers on the news it
        cannot be taken without, and on nothing else.

        True only until the event is *taken*: the dispatch loop writes what an
        event means with no await between taking it and recording it, so there
        is no third state in which nothing is waiting and the news is still
        unread.
        """
        return any(self._unread[kind] for kind in kinds)

    async def next_event(self) -> Event:
        """Wait for the next event. The dispatch loop's one call."""
        return self._taken(await self._events.get())

    def drain(self) -> tuple[Event, ...]:
        """Take everything waiting, in arrival order, without waiting for more."""
        drained: list[Event] = []
        while not self._events.empty():
            drained.append(self._taken(self._events.get_nowait()))
        return tuple(drained)

    def _taken(self, event: Event) -> Event:
        """One event has left the queue; it is the caller's to interpret now."""
        self._unread[type(event)] -= 1
        return event

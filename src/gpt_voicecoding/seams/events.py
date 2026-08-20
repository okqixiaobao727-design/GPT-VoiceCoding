"""How an adapter raises something upward, and how Bridge Core hears it.

One mechanism, deliberately the thinnest one that works: Bridge Core hands each
adapter an `EventSink` at construction, and the adapter calls `emit` when
something happens. `emit` never blocks and never awaits, so an adapter can call
it from a socket callback, a reader task or a synchronous hook without caring
where the hub is.

Everything else is the hub's. Bridge Core's sink puts the event on one internal
queue and one dispatch loop drains it, which is what makes event ordering,
serialisation and Reply-Window queueing naturally Core's — adapters deliver, they
never queue.

There is no subscribe/topic event bus. Nothing varies here, so nothing is
abstracted (ADR 0001, principle 2).

Every event is a frozen dataclass with named fields. No event carries a bare
dict: a payload nobody can enumerate is how the classification rules this
repository depends on get lost.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class Event:
    """Base for everything a seam raises upward. Carries nothing on its own."""


@runtime_checkable
class EventSink(Protocol):
    """What an adapter is handed so it can speak upward. Non-blocking."""

    def emit(self, event: Event) -> None:
        """Hand one event to Bridge Core. Must not block, must not raise."""
        ...

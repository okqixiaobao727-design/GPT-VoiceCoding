"""Adapters that have something to open, and something to close.

Every seam verb in this package is about *doing* the thing the seam names. This
is the one thing that is not: a socket to dial, a reader task to start, a
process to reap. Bridge Core never calls any of it — the composition root does,
once at start and once at shutdown — but it is declared here, with the seams,
because it is a promise both sides need to agree on and no single seam owns.

**It is optional, and deliberately so.** An adapter that holds nothing of its
own — the null Companion Channel is the plain case — has nothing to open, and
forcing it to implement two empty methods would be a contract that lies about
what varies (ADR 0001, principle 2). The composition root asks whether an
adapter is `Connectable` and leaves the rest alone.

Both verbs are idempotent and neither may raise on a second call: a shutdown
that is already under way must not be made worse by an adapter objecting to
being closed twice.

**Closed at two verbs.** Health checks, restarts and reconnection policy are not
a third method here — that is how an optional capability becomes a lifecycle
framework, and each of them is a decision about *policy*, which belongs to
Bridge Core or to the menu-bar shell's process parenthood (ADR 0005), not to a
contract about opening a connection.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Connectable(Protocol):
    """An adapter with a connection, a task or a child of its own to manage."""

    async def connect(self) -> None:
        """Open whatever this adapter needs before it can be used. Idempotent."""
        ...

    async def aclose(self) -> None:
        """Release it. Idempotent, and never raises on a second call."""
        ...

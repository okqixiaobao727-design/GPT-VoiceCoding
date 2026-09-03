"""Where an Answer Relay stands, across every attempt it will ever get.

- **RETAINED** — attempted or attempt-less, not delivered, and **still
  retryable**. A Relay waits here for the Session's Reply Window.
- **DELIVERED** — positively proven delivered. Terminal. Leaves the queue, so
  it cannot be re-attempted by anything.
- **REPORTED_FAILED** — reported to the user as terminal. **No automatic retry
  and no substitute action follows.** Also leaves the queue, so the retry
  boundary is structural rather than remembered.

**Three names, and it used to be five.** `PENDING` and `DROPPED` described a
notice waiting in a pipeline and a notice that pipeline gave up on, and no
notice waits anywhere since #195: a Stop reaches the Companion Channel in one
push that is graded and forgotten, and the voice side is the Call Keeper's,
which paces rather than queues. `is_terminal` and `is_retryable` went with them
— the retry boundary is which states leave the queue, and the queue is the one
thing that reads it (`core/relay_queue.py`).

This is deliberately *not* `seams.delivery.Delivery`, and the two must never be
compared. `Delivery` grades **one attempt** — what a single adapter call proved.
`Lifecycle` grades **the thing itself**. The reference implementation's worst
delivery bug was exactly this conflation: it read one attempt's grade as the
item's fate, retried an already-spoken notice, and opened duplicate calls.
"""

from __future__ import annotations

from enum import StrEnum


class Lifecycle(StrEnum):
    """Where a Relay stands. Two of the three are terminal."""

    RETAINED = "retained"
    DELIVERED = "delivered"
    REPORTED_FAILED = "reported_failed"

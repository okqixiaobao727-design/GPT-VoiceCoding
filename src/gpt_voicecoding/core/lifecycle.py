"""The five state names every pending thing in Bridge Core passes through.

Defined here because the policy pipelines issue was asked to define them and
share them: the Stop Notice escalation pipeline, the Relay queue's ceiling, and
the generated-instructions work all describe the same shape, and describing it
three times is how "retained and retried" and "no automatic retry after a
reported failure" drifted into looking like a contradiction.

- **PENDING** — accepted, not yet attempted.
- **RETAINED** — attempted or attempt-less, not delivered, and **still
  retryable**. Answer Relays wait here for the Session's Reply Window.
- **DELIVERED** — positively proven delivered. Terminal. Leaves the queue, so
  it cannot be re-attempted by anything.
- **DROPPED** — not delivered and nothing automatic follows for this item.
  Stop-Notice no-loss lives in Bridge Core's current-state reconciliation
  (#80), which may create a new notice; this item is never replayed.
- **REPORTED_FAILED** — reported to the user as terminal. **No automatic retry
  and no substitute action follows.** Also leaves the queue, so the retry
  boundary is structural rather than remembered.

This is deliberately *not* `seams.delivery.Delivery`, and the two must never be
compared. `Delivery` grades **one attempt** — what a single adapter call proved.
`Lifecycle` grades **the thing itself** — where it stands across every attempt
it will ever get. The reference implementation's worst delivery bug was exactly
this conflation: it read one attempt's grade as the item's fate, retried an
already-spoken notice, and opened duplicate calls.
"""

from __future__ import annotations

from enum import StrEnum


class Lifecycle(StrEnum):
    """Where a notice or a Relay stands. Three of the five are terminal."""

    PENDING = "pending"
    RETAINED = "retained"
    DELIVERED = "delivered"
    DROPPED = "dropped"
    REPORTED_FAILED = "reported_failed"

    @property
    def is_terminal(self) -> bool:
        """Whether anything further may happen to this automatically."""
        return self in (Lifecycle.DELIVERED, Lifecycle.DROPPED, Lifecycle.REPORTED_FAILED)

    @property
    def is_retryable(self) -> bool:
        """The retry boundary, in one place. Only RETAINED answers yes."""
        return self is Lifecycle.RETAINED

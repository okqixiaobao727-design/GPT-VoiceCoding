"""How every seam says whether words arrived.

The vocabulary is four states, fixed by the peer-socket research and the Relay
grilling, and it is Bridge Core's: adapters classify an attempt into it, and no
adapter may extend or reinterpret it.

- **DELIVERED** — positively proven to have reached the model. Nothing else is.
- **FAILED** — a positive reason to believe it did not arrive. Surface the reason.
- **HELD** — parked in front of the human, possibly forever. Never delivered.
- **UNKNOWN** — everything past the dial that produced no proof either way,
  including a readback that *contradicts* the attempt.

The receipt carries no timestamp on purpose. Bridge Core stamps time when it
records an attempt, so an adapter's clock can never disagree with the hub's.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from gpt_voicecoding.seams.identity import RequestId


class Delivery(StrEnum):
    """The four-state delivery classification. Closed: adapters classify into it."""

    DELIVERED = "delivered"
    FAILED = "failed"
    HELD = "held"
    UNKNOWN = "unknown"

    @property
    def is_delivered(self) -> bool:
        """The one question worth asking. Only DELIVERED answers yes."""
        return self is Delivery.DELIVERED


@dataclass(frozen=True, slots=True)
class DeliveryReceipt:
    """One attempt's classification, and the evidence behind it."""

    request_id: RequestId
    outcome: Delivery
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.outcome.is_delivered and not self.reason.strip():
            raise ValueError(f"a {self.outcome} receipt must carry a positive reason")

    @property
    def is_delivered(self) -> bool:
        return self.outcome.is_delivered

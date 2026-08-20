"""The Companion Channel seam — reaching the user when no Live Call is up.

Verbs Bridge Core calls: `send(message)` and `verify` (liveness — ADR 0003).

Events raised upward: inbound user text, **unclassified**. Deciding whether
inbound text is a control-plane command, an Answer Relay, or a delegation is
Bridge Core's job and never the channel's — which is why the event is named for
what it is (text that arrived) and not for what it might mean, and why it has no
field an adapter could use to volunteer an opinion.

The null implementation is a real implementation. It reports the empty module
string from `verify` and returns a positive non-delivery from `send` — never
something Bridge Core could mistake for delivery.

Adapters: Telegram is the generic public one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from gpt_voicecoding.seams.delivery import DeliveryReceipt
from gpt_voicecoding.seams.events import Event
from gpt_voicecoding.seams.identity import RequestId
from gpt_voicecoding.seams.verify import VerifyResult


@dataclass(frozen=True, slots=True)
class InboundText(Event):
    """Text the user sent. What it *means* is Bridge Core's to decide.

    `origin` is the adapter's own opaque reference to where it came from — a
    chat id, a socket path. Bridge Core does not parse it; it exists so a reply
    can go back the way the text came.
    """

    text: str
    origin: str = ""


#: The closed set of events this seam raises. Nothing else may appear.
CompanionChannelEvent = InboundText


@runtime_checkable
class CompanionChannel(Protocol):
    """Text reach when there is no call. Mechanism only — routing policy is Core's."""

    async def send(self, text: str, *, request_id: RequestId) -> DeliveryReceipt:
        """Push one message to the user."""
        ...

    async def verify(self) -> VerifyResult:
        """Report which implementation this is and whether its far side answers."""
        ...

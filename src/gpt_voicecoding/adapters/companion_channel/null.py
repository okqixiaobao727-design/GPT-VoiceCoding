"""The Companion Channel nobody configured — a real implementation, not a stub.

Running with no text reach at all is a supported state: the Message Switch is
independent of the Voice Switch, and a deployment that only ever wants to be
spoken to is not a broken one. What is *not* supported is an engine that looks
like it can reach the user and cannot, which is the outage ADR 0003 exists to
expose. So the absence is filled by something that answers both seam verbs
honestly rather than by nothing at all — configuration names this adapter, and
the engine can then say what it loaded.

Two answers, and neither of them lies:

- `send` returns **FAILED**. `Delivery` is a closed four-state vocabulary and no
  adapter may extend it, so the "not configured" class of outcome is expressed
  as the state whose definition already covers it — *a positive reason to
  believe it did not arrive*. `HELD` was rejected: it claims the words are
  parked in front of a human, and here there is no human and no queue. Bridge
  Core can never read this as delivered, which is the whole point: a Stop Notice
  that fell into an unconfigured channel must not be recorded as having reached
  anyone.
- `verify` returns **MANUAL** with an empty `loaded`. Empty is the module string
  ADR 0003 reserves for exactly this, and `MANUAL` is the outcome for a question
  the check cannot answer: nothing is configured, so there is no far side to
  fail against. The detail names the adapter an operator would configure
  instead, because a status line that says "manual" and stops is a dead end.
"""

from __future__ import annotations

from gpt_voicecoding.seams.delivery import Delivery, DeliveryReceipt
from gpt_voicecoding.seams.events import EventSink
from gpt_voicecoding.seams.identity import RequestId
from gpt_voicecoding.seams.verify import VerifyOutcome, VerifyResult

#: What a push into an unconfigured channel is told. One sentence, because it is
#: read by a person looking at a delivery record and wondering where their words
#: went.
NOT_CONFIGURED = (
    "no companion channel is configured behind this seam, so nothing was sent anywhere"
)

#: The generic public adapter an operator would name instead. Stated here so the
#: null implementation's own answer carries the way out of it.
TELEGRAM_REFERENCE = "gpt_voicecoding.adapters.companion_channel.telegram:telegram_channel"


class NullCompanionChannel:
    """Text reach that was deliberately not configured. Implements `CompanionChannel`."""

    def __init__(self, *, sink: EventSink | None = None) -> None:
        # Held and unused on purpose: this channel has no far side, so there is
        # never an inbound event to raise. Taking the sink keeps it constructible
        # exactly like every other adapter, so the composition root needs no
        # special case for the seam that is empty.
        self._sink = sink

    async def send(self, text: str, *, request_id: RequestId) -> DeliveryReceipt:
        """Report the truth: there was nowhere to send it."""
        return DeliveryReceipt(
            request_id=request_id, outcome=Delivery.FAILED, reason=NOT_CONFIGURED
        )

    async def verify(self) -> VerifyResult:
        """Report the null implementation as itself — the empty module string."""
        return VerifyResult(
            outcome=VerifyOutcome.MANUAL,
            loaded="",
            detail=(
                "this engine deliberately runs without text reach; configure "
                f"{TELEGRAM_REFERENCE} to give it one"
            ),
        )

"""The undelivered Answer Relay queue.

Stop Notices do not enter this queue. Their no-loss behaviour lives in Bridge
Core's current-state reconciliation, which inspects live main Sessions instead
of replaying historical notice objects.

Every entry carries its classification from the shared lifecycle vocabulary, and a
DELIVERED entry *leaves the queue*. That is deliberate: the reference
implementation graded an audibly spoken notice FAILED by matching another
surface's records, retried it, and opened duplicate calls. Here, proving delivery
and remaining retryable are mutually exclusive states of the same object.

Nothing here is durable. The durable subset is switch state and the Session
registry (ADR 0001); a queue that survived a restart would be re-delivering words
whose moment has passed.

Policy lives elsewhere. The queue holds the deadline it is handed — the
ten-minute ceiling is the pipelines issue's number, not a constant here — reports
what has passed it, and never decides what to do about it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

from gpt_voicecoding.core.errors import DuplicateRelayError, UnknownRelayError
from gpt_voicecoding.seams.agent import RelayRoute
from gpt_voicecoding.seams.delivery import DeliveryReceipt
from gpt_voicecoding.seams.identity import RequestId, SessionTarget


class RelayKind(StrEnum):
    """Which Agent-seam verb will carry this entry when it goes.

    Bookkeeping for the queue, never an envelope field: the adapter is told what
    to do by *which verb Bridge Core calls*, which is the repair for the
    reference implementation's universal `kind: "user_answer"` envelope.

    There is no approval member. An Approval Relay has a budget and a fallback —
    it is answered or it hands back to the on-screen dialog. It never waits here.
    """

    #: The user's own words, carrying the user's authority.
    ANSWER = "answer"


@dataclass(frozen=True, slots=True)
class PendingRelay:
    """One Relay that has not been proven delivered."""

    request_id: RequestId
    target: SessionTarget
    kind: RelayKind
    text: str
    queued_at: float
    expires_at: float
    route: RelayRoute = RelayRoute.DELIVER
    #: The last attempt, whole — its grade and the evidence behind it — or
    #: `None` for an entry nothing has been attempted for yet.
    #:
    #: **`None` rather than `UNKNOWN`, and the difference is P9's whole rule.**
    #: `UNKNOWN` is a positive observation — something went on the wire and
    #: produced no proof either way, so re-sending it risks a duplicate. Words
    #: that were queued against a closed Reply Window went nowhere and carry no
    #: such risk. Spelling both `UNKNOWN` made one value mean two things and
    #: made the settlement policy unable to tell "may already have arrived" from
    #: "never left this process".
    #:
    #: **The whole receipt rather than the grade alone**, because an entry can
    #: end minutes after its attempt — at the ceiling, or when the Session goes
    #: — and what the surface owes the user then is that attempt's grade with
    #: the evidence the seam requires beside it (`seams/delivery.py`). A queue
    #: that kept only the grade left the pipeline writing a sentence about the
    #: attempt instead of carrying it.
    receipt: DeliveryReceipt | None = None

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("a Relay carries words; there are none here")
        if self.expires_at <= self.queued_at:
            raise ValueError("a Relay's deadline must be after the moment it was queued")


class RelayQueue:
    """What is still pending. Holds entries; decides nothing about them."""

    def __init__(self) -> None:
        self._pending: dict[RequestId, PendingRelay] = {}

    def __len__(self) -> int:
        return len(self._pending)

    def enqueue(self, pending: PendingRelay) -> PendingRelay:
        """Hold a Relay until something proves it delivered or releases it."""
        if pending.receipt is not None and pending.receipt.is_delivered:
            raise ValueError(
                "this queue holds undelivered Relays; something already delivered cannot wait in it"
            )
        if pending.request_id in self._pending:
            raise DuplicateRelayError(pending.request_id)
        self._pending[pending.request_id] = pending
        return pending

    def pending(self) -> tuple[PendingRelay, ...]:
        """Everything still waiting, in the order it arrived."""
        return tuple(self._pending.values())

    def pending_for(self, target: SessionTarget) -> tuple[PendingRelay, ...]:
        """Everything waiting for one exact Session. A fork is a different Session."""
        return tuple(waiting for waiting in self._pending.values() if waiting.target == target)

    def classify(self, request_id: RequestId, receipt: DeliveryReceipt) -> PendingRelay:
        """Record what an attempt proved. Proving delivery takes the entry out."""
        waiting = self._waiting(request_id)
        classified = replace(waiting, receipt=receipt)
        if receipt.is_delivered:
            del self._pending[request_id]
        else:
            self._pending[request_id] = classified
        return classified

    def release(self, request_id: RequestId) -> PendingRelay:
        """Take an entry out without claiming it was delivered."""
        waiting = self._waiting(request_id)
        del self._pending[request_id]
        return waiting

    def drop_for(self, target: SessionTarget) -> tuple[PendingRelay, ...]:
        """Take out everything waiting for a Session that is gone."""
        dropped = self.pending_for(target)
        for waiting in dropped:
            del self._pending[waiting.request_id]
        return dropped

    def expired(self, *, now: float) -> tuple[PendingRelay, ...]:
        """Everything past the deadline it was given. Reports; does not remove."""
        return tuple(waiting for waiting in self._pending.values() if waiting.expires_at <= now)

    def _waiting(self, request_id: RequestId) -> PendingRelay:
        """The entry still queued under that id. Named for waiting, not for HELD."""
        waiting = self._pending.get(request_id)
        if waiting is None:
            raise UnknownRelayError(request_id)
        return waiting

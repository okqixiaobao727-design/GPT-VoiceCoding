"""The Agent seam — carrying words into a Session, and hearing back from it.

Verbs Bridge Core calls: `answer_relay`, `notice_relay`, `approval_relay`, and
`verify` (ADR 0003 — liveness is a verb on every pluggable seam).

Events raised upward: Session stopped, Session ended, Session awaiting approval,
Reply Window changed, and delivery receipts that arrive asynchronously.

Reply-Window queueing is Bridge Core policy. Adapters deliver; they never queue.

**Deliver and supplement are one verb with a route, not two verbs.** The Relay
grilling fixed two required behaviours — deliver (between turns) and supplement
(mid-turn, with the user's authority intact) — and both are required of both
agents. They are a parameter of `answer_relay` alone, because supplement only
ever carries *user-authored* words: a Notice Relay is system-authored by
construction and an Approval Relay is a verdict. A second verb would duplicate
one signature to encode one boolean.

Which routes an adapter really has is reported by `supported_routes`, statically.
An adapter that lacks SUPPLEMENT says so and does nothing else — deciding what to
do instead (queue it as a DELIVER against the Reply Window) is Bridge Core's
policy. Route choice follows the user's explicit intent and is never inferred
from Session status: the same "busy" carries both "add this now" and "this can
wait".

Adapters: Codex and Claude.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from gpt_voicecoding.seams.delivery import DeliveryReceipt
from gpt_voicecoding.seams.events import Event
from gpt_voicecoding.seams.identity import RequestId, SessionTarget
from gpt_voicecoding.seams.verify import VerifyResult


class RelayRoute(StrEnum):
    """How user-authored words reach a Session. Chosen by the user, not inferred."""

    #: Between turns, into an open Reply Window. Always available.
    DELIVER = "deliver"
    #: Mid-turn, authority intact — "the agent is working and I want to add
    #: something". Optional: an adapter may honestly not have it.
    SUPPLEMENT = "supplement"


class ReplyWindow(StrEnum):
    """Whether a Session can accept an inbound Relay as a user turn."""

    OPEN = "open"
    CLOSED = "closed"


class ApprovalVerdict(StrEnum):
    """The user's decision on one pending permission request."""

    ALLOW = "allow"
    DENY = "deny"
    #: Hand it back to the on-screen dialog. This is what a budget expiry
    #: answers — never deny on timeout.
    ASK = "ask"


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """A Session's pending permission request, as the adapter observed it.

    `approval_id` is the adapter's own opaque handle for the pending dialog. It
    is deliberately not a `RequestId`: this request was raised by the Session,
    while a `RequestId` is minted by Bridge Core for an attempt it sends.
    """

    approval_id: str
    target: SessionTarget
    tool_name: str
    detail: str = ""
    #: The decisions the far side offers, when it offers a list — the ready-made
    #: voice menu. Empty when the route offers only allow/deny.
    options: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.approval_id.strip():
            raise ValueError("an approval request must carry the adapter's handle for it")


@dataclass(frozen=True, slots=True)
class SessionStopped(Event):
    """A Session stopped and may need the user. Feeds the Stop Notice pipeline."""

    target: SessionTarget
    detail: str = ""


@dataclass(frozen=True, slots=True)
class SessionEnded(Event):
    """A Session is gone. The registry may no longer be Relayed into."""

    target: SessionTarget
    detail: str = ""


@dataclass(frozen=True, slots=True)
class AwaitingApproval(Event):
    """A permission dialog is on screen. It blocks every other Relay until answered."""

    request: ApprovalRequest


@dataclass(frozen=True, slots=True)
class ReplyWindowChanged(Event):
    """The Session's willingness to accept an inbound Relay as a user turn changed."""

    target: SessionTarget
    window: ReplyWindow


@dataclass(frozen=True, slots=True)
class RelayReceipt(Event):
    """A receipt that arrived after the call returned — a held or expired Relay."""

    target: SessionTarget
    receipt: DeliveryReceipt


#: The closed set of events this seam raises. Nothing else may appear.
AgentEvent = SessionStopped | SessionEnded | AwaitingApproval | ReplyWindowChanged | RelayReceipt


@runtime_checkable
class AgentAdapter(Protocol):
    """What Codex and Claude each implement. Mechanism only; no policy, no queueing."""

    def supported_routes(self) -> frozenset[RelayRoute]:
        """Which routes this adapter really has. Static, and honest about gaps."""
        ...

    async def answer_relay(
        self,
        target: SessionTarget,
        text: str,
        *,
        request_id: RequestId,
        route: RelayRoute = RelayRoute.DELIVER,
    ) -> DeliveryReceipt:
        """Carry the user's own words in, with the user's authority."""
        ...

    async def notice_relay(
        self, target: SessionTarget, text: str, *, request_id: RequestId
    ) -> DeliveryReceipt:
        """Carry words the system itself originates. Claims no user authority."""
        ...

    async def approval_relay(
        self, request: ApprovalRequest, verdict: ApprovalVerdict, *, request_id: RequestId
    ) -> DeliveryReceipt:
        """Carry the user's verdict on one pending permission request."""
        ...

    async def verify(self) -> VerifyResult:
        """Report which implementation this is and whether its far side answers."""
        ...

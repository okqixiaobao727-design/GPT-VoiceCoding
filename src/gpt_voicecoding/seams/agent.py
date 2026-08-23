"""The Agent seam — carrying words into a Session, and hearing back from it.

Verbs Bridge Core calls: `answer_relay`, `notice_relay`, `approval_relay`,
`reply_window` and `verify` (ADR 0003 — liveness is a verb on every pluggable
seam).

**The Reply Window is a level, so it is both asked for and reported.** `reply_
window` answers where it stands right now and is asked exactly once, when Bridge
Core enters a Session in its roster; `ReplyWindowChanged` reports every
transition after that. The split is not redundancy — an event cannot bootstrap a
level, because registration happens before Bridge Core holds the Session and a
report raised there is dropped as belonging to a Session nobody knows (#27).

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

    def reply_window(self, target: SessionTarget) -> ReplyWindow:
        """Where one Session's Reply Window stands right now, asked rather than awaited.

        The level, pulled; `ReplyWindowChanged` remains the transition, pushed.
        Bridge Core calls this once, the instant it enters a Session in its
        roster, so the Session starts from an observed level instead of from the
        fail-closed default — and calls nothing here again.

        **A pull exists because the push cannot bootstrap a level (#27).** An
        adapter is registered before Bridge Core holds the Session, so a report
        emitted at registration is dropped as belonging to a Session nobody knows
        — and it is a report the adapter has already recorded as sent, so no
        later transition repeats it. A Session that was already idle when it was
        registered therefore stayed at CLOSED forever, unreachable while
        perfectly healthy. Asking closes that hole by construction rather than by
        timing: the roster provably holds the Session one line before the
        question is asked.

        **Deliberately synchronous**, alone among this seam's verbs except
        `supported_routes`. An await here would reintroduce the very gap the pull
        exists to close, by letting the dispatch loop run between the roster
        write and the answer being applied. Both real adapters can answer without
        one — Claude from the registry record it already reads, Codex from the
        status it has already observed — so the seam asks for no more than they
        need.

        **Fail closed, and never fail the caller.** An adapter that does not hold
        this target answers CLOSED, because "I cannot reach this Session" is not
        an observation that its window is open. Bridge Core treats a raise the
        same way and completes the launch regardless: a Session that is listed
        but conservatively closed is recoverable on the next transition, while a
        launch failed over a level query is not.

        Extending this seam's verb set was adjudicated for this use case.
        """
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

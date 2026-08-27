"""The Approval Relay budget, its fallback, and the notice that closes the loop.

A pending permission dialog is one more attention-needing stall, so its delivery
**rides the Stop Notice escalation pipeline** rather than getting a flow of its
own — same route matrix and same switches. The one thing it asks
for that a Stop Notice does not is `Reach.EVERY_OUTLET`: the push fires
immediately, in parallel with the voice attempt, because the user may be nowhere
near the screen and waiting to see whether the call worked wastes the budget.

**The budget never denies.** Running out means the user was not reachable, not
that they said no, so expiry answers `ask` and the on-screen dialog takes over.
The number is `CorePolicy`'s and configurable; the never-deny rule is not.

**A closing notice fires on every resolution.** That is what absorbs the
duplicate the parallel push created — and the expiry case is the one that
matters most, because a never-deny fallback otherwise leaves the pushed user
believing a decision is still wanted from them. A verdict that arrives after
the loop was already closed is discarded and emits nothing: its closing notice
already went out.

**And it may only claim what the receipt proves** (P14, #61 R5). "Approved by
voice" is a statement about the *Session*, so only a `DELIVERED` receipt earns
it; every other grade says the verdict was not confirmed and points the user
back at the on-screen dialog, which is still the thing that can resolve it. The
reference implementation was honest here for free — it had no approval transport
at all, so it never claimed a verdict had arrived — and carrying verdicts is
what makes the claim possible and therefore the restraint necessary.

Pending approvals are held here rather than in the undelivered Relay queue, on
that queue's own instruction — an Approval Relay has a budget and a fallback, so
it is answered or handed back; it never waits in the ledger.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass

from gpt_voicecoding.core.clock import Clock, default_clock
from gpt_voicecoding.core.escalation import EscalationPipeline, Notice, NoticeOutcome, Reach
from gpt_voicecoding.core.lifecycle import Lifecycle
from gpt_voicecoding.core.policy import CorePolicy
from gpt_voicecoding.core.sessions import spoken_target
from gpt_voicecoding.seams.agent import AgentAdapter, ApprovalRequest, ApprovalVerdict
from gpt_voicecoding.seams.delivery import Delivery
from gpt_voicecoding.seams.identity import AgentKind, RequestId, new_request_id

_log = logging.getLogger(__name__)


def announcement_for(request: ApprovalRequest, spoken_as: str) -> str:
    """What the user is told is waiting. Names the Session and the tool, never the answer.

    **`spoken_as` is the Session, said the way the user names it**, and it is a
    required argument rather than an optional flourish. This sentence used to
    open "a session is waiting…", which is the one thing the user cannot act on:
    the bridge covers every Session on the machine, so on any real machine
    several of them can be waiting at once, and this is the notice that carries a
    budget and a `bridgectl approve` — the *most* answerable thing the product
    says, and the only one that did not say which Session it was about (#109,
    found on a run where a stranger's permission prompt was indistinguishable
    from the lane's own).

    Bridge Core composes `spoken_as` at the call site from the same two lines
    `stop_notice_for` uses — `spoken_name` where the Session is known, its
    address as the floor — because "what to call it" has one answer
    (`core/sessions.py`) and this is not a second one.

    Legacy: **ported**. `legacy@1d32845:bridge/host.py:213-235` rendered
    `Session: {session_label}` above "This session is waiting for permission.";
    gen-1 named the Session on its permission notice and the rewrite dropped it,
    which is the class of loss ADR 0010 exists for.
    """
    detail = f" — {request.detail}" if request.detail.strip() else ""
    return f"{spoken_as} is waiting for your permission to use {request.tool_name}{detail}"


#: What closes the loop when the adapter **proved** the verdict arrived, per
#: resolution. Kept together so no path can resolve a request without a wording
#: for it.
#: The `ASK` wording is conditional on purpose, and it was not always. It read
#: "it's waiting at the on-screen dialog", which is false on the path the Claude
#: hook route made visible: a human who answers the dialog themselves ends the
#: request, and the budget can then run out up to ten minutes later on something
#: nobody is looking at any more. Surfaces render these notices verbatim, so the
#: sentence has to be true in every path that can fire it.
CLOSING_NOTICES: dict[ApprovalVerdict, str] = {
    ApprovalVerdict.ALLOW: "approved by voice",
    ApprovalVerdict.DENY: "denied by voice",
    ApprovalVerdict.ASK: (
        "the voice window closed — if the dialog is still on screen, answer it there"
    ),
}

#: What closes the loop when it did not (P14, #61 R5). Same keys, so the choice
#: between the two tables is total and no verdict can arrive without a sentence.
#:
#: **The reference implementation never needed this table, and that is the
#: point.** It had no approval transport at all — it detected a pending request
#: and sent the user to the screen (`legacy@1d32845:bridge/transcript.py:
#: 1633-1713`; `legacy@1d32845:bridge/daemon.py:1901-2052`) — so it could not
#: claim a verdict had landed. v1 carries verdicts, and inherits the obligation
#: legacy met for free: "approved by voice" is a claim about the *Session*, and
#: only a `DELIVERED` receipt is evidence for it.
#:
#: Each sentence points back at the on-screen dialog, because on every grade
#: that lands here the dialog is still the thing that can actually resolve it —
#: `HELD` says so outright, and `FAILED`/`UNKNOWN` leave it untouched.
UNCONFIRMED_NOTICES: dict[ApprovalVerdict, str] = {
    ApprovalVerdict.ALLOW: (
        "your approval was not confirmed to have reached the session — if the dialog is "
        "still on screen, answer it there"
    ),
    ApprovalVerdict.DENY: (
        "your denial was not confirmed to have reached the session — if the dialog is "
        "still on screen, answer it there"
    ),
    #: `ask` carried no verdict, so there is nothing a receipt could fail to
    #: confirm: its own wording already claims nothing and stands on every grade.
    ApprovalVerdict.ASK: CLOSING_NOTICES[ApprovalVerdict.ASK],
}


def closing_notice_for(verdict: ApprovalVerdict, outcome: Delivery) -> str:
    """What the user is told, claiming exactly what the receipt proves and no more."""
    return CLOSING_NOTICES[verdict] if outcome.is_delivered else UNCONFIRMED_NOTICES[verdict]


@dataclass(frozen=True, slots=True)
class PendingApproval:
    """One permission request the user has a budget to answer by voice."""

    request: ApprovalRequest
    #: Bridge Core's id for the verdict it will carry back. Distinct from the
    #: adapter's `approval_id`, which names the dialog the Session raised.
    request_id: RequestId
    opened_at: float
    expires_at: float


@dataclass(frozen=True, slots=True)
class ApprovalOutcome:
    """How one pending request resolved, and what the user is told about it."""

    request: ApprovalRequest
    verdict: ApprovalVerdict
    state: Lifecycle
    #: Fires on every resolution. Exactly one per request.
    closing_notice: str
    outcome: Delivery = Delivery.UNKNOWN


class ApprovalPipeline:
    """Announces pending dialogs, carries verdicts, and never denies on timeout."""

    def __init__(
        self,
        *,
        agents: Mapping[AgentKind, AgentAdapter],
        escalation: EscalationPipeline,
        policy: CorePolicy | None = None,
        clock: Clock = default_clock,
    ) -> None:
        self._agents = dict(agents)
        self._escalation = escalation
        self._policy = policy or CorePolicy()
        self._clock = clock
        self._pending: dict[str, PendingApproval] = {}

    def pending(self) -> tuple[PendingApproval, ...]:
        """Every request still inside its budget, in the order they arrived."""
        return tuple(self._pending.values())

    async def opened(
        self, request: ApprovalRequest, spoken_as: str | None = None
    ) -> tuple[PendingApproval, NoticeOutcome]:
        """A dialog is on screen. Start the budget and announce it everywhere.

        The budget starts here and ticks regardless of whether any outlet took
        the announcement: the dialog is stalled either way, and a budget that
        only ran when someone was listening would never expire on the one path
        where the fallback matters most.

        `spoken_as` is what the announcement refers to the Session by (#109). It is the
        caller's because the Session *registry* is not this pipeline's — the
        dialog arrives as an `ApprovalRequest`, which carries a target and no
        name — and it is optional because the address is a complete answer on its
        own: `spoken_target` is the floor `spoken_name` itself falls back to, so
        a caller with nothing better to say still names the Session.
        """
        now = self._clock()
        waiting = PendingApproval(
            request=request,
            request_id=new_request_id(),
            opened_at=now,
            expires_at=now + self._policy.approval_budget_seconds,
        )
        self._pending[request.approval_id] = waiting

        outcome = await self._announce(waiting, spoken_as)
        return waiting, outcome

    async def reoffer(self, approval_id: str, spoken_as: str | None = None) -> NoticeOutcome | None:
        """Announce the request already inside its budget, without opening it twice."""
        waiting = self._pending.get(approval_id)
        if waiting is None:
            return None
        return await self._announce(waiting, spoken_as)

    async def _announce(
        self, waiting: PendingApproval, spoken_as: str | None = None
    ) -> NoticeOutcome:
        request = waiting.request
        return await self._escalation.escalate(
            Notice(
                request_id=waiting.request_id,
                target=request.target,
                text=announcement_for(request, spoken_as or spoken_target(request.target)),
            ),
            reach=Reach.EVERY_OUTLET,
        )

    async def answer(self, approval_id: str, verdict: ApprovalVerdict) -> ApprovalOutcome | None:
        """Carry the user's verdict. Returns None when nothing is waiting for it.

        A verdict for a request that already resolved — expired, or answered —
        is **discarded safely**: it carries nothing and emits nothing, because
        the closing notice for that request has already gone out.
        """
        waiting = self._pending.pop(approval_id, None)
        if waiting is None:
            _log.info(
                "verdict %s for %s arrived after it resolved; discarded", verdict, approval_id
            )
            return None
        return await self._resolve(waiting, verdict)

    async def sweep_expired(self) -> tuple[ApprovalOutcome, ...]:
        """Every request past its budget falls back to the on-screen dialog.

        `ask`, never deny. Popping first is what makes it exactly once, and what
        makes a verdict arriving a moment later discardable rather than racing.
        """
        now = self._clock()
        expired = [waiting for waiting in self._pending.values() if waiting.expires_at <= now]
        resolved: list[ApprovalOutcome] = []
        for waiting in expired:
            del self._pending[waiting.request.approval_id]
            resolved.append(await self._resolve(waiting, ApprovalVerdict.ASK))
        return tuple(resolved)

    async def _resolve(self, waiting: PendingApproval, verdict: ApprovalVerdict) -> ApprovalOutcome:
        """Carry the verdict, then close the loop on every surface that was told."""
        outcome = Delivery.UNKNOWN
        adapter = self._agents.get(waiting.request.target.agent)
        if adapter is not None:
            receipt = await adapter.approval_relay(
                waiting.request, verdict, request_id=waiting.request_id
            )
            outcome = receipt.outcome

        closing = closing_notice_for(verdict, outcome)
        await self._escalation.escalate(
            Notice(
                request_id=new_request_id(),
                target=waiting.request.target,
                text=closing,
            ),
            reach=Reach.EVERY_OUTLET,
        )

        # RETAINED is deliberately unreachable here. A pending approval has a
        # budget and a fallback, so it is resolved or handed back — it never
        # enters the Answer Relay queue. An
        # `ask` is terminal for the voice path even when the adapter carried it
        # cleanly, and that is exactly what the closing notice tells the user.
        state = (
            Lifecycle.DELIVERED
            if verdict is not ApprovalVerdict.ASK and outcome.is_delivered
            else Lifecycle.REPORTED_FAILED
        )
        return ApprovalOutcome(
            request=waiting.request,
            verdict=verdict,
            state=state,
            closing_notice=closing,
            outcome=outcome,
        )

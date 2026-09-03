"""Relay queueing against the Reply Window — the hub's, never an adapter's.

The locked behaviour and the reason for it: unsolicited user text **queues until
the Reply Window is open**. That is not politeness. Delivering the user's words
mid-turn without being asked gets them framed as untrusted and refused —
verified live — so waiting for the window is what makes them arrive with the
user's authority intact. Adapters deliver; they never queue, because a queue
inside an adapter would be a second ledger and a second policy.

Three rules hang off that, all of them here:

- **The receipt is a grade and a reason, never a sentence.** The verb answers
  with `RelayOutcome`: where the words are, what the last attempt proved as the
  seam's own `DeliveryReceipt` — or nothing, when none was made — and one
  `RelayReason` code. Seven English sentences used to live here and be rendered
  verbatim by whatever surface asked, which made Bridge Core the author of words
  the user hears. The Voice re-renders whatever it is handed (#175), so those
  sentences were a second renderer for words the model rewrites anyway; they are
  the Voice's rule now, in the generated instructions. Legacy had no scripted
  acknowledgement to port (`legacy@1d32845:skill/SKILL.md:63-68` covers failure
  only) — **dropped**; its synchronous relay reply
  (`legacy@1d32845:bridge/__main__.py:656-661,683-780`) is **adapted** into this
  structured answer.
- **A ten-minute ceiling, then a reported failure.** The number is
  `CorePolicy`'s, not this module's, and expiry takes the entry *out* of the
  ledger, so REPORTED_FAILED means what it says: nothing retries it.
- **Route follows the user's explicit intent.** Deliver (between turns) versus
  supplement (mid-turn) is what the user asked for, never what the Session
  happens to be doing — the same "busy" carries both "add this now" and "this
  can wait". An adapter that honestly lacks SUPPLEMENT does not get guessed
  around: the words queue as a DELIVER, which is this module's decision to make
  and not the adapter's.

A non-delivery keeps the words. FAILED, HELD and UNKNOWN all mean "not proven
delivered", so the entry waits — and it waits for the *next Reply Window
transition*, never retrying off the back of its own failure.

**But waiting and being sent again are not the same thing** (P9). A second
attempt is permitted only where non-delivery was **proven**, which is the
reference implementation's own settlement rule
(`legacy@1d32845:bridge/delivery.py:28-75`;
`legacy@1d32845:bridge/coordinator.py:1075-1109`;
`legacy@1d32845:bridge/store.py:964-1035,3394-3555,3653-3874`) — **ported**, with
its durable ledger, its confirmed-request history and its crash enquiry left
behind (#61 R1). So:

- **DELIVERED completes.** The entry leaves the queue and nothing retries it.
- **FAILED may go again** at the next window, under the existing ten-minute
  ceiling (#61 R2). Nothing arrived, so nothing can arrive twice.
- **UNKNOWN never goes again on this system's own authority.** It is kept, as
  duplicate-risk information, and the user is told plainly that it may already
  have landed — a second attempt is theirs to authorise, by saying the words
  again. #71 makes this concrete: on the Claude inbox route an accepted socket
  write proves nothing, so most UNKNOWNs there are Relays that *did* arrive.
- **HELD never goes again either.** It is parked in front of a person on the far
  side and will settle on its own; sending it a second time is how one decision
  becomes two identical messages waiting for the same human.

That is why an entry that has been attempted carries the grade that attempt
produced, and one that has not carries `None`. Re-sending the user's own words
on an UNKNOWN grade is how the reference implementation produced duplicates
before it learned the rule; spelling "nothing was attempted" `UNKNOWN` would
bring the duplicates back by making the two indistinguishable.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from gpt_voicecoding.core.clock import Clock, default_clock
from gpt_voicecoding.core.lifecycle import Lifecycle
from gpt_voicecoding.core.policy import CorePolicy
from gpt_voicecoding.core.relay_queue import PendingRelay, RelayKind, RelayQueue
from gpt_voicecoding.core.sessions import SessionRegistry
from gpt_voicecoding.seams.agent import (
    AgentAdapter,
    RelayRoute,
    ReplyWindow,
    SessionState,
    WaitingKind,
    derive_reply_window,
)
from gpt_voicecoding.seams.delivery import Delivery, DeliveryReceipt
from gpt_voicecoding.seams.identity import AgentKind, RequestId, SessionTarget, new_request_id

_log = logging.getLogger(__name__)


class RelayReason(StrEnum):
    """Why a Relay stands where it does. Closed, and the only thing said about it.

    This replaces seven English sentences and one inline apology. They were
    written here because a surface rendered them verbatim, which made Bridge
    Core the author of words the user hears — a second renderer beside the
    Voice, which re-renders whatever it is handed anyway (#175). A code says the
    fact; composing the sentence is the Voice's rule, in the instructions.

    **The proven/unproven pairs collapsed.** Two of these used to be four,
    because a sentence about a ceiling may not claim non-delivery of an
    `UNKNOWN` — the grade that means the far side may well have the words.
    A code claims nothing about arrival: `ceiling_passed` is a fact about this
    system's own limit, and the attempt's grade travels beside it.
    """

    #: The attempt proved the words reached the model. Nothing else does.
    DELIVERED = "delivered"
    #: The words wait, and may go again when the Session next takes a turn.
    #: Both grades that earn another attempt live here: nothing was sent, or an
    #: attempt **proved** nothing arrived.
    AWAITING_REPLY_WINDOW = "awaiting_reply_window"
    #: An attempt proved nothing either way, so the words are kept and never
    #: sent again on this system's own authority (P9). Saying them again is the
    #: user's to authorise.
    DUPLICATE_RISK = "duplicate_risk"
    #: The far side parked the words in front of a person. It settles on its
    #: own; a second copy is a second decision for the same human.
    HELD_FAR_SIDE = "held_far_side"
    #: Terminal: the words waited past `relay_ceiling_seconds` and left the
    #: ledger, so nothing retries them.
    CEILING_PASSED = "ceiling_passed"
    #: Terminal: the Session those words were for ended while they waited.
    SESSION_ENDED = "session_ended"
    #: Terminal, and refused before the wire: the question is no longer
    #: answerable from here, so the words were never queued for an inbox that
    #: cannot take them (#68).
    QUESTION_UNANSWERABLE = "question_unanswerable"


#: Which code each grade earns while a Relay is still in play. Total over the
#: four grades and the absent attempt, and a function: one grade never produces
#: two codes. The terminal codes are not in here because they are facts about
#: what happened *here*, not about what an attempt proved.
_REASON_BY_GRADE: Mapping[Delivery | None, RelayReason] = {
    None: RelayReason.AWAITING_REPLY_WINDOW,
    Delivery.FAILED: RelayReason.AWAITING_REPLY_WINDOW,
    Delivery.UNKNOWN: RelayReason.DUPLICATE_RISK,
    Delivery.HELD: RelayReason.HELD_FAR_SIDE,
    Delivery.DELIVERED: RelayReason.DELIVERED,
}

#: What the grade field says when there is no attempt to grade. Not `unknown`:
#: that is a positive observation, and this is the absence of one.
NO_GRADE = "none"


def reason_for(receipt: DeliveryReceipt | None) -> RelayReason:
    """The code an attempt earns, or the one that says nothing was attempted."""
    return _REASON_BY_GRADE[None if receipt is None else receipt.outcome]


def receipt_line(*, state: str, grade: str, reason: str) -> str:
    """The receipt as one line of codes — the one format every surface prints.

    Three facts and no sentence. One place, so the CLI and the Companion Channel
    cannot drift into two ways of saying the same receipt, and so a field a
    harness parses is named the same on both.

    The attempt's own evidence (`DeliveryReceipt.reason`) is deliberately absent:
    it travels on the wire and into the log, where a defect is diagnosed, and it
    is the adapter's words rather than anything the user asked for.
    """
    return f"state={state} grade={grade} reason={reason}"


def may_be_retried(receipt: DeliveryReceipt | None) -> bool:
    """Whether a waiting Relay may go again on this system's own authority (P9).

    Two cases and no others: nothing was attempted, so nothing can arrive twice;
    or an attempt **proved** it did not arrive. `UNKNOWN` and `HELD` are both
    "it may have", and this system does not gamble the user's own words on a may.
    """
    return receipt is None or receipt.outcome is Delivery.FAILED


@dataclass(frozen=True, slots=True)
class RelayOutcome:
    """Where one Relay stands: the receipt, and the whole of it.

    Three facts and no sentence. `state` is where the words are, `receipt` is
    what the last attempt proved — the seam's own value, evidence included, or
    `None` when nothing has been attempted — and `reason` is the one code that
    says why it stands there.

    **"Not attempted" is the absent attempt, never `UNKNOWN`.** The two are the
    difference between "it may already have arrived" and "it never left this
    process", which is the whole of P9's settlement rule; a `None` grade spelt
    `UNKNOWN` would bring back the duplicates that rule exists to prevent.
    """

    request_id: RequestId
    target: SessionTarget
    state: Lifecycle
    route: RelayRoute
    #: Why the Relay stands where it does. One code, always present.
    reason: RelayReason
    #: The last attempt, whole, or `None` when nothing was attempted.
    receipt: DeliveryReceipt | None = None

    def __post_init__(self) -> None:
        if (self.state is Lifecycle.DELIVERED) is not (
            self.receipt is not None and self.receipt.is_delivered
        ):
            raise ValueError(
                "a Relay is DELIVERED exactly when an attempt proved it: "
                f"{self.state} beside {self.receipt}"
            )

    @property
    def grade(self) -> str:
        """The attempt's grade as a surface prints it, or `none` when there was none."""
        return NO_GRADE if self.receipt is None else str(self.receipt.outcome)

    @property
    def line(self) -> str:
        """This receipt in the one format every surface prints."""
        return receipt_line(state=str(self.state), grade=self.grade, reason=str(self.reason))


def terminal_line(outcome: RelayOutcome) -> str:
    """A relay that finally failed, as a bare code line pushed at the user.

    **The grade travels with it**, so an expired `UNKNOWN` and an expired
    `FAILED` do not read alike. That distinction is the entire reason the
    deleted reports came in proven and unproven pairs: "it never reached the
    session" is true of one and a guess about the other. The code says what
    happened here, the grade says what was proved, and neither has to hedge on
    the other's behalf — which is why one line of codes can replace four
    sentences without claiming more than the receipt does.

    **Temporary, and deliberately the narrowest thing that keeps the news
    flowing.** A terminal failure has to reach the user, and until #197 lands the
    only route it has is the Companion Channel push, which carries text. So it
    carries codes, and no sentence. #197 folds the reason onto the Session's row
    and wakes the Keeper instead, and deletes this function whole with the three
    calls that use it.
    """
    return outcome.line


class RelayPipeline:
    """Carries the user's own words in, or holds them until they can go."""

    def __init__(
        self,
        *,
        agents: Mapping[AgentKind, AgentAdapter],
        sessions: SessionRegistry,
        relays: RelayQueue,
        policy: CorePolicy | None = None,
        clock: Clock = default_clock,
    ) -> None:
        self._agents = dict(agents)
        self._sessions = sessions
        self._relays = relays
        self._policy = policy or CorePolicy()
        self._clock = clock

    async def relay(
        self,
        target: SessionTarget,
        text: str,
        *,
        route: RelayRoute = RelayRoute.DELIVER,
        request_id: RequestId | None = None,
    ) -> RelayOutcome:
        """Take the user's words for one Session. Delivers now, or queues them.

        Fails closed on the target: an unknown or stale identity raises rather
        than queueing words for a Session that will never take them.
        """
        session = self._sessions.resolve(target)
        adapter = self._adapter(target)
        chosen = self._honest_route(adapter, route)
        rid = request_id or new_request_id()

        try:
            question_answerable = adapter.question_answerable(target)
        except Exception:  # noqa: BLE001 - a level query fails closed, never the Relay call
            _log.exception("the %s lane could not report its question route", target.agent)
            question_answerable = False
        window = derive_reply_window(
            session.state,
            session.waiting_for,
            session.child,
            question_answerable=question_answerable,
        )
        if (
            chosen is RelayRoute.DELIVER
            and session.state is SessionState.WAITING
            and session.waiting_for.kind is WaitingKind.QUESTION
            and not question_answerable
        ):
            # Terminal before the wire, and therefore **ungraded**: nothing was
            # attempted, so there is no attempt to classify. The code is the
            # whole answer — where the question can still be answered is the
            # Voice's sentence to compose, not this module's (#175).
            _log.info("relay %s refused: %s is no longer answerable from here", rid, target)
            return RelayOutcome(
                request_id=rid,
                target=target,
                state=Lifecycle.REPORTED_FAILED,
                route=chosen,
                reason=RelayReason.QUESTION_UNANSWERABLE,
            )
        may_go_now = chosen is RelayRoute.SUPPLEMENT or window is ReplyWindow.OPEN
        if may_go_now:
            attempt = await adapter.answer_relay(target, text, request_id=rid, route=chosen)
            if attempt.is_delivered:
                return RelayOutcome(
                    request_id=rid,
                    target=target,
                    state=Lifecycle.DELIVERED,
                    route=chosen,
                    reason=RelayReason.DELIVERED,
                    receipt=attempt,
                )
            _log.info(
                "relay %s not proven delivered (%s: %s); it waits",
                rid,
                attempt.outcome,
                attempt.reason,
            )
            receipt: DeliveryReceipt | None = attempt
        else:
            # Nothing went on the wire, so there is no attempt to grade and no
            # duplicate to risk. `None` is that fact, and it is what makes this
            # entry retriable where an attempted-but-unproven one is not.
            receipt = None

        # Anything that did not prove delivery waits for the next window, and a
        # SUPPLEMENT that could not go mid-turn waits as an ordinary DELIVER.
        self._enqueue(rid, target, text, receipt=receipt)
        return RelayOutcome(
            request_id=rid,
            target=target,
            state=Lifecycle.RETAINED,
            route=RelayRoute.DELIVER,
            reason=reason_for(receipt),
            receipt=receipt,
        )

    async def reply_window_opened(self, target: SessionTarget) -> tuple[RelayOutcome, ...]:
        """The Session will take a user turn. Flush what may still be sent to it.

        This is the *only* retry trigger. Nothing here fires off the back of a
        failed attempt, so a Session that keeps refusing cannot be hammered with
        the same words.

        **And it flushes only what `may_be_retried` allows** (P9). An entry whose
        attempt proved nothing either way is passed over, every time this fires,
        for as long as it is held: the window opening is news about the Session,
        not evidence that the earlier attempt failed. It leaves on its ceiling,
        on a late receipt, or when the user says the words again.
        """
        adapter = self._agents.get(target.agent)
        if adapter is None:
            return ()

        flushed: list[RelayOutcome] = []
        for waiting in self._waiting_answers(target):
            if not may_be_retried(waiting.receipt):
                _log.info(
                    "relay %s is held rather than retried: an attempt graded %s may already "
                    "have arrived, and a second one would duplicate the user's words",
                    waiting.request_id,
                    None if waiting.receipt is None else waiting.receipt.outcome,
                )
                continue
            receipt = await adapter.answer_relay(
                target, waiting.text, request_id=waiting.request_id, route=waiting.route
            )
            if not receipt.is_delivered:
                _log.info(
                    "relay %s not proven delivered on Reply Window retry (%s: %s); it waits",
                    waiting.request_id,
                    receipt.outcome,
                    receipt.reason,
                )
            self._relays.classify(waiting.request_id, receipt)
            flushed.append(
                RelayOutcome(
                    request_id=waiting.request_id,
                    target=target,
                    state=(Lifecycle.DELIVERED if receipt.is_delivered else Lifecycle.RETAINED),
                    route=waiting.route,
                    reason=reason_for(receipt),
                    receipt=receipt,
                )
            )
        return tuple(flushed)

    def sweep_expired(self) -> tuple[RelayOutcome, ...]:
        """Everything past its ceiling becomes a reported failure, once.

        Taking the entry out is what makes "once" true, and what makes the retry
        boundary structural: after this there is nothing left to retry.
        """
        now = self._clock()
        expired = [
            waiting for waiting in self._relays.expired(now=now) if waiting.kind is RelayKind.ANSWER
        ]
        return tuple(
            self._report_failed(waiting, RelayReason.CEILING_PASSED) for waiting in expired
        )

    def session_ended(self, target: SessionTarget) -> tuple[RelayOutcome, ...]:
        """That Session is gone. Words still waiting for it can never arrive."""
        dropped = [
            waiting
            for waiting in self._relays.pending_for(target)
            if waiting.kind is RelayKind.ANSWER
        ]
        return tuple(self._report_failed(waiting, RelayReason.SESSION_ENDED) for waiting in dropped)

    def waiting_for(self, target: SessionTarget) -> tuple[PendingRelay, ...]:
        """The user's words still queued for one Session, oldest first."""
        return self._waiting_answers(target)

    def _adapter(self, target: SessionTarget) -> AgentAdapter:
        adapter = self._agents.get(target.agent)
        if adapter is None:
            raise KeyError(f"no adapter is loaded for {target.agent} Sessions")
        return adapter

    @staticmethod
    def _honest_route(adapter: AgentAdapter, route: RelayRoute) -> RelayRoute:
        """The route the user asked for, unless the adapter honestly lacks it."""
        if route in adapter.supported_routes():
            return route
        return RelayRoute.DELIVER

    def _waiting_answers(self, target: SessionTarget) -> tuple[PendingRelay, ...]:
        return tuple(
            waiting
            for waiting in self._relays.pending_for(target)
            if waiting.kind is RelayKind.ANSWER
        )

    def _enqueue(
        self,
        request_id: RequestId,
        target: SessionTarget,
        text: str,
        *,
        receipt: DeliveryReceipt | None,
    ) -> PendingRelay:
        queued_at = self._clock()
        return self._relays.enqueue(
            PendingRelay(
                request_id=request_id,
                target=target,
                kind=RelayKind.ANSWER,
                text=text,
                queued_at=queued_at,
                expires_at=queued_at + self._policy.relay_ceiling_seconds,
                route=RelayRoute.DELIVER,
                receipt=receipt,
            )
        )

    def _report_failed(self, waiting: PendingRelay, reason: RelayReason) -> RelayOutcome:
        """Take the entry out and say why, in the code and the last attempt's grade.

        The two used to be one sentence in two spellings, because a rendered
        sentence may not claim non-delivery of an attempt that proved nothing.
        The code says what happened here and the receipt says what was proved,
        so neither has to hedge on the other's behalf.
        """
        released = self._relays.release(waiting.request_id)
        _log.info(
            "relay %s is terminal: reason=%s grade=%s",
            released.request_id,
            reason,
            None if released.receipt is None else released.receipt.outcome,
        )
        return RelayOutcome(
            request_id=released.request_id,
            target=released.target,
            state=Lifecycle.REPORTED_FAILED,
            route=released.route,
            reason=reason,
            receipt=released.receipt,
        )


__all__ = [
    "NO_GRADE",
    "RelayOutcome",
    "RelayPipeline",
    "RelayReason",
    "may_be_retried",
    "reason_for",
    "receipt_line",
    "terminal_line",
]

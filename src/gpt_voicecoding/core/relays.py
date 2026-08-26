"""Relay queueing against the Reply Window — the hub's, never an adapter's.

The locked behaviour and the reason for it: unsolicited user text **queues until
the Reply Window is open**. That is not politeness. Delivering the user's words
mid-turn without being asked gets them framed as untrusted and refused —
verified live — so waiting for the window is what makes them arrive with the
user's authority intact. Adapters deliver; they never queue, because a queue
inside an adapter would be a second ledger and a second policy.

Three rules hang off that, all of them here:

- **One confirmation, on receipt**, and then silence: delivery announces nothing
  a second time. Structural rather than remembered — only `relay` can produce a
  confirmation, and the flush path that delivers a waiting entry has no way to
  set one. *Which* confirmation depends on what the attempt proved, because the
  wording is a promise about what happens next and the three cases promise
  different things (`confirmation_for`).
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

from gpt_voicecoding.core.clock import Clock, default_clock
from gpt_voicecoding.core.lifecycle import Lifecycle
from gpt_voicecoding.core.policy import CorePolicy
from gpt_voicecoding.core.relay_queue import PendingRelay, RelayKind, RelayQueue
from gpt_voicecoding.core.sessions import SessionRegistry
from gpt_voicecoding.seams.agent import AgentAdapter, RelayRoute, ReplyWindow
from gpt_voicecoding.seams.delivery import Delivery
from gpt_voicecoding.seams.identity import AgentKind, RequestId, SessionTarget, new_request_id

_log = logging.getLogger(__name__)

#: What the user hears back the moment words are taken but not yet delivered.
#: One place, so the promise and the behaviour cannot drift apart — and this one
#: promises another attempt, so only the two grades that get one may use it.
QUEUED_CONFIRMATION = "got it, it'll go when this turn ends"

#: What the user hears when an attempt proved nothing either way (P9). It says
#: the two things that are true and that they cannot infer: the words may
#: already be in the Session, and nothing here will send them again by itself.
DUPLICATE_RISK_CONFIRMATION = (
    "that may already have reached the session — I'm not sending it again on my own, "
    "because it could arrive twice; say it again if you want another attempt"
)

#: What the user hears when the far side parked the words in front of a person.
#: Distinct from the above because the right thing to do differs: this one
#: settles on its own, and saying it again only queues a second copy for the
#: same human to approve.
HELD_CONFIRMATION = (
    "that is parked waiting for approval on the session's side — I'm not sending it again"
)

#: What is reported when a queued Relay passes its ceiling. This is the moment
#: the Relay becomes terminal, so the wording says so plainly.
CEILING_REPORT = "that never reached the session — it waited past the limit and was dropped"

#: The same moment for a Relay nothing ever proved either way. Surfaces render
#: these verbatim, so this one may not say "never reached the session": that is
#: exactly the claim an UNKNOWN grade refuses to make.
CEILING_UNPROVEN_REPORT = (
    "that was never confirmed either way, and I've stopped holding it — it may or may "
    "not have reached the session"
)

#: What is reported when the Session those words were for is gone.
SESSION_GONE_REPORT = "that never reached the session — it ended while the words were waiting"

#: The same, for a Relay that was attempted and proved nothing. The Session's
#: ending is certain; whether the words got there first is not.
SESSION_GONE_UNPROVEN_REPORT = (
    "that session ended, and whether the words reached it first was never confirmed"
)


def may_be_retried(outcome: Delivery | None) -> bool:
    """Whether a waiting Relay may go again on this system's own authority (P9).

    Two cases and no others: nothing was attempted, so nothing can arrive twice;
    or an attempt **proved** it did not arrive. `UNKNOWN` and `HELD` are both
    "it may have", and this system does not gamble the user's own words on a may.
    """
    return outcome is None or outcome is Delivery.FAILED


def confirmation_for(outcome: Delivery | None) -> str:
    """What the user is told when their words had to wait, per what was proved."""
    if outcome is Delivery.UNKNOWN:
        return DUPLICATE_RISK_CONFIRMATION
    if outcome is Delivery.HELD:
        return HELD_CONFIRMATION
    return QUEUED_CONFIRMATION


def _report_for(outcome: Delivery | None, *, proven: str, unproven: str) -> str:
    """One of two reports, chosen by whether non-delivery was actually established."""
    return proven if may_be_retried(outcome) else unproven


@dataclass(frozen=True, slots=True)
class RelayOutcome:
    """Where one Relay stands, and the one thing the user may be told about it."""

    request_id: RequestId
    target: SessionTarget
    state: Lifecycle
    route: RelayRoute
    #: Spoken or sent back **once**, on receipt, and only when the words had to
    #: wait. Empty on every other path, including the delivery that follows.
    confirmation: str = ""
    #: Said to the user only when the Relay became terminal without arriving.
    report: str = ""
    #: The last attempt's honest grade, or `None` when nothing was attempted.
    outcome: Delivery | None = None


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

        may_go_now = chosen is RelayRoute.SUPPLEMENT or session.reply_window is ReplyWindow.OPEN
        if may_go_now:
            receipt = await adapter.answer_relay(target, text, request_id=rid, route=chosen)
            if receipt.is_delivered:
                return RelayOutcome(
                    request_id=rid,
                    target=target,
                    state=Lifecycle.DELIVERED,
                    route=chosen,
                    outcome=receipt.outcome,
                )
            _log.info(
                "relay %s not proven delivered (%s: %s); it waits",
                rid,
                receipt.outcome,
                receipt.reason,
            )
            outcome = receipt.outcome
        else:
            # Nothing went on the wire, so there is no grade to record and no
            # duplicate to risk. `None` is that fact, and it is what makes this
            # entry retriable where an attempted-but-unproven one is not.
            outcome = None

        # Anything that did not prove delivery waits for the next window, and a
        # SUPPLEMENT that could not go mid-turn waits as an ordinary DELIVER.
        self._enqueue(rid, target, text, outcome=outcome)
        return RelayOutcome(
            request_id=rid,
            target=target,
            state=Lifecycle.RETAINED,
            route=RelayRoute.DELIVER,
            confirmation=confirmation_for(outcome),
            outcome=outcome,
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
            if not may_be_retried(waiting.outcome):
                _log.info(
                    "relay %s is held rather than retried: an attempt graded %s may already "
                    "have arrived, and a second one would duplicate the user's words",
                    waiting.request_id,
                    waiting.outcome,
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
            self._relays.classify(waiting.request_id, receipt.outcome)
            flushed.append(
                RelayOutcome(
                    request_id=waiting.request_id,
                    target=target,
                    state=(Lifecycle.DELIVERED if receipt.is_delivered else Lifecycle.RETAINED),
                    route=waiting.route,
                    outcome=receipt.outcome,
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
            self._report_failed(
                waiting,
                _report_for(
                    waiting.outcome, proven=CEILING_REPORT, unproven=CEILING_UNPROVEN_REPORT
                ),
            )
            for waiting in expired
        )

    def session_ended(self, target: SessionTarget) -> tuple[RelayOutcome, ...]:
        """That Session is gone. Words still waiting for it can never arrive."""
        dropped = [
            waiting
            for waiting in self._relays.pending_for(target)
            if waiting.kind is RelayKind.ANSWER
        ]
        return tuple(
            self._report_failed(
                waiting,
                _report_for(
                    waiting.outcome,
                    proven=SESSION_GONE_REPORT,
                    unproven=SESSION_GONE_UNPROVEN_REPORT,
                ),
            )
            for waiting in dropped
        )

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
        self, request_id: RequestId, target: SessionTarget, text: str, *, outcome: Delivery | None
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
                outcome=outcome,
            )
        )

    def _report_failed(self, waiting: PendingRelay, report: str) -> RelayOutcome:
        released = self._relays.release(waiting.request_id)
        _log.info("relay %s reported to the user as failed: %s", released.request_id, report)
        return RelayOutcome(
            request_id=released.request_id,
            target=released.target,
            state=Lifecycle.REPORTED_FAILED,
            route=released.route,
            report=report,
            outcome=released.outcome,
        )


__all__ = [
    "CEILING_REPORT",
    "CEILING_UNPROVEN_REPORT",
    "DUPLICATE_RISK_CONFIRMATION",
    "HELD_CONFIRMATION",
    "QUEUED_CONFIRMATION",
    "SESSION_GONE_REPORT",
    "SESSION_GONE_UNPROVEN_REPORT",
    "RelayOutcome",
    "RelayPipeline",
    "confirmation_for",
    "may_be_retried",
]

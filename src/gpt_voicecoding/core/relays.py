"""Relay queueing against the Reply Window — the hub's, never an adapter's.

The locked behaviour and the reason for it: unsolicited user text **queues until
the Reply Window is open**. That is not politeness. Delivering the user's words
mid-turn without being asked gets them framed as untrusted and refused —
verified live — so waiting for the window is what makes them arrive with the
user's authority intact. Adapters deliver; they never queue, because a queue
inside an adapter would be a second ledger and a second policy.

Three rules hang off that, all of them here:

- **One confirmation, on receipt.** "Got it, it'll go when this turn ends", and
  then silence: delivery announces nothing a second time. Structural rather than
  remembered — only `relay` can produce a confirmation, and the flush path that
  delivers a waiting entry has no way to set one.
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
transition*, never retrying off the back of its own failure. Re-sending the
user's own words into a Session on an UNKNOWN grade is how the reference
implementation produced duplicates.
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
#: One place, so the promise and the behaviour cannot drift apart.
QUEUED_CONFIRMATION = "got it, it'll go when this turn ends"

#: What is reported when a queued Relay passes its ceiling. This is the moment
#: the Relay becomes terminal, so the wording says so plainly.
CEILING_REPORT = "that never reached the session — it waited past the limit and was dropped"

#: What is reported when the Session those words were for is gone.
SESSION_GONE_REPORT = "that never reached the session — it ended while the words were waiting"


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
    #: The last attempt's honest grade, or UNKNOWN when nothing was attempted.
    outcome: Delivery = Delivery.UNKNOWN


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
            outcome = Delivery.UNKNOWN

        # Anything that did not prove delivery waits for the next window, and a
        # SUPPLEMENT that could not go mid-turn waits as an ordinary DELIVER.
        self._enqueue(rid, target, text, outcome=outcome)
        return RelayOutcome(
            request_id=rid,
            target=target,
            state=Lifecycle.RETAINED,
            route=RelayRoute.DELIVER,
            confirmation=QUEUED_CONFIRMATION,
            outcome=outcome,
        )

    async def reply_window_opened(self, target: SessionTarget) -> tuple[RelayOutcome, ...]:
        """The Session will take a user turn. Flush what has been waiting for it.

        This is the *only* retry trigger. Nothing here fires off the back of a
        failed attempt, so a Session that keeps refusing cannot be hammered with
        the same words.
        """
        adapter = self._agents.get(target.agent)
        if adapter is None:
            return ()

        flushed: list[RelayOutcome] = []
        for waiting in self._waiting_answers(target):
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
        return tuple(self._report_failed(waiting, CEILING_REPORT) for waiting in expired)

    def session_ended(self, target: SessionTarget) -> tuple[RelayOutcome, ...]:
        """That Session is gone. Words still waiting for it can never arrive."""
        dropped = [
            waiting
            for waiting in self._relays.pending_for(target)
            if waiting.kind is RelayKind.ANSWER
        ]
        return tuple(self._report_failed(waiting, SESSION_GONE_REPORT) for waiting in dropped)

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
        self, request_id: RequestId, target: SessionTarget, text: str, *, outcome: Delivery
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
    "QUEUED_CONFIRMATION",
    "SESSION_GONE_REPORT",
    "RelayOutcome",
    "RelayPipeline",
]

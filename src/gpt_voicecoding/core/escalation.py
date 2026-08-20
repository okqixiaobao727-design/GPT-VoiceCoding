"""The Stop Notice escalation pipeline — which outlet, and what if none.

**Routing is this pipeline's to own.** The channel never decides, the Call
adapter never decides, and nothing below a seam knows the matrix. That is the
repair for the reference implementation, where Stop Notice delivery was
hard-wired to one surface: when that surface stopped answering, every notice
failed and there was no second route to fall to, because no component owned the
question of what the second route would be.

The matrix itself is a pure function — `route_matrix` — over three inputs: is a
Live Call up, may the system touch the call, may the system push text. It is
pure so that the whole table can be read, and tested, without a fake in sight.
Executing it is the pipeline's job, and permission is re-read **between**
routes: Duty flipping off mid-escalation halts what has not gone out yet.

Every row of the matrix ends in RETAIN, because no-loss is a Bridge Core
invariant. A notice with no outlet is one `RelayKind.NOTICE` entry in the one
ledger — never a second stop table — and it waits there **indefinitely**. There
is no attempt cap: retention *is* the policy, and the locked words are "surfaces
on the next available outlet". What stops that becoming a livelock is that
attempts fire only on outlet transitions (`sweep`, called when a call starts or
a switch turns effective-on), never on the failure of the attempt before.

Two states are terminal, and both are terminal by *leaving the ledger* rather
than by carrying a flag:

- DELIVERED — an adapter positively proved it. The entry is gone, so no sweep
  can find it and re-speak it. This is the reference implementation's worst bug
  made structurally impossible: there, an audibly spoken notice was graded
  FAILED by matching another surface's records and retried, opening duplicate
  calls. Here, proving delivery and remaining retryable are mutually exclusive.
- REPORTED_FAILED — someone told the user it failed. `report_failed` is the one
  door, and after it no automatic retry and no substitute action follows.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass
from enum import StrEnum

from gpt_voicecoding.core.adjudication import SwitchAdjudicator
from gpt_voicecoding.core.clock import Clock, default_clock
from gpt_voicecoding.core.errors import SecondCallRefused, UnknownRelayError
from gpt_voicecoding.core.interlock import CallInterlock
from gpt_voicecoding.core.lifecycle import Lifecycle
from gpt_voicecoding.core.relay_queue import PendingRelay, RelayKind, RelayQueue
from gpt_voicecoding.seams.call import CallAdapter
from gpt_voicecoding.seams.companion_channel import CompanionChannel
from gpt_voicecoding.seams.delivery import Delivery, DeliveryReceipt
from gpt_voicecoding.seams.identity import RequestId, SessionTarget

_log = logging.getLogger(__name__)

#: A retained notice's deadline. There is no cap on retention, and the ledger
#: requires a deadline after the moment of queueing, so the honest value for
#: "this waits until an outlet appears" is one that never arrives.
NO_DEADLINE = math.inf


class NoticeRoute(StrEnum):
    """One way a notice can be attempted. The matrix is a sequence of these."""

    #: Speak into the Live Call the system already owns.
    SPEAK_INTO_CALL = "speak_into_call"
    #: Open a Live Call, then speak into it. Only legal with none up.
    OPEN_CALL_AND_SPEAK = "open_call_and_speak"
    #: Push text through the Companion Channel.
    PUSH_TO_CHANNEL = "push_to_channel"
    #: Hold it in the one ledger for the next available outlet. Always last.
    RETAIN = "retain"


class Reach(StrEnum):
    """How many outlets one escalation uses."""

    #: Stop at the first outlet that proved delivery — a Stop Notice heard
    #: twice is worse than one heard once. The default, and what a Stop Notice
    #: uses.
    FIRST_OUTLET = "first_outlet"
    #: Use every outlet the switches allow, in parallel with each other. A
    #: pending permission dialog is a stall the user may be nowhere near, so the
    #: push does not wait on the voice attempt; a closing notice absorbs the
    #: duplicate when the request resolves.
    EVERY_OUTLET = "every_outlet"


@dataclass(frozen=True, slots=True)
class Notice:
    """One attention-needing thing to tell the user, and the words for it.

    Deliberately *not* called a Stop Notice. `CONTEXT.md` defines that term
    narrowly — the announcement that a Session stopped — and this pipeline
    carries more than one kind: a pending permission dialog, a closing notice, a
    Relay that never arrived. The Stop Notice is the archetype the pipeline is
    named for, not the only thing that rides it.
    """

    request_id: RequestId
    #: The Session this is about. A notice is always about a Session, so a
    #: retained one can be dropped when that Session goes away.
    target: SessionTarget
    text: str

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("a notice announces something; there are no words here")


@dataclass(frozen=True, slots=True)
class NoticeAttempt:
    """What one route proved. Graded by the adapter, in the four-state vocabulary."""

    route: NoticeRoute
    outcome: Delivery
    reason: str


@dataclass(frozen=True, slots=True)
class NoticeOutcome:
    """Where a notice stands after one escalation, and how it got there."""

    notice: Notice
    state: Lifecycle
    attempts: tuple[NoticeAttempt, ...] = ()

    @property
    def delivered_by(self) -> NoticeRoute | None:
        """The route that positively proved delivery, if any did."""
        for attempt in self.attempts:
            if attempt.outcome.is_delivered:
                return attempt.route
        return None


def route_matrix(
    *, call_is_up: bool, may_touch_call: bool, may_push: bool
) -> tuple[NoticeRoute, ...]:
    """Which routes to try, in order, for the state the system is in right now.

    The whole locked matrix, in three rules:

    - With a Live Call up and Voice on, the notice is **spoken into the existing
      call**. Never re-opened — that is the one-call invariant, and opening on
      top of a system-owned call is what put two assistants on shared speakers.
    - With no call up and Voice on, escalation **may open one**.
    - With Message on, the Companion Channel is the fallback — and with Voice
      off it is the only route, because messages-only is a supported state and
      not a degraded one.

    Every row ends in RETAIN. Nothing here can drop a notice.
    """
    routes: list[NoticeRoute] = []
    if may_touch_call:
        routes.append(
            NoticeRoute.SPEAK_INTO_CALL if call_is_up else NoticeRoute.OPEN_CALL_AND_SPEAK
        )
    if may_push:
        routes.append(NoticeRoute.PUSH_TO_CHANNEL)
    routes.append(NoticeRoute.RETAIN)
    return tuple(routes)


class EscalationPipeline:
    """Routes notices out, and retains the ones that could not go."""

    def __init__(
        self,
        *,
        call: CallAdapter,
        channel: CompanionChannel,
        interlock: CallInterlock,
        adjudicator: SwitchAdjudicator,
        relays: RelayQueue,
        clock: Clock = default_clock,
    ) -> None:
        self._call = call
        self._channel = channel
        self._interlock = interlock
        self._adjudicator = adjudicator
        self._relays = relays
        self._clock = clock
        #: One sweep at a time, FIFO. Two overlapping sweeps would attempt the
        #: same retained notice twice, which is the duplicate-delivery shape
        #: this pipeline exists to prevent.
        self._sweeping = asyncio.Lock()

    async def escalate(self, notice: Notice, *, reach: Reach = Reach.FIRST_OUTLET) -> NoticeOutcome:
        """Take one notice out through the matrix, or retain it."""
        routes = tuple(
            route
            for route in route_matrix(
                call_is_up=self._interlock.owns_call(),
                may_touch_call=self._adjudicator.may_touch_call(),
                may_push=self._adjudicator.may_push(),
            )
            if route is not NoticeRoute.RETAIN
        )
        if reach is Reach.EVERY_OUTLET:
            attempts = await self._fan_out(routes, notice)
        else:
            attempts = await self._in_turn(routes, notice)
        delivered = any(attempt.outcome.is_delivered for attempt in attempts)

        if delivered:
            self._forget(notice.request_id)
            return NoticeOutcome(notice=notice, state=Lifecycle.DELIVERED, attempts=tuple(attempts))

        self._retain(notice, attempts)
        return NoticeOutcome(notice=notice, state=Lifecycle.RETAINED, attempts=tuple(attempts))

    async def sweep(self) -> tuple[NoticeOutcome, ...]:
        """Re-offer every retained notice. Called on an outlet transition only.

        Never called from a failed attempt: an attempt that failed re-triggering
        a sweep of itself is a livelock, and the locked wording is "surfaces on
        the next available outlet", not "keeps trying".
        """
        if not self._adjudicator.outlets():
            return ()

        async with self._sweeping:
            retained = self.retained()
            return tuple([await self.escalate(self._as_notice(waiting)) for waiting in retained])

    def retained(self) -> tuple[PendingRelay, ...]:
        """Every notice waiting for an outlet, in the order it was retained."""
        return tuple(
            waiting for waiting in self._relays.pending() if waiting.kind is RelayKind.NOTICE
        )

    def report_failed(self, request_id: RequestId) -> NoticeOutcome:
        """Tell the ledger this notice was reported to the user as terminal.

        The one door to REPORTED_FAILED, and it takes the entry out. After this
        there is nothing left for a sweep to find, so "no automatic retry and no
        substitute action after a reported failure" is structural.
        """
        waiting = self._relays.release(request_id)
        _log.info("stop notice reported to the user as failed: %s", request_id)
        return NoticeOutcome(notice=self._as_notice(waiting), state=Lifecycle.REPORTED_FAILED)

    async def _in_turn(
        self, routes: tuple[NoticeRoute, ...], notice: Notice
    ) -> list[NoticeAttempt]:
        """Try one outlet at a time and stop at the one that worked.

        Permission is re-read **between** routes: Duty going off while an
        earlier route was in flight halts whatever has not gone out yet, rather
        than forcing it through on a stale permission.
        """
        attempts: list[NoticeAttempt] = []
        for route in routes:
            if not self._still_permitted(route):
                break
            receipt = await self._attempt(route, notice)
            attempts.append(
                NoticeAttempt(route=route, outcome=receipt.outcome, reason=receipt.reason)
            )
            if receipt.outcome.is_delivered:
                break
        return attempts

    async def _fan_out(
        self, routes: tuple[NoticeRoute, ...], notice: Notice
    ) -> list[NoticeAttempt]:
        """Try every open outlet at once.

        "In parallel" is the locked word and it has to be literal: a pending
        permission dialog is burning its budget, so the push must not wait to
        find out whether the voice attempt worked. Sequential fall-through would
        make a stalled call hold the text back for as long as it stalls.

        Permission is therefore read once, at the top — an attempt already in
        flight cannot be recalled by a switch flipped a moment later. That is
        inherent to firing them together, and it is why the Stop Notice path
        does not use this reach.
        """
        permitted = tuple(route for route in routes if self._still_permitted(route))
        receipts = await asyncio.gather(*(self._attempt(route, notice) for route in permitted))
        return [
            NoticeAttempt(route=route, outcome=receipt.outcome, reason=receipt.reason)
            for route, receipt in zip(permitted, receipts, strict=True)
        ]

    def retire(self, request_id: RequestId) -> Notice | None:
        """Drop a retained notice whose reason for existing has gone away.

        Distinct from both terminal states, and deliberately quiet: nothing was
        delivered and nothing was reported, because there is no longer anything
        worth saying. A pending approval that resolves retires the announcement
        it may still be holding — otherwise the next outlet transition speaks a
        prompt for a decision already made.

        Returns None when nothing was waiting, because the notice going out
        first is the ordinary case, not an error.
        """
        try:
            released = self._relays.release(request_id)
        except UnknownRelayError:
            return None
        _log.info("notice retired before it went out: %s", request_id)
        return self._as_notice(released)

    def _still_permitted(self, route: NoticeRoute) -> bool:
        if route is NoticeRoute.PUSH_TO_CHANNEL:
            return self._adjudicator.may_push()
        return self._adjudicator.may_touch_call()

    async def _attempt(self, route: NoticeRoute, notice: Notice) -> DeliveryReceipt:
        if route is NoticeRoute.PUSH_TO_CHANNEL:
            return await self._channel.send(notice.text, request_id=notice.request_id)
        if route is NoticeRoute.OPEN_CALL_AND_SPEAK:
            return await self._open_and_speak(notice)
        return await self._call.speak(notice.text, request_id=notice.request_id)

    async def _open_and_speak(self, notice: Notice) -> DeliveryReceipt:
        try:
            snapshot = await self._interlock.open_call()
        except SecondCallRefused:
            # A call came up while this notice was being routed. Speaking into it
            # is exactly what the matrix would have chosen had it known.
            return await self._call.speak(notice.text, request_id=notice.request_id)

        if not snapshot.is_up:
            return DeliveryReceipt(
                request_id=notice.request_id,
                outcome=Delivery.FAILED,
                reason=f"the call did not come up: it is {snapshot.state}",
            )
        return await self._call.speak(notice.text, request_id=notice.request_id)

    def _retain(self, notice: Notice, attempts: list[NoticeAttempt]) -> None:
        """Hold a notice for the next available outlet. Never a second ledger."""
        outcome = attempts[-1].outcome if attempts else Delivery.UNKNOWN
        try:
            self._relays.classify(notice.request_id, outcome)
        except UnknownRelayError:
            self._relays.enqueue(
                PendingRelay(
                    request_id=notice.request_id,
                    target=notice.target,
                    kind=RelayKind.NOTICE,
                    text=notice.text,
                    queued_at=self._clock(),
                    expires_at=NO_DEADLINE,
                    outcome=outcome,
                )
            )
        _log.info("stop notice retained for the next available outlet: %s", notice.request_id)

    def _forget(self, request_id: RequestId) -> None:
        """Take a proven-delivered notice out, if it had been waiting."""
        try:
            self._relays.classify(request_id, Delivery.DELIVERED)
        except UnknownRelayError:
            # It was delivered on its first attempt and never had to wait.
            return

    @staticmethod
    def _as_notice(waiting: PendingRelay) -> Notice:
        return Notice(request_id=waiting.request_id, target=waiting.target, text=waiting.text)

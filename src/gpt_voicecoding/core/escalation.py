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

An undelivered notice is DROPPED after this attempt. Stop-Notice no-loss belongs
to Bridge Core's current-state reconciliation: an outlet transition inspects
live main Sessions again and may create a fresh notice for what is still
waiting. Historical notice objects are never replayed.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import StrEnum

from gpt_voicecoding.core.adjudication import SwitchAdjudicator
from gpt_voicecoding.core.errors import SecondCallRefused, VoiceInstructionsMissing
from gpt_voicecoding.core.interlock import CallInterlock
from gpt_voicecoding.core.lifecycle import Lifecycle
from gpt_voicecoding.seams.companion_channel import CompanionChannel
from gpt_voicecoding.seams.delivery import Delivery, DeliveryReceipt
from gpt_voicecoding.seams.identity import RequestId, SessionTarget

_log = logging.getLogger(__name__)


class NoticeRoute(StrEnum):
    """One way a notice can be attempted. The matrix is a sequence of these."""

    #: Speak into the Live Call the system already owns.
    SPEAK_INTO_CALL = "speak_into_call"
    #: Open a Live Call, then speak into it. Only legal with none up.
    OPEN_CALL_AND_SPEAK = "open_call_and_speak"
    #: Push text through the Companion Channel.
    PUSH_TO_CHANNEL = "push_to_channel"


class Reach(StrEnum):
    """How many outlets one escalation uses.

    The two reaches read permission at different moments, and that is one
    principle rather than two behaviours: **permission is read at the moment an
    attempt becomes irrevocable.** FIRST_OUTLET re-reads between routes because
    each route is a fresh decision point, so Duty going off mid-escalation halts
    what has not gone yet. EVERY_OUTLET reads once at the top because both
    attempts become irrevocable together — a switch flipped a moment later
    cannot recall a call already in flight.

    A closing notice is its own later emission and is adjudicated at its own
    time, by the same rule.
    """

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
    #: The Session this attempt is about. Deciding whether a wait is announced
    #: at all is Bridge Core's current-state reconciliation, not this notice
    #: object's — and since #161 that decision reads only the current state.
    target: SessionTarget
    #: The words. For a Stop Notice these are `Briefing.text` of the Session
    #: Brief (#189), which is what `CONTEXT.md`'s *Stop Notice* is: "a Session
    #: Brief published as text".
    #:
    #: **Carrying text into the call is interim, and #194 retires it.** The same
    #: glossary entry says the Live Call "does not receive text to read out; it
    #: receives the Session Brief itself and speaks from it" — but a Core type
    #: may not cross the Call seam (ADR 0001: seams and adapters never depend on
    #: Core), so the seam-owned carrier for a spoken brief is designed once,
    #: beside `Dial`, in #194. Until then `SPEAK_INTO_CALL` and
    #: `OPEN_CALL_AND_SPEAK` hand the brief's text to the existing generic
    #: `speak(text)` verb, exactly where they used to hand a composed sentence.
    #: The Companion Channel, the log and the CLI are not interim: text is what
    #: they render.
    text: str

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("a notice announces something; there are no words here")


@dataclass(frozen=True, slots=True)
class NoticeAttempt:
    """What one route proved, graded in the adapter delivery vocabulary."""

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

    An empty row means this attempt has no available outlet.
    """
    routes: list[NoticeRoute] = []
    if may_touch_call:
        routes.append(
            NoticeRoute.SPEAK_INTO_CALL if call_is_up else NoticeRoute.OPEN_CALL_AND_SPEAK
        )
    if may_push:
        routes.append(NoticeRoute.PUSH_TO_CHANNEL)
    return tuple(routes)


class EscalationPipeline:
    """Route one notice attempt through the outlets currently permitted."""

    def __init__(
        self,
        *,
        channel: CompanionChannel,
        interlock: CallInterlock,
        adjudicator: SwitchAdjudicator,
        voice_instructions: str = "",
    ) -> None:
        self._channel = channel
        #: The house rules a call this pipeline opens starts with. Generated by
        #: Bridge Core, held here only to pass on: an engine that generated none
        #: opens no call rather than opening one with nothing to go on.
        self._voice_instructions = voice_instructions
        self._interlock = interlock
        self._adjudicator = adjudicator

    async def escalate(self, notice: Notice, *, reach: Reach = Reach.FIRST_OUTLET) -> NoticeOutcome:
        """Take one notice out through the matrix, or drop this attempt."""
        routes = route_matrix(
            call_is_up=self._interlock.owns_call(),
            may_touch_call=self._adjudicator.may_touch_call(),
            may_push=self._adjudicator.may_push(),
        )
        if reach is Reach.EVERY_OUTLET:
            attempts = await self._fan_out(routes, notice)
        else:
            attempts = await self._in_turn(routes, notice)
        delivered = any(attempt.outcome.is_delivered for attempt in attempts)

        if delivered:
            return NoticeOutcome(notice=notice, state=Lifecycle.DELIVERED, attempts=tuple(attempts))

        reason = attempts[-1].reason if attempts else "no outlet is available"
        _log.info(
            "notice not delivered; this attempt is not replayed: %s (%s)",
            notice.request_id,
            reason,
        )
        return NoticeOutcome(notice=notice, state=Lifecycle.DROPPED, attempts=tuple(attempts))

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

    def _still_permitted(self, route: NoticeRoute) -> bool:
        if route is NoticeRoute.PUSH_TO_CHANNEL:
            return self._adjudicator.may_push()
        return self._adjudicator.may_touch_call()

    async def _attempt(self, route: NoticeRoute, notice: Notice) -> DeliveryReceipt:
        if route is NoticeRoute.PUSH_TO_CHANNEL:
            return await self._channel.send(notice.text, request_id=notice.request_id)
        if route is NoticeRoute.OPEN_CALL_AND_SPEAK:
            return await self._open_and_speak(notice)
        return await self._interlock.speak(notice.text, request_id=notice.request_id)

    async def _open_and_speak(self, notice: Notice) -> DeliveryReceipt:
        try:
            snapshot = await self._interlock.open_call(self._voice_instructions)
        except SecondCallRefused:
            # A call came up while this notice was being routed. Speaking into it
            # is exactly what the matrix would have chosen had it known.
            return await self._interlock.speak(notice.text, request_id=notice.request_id)
        except VoiceInstructionsMissing as refused:
            # Nothing to open a voice thread on. A positive reason the notice can
            # carry when this attempt is dropped.
            return DeliveryReceipt(
                request_id=notice.request_id,
                outcome=Delivery.FAILED,
                reason=str(refused),
            )

        if not snapshot.is_up:
            return DeliveryReceipt(
                request_id=notice.request_id,
                outcome=Delivery.FAILED,
                reason=f"the call did not come up: it is {snapshot.state}",
            )
        return await self._interlock.speak(notice.text, request_id=notice.request_id)

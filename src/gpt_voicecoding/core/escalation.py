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

**The two call routes carry a brief; the channel carries words.** A notice is
both, built from one Session Brief by whoever made it. The call route hands over
the brief itself and the channel renders the text, and a notice that is not
about a Session's state — a failed Relay's terminal line — has only the second
half and can only take the channel (#194, `CONTEXT.md` *Stop Notice*).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from gpt_voicecoding.core.adjudication import SwitchAdjudicator
from gpt_voicecoding.core.errors import CallInstructionsMissing, SecondCallRefused
from gpt_voicecoding.core.interlock import CallInterlock
from gpt_voicecoding.core.lifecycle import Lifecycle
from gpt_voicecoding.seams.call import Dial, SpokenBrief
from gpt_voicecoding.seams.companion_channel import CompanionChannel
from gpt_voicecoding.seams.delivery import Delivery, DeliveryReceipt
from gpt_voicecoding.seams.identity import RequestId, SessionTarget

_log = logging.getLogger(__name__)


class NoticeRoute(StrEnum):
    """One way a notice can be attempted. The matrix is a sequence of these."""

    #: Hand the brief to the Live Call the system already owns.
    SPEAK_INTO_CALL = "speak_into_call"
    #: Open a Live Call already holding the briefing. Only legal with none up.
    OPEN_CALL_WITH_BRIEFING = "open_call_with_briefing"
    #: Push text through the Companion Channel.
    PUSH_TO_CHANNEL = "push_to_channel"


@dataclass(frozen=True, slots=True)
class Notice:
    """One attention-needing thing to tell the user, and the words for it.

    Deliberately *not* called a Stop Notice. `CONTEXT.md` defines that term
    narrowly — the announcement that a Session stopped — and this pipeline
    carries a Relay that never arrived as well. The Stop Notice is the archetype
    the pipeline is named for, not the only thing that rides it. Two other kinds
    used to: the permission dialog's own announcement and the closing notice
    that absorbed its duplicate, both retired with that budget (#191).
    """

    request_id: RequestId
    #: The Session this attempt is about. Deciding whether a wait is announced
    #: at all is Bridge Core's current-state reconciliation, not this notice
    #: object's — and since #161 that decision reads only the current state.
    target: SessionTarget
    #: The words, for the surfaces that render words. For a Stop Notice these
    #: are `briefing.text` of the Session Brief (#189), which is what
    #: `CONTEXT.md`'s *Stop Notice* is: "a Session Brief published as text". The
    #: Companion Channel, the log and the CLI all take this half.
    text: str
    #: The same Session Brief for the surface that does not render words. The
    #: glossary entry continues: the Live Call "does not receive text to read
    #: out; it receives the Session Brief itself and speaks from it" — so the
    #: call routes take this half, built by Briefing from the same brief the
    #: text was (#194 retiring the interim `speak(text)` path #189 left).
    #:
    #: **None on a notice that is not about a Session's state.** The terminal
    #: line of a failed Answer Relay is text and has no brief behind it
    #: (`core/relays.py::terminal_line`, temporary until #197); a notice like
    #: that reaches the user through the Companion Channel, because the Live
    #: Call is not a surface that reads sentences out.
    spoken: SpokenBrief | None = None

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

    - With a Live Call up and Voice on, the notice's brief is **handed to the
      existing call**. Never re-opened — that is the one-call invariant, and
      opening on top of a system-owned call is what put two assistants on shared
      speakers.
    - With no call up and Voice on, escalation **may open one**, and since #194
      the call it opens comes up already holding the briefing.
    - With Message on, the Companion Channel is the fallback — and with Voice
      off it is the only route, because messages-only is a supported state and
      not a degraded one.

    An empty row means this attempt has no available outlet. So does a notice
    with no Session Brief behind it, on either call route: the matrix answers
    from the state alone, and whether *this* notice can travel a route it names
    is `_attempt`'s question.
    """
    routes: list[NoticeRoute] = []
    if may_touch_call:
        routes.append(
            NoticeRoute.SPEAK_INTO_CALL if call_is_up else NoticeRoute.OPEN_CALL_WITH_BRIEFING
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
        system_dial: Callable[[Notice], Dial],
    ) -> None:
        self._channel = channel
        #: How this pipeline dials: one callable, supplied by Bridge Core, that
        #: answers *what would a call opened for this notice be opened on* — both
        #: audiences' instructions and a hand-over read from the roster as it
        #: stands right now (ADR 0017: a missed call is briefed from a fresh
        #: reading, never from replayed events).
        #:
        #: A callable rather than the parts, because the roster, the Briefing and
        #: the generated instruction sets are all the hub's, and a pipeline that
        #: held them would be a second place that knows how a call is composed.
        #: Required, with no default: a hub that generated no instructions still
        #: supplies one — it raises `CallInstructionsMissing` when asked, which
        #: is a reason the notice can carry, and is the only shape that case has.
        self._system_dial = system_dial
        self._interlock = interlock
        self._adjudicator = adjudicator

    async def escalate(self, notice: Notice) -> NoticeOutcome:
        """Take one notice out through the matrix, or drop this attempt.

        **One reach, and it is the first outlet that proves delivery.** A second
        reach existed for the retired approval announcement, which fired every
        outlet at once against a budget it was racing; with the budget gone
        (#191, ADR 0015 amended) a notice heard twice is simply worse than one
        heard once, which is what this pipeline was named for.
        """
        routes = route_matrix(
            call_is_up=self._interlock.owns_call(),
            may_touch_call=self._adjudicator.may_touch_call(),
            may_push=self._adjudicator.may_push(),
        )
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

        **Permission is read at the moment an attempt becomes irrevocable**, so
        it is re-read between routes: Duty going off while an earlier route was
        in flight halts whatever has not gone out yet, rather than forcing it
        through on a stale permission.
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

    def _still_permitted(self, route: NoticeRoute) -> bool:
        if route is NoticeRoute.PUSH_TO_CHANNEL:
            return self._adjudicator.may_push()
        return self._adjudicator.may_touch_call()

    async def _attempt(self, route: NoticeRoute, notice: Notice) -> DeliveryReceipt:
        if route is NoticeRoute.PUSH_TO_CHANNEL:
            return await self._channel.send(notice.text, request_id=notice.request_id)
        if notice.spoken is None:
            # **Neither call route carries a notice with no brief behind it.**
            # The Live Call is a Session-state surface: it is handed briefs and
            # speaks from them, and it reads no sentences (`CONTEXT.md`, *Stop
            # Notice*). Opening one would be worse than speaking into one — the
            # hand-over is read from the roster and would say nothing about the
            # news that dialled it, so the user would be called about something
            # and then told something else. The Companion Channel takes it.
            return DeliveryReceipt(
                request_id=notice.request_id,
                outcome=Delivery.FAILED,
                reason="this notice carries no Session Brief, and the call reads no sentences",
            )
        if route is NoticeRoute.OPEN_CALL_WITH_BRIEFING:
            return await self._open_holding_the_briefing(notice)
        return await self._speak(notice)

    async def _speak(self, notice: Notice) -> DeliveryReceipt:
        """Hand the brief to a call that is already up."""
        assert notice.spoken is not None  # `_attempt` refuses the route without one
        return await self._interlock.speak(notice.spoken, request_id=notice.request_id)

    async def _open_holding_the_briefing(self, notice: Notice) -> DeliveryReceipt:
        """Dial a call that comes up already holding the briefing, and say nothing after.

        **The hand-over is the announcement.** The call is opened with the
        Briefing's dial-time items in it (ADR 0018's third payload), which the
        Voice has silently and speaks from; a `speak` on top of that would hand
        the same brief twice — once as history and once as a thing to say — which
        is how a notice gets heard twice. So a call that came up is delivery, and
        the receipt says which door the brief went through.
        """
        try:
            dial = self._system_dial(notice)
        except CallInstructionsMissing as refused:
            # Nothing to open a call on. A positive reason the notice can carry
            # when this attempt is dropped.
            return DeliveryReceipt(
                request_id=notice.request_id,
                outcome=Delivery.FAILED,
                reason=str(refused),
            )

        try:
            snapshot = await self._interlock.open_call(dial)
        except SecondCallRefused:
            # A call came up while this notice was being routed. Handing the
            # brief to it is exactly what the matrix would have chosen had it
            # known — and that call was opened holding a briefing of its own.
            return await self._speak(notice)

        if not snapshot.is_up:
            return DeliveryReceipt(
                request_id=notice.request_id,
                outcome=Delivery.FAILED,
                reason=f"the call did not come up: it is {snapshot.state}",
            )
        return DeliveryReceipt(
            request_id=notice.request_id,
            outcome=Delivery.DELIVERED,
            reason="the call came up holding the hand-over",
        )

"""The Stop Notice escalation pipeline, and the route matrix it runs on.

This is where the two reference-implementation failures meet. Escalation there
opened a second call on top of a system-owned one, and delivery there was
hard-wired to one surface, so when that surface stopped answering every notice
failed. Both are policy questions, so both are answered here and nowhere else:
*which outlet does this notice go out on, given the call state and the
switches*, and *what happens to it when none of them work*.

The answer to the second one is a terminal DROPPED attempt. Bridge Core provides
no-loss by re-inspecting current Session state when an outlet transition occurs;
this pipeline never replays a historical notice.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from fakes import FakeCall, FakeCompanionChannel, dial
from gpt_voicecoding.core.adjudication import SwitchAdjudicator
from gpt_voicecoding.core.errors import CallInstructionsMissing
from gpt_voicecoding.core.escalation import (
    EscalationPipeline,
    Notice,
    NoticeRoute,
    route_matrix,
)
from gpt_voicecoding.core.interlock import CallInterlock
from gpt_voicecoding.core.lifecycle import Lifecycle
from gpt_voicecoding.core.switches import Switchboard, SwitchName
from gpt_voicecoding.seams.call import Dial, DialReason, SpokenBrief
from gpt_voicecoding.seams.delivery import Delivery
from gpt_voicecoding.seams.identity import AgentKind, SessionTarget, new_request_id

CODEX = SessionTarget(agent=AgentKind.CODEX, session_id="abc")


#: What Bridge Core hands this pipeline: how a call dialled for a notice would be
#: opened. A hand-over of one item is enough here — what the items *say* is
#: Briefing's, and this module is about which outlet a notice takes.
def system_dial(notice: Notice) -> Dial:
    return dial(DialReason(text=f"dialled about {notice.target}"))


def refusing_dial(notice: Notice) -> Dial:
    raise CallInstructionsMissing("prose for the Voice")


def brief(text: str) -> SpokenBrief:
    return SpokenBrief(
        name="repo · task",
        agent="codex",
        state="finished",
        newest=text,
        decision=(),
        answerable_here="at the terminal",
        last_activity_at="not read",
    )


def notice(text: str = "that session stopped and may need you") -> Notice:
    return Notice(request_id=new_request_id(), target=CODEX, text=text, spoken=brief(text))


class Harness:
    """A pipeline over fakes, with the switches reachable so a test can flip them."""

    def __init__(
        self,
        *,
        duty: bool = True,
        voice: bool = True,
        message: bool = True,
        call: FakeCall | None = None,
        channel: FakeCompanionChannel | None = None,
        dialling: Callable[[Notice], Dial] = system_dial,
    ) -> None:
        self.switches = Switchboard()
        self.switches.flip(SwitchName.DUTY, duty)
        self.switches.flip(SwitchName.VOICE, voice)
        self.switches.flip(SwitchName.MESSAGE, message)

        self.call = call or FakeCall()
        self.channel = channel or FakeCompanionChannel()
        self.interlock = CallInterlock(self.call)
        self.pipeline = EscalationPipeline(
            channel=self.channel,
            interlock=self.interlock,
            adjudicator=SwitchAdjudicator(self.switches),
            system_dial=dialling,
        )

    def escalate(self, item: Notice, **kwargs: object) -> object:
        return asyncio.run(self.pipeline.escalate(item, **kwargs))  # type: ignore[arg-type]


class TestTheRouteMatrix:
    """The full matrix is a pure function, so it can be read as a table."""

    def test_a_call_that_is_up_is_spoken_into_never_reopened(self) -> None:
        assert route_matrix(call_is_up=True, may_touch_call=True, may_push=True) == (
            NoticeRoute.SPEAK_INTO_CALL,
            NoticeRoute.PUSH_TO_CHANNEL,
        )

    def test_with_no_call_up_escalation_may_open_one(self) -> None:
        assert route_matrix(call_is_up=False, may_touch_call=True, may_push=True) == (
            NoticeRoute.OPEN_CALL_WITH_BRIEFING,
            NoticeRoute.PUSH_TO_CHANNEL,
        )

    def test_voice_off_and_message_on_is_the_channel_alone(self) -> None:
        assert route_matrix(call_is_up=False, may_touch_call=False, may_push=True) == (
            NoticeRoute.PUSH_TO_CHANNEL,
        )

    def test_voice_off_with_a_call_up_still_does_not_touch_the_call(self) -> None:
        """The Voice Switch is the whole call, not just opening it."""
        assert route_matrix(call_is_up=True, may_touch_call=False, may_push=True) == (
            NoticeRoute.PUSH_TO_CHANNEL,
        )

    def test_message_off_and_voice_on_never_pushes_text(self) -> None:
        assert route_matrix(call_is_up=True, may_touch_call=True, may_push=False) == (
            NoticeRoute.SPEAK_INTO_CALL,
        )

    def test_with_no_outlet_at_all_there_is_no_route(self) -> None:
        assert route_matrix(call_is_up=False, may_touch_call=False, may_push=False) == ()

    def test_with_no_call_and_message_off_the_open_call_is_the_only_route(self) -> None:
        assert route_matrix(call_is_up=False, may_touch_call=True, may_push=False) == (
            NoticeRoute.OPEN_CALL_WITH_BRIEFING,
        )


class TestSpeakingIntoTheCallThatIsUp:
    def test_a_stop_that_arrives_mid_call_opens_no_second_call(self) -> None:
        """The exact failure this pipeline exists to prevent."""
        harness = Harness()
        asyncio.run(harness.interlock.open_call(dial()))

        outcome = harness.escalate(notice("build finished"))

        assert harness.call.calls_started == 1
        assert harness.call.spoken == [brief("build finished")]
        assert outcome.state is Lifecycle.DELIVERED

    def test_a_delivered_notice_never_pushes_the_same_words_as_text(self) -> None:
        harness = Harness()
        asyncio.run(harness.interlock.open_call(dial()))

        harness.escalate(notice())

        assert harness.channel.sent == []


class TestOpeningACallToEscalateInto:
    def test_with_no_call_up_escalation_opens_one_holding_the_briefing(self) -> None:
        """The hand-over *is* the announcement, so nothing is said after opening.

        A `speak` on top of a call that came up already holding the brief would
        hand the same brief twice — once as history and once as a thing to say —
        which is a notice heard twice (#194; ADR 0018's third payload).
        """
        harness = Harness()

        outcome = harness.escalate(notice("you are needed"))

        assert harness.call.calls_started == 1
        assert harness.call.spoken == []
        assert harness.call.opened_on[0].hand_over == (DialReason(text=f"dialled about {CODEX}"),)
        assert outcome.state is Lifecycle.DELIVERED

    def test_the_opened_call_becomes_the_one_the_system_owns(self) -> None:
        harness = Harness()

        harness.escalate(notice())

        assert harness.interlock.owns_call() is True

    def test_a_call_that_never_comes_up_falls_back_to_the_channel(self) -> None:
        harness = Harness(call=FakeCall(reachable=False))

        outcome = harness.escalate(notice("you are needed"))

        assert harness.channel.sent == ["you are needed"]
        assert outcome.state is Lifecycle.DELIVERED
        assert harness.interlock.owns_call() is False


class TestWithNoInstructionsToOpenOn:
    def test_escalation_opens_no_call_and_drops_this_attempt(self) -> None:
        """The same refusal the hub meets, turned into a reason the notice carries.

        It is not raised out of the pipeline. Current-state reconciliation may
        create another notice when a later outlet transition occurs.
        """
        harness = Harness(message=False, dialling=refusing_dial)

        outcome = harness.escalate(notice())

        assert harness.call.calls_started == 0
        assert harness.call.spoken == []
        assert outcome.state is Lifecycle.DROPPED

    def test_the_reason_is_the_one_the_hub_worded(self) -> None:
        """Not this pipeline's own sentence — the one from where the dial is built."""
        harness = Harness(message=False, dialling=refusing_dial)

        outcome = harness.escalate(notice())

        assert [attempt.route for attempt in outcome.attempts] == [
            NoticeRoute.OPEN_CALL_WITH_BRIEFING
        ]
        assert outcome.attempts[0].outcome is Delivery.FAILED
        assert str(CallInstructionsMissing("prose for the Voice")) == outcome.attempts[0].reason


class TestANoticeWithNoBriefBehindIt:
    """A terminal Relay line is text, and the Live Call reads no sentences.

    `core/relays.py::terminal_line` is temporary until #197 and has no Session
    Brief behind it. Since #194 the call takes a brief and not a sentence, so a
    notice like that reaches the user through the Companion Channel.
    """

    def test_the_call_route_fails_and_the_channel_carries_it(self) -> None:
        harness = Harness()
        asyncio.run(harness.interlock.open_call(dial()))

        outcome = harness.escalate(
            Notice(request_id=new_request_id(), target=CODEX, text="relay expired: unknown")
        )

        assert harness.call.spoken == []
        assert harness.channel.sent == ["relay expired: unknown"]
        assert outcome.state is Lifecycle.DELIVERED
        assert outcome.attempts[0].outcome is Delivery.FAILED


class TestSwitchIndependence:
    def test_voice_off_and_message_on_reaches_the_user_as_text(self) -> None:
        """Messages-only is a supported state."""
        harness = Harness(voice=False)

        outcome = harness.escalate(notice("you are needed"))

        assert harness.channel.sent == ["you are needed"]
        assert harness.call.calls_started == 0
        assert outcome.state is Lifecycle.DELIVERED

    def test_message_off_and_voice_on_never_pushes_text(self) -> None:
        harness = Harness(message=False, call=FakeCall(reachable=False))

        outcome = harness.escalate(notice())

        assert harness.channel.sent == []
        assert outcome.state is Lifecycle.DROPPED

    def test_duty_off_neither_speaks_nor_pushes_nor_touches_the_call(self) -> None:
        harness = Harness(duty=False)

        outcome = harness.escalate(notice())

        assert harness.call.calls_started == 0
        assert harness.call.spoken == []
        assert harness.channel.sent == []
        assert outcome.state is Lifecycle.DROPPED

    def test_duty_flipping_off_mid_escalation_halts_the_pipeline(self) -> None:
        """Permission is re-read between routes, not decided once at the top."""
        harness = Harness(call=FakeCall(reachable=False))
        channel = harness.channel

        original = harness.call.ensure_call

        async def flip_duty_off_while_connecting(instructions: str) -> object:
            harness.switches.flip(SwitchName.DUTY, False)
            return await original(instructions)

        harness.call.ensure_call = flip_duty_off_while_connecting  # type: ignore[method-assign]

        outcome = harness.escalate(notice())

        assert channel.sent == []
        assert outcome.state is Lifecycle.DROPPED


class TestDroppedAttempts:
    def test_a_notice_with_no_outlet_is_terminal_and_not_retryable(self) -> None:
        outcome = Harness(duty=False).escalate(notice("you are needed"))

        assert outcome.state is Lifecycle.DROPPED
        assert outcome.state.is_terminal is True
        assert outcome.state.is_retryable is False
        assert outcome.attempts == ()

    def test_an_unreachable_channel_drops_this_attempt_with_the_adapter_reason(self) -> None:
        harness = Harness(
            voice=False,
            channel=FakeCompanionChannel(outcome=Delivery.FAILED, reason="chat unreachable"),
        )

        outcome = harness.escalate(notice())

        assert outcome.state is Lifecycle.DROPPED
        assert outcome.delivered_by is None
        assert [attempt.reason for attempt in outcome.attempts] == ["chat unreachable"]


class TestOneReach:
    """Every notice stops at the first outlet that proved delivery (#191).

    A second reach existed for the retired approval announcement, which fired
    every outlet at once because it was racing a budget. Both are gone: a notice
    heard twice is worse than one heard once, which is what this pipeline was
    named for, and there is no longer a caller with a reason to differ.
    """

    def test_a_notice_stops_at_the_first_outlet_that_worked(self) -> None:
        harness = Harness()

        harness.escalate(notice("stopped"))

        assert harness.call.calls_started == 1
        assert harness.channel.sent == []

    def test_with_voice_off_the_channel_is_the_first_outlet(self) -> None:
        harness = Harness(voice=False)

        outcome = harness.escalate(notice("stopped"))

        assert harness.call.spoken == []
        assert harness.channel.sent == ["stopped"]
        assert outcome.state is Lifecycle.DELIVERED

    def test_with_nothing_open_the_attempt_is_dropped(self) -> None:
        harness = Harness(duty=False)

        outcome = harness.escalate(notice())

        assert outcome.state is Lifecycle.DROPPED
        assert outcome.attempts == ()

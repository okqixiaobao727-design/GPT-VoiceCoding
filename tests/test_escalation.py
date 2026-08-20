"""The Stop Notice escalation pipeline, and the route matrix it runs on.

This is where the two reference-implementation failures meet. Escalation there
opened a second call on top of a system-owned one, and delivery there was
hard-wired to one surface, so when that surface stopped answering every notice
failed. Both are policy questions, so both are answered here and nowhere else:
*which outlet does this notice go out on, given the call state and the
switches*, and *what happens to it when none of them work*.

The answer to the second one is no-loss: a notice that could not go out is
**retained** — one `RelayKind.NOTICE` entry in the one ledger — and surfaces on
the next available outlet. There is no attempt cap, because retention is the
invariant; what stops a livelock is that attempts fire only on outlet
transitions, never on the failure of the attempt before.
"""

from __future__ import annotations

import asyncio

from fakes import FakeCall, FakeCompanionChannel
from gpt_voicecoding.core.adjudication import SwitchAdjudicator
from gpt_voicecoding.core.escalation import (
    EscalationPipeline,
    Notice,
    NoticeRoute,
    Reach,
    route_matrix,
)
from gpt_voicecoding.core.interlock import CallInterlock
from gpt_voicecoding.core.lifecycle import Lifecycle
from gpt_voicecoding.core.relay_queue import PendingRelay, RelayKind, RelayQueue
from gpt_voicecoding.core.switches import Switchboard, SwitchName
from gpt_voicecoding.seams.delivery import Delivery
from gpt_voicecoding.seams.identity import AgentKind, SessionTarget, new_request_id

CODEX = SessionTarget(agent=AgentKind.CODEX, session_id="abc")


def notice(text: str = "that session stopped and may need you") -> Notice:
    return Notice(request_id=new_request_id(), target=CODEX, text=text)


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
    ) -> None:
        self.switches = Switchboard()
        self.switches.flip(SwitchName.DUTY, duty)
        self.switches.flip(SwitchName.VOICE, voice)
        self.switches.flip(SwitchName.MESSAGE, message)

        self.call = call or FakeCall()
        self.channel = channel or FakeCompanionChannel()
        self.interlock = CallInterlock(self.call)
        self.relays = RelayQueue()
        self.pipeline = EscalationPipeline(
            call=self.call,
            channel=self.channel,
            interlock=self.interlock,
            adjudicator=SwitchAdjudicator(self.switches),
            relays=self.relays,
            clock=lambda: 1_000.0,
        )

    def escalate(self, item: Notice, **kwargs: object) -> object:
        return asyncio.run(self.pipeline.escalate(item, **kwargs))  # type: ignore[arg-type]

    def sweep(self) -> object:
        return asyncio.run(self.pipeline.sweep())


class TestTheRouteMatrix:
    """The full matrix is a pure function, so it can be read as a table."""

    def test_a_call_that_is_up_is_spoken_into_never_reopened(self) -> None:
        assert route_matrix(call_is_up=True, may_touch_call=True, may_push=True) == (
            NoticeRoute.SPEAK_INTO_CALL,
            NoticeRoute.PUSH_TO_CHANNEL,
            NoticeRoute.RETAIN,
        )

    def test_with_no_call_up_escalation_may_open_one(self) -> None:
        assert route_matrix(call_is_up=False, may_touch_call=True, may_push=True) == (
            NoticeRoute.OPEN_CALL_AND_SPEAK,
            NoticeRoute.PUSH_TO_CHANNEL,
            NoticeRoute.RETAIN,
        )

    def test_voice_off_and_message_on_is_the_channel_alone(self) -> None:
        assert route_matrix(call_is_up=False, may_touch_call=False, may_push=True) == (
            NoticeRoute.PUSH_TO_CHANNEL,
            NoticeRoute.RETAIN,
        )

    def test_voice_off_with_a_call_up_still_does_not_touch_the_call(self) -> None:
        """The Voice Switch is the whole call, not just opening it."""
        assert route_matrix(call_is_up=True, may_touch_call=False, may_push=True) == (
            NoticeRoute.PUSH_TO_CHANNEL,
            NoticeRoute.RETAIN,
        )

    def test_message_off_and_voice_on_never_pushes_text(self) -> None:
        assert route_matrix(call_is_up=True, may_touch_call=True, may_push=False) == (
            NoticeRoute.SPEAK_INTO_CALL,
            NoticeRoute.RETAIN,
        )

    def test_with_no_outlet_at_all_the_only_route_is_retention(self) -> None:
        assert route_matrix(call_is_up=False, may_touch_call=False, may_push=False) == (
            NoticeRoute.RETAIN,
        )

    def test_every_row_ends_in_retention_so_nothing_can_be_dropped(self) -> None:
        for call_is_up in (True, False):
            for may_touch_call in (True, False):
                for may_push in (True, False):
                    routes = route_matrix(
                        call_is_up=call_is_up,
                        may_touch_call=may_touch_call,
                        may_push=may_push,
                    )
                    assert routes[-1] is NoticeRoute.RETAIN
                    assert NoticeRoute.RETAIN not in routes[:-1]


class TestSpeakingIntoTheCallThatIsUp:
    def test_a_stop_that_arrives_mid_call_opens_no_second_call(self) -> None:
        """The exact failure this pipeline exists to prevent."""
        harness = Harness()
        asyncio.run(harness.interlock.open_call())

        outcome = harness.escalate(notice("build finished"))

        assert harness.call.calls_started == 1
        assert harness.call.spoken == ["build finished"]
        assert outcome.state is Lifecycle.DELIVERED

    def test_a_delivered_notice_never_pushes_the_same_words_as_text(self) -> None:
        harness = Harness()
        asyncio.run(harness.interlock.open_call())

        harness.escalate(notice())

        assert harness.channel.sent == []

    def test_a_delivered_notice_leaves_no_entry_in_the_ledger(self) -> None:
        harness = Harness()
        asyncio.run(harness.interlock.open_call())

        harness.escalate(notice())

        assert harness.relays.pending() == ()


class TestOpeningACallToEscalateInto:
    def test_with_no_call_up_escalation_opens_one_and_speaks(self) -> None:
        harness = Harness()

        outcome = harness.escalate(notice("you are needed"))

        assert harness.call.calls_started == 1
        assert harness.call.spoken == ["you are needed"]
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
        assert outcome.state is Lifecycle.RETAINED

    def test_duty_off_neither_speaks_nor_pushes_nor_touches_the_call(self) -> None:
        harness = Harness(duty=False)

        outcome = harness.escalate(notice())

        assert harness.call.calls_started == 0
        assert harness.call.spoken == []
        assert harness.channel.sent == []
        assert outcome.state is Lifecycle.RETAINED

    def test_duty_flipping_off_mid_escalation_halts_the_pipeline(self) -> None:
        """Permission is re-read between routes, not decided once at the top."""
        harness = Harness(call=FakeCall(reachable=False))
        channel = harness.channel

        original = harness.call.ensure_call

        async def flip_duty_off_while_connecting() -> object:
            harness.switches.flip(SwitchName.DUTY, False)
            return await original()

        harness.call.ensure_call = flip_duty_off_while_connecting  # type: ignore[method-assign]

        outcome = harness.escalate(notice())

        assert channel.sent == []
        assert outcome.state is Lifecycle.RETAINED


class TestRetentionAndRetry:
    def test_a_notice_with_no_outlet_is_retained_in_the_one_ledger(self) -> None:
        harness = Harness(duty=False)
        item = notice("you are needed")

        harness.escalate(item)

        (waiting,) = harness.relays.pending()
        assert waiting.request_id == item.request_id
        assert waiting.kind is RelayKind.NOTICE
        assert waiting.text == "you are needed"

    def test_a_retained_notice_is_never_marked_delivered(self) -> None:
        """An unreachable channel is a non-delivery, and stays one."""
        harness = Harness(
            voice=False,
            channel=FakeCompanionChannel(outcome=Delivery.FAILED, reason="chat unreachable"),
        )

        outcome = harness.escalate(notice())

        assert outcome.state is Lifecycle.RETAINED
        (waiting,) = harness.relays.pending()
        assert waiting.outcome.is_delivered is False

    def test_a_retained_notice_has_no_deadline_because_no_loss_has_no_cap(self) -> None:
        harness = Harness(duty=False)
        harness.escalate(notice())

        assert harness.relays.expired(now=1e18) == ()

    def test_a_retained_notice_goes_out_once_the_interlock_clears(self) -> None:
        """Retained → retried on the next outlet transition, not on a timer."""
        harness = Harness(duty=False)
        item = notice("you are needed")
        harness.escalate(item)

        harness.switches.flip(SwitchName.DUTY, True)
        outcomes = harness.sweep()

        assert harness.call.spoken == ["you are needed"]
        assert [one.state for one in outcomes] == [Lifecycle.DELIVERED]
        assert harness.relays.pending() == ()

    def test_a_sweep_with_no_outlet_open_attempts_nothing(self) -> None:
        harness = Harness(duty=False)
        harness.escalate(notice())

        assert harness.sweep() == ()
        assert harness.call.calls_started == 0

    def test_re_escalating_a_retained_notice_does_not_duplicate_the_entry(self) -> None:
        harness = Harness(duty=False)
        item = notice()

        harness.escalate(item)
        harness.escalate(item)

        assert len(harness.relays.pending()) == 1

    def test_a_notice_the_adapter_delivered_is_never_re_spoken_by_a_sweep(self) -> None:
        """The reference implementation's worst bug, made structurally impossible.

        There it graded an audibly spoken notice FAILED by matching another
        surface's records and retried it, opening duplicate calls. Here a
        DELIVERED attempt takes the entry out of the ledger, so the sweep has
        nothing to find.
        """
        harness = Harness()
        harness.escalate(notice("said once"))

        harness.sweep()
        harness.sweep()

        assert harness.call.spoken == ["said once"]

    def test_notices_are_swept_in_the_order_they_were_retained(self) -> None:
        harness = Harness(duty=False)
        harness.escalate(notice("first"))
        harness.escalate(notice("second"))

        harness.switches.flip(SwitchName.DUTY, True)
        harness.sweep()

        assert harness.call.spoken == ["first", "second"]

    def test_a_sweep_leaves_a_queued_answer_relay_alone(self) -> None:
        """This pipeline owns notices; the ledger it shares holds relays too."""
        harness = Harness()
        harness.relays.enqueue(
            PendingRelay(
                request_id=new_request_id(),
                target=CODEX,
                kind=RelayKind.ANSWER,
                text="my own words",
                queued_at=1_000.0,
                expires_at=1_600.0,
            )
        )

        harness.sweep()

        assert harness.call.spoken == []
        assert len(harness.relays.pending()) == 1


class TestTheRetryBoundary:
    def test_reporting_a_notice_failed_takes_it_out_of_the_ledger(self) -> None:
        harness = Harness(duty=False)
        item = notice()
        harness.escalate(item)

        outcome = harness.pipeline.report_failed(item.request_id)

        assert outcome.state is Lifecycle.REPORTED_FAILED
        assert harness.relays.pending() == ()

    def test_nothing_automatic_follows_a_reported_failure(self) -> None:
        """Once terminal, no retry and no substitute action — the locked rule."""
        harness = Harness(duty=False)
        item = notice()
        harness.escalate(item)
        harness.pipeline.report_failed(item.request_id)

        harness.switches.flip(SwitchName.DUTY, True)

        assert harness.sweep() == ()
        assert harness.call.spoken == []
        assert harness.channel.sent == []


class TestReach:
    def test_a_pending_approval_pushes_and_speaks_together(self) -> None:
        """The notification fires immediately, in parallel with the voice attempt."""
        harness = Harness()

        outcome = harness.escalate(notice("approval needed"), reach=Reach.EVERY_OUTLET)

        assert harness.call.spoken == ["approval needed"]
        assert harness.channel.sent == ["approval needed"]
        assert outcome.state is Lifecycle.DELIVERED

    def test_a_stop_notice_stops_at_the_first_outlet_that_worked(self) -> None:
        harness = Harness()

        harness.escalate(notice("stopped"), reach=Reach.FIRST_OUTLET)

        assert harness.call.spoken == ["stopped"]
        assert harness.channel.sent == []

    def test_every_outlet_still_means_every_outlet_the_switches_allow(self) -> None:
        harness = Harness(message=False)

        harness.escalate(notice("approval needed"), reach=Reach.EVERY_OUTLET)

        assert harness.call.spoken == ["approval needed"]
        assert harness.channel.sent == []

    def test_every_outlet_with_voice_off_is_the_channel_alone(self) -> None:
        harness = Harness(voice=False)

        harness.escalate(notice("approval needed"), reach=Reach.EVERY_OUTLET)

        assert harness.call.spoken == []
        assert harness.channel.sent == ["approval needed"]

    def test_every_outlet_with_nothing_open_retains_like_any_notice(self) -> None:
        harness = Harness(duty=False)

        outcome = harness.escalate(notice(), reach=Reach.EVERY_OUTLET)

        assert outcome.state is Lifecycle.RETAINED
        assert len(harness.relays.pending()) == 1

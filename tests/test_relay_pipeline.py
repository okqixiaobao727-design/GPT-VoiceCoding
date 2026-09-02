"""Relay queueing against the Reply Window.

The locked behaviour, and the reason for it: unsolicited user text **queues
until the Reply Window is open**, because that is what makes it arrive with the
user's authority intact. Delivering mid-turn without asking gets the words
framed as untrusted and refused — verified live. So the hub queues, and the
adapters only ever deliver.

The three numbers and rules that hang off that: a receipt that is a grade and a
reason code and never a sentence; a ten-minute ceiling and then a reported
failure; and route choice — deliver between turns versus supplement mid-turn —
that follows the user's explicit intent and is never read off what the Session
happens to be doing.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from fakes import FakeAgent
from gpt_voicecoding.core.errors import StaleSessionError, UnknownSessionError
from gpt_voicecoding.core.lifecycle import Lifecycle
from gpt_voicecoding.core.policy import CorePolicy
from gpt_voicecoding.core.relay_queue import RelayKind, RelayQueue
from gpt_voicecoding.core.relays import RelayPipeline, RelayReason, reason_for
from gpt_voicecoding.core.sessions import Session, SessionRegistry
from gpt_voicecoding.seams.agent import (
    RelayRoute,
    ReplyWindow,
    SessionInspection,
    SessionState,
    WaitingFor,
    WaitingKind,
)
from gpt_voicecoding.seams.delivery import Delivery, DeliveryReceipt
from gpt_voicecoding.seams.identity import AgentKind, RequestId, SessionName, SessionTarget

CODEX = SessionTarget(agent=AgentKind.CODEX, session_id="abc")
CLAUDE = SessionTarget(agent=AgentKind.CLAUDE, session_id="def", pid=100)

TEN_MINUTES = 600.0


class Harness:
    """A Relay pipeline over a fake agent, with a clock a test can wind forward."""

    def __init__(
        self,
        *,
        window: ReplyWindow = ReplyWindow.CLOSED,
        agent: FakeAgent | None = None,
        targets: tuple[SessionTarget, ...] = (CODEX,),
    ) -> None:
        self.now = 1_000.0
        self.sessions = SessionRegistry()
        # The Reply Window is derived from what the Session is doing, so a test
        # that wants one open says the Session is idle.
        state = SessionState.IDLE if window is ReplyWindow.OPEN else SessionState.RUNNING
        for target in targets:
            self.sessions.register(
                Session(
                    target=target,
                    name=SessionName("GPT-VoiceCoding", f"task {target.session_id}"),
                    workspace=Path("/tmp/workspace"),
                    first_seen=0.0,
                    state=state,
                )
            )
        self.agent = agent or FakeAgent(routes=frozenset(RelayRoute))
        self.relays = RelayQueue()
        self.pipeline = RelayPipeline(
            agents={AgentKind.CODEX: self.agent, AgentKind.CLAUDE: self.agent},
            sessions=self.sessions,
            relays=self.relays,
            policy=CorePolicy(),
            clock=lambda: self.now,
        )

    def relay(self, text: str = "my own words", **kwargs: object) -> object:
        return asyncio.run(self.pipeline.relay(CODEX, text, **kwargs))  # type: ignore[arg-type]

    def window_opened(self, target: SessionTarget = CODEX) -> object:
        self.sessions.set_state(target, SessionState.IDLE)
        return asyncio.run(self.pipeline.reply_window_opened(target))

    def sweep(self) -> object:
        return self.pipeline.sweep_expired()


class TestDeliveringIntoAnOpenReplyWindow:
    def test_an_open_window_takes_the_words_straight_through(self) -> None:
        harness = Harness(window=ReplyWindow.OPEN)

        outcome = harness.relay("ship it")

        assert outcome.state is Lifecycle.DELIVERED
        assert [call.text for call in harness.agent.calls] == ["ship it"]
        assert harness.relays.pending() == ()

    def test_a_delivered_relay_carries_the_attempt_that_proved_it(self) -> None:
        harness = Harness(window=ReplyWindow.OPEN)

        outcome = harness.relay()

        assert outcome.receipt is not None
        assert outcome.receipt.outcome is Delivery.DELIVERED
        assert outcome.reason is RelayReason.DELIVERED

    def test_the_users_own_words_go_by_the_answer_verb(self) -> None:
        """Which verb Bridge Core calls is how the adapter knows whose words these are."""
        harness = Harness(window=ReplyWindow.OPEN)

        harness.relay()

        assert [call.verb for call in harness.agent.calls] == ["answer_relay"]


class TestQueueingAgainstAClosedWindow:
    def test_a_closed_window_queues_rather_than_delivering(self) -> None:
        harness = Harness()

        outcome = harness.relay("ship it")

        assert outcome.state is Lifecycle.RETAINED
        assert harness.agent.calls == []

    def test_a_queued_relay_is_one_answer_entry_in_the_one_ledger(self) -> None:
        harness = Harness()

        harness.relay("ship it")

        (waiting,) = harness.relays.pending()
        assert waiting.kind is RelayKind.ANSWER
        assert waiting.text == "ship it"

    def test_the_queued_relay_carries_the_ten_minute_ceiling(self) -> None:
        harness = Harness()

        harness.relay()

        (waiting,) = harness.relays.pending()
        assert waiting.expires_at == waiting.queued_at + TEN_MINUTES

    def test_a_closed_question_route_refuses_instead_of_queueing_for_the_inbox(self) -> None:
        harness = Harness(targets=(CLAUDE,))
        harness.sessions.observed_one(
            SessionInspection(
                target=CLAUDE,
                workspace=Path("/tmp/workspace"),
                state=SessionState.WAITING,
                waiting_for=WaitingFor(
                    kind=WaitingKind.QUESTION,
                    prompt="Which base?",
                ),
            ),
            now=harness.now,
        )

        outcome = asyncio.run(harness.pipeline.relay(CLAUDE, "main"))

        assert outcome.state is Lifecycle.REPORTED_FAILED
        assert outcome.reason is RelayReason.QUESTION_UNANSWERABLE
        # Nothing went on the wire, so there is no attempt to grade. A receipt
        # here would claim an attempt that never happened.
        assert outcome.receipt is None
        assert harness.agent.calls == []
        assert harness.relays.pending() == ()

    def test_the_ceiling_is_configurable_rather_than_baked_in(self) -> None:
        harness = Harness()
        harness.pipeline = RelayPipeline(
            agents={AgentKind.CODEX: harness.agent},
            sessions=harness.sessions,
            relays=harness.relays,
            policy=CorePolicy(relay_ceiling_seconds=30.0),
            clock=lambda: harness.now,
        )

        harness.relay()

        (waiting,) = harness.relays.pending()
        assert waiting.expires_at == waiting.queued_at + 30.0

    def test_the_open_window_releases_it_and_the_adapter_delivers(self) -> None:
        harness = Harness()
        harness.relay("ship it")

        outcomes = harness.window_opened()

        assert [call.text for call in harness.agent.calls] == ["ship it"]
        assert [one.state for one in outcomes] == [Lifecycle.DELIVERED]
        assert harness.relays.pending() == ()

    def test_queued_relays_reach_the_session_in_the_order_they_were_said(self) -> None:
        harness = Harness()
        harness.relay("first")
        harness.relay("second")

        harness.window_opened()

        assert [call.text for call in harness.agent.calls] == ["first", "second"]

    def test_another_sessions_window_opening_releases_nothing_here(self) -> None:
        harness = Harness(targets=(CODEX, CLAUDE))
        harness.relay("for codex")

        harness.window_opened(CLAUDE)

        assert harness.agent.calls == []
        assert len(harness.relays.pending()) == 1


class TestTheReceiptIsAGradeAndAReason:
    """The receipt is `RelayOutcome` itself: a state, a graded attempt, a code.

    No sentence anywhere in it. The words the user hears are the Voice's to
    compose from these facts (#175), and a sentence composed here would be a
    second renderer for words the model rewrites anyway.
    """

    def test_a_queued_relay_says_what_it_is_waiting_for(self) -> None:
        harness = Harness()

        outcome = harness.relay()

        assert outcome.state is Lifecycle.RETAINED
        assert outcome.reason is RelayReason.AWAITING_REPLY_WINDOW

    def test_words_that_never_went_carry_no_grade_at_all(self) -> None:
        """ "Not attempted" is the absent attempt, never `UNKNOWN`."""
        harness = Harness()

        assert harness.relay().receipt is None

    def test_every_grade_maps_to_exactly_one_reason_code(self) -> None:
        """Total, and a function: one grade never produces two codes."""
        grades: list[Delivery | None] = [None, *Delivery]

        codes = {
            grade: reason_for(
                None
                if grade is None
                else DeliveryReceipt(request_id=RequestId("r"), outcome=grade, reason="because")
            )
            for grade in grades
        }

        assert codes == {
            None: RelayReason.AWAITING_REPLY_WINDOW,
            Delivery.FAILED: RelayReason.AWAITING_REPLY_WINDOW,
            Delivery.UNKNOWN: RelayReason.DUPLICATE_RISK,
            Delivery.HELD: RelayReason.HELD_FAR_SIDE,
            Delivery.DELIVERED: RelayReason.DELIVERED,
        }

    def test_the_delivery_that_follows_is_graded_rather_than_announced(self) -> None:
        """The flush says what the attempt proved, and says nothing to the user."""
        harness = Harness()
        harness.relay()

        outcomes = harness.window_opened()

        assert [one.state for one in outcomes] == [Lifecycle.DELIVERED]
        assert [one.reason for one in outcomes] == [RelayReason.DELIVERED]


class TestTheRouteFollowsTheUsersIntent:
    def test_supplement_goes_mid_turn_without_waiting_for_the_window(self) -> None:
        """ "The agent is working and I want to add something" — authority intact."""
        harness = Harness()

        outcome = harness.relay("also check the tests", route=RelayRoute.SUPPLEMENT)

        assert outcome.state is Lifecycle.DELIVERED
        assert [call.route for call in harness.agent.calls] == [RelayRoute.SUPPLEMENT]

    def test_the_same_closed_window_carries_both_intents(self) -> None:
        """The route is the user's word, never read off the Session's status."""
        harness = Harness()

        waited = harness.relay("this can wait")
        added = harness.relay("add this now", route=RelayRoute.SUPPLEMENT)

        assert waited.state is Lifecycle.RETAINED
        assert added.state is Lifecycle.DELIVERED

    def test_an_adapter_without_supplement_queues_it_as_a_deliver(self) -> None:
        """The adapter says what it has; deciding what to do instead is policy."""
        harness = Harness(agent=FakeAgent(routes=frozenset({RelayRoute.DELIVER})))

        outcome = harness.relay("add this now", route=RelayRoute.SUPPLEMENT)

        assert outcome.state is Lifecycle.RETAINED
        assert harness.agent.calls == []
        (waiting,) = harness.relays.pending()
        assert waiting.route is RelayRoute.DELIVER

    def test_the_downgraded_relay_delivers_as_a_deliver_when_the_window_opens(self) -> None:
        harness = Harness(agent=FakeAgent(routes=frozenset({RelayRoute.DELIVER})))
        harness.relay("add this now", route=RelayRoute.SUPPLEMENT)

        harness.window_opened()

        assert [call.route for call in harness.agent.calls] == [RelayRoute.DELIVER]


class TestNonDelivery:
    def test_the_engine_log_keeps_the_adapters_failure_reason(self, caplog) -> None:
        """#39: the next diagnosis must not need `lsof` to recover this reason."""
        caplog.set_level("INFO", logger="gpt_voicecoding.core.relays")
        harness = Harness(
            window=ReplyWindow.OPEN,
            agent=FakeAgent(outcome=Delivery.FAILED, reason="the far side is gone"),
        )

        harness.relay("ship it")

        assert any("the far side is gone" in record.getMessage() for record in caplog.records)

    def test_a_failed_retry_logs_the_adapters_reason(self, caplog) -> None:
        """#39: the Reply Window flush must leave the same diagnostic evidence."""
        caplog.set_level("INFO", logger="gpt_voicecoding.core.relays")
        harness = Harness()
        harness.relay("ship it")
        harness.agent.outcome = Delivery.FAILED
        harness.agent.reason = "the retried connection is gone"

        harness.window_opened()

        assert any(
            "the retried connection is gone" in record.getMessage() for record in caplog.records
        )

    def test_an_attempt_that_proves_nothing_keeps_the_words_queued(self) -> None:
        """UNKNOWN is not delivered, and the user's own words are not lost."""
        harness = Harness(
            window=ReplyWindow.OPEN,
            agent=FakeAgent(outcome=Delivery.UNKNOWN, reason="no readback"),
        )

        outcome = harness.relay("ship it")

        assert outcome.state is Lifecycle.RETAINED
        assert len(harness.relays.pending()) == 1

    def test_a_held_relay_waits_rather_than_being_reported_delivered(self) -> None:
        harness = Harness(
            window=ReplyWindow.OPEN,
            agent=FakeAgent(outcome=Delivery.HELD, reason="parked in front of the human"),
        )

        assert harness.relay().state is Lifecycle.RETAINED

    def test_a_failed_attempt_does_not_immediately_try_again(self) -> None:
        """Retry rides the next window transition, never the failure itself."""
        harness = Harness(
            window=ReplyWindow.OPEN,
            agent=FakeAgent(outcome=Delivery.FAILED, reason="the far side is gone"),
        )

        harness.relay("ship it")

        assert len(harness.agent.calls) == 1


class TestTheTenMinuteCeiling:
    def test_nothing_expires_before_the_ceiling(self) -> None:
        harness = Harness()
        harness.relay()

        harness.now += TEN_MINUTES - 1

        assert harness.sweep() == ()
        assert len(harness.relays.pending()) == 1

    def test_at_the_ceiling_the_relay_becomes_a_reported_failure(self) -> None:
        harness = Harness()
        harness.relay("ship it")

        harness.now += TEN_MINUTES
        (outcome,) = harness.sweep()

        assert outcome.state is Lifecycle.REPORTED_FAILED
        assert outcome.reason is RelayReason.CEILING_PASSED

    def test_an_expired_relay_leaves_the_ledger_so_nothing_can_retry_it(self) -> None:
        harness = Harness()
        harness.relay()
        harness.now += TEN_MINUTES
        harness.sweep()

        harness.window_opened()

        assert harness.agent.calls == []
        assert harness.relays.pending() == ()

    def test_a_relay_is_reported_failed_exactly_once(self) -> None:
        harness = Harness()
        harness.relay()
        harness.now += TEN_MINUTES

        assert len(harness.sweep()) == 1
        assert harness.sweep() == ()


class TestFailingClosedOnTheTarget:
    def test_an_unknown_session_is_refused_rather_than_queued(self) -> None:
        harness = Harness()
        gone = SessionTarget(agent=AgentKind.CODEX, session_id="never-registered")

        with pytest.raises(UnknownSessionError):
            asyncio.run(harness.pipeline.relay(gone, "ship it"))

        assert harness.relays.pending() == ()

    def test_an_ended_session_is_refused_as_stale(self) -> None:
        harness = Harness()
        harness.sessions.mark_ended(CODEX)

        with pytest.raises(StaleSessionError):
            harness.relay()

    def test_a_session_that_ends_drops_the_words_still_waiting_for_it(self) -> None:
        harness = Harness()
        harness.relay("ship it")

        dropped = harness.pipeline.session_ended(CODEX)

        assert [one.state for one in dropped] == [Lifecycle.REPORTED_FAILED]
        assert harness.relays.pending() == ()


class TestDuplicateSafety:
    """P9: a second attempt is permitted only for **proven** non-delivery.

    The rule is the reference implementation's, and the 539 lines behind it prove
    it rather than authorise porting them (`legacy@1d32845:bridge/delivery.py:
    28-75`; `legacy@1d32845:bridge/coordinator.py:1075-1109`;
    `legacy@1d32845:bridge/store.py:964-1035,3394-3555,3653-3874`): the ledger
    recorded a terminal grade, permitted a new attempt only where non-delivery
    was proven, and **never** turned indeterminate into an automatic resend.
    Ported whole; its storage is left behind (#61 R1).

    v1 enqueued every non-delivered outcome and retried all of them on the next
    Reply Window, which is exactly how the reference implementation produced
    duplicates before it learned this rule. #71 makes it concrete on the Claude
    lane: an accepted socket write proves nothing, so an `UNKNOWN` there is a
    Relay that very likely *did* arrive.
    """

    def unknown(self) -> Harness:
        return Harness(
            window=ReplyWindow.OPEN,
            agent=FakeAgent(outcome=Delivery.UNKNOWN, reason="no readback"),
        )

    def held(self) -> Harness:
        return Harness(
            window=ReplyWindow.OPEN,
            agent=FakeAgent(outcome=Delivery.HELD, reason="parked in front of the human"),
        )

    def test_an_unknown_attempt_is_never_sent_again_on_the_next_window(self) -> None:
        harness = self.unknown()
        harness.relay("ship it")

        harness.window_opened()

        assert len(harness.agent.calls) == 1

    def test_a_held_relay_is_never_sent_again_either(self) -> None:
        """It is parked in front of a person and will settle. Sending twice duplicates it."""
        harness = self.held()
        harness.relay("ship it")

        harness.window_opened()

        assert len(harness.agent.calls) == 1

    def test_a_proven_failure_is_tried_again_when_the_window_opens(self) -> None:
        """Proven non-delivery carries no duplicate risk, so the ceiling policy applies."""
        harness = Harness(
            window=ReplyWindow.OPEN,
            agent=FakeAgent(outcome=Delivery.FAILED, reason="the far side is gone"),
        )
        harness.relay("ship it")

        harness.window_opened()

        assert len(harness.agent.calls) == 2

    def test_words_that_were_never_attempted_are_delivered_as_before(self) -> None:
        """Nothing went on the wire, so there is nothing that could arrive twice."""
        harness = Harness()
        harness.relay("ship it")

        harness.window_opened()

        assert len(harness.agent.calls) == 1
        assert harness.relays.pending() == ()

    def test_the_unknown_relay_is_kept_rather_than_dropped(self) -> None:
        """Retained as duplicate-risk information: the user may still authorise another."""
        harness = self.unknown()

        harness.relay("ship it")
        harness.window_opened()

        assert [waiting.text for waiting in harness.relays.pending()] == ["ship it"]

    def test_an_unproven_attempt_is_graded_and_coded_as_a_duplicate_risk(self) -> None:
        harness = self.unknown()

        outcome = harness.relay("ship it")

        assert outcome.receipt is not None
        assert outcome.receipt.outcome is Delivery.UNKNOWN
        assert outcome.reason is RelayReason.DUPLICATE_RISK

    def test_a_held_relay_is_coded_as_parked_rather_than_waiting_for_a_turn(self) -> None:
        harness = self.held()

        outcome = harness.relay("ship it")

        assert outcome.reason is RelayReason.HELD_FAR_SIDE

    def test_a_proven_failure_is_coded_as_waiting_for_the_next_window(self) -> None:
        harness = Harness(
            window=ReplyWindow.OPEN,
            agent=FakeAgent(outcome=Delivery.FAILED, reason="the far side is gone"),
        )

        outcome = harness.relay("ship it")

        assert outcome.reason is RelayReason.AWAITING_REPLY_WINDOW
        assert outcome.receipt is not None
        assert outcome.receipt.outcome is Delivery.FAILED

    def test_the_user_may_authorise_another_attempt_by_saying_it_again(self) -> None:
        """The explicit authority P9 asks for is the user relaying the words again."""
        harness = self.unknown()
        harness.relay("ship it")

        harness.relay("ship it")

        assert len(harness.agent.calls) == 2

    def test_an_unknown_relay_that_is_later_proven_delivered_leaves_the_ledger(self) -> None:
        """The receipt that arrives late is the other way a duplicate is avoided."""
        harness = self.unknown()
        outcome = harness.relay("ship it")

        harness.relays.classify(
            outcome.request_id,
            DeliveryReceipt(request_id=outcome.request_id, outcome=Delivery.DELIVERED),
        )

        assert harness.relays.pending() == ()


class TestWhatATerminalRelaySaysAboutAnUnprovenAttempt:
    """The reason code says what happened here; the grade says what was proved.

    The two used to be one sentence, in two spellings each, because a rendered
    sentence may not claim non-delivery of an `UNKNOWN` — the grade that means
    the far side may well have the words. Splitting them makes the pair
    unnecessary: `ceiling_passed` is a fact about this system's ceiling and
    claims nothing about arrival, and the attempt's grade travels beside it.
    """

    def unproven(self) -> Harness:
        return Harness(
            window=ReplyWindow.OPEN,
            agent=FakeAgent(outcome=Delivery.UNKNOWN, reason="no readback"),
        )

    def test_a_terminal_relay_keeps_the_grade_of_the_attempt_it_made(self) -> None:
        harness = self.unproven()
        harness.relay("ship it")

        harness.now += TEN_MINUTES
        (outcome,) = harness.sweep()

        assert outcome.state is Lifecycle.REPORTED_FAILED
        assert outcome.reason is RelayReason.CEILING_PASSED
        assert outcome.receipt is not None
        assert outcome.receipt.outcome is Delivery.UNKNOWN
        # The adapter's own evidence survives the ceiling rather than being
        # rewritten into a sentence about it.
        assert outcome.receipt.reason == "no readback"

    def test_words_that_never_went_reach_the_ceiling_with_no_grade(self) -> None:
        harness = Harness()
        harness.relay("ship it")

        harness.now += TEN_MINUTES
        (outcome,) = harness.sweep()

        assert outcome.reason is RelayReason.CEILING_PASSED
        assert outcome.receipt is None

    def test_a_proven_failure_reaches_the_ceiling_under_the_same_code(self) -> None:
        harness = Harness(
            window=ReplyWindow.OPEN,
            agent=FakeAgent(outcome=Delivery.FAILED, reason="the far side is gone"),
        )
        harness.relay("ship it")

        harness.now += TEN_MINUTES
        (outcome,) = harness.sweep()

        assert outcome.reason is RelayReason.CEILING_PASSED
        assert outcome.receipt is not None
        assert outcome.receipt.outcome is Delivery.FAILED

    def test_a_session_that_ends_under_an_unproven_relay_keeps_that_grade(self) -> None:
        harness = self.unproven()
        harness.relay("ship it")

        (outcome,) = harness.pipeline.session_ended(CODEX)

        assert outcome.reason is RelayReason.SESSION_ENDED
        assert outcome.receipt is not None
        assert outcome.receipt.outcome is Delivery.UNKNOWN

    def test_a_session_that_ends_under_words_that_never_went_carries_no_grade(self) -> None:
        harness = Harness()
        harness.relay("ship it")

        (outcome,) = harness.pipeline.session_ended(CODEX)

        assert outcome.reason is RelayReason.SESSION_ENDED
        assert outcome.receipt is None

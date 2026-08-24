"""Relay queueing against the Reply Window.

The locked behaviour, and the reason for it: unsolicited user text **queues
until the Reply Window is open**, because that is what makes it arrive with the
user's authority intact. Delivering mid-turn without asking gets the words
framed as untrusted and refused — verified live. So the hub queues, and the
adapters only ever deliver.

The three numbers and rules that hang off that: one confirmation on receipt and
never a second announcement on delivery; a ten-minute ceiling and then a
reported failure; and route choice — deliver between turns versus supplement
mid-turn — that follows the user's explicit intent and is never read off what
the Session happens to be doing.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from fakes import FakeAgent
from gpt_voicecoding.core.errors import StaleSessionError, UnknownSessionError
from gpt_voicecoding.core.escalation import NO_DEADLINE
from gpt_voicecoding.core.lifecycle import Lifecycle
from gpt_voicecoding.core.policy import CorePolicy
from gpt_voicecoding.core.relay_queue import PendingRelay, RelayKind, RelayQueue
from gpt_voicecoding.core.relays import RelayPipeline
from gpt_voicecoding.core.sessions import Session, SessionRegistry
from gpt_voicecoding.seams.agent import RelayRoute, ReplyWindow
from gpt_voicecoding.seams.delivery import Delivery
from gpt_voicecoding.seams.identity import AgentKind, SessionLabel, SessionTarget, new_request_id

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
        for target in targets:
            self.sessions.register(
                Session(
                    target=target,
                    label=SessionLabel("GPT-VoiceCoding", f"task {target.session_id}"),
                    workspace=Path("/tmp/workspace"),
                    registered_at=0.0,
                    reply_window=window,
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
        self.sessions.set_reply_window(target, ReplyWindow.OPEN)
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

    def test_a_delivered_relay_is_never_confirmed_because_it_did_not_wait(self) -> None:
        harness = Harness(window=ReplyWindow.OPEN)

        assert harness.relay().confirmation == ""

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


class TestConfirmingExactlyOnce:
    def test_a_queued_relay_is_confirmed_on_receipt(self) -> None:
        harness = Harness()

        outcome = harness.relay()

        assert outcome.confirmation

    def test_delivery_announces_nothing_a_second_time(self) -> None:
        """ "Got it, it'll go when this turn ends" — and then silence."""
        harness = Harness()
        harness.relay()

        outcomes = harness.window_opened()

        assert [one.confirmation for one in outcomes] == [""]


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
        assert outcome.report

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

    def test_the_ceiling_never_touches_a_retained_stop_notice(self) -> None:
        """A notice has no deadline at all; only a queued Relay has a ceiling."""
        harness = Harness()
        harness.relays.enqueue(
            PendingRelay(
                request_id=new_request_id(),
                target=CODEX,
                kind=RelayKind.NOTICE,
                text="that session stopped and may need you",
                queued_at=harness.now,
                expires_at=NO_DEADLINE,
            )
        )
        harness.relay("ship it")

        harness.now += TEN_MINUTES
        (outcome,) = harness.sweep()

        assert outcome.state is Lifecycle.REPORTED_FAILED
        assert [waiting.kind for waiting in harness.relays.pending()] == [RelayKind.NOTICE]


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

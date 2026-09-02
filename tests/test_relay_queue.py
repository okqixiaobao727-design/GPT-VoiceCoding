"""The undelivered Answer Relay queue.

Stop Notices are reconstructed from current Session state and never enter this
queue. A delivered Answer Relay leaves the queue, so "graded FAILED after being
delivered, then retried" cannot be built.

The 10-minute ceiling itself is policy and belongs to the pipelines issue; the
queue only holds the deadline it is given.
"""

from __future__ import annotations

import pytest

from gpt_voicecoding.core.errors import DuplicateRelayError, UnknownRelayError
from gpt_voicecoding.core.relay_queue import PendingRelay, RelayKind, RelayQueue
from gpt_voicecoding.seams.agent import RelayRoute
from gpt_voicecoding.seams.delivery import Delivery, DeliveryReceipt
from gpt_voicecoding.seams.identity import AgentKind, RequestId, SessionTarget, new_request_id

CODEX = SessionTarget(agent=AgentKind.CODEX, session_id="abc")
CLAUDE = SessionTarget(agent=AgentKind.CLAUDE, session_id="def", pid=100)

TEN_MINUTES = 600.0


def answer(
    target: SessionTarget = CODEX,
    *,
    request_id: RequestId | None = None,
    queued_at: float = 1_000.0,
    text: str = "yes, go ahead",
    route: RelayRoute = RelayRoute.DELIVER,
) -> PendingRelay:
    return PendingRelay(
        request_id=request_id or new_request_id(),
        target=target,
        kind=RelayKind.ANSWER,
        text=text,
        queued_at=queued_at,
        expires_at=queued_at + TEN_MINUTES,
        route=route,
    )


def graded(request_id: RequestId, outcome: Delivery) -> DeliveryReceipt:
    """What an attempt proved, with the evidence a non-delivered grade owes."""
    return DeliveryReceipt(
        request_id=request_id,
        outcome=outcome,
        reason="" if outcome.is_delivered else "the fake adapter said so",
    )


class TestTheOneLedger:
    def test_the_queue_holds_only_user_answer_relays(self) -> None:
        """An Approval Relay has a budget and a fallback; it never waits here."""
        assert {member.value for member in RelayKind} == {"answer"}

    def test_entries_come_back_in_the_order_they_arrived(self) -> None:
        queue = RelayQueue()
        first = queue.enqueue(answer(queued_at=1.0))
        second = queue.enqueue(answer(queued_at=2.0))
        third = queue.enqueue(answer(queued_at=3.0))
        assert queue.pending() == (first, second, third)


class TestEnqueueing:
    def test_one_request_id_may_only_be_queued_once(self) -> None:
        queue = RelayQueue()
        request_id = new_request_id()
        queue.enqueue(answer(request_id=request_id))
        with pytest.raises(DuplicateRelayError):
            queue.enqueue(answer(request_id=request_id))

    def test_an_entry_starts_ungraded_because_nothing_has_been_attempted(self) -> None:
        """`None`, not `UNKNOWN` — P9 turns on telling those two apart.

        `UNKNOWN` is a positive observation: something went on the wire and
        proved nothing, so sending it again risks a duplicate. An entry queued
        against a closed Reply Window went nowhere and carries no such risk.
        """
        queue = RelayQueue()
        queued = queue.enqueue(answer())
        assert queued.receipt is None

    def test_something_already_delivered_cannot_wait_in_the_undelivered_queue(self) -> None:
        queue = RelayQueue()
        already = PendingRelay(
            request_id=new_request_id(),
            target=CODEX,
            kind=RelayKind.ANSWER,
            text="yes, go ahead",
            queued_at=1_000.0,
            expires_at=1_600.0,
            receipt=graded(RequestId("r-1"), Delivery.DELIVERED),
        )
        with pytest.raises(ValueError):
            queue.enqueue(already)
        assert queue.pending() == ()

    def test_a_deadline_before_the_queueing_moment_is_refused(self) -> None:
        with pytest.raises(ValueError):
            PendingRelay(
                request_id=new_request_id(),
                target=CODEX,
                kind=RelayKind.ANSWER,
                text="hello",
                queued_at=1_000.0,
                expires_at=999.0,
            )

    def test_empty_text_is_refused(self) -> None:
        with pytest.raises(ValueError):
            answer(text="   ")

    def test_an_answer_may(self) -> None:
        assert answer(route=RelayRoute.SUPPLEMENT).route is RelayRoute.SUPPLEMENT


class TestClassifying:
    def test_a_delivered_entry_leaves_the_queue_and_can_never_be_retried(self) -> None:
        """The reference implementation graded a spoken notice FAILED and retried it."""
        queue = RelayQueue()
        queued = queue.enqueue(answer())

        released = queue.classify(queued.request_id, graded(queued.request_id, Delivery.DELIVERED))

        assert released.receipt is not None
        assert released.receipt.outcome is Delivery.DELIVERED
        assert queue.pending() == ()
        with pytest.raises(UnknownRelayError):
            queue.classify(queued.request_id, graded(queued.request_id, Delivery.FAILED))

    def test_a_held_entry_stays_pending_and_is_never_delivered(self) -> None:
        queue = RelayQueue()
        queued = queue.enqueue(answer())
        held = queue.classify(queued.request_id, graded(queued.request_id, Delivery.HELD))

        assert held.receipt is not None
        assert held.receipt.outcome is Delivery.HELD
        assert held.receipt.is_delivered is False
        # The attempt's own evidence is kept, so a terminal outcome can carry it
        # rather than have a sentence written about it later.
        assert held.receipt.reason == "the fake adapter said so"
        assert queue.pending() == (held,)

    def test_a_failed_entry_stays_until_something_releases_it(self) -> None:
        """Whether a failure is terminal is the pipelines issue's call, not the queue's."""
        queue = RelayQueue()
        queued = queue.enqueue(answer())
        failed = queue.classify(queued.request_id, graded(queued.request_id, Delivery.FAILED))
        assert queue.pending() == (failed,)

    def test_classifying_something_never_queued_fails_closed(self) -> None:
        queue = RelayQueue()
        with pytest.raises(UnknownRelayError):
            queue.classify(new_request_id(), graded(RequestId("r-1"), Delivery.DELIVERED))


class TestReleasing:
    def test_releasing_takes_an_entry_out(self) -> None:
        queue = RelayQueue()
        queued = queue.enqueue(answer())
        assert queue.release(queued.request_id) == queued
        assert queue.pending() == ()

    def test_releasing_twice_fails_closed(self) -> None:
        queue = RelayQueue()
        queued = queue.enqueue(answer())
        queue.release(queued.request_id)
        with pytest.raises(UnknownRelayError):
            queue.release(queued.request_id)

    def test_everything_for_an_ended_session_can_be_dropped_at_once(self) -> None:
        queue = RelayQueue()
        doomed = queue.enqueue(answer(CODEX))
        survivor = queue.enqueue(answer(CLAUDE))

        assert queue.drop_for(CODEX) == (doomed,)
        assert queue.pending() == (survivor,)


class TestSelecting:
    def test_pending_for_one_session_keeps_arrival_order(self) -> None:
        queue = RelayQueue()
        first = queue.enqueue(answer(CODEX, queued_at=1.0))
        queue.enqueue(answer(CLAUDE, queued_at=2.0))
        second = queue.enqueue(answer(CODEX, queued_at=3.0))
        assert queue.pending_for(CODEX) == (first, second)

    def test_a_fork_does_not_inherit_the_other_pids_queue(self) -> None:
        queue = RelayQueue()
        queue.enqueue(answer(CLAUDE))
        fork = SessionTarget(agent=AgentKind.CLAUDE, session_id="def", pid=101)
        assert queue.pending_for(fork) == ()

    def test_expired_entries_are_the_ones_past_their_deadline(self) -> None:
        queue = RelayQueue()
        old = queue.enqueue(answer(queued_at=0.0))
        queue.enqueue(answer(queued_at=1_000.0))

        assert queue.expired(now=TEN_MINUTES) == (old,)

    def test_an_entry_is_not_expired_a_moment_before_its_deadline(self) -> None:
        queue = RelayQueue()
        queue.enqueue(answer(queued_at=0.0))
        assert queue.expired(now=TEN_MINUTES - 0.001) == ()

    def test_expiring_reports_but_does_not_remove(self) -> None:
        """What to do about an expiry is policy; the queue only answers the question."""
        queue = RelayQueue()
        queue.enqueue(answer(queued_at=0.0))
        queue.expired(now=TEN_MINUTES)
        assert len(queue) == 1

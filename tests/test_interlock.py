"""One call at a time — the system-level invariant that sits above the Call seam.

The defect this exists to make impossible: the reference implementation's
escalation path pressed the GUI toggle while the system already owned a call,
and two assistants on shared speakers talked to each other in an unbounded loop.

So the invariant is not "the adapter is idempotent" — it is that *nothing may
decide to open a voice surface while the system owns one*. The interlock is the
only door to opening a call, and it refuses rather than quietly returning the
call that is already up: a caller that meant to open one has a different plan to
make when one is already there, and hiding that is how the loop got built.
"""

from __future__ import annotations

import asyncio

import pytest

from fakes import HOUSE_RULES, FakeCall
from gpt_voicecoding.core.errors import SecondCallRefused
from gpt_voicecoding.core.interlock import CallInterlock
from gpt_voicecoding.seams.call import CallState


def interlock(call: FakeCall | None = None) -> tuple[CallInterlock, FakeCall]:
    fake = call or FakeCall()
    return CallInterlock(fake), fake


class TestOwnership:
    def test_the_system_owns_no_call_to_begin_with(self) -> None:
        guard, _ = interlock()

        assert guard.owns_call() is False
        assert guard.call_id() is None

    def test_opening_a_call_takes_ownership_of_it(self) -> None:
        guard, call = interlock()

        snapshot = asyncio.run(guard.open_call(HOUSE_RULES))

        assert snapshot.state is CallState.UP
        assert guard.owns_call() is True
        assert guard.call_id() == snapshot.call_id

    def test_ending_a_call_releases_ownership(self) -> None:
        guard, _ = interlock()
        asyncio.run(guard.open_call(HOUSE_RULES))

        asyncio.run(guard.end_call())

        assert guard.owns_call() is False
        assert guard.call_id() is None

    def test_a_call_that_never_came_up_is_not_owned(self) -> None:
        """CONNECTING is not UP. Claiming it would bar the retry that fixes it."""
        guard, call = interlock(FakeCall(reachable=False))

        snapshot = asyncio.run(guard.open_call(HOUSE_RULES))

        assert snapshot.state is not CallState.UP
        assert guard.owns_call() is False


class TestTheInvariant:
    def test_opening_a_second_call_is_refused(self) -> None:
        guard, call = interlock()
        asyncio.run(guard.open_call(HOUSE_RULES))

        with pytest.raises(SecondCallRefused) as refusal:
            asyncio.run(guard.open_call(HOUSE_RULES))

        assert refusal.value.call_id == guard.call_id()

    def test_the_refusal_never_reaches_the_adapter(self) -> None:
        """The adapter neither knows nor enforces this rule (ADR 0001)."""
        guard, call = interlock()
        asyncio.run(guard.open_call(HOUSE_RULES))

        with pytest.raises(SecondCallRefused):
            asyncio.run(guard.open_call(HOUSE_RULES))

        assert call.calls_started == 1

    def test_ending_a_call_makes_opening_the_next_one_legal_again(self) -> None:
        guard, call = interlock()
        asyncio.run(guard.open_call(HOUSE_RULES))
        asyncio.run(guard.end_call())

        asyncio.run(guard.open_call(HOUSE_RULES))

        assert call.calls_started == 2


class TestWhatTheCallSeamReportsUpward:
    def test_a_call_the_user_started_is_adopted_as_the_system_owned_one(self) -> None:
        """One voice surface means one, whoever pressed the toggle."""
        guard, _ = interlock()

        guard.note_started("call-7")

        assert guard.owns_call() is True
        assert guard.call_id() == "call-7"

    def test_a_dropped_call_releases_ownership_so_escalation_may_open_one(self) -> None:
        guard, _ = interlock()
        asyncio.run(guard.open_call(HOUSE_RULES))
        held = guard.call_id()
        assert held is not None

        guard.note_ended(held)

        assert guard.owns_call() is False

    def test_an_end_reported_for_some_other_call_does_not_release_this_one(self) -> None:
        """A late event about a finished call must not unlock the live one."""
        guard, _ = interlock()
        asyncio.run(guard.open_call(HOUSE_RULES))

        assert guard.note_ended("call-from-last-week") is False
        assert guard.owns_call() is True

    def test_ending_a_call_is_idempotent_from_upward_events(self) -> None:
        guard, _ = interlock()

        assert guard.note_ended("call-1") is False
        assert guard.owns_call() is False

    def test_only_a_real_release_reports_the_transition(self) -> None:
        """Callers sweep on this answer, so a stale event must not claim one."""
        guard, _ = interlock()
        asyncio.run(guard.open_call(HOUSE_RULES))
        held = guard.call_id()
        assert held is not None

        assert guard.note_ended(held) is True
        assert guard.note_ended(held) is False

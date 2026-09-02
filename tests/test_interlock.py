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
import inspect
from pathlib import Path

import pytest

from fakes import FakeCall, dial
from gpt_voicecoding.core.errors import SecondCallRefused
from gpt_voicecoding.core.interlock import CallInterlock
from gpt_voicecoding.seams.call import CallState, Dial, SpokenBrief
from gpt_voicecoding.seams.identity import new_request_id

STOPPED = SpokenBrief(
    name="repo · task",
    agent="claude",
    state="finished",
    newest="that session stopped",
    decision=(),
    answerable_here="from here",
    last_activity_at="not read",
)


def interlock(call: FakeCall | None = None) -> tuple[CallInterlock, FakeCall]:
    fake = call or FakeCall()
    return CallInterlock(fake), fake


async def open_and_report_started(guard: CallInterlock) -> None:
    snapshot = await guard.open_call(dial())
    assert snapshot.call_id is not None
    guard.note_started(snapshot.call_id)


class YieldingSpeakCall(FakeCall):
    def __init__(self) -> None:
        super().__init__()
        self.speak_entered = asyncio.Event()
        self.release_speak = asyncio.Event()

    async def speak(self, brief, *, request_id):
        self.speak_entered.set()
        await self.release_speak.wait()
        return await super().speak(brief, request_id=request_id)


class TestOwnership:
    def test_the_system_owns_no_call_to_begin_with(self) -> None:
        guard, _ = interlock()

        assert guard.owns_call() is False
        assert guard.call_id() is None

    def test_opening_a_call_takes_ownership_of_it(self) -> None:
        guard, call = interlock()

        snapshot = asyncio.run(guard.open_call(dial()))

        assert snapshot.state is CallState.UP
        assert guard.owns_call() is True
        assert guard.call_id() == snapshot.call_id

    def test_ending_a_call_releases_ownership(self) -> None:
        guard, _ = interlock()
        asyncio.run(guard.open_call(dial()))

        asyncio.run(guard.end_call())

        assert guard.owns_call() is False
        assert guard.call_id() is None

    def test_a_call_that_never_came_up_is_not_owned(self) -> None:
        """CONNECTING is not UP. Claiming it would bar the retry that fixes it."""
        guard, call = interlock(FakeCall(reachable=False))

        snapshot = asyncio.run(guard.open_call(dial()))

        assert snapshot.state is not CallState.UP
        assert guard.owns_call() is False


class TestTheSilenceCeiling:
    def test_an_end_never_lands_in_the_middle_of_a_notice_being_spoken(self) -> None:
        """Speaking and ending are one operation, so neither sees the other half-done.

        The stamp is gone (#184) but the serialisation is not: a call ended
        while `speak` is still inside the adapter is a notice delivered into a
        surface that is being torn down under it.
        """

        async def race() -> tuple[bool, int, int]:
            now = 100.0
            call = YieldingSpeakCall()
            guard = CallInterlock(call, clock=lambda: now)
            await open_and_report_started(guard)
            now = 160.0

            speaking = asyncio.create_task(guard.speak(STOPPED, request_id=new_request_id()))
            await call.speak_entered.wait()
            ending = asyncio.create_task(guard.end_silent_call(60.0))
            await asyncio.sleep(0)
            ended_while_speaking = call.calls_ended
            call.release_speak.set()

            await speaking
            return await ending, ended_while_speaking, call.calls_ended

        ended, while_speaking, attempts = asyncio.run(race())

        assert while_speaking == 0
        assert ended is True
        assert attempts == 1

    def test_an_owned_call_is_due_once_when_its_configured_silence_elapses(self) -> None:
        now = 100.0
        call = FakeCall()
        guard = CallInterlock(call, clock=lambda: now)
        asyncio.run(open_and_report_started(guard))

        now = 159.9
        assert asyncio.run(guard.end_silent_call(60.0)) is False
        now = 160.0
        assert asyncio.run(guard.end_silent_call(60.0)) is True
        assert asyncio.run(guard.end_silent_call(60.0)) is False
        assert call.calls_ended == 1

    def test_activity_restarts_the_owned_calls_silence_window(self) -> None:
        now = 100.0
        guard = CallInterlock(FakeCall(), clock=lambda: now)
        asyncio.run(open_and_report_started(guard))

        now = 150.0
        guard.note_activity()
        now = 209.9
        assert asyncio.run(guard.end_silent_call(60.0)) is False
        now = 210.0
        assert asyncio.run(guard.end_silent_call(60.0)) is True

    def test_a_call_change_starts_a_fresh_window_and_a_fresh_end_attempt(self) -> None:
        now = 100.0
        guard = CallInterlock(FakeCall(), clock=lambda: now)
        guard.note_started("call-1")
        now = 160.0
        assert asyncio.run(guard.end_silent_call(60.0)) is True

        guard.note_started("call-2")
        now = 219.9
        assert asyncio.run(guard.end_silent_call(60.0)) is False
        now = 220.0
        assert asyncio.run(guard.end_silent_call(60.0)) is True

    def test_a_delivered_notice_is_not_itself_call_activity(self) -> None:
        """A `speak` receipt timestamps a text hand-over, not a voice (#184).

        What actually keeps the call alive is the Voice saying it, which arrives
        as `VoiceSpeech` — so counting the hand-over as well would restart the
        window from before the answer instead of from the end of it.
        """
        now = 100.0
        call = FakeCall()
        guard = CallInterlock(call, clock=lambda: now)
        asyncio.run(open_and_report_started(guard))

        now = 150.0
        asyncio.run(guard.speak(STOPPED, request_id=new_request_id()))
        now = 160.0
        assert asyncio.run(guard.end_silent_call(60.0)) is True
        assert call.spoken == [STOPPED]


class TestTheVoiceKeepsTheCallAlive:
    """The ceiling counts both sides of the conversation, as legacy's did (#184).

    `legacy@1d32845:bridge/livecall.py:102-105` counted "somebody is speaking:
    the user, or the Realtime Voice Layer" with one regex over both roles. The
    rewrite kept only the user half, so on headphones a long answer was hung up
    in the middle of itself (#169, ADR 0010).
    """

    def test_the_ceiling_holds_for_as_long_as_the_voice_is_speaking(self) -> None:
        """A span, not an edge: 75 s of audio generated in 10 s is still 75 s of call."""
        now = 100.0
        guard = CallInterlock(FakeCall(), clock=lambda: now)
        asyncio.run(open_and_report_started(guard))

        guard.note_voice_speech(speaking=True)
        now = 1_000.0

        assert asyncio.run(guard.end_silent_call(60.0)) is False

    def test_both_edges_restart_the_window_and_the_last_one_is_what_counts(self) -> None:
        now = 100.0
        guard = CallInterlock(FakeCall(), clock=lambda: now)
        asyncio.run(open_and_report_started(guard))

        now = 150.0
        guard.note_voice_speech(speaking=True)
        now = 225.0
        guard.note_voice_speech(speaking=False)

        now = 284.9
        assert asyncio.run(guard.end_silent_call(60.0)) is False
        now = 285.0
        assert asyncio.run(guard.end_silent_call(60.0)) is True

    def test_a_call_that_clears_while_speaking_leaves_no_flag_behind(self) -> None:
        """A start with no stop, then the call goes away: the next call is unheld."""
        now = 100.0
        guard = CallInterlock(FakeCall(), clock=lambda: now)
        asyncio.run(open_and_report_started(guard))
        guard.note_voice_speech(speaking=True)
        held = guard.call_id()
        assert held is not None

        assert guard.note_ended(held) is True
        guard.note_started("call-2")
        now = 160.0

        assert asyncio.run(guard.end_silent_call(60.0)) is True

    def test_adopting_a_call_starts_it_unheld(self) -> None:
        """The same hazard from the other direction — adoption is a fresh call."""
        now = 100.0
        guard = CallInterlock(FakeCall(), clock=lambda: now)
        guard.note_started("call-1")
        guard.note_voice_speech(speaking=True)

        guard.note_started("call-2")
        now = 160.0

        assert asyncio.run(guard.end_silent_call(60.0)) is True

    def test_a_voice_edge_on_no_owned_call_holds_nothing(self) -> None:
        now = 100.0
        guard = CallInterlock(FakeCall(), clock=lambda: now)

        guard.note_voice_speech(speaking=True)
        guard.note_started("call-1")
        now = 160.0

        assert asyncio.run(guard.end_silent_call(60.0)) is True


class TestTheOtherRefusalHasLeftThisDoor:
    """A call is still never started on nothing — one door earlier than this one.

    It has to be *one* place. The hub and the escalation pipeline both open
    calls, and when each carried its own copy of the blank check the two wordings
    had already drifted apart before anything noticed. That is still true, and
    the one place is now the `Dial` itself (#194, ADR 0018): a caller cannot even
    build the argument this door used to refuse, and the interlock is left with
    the one rule it exists for.
    """

    def test_this_door_no_longer_checks_what_a_call_is_opened_on(self) -> None:
        """It could not: what it is handed has already refused its own blanks."""
        source = Path(inspect.getsourcefile(CallInterlock.open_call) or "")
        assert "cannot dial a call" not in source.read_text(encoding="utf-8")

    def test_a_blank_half_is_refused_before_any_door_is_reached(self) -> None:
        guard, call = interlock()

        with pytest.raises(ValueError):
            asyncio.run(guard.open_call(Dial(voice="", agent="rules")))

        assert call.calls_started == 0
        assert guard.owns_call() is False

    def test_whitespace_is_not_instructions(self) -> None:
        guard, call = interlock()

        with pytest.raises(ValueError):
            asyncio.run(guard.open_call(Dial(voice="prose", agent="   \n  ")))

        assert call.calls_started == 0

    def test_the_refusal_is_worded_in_exactly_one_place(self) -> None:
        """Nothing else may restate it — a second copy is a second rule."""
        package = Path(__file__).resolve().parents[1] / "src" / "gpt_voicecoding"
        wording = "so it cannot dial a call"
        holding = [
            path.relative_to(package)
            for path in sorted(package.rglob("*.py"))
            if wording in path.read_text(encoding="utf-8")
        ]

        assert [str(path) for path in holding] == ["core/errors.py"]


class TestTheInvariant:
    def test_opening_a_second_call_is_refused(self) -> None:
        guard, call = interlock()
        asyncio.run(guard.open_call(dial()))

        with pytest.raises(SecondCallRefused) as refusal:
            asyncio.run(guard.open_call(dial()))

        assert refusal.value.call_id == guard.call_id()

    def test_the_refusal_never_reaches_the_adapter(self) -> None:
        """The adapter neither knows nor enforces this rule (ADR 0001)."""
        guard, call = interlock()
        asyncio.run(guard.open_call(dial()))

        with pytest.raises(SecondCallRefused):
            asyncio.run(guard.open_call(dial()))

        assert call.calls_started == 1

    def test_ending_a_call_makes_opening_the_next_one_legal_again(self) -> None:
        guard, call = interlock()
        asyncio.run(guard.open_call(dial()))
        asyncio.run(guard.end_call())

        asyncio.run(guard.open_call(dial()))

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
        asyncio.run(guard.open_call(dial()))
        held = guard.call_id()
        assert held is not None

        guard.note_ended(held)

        assert guard.owns_call() is False

    def test_an_end_reported_for_some_other_call_does_not_release_this_one(self) -> None:
        """A late event about a finished call must not unlock the live one."""
        guard, _ = interlock()
        asyncio.run(guard.open_call(dial()))

        assert guard.note_ended("call-from-last-week") is False
        assert guard.owns_call() is True

    def test_ending_a_call_is_idempotent_from_upward_events(self) -> None:
        guard, _ = interlock()

        assert guard.note_ended("call-1") is False
        assert guard.owns_call() is False

    def test_only_a_real_release_reports_the_transition(self) -> None:
        """Callers reconcile on this answer, so a stale event must not claim one."""
        guard, _ = interlock()
        asyncio.run(guard.open_call(dial()))
        held = guard.call_id()
        assert held is not None

        assert guard.note_ended(held) is True
        assert guard.note_ended(held) is False

"""The Call Keeper: one call, Cool-down, and the Silence Ceiling.

Everything here runs on a **fake clock** and a **fake Briefer**. That is the
point of the two-layer shape (`core/call_keeper.py`): the rules are a pure state
machine, so a sixty-second ceiling and a thirty-second Cool-down cost no wall
time, and "who needs the user" is one answer a test hands over rather than a
roster it has to build.

The defects these exist to make impossible:

* two assistants on shared speakers, which the reference implementation produced
  by pressing the GUI toggle while the system already owned a call;
* the call that rings, is missed, and rings again immediately — no Cool-down at
  all in legacy, `legacy@1d32845:bridge/livecall.py:561-581` being the incident;
* a call ended in the middle of the answer that was about to hold it open (#184),
  and its user-side twin: a user who talks for a whole ceiling without the Voice
  answering, judged silent because only the finished transcript counted (#195).
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from fakes import FakeCall
from gpt_voicecoding.core.adjudication import SwitchAdjudicator
from gpt_voicecoding.core.call_keeper import (
    USER_OPENED,
    CallKeeper,
    Dialling,
    Ending,
    Permits,
    Sounding,
    Speaking,
)
from gpt_voicecoding.core.errors import CallInstructionsMissing
from gpt_voicecoding.core.policy import CorePolicy
from gpt_voicecoding.core.switches import Switchboard, SwitchName
from gpt_voicecoding.seams.call import (
    CallDropped,
    CallEnded,
    CallStarted,
    CallState,
    Cue,
    Dial,
    DialReason,
    HandoverItem,
    SpokenBrief,
    UserSpeaking,
    UserSpeech,
    VoiceSpeech,
)

COOL_DOWN = 30.0
CEILING = 60.0
SETTLE = 5.0

WAITING = SpokenBrief(
    name="repo · port the log",
    agent="claude",
    state="waiting for a decision",
    newest="which log file?",
    decision=("either one",),
    answerable_here="from here",
    last_activity_at="a moment ago",
)

#: What the Focus Session's own brief reads as, mid-call. A different Session
#: from `WAITING`, so a test can tell the brief a call was *dialled* on from the
#: one spoken into a call that was already up.
FOCUS = SpokenBrief(
    name="repo · the focus session",
    agent="codex",
    state="waiting for a decision",
    newest="which branch?",
    decision=("this one",),
    answerable_here="from here",
    last_activity_at="a moment ago",
)

#: What a system-dialled call's hand-over looks like in these tests: the reason
#: the Briefer's production adapter puts first, and one Session Brief behind it.
NEEDS_THE_USER: tuple[HandoverItem, ...] = (DialReason(text="Sessions need the user."), WAITING)


class FakeBriefer:
    """Who needs the user, as one answer a test sets and counts the readings of."""

    def __init__(
        self,
        answer: tuple[HandoverItem, ...] | None = NEEDS_THE_USER,
        *,
        focus: SpokenBrief | None = FOCUS,
    ) -> None:
        self.answer = answer
        #: What the Focus Session's own brief reads as *right now*. A test that
        #: proves the fresh reading (#196) changes this between the event and
        #: the gap, exactly as a Session answered at the terminal would.
        self.focus = focus
        #: How many times the Keeper actually asked. ADR 0017 is a claim about
        #: *when* this is read, so a test that proves freshness counts it.
        self.readings = 0
        #: The same count for the mid-call verb, which is read at the moment of
        #: sounding and never at the moment of the event.
        self.focus_readings = 0

    def handover(self) -> tuple[HandoverItem, ...] | None:
        self.readings += 1
        return self.answer

    def focus_brief(self) -> SpokenBrief | None:
        self.focus_readings += 1
        return self.focus


class Keeper:
    """One assembled Call Keeper over a fake call, a fake Briefer and a fake clock."""

    def __init__(
        self,
        *,
        duty: bool = True,
        voice: bool = True,
        auto_hangup: bool = True,
        briefer: FakeBriefer | None = None,
        call: FakeCall | None = None,
        dial_for: object = None,
    ) -> None:
        self.now = 1_000.0
        self.switches = Switchboard()
        self.switches.flip(SwitchName.DUTY, duty)
        self.switches.flip(SwitchName.VOICE, voice)
        self.switches.flip(SwitchName.AUTO_HANGUP, auto_hangup)
        self.call = call or FakeCall()
        self.briefer = briefer or FakeBriefer()
        self.dialled_on: list[Dial] = []
        self.keeper = CallKeeper(
            call=self.call,
            briefer=self.briefer,
            adjudicator=SwitchAdjudicator(self.switches),
            dial_for=dial_for or self._dial,  # type: ignore[arg-type]
            policy=CorePolicy(
                cool_down_seconds=COOL_DOWN,
                silence_end_seconds=CEILING,
                speech_settle_seconds=SETTLE,
            ),
            clock=lambda: self.now,
        )

    def _dial(self, hand_over: tuple[HandoverItem, ...]) -> Dial:
        built = Dial(voice="prose for the Voice", agent="rules for the Agent", hand_over=hand_over)
        self.dialled_on.append(built)
        return built

    # -- driving it --------------------------------------------------------

    def wake(self, *, focus: bool = False) -> None:
        asyncio.run(self.keeper.wake(focus=focus))

    def tick(self) -> None:
        asyncio.run(self.keeper.tick(self.now))

    def hear(self, *events: object) -> None:
        for event in events:
            asyncio.run(self.keeper.heard(event))  # type: ignore[arg-type]

    def toggle(self) -> object:
        return asyncio.run(self.keeper.live_toggle())

    def flip(self, name: str, on: bool) -> None:
        self.switches.flip(name, on)

    def wait(self, seconds: float) -> None:
        """Advance the fake clock and let the one-second clock run once."""
        self.now += seconds
        self.tick()

    # -- reading it --------------------------------------------------------

    @property
    def status(self) -> object:
        return self.keeper.status()

    def up(self, call_id: str = "call-1") -> None:
        """Bring a call up the way the seam does, so the Keeper adopts it."""
        self.hear(CallStarted(call_id=call_id))


def permits(*, dial: bool = True, hang_up: bool = True) -> Permits:
    return Permits(dial=dial, hang_up=hang_up)


class TestTheInterfaceIsFiveEntriesAndNoContent:
    """`CONTEXT.md`'s *Call Keeper*: it knows nothing of what is said."""

    def test_the_keeper_has_exactly_the_five_public_entries(self) -> None:
        surface = {
            name
            for name, _ in inspect.getmembers(CallKeeper, inspect.isfunction)
            if not name.startswith("_")
        }
        assert surface == {"live_toggle", "wake", "tick", "heard", "status"}

    def test_no_entry_takes_a_session_brief_or_any_other_words(self) -> None:
        """The Keeper decides *when* to sound, never *what* is said.

        A `speak` would be the whole of mid-call behaviour, and that is #196's.
        A brief crossing this interface is how "it knows nothing of what is
        said" stops being true.
        """
        for name in ("live_toggle", "wake", "tick", "heard", "status"):
            hints = inspect.signature(getattr(CallKeeper, name)).parameters
            assert SpokenBrief not in {parameter.annotation for parameter in hints.values()}


class TestOneCallAtATime:
    def test_the_live_toggle_opens_a_call_when_none_is_up(self) -> None:
        keeper = Keeper()
        snapshot = keeper.toggle()
        assert snapshot.state is CallState.UP  # type: ignore[attr-defined]
        assert keeper.call.calls_started == 1

    def test_the_live_toggle_ends_the_call_the_system_owns(self) -> None:
        keeper = Keeper()
        keeper.toggle()
        keeper.toggle()
        assert keeper.call.calls_ended == 1
        assert keeper.call.calls_started == 1

    def test_a_wake_while_a_call_is_up_opens_no_second_one(self) -> None:
        """The one-call rule, encoded once: `wake` yields nothing with a call up.

        Mid-call news is #196's; what may never happen meanwhile is a second
        voice surface, which is what put two assistants on shared speakers.
        """
        keeper = Keeper()
        keeper.toggle()
        keeper.wake()
        assert keeper.call.calls_started == 1

    def test_a_call_the_user_started_is_adopted_and_the_toggle_ends_it(self) -> None:
        keeper = Keeper()
        keeper.up("a-call-the-user-started")
        assert keeper.status.call_id == "a-call-the-user-started"  # type: ignore[attr-defined]
        keeper.toggle()
        assert keeper.call.calls_started == 0
        assert keeper.call.calls_ended == 1

    def test_a_late_call_ended_for_a_previous_call_does_not_clear_the_live_one(self) -> None:
        keeper = Keeper()
        keeper.up("current-call")
        keeper.hear(CallEnded(call_id="a-call-that-finished-earlier"))
        assert keeper.status.call_id == "current-call"  # type: ignore[attr-defined]

    def test_that_late_event_starts_no_cool_down_either(self) -> None:
        """A Cool-down is paced off *this* call's end, not off news about another."""
        keeper = Keeper()
        keeper.up("current-call")
        keeper.hear(CallEnded(call_id="some-older-call"))
        assert keeper.status.cool_down_remaining == 0.0  # type: ignore[attr-defined]


class TestCoolDownAfterAnyEndOfACall:
    """`CONTEXT.md`, *Cool-down*: hung up, dropped, or a dial that failed."""

    def test_a_manual_end_starts_the_cool_down(self) -> None:
        keeper = Keeper()
        keeper.toggle()
        keeper.toggle()
        assert keeper.status.cool_down_remaining == COOL_DOWN  # type: ignore[attr-defined]

    def test_a_dropped_call_starts_the_cool_down(self) -> None:
        keeper = Keeper()
        keeper.up()
        keeper.hear(CallDropped(call_id="call-1", detail="the far side went away"))
        assert keeper.status.cool_down_remaining == COOL_DOWN  # type: ignore[attr-defined]

    def test_the_silence_ceiling_ending_a_call_starts_the_cool_down(self) -> None:
        keeper = Keeper()
        keeper.up()
        keeper.wait(CEILING + 1)
        assert keeper.call.calls_ended == 1
        assert keeper.status.cool_down_remaining == COOL_DOWN  # type: ignore[attr-defined]

    def test_a_dial_that_failed_starts_the_cool_down(self) -> None:
        """A call that never came up is an end of a call, for pacing purposes."""
        keeper = Keeper(call=FakeCall(reachable=False))
        keeper.wake()
        assert keeper.call.calls_started == 0
        assert keeper.status.cool_down_remaining == COOL_DOWN  # type: ignore[attr-defined]

    def test_a_failed_dial_clears_the_owed_flag_so_one_event_buys_one_attempt(self) -> None:
        keeper = Keeper(call=FakeCall(reachable=False))
        keeper.wake()
        assert keeper.status.dial_owed is False  # type: ignore[attr-defined]
        keeper.wait(COOL_DOWN + 1)
        assert len(keeper.call.opened_on) == 1

    def test_no_call_is_dialled_while_the_cool_down_runs(self) -> None:
        keeper = Keeper()
        keeper.toggle()
        keeper.toggle()
        keeper.wake()
        keeper.wait(COOL_DOWN - 1)
        assert keeper.call.calls_started == 1  # only the one the toggle opened


class TestTheOwedDialIsPaidFromAFreshReading:
    def test_an_event_inside_the_cool_down_marks_one_owed_dial(self) -> None:
        keeper = Keeper()
        keeper.toggle()
        keeper.toggle()
        keeper.wake()
        assert keeper.status.dial_owed is True  # type: ignore[attr-defined]

    def test_it_is_paid_when_the_cool_down_elapses(self) -> None:
        keeper = Keeper()
        keeper.toggle()
        keeper.toggle()
        keeper.wake()
        keeper.wait(COOL_DOWN + 1)
        assert keeper.call.calls_started == 2
        assert keeper.call.opened_on[-1].hand_over == NEEDS_THE_USER

    def test_the_reading_is_taken_at_the_moment_of_dialling_and_not_at_the_event(self) -> None:
        """ADR 0017: briefed from a fresh reading, never from replayed events."""
        keeper = Keeper()
        keeper.toggle()
        keeper.toggle()
        keeper.wake()
        assert keeper.briefer.readings == 0
        keeper.wait(COOL_DOWN + 1)
        assert keeper.briefer.readings == 1

    def test_a_briefer_that_says_nobody_needs_the_user_cancels_the_owed_dial(self) -> None:
        keeper = Keeper()
        keeper.toggle()
        keeper.toggle()
        keeper.wake()
        keeper.briefer.answer = None
        keeper.wait(COOL_DOWN + 1)
        assert keeper.call.calls_started == 1
        assert keeper.status.dial_owed is False  # type: ignore[attr-defined]

    def test_a_cancelled_dial_starts_no_second_cool_down(self) -> None:
        """Nothing ended, so nothing is being paced: the next event dials at once."""
        keeper = Keeper(briefer=FakeBriefer(answer=None))
        keeper.wake()
        assert keeper.status.cool_down_remaining == 0.0  # type: ignore[attr-defined]
        keeper.briefer.answer = NEEDS_THE_USER
        keeper.wake()
        assert keeper.call.calls_started == 1

    def test_three_events_inside_one_cool_down_produce_one_dial(self) -> None:
        keeper = Keeper()
        keeper.toggle()
        keeper.toggle()
        keeper.wake()
        keeper.wake()
        keeper.wake()
        keeper.wait(COOL_DOWN + 1)
        assert keeper.call.calls_started == 2
        assert keeper.briefer.readings == 1


class TestWhatTheSwitchesDecideAndWhen:
    def test_duty_off_records_no_owed_dial(self) -> None:
        """Legacy suppressed the event rather than queueing it (`host.py:2100-2101`)."""
        keeper = Keeper()
        keeper.toggle()
        keeper.toggle()
        keeper.flip(SwitchName.DUTY, False)
        keeper.wake()
        assert keeper.status.dial_owed is False  # type: ignore[attr-defined]
        keeper.flip(SwitchName.DUTY, True)
        keeper.wait(COOL_DOWN + 1)
        assert keeper.call.calls_started == 1

    def test_duty_off_dials_nothing_at_all(self) -> None:
        keeper = Keeper(duty=False)
        keeper.wake()
        assert keeper.call.opened_on == []

    def test_voice_off_dials_nothing_at_all(self) -> None:
        keeper = Keeper(voice=False)
        keeper.wake()
        assert keeper.call.opened_on == []

    def test_a_flip_off_during_the_cool_down_changes_the_outcome(self) -> None:
        """Duty ∧ Voice is judged at dial time, not at `wake`."""
        keeper = Keeper()
        keeper.toggle()
        keeper.toggle()
        keeper.wake()
        keeper.flip(SwitchName.VOICE, False)
        keeper.wait(COOL_DOWN + 1)
        assert keeper.call.calls_started == 1
        assert keeper.status.dial_owed is False  # type: ignore[attr-defined]

    def test_a_dropped_owed_dial_is_not_held_for_a_later_flip(self) -> None:
        """A later flip is a `wake` of its own; holding it too would ring twice."""
        keeper = Keeper()
        keeper.toggle()
        keeper.toggle()
        keeper.wake()
        keeper.flip(SwitchName.VOICE, False)
        keeper.wait(COOL_DOWN + 1)
        keeper.flip(SwitchName.VOICE, True)
        keeper.tick()
        assert keeper.call.calls_started == 1

    def test_the_live_toggle_works_with_every_switch_off(self) -> None:
        keeper = Keeper(duty=False, voice=False, auto_hangup=False)
        assert keeper.toggle().state is CallState.UP  # type: ignore[attr-defined]

    def test_the_live_toggle_ignores_the_cool_down(self) -> None:
        """`CONTEXT.md`: the user's own Live Toggle is not subject to it."""
        keeper = Keeper()
        keeper.toggle()
        keeper.toggle()
        assert keeper.status.cool_down_remaining == COOL_DOWN  # type: ignore[attr-defined]
        assert keeper.toggle().state is CallState.UP  # type: ignore[attr-defined]
        assert keeper.call.calls_started == 2


class TestWhatACallIsOpenedOn:
    def test_a_user_opened_call_carries_the_single_item_and_stays_silent(self) -> None:
        keeper = Keeper()
        keeper.toggle()
        assert keeper.call.opened_on[-1].hand_over == (DialReason(text=USER_OPENED),)
        assert keeper.briefer.readings == 0
        assert keeper.call.spoken == []

    def test_a_system_dialled_call_carries_the_briefers_whole_answer(self) -> None:
        keeper = Keeper()
        keeper.wake()
        assert keeper.call.opened_on[-1].hand_over == NEEDS_THE_USER
        assert keeper.call.spoken == []

    def test_a_hub_that_generated_no_instructions_opens_no_call(self) -> None:
        def refuse(_hand_over: tuple[HandoverItem, ...]) -> Dial:
            raise CallInstructionsMissing("prose for the Voice")

        keeper = Keeper(dial_for=refuse)
        with pytest.raises(CallInstructionsMissing):
            keeper.toggle()
        assert keeper.call.calls_started == 0

    def test_that_refusal_is_a_failed_dial_and_paces_the_next_one(self) -> None:
        def refuse(_hand_over: tuple[HandoverItem, ...]) -> Dial:
            raise CallInstructionsMissing("rules for the Call Agent")

        keeper = Keeper(dial_for=refuse)
        keeper.wake()
        assert keeper.status.cool_down_remaining == COOL_DOWN  # type: ignore[attr-defined]


class TestTheSilenceCeiling:
    def test_a_silent_call_is_ended_when_the_ceiling_arrives(self) -> None:
        keeper = Keeper()
        keeper.up()
        keeper.wait(CEILING - 1)
        assert keeper.call.calls_ended == 0
        keeper.wait(2)
        assert keeper.call.calls_ended == 1

    def test_the_ceiling_is_attempted_once_per_call(self) -> None:
        keeper = Keeper()
        keeper.up()
        keeper.wait(CEILING + 1)
        keeper.up("call-2")
        keeper.wait(CEILING + 1)
        assert keeper.call.calls_ended == 2

    def test_the_users_speech_restarts_the_window(self) -> None:
        keeper = Keeper()
        keeper.up()
        keeper.wait(CEILING - 1)
        keeper.hear(UserSpeech(text="are you still there"))
        keeper.wait(CEILING - 1)
        assert keeper.call.calls_ended == 0

    def test_the_user_speaking_holds_the_ceiling_open(self) -> None:
        """The user's half, as a span (#195): a long question is not silence.

        Until this event the user's half arrived only as the finished
        `UserSpeech(text)`, which since #194 often lands at hand-off — so a user
        who talked for a whole ceiling was judged silent.
        """
        keeper = Keeper()
        keeper.up()
        keeper.hear(UserSpeaking(speaking=True))
        keeper.wait(CEILING * 2)
        assert keeper.call.calls_ended == 0

    def test_the_voice_speaking_holds_the_ceiling_open(self) -> None:
        keeper = Keeper()
        keeper.up()
        keeper.hear(VoiceSpeech(speaking=True))
        keeper.wait(CEILING * 2)
        assert keeper.call.calls_ended == 0

    def test_after_speaking_stops_the_settle_window_is_waited_out(self) -> None:
        """`speech_settle_seconds` after the stop edge before silence counts."""
        keeper = Keeper()
        keeper.up()
        keeper.hear(VoiceSpeech(speaking=True))
        keeper.now += 10
        keeper.hear(VoiceSpeech(speaking=False))
        keeper.wait(CEILING + SETTLE - 1)
        assert keeper.call.calls_ended == 0
        keeper.wait(2)
        assert keeper.call.calls_ended == 1

    def test_one_side_stopping_while_the_other_speaks_starts_no_settle_window(self) -> None:
        keeper = Keeper()
        keeper.up()
        keeper.hear(UserSpeaking(speaking=True), VoiceSpeech(speaking=True))
        keeper.hear(UserSpeaking(speaking=False))
        keeper.wait(CEILING * 2)
        assert keeper.call.calls_ended == 0

    def test_the_auto_hangup_switch_off_keeps_a_silent_call_up(self) -> None:
        keeper = Keeper(auto_hangup=False)
        keeper.up()
        keeper.wait(CEILING * 2)
        assert keeper.call.calls_ended == 0

    def test_turning_the_switch_back_on_ends_a_call_silent_all_along(self) -> None:
        """The attempt is unspent while the switch is off, so it is still there."""
        keeper = Keeper(auto_hangup=False)
        keeper.up()
        keeper.wait(CEILING * 2)
        keeper.flip(SwitchName.AUTO_HANGUP, True)
        keeper.tick()
        assert keeper.call.calls_ended == 1

    def test_the_ceiling_holds_with_duty_off_on_a_call_the_user_opened(self) -> None:
        """It is the call's own limit, not an act toward the user (`CONTEXT.md`)."""
        keeper = Keeper(duty=False)
        keeper.up()
        keeper.wait(CEILING + 1)
        assert keeper.call.calls_ended == 1

    def test_a_speaking_flag_does_not_carry_across_to_the_next_call(self) -> None:
        keeper = Keeper()
        keeper.up()
        keeper.hear(VoiceSpeech(speaking=True))
        keeper.hear(CallDropped(call_id="call-1"))
        keeper.up("call-2")
        keeper.wait(CEILING + 1)
        assert keeper.call.calls_ended == 1


class TestWhatTheUserHearsAtEachEndOfACall:
    def test_connected_then_ended_in_order(self) -> None:
        keeper = Keeper()
        keeper.up()
        keeper.hear(CallEnded(call_id="call-1"))
        assert keeper.call.cues == [Cue.CONNECTED, Cue.ENDED]

    def test_a_call_that_went_away_by_itself_is_heard_the_same_way(self) -> None:
        keeper = Keeper()
        keeper.up()
        keeper.hear(CallDropped(call_id="call-1", detail="the far side went away"))
        assert keeper.call.cues == [Cue.CONNECTED, Cue.ENDED]

    def test_an_end_for_a_call_the_keeper_was_not_holding_is_still_heard(self) -> None:
        """What the user is owed is the sound of the call *they* were on (#186)."""
        keeper = Keeper()
        keeper.hear(CallEnded(call_id="a-call-nobody-here-held"))
        assert keeper.call.cues == [Cue.ENDED]

    def test_speech_on_a_quiet_call_rings_nothing_by_itself(self) -> None:
        """The cue marks news, not talking: #196 rings it from `wake` and nowhere else."""
        keeper = Keeper()
        keeper.up()
        keeper.hear(UserSpeech(text="hello"), VoiceSpeech(speaking=True))
        assert Cue.EVENT not in keeper.call.cues

    def test_a_cue_that_raises_never_stops_the_act_it_was_asked_from(self) -> None:
        class DeafCall(FakeCall):
            async def play_cue(self, cue: Cue) -> None:
                raise RuntimeError("no output device")

        keeper = Keeper(call=DeafCall())
        keeper.up()
        keeper.hear(CallEnded(call_id="call-1"))
        assert keeper.status.cool_down_remaining == COOL_DOWN  # type: ignore[attr-defined]


class TestMidCallNewsSpeaksInTheGapAndRingsForTheRest:
    """#196: while a call is up, the Focus Session is spoken and the rest rings.

    The wire has no silent mid-call path (#175) — a second append truncates the
    utterance in flight — so every announcement waits for a gap on both sides
    and for one interval since the last mid-call sound. Both facts are timing,
    and both are proved here on the fake clock rather than against a call.
    """

    def in_a_call(self, **kwargs: object) -> Keeper:
        """A Keeper holding a call the events below arrive during.

        Opened through the Live Toggle rather than by handing the machine a
        `CallStarted`, because the adapter has to be holding a call too: a
        `speak` into an adapter whose own call is down is refused there, which is
        exactly the fake being honest (`tests/fakes.py::FakeCall.speak`).
        """
        keeper = Keeper(**kwargs)  # type: ignore[arg-type]
        keeper.toggle()
        return keeper

    def gap(self, keeper: Keeper) -> None:
        """Let the settle window run out on a call where nobody is speaking."""
        keeper.wait(SETTLE)

    # -- the Focus Session, in the gap -------------------------------------

    def test_a_focus_event_says_nothing_while_the_voice_is_still_speaking(self) -> None:
        keeper = self.in_a_call()
        keeper.hear(VoiceSpeech(speaking=True))
        keeper.wake(focus=True)
        keeper.wait(SETTLE * 2)
        assert keeper.call.spoken == []

    def test_it_speaks_the_fresh_brief_once_the_voice_has_settled(self) -> None:
        keeper = self.in_a_call()
        keeper.hear(VoiceSpeech(speaking=True))
        keeper.wake(focus=True)
        keeper.wait(1.0)
        keeper.hear(VoiceSpeech(speaking=False))
        keeper.wait(SETTLE - 1.0)
        assert keeper.call.spoken == [], "the settle window had not run out"
        keeper.wait(1.0)
        assert keeper.call.spoken == [FOCUS]

    def test_the_brief_is_read_at_the_moment_of_sounding_and_not_at_the_event(self) -> None:
        """ADR 0017's rule, mid-call: what is spoken is what stands *now*."""
        keeper = self.in_a_call()
        keeper.hear(VoiceSpeech(speaking=True))
        keeper.wake(focus=True)
        assert keeper.briefer.focus_readings == 0
        keeper.hear(VoiceSpeech(speaking=False))
        self.gap(keeper)
        assert keeper.briefer.focus_readings == 1

    def test_three_focus_events_in_one_utterance_are_one_brief(self) -> None:
        keeper = self.in_a_call()
        keeper.hear(VoiceSpeech(speaking=True))
        for _ in range(3):
            keeper.wake(focus=True)
        keeper.hear(VoiceSpeech(speaking=False))
        self.gap(keeper)
        keeper.wait(COOL_DOWN)
        assert keeper.call.spoken == [FOCUS]

    def test_a_session_that_no_longer_needs_the_user_is_not_spoken_about(self) -> None:
        """The flag is cleared by the reading, so nothing is owed afterwards."""
        keeper = self.in_a_call()
        keeper.wake(focus=True)
        keeper.briefer.focus = None
        self.gap(keeper)
        assert keeper.call.spoken == []
        keeper.briefer.focus = FOCUS
        keeper.wait(COOL_DOWN)
        assert keeper.call.spoken == [], "the word was owed to a Session that answered itself"

    def test_the_user_speaking_holds_the_word_back_as_the_voice_does(self) -> None:
        keeper = self.in_a_call()
        keeper.hear(UserSpeaking(speaking=True))
        keeper.wake(focus=True)
        keeper.wait(SETTLE * 2)
        assert keeper.call.spoken == []
        keeper.hear(UserSpeaking(speaking=False))
        self.gap(keeper)
        assert keeper.call.spoken == [FOCUS]

    def test_a_word_owed_waits_indefinitely_rather_than_going_stale(self) -> None:
        """It cannot go stale: it is composed when it is spoken, not when armed."""
        keeper = self.in_a_call(auto_hangup=False)
        keeper.hear(VoiceSpeech(speaking=True))
        keeper.wake(focus=True)
        keeper.wait(CEILING * 3)
        assert keeper.call.spoken == []
        keeper.hear(VoiceSpeech(speaking=False))
        self.gap(keeper)
        assert keeper.call.spoken == [FOCUS]

    def test_a_late_transcript_does_not_push_the_gap_out(self) -> None:
        """The gap is the two speaking states, never `UserSpeech(text)` (#196, #194).

        The finished transcript often lands at hand-off or at teardown, long
        after the utterance it describes ended. A gap measured from it would be
        pushed out by news of a pause that was already over.
        """
        keeper = self.in_a_call()
        keeper.hear(UserSpeaking(speaking=True))
        keeper.wake(focus=True)
        keeper.now += 1.0
        keeper.hear(UserSpeaking(speaking=False))
        keeper.wait(SETTLE - 1.0)
        keeper.hear(UserSpeech(text="what the user had said a while ago"))
        keeper.wait(1.0)
        assert keeper.call.spoken == [FOCUS]

    def test_no_word_is_carried_into_the_next_call(self) -> None:
        """The gap it was waiting for was that call's, and that call is over."""
        keeper = self.in_a_call()
        keeper.hear(VoiceSpeech(speaking=True))
        keeper.wake(focus=True)
        keeper.toggle()
        keeper.now += COOL_DOWN
        keeper.toggle()
        self.gap(keeper)
        assert keeper.call.spoken == []

    # -- everything else, as one ring --------------------------------------

    def test_an_event_about_another_session_rings_and_says_nothing(self) -> None:
        keeper = self.in_a_call()
        keeper.wake(focus=False)
        assert keeper.call.cues.count(Cue.EVENT) == 1
        assert keeper.call.spoken == []

    def test_a_second_ring_inside_the_interval_is_folded_into_the_first(self) -> None:
        keeper = self.in_a_call(auto_hangup=False)
        keeper.wake(focus=False)
        keeper.wait(COOL_DOWN - 1.0)
        keeper.wake(focus=False)
        assert keeper.call.cues.count(Cue.EVENT) == 1
        keeper.wait(1.0)
        keeper.wake(focus=False)
        assert keeper.call.cues.count(Cue.EVENT) == 2

    def test_the_ring_and_the_spoken_word_share_the_one_interval(self) -> None:
        """One "last sounded at" for both, so news of any kind is paced together."""
        keeper = self.in_a_call(auto_hangup=False)
        keeper.wake(focus=False)
        keeper.wake(focus=True)
        keeper.wait(COOL_DOWN - 1.0)
        assert keeper.call.spoken == [], "the ring had not been an interval ago"
        keeper.wait(1.0)
        assert keeper.call.spoken == [FOCUS]

    def test_a_ring_after_a_spoken_word_waits_out_the_same_interval(self) -> None:
        keeper = self.in_a_call(auto_hangup=False)
        keeper.wake(focus=True)
        self.gap(keeper)
        assert keeper.call.spoken == [FOCUS]
        keeper.wake(focus=False)
        assert keeper.call.cues.count(Cue.EVENT) == 0
        keeper.wait(COOL_DOWN)
        keeper.wake(focus=False)
        assert keeper.call.cues.count(Cue.EVENT) == 1

    # -- the switches ------------------------------------------------------

    def test_duty_off_mid_call_neither_rings_nor_announces(self) -> None:
        """The call stays up and the ceiling still runs; the system is just quiet."""
        keeper = self.in_a_call(duty=False)
        keeper.wake(focus=True)
        keeper.wake(focus=False)
        self.gap(keeper)
        assert keeper.call.spoken == []
        assert keeper.call.cues.count(Cue.EVENT) == 0
        assert keeper.call.calls_ended == 0

    def test_duty_coming_back_on_is_one_wake_and_one_ring(self) -> None:
        keeper = self.in_a_call(duty=False)
        keeper.wake(focus=True)
        keeper.flip(SwitchName.DUTY, True)
        keeper.wake(focus=False)
        assert keeper.call.cues.count(Cue.EVENT) == 1
        self.gap(keeper)
        assert keeper.call.spoken == [], "the event that arrived with Duty off was not queued"

    def test_voice_off_mid_call_is_the_same_silence(self) -> None:
        keeper = self.in_a_call(voice=False)
        keeper.wake(focus=True)
        self.gap(keeper)
        assert keeper.call.spoken == []

    # -- a speak that does not land ----------------------------------------

    def test_a_speak_the_adapter_refuses_is_not_tried_again(self) -> None:
        """One event buys one attempt (#195), on the voice path as on the dial."""

        class MuteCall(FakeCall):
            async def speak(self, brief: SpokenBrief, *, request_id: object) -> object:  # type: ignore[override]
                raise RuntimeError("the wire would not take it")

        keeper = self.in_a_call(call=MuteCall(), auto_hangup=False)
        keeper.wake(focus=True)
        self.gap(keeper)
        keeper.wait(COOL_DOWN * 2)
        assert keeper.briefer.focus_readings == 1


class TestTheStateMachineOnItsOwn:
    """The rules with no adapter, no lock and no clock — acts in, acts out."""

    def machine(self) -> object:
        from gpt_voicecoding.core.call_keeper import CallTime

        return CallTime(
            cool_down_seconds=COOL_DOWN,
            silence_end_seconds=CEILING,
            speech_settle_seconds=SETTLE,
        )

    def test_a_wake_with_nothing_in_the_way_asks_for_a_dial(self) -> None:
        time = self.machine()
        assert time.wake(0.0, permits(), focus=False) == (Dialling(),)  # type: ignore[attr-defined]

    def test_a_wake_that_can_do_nothing_answers_with_nothing_at_all(self) -> None:
        time = self.machine()
        assert time.wake(0.0, permits(dial=False), focus=False) == ()  # type: ignore[attr-defined]

    def test_the_toggle_ends_what_is_up_and_opens_what_is_not(self) -> None:
        time = self.machine()
        assert time.toggled(0.0) == (Dialling(user_opened=True),)  # type: ignore[attr-defined]
        time.dialled(0.0, call_id="call-1")  # type: ignore[attr-defined]
        assert time.toggled(1.0) == (Ending(),)  # type: ignore[attr-defined]

    def test_the_ceiling_and_the_cue_are_the_machines_own_answers(self) -> None:
        time = self.machine()
        assert time.heard(CallStarted(call_id="call-1"), 0.0, permits()) == (  # type: ignore[attr-defined]
            Sounding(Cue.CONNECTED),
        )
        assert time.tick(CEILING + 1, permits()) == (Ending(ceiling=True),)  # type: ignore[attr-defined]

    def test_the_focus_flag_decides_nothing_when_no_call_is_up(self) -> None:
        """With no call, both kinds of event buy the same thing: one dial."""
        for focus in (True, False):
            time = self.machine()
            assert time.wake(0.0, permits(), focus=focus) == (Dialling(),)  # type: ignore[attr-defined]

    def test_a_focus_event_mid_call_becomes_a_word_owed_and_then_a_speaking(self) -> None:
        time = self.machine()
        time.heard(CallStarted(call_id="call-1"), 0.0, permits())  # type: ignore[attr-defined]
        assert time.wake(0.0, permits(), focus=True) == ()  # type: ignore[attr-defined]
        assert time.tick(SETTLE, permits()) == (Speaking(),)  # type: ignore[attr-defined]

    def test_a_non_focus_event_mid_call_is_the_cue_and_nothing_else(self) -> None:
        time = self.machine()
        time.heard(CallStarted(call_id="call-1"), 0.0, permits())  # type: ignore[attr-defined]
        assert time.wake(0.0, permits(), focus=False) == (Sounding(Cue.EVENT),)  # type: ignore[attr-defined]

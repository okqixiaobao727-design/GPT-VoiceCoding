"""The Call Keeper — the part of Bridge Core that keeps the Live Call's time.

`CONTEXT.md`'s *Call Keeper*: "when a call is dialled, when it ends, and when
the system may ring or speak into it. It knows nothing of what is said — it asks
for a fresh reading at the moment it decides to sound." Every word of that is
load-bearing here, and the last sentence is why this module imports `Briefing`
through a Protocol and never calls it directly.

**An internal component, not a seam** (ADR 0001, principle 2): there is one
implementation and nothing about it varies in production, so it lives in `core/`
beside the hub that owns it rather than behind `seams/`.

**Two layers, and the seam between them is the one a test drives.**

- `CallTime` is a *pure state machine*: events and a time in, acts out — dial,
  end, cue, or nothing. It performs no I/O, holds no adapter and reads no clock
  of its own, so the whole of the Cool-down / Silence Ceiling / owed-dial
  behaviour is exercised by handing it numbers.
- `CallKeeper` is the async shell around it: it owns the Call adapter, the one
  operation lock, the Briefer and the clock, and it does what the machine says.

The split exists because the reference implementation's version of this was one
object that could only be tested against a live call — the reason the silence
rule shipped counting one speaker instead of two for as long as it did (#184).

**One key, two reasons, one lock.** The reference implementation serialised
"still silent, then end" against everything else that touched the call with a
single lock (`legacy@1d32845:bridge/host.py:1793-1798`) — **ported** as this
object's `_operation_lock`. A dial that is landing while the ceiling is firing
is exactly the interleaving that lock exists to forbid.

**The one-call rule is encoded once, here.** It is the machine's `_call_id`:
`wake` yields nothing while one is set, and the Live Toggle ends rather than
opens. What replaced the interlock's *refusal* is the fact that there is now one
caller — the Keeper itself — so there is no second door to refuse at.

Legacy (ADR 0010): the silence rule counting both sides on a wall clock
(`legacy@1d32845:bridge/livecall.py:16-18,102-105`) — **ported**. Duty off ⇒ the
event is suppressed and never queued (`legacy@1d32845:bridge/host.py:2100-2101`)
— **ported**, as `wake`'s first refusal. Duty off ⇒ no auto hang-up
(`legacy@1d32845:bridge/host.py:1867-1872`) — **dropped, because** the ceiling
is the call's own limit and the Auto Hang-up Switch is what answers for it
(`CONTEXT.md`, *Auto Hang-up Switch*). A failed notice retained for the next
round (`legacy@1d32845:bridge/coordinator.py:1014-1019`) — **adapted** to the
owed dial, which is re-armed by an event and paid from a fresh reading rather
than replayed. Cool-down itself is **new**: legacy had none, and
`legacy@1d32845:bridge/livecall.py:561-581` is the incident that says why.

Mid-call news (#196) has the same accounting. Legacy paced what it said into a
live call with a one-at-a-time FIFO and supersession — a queued notice replaced
by a newer one about the same Session
(`legacy@1d32845:bridge/coordinator.py:1306-1337`,
`legacy@1d32845:bridge/store.py:2768-2814`). The supersession is **adapted**
into the fresh reading: this module keeps one flag and no queue, and what is
said is `focus_brief()`'s answer at the moment of speaking, which supersedes
every event since the flag was armed without holding any of them. Pacing by an
interval is **new** — legacy paced by the queue draining, which is not a pace at
all on a wire with no silent mid-call path (#175). The EVENT cue and the Focus
Session itself: **legacy has no such behaviour**, so there is nothing to port —
it had one call, one queue and no notion of which Session the user was on.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from gpt_voicecoding.core.adjudication import SwitchAdjudicator
from gpt_voicecoding.core.clock import Clock, default_clock
from gpt_voicecoding.core.policy import CorePolicy
from gpt_voicecoding.seams.call import (
    CallAdapter,
    CallDropped,
    CallEnded,
    CallEvent,
    CallSnapshot,
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
from gpt_voicecoding.seams.delivery import Delivery
from gpt_voicecoding.seams.identity import new_request_id

_log = logging.getLogger(__name__)

#: What the run is told when the Silence Ceiling closes a call, and the only
#: witness a whole-lane run has for it: an engine that says nothing when the
#: ceiling fires leaves a run no way to tell "the call outlived the ceiling"
#: from "the ceiling never ran". `tests/acceptance/journey.py::CEILING_END_LINE`
#: greps it, which is why the wording is a constant and not a sentence rewritten
#: at the call site.
CEILING_END_LINE = "ended the Live Call after %g seconds without call activity"

#: What the run is told when an event arrives inside a Cool-down, and when the
#: Cool-down then pays what it owed. Cool-down is the one rule here with no
#: surface of its own — a call that does *not* happen leaves no cue, no snapshot
#: and no wrapper run — so these two lines are the only witness a whole-lane run
#: has for it, and `tests/acceptance/journey.py` greps them.
COOL_DOWN_OWED_LINE = "a Cool-down is running for another %g seconds; one dial is owed"
COOL_DOWN_PAID_LINE = "the Cool-down elapsed; dialling on a fresh reading"

#: What the run is told about mid-call news. The gap and the interval have no
#: surface of their own either — an announcement that *waits* leaves no cue and
#: no snapshot — so these three lines are what a whole-lane run reads the rule
#: off, and `tests/acceptance/journey.py` greps them.
MID_CALL_SPOKEN_LINE = "spoke the Focus Session's brief into the gap in the Live Call: %s"
MID_CALL_NOTHING_LINE = (
    "the gap came and the Focus Session no longer needs the user; nothing is spoken"
)
MID_CALL_UNDELIVERED_LINE = "the Live Call would not carry the Focus Session's brief: %s"

#: Why a call the user opened exists, and the *whole* hand-over it gets (#167
#: Q6). A user who pressed the toggle is about to say what they want; briefing
#: them on the roster they were looking at when they pressed it would be the
#: system talking first, on a call the user opened to talk.
USER_OPENED = "The user opened this call. Wait to be spoken to, then act on what they ask for."


@runtime_checkable
class Briefer(Protocol):
    """Who needs the user, read **now** — the Keeper's one seam.

    The Keeper decides *when* to sound and never *what* is said, so everything
    about Sessions reaches it through this one verb. Two adapters: the
    production one over `Briefing` and the Session registry, and a fake in this
    module's own tests, which is what lets the whole of Cool-down and the
    Silence Ceiling be proved with no roster in sight (#167).

    Asked at the moment of acting and never at the moment of the event, which is
    ADR 0017: a call the system dials is briefed from a fresh reading, never
    from the event that provoked it. By the time a Cool-down elapses the wait
    that armed the owed dial may have been answered at the terminal.
    """

    def handover(self) -> tuple[HandoverItem, ...] | None:
        """The hand-over a system-dialled call comes up holding, or None.

        `None` means **nobody needs the user** — not "nothing to say about the
        Session that woke me". It is the answer that cancels an owed dial, so a
        Cool-down that elapses onto a quiet machine ends in silence rather than
        in a call about nothing.

        The items are `Dial.hand_over`'s own, exactly as #194 shapes them: the
        `SpokenBrief` and friends `Briefing` builds. There is no type between
        the two, because a second one would be a second place that decides what
        a hand-over is made of.
        """
        ...

    def focus_brief(self) -> SpokenBrief | None:
        """The Focus Session as it stands **now**, or None if it is past needing the user.

        The mid-call counterpart of `handover`, and read on the same terms: at
        the moment of sounding, never at the moment of the event (ADR 0017). A
        word owed to the Focus Session cannot go stale, because it is composed
        when it is spoken — so the answer to "the Session was answered at the
        terminal while the Voice was mid-sentence" is `None` here, and the
        Keeper says nothing at all.

        A `SpokenBrief` and not Briefing's own `SessionBrief`: the Keeper knows
        nothing of what is said (`CONTEXT.md`, *Call Keeper*), so what crosses
        this Protocol is the thing the Call seam already carries (#194,
        `seams/call.py::speak`) and the mapping is the production adapter's, as
        it already is for `handover`'s items.
        """
        ...


@dataclass(frozen=True, slots=True)
class Permits:
    """What the switches allow, read at the moment of acting.

    Passed *in* rather than read here, because `CallTime` is pure. Both fields
    are the adjudicator's own answers (`core/adjudication.py`), and they are
    separate because they are separately governed: the Auto Hang-up Switch
    stands beside Duty rather than under it, so the ceiling still fires on a
    call the user opened with Duty off (`CONTEXT.md`).
    """

    #: Duty ∧ Voice: may the system touch the Live Call unbidden.
    dial: bool
    #: The Auto Hang-up Switch: may the Silence Ceiling end the call.
    hang_up: bool


@dataclass(frozen=True, slots=True)
class Dialling:
    """Open a call. `user_opened` says which of the two kinds it is.

    A user-opened call carries the single `USER_OPENED` item and is never
    briefed; a system-dialled one carries whatever the Briefer answers with, and
    is cancelled when that answer is `None`.
    """

    user_opened: bool = False


@dataclass(frozen=True, slots=True)
class Ending:
    """End the call. `ceiling` says which of the two asked.

    The Silence Ceiling is the one the run has to be *told* about: an engine
    that says nothing when it fires leaves a whole-lane run no way to tell "the
    call outlived the ceiling" from "the ceiling never ran". The Live Toggle
    needs no line — the user pressed it and the control plane answered them.
    """

    ceiling: bool = False


@dataclass(frozen=True, slots=True)
class Sounding:
    """Mark one moment of the call with a cue."""

    cue: Cue


@dataclass(frozen=True, slots=True)
class Speaking:
    """Say a word to the Focus Session, from a reading taken now.

    Carries nothing, and that is the whole point: the machine decides *that* the
    gap is open and the interval has run, and what is actually said is the
    Briefer's answer at the instant the shell asks. An act that carried a brief
    would be a brief the machine had been holding since the event, which is the
    replay ADR 0017 forbids.
    """


#: The closed set of things the state machine asks the shell to do. An empty
#: tuple is the answer beside them — *nothing* — and it is spelled as an absence
#: rather than as a member, because a `Nothing()` act would be a thing the shell
#: has to look at and then not do.
Act = Dialling | Ending | Sounding | Speaking


@dataclass(frozen=True, slots=True)
class KeeperStatus:
    """What the Keeper is holding right now, for whoever asks."""

    #: The call the system owns, or None. Opaque; only compared for identity.
    call_id: str | None
    #: Seconds of Cool-down left, or 0.0 when none is running.
    cool_down_remaining: float
    #: Whether an event inside a Cool-down bought a dial that has not been paid.
    dial_owed: bool


class CallTime:
    """The Call Keeper's rules, as a pure function of events and the clock.

    Every entry takes `now` and returns the acts to perform. Nothing here
    awaits, opens, ends or reads a clock: a test drives it by handing it
    seconds, which is the only way the Cool-down, the ceiling and the settle
    window can be proved without sixty seconds of real time each.

    The shell reports back what its acts produced — `dialled`, `ended`,
    `nothing_to_say` — because an act's *outcome* is a fact only the shell can
    see, and a machine that assumed its own dial landed would be a machine that
    loses a call the network refused.
    """

    def __init__(
        self,
        *,
        cool_down_seconds: float,
        silence_end_seconds: float,
        speech_settle_seconds: float,
    ) -> None:
        self._cool_down_seconds = cool_down_seconds
        self._silence_end_seconds = silence_end_seconds
        self._speech_settle_seconds = speech_settle_seconds
        self._call_id: str | None = None
        self._last_activity_at: float | None = None
        self._voice_speaking = False
        self._user_speaking = False
        #: When both sides last fell quiet, or None while either is speaking.
        #: The settle window is measured from here, so a pause only starts
        #: counting as silence once it has been a pause for a while.
        self._quiet_at: float | None = None
        #: One ceiling attempt per call. A call the adapter refused to end is
        #: not tried again until something about the call changes.
        self._ceiling_attempted_for: str | None = None
        self._cool_down_until: float | None = None
        self._dial_owed = False
        #: **One "last sounded at" for rings and Focus announcements alike**
        #: (#196), and it is paced by `cool_down_seconds` rather than by a dial
        #: of its own: mid-call news and a fresh call are the same interruption
        #: at two volumes, and Simon settled it with the ticket's own words —
        #: 还是沿用那个间隔的配置. None until this call has sounded once.
        self._last_sounded_at: float | None = None
        #: Whether a word is owed to the Focus Session. One flag, not a queue:
        #: three events during one utterance buy one brief, because what is
        #: spoken is a reading taken at the gap and not the events themselves.
        self._focus_owed = False
        #: When this call last had nobody speaking on it — the dial, or the
        #: moment the later of the two speakers stopped — and None while either
        #: is speaking. The gap is measured from here and from **nothing else**:
        #: the run's landed-facts note on #196 is explicit that the gap is not
        #: built on `UserSpeech(text)`, which is the finished transcript and
        #: often lands only at hand-off or teardown (#194). `_last_activity_at`
        #: is fed by that transcript, so a gap measured from it would be pushed
        #: out by a late transcript of an utterance that had already ended.
        self._gap_since: float | None = None

    @property
    def silence_end_seconds(self) -> float:
        """The ceiling this machine is measuring, for the line that reports it."""
        return self._silence_end_seconds

    # -- what the outside asks -------------------------------------------

    def wake(self, now: float, permits: Permits, *, focus: bool) -> tuple[Act, ...]:
        """Something happened that could be worth a call.

        `focus` says only whether the event concerns the Focus Session — never
        what it was about, and never a brief. Who needs the user is read off the
        Briefer at the moment of acting, in both directions: a call is dialled
        on a fresh hand-over, and a word is spoken from a fresh `focus_brief`.

        Four answers, in the order they are decided:

        - Duty or Voice off: **nothing, and nothing recorded.** Legacy suppressed
          the event rather than queueing it (`legacy@1d32845:bridge/host.py:2100-2101`),
          and a later flip is a fresh `wake` on its own — so an owed dial or an
          owed word written here would be the queue that rule exists to refuse.
          Mid-call this is the whole of "Duty or Voice off mid-call": no ring, no
          announcement, and the call stays up with the ceiling still running.
        - A call is already up: **mid-call news** (#196). The Focus Session earns
          a word owed, spoken in the first gap an interval after the last mid-call
          sound; every other Session earns the EVENT cue and nothing more, folded
          to one ring per interval, and the user asks with `brief`. The one thing
          that must not happen meanwhile is a second call.
        - Inside a Cool-down: one dial becomes **owed**, and only one. Three
          events buy one call, because what is paid out at expiry is a fresh
          reading of the whole roster and not the events themselves.
        - Otherwise: dial.
        """
        if not permits.dial:
            return ()
        if self._call_id is not None:
            return self._mid_call(now, permits, focus=focus)
        if self._cooling_down(now):
            self._dial_owed = True
            _log.info(COOL_DOWN_OWED_LINE, self._cool_down_until - now)  # type: ignore[operator]
            return ()
        return (Dialling(),)

    def toggled(self, now: float) -> tuple[Act, ...]:
        """The user pressed the Live Toggle. Never gated, and Cool-down does not apply.

        `CONTEXT.md`'s *Cool-down* ends on exactly this sentence: "The user's own
        Live Toggle is not subject to it." The switches do not reach here either
        (ADR 0002, and `core/adjudication.py`'s boundary): this is the user
        touching the call with the system as the instrument.
        """
        del now  # the toggle reads no clock; it reads whether a call is up
        if self._call_id is not None:
            return (Ending(),)
        return (Dialling(user_opened=True),)

    def tick(self, now: float, permits: Permits) -> tuple[Act, ...]:
        """The one-second clock: Cool-down expiry, the owed word, then the Ceiling.

        The first and the last cannot both fire — an owed dial is only paid with
        no call up, and the ceiling only measures one that is — so the order
        between them is documentation rather than precedence.

        **This is where a gap is noticed.** The falling edge of an utterance is
        never itself a gap: the settle window has to run out first, and nothing
        else happens on a call where both sides have stopped talking. So the
        word owed to the Focus Session is paid on the clock, and the edges only
        re-ask in case the window had already passed.

        **The ceiling wins a tick they would share.** A call about to be ended
        for silence is not a call to start an utterance in, and the ceiling's
        sixty seconds are twelve settle windows: they can only coincide when
        the word was owed and the gap opened in the same second the call ran
        out, and then the ending is what the user is owed.
        """
        acts: list[Act] = []
        acts.extend(self._cool_down_elapsed(now, permits))
        ceiling = self._ceiling(now, permits)
        if not ceiling:
            acts.extend(self._owed_word(now, permits))
        acts.extend(ceiling)
        return tuple(acts)

    def heard(self, event: CallEvent, now: float, permits: Permits) -> tuple[Act, ...]:
        """One event from the Call seam. Cues live here; nothing else does.

        **CONNECTED when the dial comes up, ENDED on any end** — and the cue is
        deliberately not conditional on this object still holding the call. What
        the user is owed is the sound of the call *they* were on ending, and
        whether the system was still the one holding it is a bookkeeping
        question they cannot hear (#186).

        The speaking edges take `permits` because a word owed to the Focus
        Session is re-asked on each of them (#196). It is almost always the
        clock that pays it — the settle window has not run out at the instant an
        utterance ends — but an edge that arrives after the window has already
        passed is a gap opening, and the tick would only be the second to see it.
        """
        match event:
            case CallEnded() | CallDropped():
                if event.call_id == self._call_id:
                    self.ended(now)
                return (Sounding(Cue.ENDED),)
            case UserSpeech():
                self._note_activity(now)
                return ()
            case UserSpeaking():
                self._note_speech(now, user=event.speaking)
                return self._owed_word(now, permits)
            case VoiceSpeech():
                self._note_speech(now, voice=event.speaking)
                return self._owed_word(now, permits)
            case CallStarted():
                # Adopted whoever opened it: one voice surface, one holder.
                self.dialled(now, call_id=event.call_id)
                return (Sounding(Cue.CONNECTED),)

    def status(self, now: float) -> KeeperStatus:
        """The three facts the control plane and the hub read off this object."""
        remaining = 0.0
        if self._cool_down_until is not None:
            remaining = max(0.0, self._cool_down_until - now)
        return KeeperStatus(
            call_id=self._call_id, cool_down_remaining=remaining, dial_owed=self._dial_owed
        )

    # -- what the shell reports back --------------------------------------

    def dialled(self, now: float, *, call_id: str | None) -> None:
        """What a dial produced: a call id, or None for one that did not come up.

        A failed dial is an end of a call as far as Cool-down is concerned
        (`CONTEXT.md`, *Cool-down*: "hung up, dropped, or a dial that failed"),
        and it clears the owed flag rather than keeping it: **one event buys one
        attempt.** Retrying on this system's own authority is what turned a
        refused call into a loop in the reference implementation.
        """
        self._dial_owed = False
        if call_id is None:
            self._start_cool_down(now)
            return
        self._call_id = call_id
        self._last_activity_at = now
        # Flags belong to the call they were raised on. Carrying one across
        # would pin the ceiling open on a call nobody is speaking into.
        self._voice_speaking = False
        self._user_speaking = False
        self._quiet_at = None
        self._ceiling_attempted_for = None
        # The gap is open from the dial: nobody is speaking on a call that has
        # just come up, and the settle window is what keeps the Voice's reading
        # of the hand-over from being cut into.
        self._gap_since = now
        self._forget_mid_call()

    def ended(self, now: float) -> None:
        """The call is over, however it ended. Cool-down starts here and only here."""
        self._call_id = None
        self._last_activity_at = None
        self._voice_speaking = False
        self._user_speaking = False
        self._quiet_at = None
        self._ceiling_attempted_for = None
        self._gap_since = None
        self._forget_mid_call()
        self._start_cool_down(now)

    def nothing_to_say(self, now: float) -> None:
        """The Briefer answered `None`: the owed dial is cancelled, and no more.

        No Cool-down starts, because nothing ended — no call was attempted and
        the machine is simply quiet. The next `wake` dials at once, which is
        right: it is a fresh event about a roster that has since changed.
        """
        del now
        self._dial_owed = False

    def spoke(self, now: float) -> None:
        """A brief was handed to the call — delivered, refused, or raised on.

        All three stamp the interval and clear the flag, by #195's rule that one
        event buys one attempt. A flag re-armed by its own failure would wait for
        the next gap holding a reading it had already taken, which is the replay
        ADR 0017 forbids in another shape; the next wake-worthy event arms it
        again, and that one is news.
        """
        self._focus_owed = False
        self._last_sounded_at = now

    def nothing_to_speak(self, now: float) -> None:
        """The Briefer answered `None` at the gap: the word is cancelled, silently.

        Nothing sounded, so the interval is **not** stamped: a ring the user
        never heard may not pace the next one. The flag goes because the reading
        answered it — the Session was seen to no longer need the user, which is
        also how a Focus Session that has ended clears what was owed to it.
        """
        del now
        self._focus_owed = False

    # -- the rules --------------------------------------------------------

    def _mid_call(self, now: float, permits: Permits, *, focus: bool) -> tuple[Act, ...]:
        """News while a call is up: a word for the Focus Session, a ring for the rest.

        The two are not alternatives to each other in the same instant. A Focus
        event arms the flag and then asks whether it can be paid *now*; anything
        else is the EVENT cue, and it is the whole of what the system says about
        another Session mid-call — the user asks with `brief` when they want to
        know which (#167 Q7-Q9).
        """
        if focus:
            self._focus_owed = True
            return self._owed_word(now, permits)
        if not self._interval_elapsed(now):
            return ()
        self._sounded(now)
        return (Sounding(Cue.EVENT),)

    def _owed_word(self, now: float, permits: Permits) -> tuple[Act, ...]:
        """Pay the word owed to the Focus Session, when the call will take one.

        Three conditions, and all three are about the call rather than about the
        Session: the switches still allow the system to touch it, both sides have
        fallen silent for the settle window, and one interval has passed since
        the last mid-call sound of either kind. What is *said* is decided
        afterwards, by the Briefer, which is why nothing here looks at a brief.

        The interval is not stamped here. The act may find that the Session no
        longer needs the user, and a word nobody heard may not pace the next one.
        """
        if not permits.dial or not self._focus_owed or self._call_id is None:
            return ()
        if not self._in_gap(now) or not self._interval_elapsed(now):
            return ()
        return (Speaking(),)

    def _in_gap(self, now: float) -> bool:
        """Whether both sides have been silent long enough to speak into.

        **The two speaking states and the settle window, and nothing else.** The
        window covers the human pause — a breath between two sentences is not a
        turn ending — and since #195 it covers only that, the transport's playout
        lag having been absorbed before `VoiceSpeech(speaking=False)` is raised.

        `_last_activity_at` is deliberately not consulted, though the Silence
        Ceiling measures from it: it is fed by `UserSpeech(text)`, the finished
        transcript, which since #194 often lands at hand-off or at teardown —
        long after the utterance it describes ended. A gap measured from it would
        be pushed out by news of a pause that was already over, and the run's
        landed-facts note on #196 says in as many words not to build the gap on
        that event.

        `_gap_since` starts at the dial, so a call is not spoken into the instant
        it connects: the Voice is about to read the hand-over it was dialled on,
        and the wire has no silent mid-call path to insert a second utterance
        through (#175).
        """
        if self._call_id is None or self._gap_since is None:
            return False
        if self._voice_speaking or self._user_speaking:
            return False
        return now >= self._gap_since + self._speech_settle_seconds

    def _interval_elapsed(self, now: float) -> bool:
        """Whether this call has been quiet of *system* sound for one interval."""
        return (
            self._last_sounded_at is None or now - self._last_sounded_at >= self._cool_down_seconds
        )

    def _sounded(self, now: float) -> None:
        self._last_sounded_at = now

    def _forget_mid_call(self) -> None:
        """Both mid-call facts belong to the call they were raised on.

        A word owed on a call that has ended is owed to nobody: the gap it was
        waiting for was that call's, and the next wake-worthy event dials a call
        of its own on a fresh reading. Carrying the interval across would pace
        the new call's first ring by a sound heard on the old one.
        """
        self._last_sounded_at = None
        self._focus_owed = False

    def _cooling_down(self, now: float) -> bool:
        return self._cool_down_until is not None and now < self._cool_down_until

    def _start_cool_down(self, now: float) -> None:
        self._cool_down_until = now + self._cool_down_seconds

    def _cool_down_elapsed(self, now: float, permits: Permits) -> tuple[Act, ...]:
        """Pay the owed dial when the Cool-down runs out, if it is still owed to anyone.

        **Duty ∧ Voice is judged here, not at `wake`.** A flip during a
        Cool-down changes the outcome, in both directions: switched off, the
        owed dial is dropped rather than held for a later flip — a later flip is
        a `wake` of its own, and holding it as well would ring twice for one
        event.
        """
        if self._cool_down_until is None or now < self._cool_down_until:
            return ()
        self._cool_down_until = None
        if not self._dial_owed:
            return ()
        self._dial_owed = False
        if not permits.dial or self._call_id is not None:
            return ()
        _log.info(COOL_DOWN_PAID_LINE)
        return (Dialling(),)

    def _ceiling(self, now: float, permits: Permits) -> tuple[Act, ...]:
        """The Silence Ceiling: end a call in which neither side has sounded.

        **Both sides, on a wall clock** — legacy counted them with one regex over
        both roles (`legacy@1d32845:bridge/livecall.py:16-18,102-105`), ported.
        The rewrite kept only the user half, and until this module the user half
        arrived only as a finished transcript.

        **Held while either side speaks**, which is the part a stamp cannot do:
        an answer generated in ten seconds and spoken over seventy-five is
        seventy-five seconds of call, and the span is still open, so there is no
        window to measure yet (#184).

        **The Auto Hang-up Switch gates it and nothing else does.** Asked here on
        each pass rather than latched, so a switch that comes back on ends a call
        that has been silent all along — the attempt this call is allowed has not
        been spent while the switch was off.
        """
        if not permits.hang_up:
            return ()
        if self._call_id is None or self._last_activity_at is None:
            return ()
        if self._voice_speaking or self._user_speaking:
            return ()
        if self._ceiling_attempted_for == self._call_id:
            return ()
        if now < self._silent_from() + self._silence_end_seconds:
            return ()
        self._ceiling_attempted_for = self._call_id
        return (Ending(ceiling=True),)

    def _silent_from(self) -> float:
        """The moment the current stretch of silence began counting.

        Activity restarts it. So does the *settle window*: for
        `speech_settle_seconds` after the last speaker stopped, the pause is
        still a pause and not silence — a human breath between two sentences is
        not the call going idle. The two are combined with `max` rather than
        chosen between, because a `UserSpeech` transcript can land after the
        speaking edge that carried it.
        """
        assert self._last_activity_at is not None  # the caller checked
        if self._quiet_at is None:
            return self._last_activity_at
        return max(self._last_activity_at, self._quiet_at + self._speech_settle_seconds)

    def _note_activity(self, now: float) -> None:
        """Restart the silence window, when there is a call for it to belong to."""
        if self._call_id is not None:
            self._last_activity_at = now

    def _note_speech(
        self, now: float, *, user: bool | None = None, voice: bool | None = None
    ) -> None:
        """One side started or stopped speaking. Both edges are activity.

        The start says the call is not idle; the stop is what the settle window
        is then measured from — the end of the utterance, not the moment before
        it began.
        """
        if self._call_id is None:
            return
        if user is not None:
            self._user_speaking = user
        if voice is not None:
            self._voice_speaking = voice
        self._note_activity(now)
        speaking = self._user_speaking or self._voice_speaking
        self._quiet_at = None if speaking else now
        self._gap_since = None if speaking else now


class CallKeeper:
    """The async shell: the Call adapter, one lock, the Briefer, and the clock.

    Five entries, and no content passes through any of them. `live_toggle`,
    `wake`, `tick`, `heard` and `status` are the whole surface. Mid-call news
    (#196) added no sixth: what is spoken into a gap is the Briefer's answer at
    the moment of speaking, so it arrives through the same one seam the dial's
    hand-over does and never through a caller's argument.
    """

    def __init__(
        self,
        *,
        call: CallAdapter,
        briefer: Briefer,
        adjudicator: SwitchAdjudicator,
        dial_for: Callable[[tuple[HandoverItem, ...]], Dial],
        policy: CorePolicy | None = None,
        clock: Clock = default_clock,
    ) -> None:
        self._call = call
        self._briefer = briefer
        self._adjudicator = adjudicator
        #: How this Keeper builds a `Dial` around a hand-over: one callable, the
        #: hub's, because both audiences' instruction sets are generated by
        #: Bridge Core and it is their only source (ADR 0018). A Keeper that held
        #: them would be a second place that knows how a call is composed, and
        #: it would have to be told every time the hub regenerated them.
        self._dial_for = dial_for
        self._clock = clock
        dials = policy or CorePolicy()
        self._time = CallTime(
            cool_down_seconds=dials.cool_down_seconds,
            silence_end_seconds=dials.silence_end_seconds,
            speech_settle_seconds=dials.speech_settle_seconds,
        )
        #: One key, two reasons (`legacy@1d32845:bridge/host.py:1793-1798`). A
        #: dial that is landing while the ceiling is firing is the interleaving
        #: this forbids, and it is the same lock for both.
        self._operation_lock = asyncio.Lock()

    async def live_toggle(self) -> CallSnapshot:
        """End the call the system owns, or start one if none is up.

        **Never gated, and Cool-down does not apply.** The switches constrain
        what the system does unbidden; this is the user acting (ADR 0002). The
        indefensible case is the one that settles it: Voice flipped off while a
        call is up, and the user's own "end this call" refused by the very
        switch that says the system should be quiet.

        A call opened here gets no hand-over beyond the single line saying the
        user opened it (#167 Q6): they pressed the toggle in order to talk, and
        briefing them on the roster they were already looking at would be the
        system speaking first.
        """
        async with self._operation_lock:
            now = self._clock()
            snapshot = await self._perform(self._time.toggled(now), now)
            return snapshot if snapshot is not None else CallSnapshot(state=CallState.DOWN)

    async def wake(self, *, focus: bool) -> None:
        """Something wake-worthy happened — a question, a permission, a finished turn.

        Also a Duty or Voice switch turning on: an outlet becoming available is
        one more reason to look at who needs the user, and it arrives here as a
        `wake` rather than as a mechanism of its own.
        """
        async with self._operation_lock:
            now = self._clock()
            await self._unattended(self._time.wake(now, self._permits(), focus=focus), now)

    async def tick(self, now: float) -> None:
        """The one-second clock. The composition root calls this on a timer."""
        async with self._operation_lock:
            await self._unattended(self._time.tick(now, self._permits()), now)

    async def heard(self, event: CallEvent) -> None:
        """One event the Call seam raised. Recorded, and cued."""
        async with self._operation_lock:
            now = self._clock()
            await self._unattended(self._time.heard(event, now, self._permits()), now)

    def status(self) -> KeeperStatus:
        """The call id, the Cool-down remaining, and whether a dial is owed.

        Not locked and not async: three reads of plain fields, on the path the
        control plane answers `status` from, which must never wait on a call
        operation to say what is going on (ADR 0002).
        """
        return self._time.status(self._clock())

    # -- doing what the machine says --------------------------------------

    def _permits(self) -> Permits:
        return Permits(
            dial=self._adjudicator.may_touch_call(),
            hang_up=self._adjudicator.may_auto_hangup(),
        )

    async def _unattended(self, acts: tuple[Act, ...], now: float) -> None:
        """Perform acts on the system's own initiative: nobody is holding the answer.

        The three entries that are not the Live Toggle have no caller waiting on
        a snapshot, so a refused dial or an end the adapter would not perform is
        written down and dropped here. It is never *retried*: the machine has
        already spent this call's ceiling attempt or this event's dial, and a
        retry on the system's own authority is what turned a refused call into a
        loop in the reference implementation.
        """
        try:
            await self._perform(acts, now)
        except Exception:  # noqa: BLE001 - the Keeper outlives one refused operation
            _log.exception("the Call adapter refused an operation the Keeper asked for")

    async def _perform(self, acts: tuple[Act, ...], now: float) -> CallSnapshot | None:
        """Carry out the machine's acts in order, and report the call's new state."""
        snapshot: CallSnapshot | None = None
        for act in acts:
            match act:
                case Sounding():
                    await self._play(act.cue)
                case Speaking():
                    await self._say_what_stands_now(now)
                case Ending():
                    if act.ceiling:
                        _log.info(CEILING_END_LINE, self._time.silence_end_seconds)
                    snapshot = await self._end(now)
                case Dialling():
                    snapshot = await self._open(now, user_opened=act.user_opened)
        return snapshot

    async def _open(self, now: float, *, user_opened: bool) -> CallSnapshot | None:
        """Bring a call up, on a hand-over read at this moment and never before.

        The Briefer answering `None` is not a failure: nobody needs the user, so
        the owed dial is cancelled and no Cool-down starts. Everything else that
        goes wrong is a **failed dial** — no instructions to open on, an adapter
        that raised, or a snapshot that is not `UP` — and all three land in the
        same place, because "one event buys one attempt" does not distinguish
        between them.
        """
        hand_over: tuple[HandoverItem, ...]
        if user_opened:
            hand_over = (DialReason(text=USER_OPENED),)
        else:
            fresh = self._briefer.handover()
            if fresh is None:
                _log.info("the Cool-down elapsed and nobody needs the user; the dial is cancelled")
                self._time.nothing_to_say(now)
                return None
            hand_over = fresh
        try:
            dial = self._dial_for(hand_over)
            snapshot = await self._call.ensure_call(dial)
        except Exception:
            # A dial that could not be built — no instructions to open on — or an
            # adapter that raised. Both are a failed dial, recorded before the
            # refusal travels on: the Live Toggle's caller is owed the reason
            # (the control plane words `CallInstructionsMissing` for the user),
            # and the system's own paths log it and carry on (`wake`, `tick`).
            self._time.dialled(now, call_id=None)
            raise
        # A snapshot that is not UP is deliberately **not** claimed: claiming a
        # call that never arrived would bar the dial that fixes it.
        self._time.dialled(now, call_id=snapshot.call_id if snapshot.is_up else None)
        return snapshot

    async def _say_what_stands_now(self, now: float) -> None:
        """Speak the Focus Session's brief into the gap, read at this instant.

        The reading is taken here and nowhere earlier (ADR 0017): the wait that
        armed the word may have been answered at the terminal while the Voice was
        mid-sentence, and the answer to that is silence rather than an
        announcement about a Session that is past needing anyone.

        `Briefing.text` is never on this path. What crosses is the seam's own
        `SpokenBrief` (#194) and the Voice words it (`CONTEXT.md`, *Session
        Brief*), so nothing here phrases anything.

        **The attempt is stamped whether or not it lands.** The interval is
        recorded before the refusal travels on, so an adapter that raises cannot
        be asked again in the same gap; `_unattended` writes down what happened.
        """
        brief = self._briefer.focus_brief()
        if brief is None:
            _log.info(MID_CALL_NOTHING_LINE)
            self._time.nothing_to_speak(now)
            return
        try:
            receipt = await self._call.speak(brief, request_id=new_request_id())
        finally:
            self._time.spoke(now)
        if receipt.outcome is Delivery.DELIVERED:
            # The Session's name, and nothing else off the brief: a whole-lane
            # run has no other way to tell *which* Session was announced, and
            # the name is what the user would have heard first.
            _log.info(MID_CALL_SPOKEN_LINE, brief.name)
        else:
            _log.info(MID_CALL_UNDELIVERED_LINE, receipt.reason)

    async def _end(self, now: float) -> CallSnapshot | None:
        """End the call, and let go of it here rather than on the event that follows.

        The adapter raises `CallEnded` of its own accord and that event plays the
        cue; what it must not do is start a second Cool-down, so the release is
        recorded once, at the moment the ending was asked for.
        """
        snapshot = await self._call.end_call()
        self._time.ended(now)
        return snapshot

    async def _play(self, cue: Cue) -> None:
        """Ask the Call adapter to mark one moment with a sound.

        The Keeper names the moment and never the sound: which notes, how loud
        and how long were chosen by ear against one machine's speakers (#174).

        A shipped adapter swallows its own playback failures, so this guard is
        for a **defective** one — and a defective adapter may not stop the act it
        was called from (#186). What follows an ENDED cue is the call being
        released, and a missing tone is not a reason to keep one held.
        """
        try:
            await self._call.play_cue(cue)
        except Exception:  # noqa: BLE001 - a sound may not take down the call it marks
            _log.exception("the Call adapter raised on the %s cue", cue)

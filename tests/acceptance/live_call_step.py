"""The selectable phases of the Live Call acceptance walk (#223)."""

from __future__ import annotations

import os
import re
import time
import tomllib
from collections.abc import Callable, Container, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Protocol

import hand_started
import journey as journey_module
import live_call
import support
from support import LaneBlocked, StepFailed

from gpt_voicecoding.adapters.call.realtime import cues
from gpt_voicecoding.adapters.call.realtime.adapter import VOICE_SAID_LINE
from gpt_voicecoding.core.bridge import VOICE_QUIET_LINE, VOICE_SPEAKING_LINE
from gpt_voicecoding.core.call_keeper import (
    CARRIED_UNDELIVERED,
    COOL_DOWN_OWED_LINE,
    COOL_DOWN_PAID_LINE,
    MID_CALL_NOTHING_LINE,
    MID_CALL_SPOKEN_LINE,
)
from gpt_voicecoding.core.lifecycle import Lifecycle
from gpt_voicecoding.core.policy import (
    DEFAULT_COOL_DOWN_SECONDS,
    DEFAULT_SILENCE_END_SECONDS,
    DEFAULT_SPEECH_SETTLE_SECONDS,
)
from gpt_voicecoding.seams.call import Cue, SpokenBrief, SpokenRosterBrief
from gpt_voicecoding.seams.control_plane import Action

ACKNOWLEDGE = journey_module.ACKNOWLEDGE
ASK_A_QUESTION = journey_module.ASK_A_QUESTION
ASK_A_QUESTION_THEN_SAY = journey_module.ASK_A_QUESTION_THEN_SAY
DISCOVERY_SECONDS = journey_module.DISCOVERY_SECONDS
Instruction = journey_module.Instruction
QUESTION_ASKED_SPOKEN_SUBSTRING = journey_module.QUESTION_ASKED_SPOKEN_SUBSTRING
THE_QUESTION_ASKED = journey_module.THE_QUESTION_ASKED
UNDELIVERED = journey_module.UNDELIVERED
_address_of = journey_module._address_of
_history_reading = journey_module._history_reading
_newest_message = journey_module._newest_message
_receipt_fields = journey_module._receipt_fields


PHASES = (
    "dial",
    "hand-over",
    "relay",
    "detail",
    "history",
    "long answer",
    "mid-call news",
    "hang-up",
    "undelivered",
)

PHASE_GROUND: Mapping[str, tuple[str, ...]] = {
    "dial": (),
    "hand-over": ("dial",),
    "relay": ("dial",),
    "detail": ("dial", "relay"),
    "history": ("dial", "relay"),
    "long answer": ("dial",),
    "mid-call news": ("dial", "relay"),
    "hang-up": ("dial",),
    "undelivered": (),
}


class UnknownPhase(Exception):
    """`--phase` named something that is not a Live Call phase."""


class _ObservationSink(Protocol):
    def observe(self, what: str, detail: str) -> None: ...


def _facts_text(facts: Mapping[str, object]) -> str:
    return ", ".join(f"{name}={value!r}" for name, value in facts.items()) or "none"


class _PhaseStopped(Exception):
    """A phase cannot collect meaningful downstream facts after one false grade."""


@dataclass(slots=True)
class _PhaseFacts:
    """Named facts collected before the phase's one fixed record outlet (#223)."""

    disposition: str
    phase: str = ""
    graded: dict[str, object] = field(default_factory=dict)
    recorded: dict[str, object] = field(default_factory=dict)
    failed: list[str] = field(default_factory=list)
    failure: str = ""

    def check(self, name: str, value: object, complaint: str) -> None:
        destination = self.graded if self.disposition == "graded" else self.recorded
        destination[name] = value
        if not value:
            self.failed.append(name)
            self.failure = complaint
            raise _PhaseStopped

    def record(self, name: str, value: object) -> None:
        self.recorded[name] = value


@dataclass(frozen=True)
class _PhaseResult:
    """One phase's single, fixed-shape record outlet."""

    phase: str
    rule: str
    source: str
    graded: Mapping[str, object]
    recorded: Mapping[str, object]
    engine_held: str
    failed: tuple[str, ...] = ()

    def finish(self, journey: _ObservationSink, *, disposition: str, blocked: bool = False) -> str:
        detail = (
            f"{disposition} phase {self.phase}; rule: {self.rule}; source: {self.source}; "
            f"graded facts: {_facts_text(self.graded)}; "
            f"recorded facts: {_facts_text(self.recorded)}; engine held: {self.engine_held}; "
            f"failed facts: {', '.join(self.failed) or 'none'}"
        )
        journey.observe(f"live call {self.phase}", detail)
        if self.failed:
            error = LaneBlocked if blocked or disposition == "arranged" else StepFailed
            raise error(detail)
        return detail


@dataclass(frozen=True, slots=True)
class _PhaseOutcome:
    """A completed phase held until step-level transport evidence is available."""

    result: _PhaseResult
    disposition: str
    blocked: bool = False


@dataclass(frozen=True)
class PhaseSelection:
    """The phases graded by a run and the ground arranged beneath them."""

    graded: tuple[str, ...]
    arranged: tuple[str, ...]

    @property
    def phases(self) -> tuple[str, ...]:
        chosen = set(self.graded) | set(self.arranged)
        return tuple(phase for phase in PHASES if phase in chosen)

    def is_graded(self, phase: str) -> bool:
        return phase in self.graded


def select_phases(names: Sequence[str] | None = None) -> PhaseSelection:
    """Select every phase when no narrower phase was requested."""
    asked = tuple(dict.fromkeys(names or ()))
    if not asked:
        return PhaseSelection(graded=PHASES, arranged=())
    unknown = [name for name in asked if name not in PHASE_GROUND]
    if unknown:
        raise UnknownPhase(
            f"no such live call phase: {', '.join(repr(name) for name in unknown)}. "
            f"The phases are: {', '.join(PHASES)}."
        )
    graded = tuple(phase for phase in PHASES if phase in set(asked))
    needed: set[str] = set()
    pending = list(graded)
    while pending:
        for one in PHASE_GROUND[pending.pop()]:
            if one not in needed:
                needed.add(one)
                pending.append(one)
    arranged = tuple(phase for phase in PHASES if phase in needed - set(graded))
    return PhaseSelection(graded=graded, arranged=arranged)


#: The engine's own line for what the user said (`core/bridge.py:796`). It is the
#: only line either call event has: `CallStarted` and `CallEnded` are noted into
#: the interlock and never logged (`core/bridge.py:777-785`), which is why the
#: `live call` step reads those two off the control plane instead.
USER_SPEECH_LINE = r"user speech, for the voice thread to act on"

#: The request-derived fragment matched after removing recogniser-added spaces
#: from both sides (#181, #223 story 16).
LIVE_CALL_HEARD_SUBSTRING = live_call.HEARD_FRAGMENTS[live_call.PLAIN]

#: The same, for the long-answer variant (`live_call.LONG_REQUEST`), which has no
#: hang-up in it to end with. Its own middle, for the same reason: a fragment,
#: spaceless, and one the recogniser has no word boundary to insert inside.
LIVE_CALL_LONG_HEARD_SUBSTRING = live_call.HEARD_FRAGMENTS[live_call.LONG]

#: The same, for the hand-over variant (`live_call.NEEDS_REQUEST`). Its own
#: middle again, and spaceless: "需要我" is what survives a recogniser that puts a
#: boundary anywhere in a four-character question.
LIVE_CALL_NEEDS_HEARD_SUBSTRING = live_call.HEARD_FRAGMENTS[live_call.NEEDS]

#: The same, for the narrowing that follows it (#198). Its own middle again: the
#: Session's name is in this sentence, but the name is what the *Call Agent* and
#: the Voice have to get right, and this line only asks whether the words landed.
LIVE_CALL_NARROWING_HEARD_SUBSTRING = live_call.HEARD_FRAGMENTS[live_call.NARROWING]

#: What a counted Roster Brief has to carry, as the Voice is told to say it:
#: *"give the counts rather than the list … how many have finished, how many are
#: still working"* (`core/instructions/voice.py`, #167 Q7-Q9). A **number**, and
#: the numbers are Chinese numerals because the Voice answers this user in the
#: language they are speaking. `个` is the measure word for the count, so a
#: numeral followed by it is the smallest distinction from a named list.
ROSTER_COUNT_PATTERN = r"[一二三四五六七八九十两0-9]+\s*个"

#: How the engine says a call it is opening is carrying a briefing (#194). The
#: hand-over rides `initialItems`, which the Voice holds **silently** and never
#: repeats, so a call that came up holding nothing is indistinguishable from one
#: that came up holding the whole roster — except for this line, which says how
#: many items went and of what kind (`adapters/call/realtime/adapter.py`).
HAND_OVER_LINE = r"dialling a call holding \d+ hand-over item"

#: The two hand-over kinds #198's phase 1 asks that dial to have carried, named
#: by their own classes rather than spelled here: the adapter writes
#: `type(item).__name__` into the line, so a rename that this harness had its own
#: copy of would go on passing against a kind the product no longer sends
#: (`seams/call.py`, `adapters/call/realtime/adapter.py`).
ROSTER_BRIEF_KIND = SpokenRosterBrief.__name__
SESSION_BRIEF_KIND = SpokenBrief.__name__

#: The option that makes a History read a *page* read (#171). Graded on the Call
#: Agent's own argv, because which entry its cursor landed on is its business and
#: that it paged at all is the fact the map's destination asks for.
HISTORY_CURSOR_OPTION = "--before"

#: The two lines the Call Keeper writes about a Cool-down, as patterns. Cool-down
#: is the one rule with no surface of its own — a call that does *not* happen
#: leaves no cue, no snapshot and no wrapper run — so the engine's own account is
#: the only witness. Built from the format strings themselves, so a wording
#: change breaks the grep at the source rather than silently passing (#195).
COOL_DOWN_OWED_PATTERN = re.escape(COOL_DOWN_OWED_LINE.split("%g")[0])
COOL_DOWN_PAID_PATTERN = re.escape(COOL_DOWN_PAID_LINE)

#: The two lines the Call Keeper writes about mid-call news (#196), as patterns.
#: The gap and the interval have no surface of their own either — an
#: announcement that *waits* leaves no cue, no snapshot and no wrapper run — so
#: these are the run's only witness, and they are built from the format strings
#: for `COOL_DOWN_OWED_PATTERN`'s reason.
MID_CALL_SPOKEN_PATTERN = re.escape(MID_CALL_SPOKEN_LINE.split("%s")[0])
MID_CALL_NOTHING_PATTERN = re.escape(MID_CALL_NOTHING_LINE)

#: A mid-call announcement whose brief said the user's last reply never arrived
#: (#197). The Keeper writes one code-shaped token at the end of the line it
#: already writes, so this is that line with the token pinned; the *sentence* is
#: Briefing's and is spoken, never logged.
MID_CALL_UNDELIVERED_PATTERN = (
    rf"{re.escape(MID_CALL_SPOKEN_LINE.split('%s')[0])}.*undelivered={CARRIED_UNDELIVERED}"
)

#: What the Voice actually said, as the realtime adapter writes it down (#197).
VOICE_SAID_PATTERN = re.escape(VOICE_SAID_LINE.split("%s")[0])

#: What #173 §6 pins in the sentence the user hears when their last reply never
#: arrived — "你上次的回复没送到，因为…". Matched as a **substring per #181**,
#: and the substring is the negated verb rather than the whole phrase, because
#: the Voice composes the sentence and this run does not get to predict it.
#:
#: Mandarin and Cantonese negations vary, so pinning one spelling would grade
#: the product's own language rule — a Session
#: Brief is spoken "in the language the user is speaking"
#: (`instructions/catalogue.py`, `voice.notice.*`) — as a failure. What cannot
#: vary is that the user is told the words did **not** go: the negation and the
#: verb. That is what is pinned, and nothing around it.
UNDELIVERED_SPOKEN_PATTERN = r"[没冇未]送"

#: The fragment of `live_call.relay_request` the engine's user-speech line has to
#: carry, chosen the way the other three were: the middle of the sentence, and
#: spaceless, so a recogniser that inserts a boundary is not what the step grades.
#: Neither the target nor the payload — both of those are what the *Call Agent*
#: has to get right, and this line is only asking whether the words arrived.
LIVE_CALL_RELAY_HEARD_SUBSTRING = live_call.HEARD_FRAGMENTS[live_call.RELAY]

#: The payload half of that same sentence: what the user answers the Session's
#: question with, and so what that Session's **next turn** has to carry (#198).
#: The one fragment of the utterance the step follows all the way through — the
#: air, the Call Agent's argv, and the Session's own record.
LIVE_CALL_ANSWER_SUBSTRING = live_call.ANSWER_FRAGMENT

#: The fragment of `live_call.DICTATED_REPLY` the Voice's **Detail** answer has
#: to carry (#198 §3a). The relayed answer dictates that reply, so by the time
#: Detail asks, this is a mid-sentence run of the Session's own `newest` — and
#: one both lanes reproduce, which a free-form reply was not (`DICTATED_REPLY`).
#:
#: Mid-sentence and spaceless for `LIVE_CALL_HEARD_SUBSTRING`'s reasons, and it
#: shares no run with `LIVE_CALL_ANSWER_SUBSTRING`, so an answer that read the
#: *relayed* words back rather than the Session's reply does not pass it.
LIVE_CALL_DICTATED_REPLY_SUBSTRING = live_call.DICTATED_REPLY_FRAGMENT

#: The three fragments #198's Detail and History utterances are recognised by,
#: chosen for `LIVE_CALL_HEARD_SUBSTRING`'s reasons: the middle of each sentence,
#: spaceless, and none of them the Session's name — which is the Call Agent's
#: half to get right and not this line's.
LIVE_CALL_DETAIL_HEARD_SUBSTRING = live_call.HEARD_FRAGMENTS[live_call.DETAIL]
LIVE_CALL_HISTORY_HEARD_SUBSTRING = live_call.HEARD_FRAGMENTS[live_call.HISTORY]
LIVE_CALL_EARLIER_HEARD_SUBSTRING = live_call.HEARD_FRAGMENTS[live_call.EARLIER]

HEARD_FRAGMENTS = live_call.HEARD_FRAGMENTS

#: What the Voice is told to say once the engine has graded the user's words
#: (`core/instructions/voice.py`, #193 §Voice): `已转达` when the relay was
#: delivered, `收到` when it is queued behind that Session's turn, and one clause
#: of the engine's reason otherwise. Quoted here because the run cannot know
#: which grade a real relay earned — a Session that happens to be mid-turn queues
#: it — so what is graded is that the user was told *one of the two*, matched as
#: a substring per #181. The third branch is the engine's own sentence and is not
#: pinned: a walk whose relay neither arrived nor queued has lost its premise
#: before this line reads anything.
RELAY_RECEIPT_DELIVERED = "已转达"

#: The delivered receipt permits the two product-observed synonyms, so the
#: grade is delivery rather than the Voice's wording choice (#193).
DELIVERED_SPOKEN_PATTERN = r"已[转送]达"

RELAY_RECEIPT_QUEUED = "收到"

#: The two a relay can earn, as the step looks for them. `收到` stays a word:
#: #193 §Voice dictates it for a queued relay and no run has yet spelled it any
#: other way.
RECEIPT_SPOKEN_PATTERNS = (DELIVERED_SPOKEN_PATTERN, re.escape(RELAY_RECEIPT_QUEUED))

#: How the engine says its own Silence Ceiling ended a call. A pattern rather
#: than an imported string because the line carries the configured number
#: (`core/bridge.py`, `_log.info("ended the Live Call after %g seconds …")`), and
#: what is being recognised is the sentence around it.
CEILING_END_LINE = r"ended the Live Call after .* without call activity"


def _announced_after_the_voice_fell_silent(lines: list[str]) -> bool:
    """Whether the first mid-call announcement came after the Voice's own last edge closed.

    The gap rule, read off the engine's own log: what may be spoken into is a
    call on which nobody is speaking, and the Voice's two edges
    (`core/bridge.py`) are the half of that the log carries. So of the edges
    written **before** the announcement, the last one must be the *stopped* one —
    or there must be none at all, which is a call whose Voice had not spoken
    since the dial and is a legitimate gap of its own.

    A module-level rule with no walk behind it, for `_cue_complaint`'s reason: an
    acceptance run is an expensive place to discover an ordering written the
    wrong way round (#109), and CI grades this one for free.

    `True` when there is no announcement: this answers "was it spoken into a
    gap", and the step asks separately whether it was spoken at all.
    """
    announced = next(
        (at for at, line in enumerate(lines) if re.search(MID_CALL_SPOKEN_PATTERN, line)), None
    )
    if announced is None:
        return True
    speaking = None
    for line in lines[:announced]:
        if VOICE_SPEAKING_LINE in line:
            speaking = True
        elif VOICE_QUIET_LINE in line:
            speaking = False
    return speaking is not True


def _hand_over_kinds(line: str) -> list[str]:
    """The kinds a dial line says it handed over, in order.

    The adapter writes counts and kinds and never their words — the words are on
    the wire and in the Session Brief the log already carries
    (`adapters/call/realtime/adapter.py`) — so this list is the whole of what a
    run can read about what a call came up holding.

    Parsed off the tail after the colon rather than searched for as substrings:
    `SpokenBrief` is not a substring of `SpokenRosterBrief`, but a rename that
    made one contain the other would turn a count into a wrong count silently.
    """
    tail = line.split("hand-over item(s):", 1)
    if len(tail) != 2:
        return []
    return [kind.strip() for kind in tail[1].split(",") if kind.strip() not in ("", "none")]


def _unaccounted_voice_turns(lines: list[str], receipts: tuple[str, ...]) -> int | None:
    """Assistant turns after a spoken receipt that no engine payment accounts for (#198).

    The ticket asks that the Voice say the grade the engine gave the user's words
    and **then stop**. Taken as a bare absence that grades #196's own rule as a
    failure: the relayed words drive the Focus Session's next Stop, and a Focus
    Stop mid-call is a `speak` the engine hands to the call in the first gap. So
    what is counted is the difference — every `transcript/done` the adapter wrote
    down after the receipt (`VOICE_SAID_PATTERN`, one line per turn), less every
    mid-call payment the engine logged in the same window
    (`MID_CALL_SPOKEN_PATTERN`). Zero or less is the rule holding; more is the
    Voice going on by itself, which is what the ticket forbids.

    `None` when no line carrying a receipt is there at all: that is a different
    failure and the caller has already asked about it. `receipts` are patterns
    rather than words (`DELIVERED_SPOKEN_PATTERN`), searched against the line
    with its spaces taken out for `_user_speech_lines`' reason.

    **The window this counts over starts at the relay, not at the utterance.**
    A receipt spoken *before* the relay ran is #221's symptom — the Voice says
    the delivered wording at hand-off and then again from the result — and
    counting from it would make the real receipt one of the turns it forbids.
    The caller passes the lines from the relay on.

    A module-level rule with no walk behind it, for `_cue_complaint`'s reason: an
    acceptance run is an expensive place to discover an arithmetic written the
    wrong way round, and CI grades this one for free.
    """
    at = next(
        (
            index
            for index, line in enumerate(lines)
            if re.search(VOICE_SAID_PATTERN, line)
            and any(re.search(pattern, _unspaced(line)) for pattern in receipts)
        ),
        None,
    )
    if at is None:
        return None
    after = lines[at + 1 :]
    return len(support.matching_lines(after, VOICE_SAID_PATTERN)) - len(
        support.matching_lines(after, MID_CALL_SPOKEN_PATTERN)
    )


def _spoken_fragment(said: str, *, longest: int = 12) -> str:
    """A fragment of a recorded message short enough for the Voice to have said it.

    #181 grades every read by substring, and the substring has to be one the
    *speaking* side can plausibly reproduce: a Session's newest message is a
    whole answer, and the Voice paraphrases it into a sentence rather than
    reciting it. So what is looked for in the transcript is the opening of the
    message, whitespace-folded, and short.

    Twelve characters because both lanes' driven turns answer with a dictated
    line (`ACKNOWLEDGE`, `ASK_A_QUESTION`) whose opening words are the whole of
    what there is to quote — `ACKNOWLEDGE`'s `READY` is five — and because a
    fragment long enough to span a clause boundary is one the Voice will have
    reworded. Empty in, empty out: the caller checks.
    """
    return " ".join(said.split())[:longest]


def _hand_over_of_more_than_one(line: str) -> bool:
    """Whether that dial line names a hand-over of more than one item.

    A call the user opened carries exactly one — why it exists, and nothing else
    (#167 Q6) — so "more than one" is what separates a system dial from it, read
    off the engine's own count rather than off the kinds it also names.
    """
    found = re.search(r"holding (\d+) hand-over item", line)
    return found is not None and int(found.group(1)) > 1


#: What `bridgectl status` calls a call that is not up. Everything the Call
#: Keeper is *waiting out* is rendered after it, in brackets, on the same line
#: (`control_plane/commands.py`, `_cool_down`).
CALL_DOWN_LINE = "call: none"


def _no_call_is_up(call_line: str) -> bool:
    """Read the call-state head while allowing a trailing Cool-down note (#195)."""
    head, _, _note = call_line.partition(" (")
    return head == CALL_DOWN_LINE


@dataclass(frozen=True, slots=True)
class _SpokenAsk:
    """The two pre-ask marks and the engine line where the words landed (#223)."""

    engine_mark: int
    wrapper_mark: int
    landed_at: int
    heard: bool


@dataclass(frozen=True, slots=True)
class _HandOverAnswer:
    """One hand-over question's landing and whether the Voice answered it (#198)."""

    ask: _SpokenAsk
    answered: bool


@dataclass(frozen=True, slots=True)
class _ReadAnswer:
    """One spoken read's fixed detail and the marks that bound its evidence (#223)."""

    detail: str
    ask: _SpokenAsk


@dataclass(frozen=True)
class _VoiceWatch:
    """Voice edges and activity after one engine-log mark, on the walk's clock (#184)."""

    #: Whether the call went down before the step gave up waiting for it.
    went_down: bool
    #: The edges this call produced, as of the last poll.
    edges: dict[bool, int]
    #: When the Voice was first, and last, seen to have said anything.
    first_voice_at: float | None
    last_voice_at: float | None
    #: When the call was seen to be down, or when the watch gave up.
    down_at: float


def _unspaced(text: str) -> str:
    """One line with all whitespace removed — see `LIVE_CALL_HEARD_SUBSTRING`."""
    return "".join(text.split())


#: How the harness's Call adapter words a connection that went away by itself
#: (`live_call.HarnessCallTransport.on_lost`). The engine raises `CallDropped`
#: for that, not `CallEnded`.
DROPPED_REASON = "went away by itself"


def _cue_line_indices(lines: Sequence[str], cue: Cue) -> list[int]:
    """Where in one reading of the log each `cue` says it was played.

    Offsets rather than the lines, because what is graded here is an *order*.
    The engine writes no line at all for `CallStarted` or `CallEnded` — the hub
    notes them into the interlock and moves on — so a cue's own line is both the
    evidence that it played and the only thing the rest of the call's log can be
    ordered against (#186). One reading is passed in rather than taken twice:
    the log grows while a step reads it, and two readings are two numberings.
    """
    phrase = cues.cue_phrase(cue)
    return [index for index, line in enumerate(lines) if phrase in line]


def _cue_record(cue: Cue, device: int | None) -> str:
    """What a played cue's line must actually carry, built the adapter's own way.

    The ticket asks the adapter's log to record "the output device and the span
    written for each", and matching only `played the … cue` would pass a line
    carrying neither. Composed with the product's own function rather than a
    pattern typed here, so the two cannot drift: the frames are what the cue
    really synthesises to, and the device is the one this lane configured.
    """
    return cues.played_line(
        cues.CueSpan(cue=cue, device=device, started=0.0, frames=cues.frames_in(cues.render(cue)))
    )


def _cue_complaint(lines: Sequence[str], spoken: Container[str], *, device: int | None) -> str:
    """Why this call's log is not the two cues in the right places, or "" if it is.

    Two claims, each ordered against the only other line this call's own half of
    the log carries — the user speech line. CONNECTED must be written *before*
    it: the cue marks the call coming up, and the utterance goes on the track ten
    seconds after the peer connection settles, so a CONNECTED that arrived after
    it would be a cue played for the wrong moment. ENDED must be written *after*
    it, because it marks the other end of the same call.

    Absence is the likelier failure and is why presence is graded at all: a
    machine with no usable output device logs a refusal instead — the adapter
    swallows its own playback failures by design, because a missing tone may not
    take down the call it was only commenting on — and this step would otherwise
    go green over a silent product.

    Pure, and module-level, so CI runs it: the acceptance walk itself never
    reaches CI, and a rule with no test at CI speed is what #109 cost.
    """
    wanted = (Cue.CONNECTED, Cue.ENDED)
    named = {cue: _cue_line_indices(lines, cue) for cue in wanted}
    # **The same line has to carry both**, which is why the order below is read
    # off `at` and never off `named`. A thin line before the speech and a whole
    # one after it satisfies "a whole line exists" and "something was logged
    # early" separately, while describing a call whose connect was never
    # actually recorded.
    at = {
        cue: [index for index in named[cue] if _cue_record(cue, device) in lines[index]]
        for cue in wanted
    }
    spoken_at = [index for index, line in enumerate(lines) if line in spoken]

    missing = [f"no {cue} cue" for cue in wanted if not named[cue]]
    if missing:
        return (
            f"the call came up and went down and the engine logged {', '.join(missing)}. "
            f"The Call adapter writes one line per cue it played, naming the output device "
            f"and the span written, and a device it could not open is logged as a refusal "
            f"instead. Engine log tail: {list(lines[-8:])}"
        )
    thin = [f"{cue}: {[lines[index] for index in named[cue]]}" for cue in wanted if not at[cue]]
    if thin:
        return (
            f"a cue was logged without the output device and the span written, which is what "
            f"this step reads it for. Expected a line carrying "
            f"{[_cue_record(cue, device) for cue in wanted]}; got {thin}"
        )
    if not spoken_at:
        return "no user speech line to order the cues against"
    if at[Cue.CONNECTED][0] > spoken_at[0]:
        return (
            f"the connected cue was logged at line {at[Cue.CONNECTED][0]}, after the user speech "
            f"at {spoken_at[0]} — a cue for the call coming up that arrived after the call had "
            f"already been talked into"
        )
    if at[Cue.ENDED][-1] < spoken_at[0]:
        return (
            f"the ended cue was logged at line {at[Cue.ENDED][-1]}, before the user speech at "
            f"{spoken_at[0]} — that is not this call's ending"
        )
    return ""


def _ended_by(*, end_reason: str | None, by_ceiling: bool, by_agent: bool) -> str:
    """Attribute a call end to the adapter, ceiling, agent, or an unknown source (#193)."""
    if end_reason and DROPPED_REASON in end_reason:
        return "lost"
    if by_ceiling:
        return "ceiling"
    return "agent" if by_agent else "harness"


#: How long `bridgectl live` gets to bring a call up. The adapter's own connect
#: timeout is 45s (`realtime/settings.py` `DEFAULT_CONNECT_TIMEOUT_SECONDS`) and
#: a `thread/start` precedes it, so this is that with room for the round trip —
#: shorter would time the surface out on a handshake that is merely finishing.
LIVE_CALL_OPEN_SECONDS = 120.0

#: Three times the measured settle, playout, and ASR landing budget (#181).
LIVE_CALL_HEARD_SECONDS = 90.0

#: How long the hand-off gets after that. The Call Agent has to decide, then run
#: a shell command; the probe saw the assistant answering 3.6s after the user
#: transcript and the verb run within the same reply window.
LIVE_CALL_HANDOFF_SECONDS = 120.0

#: And how long the call then gets to actually go down.
LIVE_CALL_END_SECONDS = 60.0

#: How long the ENDED cue's own log line gets to appear after the call is down.
#: The cue is played off the dispatch loop, on the adapter's own thread, and the
#: line is written when the device write has drained — 320-620 ms of wall time
#: for 60-300 ms of sound on this path (#174). Ten seconds is that with room for
#: a machine under load, and it is a wait rather than a single read because a
#: read taken the instant `bridgectl status` says `call: none` would be racing
#: a thread that had not been scheduled yet.
LIVE_CALL_CUE_SECONDS = 10.0

#: The measured 220-second two-hundred-number answer with bounded headroom (#184).
LIVE_CALL_ANSWER_SECONDS = 300.0

#: How often those three are checked. Each poll is a `bridgectl status` or a log
#: read, so it is cheap; frequent enough that the measured duration means
#: something and rare enough not to be a load on the engine mid-call.
LIVE_CALL_POLL_SECONDS = 2.0

#: How long a hand-off gets to appear before the walk's hand-over phase grades that none
#: did. Measured rather than guessed: the Call Agent ran `bridgectl live` four to
#: five seconds after each of three hand-offs (#179), and the wait runs *after*
#: the Voice's answer has finished — so this is that span with room over, spent
#: only on the run where the step is about to claim an absence.
LIVE_CALL_NO_VERB_SECONDS = 20.0


@dataclass(frozen=True, slots=True)
class _ExtraSession:
    session: hand_started.HandStartedSession
    workspace: Path
    first_address: str

    def address(self, run: _LiveCallRun) -> str:
        """Re-read the address because discovery may re-key the roster row."""
        return run._extra_address(self.workspace, self.first_address)


@dataclass(frozen=True, slots=True)
class _LiveCallState:
    focus: _ExtraSession
    ringing: _ExtraSession
    waiting: _ExtraSession
    turn_seconds: float
    ceiling_seconds: float
    cool_down_seconds: float
    speech_settle_seconds: float
    opening_mark: int


class _LiveCallRun:
    """The moved Live Call walk, operating on one acceptance `Walk`."""

    def __init__(self, walk: journey_module.Walk, selection: PhaseSelection | None = None) -> None:
        self.walk = walk
        self.selection = selection if selection is not None else select_phases()
        self._voice_track_mark: int | None = None

    def __getattr__(self, name: str) -> object:
        return getattr(self.walk, name)

    def live_call(self) -> str:
        """Walk the selected phases on three shared Sessions (#223).

        Every phase grades only its own trip through the air. Ground phases run
        deterministically and are recorded as arrangement; an arrangement that
        cannot establish its state blocks the lane.
        """
        if self.config.call_observations is None or self.config.call_wav_directory is None:
            raise LaneBlocked(
                "this run's engine was not given the harness Call adapter, so there is no "
                "call to hold without a microphone (`support.derive_config(spoken_call=True)`)"
            )
        names = (
            self.config.call_focus_workspace,
            self.config.call_ringing_workspace,
            self.config.call_waiting_workspace,
        )
        if any(name is None for name in names):
            raise LaneBlocked(
                "this run's config names no workspaces for the three extra Sessions, so the "
                "utterances and the step cannot agree on which Session is which (#196, #198)"
            )
        focus_name, ringing_name, waiting_name = names
        assert focus_name is not None and ringing_name is not None and waiting_name is not None

        ceiling = self._silence_ceiling_seconds()
        cool_down = self._cool_down_seconds()
        settle = self._speech_settle_seconds()
        turn = self.far_side.agent_turn_seconds
        if cool_down + settle + LIVE_CALL_CUE_SECONDS >= ceiling:
            raise LaneBlocked(
                f"this lane's Silence Ceiling is {ceiling:.0f}s and one mid-call interval is "
                f"{cool_down:.0f}s: the wait between the ring and the announcement would end "
                "the call before the announcement could be graded"
            )

        self._arm_auto_hangup()
        started = time.monotonic()
        outcomes: list[_PhaseOutcome] = []
        with (
            self._an_extra_session(focus_name) as (focus, focus_at),
            self._an_extra_session(ringing_name) as (ringing, ringing_at),
            self._an_extra_session(waiting_name) as (waiting, waiting_at),
        ):
            focus_address = self._await_extra_session(focus_at, turn)
            ringing_address = self._await_extra_session(ringing_at, turn)
            waiting_address = self._await_extra_session(waiting_at, turn)
            if not self._leave_no_call_up(LIVE_CALL_CUE_SECONDS):
                raise LaneBlocked(
                    f"a Live Call was still up when the walk began: {self._call_line()!r}"
                )

            mark = len(self.engine.log_lines())
            if "dial" in self.selection.phases:
                if "history" in self.selection.phases:
                    self._fill_a_history_worth_paging(focus, focus_at, focus_address, turn)
                self._drive_extra_session(focus, focus_at, turn, ASK_A_QUESTION_THEN_SAY)
                self._await_the_question(focus_at, focus_address, turn)
                live_call.ask_for_nothing(self.config.call_wav_directory)
                mark = len(self.engine.log_lines())

            self._voice_track_mark = mark
            state = _LiveCallState(
                focus=_ExtraSession(focus, focus_at, focus_address),
                ringing=_ExtraSession(ringing, ringing_at, ringing_address),
                waiting=_ExtraSession(waiting, waiting_at, waiting_address),
                turn_seconds=turn,
                ceiling_seconds=ceiling,
                cool_down_seconds=cool_down,
                speech_settle_seconds=settle,
                opening_mark=mark,
            )
            in_call = tuple(phase for phase in self.selection.phases if phase != "undelivered")
            if in_call:
                with self._voice_route_only():
                    for phase in in_call:
                        outcome = self._run_phase(phase, state)
                        outcomes.append(outcome)
                        if outcome.result.failed:
                            self._finish_outcomes(outcomes)

            left_up = not self._leave_no_call_up(LIVE_CALL_CUE_SECONDS)
            if "undelivered" in self.selection.phases:
                outcome = self._run_phase("undelivered", state)
                outcomes.append(outcome)
                if outcome.result.failed:
                    self._finish_outcomes(outcomes)
                left_up = not self._leave_no_call_up(LIVE_CALL_CUE_SECONDS)

        self._measured("live call", started, self._call_is_down())
        seen = live_call.observed(self.config.call_observations)
        missing = [
            name
            for name, value in (
                ("transport factory", seen.transport_factory),
                ("wav variant", seen.variant),
                ("end reason", seen.end_reason),
            )
            if not value
        ]
        transport = {
            "transport factory": seen.transport_factory or "none recorded",
            "last wav variant": seen.variant or "none recorded",
            "last end reason": seen.end_reason or "none recorded",
            "transport observations": str(self.config.call_observations),
            "wrapper runs": len(support.cli_wrapper_runs(self.config.cli_wrapper_log)),
            "wrapper verbs": self._verbs_run(),
        }
        final = outcomes[-1]
        failure = ""
        failed = list(final.result.failed)
        graded = dict(final.result.graded)
        if left_up:
            failure = (
                f"the selected phases could not put their call back down: {self._call_line()!r}"
            )
        elif missing:
            failure = (
                f"the selected phases record no {', no '.join(missing)} in "
                f"{self.config.call_observations}; held: {seen.entries[-3:] or 'nothing'}"
            )
        if failure:
            graded["transport complete and call down"] = False
            failed.append("transport complete and call down")
            transport["failure"] = failure
        outcomes[-1] = replace(
            final,
            result=replace(
                final.result,
                graded=graded,
                recorded={**final.result.recorded, **transport},
                failed=tuple(failed),
            ),
        )
        evidence = self._finish_outcomes(outcomes)
        return (
            f"graded phases {self.selection.graded}; arranged phases "
            f"{self.selection.arranged}; calls held: "
            f"{', '.join(self._calls_held()) or 'none'}; " + " Then ".join(evidence)
        )

    def _calls_held(self) -> tuple[str, ...]:
        """Name each call the selected phases held, in the order the walk holds them.

        **One flow, three calls.** User Story 4's "one call" is one *flow*, not
        one dial, and never was: `hang-up` ends the first call by voice and the
        Cool-down it starts pays a second, silent dial that the Silence Ceiling
        closes, and #197's phase holds a third of its own (#223 §2). The count is
        only legible in the verdict if the calls are named there, so they are —
        the observations stay one per phase.
        """
        return tuple(
            name
            for phase, name in (
                ("dial", "the engine's first call"),
                ("hang-up", "the Cool-down's paid call"),
                ("undelivered", "undelivered's own call"),
            )
            if phase in self.selection.phases
        )

    def _finish_outcomes(self, outcomes: Sequence[_PhaseOutcome]) -> list[str]:
        return [
            outcome.result.finish(
                self.journey,
                disposition=outcome.disposition,
                blocked=outcome.blocked,
            )
            for outcome in outcomes
        ]

    def _run_phase(self, phase: str, state: _LiveCallState) -> _PhaseOutcome:
        disposition = "graded" if self.selection.is_graded(phase) else "arranged"
        facts = _PhaseFacts(disposition, phase=phase)
        handler_name = (
            f"_phase_{phase.replace('-', '_').replace(' ', '_')}"
            if disposition == "graded"
            else f"_arrange_{phase.replace('-', '_').replace(' ', '_')}"
        )
        handler = getattr(self, handler_name)
        blocked = False
        try:
            detail = handler(state, facts)
        except _PhaseStopped:
            detail = facts.failure
        except LaneBlocked as failure:
            detail = str(failure)
            facts.record("blocked", detail)
            facts.failed.append("phase ground available")
            blocked = True
        failed = tuple(facts.failed)
        if facts.failure:
            facts.record("failure", facts.failure)
        statement = handler.__doc__.strip().splitlines()[0]
        rule, source = statement[:-2].rsplit(" (", 1)
        result = _PhaseResult(
            phase=phase,
            rule=rule,
            source=source,
            graded=facts.graded,
            recorded={**facts.recorded, "evidence": detail},
            engine_held=self._engine_held(),
            failed=failed,
        )
        return _PhaseOutcome(result=result, disposition=disposition, blocked=blocked)

    def _engine_held(self) -> str:
        return (
            f"call={self._call_line()!r}, "
            f"log tail={[line.strip() for line in self.engine.log_lines()[-4:]]!r}"
        )

    def _ask_by_voice(self, variant: str, facts: _PhaseFacts) -> _SpokenAsk:
        """Queue one WAV into a quiet track and return both marks plus its landing (#223)."""
        assert self._voice_track_mark is not None
        self._wait_for_a_quiet_track(variant, since=self._voice_track_mark, facts=facts)
        engine_mark = len(self.engine.log_lines())
        wrapper_mark = len(support.cli_wrapper_runs(self.config.cli_wrapper_log))
        heard_fragment = HEARD_FRAGMENTS[variant]
        self._voice_track_mark = engine_mark
        live_call.ask_next(self.config.call_wav_directory, variant)
        heard = self._while_the_call_is_up(
            lambda: bool(self._user_speech_lines(heard_fragment, since=engine_mark)),
            deadline_seconds=LIVE_CALL_HEARD_SECONDS + live_call.PLAYLIST_POLL_SECONDS,
        )
        return _SpokenAsk(
            engine_mark=engine_mark,
            wrapper_mark=wrapper_mark,
            landed_at=self._user_speech_landed_at(heard_fragment, since=engine_mark),
            heard=heard,
        )

    def _wait_for_a_quiet_track(self, variant: str, *, since: int, facts: _PhaseFacts) -> None:
        """Hold the next utterance until prior Voice spans and their gap settle (#223)."""
        started = time.monotonic()
        settle = self._speech_settle_seconds()
        quiet_since: float | None = None

        def track_settled() -> bool:
            nonlocal quiet_since
            if not self._voice_was_active(since=since):
                quiet_since = None
                return False
            if self._a_voice_span_is_open(since=since):
                quiet_since = None
                return False
            now = time.monotonic()
            if quiet_since is None:
                quiet_since = now
            return now - quiet_since >= settle

        settled = self._while_the_call_is_up(
            track_settled,
            deadline_seconds=LIVE_CALL_ANSWER_SECONDS + settle,
        )
        waited = time.monotonic() - started
        waits = facts.recorded.get("voice track waits")
        if not isinstance(waits, list):
            waits = []
            facts.record("voice track waits", waits)
        waits.append(f"{variant}: settled={settled}, waited={waited:.1f}s")
        if not settled:
            said = self._voice_said_lines(since=since)
            raise LaneBlocked(
                f"live call phase {facts.phase!r} could not queue variant {variant!r}: the Voice "
                f"track had not settled after it waited {waited:.1f}s; the Voice's last line "
                f"was {said[-1] if said else 'nothing since the previous ask'}"
            )

    def _voice_was_active(self, *, since: int) -> bool:
        """Whether Voice produced an edge or transcript after one stimulus (#223)."""
        return any(
            VOICE_SPEAKING_LINE in line or VOICE_QUIET_LINE in line
            for line in self._log_since(since)
        ) or bool(self._voice_said_lines(since=since))

    def _a_voice_span_is_open(self, *, since: int) -> bool:
        """Whether a Voice span after one ask's mark remains unclosed (#223)."""
        speaking = False
        for line in self._log_since(since):
            if VOICE_SPEAKING_LINE in line:
                speaking = True
            elif VOICE_QUIET_LINE in line:
                speaking = False
        return speaking

    def _phase_dial(self, state: _LiveCallState, facts: _PhaseFacts) -> str:
        """The engine dials on the Session's stopped question (#195, #198)."""
        return self._the_engine_dials_about_a_session_that_stopped(
            state.opening_mark, state.cool_down_seconds, facts
        )

    def _arrange_dial(self, state: _LiveCallState, facts: _PhaseFacts) -> str:
        """The engine's own dial establishes the downstream call (#223 story 2)."""
        return self._the_engine_dials_about_a_session_that_stopped(
            state.opening_mark, state.cool_down_seconds, facts
        )

    def _phase_hand_over(self, state: _LiveCallState, facts: _PhaseFacts) -> str:
        """The Voice answers from its dial-time hand-over (#194, #198 ruling 4)."""
        return self._the_voice_answers_out_of_the_hand_over(
            self.config.call_focus_workspace, state.focus.address(self), facts
        )

    def _phase_relay(self, state: _LiveCallState, facts: _PhaseFacts) -> str:
        """The spoken answer is relayed and receipted (#193, #198 ruling 7)."""
        return self._the_answer_is_relayed_and_receipted(
            self.config.call_focus_workspace,
            state.focus.workspace,
            state.focus.address(self),
            state.turn_seconds,
            facts,
        )

    def _arrange_relay(self, state: _LiveCallState, facts: _PhaseFacts) -> str:
        """A direct Relay settles both History and Brief ground (#223 story 15)."""
        address = state.focus.address(self)
        assert self._voice_track_mark is not None
        stimulus_mark = len(self.engine.log_lines())
        relayed = self.bridgectl(
            "relay",
            address,
            LIVE_CALL_ANSWER_SUBSTRING,
            timeout=support.RELAY_DEADLINE_SECONDS,
        )
        readings: dict[str, str] = {"history": "", "brief": ""}
        read_at: dict[str, float] = {}

        def both_surfaces_settled() -> bool:
            current = state.focus.address(self)
            history = self.bridgectl("history", current)
            readings["history"] = history.text
            read_at["history"] = time.monotonic()
            self.journal(
                "live.call.ground.reply.read",
                lane=self.lane.name,
                surface="history",
                address=current,
                read_at=read_at["history"],
                answer=history.text,
            )
            brief = self.bridgectl("brief", current)
            readings["brief"] = brief.text
            read_at["brief"] = time.monotonic()
            self.journal(
                "live.call.ground.reply.read",
                lane=self.lane.name,
                surface="brief",
                address=current,
                read_at=read_at["brief"],
                answer=brief.text,
            )
            history_ready = history.ok and LIVE_CALL_DICTATED_REPLY_SUBSTRING in _unspaced(
                history.text
            )
            brief_ready = brief.ok and LIVE_CALL_DICTATED_REPLY_SUBSTRING in _unspaced(
                _newest_message(brief.text)
            )
            return history_ready and brief_ready

        settled = relayed.ok and support.wait_for(
            both_surfaces_settled,
            deadline_seconds=state.turn_seconds + support.RELAY_DEADLINE_SECONDS,
            poll_seconds=LIVE_CALL_POLL_SECONDS,
        )
        facts.record("direct relay", relayed.text)
        facts.record("history read", readings["history"][:240])
        facts.record("history read at", read_at.get("history"))
        facts.record("brief read", readings["brief"][:240])
        facts.record("brief read at", read_at.get("brief"))
        facts.check(
            "both reply surfaces settled",
            settled,
            (
                f"direct Relay answered {relayed.text!r}; History read "
                f"{readings['history'][:240]!r} at {read_at.get('history')}; Brief read "
                f"{readings['brief'][:240]!r} at {read_at.get('brief')}"
            ),
        )
        decision = support.wait_for(
            lambda: self._mid_call_speech_decision(since=stimulus_mark),
            deadline_seconds=(
                state.turn_seconds
                + state.cool_down_seconds
                + state.speech_settle_seconds
                + LIVE_CALL_CUE_SECONDS
            ),
            poll_seconds=LIVE_CALL_POLL_SECONDS,
        )
        if decision is None:
            raise LaneBlocked(
                f"arranged relay to {address} settled History and Brief, but no mid-call "
                "speech decision followed it"
            )
        outcome, line = decision
        facts.record("mid-call decision", line)
        if outcome == "spoken":
            self._voice_track_mark = stimulus_mark
        return (
            f"direct Relay to {address} answered {relayed.text!r}; History and Brief newest "
            f"both carried {LIVE_CALL_DICTATED_REPLY_SUBSTRING!r} at {read_at}"
        )

    def _mid_call_speech_decision(self, *, since: int) -> tuple[str, str] | None:
        """Read whether Keeper spoke or declined one mid-call brief (#223)."""
        for line in self._log_since(since):
            if re.search(MID_CALL_SPOKEN_PATTERN, line):
                return "spoken", line.strip()
            if re.search(MID_CALL_NOTHING_PATTERN, line):
                return "nothing", line.strip()
        return None

    def _phase_detail(self, state: _LiveCallState, facts: _PhaseFacts) -> str:
        """Detail speaks the Session Brief's dictated newest (#198 ruling 5)."""
        return self._detail_asked_for_by_voice(
            state.focus.workspace, state.focus.address(self), facts
        )

    def _phase_history(self, state: _LiveCallState, facts: _PhaseFacts) -> str:
        """History speaks and pages the Session's older entries (#171, #223 §6)."""
        return self._history_asked_for_by_voice(
            state.focus.workspace, state.focus.address(self), facts
        )

    def _phase_long_answer(self, state: _LiveCallState, facts: _PhaseFacts) -> str:
        """Voice activity holds the call beyond the Silence Ceiling (#184)."""
        return self._the_voice_holds_the_call_open(state.ceiling_seconds, facts)

    def _phase_mid_call_news(self, state: _LiveCallState, facts: _PhaseFacts) -> str:
        """Focus news speaks while another Session only rings (#196)."""
        return self._mid_call_the_focus_session_speaks_and_the_rest_rings(
            mark=state.opening_mark,
            focus=state.focus.session,
            focus_at=state.focus.workspace,
            focus_address=state.focus.address(self),
            ringing=state.ringing.session,
            ringing_at=state.ringing.workspace,
            ringing_address=state.ringing.address(self),
            ringing_name=self.config.call_ringing_workspace,
            turn=state.turn_seconds,
            cool_down=state.cool_down_seconds,
            settle=state.speech_settle_seconds,
            facts=facts,
        )

    def _phase_hang_up(self, state: _LiveCallState, facts: _PhaseFacts) -> str:
        """Voice hang-up starts a Cool-down paid into a ceiling-ended call (#195)."""
        return self._hung_up_by_voice_then_a_cool_down_and_a_ceiling(
            mark=state.opening_mark,
            waiting=state.waiting.session,
            waiting_at=state.waiting.workspace,
            waiting_address=state.waiting.address(self),
            turn=state.turn_seconds,
            cool_down=state.cool_down_seconds,
            ceiling=state.ceiling_seconds,
            facts=facts,
        )

    def _phase_undelivered(self, state: _LiveCallState, facts: _PhaseFacts) -> str:
        """A retained Relay is spoken after its delivery ceiling (#197)."""
        return self._mid_call_a_relay_that_finally_failed(state, facts)

    def _fill_a_history_worth_paging(
        self,
        extra: hand_started.HandStartedSession,
        workspace: Path,
        address: str,
        turn: float,
    ) -> int:
        """Drive turns at an extra Session until its History has a page behind it.

        `_drive_until_history_pages`' loop, for a Session the harness started
        rather than for the walk's own, and **without #171's floor**: what phase
        3a needs is not "more than the page size, driven by this step" — that is
        #171's red line and `brief` still walks it — but simply an older page for
        `再往前` to have been answered out of.

        Turns are words-only for `ACKNOWLEDGE`'s reason, and they run with Voice
        off: every one of them ends in a Stop, and a Stop on a call that was up
        would be mid-call news a later phase is graded on.
        """

        def take_a_turn() -> None:
            self._drive_extra_session(extra, workspace, turn)
            support.wait_for(
                lambda: str((self._row_in(workspace) or {}).get("state")) != "running",
                deadline_seconds=turn,
                poll_seconds=LIVE_CALL_POLL_SECONDS,
            )

        return self._fill_until_a_page_is_full(
            page=lambda: self._history_read_yet(self._extra_address(workspace, address)),
            drive=take_a_turn,
            complaint=(
                f"turns at {address} did not produce a second History page, so there is "
                f"nothing for `再往前` to page back to"
            ),
        )

    def _await_the_question(self, workspace: Path, address: str, turn_seconds: float) -> str:
        """Wait until the engine reads that Session as having stopped on the question.

        The precondition every later phase rests on, made true rather than
        assumed: the dial has to be about a Session that has *finished* asking,
        because phase 2 grades what the Voice was handed about it and phase 3
        relays an answer to it.

        **Read as the Session's newest message, not as its roster state.** A row
        leaves `running` the moment the turn ends, but what the hand-over carries
        is `newest` (`seams/call.py`), and those are two readings of one turn that
        land at different times. What this waits for is the one the walk grades.
        """
        wanted = QUESTION_ASKED_SPOKEN_SUBSTRING
        landed = support.wait_for(
            lambda: (
                _unspaced(wanted)
                in _unspaced(
                    _newest_message(self._brief_of(self._extra_address(workspace, address)))
                )
            ),
            deadline_seconds=turn_seconds,
            poll_seconds=LIVE_CALL_POLL_SECONDS,
        )
        newest = _newest_message(self._brief_of(self._extra_address(workspace, address)))
        if not landed:
            raise LaneBlocked(
                f"{address} was asked to stop on a question and {turn_seconds:.0f}s later the "
                f"engine still reads its newest message as {newest[:120]!r}, not "
                f"{wanted!r}. Nothing this walk dials about would be the Session it grades"
            )
        self.journal(
            "extra.session.stopped.on.a.question",
            lane=self.lane.name,
            workspace=str(workspace),
            newest=newest[:200],
        )
        return newest

    def _the_engine_dials_about_a_session_that_stopped(
        self, mark: int, cool_down: float, facts: _PhaseFacts
    ) -> str:
        """Grade the automatic dial, its hand-over kinds, and CONNECTED cue (#194, #195)."""
        opened = support.wait_for(
            lambda: bool(support.matching_lines(self._log_since(mark), HAND_OVER_LINE)),
            deadline_seconds=LIVE_CALL_OPEN_SECONDS + cool_down,
            poll_seconds=LIVE_CALL_POLL_SECONDS,
        )
        up = support.wait_for(
            lambda: not self._call_is_down(),
            deadline_seconds=LIVE_CALL_CUE_SECONDS,
            poll_seconds=LIVE_CALL_POLL_SECONDS,
        )
        connected = support.wait_for(
            lambda: bool(self._cue_lines(Cue.CONNECTED, since=mark)),
            deadline_seconds=LIVE_CALL_CUE_SECONDS,
            poll_seconds=LIVE_CALL_POLL_SECONDS,
        )
        hand_over = support.matching_lines(self._log_since(mark), HAND_OVER_LINE)
        facts.record("dial lines", [line.strip() for line in hand_over])
        facts.record("cues", self._cue_order(since=mark))
        facts.check(
            "call dialled and up",
            opened and up,
            (
                f"a Session stopped on a question with Voice on and Message off and the engine "
                f"never dialled within {LIVE_CALL_OPEN_SECONDS + cool_down:.0f}s — a call is "
                f"the only outlet that state leaves. Dialled: {opened}, up: "
                f"{self._call_line()!r}. Engine log tail: {self._log_since(mark)[-8:]}"
            ),
        )
        facts.check(
            "more than one hand-over item",
            _hand_over_of_more_than_one(hand_over[0]),
            (
                f"the engine dialled, but the hand-over is {hand_over[0].strip()!r}. A call the "
                f"*system* dialled carries the reason, the Roster Brief and every Session that "
                f"needs the user (#194); one item is what a call the *user* opened gets"
            ),
        )
        carried = _hand_over_kinds(hand_over[0])
        for kind in (ROSTER_BRIEF_KIND, SESSION_BRIEF_KIND):
            facts.check(
                f"hand-over carries {kind}",
                kind in carried,
                (
                    f"the engine dialled holding {carried or 'nothing it named'} and not a "
                    f"{kind}. The ticket's hand-over is the Roster Brief *and* the brief of the "
                    f"Session that stopped, and the dial line is where the kinds are written "
                    f"down (`adapters/call/realtime/adapter.py`): {hand_over[0].strip()!r}"
                ),
            )
        facts.check(
            "CONNECTED cue played",
            connected,
            (
                f"the engine dialled and no CONNECTED cue was written within "
                f"{LIVE_CALL_CUE_SECONDS:.0f}s. The Keeper plays it when the dial comes up "
                f"(#195), and the adapter's own line is the only witness a run that cannot "
                f"hear has. Engine log tail: {self._log_since(mark)[-8:]}"
            ),
        )
        return (
            f"a Session stopped on a question with Voice on and Message off and the engine "
            f"dialled by itself, holding {hand_over[0].split('holding', 1)[-1].strip()}, with "
            f"CONNECTED played"
        )

    def _the_voice_answers_out_of_the_hand_over(
        self, focus_name: str, focus_address: str, facts: _PhaseFacts
    ) -> str:
        """Grade counted and narrowed answers from the dial-time hand-over (#194, #220)."""
        # Read before either question, so what the Voice is compared against is
        # what the engine was holding when the call came up rather than anything
        # the answers themselves moved.
        newest = _newest_message(self._brief_of(focus_address))
        if QUESTION_ASKED_SPOKEN_SUBSTRING not in _unspaced(newest):
            raise LaneBlocked(
                f"`bridgectl brief {focus_address}` holds {newest[:200]!r} as its newest "
                f"message, which is not the question this Session was told to stop on "
                f"({THE_QUESTION_ASKED!r}), so there is nothing for the narrowed answer to be "
                f"compared against"
            )
        counted = self._one_question_the_hand_over_answers(variant=live_call.NEEDS, facts=facts)
        runs_before = counted.ask.wrapper_mark
        asking = counted.ask.landed_at
        # **The absence is the narrowing question's**, and the mark is taken
        # here. See the complaint below for why the general question's runs are
        # recorded rather than graded.
        named = self._one_question_the_hand_over_answers(variant=live_call.NARROWING, facts=facts)
        narrowing = named.ask.landed_at
        runs_before_narrowing = named.ask.wrapper_mark
        # **The absence is read after both answers have closed**, and only then.
        # A hand-off arrives four to five seconds after a request (#179), and
        # sometimes after the Voice has begun answering — so the answer starting
        # is not the end of the chance for one.
        support.wait_for(
            lambda: (
                len(support.cli_wrapper_runs(self.config.cli_wrapper_log)) > runs_before_narrowing
            ),
            deadline_seconds=LIVE_CALL_NO_VERB_SECONDS,
            poll_seconds=LIVE_CALL_POLL_SECONDS,
        )
        every_run = support.cli_wrapper_runs(self.config.cli_wrapper_log)
        runs = every_run[runs_before_narrowing:]
        asked_generally = every_run[runs_before:runs_before_narrowing]
        # **The general answer is the lines between the two marks**, so the
        # narrowed one — which is supposed to carry the name — cannot be read as
        # the general one having carried it.
        answered_generally = [
            line.strip()
            for line in support.matching_lines(
                self._log_since(asking)[: narrowing - asking], VOICE_SAID_PATTERN
            )
        ]
        answered_narrowly = self._voice_said_lines(since=narrowing)
        stopped_on = QUESTION_ASKED_SPOKEN_SUBSTRING
        # What this call came up holding, so the recorded runs can be read
        # against it: a Call Agent re-reading a roster it was handed ten items of
        # is the thing #194's instruction rewording was for.
        opening = support.matching_lines(self._log_since(0), HAND_OVER_LINE)
        dialled_with = _hand_over_kinds(opening[0]) if opening else []
        facts.record("general answer", answered_generally)
        facts.record("narrowed answer", answered_narrowly)
        facts.record("general question runs", asked_generally)
        facts.record("narrowing runs", runs)
        facts.record("dial hand-over kinds", dialled_with)
        facts.record(
            "general answer named focus",
            any(focus_name in line for line in answered_generally),
        )
        facts.record(
            "narrowed answer carried stopped question",
            self._voice_said_something_carrying(stopped_on, since=narrowing),
        )
        facts.check(
            "general question heard",
            counted.ask.heard,
            (
                f"the call came up holding the briefing and the engine never logged the "
                f"question within {LIVE_CALL_HEARD_SECONDS:.0f}s. The utterance the harness "
                f"put on the track is {live_call.NEEDS_REQUEST!r} and the line looked for "
                f"carries {LIVE_CALL_NEEDS_HEARD_SUBSTRING!r}. The call now: "
                f"{self._call_line()!r}. Engine log tail: {self._log_since(asking)[-8:]}"
            ),
        )
        facts.check(
            "general question answered",
            counted.answered,
            (
                f"the question reached the engine and the Voice never answered within "
                f"{LIVE_CALL_ANSWER_SECONDS:.0f}s. A Voice with nothing to say invents rather "
                f"than going quiet (ADR 0018), so silence here is the call being gone rather "
                f"than the hand-over being empty: {self._call_line()!r}"
            ),
        )
        facts.check(
            "general answer carried a count",
            any(re.search(ROSTER_COUNT_PATTERN, line) for line in answered_generally),
            (
                f"asked what needed them, the Voice answered without a count in it: "
                f"{answered_generally}. Asked generally the Voice "
                f"gives the counts rather than the list (`core/instructions/voice.py`), and a "
                f"counted answer is what says the roster it was handed at dial time arrived"
            ),
        )
        facts.check(
            "narrowing heard",
            named.ask.heard,
            (
                f"the narrowing utterance went on the track and the engine never logged it "
                f"within {LIVE_CALL_HEARD_SECONDS:.0f}s. It carries "
                f"{live_call.narrowing_request(focus_name)!r} and the line looked for carries "
                f"{LIVE_CALL_NARROWING_HEARD_SUBSTRING!r}. The call now: {self._call_line()!r}"
            ),
        )
        facts.check(
            "narrowed answer named focus",
            self._voice_said_something_carrying(focus_name, since=narrowing),
            (
                f"the user narrowed to {focus_name!r} and the Voice never named it. When they "
                f"narrow it, the Voice speaks each Session that matches "
                f"(`core/instructions/voice.py`), and that Session's whole brief rode "
                f"`initialItems`. What it said: "
                f"{answered_narrowly or 'nothing this call recorded'}"
            ),
        )
        # **Which half answers a read question is recorded, never graded** —
        # see the docstring, and #220. What is graded is the answer's shape: a
        # `SpokenBrief` carries the newest message (`seams/call.py`), so an
        # answer naming the Session and quoting what it last said is one taken
        # from a Session Brief, whichever half fetched it.
        facts.check(
            "narrowed answer carried newest",
            self._voice_said_something_carrying(QUESTION_ASKED_SPOKEN_SUBSTRING, since=narrowing),
            (
                f"the user narrowed to {focus_name!r} and the Voice named it without saying "
                f"what it last said. A Session Brief is its project and task, its agent, where "
                f"it stands, and one sentence of its newest message "
                f"(`core/instructions/voice.py`); the engine holds "
                f"{newest[:120]!r} for it. What the Voice said: "
                f"{answered_narrowly or 'nothing this call recorded'}"
            ),
        )
        return (
            f"asked what needed them the Voice answered with counts; asked to narrow to "
            f"{focus_name!r} it named it and carried the Session's newest; wrapper runs for "
            "both questions were recorded"
        )

    def _one_question_the_hand_over_answers(
        self, *, variant: str, facts: _PhaseFacts
    ) -> _HandOverAnswer:
        """Ask once and wait for a complete Voice answer after its landing (#198, #223)."""
        ask = self._ask_by_voice(variant, facts)
        answered = self._while_the_call_is_up(
            lambda: bool(self._voice_said_lines(since=ask.landed_at)),
            deadline_seconds=LIVE_CALL_ANSWER_SECONDS,
        )
        self._while_the_call_is_up(
            self._voice_finished_speaking(ask.landed_at),
            deadline_seconds=LIVE_CALL_ANSWER_SECONDS,
        )
        return _HandOverAnswer(ask=ask, answered=answered)

    def _the_answer_is_relayed_and_receipted(
        self,
        focus_name: str,
        focus_at: Path,
        focus_address: str,
        turn: float,
        facts: _PhaseFacts,
    ) -> str:
        """Grade the spoken Relay, Session effect, receipt, and silence after it (#193, #198)."""
        ask = self._ask_by_voice(live_call.RELAY, facts)
        runs_before = ask.wrapper_mark
        relaying = ask.landed_at
        relayed = support.wait_for(
            lambda: bool(self._verbs_since(runs_before, Action.RELAY)),
            deadline_seconds=LIVE_CALL_HANDOFF_SECONDS,
            poll_seconds=LIVE_CALL_POLL_SECONDS,
        )
        relays = self._verbs_since(runs_before, Action.RELAY)
        # The no-further-work window begins after Relay, because target discovery
        # before Relay is a permitted read (#198 ruling 7).
        runs_at_relay = len(support.cli_wrapper_runs(self.config.cli_wrapper_log))
        # A receipt is read only after Relay ran; an earlier receipt-shaped turn
        # is #221's separate symptom, not this phase's product grade.
        at_relay = len(self.engine.log_lines())
        receipted = self._while_the_call_is_up(
            lambda: bool(self._voice_said_matching(RECEIPT_SPOKEN_PATTERNS, since=at_relay)),
            deadline_seconds=LIVE_CALL_ANSWER_SECONDS,
        )
        # **Delivery is read off the Session's next turn, not off the record
        # holding the words.** `ASK_A_QUESTION_THEN_SAY` has this Session answer
        # a further message with `live_call.DICTATED_REPLY`, so that line is one
        # only a Session the relay reached can say — the effect, which is how the
        # `relay` step §3 cites proves delivery too.
        #
        # The words themselves are recorded, not graded, because Claude's
        # visible History may omit the system-sourced relay (#222). The dictated
        # next reply is the cross-lane delivery effect this phase grades.
        carried = support.wait_for(
            lambda: self._history_of_a_session_carries(
                focus_at, focus_address, LIVE_CALL_DICTATED_REPLY_SUBSTRING
            ),
            deadline_seconds=turn + support.RELAY_DEADLINE_SECONDS,
            poll_seconds=LIVE_CALL_POLL_SECONDS,
        )
        relayed_words_kept = self._history_of_a_session_carries(
            focus_at, focus_address, LIVE_CALL_ANSWER_SUBSTRING
        )
        # Read after the Session's turn has landed, so the window this counts
        # over is the one the payment would have fallen in.
        since_relay = self._log_since(at_relay)
        payments = support.matching_lines(since_relay, MID_CALL_SPOKEN_PATTERN)
        unaccounted = _unaccounted_voice_turns(since_relay, RECEIPT_SPOKEN_PATTERNS)
        # #221's symptom, recorded rather than graded: a receipt-worded turn
        # between the utterance going out and the relay running. It is left out
        # of the count above by where that window starts.
        premature = self._voice_said_matching(
            RECEIPT_SPOKEN_PATTERNS, since=relaying, until=at_relay
        )
        further = self._verbs_run(since=runs_at_relay)
        facts.record("relay runs", relays)
        facts.record("relay named focus", any(focus_address in verb for verb in relays))
        facts.record("voice turns", self._voice_said_lines(since=relaying))
        facts.record("engine payments", [line.strip() for line in payments])
        facts.record("premature receipts", premature)
        facts.record("runs before relay", self._verbs_run(since=runs_before)[:-1])
        facts.record("relayed words retained", relayed_words_kept)
        facts.check(
            "answer heard",
            ask.heard,
            (
                f"the answer utterance went on the track and the engine never logged the "
                f"user's speech within {LIVE_CALL_HEARD_SECONDS:.0f}s. It carries "
                f"{live_call.relay_request(focus_name)!r} and the line looked for carries "
                f"{LIVE_CALL_RELAY_HEARD_SUBSTRING!r}. Engine log tail: {since_relay[-8:]}"
            ),
        )
        facts.check(
            "Call Agent ran relay",
            relayed,
            (
                f"the engine heard the answer and the Call Agent never ran "
                f"`{Action.RELAY}` within {LIVE_CALL_HANDOFF_SECONDS:.0f}s. What it ran "
                f"instead: {self._verbs_run(since=runs_before) or 'nothing at all'}"
            ),
        )
        facts.check(
            "focus Session carried dictated reply",
            carried,
            (
                f"the Call Agent relayed with {relays} and {focus_address} never went on to say "
                f"{live_call.DICTATED_REPLY!r} within "
                f"{turn + support.RELAY_DEADLINE_SECONDS:.0f}s. The relay is the user's answer "
                f"to the question that Session stopped on, and this Session was told to answer "
                f"a further message with that line — so it is one only a Session the words "
                f"reached can say. What `bridgectl history` holds: "
                f"{self._history_page(address=self._extra_address(focus_at, focus_address))}"
            ),
        )
        facts.check(
            "Voice gave relay receipt",
            receipted,
            (
                f"the words reached the Session and the Voice never told the user so. #193 "
                f"§Voice has it say {RELAY_RECEIPT_DELIVERED!r} for a delivered relay and "
                f"{RELAY_RECEIPT_QUEUED!r} for one queued behind that Session's turn. What it "
                f"said after the relay went out: "
                f"{self._voice_said_lines(since=at_relay) or 'nothing this call recorded'}. "
                f"Before it (#221, not this grade): {premature or 'nothing'}"
            ),
        )
        facts.check(
            "Call Agent stopped after relay",
            not further,
            (
                f"the Voice gave the user their receipt and the Call Agent went on working: "
                f"{further}. One spoken answer is one hand-off, and nothing else was asked for "
                f"before the next request went on the track"
            ),
        )
        facts.check(
            "receipt found in log",
            unaccounted is not None,
            "the receipt was heard and then could not be found in the log",
        )
        assert unaccounted is not None
        facts.check(
            "no unaccounted Voice turns",
            unaccounted <= 0,
            (
                f"the Voice said its receipt and then said {unaccounted} more thing(s) nobody "
                f"asked it for: the window holds {len(payments)} engine payment(s) "
                f"({[line.strip() for line in payments]}) and more Voice turns than that. "
                f"What it said: {self._voice_said_lines(since=relaying)}"
            ),
        )
        return (
            f"the user answered by voice, the Call Agent relayed with {relays}, "
            f"{LIVE_CALL_ANSWER_SUBSTRING!r} reached {focus_address}'s own next turn, and the "
            f"Voice gave the user the engine's grade and then stopped "
            f"({len(payments)} payment(s) accounted for)"
        )

    def _history_of_a_session_carries(self, workspace: Path, fallback: str, fragment: str) -> bool:
        """Whether a Session's own record carries a fragment, read through `history`.

        The surface a user gets, for `_history_page`'s reason, and the only
        reading available for a Session this harness started and took no ground
        truth for. The address is re-read on every call rather than carried: a
        row is re-keyed as the lane learns more about it, and this is polled
        across a turn, which is exactly when that happens.
        """
        page = self._history_read_yet(self._extra_address(workspace, fallback))
        if page is None:
            return False
        return any(_unspaced(fragment) in _unspaced(text) for _, text in page.entries)

    def _detail_asked_for_by_voice(
        self, focus_at: Path, focus_address: str, facts: _PhaseFacts
    ) -> str:
        """Detail is graded against the dictated newest in the Session Brief (#198)."""
        address = self._extra_address(focus_at, focus_address)
        latest: dict[str, str] = {"brief": "", "newest": ""}

        def dictated_reply_is_newest() -> bool:
            brief = self.bridgectl("brief", self._extra_address(focus_at, address))
            latest["brief"] = brief.text
            latest["newest"] = _newest_message(brief.text) if brief.ok else ""
            return brief.ok and LIVE_CALL_DICTATED_REPLY_SUBSTRING in _unspaced(latest["newest"])

        settled = support.wait_for(
            dictated_reply_is_newest,
            deadline_seconds=self.far_side.agent_turn_seconds + support.RELAY_DEADLINE_SECONDS,
            poll_seconds=LIVE_CALL_POLL_SECONDS,
        )
        if not settled:
            raise LaneBlocked(
                f"`bridgectl brief {address}` never carried the dictated reply as `newest`; "
                f"last newest {latest['newest'][:200]!r}, brief {latest['brief'][:300]!r}"
            )
        return self._one_read_asked_for_by_voice(
            variant=live_call.DETAIL,
            action=Action.BRIEF,
            address=address,
            wanted=(LIVE_CALL_DICTATED_REPLY_SUBSTRING,),
            about=f"the Session Brief's newest message {latest['newest'][:120]!r}",
            verb_is_graded=False,
            facts=facts,
        ).detail

    def _history_asked_for_by_voice(
        self, focus_at: Path, focus_address: str, facts: _PhaseFacts
    ) -> str:
        """History is fetched and its older page is spoken by voice (#171, #223 §6)."""
        address = self._extra_address(focus_at, focus_address)
        newest_page = self._history_page(address=address)
        if not newest_page.entries:
            raise LaneBlocked(
                f"`bridgectl history {address}` is empty mid-call, so History has no answer"
            )
        cursor = min(ordinal for ordinal, _ in newest_page.entries)
        latest = max(ordinal for ordinal, _ in newest_page.entries)
        older_page = self._history_page(before=cursor, address=address)
        earlier_entries = tuple(text for ordinal, text in newest_page.entries if ordinal < latest)
        if not earlier_entries:
            raise LaneBlocked(f"{address}'s newest History page holds only {newest_page.entries!r}")

        history = self._one_read_asked_for_by_voice(
            variant=live_call.HISTORY,
            action=Action.HISTORY,
            address=address,
            wanted=tuple(_spoken_fragment(text) for text in earlier_entries),
            about=f"an entry on {address}'s newest History page ({newest_page.entries!r})",
            facts=facts,
        )
        runs_before_history = history.ask.wrapper_mark
        asking_history = history.ask.landed_at
        history_verbs = self._verbs_since(history.ask.wrapper_mark, Action.HISTORY)
        opening_with_cursor = bool(
            history_verbs
            and address in history_verbs[0]
            and HISTORY_CURSOR_OPTION in history_verbs[0]
        )
        if not older_page.entries:
            raise LaneBlocked(
                f"{address} has no History page older than ordinal {cursor}; "
                f"newest page {newest_page.entries!r}"
            )

        earlier = self._one_read_asked_for_by_voice(
            variant=live_call.EARLIER,
            action=Action.HISTORY,
            address=address,
            wanted=tuple(_spoken_fragment(text) for _, text in older_page.entries),
            about=f"an entry on the page before ordinal {cursor} ({older_page.entries!r})",
            verb_is_graded=False,
            answer_is_graded=False,
            facts=facts,
        )
        runs_before_earlier = earlier.ask.wrapper_mark
        asking_earlier = earlier.ask.landed_at
        older_fragments = [
            fragment for _, text in older_page.entries if (fragment := _spoken_fragment(text))
        ]
        older_said = [
            fragment
            for fragment in older_fragments
            if self._voice_said_something_carrying(fragment, since=asking_history)
        ]
        under_earlier_answer = [
            fragment
            for fragment in older_said
            if self._voice_said_something_carrying(fragment, since=asking_earlier)
        ]
        paged = [
            verb
            for verb in self._verbs_since(runs_before_history, Action.HISTORY)
            if address in verb and HISTORY_CURSOR_OPTION in verb
        ]
        under_earlier = [
            verb
            for verb in self._verbs_since(runs_before_earlier, Action.HISTORY)
            if address in verb and HISTORY_CURSOR_OPTION in verb
        ]
        facts.record("opening History read carried cursor", opening_with_cursor)
        facts.record("paged History runs", paged)
        facts.record("paging runs after earlier ask", under_earlier)
        facts.record("older entries spoken", older_said)
        facts.record("older entries spoken after earlier ask", under_earlier_answer)
        facts.check(
            "older History page spoken",
            bool(older_said),
            (
                f"the Voice never read an older-page entry; page {older_page.entries!r}, "
                f"fragments {older_fragments!r}"
            ),
        )
        facts.check(
            "History paged with cursor",
            bool(paged),
            (
                f"the Call Agent never ran `{Action.HISTORY}` with "
                f"`{HISTORY_CURSOR_OPTION}` for {address}; verbs {history_verbs!r}"
            ),
        )
        return (
            f"{history.detail}; {earlier.detail}; paged with {paged}; opening cursor "
            f"recorded as {opening_with_cursor}"
        )

    def _one_read_asked_for_by_voice(
        self,
        *,
        variant: str,
        action: Action,
        address: str,
        wanted: tuple[str, ...],
        about: str,
        answer_is_graded: bool = True,
        verb_is_graded: bool = True,
        facts: _PhaseFacts,
    ) -> _ReadAnswer:
        """Grade or record one spoken read from its actual landing (#171, #198, #223)."""
        ask = self._ask_by_voice(variant, facts)
        runs_before = ask.wrapper_mark
        asking = ask.landed_at
        heard = HEARD_FRAGMENTS[variant]
        if verb_is_graded:
            self._while_the_call_is_up(
                lambda: bool(self._verbs_since(runs_before, action)),
                deadline_seconds=LIVE_CALL_HANDOFF_SECONDS,
            )

        # **When the answer is not this question's to carry, wait for the Voice
        # to say *something*.** Waiting for a fragment the previous answer
        # already read out would sit here until the Silence Ceiling took the
        # call — and the walk speaks into this call again afterwards.
        def carried_the_answer() -> bool:
            return any(
                self._voice_said_something_carrying(fragment, since=asking)
                for fragment in wanted
                if fragment
            )

        def said_anything() -> bool:
            return bool(self._voice_said_lines(since=asking))

        answered = self._while_the_call_is_up(
            carried_the_answer if answer_is_graded else said_anything,
            deadline_seconds=LIVE_CALL_ANSWER_SECONDS,
        )
        verbs = self._verbs_since(runs_before, action)
        # **The verb has to be about the Session the question named.** The Call
        # Agent picks the target off what it heard, and this machine runs nine
        # Sessions during a walk — a `history` of somebody else's record read
        # back convincingly would otherwise pass.
        about_it = [verb for verb in verbs if address in verb]
        said = self._voice_said_lines(since=asking)
        facts.record(f"{variant} runs", verbs)
        facts.record(f"{variant} runs naming Session", about_it)
        facts.record(f"{variant} Voice answer", said)
        facts.record(f"{variant} engine ground", about)
        facts.check(
            f"{variant} heard",
            ask.heard,
            (
                f"the {variant} utterance went on the track and the engine never logged the "
                f"user's speech within {LIVE_CALL_HEARD_SECONDS:.0f}s. The line looked for "
                f"carries {heard!r}. The call now: {self._call_line()!r}. Engine log tail: "
                f"{self._log_since(asking)[-8:]}"
            ),
        )
        if verb_is_graded:
            facts.check(
                f"{variant} ran {action} for Session",
                bool(about_it),
                (
                    f"the engine heard the {variant} question about {address} and the Call Agent "
                    f"never ran `{action}` for it within {LIVE_CALL_HANDOFF_SECONDS:.0f}s. No "
                    f"hand-over carries a Session's history — a `{SESSION_BRIEF_KIND}` holds its "
                    f"newest message and nothing before it (`seams/call.py`) — so this one cannot "
                    f"be answered without the verb. What it ran: "
                    f"{self._verbs_run(since=runs_before) or 'nothing at all'}"
                ),
            )
        facts.check(
            f"{variant} answered",
            answered,
            (
                f"the Call Agent ran {verbs or 'nothing'} and the Voice never said "
                + (
                    f"anything carrying {[fragment for fragment in wanted if fragment]!r} — "
                    f"{about}."
                    if answer_is_graded
                    else "anything at all."
                )
                + f" What it said: {said or 'nothing this call recorded'}"
            ),
        )
        return _ReadAnswer(
            detail=(
                f"{variant} answered out of {about_it or 'the hand-over'} and read back to the user"
            ),
            ask=ask,
        )

    def _the_voice_holds_the_call_open(self, ceiling: float, facts: _PhaseFacts) -> str:
        """Grade Voice activity holding a call beyond its Silence Ceiling (#184)."""
        ask = self._ask_by_voice(live_call.LONG, facts)
        asking = ask.landed_at
        # Voice can start answering just before the recogniser logs the user's
        # speech. The quiet-track gate closed the previous answer before this
        # pre-request mark, so it includes that first edge without borrowing an
        # earlier answer's tail (#223 story 5; run 20260904T043017Z).
        watching = time.monotonic()
        watch = self._watch_the_voice(
            ask.engine_mark,
            deadline_seconds=LIVE_CALL_ANSWER_SECONDS + ceiling,
            quiet_seconds=LIVE_CALL_CUE_SECONDS,
        )
        edges = watch.edges
        # Measure from the landing rather than a first edge: one Voice span may
        # cover the boundary between consecutive answers (#184, #223).
        answered_for = 0.0 if watch.last_voice_at is None else watch.last_voice_at - watching
        facts.record("Silence Ceiling seconds", ceiling)
        facts.record("Voice answer seconds", answered_for)
        facts.record("Voice start edges", edges[True])
        facts.record("Voice stop edges", edges[False])
        facts.record("call went down", watch.went_down)
        facts.check(
            "long answer heard",
            ask.heard,
            (
                f"the long-answer utterance went on the track and the engine never logged the "
                f"user's speech within {LIVE_CALL_HEARD_SECONDS:.0f}s. It carries "
                f"{live_call.LONG_REQUEST!r} and the line looked for carries "
                f"{LIVE_CALL_LONG_HEARD_SUBSTRING!r}. Engine log tail: "
                f"{self._log_since(asking)[-8:]}"
            ),
        )
        facts.check(
            "Voice answered",
            watch.last_voice_at is not None,
            (
                f"the words arrived and the Voice neither spoke nor closed a span it already "
                f"had open, so there is no answer for the ceiling to have counted. Edges since "
                f"the request: {edges}. Engine log tail: {self._log_since(asking)[-5:]}"
            ),
        )
        facts.check(
            "Voice spans closed",
            edges[False] >= edges[True],
            (
                f"the Voice left a span open: {edges[True]} start and {edges[False]} stop "
                f"edge(s). That is the bug itself (#169) — the ceiling firing mid-answer takes "
                f"the call away before the `transcript/done` that would have closed the span. "
                f"The Voice was answering for {answered_for:.0f}s against a {ceiling:.0f}s "
                f"ceiling; the call is {self._call_line()!r}"
            ),
        )
        facts.check(
            "Voice answer outlasted Silence Ceiling",
            answered_for > ceiling,
            (
                f"the Voice was answering for {answered_for:.0f}s, which does not outlast this "
                f"lane's own {ceiling:.0f}s Silence Ceiling — the request asks for two hundred "
                f"numbers, so an answer this short is one the Voice cut rather than one the "
                f"ceiling would ever have had to hold a call open through. Nothing about the "
                f"both-sides rule is proven by it"
            ),
        )
        facts.check(
            "call stayed up",
            not watch.went_down,
            (
                f"the call went down while the Voice was answering, {answered_for:.0f}s into a "
                f"stretch its own speech is supposed to keep alive ({self._call_line()!r}). "
                f"The ceiling counts both sides (#184), and the walk speaks into this call "
                f"again after this"
            ),
        )
        return (
            f"the Voice answered for {answered_for:.0f}s against a {ceiling:.0f}s ceiling, "
            f"across {edges[True]} start / {edges[False]} stop edge(s) with none left open, "
            f"and the call is still up"
        )

    def _cool_down_remaining(self) -> float:
        """How much Cool-down the engine says is still to run, in its own words.

        Read off `bridgectl status`, which reports it beside `call: none` (#195),
        rather than timed on this side: the release and this harness's next poll
        are separated by two lanes' worth of subprocesses, and a window measured
        here would be measuring the harness. Zero when nothing is pending, which
        is also what a surface with no such line answers.
        """
        found = re.search(r"cool-down (\d+(?:\.\d+)?)s", self._call_line())
        return float(found.group(1)) if found else 0.0

    def _cool_down_seconds(self) -> float:
        """The Cool-down this lane's engine is actually running.

        Read out of the lane's own config and only then off the shipped default,
        exactly as the Silence Ceiling is: a literal here would be a second copy
        of a policy value the engine already owns.
        """
        document = tomllib.loads(self.config.path.read_text())
        given = document.get("policy", {}).get("cool_down_seconds")
        return DEFAULT_COOL_DOWN_SECONDS if given is None else float(given)

    def _speech_settle_seconds(self) -> float:
        """The settle window this lane's engine is actually running.

        The third of the three policy durations a call step waits on, read the
        same way for the same reason: a step that waits out a *gap* is waiting
        out this number, and a literal here would be the harness's own copy of a
        dial the engine owns.
        """
        document = tomllib.loads(self.config.path.read_text())
        given = document.get("policy", {}).get("speech_settle_seconds")
        return DEFAULT_SPEECH_SETTLE_SECONDS if given is None else float(given)

    def _mid_call_a_relay_that_finally_failed(
        self, state: _LiveCallState, facts: _PhaseFacts
    ) -> str:
        """Grade a retained Relay becoming spoken undelivered news (#173, #197)."""
        ceiling = self._relay_ceiling_seconds()
        settle = state.speech_settle_seconds
        silence = state.ceiling_seconds
        cool_down = state.cool_down_seconds
        turn = state.turn_seconds
        # **The ceiling has to fit inside the Silence Ceiling.** This phase waits
        # a Relay ceiling out on a call it must still be holding afterwards, and
        # the Voice says nothing while it waits — so a Relay ceiling longer than
        # the silence one would end the call before the news it is waiting for.
        if ceiling + settle + LIVE_CALL_CUE_SECONDS >= silence:
            raise LaneBlocked(
                f"this lane's Silence Ceiling is {silence:.0f}s and its Relay ceiling "
                f"{ceiling:.0f}s: the wait this phase makes would be what ends the call, and "
                "the announcement would be graded against a call that had already gone"
            )
        started = time.monotonic()
        # The three Session lifetimes belong to `live_call`; this phase reuses
        # the shared Focus Session and never starts a fourth one (#223 story 8).
        extra = state.focus.session
        workspace = state.focus.workspace
        address = state.focus.address(self)
        if not self._leave_no_call_up(LIVE_CALL_CUE_SECONDS):
            raise LaneBlocked(
                f"a Live Call was still up after this phase asked for it to end, so there "
                f"is no call of its own to grade: {self._call_line()!r}"
            )
        # **The Focus Session has to be idle before its Stop can dial.** This
        # phase reuses the Session the phases before it drove, and both `hang-up`'s
        # Stop chain and the arranged `relay` ground leave a turn behind. A Session
        # still mid-turn would take this phase's instruction *into* that turn
        # instead of stopping on it, and the dial the announcement is graded
        # against would never come — so a Session that is not idle is missing
        # ground, not a failed rule.
        settled = support.wait_for(
            lambda: str((self._row_in(workspace) or {}).get("state")) == "idle",
            deadline_seconds=turn,
            poll_seconds=LIVE_CALL_POLL_SECONDS,
        )
        if not settled:
            raise LaneBlocked(
                f"{address} was still "
                f"{str((self._row_in(workspace) or {}).get('state'))!r} after {turn:.0f}s "
                "and never went idle, so the Stop this phase dials on is not its own to drive"
            )
        # Its Stop is what dials, so the call is up and about this Session.
        self._drive_extra_session(extra, workspace, turn)
        live_call.ask_for_nothing(self.config.call_wav_directory)
        mark = len(self.engine.log_lines())
        with self._voice_route_only():
            opened = support.wait_for(
                lambda: bool(support.matching_lines(self._log_since(mark), HAND_OVER_LINE)),
                deadline_seconds=LIVE_CALL_OPEN_SECONDS + cool_down,
                poll_seconds=LIVE_CALL_POLL_SECONDS,
            )
            up = support.wait_for(
                lambda: not self._call_is_down(),
                deadline_seconds=LIVE_CALL_CUE_SECONDS,
                poll_seconds=LIVE_CALL_POLL_SECONDS,
            )
            facts.check(
                "call dialled and up",
                opened and up,
                (
                    f"a Session stopped and Voice came on with Message off, and no call "
                    f"was up within {LIVE_CALL_OPEN_SECONDS + cool_down:.0f}s — dialled: "
                    f"{opened}, up: {self._call_line()!r}"
                ),
            )
            # Discovery can re-key a row as it learns more; resolve the
            # address again immediately before Relay (`sessions.py::_better_known`).
            address = self._extra_address(workspace, address)
            # **The Relay is what makes it the Focus Session** (#165 Q2), and
            # the instruction it carries is what shuts its Reply Window.
            shutting = self.lane.actionable(workspace)
            shut_at = shutting.path_in(workspace)
            if shut_at is not None:
                shut_at.parent.mkdir(parents=True, exist_ok=True)
            asked = self.bridgectl(
                "relay", address, shutting.words, timeout=support.RELAY_DEADLINE_SECONDS
            )
            facts.check(
                "permission Relay accepted",
                asked.ok,
                f"the relay that arms this phase was refused: {asked.text}",
            )
            waiting = support.wait_for(
                lambda: str((self._row_in(workspace) or {}).get("state")) == "waiting",
                deadline_seconds=turn,
                poll_seconds=LIVE_CALL_POLL_SECONDS,
            )
            if not waiting:
                raise LaneBlocked(
                    f"{address} never reached `waiting` on {self.lane.asks_about}, so its "
                    "Reply Window was never shut and there is no Relay to hold"
                )
            holding = len(self.engine.log_lines())
            address = self._extra_address(workspace, address)
            held = self.bridgectl(
                "relay", address, UNDELIVERED.words, timeout=support.RELAY_DEADLINE_SECONDS
            )
            receipt = _receipt_fields(held.text)
            facts.check(
                "Relay retained behind closed Reply Window",
                held.ok and receipt.get("state") == str(Lifecycle.RETAINED),
                (
                    f"the words meant to wait out the ceiling were answered "
                    f"{held.text!r}, and not as `{Lifecycle.RETAINED}` — the Session's "
                    "Reply Window was open after all"
                ),
            )
            announced = support.wait_for(
                lambda: bool(
                    support.matching_lines(self._log_since(holding), MID_CALL_UNDELIVERED_PATTERN)
                ),
                # Budget the Relay ceiling plus a complete Voice answer and
                # the two-sided settle gap (#197, `realtime/webrtc.py`).
                deadline_seconds=(
                    ceiling + LIVE_CALL_ANSWER_SECONDS + settle + LIVE_CALL_CUE_SECONDS
                ),
                poll_seconds=LIVE_CALL_POLL_SECONDS,
            )
            spoken = support.wait_for(
                lambda: bool(
                    support.matching_lines(
                        support.matching_lines(self._log_since(holding), VOICE_SAID_PATTERN),
                        UNDELIVERED_SPOKEN_PATTERN,
                    )
                ),
                deadline_seconds=LIVE_CALL_ANSWER_SECONDS,
                poll_seconds=LIVE_CALL_POLL_SECONDS,
            )
            said = [
                line.strip()
                for line in support.matching_lines(self._log_since(holding), VOICE_SAID_PATTERN)
            ]
            self._end_any_live_call()
        self._measured("live call undelivered", started, self._call_is_down())
        facts.record("Relay ceiling seconds", ceiling)
        facts.record("Focus Session address", address)
        facts.record("retained receipt", receipt)
        facts.record("Voice turns", said)
        facts.check(
            "undelivered brief announced",
            announced,
            (
                f"a Relay to the Focus Session passed its {ceiling:.0f}s ceiling on a call "
                f"that was up, and no announcement carried it. Engine log tail: "
                f"{self._log_since(holding)[-10:]}"
            ),
        )
        facts.check(
            "Voice spoke undelivered reason",
            spoken,
            (
                f"the brief carried the undelivered Relay and the Voice never said "
                f"anything matching {UNDELIVERED_SPOKEN_PATTERN!r} (#173 §6). What it said: "
                f"{said or 'nothing this call recorded'}"
            ),
        )
        return (
            f"a Relay held past {ceiling:.0f}s reached {address} as a spoken brief, and the "
            f"Voice said so in its own words"
        )

    @contextmanager
    def _an_extra_session(
        self, workspace_name: str
    ) -> Iterator[tuple[hand_started.HandStartedSession, Path]]:
        """Start one lane-matched Session in a distinct trusted workspace (#196, #208)."""
        run_directory = self.config.workspace.parent
        lane_label = f"{self.lane.name}-{workspace_name}"
        workspace = support.workspace_at(
            run_directory / lane_label / workspace_name,
            self.session.environment.get("PATH", ""),
        )
        session = hand_started.HandStartedSession(
            lane=lane_label,
            binary=self.session.binary,
            arguments=self.session.arguments,
            workspace=workspace,
            environment=self.session.environment,
            journal=self.journal,
            transcript=run_directory / f"pty-{lane_label}.log",
        )
        with support.TrustGate(
            workspace,
            run_directory=run_directory,
            journal=self.journal,
            label=lane_label,
            environment=self.session.environment,
        ):
            session.start()
            self.journal("extra.session.started", lane=self.lane.name, workspace=str(workspace))
            try:
                yield session, workspace
            finally:
                session.stop()

    def _row_in(self, workspace: Path) -> dict | None:
        """The roster row for a Session working in that directory, if the engine has one.

        The join this phase can make and `_row_for` cannot: the extra Sessions
        have no ground truth taken for them — nothing here reads their records —
        and the workspace is the one fact the harness chose and the roster
        carries.
        """
        wanted = {str(workspace), os.path.realpath(workspace)}
        for row in self._roster_rows():
            listed = row.get("workspace")
            if isinstance(listed, str) and listed in wanted:
                return row
        return None

    def _mid_call_the_focus_session_speaks_and_the_rest_rings(
        self,
        *,
        mark: int,
        focus: hand_started.HandStartedSession,
        focus_at: Path,
        focus_address: str,
        ringing: hand_started.HandStartedSession,
        ringing_at: Path,
        ringing_address: str,
        ringing_name: str,
        turn: float,
        cool_down: float,
        settle: float,
        facts: _PhaseFacts,
    ) -> str:
        """Grade Focus news speaking while a non-Focus Stop only rings (#196)."""
        before = len(self.engine.log_lines())
        # --- the ring: a Session that is not the Focus one stops --------------
        self._drive_extra_session(ringing, ringing_at, turn)
        rang = support.wait_for(
            lambda: bool(self._cue_lines(Cue.EVENT, since=before)),
            deadline_seconds=turn,
            poll_seconds=LIVE_CALL_POLL_SECONDS,
        )
        # --- the word: the Focus Session stops again --------------------------
        # On a question, so the brief it is announced with has something to say:
        # a Session the reading finds no longer needs the user cancels the word
        # silently and correctly (`call_keeper.py::nothing_to_speak`).
        owing = len(self.engine.log_lines())
        self._voice_track_mark = owing
        self._drive_extra_session(focus, focus_at, turn, ASK_A_QUESTION)
        # **What the brief will be about**, waited for rather than read once: the
        # announcement is spoken from a reading taken at the gap (ADR 0017), so
        # what the user hears has to be *this* Stop's newest message. A read
        # taken before the turn ended would be the answer the relay drove, and
        # the comparison would pass on a stale announcement.
        fresh = self._await_the_question(focus_at, focus_address, turn)
        announced = support.wait_for(
            lambda: bool(support.matching_lines(self._log_since(owing), MID_CALL_SPOKEN_PATTERN)),
            deadline_seconds=turn + cool_down + settle + LIVE_CALL_CUE_SECONDS,
            poll_seconds=LIVE_CALL_POLL_SECONDS,
        )
        since_owing = self._log_since(owing)
        announcements = support.matching_lines(since_owing, MID_CALL_SPOKEN_PATTERN)
        after_silence = _announced_after_the_voice_fell_silent(since_owing)
        named_the_ringer = [
            line
            for line in support.matching_lines(self._log_since(before), MID_CALL_SPOKEN_PATTERN)
            if ringing_name in line
        ]
        rings = len(self._cue_lines(Cue.EVENT, since=mark))
        facts.record("Focus Session", f"{focus_address} in {focus_at}")
        facts.record("ringing Session", f"{ringing_address} in {ringing_at}")
        facts.record("EVENT cues", rings)
        facts.record("announcements", [line.strip() for line in announcements])
        facts.record("announcement after Voice silence", after_silence)
        facts.check(
            "non-Focus Stop rang",
            rang,
            (
                f"{ringing_address} stopped while {focus_address} was the Focus Session and no "
                f"EVENT cue was played within {turn:.0f}s. The cues this call played: "
                f"{self._cue_order(since=mark)}; the call now: {self._call_line()!r}. Engine "
                f"log tail: {self._log_since(before)[-8:]}"
            ),
        )
        if not announced:
            silent = support.matching_lines(since_owing, MID_CALL_NOTHING_PATTERN)
            facts.check(
                "Focus Stop announced",
                False,
                (
                    f"the Focus Session {focus_address} stopped again on a call that was up and "
                    f"the engine never spoke its brief within "
                    f"{turn + cool_down + settle + LIVE_CALL_CUE_SECONDS:.0f}s. Its next Stop is "
                    f"spoken in the first gap an interval after the last mid-call sound (#196). "
                    f"Whether it decided there was nothing to say instead: "
                    f"{[line.strip() for line in silent] or 'no such line'}. Engine log tail: "
                    f"{since_owing[-10:]}"
                ),
            )
        facts.check(
            "non-Focus Stop stayed unspoken",
            not named_the_ringer,
            (
                f"a Session that is not the Focus one stopped and the engine spoke its brief "
                f"into the call. The rest of the roster is the EVENT cue and nothing more, and "
                f"the user asks with `{Action.BRIEF}` (#196). The lines naming "
                f"{ringing_name}: {[line.strip() for line in named_the_ringer]}"
            ),
        )
        facts.check(
            "one Focus announcement",
            len(announcements) == 1,
            (
                f"one Stop about the Focus Session earned {len(announcements)} announcements "
                f"on one call. A word owed is one flag and not a queue (#196): "
                f"{[line.strip() for line in announcements]}"
            ),
        )
        facts.check(
            "announcement waited for a gap",
            after_silence,
            (
                f"the brief was spoken while the Voice's own last edge was still `speaking` — "
                f"there was no gap to speak into, and the wire has no silent mid-call path "
                f"(#175, #196). The announcement: {announcements[0].strip()!r}"
            ),
        )
        # **And the user heard the brief, not just that there was one.** The
        # engine's `speak` line says a brief was handed to the call and names the
        # Session; what the Voice made of it is the transcript. A run where the
        # word was paid and the Voice said something stale or about something
        # else would pass every line above.
        # The engine's `speak` line is hand-off evidence, not proof that the user
        # heard it, so the Voice transcript is waited for separately (#196).
        #
        # Matched on the question's words without its punctuation, for
        # `QUESTION_ASKED_SPOKEN_SUBSTRING`'s reason: the same run had the Voice
        # quote `需要我把这件事做完吗?` for a line ending `？`. `fresh` is what the
        # engine holds, and `_await_the_question` has already established that it
        # is this Stop's question rather than the answer the relay drove.
        spoken_fresh = self._while_the_call_is_up(
            lambda: self._voice_said_something_carrying(
                QUESTION_ASKED_SPOKEN_SUBSTRING, since=owing
            ),
            deadline_seconds=LIVE_CALL_ANSWER_SECONDS,
        )
        facts.check(
            "Voice spoke fresh Focus brief",
            spoken_fresh,
            (
                f"the engine spoke the Focus Session's brief into the call "
                f"({announcements[0].strip()!r}) and the Voice never said what that Session "
                f"had just said. The reading is taken at the gap (ADR 0017), and the engine "
                f"holds {fresh[:120]!r} for it. What the Voice said: "
                f"{self._voice_said_lines(since=owing) or 'nothing this call recorded'}"
            ),
        )
        return (
            f"{ringing_address} stopped and rang without a word said about it ({rings} EVENT "
            f"cue(s) on this call); then the Focus Session {focus_address} stopped and earned "
            f"one announcement after the Voice fell silent, {announcements[0].strip()!r}"
        )

    def _hung_up_by_voice_then_a_cool_down_and_a_ceiling(
        self,
        *,
        mark: int,
        waiting: hand_started.HandStartedSession,
        waiting_at: Path,
        waiting_address: str,
        turn: float,
        cool_down: float,
        ceiling: float,
        facts: _PhaseFacts,
    ) -> str:
        """Grade hang-up, Cool-down payment, and a silent ceiling end (#186, #195)."""
        opening = support.matching_lines(self._log_since(mark), HAND_OVER_LINE)
        ask = self._ask_by_voice(live_call.PLAIN, facts)
        runs_before = ask.wrapper_mark
        hanging = ask.landed_at
        ran = support.wait_for(
            lambda: bool(self._verbs_since(runs_before, Action.LIVE)),
            deadline_seconds=LIVE_CALL_HANDOFF_SECONDS,
            poll_seconds=LIVE_CALL_POLL_SECONDS,
        )
        went_down = support.wait_for(
            self._call_is_down,
            deadline_seconds=LIVE_CALL_END_SECONDS,
            poll_seconds=LIVE_CALL_POLL_SECONDS,
        )
        # The cue is played off the dispatch loop, on a thread of the adapter's
        # own, so the line lands a moment after the call is already down.
        ended_cue = support.wait_for(
            lambda: bool(self._cue_lines(Cue.ENDED, since=hanging)),
            deadline_seconds=LIVE_CALL_CUE_SECONDS,
            poll_seconds=LIVE_CALL_POLL_SECONDS,
        )
        hang_up_verbs = self._verbs_since(runs_before, Action.LIVE)
        # **Read here, before the Cool-down's own call opens.** `observed` takes
        # the newest value each field was ever written with, so an end reason
        # read after the next call has ended would be that call's.
        ended_by = _ended_by(
            end_reason=live_call.observed(self.config.call_observations).end_reason,
            by_ceiling=self._ceiling_ended_the_call(since=hanging),
            by_agent=went_down,
        )
        facts.record("hang-up verbs", hang_up_verbs)
        facts.record("hang-up end source", ended_by)
        facts.check(
            "hang-up heard",
            ask.heard,
            (
                f"the hang-up utterance went on the track and the engine never logged the "
                f"user's speech within {LIVE_CALL_HEARD_SECONDS:.0f}s. It carries "
                f"{live_call.REQUEST!r} and the line looked for carries "
                f"{LIVE_CALL_HEARD_SUBSTRING!r}. Engine log tail: {self._log_since(hanging)[-8:]}"
            ),
        )
        facts.check(
            "Call Agent ran live",
            ran,
            (
                f"the engine heard {self._user_speech_lines(since=hanging)[-1]!r} and the Call "
                f"Agent never ran `{Action.LIVE}`, which is the one verb its generated "
                f"instructions say ends a call (`core/instructions/agent.py`). What it ran "
                f"instead: {self._verbs_run(since=runs_before) or 'nothing at all'}"
            ),
        )
        facts.check(
            "call went down",
            went_down,
            (
                f"the Call Agent ran {hang_up_verbs} and the call was still up "
                f"{LIVE_CALL_END_SECONDS:.0f}s later ({self._call_line()!r})"
            ),
        )
        facts.check(
            "ENDED cue played",
            ended_cue,
            (
                f"the call ended on the user's word and no ENDED cue was written within "
                f"{LIVE_CALL_CUE_SECONDS:.0f}s. The user hears a call end however it ended "
                f"(#186). Engine log tail: {self._log_since(hanging)[-8:]}"
            ),
        )
        # Running `live` and that action actually ending the call are separate
        # product facts (#193).
        facts.check(
            "Call Agent ended call",
            ended_by == "agent",
            (
                f"the Call Agent ran {hang_up_verbs} and the call was ended by the {ended_by}: "
                f"{live_call.observed(self.config.call_observations).end_reason or 'none'}. "
                f"Verbs in order: {self._verbs_run(since=runs_before)}"
            ),
        )
        cue_complaint = self._two_cues_complaint(mark)
        facts.check("CONNECTED and ENDED cues ordered", not cue_complaint, cue_complaint)
        # --- the Cool-down, and the Session that stops inside it --------------
        # The playlist is emptied first: the transport's cursor is per call, so
        # a list left holding this call's seven sentences would replay all of
        # them into the one the Cool-down is about to pay for — and nobody is
        # supposed to speak on that call at all.
        live_call.ask_for_nothing(self.config.call_wav_directory)
        inside = len(self.engine.log_lines())
        self._drive_extra_session(waiting, waiting_at, turn, ASK_A_QUESTION)
        remaining = self._cool_down_remaining()
        self.journal(
            "live.call.cool_down.read",
            lane=self.lane.name,
            remaining_seconds=remaining,
            cool_down_seconds=cool_down,
        )
        owed = support.wait_for(
            lambda: bool(support.matching_lines(self._log_since(inside), COOL_DOWN_OWED_PATTERN)),
            deadline_seconds=turn + LIVE_CALL_CUE_SECONDS,
            poll_seconds=LIVE_CALL_POLL_SECONDS,
        )
        # Read the moment the engine has answered, and before the window can
        # close: a hand-over line found after the Cool-down elapsed is the owed
        # dial being *paid*, which is the next thing this phase grades.
        dialled_inside = bool(support.matching_lines(self._log_since(inside), HAND_OVER_LINE))
        paid = support.wait_for(
            lambda: bool(support.matching_lines(self._log_since(inside), COOL_DOWN_PAID_PATTERN)),
            deadline_seconds=cool_down + LIVE_CALL_OPEN_SECONDS,
            poll_seconds=LIVE_CALL_POLL_SECONDS,
        )
        # `paid` is the Keeper decision; the later hand-over line is the adapter
        # evidence that the realtime call actually came up (#195).
        support.wait_for(
            lambda: bool(support.matching_lines(self._log_since(inside), HAND_OVER_LINE)),
            deadline_seconds=LIVE_CALL_OPEN_SECONDS,
            poll_seconds=LIVE_CALL_POLL_SECONDS,
        )
        paid_dials = support.matching_lines(self._log_since(inside), HAND_OVER_LINE)
        # --- and the call nobody speaks on ends by itself ---------------------
        by_ceiling = support.wait_for(
            lambda: self._ceiling_ended_the_call(since=inside),
            deadline_seconds=LIVE_CALL_ANSWER_SECONDS + ceiling + LIVE_CALL_END_SECONDS,
            poll_seconds=LIVE_CALL_POLL_SECONDS,
        )
        ceiling_ended_cue = support.wait_for(
            lambda: bool(self._cue_lines(Cue.ENDED, since=inside)),
            deadline_seconds=LIVE_CALL_CUE_SECONDS,
            poll_seconds=LIVE_CALL_POLL_SECONDS,
        )
        opened_briefs = _hand_over_kinds(opening[0]).count(SESSION_BRIEF_KIND) if opening else 0
        paid_briefs = _hand_over_kinds(paid_dials[0]).count(SESSION_BRIEF_KIND) if paid_dials else 0
        facts.record("Cool-down seconds", cool_down)
        facts.record("Cool-down remaining at Stop", remaining)
        facts.record("waiting Session", waiting_address)
        facts.record("owed dial lines", owed)
        facts.record("paid dial lines", [line.strip() for line in paid_dials])
        facts.record("opening Session Brief count", opened_briefs)
        facts.record("paid Session Brief count", paid_briefs)
        facts.check(
            "Stop landed inside Cool-down",
            remaining > 0,
            (
                f"the call ended and this phase did not reach the third Session's Stop before "
                f"the {cool_down:.0f}s Cool-down had elapsed — `bridgectl status` already read "
                f"`call: none` with no Cool-down on it. Nothing is proven either way: an event "
                f"outside the window is one the engine is free to dial on, which is what the "
                f"window is for. This is the harness losing a race, not the Keeper breaking a "
                f"rule"
            ),
        )
        # **The missing owed line is asked about first, and the order is why.**
        # `dialled_inside` is read after the `owed` wait, so a run where the
        # engine never wrote that line spends the wait's whole budget — long
        # enough for the Cool-down to elapse and its legitimately paid dial to
        # land inside the window this reads. Complaining about the dial first
        # would mis-state such a run as "it dialled inside the Cool-down" when
        # what actually went wrong is the line that never came.
        facts.check(
            "dial marked owed",
            owed,
            (
                f"a Session stopped with {remaining:.0f}s of Cool-down still to run and the "
                f"engine neither dialled nor said it owed a dial. One event buys one attempt, "
                f"and an event inside a Cool-down marks it owed. Engine log tail: "
                f"{self._log_since(inside)[-8:]}"
            ),
        )
        facts.check(
            "no dial inside Cool-down",
            not dialled_inside,
            (
                f"a Session stopped with {remaining:.0f}s of the {cool_down:.0f}s Cool-down "
                f"still to run and the engine dialled anyway: "
                f"{support.matching_lines(self._log_since(inside), HAND_OVER_LINE)!r}. After "
                f"any end of a call the system does not dial again until the Cool-down has "
                f"elapsed (`CONTEXT.md`, *Cool-down*)"
            ),
        )
        facts.check(
            "owed dial paid",
            paid,
            (
                f"the engine owed a dial and never paid it within {cool_down:.0f}s of Cool-down "
                f"plus {LIVE_CALL_OPEN_SECONDS:.0f}s. An owed dial is paid from a fresh reading "
                f"when the Cool-down elapses (ADR 0017). Engine log tail: "
                f"{self._log_since(inside)[-8:]}"
            ),
        )
        facts.check(
            "one paid dial",
            len(paid_dials) == 1,
            (
                f"the Cool-down elapsed and the engine dialled {len(paid_dials)} times, not "
                f"once: {paid_dials!r}. One wake buys one dial, however many events arrived"
            ),
        )
        paid_kinds = _hand_over_kinds(paid_dials[0])
        facts.check(
            "paid dial carried Session Brief",
            SESSION_BRIEF_KIND in paid_kinds,
            (
                f"the Cool-down's dial carried {paid_kinds or 'nothing it named'} and no "
                f"{SESSION_BRIEF_KIND} — a Session stopped on a question inside the window, so "
                f"the reading the dial was made on has to hold a brief for somebody: "
                f"{paid_dials[0].strip()!r}"
            ),
        )
        facts.check(
            "silent paid call ended by ceiling",
            by_ceiling,
            (
                f"the Cool-down's dial came up, nobody spoke on it, and the engine never said "
                f"its own Silence Ceiling ended it within {ceiling:.0f}s and the step's "
                f"patience. The call now: {self._call_line()!r}. Engine log tail: "
                f"{self._log_since(inside)[-8:]}"
            ),
        )
        facts.check(
            "ceiling-ended call played ENDED cue",
            ceiling_ended_cue,
            (
                f"the ceiling ended the call and no ENDED cue was written within "
                f"{LIVE_CALL_CUE_SECONDS:.0f}s. The user hears a call end however it ended "
                f"(#186), and the Keeper is what rings it now (#195)"
            ),
        )
        return (
            f"the user hung up by voice, the Call Agent ran {hang_up_verbs}, the call was "
            f"ended by the {ended_by} and ENDED was played; a Session stopped with "
            f"{remaining:.0f}s of the {cool_down:.0f}s Cool-down still to run and nothing "
            f"dialled; the owed dial was "
            f"paid exactly once when it elapsed, carrying {paid_briefs} Session Brief(s) "
            f"against the ended call's {opened_briefs}; and the call nobody spoke on ended by "
            f"the {ceiling:.0f}s Silence Ceiling"
        )

    def _extra_address(self, workspace: Path, fallback: str) -> str:
        """That extra Session's address **as the roster spells it right now**.

        A row is re-keyed as the lane learns more about it, and an address held
        across a turn can name a row the registry no longer holds — which the
        control plane answers, correctly, with `unknown Session`. Reading it
        again costs one `status` and is the only way to address a Session the
        harness did not create the identity of. The last known address is
        returned when the row has gone, so the caller's own refusal says what
        it was trying to reach.
        """
        return _address_of(self._row_in(workspace) or {}) or fallback

    def _await_extra_session(self, workspace: Path, turn_seconds: float) -> str:
        """Wait for an extra Session to reach the roster, and answer with its address.

        The budget is the roster's own discovery allowance plus one agent turn:
        a launch runs a boot turn from its first moment (#110) and a Codex
        Session is listed only once its daemon holds it (ADR 0020), so what is
        being waited out is a turn and a discovery pass, both of which this
        module already has a number for.
        """
        deadline = DISCOVERY_SECONDS + turn_seconds
        listed = support.wait_for(
            lambda: self._row_in(workspace) is not None,
            deadline_seconds=deadline,
            poll_seconds=LIVE_CALL_POLL_SECONDS,
        )
        if not listed:
            raise LaneBlocked(
                f"an extra Session was started in {workspace} the way the lane's own is and "
                f"never reached the roster within {deadline:.0f}s. What the engine holds: "
                f"{[row.get('workspace') for row in self._roster_rows()]}"
            )
        return _address_of(self._row_in(workspace) or {})

    def _drive_extra_session(
        self,
        extra: hand_started.HandStartedSession,
        workspace: Path,
        turn_seconds: float,
        instruction: Instruction = ACKNOWLEDGE,
    ) -> None:
        """Type one instruction at an extra Session's keyboard, once it can take one.

        **Which instruction is the caller's**, since #198. Most of these Sessions
        only have to *stop*, and `ACKNOWLEDGE` is the cheapest way to make one
        stop; the two that a call is dialled about have to stop **on a question**
        — the walk's whole premise is a Session that needs the user, and what the
        user says back is an answer to something — so those are driven with
        `ASK_A_QUESTION`. Both are words-only for `ACKNOWLEDGE`'s reason: a turn
        that raised a permission would sit in `waiting` until somebody answered
        it, and nobody here is going to.

        **Idle first.** A lane with a `boot` prompt is running a turn from the
        moment it starts (#110), and nothing may be typed into a Session that is
        mid-turn — the rule `settle_boot_turn` exists for, applied to a Session
        this phase started rather than to the lane's own.

        Nothing is waited for afterwards. What follows the Stop is a cue, and the
        caller waits on that; a wait on the roster in between would be a second,
        weaker reading of the same turn. No ground truth is taken for these
        Sessions either, because nothing grades what they said — only that they
        stopped.
        """
        idle = support.wait_for(
            lambda: str((self._row_in(workspace) or {}).get("state")) != "running",
            deadline_seconds=turn_seconds,
            poll_seconds=LIVE_CALL_POLL_SECONDS,
        )
        extra.submit(instruction.words)
        self.journal(
            "extra.session.driven",
            lane=self.lane.name,
            workspace=str(workspace),
            was_idle=idle,
            state=(self._row_in(workspace) or {}).get("state"),
        )

    def _verbs_since(self, runs_before: int, action: Action) -> list[str]:
        """Every run of one verb the Call Agent made after a mark, whole argv tail.

        The mark is a count of wrapper lines rather than a time, for
        `_log_since`'s reason: the Call Agent may run a verb more than once per
        request (#181 finding 1), and what a phase asks about is the runs *it*
        provoked. The whole tail and not the verb, because the argv is where the
        Session it chose is written down.
        """
        return [
            verb for verb in self._verbs_run(since=runs_before) if verb.split()[:1] == [str(action)]
        ]

    @contextmanager
    def _voice_route_only(self) -> Iterator[None]:
        """Voice on, Message off — the one state whose only outlet is opening a call.

        The walk runs text-only (`arm_switches`), which is the right mode for
        every step that reads the chat and the wrong one for the only step about
        the engine dialling: with Message on, the Companion Channel is a second
        route and a notice that took it would say nothing about the call. Put
        back on the way out whatever this step did, so the steps after it are on
        the mode they were written for.
        """
        for name, position in (("message", "off"), ("voice", "on")):
            answer = self.bridgectl("switch", name, position)
            if not answer.ok:
                raise LaneBlocked(f"`switch {name} {position}` refused: {answer.text}")
            self.journal("switch.armed", lane=self.lane.name, switch=name, to=position)
        try:
            yield
        finally:
            for name, position in (("voice", "off"), ("message", "on")):
                self.bridgectl("switch", name, position)
                self.journal("switch.armed", lane=self.lane.name, switch=name, to=position)

    def _watch_the_voice(
        self,
        mark: int,
        *,
        deadline_seconds: float,
        poll_seconds: float = LIVE_CALL_POLL_SECONDS,
        quiet_seconds: float | None = None,
    ) -> _VoiceWatch:
        """Watch Voice activity after an engine-log mark until quiet or call-down (#184)."""

        def activity() -> tuple[dict[bool, int], int]:
            # A transcript turn is activity, but the closed-edge guard below
            # keeps one intermediate fragment from proving completion (#184).
            return self._voice_speech_edges(since=mark), len(self._voice_said_lines(since=mark))

        edges, said = activity()
        spoke = bool(any(edges.values()) or said)
        first_voice_at = last_voice_at = time.monotonic() if spoke else None
        expiry = time.monotonic() + deadline_seconds
        while time.monotonic() < expiry:
            if self._call_is_down():
                break
            time.sleep(poll_seconds)
            seen, seen_said = activity()
            if (seen, seen_said) != (edges, said):
                edges, said = seen, seen_said
                last_voice_at = time.monotonic()
                if first_voice_at is None:
                    first_voice_at = last_voice_at
            elif (
                quiet_seconds is not None
                and (edges[True] > 0 or edges[False] > 0 or said > 0)
                and edges[False] >= edges[True]
                and last_voice_at is not None
                and time.monotonic() - last_voice_at >= quiet_seconds
            ):
                break
        return _VoiceWatch(
            went_down=self._call_is_down(),
            edges=edges,
            first_voice_at=first_voice_at,
            last_voice_at=last_voice_at,
            down_at=time.monotonic(),
        )

    def _verbs_run(self, *, since: int = 0) -> list[str]:
        """Read wrapper-log verbs from a pre-ask line mark (#193, #223)."""
        spoken: list[str] = []
        for line in support.cli_wrapper_runs(self.config.cli_wrapper_log)[since:]:
            words = line.split()[1:]  # the UTC stamp is the harness's, not the agent's
            while words and words[0].startswith("--"):
                # `--socket <path>`, and any other option the instructions carry.
                words = words[2:] if len(words) > 1 else []
            if words:
                spoken.append(" ".join(words))
        return spoken

    def _log_since(self, mark: int) -> list[str]:
        """The engine's log from a mark on — one call's worth, not the run's."""
        return self.engine.log_lines()[mark:]

    def _user_speech_lines(
        self, carrying: str = LIVE_CALL_HEARD_SUBSTRING, *, since: int = 0
    ) -> list[str]:
        """Read user-speech lines carrying the request-derived fragment (#181, #223)."""
        return [
            line
            for line in support.matching_lines(self._log_since(since), USER_SPEECH_LINE)
            if _unspaced(carrying) in _unspaced(line)
        ]

    def _user_speech_landed_at(self, carrying: str, *, since: int) -> int:
        """Return the engine-log index where the request-derived fragment landed (#223)."""
        window = self._log_since(since)
        return next(
            (
                since + at
                for at, line in enumerate(window)
                if re.search(USER_SPEECH_LINE, line) and _unspaced(carrying) in _unspaced(line)
            ),
            since,
        )

    def _while_the_call_is_up(
        self, condition: Callable[[], bool], *, deadline_seconds: float
    ) -> bool:
        """Bound a spoken-call wait by its condition, deadline, and call lifetime (#223)."""
        support.wait_for(
            lambda: condition() or self._call_is_down(),
            deadline_seconds=deadline_seconds,
            poll_seconds=LIVE_CALL_POLL_SECONDS,
        )
        if condition():
            return True
        # **One more look, once.** The engine writes its log from a thread, so a
        # line about the last thing said on a call can land after the call is
        # already down — and this loop's other exit is exactly that moment.
        # Without this the wait would answer "never said" about words that were
        # in the log a poll later.
        time.sleep(LIVE_CALL_POLL_SECONDS)
        return condition()

    def _voice_said_lines(self, *, since: int = 0) -> list[str]:
        """Every line the realtime adapter wrote down of what the Voice said (#197).

        The Voice transcript, which is the only witness this side has of the
        Voice's own words: the Call seam raises the Voice's half as a *span* and
        never as text (`seams/call.py`), so a step that wants to know what was
        said reads the adapter's log line and nothing else. Stripped, because
        every caller either greps it or prints it.
        """
        return [
            line.strip()
            for line in support.matching_lines(self._log_since(since), VOICE_SAID_PATTERN)
        ]

    def _voice_said_matching(
        self, patterns: tuple[str, ...], *, since: int, until: int | None = None
    ) -> list[str]:
        """The Voice's turns in a window that match any of `patterns` (#198, #221).

        A pattern rather than a word for `UNDELIVERED_SPOKEN_PATTERN`'s reason,
        and a window with two ends because phase 3 has two of them to tell apart:
        what the Voice said after the relay ran, which is the receipt, and what
        it said before, which is #221. Spaceless for
        `_voice_said_something_carrying`'s reason.
        """
        window = self._log_since(since)
        if until is not None:
            window = window[: max(until - since, 0)]
        return [
            line.strip()
            for line in support.matching_lines(window, VOICE_SAID_PATTERN)
            if any(re.search(pattern, _unspaced(line)) for pattern in patterns)
        ]

    def _voice_said_something_carrying(self, fragment: str, *, since: int) -> bool:
        """Whether the Voice's transcript since a mark carries a fragment (#181, #198).

        Spaceless on both sides for `_user_speech_lines`' reason, and for one
        more of its own: the Voice transcript comes back from the same
        recogniser-adjacent path and has been seen to put a boundary inside a
        word. What is asked is only whether the words are in there.
        """
        return any(
            _unspaced(fragment) in _unspaced(line) for line in self._voice_said_lines(since=since)
        )

    def _arm_auto_hangup(self) -> None:
        """Auto Hang-up on, which is the switch the Silence Ceiling answers to.

        The walk's last phase ends a call by silence, and `may_auto_hangup` is
        what permits it (`core/adjudication.py`) — so a walk that reached that
        phase with the switch off would wait a whole ceiling out and grade the
        engine for obeying a switch. `switches` flips it and puts it back, and
        the ticket's own first line says the state this walk starts from, so it
        is *set* rather than assumed: a step that reads a precondition it needs
        and does not establish it is a step that fails somewhere else.
        """
        answer = self.bridgectl("switch", "auto_hangup", "on")
        if not answer.ok:
            raise LaneBlocked(f"`switch auto_hangup on` refused: {answer.text}")
        self.journal("switch.armed", lane=self.lane.name, switch="auto_hangup", to="on")

    def _two_cues_complaint(self, mark: int) -> str:
        """Why CONNECTED and ENDED were not both present and ordered (#186).

        The reading is taken here and judged by `_cue_complaint`, which is a
        module-level function with no walk behind it so CI grades the rule
        itself — an acceptance run is an expensive place to discover an ordering
        written the wrong way round (#109).

        The speech lines are read first and the whole log second, so what is
        searched is a superset of what is searched for: the log grows while this
        runs, and the other way round a line could be looked for in a reading
        taken before it was written.
        """
        spoken = set(self._user_speech_lines(since=mark))
        complaint = _cue_complaint(
            self._log_since(mark), spoken, device=self._configured_output_device()
        )
        return complaint

    def _configured_output_device(self) -> int | None:
        """Which output index this lane's engine sends cues to, if it was told one.

        Read out of the lane's own config rather than assumed, the way the
        Silence Ceiling is: the adapter names whatever it was given in the line
        this step matches, and a run that pinned a device would otherwise be
        graded against a line for a device it is not using.
        """
        document = tomllib.loads(self.config.path.read_text())
        table = document.get("adapters", {}).get("settings", {}).get("call", {})
        given = table.get("output_device")
        return None if given is None else int(given)

    def _cue_lines(self, cue: Cue, *, since: int = 0) -> list[int]:
        """Where in this call's log each `cue` was played, as offsets from the mark."""
        return _cue_line_indices(self._log_since(since), cue)

    def _cue_order(self, *, since: int = 0) -> list[str]:
        """Every cue this call played, in the order the engine wrote them down."""
        lines = self._log_since(since)
        played = [(line_index, cue) for cue in Cue for line_index in _cue_line_indices(lines, cue)]
        return [str(cue) for _, cue in sorted(played)]

    def _voice_speech_edges(self, *, since: int = 0) -> dict[bool, int]:
        """How many times the engine wrote each edge of its own Voice down (#184).

        Counted rather than timed. The log's record format is a format and not a
        contract — nothing downstream parses it (`engine/logfile.py`) — so a step
        reads *whether* each edge happened out of the log and *when* off its own
        clock.
        """
        lines = self._log_since(since)
        return {
            speaking: len(support.matching_lines(lines, re.escape(pattern)))
            for speaking, pattern in ((True, VOICE_SPEAKING_LINE), (False, VOICE_QUIET_LINE))
        }

    def _voice_finished_speaking(self, mark: int) -> Callable[[], bool]:
        """Whether every Voice span visible after the request landing has closed (#223).

        A span may open before the final user-speech line and close after it, so
        the post-landing window may legitimately contain one more stop than
        start. It must contain a stop, and no post-landing start may remain open.
        """

        def finished() -> bool:
            edges = self._voice_speech_edges(since=mark)
            return edges[False] > 0 and edges[False] >= edges[True]

        return finished

    def _ceiling_ended_the_call(self, *, since: int = 0) -> bool:
        """Whether the engine says its own Silence Ceiling was what ended this call."""
        return bool(support.matching_lines(self._log_since(since), CEILING_END_LINE))

    def _silence_ceiling_seconds(self) -> float:
        """The Silence Ceiling this lane's engine is actually running.

        Read out of the lane's own config, and only then off the shipped
        default. A literal here would be a second copy of a policy value the
        engine already owns, and it would go stale the first time a run set one.
        """
        document = tomllib.loads(self.config.path.read_text())
        given = document.get("policy", {}).get("silence_end_seconds")
        return DEFAULT_SILENCE_END_SECONDS if given is None else float(given)

    def _call_line(self) -> str:
        """What `bridgectl status` says about the call, as the surface renders it."""
        answer = self.bridgectl("status")
        for line in answer.text.splitlines():
            if line.startswith("call:"):
                return line.strip()
        return answer.text[:200]

    def _call_is_down(self) -> bool:
        """No call is up **right now**, in the engine's own words.

        The reading needs a subprocess and a whole harness; the judgement is a
        string rule, and `_no_call_is_up` holds it where CI can grade it.
        """
        return _no_call_is_up(self._call_line())

    def _end_any_live_call(self) -> None:
        """Leave no call behind, and never open one doing it."""
        if self._call_is_down():
            return
        answer = self.bridgectl("live", timeout=LIVE_CALL_OPEN_SECONDS)
        self.journal("live.call.cleanup", lane=self.lane.name, answer=answer.text)

    def _leave_no_call_up(self, quiet_seconds: float) -> bool:
        """End an existing call and wait for its asynchronous cleanup (#195)."""
        quiet_since: float | None = None
        deadline = time.monotonic() + quiet_seconds + LIVE_CALL_END_SECONDS
        while time.monotonic() < deadline:
            if not self._call_is_down():
                self._end_any_live_call()
                quiet_since = None
            elif quiet_since is None:
                quiet_since = time.monotonic()
            elif time.monotonic() - quiet_since >= quiet_seconds:
                return True
            time.sleep(LIVE_CALL_POLL_SECONDS)
        return self._call_is_down()


def run(walk: journey_module.Walk, selection: PhaseSelection) -> str:
    """Walk the Live Call step through the module beside `journey.py`."""
    return _LiveCallRun(walk, selection).live_call()

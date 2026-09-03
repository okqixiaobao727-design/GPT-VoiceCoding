"""The steps of the bridge journey, written once and walked by both lanes.

## What changed, and why

The journey this module used to hold walked the **launch** journey: `bridgectl
launch` started a Session, the steps watched what the product had started, and
`bridgectl close` ended it. Map #67 redrew the destination — v1.0 is a *bridge*
over the Sessions the user starts — so a harness that starts its own Sessions
through the product is measuring the wrong thing, and #72 has since parked the
launcher out of the protocol entirely (`launch` and `close` are not actions on
`main`). The harness now starts each Session **the way the user does**: the
ordinary `claude` / `codex` binary, in a pty, no wrapper (`hand_started.py`).

Steps `0c`, `1a`, `1b` and `8` are gone with the launcher. `0b` (the realtime
contract probe) and the provenance compare stay where they were.

## The step names are a contract

Every one of the build tickets #74–#80 cites a step name from `STEPS` verbatim in
its "Red first" line. Renaming one here silently moves seven tickets' exit
criteria, so the names are fixed and their spelling is the interface.

Since #182 the names are also what `--step` takes, which makes the spelling an
interface twice over: a build ticket's "Red first" line is now a command a
developer runs. `PREREQUISITES` says what each step needs behind it, and
`select` turns a `--step` list into the steps to walk (`Selection.steps`) and the
subset of them to grade (`Selection.selected`). The rest run as **ungraded
setup**: a walk with one selected step is a claim about that step, arranged on the
state the whole lane would have given it, and the verdict says which rows are
which so a green step is never read as a green lane.

## What the steps rest on, and what they never rest on

Observations come from the agent's own roster or rollout, the filesystem, the
engine's reply and log, and the real Telegram chat. **Never from the screen.**
Measured on 2026-08-26: both TUIs redraw with cursor addressing, and `codex` in
a pty interleaves to roughly one glyph per line once escapes are stripped. The
raw stream is kept as an artifact for a human; nothing parses it.

## What a step may attribute to itself

The engine this run spawns bridges **every** Session on the machine, so the chat
is a shared surface: at any moment it may carry a notice about somebody's open
work that has nothing to do with the lane being walked. The rule that follows
from that is one line, and it is stated here so no step has to relearn it —

> **A step only ever attributes what names its own target.**

Every chat read in this module goes through `Walk._await_message_naming`, which
is where the rule is enforced; `_naming_forms` is what "names" means. That
includes the two reads that assert *absence*, where an unattributed message is a
false red rather than a false green, and `drain_boot_notice`, which is neither —
a stranger's notice accepted there ends the drain early and lets the real boot
notice land where `stop notice` is looking. Learned on run `20260826T213402Z`, where
`stop notice` passed on a permission prompt belonging to a stale `/tmp/vcprobe`
thread, and on a quieter machine would have failed for a reason equally
unrelated to the lane (#109). The sibling lesson had already been learned once,
one module over, for the pending dialogs `approval_effect.resolve` correlates —
read off the roster's own PERMISSION rows since #191.

## The turns, and why there are five

A step that needs a turn drives its own, because a turn shared between steps
makes one step's failure look like another's. The one exception is stated where
it happens: `relay` and `approval` observe the *same* turn from two ends — the
words arriving and the permission that turn raises — because a relayed
instruction that needs a permission is exactly the shape the product has to
survive, and running it twice would prove less at twice the cost.

The Codex lane runs a sixth that no step drives: it is *launched* with a prompt,
because that is what carries it past Codex's update gate (#110), and a prompt on
the command line is a turn. No step observes it and `Walk.settle_boot_turn` waits
it out before the walk begins — a turn still running when the first step types is
a turn whose Stop lands where a later step is looking for a different one.
"""

from __future__ import annotations

import json
import os
import re
import time
import tomllib
from collections.abc import Callable, Container, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import approval_effect
import hand_started
import live_call
import support
from support import LaneBlocked, StepFailed

from gpt_voicecoding.adapters.call.realtime import cues
from gpt_voicecoding.adapters.call.realtime.adapter import VOICE_SAID_LINE
from gpt_voicecoding.core.bridge import VOICE_QUIET_LINE, VOICE_SPEAKING_LINE
from gpt_voicecoding.core.briefing import STATE_WORDING, BriefState
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
    DEFAULT_RELAY_CEILING_SECONDS,
    DEFAULT_SILENCE_END_SECONDS,
    DEFAULT_SPEECH_SETTLE_SECONDS,
)
from gpt_voicecoding.core.relays import RelayReason
from gpt_voicecoding.seams.call import Cue, SpokenBrief, SpokenRosterBrief
from gpt_voicecoding.seams.control_plane import Action

if TYPE_CHECKING:
    # Named for a type and never imported at runtime: telethon lives behind this
    # module (`telegram_person.py`, "Telethon lives here and nowhere else"), and
    # the fast suite imports this file to test its attribution rule without it.
    import telegram_person

#: Every step, in the order #73 fixed and #183 appended to. The first nine are
#: cited verbatim by #74–#80; the two call steps are #183's and #184's, and they
#: are last because a call holds the interlock and a whole-lane run wants the
#: turn-driving steps behind it.
STEPS = (
    "roster",
    "stable name",
    "brief",
    "stop notice",
    "relay",
    "approval",
    "companion inbound",
    "switches",
    "child",
    "live call",
)

#: The steps that dial — one, since #198 folded v0's route, v1's dial and v2's
#: mid-call news into a single walk. A run that walks it gets the harness's own
#: Call adapter and the `bridgectl` wrapper; a run that does not keeps the Call
#: adapter the user actually configured (`conftest.py`, #183). Still a tuple:
#: `conftest` asks whether the selection holds any of them, and one name today
#: is not a promise that a later ticket adds none.
LIVE_CALL_STEPS = ("live call",)

#: What each step needs to have run **before** it, so that a step selected on its
#: own stands on the state the whole walk would have given it. Read off `Walk`'s
#: own fields rather than guessed, and every edge points backwards through
#: `STEPS` (`tests/test_journey_selection.py` holds both):
#:
#: * everything needs `roster`, because `roster` is what sets `Walk.address` and
#:   `Walk.truth`, and every later step either names the address or reads the
#:   agent's own record through it;
#: * `brief` needs `stable name`, because what it reads is history "after a
#:   turn" and `stable name` is the walk's first turn — the Claude lane launches
#:   silent, so without it there is nothing for `bridgectl brief` to carry and
#:   nothing for `bridgectl history` to page through;
#: * `stop notice` needs `stable name` for the same turn seen from the other end:
#:   the mark it waits behind is taken inside that step (`Walk.before_first_turn`)
#:   and the Stop it looks for is that turn's;
#: * `approval` needs `relay`, which is the one turn two steps share — the words
#:   arriving and the permission that turn raises (`Walk.approval_resolution`).
#:
#: `companion inbound`, `switches` and `child` drive their own turns and need
#: nothing but a Session to name.
PREREQUISITES: Mapping[str, tuple[str, ...]] = {
    "roster": (),
    "stable name": ("roster",),
    "brief": ("roster", "stable name"),
    "stop notice": ("roster", "stable name"),
    "relay": ("roster",),
    "approval": ("roster", "relay"),
    "companion inbound": ("roster",),
    "switches": ("roster",),
    "child": ("roster",),
    #: The whole 0901 flow, and it needs one thing from the walk: a roster with
    #: the lane's own Session in it. The three extra Sessions every phase is
    #: graded against are the walk's own to start (#196, #198), so no *address*
    #: is owed — but phase 1 grades a dial for carrying the Roster Brief, and a
    #: roster the lane has never proved it can read is not something to grade a
    #: hand-over against. #183's "runnable alone" clause is satisfied by the
    #: prerequisite rather than against it: `roster` is one step, not a walk.
    "live call": ("roster",),
}


class UnknownStep(Exception):
    """`--step` named something that is not a step. It carries the ones there are."""


@dataclass(frozen=True)
class Selection:
    """Which steps a run grades, and which it merely arranges to reach them.

    A single-step run is not a lane. The two tuples are kept apart all the way
    into `verdict.json` for that one reason: a reader who sees `stable name`
    green must also see that `roster` ran ungraded beneath it, or a green step
    reads as a green lane (#182).
    """

    #: Graded. What the run promised to observe, in `STEPS` order.
    selected: tuple[str, ...]
    #: Ungraded. The prerequisite closure of `selected`, minus anything selected.
    setup: tuple[str, ...]

    @property
    def steps(self) -> tuple[str, ...]:
        """Everything to walk, in `STEPS` order — the order is the walk's."""
        chosen = set(self.selected) | set(self.setup)
        return tuple(step for step in STEPS if step in chosen)

    @property
    def whole_lane(self) -> bool:
        """Every step graded: the pre-merge full run, rather than a ticket's step."""
        return self.selected == STEPS

    def graded(self, step: str) -> bool:
        return step in self.selected


def select(names: Sequence[str] | None = None) -> Selection:
    """Resolve `--step` into what to walk and what to grade.

    No names is the full run: every step graded, nothing as setup. Any name is
    matched against `STEPS` **exactly** — these spellings are the interface
    every build ticket's "Red first" line cites, so a near miss is a refusal that
    carries them all rather than a run that quietly walks one fewer.
    """
    asked = tuple(dict.fromkeys(names or ()))
    if not asked:
        return Selection(selected=STEPS, setup=())
    unknown = [name for name in asked if name not in PREREQUISITES]
    if unknown:
        raise UnknownStep(
            f"no such acceptance step: {', '.join(repr(name) for name in unknown)}. "
            f"The steps are: {', '.join(STEPS)}."
        )
    selected = tuple(step for step in STEPS if step in set(asked))
    needed: set[str] = set()
    pending = list(selected)
    while pending:
        for one in PREREQUISITES[pending.pop()]:
            if one not in needed:
                needed.add(one)
                pending.append(one)
    setup = tuple(step for step in STEPS if step in needed - set(selected))
    return Selection(selected=selected, setup=setup)


#: How many times `stable name` reads the roster. #73: "identical across three
#: `status` calls and across a Stop".
NAME_READS = 3

#: How long the engine gets to notice a Session that is already running before an
#: empty roster is taken as the answer. Not derived from the product, because on
#: `main` there is no discovery to derive it from — #74 builds it. Chosen with the
#: reason stated: long enough that any polling discovery has ticked at least once
#: and a slow `codex` boot (MCP servers; measured at tens of seconds on this
#: machine) is not read as a missing Session, short enough that a roster which is
#: simply empty is an answer rather than a wait. Re-derive it from #74's own
#: cadence once there is one.
DISCOVERY_SECONDS = 30.0

#: How long a boot turn gets, as a multiple of the far side's turn figure.
#: Derived rather than guessed: `settle_boot_turn` waits out a TUI's **boot and**
#: its first turn, and boot alone has been measured at the whole turn figure on
#: this machine — a `codex` sat in `starting MCP servers` for a full 180s
#: ground-truth wait on 2026-08-26 (`hand_started.codex_ground_truth`). So two of
#: them, one for each half, and a lane still unsettled after that is blocked
#: rather than typed into. Blocked, not merely slow: everything after it would be
#: measuring a Session with a turn still running underneath.
BOOT_TURN_ALLOWANCE = 2.0

#: The engine's own line for a Stop it announced (`core/bridge.py:_announce_waiting`).
#: `stop notice` matches a looser pattern for its own purposes; `drain_boot_notice`
#: wants the announcement itself, because "was a notice raised for the boot turn"
#: is exactly the question it is asking.
#:
#: **The record is several lines now** (#189): the Stop Notice is a Session Brief
#: and the log carries `Briefing.text` whole, so `Session stopped:` opens the
#: first line and the brief's labelled lines follow. `support.matching_lines`
#: greps line by line, so this still matches once per Stop — the header line.
ENGINE_STOP_LINE = r"(?i)Session stopped:"

#: What a Stop Notice about a permission says (`core/briefing.py::_decision_lines`).
#: **There is one notice for a permission now** (#191): the dialog rides the
#: Session's Stop like every other wait, so what this matches is the brief's own
#: decision line rather than a retired announcement of its own.
#:
#: Matched rather than quoted whole: the tool name and the detail are the agent's,
#: and this run does not get to predict them. **Which Session it is about is not
#: this pattern's business either** — the brief's header names it, and the
#: attribution rule is what reads that.
APPROVAL_ANNOUNCEMENT = re.compile(r"^\s*permission:", re.IGNORECASE | re.MULTILINE)

#: What a Session Brief says about where the user's reply can reach the Session
#: (`core/briefing.py::_session_lines`). Quoted whole, because these two lines
#: are the product's answer to the one question this harness asks of an
#: announced wait: can the person act on it from here, or are they being sent to
#: the keyboard?
ANSWERABLE_HERE = "answer: from here"
ANSWERABLE_AT_THE_TERMINAL = "answer: at the terminal"

#: The engine's own line for what the user said (`core/bridge.py:796`). It is the
#: only line either call event has: `CallStarted` and `CallEnded` are noted into
#: the interlock and never logged (`core/bridge.py:777-785`), which is why the
#: `live call` step reads those two off the control plane instead.
USER_SPEECH_LINE = r"user speech, for the voice thread to act on"

#: The fragment of the request the engine's line has to carry. A fragment rather
#: than the sentence because ASR text is unstable (#181 finding 1), and this one
#: because it is the request's own tail — every trial in the probe's record
#: (`docs/research/probes/20260901T213252Z-agent-hangup.jsonl`) carried it.
#:
#: **Matched with every space taken out of both sides**, which is not tidying: on
#: run `20260902T093755Z` the two lanes heard the same four seconds of audio and
#: wrote it down differently — the Claude lane's engine logged `…结束通话` and the
#: Codex lane's `…结束通 话`, a space inserted inside the word. The request has no
#: spaces in it at all, so a space anywhere in the transcript is the recogniser's
#: and never the speaker's, and a substring test that counted one was a test of
#: which recogniser answered rather than of whether the words arrived.
LIVE_CALL_HEARD_SUBSTRING = "结束通话"

#: The same, for the long-answer variant (`live_call.LONG_REQUEST`), which has no
#: hang-up in it to end with. Its own middle, for the same reason: a fragment,
#: spaceless, and one the recogniser has no word boundary to insert inside.
LIVE_CALL_LONG_HEARD_SUBSTRING = "一个数字"

#: The same, for the hand-over variant (`live_call.NEEDS_REQUEST`). Its own
#: middle again, and spaceless: "需要我" is what survives a recogniser that puts a
#: boundary anywhere in a four-character question.
LIVE_CALL_NEEDS_HEARD_SUBSTRING = "需要我"

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
#: Three spellings measured on three real calls, all of the same fact:
#: `冇送到` (`20260903T112830Z` — the Voice answers this user in Cantonese,
#: where 没 is 冇), `没送达` (`20260903T121349Z`) and #173 §6's own `没送到`.
#: Pinning any one of them would have graded the product's own rule — a Session
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
LIVE_CALL_RELAY_HEARD_SUBSTRING = "回一句话"

#: The payload half of that same sentence: what the user answers the Session's
#: question with, and so what that Session's **next turn** has to carry (#198).
#: The one fragment of the utterance the step follows all the way through — the
#: air, the Call Agent's argv, and the Session's own record.
LIVE_CALL_ANSWER_SUBSTRING = "可以继续"

#: The three fragments #198's Detail and History utterances are recognised by,
#: chosen for `LIVE_CALL_HEARD_SUBSTRING`'s reasons: the middle of each sentence,
#: spaceless, and none of them the Session's name — which is the Call Agent's
#: half to get right and not this line's.
LIVE_CALL_DETAIL_HEARD_SUBSTRING = "详细说说"
LIVE_CALL_HISTORY_HEARD_SUBSTRING = "之前说了什么"
LIVE_CALL_EARLIER_HEARD_SUBSTRING = "再往前"

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
RELAY_RECEIPT_QUEUED = "收到"

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


def _history_reading(printed: str) -> HistoryReading:
    """One History page as `bridgectl history` printed it, read back off its own lines.

    Parsed from the printed page rather than from a second request shape, because
    what this run accepts is the surface a user gets — and in one place, because
    two readers ask for a page (`_history_page`, for which a refusal is a defect,
    and `_history_read_yet`, for which it is an answer) and two parsers of one
    surface are two things to keep in step.
    """
    entries: list[tuple[int, str]] = []
    older = False
    for line in printed.splitlines():
        stripped = line.strip()
        if stripped.startswith("older entries remain"):
            older = True
            continue
        match = HISTORY_ENTRY.match(stripped)
        if match is not None:
            entries.append((int(match.group("ordinal")), match.group("text")))
    return HistoryReading(entries=tuple(entries), older=older)


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
    failure and the caller has already asked about it. Matched spaceless on both
    sides for `_user_speech_lines`' reason.

    A module-level rule with no walk behind it, for `_cue_complaint`'s reason: an
    acceptance run is an expensive place to discover an arithmetic written the
    wrong way round, and CI grades this one for free.
    """
    at = next(
        (
            index
            for index, line in enumerate(lines)
            if re.search(VOICE_SAID_PATTERN, line)
            and any(_unspaced(wording) in _unspaced(line) for wording in receipts)
        ),
        None,
    )
    if at is None:
        return None
    after = lines[at + 1 :]
    return len(support.matching_lines(after, VOICE_SAID_PATTERN)) - len(
        support.matching_lines(after, MID_CALL_SPOKEN_PATTERN)
    )


def _newest_message(brief: str) -> str:
    """The Session's newest message, as a Session Brief hands it over (#187).

    One line of a printed brief, read the way a person reads it. A module-level
    function with no walk behind it for `_hand_over_of_more_than_one`'s reason:
    two steps read this line — `brief` on the walk's own Session and the folded
    `live call` on the Session a call was dialled about — and a second parser is
    a second thing to keep in step with the surface.

    Empty when the brief carries no such line, which is the caller's to complain
    about: what a missing `newest` means differs between a brief taken after a
    turn and one taken of a Session that has not spoken.
    """
    return next(
        (
            line.strip().removeprefix("newest: ")
            for line in brief.splitlines()
            if line.strip().startswith("newest: ")
        ),
        "",
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
    what there is to quote — `Should I continue?` is seventeen — and because a
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
    """Whether that `call:` line says no call is up — whatever else it also says.

    **The Cool-down rides this line, and it is not a call.** #195 gave the one
    rule with no surface of its own a place to be read, and put it beside
    `call: none` because that is the line an operator looks at when they are
    asking why nothing rang. A step that compared the whole line for equality
    therefore read every second of a running Cool-down as a call still up — and
    run `20260903T060110Z` is what that cost: the ceiling released the call at
    18:06:31.382 and the step that waits for it returned at 18:07:02, when the
    window it was about to test had already elapsed. The failure looked like
    subprocess cost and was a string comparison (#218).

    So the note is severed and the head is judged. A call id is never the word
    `none` and carries no bracket, so nothing else can read as down; a surface
    that answered something else entirely — `_call_line` falls back to the head
    of whatever `status` printed — reads as *not* down, which is the safe way
    round for a step whose next move is to act on a call being over.
    """
    head, _, _note = call_line.partition(" (")
    return head == CALL_DOWN_LINE


@dataclass(frozen=True)
class _VoiceWatch:
    """What one call's Voice did, watched on the watching step's own clock.

    **A long answer is not one span.** Run `20260902T163651Z` had the Voice
    count to two hundred as a single two-hundred-and-twenty-eight-second
    utterance; run `20260902T170324Z`, on the same request, broke it into
    twenty-three turns with gaps between them. So "the answer has ended" is not
    a shape the log can be asked for while the answer is still arriving, and a
    step that waited for the first moment the spans balanced would call a gap
    the end of the answer — which is what that second run did.

    What a step *can* know at each poll is whether the Voice has said anything
    since the last one. Both measurements are taken from that.
    """

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
    """Who ended the call — and `lost` and `ceiling` are answers, not shades of `agent`.

    A connection that goes away by itself also leaves `bridgectl status` saying
    `call: none`, so "the call was down when the step looked" cannot on its own
    tell an ending from a loss. Reading only that clock attributed a dropped
    call to the Call Agent, and the verdict then carried two sentences that
    contradicted each other: an end reason saying the connection went away by
    itself, beside a claim that the agent ended it. The audio path is the one
    that knows, so it is asked first.

    **`ceiling` is the fourth, and #184 is what made it reachable.** The engine's
    own Silence Ceiling closes the audio path from this side, exactly as a
    `bridgectl live` the Call Agent ran does, so the end reason cannot tell them
    apart either — run `20260902T162146Z` recorded `agent` for a call that the
    ceiling ended sixty seconds after the Voice went quiet, with the Call Agent
    having run nothing at all. The engine says so in its own log, and that line
    is the only thing that does.

    **Every answer is passed in as its own fact**, keyword by keyword, because
    the one that was inferred is the one that was wrong: `by_agent` used to be
    read off "the call was down when the step looked", which is true of a call
    the ceiling ended, a call that dropped, and a call the step ended itself. A
    caller that has no reason to believe the Call Agent ended anything says
    `by_agent=False` and means it.
    """
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

#: How long the utterance gets to reach the engine as speech. Derived from the
#: probe's own timeline: the harness waits `live_call.SETTLE_SECONDS` after the
#: peer connection comes up, the request is about four seconds of audio, and the
#: user transcript landed at 20.7s from dial on run `20260901T213252Z`. Three
#: times that, so a slow segmentation is never read as one that never happened.
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

#: How long the long variant's answer can itself take. Measured, not guessed:
#: the Voice counted to two hundred in 220s on run `20260902T162146Z` (its
#: engine log, `the call's own Voice started speaking` 04:22:24 to `… stopped
#: speaking` 04:26:04), and this is that with room for a slower count. Nothing
#: waits this long on purpose — the step waits for the call to go down, and this
#: only bounds how long it is willing to.
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

#: Preserve the former approval helper's observation cadence while #146 replaces
#: its sequential waits. This is a cadence, not a deadline; all far-side ceilings
#: still come from `FarSideDeadlines`.
APPROVAL_EFFECT_POLL_SECONDS = 2.0


# --- what the Sessions are asked to do --------------------------------------


@dataclass(frozen=True)
class Instruction:
    """One small, deterministic action, and the effect to read back.

    The shape `docs/acceptance-design.md` prescribes: an effect the harness reads
    off the filesystem, so "the Session acted on the words" is a fact from the far
    side rather than a claim from the engine.
    """

    words: str
    #: Where the effect lands: relative to the workspace, or absolute. Both are
    #: real cases — the Codex lane asks for a file *outside* the workspace,
    #: because that is what its sandbox will not let it write (`writing_at`) —
    #: and `path_in` resolves the one against the other.
    target: Path | None = None
    content: str | None = None

    def path_in(self, workspace: Path) -> Path | None:
        if self.target is None:
            return None
        return self.target if self.target.is_absolute() else workspace / self.target

    def effect_in(self, workspace: Path) -> str | None:
        target = self.path_in(workspace)
        if target is None:
            return None
        text = support.read_if_exists(target)
        return text.strip() if text is not None else None

    def performed_in(self, workspace: Path) -> bool:
        return self.content is not None and self.effect_in(workspace) == self.content


def writing(filename: str, content: str) -> Instruction:
    """The wording, in one place, so both lanes ask for the same shape of thing."""
    return Instruction(
        words=(
            f"Create a file named {filename} in the current directory whose entire "
            f"contents are the single word {content}. Do nothing else, and do not "
            f"ask any questions."
        ),
        target=Path(filename),
        content=content,
    )


def writing_at(path: Path, content: str) -> Instruction:
    """The same action, named by absolute path, so the sandbox is what decides.

    Identical to `writing` in everything the steps read back — one file, one word,
    read off the filesystem. The only difference is *where*, and on the Codex lane
    that is the whole point: the path is outside the Session's writable roots, so
    the action cannot be taken without asking.
    """
    return Instruction(
        words=(
            f"Use your `apply_patch` file-edit tool to attempt to create a file at the "
            f"absolute path {path} whose entire contents are the single word {content}. "
            "Leave any approval request pending for the user to answer. Do nothing else, "
            "and do not ask any questions."
        ),
        target=path,
        content=content,
    )


#: Turn 1 — `stable name`'s Stop. **No tool use**, on purpose: a turn that raises
#: a permission would sit in `waiting` until something answered it, and nothing is
#: supposed to answer one until `approval`. So the first turn is words only, it
#: ends on its own, and the Stop it ends with is what `stop notice` observes.
#: How many turns `brief` drives before it reads a page at all. #171's red line:
#: "more than `history_page_entries` entries driven first (at least six turns)".
#: Six two-entry turns is already more than twice a five-entry page, and driving
#: them unconditionally is what makes the walk the step's own rather than the
#: leftover of whatever the Session had said before it.
HISTORY_TURNS_FLOOR = 6

#: And how many it will drive before it calls a history that never grows past one
#: page a failure rather than a slow start.
HISTORY_TURNS_CEILING = 10

#: One printed History page entry: its ordinal, which side spoke it, and what it
#: said. An omitted entry prints its own words for the text and is read back the
#: same way — the page's promise is that every slot is there.
HISTORY_ENTRY = re.compile(r"^(?P<ordinal>\d+) (?P<role>user|assistant): (?P<text>.*)$")


@dataclass(frozen=True, slots=True)
class HistoryReading:
    """One History page as a surface got it: its entries, and whether more remain."""

    entries: tuple[tuple[int, str], ...]
    older: bool


ACKNOWLEDGE = Instruction(
    words="Reply with the single word READY. Do not use any tools, and do not ask anything."
)

#: The second turn `brief` drives, on the lane that has two turn endings to
#: tell apart (`Lane.asking`). Words only, for `ACKNOWLEDGE`'s reasons — it must
#: raise no permission — and the words it asks for are dictated rather than
#: described, because what this turn has to produce is a *final answer* the
#: promotion gate can read (#188): one interrogative, and no menu or code around
#: it that would make the reading rest on something other than the question.
ASK_A_QUESTION = Instruction(
    words=(
        "Reply with exactly this one line and nothing else: Should I continue? "
        "Do not use any tools."
    )
)

#: Turn 2 — arrives by Relay and raises a permission on the way. The file and the
#: word are one shape for both lanes; **where** it is written is the lane's, and
#: that is what `Lane.relayed` holds. One instruction for both lanes is what left
#: the codex `approval` step silent (#105): at its own sandbox a Codex writes
#: inside its workspace without asking anybody, so there was nothing to
#: round-trip. See `CLAUDE` and `CODEX` for each lane's measurement.
RELAY_FILE = "relay.txt"
RELAY_WORD = "BRAVO"

#: Where the Codex lane's relayed instruction writes: beside the workspace, under
#: the same run directory, and outside the Session's writable roots. Kept inside
#: the run directory so the design's rule still holds — nothing outside it is
#: written by the agents — and kept out of the workspace because being outside is
#: the entire reason Codex has to ask before writing there.
OUTSIDE_THE_SANDBOX = "outside-the-sandbox"

#: The words #197 sends after the relayed instruction and never expects to
#: arrive. **No effect and no target**: they are relayed into a Session that is
#: mid-turn on a permission nobody has answered yet, so the Reply Window is shut
#: for as long as the step keeps it shut — and if the product were broken and
#: they did land, an instruction that asked for nothing changes nothing the rest
#: of the walk reads. Deliberately not a second copy of `RELAY_WORD`'s file.
UNDELIVERED = Instruction(words="Ignore this line entirely. Do nothing about it.")

#: What a brief carries once a Relay to that Session has passed its ceiling: the
#: label Briefing prints and the receipt's own reason code, matched as two facts
#: rather than as one sentence — the sentence is Briefing's to reword, the code
#: is the contract (`core/briefing.py::_undelivered_wording`).
UNDELIVERED_PATTERN = rf"undelivered:.*\b{RelayReason.CEILING_PASSED}\b"

#: The line the deleted escalation path used to push at the user for a Relay that
#: finally failed (`core/relays.py::terminal_line`, removed by #197). Asserted
#: **absent**: the news travels as a brief field now, and a run that found this
#: again would have found the notice path back.
TERMINAL_REPORT_PATTERN = r"state=reported_failed"


def _receipt_fields(answer: str) -> dict[str, str]:
    """One `bridgectl` receipt, as the fields it is made of.

    `state`, `grade` and `reason` in the one format every surface prints
    (`core/relays.py::receipt_line`). Read as fields rather than searched as a
    sentence, because a substring match on `delivered` passes on a *retained*
    relay whose `state` happens to spell it — and in one place, because two
    steps read the same receipt and two parsers are two things to keep in step.
    """
    return dict(field.split("=", 1) for field in answer.split() if "=" in field)


@dataclass(frozen=True)
class _UndeliveredObservation:
    """What #197's half of the `relay` step saw, carried to the step's own line.

    `mark` is where the engine log stood when the held Relay went in, so the
    Stop Notice read afterwards is read from the same place.
    """

    mark: int
    evidence: str


#: Turn 3 — arrives from Telegram.
INBOUND = writing("inbound.txt", "CHARLIE")

#: Turn 4 — `switches`. It has to end **waiting on the user**, because #80's rule
#: is about Sessions that are still actionable when Duty comes back on. A fresh
#: file keeps this distinct from `approval`: repeating an already-performed write
#: would give the agent no reason to raise another permission.
SWITCH_FILE = "switches.txt"
SWITCH_WORD = "DELTA"

#: #128's real Claude question. The two deterministic labels make the chosen
#: value readable in the notice, the hook result, and the filesystem effect.
QUESTION_FILE = "question.txt"
CLAUDE_QUESTION = "Which marker should be written?"
CLAUDE_OPTIONS = ("ALPHA", "DELTA")
CLAUDE_ANSWER = "DELTA"
CLAUDE_ANSWER_FRAME = f"The user answered from GPT-VoiceCoding: {CLAUDE_ANSWER}"


def asking_the_claude_question(_: Path) -> Instruction:
    """Ask one deterministic question, then persist the selected label."""
    return Instruction(
        words=(
            f"Use AskUserQuestion to ask `{CLAUDE_QUESTION}` with exactly two option labels, "
            f"`{CLAUDE_OPTIONS[0]}` and `{CLAUDE_OPTIONS[1]}`. After it is answered, create "
            f"a file named {QUESTION_FILE} in the current directory whose entire contents "
            "are the selected option label. Do nothing else."
        ),
        target=Path(QUESTION_FILE),
        content=CLAUDE_ANSWER,
    )


#: Turn 5 — `child`. The main Session is asked to do the one thing that produces a
#: second agent process under it. What each lane calls that differs, so the words
#: live on the lane.
CHILD_FILE = "child.txt"

#: How often `_drive_turn` looks, and how long a record must stand still before
#: the turn it belongs to is called over. Constants rather than literals because
#: `CHILD_LIFETIME_SECONDS` is derived from them and a number derived from a
#: literal somewhere else is a number that stops being derived the day the
#: literal moves.
TURN_POLL_SECONDS = 3.0
TURN_SETTLE_SECONDS = 9.0

#: How long the `child` step asks a Child Process to keep working, and **the
#: step cannot be observed without it** (#79, measured 2026-08-27).
#:
#: A finished child is not a row: Claude's own roster has no entry for one, so
#: the product lists a child only while it is alive and every observation of a
#: child happens inside its life. That life has to reach as far as the roster
#: read, and the roster read happens when `_drive_turn` returns.
#:
#: `_drive_turn` returns when the **parent's** record has stood still for
#: `TURN_SETTLE_SECONDS`. Measured twice: a parent's transcript is frozen for
#: the whole time a foreground subagent runs — 35,642 bytes, unchanged for 52 s
#: — so the turn reads as settled while the child is still working, and the
#: roster read lands inside the child's life. That is the whole mechanism, and
#: without a floor it is a race: a subagent that only writes one small file can
#: be done in under ten seconds, the parent resumes writing, the turn genuinely
#: ends, and the roster correctly holds no child. The step then fails saying "no
#: child row appeared", which reads exactly like the product being broken.
#:
#: So the step asks for a window instead of hoping for one. The floor is the
#: settle window plus one more poll of margin, doubled: `_drive_turn` can take
#: up to `TURN_SETTLE_SECONDS + TURN_POLL_SECONDS` to notice, and the read, the
#: absence observation and the refused Relay all happen after it.
#:
#: **Precedent is #105**: the instruction is the lane's, shaped so that the
#: situation the step judges actually exists. The three assertions are
#: untouched — listed under its parent, no Stop Notice naming it, refused as a
#: Relay target — so nothing that is judged is arranged. `DELTA` is still
#: written; only *when* moved.
CHILD_LIFETIME_SECONDS = int((TURN_SETTLE_SECONDS + TURN_POLL_SECONDS) * 2)


# --- lanes ------------------------------------------------------------------


@dataclass(frozen=True)
class Lane:
    """Everything about a lane that is not the journey itself.

    The two things that genuinely differ between lanes are *where the agent's own
    record of a Session lives* and *how to find out it exists at all*, and both
    are held here as functions. They used to be two `if self.agent == "claude"`
    branches in this class, which is the shape that grows a third branch in a
    third method the first time a lane needs one — and the lanes are the one axis
    this harness is certain to keep adding to. A lane is now a value that carries
    its own answers, and `Walk` never asks which lane it is walking.
    """

    name: str
    agent: str
    binary: str
    #: What `live call`'s three extra Sessions' workspaces are called on this
    #: lane, and so what the Voice knows those Sessions by: the project half of a
    #: Session Name is the workspace directory's basename
    #: (`adapters/agent/_project.py`), and the relay utterance says the first of
    #: them out loud to pin the Session it must land in (#196).
    #:
    #: **Per lane, and that is the whole point.** The Codex daemon is
    #: machine-wide, so the Claude lane's engine holds the Codex lane's Sessions
    #: as well as its own; two lanes sharing a name is two rows the sentence
    #: cannot tell apart, and run `20260903T093813Z` is the Claude lane's Call
    #: Agent looking at two `二号工位 · Reply READY` and answering with `brief`.
    #: Said out loud by a recogniser, so both are ordinary spoken Chinese.
    call_workspaces: tuple[str, str, str]
    #: What this lane adds to the **configured** token variable name to reach its
    #: own bot. One bot, one engine (`docs/app-bundle.md` § Cutover) is what
    #: makes two lanes at once possible at all, and it is kept by giving each
    #: lane a bot of its own rather than by serialising the lanes.
    #:
    #: A suffix rather than a name, because the first name is not this harness's
    #: to choose: the engine's own config names it (`token_env`), and a lane that
    #: spelled it out would accept an engine configured for some other variable.
    #: The second bot's variable is that name with this suffix — the convention
    #: the repo-root `.env` already follows — so the pair moves together if the
    #: first is ever renamed.
    token_env_suffix: str
    #: Arguments the *person* would not normally type, and why each is here.
    arguments: tuple[str, ...]
    #: What the lane's TUI is **launched with**, or None when it is launched
    #: silent. Not one of `arguments`, because it is not a flag: it starts a turn
    #: nobody drove, before the walk has asked for anything. `hand_started.
    #: launch_arguments` puts its words last on the command line and refuses an
    #: empty one; `Walk.settle_boot_turn` waits the turn out, through the reading
    #: the value carries, before a word is typed.
    boot: hand_started.BootPrompt | None
    #: The words that make this lane's agent spawn a Child Process. "subagent"
    #: and "sub-agent" appear here on purpose: this string is spoken *to* the
    #: agent, where it is the agent's own mechanism word and the thing that makes
    #: the instruction work. Everywhere the harness speaks about the concept, it
    #: is a Child Process (`CONTEXT.md`).
    child_words: str
    #: The instruction `relay` carries and `approval` grades, given the lane's
    #: workspace. It is the lane's because *what a permission is* is the lane's:
    #: the two agents' policies refuse different actions, and an instruction that
    #: asks one of them for permission asks the other for nothing (#105).
    relayed: Callable[[Path], Instruction]
    #: The fresh authority dialog `switches` leaves pending while Duty is off:
    #: a permission on both lanes.
    actionable: Callable[[Path], Instruction]
    #: A turn whose final answer asks the user something, on the lane where that
    #: changes the state the user is told — or None where it changes nothing.
    #: #188 promotes a Codex turn end out of DECISION only when its final answer
    #: shows no sign of an ask, so the Codex lane has two turn endings to measure
    #: and `progress` measures both. The Claude lane carries none on purpose: its
    #: question is a tool call the adapter reads, so a Claude turn that merely
    #: *says* something interrogative is finished and driving one here would cost
    #: a turn to measure nothing.
    asking: Instruction | None
    #: #128's extra acceptance route. Claude carries it; Codex explicitly
    #: records the unsupported route without grading it.
    question: Callable[[Path], Instruction] | None
    question_answer: str | None
    #: The ground the permission was measured on, given the agent's own record of
    #: the Session. `approval` says its `named` half in the evidence line, so a
    #: green step states the ground it stood on rather than implying some
    #: default — and fails on the `unsound` half, because a step that cannot
    #: stand on its own ground has not proved what it claims.
    policy_at: Callable[[Path | None], hand_started.Policy]
    #: What that policy is measured to ask about. Said by the step that finds no
    #: permission at all, so a silent lane reports the measurement it contradicts
    #: instead of the other lane's.
    asks_about: str
    #: What the agent itself says about a Session the harness started, or None
    #: when it says nothing yet. Takes the pid, the workspace, the environment to
    #: read a roster with, and the moment the harness started looking.
    ground_truth: Callable[[int, Path, dict[str, str], float], hand_started.GroundTruth | None]
    #: Where that agent's own record is **at this moment**, or None when there is
    #: not one yet. Re-located on every call, never cached: measured 2026-08-26,
    #: **neither agent has a record until it has taken a turn** — a Claude Session
    #: that has not been typed into has no transcript file, and `codex` writes its
    #: rollout when the first turn starts, not when the Session does (a full run
    #: watched it sit in `starting MCP servers` with an empty workspace for 180s).
    #: Caching the `None` that resolves at Session start would make every later
    #: turn look like a turn that never grew the record, which is exactly how
    #: `_drive_turn` decides a turn is over.
    record_now: Callable[[hand_started.GroundTruth, float], Path | None]

    def token_variable(self, configured: str) -> str:
        """This lane's bot token variable, given the one the engine's config names."""
        return f"{configured}{self.token_env_suffix}"


#: `--permission-mode default` is not the person's own flag, and it is the one
#: place this harness overrides what the machine would do. It has to: measured
#: 2026-08-26, `~/.claude/settings.json` on this machine sets
#: `permissions.defaultMode = "auto"` at user scope, so a bare hand-started
#: `claude` auto-approves the Write and **no permission is ever raised**. The
#: `approval` step would then have nothing to observe, and its silence would look
#: like a pass. #60 ruled that neither lane may set a permission mode, on the
#: grounds that overriding it would *pre-approve* the thing the step exists to
#: observe; here the user's own setting is what pre-approves it, and the flag is
#: what restores the observation. The rule is kept, its direction reversed, and
#: the reason is recorded on the verdict rather than left in a diff.
CLAUDE = Lane(
    name="claude",
    agent="claude",
    call_workspaces=(
        live_call.FOCUS_WORKSPACE_NAME,
        live_call.RINGING_WORKSPACE_NAME,
        live_call.WAITING_WORKSPACE_NAME,
    ),
    binary="claude",
    # The first lane keeps the engine's own configured variable, untouched.
    token_env_suffix="",
    arguments=("--permission-mode", "default"),
    # Launched silent. No boot gate of the Codex kind has been measured here —
    # `claude` boots into an empty composer — and a Session nobody has typed into
    # is what `roster` and `stable name`'s three reads want to find.
    boot=None,
    relayed=lambda workspace: writing(RELAY_FILE, RELAY_WORD),
    actionable=lambda workspace: writing(SWITCH_FILE, SWITCH_WORD),
    asking=None,
    question=asking_the_claude_question,
    question_answer=CLAUDE_ANSWER,
    # The flag the harness passes *is* the whole policy on this lane, and Claude
    # publishes no per-turn readback of it, so there is nothing to read back and
    # nothing that can disagree. Sound by construction, and said out loud here so
    # the asymmetry with the Codex lane is a measurement rather than an oversight.
    policy_at=lambda record: hand_started.Policy("`--permission-mode default`"),
    asks_about=(
        "a Write of a new file asks `Do you want to create <name>?` and the roster's "
        "`status` goes to `waiting` (measured 2026-08-26 on claude 2.1.246)"
    ),
    ground_truth=lambda pid, workspace, environment, since: hand_started.claude_ground_truth(
        pid, environment
    ),
    record_now=lambda truth, since: hand_started.claude_transcript(truth.session_id),
    child_words=(
        "Use the Task tool to start one subagent. The subagent must first wait "
        f"{CHILD_LIFETIME_SECONDS} seconds, and only then write a file named "
        f"{CHILD_FILE} containing the single word DELTA in the current directory. "
        "Wait for it to finish and do nothing else yourself."
    ),
)

#: `--sandbox workspace-write` pins the **sandbox**, and nothing else. It is the
#: Codex config surface #105 asks this lane to name, and it is chosen because it
#: is the one thing here the product never asserts: `turn/start` pins
#: `approvalPolicy` and `approvalsReviewer` on every relayed turn
#: (`agent/codex/threads.py:36-40`), so pinning those at the keyboard too would
#: pre-arrange the very assertion #77's approval route has to make for itself.
#: What the sandbox *allows* is nobody's assertion, and until this flag it came
#: from `~/.codex/config.toml` — a file the user owns, where one
#: `sandbox_mode = "danger-full-access"` would silence this step exactly as
#: `permissions.defaultMode = "auto"` silenced the Claude one. The value is what
#: a trusted workspace already gives (measured 2026-08-26 on the failing run's
#: own rollout, `turn_context.sandbox_policy = workspace-write`), so the flag
#: fixes the ground rather than moving it.
#:
#: The **boot prompt** is not a flag and is not here to ask for anything: it is
#: what gets this lane past the update gate (#110; the measurement is in
#: `hand_started`'s module docstring). Three things follow, and each is stated
#: because a reader will otherwise meet it as a surprise:
#:
#: * **It is an extra turn, not a replacement.** `stable name` still types
#:   `ACKNOWLEDGE` itself, because it requires a name held across a Stop it
#:   drives, and `stop notice` marks the chat immediately before that turn — a
#:   Stop crossed at launch would predate the mark and the notice would be
#:   unfindable. The words are `ACKNOWLEDGE`'s so the boot turn asks the Session
#:   nothing the run does not already ask, and it uses no tools, so it cannot
#:   raise a permission before `approval` is there to answer it, and it leaves
#:   `codex_turn_policy` reading the same ground: the sandbox is this lane's pin
#:   and the `turn_context` that step grades is `relay`'s, the last one written.
#: * **The walk waits it out first** (`Walk.settle_boot_turn`). Two turns of the
#:   same words are not two turns the harness can tell apart: a boot turn still
#:   running when `stop notice`'s mark is taken puts *its* Stop Notice after the
#:   mark, and the step would pass on the notice for a turn nobody drove.
#: * **The rollout now exists before `roster` runs.** Codex writes its rollout
#:   when the first *turn* starts, so this lane's `ground_truth` carries a real
#:   `session_id` at the first read instead of the `""` it used to carry — the
#:   evidence line `roster` prints changes shape, and the pid join it rests on
#:   does not.
CODEX = Lane(
    name="codex",
    agent="codex",
    call_workspaces=("五号工位", "六号工位", "七号工位"),
    binary="codex",
    # The second bot, which already exists and messages the same user chat.
    token_env_suffix="_2",
    arguments=("--sandbox", "workspace-write"),
    boot=hand_started.BootPrompt(words=ACKNOWLEDGE.words, turn_over=hand_started.codex_turn_over),
    # Measured 2026-08-27 through the shared daemon with the product's own pin
    # and no sandbox override, on codex-cli 0.149.1 and again on 0.150.0 over a
    # 0.149.1 app-server: a write to a path outside the workspace raises
    # `item/fileChange/requestApproval`, the thread goes to `waitingOnApproval`,
    # and **the file does not appear until the approval is answered**. The same
    # instruction aimed *inside* the workspace raised nothing, both times. The
    # old sequential harness recorded only the absence after its first window;
    # #146's replacement records a correlated terminal reason instead. The
    # directory is made by the harness, so the one refused action is the write.
    relayed=lambda workspace: writing_at(
        workspace.parent / OUTSIDE_THE_SANDBOX / RELAY_FILE, RELAY_WORD
    ),
    actionable=lambda workspace: writing_at(
        workspace.parent / OUTSIDE_THE_SANDBOX / SWITCH_FILE, SWITCH_WORD
    ),
    asking=ASK_A_QUESTION,
    question=None,
    question_answer=None,
    policy_at=lambda record: hand_started.codex_turn_policy(record),
    asks_about=(
        "a write to a path outside the Session's writable roots raises "
        "`item/fileChange/requestApproval` and the thread goes to `waitingOnApproval` "
        "(measured 2026-08-27 on codex-cli 0.149.1 and 0.150.0); a write *inside* the "
        "workspace raises nothing, which is the silence #105 was opened for"
    ),
    ground_truth=lambda pid, workspace, environment, since: hand_started.codex_ground_truth(
        pid, workspace, since
    ),
    record_now=lambda truth, since: hand_started.codex_rollout(truth.workspace, since),
    child_words=(
        "Start one sub-agent. The sub-agent must first wait "
        f"{CHILD_LIFETIME_SECONDS} seconds, and only then write a file named "
        f"{CHILD_FILE} containing the single word DELTA in the current directory. "
        "Wait for it to finish and do nothing else yourself."
    ),
)


#: Both lanes, in the order they are walked. Named here so the run can declare up
#: front what it promised to observe — see `Verdict.expected_lanes`.
LANES = (CLAUDE, CODEX)

#: The system layer Codex documents below the user's `$CODEX_HOME/config.toml`.
#: The lane has no profile flag and its fresh Git workspace has no project config,
#: so these are the only configurable layers that can add writable roots beneath
#: the lane's `--sandbox workspace-write` pin.
CODEX_SYSTEM_CONFIG = Path("/etc/codex/config.toml")


def _codex_configured_writable_roots(
    environment: Mapping[str, str],
) -> tuple[list[tuple[str, Path]], list[str]]:
    """Additional workspace-write roots from the effective Codex config layers."""
    codex_home = Path(environment.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
    configured: tuple[Path, object] | None = None
    unverifiable: list[str] = []
    for config_path in (CODEX_SYSTEM_CONFIG, codex_home / "config.toml"):
        try:
            with config_path.open("rb") as config_file:
                config = tomllib.load(config_file)
        except FileNotFoundError:
            continue
        except (OSError, tomllib.TOMLDecodeError) as unreadable:
            unverifiable.append(f"Codex config {config_path} cannot be read ({unreadable})")
            continue
        workspace_write = config.get("sandbox_workspace_write")
        if workspace_write is None:
            continue
        if not isinstance(workspace_write, Mapping):
            unverifiable.append(
                f"Codex config {config_path} has a non-table `sandbox_workspace_write`"
            )
            continue
        if "writable_roots" not in workspace_write:
            continue
        configured = (config_path, workspace_write["writable_roots"])

    if configured is None:
        return [], unverifiable
    source, values = configured
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        unverifiable.append(
            f"Codex config {source} has a non-string `sandbox_workspace_write.writable_roots`"
        )
        return [], unverifiable

    writable_roots: list[tuple[str, Path]] = []
    for value in values:
        root = Path(value).expanduser()
        if not value.strip() or not root.is_absolute():
            unverifiable.append(
                f"Codex config {source} has an unverifiable writable root {value!r}; "
                "use an absolute path"
            )
            continue
        writable_roots.append((f"Codex configured writable root ({root}) from {source}", root))
    return writable_roots, unverifiable


#: The steps whose observation rests on a Codex write the sandbox **refuses**.
#: `relay` drives that write, `approval` grades the permission it raises, and
#: `switches` leaves a second one pending — every other step is silent about the
#: sandbox. Named here because the preflight that validates the ground has to know
#: when the ground matters: a run that selected none of these is a run the check
#: would refuse for a reason it is not about (#182).
CODEX_PERMISSION_STEPS = ("relay", "approval", "switches")


def codex_permission_ground_matters(lanes: Sequence[Lane], steps: Sequence[str]) -> bool:
    """Whether this run's selection rests on the Codex permission ground at all.

    Two ways it does not, and both are ordinary since `--lane`/`--step` exist: the
    Codex lane is not being walked, or it is and none of the steps that provoke a
    Codex permission were selected. Refusing either would be preflight refusing on
    a lane or a step this run never promised to observe.
    """
    return CODEX in tuple(lanes) and bool(set(steps) & set(CODEX_PERMISSION_STEPS))


def codex_permission_ground_refusal(
    run_directory: Path, *, environment: Mapping[str, str]
) -> str | None:
    """Why this run cannot provoke either Codex permission, or None when it can.

    New harness behaviour: legacy has no real-environment acceptance runner or
    permission-trigger-ground check. Its `bridge/daemon.py:1901-2052` is runtime
    Stop-detail handling, not an acceptance preflight, so there is nothing to port.
    """
    workspace = support.workspace_path(run_directory, CODEX.name)
    consumers = (
        ("approval", CODEX.relayed(workspace)),
        ("switches", CODEX.actionable(workspace)),
    )
    writable_roots = [
        (f"Session workspace ({workspace})", workspace),
        ("/tmp", Path("/tmp")),
    ]
    if temporary_directory := environment.get("TMPDIR"):
        temporary_root = Path(temporary_directory).expanduser()
        writable_roots.append((f"TMPDIR ({temporary_root})", temporary_root))
    configured_roots, unverifiable = _codex_configured_writable_roots(environment)
    writable_roots.extend(configured_roots)
    affected: list[str] = []
    for name, instruction in consumers:
        target = instruction.path_in(workspace)
        if target is None:
            unverifiable.append(f"{name} instruction has no filesystem target to validate")
            continue
        resolved_target = target.expanduser().resolve(strict=False)
        for root_name, root in writable_roots:
            if resolved_target.is_relative_to(root.expanduser().resolve(strict=False)):
                affected.append(f"{name} target {target} is under {root_name}")
                break
    if unverifiable:
        return (
            f"configured acceptance root {run_directory.parent} cannot establish that every "
            f"Codex permission target is outside writable ground for pinned `--sandbox "
            f"{hand_started.WANTED_SANDBOX}`: {'; '.join(unverifiable)}"
        )
    if not affected:
        return None
    return (
        f"configured acceptance root {run_directory.parent} puts Codex permission targets "
        f"inside writable ground for pinned `--sandbox {hand_started.WANTED_SANDBOX}`, so Codex "
        f"can write them without approval: {'; '.join(affected)}"
    )


# --- the walk ---------------------------------------------------------------


@dataclass
class Turn:
    """One turn, timed. `docs/acceptance-design.md` § Still to measure wanted this."""

    what: str
    seconds: float
    ended: bool


class Walk:
    """One lane's journey. Every method is one step; each returns its evidence."""

    def __init__(
        self,
        *,
        lane: Lane,
        session: hand_started.HandStartedSession,
        engine: support.Engine,
        config: support.DerivedConfig,
        bridgectl: support.Bridgectl,
        person,  # telegram_person.TelegramPerson
        journal: support.Journal,
        verdict: support.Verdict,
        far_side: support.FarSideDeadlines,
        environment: dict[str, str],
        started_at: float,
        selection: Selection | None = None,
    ) -> None:
        self.lane = lane
        #: Which steps this walk grades, and which it walks to reach them. The
        #: default is the full run, so a caller that has no `--step` to pass says
        #: nothing rather than repeating `STEPS` back.
        self.selection = selection if selection is not None else select(())
        self.session = session
        self.engine = engine
        self.config = config
        self.bridgectl = bridgectl
        self.person = person
        self.journal = journal
        self.far_side = far_side
        self.environment = environment
        self.started_at = started_at
        self.journey = support.Journey(
            lane=lane.name,
            verdict=verdict,
            journal=journal,
            steps=self.selection.steps,
            setup=self.selection.setup,
        )
        self.truth: hand_started.GroundTruth | None = None
        self.address: str | None = None
        #: Held for `stop notice`: the chat's high-water mark from *before* the
        #: first turn started, so the notice it looks for cannot predate the Stop.
        self.before_first_turn: int | None = None
        #: Held for `approval`: how `relay`'s shared turn resolved its effect and authority.
        self.approval_resolution: approval_effect.Resolution | None = None
        #: Held for `drain_boot_notice`, which writes the one `boot turn`
        #: observation once both halves of the arrangement are done.
        self.boot_turn: Turn | None = None
        self.turns: list[Turn] = []

    # --- the walk ---------------------------------------------------------

    def walk(self) -> None:
        self.journey.observe(
            "workspace trust",
            "arranged by the harness, not observed: both agents stop a run in a directory they "
            "have never seen with a full-screen trust dialog and the Session never registers "
            "(re-measured on claude 2.1.259, 2026-09-03). `journal.jsonl` carries the grant and "
            "the revoke, and names the state file each landed in — #217 lost a whole lane to a "
            "grant written where the Session was not reading. It is not a step: the run cannot "
            "both arrange this and judge it.",
        )
        try:
            boot_mark = self.settle_boot_turn()
            self.arm_switches()
            self.drain_boot_notice(boot_mark)
        except LaneBlocked as unarmed:
            self.journey.skip_rest(str(unarmed))
            return
        walked = self.bound_steps()
        for step in self.selection.steps:
            self.journey.run(step, walked[step])
        self.journey.observe(
            "turns measured",
            "; ".join(f"{turn.what} {turn.seconds:.1f}s ended={turn.ended}" for turn in self.turns)
            or "no turn ran",
        )

    def bound_steps(self) -> dict[str, Callable[[], str]]:
        """Every name bound to its method, and the only place they are.

        `STEPS` is the contract every build ticket's "Red first" line cites; this
        is where a name becomes code. Written as a table rather than as nine
        statements because the walk no longer runs all nine unconditionally —
        `--step` selects, and the prerequisite closure decides what comes with it
        (#182). The order is never taken from here: `Selection.steps` is in
        `STEPS` order, and that is the walk's order.
        """
        return {
            "roster": self.roster,
            "stable name": self.stable_name,
            "brief": self.brief,
            "stop notice": self.stop_notice,
            "relay": self.relay,
            "approval": self.approval,
            "companion inbound": self.companion_inbound,
            "switches": self.switches,
            "child": self.child,
            "live call": self.live_call,
        }

    def settle_boot_turn(self) -> int | None:
        """Wait out the turn the *launch* started, and mark the chat behind it.

        Not a step, for `arm_switches`' reason: it is how this lane is put in the
        state the walk assumes, not a claim about the product. But it is not
        optional either, and what it prevents is a **false green** rather than a
        red.

        A lane with a `boot` prompt is running a turn from the moment it starts
        (#110 — a non-empty prompt is what carries it past the update gate).
        Nothing may be typed into a Session that is mid-turn, and no chat mark may
        be taken while one is in flight: `stable name` drives the walk's first
        turn and hands `stop notice` the mark from just before it, so a boot turn
        that ends *after* that mark puts its own Stop Notice on the far side of it
        — and `stop notice` passes on a notice for a turn nobody drove, having
        proved nothing. The two turns carry the same words, so no reader of the
        chat could tell them apart afterwards either.

        This waits on the agent's **own** turn boundary rather than on the record
        going quiet. `_drive_turn` settles for nine seconds of silence, which for
        a graded turn costs a slow reading and here would cost the run its
        meaning, because a turn waiting on the model appends nothing either.
        Codex says which it is (`hand_started.codex_turn_over`), so this asks
        Codex.

        The mark it returns is the second half, and `drain_boot_notice` spends
        it. Nothing is typed and nothing is asked — this only watches.
        """
        boot = self.lane.boot
        if boot is None:
            return None
        started = time.monotonic()
        # Resolves the record this waits on. The ordinary first call: every step
        # reads ground truth through here, and it is cached after the first.
        truth = self._ground_truth()
        allowed = self.far_side.agent_turn_seconds * BOOT_TURN_ALLOWANCE
        over = support.wait_for(
            lambda: boot.turn_over(self._record_now()),
            deadline_seconds=allowed,
            poll_seconds=2.0,
        )
        self.boot_turn = self._measured("boot prompt", started, bool(over))
        if not self.boot_turn.ended:
            raise LaneBlocked(
                f"the turn this lane was launched with had not ended after "
                f"{self.boot_turn.seconds:.0f}s, so the walk cannot type into this Session "
                f"without racing it. The agent reports {truth.describe()}; its record is "
                f"{self._record_size()} bytes. Screen tail: {self.session.screen_tail()[-600:]!r}"
            )
        return self.person.latest_message_id()

    def drain_boot_notice(self, mark: int | None) -> None:
        """Let the boot turn's Stop Notice land before the walk marks the chat for a later one.

        **Turning an outlet on asks the next discovery pass to reconcile current
        state.** A notice with no route is dropped, not held; after
        `arm_switches`, fresh discovery raises a new notice only if the boot
        Session is still waiting on a question or permission. That happens before
        `stable name` takes its mark and needs nothing from that later step.

        What needs this is the other path. The rollout's `task_complete` and the
        engine's own observation of the Stop are not synchronised, so an engine
        that observes it after the arming escalates it straight out, and a green
        `stop notice` would then rest on that message losing a race — which is
        the one thing this harness may not do. So the walk waits here, on a mark
        taken behind the boot turn, until either the notice arrives or the window
        `stop notice` itself trusts has passed.

        **It records and never asserts**, because *no notice* is a legitimate
        answer: a Stop is only raised on a transition out of `active`, and the
        first `idle` a thread reports is it sitting there having done nothing
        (`adapters/agent/codex/adapter.py:965-985`). An engine that first saw this
        thread already idle raised nothing for the boot turn, and there is nothing
        to drain — which is also the case that pays the full window.

        **It reads the chat, so it obeys the attribution rule** (#109), and here
        that is load-bearing rather than tidy. A stranger's notice taken as the
        boot turn's would end this wait early and leave the *real* boot notice
        still in flight, to land after `stable name`'s mark — re-creating exactly
        the false green this drain exists to prevent, and writing a sentence about
        someone else's Session into the verdict on the way. This is the one chat
        read that runs before `roster`, so the naming forms may come from the
        agent's own record rather than the roster row (`_own_row`).
        """
        if mark is None or self.boot_turn is None:
            return
        try:
            arrived = self._await_own_message(
                mark, deadline_seconds=self.far_side.telegram_round_trip_seconds
            )
        except StepFailed as unattributable:
            # Arrangement, not judgement: this method has no step to fail. A lane
            # whose messages cannot be told from another Session's is a lane none
            # of the steps could read either, so it is blocked here rather
            # than walked into nine reds with one cause.
            raise LaneBlocked(
                f"the boot turn's notice could not be attributed, and neither could anything "
                f"a later step reads: {unattributable}"
            ) from unattributable
        announced = support.matching_lines(self.engine.log_lines(), ENGINE_STOP_LINE)
        boot = self.lane.boot
        assert boot is not None  # there is no mark to spend without one
        self.journey.observe(
            "boot turn",
            f"the Session was launched with {boot.words!r} — the words "
            f"`stable name` types anyway — because a non-empty prompt is what skips Codex's "
            f"update gate (#110). Waited out on Codex's own `task_started`/`task_complete` "
            f"bracketing, with every outlet still off: {self.boot_turn.seconds:.1f}s. Its Stop "
            f"Notice was then drained behind chat mark {mark} — only a message naming this "
            f"Session counting as it (#109) — so that nothing after "
            f"`stable name`'s later mark can be it: "
            + (
                f"chat message {arrived.id} ({arrived.text[:80]!r})"
                if arrived is not None
                else f"nothing arrived in "
                f"{self.far_side.telegram_round_trip_seconds:.0f}s, which is what an engine "
                f"that first saw this thread already idle would do"
            )
            + f"; engine.log Stop lines: {announced[-2:] or 'none'}. Arranged by the harness, "
            f"not judged by it.",
        )

    def arm_switches(self) -> None:
        """Voice off, Message on, Duty on — the text-only mode this whole run exercises.

        Not a step, because it is the run's mode rather than a claim about the
        product. Measured at build time on #60 and unchanged: a fresh engine
        answers `switches: duty off, message off, voice off`, so an unarmed run
        would see no push anywhere and read one cause as four failures.
        """
        for name, position in (("voice", "off"), ("message", "on"), ("duty", "on")):
            answer = self.bridgectl("switch", name, position)
            self.journal(
                "switch.armed", lane=self.lane.name, switch=name, to=position, reply=answer.text
            )
            if not answer.ok:
                raise LaneBlocked(f"`switch {name} {position}` refused: {answer.text}")

    # --- roster -----------------------------------------------------------

    def roster(self) -> str:
        """Every main Session the user starts is in the roster, and is a target like any other.

        **This step was rewritten after the fact, and the reason belongs here.**
        #73's own wording asked for "provenance and separate Relay/Approval reach
        grades, and an unattached row refused as a target" — the vocabulary
        #74's *body* still locks. #68 removed it, and Simon said so on #74 in as
        many words: *the product has no Reach / Attached / Unattached /
        Provenance vocabulary — every listed Session is one the bridge talks to,
        and a route that fails surfaces as a delivery failure with a reason
        through the existing delivery grades. Simplify the locked `Reach`,
        `ReachGrade` and `Provenance` types … before starting.* #82 says the same
        of the Codex fallback: it "adds no Reach/Provenance state and returns
        existing `FAILED` before the wire". A step asserting the old shape would
        have been a red line #74 could only clear by building types Simon had
        already deleted.

        So there is **no second class of row**. What this step claims:

        1. the Session the harness started by hand — identified from the agent's
           own record, never from the engine — has a row (blocking: nothing
           after it is observable without one);
        2. that row is a *target*: its `target` writes out as an address
           `bridgectl` accepts, and its workspace is the one the harness made,
           which is the join that makes it this Session rather than a coincidence.

        Unreachability is not this step's business and has no row of its own —
        the Codex lane with no shared daemon is exactly such a Session, and the
        proof it is still listed is that *this same step runs on that lane*.
        Where its unreachability does surface is `relay`, as a graded failure
        carrying a reason (`seams/delivery.py:40-49` — a non-delivered receipt
        cannot be built without one).
        """
        truth = self._ground_truth()
        rows = self._roster_rows()
        mine = self._row_for(rows, truth)
        if mine is None:
            # Give a polling discovery its tick before calling the roster empty.
            deadline = time.monotonic() + DISCOVERY_SECONDS
            while mine is None and time.monotonic() < deadline:
                time.sleep(5.0)
                rows = self._roster_rows()
                mine = self._row_for(rows, truth)
        if mine is None:
            raise LaneBlocked(
                f"the hand-started Session is not in the engine's roster. The agent itself "
                f"reports {truth.describe()}; the engine reports "
                f"{[support.flatten([row.get('target')]) for row in rows] or 'no sessions'}"
            )
        self.address = _address_of(mine)
        if "<no target>" in self.address or self.address.endswith(":None"):
            raise StepFailed(
                f"the roster row carries no address a surface could name it by: "
                f"target is {mine.get('target')!r}"
            )

        listed = mine.get("workspace")
        if not listed or os.path.realpath(str(listed)) != os.path.realpath(self.config.workspace):
            raise StepFailed(
                f"{self.address} is listed against workspace {listed!r}, not the one the "
                f"harness started it in ({self.config.workspace}) — the join that makes this "
                f"row this Session rather than a coincidence"
            )
        brief = self.bridgectl("brief")
        if not brief.ok:
            raise StepFailed(f"`bridgectl brief` refused: {brief.text}")
        if self.address not in brief.text:
            raise StepFailed(
                f"the Roster Brief does not carry {self.address}, so the voice side has no "
                f"row to ask about: {brief.text[:300]!r}. #187 makes `brief` the surface the "
                f"roster is read through, and every row it lists is one it can be asked about."
            )
        return (
            f"{self.address} present in the roster against its own workspace and in the "
            f"Roster Brief; agent's own record {truth.describe()}; "
            f"the engine lists {len(rows)} session(s)"
        )

    # --- stable name ------------------------------------------------------

    def stable_name(self) -> str:
        """One name, unchanged across three reads and across a Stop (#78).

        This is also the walk's **first turn**, and it is words-only by design —
        see `ACKNOWLEDGE`. The chat is marked before it starts and the mark is
        handed to `stop notice`, so what that step waits for cannot be a message
        that predates the Stop it is about.
        """
        if self.address is None:
            raise LaneBlocked("no Session in the roster to name")
        before = [self._name_now() for _ in range(NAME_READS)]
        if len(set(before)) != 1:
            raise StepFailed(f"three consecutive reads gave {before!r}, not one name")
        if before[0] is None:
            official = self.truth.name if self.truth else None
            raise StepFailed(
                f"{self.address} has no name in the roster after {NAME_READS} reads — #78 "
                f"requires the official one, and the agent's own record calls it {official!r}"
            )

        self.before_first_turn = self.person.latest_message_id()
        turn = self._drive_turn("acknowledge", ACKNOWLEDGE)
        after = self._name_now()
        if after != before[0]:
            raise StepFailed(f"the name was {before[0]!r} before the Stop and {after!r} after it")
        if not turn.ended:
            raise StepFailed(
                f"the name held at {after!r}, but the turn never ended within "
                f"{self.far_side.agent_turn_seconds:.0f}s so no Stop was crossed"
            )
        brief = self.bridgectl("brief")
        if not brief.ok:
            raise StepFailed(f"`bridgectl brief` refused: {brief.text}")
        if after not in brief.text:
            raise StepFailed(
                f"the roster holds the name {after!r} and the Roster Brief says "
                f"{brief.text[:300]!r} — the name the user hears has to be the name the "
                f"roster stabilised, or #78's stability is about a field nobody is told"
            )
        return (
            f"{after!r} across {NAME_READS} reads, across a Stop, and in the Roster Brief "
            f"({turn.seconds:.1f}s turn)"
        )

    # --- brief -------------------------------------------------------------

    def brief(self) -> str:
        """A Session is briefed, and its history pages, without costing a turn.

        Renamed from `progress` with the verb it exercised (#171). Three things
        have to hold at once: the Session Brief carries the newest message
        whole, the History page can be walked backwards through it, and neither
        read makes the Session work — checked the only way it can be from
        outside, which is that the agent's own record does not grow across them.

        **The page is walked, not sampled.** Enough turns are driven first that
        the newest page cannot hold the whole history, so `--before` has
        somewhere to go; then the cursor is taken from the page the engine
        handed over, never invented here. Past the oldest entry the page is
        empty and `older` says `false`, which is an answer and not a refusal —
        the distinction the whole verb is drawn around.
        """
        if self.address is None:
            raise LaneBlocked("no Session to brief")

        brief = self.bridgectl("brief", self.address)
        if not brief.ok:
            raise StepFailed(f"`bridgectl brief {self.address}` refused: {brief.text}")
        newest = next(
            (
                line.strip().removeprefix("newest: ")
                for line in brief.text.splitlines()
                if line.strip().startswith("newest: ")
            ),
            None,
        )
        if not newest:
            raise StepFailed(
                f"`bridgectl brief {self.address}` carried no newest message after a turn: "
                f"{brief.text[:300]!r}. #187 makes the Session Brief the one place the "
                f"newest message is handed over, whole."
            )
        # #188: what the turn end is *called*. This one is `stable name`'s turn,
        # which asked nothing (`ACKNOWLEDGE`), so both lanes owe FINISHED here —
        # Claude because its question is structural and there was none, Codex
        # because the promotion gate found no sign of an ask in its final answer.
        finished = STATE_WORDING[BriefState.FINISHED]
        called = self._briefed_state(brief.text)
        if called != finished:
            raise StepFailed(
                f"`bridgectl brief {self.address}` calls the Session {called!r} after a turn "
                f"that asked nothing, where #188 requires {finished!r}: {brief.text[:300]!r}"
            )

        driven = self._drive_until_history_pages()
        before_size = self._record_size()
        before_state = self._roster_field("state")

        newest_page = self._history_page()
        if not newest_page.entries:
            raise StepFailed(
                f"`bridgectl history {self.address}` carried no entries after "
                f"{driven} turns: the newest page is the one that includes the newest "
                f"entry, and #171 makes every page complete on its own"
            )
        opening = " ".join(newest.split())[:60]
        if not any(opening in " ".join(text.split()) for _, text in newest_page.entries):
            raise StepFailed(
                f"the Session Brief's newest message {newest[:120]!r} is not on the newest "
                f"History page ({newest_page.entries!r}) — #171 amends ADR 0016 so that the "
                f"newest page *includes* `newest`, and two readings of one Session that "
                f"disagree is what one canonical observation exists to prevent"
            )
        if not newest_page.older:
            raise StepFailed(
                f"`bridgectl history {self.address}` says nothing is older after {driven} "
                f"turns, so there is no second page to prove the cursor with: "
                f"{newest_page.entries!r}"
            )

        cursor = min(ordinal for ordinal, _ in newest_page.entries)
        older_page = self._history_page(before=cursor)
        if not older_page.entries:
            raise StepFailed(
                f"`bridgectl history {self.address} --before {cursor}` came back empty while "
                f"the page before it said older entries remained"
            )
        above = [ordinal for ordinal, _ in older_page.entries if ordinal >= cursor]
        if above:
            raise StepFailed(
                f"`--before {cursor}` answered with ordinals {above!r}, which are not before "
                f"it — the cursor is exclusive (#171)"
            )

        oldest = min(ordinal for ordinal, _ in older_page.entries)
        while older_page.older:
            older_page = self._history_page(before=oldest)
            if not older_page.entries:
                raise StepFailed(
                    f"`--before {oldest}` came back empty while the page before it said "
                    f"older entries remained"
                )
            oldest = min(ordinal for ordinal, _ in older_page.entries)
        past_the_oldest = self._history_page(before=oldest)
        if past_the_oldest.entries or past_the_oldest.older:
            raise StepFailed(
                f"`--before {oldest}` is past the oldest entry and must be an empty page "
                f"with no promise of more, not {past_the_oldest!r}"
            )

        time.sleep(2.0)
        after_size = self._record_size()
        after_state = self._roster_field("state")
        if after_size != before_size:
            raise StepFailed(
                f"reading history grew the Session's own record from {before_size} to "
                f"{after_size} bytes — that is a turn, and #171 forbids one"
            )
        asked = self._brief_a_turn_that_ends_on_a_question()
        return (
            f"the Session Brief carried the newest message whole ({newest[:120]!r}) and "
            f"{driven} turns of history paged back to ordinal {oldest} and then to an empty "
            f"page; record steady at {after_size} bytes; state {before_state!r} → "
            f"{after_state!r}; turn end called {called!r}, and {asked}"
        )

    def _drive_until_history_pages(self) -> int:
        """Drive turns until the history is longer than one page can hold.

        **A floor and a ceiling, and the floor is the ticket's.** #171's red line
        asks for more than `history_page_entries` entries driven *first*, at
        least six turns of them — so six are driven whatever the Session already
        said, and a lane that arrived with a long history is not allowed to skip
        the walk the step is supposed to make. Past the floor the engine's own
        `older` is what says the page has somewhere to go, because the page size
        is the engine's dial and this harness deliberately does not read it.

        Turns are words-only for `ACKNOWLEDGE`'s reason — one that raised a
        permission would sit in `waiting` until `approval` answered it.
        """
        driven = 0
        while driven < HISTORY_TURNS_FLOOR or not self._history_page().older:
            if driven >= HISTORY_TURNS_CEILING:
                raise StepFailed(
                    f"{driven} turns did not produce more than one History page: either "
                    f"nothing is being recorded or the page is unbounded"
                )
            self._drive_turn("fill the history", ACKNOWLEDGE)
            driven += 1
        return driven

    def _history_page(
        self, *, before: int | None = None, address: str | None = None
    ) -> HistoryReading:
        """One page as `bridgectl history` printed it, read back off its own lines.

        Parsed from the printed page rather than from a second request shape,
        because what this run accepts is the surface a user gets.

        **The address is a parameter, defaulting to the walk's own** (#198). The
        `brief` step reads the Session `roster` took ground truth for; the folded
        `live call` reads the extra Session a call was dialled about, which the
        harness started and holds no truth for. One reader for both, because two
        parsers of one printed page are two things to keep in step.
        """
        wanted = address or self.address
        assert wanted is not None
        cursor = ["--before", str(before)] if before is not None else []
        answer = self.bridgectl("history", wanted, *cursor)
        if not answer.ok:
            raise StepFailed(
                f"`bridgectl history {wanted} {' '.join(cursor)}`".rstrip()
                + f" refused: {answer.text}"
            )
        if "read at " not in answer.text:
            raise StepFailed(
                f"`bridgectl history {wanted}` carried no observation time: {answer.text[:200]!r}"
            )
        return _history_reading(answer.text)

    def _history_read_yet(self, address: str) -> HistoryReading | None:
        """That Session's newest History page, or `None` if there is not one yet (#198).

        **A refusal here is an answer, not a failure.** A Session that has just
        entered the roster has a record the engine has not read — `history`
        refuses with *nothing has read what … said: this engine reads a Session's
        own record and never infers one* — and run `20260903T220718Z` is the
        folded walk treating that as the step failing, on the first Session it
        started. What the caller wants to know is whether there is a page to page
        back through, and "not yet" is one of the answers to that.

        Only for the Sessions the walk starts itself. Every read of the walk's
        *own* Session goes through `_history_page`, where a refusal is a defect:
        that Session has been driven through `roster` before anything asks.
        """
        answer = self.bridgectl("history", address)
        return _history_reading(answer.text) if answer.ok else None

    def _brief_a_turn_that_ends_on_a_question(self) -> str:
        """Drive a turn that ends on a question, and read what the brief calls it.

        The other half of #188's rule, measured live rather than in the fast
        suite because what is under test is the whole route: codex marks its own
        final answer `final_answer`, the tail reader carries that phase through,
        and Briefing reads the words behind it. Nothing short of a real turn
        against a real daemon produces the phase, and the fast suite hands it to
        Briefing already made.

        A lane with one turn ending drives nothing and says so — see
        `Lane.asking` for why the Claude lane is that lane.
        """
        if self.lane.asking is None or self.address is None:
            return "this lane has one turn ending, so there is nothing else to call"
        turn = self._drive_turn("ask a question", self.lane.asking)
        if not turn.ended:
            raise StepFailed(
                f"the turn that had to end on a question never ended within "
                f"{self.far_side.agent_turn_seconds:.0f}s, so there is no turn end to brief"
            )
        brief = self.bridgectl("brief", self.address)
        if not brief.ok:
            raise StepFailed(f"`bridgectl brief {self.address}` refused: {brief.text}")
        decision = STATE_WORDING[BriefState.DECISION]
        called = self._briefed_state(brief.text)
        if called != decision:
            raise StepFailed(
                f"the Session ended a turn on a question and `bridgectl brief "
                f"{self.address}` calls it {called!r}, where #188 requires {decision!r}. "
                f"The brief reads {brief.text[:300]!r}"
            )
        return f"a turn ending on a question is called {called!r} ({turn.seconds:.1f}s turn)"

    @staticmethod
    def _briefed_state(brief: str) -> str:
        """The state word off a Session Brief, read where Briefing writes it.

        `<name> — <address> — <state>` is the header line
        (`core/briefing.py::_headline`), so the state is what follows the last
        separator of the first line. Taken from that one position rather than
        searched for anywhere in the text, because every state word also appears
        inside the message a brief carries, and a step that went looking would
        pass on the Session quoting itself.
        """
        lines = brief.splitlines()
        return lines[0].rsplit(" — ", 1)[-1].strip() if lines else ""

    # --- stop notice ------------------------------------------------------

    def stop_notice(self) -> str:
        """The Stop `stable name` crossed reached the chat, and it says what it stopped on.

        #75's shape: the notice carries the question or the permission, not a
        flattened sentence. Since #189 it is a **Session Brief published as
        text** — `Briefing.text` and nothing wrapped around it — so what can be
        checked from the chat is that a message arrived for that Stop, that it
        has the brief's shape (a state word in the header, a `newest` line), and
        that the roster corroborates what it says the Session stopped on, which
        is #75's own exit and is read off the payload.

        **The message has to name this Session**, which is the module's
        attribution rule and not a nicety of this step: until #109 this took the
        next bot message after its mark, and on run `20260826T213402Z` that was a
        stranger's permission prompt.
        """
        if self.before_first_turn is None:
            raise LaneBlocked("no turn was driven, so no Stop was crossed to be announced")
        message = self._await_own_message(
            self.before_first_turn, deadline_seconds=self.far_side.telegram_round_trip_seconds
        )
        stop_lines = support.matching_lines(self.engine.log_lines(), r"(?i)stop|SessionStopped")
        if message is None:
            raise StepFailed(
                f"no message naming {self.address} reached the chat within "
                f"{self.far_side.telegram_round_trip_seconds:.0f}s of the turn ending. The bot "
                f"said {self._other_traffic(self.before_first_turn)} in that window, none of it "
                f"about this Session; engine.log stop lines: {stop_lines[-3:] or 'none'}"
            )
        if not message.text.strip():
            raise StepFailed(f"the Stop reached the chat as an empty message ({message.id})")
        # #189: the Stop Notice **is** a Session Brief published as text
        # (`CONTEXT.md`), so what arrived has the brief's own shape — the header
        # line's state word, and the `newest` line, which is what a Bridge Core
        # sentence would not have. Read where Briefing writes them, never
        # searched for loose in the message: every state word also occurs inside
        # the message a brief carries.
        called = self._briefed_state(message.text)
        if called not in set(STATE_WORDING.values()):
            raise StepFailed(
                f"the Stop reached the chat as {message.text[:200]!r}, whose header line ends "
                f"{called!r} — not one of Briefing's state words {sorted(STATE_WORDING.values())}. "
                f"Since #189 the Stop Notice is `Briefing.text`, not a sentence Bridge Core "
                f"composes"
            )
        if not any(line.startswith("  newest: ") for line in message.text.splitlines()):
            raise StepFailed(
                f"the Stop reached the chat calling the Session {called!r} but carries no "
                f"`newest` line, which every Session Brief has: {message.text[:200]!r}"
            )
        waiting = self._roster_field("waiting_for")
        kind = waiting.get("kind") if isinstance(waiting, dict) else None
        if not kind:
            raise StepFailed(
                f"a message arrived for the Stop ({message.id}: {message.text!r}) but the roster "
                f"does not say what the Session stopped on: waiting_for is {waiting!r}. #75 "
                f"replaces `SessionStopped.detail` free text with the typed `WaitingFor`, and a "
                f"notice the roster cannot corroborate is a sentence, not a state."
            )
        if not stop_lines:
            raise StepFailed(
                f"message {message.id} reached the chat and the roster says {kind!r}, but "
                f"engine.log carries no Stop line — the run cannot attribute the message to "
                f"this engine's own Stop"
            )
        return (
            f"bot message {message.id}, naming this Session and briefing it as {called!r}: "
            f"{message.text!r}; roster waiting_for kind {kind!r}; engine.log: {stop_lines[-1]!r}"
        )

    # --- relay ------------------------------------------------------------

    def relay(self) -> str:
        """Words go in through `bridgectl relay`, come out as a receipt and an effect.

        **DELIVERED is never inferred from a write** (#71, carried into #77), so
        this step wants two things the engine cannot fake: a receipt whose
        **grade** is `delivered` rather than retained or unproven, and the file
        the words asked for. The permission this turn raises is answered here — through
        `bridgectl approve`, so the *bridge* answers it — and the evidence is
        handed to `approval`, which is the step that grades it.

        **The words are the lane's** (`Lane.relayed`). Both lanes ask for one
        file containing one word; only the path differs, because only the path
        decides whether the agent has to ask permission first (#105).
        """
        if self.address is None:
            raise LaneBlocked("no Session to relay to")
        relayed = self.lane.relayed(self.config.workspace)
        # The directory the effect lands in is the harness's to make. On the
        # Codex lane it is outside the sandbox, and an agent that had to create
        # it as well would be asking permission twice for one instruction — two
        # permissions where the step grades one.
        target = relayed.path_in(self.config.workspace)
        if target is not None:
            target.parent.mkdir(parents=True, exist_ok=True)
        mark = self.person.latest_message_id()
        started = time.monotonic()
        answer = self.bridgectl(
            "relay", self.address, relayed.words, timeout=support.RELAY_DEADLINE_SECONDS
        )
        if not answer.ok:
            raise StepFailed(f"relay refused: {answer.text}")

        # #197's half of this turn, and it goes **before** the approval is
        # answered: the Session is mid-turn on a permission nobody has resolved
        # yet, so its Reply Window is shut and stays shut for as long as this
        # step leaves it shut. That is what makes the hold a fact rather than a
        # race against how long an agent takes.
        undelivered = self._a_relay_that_outlives_its_ceiling()

        self.approval_resolution = self._resolve_approval_effect(
            scenario="relay",
            requirement=approval_effect.ApprovalRequirement.REQUIRED,
            instruction=relayed,
            mark=mark,
            effect_seconds=self.far_side.workspace_effect_seconds,
        )
        resolved = self.approval_resolution.succeeded
        self.turns.append(Turn("relay", time.monotonic() - started, resolved))
        # The receipt is a grade and a reason code, and the step reads the
        # fields rather than looking for a word anywhere in a sentence: `state`
        # and `grade` both spell `delivered`, so a substring match passed on a
        # retained relay whose *state* happened to say so.
        receipt = _receipt_fields(answer.text)
        if receipt.get("grade") != "delivered":
            # #68's rule, and the one place it is observable: a route that cannot
            # be taken surfaces **as a graded delivery failure carrying a reason
            # code**, never as silence and never as a bare refusal.
            reason = receipt.get("reason")
            raise StepFailed(
                f"relay answered {answer.text!r}, whose grade is "
                f"{receipt.get('grade', '<no grade field>')!r} and not `delivered`"
                + (
                    f"; reason code: {reason!r}"
                    if reason
                    else "; AND no reason code travelled with it — #68 requires a delivery "
                    "failure to carry one"
                )
                + f". {target} is {relayed.effect_in(self.config.workspace)!r}"
            )
        if not resolved:
            raise StepFailed(
                f"relay answered {answer.text!r}, but its required approval/effect resolution "
                f"failed: {self.approval_resolution.failure}; effect observed="
                f"{self.approval_resolution.effect_observed}; {target} contains "
                f"{relayed.effect_in(self.config.workspace)!r}"
            )
        stop_notice = self._stop_notice_carrying_the_undelivered_relay(since=undelivered.mark)
        if not stop_notice:
            raise StepFailed(
                "the Relay that passed its ceiling reached `brief`, and the Stop Notice this "
                "turn published afterwards did not carry it — the two readings of one row "
                f"disagree (#197). Engine log tail: {self._log_since(undelivered.mark)[-8:]}"
            )
        return (
            f"{answer.text}; {target} contains {relayed.content}; "
            f"{undelivered.evidence}; the Stop Notice carried it too"
        )

    # --- a relay that finally failed (#197) --------------------------------

    def _a_relay_that_outlives_its_ceiling(self) -> _UndeliveredObservation:
        """Hold one Relay past the ceiling, and read the reason off the Session.

        #197's rule from the user's side: a Relay that finally fails reaches
        them **through Briefing**, as a field on that Session's brief, and not as
        a line pushed beside it. Three things are graded here and each is a fact
        a broken product would get wrong differently:

        * the words were **held** — the receipt says `retained`, so what is
          measured afterwards is a ceiling and not a delivery that failed;
        * one ceiling later `brief <address>` carries `undelivered` with the
          receipt's own reason code, on a Session that is still alive;
        * **no free-text report went out.** The three announce sites #197
          deleted pushed `terminal_line` at the Companion Channel, so a run that
          found that line again would have found the notice path back.

        The wait is the engine's own dial, read out of this run's config the way
        every other duration here is (`_relay_ceiling_seconds`); one poll of
        margin is added because the sweep runs on the engine's one-second tick.
        """
        if self.address is None:  # pragma: no cover - the caller checked
            raise LaneBlocked("no Session to hold a relay for")
        ceiling = self._relay_ceiling_seconds()
        mark = len(self.engine.log_lines())
        # The same deadline the turn-driving relay gets, and for the same
        # reason: `bridgectl` hands every action ten seconds and the engine's
        # own proof of delivery on the Claude lane waits forty-five, so the
        # surface cannot reach the reply on the shipped budget
        # (`support.RELAY_DEADLINE_SECONDS`). Measured again on run
        # `20260903T105429Z`, where this exact call timed out at 10s.
        held = self.bridgectl(
            "relay", self.address, UNDELIVERED.words, timeout=support.RELAY_DEADLINE_SECONDS
        )
        if not held.ok:
            raise StepFailed(f"the relay that should have been held was refused: {held.text}")
        receipt = _receipt_fields(held.text)
        if receipt.get("state") != str(Lifecycle.RETAINED):
            raise StepFailed(
                f"the words meant to wait out the ceiling were answered {held.text!r}: their "
                f"state is {receipt.get('state', '<no state field>')!r} and not "
                f"`{Lifecycle.RETAINED}`, so this turn's Reply Window was open and there is "
                "no held Relay to grade"
            )
        time.sleep(ceiling + TURN_POLL_SECONDS)
        briefed = support.wait_for(
            lambda: bool(re.search(UNDELIVERED_PATTERN, self._brief_text())),
            deadline_seconds=TURN_SETTLE_SECONDS,
            poll_seconds=TURN_POLL_SECONDS,
        )
        text = self._brief_text()
        if not briefed:
            raise StepFailed(
                f"a Relay to {self.address} passed its {ceiling:.0f}s ceiling and "
                f"`bridgectl brief` says nothing about it: {text!r}"
            )
        pushed = support.matching_lines(self._log_since(mark), TERMINAL_REPORT_PATTERN)
        if pushed:
            raise StepFailed(
                "a Relay that finally failed was reported as free text as well as on the "
                f"row — the notice path #197 deleted is back: {[line.strip() for line in pushed]}"
            )
        line = next(
            (one.strip() for one in text.splitlines() if re.search(UNDELIVERED_PATTERN, one)),
            "",
        )
        return _UndeliveredObservation(
            mark=mark, evidence=f"a Relay held past {ceiling:.0f}s reads back as {line!r}"
        )

    def _brief_text(self) -> str:
        """What `bridgectl brief <address>` says about this walk's Session, whole."""
        if self.address is None:  # pragma: no cover - the caller checked
            return ""
        answer = self.bridgectl("brief", self.address)
        return answer.text if answer.ok else ""

    def _stop_notice_carrying_the_undelivered_relay(self, *, since: int) -> bool:
        """Whether a Stop published since `since` carried the undelivered field.

        The second half of #197's `relay` observation: the field is on the row,
        so **every** reading of that row carries it — the one `brief` takes and
        the one the Stop Notice is rendered from, which is the same `briefing`
        call and must not be able to disagree with it.
        """

        def carried() -> bool:
            lines = self._log_since(since)
            if not support.matching_lines(lines, ENGINE_STOP_LINE):
                return False
            return bool(support.matching_lines(lines, UNDELIVERED_PATTERN))

        return bool(
            support.wait_for(
                carried,
                deadline_seconds=TURN_SETTLE_SECONDS + TURN_POLL_SECONDS,
                poll_seconds=TURN_POLL_SECONDS,
            )
        )

    def _relay_ceiling_seconds(self) -> float:
        """The Relay ceiling this lane's engine is actually running.

        Read out of the lane's own config and only then off the shipped default,
        exactly as the Cool-down and the Silence Ceiling are. The run's config
        carries the harness's own number (`support.derive_config`), and reading
        it back rather than importing it is what keeps the step honest if that
        deviation is ever withdrawn.
        """
        document = tomllib.loads(self.config.path.read_text())
        given = document.get("policy", {}).get("relay_ceiling_seconds")
        return DEFAULT_RELAY_CEILING_SECONDS if given is None else float(given)

    # --- approval ---------------------------------------------------------

    def approval(self) -> str:
        """A permission raised inside a Session round-trips through the bridge.

        Graded here, observed during `relay` — the same turn, from the other end.
        Splitting them into two turns would prove less: the shape the product has
        to survive is a *relayed* instruction that needs a permission, and that is
        one turn by definition.

        The evidence names the policy the permission was measured at, read from
        the agent's own record where the agent records one. A green step that did
        not say which ground it stood on is a step that would read the same on
        ground where the permission could not have been raised at all — which is
        the run #105 was opened on.
        """
        policy = self.lane.policy_at(self._record_now())
        resolution = self.approval_resolution
        if (
            resolution is None
            or not resolution.succeeded
            or resolution.terminal_reason is not approval_effect.TerminalReason.APPROVAL
            or resolution.authority_evidence is None
        ):
            # Each lane reports the measurement its own silence contradicts. One
            # shared sentence here is how the codex step spent a run explaining
            # what a Claude at `--permission-mode default` would have done
            # (#105): true, and about the other lane.
            raise StepFailed(
                "the relayed instruction did not complete the required authority round trip: "
                f"{resolution.failure if resolution is not None else 'relay did not resolve'}. "
                f"Measured at {policy.named}: {self.lane.asks_about}"
            )
        if policy.unsound:
            # A round trip that happened is not the whole claim. #105 asks this
            # step to *name* the policy it was measured at, and a green line
            # reading `no policy` — or naming ground on which no permission could
            # have been raised — is the same silent pass in a new costume.
            raise StepFailed(
                f"a permission did round-trip ({resolution.authority_evidence}), but the ground "
                f"it was measured on is not the ground this lane stands on: {policy.unsound}"
            )
        return f"{resolution.authority_evidence}; measured at {policy.named}"

    # --- companion inbound ------------------------------------------------

    def companion_inbound(self) -> str:
        """A typed `@<name>: words` becomes a delivered relay, with the line #48 requires."""
        name = self._name_now()
        if not name:
            raise StepFailed("no Session name to address an inbound message to")
        mark = self.person.latest_message_id()
        sent = self.person.send(f"@{name}: {INBOUND.words}")
        resolution = self._resolve_approval_effect(
            scenario="companion inbound",
            requirement=approval_effect.ApprovalRequirement.OPTIONAL,
            instruction=INBOUND,
            mark=mark,
            effect_seconds=self.far_side.workspace_effect_seconds,
        )
        inbound_lines = support.matching_lines(self.engine.log_lines(), r"(?i)inbound")
        if not resolution.succeeded:
            raise StepFailed(
                f"message {sent.id} addressed to @{name} did not satisfy its optional "
                f"approval/effect resolution: {resolution.failure}; effect observed="
                f"{resolution.effect_observed}; engine.log inbound lines: "
                f"{inbound_lines[-3:] or 'none'}"
            )
        if not inbound_lines:
            raise StepFailed(
                f"{INBOUND.target} was written, so the words arrived, but engine.log carries "
                f"no inbound line — #48's requirement"
            )
        return (
            f"message {sent.id} → @{name} → {INBOUND.target} contains {INBOUND.content}; "
            f"resolved by {resolution.terminal_reason.value} in "
            f"{resolution.elapsed_seconds:.3f}s; engine.log: {inbound_lines[-1]!r}"
        )

    # --- switches ---------------------------------------------------------

    def switches(self) -> str:
        """Duty off pushes nothing; Duty on reports only what is still actionable (#80).

        The Auto Hang-up Switch is exercised here too (#185), as a wire claim
        only: flipped off and on through `bridgectl switch`, each position read
        back from `status`. The behaviour it governs — a silent call outliving
        the ceiling — needs a call, and this run is text-only.

        The turn here ends **waiting on the user** rather than done, because that
        is what makes a Session still actionable when Duty comes back on. Two
        observations: silence over a derived window with Duty off, and — with
        Duty back on — a notice naming this Session.

        Both lanes use a real permission here. It is answerable through the
        Approval Relay, so the Session is idle again before #128's Claude-only
        question proof and the fixed next step (`child`).

        The interval before release is derived from `agent_turn_seconds` +
        `absence_window_seconds` + `DISCOVERY_SECONDS` +
        `telegram_round_trip_seconds`. Nothing on the engine side expires the
        dialog while that runs — the wire alone bounds a held hook (#191, ADR
        0015) — so the sum has only Claude Code's own hook timeout above it.
        #146's required resolution grades the Stop Notice, Approval Relay answer
        and file effect together before the Session proceeds to the question
        proof and `child`.

        Both observations are about *this* Session, under the module's
        attribution rule. The silence one is where that matters most and reads
        least obviously: Duty is a global switch, so a stranger's notice arriving
        with Duty off would be a real product bug — but it would be a bug about
        somebody else's Session, and failing this lane on it is the mirror image
        of the #109 pass.
        """
        if self.address is None:
            raise LaneBlocked("no Session to watch the switches over")

        def position(name: str) -> str | None:
            """One switch's position, as `status` renders it for a person."""
            reading = self.bridgectl("status")
            if not reading.ok:
                raise StepFailed(f"reading back {name}, `status` refused: {reading.text}")
            for line in reading.text.splitlines():
                if not line.startswith("switches:"):
                    continue
                for entry in line.removeprefix("switches:").split(","):
                    switch, _, state = entry.strip().partition(" ")
                    if switch == name:
                        return state
            return None

        # The Auto Hang-up Switch (#185), flipped where the other switches are.
        # What it governs is the Silence Ceiling, and this run is text-only — no
        # call is opened here to be ended. What the step claims is the wire: the
        # fourth switch is settable from `bridgectl` and readable in `status`,
        # on the same surface and under the same name as the other three.
        auto_off = self.bridgectl("switch", "auto_hangup", "off")
        if not auto_off.ok:
            raise StepFailed(f"`switch auto_hangup off` refused: {auto_off.text}")
        read_off = position("auto_hangup")
        if read_off != "off":
            self.bridgectl("switch", "auto_hangup", "on")
            raise StepFailed(
                f"`switch auto_hangup off` was accepted, but `status` reads {read_off!r}"
            )
        auto_on = self.bridgectl("switch", "auto_hangup", "on")
        if not auto_on.ok:
            raise StepFailed(f"`switch auto_hangup on` refused: {auto_on.text}")
        read_on = position("auto_hangup")
        if read_on != "on":
            raise StepFailed(
                f"`switch auto_hangup on` was accepted, but `status` reads {read_on!r}"
            )

        off = self.bridgectl("switch", "duty", "off")
        if not off.ok:
            raise StepFailed(f"`switch duty off` refused: {off.text}")

        actionable = self.lane.actionable(self.config.workspace)
        target = actionable.path_in(self.config.workspace)
        if target is None:
            raise StepFailed("the switches permission names no filesystem effect")
        target.parent.mkdir(parents=True, exist_ok=True)
        mark = self.person.latest_message_id()
        turn = self._drive_turn("wait for permission", actionable, expect_waiting=True)
        waiting = self._roster_field("waiting_for")
        if not isinstance(waiting, dict) or waiting.get("kind") != "permission":
            self.bridgectl("switch", "duty", "on")
            raise StepFailed(
                f"the switches turn did not stop on a permission (turn ended={turn.ended}, "
                f"roster waiting_for={waiting!r})"
            )
        status = self.bridgectl("status")
        if not status.ok:
            raise StepFailed(f"with Duty off, `status` refused: {status.text}")
        intruder = self._await_own_message(
            mark, deadline_seconds=self.far_side.absence_window_seconds
        )
        if intruder is not None:
            self.bridgectl("switch", "duty", "on")
            raise StepFailed(
                f"with Duty off a message about this Session still reached the chat: "
                f"{intruder.id} {intruder.text!r}"
            )

        mark = self.person.latest_message_id()
        back_on = self.bridgectl("switch", "duty", "on")
        if not back_on.ok:
            raise StepFailed(f"`switch duty on` refused: {back_on.text}")
        resolution = self._resolve_approval_effect(
            scenario="switches",
            requirement=approval_effect.ApprovalRequirement.REQUIRED,
            instruction=actionable,
            mark=mark,
            announcement_seconds=DISCOVERY_SECONDS + self.far_side.telegram_round_trip_seconds,
            effect_seconds=self.far_side.workspace_effect_seconds,
        )
        if not resolution.succeeded:
            raise StepFailed(
                f"Duty off held silence for {self.far_side.absence_window_seconds:.0f}s "
                f"(correct), but required resolution after Duty returned on failed: "
                f"{resolution.failure}. Other traffic: {self._other_traffic(mark)}"
            )
        if resolution.approval_id is None or resolution.authority_evidence is None:
            raise StepFailed(f"required switches resolution returned no authority: {resolution}")
        approval_id = resolution.approval_id
        if not support.wait_for(
            lambda: self._roster_field("state") == "idle",
            deadline_seconds=self.far_side.agent_turn_seconds,
        ):
            raise StepFailed(
                f"permission {approval_id} was approved, but the Session did not return to "
                "idle before the question proof"
            )
        forms = _naming_forms(self._own_row())
        announcements = [
            message
            for message in self.person.messages_after(mark)
            if message.from_bot
            and _named_in(message.text, forms)
            and APPROVAL_ANNOUNCEMENT.search(message.text)
        ]
        if len(announcements) != 1:
            raise StepFailed(
                f"Duty on produced {len(announcements)} permission Stop Notices for "
                f"{approval_id}, not exactly one: {[message.text for message in announcements]!r}"
            )
        question_evidence = self._accept_question()
        return (
            "auto_hangup off then on, each position read back from `status`; "
            f"Duty off: nothing pushed in {self.far_side.absence_window_seconds:.0f}s, `status` "
            f"still answered ({status.text.splitlines()[0]!r}); Duty on: "
            f"{resolution.authority_evidence}; effect resolved in "
            f"{resolution.elapsed_seconds:.3f}s; Session returned idle; {question_evidence}"
        )

    def _accept_question(self) -> str:
        """Exercise #128's Claude route; Codex is recorded, not graded.

        Claude Code 2.1.248 was measured on this machine on 2026-08-28: its
        `AskUserQuestion` dialog stays visible and interactive while the
        `PermissionRequest` hook is held, but that request carries no usable
        `prompt_id` while an ordinary permission request does. The listener
        therefore uses an engine-private correlator without projecting it as the
        question's `approval_id`. The product is deliberately not version pinned.
        This proof reads the chat, public control plane, transcript, and filesystem;
        it never scrapes that terminal dialog.
        """
        if self.lane.question is None:
            evidence = "Codex projects no question dialog; recorded, not graded (#128)"
            self.journey.observe("question", evidence)
            return evidence
        if self.address is None or self.lane.question_answer is None:
            raise LaneBlocked("the Claude question route has no Session address or answer")

        question = self.lane.question(self.config.workspace)
        target = question.path_in(self.config.workspace)
        if target is None:
            raise StepFailed("the question continuation names no filesystem effect")
        target.parent.mkdir(parents=True, exist_ok=True)
        mark = self.person.latest_message_id()
        turn = self._drive_turn("question", question, expect_waiting=True)

        waiting = self._roster_field("waiting_for")
        options = (
            tuple(
                option.get("text")
                for option in waiting.get("options", [])
                if isinstance(option, dict)
            )
            if isinstance(waiting, dict)
            else ()
        )
        if (
            not isinstance(waiting, dict)
            or waiting.get("kind") != "question"
            or waiting.get("prompt") != CLAUDE_QUESTION
            or options != CLAUDE_OPTIONS
            or self._roster_field("reply_window") != "open"
        ):
            raise StepFailed(
                "the Claude turn did not expose the answerable question exactly: "
                f"turn ended={turn.ended}, waiting_for={waiting!r}, options={options!r}, "
                f"reply_window={self._roster_field('reply_window')!r}"
            )

        def is_question_notice(seen) -> bool:
            """A brief about *this question*, matched on the lines only it has.

            The brief's own words for what a Session is waiting on and where the
            user can answer it (`core/briefing.py::_decision_lines`,
            `_session_lines`). It read "reply with your answer" until #189 made
            the Stop Notice a Session Brief, which is what this harness must
            mirror rather than remember.

            **Matched on the decision lines, not on the words anywhere in the
            text.** A brief also quotes the Session's newest message, so the
            permission notice for the `Write` this question leads to carries the
            question and both option labels inside `newest:` — measured on run
            `20260902T133429Z`, where a looser match counted two notices for one
            question. `asked:` and `option:` are the question brief's own; a
            permission's decision line is `permission:`.
            """
            return (
                f"  asked: {CLAUDE_QUESTION}" in seen.text
                and all(f"  option: {option}" in seen.text for option in CLAUDE_OPTIONS)
                and ANSWERABLE_HERE in seen.text
            )

        announced = self._await_own_message(
            mark,
            deadline_seconds=self.far_side.telegram_round_trip_seconds,
            matching=is_question_notice,
        )
        if announced is None:
            raise StepFailed(
                "the question reached the roster but no answerable notice naming this Session "
                f"reached chat; other traffic: {self._other_traffic(mark)}"
            )
        forms = _naming_forms(self._own_row())
        if not any(announced.text.startswith(form) for form in forms):
            raise StepFailed(
                f"the question notice did not name the Session first: {announced.text!r}"
            )
        if ANSWERABLE_AT_THE_TERMINAL in announced.text:
            raise StepFailed(
                f"the answerable question told the user to use the terminal: {announced.text!r}"
            )

        answer = self.bridgectl(
            "relay",
            self.address,
            self.lane.question_answer,
            timeout=support.RELAY_DEADLINE_SECONDS,
        )
        if not answer.ok or "delivered" not in answer.text.lower():
            raise StepFailed(
                f"`bridgectl relay {self.address} {self.lane.question_answer}` did not return "
                f"DELIVERED: {answer.text!r}"
            )

        tool_result_proof: str | None = None

        def tool_result_arrived() -> bool:
            nonlocal tool_result_proof
            tool_result_proof = self._question_tool_result_proof(self.lane.question_answer or "")
            return tool_result_proof is not None

        if not support.wait_for(
            tool_result_arrived,
            deadline_seconds=self.far_side.agent_turn_seconds,
        ):
            raise StepFailed(
                f"the transcript never recorded an error tool_result containing "
                f"{CLAUDE_ANSWER_FRAME!r}"
            )

        write_resolution = self._resolve_approval_effect(
            scenario="question continuation",
            requirement=approval_effect.ApprovalRequirement.REQUIRED,
            instruction=question,
            mark=mark,
            effect_seconds=self.far_side.agent_turn_seconds,
        )
        if not write_resolution.succeeded or write_resolution.authority_evidence is None:
            raise StepFailed(
                "the continued Claude turn did not complete its required write authority: "
                f"{write_resolution.failure}; {target} contains "
                f"{question.effect_in(self.config.workspace)!r}"
            )

        transcript_proof: str | None = None

        def transcript_continued() -> bool:
            nonlocal transcript_proof
            transcript_proof = self._question_transcript_proof(self.lane.question_answer or "")
            return transcript_proof is not None

        if not support.wait_for(
            transcript_continued,
            deadline_seconds=self.far_side.agent_turn_seconds,
        ):
            raise StepFailed(
                f"the transcript recorded {tool_result_proof}, but no later assistant record"
            )
        if not support.wait_for(
            lambda: self._roster_field("state") == "idle",
            deadline_seconds=self.far_side.agent_turn_seconds,
        ):
            raise StepFailed("the question turn did not return to idle after writing its answer")

        notices = [
            message
            for message in self.person.messages_after(mark)
            if message.from_bot and _named_in(message.text, forms) and is_question_notice(message)
        ]
        if len(notices) != 1:
            raise StepFailed(
                f"the question produced {len(notices)} answerable notices, not exactly one: "
                f"{[message.text for message in notices]!r}"
            )
        evidence = (
            f"question notice {announced.id}: {announced.text!r}; relay {answer.text!r}; "
            f"{tool_result_proof}; {transcript_proof}; {write_resolution.authority_evidence}; "
            f"{target} contains {question.content}"
        )
        self.journey.observe("question", evidence)
        return evidence

    # --- child ------------------------------------------------------------

    def child(self) -> str:
        """A child process is seen, never announced, and never spoken to (#79).

        Measured 2026-08-26 and the reason this step can exist at all: a `claude`
        started with `CLAUDE_CODE_CHILD_SESSION` set is absent from `claude
        agents --json` altogether. So "the child appears under its parent" is a
        claim about a source the official roster does not serve, and the product
        has to find it another way — which is what makes this #79's work rather
        than a formality.

        **"Never announced" is a claim about the child**, so the module's
        attribution rule is applied to the *child's* names here, not the parent's
        — the one place in this walk where the target of a read is not the
        Session under test. It has to be: the parent's own turn ends inside this
        step's window, and the parent's Stop Notice is the product working. A
        read that took the next bot message would have called that notice the
        child's and failed #79 for the one thing #79 does not forbid.
        """
        if self.address is None:
            raise LaneBlocked("no parent Session to hang a child from")
        mark = self.person.latest_message_id()
        before = {_address_of(row) for row in self._roster_rows()}
        turn = self._drive_turn("child", Instruction(words=self.lane.child_words))

        child_row, rows = self._await_child_row(before)
        if child_row is None:
            raise StepFailed(
                f"no child row appeared under {self.address} within "
                f"{self.far_side.agent_turn_seconds:.0f}s (turn ended={turn.ended}); the roster "
                f"gained {sorted({_address_of(row) for row in rows} - before) or 'nothing'}. "
                f"#74 locks `ChildClassification` and #79 fills it."
            )
        child_address = _address_of(child_row)
        parent = child_row["child"].get("parent")
        if not parent:
            raise StepFailed(f"the child row {child_address} names no parent")
        # "Listed **under its parent**" is the claim, and any non-empty parent
        # satisfied it before — including one naming some other Session, which is
        # precisely the bug a roster of several Sessions would produce and a
        # roster of one would hide.
        parent_address = _address_of({"target": parent})
        if parent_address != self.address:
            raise StepFailed(
                f"the child row {child_address} is listed under {parent_address}, not "
                f"under the Session that started it ({self.address})"
            )

        announced = self._await_message_naming(
            child_row,
            mark,
            deadline_seconds=self.far_side.absence_window_seconds,
            whose=f"the child {child_address}",
        )
        if announced is not None:
            raise StepFailed(
                f"a child raised a Stop Notice: message {announced.id} {announced.text!r} — "
                f"#79: children are seen, never announced"
            )
        refused = self.bridgectl("relay", child_address, "this must be refused")
        if refused.ok:
            raise StepFailed(
                f"the child {child_address} was accepted as a Relay target: {refused.text!r} — "
                f"#79: seen, never spoken to"
            )
        # A non-zero exit is not by itself a refusal: the surface exits non-zero
        # for an engine that never answered, a malformed address, a socket that
        # is not there. Only a refusal that *names this Session* proves the rule
        # was applied rather than the call merely failing.
        if child_address not in refused.text:
            raise StepFailed(
                f"the relay to the child {child_address} failed without refusing it — the answer "
                f"{refused.text!r} does not name it, so this is the call going wrong rather than "
                f"the child rule being applied"
            )
        # Seen, never spoken to — and never briefed about either. The Roster
        # Brief is what the voice side picks rows from, so a child listed there
        # would be a row the model can ask about and nothing can answer.
        roster_brief = self.bridgectl("brief")
        if roster_brief.ok and child_address in roster_brief.text:
            raise StepFailed(
                f"the child {child_address} has a row in the Roster Brief: "
                f"{roster_brief.text[:300]!r} — every row there is one `brief <address>` "
                f"answers, and a child is refused"
            )
        unbriefed = self.bridgectl("brief", child_address)
        if unbriefed.ok:
            raise StepFailed(
                f"the child {child_address} was briefed: {unbriefed.text!r} — #79: seen, "
                f"never spoken to, and #187 refuses it by the same rule `relay` does"
            )
        if child_address not in unbriefed.text:
            raise StepFailed(
                f"`brief {child_address}` failed without refusing it — the answer "
                f"{unbriefed.text!r} does not name it, so this is the call going wrong rather "
                f"than the child rule being applied"
            )
        return (
            f"{child_address} listed under {support.flatten([parent])} (asked to work for "
            f"{CHILD_LIFETIME_SECONDS}s, so the window it was read in is one this step made); "
            f"no notice naming it in {self.far_side.absence_window_seconds:.0f}s; "
            f"relay refused: {refused.text!r}; brief refused: {unbriefed.text!r}"
        )

    # --- live call --------------------------------------------------------

    def live_call(self) -> str:
        """The 0901 flow, end to end, on the calls the engine dials for itself (#198).

        The map's last slice, and its destination: what the earlier versions
        proved one call at a time — v0's route, #184's ceiling hold, #194's
        hand-over, v1's dial and pacing, #196's mid-call news, #197's failed
        relay — is one walk here, because the thing the product has to survive is
        the flow and not any one of its moments.

        **Six phases, in the order the ticket wrote them.** Each is a method of
        its own below and each returns its own sentence; this one holds what they
        share — the three extra Sessions, the call they all speak into, and the
        switch state the whole walk runs under.

        1. a Session stops **on a question**, and the engine dials about it;
        2. the user asks what needs them, and the Voice answers out of the
           hand-over with no verb behind it (#194);
        3. the user answers the question, the Call Agent relays it, and the Voice
           says the grade the engine gave those words (#193 §Voice);
        3a. Detail and History, and History's older page — the paging on the
           map's destination (#171), each asked by voice and answered by a verb;
        3b. the Voice is asked for a long answer and holds the call open past its
           own Silence Ceiling (#184), which is the only stretch in the walk that
           puts the *Voice's* half of the both-sides rule to a real call;
        4. mid-call news reaches the user two ways: a ring for a Session that is
           not the Focus one, a spoken brief for the one that is (#196);
        5. the user hangs up by voice, and what follows the ending is graded —
           the Cool-down holds a third Session's Stop, pays it once when it
           elapses from a **fresh** reading, and the call nobody speaks on ends
           by the Silence Ceiling.

        Then #197's phase, which holds a call of its own: a Relay that finally
        failed reaching the user through Briefing.

        **Three extra Sessions, and each phase is graded against one no other
        phase has moved.** The Focus one is dialled about, relayed into, briefed,
        paged and announced — one chain through one Session, which is what makes
        the walk a flow rather than six unrelated readings. The ringing one only
        ever rings, and the run's proof about it is that nothing spoken ever
        names it. The waiting one stops inside the Cool-down and is named by the
        dial that pays it, which is the fact no replayed event could produce.

        **Every read is lower-bounded and by substring** (#181): a hand-off may
        happen more than once per request and may arrive before the user's own
        transcript, so nothing here counts what it cannot bound or asserts an
        absence outside a window it opened itself. Tones are asserted by log
        order only, and never on audio — whether either was audible is #174's ear
        test (#186).
        """
        if self.config.call_observations is None or self.config.call_wav_directory is None:
            raise LaneBlocked(
                "this run's engine was not given the harness Call adapter, so there is no "
                "call to hold without a microphone (`support.derive_config(spoken_call=True)`)"
            )
        focus_name = self.config.call_focus_workspace
        ringing_name = self.config.call_ringing_workspace
        waiting_name = self.config.call_waiting_workspace
        if focus_name is None or ringing_name is None or waiting_name is None:
            raise LaneBlocked(
                "this run's config names no workspaces for the three extra Sessions, so the "
                "utterances and the step cannot agree on which Session is which (#196, #198)"
            )
        ceiling = self._silence_ceiling_seconds()
        cool_down = self._cool_down_seconds()
        settle = self._speech_settle_seconds()
        turn = self.far_side.agent_turn_seconds
        # **The mid-call interval has to fit inside the ceiling**, because phase 4
        # waits one out on a call it must still be holding afterwards. #196's
        # guard, kept whole: what has to fit is the interval plus the settle
        # window plus the slack a cue line is written with.
        if cool_down + settle + LIVE_CALL_CUE_SECONDS >= ceiling:
            raise LaneBlocked(
                f"this lane's Silence Ceiling is {ceiling:.0f}s and one mid-call interval is "
                f"{cool_down:.0f}s: the wait phase 4 makes between the ring and the "
                f"announcement would be what ends the call, and the announcement would be "
                f"graded against a call that had already gone"
            )
        # The ticket's own first line: Duty and Voice on, Auto Hang-up on, no
        # call. Voice is armed per phase by `_voice_route_only`; Auto Hang-up is
        # what phase 5's ending answers to, and `switches` may have been the step
        # before this one.
        self._arm_auto_hangup()
        started = time.monotonic()
        with (
            self._an_extra_session(focus_name) as (focus, focus_at),
            self._an_extra_session(ringing_name) as (ringing, ringing_at),
            self._an_extra_session(waiting_name) as (waiting, waiting_at),
        ):
            # All three are waited for before anything else happens, so the
            # roster the first call is dialled on already holds them and the boot
            # turn each launch starts (#110) is over before the walk types
            # anything.
            focus_address = self._await_extra_session(focus_at, turn)
            ringing_address = self._await_extra_session(ringing_at, turn)
            waiting_address = self._await_extra_session(waiting_at, turn)
            if not self._leave_no_call_up(LIVE_CALL_CUE_SECONDS):
                raise LaneBlocked(
                    f"a Live Call was still up when the walk began, so there is no call of its "
                    f"own to grade: {self._call_line()!r}"
                )
            # **Phase 3a needs two History pages, so they are made here.** The
            # older page is what `再往前` is answered out of, and a Session three
            # turns old has one page and nothing before it. Driven with Voice
            # still off, so none of these Stops is mid-call news about a Session
            # a later phase grades.
            filled = self._fill_a_history_worth_paging(focus, focus_at, focus_address, turn)
            # **The Stop goes in with Voice still off, and Voice comes on after
            # it.** Turning Voice on is itself a `wake` (#195), so in this order
            # the dial cannot land before the Session it is supposed to be about
            # has stopped. #196's ordering, and its reason.
            self._drive_extra_session(focus, focus_at, turn, ASK_A_QUESTION)
            live_call.ask_for_nothing(self.config.call_wav_directory)
            mark = len(self.engine.log_lines())
            with self._voice_route_only():
                dialled = self._the_engine_dials_about_a_session_that_stopped(mark, cool_down)
                answered = self._the_voice_answers_out_of_the_hand_over(focus_name)
                relayed = self._the_answer_is_relayed_and_receipted(
                    focus_name, focus_at, focus_address, turn
                )
                paged = self._detail_and_history_are_asked_for_by_voice(focus_at, focus_address)
                held = self._the_voice_holds_the_call_open(ceiling)
                news = self._mid_call_the_focus_session_speaks_and_the_rest_rings(
                    mark=mark,
                    focus=focus,
                    focus_at=focus_at,
                    focus_address=focus_address,
                    ringing=ringing,
                    ringing_at=ringing_at,
                    ringing_address=ringing_address,
                    ringing_name=ringing_name,
                    turn=turn,
                    cool_down=cool_down,
                    settle=settle,
                )
                hung_up = self._hung_up_by_voice_then_a_cool_down_and_a_ceiling(
                    mark=mark,
                    waiting=waiting,
                    waiting_at=waiting_at,
                    waiting_address=waiting_address,
                    turn=turn,
                    cool_down=cool_down,
                    ceiling=ceiling,
                )
            # **And then again, with Voice off.** A dial decided is a call that
            # arrives seconds later, so a phase that read the call down and went
            # can leave one behind for whatever runs next. Out here rather than
            # inside the block above, because the watch is only finite once
            # nothing can dial — which is what leaving `_voice_route_only`
            # settles.
            left_up = not self._leave_no_call_up(LIVE_CALL_CUE_SECONDS)
        self._measured("live call", started, self._call_is_down())
        seen = live_call.observed(self.config.call_observations)
        self.journey.observe(
            "live call transport",
            f"factory {seen.transport_factory or 'none recorded'}; last wav variant "
            f"{seen.variant or 'none recorded'}; last end reason "
            f"{seen.end_reason or 'none recorded'}; observations "
            f"{self.config.call_observations}",
        )
        self.journey.observe(
            "live call bridgectl runs",
            f"{len(support.cli_wrapper_runs(self.config.cli_wrapper_log))} logged in "
            f"{self.config.cli_wrapper_log}; verbs in order: {self._verbs_run() or 'none'}",
        )
        if left_up:
            raise StepFailed(
                f"the walk dialled and could not put the call back down: {self._call_line()!r}. "
                f"Every step after this one would be graded against a call it did not open"
            )
        # The three observations the ticket asks `verdict.json` to carry. Graded
        # rather than merely written down: a run that recorded none of them is a
        # run whose Call adapter never wrote its file, and a green step that says
        # `none recorded` three times has reported nothing about the calls it held.
        missing = [
            name
            for name, value in (
                ("transport factory", seen.transport_factory),
                ("wav variant", seen.variant),
                ("end reason", seen.end_reason),
            )
            if not value
        ]
        if missing:
            raise StepFailed(
                f"the walk ran and {self.config.call_observations} records no "
                f"{', no '.join(missing)} — the step cannot report what the ticket asks it to "
                f"observe. What it does hold: {seen.entries[-3:] or 'nothing at all'}"
            )
        failed = self._mid_call_a_relay_that_finally_failed()
        return (
            f"{filled} turn(s) driven to give the Focus Session a History worth paging; "
            f"{dialled}. Then {answered}. Then {relayed}. Then {paged}. Then {held}. Then "
            f"{news}. Then {hung_up}. Then {failed}"
        )

    def _fill_a_history_worth_paging(
        self,
        extra: hand_started.HandStartedSession,
        workspace: Path,
        address: str,
        turn: float,
    ) -> int:
        """Drive turns at an extra Session until its History has a page behind it.

        `_drive_until_history_pages`' rule, for a Session the harness started
        rather than for the walk's own, and without #171's floor: what phase 3a
        needs is not "more than the page size, driven by this step" — that is
        #171's own red line and `brief` still walks it — but simply an older page
        for `再往前` to have been answered out of. The engine's own `older` is
        what says the page has somewhere to go, because the page size is the
        engine's dial and this harness deliberately does not read it.

        Turns are words-only for `ACKNOWLEDGE`'s reason, and they run with Voice
        off: every one of them ends in a Stop, and a Stop on a call that was up
        would be mid-call news a later phase is graded on.
        """
        driven = 0
        while True:
            page = self._history_read_yet(self._extra_address(workspace, address))
            if page is not None and page.older:
                return driven
            if driven >= HISTORY_TURNS_CEILING:
                raise LaneBlocked(
                    f"{driven} turns at {address} did not produce a second History page, so "
                    f"there is nothing for `再往前` to page back to. What one page holds: "
                    f"{page if page is not None else 'no record the engine has read'}"
                )
            self._drive_extra_session(extra, workspace, turn)
            support.wait_for(
                lambda: str((self._row_in(workspace) or {}).get("state")) != "running",
                deadline_seconds=turn,
                poll_seconds=LIVE_CALL_POLL_SECONDS,
            )
            driven += 1

    def _the_engine_dials_about_a_session_that_stopped(self, mark: int, cool_down: float) -> str:
        """Phase 1: nobody presses anything, and a call comes up holding a briefing.

        A Session stopped on a question with Voice on and Message off — the one
        state whose only outlet is opening a call — so what is graded is that the
        engine took it, and what the dial carried.

        * **it dialled**, read off `HAND_OVER_LINE`, which is the only line
          either call event has: `CallStarted` is noted into the interlock and
          never logged (`core/bridge.py:777-785`);
        * **the hand-over carries more than one item** — a call the *user* opened
          carries exactly one (#167 Q6), so this is what separates a system dial
          from a toggle;
        * **and the kinds are the Roster Brief and a Session Brief.** The line
          names the kinds it sent and never their words, so this is as far as the
          log can go; that the Session Brief is *this* Session's is what phase 2
          proves, by asking a question only the hand-over can answer;
        * **CONNECTED was played** — the user hears a call connect, and on a dial
          the engine made rather than on a toggle somebody pressed. The adapter's
          own record of the cue it played is the only witness a run that cannot
          hear has (#186).
        """
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
        self.journey.observe(
            "live call the dial",
            f"{len(hand_over)} dial(s); the first "
            f"{hand_over[0].strip() if hand_over else 'never made'}; cues so far "
            f"{self._cue_order(since=mark)}",
        )
        if not (opened and up):
            raise StepFailed(
                f"a Session stopped on a question with Voice on and Message off and the engine "
                f"never dialled within {LIVE_CALL_OPEN_SECONDS + cool_down:.0f}s — a call is "
                f"the only outlet that state leaves. Dialled: {opened}, up: "
                f"{self._call_line()!r}. Engine log tail: {self._log_since(mark)[-8:]}"
            )
        if not _hand_over_of_more_than_one(hand_over[0]):
            raise StepFailed(
                f"the engine dialled, but the hand-over is {hand_over[0].strip()!r}. A call the "
                f"*system* dialled carries the reason, the Roster Brief and every Session that "
                f"needs the user (#194); one item is what a call the *user* opened gets"
            )
        carried = _hand_over_kinds(hand_over[0])
        for kind in (ROSTER_BRIEF_KIND, SESSION_BRIEF_KIND):
            if kind not in carried:
                raise StepFailed(
                    f"the engine dialled holding {carried or 'nothing it named'} and not a "
                    f"{kind}. The ticket's hand-over is the Roster Brief *and* the brief of the "
                    f"Session that stopped, and the dial line is where the kinds are written "
                    f"down (`adapters/call/realtime/adapter.py`): {hand_over[0].strip()!r}"
                )
        if not connected:
            raise StepFailed(
                f"the engine dialled and no CONNECTED cue was written within "
                f"{LIVE_CALL_CUE_SECONDS:.0f}s. The Keeper plays it when the dial comes up "
                f"(#195), and the adapter's own line is the only witness a run that cannot "
                f"hear has. Engine log tail: {self._log_since(mark)[-8:]}"
            )
        return (
            f"a Session stopped on a question with Voice on and Message off and the engine "
            f"dialled by itself, holding {hand_over[0].split('holding', 1)[-1].strip()}, with "
            f"CONNECTED played"
        )

    def _the_voice_answers_out_of_the_hand_over(self, focus_name: str) -> str:
        """Phase 2: a question only the dial can answer, answered with no verb (#194).

        `initialItems` is retained and never repeated (#175 Q5), so a Voice that
        was handed the roster and a Voice that was handed nothing sound identical
        until one is asked something only the hand-over holds. `现在有哪些需要我的
        事情?` is that question, and a Voice with nothing does not fall silent —
        asked the time with no answer to hand, the probe's Voice invented one
        eleven hours out and kept advancing it (ADR 0018).

        **Three facts, and the third is the sharp one.**

        * the words reached the engine as speech, by substring (#181);
        * the Voice's own transcript **names the Session** the call was dialled
          about. The name is the workspace's basename, which is the project half
          of a Session Name (`adapters/agent/_project.py`) and which this harness
          chose — so an answer carrying it came out of `initialItems`, and there
          is nowhere else on this call it could have come from;
        * **and the Call Agent ran nothing for it.** The wrapper log gains no run
          across this question — the ticket's own sentence, and the half that
          says the Voice was holding the answer rather than going to look for it.
          An absence, so it is scoped to this phase's own window (#181): the
          Voice's span is waited out first, and then watched a while longer,
          because a hand-off can arrive after the answer has begun.
        """
        runs_before = len(support.cli_wrapper_runs(self.config.cli_wrapper_log))
        asking = len(self.engine.log_lines())
        live_call.ask_next(self.config.call_wav_directory, live_call.NEEDS)
        heard = support.wait_for(
            lambda: bool(self._user_speech_lines(LIVE_CALL_NEEDS_HEARD_SUBSTRING, since=asking)),
            deadline_seconds=LIVE_CALL_HEARD_SECONDS + live_call.PLAYLIST_POLL_SECONDS,
            poll_seconds=LIVE_CALL_POLL_SECONDS,
        )
        # From this phase's own mark and not from the question's line: the Voice
        # begins answering as the recogniser finalises the sentence, and on run
        # `20260902T215924Z` its start edge was logged one line *before* the
        # user-speech line (#181 finding 2, seen from the other side).
        named = support.wait_for(
            lambda: self._voice_said_something_carrying(focus_name, since=asking),
            deadline_seconds=LIVE_CALL_ANSWER_SECONDS,
            poll_seconds=LIVE_CALL_POLL_SECONDS,
        )
        support.wait_for(
            self._voice_finished_speaking(asking),
            deadline_seconds=LIVE_CALL_ANSWER_SECONDS,
            poll_seconds=LIVE_CALL_POLL_SECONDS,
        )
        support.wait_for(
            lambda: len(support.cli_wrapper_runs(self.config.cli_wrapper_log)) > runs_before,
            deadline_seconds=LIVE_CALL_NO_VERB_SECONDS,
            poll_seconds=LIVE_CALL_POLL_SECONDS,
        )
        runs = support.cli_wrapper_runs(self.config.cli_wrapper_log)[runs_before:]
        said = self._voice_said_lines(since=asking)
        self.journey.observe(
            "live call the hand-over answered",
            f"the Voice opened {self._voice_speech_edges(since=asking)[True]} span(s) for this "
            f"question and said: {said or 'nothing recorded'}; `bridgectl` runs across it: "
            f"{runs or 'none'}",
        )
        if not heard:
            raise StepFailed(
                f"the call came up holding the briefing and the engine never logged the "
                f"question within {LIVE_CALL_HEARD_SECONDS:.0f}s. The utterance the harness "
                f"put on the track is {live_call.NEEDS_REQUEST!r} and the line looked for "
                f"carries {LIVE_CALL_NEEDS_HEARD_SUBSTRING!r}. Engine log tail: "
                f"{self._log_since(asking)[-8:]}"
            )
        if not named:
            raise StepFailed(
                f"the question reached the engine and the Voice never named {focus_name!r} — "
                f"the Session this call was dialled about, whose brief rode `initialItems`. A "
                f"Voice with nothing to say invents rather than going quiet (ADR 0018), so "
                f"what it said instead is the answer: {said or 'nothing this call recorded'}. "
                f"The call now: {self._call_line()!r}"
            )
        if runs:
            raise StepFailed(
                f"the Voice answered, but the Call Agent ran {len(runs)} `bridgectl` "
                f"command(s) for a question with no verb in it: {runs!r}. The whole point of "
                f"the hand-over is that the answer was already in the call — a hand-off here "
                f"says the Voice went looking for what it was holding (#194)"
            )
        return (
            f"the user asked what needed them and the Voice answered out of the hand-over, "
            f"naming {focus_name!r}, with no `bridgectl` run behind it"
        )

    def _the_answer_is_relayed_and_receipted(
        self, focus_name: str, focus_at: Path, focus_address: str, turn: float
    ) -> str:
        """Phase 3: the user answers the question, and hears what became of the words.

        The one utterance the walk follows all the way through — the air, the
        Call Agent's argv, the Session's own record, and the Voice's receipt.
        Four facts:

        * **the words arrived**, by substring, for `_user_speech_lines`' reason;
        * **the Call Agent ran `relay`** — a lower bound of one run of that verb,
          whatever else it ran. Which Session it chose is recorded and not
          graded: grading it would grade the Voice;
        * **that Session's next turn carries the answer.** Read through
          `bridgectl history`, which is the surface a user gets, rather than off
          a record this harness took no ground truth for. The address is read
          again first: a row is re-keyed as the lane learns more about it, and an
          address held across a turn can name a row the registry no longer holds
          (`sessions.py::_better_known`, run `20260903T111253Z`);
        * **the Voice said the grade the engine gave those words.** `已转达` for
          a delivered relay, `收到` for one queued behind that Session's turn
          (#193 §Voice). One of the two, because a run cannot choose which grade
          a real relay earns — a Session that happens to be mid-turn queues it.

        **And then the Voice stops, accounted for.** The ticket asks that nothing
        else be said before the next spoken request. Taken literally that grades
        #196's own rule as a failure: the answer this phase relays is what drives
        the Focus Session's next Stop, and a Focus Stop mid-call is a `speak` the
        engine hands to the call in the first gap (`call_keeper.py::_owed_word`).
        So what is graded is that every assistant turn after the receipt is
        **accounted for by an engine payment in the same window** — the Voice
        going on by itself is what is forbidden, and the engine handing it a
        brief is not that. The count is recorded either way, which is what makes
        the reading legible in `verdict.json`.
        """
        runs_before = len(support.cli_wrapper_runs(self.config.cli_wrapper_log))
        relaying = len(self.engine.log_lines())
        live_call.ask_next(self.config.call_wav_directory, live_call.RELAY)
        heard = support.wait_for(
            lambda: bool(self._user_speech_lines(LIVE_CALL_RELAY_HEARD_SUBSTRING, since=relaying)),
            deadline_seconds=LIVE_CALL_HEARD_SECONDS + live_call.PLAYLIST_POLL_SECONDS,
            poll_seconds=LIVE_CALL_POLL_SECONDS,
        )
        relayed = support.wait_for(
            lambda: bool(self._verbs_since(runs_before, Action.RELAY)),
            deadline_seconds=LIVE_CALL_HANDOFF_SECONDS,
            poll_seconds=LIVE_CALL_POLL_SECONDS,
        )
        relays = self._verbs_since(runs_before, Action.RELAY)
        receipted = support.wait_for(
            lambda: any(
                self._voice_said_something_carrying(wording, since=relaying)
                for wording in (RELAY_RECEIPT_DELIVERED, RELAY_RECEIPT_QUEUED)
            ),
            deadline_seconds=LIVE_CALL_ANSWER_SECONDS,
            poll_seconds=LIVE_CALL_POLL_SECONDS,
        )
        # The Session's own next turn, through the surface a user reads it on.
        # Budget is one turn plus the Relay's own deadline: the words wait in the
        # Reply Window until the Session can take them.
        carried = support.wait_for(
            lambda: self._history_of_a_session_carries(
                focus_at, focus_address, LIVE_CALL_ANSWER_SUBSTRING
            ),
            deadline_seconds=turn + support.RELAY_DEADLINE_SECONDS,
            poll_seconds=LIVE_CALL_POLL_SECONDS,
        )
        # Read after the Session's turn has landed, so the window this counts
        # over is the one the payment would have fallen in.
        since_relay = self._log_since(relaying)
        payments = support.matching_lines(since_relay, MID_CALL_SPOKEN_PATTERN)
        unaccounted = _unaccounted_voice_turns(
            since_relay, (RELAY_RECEIPT_DELIVERED, RELAY_RECEIPT_QUEUED)
        )
        after = self._verbs_since(runs_before, Action.RELAY)
        further = [
            verb
            for verb in self._verbs_run(since=runs_before)
            if verb.split()[:1] != [str(Action.RELAY)]
        ]
        self.journey.observe(
            "live call the answer relayed",
            f"the Call Agent relayed with {relays or 'nothing'} — which Session it picked is "
            f"recorded and not graded, and it is the Session the call was dialled about: "
            f"{any(focus_address in verb for verb in relays)}; the Voice said "
            f"{self._voice_said_lines(since=relaying) or 'nothing recorded'}; "
            f"{len(payments)} engine payment(s) in the window, "
            f"{'no' if not unaccounted else unaccounted} assistant turn(s) unaccounted for",
        )
        if not heard:
            raise StepFailed(
                f"the answer utterance went on the track and the engine never logged the "
                f"user's speech within {LIVE_CALL_HEARD_SECONDS:.0f}s. It carries "
                f"{live_call.relay_request(focus_name)!r} and the line looked for carries "
                f"{LIVE_CALL_RELAY_HEARD_SUBSTRING!r}. Engine log tail: {since_relay[-8:]}"
            )
        if not relayed:
            raise StepFailed(
                f"the engine heard the answer and the Call Agent never ran "
                f"`{Action.RELAY}` within {LIVE_CALL_HANDOFF_SECONDS:.0f}s. What it ran "
                f"instead: {self._verbs_run(since=runs_before) or 'nothing at all'}"
            )
        if not carried:
            raise StepFailed(
                f"the Call Agent relayed with {after} and {LIVE_CALL_ANSWER_SUBSTRING!r} never "
                f"reached {focus_address}'s own record within "
                f"{turn + support.RELAY_DEADLINE_SECONDS:.0f}s. The relay is the user's answer "
                f"to the question that Session stopped on, so its next turn is where the words "
                f"have to be. What `bridgectl history` holds: "
                f"{self._history_page(address=self._extra_address(focus_at, focus_address))}"
            )
        if not receipted:
            raise StepFailed(
                f"the words reached the Session and the Voice never told the user so. #193 "
                f"§Voice has it say {RELAY_RECEIPT_DELIVERED!r} for a delivered relay and "
                f"{RELAY_RECEIPT_QUEUED!r} for one queued behind that Session's turn. What it "
                f"said: {self._voice_said_lines(since=relaying) or 'nothing this call recorded'}"
            )
        if further:
            raise StepFailed(
                f"the Voice gave the user their receipt and the Call Agent went on working: "
                f"{further}. One spoken answer is one hand-off, and nothing else was asked for "
                f"before the next request went on the track"
            )
        if unaccounted is None:  # pragma: no cover - `receipted` already answered this
            raise StepFailed("the receipt was heard and then could not be found in the log")
        if unaccounted > 0:
            raise StepFailed(
                f"the Voice said its receipt and then said {unaccounted} more thing(s) nobody "
                f"asked it for: the window holds {len(payments)} engine payment(s) "
                f"({[line.strip() for line in payments]}) and more assistant turns than that. "
                f"What it said: {self._voice_said_lines(since=relaying)}"
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

    def _detail_and_history_are_asked_for_by_voice(self, focus_at: Path, focus_address: str) -> str:
        """Phase 3a: Detail, History, and History's older page — all three by voice (#171).

        Paging is on the map's destination, and this is where the two read verbs
        the Call Agent owns are put to a real call. Each of the three sentences
        is the same shape: what the control plane would answer is read **first**,
        then the sentence goes on the track, then the verb is waited for, then
        the Voice's own transcript is asked whether it carries what the engine
        holds.

        **Read first, and by substring.** The reading has to be taken before the
        question is asked, or the thing compared against is a page the answer
        itself could have changed; and what is looked for is a short opening
        fragment of it (`_spoken_fragment`), because the Voice paraphrases a
        record into a sentence and #181 grades every read by substring.

        **`--before` is graded on the argv and not on the answer.** Which entry
        the Call Agent's own older page held is its cursor's business; that it
        paged at all is the fact the ticket asks for, and it is written in the
        wrapper log.
        """
        address = self._extra_address(focus_at, focus_address)
        brief = self.bridgectl("brief", address)
        if not brief.ok:
            raise LaneBlocked(f"`bridgectl brief {address}` refused mid-call: {brief.text}")
        newest = _newest_message(brief.text)
        if not newest:
            raise LaneBlocked(
                f"`bridgectl brief {address}` carries no `newest` line mid-call, so there is "
                f"nothing for the Voice's Detail answer to be compared against: "
                f"{brief.text[:300]!r}"
            )
        newest_page = self._history_page(address=address)
        if not newest_page.entries:
            raise LaneBlocked(
                f"`bridgectl history {address}` is empty mid-call, so neither History question "
                f"has an answer to be compared against"
            )
        cursor = min(ordinal for ordinal, _ in newest_page.entries)
        latest = max(ordinal for ordinal, _ in newest_page.entries)
        older_page = self._history_page(before=cursor, address=address)
        detail = self._one_read_asked_for_by_voice(
            variant=live_call.DETAIL,
            heard=LIVE_CALL_DETAIL_HEARD_SUBSTRING,
            action=Action.BRIEF,
            wanted=(_spoken_fragment(newest),),
            about=f"the Session Brief's newest message {newest[:120]!r}",
        )
        # **Older than `newest`, which is what the question asks for.** The newest
        # page *includes* the newest entry (#171 amends ADR 0016), so the entry
        # the Detail answer already read back is excluded here — an answer that
        # repeated it would say nothing about History at all. Every remaining
        # entry on the page is acceptable: which one the Voice reads out is its
        # choice, and pinning one would grade the choice.
        earlier_entries = tuple(text for ordinal, text in newest_page.entries if ordinal < latest)
        if not earlier_entries:
            raise LaneBlocked(
                f"{address}'s newest History page holds one entry ({newest_page.entries!r}), so "
                f"there is nothing older than `newest` on it for `它之前说了什么` to have been "
                f"answered out of"
            )
        history = self._one_read_asked_for_by_voice(
            variant=live_call.HISTORY,
            heard=LIVE_CALL_HISTORY_HEARD_SUBSTRING,
            action=Action.HISTORY,
            wanted=tuple(_spoken_fragment(text) for text in earlier_entries),
            about=f"an entry on {address}'s newest History page ({newest_page.entries!r})",
        )
        if not older_page.entries:
            raise LaneBlocked(
                f"{address} has no History page older than ordinal {cursor}, so there is no "
                f"older page for `再往前` to have been answered out of. What one page holds: "
                f"{newest_page.entries!r}. The walk fills this Session's history before it "
                f"dials, so an empty older page is the fill not having taken"
            )
        earlier = self._one_read_asked_for_by_voice(
            variant=live_call.EARLIER,
            heard=LIVE_CALL_EARLIER_HEARD_SUBSTRING,
            action=Action.HISTORY,
            wanted=tuple(_spoken_fragment(text) for _, text in older_page.entries),
            about=f"an entry on the page before ordinal {cursor} ({older_page.entries!r})",
            argv=HISTORY_CURSOR_OPTION,
        )
        return f"{detail}; {history}; {earlier}"

    def _one_read_asked_for_by_voice(
        self,
        *,
        variant: str,
        heard: str,
        action: Action,
        wanted: tuple[str, ...],
        about: str,
        argv: str | None = None,
    ) -> str:
        """One spoken question that the Call Agent has to answer with a read verb.

        Three of these make phase 3a, and they differ only in the sentence, the
        verb, and what the engine was holding when it was asked — so they are one
        method rather than three that would drift apart. Every wait is
        lower-bounded and every read is a substring (#181).

        `wanted` is a *set* of acceptable fragments and any one of them passes: a
        History page holds several entries and the Voice picks which to read out,
        and pinning one would grade the Voice's choice rather than whether it was
        answering out of the record. `argv` is an option that has to appear in
        the wrapper log, which is how paging is observed.
        """
        runs_before = len(support.cli_wrapper_runs(self.config.cli_wrapper_log))
        asking = len(self.engine.log_lines())
        live_call.ask_next(self.config.call_wav_directory, variant)
        arrived = support.wait_for(
            lambda: bool(self._user_speech_lines(heard, since=asking)),
            deadline_seconds=LIVE_CALL_HEARD_SECONDS + live_call.PLAYLIST_POLL_SECONDS,
            poll_seconds=LIVE_CALL_POLL_SECONDS,
        )
        ran = support.wait_for(
            lambda: bool(self._verbs_since(runs_before, action)),
            deadline_seconds=LIVE_CALL_HANDOFF_SECONDS,
            poll_seconds=LIVE_CALL_POLL_SECONDS,
        )
        answered = support.wait_for(
            lambda: any(
                self._voice_said_something_carrying(fragment, since=asking)
                for fragment in wanted
                if fragment
            ),
            deadline_seconds=LIVE_CALL_ANSWER_SECONDS,
            poll_seconds=LIVE_CALL_POLL_SECONDS,
        )
        verbs = self._verbs_since(runs_before, action)
        said = self._voice_said_lines(since=asking)
        self.journey.observe(
            f"live call {variant} by voice",
            f"the Call Agent ran {verbs or 'nothing'}; the Voice said "
            f"{said or 'nothing recorded'}; what the engine was holding: {about}",
        )
        if not arrived:
            raise StepFailed(
                f"the {variant} utterance went on the track and the engine never logged the "
                f"user's speech within {LIVE_CALL_HEARD_SECONDS:.0f}s. The line looked for "
                f"carries {heard!r}. Engine log tail: {self._log_since(asking)[-8:]}"
            )
        if not ran:
            raise StepFailed(
                f"the engine heard the {variant} question and the Call Agent never ran "
                f"`{action}` within {LIVE_CALL_HANDOFF_SECONDS:.0f}s. What it ran instead: "
                f"{self._verbs_run(since=runs_before) or 'nothing at all'}"
            )
        if argv is not None and not any(argv in verb for verb in verbs):
            raise StepFailed(
                f"the user asked for the older page and the Call Agent ran `{action}` without "
                f"`{argv}`: {verbs}. Paging is the cursor, and a read that did not carry one "
                f"answered out of the newest page (#171)"
            )
        if not answered:
            raise StepFailed(
                f"the Call Agent ran {verbs} and the Voice never said anything carrying "
                f"{[fragment for fragment in wanted if fragment]!r} — {about}. What it said: "
                f"{said or 'nothing this call recorded'}"
            )
        return f"{variant} answered with {verbs} and read back to the user"

    def _the_voice_holds_the_call_open(self, ceiling: float) -> str:
        """Phase 3b: the Voice's own answer outlasts the Silence Ceiling (#184).

        The one stretch of the walk that puts the **Voice's** half of the
        both-sides rule to a real call. Everywhere else the user speaks
        immediately before the Voice does, so the ceiling is restarted from the
        user's side and nothing is learned about the other one; here the request
        is `请你从一数到两百…`, which has no hang-up in it and nothing for the Call
        Agent to do, and what keeps the call alive for the next minute is the
        Voice answering.

        Two facts, and each is one moment on this step's own clock subtracted
        from another rather than a stamp parsed out of the log:

        * **it went on answering for longer than the ceiling.** First edge to
          last, watched across the stretch (`_VoiceWatch`). Before #184 the only
          activity the engine counted was the user's transcript, so the call
          would have gone sixty seconds into this;
        * **every span it opened closed.** A start with no stop is precisely the
          bug (#169): the ceiling firing mid-answer takes the call away before
          the `transcript/done` that would have closed the span. Counted rather
          than paired off in time, because the Voice may answer more than once
          and a `done` with no delta before it is still an utterance that ended.

        Whether the stretch survived because the ceiling *held* through one long
        span or because both edges kept restarting it is the Voice's own choice
        of turn structure and varies run to run — both are #184, so the hub tests
        pin the hold on its own (`tests/test_bridge.py`) and this proves the rule
        end to end.

        **And the call is still up afterwards**, which is what makes phase 4
        possible: the walk goes on speaking into this call.
        """
        asking = len(self.engine.log_lines())
        live_call.ask_next(self.config.call_wav_directory, live_call.LONG)
        heard = support.wait_for(
            lambda: bool(self._user_speech_lines(LIVE_CALL_LONG_HEARD_SUBSTRING, since=asking)),
            deadline_seconds=LIVE_CALL_HEARD_SECONDS + live_call.PLAYLIST_POLL_SECONDS,
            poll_seconds=LIVE_CALL_POLL_SECONDS,
        )
        watch = self._watch_the_voice(
            asking,
            deadline_seconds=LIVE_CALL_ANSWER_SECONDS + ceiling,
            quiet_seconds=LIVE_CALL_CUE_SECONDS,
        )
        edges = watch.edges
        answered_for = (
            0.0
            if watch.first_voice_at is None or watch.last_voice_at is None
            else watch.last_voice_at - watch.first_voice_at
        )
        self.journey.observe(
            "live call the voice holds the call open",
            f"ceiling {ceiling:.0f}s; the Voice was answering for {answered_for:.0f}s across "
            f"{edges[True]} start / {edges[False]} stop edge(s); the call was "
            f"{'down' if watch.went_down else 'still up'} when the answer closed",
        )
        if not heard:
            raise StepFailed(
                f"the long-answer utterance went on the track and the engine never logged the "
                f"user's speech within {LIVE_CALL_HEARD_SECONDS:.0f}s. It carries "
                f"{live_call.LONG_REQUEST!r} and the line looked for carries "
                f"{LIVE_CALL_LONG_HEARD_SUBSTRING!r}. Engine log tail: "
                f"{self._log_since(asking)[-8:]}"
            )
        if not edges[True]:
            raise StepFailed(
                f"the engine never wrote a {VOICE_SPEAKING_LINE!r} line: the words arrived and "
                f"the Voice answered nothing, so there is no answer for the ceiling to have "
                f"counted. Engine log tail: {self._log_since(asking)[-5:]}"
            )
        if edges[False] < edges[True]:
            raise StepFailed(
                f"the Voice left a span open: {edges[True]} start and {edges[False]} stop "
                f"edge(s). That is the bug itself (#169) — the ceiling firing mid-answer takes "
                f"the call away before the `transcript/done` that would have closed the span. "
                f"The Voice was answering for {answered_for:.0f}s against a {ceiling:.0f}s "
                f"ceiling; the call is {self._call_line()!r}"
            )
        if answered_for <= ceiling:
            raise StepFailed(
                f"the Voice was answering for {answered_for:.0f}s, which does not outlast this "
                f"lane's own {ceiling:.0f}s Silence Ceiling — the request asks for two hundred "
                f"numbers, so an answer this short is one the Voice cut rather than one the "
                f"ceiling would ever have had to hold a call open through. Nothing about the "
                f"both-sides rule is proven by it"
            )
        if watch.went_down:
            raise StepFailed(
                f"the call went down while the Voice was answering, {answered_for:.0f}s into a "
                f"stretch its own speech is supposed to keep alive ({self._call_line()!r}). "
                f"The ceiling counts both sides (#184), and the walk speaks into this call "
                f"again after this"
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

    def _mid_call_a_relay_that_finally_failed(self) -> str:
        """#197's phase of `live call`: the user hears why their words never landed.

        The ticket's own Red-first, on a real call. A Relay is held past its
        ceiling into the **Focus Session** while a call is up, and the two
        things that follow are the user's side of it:

        * **one `speak` whose brief carried `undelivered`** — the Keeper's
          mid-call line ends on a code-shaped token saying so
          (`core/call_keeper.py::MID_CALL_SPOKEN_LINE`), because the brief
          itself crosses the Call seam and is never written down;
        * **the Voice said it** — the assistant transcript the realtime adapter
          writes down carries #173 §6's "没送到". A substring per #181: the Voice
          composes the sentence from what Briefing handed it, and this run does
          not get to predict the rest of it.

        **The hold is arranged, never raced.** The Relay is queued only after the
        Session is *already* waiting on a permission nobody will answer, which is
        a shut Reply Window that stays shut for as long as this phase leaves it
        shut (`seams/agent.py::derive_reply_window`). Precedent is #105 and #79:
        the situation the step judges is arranged, and nothing that is judged is.

        The permission is raised by the lane's own actionable instruction, so
        neither lane is handed the other's idea of what needs asking about.
        """
        if self.config.call_observations is None or self.config.call_wav_directory is None:
            raise LaneBlocked(
                "this run's engine was not given the harness Call adapter, so there is no "
                "call to hold without a microphone (`support.derive_config(spoken_call=True)`)"
            )
        focus_name = self.config.call_focus_workspace
        if focus_name is None:
            raise LaneBlocked("this run's config names no workspace for the extra Session (#196)")
        ceiling = self._relay_ceiling_seconds()
        settle = self._speech_settle_seconds()
        silence = self._silence_ceiling_seconds()
        cool_down = self._cool_down_seconds()
        turn = self.far_side.agent_turn_seconds
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
        with self._an_extra_session(focus_name) as (extra, workspace):
            address = self._await_extra_session(workspace, turn)
            if not self._leave_no_call_up(LIVE_CALL_CUE_SECONDS):
                raise LaneBlocked(
                    f"a Live Call was still up after this phase asked for it to end, so there "
                    f"is no call of its own to grade: {self._call_line()!r}"
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
                if not (opened and up):
                    raise StepFailed(
                        f"a Session stopped and Voice came on with Message off, and no call "
                        f"was up within {LIVE_CALL_OPEN_SECONDS + cool_down:.0f}s — dialled: "
                        f"{opened}, up: {self._call_line()!r}"
                    )
                # **The address is read again here, never carried.** A row is
                # re-keyed as the lane learns more about it — a Claude row is
                # matched on its session id and its pid may be read differently
                # between passes (`sessions.py::_better_known`) — so the address
                # taken when the Session first reached the roster can name a row
                # the registry no longer holds. Run `20260903T111253Z` is that:
                # `relay` refused with `unknown Session`.
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
                if not asked.ok:
                    raise StepFailed(f"the relay that arms this phase was refused: {asked.text}")
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
                if not held.ok or receipt.get("state") != str(Lifecycle.RETAINED):
                    raise StepFailed(
                        f"the words meant to wait out the ceiling were answered "
                        f"{held.text!r}, and not as `{Lifecycle.RETAINED}` — the Session's "
                        "Reply Window was open after all"
                    )
                announced = support.wait_for(
                    lambda: bool(
                        support.matching_lines(
                            self._log_since(holding), MID_CALL_UNDELIVERED_PATTERN
                        )
                    ),
                    # **The ceiling, and then a whole gap.** The word waits for
                    # one judged on both sides, and the Voice's own answer is
                    # what it usually waits behind: run `20260903T112830Z`
                    # passed the ceiling at 23:41:16 and was spoken at 23:44:12,
                    # because that call's playout took the full 180s drain
                    # (`realtime/webrtc.py`) to report its stop edge. The budget
                    # is therefore a whole answer, as every other wait on a
                    # speaking call here is.
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
        self.journey.observe(
            "live call a relay that finally failed",
            f"a Relay to {address} was held while it waited on a permission and passed its "
            f"{ceiling:.0f}s ceiling; the Voice said: {said or 'nothing recorded'}",
        )
        if not announced:
            raise StepFailed(
                f"a Relay to the Focus Session passed its {ceiling:.0f}s ceiling on a call "
                f"that was up, and no announcement carried it. Engine log tail: "
                f"{self._log_since(holding)[-10:]}"
            )
        if not spoken:
            raise StepFailed(
                f"the brief carried the undelivered Relay and the Voice never said "
                f"anything matching {UNDELIVERED_SPOKEN_PATTERN!r} (#173 §6). What it said: "
                f"{said or 'nothing this call recorded'}"
            )
        return (
            f"a Relay held past {ceiling:.0f}s reached {address} as a spoken brief, and the "
            f"Voice said so in its own words"
        )

    @contextmanager
    def _an_extra_session(
        self, workspace_name: str
    ) -> Iterator[tuple[hand_started.HandStartedSession, Path]]:
        """One more Session of this lane's kind, in its own trusted workspace (#196).

        Started **exactly the way the lane's own is** — the `TAKE_THE_TERMINAL`
        shim on a pty that is the child's controlling terminal — because a Codex
        Session is a daemon-held root that a live terminal in its workspace
        vouches for (ADR 0020, #208), and one started any other way never enters
        the roster at all. That is what failed four times in wave 1.

        **The basename is the caller's**, and it is what the Voice knows this
        Session by: the project half of a Session Name is the workspace
        directory's name (`adapters/agent/_project.py`), so
        `live_call.relay_request` can say one of these out loud and the run can
        grade that nothing ever says the other. The per-lane directory above it
        is what keeps two lanes' extra Sessions apart while they share a run.

        Its own workspace, and its own trust grant: two Sessions in one directory
        would make "which rollout is this Session's" unanswerable for the steps
        after this one, and a workspace neither agent has seen stops at a
        full-screen trust dialog before it registers anything (`TrustGate`).
        """
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
    ) -> str:
        """Phase 4: mid-call news reaches the user two different ways (#196).

        The rule under test is `CONTEXT.md`'s and #196's: while a call is up, a
        wake-worthy event about the **Focus Session** is spoken in the first gap
        on both sides, from a reading taken at that instant; an event about any
        other Session is the EVENT cue and nothing more. Neither has a surface a
        run can see — a cue is a sound and an announcement is one utterance among
        others — so both are read off the engine's own lines.

        **The order is the ticket's: the ring first, then the word.** A Session
        that is not the Focus one stops, which rings; then the Focus Session
        stops again, and its word waits out the interval the ring just stamped
        (`call_keeper.py::_interval_elapsed`) before it is paid into a gap. The
        interval cannot be raced, so it is waited out rather than typed against —
        run `20260903T093813Z` is the step racing it and grading the rule working
        as a failure.

        The Focus Session is already the Focus one when this phase starts: phase
        3 relayed into it, and a Relay is what makes a Session the Focus Session
        (#165 Q2). So nothing here has to arrange that, and the phase is about
        the two kinds of news alone.

        **The ring is graded by name.** Every announcement carries the Session it
        named (`MID_CALL_SPOKEN_LINE`), so what is asserted is that no
        announcement on this call ever names the ringing workspace — which says
        exactly what the rule says rather than sampling an absence.
        """
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
        self._drive_extra_session(focus, focus_at, turn, ASK_A_QUESTION)
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
        self.journey.observe(
            "live call mid-call news",
            f"the Focus Session is {focus_address} in {focus_at} and the ringing one "
            f"{ringing_address} in {ringing_at}; EVENT cues on this call: {rings}; "
            f"announcements after the ring: {[line.strip() for line in announcements] or 'none'};"
            f" the last edge of the Voice before the first of them: {after_silence}",
        )
        if not rang:
            raise StepFailed(
                f"{ringing_address} stopped while {focus_address} was the Focus Session and no "
                f"EVENT cue was played within {turn:.0f}s. The cues this call played: "
                f"{self._cue_order(since=mark)}; the call now: {self._call_line()!r}. Engine "
                f"log tail: {self._log_since(before)[-8:]}"
            )
        if not announced:
            silent = support.matching_lines(since_owing, MID_CALL_NOTHING_PATTERN)
            raise StepFailed(
                f"the Focus Session {focus_address} stopped again on a call that was up and "
                f"the engine never spoke its brief within "
                f"{turn + cool_down + settle + LIVE_CALL_CUE_SECONDS:.0f}s. Its next Stop is "
                f"spoken in the first gap an interval after the last mid-call sound (#196). "
                f"Whether it decided there was nothing to say instead: "
                f"{[line.strip() for line in silent] or 'no such line'}. Engine log tail: "
                f"{since_owing[-10:]}"
            )
        if named_the_ringer:
            raise StepFailed(
                f"a Session that is not the Focus one stopped and the engine spoke its brief "
                f"into the call. The rest of the roster is the EVENT cue and nothing more, and "
                f"the user asks with `{Action.BRIEF}` (#196). The lines naming "
                f"{ringing_name}: {[line.strip() for line in named_the_ringer]}"
            )
        if len(announcements) != 1:
            raise StepFailed(
                f"one Stop about the Focus Session earned {len(announcements)} announcements "
                f"on one call. A word owed is one flag and not a queue (#196): "
                f"{[line.strip() for line in announcements]}"
            )
        if not after_silence:
            raise StepFailed(
                f"the brief was spoken while the Voice's own last edge was still `speaking` — "
                f"there was no gap to speak into, and the wire has no silent mid-call path "
                f"(#175, #196). The announcement: {announcements[0].strip()!r}"
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
    ) -> str:
        """Phase 5: the user hangs up by voice, and what follows the ending is the rule.

        Three things in one phase, because they are one sentence: a call ends,
        and what the system may do next is paced by that ending.

        **The hang-up.** `请你把电话挂了…` goes on the track and the Call Agent has
        to run `bridgectl live` — the one verb its generated instructions say
        ends a call (#193, `core/instructions/agent.py`). Graded rather than
        recorded, and this is where the rule is proved on the wire rather than at
        the generator's interface: before #193 the Call Agent guessed, and run
        `20260902T095448Z` had it guess `live` on one lane and `call end` — not
        an action this engine serves — on the other, from the same instructions.
        A lower bound of "some `bridgectl` run happened" would pass on `call
        end`, which is the state the rule exists to end. Then ENDED is played,
        and the two cues of the whole call are graded in order (#186).

        **The Cool-down.** After any end of a call the system does not dial again
        until the Cool-down has elapsed (`CONTEXT.md`). A **third** Session stops
        on a question inside that window: no dial, and the engine says it owes
        one. When the window elapses the owed dial is paid, exactly once.

        **What "from a fresh reading" can and cannot be graded as, here.** The
        ticket asks that the paid dial's hand-over *name* the third Session,
        "which a replayed event could not". It is not graded, for three reasons,
        and the third is the one that matters. The dial line names **kinds and
        counts and never their words** (`adapters/call/realtime/adapter.py:619`),
        deliberately, because the words are on the wire and in the Session Brief
        the log already carries. Nor does the count separate the two readings —
        `earns_a_brief` gives a whole brief to every Session that is not RUNNING
        (`core/briefing.py:287`), so the third Session was already earning one
        while it sat idle. **And the name would not discriminate even if it were
        there**: the event a replay would replay *is* that Session's own Stop, so
        a replayed hand-over names it exactly as a fresh one does. What separates
        fresh from replayed is state that changed between the event and the dial,
        which is a Keeper state rule and is pinned where it belongs — ADR 0017,
        `tests/test_call_keeper.py`.

        So what is graded is everything the Cool-down rule *does* leave a witness
        for: no dial inside the window, an owed dial said out loud, exactly one
        dial when it elapses, and that dial carrying a Session Brief at all. The
        engine's own `COOL_DOWN_PAID_LINE` says "dialling on a fresh reading",
        and both dials' kinds go into the verdict so a reader can see what each
        carried.

        **The window is read, never assumed.** `bridgectl status` reports the
        Cool-down still to run (#195), so the flip is made against a number the
        engine published and the reading at the moment the Stop is driven goes
        into the verdict — a Stop that landed late is the harness losing a race,
        not the Keeper breaking a rule, and run `20260903T050619Z` is the step
        grading one as the other.

        **And nobody speaks on the call it pays for**, so what closes it is the
        Silence Ceiling — v1's proof, kept: `CEILING_END_LINE` on this lane's own
        configured ceiling, with ENDED played after it.
        """
        opening = support.matching_lines(self._log_since(mark), HAND_OVER_LINE)
        runs_before = len(support.cli_wrapper_runs(self.config.cli_wrapper_log))
        hanging = len(self.engine.log_lines())
        live_call.ask_next(self.config.call_wav_directory, live_call.PLAIN)
        heard = support.wait_for(
            lambda: bool(self._user_speech_lines(since=hanging)),
            deadline_seconds=LIVE_CALL_HEARD_SECONDS + live_call.PLAYLIST_POLL_SECONDS,
            poll_seconds=LIVE_CALL_POLL_SECONDS,
        )
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
        if not heard:
            raise StepFailed(
                f"the hang-up utterance went on the track and the engine never logged the "
                f"user's speech within {LIVE_CALL_HEARD_SECONDS:.0f}s. It carries "
                f"{live_call.REQUEST!r} and the line looked for carries "
                f"{LIVE_CALL_HEARD_SUBSTRING!r}. Engine log tail: {self._log_since(hanging)[-8:]}"
            )
        if not ran:
            raise StepFailed(
                f"the engine heard {self._user_speech_lines(since=hanging)[-1]!r} and the Call "
                f"Agent never ran `{Action.LIVE}`, which is the one verb its generated "
                f"instructions say ends a call (`core/instructions/agent.py`). What it ran "
                f"instead: {self._verbs_run(since=runs_before) or 'nothing at all'}"
            )
        if not went_down:
            raise StepFailed(
                f"the Call Agent ran {hang_up_verbs} and the call was still up "
                f"{LIVE_CALL_END_SECONDS:.0f}s later ({self._call_line()!r})"
            )
        if not ended_cue:
            raise StepFailed(
                f"the call ended on the user's word and no ENDED cue was written within "
                f"{LIVE_CALL_CUE_SECONDS:.0f}s. The user hears a call end however it ended "
                f"(#186). Engine log tail: {self._log_since(hanging)[-8:]}"
            )
        # **The second of #193's two facts.** The Call Agent running `live` and
        # the call ending *because* it ran it are graded separately, because they
        # fail differently: a Call Agent that ran nothing and one that ran `call
        # end` — the invention run `20260902T095448Z` recorded — both leave the
        # call for something else to close, and only this says which happened.
        if ended_by != "agent":
            raise StepFailed(
                f"the Call Agent ran {hang_up_verbs} and the call was ended by the {ended_by}: "
                f"{live_call.observed(self.config.call_observations).end_reason or 'none'}. "
                f"Verbs in order: {self._verbs_run(since=runs_before)}"
            )
        self._graded_the_two_cues(mark)
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
        # **The decision and the dial are two lines, and the second is waited
        # for.** `paid` is the Keeper saying it will dial; the hand-over line is
        # the adapter saying it did, written when the realtime session is up. Run
        # `20260903T063156Z` measured the gap at 5.1s on both lanes, and a count
        # taken at the decision counted nothing on both.
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
        self.journey.observe(
            "live call hang-up, cool-down and ceiling",
            f"the Call Agent hung up with {hang_up_verbs}; {remaining:.0f}s of the "
            f"{cool_down:.0f}s Cool-down were still to run when {waiting_address} was driven; "
            f"the wake was owed: {owed}, paid: {paid}, in {len(paid_dials)} dial(s); the dial "
            f"that opened the call just ended carried {opened_briefs} Session Brief(s) and the "
            f"paid one {paid_briefs}; ended by the ceiling: {by_ceiling}",
        )
        if remaining <= 0:
            raise StepFailed(
                f"the call ended and this phase did not reach the third Session's Stop before "
                f"the {cool_down:.0f}s Cool-down had elapsed — `bridgectl status` already read "
                f"`call: none` with no Cool-down on it. Nothing is proven either way: an event "
                f"outside the window is one the engine is free to dial on, which is what the "
                f"window is for. This is the harness losing a race, not the Keeper breaking a "
                f"rule"
            )
        # **The missing owed line is asked about first, and the order is why.**
        # `dialled_inside` is read after the `owed` wait, so a run where the
        # engine never wrote that line spends the wait's whole budget — long
        # enough for the Cool-down to elapse and its legitimately paid dial to
        # land inside the window this reads. Complaining about the dial first
        # would mis-state such a run as "it dialled inside the Cool-down" when
        # what actually went wrong is the line that never came.
        if not owed:
            raise StepFailed(
                f"a Session stopped with {remaining:.0f}s of Cool-down still to run and the "
                f"engine neither dialled nor said it owed a dial. One event buys one attempt, "
                f"and an event inside a Cool-down marks it owed. Engine log tail: "
                f"{self._log_since(inside)[-8:]}"
            )
        if dialled_inside:
            raise StepFailed(
                f"a Session stopped with {remaining:.0f}s of the {cool_down:.0f}s Cool-down "
                f"still to run and the engine dialled anyway: "
                f"{support.matching_lines(self._log_since(inside), HAND_OVER_LINE)!r}. After "
                f"any end of a call the system does not dial again until the Cool-down has "
                f"elapsed (`CONTEXT.md`, *Cool-down*)"
            )
        if not paid:
            raise StepFailed(
                f"the engine owed a dial and never paid it within {cool_down:.0f}s of Cool-down "
                f"plus {LIVE_CALL_OPEN_SECONDS:.0f}s. An owed dial is paid from a fresh reading "
                f"when the Cool-down elapses (ADR 0017). Engine log tail: "
                f"{self._log_since(inside)[-8:]}"
            )
        if len(paid_dials) != 1:
            raise StepFailed(
                f"the Cool-down elapsed and the engine dialled {len(paid_dials)} times, not "
                f"once: {paid_dials!r}. One wake buys one dial, however many events arrived"
            )
        paid_kinds = _hand_over_kinds(paid_dials[0])
        if SESSION_BRIEF_KIND not in paid_kinds:
            raise StepFailed(
                f"the Cool-down's dial carried {paid_kinds or 'nothing it named'} and no "
                f"{SESSION_BRIEF_KIND} — a Session stopped on a question inside the window, so "
                f"the reading the dial was made on has to hold a brief for somebody: "
                f"{paid_dials[0].strip()!r}"
            )
        if not by_ceiling:
            raise StepFailed(
                f"the Cool-down's dial came up, nobody spoke on it, and the engine never said "
                f"its own Silence Ceiling ended it within {ceiling:.0f}s and the step's "
                f"patience. The call now: {self._call_line()!r}. Engine log tail: "
                f"{self._log_since(inside)[-8:]}"
            )
        if not ceiling_ended_cue:
            raise StepFailed(
                f"the ceiling ended the call and no ENDED cue was written within "
                f"{LIVE_CALL_CUE_SECONDS:.0f}s. The user hears a call end however it ended "
                f"(#186), and the Keeper is what rings it now (#195)"
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
            raise StepFailed(
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
        """Poll while the Voice answers, remembering when it first and last spoke.

        One loop rather than a sequence of `support.wait_for` calls, because the
        two things being measured are not conditions that become true and stay
        true: they are *when* the Voice first and last said anything, and only
        something watching the whole stretch can say. See `_VoiceWatch` for the
        two runs that settled the shape.

        **Two stopping conditions, and the caller picks the second one.** The
        call going down always stops it. `quiet_seconds` adds the other: stop
        once every span the Voice opened has closed and no new edge has arrived
        for that long. The folded walk speaks into its call again after the long
        answer, so it cannot wait for the ending — and it cannot stop at the
        first moment the spans balance either, because codex emits one
        `transcript/done` per turn and run `20260902T170324Z`'s two hundred
        numbers arrived as twenty-three of them. Left `None`, the watch is the
        one #184 wrote: it runs until something takes the call away.

        The poll is a parameter only so that a test of this loop is a test and
        not a wait; every caller here takes the one the rest of the step uses.
        """
        edges = self._voice_speech_edges(since=mark)
        first_voice_at = last_voice_at = time.monotonic() if edges[True] else None
        expiry = time.monotonic() + deadline_seconds
        while time.monotonic() < expiry:
            if self._call_is_down():
                break
            time.sleep(poll_seconds)
            seen = self._voice_speech_edges(since=mark)
            if seen != edges:
                edges = seen
                last_voice_at = time.monotonic()
                if first_voice_at is None:
                    first_voice_at = last_voice_at
            elif (
                quiet_seconds is not None
                and edges[True] > 0
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
        """Every verb the Call Agent put to the control plane, in order.

        `since` is a count of *wrapper lines* and not of verbs, because it is a
        mark a phase took before provoking anything and the wrapper log is what
        it counted: a line carrying nothing but options contributes no verb, and
        slicing the verbs by a line count would drift by one from that line on.

        Recorded rather than graded (see `live_call`). The wrapper logs the whole
        argv, and what identifies the verb is the first word that is not the
        `--socket` the engine's own instructions put in front of it — so an
        invented verb (`call end`, run `20260902T095448Z`) reads as itself rather
        than being silently dropped for not matching anything known.
        """
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
        """Every engine line about what the user said, matched by substring.

        The fragment is a parameter because the two call steps put different
        sentences on the track, and each one looks for its own.

        ASR text is unstable, so the pattern is a fragment of the request rather
        than the request: the probe's three trials came back verbatim
        (`docs/research/probes/20260901T213252Z-agent-hangup.jsonl`), which is
        exactly the kind of luck an assertion may not depend on — and run
        `20260902T093755Z` is where it ran out, with one lane hearing `结束通话`
        and the other `结束通 话` from the same audio.
        """
        return [
            line
            for line in support.matching_lines(self._log_since(since), USER_SPEECH_LINE)
            if _unspaced(carrying) in _unspaced(line)
        ]

    def _voice_said_lines(self, *, since: int = 0) -> list[str]:
        """Every line the realtime adapter wrote down of what the Voice said (#197).

        The assistant transcript, which is the only witness this side has of the
        Voice's own words: the Call seam raises the Voice's half as a *span* and
        never as text (`seams/call.py`), so a step that wants to know what was
        said reads the adapter's log line and nothing else. Stripped, because
        every caller either greps it or prints it.
        """
        return [
            line.strip()
            for line in support.matching_lines(self._log_since(since), VOICE_SAID_PATTERN)
        ]

    def _voice_said_something_carrying(self, fragment: str, *, since: int) -> bool:
        """Whether the Voice's transcript since a mark carries a fragment (#181, #198).

        Spaceless on both sides for `_user_speech_lines`' reason, and for one
        more of its own: the assistant transcript comes back from the same
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

    def _graded_the_two_cues(self, mark: int) -> None:
        """The user heard the call connect and heard it end, in that order (#186).

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
        if complaint:
            raise StepFailed(complaint)

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
        """Whether every span this call opened has closed — the condition, to wait on.

        Not "a stop edge exists": the Voice may answer more than once, and an
        earlier closed span would then stand in for the open one this step is
        waiting on. Not a pairing in time either, because a `done` with no delta
        before it is a real utterance and makes stops legitimately outnumber
        starts. At least one start, and no more starts than stops.
        """

        def finished() -> bool:
            edges = self._voice_speech_edges(since=mark)
            return edges[True] > 0 and edges[False] >= edges[True]

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
        """End whatever call is up, and watch long enough to see none come back.

        **One reading of "down" is not a call that is not about to be up.** A
        dial decided is a call that arrives seconds later — run
        `20260903T063156Z` measured 5.1s between the Keeper saying it would dial
        and the adapter saying it had — so a phase that reads the call down and
        starts work can be holding a call it never asked for by its second
        action. Run `20260903T090555Z` is that, exactly: the Cool-down of the
        phase before elapsed, its owed dial was paid, and the call came up
        1.5 seconds after that phase had cleaned up and gone.

        Called with **Voice off**, which is what makes the watch finite: nothing
        the system does unbidden can dial while it is off (`wake`'s first
        refusal), so what is being waited out is only a dial already in flight.
        """
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

    # --- plumbing ---------------------------------------------------------

    def _ground_truth(self) -> hand_started.GroundTruth:
        """Who the harness started, according to the agent — the oracle, not the product."""
        if self.truth is not None:
            return self.truth
        pid = self.session.pid
        if pid is None:
            raise LaneBlocked("the hand-started command never started")

        def found() -> bool:
            self.truth = self.lane.ground_truth(
                pid, self.config.workspace, self.environment, self.started_at
            )
            return self.truth is not None

        if not support.wait_for(found, deadline_seconds=self.far_side.agent_turn_seconds):
            raise LaneBlocked(
                f"the agent itself never recorded the Session the harness started (pid {pid}, "
                f"{self.config.workspace}). Screen tail: {self.session.screen_tail()[-600:]!r}"
            )
        assert self.truth is not None
        if not self.session.alive:
            raise LaneBlocked(
                f"the hand-started command exited before anything could be asked of it. "
                f"Screen tail: {self.session.screen_tail()[-600:]!r}"
            )
        self.journal("ground.truth", lane=self.lane.name, **vars(self.truth))
        return self.truth

    def _await_child_row(self, before: set[str]) -> tuple[dict | None, list[dict]]:
        """The first child row to appear, and the roster read that last looked.

        **A turn ending is not the child existing**, and reading the roster once
        when `_drive_turn` returns assumed it was. Measured on the run of
        2026-08-27 (`20260827T015022Z`): the Codex parent reported its turn ended
        at 14:03:18, its sub-agent was not spawned until 14:03:30, and the child's
        rollout reached disk at 14:03:25 — so the single read happened twelve
        seconds before there was anything to see, and the step failed saying the
        roster "gained nothing". The child was real: `thread_source: subagent`,
        `parent_thread_id`, depth 1, the same shape as the one an earlier run did
        see. The ordering, not the child, was what differed.

        The cause is #73's: Codex answers `spawn_agent` with a blocking
        `wait_agent`, and a parent blocked in it reads as silent, which
        `_drive_turn` scores as a finished turn. The claude lane cannot show this
        — a Claude parent's transcript is frozen while its child works, so its
        turn does not end early — which is why one lane failed and the other
        never has.

        So this waits, to the deadline the failure message already claimed. It
        arranges nothing the step judges: the three assertions are applied to
        whatever is found, and finding nothing within the window is still a
        failure. **First sighting wins**, because a child is transient and a
        later poll may find it already gone.
        """
        deadline = time.monotonic() + self.far_side.agent_turn_seconds
        rows: list[dict] = []
        while True:
            rows = self._roster_rows()
            mine = _first_child_of(rows, self.lane.agent, before)
            if mine is not None:
                return mine, rows
            if time.monotonic() >= deadline:
                return None, rows
            time.sleep(TURN_POLL_SECONDS)

    def _roster_rows(self) -> list[dict]:
        """Every roster row the engine holds, with every field on it.

        Read from `status` since #187 retired `sessions`. It is the same rows —
        `status` always carried them — and it is the surface that still answers
        "what is this engine holding": workspace, `waiting_for`, the roster
        progress summary, `ChildClassification`. `brief` answers the other
        question, "what should the user be told", and deliberately carries none
        of those; the steps that make a claim about Briefing's words ask `brief`
        for them, beside this.
        """
        data = support.control_plane_payload(
            support.Action.STATUS,
            socket_path=self.config.socket_path,
            journal=self.journal,
            why="the roster payload carries fields no rendered line does",
        )
        rows = data.get("sessions", [])
        return [row for row in rows if isinstance(row, dict)]

    def _row_for(self, rows: list[dict], truth: hand_started.GroundTruth) -> dict | None:
        """The roster row for the Session the harness started — by id, else by pid.

        The session id is the exact key and is used whenever there is one. There
        is not always one: `codex` writes the rollout that names it when the
        first *turn* starts (measured 2026-08-26), so before that the only thing
        either side can agree on is the process. `SessionTarget` carries the pid
        (`seams/identity.py:8-10`) and #74's Codex process fallback discovers
        rows by pid and cwd, so matching on it here is the same join the product
        has to make — not a weaker one the harness invented for itself.
        """
        for row in rows:
            target = row.get("target")
            if not isinstance(target, dict):
                continue
            if truth.session_id and str(target.get("session_id")) == truth.session_id:
                return row
            if not truth.session_id and target.get("pid") == truth.pid:
                return row
        return None

    def _roster_row(self) -> dict | None:
        if self.truth is None:
            return None
        return self._row_for(self._roster_rows(), self.truth)

    def _roster_field(self, field_name: str) -> object:
        row = self._roster_row()
        return row.get(field_name) if row else None

    def _name_now(self) -> str | None:
        row = self._roster_row()
        if row is None:
            return None
        name = row.get("name")
        return str(name) if name else None

    def _own_row(self) -> dict:
        """The roster's row for the Session under test, or the agent's own account of it.

        The engine is not the only thing that knows which Session this is, and
        the walk reads the chat before `roster` has established that it does:
        `drain_boot_notice` runs seconds after the launch turn (#110), where the
        engine's discovery may not have listed the Session yet — `roster` itself
        allows it `DISCOVERY_SECONDS` to.

        Ground truth is the fallback and it is not a weaker one for this
        question. A Session the engine holds no Session Name for is announced by
        its address (`core/sessions.py:529-546`), and the address is exactly what
        the agent's own record gives. `{}` — no roster row and no ground truth —
        is the honest empty answer, and `_await_message_naming` refuses on it.
        """
        row = self._roster_row()
        if row is not None:
            return row
        if self.truth is None:
            return {}
        return {
            "target": {
                "agent": self.lane.agent,
                "session_id": self.truth.session_id or None,
                "pid": self.truth.pid,
            },
            "name": None,
        }

    def _await_message_naming(
        self,
        row: dict | None,
        mark: int,
        *,
        deadline_seconds: float,
        whose: str,
        matching: Callable[..., bool] | None = None,
    ) -> telegram_person.PersonMessage | None:
        """Wait for a bot message that names *that* Session, and never any other.

        The one door every chat read in this walk goes through — the module's
        attribution rule, enforced. `None` stays a legitimate answer, because two
        of this walk's reads assert an absence.

        `matching` narrows what a caller will take **on top of** the rule, never
        instead of it: it is `and`-ed, so no caller can widen its way back to the
        message that made #109.

        The naming forms are resolved **once**, before the wait, and not inside
        the predicate: they come off the control plane, and re-deriving them per
        message per poll would ask the engine for the roster a hundred times over
        a two-minute absence window.
        """
        forms = _naming_forms(row or {})
        if not forms:
            # Nothing to attribute *with*. Louder than a silent `None`, which two
            # of the three callers would read as the absence they wanted.
            raise StepFailed(
                f"nothing in the roster names {whose}, so no message in the chat could be "
                f"attributed to it either way: the row is {row!r}"
            )
        shared = _indistinguishable_from(forms, self._roster_rows(), _address_of(row or {}))
        if shared is not None:
            raise StepFailed(
                f"{whose} is named {list(forms)}, and so is {shared} — a message naming one "
                f"names both, so this run cannot attribute anything in the chat to either"
            )
        self.journal("chat.attribution", lane=self.lane.name, whose=whose, names=list(forms))
        return self.person.await_message(
            mark,
            deadline_seconds=deadline_seconds,
            matching=lambda seen: (
                seen.from_bot
                and _named_in(seen.text, forms)
                and (matching is None or matching(seen))
            ),
        )

    def _await_own_message(
        self,
        mark: int,
        *,
        deadline_seconds: float,
        matching: Callable[..., bool] | None = None,
    ) -> telegram_person.PersonMessage | None:
        """The same, for the Session this walk is walking."""
        row = self._own_row()
        return self._await_message_naming(
            row,
            mark,
            deadline_seconds=deadline_seconds,
            # `roster` is what sets `self.address`, and one caller runs before it
            # (`drain_boot_notice`), so the row says who this is when it cannot.
            whose=f"the Session under test ({self.address or _address_of(row)})",
            matching=matching,
        )

    def _other_traffic(self, mark: int) -> str:
        """What else the bot said in the window, for a human reading a red step.

        A step that failed to find its own message is worth telling apart from a
        step that found nothing at all, and on a machine the bridge covers wholly
        those are different situations with different causes.
        """
        others = [
            f"{message.id}: {message.text!r}"
            for message in self.person.messages_after(mark)
            if message.from_bot
        ]
        return support.flatten(others) if others else "nothing"

    def _record_now(self) -> Path | None:
        """Where the agent's own record of this Session is, at this moment."""
        if self.truth is None:
            return None
        return self.lane.record_now(self.truth, self.started_at)

    def _record_size(self) -> int:
        """How big the agent's own record is — the far side's own measure of work."""
        record = self._record_now()
        return record.stat().st_size if record and record.exists() else 0

    def _question_result(self, answer: str) -> tuple[list[dict], int, str] | None:
        """Find the framed error tool result in Claude's JSONL."""
        record = self._record_now()
        if record is None or not record.exists():
            return None
        rows: list[dict] = []
        for line in record.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
        framed = f"The user answered from GPT-VoiceCoding: {answer}"
        for index, row in enumerate(rows):
            message = row.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            blocks = content if isinstance(content, list) else ()
            answered = any(
                isinstance(block, dict)
                and block.get("type") == "tool_result"
                and block.get("is_error") is True
                and block.get("content") == framed
                for block in blocks
            )
            if answered:
                return rows, index, framed
        return None

    def _question_tool_result_proof(self, answer: str) -> str | None:
        """Describe the transcript row that proves the hook carried the answer."""
        found = self._question_result(answer)
        if found is None:
            return None
        _, index, framed = found
        return f"transcript row {index + 1} has is_error=True and {framed!r}"

    def _question_transcript_proof(self, answer: str) -> str | None:
        """Find an assistant record after the hook answer in Claude's JSONL."""
        found = self._question_result(answer)
        if found is None:
            return None
        rows, index, framed = found
        for later_index, later in enumerate(rows[index + 1 :], start=index + 1):
            if later.get("type") == "assistant":
                return (
                    f"transcript row {index + 1} has is_error=True and {framed!r}; "
                    f"assistant row {later_index + 1} follows"
                )
        return None

    def _drive_turn(
        self, what: str, instruction: Instruction, *, expect_waiting: bool = False
    ) -> Turn:
        """Type an instruction at the keyboard and wait for the turn to be over.

        Over means one of two things, and the caller says which: the agent's own
        record stopped growing (a turn that finished), or the roster says the
        Session is waiting on the user. Both are read from outside; neither is
        the screen.
        """
        started = time.monotonic()
        self.session.submit(instruction.words)
        settled_for = 0.0
        last = self._record_size()
        ended = False
        deadline = started + self.far_side.agent_turn_seconds
        while time.monotonic() < deadline:
            time.sleep(TURN_POLL_SECONDS)
            if expect_waiting and self._roster_field("state") == "waiting":
                ended = True
                break
            size = self._record_size()
            if size != last:
                last, settled_for = size, 0.0
                continue
            settled_for += TURN_POLL_SECONDS
            # A record that has not grown for two polls after growing at all is a
            # turn that is over; before it grows at all there is nothing to settle.
            if size > 0 and settled_for >= TURN_SETTLE_SECONDS:
                ended = True
                break
        return self._measured(what, started, ended)

    def _measured(self, what: str, started: float, ended: bool) -> Turn:
        """Record one turn on the walk, whoever asked for it."""
        turn = Turn(what, time.monotonic() - started, ended)
        self.turns.append(turn)
        self.journal("turn", lane=self.lane.name, what=what, seconds=turn.seconds, ended=turn.ended)
        return turn

    def _resolve_approval_effect(
        self,
        *,
        scenario: str,
        requirement: approval_effect.ApprovalRequirement,
        instruction: Instruction,
        mark: int,
        effect_seconds: float,
        announcement_seconds: float | None = None,
    ) -> approval_effect.Resolution:
        """Adapt this walk's real far sides into #146's one resolution interface."""
        if self.address is None:
            raise LaneBlocked(
                f"no Session address is available for {scenario} approval/effect resolution"
            )

        def await_announcement(deadline_seconds: float) -> approval_effect.Announcement | None:
            announced = self._await_own_message(
                mark,
                deadline_seconds=deadline_seconds,
                matching=lambda seen: bool(APPROVAL_ANNOUNCEMENT.search(seen.text)),
            )
            if announced is None:
                return None
            return approval_effect.Announcement(
                f"announced as chat message {announced.id} ({announced.text!r})"
            )

        def answer_approval(approval_id: str) -> approval_effect.ApprovalAnswer:
            answer = self.bridgectl("approve", approval_id, "allow")
            return approval_effect.ApprovalAnswer(answer.ok, f"approve answered {answer.text!r}")

        def journal(event: str, **fields: object) -> object:
            return self.journal(event, lane=self.lane.name, what=scenario, **fields)

        return approval_effect.resolve(
            requirement=requirement,
            session_address=self.address,
            deadlines=approval_effect.Deadlines(
                resolution_seconds=self.far_side.agent_turn_seconds,
                announcement_seconds=(
                    announcement_seconds
                    if announcement_seconds is not None
                    else self.far_side.telegram_round_trip_seconds
                ),
                effect_seconds=effect_seconds,
                poll_seconds=APPROVAL_EFFECT_POLL_SECONDS,
            ),
            collaborators=approval_effect.Collaborators(
                effect=lambda: instruction.performed_in(self.config.workspace),
                pending_approvals=self._pending_approvals,
                await_announcement=await_announcement,
                answer_approval=answer_approval,
                journal=journal,
                monotonic=time.monotonic,
                wait=time.sleep,
            ),
        )

    def _pending_approvals(self) -> tuple[approval_effect.PendingApproval, ...]:
        """Every pending dialog, read off the roster rows that carry one (#191).

        `status` retired its `pending_approvals` list at protocol 8, because a
        pending permission is what the row is waiting on, and its `approval_id`
        is the handle `bridgectl approve` answers with. A permission row whose
        handle is gone is a dialog that went back to the keyboard, and it is
        deliberately not offered here — correlating one would send the walk to
        answer something no verdict can reach.

        **The row's `state` is not read, and that is the Codex lane's whole
        case.** A Codex thread stays `active` while its prompt is on screen —
        the turn has not ended — so the row says `running` and carries the
        dialog's handle at the same time, which is exactly the state a verdict
        is wanted in. `answer_approval` reads the handle and not the state for
        the same reason (`core/bridge.py::_dialog_on_the_roster`), and a filter
        here that the product does not apply would make this harness answer a
        different question from the one the product answers.
        """
        data = support.control_plane_status(self.config.socket_path, self.journal)
        pending: list[approval_effect.PendingApproval] = []
        for row in data.get("sessions", []):
            if not isinstance(row, dict):
                continue
            waiting = row.get("waiting_for")
            if not isinstance(waiting, dict) or waiting.get("kind") != "permission":
                continue
            approval_id = waiting.get("approval_id")
            if approval_id is None:
                continue
            pending.append(
                approval_effect.PendingApproval(
                    approval_id=str(approval_id),
                    session_address=_address_of(row),
                )
            )
        return tuple(pending)


def _address_of(row: dict) -> str:
    """`agent:session_id[:pid]`, the way `seams/identity.py::address_of` writes it.

    **The id half is written as nothing at all where there is none**, not as the
    word `None` — that is the product's own rule (#73), and it is what
    `commands.parse_address` reads back as "not named yet". This mirrored it
    without that clause until #189, which is when the shape started being read
    rather than only compared: `_naming_forms` names an unnamed Session by this
    address, because that is what a Session Brief's header prints.
    """
    target = row.get("target")
    if not isinstance(target, dict):
        return "<no target>"
    session_id = target.get("session_id")
    pid = target.get("pid")
    tail = f":{pid}" if pid else ""
    return f"{target.get('agent')}:{session_id or ''}{tail}"


def _first_child_of(rows: list[dict], agent: str, before: set[str]) -> dict | None:
    """The first Child Process row **this lane's agent** produced, new since `before`.

    The agent is the whole point. Two lanes run at once, each with its own engine,
    and an engine bridges *every* Session on the machine — so the Codex lane's
    child has a row on the Claude lane's roster too, and it is a real, correctly
    classified child, which is what makes it dangerous. Run `20260902T065340Z`
    measured it: the Claude lane's `child` step took `codex:01a060e9-…` (the Codex
    lane's child, which the Codex lane's own step passed on) and failed it for
    being listed under a Codex parent. It could not happen before that run,
    because until #208 the Codex lane had no roster row at all and so left no
    child on anyone's roster.

    **The lane's own agent, and not its own Session id**, deliberately. Filtering
    on the parent would make the step's own assertion — "the child is listed under
    the Session that started it" — unfalsifiable, since only rows that already
    satisfied it could be graded. The agent is the coarsest key that separates the
    two lanes, and it leaves every claim the step makes able to fail.

    `before` carries #79's other rule: first *new* sighting wins, because a child
    is transient and the roster held before the turn is not evidence of this one.
    """
    for row in rows:
        target, child = row.get("target"), row.get("child")
        if not isinstance(target, dict) or not isinstance(child, dict):
            continue
        if target.get("agent") != agent or child.get("kind") != "child":
            continue
        if _address_of(row) in before:
            continue
        return row
    return None


def _naming_forms(row: dict) -> tuple[str, ...]:
    """Every string the product would name one Session by, from its roster row.

    Mirrors `core/sessions.py:529-546` rather than importing it: a harness that
    asked the product what it had said would agree with the product by
    construction, and the whole point of reading the chat is that it might not.

    Every form is kept because the product chooses between them by what it holds
    *at the moment it speaks* — `spoken_name` where the Session has a Session
    Name, `spoken_target` where it does not — and the roster read that answers
    this question is a different moment from the one the message was composed in.
    A Codex Session, in particular, has no name until its first turn.

    **The address is the third form**, and it is what a Stop Notice uses since
    the notice became a Session Brief (#189): `Briefing`'s header line is
    `<name> — <address> — <state>`, and its address half is
    `seams/identity.py::address_of`'s `agent:session_id[:pid]`, not the
    space-separated `spoken_target`. A run whose Session has no name yet — every
    Codex thread before its first turn — is named by that and by nothing else,
    so a harness that did not know the shape could attribute none of its notices.
    Written through `_address_of`, which is this module's one mirror of that
    format: two spellings of one rule are two rules the moment either is edited.
    """
    target = row.get("target")
    target = target if isinstance(target, dict) else {}
    forms: list[str] = []
    name = row.get("name")
    if name:
        forms.append(str(name))
    agent = target.get("agent")
    session_id = target.get("session_id")
    pid = target.get("pid")
    if agent and session_id:
        forms.append(f"{agent} {session_id}")
    elif agent and pid:
        forms.append(f"{agent} pid {pid}")
    if agent and (session_id or pid):
        forms.append(_address_of(row))
    return tuple(forms)


def _named_in(text: str, forms: Sequence[str]) -> bool:
    """Does this message name one of these Sessions? The attribution rule's whole test.

    Substring, not equality: the product's own words wrap the name in a brief it
    composes (`core/briefing.py::_headline`), and what that brief says is the
    product's business rather than this harness's.
    """
    return any(form in text for form in forms)


def _indistinguishable_from(forms: Sequence[str], rows: Sequence[dict], mine: str) -> str | None:
    """Another Session in the roster that a message naming *this* one would also name.

    A Session Name is `<project> · <task>` and **nothing makes it unique**
    (`adapters/agent/_naming.py:39-62` composes it from a project and a task and
    checks neither against the other rows). So two Sessions on one machine can be
    called the same thing, and the product already knows it: `match_name` refuses
    with `AmbiguousNameError` rather than picking one of them
    (`core/sessions.py:456-463`). This is that same fact, met from the chat.

    **The pair it is not is a Child Process and its parent.** A child carries no
    Session Name at all — `core/sessions.py:225` keeps `name` for main Sessions
    and gives a child `None` — so its only naming form is its address, which is
    unique by construction. The `child` step is therefore safe from this by
    #78/#79's own design rather than by luck, which is worth saying because a
    parent announced *while* the child must not be is exactly the shape a
    collision would ruin.

    Where that happens the run cannot attribute either way, and the honest answer
    is to say so rather than pick: a message accepted would be a guess and a
    message rejected would be a guess too. That is the whole lesson of #109 —
    a step that passes for a reason it cannot name has not passed.

    **The containment test runs one way, and the direction is the whole point.**
    A message about the other Session carries *their* form, so this Session
    misreads it exactly when its own form is inside theirs — `mine in theirs`.
    The reverse, theirs inside mine, is safe here and must not refuse: their
    notice does not carry this Session's longer form, so nothing is misread, and
    refusing would turn a soluble case into a red. The Session that *is* at risk
    in that pair asks this same question from its own side and gets the answer
    then. Both directions are held by `tests/test_journey_attribution.py`.
    """
    for other in rows:
        if _address_of(other) == mine:
            continue
        theirs = _naming_forms(other)
        if any(form in one for form in forms for one in theirs):
            return f"{_address_of(other)}, which the roster names {list(theirs)}"
    return None

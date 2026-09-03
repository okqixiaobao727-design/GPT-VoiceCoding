"""Briefing — the one source of words about what a Session is doing.

Pure, in-process, and fed the roster rows Bridge Core has already folded: no
I/O, no clock, no lane. Six functions and nothing else —

    briefing.roster(sessions, focus)  -> RosterBrief
    briefing.session(session)         -> SessionBrief
    briefing.omitting_newest(brief)   -> SessionBrief
    briefing.text(brief)              -> str
    briefing.spoken(brief)            -> SpokenBrief
    briefing.handover(sessions, ...)  -> tuple[HandoverItem, ...]

`spoken` and `handover` are the Live Call's two: the first hands one brief across
the Call seam as the seam's own carrier (no Core type crosses a seam, ADR 0001),
the second builds everything a system-dialled call opens holding. Both are still
only words about Sessions — the carriers are filled with the wording below, and
the adapter that puts them on the wire chooses none of it.

`text` is the **only** text renderer for Session state. The Companion Channel,
the engine log and ``bridgectl brief`` all print what it returns, so there is
one place the wording lives and no two surfaces can describe one Session two
ways. That was the defect #166 named: six renderers, each with its own half of
the vocabulary, and a Session that read as *waiting* on one surface and *idle*
on another.

**The engine never condenses.** `newest` is the newest assistant message whole,
under ADR 0016's omission rules, and the decision carries the prompt, every
option and any recommendation. The one-line conclusion the user hears and the
detail they may ask for are the *same field*: the Voice condenses it for a
brief and reads it whole for detail, and the channel shows it whole. An engine
that summarised would be deciding what the user is told, in a place where the
decision cannot be reviewed.

**A brief is derived from one row, never remembered.** Every value here comes
off the `Session` it was handed, so a brief taken now says what is true now —
legacy's "exactly one fetch, read at the moment you speak"
(`legacy@1d32845:skill/announcing.md` step 1, `bridge/host.py:399-405`),
**ported**. The aggregate Roster Brief and the Focus Session are **new**:
legacy ran one job at a time and had neither (`bridge/coordinator.py:836-838`).
FINISHED is legacy's "finished its turn and is waiting for the user"
(`bridge/host.py:226-234`), **ported**; `decision.recommendation` is its single
recommendation (`bridge/transcript.py:1736-1741`), **ported**.

Module-level functions rather than a class, because there is no state to hold:
the caller writes `briefing.roster(...)`, which reads the same as `#166`'s
`Briefing.roster(...)` and cannot grow a constructor.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Final

from gpt_voicecoding.core.sessions import Session
from gpt_voicecoding.seams.agent import (
    ProgressAvailability,
    ProgressObservation,
    ProgressOmission,
    ProgressPhase,
    ProgressRole,
    ReplyWindow,
    SessionState,
    WaitingKind,
    derive_reply_window,
)
from gpt_voicecoding.seams.call import (
    HANDOVER_BUDGET_BYTES,
    MAX_HANDOVER_ITEMS,
    DialReason,
    HandoverItem,
    SpokenBrief,
    SpokenRosterBrief,
)
from gpt_voicecoding.seams.identity import AgentKind, SessionName, SessionTarget


class BriefState(StrEnum):
    """What one Session is doing, in the five words the user is ever told.

    Deliberately not `SessionState`: that is the agent's own vocabulary for a
    lifecycle (`running`, `idle`, `waiting`), and these five are what the user
    is owed — three of them actionable, one of them the honest admission that
    something could not be read.
    """

    #: A question is waiting for the user, or a Codex turn ended (#166 B2).
    DECISION = "decision"
    #: A permission dialog is open.
    PERMISSION = "permission"
    #: This turn is done and the Session is idle for a new instruction (Q7).
    FINISHED = "finished"
    #: Mid-turn. Nothing is being asked of the user.
    RUNNING = "running"
    #: It stopped, and what it stopped on or what it said could not be read.
    #: **Never counted as a decision** (#166 B7): the brief carries whatever was
    #: read, and says plainly what it could not.
    UNREADABLE = "unreadable"


class NewestState(StrEnum):
    """Whether the newest assistant message is here, and if not, why not."""

    #: It is here, whole.
    SAID = "said"
    #: The source answered and the Session has said nothing yet.
    NOTHING_SAID = "nothing_said"
    #: Nobody looked. Not the same fact as having looked and failed.
    NOT_READ = "not_read"
    #: Somebody looked and the source could not be read.
    UNREADABLE = "unreadable"
    #: It exists and is too large to carry whole — and text is never sliced.
    OVERSIZE = "oversize"


#: The omission wording, in one table. Every surface that says why a message is
#: absent says it in these words: the same sentence in two renderers is the same
#: sentence only until one of them is edited.
NEWEST_WORDING: Mapping[NewestState, str] = {
    NewestState.NOTHING_SAID: "nothing said yet",
    NewestState.NOT_READ: "not read",
    NewestState.UNREADABLE: "could not be read",
    NewestState.OVERSIZE: "the newest entry is too large to carry",
}

#: How each state is said. The three spoken states are #165 Q1's own words.
STATE_WORDING: Mapping[BriefState, str] = {
    BriefState.DECISION: "waiting for your decision",
    BriefState.PERMISSION: "requesting permission",
    BriefState.FINISHED: "finished",
    BriefState.RUNNING: "running",
    BriefState.UNREADABLE: "unreadable",
}


@dataclass(frozen=True, slots=True)
class Newest:
    """The newest assistant message, whole — or the named reason it is not here."""

    state: NewestState
    text: str | None = None

    def __post_init__(self) -> None:
        if (self.state is NewestState.SAID) != (self.text is not None):
            raise ValueError("a newest message is either carried whole or named as absent")

    @property
    def words(self) -> str:
        """What a renderer prints for it — the message, or why it is missing."""
        return self.text if self.text is not None else NEWEST_WORDING[self.state]


@dataclass(frozen=True, slots=True)
class BriefOption:
    """One answer a Session offered, as the user will hear it."""

    text: str
    description: str | None = None
    recommended: bool = False


@dataclass(frozen=True, slots=True)
class Decision:
    """What the Session is waiting on, whole.

    Two shapes in one type, because a brief carries exactly one of them and a
    consumer branches on the `BriefState` it came with: a question carries
    `prompt`, `options` and `recommendation`; a permission carries `tool` and a
    one-line `summary`.
    """

    prompt: str | None = None
    options: tuple[BriefOption, ...] = ()
    recommendation: str | None = None
    tool: str | None = None
    summary: str | None = None


@dataclass(frozen=True, slots=True)
class SessionBrief:
    """What the system knows about one Session, structured for telling the user."""

    target: SessionTarget
    name: SessionName | None
    agent: AgentKind
    state: BriefState
    newest: Newest
    #: `None` when nothing is being asked — a running or finished Session.
    decision: Decision | None
    #: Whether the user can answer this from here, rather than at the terminal.
    answerable_here: bool
    last_activity_at: datetime | None


@dataclass(frozen=True, slots=True)
class RosterRow:
    """One header row: name, agent, state — and the address to ask about it by."""

    target: SessionTarget
    name: SessionName | None
    agent: AgentKind
    state: BriefState
    #: Whether this is the Focus Session. Exactly one row may carry it.
    focus: bool = False


@dataclass(frozen=True, slots=True)
class RosterBrief:
    """How many Sessions are in each state, and one header row for each.

    `counts` is **the others** whenever there is a Focus Session (#165 Q6): the
    Focus Session is spoken first and by name, so counting it again would be the
    same Session told twice.
    """

    counts: Mapping[BriefState, int]
    rows: tuple[RosterRow, ...]
    focus: SessionTarget | None = None


# ----------------------------------------------------------------------
# The three verbs.
# ----------------------------------------------------------------------


def roster(sessions: Sequence[Session], focus: SessionTarget | None) -> RosterBrief:
    """Counts per state and one header row per live Session, Focus first.

    **Exited Sessions appear nowhere** (#165 Q7), and neither does a Child
    Process. Two reasons, and they are the same one: every row here is one the
    model may ask `brief <address>` about, and a child is refused as a target;
    and `CONTEXT.md`'s *Child Process* is a row that gets "no Relay, no Stop
    Notice, no name" — a Roster Brief is what the user is *told*, and a child is
    seen rather than spoken about (#68). The registry still lists it and
    `status` still carries it, which is where "appears in the roster" is true.
    The menu-bar panel already counts the user-facing roster this way
    (`shell/Sources/ShellCore/ControlPanel.swift:44`).
    """
    rows = tuple(
        _row(session, focus=session.target == focus)
        for session in sessions
        if session.is_live and session.child.is_main
    )
    ordered = tuple(sorted(rows, key=lambda row: not row.focus))
    counts: dict[BriefState, int] = {}
    for row in ordered:
        if row.focus:
            continue
        counts[row.state] = counts.get(row.state, 0) + 1
    return RosterBrief(
        counts=counts,
        rows=ordered,
        focus=next((row.target for row in ordered if row.focus), None),
    )


def session(session: Session, *, question_answerable: bool = False) -> SessionBrief:
    """One Session, briefed from the row as it stands.

    `question_answerable` is the one fact a row cannot carry: whether the lane
    still holds the exact prompt and can route the next Answer Relay into it
    (`seams/agent.py::derive_reply_window`). It is a live adapter reading, so
    the hub passes it in rather than this module inventing it — the default is
    the safe one, because a question announced as answerable from here and
    answerable only at the terminal is worse than no announcement at all.
    """
    state = _state(session)
    return SessionBrief(
        target=session.target,
        name=session.name,
        agent=session.target.agent,
        state=state,
        newest=_newest(session.progress),
        decision=_decision(session),
        answerable_here=_answerable_here(session, question_answerable=question_answerable),
        last_activity_at=session.last_activity,
    )


def omitting_newest(brief: SessionBrief) -> SessionBrief:
    """The same brief with its newest message named as too large to carry.

    ADR 0016's rule, applied where the wire is measured: an entry that will not
    fit is **named as omitted and never sliced**, so the user is told the
    message exists and could not be carried rather than handed half of it. The
    header, the state and the whole decision stay — they are what the user acts
    on, and they are small.

    Here rather than in the publisher because the wording is Briefing's: this is
    the one place that puts words to a message that is absent.
    """
    return replace(brief, newest=Newest(state=NewestState.OVERSIZE))


def spoken(brief: SessionBrief) -> SpokenBrief:
    """One Session Brief as the Call seam carries it, in this module's own words.

    The Core type may not cross a seam (ADR 0001), so what crosses is the seam's
    `SpokenBrief` — and it is filled with the wording tables above rather than
    with raw values, so that the Voice hears the same five state words the
    Companion Channel and the log print. An adapter downstream assembles these
    strings into a wire item and chooses none of them.

    The address does not travel. A `SessionTarget` is how *this* process names a
    Session; the Voice names it the way the user does, which is the Session Name
    where there is one and the address only where there is not
    (`core/sessions.py::spoken_name`, the same rule `_headline` follows).
    """
    return SpokenBrief(
        name=str(brief.name) if brief.name is not None else str(brief.target),
        agent=str(brief.agent),
        state=STATE_WORDING[brief.state],
        newest=brief.newest.words,
        decision=tuple(line.strip() for line in _decision_lines(brief)),
        answerable_here=_answer_wording(brief.answerable_here),
        last_activity_at=_when(brief.last_activity_at),
    )


def handover(
    sessions: Sequence[Session],
    focus: SessionTarget | None,
    *,
    reason: str,
    answerable: Collection[SessionTarget] = (),
) -> tuple[HandoverItem, ...]:
    """Everything a system-dialled call opens holding, inside the wire's ceilings.

    Three kinds of item, in one order the Voice can read down: why the call was
    dialled, then the Roster Brief, then the full brief of every Session that
    needs the user — Focus first, because the roster it is taken from is ordered
    Focus first (#165 Q6). **A running Session gets its header row and nothing
    more:** it is in the roster, and there is nothing it is asking of anybody.

    **Over budget, bodies go and words stay.** The newest message is dropped
    from the *back* — the Sessions the roster ordered last — and each one that
    goes is named as omitted rather than left absent (`omitting_newest`, ADR
    0016): the user is told a message exists and could not be carried, never
    handed half of it. The header and the whole decision stay, because they are
    what the user acts on and they are small.

    **Then the roster's header rows go, before any brief does.** A Session that
    has a full brief is already named in it, so its header row is the one thing
    in a hand-over that says nothing twice — and a ladder that spent briefs
    first bought header rows with decisions. Measured: two hundred waiting
    Sessions used to come out as one hundred and fifty-four header rows and no
    briefs at all, which is every decision in the roster dropped to keep a list
    of names. Whole briefs go last, from the back.

    **The counts never go, at any scale.** They are the summary ADR 0016 asks
    for: with them, a hand-over that could carry thirty-five of two hundred
    waiting Sessions still says two hundred are waiting, so what it could not
    carry is *named* rather than silently absent — and named without a fourth
    kind of item on the seam #195 and #196 build on. The reason never goes
    either.

    Both ceilings are the wire's, and both are hard rejections there rather than
    truncations (`seams/call.py`), so this returns a hand-over that fits and
    `Dial` asserts that it does.

    **Which Sessions are briefed is the roster's answer, and only the
    roster's.** An earlier draft let the caller name the Session a call was
    dialled about and briefed it whatever the row said, which put that Session
    ahead of the Focus Session; a later one took the brief the provoking Stop had
    read and appended it last, because `sessions.set_stop_reading` used to leave
    a row that merely ended a turn in `RUNNING` (#209) and a running Session is
    briefed by nothing here — so a call dialled by that Stop could say a Session
    needs the user and never mention which. Since #213 the row itself says the
    Session stopped, so the roster answers for it too and no caller passes a
    brief in beside it.

    `answerable` is the one fact a row cannot carry, as in `session`: the targets
    whose question the lane can still route an Answer Relay into. A live adapter
    reading, so the hub passes it in and the default is the safe one.
    """
    summary = roster(sessions, focus)
    by_target = {live.target: live for live in sessions}
    briefs = [
        session(by_target[row.target], question_answerable=row.target in answerable)
        for row in summary.rows
        if row.state is not BriefState.RUNNING and row.target in by_target
    ]
    return _fitted(DialReason(text=reason), summary, briefs)


def text(brief: SessionBrief | RosterBrief) -> str:
    """The one rendering of a brief: labelled lines carrying every field.

    Nothing is dropped for brevity. The Voice condenses what it is given; a
    channel and a log show the whole thing, and an engine that shortened here
    would take that choice away from both.
    """
    if isinstance(brief, RosterBrief):
        return "\n".join(_roster_lines(brief))
    return "\n".join(_session_lines(brief))


# ----------------------------------------------------------------------
# Reading one row.
# ----------------------------------------------------------------------


def _row(session: Session, *, focus: bool) -> RosterRow:
    return RosterRow(
        target=session.target,
        name=session.name,
        agent=session.target.agent,
        state=_state(session),
        focus=focus,
    )


def _state(session: Session) -> BriefState:
    """The five states, read off one row.

    Order matters. A **running** Session stays RUNNING however its progress
    read went: the state is the lifecycle's, and the read only fills the fields
    — a Session that is working is not a Session that stopped on something.
    Everything else has stopped, and a stop nobody could read is UNREADABLE
    before it is anything else (#166 B7).
    """
    if session.state is SessionState.RUNNING:
        return BriefState.RUNNING
    if (
        session.progress.availability is ProgressAvailability.UNREADABLE
        or session.waiting_for.kind is WaitingKind.UNKNOWN
    ):
        return BriefState.UNREADABLE
    match session.waiting_for.kind:
        case WaitingKind.QUESTION:
            return BriefState.DECISION
        case WaitingKind.PERMISSION:
            return BriefState.PERMISSION
        case _:
            return _turn_ended(session)


def _turn_ended(session: Session) -> BriefState:
    """A Session that stopped and is waiting on nothing this reader can name.

    On the Claude lane that is FINISHED: the turn is done and the Session is
    idle for a new instruction (#165 Q7), which is legacy's own sentence
    (`legacy@1d32845:bridge/host.py:226-234`), **ported**. Its question is
    structural — a tool call the adapter reads — so a Claude turn that ended
    without one ended without one, and there is nothing here to guess.

    On the Codex lane the default is DECISION (#166 B2), and a turn is promoted
    out of it only on the evidence in `_asking`. The lane is asked here rather
    than the answer being stored on the row, because it is Briefing's reading
    and not the lane's observation.
    """
    if session.target.agent is AgentKind.CLAUDE:
        return BriefState.FINISHED
    answer = _final_answer(session.progress)
    if answer is None or _asking(answer):
        return BriefState.DECISION
    return BriefState.FINISHED


# ----------------------------------------------------------------------
# Did the Codex turn end on a question? (#188, on the evidence in #176.)
# ----------------------------------------------------------------------

#: What the user was shown, once what they were not is taken out: a fenced code
#: block, an inline code span, and the target half of a markdown link (the label
#: is kept, because the label is words they read). Measured: stripping code
#: spans alone removes the one false positive a raw match makes, a `done` answer
#: containing the literal `?? uv.lock` inside backticks (#176 §5, A → B).
_FENCED: Final = re.compile(r"```.*?```|~~~.*?~~~", re.DOTALL)
_CODE_SPAN: Final = re.compile(r"`+[^`]*`+")
#: One level of balanced parentheses inside the target, because a URL may hold
#: a pair — `…/path_(part)?run=7` — and a target cut at the first `)` leaves its
#: query string standing in the prose, where `?` reads as an ask.
_LINK: Final = re.compile(r"\[([^\]]*)\]\((?:[^()]|\([^()]*\))*\)")
_AUTOLINK: Final = re.compile(r"<[a-z][a-z0-9+.-]*:[^>\s]*>", re.IGNORECASE)

#: An interrogative in either width. The corpus is 98% Chinese, so `？` is not a
#: rare spelling here; the English behaviour of this rule is **uncertain**
#: (#176 §1.2) and errs toward DECISION, which is the cheap direction.
_ASKS: Final = re.compile(r"[?？]")

#: A labelled menu, which asks even when no interrogative survives: a line whose
#: first word is `A`, `B` or `C` against a separator, or a named `选项 X` /
#: `方案 X` anywhere. **No numeric-list clause, deliberately** — Codex is told to
#: number its suggestions (`codex-rs/core/gpt_5_codex_prompt.md:47`), and it also
#: numbers the findings of a review, which is the most common *done* shape in
#: this corpus (#176 §3, §5). A comma is not a separator, so English prose
#: reading "A, B and C" is not a menu.
_OPTION_BLOCK: Final = re.compile(
    r"^[ \t]*(?:[-*+>]\s*)?\**[ABC]\**[ \t]*[)\]】.。:：、\-—–【《]",
    re.MULTILINE,
)
_NAMED_OPTION: Final = re.compile(r"(?:选项|方案)\s*[A-Za-z\d一二三四五六七八九十]")


def _final_answer(progress: ProgressObservation) -> str | None:
    """The newest message the source marked as this turn's answer, if it did.

    **The search stops at the turn it is about.** A progress tail holds several
    turns, and an answer found behind the newest one is the *previous* turn's:
    reading it would brief a turn that is still working, or one that has
    produced only commentary, on words it never said. The newest turn is the
    turn of the newest entry, by the `turn_id` the lane put on it (#210), and
    only entries naming that turn are searched. That boundary is the source's
    own and survives a turn opened by a message with no words in it — a
    `userMessage` carrying only an image leaves no entry at all
    (`adapters/agent/codex/thread_tail.py::_entry`, and the seam refuses an
    entry with nothing said in it), so a turn opened by one that has produced
    only commentary is a DECISION rather than the previous turn's FINISHED.

    **A reading that names no turn keeps the rule it had before.** The Claude
    lane has no turn in a transcript file, and a Codex build may name none, so
    when the newest entry carries no `turn_id` the search stops at the newest
    `USER` entry instead — the boundary that is in the tail already, as the
    entry the user put there. Nothing regresses on a reading without turns; it
    is only the wordless opener that reads past it, which is the case the
    `turn_id` closes.

    Nothing is classified without an answer, and every way of not having one
    stays DECISION: a build old enough to mark no `phase`, a turn that has said
    nothing yet, and a turn whose only message so far is `commentary`. A
    `commentary` newest with this turn's answer behind it is not one of them —
    the answer is what is classified (3 of 669 turns, #176 §2.1) — and reading
    the commentary instead would manufacture a decision out of the mid-turn
    question Codex's own prompt permits there.
    """
    newest_turn = progress.recent[-1].turn_id if progress.recent else None
    for entry in reversed(progress.recent):
        if newest_turn is None:
            if entry.role is ProgressRole.USER:
                return None
        elif entry.turn_id != newest_turn:
            return None
        if entry.phase is ProgressPhase.FINAL_ANSWER:
            return entry.text
    return None


def _asking(answer: str) -> bool:
    """Whether a final answer shows the user is being asked something.

    #176 §5's heuristic C, measured at 86% recall and a 2% false-positive rate
    over 72 hand-labelled finals. It is a **promotion gate**, not a classifier:
    FINISHED is claimed only when this is false, so every shape it cannot read
    keeps #166 B2's default. The tuned phrase list that scored higher on the
    same sample is **not adopted** — it was fitted after reading that sample's
    misses, and its number is not an estimate of anything (#176 §5, D).

    Legacy classified nothing here: `legacy@1d32845:bridge/transcript.py:431-454`
    returned `pending_question=None` for every Codex stop, on the ground that
    "reporting nothing is the honest answer". **Dropped, because** #166 B2
    reversed the default to DECISION, so the choice is no longer between
    guessing and silence but between always claiming a decision and promoting
    out of one on evidence.
    """
    prose = _AUTOLINK.sub(" ", _CODE_SPAN.sub(" ", _LINK.sub(r"\1", _FENCED.sub(" ", answer))))
    return bool(_ASKS.search(prose) or _OPTION_BLOCK.search(prose) or _NAMED_OPTION.search(prose))


def _newest(progress: ProgressObservation) -> Newest:
    """The newest assistant message whole, under ADR 0016's omission rules."""
    if progress.availability is ProgressAvailability.NOT_READ:
        return Newest(state=NewestState.NOT_READ)
    if progress.availability is ProgressAvailability.UNREADABLE:
        return Newest(state=NewestState.UNREADABLE)
    if progress.has_history is False:
        return Newest(state=NewestState.NOTHING_SAID)
    said = next(
        (entry for entry in reversed(progress.recent) if entry.role is ProgressRole.ASSISTANT),
        None,
    )
    if said is not None:
        return Newest(state=NewestState.SAID, text=said.text)
    if progress.omission is ProgressOmission.NEWEST_OVERSIZE:
        return Newest(state=NewestState.OVERSIZE)
    # History exists and this publication carried none of it — the roster
    # summary's own case (`ProgressOmission.STATUS_SUMMARY`), and the case of a
    # tail whose entries are all the user's. Nobody read the message; saying so
    # is the honest answer and the one `NOT_READ` already means.
    return Newest(state=NewestState.NOT_READ)


def _decision(session: Session) -> Decision | None:
    """What it is waiting on, whole — or `None` when it is waiting on nothing.

    Carried even when the state is UNREADABLE, because the brief keeps whatever
    was read: a partial label is worth more to the user than a blank.
    """
    waiting_for = session.waiting_for
    match waiting_for.kind:
        case WaitingKind.QUESTION:
            return Decision(
                prompt=waiting_for.prompt,
                options=tuple(
                    BriefOption(
                        text=option.text,
                        description=option.description,
                        recommended=option.recommended,
                    )
                    for option in waiting_for.options
                ),
                recommendation=waiting_for.recommendation,
            )
        case WaitingKind.PERMISSION:
            return Decision(tool=waiting_for.tool_name, summary=waiting_for.detail)
        case _:
            return None


def _answerable_here(session: Session, *, question_answerable: bool) -> bool:
    """Whether the user's reply can reach this Session from here.

    Two routes, and the second is not a Reply Window: a permission is answered
    by the Approval Relay, and only while the adapter still holds the handle the
    dialog is parked on. A permission whose handle is gone was handed back to
    the terminal, and the user is owed that fact rather than a notice they would
    try to answer and could not.
    """
    if not session.is_live or not session.child.is_main:
        # An ended Session accepts nothing, and a Child Process is seen and
        # never spoken to (#68). Said here rather than left to the window
        # derivation, because the permission route below does not go through it.
        return False
    if (
        session.waiting_for.kind is WaitingKind.PERMISSION
        and session.waiting_for.approval_id is not None
    ):
        return True
    window = derive_reply_window(
        session.state,
        session.waiting_for,
        session.child,
        question_answerable=question_answerable,
    )
    return window is ReplyWindow.OPEN


# ----------------------------------------------------------------------
# Fitting a hand-over into the wire's two ceilings.
# ----------------------------------------------------------------------


def _fitted(
    reason: DialReason,
    summary: RosterBrief,
    briefs: list[SessionBrief],
) -> tuple[HandoverItem, ...]:
    """Give things back, in the order the docstring of `handover` names, until it fits.

    Three rungs, in the order `handover` states — every newest body from the back,
    then the roster's header rows from the back, then whole briefs from the back.
    The loop stops at the first arrangement that fits, so a hand-over that already
    does is returned untouched: the common case, and the one where every body is
    carried whole.

    A header row is given up before a brief because it is the only thing here
    that repeats something already said. A brief is given up last because it is
    the only thing that carries a decision.

    **The reason and the counts are never given up**, at any scale: they are what
    is left when everything else has gone, and the counts are what still say how
    many Sessions the call could not carry (ADR 0016).
    """
    carried = list(briefs)
    rows = list(summary.rows)
    while True:
        items = _handover_items(reason, summary, rows, carried)
        if _within_ceilings(items):
            return items
        if _one_body_less(carried):
            continue
        if _one_row_less(rows, carried):
            continue
        if carried:
            carried.pop()
            continue
        # The reason and the counts alone. Nothing here is the caller's to trim,
        # and both are bounded by their own writers.
        return items


def _one_row_less(rows: list[RosterRow], briefs: list[SessionBrief]) -> bool:
    """Give up one header row, the ones a brief already names first.

    From the back within each group, so the Focus Session's row is the last to
    go — and a row whose Session is briefed goes before any row whose Session is
    not, because the brief says everything the row does and more.
    """
    if not rows:
        return False
    briefed = {brief.target for brief in briefs}
    for index in reversed(range(len(rows))):
        if rows[index].target in briefed:
            rows.pop(index)
            return True
    rows.pop()
    return True


def _handover_items(
    reason: DialReason,
    summary: RosterBrief,
    rows: list[RosterRow],
    briefs: list[SessionBrief],
) -> tuple[HandoverItem, ...]:
    return (
        reason,
        _spoken_roster(summary, rows),
        *(spoken(brief) for brief in briefs),
    )


def _within_ceilings(items: tuple[HandoverItem, ...]) -> bool:
    if len(items) > MAX_HANDOVER_ITEMS:
        return False
    return sum(item.size_in_bytes for item in items) <= HANDOVER_BUDGET_BYTES


def _one_body_less(briefs: list[SessionBrief]) -> bool:
    """Name the last carried newest message as omitted. False when none is left.

    From the back, because the roster ordered these by what the user is most
    likely to be asked about first, and a hand-over that gave up the Focus
    Session's message to keep the last row's would be answering the wrong
    question with the bytes it has.
    """
    for index in reversed(range(len(briefs))):
        if briefs[index].newest.text is not None:
            briefs[index] = omitting_newest(briefs[index])
            return True
    return False


def _spoken_roster(brief: RosterBrief, rows: list[RosterRow]) -> SpokenRosterBrief:
    """The Roster Brief as the Call seam carries it — the same words `text` prints."""
    listed = [row for row in rows if not row.focus]
    focus = next((row for row in rows if row.focus), None)
    return SpokenRosterBrief(
        counts=_counts_line(brief),
        rows=tuple(_row_line(row) for row in listed),
        focus=_row_line(focus) if focus is not None else None,
    )


# ----------------------------------------------------------------------
# The one renderer.
# ----------------------------------------------------------------------


def _session_lines(brief: SessionBrief) -> list[str]:
    lines = [_headline(brief.name, brief.target, brief.state)]
    lines.append(f"  newest: {brief.newest.words}")
    lines.extend(_decision_lines(brief))
    lines.append(f"  answer: {_answer_wording(brief.answerable_here)}")
    lines.append(f"  last activity: {_when(brief.last_activity_at)}")
    return lines


def _answer_wording(answerable_here: bool) -> str:
    """Where the user answers this, in the two phrases both carriers use."""
    return "from here" if answerable_here else "at the terminal"


def _when(last_activity_at: datetime | None) -> str:
    """The last activity stamp, or the admission that nobody read one."""
    return last_activity_at.isoformat() if last_activity_at is not None else "not read"


def _decision_lines(brief: SessionBrief) -> list[str]:
    """The one line, or the several, that say what the Session is waiting on.

    **The state decides which shape this is, not the fields.** `Decision` holds
    two shapes in one type and a permission the roster named without naming its
    tool carries neither half — `WaitingFor(kind=PERMISSION)` off a roster that
    says *waiting* and nothing more (`adapters/agent/claude/waiting_labels.py`).
    Read off the fields alone that is indistinguishable from a question nobody
    could read, and it used to render as one: "asked: it asked you something"
    about a permission dialog. The tool name is then the renderer's floor, which
    is where a name nobody supplied belongs.

    A permission that reaches here under `UNREADABLE` — the progress read failed
    on top of the dialog — is still recognised by its half of the fields, and one
    that carries neither is the residue: it renders as an unreadable ask, which
    is what it is.

    Legacy (ADR 0010): `legacy@1d32845:bridge/host.py:213-235`
    (`SessionStopSpeech.render`) chose "This session is waiting for permission."
    off the Hook **event kind** it was created from, never off whether any field
    about the tool had been read. **Ported** — the same rule, read off the state
    the brief carries rather than off a ledger's event kind. What gen-1 had no
    equivalent of is the tool name and its one-line summary, which this
    generation's `WaitingFor` carries and legacy's line did not: those stay the
    renderer's optional tail, and "a tool" is the floor beneath them.
    """
    decision = brief.decision
    if decision is None:
        return []
    if (
        brief.state is BriefState.PERMISSION
        or decision.tool is not None
        or decision.summary is not None
    ):
        asked = decision.tool or "a tool"
        return [f"  permission: {asked}" + (f" — {decision.summary}" if decision.summary else "")]
    lines = [f"  asked: {decision.prompt or 'it asked you something'}"]
    lines.extend(
        f"  option: {option.text}"
        + (f" — {option.description}" if option.description else "")
        + (" (recommended)" if option.recommended else "")
        for option in decision.options
    )
    if decision.recommendation:
        lines.append(f"  recommends: {decision.recommendation}")
    return lines


def _roster_lines(brief: RosterBrief) -> list[str]:
    lines: list[str] = []
    rows = list(brief.rows)
    if rows and rows[0].focus:
        lines.append(f"focus: {_row_line(rows[0])}")
        rows = rows[1:]
    lines.append(_counts_line(brief))
    lines.extend(f"  {_row_line(row)}" for row in rows)
    return lines


def _headline(name: SessionName | None, target: SessionTarget, state: BriefState) -> str:
    """`<name> — <address> — <state>`, and the name is dropped when there is none.

    A Session with no Session Name is announced by its address
    (`core/sessions.py::spoken_name`), so writing the address where the name
    goes as well would say one thing twice. The agent is not a field of its own
    here either: it is the first half of the address, and a line that spelled it
    out beside it would be the same fact printed twice — the structured brief
    carries `agent` for a consumer that wants it apart.
    """
    named = f"{name} — " if name is not None else ""
    return f"{named}{target} — {STATE_WORDING[state]}"


def _row_line(row: RosterRow) -> str:
    return _headline(row.name, row.target, row.state)


def _counts_line(brief: RosterBrief) -> str:
    """The counts, under the heading that says which Sessions they are.

    **The others**, whenever there is a Focus Session (#165 Q6). One function, so
    the rendered roster and the one the Live Call is handed cannot disagree about
    whether the Focus Session was counted.
    """
    heading = "the others" if brief.focus is not None else "sessions"
    return f"{heading}: {_counts(brief.counts)}"


def _counts(counts: Mapping[BriefState, int]) -> str:
    """Every state that has any Sessions in it, in the order the states are named."""
    said = [f"{counts[state]} {STATE_WORDING[state]}" for state in BriefState if counts.get(state)]
    return ", ".join(said) if said else "none"

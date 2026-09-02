"""Briefing — the one source of words about what a Session is doing.

Pure, in-process, and fed the roster rows Bridge Core has already folded: no
I/O, no clock, no lane. Three functions and nothing else —

    briefing.roster(sessions, focus) -> RosterBrief
    briefing.session(session)        -> SessionBrief
    briefing.text(brief)             -> str

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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from gpt_voicecoding.core.sessions import Session
from gpt_voicecoding.seams.agent import (
    ProgressAvailability,
    ProgressObservation,
    ProgressOmission,
    ProgressRole,
    ReplyWindow,
    SessionState,
    WaitingKind,
    derive_reply_window,
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
    (`legacy@1d32845:bridge/host.py:226-234`), **ported**.

    On the Codex lane it is DECISION. Codex has no question hook, so a turn that
    ended with a question of its own looks exactly like one that ended finished,
    and Simon's ruling (#166 B2) is to brief the ambiguity as the answerable
    state until the heuristic that tells them apart lands — #188, on the
    evidence in #176. The lane is asked here rather than the answer being stored
    on the row, because it is Briefing's reading and not the lane's observation.
    """
    return BriefState.FINISHED if session.target.agent is AgentKind.CLAUDE else BriefState.DECISION


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
# The one renderer.
# ----------------------------------------------------------------------


def _session_lines(brief: SessionBrief) -> list[str]:
    lines = [_headline(brief.name, brief.target, brief.state)]
    lines.append(f"  newest: {brief.newest.words}")
    lines.extend(_decision_lines(brief))
    lines.append(f"  answer: {'from here' if brief.answerable_here else 'at the terminal'}")
    when = brief.last_activity_at.isoformat() if brief.last_activity_at is not None else "not read"
    lines.append(f"  last activity: {when}")
    return lines


def _decision_lines(brief: SessionBrief) -> list[str]:
    decision = brief.decision
    if decision is None:
        return []
    if decision.tool is not None or decision.summary is not None:
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
    heading = "the others" if brief.focus is not None else "sessions"
    lines.append(f"{heading}: {_counts(brief.counts)}")
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


def _counts(counts: Mapping[BriefState, int]) -> str:
    """Every state that has any Sessions in it, in the order the states are named."""
    said = [f"{counts[state]} {STATE_WORDING[state]}" for state in BriefState if counts.get(state)]
    return ", ".join(said) if said else "none"

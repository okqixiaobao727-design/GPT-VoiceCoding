"""The Agent seam — carrying words into a Session, and hearing back from it.

Verbs Bridge Core calls: `answer_relay`, `approval_relay`, `reply_window` and
`verify` (ADR 0003 — liveness is a verb on every pluggable seam).

**The Reply Window is a level, so it is both asked for and reported.** `reply_
window` answers where it stands right now and is asked exactly once, when Bridge
Core enters a Session in its roster; `ReplyWindowChanged` reports every
transition after that. The split is not redundancy — an event cannot bootstrap a
level, because registration happens before Bridge Core holds the Session and a
report raised there is dropped as belonging to a Session nobody knows (#27).

Events raised upward: Session stopped, Session ended, Reply Window changed, and
delivery receipts that arrive asynchronously.

**A pending permission is not an event of its own.** Both adapters fold the
dialog's handle into the Stop's `WaitingFor`, so the Session's PERMISSION state
is what travels and `as_approval_request` is how the Approval Relay addresses it
(#191). A second event for the same dialog only ever asked Bridge Core to
recognise two things as one.

Reply-Window queueing is Bridge Core policy. Adapters deliver; they never queue.

**Deliver and supplement are one verb with a route, not two verbs.** The Relay
grilling fixed two required behaviours — deliver (between turns) and supplement
(mid-turn, with the user's authority intact) — and both are required of both
agents. They are a parameter of `answer_relay` alone, because supplement only
ever carries *user-authored* words, while an Approval Relay is a verdict. A
second verb would duplicate one signature to encode one boolean.

Which routes an adapter really has is reported by `supported_routes`, statically.
An adapter that lacks SUPPLEMENT says so and does nothing else — deciding what to
do instead (queue it as a DELIVER against the Reply Window) is Bridge Core's
policy. Route choice follows the user's explicit intent and is never inferred
from Session status: the same "busy" carries both "add this now" and "this can
wait".

**There is one Session-inspection result type, and it is `SessionInspection`.**
Everything this system knows about one running Session — what it is doing, what
it stopped on, how far along it is, whose child it is — arrives as that one
value, from that one verb. A consumer that needs a fact the type does not carry
**widens the type and both lane projections**; it never grows a sibling reader,
a second seam, or its own parsing of an agent's files. That is the architecture
gate #74 exists to close: the reference implementation had four readers of the
same transcript answering slightly different questions, and no two of them
agreed about a Session that was mid-write.

**No Reach and no Provenance.** An earlier draft graded every row by how the
bridge could reach it and where the row came from. #68 removed that vocabulary
and this seam does not carry it: *every* listed Session is one the bridge talks
to, and a route that cannot be walked surfaces where it fails — as a
`DeliveryReceipt` that is not `DELIVERED` and carries the reason why
(`seams/delivery.py`). A lane that cannot enumerate *at all* is a different
fact, about the lane and not about any row, and it rides on `LaneDiscovery`.

Adapters: Codex and Claude.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol, runtime_checkable

from gpt_voicecoding.seams.delivery import DeliveryReceipt
from gpt_voicecoding.seams.events import Event
from gpt_voicecoding.seams.identity import (
    AgentKind,
    RequestId,
    SessionName,
    SessionTarget,
)
from gpt_voicecoding.seams.verify import VerifyResult


class RelayRoute(StrEnum):
    """How user-authored words reach a Session. Chosen by the user, not inferred."""

    #: Between turns, into an open Reply Window. Always available.
    DELIVER = "deliver"
    #: Mid-turn, authority intact — "the agent is working and I want to add
    #: something". Optional: an adapter may honestly not have it.
    SUPPLEMENT = "supplement"


class ReplyWindow(StrEnum):
    """Whether a Session can accept an inbound Relay as a user turn."""

    OPEN = "open"
    CLOSED = "closed"


class ApprovalVerdict(StrEnum):
    """The user's decision on one pending permission request."""

    ALLOW = "allow"
    DENY = "deny"
    #: Hand it back to the on-screen dialog. This is what a budget expiry
    #: answers — never deny on timeout.
    ASK = "ask"


# ----------------------------------------------------------------------
# The one Session-inspection result type, and the vocabulary it is made of.
# ----------------------------------------------------------------------


class SessionLifecycle(StrEnum):
    """Whether this Session is still there at all."""

    LIVE = "live"
    ENDED = "ended"


class SessionState(StrEnum):
    """What a live Session is doing right now, in the agent's own terms.

    Read straight off the official Claude roster, which moves `idle → busy →
    waiting → idle` across one turn (#73, measured on 2.1.246) — `busy` is
    spelled `running` here because that is the word the product's surfaces use.
    Deliberately *not* a Reply Window: this says what the Session is doing, and
    `derive_reply_window` says what follows from it.
    """

    RUNNING = "running"
    IDLE = "idle"
    WAITING = "waiting"


class WaitingKind(StrEnum):
    """What a Session that stopped is waiting for."""

    #: Not waiting on the user at all.
    NONE = "none"
    #: A question with options, asked of the user.
    QUESTION = "question"
    #: A permission dialog. The Approval Relay's business.
    PERMISSION = "permission"
    #: Something is being waited on and we cannot yet say what. Only honest
    #: alongside `caught_up=False`; see `WaitingFor`.
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Option:
    """One answer a Session offered — the ready-made voice menu, one line of it."""

    text: str
    #: Whether the Session marked this one as its own recommendation. Carried
    #: rather than acted on: the recommendation is the agent's, and the choice
    #: is the user's.
    recommended: bool = False
    #: The Session's own explanation of what choosing this option means, when
    #: it supplied one. Carried beside the label so a surface never has to
    #: reconstruct it from the question or transcript (#151).
    description: str | None = None

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("an option the user could choose must have words")


#: What a permission whose tool has no name of its own is called. Claude Code's
#: `sandbox request` says only that — no tool, no command — so something has to
#: name it, and the seam is where both sides can read the same word: the lane
#: writes it onto `WaitingFor.tool_name` and Briefing renders it as it renders
#: any other tool. It sat in the Claude lane's label table until #187, which is
#: where #166 B6 found it and asked for it to live once; Briefing itself cannot
#: hold it, because an adapter may not import Bridge Core (ADR 0001,
#: `tests/test_architecture.py::test_adapters_never_import_bridge_core`).
SANDBOX_TOOL_NAME: Final = "sandbox network access"


@dataclass(frozen=True, slots=True)
class WaitingFor:
    """What one Session stopped on, or the honest admission that we cannot tell yet.

    **`kind=UNKNOWN` with `caught_up=False` is the one that matters.** It means
    the agent's own record has not flushed the awaited entry — a fresh Session
    has no transcript file at all until it takes a turn (#73) — so the answer is
    *ask again*, never guess. Knowing what a Session is waiting for and not
    having caught up with its record are mutually exclusive, so the pair is
    enforced here rather than remembered: if the kind is known, the reader had
    to have read the record that says so.
    """

    kind: WaitingKind = WaitingKind.NONE
    #: Whether the reader has seen everything the Session has written so far.
    caught_up: bool = True
    #: The question as the Session asked it.
    prompt: str | None = None
    options: tuple[Option, ...] = ()
    #: The Session's own recommendation among its options, when it made one.
    recommendation: str | None = None
    #: The tool a permission dialog is about.
    tool_name: str | None = None
    #: One line summarising the call, for speech.
    detail: str | None = None
    #: The adapter's handle for the pending dialog, when it has one. Absent on a
    #: permission observed from a roster alone: the official roster says a
    #: Session is `waiting` without naming the dialog, and the handle arrives
    #: with the hook that holds it open.
    approval_id: str | None = None

    def __post_init__(self) -> None:
        if not self.caught_up and self.kind is not WaitingKind.UNKNOWN:
            raise ValueError(
                f"a reader that has not caught up with the Session's record cannot also "
                f"know it is waiting on a {self.kind}; that pair is a guess wearing a fact's "
                "clothes"
            )

    @property
    def needs_the_user(self) -> bool:
        """Whether this is something only the user can answer."""
        return self.kind in (WaitingKind.QUESTION, WaitingKind.PERMISSION)

    @property
    def stopped_state(self) -> SessionState:
        """The state a Session that stopped on this wait is in.

        **One rule, in one place, because two readers used to derive it apart.**
        A Session that stopped is not a Session running: a turn that ended asking
        nothing is `IDLE`, which is what `BriefState.FINISHED` means (#165 Q7),
        and anything else is `WAITING` — a question or a permission because only
        the user can end it, an `UNKNOWN` because a wait nobody could read is
        still a stop, and reading it as `WAITING` is what makes Briefing say
        *unreadable* rather than a false *running* (#166 B7).

        Read by `SessionRegistry.set_stop_reading` for a registered row and by
        `bridge.stop_brief` for a Stop whose Session no discovery pass has landed
        yet (#213). Deliberately not a field on `SessionStopped`: the lanes
        observe the wait, and what it implies is this side's rule.
        """
        return SessionState.IDLE if self.kind is WaitingKind.NONE else SessionState.WAITING

    def as_approval_request(self, target: SessionTarget) -> ApprovalRequest | None:
        """The pending permission as the Approval Relay addresses it, if it is one.

        `None` covers both "not a permission" and "a permission nobody has
        handed us a handle for yet" — an Approval Relay has nothing to answer
        into in either case, and saying so here is what keeps every consumer
        from inventing its own half of the check.
        """
        if self.kind is not WaitingKind.PERMISSION or not self.approval_id:
            return None
        return ApprovalRequest(
            approval_id=self.approval_id,
            target=target,
            tool_name=self.tool_name or "",
            detail=self.detail or "",
            options=tuple(option.text for option in self.options),
        )


class ProgressRole(StrEnum):
    """Which side said one progress entry.

    **Carried rather than inferred.** A roster of bare strings reads "make it
    blue" and "I made it blue" the same way, so every surface would have to
    guess — which is the per-consumer parsing #74 exists to end. The reference
    implementation carried the role all the way to the wire
    (`legacy@1d32845:bridge/transcript.py:40-46`), and this is that fact, named.
    """

    USER = "user"
    ASSISTANT = "assistant"


class ProgressPhase(StrEnum):
    """Which part of a turn one thing that was said belonged to.

    This seam's vocabulary, not the source's: the values below are the words
    Bridge Core reads, and the Codex spellings they are mapped from live in one
    table in that lane's reader (`adapters/agent/codex/thread_tail.py`), which
    is the only place a build's own name for a phase is written (ADR 0001 —
    Bridge Core speaks seam verbs, protocol mechanics stay in the adapter).
    Renaming a value back to a source string would put that mechanic here.

    `UNKNOWN` is a member rather than an error (#210). A phase a future build
    invents must cost the reading nothing and read as *not the answer*, because
    the reading rides on a roster row the user is looking at and raising there
    would blank it — so an adapter maps everything it does not recognise here,
    and a lane that marks nothing at all leaves the field `None` instead.
    """

    COMMENTARY = "commentary"
    FINAL_ANSWER = "answer"
    UNKNOWN = "unknown"


class ProgressAvailability(StrEnum):
    """Whether an authoritative progress source was read, and whether it answered."""

    NOT_READ = "not_read"
    UNREADABLE = "unreadable"
    READABLE = "readable"


class ProgressOmission(StrEnum):
    """Why known history is absent from, or incomplete in, one progress view.

    The first four are an *observation's* omission and describe a whole view.
    `OVERSIZE` is the History page's and describes **one entry**: the page keeps
    its slot with its ordinal and role and drops only its text (#171), so an
    entry too large for the encoded Reply never blocks the entries before it. It
    is refused on a `ProgressObservation` for that reason — one entry's omission
    is not a reading's.
    """

    NONE = "none"
    OLDER = "older"
    STATUS_SUMMARY = "status_summary"
    NEWEST_OVERSIZE = "newest_oversize"
    OVERSIZE = "oversize"


@dataclass(frozen=True, slots=True)
class ProgressEntry:
    """One thing that was said in a Session, and by which side.

    Legacy carried `turn_id` and `turn_status` beside them on the Codex lane
    (`legacy@1d32845:bridge/codex.py:1405, 1484-1492, 1516-1520`). `turn_id` is
    **ported** (#210): which turn an entry belongs to is the only boundary that
    survives a turn opened by a message with no words in it — a `userMessage`
    carrying only an image leaves no entry at all, and a reader that finds the
    newest turn by looking for the newest thing the user said reads straight
    past it into the turn before. `turn_status` stays **dropped, because** no
    v1.0 consumer reads it — the Live Call, the Companion Channel and the
    Control Panel ask what a Session last said and what it was last told, never
    how the turn ended — and a running turn is not a special case to the reader
    that carries what it has said so far.

    `phase` is the other field beside them (#188), and it is a `ProgressPhase` —
    this seam's vocabulary rather than the source's. Codex marks each
    `agentMessage` with a word of its own
    (`codex-rs/app-server-protocol/src/protocol/v2/item.rs:249-258`, serialised
    `Option<MessagePhase>`); that lane's reader maps it, anything it does not
    recognise becomes `UNKNOWN`, and a lane that marks nothing leaves this
    `None`. An earlier draft carried the source string raw and argued an enum
    would raise on a roster row (#188); `UNKNOWN` is what answers that instead,
    so nothing is lost and Bridge Core no longer compares a protocol word
    (ADR 0001). Who compares it to what is still the reader's — the tail readers
    set it and never look at it, and Briefing is the one place that asks whether
    a turn ended on its answer (`core/briefing.py`).
    """

    #: Where this entry sits in the Session's visible record, counted **from
    #: the oldest visible entry and starting at 0**, assigned by the lane at
    #: read time (#171). Both sources are append-only for the entries this seam
    #: keeps — a Claude transcript file, a Codex thread's turns — so an ordinal
    #: names the same entry across reads while the Session lives, which is what
    #: makes it usable as the History page's cursor. Required rather than
    #: defaulted: both lanes build every entry before anything trims them, so
    #: there is no reading in which one is unknown, and a default would let a
    #: fixture ship a cursor that points nowhere.
    ordinal: int
    role: ProgressRole
    text: str
    #: Which part of the turn this was, in this seam's vocabulary, or `None`
    #: when the source did not mark it at all.
    phase: ProgressPhase | None = None
    #: Which turn this entry belongs to, named by the source and assigned by the
    #: lane at read time (#210), or `None` when the source has no turn to name.
    #: Opaque here: it is compared to another entry's and never parsed, so it
    #: groups entries without this seam knowing how either source spells one. A
    #: lane whose record has no turn concept — a Claude transcript file is one
    #: long append — leaves it `None`, and so does a Codex turn whose document
    #: named no `id`; a reader that finds no turns falls back to the boundary it
    #: had before (`core/briefing.py::_final_answer`).
    turn_id: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int):
            raise ValueError("a progress entry ordinal is its whole place in the record")
        if self.ordinal < 0:
            raise ValueError("a progress entry ordinal counts from the oldest entry, at 0")
        if not isinstance(self.role, ProgressRole):
            raise ValueError("a progress entry role must use the Agent seam vocabulary")
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("an entry with nothing said in it is not progress")
        if self.phase is not None and not isinstance(self.phase, ProgressPhase):
            raise ValueError("a progress entry phase must use the Agent seam vocabulary")
        if self.turn_id is not None and (
            not isinstance(self.turn_id, str) or not self.turn_id.strip()
        ):
            raise ValueError("a progress entry turn is named by the source, or not named")


class ProgressCapture(Protocol):
    """The publication-owned source capture strategy supplied to an Agent adapter."""

    @property
    def max_bytes(self) -> int:
        """The canonical encoded entry capacity of the largest publication."""

    def select(
        self,
        entries: Sequence[ProgressEntry],
    ) -> tuple[tuple[ProgressEntry, ...], ProgressOmission]:
        """Return the newest whole source tail and name any omission."""


@dataclass(frozen=True, slots=True)
class ProgressObservation:
    """One canonical reading from an Agent's authoritative progress source.

    Availability is explicit because no source read, a failed source read and a
    source that answered with no visible history are three different facts. The
    invariants live here so neither adapter nor publisher can recreate the old
    false equivalence between an empty tail and an omitted one (ADR 0016).
    """

    availability: ProgressAvailability = ProgressAvailability.NOT_READ
    has_history: bool | None = None
    recent: tuple[ProgressEntry, ...] = ()
    omission: ProgressOmission = ProgressOmission.NONE
    read_at: datetime | None = None
    reason: str | None = None

    @classmethod
    def readable(
        cls,
        *,
        has_history: bool,
        read_at: datetime,
        recent: tuple[ProgressEntry, ...] = (),
        omission: ProgressOmission = ProgressOmission.NONE,
    ) -> ProgressObservation:
        return cls(
            availability=ProgressAvailability.READABLE,
            has_history=has_history,
            recent=recent,
            omission=omission,
            read_at=read_at,
        )

    @classmethod
    def unreadable(cls, reason: str) -> ProgressObservation:
        return cls(availability=ProgressAvailability.UNREADABLE, reason=reason)

    @classmethod
    def from_capture(
        cls,
        *,
        recent: tuple[ProgressEntry, ...],
        omission: ProgressOmission,
        read_at: datetime,
    ) -> ProgressObservation:
        """Build one readable source fact without duplicating history semantics."""
        if omission is ProgressOmission.STATUS_SUMMARY:
            raise ValueError("a source capture cannot be a roster summary")
        return cls.readable(
            has_history=bool(recent) or omission is not ProgressOmission.NONE,
            recent=recent,
            omission=omission,
            read_at=read_at,
        )

    def __post_init__(self) -> None:
        if not isinstance(self.availability, ProgressAvailability):
            raise ValueError("progress availability must use the Agent seam vocabulary")
        if not isinstance(self.omission, ProgressOmission):
            raise ValueError("progress omission must use the Agent seam vocabulary")
        if self.omission is ProgressOmission.OVERSIZE:
            raise ValueError("oversize names one History page entry, never a whole reading")
        if self.has_history is not None and type(self.has_history) is not bool:
            raise ValueError("progress history presence must be true, false or absent")
        if not isinstance(self.recent, tuple) or any(
            not isinstance(entry, ProgressEntry) for entry in self.recent
        ):
            raise ValueError("progress recent must be an ordered tuple of whole entries")
        if self.read_at is not None and not isinstance(self.read_at, datetime):
            raise ValueError("progress read time must be a datetime or absent")
        match self.availability:
            case ProgressAvailability.NOT_READ:
                if (
                    self.has_history is not None
                    or self.recent
                    or self.omission is not ProgressOmission.NONE
                    or self.read_at is not None
                    or self.reason is not None
                ):
                    raise ValueError("progress that was not read carries no observed facts")
            case ProgressAvailability.UNREADABLE:
                if not isinstance(self.reason, str) or not self.reason.strip():
                    raise ValueError("unreadable progress must carry its source's reason")
                if (
                    self.has_history is not None
                    or self.recent
                    or self.omission is not ProgressOmission.NONE
                    or self.read_at is not None
                ):
                    raise ValueError("unreadable progress carries only its source's reason")
            case ProgressAvailability.READABLE:
                if self.has_history is None or self.read_at is None:
                    raise ValueError("readable progress carries history presence and read time")
                if self.reason is not None:
                    raise ValueError("readable progress does not also carry an unreadable reason")
                if not self.has_history:
                    if self.recent or self.omission is not ProgressOmission.NONE:
                        raise ValueError("empty history carries no entries and no omission")
                    return
                if not self.recent and self.omission is ProgressOmission.NONE:
                    raise ValueError("history exists, so an empty tail must name its omission")
                if self.recent and self.omission in (
                    ProgressOmission.STATUS_SUMMARY,
                    ProgressOmission.NEWEST_OVERSIZE,
                ):
                    raise ValueError(f"{self.omission} carries no progress entries")


@dataclass(frozen=True, slots=True)
class HistoryPage:
    """One page of what a Session said and was told, older on request (#171).

    The third publication of the one canonical observation (ADR 0016's
    amendment), and a **separate read**: `inspect` still answers the newest tail
    and folds into the roster, while this never touches the roster at all.

    `entries` are newest-first and bounded by a **count**, not by bytes — the
    encoded Reply's ceiling stays the wire's, applied by the publication, which
    keeps an over-ceiling entry's slot rather than dropping it. `older` says
    whether anything remains before the oldest entry on this page, so the next
    request passes that entry's `ordinal` as `before`.

    **`read_at=None` is a stated contract, not a missing value.** It means the
    lane holds no record for this target to read — an unattached Codex thread,
    or a Claude Session whose transcript this engine was never told about. Bridge
    Core turns it into the same typed refusal `progress` gave that case, because
    a Session nobody could read must never be published as one that said
    nothing. A lane that could not be read *at all* raises `LaneUnavailable`
    instead, which is a different fact about the lane rather than about this
    Session. A read that found nothing before the cursor is neither: it is an
    empty page with `older=False`, and that is an answer.
    """

    entries: tuple[ProgressEntry, ...] = ()
    older: bool = False
    read_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.entries, tuple) or any(
            not isinstance(entry, ProgressEntry) for entry in self.entries
        ):
            raise ValueError("a history page carries an ordered tuple of whole entries")
        if type(self.older) is not bool:
            raise ValueError("whether older entries remain is true or false")
        if self.read_at is not None and not isinstance(self.read_at, datetime):
            raise ValueError("a history page read time is a datetime or absent")
        if self.read_at is None and (self.entries or self.older):
            raise ValueError("a page nothing read carries no entries and no promise of more")
        ordinals = [entry.ordinal for entry in self.entries]
        if ordinals != sorted(ordinals, reverse=True) or len(set(ordinals)) != len(ordinals):
            raise ValueError("a history page is newest-first, and names each entry once")


class ChildKind(StrEnum):
    """Whether this Session is the user's, or something a Session of theirs spawned."""

    MAIN = "main"
    CHILD = "child"


@dataclass(frozen=True, slots=True)
class ChildClassification:
    """Seen, not spoken to. A Child Process is listed and never Relayed into (#68).

    The parent is carried when it can be established and is `None` when it
    cannot — a child whose parent we failed to identify is still a child, and
    demoting it to `main` over a missing link would open exactly the Relay the
    classification exists to close.
    """

    kind: ChildKind = ChildKind.MAIN
    parent: SessionTarget | None = None

    def __post_init__(self) -> None:
        if self.kind is ChildKind.MAIN and self.parent is not None:
            raise ValueError("a main Session is nobody's child, so it names no parent")

    @property
    def is_main(self) -> bool:
        return self.kind is ChildKind.MAIN


#: The ordinary case, named once so no reader spells it out.
MAIN_SESSION = ChildClassification()


@dataclass(frozen=True, slots=True)
class SessionInspection:
    """Everything this system knows about one Session, as one lane observed it.

    `target` is the **exact** identity, Claude's pid included
    (`seams/identity.py`): a resumed Session forks two processes under one
    session id and they are two rows, not one row that moved.
    """

    target: SessionTarget
    #: Where the Session is running. Compared by realpath wherever it is
    #: joined against anything: the official roster reports a resolved cwd (#73).
    workspace: Path
    lifecycle: SessionLifecycle = SessionLifecycle.LIVE
    state: SessionState = SessionState.RUNNING
    waiting_for: WaitingFor = field(default_factory=WaitingFor)
    progress: ProgressObservation = field(default_factory=ProgressObservation)
    #: Separate from `progress` on purpose: a Session can have moved without
    #: having said anything a reader would show, and #76 consumes both.
    last_activity: datetime | None = None
    child: ChildClassification = MAIN_SESSION
    #: What this Session is called — `<project> · <title>`, composed by the lane
    #: from the agent's own name for it and the workspace it runs in
    #: (`adapters/agent/_naming.py`). `None` is ordinary: a Codex thread that has
    #: not taken its first turn has neither a name nor an id to make one from.
    #: The registry takes the first one it is given and then follows this field
    #: (#78 as amended on #113), so a lane may only ever compose this from the
    #: agent's *official* name for the Session: a second, different name is read
    #: as the agent having renamed it, and reaches the user as a rename.
    name: SessionName | None = None


@dataclass(frozen=True, slots=True)
class LaneDiscovery:
    """One lane's answer to "what Sessions are there?" — and how much it is worth.

    **Three states, because there are three facts and they are not the same one.**

    - *Enumerated.* `error is None`. These rows are this lane's whole truth, and
      an empty tuple is a real answer: the machine has no Sessions on this lane.
    - *Degraded.* `error is None`, `degraded` says why. The rows are still the
      truth — they were just read by a weaker means, such as the process table
      when Codex's shared daemon is not up. They are adopted like any other; the
      note is for `status`, so the user can see the lane is running on evidence
      rather than on the daemon's word.
    - *Failed.* `error` says what stopped it, and the rows **must** be empty.
      Bridge Core leaves this lane's held rows exactly as they were, because
      "I could not look" is not a sighting of an empty machine.

    The invariant that a failure carries no rows is enforced here rather than
    remembered, because the alternative encoding — reading "no rows plus an
    error" as failure — cannot tell a lane that could not look from a lane that
    looked and found nothing, and the second one has to be able to end rows.

    The other lane's rows are unaffected in every case: two agents fail
    independently, and one of them being down is not news about the other.
    """

    rows: tuple[SessionInspection, ...] = ()
    #: Why this lane could not enumerate at all. Never about one row.
    error: str | None = None
    #: Why these rows come from a weaker source than usual. The rows still count.
    degraded: str | None = None

    def __post_init__(self) -> None:
        if self.error is not None and not self.error.strip():
            raise ValueError("a lane that failed to enumerate must say what stopped it")
        if self.degraded is not None and not self.degraded.strip():
            raise ValueError("a lane reading from a weaker source must say which")
        if self.error is not None and self.rows:
            raise ValueError(
                "a lane that could not enumerate has no rows to offer; rows read by a "
                "weaker means are `degraded`, not `error`"
            )

    @property
    def enumerated(self) -> bool:
        """Whether this lane looked at all. Says nothing about how well."""
        return self.error is None


class LaneUnavailable(Exception):
    """`inspect` could not look, so it says nothing about the Session at all.

    **The one thing on this seam that raises, and only because `inspect` has no
    other channel.** `discover` reports the same trouble as data
    (`LaneDiscovery.error`) because "this lane is unavailable" is a row the
    roster can show; `SessionInspection` has no such field, so an `inspect` that
    returned *anything* here would be asserting a lifecycle and a state nobody
    read. A raise is the only answer that claims nothing.

    It is deliberately not `WaitingFor(kind=UNKNOWN, caught_up=False)`, which is
    the seam's other way of saying "I do not know yet". The two mean different
    retries: `caught_up=False` is a record the transcript has not flushed, read
    again in a moment; this is `claude` missing from the PATH or a command that
    failed, and re-reading it in a moment is a loop against a lane that is down.

    **What a caller does with it is settled here, so no consumer invents its
    own:** keep the row's last observed state, record `reason` where a lane's
    `LaneDiscovery.error` is already recorded for `status`, and never end the
    row. Not being able to look is not a sighting.
    """

    def __init__(self, agent: AgentKind, reason: str) -> None:
        super().__init__(f"the {agent} lane could not be read: {reason}")
        self.agent = agent
        #: The lane's own words — the same sentence `LaneDiscovery.error` carries.
        self.reason = reason


def derive_reply_window(
    state: SessionState,
    waiting_for: WaitingFor,
    child: ChildClassification,
    *,
    question_answerable: bool = False,
) -> ReplyWindow:
    """Whether a Session will act on the next Relay as its next turn.

    **Derived, never stored.** A Reply Window is a statement about the Session's
    current state, so keeping a copy of it is keeping a second answer that can
    disagree with the first — the reference implementation ran two live ledgers
    and rendered both.

    Two conditions, and each closes a different hole:

    - **`IDLE`, or an answerable question.** `RUNNING` is mid-turn. A permission
      dialog is closed. A question is open only while its lane still holds the
      exact prompt and can route the next Answer Relay into that hook; after
      expiry, keyboard EOF, or shutdown the same `WAITING` row is closed. The
      boolean is a live adapter fact, not inferred from the roster.
    - **Main Sessions only.** A Child Process is seen, not spoken to (#68).
    """
    if not child.is_main:
        return ReplyWindow.CLOSED
    if state is SessionState.IDLE:
        return ReplyWindow.OPEN
    if (
        state is SessionState.WAITING
        and waiting_for.kind is WaitingKind.QUESTION
        and question_answerable
    ):
        return ReplyWindow.OPEN
    return ReplyWindow.CLOSED


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """A Session's pending permission, as the adapter observed it.

    `approval_id` is the adapter's own opaque handle for the pending dialog. It
    is deliberately not a `RequestId`: this request was raised by the Session,
    while a `RequestId` is minted by Bridge Core for an attempt it sends.
    """

    approval_id: str
    target: SessionTarget
    tool_name: str
    detail: str = ""
    #: The decisions the far side offers, when it offers a list — the ready-made
    #: voice menu. Empty when the route offers only allow/deny.
    options: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.approval_id.strip():
            raise ValueError("an approval request must carry the adapter's handle for it")


@dataclass(frozen=True, slots=True)
class SessionStopped(Event):
    """A Session stopped and may need the user. Feeds the Stop Notice pipeline.

    Its `progress` is the authoritative observation made at the Stop, so Bridge
    Core publishes the notice and roster from one fact without another read.

    **What it stopped on is a `WaitingFor`, not free text.** The reference
    implementation carried a rendered sentence here, so every consumer that
    wanted the question's options — the voice menu, the Companion Channel's
    buttons — had to parse prose the adapter had already thrown structure away
    to produce. The structure travels; rendering is the surface's job.
    """

    target: SessionTarget
    progress: ProgressObservation = field(default_factory=ProgressObservation)
    waiting_for: WaitingFor = field(default_factory=WaitingFor)


@dataclass(frozen=True, slots=True)
class SessionEnded(Event):
    """A Session is gone. The registry may no longer be Relayed into."""

    target: SessionTarget
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ReplyWindowChanged(Event):
    """The Session's willingness to accept an inbound Relay as a user turn changed."""

    target: SessionTarget
    window: ReplyWindow


@dataclass(frozen=True, slots=True)
class RelayReceipt(Event):
    """A receipt that arrived after the call returned — a held or expired Relay."""

    target: SessionTarget
    receipt: DeliveryReceipt


#: The closed set of events this seam raises. Nothing else may appear.
AgentEvent = SessionStopped | SessionEnded | ReplyWindowChanged | RelayReceipt


@runtime_checkable
class AgentAdapter(Protocol):
    """What Codex and Claude each implement. Mechanism only; no policy, no queueing."""

    def supported_routes(self) -> frozenset[RelayRoute]:
        """Which routes this adapter really has. Static, and honest about gaps."""
        ...

    async def discover(self) -> LaneDiscovery:
        """Every Session this lane can see right now, or why it can see none.

        **Async, alone with `inspect` among this seam's readers**, because both
        do real I/O — a subprocess for Claude's official roster, a daemon round
        trip and a filesystem walk for Codex — and a blocking call here would
        stall the dispatch loop for as long as the far side takes to answer.

        Called on a cadence rather than subscribed to: neither agent offers a
        "a Session appeared" event, and the two that come close (Claude's
        `SessionStart` hook, Codex's daemon notifications) each cover a strict
        subset of the Sessions the user starts. Polling one source that sees all
        of them beats stitching two that each see some.

        **It never raises to say a lane is down.** An adapter that cannot
        enumerate returns `LaneDiscovery(error=...)`, because "this lane is
        unavailable" is an answer the roster can show and an exception is not.
        """
        ...

    async def inspect(self, target: SessionTarget) -> SessionInspection:
        """Everything this lane knows about one Session, freshly read.

        The same value `discover` yields per row, for the one Session a caller
        already holds — used when a fact has to be re-read now rather than at
        the next tick, which is what `WaitingFor(kind=UNKNOWN, caught_up=False)`
        asks its reader to do.

        **It does not answer "is this Session still there".** That is
        `discover`'s question, asked of the whole lane on a cadence; this reads
        detail for a target Bridge Core already holds. A caller that derives a
        lifecycle from what this returns is reading a roster off a magnifying
        glass.

        **Raises `LaneUnavailable` when the lane cannot be read at all**, and
        the caller keeps the row's last observed state rather than ending it —
        the same rule `observe` follows for `LaneDiscovery.error`, for the same
        reason.
        """
        ...

    async def history(
        self,
        target: SessionTarget,
        *,
        before: int | None,
        count: int,
    ) -> HistoryPage:
        """One page of this Session's own record, newest-first, older on request.

        **A separate read, never folded into the roster** (ADR 0016's
        amendment). `inspect` answers what a Session is doing now; this answers
        what it said, `count` entries at a time, and Bridge Core publishes the
        page without touching the row.

        `before` is an `ordinal` this lane handed out on an earlier page: the
        page returned holds the entries immediately before it. `None` asks for
        the newest page, which **includes** the newest entry — every page is
        complete on its own and the engine remembers no cursor between reads
        (#171). A `before` larger than any ordinal is the newest page too, and a
        `before` past the oldest entry is an empty page with `older=False` — an
        answer, not a refusal.

        **The same windowing on both lanes.** Each lane already builds the full
        list of visible entries before anything trims it; the shared window
        (`adapters/agent/_progress.py`) is applied to that list, so two lanes
        cannot page differently over the same record.

        **Raises `LaneUnavailable` when the lane cannot be read at all**, and
        answers `HistoryPage(read_at=None)` when this lane holds no record for
        this target — see `HistoryPage` for why those are two different facts.
        """
        ...

    def reply_window(self, target: SessionTarget) -> ReplyWindow:
        """Where one Session's Reply Window stands right now, asked rather than awaited.

        The level, pulled; `ReplyWindowChanged` remains the transition, pushed.
        Bridge Core calls this once, the instant it enters a Session in its
        roster, so the Session starts from an observed level instead of from the
        fail-closed default — and calls nothing here again.

        **A pull exists because the push cannot bootstrap a level (#27).** An
        adapter is registered before Bridge Core holds the Session, so a report
        emitted at registration is dropped as belonging to a Session nobody knows
        — and it is a report the adapter has already recorded as sent, so no
        later transition repeats it. A Session that was already idle when it was
        registered therefore stayed at CLOSED forever, unreachable while
        perfectly healthy. Asking closes that hole by construction rather than by
        timing: the roster provably holds the Session one line before the
        question is asked.

        **Deliberately synchronous**, alone among this seam's verbs except
        `supported_routes`. An await here would reintroduce the very gap the pull
        exists to close, by letting the dispatch loop run between the roster
        write and the answer being applied. Both real adapters can answer without
        one — Claude from the registry record it already reads, Codex from the
        status it has already observed — so the seam asks for no more than they
        need.

        **Fail closed, and never fail the caller.** An adapter that does not hold
        this target answers CLOSED, because "I cannot reach this Session" is not
        an observation that its window is open. Bridge Core treats a raise the
        same way and carries on regardless: a Session that is listed but
        conservatively closed is recoverable on its next transition, while one
        dropped from the roster over a level query is not.

        Extending this seam's verb set was adjudicated for this use case.
        """
        ...

    def question_answerable(self, target: SessionTarget) -> bool:
        """Whether this lane still holds this Session's question answer route.

        This is the live half of the Reply Window rule. A roster can say that a
        Session is waiting on a question, but only the adapter knows whether the
        exact hook is still parked. False is the fail-closed answer for an
        unknown target, a permission, a question whose hook has ended, or a lane
        without this route.
        """
        ...

    async def answer_relay(
        self,
        target: SessionTarget,
        text: str,
        *,
        request_id: RequestId,
        route: RelayRoute = RelayRoute.DELIVER,
    ) -> DeliveryReceipt:
        """Carry the user's own words in, with the user's authority."""
        ...

    async def approval_relay(
        self, request: ApprovalRequest, verdict: ApprovalVerdict, *, request_id: RequestId
    ) -> DeliveryReceipt:
        """Carry the user's verdict on one pending permission request."""
        ...

    async def verify(self) -> VerifyResult:
        """Report which implementation this is and whether its far side answers."""
        ...

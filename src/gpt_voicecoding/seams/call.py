"""The Call seam — the system's one voice surface.

Verbs Bridge Core calls: `ensure_call(dial)` and `end_call` (the two halves of
the Live Toggle), `call_state`, `speak(brief)`, `delegate(text) -> reply` (the
Delegated Turn — the cost lever, whose model the caller selects), `play_cue(cue)`,
and `verify`.

**`play_cue` names a moment, not a sound.** The user hears the call connect and
hears it end, and what those are heard *as* was chosen by ear on real speakers
(#174) — a decision with no policy in it, which is why the caller states
`CONNECTED` or `ENDED` and the adapter owns everything else about it.

**Instructions arrive at the call site, as plain data.** Both verbs that start a
thread take the instruction set that thread begins with, because Bridge Core
generates them and is their only source (ADR 0001; the instruction-generation
issue). Handing them in per attempt rather than installing them once keeps the
adapter stateless about them: there is no window in which a call could be opened
with instructions from a generation that is no longer the hub's. An adapter may
not hold them past the call they were given for.

**One dial, two audiences, three payloads.** A call is two models — the Voice the
user hears and the Call Agent behind it, the only half with tools — and
`ensure_call` therefore takes a `Dial` rather than one string (ADR 0018, proved
by slot-swap in #175 Q4). The `Dial` names its *audiences*: prose for the Voice,
rules for the Call Agent, and the Briefing's dial-time hand-over. Which wire
slot carries which audience is the realtime adapter's alone to know; nothing
above this seam ever learns a slot name.

**A brief crosses this seam as a brief, not as a sentence to read out.** `speak`
takes a `SpokenBrief` — `CONTEXT.md`'s *Stop Notice* says the Live Call "does not
receive text to read out; it receives the Session Brief itself and speaks from
it". The carrier is seam-owned because no Core type may cross a seam (ADR 0001),
and it carries Briefing's *own words* rather than raw values, because Briefing is
the one renderer of Session state (`core/briefing.py`): an adapter that turned a
state enum into a phrase would be a second place the user's vocabulary lives.
What an adapter does with those words is assemble them; it picks none of them.

Events raised upward: the user's speech transcript, whether the call's own
Voice is speaking, and call started / ended / dropped.

The one-call-at-a-time invariant lives *above* this seam, in Bridge Core, not in
any adapter (ADR 0001). An adapter neither knows nor enforces it.

`delegate` takes its model as a required argument. It is a user-facing setting —
the cost lever — so there is no default here for configuration to be quietly
overruled by.

An adapter grades its own `speak` from its own connection state and its own
events, never by matching against another surface's records. The reference
implementation graded an audibly spoken notice FAILED that way, and the retries
opened duplicate calls.

Adapters: the bridge-owned realtime call is the only one shipped. The GUI Live
Driver is historical — it is not migrated, and it is why this seam exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Protocol, runtime_checkable

from gpt_voicecoding.seams.delivery import DeliveryReceipt
from gpt_voicecoding.seams.events import Event
from gpt_voicecoding.seams.identity import RequestId
from gpt_voicecoding.seams.verify import VerifyResult


class CallState(StrEnum):
    """Whether a Live Call is up. Three states, because connecting is not up."""

    DOWN = "down"
    CONNECTING = "connecting"
    UP = "up"


class Cue(StrEnum):
    """A moment in the call the user is owed a sound for — never the sound itself.

    The seam names the moment because the sound is the adapter's: which notes,
    how loud and how long were chosen by ear (#174) against one machine's
    speakers, and nothing above this seam has an opinion about any of it. A
    caller says *the call came up*; what that is heard as belongs behind here.

    `EVENT` is the mid-call one — something happened that is not the call
    starting or ending — and it has no caller yet: the Call Keeper (#170, #174)
    is what will ring it. It ships implemented rather than deferred because the
    three sounds were chosen together, as a set a listener learns at once, and
    picking the third one later would be picking it against a set that had
    already gone out.
    """

    CONNECTED = "connected"
    ENDED = "ended"
    EVENT = "event"


#: How many bytes of hand-over one dial may carry, and the reason it is bytes.
#: The wire caps the slot at 8,192 **tokens** (`REALTIME_INITIAL_ITEMS_MAX_TOKENS`,
#: `docs/research/2026-09-01-realtime-live-probe.md` "the `initialItems` budget"),
#: and a UTF-8 byte is the floor of a token, so a hand-over inside this many
#: bytes can never exceed that cap (ADR 0018 measures the instruction budgets the
#: same way). It is **over-conservative for Chinese** — where one character is
#: three bytes and rather less than three tokens — and that is accepted: this
#: ticket takes no live measurement, and loosening the figure is a later ticket
#: that brings one.
HANDOVER_BUDGET_BYTES: Final = 8192

#: How many hand-over items one dial may carry. The wire's own count ceiling
#: (`REALTIME_INITIAL_ITEMS_MAX_COUNT`, same probe), which it enforces by
#: refusing the request rather than by truncating it — so the ceiling is kept on
#: this side, where a brief that will not fit can be trimmed with words.
MAX_HANDOVER_ITEMS: Final = 128


@dataclass(frozen=True, slots=True)
class DialReason:
    """Why this call exists, in one line — the hand-over's leading item.

    A call the user opened and a call the system dialled are different calls to
    be on, and the Voice is owed which one it is before anything else it is
    handed. The words are the caller's; this carries them.
    """

    text: str

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("a dial reason says why the call exists; there are no words here")

    @property
    def size_in_bytes(self) -> int:
        return _bytes_of(self.text)


@dataclass(frozen=True, slots=True)
class SpokenRosterBrief:
    """How many Sessions are in each state, and one header row for each.

    The Roster Brief as the call carries it: `counts` and `focus` are already
    the words Briefing chose, and `rows` are its header lines in its own order.
    A running Session appears here and nowhere else in a hand-over, which is what
    "running Sessions get header rows only" means.
    """

    #: The whole counts line, heading included, because the heading is a *fact*
    #: and not a label: with a Focus Session the counts are **the others**, since
    #: that Session is spoken first and by name and counting it again would tell
    #: the user about it twice (#165 Q6). An adapter that wrote the heading would
    #: be deciding that, so Briefing writes it.
    counts: str
    rows: tuple[str, ...] = ()
    #: The Focus Session's header row, when there is one. Kept apart from `rows`
    #: because it is spoken first and by name, and the counts are *the others*.
    focus: str | None = None

    @property
    def size_in_bytes(self) -> int:
        return _bytes_of(self.counts, self.focus or "", *self.rows)


@dataclass(frozen=True, slots=True)
class SpokenBrief:
    """One Session Brief as the Live Call carries it — Briefing's words, as data.

    The Session Brief's own fields, minus the address it was taken by: an
    adapter has nothing to do with a `SessionTarget`, and the Session is named
    to the Voice by `name`. Every field is a string Briefing has already worded,
    so the adapter assembles and never phrases (see the module docstring).

    **There is no `undelivered` field.** #194's body listed one, and `CONTEXT.md`'s
    *Session Brief* promises it — "when the user's last reply to it never
    arrived, that it did not and why". Nothing in Core carries that fact onto a
    brief today: `core/briefing.py::SessionBrief` has no such field and the
    undelivered Relay queue is never read into one (the run's landed-facts note
    on #194). A field fed by nothing would read as an answered question, so it is
    absent here rather than blank. Reconciled and not forgotten: the ticket that
    gives the glossary's sentence a source adds it in both places at once.
    """

    name: str
    agent: str
    state: str
    newest: str
    decision: tuple[str, ...]
    answerable_here: str
    last_activity_at: str

    def __post_init__(self) -> None:
        if not self.state.strip():
            raise ValueError("a spoken brief says what the Session is doing")

    @property
    def size_in_bytes(self) -> int:
        return _bytes_of(
            self.name,
            self.agent,
            self.state,
            self.newest,
            self.answerable_here,
            self.last_activity_at,
            *self.decision,
        )


#: The closed set of things a hand-over is made of. Three kinds, and the adapter
#: maps each to exactly one wire item.
HandoverItem = DialReason | SpokenRosterBrief | SpokenBrief


@dataclass(frozen=True, slots=True)
class Dial:
    """What one call is opened on: two audiences and the hand-over between them.

    `voice` and `agent` are required and non-blank. Sending no prose to the
    Voice does not mean "say nothing" — it hands the Voice back to codex's own
    stock prompt, which is the state ADR 0018 exists to end; and sending no rules
    to the Call Agent leaves the half with the tools nothing to go on. Both
    refusals are `ValueError` at construction, so a dial that reaches an adapter
    is a dial that can be sent.

    The two ceilings are refused here as well. Both are hard rejections on the
    wire — an over-budget request is an error, not a truncation — so a `Dial` the
    far side would refuse is not a dial, and catching it at construction is what
    makes `Briefing`'s trimming testable without a network.
    """

    voice: str
    agent: str
    hand_over: tuple[HandoverItem, ...] = ()

    def __post_init__(self) -> None:
        if not self.voice.strip():
            raise ValueError("a dial addresses the Voice; there is no prose here")
        if not self.agent.strip():
            raise ValueError("a dial addresses the Call Agent; there are no rules here")
        if len(self.hand_over) > MAX_HANDOVER_ITEMS:
            raise ValueError(
                f"a hand-over carries at most {MAX_HANDOVER_ITEMS} items; "
                f"this one carries {len(self.hand_over)}"
            )
        if self.hand_over_size_in_bytes > HANDOVER_BUDGET_BYTES:
            raise ValueError(
                f"a hand-over carries at most {HANDOVER_BUDGET_BYTES} bytes; "
                f"this one carries {self.hand_over_size_in_bytes}"
            )

    @property
    def hand_over_size_in_bytes(self) -> int:
        return sum(item.size_in_bytes for item in self.hand_over)


#: What one carried string costs *beyond its own bytes* once an adapter has put
#: it on a line: a label, an indent, a separator and a newline. Twenty-four
#: bytes covers every one an adapter here writes — the longest is
#: `"  last activity: "` at seventeen — and the headline's three fields are each
#: charged it although they share one line, so the figure is an over-estimate by
#: construction. It has to be: `HANDOVER_BUDGET_BYTES` is a promise that the
#: wire cannot refuse what a `Dial` accepted, and a count that measured the words
#: without the labels around them made that promise on 8,192 bytes of text that
#: reached the wire as 8,242 (#194 review). `tests/test_realtime_call.py` holds
#: the invariant: no assembled item is larger than the size it was budgeted at.
WIRE_LINE_OVERHEAD_BYTES: Final = 24


def _bytes_of(*words: str) -> int:
    """What a set of already-worded strings costs on the wire, in UTF-8 bytes.

    The words, plus what surrounds each of them once it is a line. An adapter
    chooses those labels, so this cannot know them exactly — it charges an
    allowance that is larger than any of them instead, which is the only
    direction a budget may be wrong in.
    """
    return sum(len(word.encode("utf-8")) + WIRE_LINE_OVERHEAD_BYTES for word in words)


@dataclass(frozen=True, slots=True)
class CallSnapshot:
    """The adapter's own answer about its own call."""

    state: CallState
    call_id: str | None = None

    def __post_init__(self) -> None:
        if self.state is CallState.UP and not (self.call_id or "").strip():
            raise ValueError("a call that is up must name itself")
        if self.state is CallState.DOWN and self.call_id is not None:
            raise ValueError("a call that is down has no id")

    @property
    def is_up(self) -> bool:
        return self.state is CallState.UP


@dataclass(frozen=True, slots=True)
class DelegatedReply:
    """One Delegated Turn's answer, and which model actually produced it."""

    text: str
    model: str

    def __post_init__(self) -> None:
        if not self.model.strip():
            raise ValueError("a delegated reply must name the model that produced it")


@dataclass(frozen=True, slots=True)
class UserSpeech(Event):
    """What the user said, as the call transcribed it."""

    text: str


@dataclass(frozen=True, slots=True)
class VoiceSpeech(Event):
    """Whether the call's own Voice is producing speech right now.

    Named for the glossary's **Voice**, not for the wire: `role: assistant` is
    the realtime protocol's word, known to the adapter that translates it and
    to nothing above this seam.

    A state and not a tick, because the two things that need it need different
    questions answered. The Silence Ceiling asks "was there activity" *and* has
    to hold while an answer is still being spoken — an answer generated in ten
    seconds and spoken over seventy-five is seventy-five seconds of call, which
    no bare edge describes. "Wait for a gap" asks whether it is speaking now.

    `speaking=False` means the Voice stopped **generating**, not that the
    speaker stopped **playing**: playout trails it by the transport's jitter
    buffer and this system's own playback buffer. A caller that waits for a gap
    owes a settle window on top of this edge; the ceiling does not, because
    trailing audio only makes a call it holds open longer.
    """

    speaking: bool


@dataclass(frozen=True, slots=True)
class CallStarted(Event):
    call_id: str


@dataclass(frozen=True, slots=True)
class CallEnded(Event):
    """The call ended as asked."""

    call_id: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class CallDropped(Event):
    """The call ended without being asked to. Bridge Core decides what follows."""

    call_id: str
    detail: str = ""


#: The closed set of events this seam raises. Nothing else may appear.
CallEvent = UserSpeech | VoiceSpeech | CallStarted | CallEnded | CallDropped


@runtime_checkable
class CallAdapter(Protocol):
    """The one voice surface. Holds the call; holds no policy about it."""

    async def ensure_call(self, dial: Dial) -> CallSnapshot:
        """Bring a call up on that dial, or report the one already up.

        Idempotent: a call that is already up is reported as it is, and the dial
        is not re-applied to it. Only the thread this verb starts is ever given
        it — the hand-over in particular is dial-time and nothing else, so a
        second `ensure_call` cannot smuggle a fresh briefing into a live call.
        """
        ...

    async def end_call(self) -> CallSnapshot:
        """End the current call. Idempotent when none is up."""
        ...

    async def call_state(self) -> CallSnapshot:
        """What this adapter's own connection state says, right now."""
        ...

    async def speak(self, brief: SpokenBrief, *, request_id: RequestId) -> DeliveryReceipt:
        """Hand the call one Session Brief. Graded from this adapter's own state.

        A brief and not a sentence: the Voice words what it is given, and this
        seam hands it the thing to be worded (`CONTEXT.md`, *Stop Notice*).
        """
        ...

    async def delegate(
        self, text: str, *, model: str, instructions: str, request_id: RequestId
    ) -> DelegatedReply:
        """Hand work to a coding model on the user's behalf — the Delegated Turn."""
        ...

    async def play_cue(self, cue: Cue) -> None:
        """Mark one moment of the call with a sound, on the user's own speakers.

        **Returns as soon as the cue is on its way, not when it has been heard.**
        A cue is feedback about something that already happened, and a caller
        that waited for one would be holding its own dispatch open for a third
        of a second to play a noise about the thing it has finished doing.

        **Cues are heard in the order they were asked for.** They mark moments,
        and the moments have an order — a caller that says CONNECTED and then
        ENDED has described a call, not a set of two things. How an adapter
        keeps that promise while still returning at once is its own business.

        Nothing is reported back. A cue that could not be played — no output
        device, no audio library, a device somebody unplugged — is the adapter's
        to write down and swallow: there is no recovery a caller could attempt,
        and a raise here would let a missing sound take down the call it was
        only commenting on.

        Not tied to the call being up. `ENDED` plays *after* the call's own
        audio stream has closed, which is why the player is the adapter's and
        not the transport's.
        """
        ...

    async def verify(self) -> VerifyResult:
        """Report which implementation this is and whether its far side answers."""
        ...

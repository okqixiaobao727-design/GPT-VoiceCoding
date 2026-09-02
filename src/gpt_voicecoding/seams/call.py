"""The Call seam — the system's one voice surface.

Verbs Bridge Core calls: `ensure_call` and `end_call` (the two halves of the Live
Toggle), `call_state`, `speak(text)`, `delegate(text) -> reply` (the Delegated
Turn — the cost lever, whose model the caller selects), `play_cue(cue)`, and
`verify`.

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
from typing import Protocol, runtime_checkable

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

    async def ensure_call(self, instructions: str) -> CallSnapshot:
        """Bring a call up on those house rules, or report the one already up.

        Idempotent: a call that is already up is reported as it is, and the
        instructions are not re-applied to it. Only the thread this verb starts
        is ever given them.
        """
        ...

    async def end_call(self) -> CallSnapshot:
        """End the current call. Idempotent when none is up."""
        ...

    async def call_state(self) -> CallSnapshot:
        """What this adapter's own connection state says, right now."""
        ...

    async def speak(self, text: str, *, request_id: RequestId) -> DeliveryReceipt:
        """Say something into the call. Graded from this adapter's own state."""
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

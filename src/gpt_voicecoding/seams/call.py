"""The Call seam — the system's one voice surface.

Verbs Bridge Core calls: `ensure_call` and `end_call` (the two halves of the Live
Toggle), `call_state`, `speak(text)`, `delegate(text) -> reply` (the Delegated
Turn — the cost lever, whose model the caller selects), and `verify`.

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

    async def verify(self) -> VerifyResult:
        """Report which implementation this is and whether its far side answers."""
        ...

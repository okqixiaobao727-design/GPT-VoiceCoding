"""The Call seam — the system's one voice surface.

Verbs Bridge Core calls: `ensure_call` and `end_call` (the two halves of the Live
Toggle), `call_state`, `speak(text)`, `delegate(text) -> reply` (the Delegated
Turn — the cost lever, whose model the caller selects), and `verify`.

Events raised upward: the user's speech transcript, and call started / ended /
dropped.

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
CallEvent = UserSpeech | CallStarted | CallEnded | CallDropped


@runtime_checkable
class CallAdapter(Protocol):
    """The one voice surface. Holds the call; holds no policy about it."""

    async def ensure_call(self) -> CallSnapshot:
        """Bring a call up, or report the one already up. Idempotent."""
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

    async def delegate(self, text: str, *, model: str, request_id: RequestId) -> DelegatedReply:
        """Hand work to a coding model on the user's behalf — the Delegated Turn."""
        ...

    async def verify(self) -> VerifyResult:
        """Report which implementation this is and whether its far side answers."""
        ...

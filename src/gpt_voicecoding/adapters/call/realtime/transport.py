"""The audio half of a Live Call, named so the signalling half can be tested.

This is **not** a seam. Nothing about it varies in production — there is one
WebRTC route and one implementation of it (ADR 0001, principle 2). It exists for
a narrower reason: the adapter's real work is a signalling conversation with
`codex app-server` and a set of classification rules, and none of that can be
exercised in CI against a process that wants a microphone, a speaker and a
network. So the audio device and the peer connection live behind one small
interface that a test can stand in for, and everything else stays testable.

The interface is the minimum the signalling conversation actually needs, in the
order it needs it: an offer to send, an answer to apply, a connection to wait
for, a state to grade a `speak` against, and a way to be told the far side went
away. Anything larger would be inventing a lifecycle framework for one caller.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable


class TransportError(Exception):
    """The audio path could not be established, or went away while in use."""


#: Called with the reason when the peer connection ends by itself. Never called
#: for a close this side asked for: the adapter already knows about those.
LostHandler = Callable[[str], None]


@runtime_checkable
class CallTransport(Protocol):
    """One call's audio path: mic in, speaker out, and whether it is really up."""

    async def offer(self) -> str:
        """The local SDP offer, with ICE candidates already gathered into it.

        Gathered rather than trickled because the signalling route carries one
        SDP and no candidate messages: an offer sent before gathering finished
        would describe a connection that cannot be completed.
        """
        ...

    async def accept_answer(self, sdp: str) -> None:
        """Apply the remote description the app-server sent back."""
        ...

    async def wait_connected(self, timeout_seconds: float) -> None:
        """Block until audio is really flowing, or raise saying it never did."""
        ...

    @property
    def is_connected(self) -> bool:
        """Whether the audio path is up *right now*. What a `speak` is graded on."""
        ...

    def on_lost(self, handler: LostHandler) -> None:
        """Be told, once, if the connection ends without being asked to."""
        ...

    async def aclose(self) -> None:
        """Close the connection and release the audio devices. Idempotent."""
        ...


#: How the adapter makes one. Called once per call, and the call owns what it
#: returns — there is no pooling, because a peer connection that has been closed
#: cannot be reopened.
TransportFactory = Callable[[], CallTransport]

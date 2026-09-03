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
for, a state to grade a `speak` against, one fact about playout, and a way to be
told the far side went away. Anything larger would be inventing a lifecycle
framework for one caller.

**Why playout is here and not above the Call seam.** `VoiceSpeech(speaking=False)`
means the Voice's audio has *finished playing* (#195). Only this side knows when
that is: the jitter prefetch, this transport's own playback buffer and the
device all sit between the last inbound frame and the last audible sample, and
none of them is visible above the seam. Publishing the *generating* edge instead
made every caller that waits for a gap add a settle window of its own, computed
from numbers it could not see — the shallow shape #184 shipped and #195 closed.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable


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

    async def playback_drained(self, timeout_seconds: float) -> None:
        """Return when the last inbound audio frame has finished playing.

        **Returns rather than raises when the time runs out.** The caller is
        about to publish "the Voice stopped", and a bounded wait that expired is
        still the best answer anybody has: an edge that never arrives is worse
        than one that arrives a little early, because the whole point of the
        edge is that something downstream is waiting for the gap.

        "Drained" is two facts and both are this side's: nothing more has come
        in for the transport's own quiet bound, and nothing it did come in with
        is still queued for the device. A silent run has no device and no queue,
        so the first fact is the whole answer there — which is correct, not a
        stand-in: there is no speaker to trail.
        """
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


@runtime_checkable
class CueOutput(Protocol):
    """Where a cue is played out, named here for the same reason `CallTransport` is.

    A cue opens a real output device, and every decision worth grading about one
    — which moment gets which sound, when it is played, what happens when it
    cannot be — is on this side of that device. So the device goes behind an
    interface a test can stand in for, and the rest stays testable in CI, which
    never installs the voice extra.

    Not a seam and nothing about it varies in production (ADR 0001, principle 2):
    there is one player, and it lives in the audio module beside the call's own
    speaker. It is **per adapter and not per call**, because the cue that most
    needs playing is the one that marks a call that has already gone.
    """

    @property
    def device(self) -> int | None:
        """Which output index cues go to. `None` is the machine's own default."""
        ...

    @property
    def playing(self) -> Any | None:
        """The span going out right now, or `None`.

        What #145 gates capture on: the microphone stays open through a cue, and
        the mid-call one is deliberately loud enough to carry over speech — which
        is the same thing as loud enough to be heard back.
        """
        ...

    def play(self, pcm: bytes, *, span: Any = None) -> None:
        """Play one buffer to the end, holding `span` while it goes out.

        **Blocking, and says so.** The write is a device write and `stop` drains
        after it — 60-300 ms of sound measured 320-620 ms of wall time on this
        path (#174) — so the caller is the one that decides what thread this
        runs on. Raises whatever the audio library raises: what a failed cue
        must not take down is the caller's business, and the caller knows.
        """
        ...

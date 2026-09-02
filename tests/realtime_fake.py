"""What stands in for the two things a Live Call needs and CI cannot have.

A real call needs a `codex app-server` process and an audio device. `codex_fake`
already supplies the first as a real socket speaking real frames, so the only
thing invented here is the second — plus the small stand-in for the *component*
the Codex Agent adapter owns, of which the Call adapter uses exactly two members.

Nothing here simulates a call. The transport does what a test tells it to do and
records what it was asked, which is what lets a test put the adapter in the
states that matter: audio that never arrives, audio that goes away mid-sentence,
a hang-up in the middle of the handshake.
"""

from __future__ import annotations

import asyncio
from typing import Any

from gpt_voicecoding.adapters.call.realtime.transport import LostHandler, TransportError

#: What a real offer looks like, near enough. Nothing parses it.
OFFER_SDP = "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=-\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"

#: And what the app-server answers with.
ANSWER_SDP = "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"


class SharedAppServer:
    """The two members of `OwnedAppServer` the Call adapter actually uses.

    Deliberately not the real class: the real one spawns a `codex` process, and
    what this seam's tests are about is the signalling conversation on the far
    side of the socket, not process ownership. That the real component fans its
    notifications out to more than one listener is asserted where it lives, in
    `test_codex_process`.
    """

    def __init__(self, connection: Any) -> None:
        self.connection = connection
        self.listeners: list[Any] = []

    def listen(self, handler: Any) -> None:
        self.listeners.append(handler)

    def heard(self, message: dict[str, Any]) -> None:
        """What the connection calls. Wired in as its notification handler."""
        for listener in list(self.listeners):
            listener(message)


class FakeTransport:
    """An audio path a test drives directly. Opens no device and dials nothing."""

    def __init__(
        self,
        *,
        connects: bool = True,
        offer_sdp: str = OFFER_SDP,
        fail_offer: str = "",
    ) -> None:
        self.connects = connects
        self.offer_sdp = offer_sdp
        self.fail_offer = fail_offer
        self.answers: list[str] = []
        self.closed = False
        self._connected = False
        self._on_lost: LostHandler | None = None

    async def offer(self) -> str:
        if self.fail_offer:
            raise TransportError(self.fail_offer)
        return self.offer_sdp

    async def accept_answer(self, sdp: str) -> None:
        self.answers.append(sdp)

    async def wait_connected(self, timeout_seconds: float) -> None:
        if not self.connects:
            await asyncio.sleep(timeout_seconds)
            raise TransportError("audio never started flowing")
        self._connected = True

    @property
    def is_connected(self) -> bool:
        return self._connected and not self.closed

    def on_lost(self, handler: LostHandler) -> None:
        self._on_lost = handler

    async def aclose(self) -> None:
        self.closed = True
        self._connected = False

    # -- what a test does to it -------------------------------------------

    def lose(self, reason: str = "the audio connection failed") -> None:
        """The peer connection goes away by itself, as it does on a real drop."""
        self._connected = False
        handler, self._on_lost = self._on_lost, None
        if handler is not None:
            handler(reason)

    def go_quiet(self) -> None:
        """Audio stops without anything announcing it — a `speak` grades UNKNOWN."""
        self._connected = False


def realtime_script(server: Any, *, thread_id: str = "thread-1", model: str = "gpt-5") -> None:
    """Teach a `FakeAppServer` the happy path of one realtime call.

    The SDP answer and `started` arrive as notifications *after* the request
    they answer has returned, because that is the shape of the real route: the
    app-server accepts the start and then talks back on the socket.
    """
    server.answers("thread/start", {"thread": {"id": thread_id}, "model": model})
    server.answers("thread/realtime/start", _starting(server, thread_id))
    server.answers("thread/realtime/appendSpeech", {})
    server.answers("thread/realtime/stop", {})
    server.answers("thread/unsubscribe", {})


def _starting(server: Any, thread_id: str) -> Any:
    def start(_params: dict[str, Any]) -> dict[str, Any]:
        async def answer() -> None:
            await server.notify_all(
                "thread/realtime/sdp", {"threadId": thread_id, "sdp": ANSWER_SDP}
            )
            await server.notify_all(
                "thread/realtime/started",
                {"threadId": thread_id, "realtimeSessionId": "rt-1", "version": "v3"},
            )

        asyncio.ensure_future(answer())
        return {}

    return start


def delegated_script(
    server: Any, *, thread_id: str = "delegated-1", model: str = "gpt-5", says: str = "done"
) -> None:
    """Teach a `FakeAppServer` one whole Delegated Turn, answer included."""
    server.answers("thread/start", {"thread": {"id": thread_id}, "model": model})
    server.answers("thread/unsubscribe", {})

    def start_turn(_params: dict[str, Any]) -> dict[str, Any]:
        async def finish() -> None:
            await server.notify_all(
                "item/completed",
                {
                    "threadId": thread_id,
                    "turnId": "turn-1",
                    "item": {"type": "agentMessage", "id": "item-1", "text": says},
                },
            )
            await server.notify_all(
                "turn/completed",
                {"threadId": thread_id, "turn": {"id": "turn-1", "status": "completed"}},
            )

        asyncio.ensure_future(finish())
        return {"turn": {"id": "turn-1"}}

    server.answers("turn/start", start_turn)


class FakeCueOutput:
    """An output device a test can read back. Opens nothing and makes no sound.

    The third thing CI cannot have, after the app-server and the microphone: a
    speaker. It records the buffers it was handed and the spans that went with
    them, and it can be told to fail the way a missing device fails — which is
    the case that matters, because a cue that cannot be played may not take down
    the call it was only commenting on (#186).
    """

    def __init__(self, *, device: int | None = None, fails: str = "") -> None:
        self._device = device
        #: Non-empty makes every `play` raise it, the way an unplugged device does.
        self.fails = fails
        self.buffers: list[bytes] = []
        self.spans: list[Any] = []
        #: What `playing` said from *inside* the write, one entry a call. The
        #: span a capture gate would have read (#145) — unreadable afterwards,
        #: because a finished cue is holding nothing.
        self.seen_playing: list[Any] = []
        #: Called at the top of `play`, before anything is recorded. A test that
        #: needs the write to still be running when it looks blocks in here.
        self.while_playing: Any = None
        self._playing: Any = None

    @property
    def device(self) -> int | None:
        return self._device

    @property
    def playing(self) -> Any:
        return self._playing

    def play(self, pcm: bytes, *, span: Any = None) -> None:
        self._playing = span
        try:
            self.seen_playing.append(self._playing)
            if self.while_playing is not None:
                self.while_playing()
            if self.fails:
                raise TransportError(self.fails)
            self.buffers.append(pcm)
            self.spans.append(span)
        finally:
            self._playing = None

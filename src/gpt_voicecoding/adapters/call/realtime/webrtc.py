"""The one module in this repository that opens a microphone and a peer connection.

Every `aiortc`, `av` and `sounddevice` import in the system is here, and that is
the whole reason the module exists. `tests/test_architecture.py` asserts it:
Bridge Core and the seams may not speak a protocol at all, and this spoke's
protocol may not leak out of this file into the adapter's signalling logic,
which is the part CI actually runs.

They are optional dependencies (`pip install 'gpt-voicecoding[voice]'`). This
module therefore imports them at call time rather than at import time, and
`probe()` is what the factory calls to turn "the operator did not install the
voice extra" into a refusal to assemble the engine — before the user is on a
call at two in the morning, rather than during one.

**What the shape of the audio path is, and why.** 48 kHz mono `s16` in 20 ms
frames, which is `aiortc`'s Opus-native rate; the backend resamples to its own
24 kHz internally. Both directions carry a bounded jitter buffer, and both drop
the *oldest* audio when it overflows: a consumer that fell behind should cost
latency, never an exception per frame, and never unbounded memory. The playback
side copies only `samples * 2` bytes out of each resampled plane — the plane's
buffer is padded, and playing the padding is audible as static. That last one
was learned from the prototype the hard way and is the sort of detail a rewrite
silently loses.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import fractions
import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from gpt_voicecoding.adapters.call.realtime.transport import (
    CallTransport,
    LostHandler,
    TransportError,
)

_log = logging.getLogger(__name__)

#: `aiortc`'s Opus-native sample rate. The backend resamples to its own.
SAMPLE_RATE = 48_000

#: 20 ms at that rate — one Opus frame.
FRAME_SAMPLES = 960

#: The rest of the shape, named once. Every stream this module opens is the same
#: mono 16-bit audio at `SAMPLE_RATE`, and so is every buffer handed to one — the
#: microphone's frames, the speaker's playback and a cue's PCM. Spelled out at
#: each `sounddevice` call it would be three chances to open one stream in a
#: format the buffers feeding it are not in, and `bytes // 2` scattered around is
#: the same fact written as arithmetic nobody can search for.
CHANNELS = 1
SAMPLE_FORMAT = "int16"
SAMPLE_BYTES = 2

#: One frame, as a duration. Derived from the two numbers above rather than
#: written out, so the two things measured in frames below cannot drift from the
#: frames they are counting.
FRAME_SECONDS = FRAME_SAMPLES / SAMPLE_RATE

#: How many frames the speaker must go without a new inbound one before what it
#: has is called the *last* one. Three: shorter than any pause a listener hears
#: as the end of a sentence, and longer than the jitter between two frames of one
#: continuous utterance. It bounds only the *recognition* of the end, never the
#: playout itself — the buffer being empty is the other half of the answer, and
#: both must hold. Since #235 this is the *fallback* recognition: a peer that
#: keeps streaming silence after the Voice has finished never goes quiet, and
#: the server's own word (`OutputAudioEvent.FINISHED`) closes a span there.
PLAYBACK_QUIET_FRAMES = 3

#: The label of the events data channel this adapter offers. The backend's SDP
#: expects one to be described; since #235 it is also read, for the family of
#: events above. Every *other* event this adapter acts on still arrives as a
#: JSON-RPC notification on the app-server socket.
EVENTS_CHANNEL = "realtime-events"

#: How often `playback_drained` looks: one frame, because a poll finer than the
#: thing it is measuring buys nothing but wake-ups.
PLAYBACK_POLL_SECONDS = FRAME_SECONDS

#: How much captured audio is held before the oldest is dropped. Two seconds:
#: long enough to ride out a scheduling hiccup, short enough that what is
#: eventually sent is still a reply to what was said.
MAX_CAPTURE_FRAMES = 100

#: The same bound for playback, in bytes of 16-bit mono at `SAMPLE_RATE`.
MAX_PLAYBACK_BYTES = SAMPLE_RATE * SAMPLE_BYTES * 2

#: What to install when the import fails.
INSTALL_HINT = "pip install 'gpt-voicecoding[voice]'"

#: The distributions this route cannot run without.
REQUIRED = ("aiortc", "av", "sounddevice")


class VoiceDependencyError(Exception):
    """The voice extra is not installed, so there is no audio path to build."""


class OutputAudioEvent(enum.Enum):
    """The server's own words about a response's audio playing out (#235).

    OpenAI Realtime API server events, documented as **WebRTC/SIP only** and
    delivered on the client-created events data channel (the realtime-webrtc
    guide, "Set up data channel for sending and receiving events"). Per
    response, not per buffer: each carries the `response_id` it is about.

    `FINISHED` is `output_audio_buffer.stopped`: "Emitted when the output audio
    buffer has been completely drained on the server, and no more audio is
    forthcoming. This event is emitted after the full response data has been
    sent to the client (`response.done`)." Fields: `event_id`, `response_id`,
    `type`. `STARTED` is its opening word, `output_audio_buffer.started`.
    Verified 2026-09-05 against `openai/openai-python` main,
    `src/openai/types/realtime/realtime_server_event.py:79-113`
    (`OutputAudioBufferStarted`, `OutputAudioBufferStopped`; last changed
    2026-08-10), whose docstrings link
    https://platform.openai.com/docs/guides/realtime-conversations#client-and-server-events-for-audio-in-webrtc
    — the rendered reference at developers.openai.com surfaces the family only
    through those SDK types.

    **Whether this backend sends them is what the next run decides.** The call
    is codex's `realtime/calls` proxy (`intent=quicksilver&architecture=avas`,
    v3 vocabulary: `turn.done`, `output_audio.delta`), not the public API, and
    codex itself has no WebRTC client that reads the channel, so no source
    settles it. `_WebRtcTransport` therefore logs each event type the channel
    carries, once per call, and the stop-edge line names the fact that closed
    it — the run's engine log answers the question either way.

    `STARTED`'s one job here is to take back a `FINISHED` that arrived for the
    previous response after the quiet rule had already closed that span —
    left latched, it would close the next span the moment its buffer was
    empty. Any other event type on the channel is not this family and is
    dropped where the channel is read.
    """

    STARTED = "output_audio_buffer.started"
    FINISHED = "output_audio_buffer.stopped"


class SpanClosedBy(enum.Enum):
    """Which fact closed a span of the Voice's playout (#235).

    The value is the clause the stop-edge line carries, so the log and the code
    cannot name the same fact two ways. `SERVER` is the rule; `QUIET` is the
    fallback for a peer that never says so; a wait that ran out its bound has
    no member here, because nothing closed it.
    """

    SERVER = "the server said its audio had finished"
    QUIET = "inbound audio went quiet"


@dataclass(frozen=True)
class Playout:
    """What the speaker saw over one stretch of waiting — the four facts (#230).

    Until #235, `drained` was decided by two things this side cannot influence
    and did not report: whether inbound frames stopped for
    `PLAYBACK_QUIET_FRAMES` frames, and whether the device still has audio
    queued. So a stop edge that ran out its bound said only that it had, and
    two entirely different causes — a remote peer that keeps RTP flowing
    through silence, and an event loop starved into delivering frames in
    bursts — arrived at the same one line.

    The four together are what tells them apart. A large `largest_gap_seconds`
    with `frames` still climbing is a starved loop: the pauses were real, and
    the burst that followed refreshed the trailing gap before anyone looked. A
    small one beside a small `since_last_frame_seconds` is a peer that never
    stopped sending. Either beside a non-zero `buffered_bytes` is neither: the
    device stalled with audio still queued, which leaves no `dropped` line
    either, and was the one candidate the ticket's own reasoning did not
    exclude.

    Read as a value, never as live attributes: every field is measured at the
    same instant, and a reader comparing two of them sampled a moment apart
    would be comparing two different stalls.
    """

    #: Inbound frames over the window — see `_Speaker.take_playout` for which.
    frames: int
    #: How long ago the last inbound frame arrived, `None` if none ever has.
    #: Measured from the whole call, not the window: a window with no frames in
    #: it still has a meaningful answer, and it is the interesting one.
    since_last_frame_seconds: float | None
    #: The longest silence between two consecutive frames in the window, `None`
    #: when the window holds no gap to measure.
    largest_gap_seconds: float | None
    #: What was still queued for the device, and so still unplayed.
    buffered_bytes: int

    def __str__(self) -> str:
        """One clause per fact, in the order a reader needs them.

        Rendered here rather than at each log call so the two lines that carry
        this cannot drift into two vocabularies for one measurement. Absent is
        spelled out rather than printed as zero: "no frame ever arrived" and "a
        gap of zero" are opposite diagnoses.
        """
        last = self.since_last_frame_seconds
        widest = self.largest_gap_seconds
        return (
            f"{self.frames} inbound frame{'' if self.frames == 1 else 's'}, "
            f"{'none has arrived' if last is None else f'last {last:.3f}s ago'}, "
            f"{'no gap measured' if widest is None else f'largest gap {widest:.3f}s'}, "
            f"{self.buffered_bytes} bytes still buffered"
        )


def probe() -> None:
    """Prove the voice extra is really installed, or refuse by name.

    Called when the adapter is constructed — that is, while the engine is being
    assembled — so a deployment that configured this Call adapter without the
    dependencies it needs fails at start with a sentence naming the fix. ADR
    0003's rule is that a seam configured but not loadable is an outage; voice
    is this product's main path, and discovering the outage at the moment the
    user tries to talk is the worst possible place to discover it.
    """
    missing = []
    for name in REQUIRED:
        try:
            __import__(name)
        except ImportError:
            missing.append(name)
    if missing:
        raise VoiceDependencyError(
            f"the bridge-owned realtime call needs {', '.join(missing)}, which "
            f"{'are' if len(missing) > 1 else 'is'} not installed: {INSTALL_HINT}"
        )


def webrtc_transport(
    *, input_device: int | None = None, output_device: int | None = None, silent: bool = False
) -> CallTransport:
    """One WebRTC audio path, ready to be offered.

    `silent` opens no audio device at all: it sends paced silence and counts
    what comes back. That is what makes a signalling round trip against a real
    app-server runnable on a machine with no microphone grant, and it is the
    only reason the flag exists — it is not a mode the engine ever selects.
    """
    probe()
    return _WebRtcTransport(input_device=input_device, output_device=output_device, silent=silent)


class _WebRtcTransport:
    """`aiortc` behind the `CallTransport` interface. Built by `webrtc_transport`."""

    def __init__(
        self, *, input_device: int | None, output_device: int | None, silent: bool
    ) -> None:
        from aiortc import RTCPeerConnection

        self._pc = RTCPeerConnection()
        self._silent = silent
        self._microphone = _Microphone(silent=silent, device=input_device)
        self._speaker = _Speaker(silent=silent, device=output_device)
        self._on_lost: LostHandler | None = None
        self._closing = False
        #: Whether the loss has already been reported upward. Its own flag, and
        #: not `_closing`: see `_note`.
        self._reported = False
        self._connected: asyncio.Future[None] = asyncio.get_event_loop().create_future()
        self._connected.add_done_callback(_retrieved)

        # The realtime events data channel. The backend expects the offer to
        # describe one. Every event this adapter *acts* on still arrives as a
        # JSON-RPC notification on the app-server socket — one stream of truth
        # for the call's course. What is read here is one family only: the
        # server's word that a response's audio has finished playing out, which
        # exists nowhere else (#235; `OutputAudioEvent`). Event types seen
        # on the channel are named in the log once each, so a run shows what
        # this backend actually sends.
        self._events_seen: set[str] = set()
        channel = self._pc.createDataChannel(EVENTS_CHANNEL)

        @channel.on("message")
        def _on_message(message: Any) -> None:
            self._read_channel_event(message)

        self._pc.addTrack(self._microphone.track())

        @self._pc.on("track")
        def _on_track(track: Any) -> None:
            if track.kind == "audio":
                self._speaker.attach(track)

        @self._pc.on("connectionstatechange")
        def _on_state() -> None:
            self._note(self._pc.connectionState)

    # -- the interface ----------------------------------------------------

    async def offer(self) -> str:
        offer = await self._pc.createOffer()
        await self._pc.setLocalDescription(offer)
        # aiortc gathers without trickling, so this settles in milliseconds.
        while self._pc.iceGatheringState != "complete":
            await asyncio.sleep(0.05)
        description = self._pc.localDescription
        if description is None:  # pragma: no cover - aiortc always sets one
            raise TransportError("the peer connection produced no local description")
        return str(description.sdp)

    async def accept_answer(self, sdp: str) -> None:
        from aiortc import RTCSessionDescription

        try:
            await self._pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="answer"))
        except Exception as unusable:
            raise TransportError(f"the app-server's SDP answer was refused: {unusable}") from None

    async def wait_connected(self, timeout_seconds: float) -> None:
        self._note(self._pc.connectionState)
        try:
            await asyncio.wait_for(asyncio.shield(self._connected), timeout_seconds)
        except TimeoutError:
            raise TransportError(
                f"audio never started flowing within {timeout_seconds:g}s "
                f"(the peer connection is {self._pc.connectionState})"
            ) from None

    @property
    def is_connected(self) -> bool:
        return bool(self._pc.connectionState == "connected")

    async def playback_drained(self, timeout_seconds: float) -> None:
        """Wait out the speaker, within a bound. See `CallTransport.playback_drained`.

        **Both exits say what the speaker saw** (#230). The timeout line used to
        report that a thing had not happened and nothing about why, which left
        the one measurement that distinguishes a peer still sending from a
        starved event loop unrecorded on the only run that needed it. The
        ordinary exit carries the same four facts for the same reason a control
        needs to be measured too: a stalled run is only diagnosable against what
        a clean one on this machine looks like.

        The window is closed on the way past either exit and never twice, so
        each line describes exactly the wait it ends. `Playout` renders itself,
        so this decides nothing about wording.
        """
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while (closed_by := self._speaker.closed_by) is None:
            if asyncio.get_running_loop().time() >= deadline:
                _log.info(
                    "playout had not drained %gs after the Voice stopped generating "
                    "(neither the server's word nor quiet arrived); "
                    "reporting the stop edge anyway — %s",
                    timeout_seconds,
                    self._speaker.take_playout(),
                )
                return
            await asyncio.sleep(PLAYBACK_POLL_SECONDS)
        _log.info(
            "the Voice's playout drained: %s — %s", closed_by.value, self._speaker.take_playout()
        )

    def _read_channel_event(self, message: Any) -> None:
        """One message off the events channel, parsed for the speaker (#235).

        Anything that is not a JSON object with a string `type` is not a server
        event and is dropped; a type outside `OutputAudioEvent` is dropped here
        too, so the speaker only ever hears a typed word. Each type is named in
        the log the first time this call sees it — the engine log has to show,
        on its own, whether this backend sends `OutputAudioEvent.FINISHED` at all.
        """
        if isinstance(message, bytes | bytearray):
            message = message.decode("utf-8", errors="replace")
        if not isinstance(message, str):
            return
        try:
            event = json.loads(message)
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        if not isinstance(kind, str):
            return
        if kind not in self._events_seen:
            self._events_seen.add(kind)
            _log.info("the realtime events channel carried %s", kind)
        try:
            event = OutputAudioEvent(kind)
        except ValueError:
            return
        self._speaker.heard_from_server(event)

    def on_lost(self, handler: LostHandler) -> None:
        self._on_lost = handler

    async def aclose(self) -> None:
        if self._closing:
            return
        self._closing = True
        if not self._connected.done():
            # Cancelled rather than failed: a close this side asked for is not
            # the connection having gone wrong, and anything still waiting on
            # the handshake is being abandoned, not told about a fault.
            self._connected.cancel()
        self._microphone.stop()
        self._speaker.stop()
        with contextlib.suppress(Exception):
            await self._pc.close()

    # -- state ------------------------------------------------------------

    def _note(self, state: str) -> None:
        """One connection-state reading, turned into the two things that matter.

        **Reporting a loss is not closing.** This once set `_closing` to keep
        itself from reporting the same loss twice, and `_closing` is also what
        makes `aclose` idempotent — so a connection that went away by itself was
        marked closed without anything having been closed, and the `aclose` the
        adapter then ran returned at the first line. The microphone and the
        speaker stayed open on a dead call, which is a device held and a
        microphone live with nothing listening. Two facts, two flags.
        """
        if state == "connected" and not self._connected.done():
            self._connected.set_result(None)
            return
        if state not in ("failed", "closed"):
            return
        reason = f"the call's audio connection is {state}"
        if not self._connected.done():
            self._connected.set_exception(TransportError(reason))
        if self._closing or self._reported:
            return  # a close this side asked for is not a loss, and once is enough
        self._reported = True
        handler, self._on_lost = self._on_lost, None
        if handler is not None:
            handler(reason)


def _retrieved(waiting: asyncio.Future[None]) -> None:
    """Look at the outcome, so one nobody awaited is not an asyncio warning.

    Whether anything is waiting on the handshake depends on exactly where in it
    the connection died — and on a call that failed before `wait_connected` was
    reached, an "exception was never retrieved" warning is noise standing where
    the real reason, already reported upward as a dropped call, should be.
    """
    if not waiting.cancelled():
        waiting.exception()


class CuePlayer:
    """A second output stream, opened for one cue and closed after it.

    **Beside the call's own `_Speaker`, not inside it.** The cue that matters
    most plays when there is no call left to play it through: `ENDED` goes out
    after the peer connection and its speaker have already closed. Mixing cues
    into the playback buffer would tie the two lifetimes together and lose
    exactly that one. A stream per cue also costs nothing at all in between —
    there is no device held open for a sound that plays three times an hour.

    The stream's parameters are the speaker's own (48 kHz, mono, int16, 960-frame
    blocks), so a cue needs no second opinion about what this machine's audio
    path looks like, and it honours the same `output_device` the call does.

    **It starts no thread.** `play` blocks — the write is a device write and
    `stop` drains after it, which #174 measured at 320-620 ms of wall time for
    60-300 ms of sound — and the caller decides where that runs and what a
    failure means. This class knows about a device and nothing else.

    **What is playing is a set, not a slot.** The adapter feeds this from a
    single worker, so in practice the cues arrive one at a time — but `play_now`
    is public and #145 may call it, so a second playback is something this class
    cannot see coming. A single `_playing` slot cleared by whoever finishes gets
    that wrong in one of the two orders: the later playback finishing first
    would announce silence while the earlier one is still writing, and a capture
    gate reading that opens the microphone into a live tone. So every playback
    in flight is held, and `playing` answers from the newest of them.
    """

    def __init__(self, *, device: int | None = None) -> None:
        self._device = device
        self._lock = threading.Lock()
        self._spans: list[Any] = []

    @property
    def device(self) -> int | None:
        return self._device

    @property
    def playing(self) -> Any | None:
        """The span going out right now, or None. Read by #145's capture gate.

        The newest when more than one is in flight: what a gate asks is whether
        a cue is sounding, and the newest is the one that has longest to run.
        """
        with self._lock:
            return self._spans[-1] if self._spans else None

    def play(self, pcm: bytes, *, span: Any = None) -> None:
        """Open, write, stop, close. Blocking, and raises what the library raises."""
        import sounddevice

        stream = sounddevice.RawOutputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype=SAMPLE_FORMAT,
            blocksize=FRAME_SAMPLES,
            device=self._device,
        )
        # Held only once the stream exists: a cue that could not open a device
        # never occupied one, and a gate reading `playing` would otherwise hold
        # capture shut over a sound nobody made.
        with self._lock:
            self._spans.append(span)
        try:
            stream.start()
            try:
                stream.write(pcm)
                stream.stop()
            finally:
                stream.close()
        finally:
            with self._lock:
                # By identity: two cues of the same kind a moment apart are equal
                # spans, and dropping "one that compares equal" is how a playback
                # releases somebody else's.
                self._spans[:] = [held for held in self._spans if held is not span]


class _Microphone:
    """What the user says, as 20 ms frames, or paced silence in a silent run."""

    def __init__(self, *, silent: bool, device: int | None) -> None:
        self._silent = silent
        self._device = device
        self._stream: Any = None
        self._track: Any = None
        self._frames: asyncio.Queue[bytes] = asyncio.Queue(maxsize=MAX_CAPTURE_FRAMES)
        self._dropped = 0

    def track(self) -> Any:
        from aiortc.mediastreams import AudioStreamTrack

        microphone = self

        class _Track(AudioStreamTrack):  # type: ignore[misc, valid-type]
            def __init__(self) -> None:
                super().__init__()
                self._pts = 0
                self._started: float | None = None

            async def recv(self) -> Any:
                import av

                payload = await microphone._next(self)
                frame = av.AudioFrame(format="s16", layout="mono", samples=FRAME_SAMPLES)
                frame.planes[0].update(payload)
                frame.sample_rate = SAMPLE_RATE
                frame.pts = self._pts
                frame.time_base = fractions.Fraction(1, SAMPLE_RATE)
                self._pts += FRAME_SAMPLES
                return frame

        self._track = _Track()
        if not self._silent:
            self._open()
        return self._track

    async def _next(self, track: Any) -> bytes:
        """One frame's worth of PCM: captured, or silence paced in real time.

        Silence has to be *paced*. Handing the encoder frames as fast as it asks
        would run the media clock far ahead of the wall clock, and the far side
        would hear a call that had already ended.
        """
        if not self._silent:
            return await self._frames.get()
        if track._started is None:
            track._started = time.monotonic()
        delay = track._started + track._pts / SAMPLE_RATE - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        return b"\x00\x00" * FRAME_SAMPLES

    def _open(self) -> None:
        import sounddevice

        loop = asyncio.get_event_loop()

        def push(data: bytes) -> None:
            if self._frames.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    self._frames.get_nowait()
                self._dropped += 1
            self._frames.put_nowait(data)

        def captured(indata: Any, _frames: int, _time: Any, status: Any) -> None:
            if status:
                _log.info("microphone reported %s", status)
            loop.call_soon_threadsafe(push, bytes(indata))

        try:
            self._stream = sounddevice.RawInputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype=SAMPLE_FORMAT,
                blocksize=FRAME_SAMPLES,
                device=self._device,
                callback=captured,
            )
            self._stream.start()
        except Exception as unavailable:
            raise TransportError(f"the microphone could not be opened: {unavailable}") from None

    def stop(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            with contextlib.suppress(Exception):
                stream.stop()
                stream.close()
        track, self._track = self._track, None
        if track is not None:
            with contextlib.suppress(Exception):
                track.stop()
        if self._dropped:
            _log.info("dropped %d captured frames while the consumer lagged", self._dropped)


class _Speaker:
    """What the call says, played out — or counted, in a silent run.

    **The drain rule, decided from run data (#235).** A span of the Voice's
    playout is over when nothing is still queued for the device *and* one of:

    1. the server said the response's audio has finished playing out
       (`OutputAudioEvent.FINISHED`, read off the events data channel) — the rule;
    2. no inbound frame has arrived for `PLAYBACK_QUIET_FRAMES` frames — the
       fallback, for a peer that never sends the word;

    and a wait that reaches neither is closed by `playback_drained`'s bound as
    the last resort, which should then almost never fire.

    Decided by runs `20260905T071849Z`, `075128Z`, `090222Z` and `092046Z`:
    the bound fired seven times, every time with 0 bytes buffered, the last
    inbound frame 2-20 ms ago and the largest gap under 0.35 s — around
    10 000 frames (≈200 s of audio) held open behind a 15-second answer. That
    is a peer that keeps streaming silence after the Voice has finished, so
    the quiet rule alone can never close the span; not a starved event loop,
    which would have shown buffered bytes and large gaps. Whether the peer pads
    its stream is the peer's business, so what closes a span is now the
    server's own word, and the quiet rule is kept for the runs — `075128Z`
    drained 13 of 13 spans on it — where the peer does stop.
    """

    def __init__(self, *, silent: bool, device: int | None) -> None:
        self._silent = silent
        self._device = device
        self._stream: Any = None
        self._task: asyncio.Task[None] | None = None
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._dropped = 0
        #: When the last inbound frame arrived. `None` while none has, which
        #: reads as drained: there is nothing playing that has not finished.
        self._last_frame_at: float | None = None
        #: The window `take_playout` reports and then clears. All three are
        #: touched only from the receive loop and read only from
        #: `playback_drained`, which runs on the same event loop — so unlike
        #: `_buffer`, which a device callback thread also drains, they need no
        #: lock. `_lock` guards the buffer and nothing else.
        self._window_frames = 0
        self._window_largest_gap: float | None = None
        #: The frame the next gap is measured from, cleared at every window
        #: boundary — which is what keeps `_window_largest_gap` a gap *between
        #: two frames of this window* and not the idle since the last one.
        self._window_gap_from: float | None = None
        #: Whether the server has said the current response's audio finished
        #: playing out (#235). Set from the events channel, taken back by the
        #: next response starting, and consumed with the window it closed.
        #: Touched from the data-channel callback and read from
        #: `playback_drained`, both on the event loop: no lock.
        self._server_finished = False

    def heard_from_server(self, event: OutputAudioEvent) -> None:
        """The server's word about the current response's audio: finished, or not yet.

        `STARTED` while a wait is still polling for the previous span erases
        nothing that matters: the Voice speaking again is what makes that wait
        stale in the adapter (`speaking_span` moves on), and the span stays
        open — correctly — until this response's own `FINISHED`.
        """
        self._server_finished = event is OutputAudioEvent.FINISHED

    @property
    def closed_by(self) -> SpanClosedBy | None:
        """Which fact says the last inbound frame has finished playing, if any.

        Nothing still queued for the device is required first: whatever the
        server or the quiet rule says, audio this side has not yet written is
        audio the user has not yet heard. A silent run buffers nothing, which
        leaves the other fact answering on its own — correctly, because there
        is no speaker for audio to trail in. No frame ever arriving reads as
        quiet: there is nothing playing that has not finished.
        """
        with self._lock:
            if self._buffer:
                return None
        if self._server_finished:
            return SpanClosedBy.SERVER
        if (
            self._last_frame_at is None
            or time.monotonic() - self._last_frame_at >= PLAYBACK_QUIET_FRAMES * FRAME_SECONDS
        ):
            return SpanClosedBy.QUIET
        return None

    @property
    def playout(self) -> Playout:
        """What the open window holds right now, taking nothing from it.

        Reading and closing are separate so that reading is safe: a second
        caller — part 2 of #230 will want one — must be able to look without
        silently emptying the window the stop edge is about to report.

        `since_last_frame_seconds` deliberately spans windows: a window with no
        frames in it at all still has an answer, and "nothing has arrived for
        four minutes" is exactly the reading that matters there.
        """
        with self._lock:
            buffered = len(self._buffer)
        now = time.monotonic()
        return Playout(
            frames=self._window_frames,
            since_last_frame_seconds=(
                None if self._last_frame_at is None else now - self._last_frame_at
            ),
            largest_gap_seconds=self._window_largest_gap,
            buffered_bytes=buffered,
        )

    def take_playout(self) -> Playout:
        """The window just ended, and the start of the next one (#230).

        `playback_drained` calls this exactly once per wait, so a window is one
        stretch of the Voice speaking and two of them on one call are directly
        comparable — which is the whole point, since the question a reader
        brings to these numbers is why *this* stretch stalled when the last one
        did not. A cumulative count over a call could not answer it: by the
        third stall the totals are dominated by the audio that played correctly.
        """
        taken = self.playout
        self._window_frames = 0
        self._window_largest_gap = None
        self._window_gap_from = None
        self._server_finished = False
        return taken

    def attach(self, track: Any) -> None:
        if not self._silent and self._stream is None:
            self._open()
        self._task = asyncio.ensure_future(self._playing(track))

    async def _playing(self, track: Any) -> None:
        import av

        resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
        while True:
            try:
                frame = await track.recv()
            except Exception:
                return
            arrived = time.monotonic()
            if self._window_gap_from is not None:
                # Between two frames of *this* window, never across its opening
                # edge. The interval from the previous window's last frame to
                # this one's first is the user's whole turn — several seconds of
                # perfectly correct silence — and letting it in would make it
                # the maximum on every healthy call, burying the 1.4s hole this
                # measurement exists to find (#230).
                gap = arrived - self._window_gap_from
                if self._window_largest_gap is None or gap > self._window_largest_gap:
                    self._window_largest_gap = gap
            self._window_gap_from = arrived
            self._window_frames += 1
            self._last_frame_at = arrived
            if self._silent:
                continue
            for out in resampler.resample(frame):
                # Only the first `samples * SAMPLE_BYTES` bytes are real audio;
                # the rest of the plane is padding, and padding is audible static.
                chunk = bytes(out.planes[0])[: out.samples * SAMPLE_BYTES]
                with self._lock:
                    self._buffer.extend(chunk)
                    overflow = len(self._buffer) - MAX_PLAYBACK_BYTES
                    if overflow > 0:
                        del self._buffer[:overflow]
                        self._dropped += 1

    def _open(self) -> None:
        import sounddevice

        def wanted(outdata: Any, frames: int, _time: Any, _status: Any) -> None:
            need = frames * SAMPLE_BYTES
            with self._lock:
                available = bytes(self._buffer[:need])
                del self._buffer[: len(available)]
            outdata[: len(available)] = available
            if len(available) < need:
                outdata[len(available) :] = b"\x00" * (need - len(available))

        try:
            self._stream = sounddevice.RawOutputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype=SAMPLE_FORMAT,
                blocksize=FRAME_SAMPLES,
                device=self._device,
                callback=wanted,
            )
            self._stream.start()
        except Exception as unavailable:
            raise TransportError(f"the speaker could not be opened: {unavailable}") from None

    def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
        stream, self._stream = self._stream, None
        if stream is not None:
            with contextlib.suppress(Exception):
                stream.stop()
                stream.close()
        if self._dropped:
            _log.info("dropped playback audio %d times while the buffer overflowed", self._dropped)

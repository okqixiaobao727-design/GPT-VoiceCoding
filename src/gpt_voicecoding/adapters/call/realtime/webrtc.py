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
import fractions
import logging
import threading
import time
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
        # describe one; nothing here reads it, because every event this adapter
        # acts on arrives as a JSON-RPC notification on the app-server socket
        # instead — one stream of truth rather than two that can disagree.
        self._pc.createDataChannel("realtime-events")
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
    """What the call says, played out — or counted, in a silent run."""

    def __init__(self, *, silent: bool, device: int | None) -> None:
        self._silent = silent
        self._device = device
        self._stream: Any = None
        self._task: asyncio.Task[None] | None = None
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._frames = 0
        self._dropped = 0

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
            self._frames += 1
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

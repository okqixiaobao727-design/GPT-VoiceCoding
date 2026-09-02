"""The three call cues, synthesised with the standard library and nothing else.

## What is here, and why it is not in `webrtc.py`

A cue is two things: a waveform, and a stream to play it on. The stream is the
audio module's (`webrtc.CuePlayer`) because it opens a device; the waveform is
here because it opens nothing. #174's resolution says the real adapter
synthesises these shapes "from named module constants inside `webrtc.py`", and
this module is a deliberate deviation from that sentence, for a reason that
comment could not have weighed: `webrtc.py` is the one file allowed to import
the audio libraries, those libraries are an optional extra CI never installs,
and #186 asks for the durations and the peaks to be *graded*. Synthesis that
lives here is graded on every push (`tests/test_call_cues.py`); synthesis that
lived there would be graded on one laptop. The confinement rule is untouched —
this module imports no audio library at all, which
`tests/test_architecture.py` asserts by reading it.

## The table is the specification; the reference WAVs are not

The shapes were chosen by ear on this machine (#174: three candidates a moment,
played on the very path this module feeds) and the reference renderings sit on
the local branch `prototype/174-tone-cues`. Those WAVs were written with a naive
gain — the prototype multiplied a unit waveform by the target peak — which
lands the ENDED cue about 1.2 dB *under* its stated peak, because summing a
second harmonic onto a sine does not produce a unit peak. #174's table states
peaks, so a cue here is **normalised** to reach the one it was given, and that
is the number a listener heard.

## The design rule, not just the picked sounds

CONNECTED is three different notes rising and ENDED is two different notes
falling; EVENT is two *identical* notes and never carries a melody. That is what
keeps the mid-call cue from being heard as a small connect, whichever way round
a listener remembers the other two. EVENT also peaks 6 dB hotter than the pair,
because it is the only one that has to arrive over the Voice's own speech.

## Legacy (ADR 0010)

None — **dropped, because** legacy never owned the audio path at all
(`legacy@1d32845:bridge/livecall.py:1-30` reads the host application's log, and
its only contact with audio is an `audio_duration_ms` regex at `:97-100`).
"""

from __future__ import annotations

import array
import math
from dataclasses import dataclass
from functools import cache

from gpt_voicecoding.adapters.call.realtime.webrtc import (
    FRAME_SAMPLES,
    SAMPLE_BYTES,
    SAMPLE_RATE,
)
from gpt_voicecoding.seams.call import Cue

#: Full scale for one signed 16-bit sample. A peak in dBFS is measured against
#: this and nothing else.
FULL_SCALE = 32767

#: How far into a note the exponential tail has fallen by the note's end: about
#: 5 %, which is percussive without buzzing (#174 heard every candidate with it).
TAIL = 3.0

#: What `output_device` reads as in a log line when nothing was configured. The
#: machine's own default is an answer, and an empty space where an index should
#: be is not.
DEFAULT_DEVICE = "default"


@dataclass(frozen=True, slots=True)
class Note:
    """One note of a cue: a pitch, a length, and how it opens and closes.

    `attack` is the fraction of the note spent rising from silence, and what is
    left decays to `TAIL`. Both ends matter for the same reason: a waveform that
    starts or stops at a non-zero sample is a step, and a step is a click.
    """

    hz: float
    ms: int
    attack: float = 0.08
    #: How much second harmonic rides on the fundamental, relative to it. It is
    #: what makes a note read through room noise rather than louder.
    bright: float = 0.0


@dataclass(frozen=True, slots=True)
class CueShape:
    """One cue as #174's table states it: its notes, its gaps and its peak."""

    notes: tuple[Note, ...]
    gap_ms: int
    peak_dbfs: float


@dataclass(frozen=True, slots=True)
class CueSpan:
    """What one cue occupies on the output device while it is going out.

    Held by the player for exactly as long as the cue is playing, so the capture
    side can be gated on it (#145): the microphone is open through a mid-call
    cue, and EVENT is 6 dB hotter than the rest precisely so it carries — which
    is also what makes it loud enough to come back in.

    `started` is a monotonic reading, because what a gate needs is an elapsed
    time and never a wall-clock date.
    """

    cue: Cue
    device: int | None
    started: float
    frames: int

    @property
    def seconds(self) -> float:
        return self.frames / SAMPLE_RATE


#: #174's chosen cues, one shape a moment. Named constants rather than literals
#: at the call site, so the table and the code are the same thing.
CONNECTED = CueShape(
    notes=(Note(hz=523, ms=70), Note(hz=659, ms=70), Note(hz=784, ms=130)),
    gap_ms=15,
    peak_dbfs=-12.0,
)
ENDED = CueShape(
    notes=(Note(hz=990, ms=80, bright=0.35), Note(hz=660, ms=140, bright=0.35)),
    gap_ms=20,
    peak_dbfs=-12.0,
)
EVENT = CueShape(
    notes=(Note(hz=1320, ms=45, attack=0.15), Note(hz=1320, ms=45, attack=0.15)),
    gap_ms=60,
    #: Six decibels hotter than the other two, and the one number in this table
    #: that is not symmetrical with them: this is the cue that lands while the
    #: Voice is speaking, and at -12 dBFS it was inaudible under one (#174).
    peak_dbfs=-6.0,
)

#: Every moment the seam can name, and the sound it is heard as.
SHAPES: dict[Cue, CueShape] = {Cue.CONNECTED: CONNECTED, Cue.ENDED: ENDED, Cue.EVENT: EVENT}


@cache
def render(cue: Cue) -> bytes:
    """One cue as 16-bit mono PCM at the speaker's own rate.

    Cached because a cue is a constant: the same three buffers are played for as
    long as the engine runs, and synthesising one per call would be arithmetic
    repeated to produce a byte-identical answer.
    """
    shape = SHAPES[cue]
    wave: list[float] = []
    gap = [0.0] * round(SAMPLE_RATE * shape.gap_ms / 1000)
    for index, note in enumerate(shape.notes):
        if index:
            wave.extend(gap)
        wave.extend(_note(note))
    # Padded out to whole blocks, so the stream drains without a partial one at
    # the end. It is what makes EVENT's ~150 ms of sound the ~160 ms the table
    # states; the other two land on a block boundary already and gain nothing.
    remainder = len(wave) % FRAME_SAMPLES
    if remainder:
        wave.extend([0.0] * (FRAME_SAMPLES - remainder))
    return _at_peak(wave, shape.peak_dbfs)


def _note(note: Note) -> list[float]:
    """One note as a unit-ish waveform: attack ramp, exponential tail, no click."""
    total = round(SAMPLE_RATE * note.ms / 1000)
    rise = max(1, int(total * note.attack))
    shaped: list[float] = []
    for index in range(total):
        seconds = index / SAMPLE_RATE
        if index < rise:
            envelope = index / rise
        else:
            envelope = math.exp(-TAIL * (index - rise) / max(1, total - rise))
        value = math.sin(2 * math.pi * note.hz * seconds)
        if note.bright:
            value = (value + note.bright * math.sin(4 * math.pi * note.hz * seconds)) / (
                1 + note.bright
            )
        shaped.append(envelope * value)
    return shaped


def _at_peak(wave: list[float], dbfs: float) -> bytes:
    """Scale a waveform so its loudest sample sits at `dbfs`, and quantise it.

    Normalised rather than multiplied. The prototype multiplied by the target
    and called the result that peak, which is true only for a pure sine: a note
    carrying a second harmonic sums to about 0.87 of a unit peak, so the ENDED
    cue came out 1.2 dB quiet. #174's table states peaks, and this is what makes
    the stated one the delivered one.
    """
    loudest = max((abs(value) for value in wave), default=0.0)
    if loudest == 0.0:  # pragma: no cover - every shape has at least one note
        return bytes(len(wave) * SAMPLE_BYTES)
    scale = FULL_SCALE * (10 ** (dbfs / 20)) / loudest
    return array.array("h", [round(value * scale) for value in wave]).tobytes()


def frames_in(pcm: bytes) -> int:
    """How many samples one rendered cue holds.

    Its own function because everything that wants a span wants this number —
    the adapter that logs one, and the acceptance step that composes the line it
    looks for — and `len(pcm) // 2` written at each of them is the sample width
    restated as arithmetic nobody can search for.
    """
    return len(pcm) // SAMPLE_BYTES


def cue_phrase(cue: Cue) -> str:
    """How a played cue names itself in the engine's log.

    Its own function because it is read on both sides of a process boundary: the
    adapter writes it, and the acceptance harness looks for it in the engine log
    (#186 — the engine writes no line at all for `CallStarted` or `CallEnded`,
    so this is the only witness a step has that a cue went out).
    """
    return f"played the {cue} cue"


def played_line(span: CueSpan) -> str:
    """The whole line: which cue, which output device, and the span written."""
    device = DEFAULT_DEVICE if span.device is None else span.device
    return (
        f"{cue_phrase(span.cue)} on output device {device}: "
        f"{span.frames} frames, {span.seconds:.3f}s"
    )

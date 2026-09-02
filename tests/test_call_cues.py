"""The three call cues as sound: #186's table, graded rather than remembered.

The synthesis is standard-library PCM in a module that imports no audio library
at all (`tests/test_architecture.py`), so everything here runs in CI, which never
installs the voice extra. Everything except the one thing that matters most:
whether a cue is *audible* is #174's ear test, which a harness cannot run and is
never asked to (#181).

**The table is the spec, and the prototype is not.** #174's resolution comment
is where these shapes were chosen by ear and written down; the branch
`prototype/174-tone-cues` holds the prototype that played them and the reference
WAVs it exported, and is named second because a branch is a thing one machine can
lose. Those WAVs came from a naive gain, which leaves a cue carrying a second
harmonic over a decibel under the peak it was supposed to have. The peaks below
are the ones #174 and #186 state, and a cue is normalised to reach them.
"""

from __future__ import annotations

import array
import math

import pytest

from gpt_voicecoding.adapters.call.realtime import cues
from gpt_voicecoding.seams.call import Cue

#: #186's table, one row a cue: how long the cue lasts, what it peaks at, and how
#: many distinct pitches it is allowed to use. The durations are the padded ones
#: — a cue is whole 20 ms blocks, so the stream drains without a tail — and the
#: peaks are dBFS.
TABLE = (
    (Cue.CONNECTED, 300, -12.0, 3),
    (Cue.ENDED, 240, -12.0, 2),
    (Cue.EVENT, 160, -6.0, 1),
)


def samples(pcm: bytes) -> array.array[int]:
    """One cue's PCM as signed 16-bit samples."""
    read = array.array("h")
    read.frombytes(pcm)
    return read


def peak_dbfs(pcm: bytes) -> float:
    """How loud the loudest sample in one cue is, relative to full scale."""
    loudest = max(abs(sample) for sample in samples(pcm))
    return 20 * math.log10(loudest / cues.FULL_SCALE)


def duration_ms(pcm: bytes) -> float:
    return cues.frames_in(pcm) / cues.SAMPLE_RATE * 1000


class TestTheTable:
    @pytest.mark.parametrize(("cue", "ms", "_dbfs", "_pitches"), TABLE)
    def test_each_cue_is_as_long_as_the_table_says(
        self, cue: Cue, ms: float, _dbfs: float, _pitches: int
    ) -> None:
        assert duration_ms(cues.render(cue)) == pytest.approx(ms)

    @pytest.mark.parametrize(("cue", "_ms", "dbfs", "_pitches"), TABLE)
    def test_each_cue_peaks_where_the_table_says(
        self, cue: Cue, _ms: float, dbfs: float, _pitches: int
    ) -> None:
        """Within half a decibel, which is finer than anyone hears and coarser
        than rounding to 16 bits."""
        assert peak_dbfs(cues.render(cue)) == pytest.approx(dbfs, abs=0.5)

    @pytest.mark.parametrize(("cue", "_ms", "_dbfs", "pitches"), TABLE)
    def test_each_cue_uses_the_pitches_the_table_gives_it(
        self, cue: Cue, _ms: float, _dbfs: float, pitches: int
    ) -> None:
        assert len({note.hz for note in cues.SHAPES[cue].notes}) == pitches

    def test_the_event_cue_never_carries_a_melody(self) -> None:
        """#186's design rule, and the reason EVENT is not just a shorter CONNECTED.

        A rise or a fall is a *statement* about the call, and the mid-call cue
        makes none: it says only that something happened. Two notes at one pitch
        cannot be mistaken for either of the two-different-note cues, whichever
        way round a listener remembers them.
        """
        assert len({note.hz for note in cues.SHAPES[Cue.EVENT].notes}) == 1

    def test_the_event_cue_is_the_loud_one_because_it_lands_on_speech(self) -> None:
        """The only cue that has to be heard over the Voice, and 6 dB is the margin."""
        over_speech = cues.SHAPES[Cue.EVENT].peak_dbfs
        for quiet in (Cue.CONNECTED, Cue.ENDED):
            assert over_speech - cues.SHAPES[quiet].peak_dbfs == pytest.approx(6.0)

    def test_every_moment_the_seam_names_has_a_sound(self) -> None:
        """The seam's verb takes a `Cue`, so every member of it must be playable."""
        assert set(cues.SHAPES) == set(Cue)


class TestTheWaveform:
    @pytest.mark.parametrize("cue", list(Cue))
    def test_a_cue_opens_on_silence_and_closes_on_its_own_tail(self, cue: Cue) -> None:
        """No click at either end, which is two different claims.

        The first sample is exactly zero: every note opens on an attack ramp
        from nothing. The last one is not zero and is not asked to be — the
        notes close on an exponential tail that has fallen to `TAIL` of the
        note, and that is the waveform #174 listened to. What "no click" means
        there is that the step left at the end is inaudible, so it is graded
        against the cue's own tail and against -30 dBFS.
        """
        read = samples(cues.render(cue))
        assert read[0] == 0
        peak = cues.FULL_SCALE * 10 ** (cues.SHAPES[cue].peak_dbfs / 20)
        assert abs(read[-1]) <= peak * math.exp(-cues.TAIL)
        assert abs(read[-1]) <= cues.FULL_SCALE * 10 ** (-30 / 20)

    @pytest.mark.parametrize("cue", list(Cue))
    def test_a_cue_is_whole_blocks_of_the_speakers_own_shape(self, cue: Cue) -> None:
        """48 kHz mono int16 in 960-sample blocks — what the call's own speaker
        opens, so a cue needs no second set of stream parameters."""
        read = samples(cues.render(cue))
        assert len(read) % cues.FRAME_SAMPLES == 0

    @pytest.mark.parametrize("cue", list(Cue))
    def test_a_cue_is_the_same_sound_every_time(self, cue: Cue) -> None:
        assert cues.render(cue) == cues.render(cue)

    def test_the_three_cues_are_three_different_sounds(self) -> None:
        assert len({cues.render(cue) for cue in Cue}) == len(list(Cue))


class TestTheSpanACuePlays:
    def test_a_span_says_what_played_where_and_for_how_long(self) -> None:
        """What #145 inherits: the adapter logs it, and the seam verb returns nothing."""
        span = cues.CueSpan(cue=Cue.CONNECTED, device=4, started=100.0, frames=14_400)
        assert span.seconds == pytest.approx(0.3)
        assert span.device == 4

    def test_a_span_on_the_machines_own_output_names_no_index(self) -> None:
        """`output_device` is optional, and `None` is the machine's default."""
        assert cues.CueSpan(cue=Cue.ENDED, device=None, started=1.0, frames=960).device is None

    @pytest.mark.parametrize("cue", list(Cue))
    def test_the_line_a_played_cue_writes_names_the_cue_the_device_and_the_span(
        self, cue: Cue
    ) -> None:
        """The engine logs nothing for `CallStarted` or `CallEnded`, so this line
        is the acceptance harness's only witness that a cue went out (#186)."""
        line = cues.played_line(cues.CueSpan(cue=cue, device=7, started=0.0, frames=960))
        assert line.startswith(cues.cue_phrase(cue))
        assert "7" in line
        assert "0.020" in line

    def test_the_phrase_the_harness_looks_for_names_the_moment_not_the_sound(self) -> None:
        assert cues.cue_phrase(Cue.CONNECTED) == "played the connected cue"

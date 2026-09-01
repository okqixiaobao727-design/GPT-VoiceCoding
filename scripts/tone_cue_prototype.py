#!/usr/bin/env python3
"""PROTOTYPE — throwaway. Hear candidate cues for the three call moments (issue #174).

#170 fixed *where* a cue plays: a second, short-lived `sounddevice.RawOutputStream`
opened per cue, on the same device index the call's speaker uses, synthesised with the
standard library. This script plays candidates on exactly that path so Simon can react
to length, pitch, loudness relative to speech, and whether the mid-call cue can be told
from the connect cue while the assistant is talking. It decides nothing; it is the
thing to react to. Nothing here starts a call.

    .venv/bin/python scripts/tone_cue_prototype.py            # interactive menu
    .venv/bin/python scripts/tone_cue_prototype.py --list     # print the catalogue
    .venv/bin/python scripts/tone_cue_prototype.py --device 4 # a PortAudio index
    .venv/bin/python scripts/tone_cue_prototype.py --export /tmp/cues  # WAVs of every candidate
    .venv/bin/python scripts/tone_cue_prototype.py --play 'seq 1' 'over v2'  # no terminal needed

Menu commands (one per line):

    c1 c2 c3     play CONNECTED candidate 1/2/3
    e1 e2 e3     play ENDED candidate 1/2/3
    v1 v2 v3     play EVENT (mid-call, non-Focus) candidate 1/2/3
    seq N        play connected → event → ended, all candidate N, 1 s apart
    over vN      say a sentence with macOS `say` and play EVENT N over it (1.5 s in)
    say          just the reference sentence, to compare loudness against
    gain -18     set peak level in dBFS (default -12; try -18 / -12 / -6)
    devices      list output devices
    q            quit

`say` goes to the system default output (it cannot take a PortAudio index), so when
`--device` is given, `over` compares a cue on that device against speech on the default
one — fine on a laptop where both are the built-in speakers, misleading otherwise.

Requires the voice extra: `.venv/bin/pip install -e '.[voice]'`.
"""

from __future__ import annotations

import argparse
import array
import math
import subprocess
import sys
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path

# The call's own playback parameters (adapters/call/realtime/webrtc.py:44-50).
SAMPLE_RATE = 48_000
FRAME_SAMPLES = 960

REFERENCE_SENTENCE = (
    "The codex agent on the voice coding project has finished task one twenty "
    "and is waiting for your decision. It recommends option A."
)


@dataclass(frozen=True)
class Note:
    hz: float
    ms: int
    #: 0..1 of the note spent rising; the rest decays exponentially-ish to the tail.
    attack: float = 0.08
    #: Amplitude of the second harmonic relative to the fundamental (0 = pure sine).
    bright: float = 0.0


@dataclass(frozen=True)
class Cue:
    key: str
    moment: str
    name: str
    why: str
    notes: tuple[Note, ...]
    #: Silence between notes.
    gap_ms: int = 30


CATALOGUE: tuple[Cue, ...] = (
    # ---- CONNECTED: two rising notes — the "line is open" gesture everyone knows.
    Cue(
        "c1",
        "CONNECTED",
        "soft rise",
        "two pure sines, a fourth apart, gentle attack; the quietest reading of 'connect'",
        (Note(660, 110), Note(880, 140)),
    ),
    Cue(
        "c2",
        "CONNECTED",
        "bright rise",
        "same interval, shorter, with a second harmonic so it reads through room noise",
        (Note(660, 80, bright=0.35), Note(990, 120, bright=0.35)),
        gap_ms=20,
    ),
    Cue(
        "c3",
        "CONNECTED",
        "three-step rise",
        "a major triad up (C5 E5 G5): unmistakably 'opening', at the cost of ~50 ms more",
        (Note(523, 70), Note(659, 70), Note(784, 130)),
        gap_ms=15,
    ),
    # ---- ENDED: the mirror of CONNECTED, falling. Plays after the call stream closed.
    Cue(
        "e1",
        "ENDED",
        "soft fall",
        "c1 reversed; a listener who learnt c1 needs nothing new to learn",
        (Note(880, 110), Note(660, 160)),
    ),
    Cue(
        "e2",
        "ENDED",
        "bright fall",
        "c2 reversed",
        (Note(990, 80, bright=0.35), Note(660, 140, bright=0.35)),
        gap_ms=20,
    ),
    Cue(
        "e3",
        "ENDED",
        "single low",
        "one low note with a long tail: 'done', without the melody",
        (Note(440, 260, attack=0.05),),
    ),
    # ---- EVENT: mid-call, a non-Focus Session stopped. Must be short (the mic is open,
    #      the assistant may be speaking) and must not resemble CONNECTED's rise.
    Cue(
        "v1",
        "EVENT",
        "single tick",
        "one very short high blip; the least intrusive thing that is still a sound",
        (Note(1320, 55, attack=0.15),),
    ),
    Cue(
        "v2",
        "EVENT",
        "double tick",
        "two identical short blips: distinct from any two-*different*-note cue",
        (Note(1320, 45, attack=0.15), Note(1320, 45, attack=0.15)),
        gap_ms=60,
    ),
    Cue(
        "v3",
        "EVENT",
        "soft knock",
        "a lower, muted single note (a 'tap on the door'), less piercing than v1",
        (Note(880, 90, attack=0.1, bright=0.2),),
    ),
)


def _by_key(key: str) -> Cue:
    for cue in CATALOGUE:
        if cue.key == key:
            return cue
    raise KeyError(key)


def render(cue: Cue, gain_dbfs: float) -> bytes:
    """16-bit mono PCM at SAMPLE_RATE, peak at `gain_dbfs`, stdlib only."""
    peak = 32767 * (10 ** (gain_dbfs / 20))
    out = array.array("h")
    gap = array.array("h", [0]) * int(SAMPLE_RATE * cue.gap_ms / 1000)
    for index, note in enumerate(cue.notes):
        if index:
            out.extend(gap)
        total = int(SAMPLE_RATE * note.ms / 1000)
        rise = max(1, int(total * note.attack))
        for i in range(total):
            t = i / SAMPLE_RATE
            if i < rise:
                env = i / rise
            else:
                # Decay to ~5 % at the end of the note: a percussive, non-buzzing tail.
                env = math.exp(-3.0 * (i - rise) / max(1, total - rise))
            sample = math.sin(2 * math.pi * note.hz * t)
            if note.bright:
                harmonic = note.bright * math.sin(4 * math.pi * note.hz * t)
                sample = (sample + harmonic) / (1 + note.bright)
            out.append(int(peak * env * sample))
    # Pad to a whole block so the stream drains cleanly.
    remainder = len(out) % FRAME_SAMPLES
    if remainder:
        out.extend(array.array("h", [0]) * (FRAME_SAMPLES - remainder))
    return out.tobytes()


def play(pcm: bytes, device: int | None) -> float:
    """The #170 path: open, write, stop, close — one short-lived stream. Returns seconds taken."""
    import sounddevice

    started = time.perf_counter()
    stream = sounddevice.RawOutputStream(
        samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=FRAME_SAMPLES, device=device
    )
    stream.start()
    try:
        stream.write(pcm)
        stream.stop()
    finally:
        stream.close()
    return time.perf_counter() - started


def duration_ms(pcm: bytes) -> int:
    return round(len(pcm) / 2 / SAMPLE_RATE * 1000)


def report(cue: Cue, pcm: bytes, device: int | None, note: str = "") -> None:
    took = play(pcm, device)
    print(
        f"  {cue.moment:<9} {cue.key} {cue.name:<16} {duration_ms(pcm)} ms sound, "
        f"{took * 1000:.0f} ms wall {note}"
    )


def export(directory: Path, gain_dbfs: float) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for cue in CATALOGUE:
        path = directory / f"{cue.moment.lower()}-{cue.key}-{cue.name.replace(' ', '-')}.wav"
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(SAMPLE_RATE)
            handle.writeframes(render(cue, gain_dbfs))
        print(f"wrote {path}")


def describe() -> None:
    moment = None
    for cue in CATALOGUE:
        if cue.moment != moment:
            moment = cue.moment
            print(f"\n{moment}")
        notes = " → ".join(f"{n.hz:g} Hz/{n.ms} ms" for n in cue.notes)
        print(f"  {cue.key}  {cue.name:<16} {notes}")
        print(f"      {cue.why}")
    print()


def say_reference() -> subprocess.Popen[bytes]:
    # `-r 150` is a slow pace, per the flow's "慢语速"; the rate only matters for the overlap test.
    return subprocess.Popen(["say", "-r", "150", REFERENCE_SENTENCE])


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--device", type=int, default=None, help="PortAudio output index; default = the machine's"
    )
    parser.add_argument("--gain", type=float, default=-12.0, help="peak level in dBFS")
    parser.add_argument("--list", action="store_true", help="print the catalogue and exit")
    parser.add_argument(
        "--export", type=Path, default=None, help="write every candidate as WAV into this directory"
    )
    parser.add_argument(
        "--play",
        nargs="+",
        default=None,
        metavar="CMD",
        help="run these menu commands in order and exit, e.g. --play 'seq 1' 'over v2'",
    )
    arguments = parser.parse_args()

    if arguments.list:
        describe()
        return 0
    if arguments.export:
        export(arguments.export, arguments.gain)
        return 0

    try:
        import sounddevice
    except ImportError:
        print("sounddevice is missing: .venv/bin/pip install -e '.[voice]'", file=sys.stderr)
        return 2

    gain = arguments.gain
    device = arguments.device
    print(f"output device: {device if device is not None else 'default'}   gain: {gain:g} dBFS")
    describe()
    print("commands: c1-3 e1-3 v1-3 | seq N | over vN | say | gain DB | devices | q")

    scripted = list(arguments.play) if arguments.play else None
    while True:
        if scripted is not None:
            if not scripted:
                return 0
            line = scripted.pop(0).strip().lower()
            print(f"cue> {line}")
        else:
            try:
                line = input("cue> ").strip().lower()
            except EOFError:
                return 0
        if not line:
            continue
        parts = line.split()
        try:
            if parts[0] == "q":
                return 0
            if parts[0] == "devices":
                print(sounddevice.query_devices())
            elif parts[0] == "gain":
                gain = float(parts[1])
                print(f"gain = {gain:g} dBFS")
            elif parts[0] == "say":
                say_reference().wait()
            elif parts[0] == "seq":
                n = parts[1]
                for key in (f"c{n}", f"v{n}", f"e{n}"):
                    cue = _by_key(key)
                    report(cue, render(cue, gain), device)
                    time.sleep(1.0)
            elif parts[0] == "over":
                cue = _by_key(parts[1])
                pcm = render(cue, gain)
                speech = say_reference()
                # The cue as it would land mid-sentence; a thread, as the real adapter must.
                thread = threading.Thread(target=report, args=(cue, pcm, device, "over speech"))
                time.sleep(1.5)
                thread.start()
                thread.join()
                speech.wait()
            else:
                cue = _by_key(parts[0])
                report(cue, render(cue, gain), device)
        except (KeyError, IndexError, ValueError) as bad:
            print(f"  ? {bad!r} — see the command list above")
        except Exception as failed:  # a cue must never take anything down; report and carry on
            print(f"  playback failed: {failed}")


if __name__ == "__main__":
    sys.exit(main())

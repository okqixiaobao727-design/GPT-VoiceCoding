# Chosen cues (issue #174, 2026-09-01)

Rendered by `scripts/tone_cue_prototype.py --export`, 48 kHz / 16-bit mono.

| Moment    | Candidate | Peak      | Shape |
|-----------|-----------|-----------|-------|
| CONNECTED | c3        | -12 dBFS  | 523 → 659 → 784 Hz, 70 / 70 / 130 ms, 15 ms gaps (~300 ms) |
| ENDED     | e2        | -12 dBFS  | 990 → 660 Hz, 80 / 140 ms, second harmonic 0.35 (~240 ms) |
| EVENT     | v2        | **-6 dBFS** | 1320 Hz × 2, 45 ms each, 60 ms gap (~160 ms) |

Loudness rule: CONNECTED and ENDED peak at -12 dBFS; EVENT peaks 6 dB hotter at -6 dBFS,
because it is the only cue that has to be heard over the assistant's own speech (chosen by
ear with `over v2` against macOS `say` at -12: too quiet; at -6: right).

These WAVs are reference renderings; the real adapter synthesises the same shapes from
module constants (#170, note 5).

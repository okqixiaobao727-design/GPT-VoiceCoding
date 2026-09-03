"""The mic-free Live Call's framing, pacing and settings — with no call at all.

`live_call` is the acceptance harness's own Call adapter: the run config points
`[adapters] call` at it, it builds the **production** WebRTC transport with
`silent=True`, and it feeds that transport's existing track from synthesised
speech instead of from a device (#183, ported from
`scripts/realtime_text_entry_probe.py:794-982`).

Three of its parts are ordinary code and are tested here, at CI speed, against
no backend and no audio device:

* **framing** — every payload handed to `av` is exactly one 20 ms frame, and the
  last one is padded rather than short;
* **pacing** — frames leave on the wall clock, not as fast as the encoder asks.
  The probe's note is the reason this is tested rather than assumed: frames
  handed over as fast as they are wanted run the media clock ahead of real time,
  and the far side ends up listening to a call that has already ended;
* **the fallback to silence** — between utterances the track must never starve.

`av` is deliberately not imported here. CI installs `.[dev]` and not `.[voice]`
(`.github/workflows/ci.yml` § Install), so a test that needed the resampler
would be a test that never runs where it matters. The one function that does
need it (`pcm_at_48k`) imports it inside its body, and its test skips when the
extra is absent.
"""

from __future__ import annotations

import asyncio
import json
import wave
from pathlib import Path

import journey
import live_call
import pytest

from gpt_voicecoding.adapters.call.realtime import cues
from gpt_voicecoding.adapters.call.realtime.webrtc import FRAME_SAMPLES, SAMPLE_RATE
from gpt_voicecoding.control_plane.commands import render
from gpt_voicecoding.seams.call import Cue
from gpt_voicecoding.seams.control_plane import Action, Reply


class FakeTrack:
    """The two attributes `_Microphone._next` reads off the real track.

    Named rather than a `SimpleNamespace` so the pacing test says which fields
    of `webrtc.py`'s `_Track` the source is reaching for (`webrtc.py:242-243`).
    """

    def __init__(self) -> None:
        self._pts = 0
        self._started: float | None = None


class Clock:
    """A wall clock the test moves by hand, and the sleeps asked of it."""

    def __init__(self) -> None:
        self.now = 1_000.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def source(clock: Clock) -> live_call.WavTrackSource:
    return live_call.WavTrackSource(clock=clock.monotonic, sleep=clock.sleep)


class FakeRealTransport:
    """The half of the production transport `HarnessCallTransport` delegates to.

    Only `is_connected` matters to the playlist: everything the frame source does
    is decided by whether the peer connection is up and how long it has been.
    """

    def __init__(self) -> None:
        self.is_connected = True
        self._microphone = FakeMicrophone()


class FakeMicrophone:
    """The one method the harness reaches past the transport's surface to shadow."""

    async def _next(self, track: object) -> bytes:  # pragma: no cover - replaced at once
        return live_call.SILENT_FRAME


# --- framing ----------------------------------------------------------------


def test_every_frame_is_one_opus_frame() -> None:
    """`av` fills a whole plane, so a short payload is not a shorter frame."""
    frames = live_call.framed(b"\x01\x02" * (FRAME_SAMPLES * 3))
    assert len(frames) == 3
    assert {len(frame) for frame in frames} == {FRAME_SAMPLES * 2}


def test_the_last_frame_is_padded_with_silence_rather_than_left_short() -> None:
    payload = b"\x01\x02" * (FRAME_SAMPLES + 10)
    frames = live_call.framed(payload)
    assert len(frames) == 2
    assert len(frames[1]) == FRAME_SAMPLES * 2
    assert frames[1] == b"\x01\x02" * 10 + b"\x00" * ((FRAME_SAMPLES - 10) * 2)


def test_no_audio_is_no_frames() -> None:
    assert live_call.framed(b"") == []


def test_the_silent_frame_is_an_exact_zero_floor_of_one_frame() -> None:
    """What the track carries between utterances — a real room never has this."""
    assert len(live_call.SILENT_FRAME) == FRAME_SAMPLES * 2
    assert set(live_call.SILENT_FRAME) == {0}


# --- the fallback to silence ------------------------------------------------


def test_an_idle_source_hands_over_silence_rather_than_starving_the_track() -> None:
    clock = Clock()
    wav = source(clock)
    assert asyncio.run(wav.next(FakeTrack())) == live_call.SILENT_FRAME


def test_queued_frames_come_out_in_order_and_then_silence_returns() -> None:
    clock = Clock()
    wav = source(clock)
    first, second = b"\x01\x01" * FRAME_SAMPLES, b"\x02\x02" * FRAME_SAMPLES
    wav.enqueue([first, second])

    async def played() -> list[bytes]:
        track = FakeTrack()
        return [await wav.next(track) for _ in range(3)]

    assert asyncio.run(played()) == [first, second, live_call.SILENT_FRAME]


def test_enqueue_reports_how_long_the_utterance_takes_to_go_out() -> None:
    clock = Clock()
    wav = source(clock)
    seconds = wav.enqueue([live_call.SILENT_FRAME] * 50)
    assert seconds == pytest.approx(50 * FRAME_SAMPLES / SAMPLE_RATE)


# --- pacing -----------------------------------------------------------------


def test_the_first_frame_starts_the_media_clock_and_waits_for_nothing() -> None:
    clock = Clock()
    wav = source(clock)
    track = FakeTrack()
    asyncio.run(wav.next(track))
    assert track._started == clock.now
    assert clock.slept == []


def test_a_source_ahead_of_the_wall_clock_waits_for_it() -> None:
    """One frame's worth of pts with no time passed is one frame's worth of sleep."""
    clock = Clock()
    wav = source(clock)
    track = FakeTrack()
    asyncio.run(wav.next(track))
    track._pts = FRAME_SAMPLES
    asyncio.run(wav.next(track))
    assert clock.slept == [pytest.approx(FRAME_SAMPLES / SAMPLE_RATE)]


def test_a_source_behind_the_wall_clock_never_sleeps() -> None:
    """A late frame goes now. Sleeping a negative delay would make it later."""
    clock = Clock()
    wav = source(clock)
    track = FakeTrack()
    asyncio.run(wav.next(track))
    clock.now += 5.0
    track._pts = FRAME_SAMPLES
    asyncio.run(wav.next(track))
    assert clock.slept == []


# --- the settings table -----------------------------------------------------


def test_the_harness_keys_are_taken_and_the_adapters_own_table_is_left_whole(
    tmp_path: Path,
) -> None:
    """The Call adapter refuses a key it does not have, so ours never reach it."""
    harness, remainder = live_call.HarnessSettings.split(
        {
            "observations": str(tmp_path / "live-call.jsonl"),
            "wav_directory": str(tmp_path / "wav"),
            "request": "end the call",
            "settle_seconds": 2.0,
            "realtime_model": "gpt-live-1-codex",
        }
    )
    assert remainder == {"realtime_model": "gpt-live-1-codex"}
    assert harness.observations == tmp_path / "live-call.jsonl"
    assert harness.wav_directory == tmp_path / "wav"
    assert harness.request == "end the call"
    assert harness.settle_seconds == 2.0


def test_a_table_naming_no_harness_key_is_refused_rather_than_defaulted() -> None:
    """The observation file is where the step reads the run from; a default would
    put it somewhere no lane is looking."""
    with pytest.raises(live_call.HarnessSettingsError):
        live_call.HarnessSettings.split({"realtime_model": "gpt-live-1-codex"})


def test_the_defaults_are_the_probes_own_proven_values(tmp_path: Path) -> None:
    harness, _ = live_call.HarnessSettings.split(
        {"observations": str(tmp_path / "o.jsonl"), "wav_directory": str(tmp_path)}
    )
    assert harness.request == live_call.REQUEST
    assert harness.voice == live_call.WAV_VOICE
    assert harness.wav_sample_rate == live_call.WAV_SAMPLE_RATE
    assert harness.settle_seconds == live_call.SETTLE_SECONDS


# --- the observation file ---------------------------------------------------


def test_observations_are_one_json_object_a_line(tmp_path: Path) -> None:
    """The step reads this file across a process boundary, so a half-written run
    still has to parse — which is what one object a line buys."""
    path = tmp_path / "deep" / "live-call.jsonl"
    observations = live_call.Observations(path)
    observations.note("wav source installed", variant="plain")
    observations.note("call ended", reason="this side ended it")
    written = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [entry["what"] for entry in written] == ["wav source installed", "call ended"]
    assert written[0]["variant"] == "plain"
    assert written[1]["reason"] == "this side ended it"
    assert all(isinstance(entry["at"], str) and entry["at"].endswith("Z") for entry in written)


def test_the_reader_answers_from_what_the_engine_wrote(tmp_path: Path) -> None:
    path = tmp_path / "live-call.jsonl"
    observations = live_call.Observations(path)
    observations.note("wav utterance on the track", variant="plain", frames=7)
    observations.note("call ended", reason="this side ended it")
    read = live_call.observed(path)
    assert read.variant == "plain"
    assert read.end_reason == "this side ended it"


def test_a_missing_observation_file_reads_as_nothing_observed(tmp_path: Path) -> None:
    read = live_call.observed(tmp_path / "never-written.jsonl")
    assert read.variant is None
    assert read.end_reason is None


# --- what only the real resampler can answer --------------------------------


def test_a_wav_already_at_the_track_rate_is_passed_through(tmp_path: Path) -> None:
    """No resampler in the path at all when there is nothing to resample."""
    path = tmp_path / "already.wav"
    payload = b"\x01\x02" * FRAME_SAMPLES
    with wave.open(str(path), "wb") as sink:
        sink.setnchannels(1)
        sink.setsampwidth(2)
        sink.setframerate(SAMPLE_RATE)
        sink.writeframes(payload)
    assert live_call.pcm_at_48k(path) == payload


def test_a_24k_wav_is_resampled_up_to_the_track_rate(tmp_path: Path) -> None:
    pytest.importorskip("av", reason="the voice extra; CI installs .[dev] only")
    path = tmp_path / "quarter.wav"
    samples = live_call.WAV_SAMPLE_RATE // 2
    with wave.open(str(path), "wb") as sink:
        sink.setnchannels(1)
        sink.setsampwidth(2)
        sink.setframerate(live_call.WAV_SAMPLE_RATE)
        sink.writeframes(b"\x00\x00" * samples)
    resampled = live_call.pcm_at_48k(path)
    # Half a second at 48 kHz, within one frame: the resampler's own tail is
    # what the tolerance is for, and the padding the probe learned to cut is
    # what would otherwise make this longer.
    assert abs(len(resampled) // 2 - SAMPLE_RATE // 2) < FRAME_SAMPLES


# --- the acceptance step's own cue rule, graded at CI speed ------------------
#
# The `live call` step reads two cues out of the engine log and grades their
# order (#186). The walk itself never reaches CI, so the rule is a module-level
# function and this is where it is held to its wording — #109 is what a harness
# rule with no test at CI speed costs.

SPOKE = "user speech, for the voice thread to act on: 结束通话"


def played(cue: Cue, *, device: int | None = None) -> str:
    """A line the way the Call adapter really writes it, not a line invented here."""
    pcm = cues.render(cue)
    return cues.played_line(cues.CueSpan(cue=cue, device=device, started=0.0, frames=len(pcm) // 2))


def test_a_call_that_marked_both_of_its_ends_is_accepted() -> None:
    complaint = journey._cue_complaint(
        [played(Cue.CONNECTED), SPOKE, played(Cue.ENDED)], {SPOKE}, device=None
    )
    assert complaint == ""


def test_a_call_that_made_no_sound_at_all_is_refused_by_name() -> None:
    """The likeliest failure: an output device the engine could not open. The
    adapter swallows that, so nothing else in the run would say so."""
    complaint = journey._cue_complaint([SPOKE], {SPOKE}, device=None)
    assert "no connected cue, no ended cue" in complaint


def test_a_call_that_only_marked_its_ending_is_refused() -> None:
    complaint = journey._cue_complaint([SPOKE, played(Cue.ENDED)], {SPOKE}, device=None)
    assert "no connected cue" in complaint
    assert "no ended cue" not in complaint


def test_a_connect_cue_that_arrived_after_the_call_was_talked_into_is_refused() -> None:
    """Order, not presence. Both lines are there and they are the wrong way round."""
    lines = [SPOKE, played(Cue.CONNECTED), played(Cue.ENDED)]
    assert "after the user speech" in journey._cue_complaint(lines, {SPOKE}, device=None)


def test_an_end_cue_from_before_this_calls_speech_is_not_this_calls_ending() -> None:
    lines = [played(Cue.ENDED), played(Cue.CONNECTED), SPOKE]
    assert "that is not this call's ending" in journey._cue_complaint(lines, {SPOKE}, device=None)


def test_a_second_cue_of_the_same_kind_does_not_unseat_the_first() -> None:
    """Two calls' worth of log read from one mark: the step grades the earliest
    connect and the latest ending, so a run that dialled twice still reads."""
    lines = [played(Cue.CONNECTED), SPOKE, played(Cue.ENDED), played(Cue.CONNECTED)]
    assert journey._cue_complaint(lines, {SPOKE}, device=None) == ""


def test_the_mid_call_cue_is_never_what_this_step_looks_for() -> None:
    """`EVENT` has no caller yet, and an EVENT line is not an ending."""
    lines = [played(Cue.CONNECTED), SPOKE, played(Cue.EVENT)]
    assert "no ended cue" in journey._cue_complaint(lines, {SPOKE}, device=None)


def test_a_cue_line_that_names_no_device_or_span_is_not_the_record_asked_for() -> None:
    """The ticket wants the adapter's log to record the output device and the
    span written — so a line carrying only the phrase is not enough."""
    lines = ["played the connected cue", SPOKE, played(Cue.ENDED)]
    complaint = journey._cue_complaint(lines, {SPOKE}, device=None)
    assert "without the output device and the span written" in complaint


def test_a_cue_played_to_a_stated_device_is_read_against_that_device() -> None:
    """A run that pinned `output_device` is graded on the line it really writes."""
    lines = [played(Cue.CONNECTED, device=4), SPOKE, played(Cue.ENDED, device=4)]
    assert journey._cue_complaint(lines, {SPOKE}, device=4) == ""
    assert "without the output device" in journey._cue_complaint(lines, {SPOKE}, device=None)


def test_the_span_in_the_line_is_the_one_the_cue_really_synthesises_to() -> None:
    """Not a number typed twice: a cue whose length changed would fail here."""
    assert "14400 frames, 0.300s" in played(Cue.CONNECTED)
    assert "11520 frames, 0.240s" in played(Cue.ENDED)
    assert "7680 frames, 0.160s" in played(Cue.EVENT)


def test_a_thin_early_line_is_not_evidence_that_a_whole_later_one_is_in_order() -> None:
    """Both claims have to come off the same line.

    The connect cue here is logged twice: once before the speech carrying only
    the phrase, and once after it carrying the whole record. Read separately,
    "a whole line exists" and "something was logged early" are both true — of a
    call whose connect was never actually recorded before it was talked into.
    """
    lines = [
        "played the connected cue",
        SPOKE,
        played(Cue.CONNECTED),
        played(Cue.ENDED),
    ]
    assert "after the user speech" in journey._cue_complaint(lines, {SPOKE}, device=None)


def test_a_thin_late_end_line_is_not_evidence_for_a_whole_early_one() -> None:
    """The mirror: the whole `ended` record is from before this call's speech."""
    lines = [played(Cue.CONNECTED), played(Cue.ENDED), SPOKE, "played the ended cue"]
    assert "that is not this call's ending" in journey._cue_complaint(lines, {SPOKE}, device=None)


# --- the acceptance step's own "is a call up?" rule, graded at CI speed ------
#
# `bridgectl status` says whether a call is up and what Cool-down is running on
# *one* line (#195), and the harness reads that line to decide when the ceiling
# has released the call. The rule is a module-level function for the same reason
# the cue rule above is: an acceptance run is an expensive place to discover a
# string comparison written the wrong way round (#109, #218).


def call_line(**data: object) -> str:
    """The `call:` line as the control plane really renders it, not one typed here.

    Built through `commands.render` so these cases move the day the surface
    does. Run `20260903T060110Z` is what a harness holding its own copy of this
    wording costs: the Cool-down suffix #195 added went unread for a whole
    release cycle, and the step waited the window out instead of using it.
    """
    reply = Reply(
        True,
        Action.STATUS,
        {
            "switches": {"duty": True},
            "sessions": [],
            "lanes": {},
            "degraded_lanes": {},
            "call_id": None,
            "cool_down_remaining": 0.0,
            "dial_owed": False,
            "pending_relays": [],
            **data,
        },
    )
    (line,) = [row for row in render(reply).splitlines() if row.startswith("call:")]
    return line


def test_a_quiet_line_reads_as_no_call_up() -> None:
    assert journey._no_call_is_up(call_line()) is True


def test_a_running_cool_down_is_still_no_call_up() -> None:
    """The regression #218 is: the ceiling has released the call and the engine
    is counting the window out, which is precisely when the step must act."""
    assert journey._no_call_is_up(call_line(cool_down_remaining=28.0)) is True


def test_a_cool_down_carrying_an_owed_dial_is_still_no_call_up() -> None:
    assert journey._no_call_is_up(call_line(cool_down_remaining=12.0, dial_owed=True)) is True


def test_an_owed_dial_past_the_windows_expiry_is_still_no_call_up() -> None:
    """`_end_any_live_call` asks this same question, and an answer of "a call is
    up" here would have it dial one to clean up after a call that never was."""
    assert journey._no_call_is_up(call_line(dial_owed=True)) is True


def test_a_line_naming_a_call_reads_as_a_call_up() -> None:
    """A call id is never the word `none`, so the reading cannot be fooled."""
    line = call_line(call_id="01a065e0-1a4f-7c10-9a9a-c2f3840f7953")

    assert line == "call: 01a065e0-1a4f-7c10-9a9a-c2f3840f7953"
    assert journey._no_call_is_up(line) is False


def test_a_surface_that_answered_something_else_entirely_is_not_read_as_down() -> None:
    """`_call_line` falls back to the head of whatever `status` printed when no
    `call:` line is in it — a refusal must not read as a quiet keeper."""
    assert journey._no_call_is_up("engine unreachable") is False


# --- the playlist, and the second utterance on a call that is up (#196) -------


def playlist_settings(tmp_path: Path) -> live_call.HarnessSettings:
    return live_call.HarnessSettings(
        observations=tmp_path / "observations.jsonl", wav_directory=tmp_path
    )


def test_a_source_is_idle_only_while_it_has_nothing_left_to_say() -> None:
    clock = Clock()
    queued = source(clock)
    assert queued.idle

    queued.enqueue(live_call.framed(b"\x01\x02" * live_call.FRAME_SAMPLES))
    assert not queued.idle

    asyncio.run(queued.next(FakeTrack()))
    assert queued.idle


class TransportUnderTest:
    """One `HarnessCallTransport` with the real WebRTC half stubbed out."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.clock = Clock()
        monkeypatch.setattr(live_call, "webrtc_transport", lambda **_: FakeRealTransport())
        self.settings = playlist_settings(tmp_path)
        # Three frames, so an utterance is still going out on the turn after the
        # one that queued it — which is the state `idle` exists to describe.
        sentence = live_call.framed(b"\x01\x02" * live_call.FRAME_SAMPLES * 3)
        self.transport = live_call.HarnessCallTransport(
            settings=self.settings,
            observations=live_call.Observations(self.settings.observations),
            utterances={
                live_call.PLAIN: list(sentence),
                live_call.NEEDS: list(sentence),
                live_call.LONG: list(sentence),
                live_call.RELAY: list(sentence),
            },
            clock=self.clock.monotonic,
        )
        self.track = FakeTrack()

    def frame(self, *, after: float = 0.0) -> None:
        """One 20 ms turn of the audio loop, `after` seconds later."""
        self.clock.now += after
        asyncio.run(self.transport._next(self.track))

    def drain(self) -> None:
        """Turn the loop until the track is carrying silence again."""
        for _ in range(16):
            if self.transport._source.idle:
                return
            self.frame()
        raise AssertionError("the utterance never finished going out")

    @property
    def played(self) -> list[str]:
        seen = live_call.observed(self.settings.observations)
        return [
            str(entry["variant"])
            for entry in seen.entries
            if entry["what"] == "wav utterance on the track"
        ]


def test_nothing_goes_out_before_the_settle_window_has_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live_call.ask_for(tmp_path, live_call.NEEDS)
    under = TransportUnderTest(tmp_path, monkeypatch)

    under.frame()
    under.frame(after=live_call.SETTLE_SECONDS - 1.0)

    assert under.played == []


def test_the_first_queued_utterance_goes_out_once_the_call_has_settled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live_call.ask_for(tmp_path, live_call.NEEDS)
    under = TransportUnderTest(tmp_path, monkeypatch)

    under.frame()
    under.frame(after=live_call.SETTLE_SECONDS)

    assert under.played == [live_call.NEEDS]


def test_a_line_appended_while_the_call_is_up_is_spoken_next(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v2's whole mechanism: the step says "and then say this", mid-call (#196)."""
    live_call.ask_for(tmp_path, live_call.NEEDS)
    under = TransportUnderTest(tmp_path, monkeypatch)
    under.frame()
    under.frame(after=live_call.SETTLE_SECONDS)
    under.drain()

    live_call.ask_next(tmp_path, live_call.PLAIN)
    under.frame(after=live_call.PLAYLIST_POLL_SECONDS)

    assert under.played == [live_call.NEEDS, live_call.PLAIN]


def test_a_queued_utterance_never_goes_out_over_the_one_before_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wire truncates an utterance a second one is appended to (#175)."""
    live_call.ask_for(tmp_path, live_call.NEEDS)
    live_call.ask_next(tmp_path, live_call.PLAIN)
    under = TransportUnderTest(tmp_path, monkeypatch)

    under.frame()
    under.frame(after=live_call.SETTLE_SECONDS)
    under.frame(after=live_call.PLAYLIST_POLL_SECONDS)

    assert under.played == [live_call.NEEDS], "the first utterance was still going out"


def test_the_playlist_is_not_read_on_every_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fifty stats a second is disk work in the one loop that may not fall behind."""
    live_call.ask_for(tmp_path, live_call.NEEDS)
    under = TransportUnderTest(tmp_path, monkeypatch)
    under.frame()
    under.frame(after=live_call.SETTLE_SECONDS)
    under.drain()

    live_call.ask_next(tmp_path, live_call.PLAIN)
    under.frame(after=0.02)

    assert under.played == [live_call.NEEDS]


def test_a_call_asked_for_nothing_puts_nothing_on_the_track(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live_call.ask_for_nothing(tmp_path)
    under = TransportUnderTest(tmp_path, monkeypatch)

    under.frame()
    under.frame(after=live_call.SETTLE_SECONDS)
    under.frame(after=live_call.PLAYLIST_POLL_SECONDS)

    assert under.played == []

    live_call.ask_next(tmp_path, live_call.RELAY)
    under.frame(after=live_call.PLAYLIST_POLL_SECONDS)

    assert under.played == [live_call.RELAY]


# --- was the brief spoken into a gap? (#196) ---------------------------------


SPOKEN = (
    "2026-09-03 20:45:13,115 INFO gpt_voicecoding.core.call_keeper: spoke the Focus "
    "Session's brief into the gap in the Live Call: 二号工位 · Reply READY"
)
STARTED = f"2026-09-03 20:45:00,000 INFO gpt_voicecoding.core.bridge: {journey.VOICE_SPEAKING_LINE}"
STOPPED = f"2026-09-03 20:45:10,000 INFO gpt_voicecoding.core.bridge: {journey.VOICE_QUIET_LINE}"


def test_an_announcement_after_the_voice_closed_its_span_was_spoken_into_a_gap() -> None:
    assert journey._announced_after_the_voice_fell_silent([STARTED, STOPPED, SPOKEN])


def test_an_announcement_over_an_open_span_was_not() -> None:
    """The wire truncates an utterance a second one is appended to (#175)."""
    assert not journey._announced_after_the_voice_fell_silent([STARTED, STOPPED, STARTED, SPOKEN])


def test_a_voice_that_never_spoke_on_this_call_is_a_gap_of_its_own() -> None:
    assert journey._announced_after_the_voice_fell_silent([SPOKEN])


def test_an_edge_after_the_announcement_is_not_read_back_onto_it() -> None:
    """The announcement makes the Voice speak; that span is its own consequence."""
    assert journey._announced_after_the_voice_fell_silent([STOPPED, SPOKEN, STARTED])


def test_no_announcement_at_all_is_not_this_rules_complaint() -> None:
    assert journey._announced_after_the_voice_fell_silent([STARTED])

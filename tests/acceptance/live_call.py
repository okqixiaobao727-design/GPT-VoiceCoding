"""The acceptance harness's own Call adapter: a Live Call with nobody at the mic.

## What this is, and where it runs

This module is named by the **run config**, not imported by the harness:
`[adapters] call = live_call:harness_call` (`support.derive_config`). So it is
constructed inside the *engine* process, by the bundle's own interpreter, and
`support.Engine.environment` puts `tests/acceptance` on that process's
`PYTHONPATH` so the name resolves. Nothing under `src/` changes — the seam the
composition root already has (`config.py:70` `REQUIRED_SEAMS`, ADR 0001) is what
this fills, and `realtime_call`'s `transport_factory` parameter
(`adapters/call/realtime/__init__.py`) is the hole it fills it through.

## Ported, not invented (ADR 0010, #183)

Every audio decision here is **ported** from the probe
`scripts/realtime_text_entry_probe.py`, which is the
reference implementation: #181 ran it and found synthesised speech on the media
track reaching the Call Agent 3/3 with no device opened.

| Here | Probe |
|---|---|
| `say` | `:794` `_say` — measures the WAV, because `say` exits 0 for a voice it cannot speak |
| `pcm_at_48k` | `:829` — `av`, not `MediaPlayer`, which ends the track at EOF |
| `framed` | `:860` — whole planes, last one padded |
| `WavTrackSource` | `:887` — queued frames falling back to paced silence |
| the `_next` injection | `:979` — the one line that reaches past the transport's surface |

Legacy has no counterpart: its acceptance followed the host app's log
(`legacy@1d32845:bridge/livecall.py:77-109`) with no synthesised speech at all —
**dropped, because** this product owns the peer connection and feeds its own
track (#180 §1).

## What is *not* ported, and why

The probe drives its own handshake and decides mid-call which WAV variant to
play next off what the backend has been heard to do (`:1000-1021`). Nothing here
decides anything by ear: the **step** says what to play, in a file, and this side
plays what it is told the moment the peer connection has been up for
`settle_seconds`. The variant is written down rather than assumed because the
step reports it as an observation.

Since #196 the file is a **list** and the step may append to it while the call is
up, which is the whole of what "several utterances per call" means here: every
sentence is still synthesised before the engine starts, and what a step chooses
at run time is only which of them goes out next, and when. Nothing is ever put on
the track over an utterance already going out — the wire truncates an utterance a
second one is appended to (#175), and the harness must not be what proves it.

## How the step on the other side of the process boundary reads this

Through `observations`: one JSON object a line, appended as things happen, read
back by `observed`. The step runs in pytest and the transport runs in the
engine, and this file is the only thing that crosses between them.
"""

from __future__ import annotations

import asyncio
import fractions
import json
import subprocess
import time
import wave
from collections import deque
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gpt_voicecoding.adapters.call.realtime import realtime_call
from gpt_voicecoding.adapters.call.realtime.transport import CallTransport, LostHandler
from gpt_voicecoding.adapters.call.realtime.webrtc import (
    FRAME_SAMPLES,
    SAMPLE_BYTES,
    SAMPLE_RATE,
    webrtc_transport,
)

#: The reference this module is named by, so `derive_config` and the verdict
#: spell it once. `module:attribute`, the form `[adapters]` takes.
REFERENCE = "live_call:harness_call"

#: One 20 ms frame of nothing — the payload `_Microphone._next` returns in a
#: silent run (`webrtc.py:276`), restated here because the WAV source returns it
#: between utterances and the track must never be starved.
SILENT_FRAME = b"\x00\x00" * FRAME_SAMPLES

#: What the call is asked. #175 run 3's exact phrasing, which #179 and #181
#: proved produces a `handoff_request`, and which the Call Agent answers by
#: running `bridgectl live` — so the request is also what ends the call.
REQUEST = "那个你把电话挂了吧,我想让你结束通话"

#: What the *other* variant asks. #184 needs a call to outlive the Silence
#: Ceiling while its own Voice is still speaking, and four seconds of answer
#: cannot show that. Counting is asked for rather than an explanation because
#: its length is the Voice's own arithmetic and not a judgement about how much
#: to say: two hundred numbers took 220s on run `20260902T162146Z`, against a
#: 60s ceiling, where "talk for two minutes" is a duration models do not hit.
#:
#: **No hang-up ask in it.** That run had the Voice count for 220s and then hand
#: nothing off at all, which is the honest answer: a request that is not a
#: hang-up ask gives the Call Agent nothing to do, and no shipped rule says it
#: should end a call by itself (#195, deferred). So the two asks are two
#: utterances rather than one sentence carrying both — `REQUEST` keeps #183's
#: graded hand-off, and this one is only ever about the ceiling.
LONG_REQUEST = "请你从一数到两百,一个数字一个数字地念出来,不要跳过也不要加快"

#: What the *third* variant asks, and it asks for nothing to be done. #194 dials
#: the call from the system side and puts the whole briefing in `initialItems`,
#: which the Voice holds silently — so the question that proves the hand-over
#: arrived is one whose answer can only have come out of it. "什么需要我" is that
#: question: the Voice either reads the Sessions it was handed at dial time or it
#: has nothing, and a Voice with nothing invents (ADR 0018, on the probe's
#: invented clock). **No verb in it**, deliberately: the Call Agent is the half
#: with the tools, and a hand-off here would mean the Voice went looking for an
#: answer it was already holding. The wrapper log staying empty for this question
#: is what says it did not.
#:
#: Longer than the four words it could have been, for `WAV_MINIMUM_SECONDS`'
#: sake: "什么需要我?" synthesises to 1.04 s against a 1.0 s floor, which is a
#: run away from being read as the stub `say` writes for a voice that was never
#: installed. This one is 2 s and change, and asks the same thing.
NEEDS_REQUEST = "现在有哪些需要我的事情?"

#: What the Focus Session's workspace is called by default, and so — since the
#: project half of a Session Name is the workspace directory's basename
#: (`adapters/agent/_project.py`) — what the Voice knows that Session by. The
#: harness picks it, which is what lets `relay_request` say it out loud.
#:
#: **A default, and every lane overrides it** (`journey.Lane`,
#: `[adapters.settings.call] focus_workspace`). Two lanes sharing this name is
#: two Sessions the sentence cannot tell apart: the Codex daemon is
#: machine-wide, so the Claude lane's engine holds the Codex lane's Sessions
#: too, and run `20260903T093813Z` had its Call Agent looking at two rows called
#: `二号工位 · Reply READY` and answering with `brief` instead of relaying.
FOCUS_WORKSPACE_NAME = "二号工位"

#: And what the *ringing* Session's workspace is called. It exists so that the
#: EVENT cue can be graded while a Focus Session exists: with only one extra
#: Session, the ring and the announcement are the same Session either side of
#: the relay, and "an event about **another** Session while a Focus Session
#: exists rings and does not speak" — the rule #196 is for — is never put to the
#: engine at all. Never named in any utterance: the run's proof is that nothing
#: spoken ever names it. Per lane for `FOCUS_WORKSPACE_NAME`'s reason.
RINGING_WORKSPACE_NAME = "三号工位"


#: And what the *waiting* Session's workspace is called. #198's walk hangs the
#: call up and then has a Session stop **inside the Cool-down**, which the paid
#: dial must be about — so it has to be a Session no earlier phase has moved.
#: Never named in any utterance: nobody speaks on the call it earns. Per lane for
#: `FOCUS_WORKSPACE_NAME`'s reason.
WAITING_WORKSPACE_NAME = "四号工位"


@dataclass(frozen=True, slots=True)
class CallWorkspaces:
    """The three roles one lane's extra Sessions play, named rather than ordered.

    They were a `tuple[str, str, str]` until the third arrived and every reader
    of it — the lane, `derive_config`'s unpacking, the settings table it writes
    and the `DerivedConfig` it returns — had to be changed in step. The roles are
    what those readers actually mean, so they are what is carried: `workspaces
    .focus` cannot be read as `workspaces.ringing` the way `[0]` can be read as
    `[1]`, and a fourth role would be a field rather than a fourth position
    threaded through four files.

    Kept out of `HarnessSettings`, which stays flat: its fields are
    `[adapters.settings.call]` keys, and that table crosses a process boundary
    as TOML.
    """

    #: The one every call is dialled about, relayed into, briefed, paged and
    #: announced. Said out loud by the utterances, which is why it is per lane.
    focus: str
    #: The one that only ever rings. The run's proof about it is that nothing
    #: spoken ever names it.
    ringing: str
    #: The one that stops inside the Cool-down after the hang-up. Nothing says it
    #: out loud either.
    waiting: str

    def __post_init__(self) -> None:
        named = (self.focus, self.ringing, self.waiting)
        if len(set(named)) != len(named):
            raise ValueError(
                f"a lane's three extra Sessions need three names it can tell apart: {named}"
            )

    def __iter__(self) -> Iterator[str]:
        """The three names, for a caller that wants to check them as a set."""
        return iter((self.focus, self.ringing, self.waiting))


#: The sentence the relayed answer tells the Session to reply with, and so what
#: that Session's `newest` is by the time Detail asks about it (#198 §3a).
#:
#: **Dictated, because a free-form reply is not gradeable.** §3a asks that the
#: Voice's Detail answer carry a substring of the Session's `newest`. The Voice
#: speaks Chinese; `newest` is whatever language that agent chose. Run
#: `20260903T231626Z` had the Codex lane answer in Chinese (substring held) and
#: the Claude lane in English — `A teammate session replied "可以继续" …` — which
#: the Voice **translated** faithfully into Chinese, sharing no character with
#: it. The criterion passed on one lane by accident of language and failed
#: correct behaviour on the other. Dictating the reply restores the harness's
#: own premise, that graded fragments come from lines the walk put there
#: (`journey._spoken_fragment`).
#:
#: Chinese, so the Voice reads it out rather than translating it. It spells
#: neither `收到` nor `已转达` — both are receipt wordings
#: (`instructions/voice.py`) an echo of which would pass this grade — and not
#: the workspace name, which the answer is already graded on elsewhere.
DICTATED_REPLY = "那我就接着往下做。"


def relay_request(focus_workspace: str) -> str:
    """The answer utterance, naming one lane's own Focus Session's workspace.

    **It is an answer**, since #198: the Session stopped on `Should I continue?`
    (`journey.ASK_A_QUESTION`), and what the user says back is what the Call
    Agent relays and what that Session's next turn then carries. The payload is
    deliberately not `收到` — that is the wording the Voice says for a *queued*
    receipt (`instructions/voice.py`), and a payload spelling it would make the
    receipt and its echo one string the step could not tell apart.

    `可以继续` stays its first clause: that fragment is the one the step follows
    through the air, the Call Agent's argv and the Session's own next turn
    (`journey.LIVE_CALL_ANSWER_SUBSTRING`). What follows it dictates the reply,
    for `DICTATED_REPLY`'s reasons.
    """
    return (
        f"请你给{focus_workspace}那个会话回一句话，内容是可以继续，"
        f"并且请它只回复这一句：{DICTATED_REPLY}"
    )


def narrowing_request(focus_workspace: str) -> str:
    """ "Just that one" — the second half of the hand-over question (#198).

    The Voice's own rule is counts first and names only when narrowed: *"Asked
    what is going on generally, give the counts rather than the list … When they
    narrow it, by name or by state, speak each one that matches"*
    (`core/instructions/voice.py`). `NEEDS_REQUEST` asks generally and is answered
    with counts; this narrows, and is where a Session name belongs. Run
    `20260903T222129Z` is the step asking generally and grading the absence of a
    name, which is the product obeying its own instruction.

    **No verb in it either.** The point of both questions is that the Call Agent
    runs nothing for them: the counted roster and this one Session's whole brief
    both rode `initialItems`, so a hand-off across either says the Voice went
    looking for what it was already holding (#194).
    """
    return f"就说{focus_workspace}那个吧。"


def detail_request(focus_workspace: str) -> str:
    """ "Tell me more" — the utterance that asks the Call Agent for `brief` (#198).

    It **names the Session** for `relay_request`'s reason: run
    `20260903T081717Z` had an utterance that named none send the Call Agent
    looking through nine Sessions this machine was running.
    """
    return f"请你详细说说{focus_workspace}那个会话现在是什么情况。"


def history_request(focus_workspace: str) -> str:
    """ "What did it say before" — the utterance that asks for `history` (#198)."""
    return f"那{focus_workspace}它之前说了什么？请你说说更早的记录。"


def earlier_request(focus_workspace: str) -> str:
    """ "Further back" — the utterance that asks for the older page (#198, #171).

    Paging is on the map's destination, and the page before the newest one is
    what `bridgectl history <address> --before <ordinal>` answers. The Session is
    named again rather than left to context: this sentence arrives after two
    others, and a Call Agent that had lost the thread would page some other
    Session's record.
    """
    return f"再往前，把{focus_workspace}更早的那一页也说一下。"


#: What the *fourth* variant asks, and it asks for a verb the Call Agent owns.
#: `live call` v2 (#196) needs the Focus Session to change **during** a call, and
#: the one thing that moves it is the user relaying into a Session (#165 Q2) — so
#: this sentence asks for a relay and the Call Agent runs `bridgectl relay`,
#: which the wrapper log records.
#:
#: **It names the Session**, by the one half of its name the harness picks. Run
#: `20260903T081717Z` is why: an earlier version named none, and on both lanes
#: the Call Agent went looking with `brief` and never relayed at all — on the
#: claude lane it briefed two of the nine Sessions this machine is running,
#: which is also a relay that must not land. The task half really cannot be
#: pinned (it is the agent's own thread name), so the project half is.
#:
#: **Worded for a recogniser, not for a page.** The same run had the corner
#: brackets dropped and 吧 come back as 把, so what reached the Call Agent was
#: not a quotable message: `请你把继续把这句话转达给…`. No brackets here, no
#: 吧/把, and `回一句话` is the Answer Relay's own shape rather than a synonym
#: for it. **No hang-up ask** either, for `LONG_REQUEST`'s reason: this call has
#: to outlive the relay by a whole turn.
RELAY_REQUEST = relay_request(FOCUS_WORKSPACE_NAME)

#: The voice `say` synthesises with. `Flo` and `Eddy` are premium zh_CN voices
#: that have never been downloaded on this machine, and `say` does not say so —
#: it exits 0 and writes 0.41 s of something that is not the sentence, where
#: `Tingting` writes 3.95 s that is (probe `:335-341`).
WAV_VOICE = "Tingting"

#: The rate the request is synthesised at: the backend's own
#: `REALTIME_AUDIO_SAMPLE_RATE`, and the rate a WebRTC track must be resampled
#: up from.
WAV_SAMPLE_RATE = 24_000

#: The shortest a synthesised request may plausibly be. Under this, the voice
#: was not installed and `say` emitted a stub.
WAV_MINIMUM_SECONDS = 1.0

#: The variants this version can play, by the names the step selects them with
#: and the observation records them under. `plain` is the probe's own name for
#: the unpadded hang-up utterance, kept because the observation is compared
#: against the probe's record; `long` is #184's.
PLAIN = "plain"
LONG = "long"
NEEDS = "needs"
RELAY = "relay"
#: The second half of the hand-over question: the narrowing the Voice's own rule
#: says a Session name belongs to (#198).
NARROWING = "narrowing"
#: #198's three: Detail, History and History's older page, each of which the
#: Call Agent has to answer with a verb of its own.
DETAIL = "detail"
HISTORY = "history"
EARLIER = "earlier"

#: What the *current or next* call plays, as a file in `wav_directory` holding
#: one variant name a line. Two things force a per-call channel rather than a
#: settings key: one engine walks every step of a lane, and every call step runs
#: on it — so a sentence fixed at engine start would be one step's sentence
#: spoken into the other step's call. It is derived from `wav_directory` rather
#: than configured because both sides already agree on that path, and one more
#: key would be one more thing `derive_config` and `HarnessSettings` had to keep
#: in step.
#:
#: **A list rather than one name** (#196). v1's mechanism played one utterance
#: per call, which is every call the harness could describe: a call that is
#: spoken into once. `live call` v2 speaks a relay into a call that is already
#: up and then drives a second Session's Stop on that same call, so the step has
#: to be able to put a *second* sentence on a track while the first call is
#: still holding it. The step appends a line; the transport plays whichever
#: lines it has not played yet, one at a time and never over itself.
#:
#: Absent, empty or unknown reads as `PLAIN`: the step that does not care about
#: the variant gets the one #183 accepted.
NEXT_VARIANT_FILE = "next-variant"

#: How often the transport looks at that file while a call is up. Not every
#: frame: `_next` runs every 20 ms inside the event loop, and a stat plus a read
#: fifty times a second is disk work in the one place that must never fall
#: behind the media clock. Half a second is far under any step's own timing —
#: the sentences either side of it are seconds long — and it is also the floor
#: on the silence between two queued utterances, which is a floor worth having.
PLAYLIST_POLL_SECONDS = 0.5

#: How long the call is left alone after the peer connection comes up, before
#: the utterance goes out. The probe's `--settle` default, and the figure every
#: #179/#181 run used.
SETTLE_SECONDS = 10.0


class HarnessSettingsError(Exception):
    """`[adapters.settings.call]` does not say enough for the harness to run."""


@dataclass(frozen=True)
class HarnessSettings:
    """What the harness tells its own Call adapter, out of the shared table.

    The composition root forwards `[adapters.settings.call]` opaquely
    (`composition.py:412`), and `RealtimeCallSettings.of` refuses every key it
    does not recognise. So the two halves of the table are separated here, on
    the way in, and the adapter is handed only its own.

    The two paths have **no defaults**: they are how the step on the other side
    of the process boundary finds this run, and a default would put them
    somewhere no lane is looking. Everything else defaults to the probe's own
    proven value.
    """

    #: Where the JSONL this run writes goes. Per lane — two lanes run at once.
    observations: Path
    #: Where the synthesised WAVs are kept, so a person can listen to them after.
    wav_directory: Path
    request: str = REQUEST
    long_request: str = LONG_REQUEST
    needs_request: str = NEEDS_REQUEST
    #: This lane's two extra Sessions' workspace names (#196). The relay
    #: utterance is built from the first rather than configured beside it, so
    #: the name that is said and the name that is created cannot drift.
    focus_workspace: str = FOCUS_WORKSPACE_NAME
    ringing_workspace: str = RINGING_WORKSPACE_NAME
    #: The third one (#198). Nothing here says it out loud; it is carried so the
    #: engine-side half and the step agree on every workspace one run creates.
    waiting_workspace: str = WAITING_WORKSPACE_NAME
    voice: str = WAV_VOICE
    wav_sample_rate: int = WAV_SAMPLE_RATE
    settle_seconds: float = SETTLE_SECONDS

    @property
    def requests(self) -> dict[str, str]:
        """Every utterance this run can put on a track, by its variant name."""
        return {
            PLAIN: self.request,
            LONG: self.long_request,
            NEEDS: self.needs_request,
            RELAY: relay_request(self.focus_workspace),
            NARROWING: narrowing_request(self.focus_workspace),
            DETAIL: detail_request(self.focus_workspace),
            HISTORY: history_request(self.focus_workspace),
            EARLIER: earlier_request(self.focus_workspace),
        }

    @property
    def next_variant_path(self) -> Path:
        """Where the step says which variant the next call plays."""
        return self.wav_directory / NEXT_VARIANT_FILE

    @classmethod
    def split(cls, table: dict[str, Any] | None) -> tuple[HarnessSettings, dict[str, Any]]:
        """This module's keys, and everything else, which is the adapter's."""
        given = dict(table or {})
        mine = {field.name: given.pop(field.name) for field in fields(cls) if field.name in given}
        for name in ("observations", "wav_directory"):
            if not str(mine.get(name, "")).strip():
                raise HarnessSettingsError(
                    f"[adapters.settings.call] must name {name}: the acceptance step reads "
                    f"this run out of that path, and it is not this module's to default"
                )
            mine[name] = Path(str(mine[name])).expanduser()
        if "wav_sample_rate" in mine:
            mine["wav_sample_rate"] = int(mine["wav_sample_rate"])
        if "settle_seconds" in mine:
            mine["settle_seconds"] = float(mine["settle_seconds"])
        return cls(**mine), given


def ask_for(wav_directory: Path, variant: str) -> None:
    """Say which variant the next call plays. Written by the step, read at dial.

    **The list is replaced, not appended to.** Every step that dials states its
    variant, including the one that wants the default: a file left behind by the
    call before it is otherwise the thing that decides, and "whatever the last
    step wanted" is not a request anybody made — nor is "and then whatever the
    step before that had left queued".
    """
    wav_directory.mkdir(parents=True, exist_ok=True)
    (wav_directory / NEXT_VARIANT_FILE).write_text(variant + "\n", encoding="utf-8")


def ask_for_nothing(wav_directory: Path) -> None:
    """Say that this call opens in silence, and waits to be told what to say (#196).

    An **empty** list, which is a different answer from no list at all: a step
    that has written nothing has not asked for silence, and gets #183's
    utterance. `live call` v2 needs a call that comes up and says nothing until
    the step has watched something else happen on it, so it says so.
    """
    wav_directory.mkdir(parents=True, exist_ok=True)
    (wav_directory / NEXT_VARIANT_FILE).write_text("", encoding="utf-8")


def ask_next(wav_directory: Path, variant: str) -> None:
    """Queue one more utterance for the call that is up (#196).

    Appended rather than written, because the transport reads the whole list and
    plays what it has not played yet: replacing the file would re-play the
    sentence this call opened with. The step that appends is saying "and then
    say this", and it says it while the call it is talking about is running.
    """
    wav_directory.mkdir(parents=True, exist_ok=True)
    with (wav_directory / NEXT_VARIANT_FILE).open("a", encoding="utf-8") as playlist:
        playlist.write(variant + "\n")


def variants_asked_for(wav_directory: Path, known: tuple[str, ...]) -> tuple[str, ...]:
    """Every variant queued so far, in order. What cannot be played reads as `PLAIN`.

    A **missing** file is one `PLAIN`: the step that did not say anything about
    the variant gets the one #183 accepted, and a call nobody wrote a line for
    is not a call that was asked to stay quiet. A file that exists and holds no
    name is `()` — a step that asked for silence and will say when to break it
    (`ask_for_nothing`).
    """
    path = wav_directory / NEXT_VARIANT_FILE
    if not path.exists():
        return (PLAIN,)
    return tuple(
        line.strip() if line.strip() in known else PLAIN
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )


# --- what crosses the process boundary --------------------------------------


class Observations:
    """One JSON object a line, appended and flushed as each thing happens.

    A line at a time rather than one document at the end, because the reader is
    another process and the writer is a call that may be cut off mid-way: a
    half-written run still parses, and what it got as far as saying is still
    readable.
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path

    def note(self, what: str, **fields: Any) -> None:
        entry = {"at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"), "what": what, **fields}
        with self.path.open("a", encoding="utf-8") as sink:
            sink.write(json.dumps(entry, ensure_ascii=False) + "\n")


@dataclass(frozen=True)
class Observed:
    """What the engine-side run wrote down, as the step reads it.

    Every field is optional: a call that never came up wrote nothing, and the
    step says "not observed" rather than failing to parse.
    """

    entries: tuple[dict[str, Any], ...] = ()
    variant: str | None = None
    end_reason: str | None = None
    transport_factory: str | None = None


def observed(path: Path) -> Observed:
    """Read one run's observation file. A missing file is nothing observed."""
    if not path.exists():
        return Observed()
    entries: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            # A line cut off mid-write is the last one; everything before it
            # still happened, and dropping the whole file would lose it.
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return Observed(
        entries=tuple(entries),
        variant=_last(entries, "variant"),
        end_reason=_last(entries, "reason"),
        transport_factory=_last(entries, "transport_factory"),
    )


def _last(entries: list[dict[str, Any]], field: str) -> Any:
    """The newest value written for one field, or None if none ever was."""
    for entry in reversed(entries):
        if entry.get(field) is not None:
            return entry[field]
    return None


# --- the audio, ported from the probe ---------------------------------------


def say(text: str, path: Path, *, voice: str = WAV_VOICE, rate: int = WAV_SAMPLE_RATE) -> Path:
    """Synthesise one utterance to a mono 16-bit WAV, and prove it is really one.

    `say` exits 0 for a voice it cannot actually speak with, writing a short stub
    instead of the sentence, so the exit code says nothing. What says something
    is the duration: a stub is under half a second and the request is about four.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "say",
            "-v",
            voice,
            "-o",
            str(path),
            f"--data-format=LEI16@{rate}",
            "--file-format=WAVE",
            text,
        ],
        check=True,
    )
    with wave.open(str(path), "rb") as synthesised:
        seconds = synthesised.getnframes() / synthesised.getframerate()
        channels, width = synthesised.getnchannels(), synthesised.getsampwidth()
    if channels != 1 or width != 2:
        raise RuntimeError(f"{path} is not mono 16-bit: channels={channels} width={width}")
    if seconds < WAV_MINIMUM_SECONDS:
        raise RuntimeError(
            f"voice {voice!r} produced {seconds:.2f}s for {text!r}, under the "
            f"{WAV_MINIMUM_SECONDS}s floor — it is almost certainly not installed. "
            "`say -v '?'` lists the names; a premium voice must be downloaded first."
        )
    return path


def pcm_at_48k(path: Path) -> bytes:
    """One WAV as 48 kHz mono s16 bytes, resampled by `av` if it is not already.

    48 kHz is what the track carries (`webrtc.py`'s `SAMPLE_RATE`), and `av` is
    the resampler already in the process — the same one `_Speaker` uses on the
    way back. `MediaPlayer` would have done all this and is still the wrong
    tool: it ends the track at EOF (`aiortc/contrib/media.py:121-127`), which
    would stop RTP in the middle of the call.

    Imported inside the body, the way `webrtc.py` does it: `av` is the voice
    extra, CI installs `.[dev]` only, and everything else in this module is
    ordinary code the fast suite runs. Imported **after** the pass-through
    return rather than before it, so a WAV already at the track's rate needs no
    resampler to be installed — which the probe's version did not distinguish
    (`:840`) because a probe only ever runs where the extra is.
    """
    with wave.open(str(path), "rb") as source:
        rate = source.getframerate()
        payload = source.readframes(source.getnframes())
    if rate == SAMPLE_RATE:
        return payload

    import av

    resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
    frame = av.AudioFrame(format="s16", layout="mono", samples=len(payload) // SAMPLE_BYTES)
    frame.planes[0].update(payload)
    frame.sample_rate = rate
    frame.pts = 0
    frame.time_base = fractions.Fraction(1, rate)
    resampled = bytearray()
    for out in [*resampler.resample(frame), *resampler.resample(None)]:
        # Only the first `samples * SAMPLE_BYTES` bytes are audio; the rest of
        # the plane is padding, and `_Speaker` learned the hard way it is audible.
        resampled += bytes(out.planes[0])[: out.samples * SAMPLE_BYTES]
    return bytes(resampled)


def framed(pcm: bytes) -> list[bytes]:
    """PCM cut into the exact 20 ms payloads `_Track.recv` hands to `av`.

    Every frame is `FRAME_SAMPLES * SAMPLE_BYTES` bytes and the last is padded with
    silence, because `plane.update` wants the plane's whole buffer.
    """
    width = FRAME_SAMPLES * SAMPLE_BYTES
    return [pcm[at : at + width].ljust(width, b"\x00") for at in range(0, len(pcm), width)]


class WavTrackSource:
    """The frame source that stands in for the microphone's.

    It is the third implementation of the same one-method hole `_Microphone`
    already has two of (`webrtc.py:262-276`): captured frames, paced silence,
    and now queued WAV frames falling back to paced silence. The pacing is
    copied exactly, because it is load-bearing — frames handed over as fast as
    the encoder asks would run the media clock ahead of the wall clock, and the
    far side would be listening to a call that had already ended.

    The clock and the sleep are parameters so the pacing can be tested without
    spending the time it paces (`tests/test_live_call_harness.py`). Production
    passes neither and gets the real ones.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        observations: Observations | None = None,
    ) -> None:
        self._clock = clock
        self._sleep = sleep
        self._observations = observations
        self._pending: deque[bytes] = deque()

    def enqueue(self, frames: list[bytes], *, variant: str = PLAIN) -> float:
        """Queue one utterance. Returns how long it will take to go out."""
        self._pending.extend(frames)
        seconds = len(frames) * FRAME_SAMPLES / SAMPLE_RATE
        if self._observations is not None:
            self._observations.note(
                "wav utterance on the track",
                variant=variant,
                frames=len(frames),
                seconds=round(seconds, 2),
            )
        return seconds

    @property
    def idle(self) -> bool:
        """Whether the track is carrying silence — nothing queued and nothing mid-word.

        The one question a second utterance has to ask before it goes out: the
        wire truncates an utterance a second one is appended to (#175), and the
        harness must not be the thing that proves it.
        """
        return not self._pending

    async def next(self, track: Any) -> bytes:
        """One 20 ms payload: the next WAV frame, or silence, paced in real time."""
        if track._started is None:
            track._started = self._clock()
        delay = track._started + track._pts / SAMPLE_RATE - self._clock()
        if delay > 0:
            await self._sleep(delay)
        if not self._pending:
            return SILENT_FRAME
        payload = self._pending.popleft()
        if not self._pending and self._observations is not None:
            self._observations.note("wav utterance finished")
        return payload


# --- the transport, and the adapter the run config names --------------------


class HarnessCallTransport:
    """The production transport, with its microphone fed from a WAV.

    Everything about the call is the real one out of `webrtc.py`: the peer
    connection, the track, the Opus encoder and the real-time pacing. Two things
    are added, and they are the whole of what this class is:

    * the frame source is replaced, which `_Microphone` exposes exactly one
      method for — `recv` looks `_next` up on the instance (`webrtc.py:248`), so
      a function set there shadows the class's own. The same seam `silent=True`
      fills, filled a third way (probe `:966-979`);
    * the ending is written down. `CallEnded` is an event with no log line of
      its own (`core/bridge.py:780-785` notes only the interlock), so what the
      audio path saw — this side closing, or the connection going away by
      itself — is recorded here, and the step reads it back.

    No device is opened: the microphone grant is triggered by opening the
    device, and `silent=True` never does.
    """

    def __init__(
        self,
        *,
        settings: HarnessSettings,
        observations: Observations,
        utterances: dict[str, list[bytes]],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._observations = observations
        #: Every utterance this run can put on a track, already synthesised,
        #: resampled and framed — by `harness_call`, while the engine was being
        #: assembled. Not here, and that is the point: this runs inside the event
        #: loop, one step before the handshake, and `say` is a subprocess and the
        #: resampler is CPU. A second of blocking there is a second the peer
        #: connection is not being set up in.
        #:
        #: All of them rather than the one this call opens with, because since
        #: #196 the step may queue another *while the call is up* and the answer
        #: to "which sentence is that" has to already be in memory.
        self._utterances = utterances
        self._clock = clock
        self._real = webrtc_transport(silent=True)
        self._source = WavTrackSource(observations=observations)
        #: How many of the playlist's lines have been **queued** — the cursor
        #: into it, not a count of finished utterances: it is incremented when a
        #: line is handed to the frame source, which is a moment before the
        #: first frame of it leaves. `WavTrackSource.idle` is what says whether
        #: the last one has actually gone out.
        self._enqueued = 0
        self._connected_at: float | None = None
        self._looked_at: float | None = None
        self._ended: str | None = None
        # Reaching past the transport's own surface, deliberately and only here.
        self._real._microphone._next = self._next  # type: ignore[attr-defined]
        queued = self._playlist()
        observations.note(
            "wav source installed",
            transport_factory=REFERENCE,
            # `None` where the step asked for silence, so the observation says
            # what this call opened as rather than naming a sentence nobody
            # queued. The reader takes the newest non-null (`_last`), which is
            # the variant that actually went out once one does.
            variant=queued[0] if queued else None,
            queued=list(queued),
            voice=settings.voice,
            request=settings.requests[queued[0]] if queued else None,
            rate=settings.wav_sample_rate,
            settle_seconds=settings.settle_seconds,
        )

    @property
    def _spoke(self) -> bool:
        """Whether this call was ever talked into at all."""
        return self._enqueued > 0

    def _playlist(self) -> tuple[str, ...]:
        return variants_asked_for(self._settings.wav_directory, tuple(self._utterances))

    async def _next(self, track: Any) -> bytes:
        """Every 20 ms, and the only clock this side has once the call is up.

        The utterance is put on the track from here rather than by anything
        outside the process, because there is nothing outside the process: the
        step that asked for the call is in pytest, and this is the engine. What
        it waits for is the peer connection being up and then staying up for
        `settle_seconds` — the probe's own dial-time silence window, which every
        run that was heard used (`--settle`, default 10 s).
        """
        if self._real.is_connected:
            now = self._clock()
            if self._connected_at is None:
                self._connected_at = now
                self._observations.note(
                    "peer connection up", settle_seconds=self._settings.settle_seconds
                )
            elif now - self._connected_at >= self._settings.settle_seconds and self._source.idle:
                if self._looked_at is None or now - self._looked_at >= PLAYLIST_POLL_SECONDS:
                    self._looked_at = now
                    self._speak_the_next_line()
        return await self._source.next(track)

    def _speak_the_next_line(self) -> None:
        """Put the next queued utterance on the track, if the step has queued one.

        Read off disk each time rather than captured at dial, because the step
        that queues the second one is in pytest and this is the engine: the file
        is the whole of what crosses between them, in the same direction
        `observations` crosses back.
        """
        queued = self._playlist()
        if self._enqueued >= len(queued):
            return
        variant = queued[self._enqueued]
        self._enqueued += 1
        self._source.enqueue(self._utterances[variant], variant=variant)

    # -- the `CallTransport` protocol, delegated ----------------------------

    async def offer(self) -> str:
        return await self._real.offer()

    async def accept_answer(self, sdp: str) -> None:
        await self._real.accept_answer(sdp)

    async def wait_connected(self, timeout_seconds: float) -> None:
        await self._real.wait_connected(timeout_seconds)

    async def playback_drained(self, timeout_seconds: float) -> None:
        # Delegated whole. The harness replaces the microphone and nothing about
        # the speaker, so the playout fact is the real transport's own (#195).
        await self._real.playback_drained(timeout_seconds)

    @property
    def is_connected(self) -> bool:
        return self._real.is_connected

    def on_lost(self, handler: LostHandler) -> None:
        def lost(reason: str) -> None:
            self._note_end(f"the connection went away by itself: {reason}")
            handler(reason)

        self._real.on_lost(lost)

    async def aclose(self) -> None:
        # `aclose` is idempotent and the adapter calls it on every path, so the
        # first reason recorded is the one that says what really ended the call.
        self._note_end("this side closed the audio path")
        await self._real.aclose()

    def _note_end(self, reason: str) -> None:
        if self._ended is not None:
            return
        self._ended = reason
        self._observations.note("call ended", reason=reason, spoke=self._spoke)


def harness_call(*, sink: Any = None, settings: dict[str, Any] | None = None) -> Any:
    """`[adapters] call` — the shipped adapter, with a transport that speaks.

    The adapter is the production `RealtimeCallAdapter`: the signalling
    conversation, the Delegated Turn and the classification rules are all the
    ones being accepted. Only the audio path is the harness's, and it is handed
    over through the parameter the shipped factory already has for it.

    **Every utterance is synthesised here**, while the engine is still being
    assembled, and the same frames are reused by every call this engine holds.
    Two reasons, and both are the shipped factory's own reasoning applied one
    seam over (`realtime/__init__.py`: the voice extra is proved present *here*
    so a misconfiguration is a refusal to start rather than an outage
    mid-call):

    * a voice that is not installed, or a `say` that writes a stub, is then a
      run that never starts rather than a call that comes up mute and fails
      twenty minutes later for a reason nothing names;
    * `say` is a subprocess and the resampler is CPU, and doing either inside
      the event loop is a second stolen from the handshake.

    **All of them, not the one this run will use**, for the first reason above:
    one engine holds every call a lane's walk makes, and a variant first
    synthesised at dial time would move a broken voice from "the run never
    started" to "the last step failed for a reason nothing names". Which ones go
    on the track, and in what order, is decided per call by the step that
    dialled — and since #196 a step may add one while its call is still up.
    """
    mine, theirs = HarnessSettings.split(settings)
    observations = Observations(mine.observations)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    utterances: dict[str, list[bytes]] = {}
    for variant, sentence in mine.requests.items():
        utterances[variant] = framed(
            pcm_at_48k(
                say(
                    sentence,
                    mine.wav_directory / f"{stamp}-{variant}-{mine.wav_sample_rate}.wav",
                    voice=mine.voice,
                    rate=mine.wav_sample_rate,
                )
            )
        )
        observations.note(
            "utterance synthesised",
            transport_factory=REFERENCE,
            voice=mine.voice,
            rate=mine.wav_sample_rate,
            frames=len(utterances[variant]),
            seconds=round(len(utterances[variant]) * FRAME_SAMPLES / SAMPLE_RATE, 2),
            synthesised=variant,
        )

    def build() -> CallTransport:
        return HarnessCallTransport(settings=mine, observations=observations, utterances=utterances)

    return realtime_call(sink=sink, settings=theirs, transport_factory=build)

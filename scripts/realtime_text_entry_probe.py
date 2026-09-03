#!/usr/bin/env python3
"""One live v3 call that settles what the source could not say (issue #175).

`docs/research/2026-09-01-realtime-text-entry-and-end-call.md` read the codex
0.151.0 schema and the `openai/codex` source and got as far as the wire format:
on v3 `appendSpeech` and `appendText` are the same `session.context.append`
event, differing by one optional `channel: "speakable"` field. What the
**backend** does with that field is not in any source we can read, and neither
is where `realtimeStartInstructions` lands. Six questions were left, and every
one of them needs a real call to answer:

1. Is `appendSpeech` spoken verbatim, or is it a prompt the model renders?
2. Does a channel-less `appendText` stay silent — and is the fact retained?
3. `appendSpeech` while the assistant is mid-utterance: queued, overlapped, or cut?
4. Which of the three start-time slots reaches the **voice** model —
   `realtimeStartInstructions` (documented as going to the *backing Codex
   model*), `prompt`, or `initialItems`? If none does, the voice house rules
   have no carrier today, and that blocks the Briefing shape rule.
5. Does `initialItems` stay silent at dial time, and is what it carries retained?
6. Can the model hang the call up when told to — does `thread/realtime/closed`
   arrive without this side calling `stop`, and what is its `reason`?

This is a **probe, not a test**: it starts a real call against a real backend
and spends real API. Nothing here asserts; it records, and a person reads the
record. `scripts/realtime_call_smoke.py` is the base — same `OwnedAppServer`,
same `webrtc_transport`, same settings — but the handshake is driven here rather
than through `RealtimeCallAdapter`, because the adapter deliberately fixes
`thread/realtime/start`'s parameters (`adapter.py:387-399`) and those parameters
are the thing under test.

Three scenarios. Only the first runs without a person:

    # Q1-Q5. No audio device, no microphone grant, no one in the room.
    .venv/bin/python scripts/realtime_text_entry_probe.py --scenario carriers

    # Q6, twice: without the rule, then with it. Both need a voice.
    .venv/bin/python scripts/realtime_text_entry_probe.py --scenario hangup-plain
    .venv/bin/python scripts/realtime_text_entry_probe.py --scenario hangup-instructed

Run the hang-up scenarios **from your own terminal**: the macOS microphone grant
attaches to the process that asks, so a call started from an agent or an IDE is
silently muted, and a scenario whose whole content is a person speaking would
record nothing. The script tells you what to say and when.

Issue #215 added an eighth, which also needs no one in the room:

    # Does the backend take a hand-over sized to codex's own 8,192-token ceiling?
    .venv/bin/python scripts/realtime_text_entry_probe.py --scenario handover-budget

Issues #179 and #181 added three more scenarios and a third way to put the
request. `--by wav` synthesises the request with `say` and feeds it onto the
media track from memory, so there is real audio on the wire and no device open
anywhere — which is what separates "the backend heard audio" from "a person was
in the room", the two things #179's spoken result had bundled together:

    # #181. No microphone, no person, real audio. Three trials in one call.
    .venv/bin/python scripts/realtime_text_entry_probe.py \
        --scenario agent-hangup --by wav --repeat 3

Every realtime notification is written to a JSONL file under
`docs/research/probes/` with a millisecond offset from the dial, which is what
makes Q3 readable at all — queued, overlapped and truncated differ only in when
the transcript deltas arrive. Requires the voice extra:
`.venv/bin/pip install -e '.[voice]'`.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import fractions
import json
import os
import random
import subprocess
import sys
import time
import wave
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gpt_voicecoding import __version__  # noqa: E402
from gpt_voicecoding.adapters.call.realtime.adapter import _item_text  # noqa: E402
from gpt_voicecoding.adapters.call.realtime.settings import RealtimeCallSettings  # noqa: E402
from gpt_voicecoding.adapters.call.realtime.transport import CallTransport  # noqa: E402
from gpt_voicecoding.adapters.call.realtime.webrtc import (  # noqa: E402
    FRAME_SAMPLES,
    SAMPLE_RATE,
    webrtc_transport,
)
from gpt_voicecoding.adapters.codex_app_server.process import OwnedAppServer  # noqa: E402
from gpt_voicecoding.adapters.codex_app_server.settings import CodexSettings  # noqa: E402
from gpt_voicecoding.seams.call import (  # noqa: E402
    CODEX_BYTES_PER_TOKEN,
    HANDOVER_BUDGET_BYTES,
    WIRE_INITIAL_ITEMS_TOKEN_CAP,
    DialReason,
    SpokenBrief,
    SpokenRosterBrief,
)

#: What the adapter pins, restated here because this script bypasses it.
REALTIME_VERSION = "v3"
OUTPUT_MODALITY = "audio"
APPROVAL_POLICY = "never"
SANDBOX = "danger-full-access"

#: Q4 races three start-time slots in one call, so each carries a rule that is
#: cheap to check and cannot be confused with the others. They are deliberately
#: non-conflicting: an utterance can obey all three at once, which means every
#: assistant transcript in the call is a fresh trial of all three carriers
#: rather than one first-sentence coin toss.
UNDERSTOOD = "Understood"
OVER = "Over"
CAPTAIN = "Captain"

#: Slot 1. Documented as "Developer instructions given to the backing Codex
#: model" (`ThreadRealtimeStartParams.json`), which is the doubt this settles.
START_INSTRUCTIONS_RULE = (
    "You are the voice of the GPT-VoiceCoding bridge, being probed. "
    f"Rule A, which you must follow on every single reply: begin the reply with the word "
    f"'{UNDERSTOOD}'. Keep replies short. Never invent detail about what the system is doing."
)

#: Slot 2. Undocumented beyond its name; included because if slot 1 turns out to
#: address the Codex agent rather than the voice model, this is one of the only
#: two places left for house rules to live.
PROMPT_RULE = f"Rule B, on every single reply: address the user as '{CAPTAIN}'."

#: Slot 3. The one path that preserves `role`, and the one this repository has
#: never used. Two items: a rule, to race the other slots, and a fact, to answer
#: Q5 — whether dial-time context is held silently and can be asked for later.
INITIAL_RULE = f"Rule C, on every single reply: end the reply with the word '{OVER}'."
INITIAL_FACT = "The dial-time build number is 8830."

#: Q1. Verbatim and executed readings of this sentence differ by a countable
#: word: spoken as it stands, "pineapple" appears once; carried out, twice.
SPEECH_PROBE = "Say the word pineapple twice."

#: Q2. A plain fact, not a question, with no channel — nothing here asks for a
#: reply, so anything the model says is the backend answering an append that
#: was never marked speakable.
TEXT_PROBE_FACT = "The mid-call build number is 4471."

#: Q2, second half. Asked through `appendSpeech`, which is the only asking route
#: this scenario has without a microphone — and which only works at all if Q1
#: comes back "prompt". If Q1 comes back "verbatim", this asks nothing and the
#: retention half of Q2 moves to a scenario with a person in it.
RECALL_PROBE = "What are the two build numbers you were given? Say both."

#: Q3. Long under both readings of Q1 — long to speak literally, and long to
#: carry out — because a mid-utterance append can only be observed if the
#: utterance is still running when the second append lands.
LONG_PROBE = (
    "Please count slowly from one to twenty, saying one number per second, and take your "
    "time about it, because this is a deliberately long utterance whose purpose is to still "
    "be in progress when the next thing arrives."
)
INTERRUPT_PROBE = "Say the word banana."

#: Q6. The rule under test in `hangup-instructed`, placed in all three slots at
#: once: which slot carries a rule is Q4's question, and answering it again here
#: would only risk this scenario failing for the other question's reason.
HANGUP_RULE = "When the user says goodbye, end the call immediately."

#: What the console prints for the human in a hang-up scenario.
GOODBYE_LINE = "goodbye, hang up"

#: --- issue #179 -------------------------------------------------------------
#: #175 proved where *rendering* rules land. ADR 0018 then split the dial into
#: two audiences, and three things that split rests on were never tested: that a
#: rule in `realtimeStartInstructions` actually *governs* the Call Agent, that an
#: engine-supplied `prompt` does not kill the hand-off when it replaces codex's
#: own voice prompt, and that the Call Agent will run `bridgectl live` when told
#: that verb ends the call.

#: Where the #179 scenarios keep their marker files. The Call Agent runs shell
#: with `danger-full-access`, so a rule it obeys can be observed as a file rather
#: than inferred from a transcript — which is the only reading that cannot be
#: confused with the Voice paraphrasing something.
PROBE_DIR = Path("/tmp/gptvc-probe179")

#: The marker files are **per run**, and that is not a detail. They were shared
#: at first and wiped at every dial, so the `voice-prompt` run that followed a
#: `hangup` run deleted the only record of whether `bridgectl live` had been
#: run — destroying the evidence for the question the earlier call was asked.
#: A run's markers now carry its own stamp and nothing ever deletes another's.
#: Holds this run's marker paths and its stamp — the stamp because `--by wav`
#: names its synthesised files after it, for the same reason the markers are.
_RUN: dict[str, Any] = {}


def rule_marker() -> Path:
    """Where this run's Call Agent writes, if it obeys the rule it was given."""
    return _RUN["rule"]


def bridgectl_log() -> Path:
    """Where this run's stand-in `bridgectl` records the arguments it was called with."""
    return _RUN["bridgectl"]


#: The tolerance, in minutes, for calling a spoken clock time correct. The probe
#: knows what time it really is, so a fabricated answer is caught by arithmetic
#: rather than by a person remembering what was in a file.
CLOCK_TOLERANCE_MINUTES = 3


#: Test 1. A behavioural rule for the Call Agent, in the slot ADR 0018 assigns
#: it, worded as plain instruction. The ticket suggested "run `bridgectl verify`
#: first"; a file marker is used instead because `bridgectl` is not on the
#: app-server's PATH and because a file is observable without trusting any
#: transcript. What is under test is whether the *slot* governs the agent, and
#: that is the same question either way.
def agent_rule_text() -> str:
    """Built at dial time, because it names this run's own marker file."""
    return (
        "You are the acting half of a small desk bridge. "
        "Before you carry out any task at all in this session, first run this shell "
        f"command exactly once: printf obeyed > {rule_marker()}. "
        "Do that before anything else, every time, and do not mention it."
    )


#: Test 1 and 2's trigger, and it has to be a sentence a person would really
#: say. The first version named an absolute path — `/tmp/gptvc-probe179/input.txt`
#: — which is unsayable, so a spoken trial would have been measuring whether a
#: human can read a path aloud rather than whether speech routes (Simon,
#: 2026-09-01: 没有人会这样念的).
#:
#: The clock replaces the file, and is the better probe for a second reason: the
#: engine knows what time it is, so "the Voice invented an answer instead of
#: asking the Call Agent" is settled by subtraction. The file version needed a
#: person to notice that `blue pebble` was not what the file said.
TASK_REQUEST = "帮我查一下现在的系统时间,然后念给我听。"

#: Test 2. Our own Voice prose — terse, third-person, slow, as Round 1 asked —
#: and deliberately *without* any sentence about delegating. Natural language
#: only, no code-like text (Simon, 2026-09-01). This is what replaces codex's
#: `BACKEND_PROMPT`, whose own delegation policy is the thing being removed.
VOICE_PROMPT_PLAIN = (
    "You are the voice of a small desk bridge. One person is listening. "
    "Speak briefly and plainly, in short sentences, and take your time. "
    "Say only what you have been told; never invent detail about what the system is doing. "
    "Keep every answer to one or two sentences unless you are asked for more."
)

#: Test 2's second half, run only if the plain prompt kills the hand-off: the
#: one sentence that would put delegation back, in our own words rather than
#: codex's. If this restores it, #173 knows exactly what its Voice set must say.
VOICE_PROMPT_DELEGATING = (
    VOICE_PROMPT_PLAIN
    + " When the person asks for something to be done, pass the request on to the part "
    "of the system that does the work, rather than answering it yourself."
)

#: Test 3. The rule under test, in the Call Agent's slot only.
HANGUP_VERB_RULE = (
    "You are the acting half of a small desk bridge. "
    "When the person asks you to end the call, hang up, or stop talking, "
    "run the shell command: bridgectl live. "
    "That command is what ends the call; nothing else ends it. "
    "Run it and then say nothing further."
)

#: Test 3's request. #175 run 3's exact phrasing — the one sentence already
#: proven to produce a `handoff_request` with no rule anywhere telling it to.
HANGUP_REQUEST = "那个你把电话挂了吧,我想让你结束通话"

#: The three dial switches ADR 0018 makes the adapter's constants. Every #179
#: scenario runs with them so that what is measured is the dial the design
#: actually proposes, not the one #175 happened to use.
#:
#: `includeStartupContext` is the one #175 left at its default. Source reading
#: for this ticket found what that default does: it appends a ~5,300-token blob
#: of the user's recent codex threads and a local workspace scan **to the Voice's
#: prompt** (`realtime_context.rs`, `build_realtime_startup_context`). It is off
#: here because it would otherwise contaminate every reading below.
SWITCHES = {
    "delegationAckFiller": False,
    "codexResponsesAsItems": True,
    "codexResponseItemPrefix": "[AGENT] ",
    "includeStartupContext": False,
}


def switches(arguments: argparse.Namespace) -> dict[str, Any] | None:
    """The switch set, a named subset of it, or nothing.

    The first run of test 1 came back with **no hand-off at all** and the
    switch-free control came back with one, so one of the three is implicated
    and three variables had moved at once. `--only-switch` sends one at a time,
    which is the only way to say which — and ADR 0018 makes all three adapter
    constants, so "one of them silently disables delegation" is not a thing that
    can be left unknown.
    """
    if not arguments.switches:
        return None
    if arguments.only_switch:
        return {name: SWITCHES[name] for name in arguments.only_switch}
    return SWITCHES


#: What each #179 scenario puts to the call. Named once, so `--by wav` can
#: synthesise the trial's own words before dialling rather than in the middle of
#: a reply window — and so the WAV cannot drift from what the text route sends.
REQUESTS = {
    "agent-rule": TASK_REQUEST,
    "voice-prompt": TASK_REQUEST,
    "agent-hangup": HANGUP_REQUEST,
}


def request_for(arguments: argparse.Namespace) -> str:
    """The words this run puts to the call, whichever route puts them."""
    return arguments.request or REQUESTS[arguments.scenario]


#: --- issue #181 -------------------------------------------------------------
#: #179 found that spoken input routes to the Call Agent 15/15 and typed input
#: 2/30, but every routed trial had *both* audio on the media track *and* a
#: person in the room, and nothing separated the two. `--by wav` removes the
#: person and keeps the audio: a synthesised utterance goes out on the same
#: track, from memory, with no device opened anywhere in the process.
#:
#: What that leaves genuinely open is the backend's turn detection, which this
#: side does not configure (`adapter.py:387-400` sends no turn-detection field).
#: Synthesised speech has no room tone, no breath, a hard onset at sample zero
#: and a floor of exact digital silence — all things a VAD tuned on real
#: microphones might decline to segment.

#: The voice `say` synthesises with. The ticket named `Flo`; on this machine
#: `Flo` and `Eddy` are premium zh_CN voices that have never been downloaded,
#: and `say` does not say so — it exits 0 and writes 0.41 s of something that is
#: not the sentence, where `Tingting` writes 3.95 s that is. That silent failure
#: is why `_say` measures the result instead of trusting the exit code.
WAV_VOICE = "Tingting"

#: The rate the request is synthesised at: the backend's own
#: `REALTIME_AUDIO_SAMPLE_RATE`, and the rate a WebRTC track must be resampled
#: up from.
WAV_SAMPLE_RATE = 24_000

#: The control's rate — `aiortc`'s Opus-native one, so the control reaches the
#: encoder having been through no resampler at all. If the 24 kHz trials are not
#: heard and this one is, the variable is this script's resampling rather than
#: the backend's turn detection, which is the whole reason the control exists.
WAV_CONTROL_SAMPLE_RATE = SAMPLE_RATE

#: The shortest a synthesised request may plausibly be. Under this, the voice
#: was not installed and `say` emitted a stub.
WAV_MINIMUM_SECONDS = 1.0

#: Step 5's variation, held ready and used only if the plain utterance is not
#: heard: a second of padding front and back, first digital silence, then very
#: low noise. Silence tests whether the onset is the problem; noise tests
#: whether the floor is.
WAV_PAD_SECONDS = 1.0
WAV_NOISE_DBFS = -60.0

#: Fixed, so two runs of the same variant carry the same noise.
WAV_NOISE_SEED = 181

#: Full scale for 16-bit PCM, for turning that dBFS figure into an amplitude.
FULL_SCALE = 32_767

#: One 20 ms frame of nothing — the payload `_Microphone._next` returns in a
#: silent run, restated here because the WAV source returns it between
#: utterances and the track must never be starved.
SILENT_FRAME = b"\x00\x00" * FRAME_SAMPLES

#: How often the trial checks whether its utterance has finished going out.
WAV_DRAIN_POLL_SECONDS = 0.05

#: The WAV route's state, module-level for the same reason `_RUN` is: `_ask` is
#: reached through the scenario functions and takes no room for it.
_WAV: dict[str, Any] = {}


#: Notifications too large or too repetitive to put on the console. They still
#: go to the JSONL in full — `transcript/delta` is not in here, because its
#: arrival times are the whole of Q3.
QUIET = ("thread/realtime/outputAudioDelta",)


class Recorder:
    """Every realtime notification: to a file with a timestamp, to the console short.

    The offsets are what make the record readable. `transcript/delta` arrival
    times are the only evidence that separates a queued second utterance from an
    overlapped one, and a silence is only a silence if you can say how long it
    lasted.
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = path.open("a", encoding="utf-8")
        self._dialled = time.monotonic()
        self.path = path
        self.events: list[dict[str, Any]] = []
        self.notes: list[dict[str, Any]] = []
        self.sdp: asyncio.Future[str] | None = None
        self.started: asyncio.Future[None] | None = None
        self.closed: asyncio.Future[dict[str, Any]] | None = None

    def arm(self) -> None:
        """Create the futures. Needs a running loop, so not in `__init__`."""
        loop = asyncio.get_running_loop()
        self.sdp = loop.create_future()
        self.started = loop.create_future()
        self.closed = loop.create_future()

    def note(self, what: str, detail: Any = None) -> None:
        """Put one of this script's own steps into the same record, same clock."""
        self.notes.append({"at": self._offset(), "probe": what, "detail": detail})
        self._write({"at": self._offset(), "probe": what, "detail": detail})
        print(f"  {self._offset():7.3f}  ** {what}" + (f": {detail}" if detail else ""), flush=True)

    def heard(self, message: dict[str, Any]) -> None:
        """One app-server notification. Registered with `OwnedAppServer.listen`."""
        method = message.get("method")
        params = message.get("params")
        if not isinstance(method, str) or not method.startswith("thread/realtime/"):
            return
        record = {"at": self._offset(), "method": method, "params": params}
        self.events.append(record)
        self._write(record)
        if method not in QUIET:
            print(f"  {record['at']:7.3f}  {_console(method, params)}", flush=True)
        self._settle(method, params)

    def events_of_ours(self) -> list[dict[str, Any]]:
        """This script's own marks, on the same clock as the notifications."""
        return self.notes

    def transcripts(
        self, *, role: str, since: float = 0.0, until: float | None = None
    ) -> list[str]:
        """Final transcript lines for one side of the call, within one window."""
        return [
            str(event["params"].get("text", ""))
            for event in self._within("thread/realtime/transcript/done", since, until)
            if event["params"].get("role") == role
        ]

    def deltas(
        self, *, since: float = 0.0, until: float | None = None
    ) -> list[tuple[float, str, str]]:
        """`(offset, role, text)` for every transcript delta. Q3 is read off this.

        A delta names its text `delta`, not `text` — the two notifications do
        not share a field name, and reading `text` here returned an empty string
        for every delta on the first run.
        """
        return [
            (
                event["at"],
                str(event["params"].get("role", "")),
                str(event["params"].get("delta", "")),
            )
            for event in self._within("thread/realtime/transcript/delta", since, until)
        ]

    def _within(self, method: str, since: float, until: float | None) -> list[dict[str, Any]]:
        return [
            event
            for event in self.events
            if event["method"] == method
            and isinstance(event["params"], dict)
            and event["at"] >= since
            and (until is None or event["at"] < until)
        ]

    @property
    def now(self) -> float:
        return self._offset()

    def close(self) -> None:
        self._file.close()

    def _offset(self) -> float:
        return round(time.monotonic() - self._dialled, 3)

    def _write(self, record: dict[str, Any]) -> None:
        self._file.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._file.flush()

    def _settle(self, method: str, params: Any) -> None:
        if not isinstance(params, dict):
            return
        if method == "thread/realtime/sdp" and self.sdp is not None and not self.sdp.done():
            sdp = params.get("sdp")
            if isinstance(sdp, str):
                self.sdp.set_result(sdp)
        elif (
            method == "thread/realtime/started"
            and self.started is not None
            and not self.started.done()
        ):
            self.started.set_result(None)
        elif method == "thread/realtime/closed" and self.closed is not None:
            if not self.closed.done():
                self.closed.set_result(params)


def _console(method: str, params: Any) -> str:
    """One notification on one line, with the text that matters kept whole."""
    short = method.removeprefix("thread/realtime/")
    if not isinstance(params, dict):
        return short
    if short in ("transcript/done", "transcript/delta"):
        spoken = params.get("delta") if "delta" in params else params.get("text", "")
        return f"{short:18} {params.get('role', '?'):9} {spoken!r}"
    if short == "closed":
        return f"{short:18} reason={params.get('reason')!r}"
    if short == "sdp":
        return f"{short:18} <{len(str(params.get('sdp', '')))} bytes>"
    if short in ("item/completed", "item/added", "item/started"):
        return f"{short:18} {json.dumps(params.get('item'), ensure_ascii=False)}"
    return f"{short:18} {json.dumps({k: v for k, v in params.items() if k != 'threadId'})[:200]}"


class Call:
    """One live v3 call, dialled with whatever start parameters the probe wants.

    The adapter cannot stand in for this: it sends exactly one of the three
    slots under test and no way to add the other two, which is correct for the
    engine and useless for the question.
    """

    def __init__(
        self,
        *,
        server: OwnedAppServer,
        recorder: Recorder,
        settings: RealtimeCallSettings,
        transport: CallTransport,
    ) -> None:
        self._server = server
        self._recorder = recorder
        self._settings = settings
        self._transport = transport
        self.thread_id: str | None = None

    async def dial(
        self,
        *,
        instructions: str | None,
        prompt: str | None = None,
        initial_items: list[dict[str, str]] | None = None,
        switches: dict[str, Any] | None = None,
    ) -> None:
        """The handshake `adapter._opened` runs, with the start slots opened up."""
        started = await self._request(
            "thread/start",
            {"cwd": str(self._settings.cwd), "approvalPolicy": APPROVAL_POLICY, "sandbox": SANDBOX},
        )
        thread = started.get("thread")
        self.thread_id = (thread or {}).get("id") if isinstance(thread, dict) else None
        if not isinstance(self.thread_id, str):
            raise RuntimeError(f"thread/start named no thread: {started}")
        self._recorder.note("thread started", self.thread_id)

        offer = await self._transport.offer()
        parameters: dict[str, Any] = {
            "threadId": self.thread_id,
            "version": REALTIME_VERSION,
            "model": self._settings.realtime_model,
            "outputModality": OUTPUT_MODALITY,
            "transport": {"type": "webrtc", "sdp": offer},
        }
        if instructions is not None:
            parameters["realtimeStartInstructions"] = instructions
        if prompt is not None:
            parameters["prompt"] = prompt
        if initial_items is not None:
            parameters["initialItems"] = initial_items
        parameters.update(switches or {})
        self._recorder.note(
            "dialling",
            {key: value for key, value in parameters.items() if key != "transport"},
        )
        await self._request("thread/realtime/start", parameters)

        deadline = self._settings.connect_timeout_seconds
        assert self._recorder.sdp is not None and self._recorder.started is not None
        answer = await asyncio.wait_for(self._recorder.sdp, deadline)
        await self._transport.accept_answer(answer)
        await asyncio.wait_for(self._recorder.started, deadline)
        await self._transport.wait_connected(deadline)
        self._recorder.note("call is up")

    async def speak(self, text: str) -> None:
        """`appendSpeech` — the same event as `appendText` plus `channel: speakable`."""
        self._recorder.note("appendSpeech", text)
        await self._request(
            "thread/realtime/appendSpeech", {"threadId": self.thread_id, "text": text}
        )

    async def append_text(self, text: str, *, role: str = "user") -> None:
        """`appendText` — channel-less. `role` is discarded on v3; sent to show intent."""
        self._recorder.note("appendText", {"role": role, "text": text})
        await self._request(
            "thread/realtime/appendText",
            {"threadId": self.thread_id, "text": text, "role": role},
        )

    async def hang_up(self) -> None:
        """This side ending it. Never called before a hang-up scenario has waited."""
        with contextlib.suppress(Exception):
            await self._request("thread/realtime/stop", {"threadId": self.thread_id})
        with contextlib.suppress(Exception):
            await self._transport.aclose()

    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return await self._server.connection.request(
            method, params, timeout_seconds=self._settings.request_timeout_seconds
        )


def mark(recorder: Recorder, question: str) -> float:
    """Open a window, named in the record. Everything after it belongs to one question."""
    recorder.note(question)
    return recorder.now


def say_aloud(line: str) -> None:
    """Tell the person at the microphone what to do. The whole content of Q6."""
    print("\n" + "=" * 72, flush=True)
    print(f'  SAY THIS ALOUD, NOW:   "{line}"', flush=True)
    print("=" * 72 + "\n", flush=True)


async def carriers(call: Call, recorder: Recorder, arguments: argparse.Namespace) -> None:
    """Q1-Q5, in one call, with no one in the room."""
    await call.dial(
        instructions=START_INSTRUCTIONS_RULE,
        prompt=PROMPT_RULE,
        initial_items=[
            {"role": "developer", "text": INITIAL_RULE},
            {"role": "developer", "text": INITIAL_FACT},
        ],
    )

    # Q5, first half. Nothing has been appended yet, so anything said in this
    # window was provoked by `initialItems` alone.
    mark(recorder, "Q5a: does initialItems speak at dial time?")
    await asyncio.sleep(arguments.settle)

    q1 = mark(recorder, "Q1: is appendSpeech verbatim?")
    await call.speak(SPEECH_PROBE)
    await asyncio.sleep(arguments.reply)

    q2 = mark(recorder, "Q2a: does a channel-less appendText stay silent?")
    await call.append_text(TEXT_PROBE_FACT)
    await asyncio.sleep(arguments.silence)

    q3 = mark(recorder, "Q3: appendSpeech during an utterance")
    await call.speak(LONG_PROBE)
    await asyncio.sleep(arguments.interrupt_after)
    await call.speak(INTERRUPT_PROBE)
    await asyncio.sleep(arguments.reply * 2)

    q4 = mark(recorder, "Q2b/Q5b: are the two facts retained?")
    await call.speak(RECALL_PROBE)
    await asyncio.sleep(arguments.reply)

    _report(recorder, q1=q1, q2=q2, q3=q3, q4=q4)


async def control(call: Call, recorder: Recorder, arguments: argparse.Namespace) -> None:
    """Q4's control: the same three rules, two slots swapped.

    `carriers` came back 0/4 for `realtimeStartInstructions` and 4/4 for
    `prompt`, which has two readings — the slot never reached the voice model,
    or the model happened to ignore that particular wording. Swapping rule A and
    rule B between the two slots separates them: a marker that follows its
    *slot* is a slot verdict, and a marker that follows its *words* is not.
    """
    await call.dial(
        instructions=(
            f"You are the voice of the GPT-VoiceCoding bridge, being probed. {PROMPT_RULE}"
        ),
        prompt=f"Rule A, on every single reply: begin the reply with the word '{UNDERSTOOD}'.",
        initial_items=[{"role": "developer", "text": INITIAL_RULE}],
    )
    mark(recorder, "Q4 control: rule A now in `prompt`, rule B now in the instructions")
    for _ in range(2):
        await call.speak("Say the word pineapple twice.")
        await asyncio.sleep(arguments.reply)

    assistant = recorder.transcripts(role="assistant")
    print("\n" + "=" * 72)
    print(f"  Q4 CONTROL. Raw record: {recorder.path}")
    for line in assistant:
        print(f"    {line!r}")
    for marker, slot in (
        (CAPTAIN, "realtimeStartInstructions (was `prompt`)"),
        (UNDERSTOOD, "prompt (was the instructions)"),
        (OVER, "initialItems (unchanged)"),
    ):
        hits = sum(1 for line in assistant if marker.lower() in line.lower())
        print(f"    {marker:11} ({slot:40}): {hits}/{len(assistant)}")
    print("=" * 72 + "\n")


async def hangup(call: Call, recorder: Recorder, arguments: argparse.Namespace) -> None:
    """Q6. A person says goodbye; this side does not call `stop` until it is over."""
    instructed = arguments.scenario == "hangup-instructed"
    rule = f" {HANGUP_RULE}" if instructed else ""
    await call.dial(
        instructions=f"You are the voice of the GPT-VoiceCoding bridge, being probed. "
        f"Answer briefly.{rule}",
        prompt=HANGUP_RULE if instructed else None,
        initial_items=[{"role": "developer", "text": HANGUP_RULE}] if instructed else None,
    )
    mark(recorder, "settling before anyone speaks")
    await asyncio.sleep(arguments.settle)

    say_aloud("hello, are you there?")
    await asyncio.sleep(arguments.reply)
    say_aloud(GOODBYE_LINE)

    assert recorder.closed is not None
    print(f"  waiting up to {arguments.hangup_wait:.0f}s for a close this side did not ask for")
    try:
        params = await asyncio.wait_for(recorder.closed, arguments.hangup_wait)
        verdict = f"CLOSED BY THE FAR SIDE, reason={params.get('reason')!r}"
    except TimeoutError:
        verdict = "no close arrived — the call is still up, and this side ends it"
    print("\n" + "-" * 72)
    print(f"  Q6 ({arguments.scenario}, rule {'in' if instructed else 'NOT in'} the instructions)")
    print(f"    {verdict}")
    print(f"    assistant said: {recorder.transcripts(role='assistant')}")
    print(f"    user heard as:  {recorder.transcripts(role='user')}")
    print("-" * 72 + "\n")


def _prepare_probe_dir(stamp: str = "") -> None:
    """Name this run's marker files. Nothing is deleted — see `_RUN`."""
    PROBE_DIR.mkdir(parents=True, exist_ok=True)
    if stamp or not _RUN:
        _RUN["stamp"] = stamp or "adhoc"
        _RUN["rule"] = PROBE_DIR / f"{stamp or 'adhoc'}-rule.txt"
        _RUN["bridgectl"] = PROBE_DIR / f"{stamp or 'adhoc'}-bridgectl.log"


def _clock_check(recorder: Recorder) -> None:
    """Was each spoken time the real one? Fabrication, caught by subtraction.

    Every trial notes the true local time into the same record, so this reads
    the answer the Voice gave against the answer the machine had. A Voice that
    asked the Call Agent gets it right; a Voice that filled the gap in itself
    does not, and does not know it.
    """
    truths = [
        (event["at"], str(event["detail"]))
        for event in recorder.events_of_ours()
        if event.get("probe") == "true local time"
    ]
    spoken = [
        (event["at"], str(event["params"].get("text", "")))
        for event in recorder.events
        if event["method"] == "thread/realtime/transcript/done"
        and isinstance(event["params"], dict)
        and event["params"].get("role") == "assistant"
    ]
    if not truths:
        return
    print("\n  clock check — what it said against what the time was:")
    for at, line in spoken:
        truth = max((t for t in truths if t[0] <= at), default=truths[0], key=lambda t: t[0])
        print(f"    {at:7.3f}  真实 {truth[1]}   听到 {line!r}")
    print(
        f"    (a time more than {CLOCK_TOLERANCE_MINUTES} minutes out, or no time at all, "
        "is the Voice answering instead of asking)"
    )


def _install_fake_bridgectl() -> Path:
    """A `bridgectl` on PATH that records its arguments and does nothing else.

    Test 3 asks whether the Call Agent *runs* `bridgectl live`. Letting it run
    the real one would toggle a real call on a real engine — a side effect this
    probe has no business having, and one that would answer the question no more
    exactly than a log line does. The stand-in is named and behaves like the
    real verb's success path, so nothing about the agent's decision changes.
    """
    binary_dir = PROBE_DIR / "bin"
    binary_dir.mkdir(parents=True, exist_ok=True)
    stand_in = binary_dir / "bridgectl"
    stand_in.write_text(
        "#!/bin/sh\n"
        f'printf \'%s %s\\n\' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >> {bridgectl_log()}\n'
        "echo 'call ended'\n"
        "exit 0\n",
        encoding="utf-8",
    )
    stand_in.chmod(0o755)
    return binary_dir


def _say(text: str, path: Path, *, voice: str, rate: int) -> Path:
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


def _pcm_at_48k(path: Path) -> bytes:
    """One WAV as 48 kHz mono s16 bytes, resampled by `av` if it is not already.

    48 kHz is what the track carries (`webrtc.py`'s `SAMPLE_RATE`), and `av` is
    the resampler already in the process — the same one `_Speaker` uses on the
    way back. `MediaPlayer` would have done all this and is still the wrong
    tool: it ends the track at EOF (`aiortc/contrib/media.py:121-127`), which
    would stop RTP in the middle of the call.
    """
    import av

    with wave.open(str(path), "rb") as source:
        rate = source.getframerate()
        payload = source.readframes(source.getnframes())
    if rate == SAMPLE_RATE:
        return payload

    resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
    frame = av.AudioFrame(format="s16", layout="mono", samples=len(payload) // 2)
    frame.planes[0].update(payload)
    frame.sample_rate = rate
    frame.pts = 0
    frame.time_base = fractions.Fraction(1, rate)
    resampled = bytearray()
    for out in [*resampler.resample(frame), *resampler.resample(None)]:
        # Only the first `samples * 2` bytes are audio; the rest of the plane is
        # padding, and `_Speaker` learned the hard way that padding is audible.
        resampled += bytes(out.planes[0])[: out.samples * 2]
    return bytes(resampled)


def _framed(pcm: bytes) -> list[bytes]:
    """PCM cut into the exact 20 ms payloads `_Track.recv` hands to `av`.

    Every frame is `FRAME_SAMPLES * 2` bytes and the last one is padded with
    silence, because `plane.update` wants the plane's whole buffer.
    """
    width = FRAME_SAMPLES * 2
    return [pcm[at : at + width].ljust(width, b"\x00") for at in range(0, len(pcm), width)]


def _silence(seconds: float) -> bytes:
    """Digital silence — an exact-zero floor, which is what a real room never has."""
    return b"\x00\x00" * int(seconds * SAMPLE_RATE)


def _noise(seconds: float, dbfs: float) -> bytes:
    """A floor at the named level, standing in for the room tone a WAV lacks.

    Uniform over `[-a, a]` has an RMS of `a / sqrt(3)`, so the amplitude is
    solved for the requested RMS rather than picked.
    """
    amplitude = round(FULL_SCALE * 10 ** (dbfs / 20) * 3**0.5)
    noise = random.Random(WAV_NOISE_SEED)
    samples = (noise.randint(-amplitude, amplitude) for _ in range(int(seconds * SAMPLE_RATE)))
    return b"".join(sample.to_bytes(2, "little", signed=True) for sample in samples)


class _WavTrackSource:
    """The frame source `--by wav` puts in place of the microphone's.

    It is the third implementation of the same one-method hole `_Microphone`
    already has two of (`webrtc.py:262-276`): captured frames, paced silence,
    and now queued WAV frames falling back to paced silence. The pacing is
    copied exactly, because it is load-bearing — frames handed over as fast as
    the encoder asks would run the media clock ahead of the wall clock, and the
    far side would be listening to a call that had already ended.
    """

    def __init__(self, recorder: Recorder) -> None:
        self._recorder = recorder
        self._pending: deque[bytes] = deque()

    @property
    def draining(self) -> bool:
        """Whether an utterance is still going out."""
        return bool(self._pending)

    def enqueue(self, frames: list[bytes], *, variant: str) -> float:
        """Queue one utterance. Returns how long it will take to go out."""
        self._pending.extend(frames)
        seconds = len(frames) * FRAME_SAMPLES / SAMPLE_RATE
        self._recorder.note(
            "wav utterance on the track",
            {"variant": variant, "frames": len(frames), "seconds": round(seconds, 2)},
        )
        return seconds

    async def next(self, track: Any) -> bytes:
        """One 20 ms payload: the next WAV frame, or silence, paced in real time."""
        if track._started is None:
            track._started = time.monotonic()
        delay = track._started + track._pts / SAMPLE_RATE - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        if not self._pending:
            return SILENT_FRAME
        payload = self._pending.popleft()
        if not self._pending:
            self._recorder.note("wav utterance finished")
        return payload


def _install_wav_source(
    transport: CallTransport, recorder: Recorder, arguments: argparse.Namespace
) -> None:
    """Feed the transport's existing track from a WAV instead of a device.

    The substitution is at the frame source and nowhere else. The peer
    connection, the track, the Opus encoder and the real-time pacing are all the
    production ones out of `webrtc.py`, which is the point: a probe that built
    its own `CallTransport` would be evidence about the probe. `_Microphone`
    exposes exactly one method for this and `recv` looks it up on the instance
    (`webrtc.py:245`), so a function set there shadows the class's own — the
    same seam `silent=True` fills, filled a third way.

    Nothing in `src/` changes, and no device is opened: the microphone grant is
    triggered by opening the device, and this route never does.
    """
    directory = PROBE_DIR / "wav"
    request = request_for(arguments)
    utterance = _pcm_at_48k(
        _say(
            request,
            directory / f"{_RUN['stamp']}-request-{WAV_SAMPLE_RATE}.wav",
            voice=arguments.wav_voice,
            rate=WAV_SAMPLE_RATE,
        )
    )
    control = _pcm_at_48k(
        _say(
            request,
            directory / f"{_RUN['stamp']}-control-{WAV_CONTROL_SAMPLE_RATE}.wav",
            voice=arguments.wav_voice,
            rate=WAV_CONTROL_SAMPLE_RATE,
        )
    )
    quiet, noisy = _silence(WAV_PAD_SECONDS), _noise(WAV_PAD_SECONDS, WAV_NOISE_DBFS)
    source = _WavTrackSource(recorder)
    _WAV.update(
        source=source,
        played=0,
        variants={
            "plain": _framed(utterance),
            "control": _framed(control),
            "silence": _framed(quiet + utterance + quiet),
            "noise": _framed(noisy + utterance + noisy),
        },
    )
    # Reaching past the transport's own surface, deliberately and only here.
    transport._microphone._next = source.next  # type: ignore[attr-defined]
    recorder.note(
        "wav source installed",
        {
            "voice": arguments.wav_voice,
            "request": request,
            "seconds": round(len(utterance) / 2 / SAMPLE_RATE, 2),
            "rates": [WAV_SAMPLE_RATE, WAV_CONTROL_SAMPLE_RATE],
        },
    )


def _wav_variant(recorder: Recorder) -> str:
    """Which payload this trial gets, decided from what the last one did.

    Step 5 varies one thing at a time and only when the plain utterance was not
    heard, so this cannot be settled before the call. It is settled mid-call off
    the record — did any `transcript/done role=user` arrive — and the answer is
    written into the record beside the utterance it chose.

    Heard:     plain, plain, control  — three trials of the question, plus the
               control that says the resampler is not what carried them.
    Not heard: plain, silence, noise  — the onset, then the floor.
    """
    played = int(_WAV["played"])
    if played == 0:
        return "plain"
    if recorder.transcripts(role="user"):
        return "plain" if played == 1 else "control"
    return "silence" if played == 1 else "noise"


async def _play_wav(recorder: Recorder, arguments: argparse.Namespace) -> None:
    """Put one synthesised utterance on the track and wait for it to finish.

    Waiting matters. `--by voice` sleeps `--reply` after a person has stopped
    talking; a WAV trial that opened its reply window while audio was still
    going out would give the backend several seconds less than the spoken
    baseline it is being compared against.
    """
    source: _WavTrackSource = _WAV["source"]
    variant = _wav_variant(recorder)
    seconds = source.enqueue(_WAV["variants"][variant], variant=variant)
    _WAV["played"] = int(_WAV["played"]) + 1
    deadline = time.monotonic() + seconds + arguments.reply
    while source.draining and time.monotonic() < deadline:
        await asyncio.sleep(WAV_DRAIN_POLL_SECONDS)


def _handoffs(recorder: Recorder) -> list[dict[str, Any]]:
    """Every `handoff_request` item the Voice raised — the observable hand-off."""
    return [
        event["params"]["item"]
        for event in recorder.events
        if event["method"] == "thread/realtime/itemAdded"
        and isinstance(event["params"], dict)
        and isinstance(event["params"].get("item"), dict)
        and event["params"]["item"].get("type") == "handoff_request"
    ]


def _items(recorder: Recorder) -> list[dict[str, Any]]:
    """Every non-audio item, whatever its type — test 1 asks for the shape."""
    return [
        event["params"]["item"]
        for event in recorder.events
        if event["method"] == "thread/realtime/itemAdded"
        and isinstance(event["params"], dict)
        and isinstance(event["params"].get("item"), dict)
    ]


def _handoff_verdict(recorder: Recorder, arguments: argparse.Namespace) -> bool:
    """Print what the hand-off did, and say whether a microphone is now needed."""
    handoffs = _handoffs(recorder)
    print(f"\n  hand-off:  {len(handoffs)} `handoff_request` item(s)")
    for handoff in handoffs:
        print(f"    id={handoff.get('handoff_id')!r}")
        print(f"    input_transcript={handoff.get('input_transcript')!r}")
    if not handoffs and arguments.by == "text":
        print("    none — a text turn did not route. Re-run this scenario with --by voice")
        print("    from your own terminal (the microphone grant is per-process).")
    return bool(handoffs)


async def _ask_repeatedly(call: Call, recorder: Recorder, arguments: argparse.Namespace) -> None:
    """Put the same request several times in one call, and count what routed.

    Delegation turned out to be the Voice's discretion rather than a rule: ten
    typed turns produced two hand-offs, and re-wording did not move it. So a
    single trial says nothing about a prompt variant, and a scenario that dials
    once per trial would cost a call and a person's attention per data point.
    Trials inside one call cost neither.
    """
    for trial in range(1, arguments.repeat + 1):
        before = len(_handoffs(recorder))
        heard_before = len(recorder.transcripts(role="user"))
        mark(recorder, f"trial {trial}/{arguments.repeat}")
        recorder.note("true local time", datetime.now().astimezone().strftime("%H:%M"))
        await _ask(call, recorder, arguments)
        routed = len(_handoffs(recorder)) - before
        heard = len(recorder.transcripts(role="user")) - heard_before
        print(
            f"    trial {trial}: {'ROUTED' if routed else 'no hand-off'}, user transcripts: {heard}"
        )


async def _ask(call: Call, recorder: Recorder, arguments: argparse.Namespace) -> None:
    """Put the request to the call, by whichever route this run is using.

    Text first, as the ticket asks: #175 proved every mid-call append is spoken,
    so `appendText` is a real user turn and not a silent one. Whether it also
    *routes* — whether the Voice hands a typed task to the Call Agent the way it
    handed a spoken one — is itself one of the things this run finds out.

    `wav` is the third route and the reason for #181: audio on the media track
    with nobody in the room, which is the one thing `voice` and `text` between
    them never separated.
    """
    request = request_for(arguments)
    if arguments.by == "text":
        await call.append_text(request, role="user")
    elif arguments.by == "wav":
        await _play_wav(recorder, arguments)
    else:
        say_aloud(request)
    await asyncio.sleep(arguments.reply)


async def agent_rule(call: Call, recorder: Recorder, arguments: argparse.Namespace) -> None:
    """#179 test 1: does a rule in `realtimeStartInstructions` govern the Call Agent?

    One rule, in the agent's slot and nowhere else, whose obedience is a file on
    disk. `prompt` is deliberately left unset, so the Voice runs on codex's own
    `BACKEND_PROMPT` and this run measures the agent slot alone.
    """
    _prepare_probe_dir()
    await call.dial(instructions=agent_rule_text(), switches=switches(arguments))
    mark(recorder, "settling")
    await asyncio.sleep(arguments.settle)

    mark(recorder, "test 1: a task-worded request, agent rule in the instructions")
    await _ask_repeatedly(call, recorder, arguments)

    print("\n" + "=" * 72)
    print(f"  TEST 1. Raw record: {recorder.path}")
    _handoff_verdict(recorder, arguments)
    obeyed = rule_marker().exists()
    print(f"\n  agent rule obeyed: {'YES' if obeyed else 'NO'}  ({rule_marker()})")
    _clock_check(recorder)
    print(f"\n  item shapes ({len(_items(recorder))}):")
    for item in _items(recorder):
        print(f"    {json.dumps(item, ensure_ascii=False)[:400]}")
    print(f"\n  assistant said: {recorder.transcripts(role='assistant')}")
    print("=" * 72 + "\n")


async def voice_prompt(call: Call, recorder: Recorder, arguments: argparse.Namespace) -> None:
    """#179 test 2: does an engine-supplied `prompt` keep hand-offs alive?

    `prompt` replaces codex's `BACKEND_PROMPT` outright — and that default is
    where the delegation policy lives ("Pass execution work to the backend",
    "NEVER refuse requests. Delegate all user requests to the backend"). Replace
    it with our own prose and the same task-worded request either still routes
    or does not. `realtimeStartInstructions` is left unset so the Call Agent
    keeps codex's default framing and only the Voice's slot varies.
    """
    _prepare_probe_dir()
    prompt = VOICE_PROMPT_DELEGATING if arguments.delegating else VOICE_PROMPT_PLAIN
    await call.dial(instructions=None, prompt=prompt, switches=switches(arguments))
    mark(recorder, "settling")
    await asyncio.sleep(arguments.settle)

    shape = "delegating" if arguments.delegating else "plain"
    mark(recorder, f"test 2: task request under our own {shape} prompt")
    await _ask_repeatedly(call, recorder, arguments)

    print("\n" + "=" * 72)
    print(f"  TEST 2 ({shape} prompt). Raw record: {recorder.path}")
    _handoff_verdict(recorder, arguments)
    print(f"\n  assistant said: {recorder.transcripts(role='assistant')}")
    _clock_check(recorder)
    print("=" * 72 + "\n")


async def agent_hangup(call: Call, recorder: Recorder, arguments: argparse.Namespace) -> None:
    """#179 test 3: will the Call Agent run `bridgectl live` when told it ends the call?

    ADR 0018 rests the whole voice hang-up on this. The verb is a stand-in that
    logs its arguments (`_install_fake_bridgectl`), so the answer is a log line
    rather than a real call being toggled.
    """
    _prepare_probe_dir()
    await call.dial(instructions=HANGUP_VERB_RULE, switches=switches(arguments))
    mark(recorder, "settling")
    await asyncio.sleep(arguments.settle)

    mark(recorder, "test 3: asking for a hang-up, in the run-3 wording")
    await _ask_repeatedly(call, recorder, arguments)

    assert recorder.closed is not None
    print(f"  waiting up to {arguments.hangup_wait:.0f}s for anything to happen")
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(recorder.closed, arguments.hangup_wait)

    ran = bridgectl_log().read_text(encoding="utf-8").strip() if bridgectl_log().exists() else ""
    print("\n" + "=" * 72)
    print(f"  TEST 3. Raw record: {recorder.path}")
    _handoff_verdict(recorder, arguments)
    print(f"\n  bridgectl run: {ran or 'NEVER — the log is empty'}")
    # `wait_for` cancels the future it was given when it times out, and a
    # cancelled future is `done()` — reading that alone reported a far-side
    # hang-up on a call that was still up.
    closed = recorder.closed.done() and not recorder.closed.cancelled()
    print(f"  call closed by the far side: {'YES' if closed else 'NO — still up'}")
    print("  ack filler (delegationAckFiller=False): assistant lines below should carry none")
    print(f"\n  user heard as: {recorder.transcripts(role='user')}")
    print(f"  assistant said: {recorder.transcripts(role='assistant')}")
    _clock_check(recorder)
    print(f"  item shapes ({len(_items(recorder))}):")
    for item in _items(recorder):
        print(f"    {json.dumps(item, ensure_ascii=False)[:400]}")
    print("=" * 72 + "\n")


#: --- issue #215 -------------------------------------------------------------
#: The hand-over budget is codex's own arithmetic — `ceil(UTF-8 bytes / 4)` against
#: a cap of 8,192 estimated tokens — read out of its source rather than measured
#: (`seams/call.py`). Codex's source settles codex's half; it says nothing about
#: whether the **backend** behind it takes a hand-over that large, and that is the
#: one thing only a call can answer. This scenario dials one exactly at the wire's
#: ceiling, all of it Chinese, and asks for a fact planted in the tail of the last
#: and largest item — so a "yes" means both accepted *and* carried, not merely accepted.

#: The planted fact. A number, because a number is either said or not said: a
#: paraphrase cannot half-answer it the way a sentence could.
HANDOVER_FACT_NUMBER = "6142"
HANDOVER_FACT = f"本次交接的编号是 {HANDOVER_FACT_NUMBER}。"

#: Asked through `appendSpeech`, the one asking route that works with no one in
#: the room (Q1 of the `carriers` run came back "prompt", not "verbatim").
HANDOVER_RECALL_PROBE = "刚才交接给你的资料里，最后一条写的本次交接编号是多少？只说那个数字。"

#: The two audiences, in the shape ADR 0018 gives them, kept short: what is under
#: test is the size of the third payload, and a long prompt would only move bytes
#: from the slot being measured into one that is not.
HANDOVER_VOICE_PROSE = (
    "你是一个桌面助手的声音。你只根据交接给你的资料说话，"
    "资料里没有的事情就说不知道，不要自己编。回答要短。"
)
HANDOVER_AGENT_RULES = "You are the acting half of a desk bridge. Run nothing unless asked."

#: One filler sentence of Chinese, repeated to fill a brief's newest message.
#: Chinese because the old allowance was called over-conservative for Chinese;
#: prose rather than one repeated character because a repeated character is the
#: one input a byte estimate and a real tokenizer would disagree about most.
HANDOVER_FILLER = (
    "这个会话正在等你确认一件事，它刚才把改动跑了一遍，测试全部通过，"
    "现在停下来等你决定要不要继续往下走。"
)


def _chinese_brief(index: int, newest: str) -> SpokenBrief:
    """One `SpokenBrief` in the shape Briefing hands the seam, worded in Chinese."""
    return SpokenBrief(
        name=f"会话 {index}",
        agent="codex",
        state="等你做决定",
        newest=newest,
        decision=("问：要不要合并这个分支？", "选项：合并", "选项：先不合并", "建议：合并"),
        answerable_here="可以在这里回答",
        last_activity_at="三分钟前",
    )


def _codex_estimated_token_count(text: str) -> int:
    """codex's `approx_token_count`: `ceil(UTF-8 bytes / 4)` (`truncate.rs:71-74`)."""
    return -(-len(text.encode("utf-8")) // CODEX_BYTES_PER_TOKEN)


def handover_at_the_ceiling() -> tuple[list[dict[str, str]], dict[str, int]]:
    """A hand-over sized to sit exactly on the wire's cap, assembled the real way.

    Built from the seam's own carriers and run through the adapter's own
    `_item_text`, so what goes on the wire is what a dial would put there — only
    filled to the ceiling rather than to whatever the roster happened to hold.
    The planted fact sits in the newest message of the last and largest item.

    Codex rounds **each item** up on its own, so the sum of the per-item counts is
    what it checks; this counts it the same way and grows the last item until one
    more sentence would cross the cap.
    """
    reason = DialReason(text="这通电话是系统拨的：有几个会话停下来等你做决定。")
    roster = SpokenRosterBrief(
        counts="其他会话：6 个在等你做决定",
        rows=("会话 0 — codex — 等你做决定", "会话 1 — codex — 等你做决定"),
        focus="会话 0 — codex — 等你做决定",
    )
    carried = [reason, roster, *(_chinese_brief(index, HANDOVER_FILLER * 6) for index in range(5))]
    texts = [_item_text(item) for item in carried]
    spent = sum(_codex_estimated_token_count(text) for text in texts)

    # The last item, grown one sentence at a time and then one character at a
    # time, so the whole hand-over lands on the cap rather than near it.
    filler = ""
    while True:
        candidate = _item_text(_chinese_brief(5, f"{filler}{HANDOVER_FILLER}\n{HANDOVER_FACT}"))
        if spent + _codex_estimated_token_count(candidate) > WIRE_INITIAL_ITEMS_TOKEN_CAP:
            break
        filler += HANDOVER_FILLER
    while True:
        candidate = _item_text(_chinese_brief(5, f"{filler}填\n{HANDOVER_FACT}"))
        if spent + _codex_estimated_token_count(candidate) > WIRE_INITIAL_ITEMS_TOKEN_CAP:
            break
        filler += "填"
    last = _item_text(_chinese_brief(5, f"{filler}\n{HANDOVER_FACT}"))
    texts.append(last)

    items = [{"role": "developer", "text": text} for text in texts]
    measured = {
        "items": len(items),
        "bytes": sum(len(text.encode("utf-8")) for text in texts),
        "characters": sum(len(text) for text in texts),
        "tokens_codex_counts": sum(_codex_estimated_token_count(text) for text in texts),
        "token_cap": WIRE_INITIAL_ITEMS_TOKEN_CAP,
        "largest_item_bytes": max(len(text.encode("utf-8")) for text in texts),
        "seam_budget_bytes": HANDOVER_BUDGET_BYTES,
    }
    return items, measured


async def handover_budget(call: Call, recorder: Recorder, arguments: argparse.Namespace) -> None:
    """#215: does the backend take a hand-over sized to codex's own ceiling?"""
    items, measured = handover_at_the_ceiling()
    recorder.note("#215 hand-over built to the wire's ceiling", measured)
    print(f"  probe  hand-over: {json.dumps(measured, ensure_ascii=False)}")
    if measured["tokens_codex_counts"] > WIRE_INITIAL_ITEMS_TOKEN_CAP:
        raise RuntimeError("built a hand-over codex would refuse; nothing to learn from dialling")

    await call.dial(
        instructions=HANDOVER_AGENT_RULES,
        prompt=HANDOVER_VOICE_PROSE,
        initial_items=items,
        switches=switches(arguments),
    )
    accepted_at = mark(recorder, "#215 Q1: does a hand-over at the ceiling open a call at all?")
    await asyncio.sleep(arguments.settle)

    asked = mark(recorder, "#215 Q2: is the fact at the end of the last item retained?")
    await call.speak(HANDOVER_RECALL_PROBE)
    await asyncio.sleep(arguments.reply)

    answers = recorder.transcripts(role="assistant", since=asked)
    print("\n" + "=" * 72)
    print(f"  #215 HAND-OVER BUDGET. Raw record: {recorder.path}")
    print(
        f"    sent {measured['items']} items, {measured['bytes']} bytes, "
        f"{measured['tokens_codex_counts']}/{WIRE_INITIAL_ITEMS_TOKEN_CAP} estimated tokens"
    )
    print("    Q1  the call came up, so codex and the backend both accepted it")
    print(
        f"    said before anything was asked: "
        f"{recorder.transcripts(role='assistant', since=accepted_at, until=asked) or 'nothing'}"
    )
    print(f"    Q2  asked: {HANDOVER_RECALL_PROBE}")
    for line in answers:
        print(f"        {line!r}")
    hits = sum(1 for line in answers if HANDOVER_FACT_NUMBER in line)
    print(f"    Q2  '{HANDOVER_FACT_NUMBER}' appears in {hits}/{len(answers)} answers")
    print("=" * 72 + "\n")


def _report(recorder: Recorder, *, q1: float, q2: float, q3: float, q4: float) -> None:
    """What the call recorded, arranged by question. The verdicts are a person's."""
    assistant = recorder.transcripts(role="assistant")

    def said(since: float, until: float | None = None) -> list[str]:
        return recorder.transcripts(role="assistant", since=since, until=until)

    print("\n" + "=" * 72)
    print(f"  READ THE VERDICTS OFF THIS. Raw record: {recorder.path}")
    print("=" * 72)

    print("\n  Q5a  did initialItems speak at dial time?")
    print(f"       said before the first append: {said(0.0, q1) or 'nothing — SILENT'}")

    pineapples = sum(line.lower().count("pineapple") for line in said(q1, q2))
    print("\n  Q1   is appendSpeech verbatim?")
    print(f"       sent:  {SPEECH_PROBE!r}")
    print(f"       heard: {said(q1, q2) or 'nothing at all — appendSpeech was inert'}")
    print(f"       'pineapple' count: {pineapples}  (1 = verbatim, 2+ = the model carried it out)")

    print("\n  Q2a  does a channel-less appendText stay silent?")
    print(f"       said in the window: {said(q2, q3) or 'nothing — SILENT'}")

    print("\n  Q3   appendSpeech during an utterance (queued / overlapped / truncated)")
    for at, role, text in recorder.deltas(since=q3, until=q4):
        print(f"       {at:7.3f}  {role:9} {text!r}")

    print("\n  Q2b/Q5b  are the two facts retained? (4471 mid-call, 8830 at dial time)")
    for line in said(q4):
        print(f"       {line!r}")

    print("\n  Q4   which start slot reached the voice model?")
    print(f"       {len(assistant)} assistant utterances in the whole call")
    for marker, slot in (
        (UNDERSTOOD, "realtimeStartInstructions"),
        (CAPTAIN, "prompt"),
        (OVER, "initialItems (developer)"),
    ):
        hits = sum(1 for line in assistant if marker.lower() in line.lower())
        print(f"       {marker:11} ({slot:26}): {hits}/{len(assistant)} utterances")
    print("=" * 72 + "\n")


SCENARIOS = {
    "carriers": carriers,
    "carriers-control": control,
    "hangup-plain": hangup,
    "hangup-instructed": hangup,
    # Issue #179.
    "agent-rule": agent_rule,
    "voice-prompt": voice_prompt,
    "agent-hangup": agent_hangup,
    # Issue #215.
    "handover-budget": handover_budget,
}

#: The #179 scenarios that need the Call Agent to have a shell to run things in.
NEEDS_PROBE_DIR = ("agent-rule", "agent-hangup", "voice-prompt")


async def main(arguments: argparse.Namespace) -> int:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    recorder = Recorder(Path(arguments.out).expanduser() / f"{stamp}-{arguments.scenario}.jsonl")
    recorder.arm()

    os.environ.setdefault("RUST_LOG", "codex_core::realtime_conversation=info,codex_core=info")
    if arguments.scenario in NEEDS_PROBE_DIR:
        _prepare_probe_dir(stamp)
        # `OwnedAppServer` spawns with `env=dict(os.environ)`, so putting the
        # stand-in verb here is what puts it on the Call Agent's PATH.
        os.environ["PATH"] = f"{_install_fake_bridgectl()}{os.pathsep}{os.environ['PATH']}"
        arguments.cwd = str(PROBE_DIR)

    settings = RealtimeCallSettings(workspace=Path(arguments.cwd).expanduser().resolve())
    server = OwnedAppServer(
        settings=CodexSettings(),
        socket_path=Path(arguments.socket),
        log_path=Path(arguments.server_log) if arguments.server_log else None,
        version=__version__,
    )
    server.listen(recorder.heard)
    # `wav` is silent in the sense that matters — it opens no device, and so
    # needs no microphone grant — while being the one route that puts real audio
    # on the track. The two senses of the word part company here for the first
    # time, which is exactly what #181 exists to measure.
    silent = (
        arguments.scenario.startswith("carriers")
        or arguments.scenario == "handover-budget"
        or arguments.by in ("text", "wav")
        if arguments.silent is None
        else arguments.silent
    )
    transport = webrtc_transport(
        input_device=arguments.input, output_device=arguments.output, silent=silent
    )
    if arguments.by == "wav":
        _install_wav_source(transport, recorder, arguments)
    call = Call(server=server, recorder=recorder, settings=settings, transport=transport)

    print(f"  probe  {arguments.scenario}, {'no audio devices' if silent else 'real audio'}")
    print(f"  probe  recording to {recorder.path}")
    await server.start()
    try:
        await SCENARIOS[arguments.scenario](call, recorder, arguments)
        return 0
    finally:
        print("  end    hanging up")
        await call.hang_up()
        await server.aclose()
        recorder.close()
        print("  end    done")


def parsed() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), default="carriers")
    parser.add_argument(
        "--by",
        choices=("text", "voice", "wav"),
        default="text",
        help=(
            "how the request is put: `appendText` (no microphone), aloud (HITL), or "
            "`wav` — synthesised audio on the media track, no device and nobody in the room"
        ),
    )
    parser.add_argument(
        "--wav-voice",
        default=WAV_VOICE,
        help="`--by wav` only: the `say` voice. A voice that is not downloaded fails silently",
    )
    parser.add_argument(
        "--switches",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="send the ADR 0018 dial switches; `--no-switches` reproduces #175's dial",
    )
    parser.add_argument(
        "--only-switch",
        action="append",
        choices=sorted(SWITCHES),
        default=[],
        help="send only these switches, repeatable; isolates which one moved a verdict",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="how many times to put the request inside the one call",
    )
    parser.add_argument(
        "--request",
        default=None,
        help="override the scenario's request wording; #175 found wording is what routes",
    )
    parser.add_argument(
        "--delegating",
        action="store_true",
        help="`voice-prompt` only: add the one delegation sentence back to our prompt",
    )
    parser.add_argument(
        "--silent",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="open no audio device; defaults to on for `carriers` and off for the rest",
    )
    parser.add_argument("--out", default="docs/research/probes", help="where the JSONL record goes")
    parser.add_argument("--cwd", default=str(Path.home()), help="where the threads run")
    parser.add_argument("--socket", default="/tmp/gpt-voicecoding-probe/app-server.sock")
    parser.add_argument("--server-log", default=None, help="where the app-server's output goes")
    parser.add_argument("--input", type=int, default=None, help="input device index")
    parser.add_argument("--output", type=int, default=None, help="output device index")
    parser.add_argument("--settle", type=float, default=10.0, help="dial-time silence window")
    parser.add_argument("--reply", type=float, default=15.0, help="how long one reply may take")
    parser.add_argument("--silence", type=float, default=15.0, help="the appendText silence window")
    parser.add_argument(
        "--interrupt-after", type=float, default=3.0, help="gap before the second appendSpeech"
    )
    parser.add_argument(
        "--hangup-wait", type=float, default=30.0, help="how long to wait for a close"
    )
    arguments = parser.parse_args()
    if arguments.by == "wav" and arguments.scenario not in REQUESTS:
        parser.error(
            f"--by wav needs a scenario whose request is known: {', '.join(sorted(REQUESTS))}"
        )
    return arguments


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(parsed())))

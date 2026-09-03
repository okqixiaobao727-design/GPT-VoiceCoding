"""The bridge-owned Live Call, against a scripted app-server and a fake audio path.

No real codex runs, no microphone opens and nothing dials a network. What is
under test is the part that decides things: the signalling conversation, the
classification of a `speak`, what happens to a thread when a Delegated Turn goes
wrong, and which of the four ways a call can stop are reported as which event.

The payload shapes are the ones codex 0.148.0 really uses — read out of its own
generated protocol bindings and out of the binary's request table
(`thread/realtime/{start,appendSpeech,stop}`, `ThreadRealtimeStartParams`'s
`realtimeStartInstructions` and `transport`), not invented here.

The edge cases the build issue named each have a test: an `ensure_call` on top
of a call that is already up, a `speak` with nothing to speak into, a call that
drops mid-`speak`, a Delegated Turn that never answers, and an `end_call` in the
middle of the handshake.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sys
import threading
import time
import types
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from codex_fake import FakeAppServer, FakeRemoteError
from gpt_voicecoding.adapters.call.realtime import (
    APPROVAL_POLICY,
    CODEX_RESPONSE_ITEM_PREFIX,
    CODEX_RESPONSES_AS_ITEMS,
    DEFAULT_REALTIME_MODEL,
    DELEGATION_ACK_FILLER,
    INCLUDE_STARTUP_CONTEXT,
    SANDBOX,
    DelegatedTurnError,
    RealtimeCallAdapter,
    RealtimeCallSettings,
    SettingsError,
    cues,
    realtime_call,
    webrtc,
)
from gpt_voicecoding.adapters.call.realtime.adapter import _item_text
from gpt_voicecoding.adapters.codex_app_server.process import AppServerError, attach
from gpt_voicecoding.adapters.codex_app_server.settings import CodexSettings
from gpt_voicecoding.seams.call import (
    CODEX_BYTES_PER_TOKEN,
    CallDropped,
    CallEnded,
    CallStarted,
    CallState,
    Cue,
    Dial,
    DialReason,
    SpokenBrief,
    SpokenRosterBrief,
    UserSpeaking,
    UserSpeech,
    VoiceSpeech,
)
from gpt_voicecoding.seams.delivery import Delivery
from gpt_voicecoding.seams.identity import RequestId
from gpt_voicecoding.seams.verify import VerifyOutcome
from realtime_fake import (
    ANSWER_SDP,
    OFFER_SDP,
    FakeCueOutput,
    FakeTransport,
    SharedAppServer,
    delegated_script,
    realtime_script,
)

THREAD = "01a02110-d18f-74a0-916d-de1208e9977a"
CALL_AGENT_INSTRUCTIONS = "speak the Session Name; never invent a detail"
CALL_VOICE_PROSE = "Speak in short sentences. Wait to be asked before giving detail."
DELEGATED_RULES = "act only through the control-plane CLI"


def dial(*hand_over: object) -> Dial:
    """What Bridge Core hands this adapter: two audiences and a hand-over."""
    return Dial(voice=CALL_VOICE_PROSE, agent=CALL_AGENT_INSTRUCTIONS, hand_over=tuple(hand_over))


def brief(newest: str) -> SpokenBrief:
    """One Session Brief in Briefing's own words, as the seam carries it."""
    return SpokenBrief(
        name="voicecoding · the dial",
        agent="codex",
        state="waiting for your decision",
        newest=newest,
        decision=("asked: ship it?", "option: yes", "option: no"),
        answerable_here="from here",
        last_activity_at="not read",
    )


#: One of every hand-over kind, in shapes assembly labels differently. Seam
#: carriers, not assembled text: the two budget invariants below run each of
#: these through `_item_text` and measure the result against what it was charged.
HANDOVER_ITEM_EXAMPLES = (
    DialReason(text="dialled because Sessions need the user"),
    SpokenRosterBrief(
        counts="the others: 2 running, 1 finished",
        rows=("build — codex:abc — running", "docs — claude:def:12 — finished"),
        focus="voicecoding · the dial — codex:ghi — waiting for your decision",
    ),
    brief("it stopped on a question"),
    SpokenBrief(
        name="a",
        agent="codex",
        state="running",
        newest="nothing said yet",
        decision=(),
        answerable_here="at the terminal",
        last_activity_at="not read",
    ),
)


_names = iter(range(10_000))


class Sink:
    """The event sink, recording what the adapter raised upward."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    def emit(self, event: Any) -> None:
        self.events.append(event)

    def of(self, kind: type) -> list[Any]:
        return [event for event in self.events if isinstance(event, kind)]


@pytest.fixture
def socket_path() -> Iterator[Path]:
    """A private directory, under a root short enough to bind. See `test_codex_agent`."""
    home = Path("/tmp") / f"vc-call-{next(_names)}-{id(object())}"
    home.mkdir(mode=0o700)
    yield home / "app-server.sock"
    shutil.rmtree(home, ignore_errors=True)


def quick(**overrides: Any) -> RealtimeCallSettings:
    """Settings whose waits are short enough for a test to actually spend them."""
    return RealtimeCallSettings(
        workspace=Path("/tmp"),
        connect_timeout_seconds=0.4,
        request_timeout_seconds=2.0,
        delegated_turn_timeout_seconds=0.4,
        **overrides,
    )


async def riding(
    server: FakeAppServer,
    sink: Sink,
    *,
    transport: FakeTransport | None = None,
    settings: RealtimeCallSettings | None = None,
    cue_player: FakeCueOutput | None = None,
) -> tuple[RealtimeCallAdapter, FakeTransport]:
    """An adapter wired to a scripted app-server, exactly as the root wires it."""
    audio = transport or FakeTransport()
    adapter = RealtimeCallAdapter(
        sink=sink,
        settings=settings or quick(),
        transport_factory=lambda: audio,
        cue_player=cue_player or FakeCueOutput(),
    )
    shared = SharedAppServer(connection=None)
    connection = await attach(
        server.path,
        version="0",
        settings=CodexSettings(request_timeout_seconds=2.0),
        on_notification=shared.heard,
        experimental=True,
    )
    shared.connection = connection
    adapter.use_app_server(shared)  # type: ignore[arg-type]
    await adapter.connect()
    return adapter, audio


def rid(text: str = "r-1") -> RequestId:
    return RequestId(text)


class _Stream:
    """What `sounddevice.RawOutputStream` is, as far as `CuePlayer` can tell."""

    def __init__(self, blocks_on: tuple[threading.Event, threading.Event] | None) -> None:
        self._blocks_on = blocks_on

    def start(self) -> None:
        return None

    def write(self, _pcm: bytes) -> None:
        if self._blocks_on is not None:
            writing, release = self._blocks_on
            writing.set()
            release.wait(2.0)

    def stop(self) -> None:
        return None

    def close(self) -> None:
        return None


class _Streams:
    """A stand-in device. Only the *first* stream opened holds its write open."""

    def __init__(self, *, blocks_on: tuple[threading.Event, threading.Event] | None) -> None:
        self._blocks_on = blocks_on
        self.opened: list[dict[str, Any]] = []

    def __call__(self, **parameters: Any) -> _Stream:
        self.opened.append(parameters)
        return _Stream(self._blocks_on if len(self.opened) == 1 else None)


@contextmanager
def _sounddevice(streams: _Streams) -> Iterator[None]:
    """`sounddevice`, for the length of a test. CI does not install the real one."""
    stood_in = types.SimpleNamespace(RawOutputStream=streams)
    was = sys.modules.get("sounddevice")
    sys.modules["sounddevice"] = stood_in  # type: ignore[assignment]
    try:
        yield
    finally:
        if was is None:
            del sys.modules["sounddevice"]
        else:
            sys.modules["sounddevice"] = was


class TestBringingACallUp:
    def test_the_handshake_is_the_route_the_prototype_proved(self, socket_path: Path) -> None:
        """Thread, offer, realtime start, SDP answer, started, audio up."""

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                adapter, audio = await riding(server, Sink())

                snapshot = await adapter.ensure_call(dial())

                assert snapshot.state is CallState.UP
                assert snapshot.call_id == THREAD
                start = server.calls_to("thread/realtime/start")[0]
                assert start["threadId"] == THREAD
                assert start["transport"] == {"type": "webrtc", "sdp": OFFER_SDP}
                assert start["realtimeStartInstructions"] == CALL_AGENT_INSTRUCTIONS
                assert audio.answers == [ANSWER_SDP]
                await adapter.aclose()

        asyncio.run(scenario())

    def test_the_dial_reaches_two_audiences_through_three_slots(self, socket_path: Path) -> None:
        """ADR 0018's mapping, pinned: `prompt`, `realtimeStartInstructions`, `initialItems`.

        The slot-swap proved which half each reaches — `realtimeStartInstructions`
        0/6 on the Voice, `prompt` 6/6, `initialItems` 5/6 and silent (#175 Q4,
        #179). This is the only place in the system that knows any of these three
        names, and each hand-over item becomes exactly one entry under
        `role: developer`.
        """

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                adapter, _ = await riding(server, Sink())

                await adapter.ensure_call(
                    dial(
                        DialReason(text="dialled because Sessions need you"),
                        SpokenRosterBrief(
                            counts="the others: 1 running",
                            rows=("build — codex:abc — running",),
                            focus="voicecoding · the dial — codex:def — finished",
                        ),
                        brief("it finished"),
                    )
                )

                start = server.calls_to("thread/realtime/start")[0]
                assert start["prompt"] == CALL_VOICE_PROSE
                assert start["realtimeStartInstructions"] == CALL_AGENT_INSTRUCTIONS
                assert [item["role"] for item in start["initialItems"]] == [
                    "developer",
                    "developer",
                    "developer",
                ]
                assert start["initialItems"][0]["text"] == "dialled because Sessions need you"
                assert start["initialItems"][1]["text"] == (
                    "focus: voicecoding · the dial — codex:def — finished\n"
                    "the others: 1 running\n"
                    "  build — codex:abc — running"
                )
                assert start["initialItems"][2]["text"].endswith("  last activity: not read")
                await adapter.aclose()

        asyncio.run(scenario())

    def test_the_three_dial_time_switches_are_this_adapters_constants(
        self, socket_path: Path
    ) -> None:
        """No caller varies them, so they are pinned here rather than on the `Dial`.

        `delegationAckFiller` off is Round 1 Q9's wordiness removed;
        `includeStartupContext` off keeps a 5,300-token scan of the user's recent
        threads out of the Voice's prompt (ADR 0018 as amended by #179);
        `codexResponsesAsItems` on with a prefix is the recorded decision and
        **buys no observability** — two live spoken calls produced no item
        carrying an agent answer, so nothing here is built on it.
        """

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                adapter, _ = await riding(server, Sink())

                await adapter.ensure_call(dial())

                start = server.calls_to("thread/realtime/start")[0]
                assert start["delegationAckFiller"] is DELEGATION_ACK_FILLER is False
                assert start["includeStartupContext"] is INCLUDE_STARTUP_CONTEXT is False
                assert start["codexResponsesAsItems"] is CODEX_RESPONSES_AS_ITEMS is True
                assert start["codexResponseItemPrefix"] == CODEX_RESPONSE_ITEM_PREFIX
                await adapter.aclose()

        asyncio.run(scenario())

    def test_no_item_reaches_the_wire_larger_than_it_was_budgeted_at(self) -> None:
        """The invariant `HANDOVER_BUDGET_BYTES` is a promise about, checked here.

        The seam counts a hand-over before this module assembles it, so the count
        has to be an upper bound on what assembly produces — otherwise a `Dial`
        the seam accepted is a request the wire refuses, which is an error and
        not a truncation. It was not one: counting the words without the labels
        around them let 8,192 budgeted bytes reach the wire as 8,242 (#194
        review). Asserted against the real assembly rather than against a
        restatement of it, so a longer label fails here rather than on a call.
        """
        for item in HANDOVER_ITEM_EXAMPLES:
            assembled = len(_item_text(item).encode("utf-8"))
            assert assembled <= item.size_in_bytes, f"{type(item).__name__} overflows its budget"

    def test_every_item_leaves_room_for_the_rounding_codex_does_on_it(self) -> None:
        """The slack `HANDOVER_BUDGET_BYTES` spends on codex rounding each item up.

        codex counts `ceil(bytes / 4)` **per item** and sums those, so a hand-over
        of exactly the budget could still be refused if the seam's count were only
        an upper bound: 128 items each rounded up would add ninety-six tokens the
        total count never sees. It cannot, because `_bytes_of` charges
        `WIRE_LINE_OVERHEAD_BYTES` for every carried string, which is far more
        than the at-most three bytes each item's rounding costs. Asserted against
        the real assembly for the same reason the test above is: a longer label
        eats this margin, and it should fail here rather than on a call.
        """
        for item in HANDOVER_ITEM_EXAMPLES:
            assembled = len(_item_text(item).encode("utf-8"))
            spare = item.size_in_bytes - assembled
            assert spare >= CODEX_BYTES_PER_TOKEN - 1, (
                f"{type(item).__name__} has {spare} bytes of slack, too few to round up in"
            )

    def test_a_user_opened_dial_carries_exactly_one_item(self, socket_path: Path) -> None:
        """#167 Q6: a call the user opened gets no hand-over, only why it exists."""

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                adapter, _ = await riding(server, Sink())

                await adapter.ensure_call(dial(DialReason(text="The user opened this call.")))

                start = server.calls_to("thread/realtime/start")[0]
                assert start["initialItems"] == [
                    {"role": "developer", "text": "The user opened this call."}
                ]
                await adapter.aclose()

        asyncio.run(scenario())

    def test_the_voice_thread_is_pinned_approval_free_in_a_full_sandbox(
        self, socket_path: Path
    ) -> None:
        """The trade recorded in legacy issue #19, asserted rather than reviewed.

        Approval-free is a decision already taken, and the sandbox is the only
        one in which the control-plane CLI's `AF_UNIX` connect succeeds. Neither
        is a configuration key, so neither may drift without this test noticing.
        """

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                adapter, _ = await riding(server, Sink())

                await adapter.ensure_call(dial())

                started = server.calls_to("thread/start")[0]
                assert started["approvalPolicy"] == APPROVAL_POLICY == "never"
                assert started["sandbox"] == SANDBOX == "danger-full-access"
                assert started["cwd"] == "/tmp"
                await adapter.aclose()

        asyncio.run(scenario())

    def test_the_realtime_model_the_backend_still_accepts_is_sent(self, socket_path: Path) -> None:
        """The model rides at the top level of the start, and it is not codex's default.

        codex serializes a `session.model` of its own on this path no matter how
        it is configured, and on 2026-08-22 the backend stopped accepting the
        value it picks. The refusal names the field — `Field \u0060session.model\u0060
        is not allowed for this Codex realtime session` — but what is refused is
        the *value*; the allowlist moved (#35, openai/codex#40140). Stating a
        model that is still on the allowlist is what brings the call up.

        This value is granted by the far side, not derived here. When it expires
        the call fails the same way again: **re-run the probe and re-derive the
        model — do not loosen this assertion.** A test that stopped checking
        which model was sent would let the next expiry look like our bug.
        """

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                adapter, _ = await riding(server, Sink())

                await adapter.ensure_call(dial())

                start = server.calls_to("thread/realtime/start")[0]
                assert start["model"] == DEFAULT_REALTIME_MODEL == "gpt-live-1-codex"
                assert "session" not in start, "the model rides at the top level"
                await adapter.aclose()

        asyncio.run(scenario())

    def test_a_stated_realtime_model_overrides_the_default(self, socket_path: Path) -> None:
        """The operator's escape hatch when the allowlist moves again, exercised."""

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                adapter, _ = await riding(
                    server, Sink(), settings=quick(realtime_model="gpt-live-2-later")
                )

                await adapter.ensure_call(dial())

                start = server.calls_to("thread/realtime/start")[0]
                assert start["model"] == "gpt-live-2-later"
                await adapter.aclose()

        asyncio.run(scenario())

    def test_a_refused_start_says_which_realtime_model_was_asked_for(
        self, socket_path: Path, caplog
    ) -> None:
        """The upstream words verbatim, plus the value this engine actually sent.

        The 2026-08-22 outage was an upstream refusal of the model *value*
        reported as a refusal of the *field*, and our own failure line never
        said which model had gone out — so the log agreed with the misleading
        reading for two days (#35). Upstream still speaks for itself; we add
        only what upstream declined to mention.
        """
        caplog.set_level("INFO", logger="gpt_voicecoding.adapters.call.realtime.adapter")

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)

                def refuse(_params: dict) -> dict:
                    raise FakeRemoteError(
                        '{"detail":"Field `session.model` is not allowed for '
                        'this Codex realtime session"}'
                    )

                server.answers("thread/realtime/start", refuse)
                adapter, _ = await riding(server, Sink())

                snapshot = await adapter.ensure_call(dial())

                assert snapshot.state is CallState.DOWN
                logged = " ".join(record.getMessage() for record in caplog.records)
                assert "Field `session.model` is not allowed" in logged
                assert DEFAULT_REALTIME_MODEL in logged
                await adapter.aclose()

        asyncio.run(scenario())

    def test_a_call_already_up_is_reported_not_reopened(self, socket_path: Path) -> None:
        """`ensure_call` is idempotent for the adapter; the invariant is Core's."""

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                sink = Sink()
                adapter, _ = await riding(server, sink)

                first = await adapter.ensure_call(dial())
                second = await adapter.ensure_call(dial())

                assert first == second
                assert len(server.calls_to("thread/start")) == 1
                assert len(sink.of(CallStarted)) == 1
                await adapter.aclose()

        asyncio.run(scenario())

    def test_a_call_that_never_connects_leaves_nothing_running(self, socket_path: Path) -> None:
        """The audio never arrives, so the thread is stopped and the state is down."""

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                sink = Sink()
                adapter, audio = await riding(server, sink, transport=FakeTransport(connects=False))

                snapshot = await adapter.ensure_call(dial())

                assert snapshot.state is CallState.DOWN
                assert audio.closed
                assert server.calls_to("thread/realtime/stop")
                assert sink.of(CallStarted) == []
                assert (await adapter.call_state()).state is CallState.DOWN
                await adapter.aclose()

        asyncio.run(scenario())

    def test_a_call_is_never_opened_on_no_instructions(self) -> None:
        """Nothing here invents house rules when the hub generated none.

        The check left this adapter with #194: a `Dial` refuses its own empty
        halves at construction (`seams/call.py`), so an argument this method
        could refuse cannot be built. One rule, one place, and an earlier one
        than the wire.
        """
        with pytest.raises(ValueError):
            Dial(voice="", agent=CALL_AGENT_INSTRUCTIONS)
        with pytest.raises(ValueError):
            Dial(voice=CALL_VOICE_PROSE, agent="   ")

    def test_hanging_up_during_the_handshake_abandons_it(self, socket_path: Path) -> None:
        """`end_call` while connecting: the attempt stops, and nothing is reported up."""

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                sink = Sink()
                audio = FakeTransport(connects=False)
                adapter, _ = await riding(server, sink, transport=audio)

                opening = asyncio.ensure_future(adapter.ensure_call(dial()))
                await asyncio.sleep(0.05)
                ended = await adapter.end_call()
                snapshot = await opening

                assert ended.state is CallState.DOWN
                assert snapshot.state is CallState.DOWN
                assert audio.closed
                # Nothing ever started, so nothing is announced as having ended.
                assert sink.of(CallStarted) == []
                assert sink.of(CallEnded) == []
                assert sink.of(CallDropped) == []
                await adapter.aclose()

        asyncio.run(scenario())


class TestHangingUpMidHandshake:
    """`end_call` during connection setup, at each step it can arrive at."""

    def test_a_hang_up_before_the_thread_exists_stops_the_handshake(
        self, socket_path: Path
    ) -> None:
        """The window that matters: `thread/start` has been sent and not answered.

        An attempt that only became visible once the thread had a name would
        leave `end_call` nothing to end here — and the handshake would carry on
        and bring up a call the user had already hung up.
        """

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                sink = Sink()
                adapter, audio = await riding(server, sink)

                slow = asyncio.Event()

                async def dawdle(_params: dict) -> dict:
                    await slow.wait()
                    return {"thread": {"id": THREAD}}

                server.answers("thread/start", dawdle)

                opening = asyncio.ensure_future(adapter.ensure_call(dial()))
                await asyncio.sleep(0.05)
                ended = await adapter.end_call()
                slow.set()
                snapshot = await opening

                assert ended.state is CallState.DOWN
                # The invariant: an `ensure_call` that was hung up never comes
                # back UP. Anything weaker and the hang-up is only advisory.
                assert snapshot.state is CallState.DOWN
                assert (await adapter.call_state()).state is CallState.DOWN
                assert audio.closed
                assert sink.of(CallStarted) == []
                # The thread `thread/start` did create is not left behind.
                assert server.calls_to("thread/realtime/stop") == [{"threadId": THREAD}]
                assert server.calls_to("thread/realtime/start") == []
                await adapter.aclose()

        asyncio.run(scenario())

    def test_a_hang_up_before_the_sdp_answer_stops_it(self, socket_path: Path) -> None:
        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                server.answers("thread/realtime/start", {})  # no SDP ever comes back
                sink = Sink()
                adapter, audio = await riding(server, sink)

                opening = asyncio.ensure_future(adapter.ensure_call(dial()))
                await asyncio.sleep(0.05)
                await adapter.end_call()
                snapshot = await opening

                assert snapshot.state is CallState.DOWN
                assert audio.closed
                assert sink.of(CallStarted) == []
                await adapter.aclose()

        asyncio.run(scenario())


class TestSpeaking:
    def test_speaking_into_a_live_call_is_delivered(self, socket_path: Path) -> None:
        """The brief goes out assembled, and every word in it is Briefing's (#194).

        What this adapter adds is the labels and the order — `newest:`, the
        decision lines under the header, `answer:` last. The five state words,
        the omission wording and the decision's own sentences all arrive already
        chosen, so nothing here can describe a Session a second way.
        """

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                adapter, _ = await riding(server, Sink())
                await adapter.ensure_call(dial())

                receipt = await adapter.speak(brief("that session stopped"), request_id=rid())

                assert receipt.outcome is Delivery.DELIVERED
                assert server.calls_to("thread/realtime/appendSpeech") == [
                    {
                        "threadId": THREAD,
                        "text": (
                            "voicecoding · the dial — codex — waiting for your decision\n"
                            "  newest: that session stopped\n"
                            "  asked: ship it?\n"
                            "  option: yes\n"
                            "  option: no\n"
                            "  answer: from here\n"
                            "  last activity: not read"
                        ),
                    }
                ]
                await adapter.aclose()

        asyncio.run(scenario())

    def test_speaking_with_no_call_up_fails_closed(self, socket_path: Path) -> None:
        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                adapter, _ = await riding(server, Sink())

                receipt = await adapter.speak(brief("anyone there"), request_id=rid())

                assert receipt.outcome is Delivery.FAILED
                assert "no call is up" in receipt.reason
                assert server.calls_to("thread/realtime/appendSpeech") == []
                await adapter.aclose()

        asyncio.run(scenario())

    def test_a_refused_speech_is_a_failure_in_codex_own_words(self, socket_path: Path) -> None:
        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                adapter, _ = await riding(server, Sink())
                await adapter.ensure_call(dial())

                def refuse(_params: dict) -> dict:
                    raise FakeRemoteError("no realtime session on that thread")

                server.answers("thread/realtime/appendSpeech", refuse)
                receipt = await adapter.speak(brief("hello"), request_id=rid())

                assert receipt.outcome is Delivery.FAILED
                assert "no realtime session on that thread" in receipt.reason
                await adapter.aclose()

        asyncio.run(scenario())

    def test_a_speech_accepted_onto_audio_that_had_gone_is_unknown(self, socket_path: Path) -> None:
        """The one grading rule this seam exists to get right.

        The app-server took the words, so calling it FAILED would make Bridge
        Core re-route a notice that may well have been heard — which is exactly
        the duplicate-call bug this adapter's contract is written against.
        """

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                adapter, audio = await riding(server, Sink())
                await adapter.ensure_call(dial())

                def go_quiet_then_accept(_params: dict) -> dict:
                    audio.go_quiet()
                    return {}

                server.answers("thread/realtime/appendSpeech", go_quiet_then_accept)
                receipt = await adapter.speak(brief("you are needed"), request_id=rid())

                assert receipt.outcome is Delivery.UNKNOWN
                assert "already gone" in receipt.reason
                await adapter.aclose()

        asyncio.run(scenario())

    def test_an_app_server_that_dies_mid_speech_is_unknown_not_failed(
        self, socket_path: Path
    ) -> None:
        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                adapter, _ = await riding(server, Sink())
                await adapter.ensure_call(dial())

                async def die(_params: dict) -> dict:
                    await server.drop_everyone()
                    return {}

                server.answers("thread/realtime/appendSpeech", die)
                receipt = await adapter.speak(brief("you are needed"), request_id=rid())

                assert receipt.outcome is Delivery.UNKNOWN
                await adapter.aclose()

        asyncio.run(scenario())


class TestHowACallStops:
    def test_ending_a_call_is_announced_and_idempotent(self, socket_path: Path) -> None:
        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                sink = Sink()
                adapter, audio = await riding(server, sink)
                await adapter.ensure_call(dial())

                first = await adapter.end_call()
                second = await adapter.end_call()

                assert first.state is second.state is CallState.DOWN
                assert audio.closed
                assert [event.call_id for event in sink.of(CallEnded)] == [THREAD]
                assert sink.of(CallDropped) == []
                await adapter.aclose()

        asyncio.run(scenario())

    def test_a_call_already_gone_still_ends_without_raising(self, socket_path: Path) -> None:
        """Bridge Core's ledger needs a clean answer, not an exception."""

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                sink = Sink()
                adapter, _ = await riding(server, sink)
                await adapter.ensure_call(dial())

                def refuse(_params: dict) -> dict:
                    raise FakeRemoteError("no realtime session on that thread")

                server.answers("thread/realtime/stop", refuse)
                snapshot = await adapter.end_call()

                assert snapshot.state is CallState.DOWN
                assert "already gone" in sink.of(CallEnded)[0].detail
                await adapter.aclose()

        asyncio.run(scenario())

    def test_the_realtime_session_closing_is_a_drop(self, socket_path: Path) -> None:
        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                sink = Sink()
                adapter, _ = await riding(server, sink)
                await adapter.ensure_call(dial())

                await server.notify_all(
                    "thread/realtime/closed",
                    {"threadId": THREAD, "reason": "the backend hung up"},
                )
                await asyncio.sleep(0.05)

                dropped = sink.of(CallDropped)
                assert [event.call_id for event in dropped] == [THREAD]
                assert "the backend hung up" in dropped[0].detail
                assert (await adapter.call_state()).state is CallState.DOWN
                await adapter.aclose()

        asyncio.run(scenario())

    def test_the_audio_going_away_is_a_drop_reported_once(self, socket_path: Path) -> None:
        """A call dropped mid-`speak` is news, and it is news exactly once."""

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                sink = Sink()
                adapter, audio = await riding(server, sink)
                await adapter.ensure_call(dial())

                audio.lose("the peer connection failed")
                await asyncio.sleep(0.05)
                receipt = await adapter.speak(brief("you are needed"), request_id=rid())

                assert len(sink.of(CallDropped)) == 1
                assert receipt.outcome is Delivery.FAILED
                assert "no call is up" in receipt.reason
                await adapter.aclose()

        asyncio.run(scenario())

    def test_a_call_whose_audio_went_quiet_is_not_reported_as_up(self, socket_path: Path) -> None:
        """`call_state` reads the connection, not this adapter's own bookkeeping."""

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                adapter, audio = await riding(server, Sink())
                await adapter.ensure_call(dial())

                audio.go_quiet()

                assert (await adapter.call_state()).state is CallState.CONNECTING
                await adapter.aclose()

        asyncio.run(scenario())


class TestWhatTheCallRaisesUpward:
    def test_the_users_speech_goes_up_as_a_transcript(self, socket_path: Path) -> None:
        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                sink = Sink()
                adapter, _ = await riding(server, sink)
                await adapter.ensure_call(dial())

                await server.notify_all(
                    "thread/realtime/transcript/done",
                    {"threadId": THREAD, "role": "user", "text": "what is codex doing"},
                )
                await asyncio.sleep(0.05)

                assert [event.text for event in sink.of(UserSpeech)] == ["what is codex doing"]
                await adapter.aclose()

        asyncio.run(scenario())

    def test_a_hand_off_is_written_down_and_raises_no_event_of_its_own(
        self, socket_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """ADR 0018: no `HandoffRequested` event. The closed event set stays closed.

        A `handoff_request` is the one observable proof the acting half was
        reached — the model's own claim to have acted is trusted by nothing
        (8/8 false hang-up claims, #179). So it is logged, and the engine still
        acts only on the Call Agent's own `bridgectl` run. What it *does* raise
        is the user's own sentence, which the item carries and which the seam
        has always published; there is still no event for the hand-off itself.
        """

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                sink = Sink()
                adapter, _ = await riding(server, sink)
                await adapter.ensure_call(dial())

                with caplog.at_level(logging.INFO):
                    await server.notify_all(
                        "thread/realtime/itemAdded",
                        {
                            "threadId": THREAD,
                            "item": {
                                "type": "handoff_request",
                                "handoff_id": "item_EJELbGbAr6yo",
                                "input_transcript": "hang up",
                            },
                        },
                    )
                    await asyncio.sleep(0.05)

                assert "item_EJELbGbAr6yo" in caplog.text
                assert "hang up" in caplog.text
                assert sink.events == [CallStarted(call_id=THREAD), UserSpeech(text="hang up")]
                await adapter.aclose()

        asyncio.run(scenario())

    def test_the_sentence_a_hand_off_routed_is_what_the_user_said(self, socket_path: Path) -> None:
        """The carrier that arrives in time, on the call this engine actually makes.

        Measured on this machine: with `delegationAckFiller` off, a request that
        routed produced ten user deltas, a `handoff_request`, `bridgectl live`
        and a closed call in eleven seconds, and **no `transcript/done` at any
        point**. Waiting for `done` loses the user's words outright — and with
        them the Silence Ceiling's only reason to hold the call open.
        """

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                sink = Sink()
                adapter, _ = await riding(server, sink)
                await adapter.ensure_call(dial())

                for delta in ("那个你", "把", "电话挂", "了吧"):
                    await server.notify_all(
                        "thread/realtime/transcript/delta",
                        {"threadId": THREAD, "role": "user", "delta": delta},
                    )
                await server.notify_all(
                    "thread/realtime/itemAdded",
                    {
                        "threadId": THREAD,
                        "item": {
                            "type": "handoff_request",
                            "handoff_id": "item_1",
                            "input_transcript": "那个你把电话挂了吧",
                        },
                    },
                )
                await asyncio.sleep(0.05)

                assert sink.of(UserSpeech) == [UserSpeech(text="那个你把电话挂了吧")]
                await adapter.aclose()

        asyncio.run(scenario())

    def test_a_done_that_follows_the_same_utterance_does_not_say_it_twice(
        self, socket_path: Path
    ) -> None:
        """One utterance, one event, whichever carrier got there first.

        The two carriers spell the same audio differently — run
        `20260902T093755Z` had two lanes write `结束通话` and `结束通 话` from one
        four-second recording — so they are compared with the spaces taken out.
        A notice heard twice is worse than one heard once, and this seam's whole
        grading discipline exists to stop it.
        """

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                sink = Sink()
                adapter, _ = await riding(server, sink)
                await adapter.ensure_call(dial())

                await server.notify_all(
                    "thread/realtime/itemAdded",
                    {
                        "threadId": THREAD,
                        "item": {
                            "type": "handoff_request",
                            "handoff_id": "item_1",
                            "input_transcript": "我想让你结束通话",
                        },
                    },
                )
                await server.notify_all(
                    "thread/realtime/transcript/done",
                    {"threadId": THREAD, "role": "user", "text": "我想让你结束通 话"},
                )
                await asyncio.sleep(0.05)

                assert sink.of(UserSpeech) == [UserSpeech(text="我想让你结束通话")]
                await adapter.aclose()

        asyncio.run(scenario())

    def test_what_the_deltas_spelled_goes_up_when_the_call_ends_on_them(
        self, socket_path: Path
    ) -> None:
        """Nothing claimed the utterance, and the call is over: raise it anyway.

        The third carrier, and the last moment the words are this side's to
        report. Without it a call the Call Agent ends four seconds after routing
        takes the user's sentence with it.
        """

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                sink = Sink()
                adapter, _ = await riding(server, sink)
                await adapter.ensure_call(dial())

                for delta in ("现在有哪些", "需要我的", "事情"):
                    await server.notify_all(
                        "thread/realtime/transcript/delta",
                        {"threadId": THREAD, "role": "user", "delta": delta},
                    )
                await asyncio.sleep(0.05)
                assert sink.of(UserSpeech) == []

                await adapter.end_call()

                assert sink.of(UserSpeech) == [UserSpeech(text="现在有哪些需要我的事情")]
                await adapter.aclose()

        asyncio.run(scenario())

    def test_the_voices_own_deltas_are_never_the_users_words(self, socket_path: Path) -> None:
        """This system does not read its own speech back to itself."""

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                sink = Sink()
                adapter, _ = await riding(server, sink)
                await adapter.ensure_call(dial())

                await server.notify_all(
                    "thread/realtime/transcript/delta",
                    {"threadId": THREAD, "role": "assistant", "delta": "that session"},
                )
                await asyncio.sleep(0.05)
                await adapter.end_call()

                assert sink.of(UserSpeech) == []
                await adapter.aclose()

        asyncio.run(scenario())

    def test_an_item_that_is_not_a_hand_off_is_not_written_down(
        self, socket_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The arm reads one item type. Everything else on that method is noise."""

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                adapter, _ = await riding(server, Sink())
                await adapter.ensure_call(dial())

                with caplog.at_level(logging.INFO):
                    await server.notify_all(
                        "thread/realtime/itemAdded",
                        {"threadId": THREAD, "item": {"type": "input_audio_buffer.speech_started"}},
                    )
                    await asyncio.sleep(0.05)

                assert "handed work to the Call Agent" not in caplog.text
                await adapter.aclose()

        asyncio.run(scenario())

    def test_this_systems_own_voice_is_not_raised_as_the_users(self, socket_path: Path) -> None:
        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                sink = Sink()
                adapter, _ = await riding(server, sink)
                await adapter.ensure_call(dial())

                await server.notify_all(
                    "thread/realtime/transcript/done",
                    {"threadId": THREAD, "role": "assistant", "text": "that session stopped"},
                )
                await asyncio.sleep(0.05)

                assert sink.of(UserSpeech) == []
                await adapter.aclose()

        asyncio.run(scenario())

    def test_the_voices_own_speech_goes_up_as_a_span_not_a_transcript(
        self, socket_path: Path
    ) -> None:
        """One edge each way, from the only two unconditional assistant signals (#184).

        `transcript/delta` and `transcript/done` are what v3 reaches
        (`docs/research/2026-09-01-assistant-speaking-signal.md` §1d), and both
        come from the app-server's unconditional path — unlike the `item/*`
        family, whose existence depends on the operator's history mode.
        """

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                sink = Sink()
                adapter, _ = await riding(server, sink)
                await adapter.ensure_call(dial())

                for delta in ("that ", "session ", "stopped"):
                    await server.notify_all(
                        "thread/realtime/transcript/delta",
                        {"threadId": THREAD, "role": "assistant", "delta": delta},
                    )
                await server.notify_all(
                    "thread/realtime/transcript/done",
                    {"threadId": THREAD, "role": "assistant", "text": "that session stopped"},
                )
                await asyncio.sleep(0.05)

                assert [event.speaking for event in sink.of(VoiceSpeech)] == [True, False]
                await adapter.aclose()

        asyncio.run(scenario())

    def test_a_second_utterance_raises_its_own_start(self, socket_path: Path) -> None:
        """`done` releases the latch, so the next answer is a span of its own."""

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                sink = Sink()
                adapter, _ = await riding(server, sink)
                await adapter.ensure_call(dial())

                for _ in range(2):
                    await server.notify_all(
                        "thread/realtime/transcript/delta",
                        {"threadId": THREAD, "role": "assistant", "delta": "more"},
                    )
                    await server.notify_all(
                        "thread/realtime/transcript/done",
                        {"threadId": THREAD, "role": "assistant", "text": "more"},
                    )
                    # Two *utterances*, which means the first one finished being
                    # heard before the second began. A delta arriving while the
                    # previous answer is still playing is one continuous stretch
                    # of speech, and is proved separately below.
                    await asyncio.sleep(0.05)

                assert [event.speaking for event in sink.of(VoiceSpeech)] == [
                    True,
                    False,
                    True,
                    False,
                ]
                await adapter.aclose()

        asyncio.run(scenario())

    def test_the_stop_edge_waits_for_the_audio_to_finish_playing(self, socket_path: Path) -> None:
        """`speaking=False` means finished *playing*, not finished generating (#195).

        The lag between the two is the jitter prefetch, this transport's own
        playback buffer and the device, and none of it is visible above the Call
        seam — so publishing the generating edge made every gap-waiter add a
        settle window computed from numbers it could not see (#184's shape).
        """

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                sink = Sink()
                audio = FakeTransport()
                audio.playback_drained_now = False
                adapter, _ = await riding(server, sink, transport=audio)
                await adapter.ensure_call(dial())

                await server.notify_all(
                    "thread/realtime/transcript/delta",
                    {"threadId": THREAD, "role": "assistant", "delta": "that "},
                )
                await server.notify_all(
                    "thread/realtime/transcript/done",
                    {"threadId": THREAD, "role": "assistant", "text": "that session stopped"},
                )
                await asyncio.sleep(0.05)

                # Generated, not yet heard: the span is still open, which is what
                # holds the Silence Ceiling on a call somebody is listening to.
                assert [event.speaking for event in sink.of(VoiceSpeech)] == [True]

                audio.playback_drained_now = True
                await asyncio.sleep(0.05)

                assert [event.speaking for event in sink.of(VoiceSpeech)] == [True, False]
                assert audio.drain_waits == [quick().voice_playout_wait_seconds]
                await adapter.aclose()

        asyncio.run(scenario())

    def test_a_delta_while_the_answer_is_still_playing_invents_no_gap(
        self, socket_path: Path
    ) -> None:
        """Generating again over its own playout is one stretch of speech, not two (#195).

        The seam publishes a span, and a gap in it is what the Silence Ceiling
        and the mid-call gap-waiter act on. A stop-then-start published here
        would be a gap the user never heard.
        """

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                sink = Sink()
                audio = FakeTransport()
                audio.playback_drained_now = False
                adapter, _ = await riding(server, sink, transport=audio)
                await adapter.ensure_call(dial())

                await server.notify_all(
                    "thread/realtime/transcript/delta",
                    {"threadId": THREAD, "role": "assistant", "delta": "one "},
                )
                await server.notify_all(
                    "thread/realtime/transcript/done",
                    {"threadId": THREAD, "role": "assistant", "text": "one"},
                )
                await asyncio.sleep(0.05)
                await server.notify_all(
                    "thread/realtime/transcript/delta",
                    {"threadId": THREAD, "role": "assistant", "delta": "two "},
                )
                await asyncio.sleep(0.05)

                assert [event.speaking for event in sink.of(VoiceSpeech)] == [True]

                await server.notify_all(
                    "thread/realtime/transcript/done",
                    {"threadId": THREAD, "role": "assistant", "text": "two"},
                )
                audio.playback_drained_now = True
                await asyncio.sleep(0.05)

                assert [event.speaking for event in sink.of(VoiceSpeech)] == [True, False]
                await adapter.aclose()

        asyncio.run(scenario())

    def test_the_users_own_speech_is_a_span_as_well_as_a_transcript(
        self, socket_path: Path
    ) -> None:
        """The user's counterpart of `VoiceSpeech`, raised from the deltas (#195).

        Until this event the user's half reached the ceiling only as the finished
        `UserSpeech(text)`, which since #194 often lands at hand-off or teardown
        — so a user who talked for a whole ceiling without the Voice answering
        was judged silent. The transcript still travels; it is what the engine
        writes down, and a span carries no words.
        """

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                sink = Sink()
                adapter, _ = await riding(server, sink)
                await adapter.ensure_call(dial())

                await server.notify_all(
                    "thread/realtime/transcript/delta",
                    {"threadId": THREAD, "role": "user", "delta": "what is"},
                )
                await asyncio.sleep(0.05)

                assert [event.speaking for event in sink.of(UserSpeaking)] == [True]

                await server.notify_all(
                    "thread/realtime/transcript/done",
                    {"threadId": THREAD, "role": "user", "text": "what is codex doing"},
                )
                await asyncio.sleep(0.05)

                assert [event.speaking for event in sink.of(UserSpeaking)] == [True, False]
                assert [event.text for event in sink.of(UserSpeech)] == ["what is codex doing"]
                await adapter.aclose()

        asyncio.run(scenario())

    def test_the_voice_answering_closes_the_users_span(self, socket_path: Path) -> None:
        """One of the four ends, and the one that needs no `done`: they are not talked over."""

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                sink = Sink()
                adapter, _ = await riding(server, sink)
                await adapter.ensure_call(dial())

                await server.notify_all(
                    "thread/realtime/transcript/delta",
                    {"threadId": THREAD, "role": "user", "delta": "what is codex doing"},
                )
                await server.notify_all(
                    "thread/realtime/transcript/delta",
                    {"threadId": THREAD, "role": "assistant", "delta": "it is "},
                )
                await asyncio.sleep(0.05)

                assert [event.speaking for event in sink.of(UserSpeaking)] == [True, False]
                assert [event.speaking for event in sink.of(VoiceSpeech)] == [True]
                await adapter.aclose()

        asyncio.run(scenario())

    def test_a_hand_off_carrying_the_utterance_closes_the_users_span(
        self, socket_path: Path
    ) -> None:
        """The carrier that arrives when no `done` ever does (#179, measured)."""

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                sink = Sink()
                adapter, _ = await riding(server, sink)
                await adapter.ensure_call(dial())

                await server.notify_all(
                    "thread/realtime/transcript/delta",
                    {"threadId": THREAD, "role": "user", "delta": "end the call"},
                )
                await server.notify_all(
                    "thread/realtime/itemAdded",
                    {
                        "threadId": THREAD,
                        "item": {
                            "type": "handoff_request",
                            "handoff_id": "h1",
                            "input_transcript": "end the call",
                        },
                    },
                )
                await asyncio.sleep(0.05)

                assert [event.speaking for event in sink.of(UserSpeaking)] == [True, False]
                await adapter.aclose()

        asyncio.run(scenario())

    def test_the_users_own_transcript_is_never_the_voice_speaking(self, socket_path: Path) -> None:
        """The role is the wire's word and this adapter is the only translator of it."""

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                sink = Sink()
                adapter, _ = await riding(server, sink)
                await adapter.ensure_call(dial())

                await server.notify_all(
                    "thread/realtime/transcript/delta",
                    {"threadId": THREAD, "role": "user", "delta": "what is"},
                )
                await server.notify_all(
                    "thread/realtime/transcript/done",
                    {"threadId": THREAD, "role": "user", "text": "what is codex doing"},
                )
                await asyncio.sleep(0.05)

                assert sink.of(VoiceSpeech) == []
                assert [event.text for event in sink.of(UserSpeech)] == ["what is codex doing"]
                await adapter.aclose()

        asyncio.run(scenario())


class TestTheDelegatedTurn:
    def test_the_callers_model_and_instructions_reach_the_thread(self, socket_path: Path) -> None:
        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                delegated_script(server, model="claude-sonnet-5", says="the diff is small")
                adapter, _ = await riding(server, Sink())

                reply = await adapter.delegate(
                    "summarise the diff",
                    model="claude-sonnet-5",
                    instructions=DELEGATED_RULES,
                    request_id=rid(),
                )

                started = server.calls_to("thread/start")[0]
                assert started["model"] == "claude-sonnet-5"
                assert started["developerInstructions"] == DELEGATED_RULES
                assert started["approvalPolicy"] == APPROVAL_POLICY
                assert started["sandbox"] == SANDBOX
                assert reply.text == "the diff is small"
                assert reply.model == "claude-sonnet-5"
                await adapter.aclose()

        asyncio.run(scenario())

    def test_the_model_reported_is_the_one_the_server_says_it_ran(self, socket_path: Path) -> None:
        """Echoing back the caller's own argument would tell it nothing."""

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                delegated_script(server, model="gpt-5-codex")
                adapter, _ = await riding(server, Sink())

                reply = await adapter.delegate(
                    "summarise the diff",
                    model="an-alias",
                    instructions=DELEGATED_RULES,
                    request_id=rid(),
                )

                assert reply.model == "gpt-5-codex"
                await adapter.aclose()

        asyncio.run(scenario())

    def test_the_thread_does_not_outlive_the_turn(self, socket_path: Path) -> None:
        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                delegated_script(server, thread_id="delegated-1")
                adapter, _ = await riding(server, Sink())

                await adapter.delegate(
                    "summarise the diff",
                    model="gpt-5",
                    instructions=DELEGATED_RULES,
                    request_id=rid(),
                )

                assert server.calls_to("thread/unsubscribe") == [{"threadId": "delegated-1"}]
                # It finished by itself, so there is nothing to interrupt —
                # whichever order codex answered the request and announced the
                # completion in.
                assert server.calls_to("turn/interrupt") == []
                await adapter.aclose()

        asyncio.run(scenario())

    def test_a_turn_that_completed_before_its_own_response_is_not_interrupted(
        self, socket_path: Path
    ) -> None:
        """`turn/completed` can land before the `turn/start` reply it belongs to.

        The notification arrives on the reader task while this side is still
        awaiting the response, so anything that recorded "finished" by clearing
        the turn id would have it written straight back — and a turn that had
        already completed would be interrupted on the way out.
        """

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                delegated_script(server, thread_id="delegated-1")

                async def finish_before_answering(_params: dict) -> dict:
                    await server.notify_all(
                        "item/completed",
                        {
                            "threadId": "delegated-1",
                            "turnId": "turn-1",
                            "item": {"type": "agentMessage", "id": "i-1", "text": "done"},
                        },
                    )
                    await server.notify_all(
                        "turn/completed",
                        {
                            "threadId": "delegated-1",
                            "turn": {"id": "turn-1", "status": "completed"},
                        },
                    )
                    await asyncio.sleep(0.05)
                    return {"turn": {"id": "turn-1"}}

                server.answers("turn/start", finish_before_answering)
                adapter, _ = await riding(server, Sink())

                reply = await adapter.delegate(
                    "summarise the diff",
                    model="gpt-5",
                    instructions=DELEGATED_RULES,
                    request_id=rid(),
                )

                assert reply.text == "done"
                assert server.calls_to("turn/interrupt") == []
                await adapter.aclose()

        asyncio.run(scenario())

    def test_a_turn_that_never_answers_is_a_classified_failure_and_leaks_nothing(
        self, socket_path: Path
    ) -> None:
        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                delegated_script(server, thread_id="delegated-1")
                server.answers("turn/start", {"turn": {"id": "turn-1"}})  # never completes
                adapter, _ = await riding(server, Sink())

                with pytest.raises(DelegatedTurnError) as refusal:
                    await adapter.delegate(
                        "summarise the diff",
                        model="gpt-5",
                        instructions=DELEGATED_RULES,
                        request_id=rid(),
                    )

                assert "did not finish" in str(refusal.value)
                # Unsubscribing only stops this engine hearing about the turn. A
                # bridge-owned thread runs approval-free in a full sandbox, so a
                # turn left running would keep acting on the user's machine and
                # spending their money with nothing watching it.
                assert server.calls_to("turn/interrupt") == [
                    {"threadId": "delegated-1", "turnId": "turn-1"}
                ]
                assert server.calls_to("thread/unsubscribe") == [{"threadId": "delegated-1"}]
                await adapter.aclose()

        asyncio.run(scenario())

    def test_a_failed_turn_says_what_codex_said(self, socket_path: Path) -> None:
        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                delegated_script(server, thread_id="delegated-1")

                def fail_the_turn(_params: dict) -> dict:
                    async def finish() -> None:
                        await server.notify_all(
                            "turn/completed",
                            {
                                "threadId": "delegated-1",
                                "turn": {
                                    "id": "turn-1",
                                    "status": "failed",
                                    "error": {"message": "the model is over its limit"},
                                },
                            },
                        )

                    asyncio.ensure_future(finish())
                    return {"turn": {"id": "turn-1"}}

                server.answers("turn/start", fail_the_turn)
                adapter, _ = await riding(server, Sink())

                with pytest.raises(DelegatedTurnError) as refusal:
                    await adapter.delegate(
                        "summarise the diff",
                        model="gpt-5",
                        instructions=DELEGATED_RULES,
                        request_id=rid(),
                    )

                assert "over its limit" in str(refusal.value)
                await adapter.aclose()

        asyncio.run(scenario())

    def test_a_delegated_turn_needs_no_call_to_be_up(self, socket_path: Path) -> None:
        """It arrives from the Companion Channel too, where there is no call at all."""

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                delegated_script(server, says="nothing is running")
                adapter, _ = await riding(server, Sink())

                assert (await adapter.call_state()).state is CallState.DOWN
                reply = await adapter.delegate(
                    "what is running",
                    model="gpt-5",
                    instructions=DELEGATED_RULES,
                    request_id=rid(),
                )

                assert reply.text == "nothing is running"
                await adapter.aclose()

        asyncio.run(scenario())


class TestTheTransportItIsLent:
    def test_it_refuses_to_open_without_an_app_server(self) -> None:
        adapter = RealtimeCallAdapter(transport_factory=FakeTransport)

        with pytest.raises(AppServerError) as refusal:
            asyncio.run(adapter.connect())

        assert "never handed" in str(refusal.value)

    def test_it_takes_an_app_server_once(self) -> None:
        adapter = RealtimeCallAdapter(transport_factory=FakeTransport)
        adapter.use_app_server(SharedAppServer(connection=None))  # type: ignore[arg-type]

        with pytest.raises(AppServerError):
            adapter.use_app_server(SharedAppServer(connection=None))  # type: ignore[arg-type]

    def test_verify_reports_what_is_loaded_and_whether_the_far_side_answers(
        self, socket_path: Path
    ) -> None:
        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                server.answers("thread/loaded/list", {"data": []})
                adapter, _ = await riding(server, Sink())

                result = await adapter.verify()

                assert result.outcome is VerifyOutcome.PASS
                assert result.loaded.endswith(":RealtimeCallAdapter")
                await adapter.aclose()

        asyncio.run(scenario())

    def test_verify_fails_when_there_is_no_app_server_to_ask(self) -> None:
        adapter = RealtimeCallAdapter(transport_factory=FakeTransport)

        result = asyncio.run(adapter.verify())

        assert result.outcome is VerifyOutcome.FAIL
        assert result.detail


class TestWhatThisSpokeMayBeTold:
    def test_an_unknown_setting_refuses_to_start(self) -> None:
        with pytest.raises(SettingsError) as refusal:
            RealtimeCallSettings.of({"conect_timeout_seconds": 1.0})

        assert "conect_timeout_seconds" in str(refusal.value)

    def test_the_two_speaking_span_timings_are_settings(self) -> None:
        """Neither is a measurement, so neither is pinned (#195).

        `user_quiet_seconds` is the one end of the user's span this adapter
        invents, and whether user deltas arrive during or after the speech is
        what #212 settles — a value nobody can change without a release is a
        value that measurement cannot correct. `voice_playout_wait_seconds`
        bounds a wait on somebody else's audio path, which is exactly the kind
        of number that differs by machine.
        """
        dialled = RealtimeCallSettings.of(
            {"user_quiet_seconds": 2.5, "voice_playout_wait_seconds": 45.0}
        )

        assert dialled.user_quiet_seconds == 2.5
        assert dialled.voice_playout_wait_seconds == 45.0
        for name in ("user_quiet_seconds", "voice_playout_wait_seconds"):
            with pytest.raises(SettingsError):
                RealtimeCallSettings.of({name: 0})

    def test_neither_the_approval_policy_nor_the_sandbox_is_a_setting(self) -> None:
        """Both are pinned. A key for either would invite half the trade to be broken."""
        for name in ("approval_policy", "sandbox", "approvalPolicy"):
            with pytest.raises(SettingsError):
                RealtimeCallSettings.of({name: "on-request"})

    def test_the_realtime_model_is_a_setting_because_the_peer_can_revoke_it(self) -> None:
        """Unlike the approval policy and the sandbox, this value is not ours to pin.

        Those two are mechanism identity: this repository verifies them and they
        move only when our code moves. The realtime model's validity is granted
        and withdrawn by the backend — it moved once inside five days with no
        client change (#35) — so it is an environment fact with a default, and
        the operator gets a one-line escape hatch instead of a wait for our next
        release.
        """
        assert RealtimeCallSettings().realtime_model == DEFAULT_REALTIME_MODEL
        assert RealtimeCallSettings.of({"realtime_model": "gpt-live-2-later"}).realtime_model == (
            "gpt-live-2-later"
        )

    def test_an_empty_realtime_model_refuses_to_start(self) -> None:
        for value in ("", "   ", 7):
            with pytest.raises(SettingsError):
                RealtimeCallSettings.of({"realtime_model": value})

    def test_the_workspace_defaults_to_a_directory_that_always_exists(self) -> None:
        assert RealtimeCallSettings().cwd == Path.home()

    def test_a_stated_workspace_is_used(self) -> None:
        assert RealtimeCallSettings.of({"workspace": "/tmp/somewhere"}).cwd == Path(
            "/tmp/somewhere"
        )

    def test_the_factory_builds_the_adapter_from_the_table(self) -> None:
        adapter = realtime_call(
            settings={"connect_timeout_seconds": 5.0}, transport_factory=FakeTransport
        )

        assert isinstance(adapter, RealtimeCallAdapter)


class TestTheCuesItPlays:
    """`play_cue`, and why the player is the adapter's rather than a call's (#186).

    Nothing here makes a sound. The stream lives in the audio module behind
    `CueOutput` for exactly that reason: what is worth grading is which moment
    got which sound, that it went out on the configured device, that it can go
    out with no call left, and that a device which will not open takes nothing
    down with it.
    """

    @staticmethod
    def played(player: FakeCueOutput, count: int = 1, *, within: float = 5.0) -> None:
        """Wait for the cue worker to have reached `count` cues.

        `play_cue` deliberately does not wait, so every test that reads what went
        out has to. Counted rather than "anything yet", because the worker plays
        in order and a test that looked once would read the cue before the one
        it asked about.
        """
        deadline = time.monotonic() + within
        while time.monotonic() < deadline:
            if len(player.seen_playing) >= count:
                return
            time.sleep(0.005)
        raise AssertionError(f"only {len(player.seen_playing)} of {count} cues were played")

    def test_each_moment_is_played_as_its_own_sound(self, socket_path: Path) -> None:
        async def scenario() -> FakeCueOutput:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                player = FakeCueOutput()
                adapter, _ = await riding(server, Sink(), cue_player=player)
                for number, cue in enumerate(Cue, start=1):
                    await adapter.play_cue(cue)
                    self.played(player, number)
                    assert player.spans[-1].cue is cue
                return player

        player = asyncio.run(scenario())
        assert [span.cue for span in player.spans] == list(Cue)
        assert player.buffers == [cues.render(cue) for cue in Cue]

    def test_a_cue_goes_to_the_output_device_the_call_was_configured_with(self) -> None:
        """One `output_device` setting, and a cue honours the one the call does.

        Built the way the composition root builds it — no player handed in — so
        this also asserts that an adapter which was told nothing about cues
        still has one, on the machine's own default output.
        """
        stated = RealtimeCallAdapter(
            settings=quick(output_device=4), transport_factory=FakeTransport
        )
        assert stated.cue_output.device == 4

        default = RealtimeCallAdapter(settings=quick(), transport_factory=FakeTransport)
        assert default.cue_output.device is None

    def test_the_ended_cue_plays_after_the_calls_own_audio_has_closed(
        self, socket_path: Path
    ) -> None:
        """The whole reason the player is per adapter and not per call.

        A cue mixed into the call's own playback buffer could not mark the end
        of a call: by the time there is an end to mark, that stream is shut.
        """

        async def scenario() -> tuple[FakeTransport, FakeCueOutput]:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                player = FakeCueOutput()
                adapter, audio = await riding(server, Sink(), cue_player=player)
                await adapter.ensure_call(dial())
                await adapter.end_call()
                assert audio.closed
                await adapter.play_cue(Cue.ENDED)
                self.played(player)
                return audio, player

        audio, player = asyncio.run(scenario())
        assert audio.closed
        assert [span.cue for span in player.spans] == [Cue.ENDED]

    def test_the_adapter_writes_down_the_device_and_the_span_it_played(
        self, socket_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The engine logs no line for `CallStarted` or `CallEnded`, so this is
        the only witness the acceptance harness has that a cue went out."""

        async def scenario() -> FakeCueOutput:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                player = FakeCueOutput(device=9)
                adapter, _ = await riding(server, Sink(), cue_player=player)
                with caplog.at_level(logging.INFO):
                    await adapter.play_cue(Cue.CONNECTED)
                    self.played(player)
                return player

        asyncio.run(scenario())
        written = [record.getMessage() for record in caplog.records]
        line = next(said for said in written if cues.cue_phrase(Cue.CONNECTED) in said)
        assert "output device 9" in line
        assert "14400 frames" in line
        assert "0.300s" in line

    def test_a_device_that_will_not_open_takes_nothing_down_with_it(
        self, socket_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """No output device, no audio library, somebody unplugged the speakers.

        A cue is feedback about something that already happened; there is no
        recovery to attempt and nothing above the seam that could attempt one.
        """

        async def scenario() -> FakeCueOutput:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                player = FakeCueOutput(fails="no such output device")
                adapter, _ = await riding(server, Sink(), cue_player=player)
                with caplog.at_level(logging.INFO):
                    await adapter.play_cue(Cue.ENDED)
                    self.played(player)
                assert (await adapter.call_state()).state is CallState.DOWN
                return player

        asyncio.run(scenario())
        written = [record.getMessage() for record in caplog.records]
        assert any("no such output device" in said for said in written)
        assert not any(cues.cue_phrase(Cue.ENDED) in said for said in written)

    def test_a_playback_that_finishes_first_does_not_release_another_ones_span(self) -> None:
        """`playing` answers for every playback in flight, not for the last setter.

        `play_now` is public, so #145 can put a second playback beside the cue
        worker's. If the *later* one finishes first, a single slot cleared by
        whoever finishes would announce silence while the earlier cue is still
        writing — and a capture gate reading that opens the microphone into a
        live tone. The device is stood in for; the bookkeeping under test is
        this class's own.
        """
        player = webrtc.CuePlayer(device=3)
        writing, release = threading.Event(), threading.Event()
        streams = _Streams(blocks_on=(writing, release))

        with _sounddevice(streams):
            first = threading.Thread(
                target=lambda: player.play(b"\x00\x00", span="the first"), daemon=True
            )
            first.start()
            assert writing.wait(2.0), "the first playback never reached the device"

            # Started later and finished first, which is the order a single slot
            # gets wrong.
            second = threading.Thread(
                target=lambda: player.play(b"\x00\x00", span="the second"), daemon=True
            )
            second.start()
            second.join(2.0)

            assert player.playing == "the first"
            release.set()
            first.join(2.0)

        assert player.playing is None

    def test_a_cue_opens_the_stream_on_the_speakers_own_parameters(self) -> None:
        """One shape for the whole audio path, so a cue needs no second opinion.

        The PCM is synthesised at these numbers (`cues.py`), so a stream opened
        at any other would play the cue at the wrong pitch and speed.
        """
        player = webrtc.CuePlayer(device=6)
        streams = _Streams(blocks_on=None)

        with _sounddevice(streams):
            player.play(cues.render(Cue.CONNECTED), span="a cue")

        assert streams.opened == [
            {
                "samplerate": webrtc.SAMPLE_RATE,
                "channels": webrtc.CHANNELS,
                "dtype": webrtc.SAMPLE_FORMAT,
                "blocksize": webrtc.FRAME_SAMPLES,
                "device": 6,
            }
        ]

    def test_the_span_is_readable_while_the_cue_is_going_out(self, socket_path: Path) -> None:
        """What #145 inherits: the microphone is open through a cue, and the
        mid-call one is loud enough to carry over speech — so loud enough to be
        heard back in. A gate needs to know a cue is playing *now*."""

        async def scenario() -> FakeCueOutput:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                player = FakeCueOutput(device=2)
                adapter, _ = await riding(server, Sink(), cue_player=player)
                await adapter.play_cue(Cue.EVENT)
                self.played(player)
                return player

        player = asyncio.run(scenario())
        held = player.seen_playing[-1]
        assert held.cue is Cue.EVENT
        assert held.device == 2
        assert held.seconds == pytest.approx(0.16)
        # And nothing is held once it has finished: a gate that stayed shut
        # would be worse than no gate.
        assert player.playing is None

    def test_asking_for_a_cue_does_not_wait_for_it(self, socket_path: Path) -> None:
        """A cue costs 320-620 ms of wall time on the real path (#174), and the
        arm that asks for one is Bridge Core's dispatch. It is not held."""
        writing = threading.Event()
        release = threading.Event()

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                player = FakeCueOutput()
                player.while_playing = lambda: (writing.set(), release.wait(2.0))
                adapter, _ = await riding(server, Sink(), cue_player=player)

                await adapter.play_cue(Cue.CONNECTED)
                # Returned while the write is still in the device. If `play_cue`
                # had waited, this line would not run until `release` was set.
                assert writing.wait(2.0)
                assert not release.is_set()
                release.set()

        asyncio.run(scenario())

    def test_a_call_that_drops_the_moment_it_came_up_is_still_heard_in_order(
        self, socket_path: Path
    ) -> None:
        """The case a thread a cue could not keep (#186).

        The two cues mark the two ends of one call, so their order is the claim.
        A call that goes away within one cue's wall time of coming up asks for
        the second before the first has finished playing — 320-620 ms on the
        real path (#174) — and two threads racing for the device would have the
        user hear the call end before they heard it start. The player is fed by
        one worker, so it cannot.

        The stand-in device holds each write open long enough that an unordered
        implementation would have to interleave to pass.
        """

        async def scenario() -> FakeCueOutput:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                player = FakeCueOutput()
                player.while_playing = lambda: time.sleep(0.05)
                adapter, _ = await riding(server, Sink(), cue_player=player)

                # No gap at all between them: both are asked for before the
                # first has reached the device.
                await adapter.play_cue(Cue.CONNECTED)
                await adapter.play_cue(Cue.ENDED)
                deadline = time.monotonic() + 5.0
                while len(player.spans) < 2 and time.monotonic() < deadline:
                    time.sleep(0.005)
                return player

        player = asyncio.run(scenario())
        assert [span.cue for span in player.spans] == [Cue.CONNECTED, Cue.ENDED]

    def test_every_cue_asked_for_is_played_once_and_in_order(self, socket_path: Path) -> None:
        """A burst longer than any real call, to prove the queue drains in order."""
        asked = [Cue.CONNECTED, Cue.EVENT, Cue.EVENT, Cue.ENDED, Cue.CONNECTED, Cue.ENDED]

        async def scenario() -> FakeCueOutput:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                player = FakeCueOutput()
                adapter, _ = await riding(server, Sink(), cue_player=player)
                for cue in asked:
                    await adapter.play_cue(cue)
                deadline = time.monotonic() + 5.0
                while len(player.spans) < len(asked) and time.monotonic() < deadline:
                    time.sleep(0.005)
                return player

        player = asyncio.run(scenario())
        assert [span.cue for span in player.spans] == asked

    def test_one_worker_plays_every_cue_rather_than_a_thread_each(self, socket_path: Path) -> None:
        """The mechanism the order rests on, asserted rather than assumed."""

        async def scenario() -> list[threading.Thread]:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                player = FakeCueOutput()
                on: list[threading.Thread] = []
                player.while_playing = lambda: on.append(threading.current_thread())
                adapter, _ = await riding(server, Sink(), cue_player=player)
                for cue in Cue:
                    await adapter.play_cue(cue)
                deadline = time.monotonic() + 5.0
                while len(player.spans) < len(list(Cue)) and time.monotonic() < deadline:
                    time.sleep(0.005)
                return on

        on = asyncio.run(scenario())
        assert len(on) == len(list(Cue))
        assert len({thread.name for thread in on}) == 1

    def test_the_cue_worker_never_holds_a_closing_engine_open(self, socket_path: Path) -> None:
        """A tone is not a reason for an engine to wait, so the worker is a daemon.

        It also never ends by itself — it has to outlive every call, because the
        cue that matters most is the one that marks a call that has gone — so a
        non-daemon worker would be a process that could not exit at all.
        """

        async def scenario() -> list[threading.Thread]:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                player = FakeCueOutput()
                started: list[threading.Thread] = []
                player.while_playing = lambda: started.append(threading.current_thread())
                adapter, _ = await riding(server, Sink(), cue_player=player)
                await adapter.play_cue(Cue.CONNECTED)
                self.played(player)
                return started

        started = asyncio.run(scenario())
        assert started and all(thread.daemon for thread in started)

    def test_a_cue_that_could_not_be_played_does_not_stop_the_ones_after_it(
        self, socket_path: Path
    ) -> None:
        """One worker for every cue means one failure could have silenced the rest."""

        async def scenario() -> FakeCueOutput:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                player = FakeCueOutput(fails="no such output device")
                adapter, _ = await riding(server, Sink(), cue_player=player)
                await adapter.play_cue(Cue.CONNECTED)
                deadline = time.monotonic() + 5.0
                while len(player.seen_playing) < 1 and time.monotonic() < deadline:
                    time.sleep(0.005)
                player.fails = ""
                await adapter.play_cue(Cue.ENDED)
                while len(player.spans) < 1 and time.monotonic() < deadline:
                    time.sleep(0.005)
                return player

        player = asyncio.run(scenario())
        assert [span.cue for span in player.spans] == [Cue.ENDED]


class TestWhatALostConnectionReleases:
    """A connection that went away by itself still gives its devices back.

    Found by #186's review, and it is #186's problem: `ENDED` is played on a
    drop, out of the adapter's own player, and it must not go out into a
    microphone that a dead call left open. The bug was older than the cue —
    `_note` set `_closing` to keep itself from reporting one loss twice, and
    `_closing` is also what makes `aclose` idempotent, so the `aclose` the
    adapter runs after a drop returned at its first line and stopped nothing.

    **Built without `aiortc`, deliberately.** `webrtc.py` is the one module CI
    cannot exercise — the voice extra is not installed there — and that is
    exactly why a defect in it survived. `_note` and `aclose` touch four
    attributes between them and none of them is a peer connection, so the object
    is assembled here rather than constructed, and the rule gets a test that runs
    on every push instead of on one laptop.
    """

    class Stopped:
        def __init__(self) -> None:
            self.stops = 0

        def stop(self) -> None:
            self.stops += 1

    class Connection:
        def __init__(self) -> None:
            self.closed = 0

        async def close(self) -> None:
            self.closed += 1

    def transport(self) -> Any:
        made = object.__new__(webrtc._WebRtcTransport)
        made._pc = self.Connection()
        made._microphone = self.Stopped()
        made._speaker = self.Stopped()
        made._connected = asyncio.get_event_loop().create_future()
        made._connected.set_result(None)
        made._closing = False
        made._reported = False
        made._on_lost = None
        return made

    def test_a_drop_is_reported_and_the_devices_are_still_released(self) -> None:
        async def scenario() -> tuple[Any, list[str]]:
            made = self.transport()
            losses: list[str] = []
            made.on_lost(losses.append)

            made._note("failed")
            # What the adapter does next, on the task it spawns for it.
            await made.aclose()
            return made, losses

        made, losses = asyncio.run(scenario())
        assert len(losses) == 1
        assert made._microphone.stops == 1
        assert made._speaker.stops == 1
        assert made._pc.closed == 1

    def test_one_loss_is_reported_once_however_many_readings_say_so(self) -> None:
        async def scenario() -> list[str]:
            made = self.transport()
            losses: list[str] = []
            made.on_lost(losses.append)
            made._note("failed")
            made._note("closed")
            return losses

        assert len(asyncio.run(scenario())) == 1

    def test_a_close_this_side_asked_for_is_never_reported_as_a_loss(self) -> None:
        async def scenario() -> list[str]:
            made = self.transport()
            losses: list[str] = []
            made.on_lost(losses.append)
            await made.aclose()
            made._note("closed")
            return losses

        assert asyncio.run(scenario()) == []

    def test_closing_twice_releases_the_devices_once(self) -> None:
        async def scenario() -> Any:
            made = self.transport()
            await made.aclose()
            await made.aclose()
            return made

        made = asyncio.run(scenario())
        assert made._microphone.stops == 1
        assert made._pc.closed == 1

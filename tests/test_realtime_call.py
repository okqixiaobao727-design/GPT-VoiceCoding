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
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from codex_fake import FakeAppServer, FakeRemoteError
from gpt_voicecoding.adapters.call.realtime import (
    APPROVAL_POLICY,
    DEFAULT_REALTIME_MODEL,
    SANDBOX,
    DelegatedTurnError,
    RealtimeCallAdapter,
    RealtimeCallSettings,
    SettingsError,
    realtime_call,
)
from gpt_voicecoding.adapters.codex_app_server.process import AppServerError, attach
from gpt_voicecoding.adapters.codex_app_server.settings import CodexSettings
from gpt_voicecoding.seams.call import (
    CallDropped,
    CallEnded,
    CallStarted,
    CallState,
    UserSpeech,
)
from gpt_voicecoding.seams.delivery import Delivery
from gpt_voicecoding.seams.identity import RequestId
from gpt_voicecoding.seams.verify import VerifyOutcome
from realtime_fake import (
    ANSWER_SDP,
    OFFER_SDP,
    FakeTransport,
    SharedAppServer,
    delegated_script,
    realtime_script,
)

THREAD = "01a02110-d18f-74a0-916d-de1208e9977a"
HOUSE_RULES = "speak the Session Label; never invent a detail"
DELEGATED_RULES = "act only through the control-plane CLI"

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
) -> tuple[RealtimeCallAdapter, FakeTransport]:
    """An adapter wired to a scripted app-server, exactly as the root wires it."""
    audio = transport or FakeTransport()
    adapter = RealtimeCallAdapter(
        sink=sink, settings=settings or quick(), transport_factory=lambda: audio
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


class TestBringingACallUp:
    def test_the_handshake_is_the_route_the_prototype_proved(self, socket_path: Path) -> None:
        """Thread, offer, realtime start, SDP answer, started, audio up."""

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                adapter, audio = await riding(server, Sink())

                snapshot = await adapter.ensure_call(HOUSE_RULES)

                assert snapshot.state is CallState.UP
                assert snapshot.call_id == THREAD
                start = server.calls_to("thread/realtime/start")[0]
                assert start["threadId"] == THREAD
                assert start["transport"] == {"type": "webrtc", "sdp": OFFER_SDP}
                assert start["realtimeStartInstructions"] == HOUSE_RULES
                assert audio.answers == [ANSWER_SDP]
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

                await adapter.ensure_call(HOUSE_RULES)

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

                await adapter.ensure_call(HOUSE_RULES)

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

                await adapter.ensure_call(HOUSE_RULES)

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

                snapshot = await adapter.ensure_call(HOUSE_RULES)

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

                first = await adapter.ensure_call(HOUSE_RULES)
                second = await adapter.ensure_call(HOUSE_RULES)

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

                snapshot = await adapter.ensure_call(HOUSE_RULES)

                assert snapshot.state is CallState.DOWN
                assert audio.closed
                assert server.calls_to("thread/realtime/stop")
                assert sink.of(CallStarted) == []
                assert (await adapter.call_state()).state is CallState.DOWN
                await adapter.aclose()

        asyncio.run(scenario())

    def test_a_call_is_never_opened_on_no_instructions(self, socket_path: Path) -> None:
        """Nothing here invents house rules when the hub generated none."""

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                adapter, _ = await riding(server, Sink())

                snapshot = await adapter.ensure_call("   ")

                assert snapshot.state is CallState.DOWN
                assert server.calls_to("thread/start") == []
                await adapter.aclose()

        asyncio.run(scenario())

    def test_hanging_up_during_the_handshake_abandons_it(self, socket_path: Path) -> None:
        """`end_call` while connecting: the attempt stops, and nothing is reported up."""

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                sink = Sink()
                audio = FakeTransport(connects=False)
                adapter, _ = await riding(server, sink, transport=audio)

                opening = asyncio.ensure_future(adapter.ensure_call(HOUSE_RULES))
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

                opening = asyncio.ensure_future(adapter.ensure_call(HOUSE_RULES))
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

                opening = asyncio.ensure_future(adapter.ensure_call(HOUSE_RULES))
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
        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                adapter, _ = await riding(server, Sink())
                await adapter.ensure_call(HOUSE_RULES)

                receipt = await adapter.speak("that session stopped", request_id=rid())

                assert receipt.outcome is Delivery.DELIVERED
                assert server.calls_to("thread/realtime/appendSpeech") == [
                    {"threadId": THREAD, "text": "that session stopped"}
                ]
                await adapter.aclose()

        asyncio.run(scenario())

    def test_speaking_with_no_call_up_fails_closed(self, socket_path: Path) -> None:
        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                adapter, _ = await riding(server, Sink())

                receipt = await adapter.speak("anyone there", request_id=rid())

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
                await adapter.ensure_call(HOUSE_RULES)

                def refuse(_params: dict) -> dict:
                    raise FakeRemoteError("no realtime session on that thread")

                server.answers("thread/realtime/appendSpeech", refuse)
                receipt = await adapter.speak("hello", request_id=rid())

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
                await adapter.ensure_call(HOUSE_RULES)

                def go_quiet_then_accept(_params: dict) -> dict:
                    audio.go_quiet()
                    return {}

                server.answers("thread/realtime/appendSpeech", go_quiet_then_accept)
                receipt = await adapter.speak("you are needed", request_id=rid())

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
                await adapter.ensure_call(HOUSE_RULES)

                async def die(_params: dict) -> dict:
                    await server.drop_everyone()
                    return {}

                server.answers("thread/realtime/appendSpeech", die)
                receipt = await adapter.speak("you are needed", request_id=rid())

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
                await adapter.ensure_call(HOUSE_RULES)

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
                await adapter.ensure_call(HOUSE_RULES)

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
                await adapter.ensure_call(HOUSE_RULES)

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
                await adapter.ensure_call(HOUSE_RULES)

                audio.lose("the peer connection failed")
                await asyncio.sleep(0.05)
                receipt = await adapter.speak("you are needed", request_id=rid())

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
                await adapter.ensure_call(HOUSE_RULES)

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
                await adapter.ensure_call(HOUSE_RULES)

                await server.notify_all(
                    "thread/realtime/transcript/done",
                    {"threadId": THREAD, "role": "user", "text": "what is codex doing"},
                )
                await asyncio.sleep(0.05)

                assert [event.text for event in sink.of(UserSpeech)] == ["what is codex doing"]
                await adapter.aclose()

        asyncio.run(scenario())

    def test_this_systems_own_voice_is_not_raised_as_the_users(self, socket_path: Path) -> None:
        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                realtime_script(server, thread_id=THREAD)
                sink = Sink()
                adapter, _ = await riding(server, sink)
                await adapter.ensure_call(HOUSE_RULES)

                await server.notify_all(
                    "thread/realtime/transcript/done",
                    {"threadId": THREAD, "role": "assistant", "text": "that session stopped"},
                )
                await asyncio.sleep(0.05)

                assert sink.of(UserSpeech) == []
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

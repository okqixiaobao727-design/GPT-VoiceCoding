"""The Companion Channel's two adapters, against a fake Telegram and a real socket.

Three layers are exercised here, and they are deliberately separate.

- The **null implementation**, which is a real implementation: it answers both
  verbs, and neither answer can be mistaken for reach the engine does not have.
- The **Telegram adapter** against an injected transport — a fake Bot API that
  can be told to refuse at any layer. This is where the contract lives: what a
  receipt says when the network dies halfway through a split message, what
  `verify` says when the token is wrong, and who is allowed to speak to this
  engine.
- The **wire itself**, against a real HTTP server on a real socket, because the
  one file that speaks HTTP is the one file no fake can prove.
"""

from __future__ import annotations

import asyncio
import json
import queue
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from gpt_voicecoding.adapters.companion_channel import (
    NULL_REFERENCE,
    NullCompanionChannel,
    null_channel,
)
from gpt_voicecoding.adapters.companion_channel.telegram import (
    FailureLayer,
    SettingsError,
    TelegramCompanionChannel,
    TelegramError,
    TelegramSettings,
    http_transport,
    split_message,
    telegram_channel,
    utf16_length,
)
from gpt_voicecoding.config import NULL_COMPANION_CHANNEL
from gpt_voicecoding.engine.composition import import_factory
from gpt_voicecoding.seams.companion_channel import CompanionChannel, InboundText
from gpt_voicecoding.seams.connection import Connectable
from gpt_voicecoding.seams.delivery import Delivery
from gpt_voicecoding.seams.identity import new_request_id
from gpt_voicecoding.seams.verify import VerifyOutcome

CHAT = "4242"
STRANGER = "999"
TOKEN_VARIABLE = "GPT_VOICECODING_TEST_TELEGRAM_TOKEN"

#: What the adapter is configured to hold a poll open for. The API counts whole
#: seconds, so this is the smallest honest value; the fake below decides for
#: itself how long to actually block, which is what keeps the suite quick.
POLL_SECONDS = 1.0

#: How long the fake holds a poll open. Long enough that the reader is really
#: waiting rather than spinning, short enough that a worker thread blocked in one
#: is gone before the test ends.
FAKE_POLL_SECONDS = 0.05

#: How long a test will wait for something the reader task has to notice. Far
#: longer than the poll, so slowness is never mistaken for absence.
PATIENCE_SECONDS = 5.0


class Sink:
    """Bridge Core's end of the seam, reduced to the one thing it promises."""

    def __init__(self) -> None:
        self.events: list[InboundText] = []

    def emit(self, event: InboundText) -> None:
        self.events.append(event)


@dataclass
class FakeTelegram:
    """A Bot API that answers, refuses, or hangs — whichever the test needs.

    `getUpdates` blocks the way the real one does, on a queue rather than on a
    network, so the adapter's long poll is exercised as a long poll instead of
    as a busy loop.
    """

    chat_id: str = CHAT
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    #: What was already waiting when this engine first made contact. The backlog
    #: probe reads it; nothing else ever does.
    backlog: list[dict[str, Any]] = field(default_factory=list)
    #: One entry per method name, consumed in order: an exception is raised, a
    #: value is returned, and `None` means "answer the way you normally would".
    answers: dict[str, list[Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.updates: queue.Queue[list[dict[str, Any]]] = queue.Queue()

    def refuse(self, method: str, error: TelegramError, *, times: int = 1) -> None:
        self.answers.setdefault(method, []).extend([error] * times)

    def deliver(self, update: dict[str, Any]) -> None:
        self.updates.put([update])

    def sent(self) -> list[str]:
        return [payload["text"] for method, payload in self.calls if method == "sendMessage"]

    def method_calls(self, method: str) -> list[dict[str, Any]]:
        return [payload for name, payload in self.calls if name == method]

    def __call__(self, method: str, payload: dict[str, Any], *, timeout_seconds: float) -> Any:
        self.calls.append((method, payload))
        queued = self.answers.get(method)
        if queued:
            answer = queued.pop(0)
            if isinstance(answer, BaseException):
                raise answer
            if answer is not None:
                return answer
        if method == "getUpdates":
            return self._updates(payload)
        if method == "getMe":
            return {"id": 1, "is_bot": True, "username": "fake_bot"}
        if method == "getChat":
            return {"id": int(self.chat_id), "type": "private"}
        if method == "sendMessage":
            return {"message_id": len(self.calls)}
        raise AssertionError(f"the adapter called a method this fake does not know: {method}")

    def _updates(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """The backlog probe reads what was waiting; a long poll waits for more."""
        if payload.get("offset") == -1:
            waiting, self.backlog = self.backlog, []
            return waiting
        try:
            return self.updates.get(timeout=FAKE_POLL_SECONDS)
        except queue.Empty:
            return []


def message(text: str, *, chat: str = CHAT, update_id: int = 1) -> dict[str, Any]:
    """One `getUpdates` entry, shaped the way the Bot API shapes it."""
    return {
        "update_id": update_id,
        "message": {"message_id": update_id, "chat": {"id": int(chat)}, "text": text},
    }


def settings(**overrides: Any) -> TelegramSettings:
    table: dict[str, Any] = {
        "token_env": TOKEN_VARIABLE,
        "chat_id": CHAT,
        "poll_timeout_seconds": POLL_SECONDS,
        "request_timeout_seconds": 1.0,
        "retry_seconds": 0.01,
    }
    table.update(overrides)
    return TelegramSettings.of(table)


def channel(api: FakeTelegram, *, sink: Sink | None = None, **overrides: Any):
    return TelegramCompanionChannel(sink=sink, settings=settings(**overrides), transport=api)


async def until(predicate, *, what: str) -> None:
    """Wait for something the reader has to do, rather than for a fixed moment."""
    deadline = asyncio.get_running_loop().time() + PATIENCE_SECONDS
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"waited {PATIENCE_SECONDS}s and {what} never happened")
        await asyncio.sleep(0.01)


class TestTheNullChannel:
    """Running with no text reach is a state, not a stub — and it never lies."""

    def test_it_is_the_seam_it_claims_to_be(self) -> None:
        assert isinstance(NullCompanionChannel(), CompanionChannel)

    def test_a_push_is_a_positive_non_delivery(self) -> None:
        """Core must never be able to record this as having reached anyone."""
        channel = NullCompanionChannel()

        receipt = asyncio.run(channel.send("you are needed", request_id=new_request_id()))

        assert receipt.outcome is Delivery.FAILED
        assert receipt.is_delivered is False
        assert "configured" in receipt.reason

    def test_verify_reports_the_empty_module_string(self) -> None:
        """ADR 0003 reserves empty for exactly this, and MANUAL for nothing else."""
        result = asyncio.run(NullCompanionChannel().verify())

        assert result.outcome is VerifyOutcome.MANUAL
        assert result.loaded == ""

    def test_verify_names_the_way_out_of_itself(self) -> None:
        assert "telegram" in asyncio.run(NullCompanionChannel().verify()).detail

    def test_it_has_nothing_to_be_told(self) -> None:
        """A settings table left behind by a swapped adapter fails the assembly."""
        with pytest.raises(TypeError):
            null_channel(sink=None, settings={"chat_id": CHAT})

    def test_the_reference_the_refusal_names_really_builds_one(self) -> None:
        """A refusal that sends the operator to a reference that does not exist is worse
        than no refusal. The spelling in `config` and the adapter's own are one string."""
        assert NULL_COMPANION_CHANNEL == NULL_REFERENCE
        built = import_factory(NULL_COMPANION_CHANNEL)(sink=None)

        assert isinstance(built, NullCompanionChannel)


class TestWhatTheAdapterMayBeTold:
    def test_a_table_it_does_not_recognise_is_refused(self) -> None:
        with pytest.raises(SettingsError) as refusal:
            TelegramSettings.of({"token_env": TOKEN_VARIABLE, "chat_id": CHAT, "webhook": "on"})

        assert "webhook" in str(refusal.value)

    def test_a_literal_token_is_refused_and_pointed_at_the_variable(self) -> None:
        """The one mistake worth guiding: a credential written into a committed file."""
        with pytest.raises(SettingsError) as refusal:
            TelegramSettings.of({"token": "123:abc", "chat_id": CHAT})

        assert "token_env" in str(refusal.value)

    def test_no_table_at_all_is_refused(self) -> None:
        with pytest.raises(SettingsError):
            TelegramSettings.of(None)

    def test_a_table_missing_what_has_no_default_is_refused_by_name(self) -> None:
        """Not left to a TypeError, which the composition root would misreport."""
        with pytest.raises(SettingsError) as refusal:
            TelegramSettings.of({"chat_id": CHAT})

        assert "token_env" in str(refusal.value)

    def test_a_chat_id_may_be_a_number_and_may_be_a_group(self) -> None:
        assert TelegramSettings.of({"token_env": TOKEN_VARIABLE, "chat_id": -100123}).chat_id == (
            "-100123"
        )

    def test_a_chat_name_is_refused_because_it_could_only_ever_half_work(self) -> None:
        """`@name` sends and never hears — a channel that is deaf without saying so."""
        with pytest.raises(SettingsError) as refusal:
            TelegramSettings.of({"token_env": TOKEN_VARIABLE, "chat_id": "@some_channel"})

        assert "numeric" in str(refusal.value)

    def test_the_token_comes_from_the_variable_the_table_names(self) -> None:
        assert settings().token_in({TOKEN_VARIABLE: " a-real-token "}) == "a-real-token"

    def test_an_unset_variable_refuses_by_name(self) -> None:
        with pytest.raises(SettingsError) as refusal:
            settings().token_in({})

        assert TOKEN_VARIABLE in str(refusal.value)

    def test_the_factory_refuses_to_build_a_channel_it_cannot_authenticate(self) -> None:
        """A missing variable never heals on its own, so it stops the start."""
        with pytest.raises(SettingsError):
            telegram_channel(settings={"token_env": TOKEN_VARIABLE, "chat_id": CHAT}, environ={})

    def test_a_built_channel_fills_both_the_seam_and_the_connection(self) -> None:
        built = telegram_channel(
            settings={"token_env": TOKEN_VARIABLE, "chat_id": CHAT},
            environ={TOKEN_VARIABLE: "123:abc"},
            transport=FakeTelegram(),
        )

        assert isinstance(built, CompanionChannel)
        assert isinstance(built, Connectable)


class TestCuttingAMessageToSize:
    def test_a_short_message_is_one_part(self) -> None:
        assert split_message("hello") == ("hello",)

    def test_nothing_is_no_parts(self) -> None:
        assert split_message("") == ()

    def test_every_part_fits_and_nothing_is_lost(self) -> None:
        text = "hello world " * 900

        parts = split_message(text)

        assert len(parts) > 1
        assert all(utf16_length(part) <= 4096 for part in parts)
        assert "".join(parts) == text

    def test_the_cap_is_counted_in_utf16_units_not_characters(self) -> None:
        """3000 emoji are 3000 characters and 6000 units. `len()` would send them whole."""
        text = "\N{GRINNING FACE}" * 3000

        parts = split_message(text)

        assert len(parts) == 2
        assert "".join(parts) == text

    def test_a_surrogate_pair_is_never_cut_in_half(self) -> None:
        parts = split_message("\N{GRINNING FACE}" * 3000)

        assert all(part.encode("utf-16", "strict") for part in parts)

    def test_it_prefers_to_break_where_the_text_does(self) -> None:
        text = ("a" * 100 + "\n") * 60

        parts = split_message(text)

        assert parts[0].endswith("\n")
        assert "".join(parts) == text


class TestPushingOneMessage:
    def test_a_push_that_lands_is_delivered_to_the_configured_chat(self) -> None:
        api = FakeTelegram()

        receipt = asyncio.run(
            channel(api).send("stopped on a question", request_id=new_request_id())
        )

        assert receipt.outcome is Delivery.DELIVERED
        assert api.method_calls("sendMessage") == [
            {"chat_id": CHAT, "text": "stopped on a question"}
        ]

    def test_a_network_that_dies_mid_send_is_a_classified_failure(self) -> None:
        api = FakeTelegram()
        api.refuse("sendMessage", TelegramError(FailureLayer.NETWORK, "connection reset"))

        receipt = asyncio.run(channel(api).send("you are needed", request_id=new_request_id()))

        assert receipt.outcome is Delivery.FAILED
        assert receipt.is_delivered is False
        assert FailureLayer.NETWORK in receipt.reason

    def test_a_push_never_blocks_the_engine_loop(self) -> None:
        """The whole reason the wire runs on a thread: other work keeps running."""

        class Slow(FakeTelegram):
            def __call__(self, method: str, payload: dict[str, Any], **kwargs: Any) -> Any:
                threading.Event().wait(0.2)
                return super().__call__(method, payload, **kwargs)

        async def both() -> int:
            ticks = 0

            async def ticking() -> None:
                nonlocal ticks
                while True:
                    await asyncio.sleep(0.01)
                    ticks += 1

            ticker = asyncio.create_task(ticking())
            await channel(Slow()).send("a message", request_id=new_request_id())
            ticker.cancel()
            return ticks

        assert asyncio.run(both()) > 1

    def test_an_oversized_message_is_split_and_not_dropped(self) -> None:
        api = FakeTelegram()
        text = "hello world " * 900

        receipt = asyncio.run(channel(api).send(text, request_id=new_request_id()))

        assert receipt.outcome is Delivery.DELIVERED
        assert len(api.sent()) > 1
        assert "".join(api.sent()) == text

    def test_a_failure_after_words_arrived_is_unknown_and_stops(self) -> None:
        """Never FAILED — words did reach the user — and never a hole in the middle."""
        api = FakeTelegram()
        api.answers["sendMessage"] = [
            {"message_id": 1},
            TelegramError(FailureLayer.NETWORK, "connection reset"),
        ]
        text = "hello world " * 1800

        receipt = asyncio.run(channel(api).send(text, request_id=new_request_id()))

        assert receipt.outcome is Delivery.UNKNOWN
        assert receipt.is_delivered is False
        assert "1 of" in receipt.reason
        assert len(api.sent()) == 2

    def test_a_message_with_no_words_is_never_called_delivered(self) -> None:
        receipt = asyncio.run(channel(FakeTelegram()).send("", request_id=new_request_id()))

        assert receipt.outcome is Delivery.FAILED
        assert receipt.reason


class TestAnsweringForItself:
    def test_a_working_channel_proves_both_halves(self) -> None:
        api = FakeTelegram()

        result = asyncio.run(channel(api).verify())

        assert result.outcome is VerifyOutcome.PASS
        assert result.loaded.endswith(":TelegramCompanionChannel")
        assert [method for method, _ in api.calls] == ["getMe", "getChat"]

    def test_a_wrong_token_fails_and_names_the_layer(self) -> None:
        api = FakeTelegram()
        api.refuse(
            "getMe", TelegramError(FailureLayer.CREDENTIALS, "getMe was refused: Unauthorized")
        )

        result = asyncio.run(channel(api).verify())

        assert result.outcome is VerifyOutcome.FAIL
        assert FailureLayer.CREDENTIALS in result.detail

    def test_a_chat_this_bot_cannot_reach_fails_as_the_destination(self) -> None:
        """A valid token pointed at the wrong chat is the outage that looks healthiest."""
        api = FakeTelegram()
        api.refuse("getChat", TelegramError(FailureLayer.DESTINATION, "chat not found"))

        result = asyncio.run(channel(api).verify())

        assert result.outcome is VerifyOutcome.FAIL
        assert FailureLayer.DESTINATION in result.detail

    def test_an_unreachable_network_still_reports_what_is_loaded(self) -> None:
        """Loaded-but-unreachable is FAIL, never MANUAL: something real is loaded."""
        api = FakeTelegram()
        api.refuse("getMe", TelegramError(FailureLayer.NETWORK, "no route to host"))

        result = asyncio.run(channel(api).verify())

        assert result.outcome is VerifyOutcome.FAIL
        assert result.loaded
        assert FailureLayer.NETWORK in result.detail


class TestListening:
    def test_text_from_the_user_surfaces_unclassified(self) -> None:
        """The adapter has no notion of Duty, of commands, or of relays. It reports text."""
        api, sink = FakeTelegram(), Sink()

        async def listening() -> None:
            listener = channel(api, sink=sink)
            await listener.connect()
            await until(lambda: api.method_calls("getUpdates"), what="the first contact")
            api.deliver(message("turn duty off"))
            await until(lambda: sink.events, what="the text surfaced")
            await listener.aclose()

        asyncio.run(listening())

        assert sink.events == [InboundText(text="turn duty off", origin=CHAT)]

    def test_a_stranger_is_met_with_silence(self) -> None:
        """Core routes inbound text into the control plane, so the front door is closed
        here — and a refusal sent back would confirm the bot is alive and attended."""
        api, sink = FakeTelegram(), Sink()

        async def listening() -> None:
            listener = channel(api, sink=sink)
            await listener.connect()
            await until(lambda: api.method_calls("getUpdates"), what="the first contact")
            api.deliver(message("turn duty off", chat=STRANGER, update_id=7))
            api.deliver(message("this one is mine", update_id=8))
            await until(lambda: sink.events, what="the user's own text surfaced")
            await listener.aclose()

        asyncio.run(listening())

        assert [event.text for event in sink.events] == ["this one is mine"]
        assert api.sent() == []

    def test_the_backlog_is_thrown_away_once_at_first_contact(self) -> None:
        """A command from three hours ago would be acted on against a state that moved."""
        api, sink = FakeTelegram(), Sink()
        api.backlog = [message("turn duty off", update_id=41)]

        async def listening() -> None:
            listener = channel(api, sink=sink)
            await listener.connect()
            await until(lambda: len(api.method_calls("getUpdates")) >= 2, what="the first poll")
            await listener.aclose()

        asyncio.run(listening())

        assert sink.events == []
        polls = api.method_calls("getUpdates")
        assert polls[0]["offset"] == -1
        assert polls[1]["offset"] == 42

    def test_a_blip_mid_run_does_not_throw_anything_away(self) -> None:
        """The engine was alive and its state never reset, so nothing here is stale."""
        api, sink = FakeTelegram(), Sink()
        api.answers["getUpdates"] = [
            None,  # the backlog probe: nothing was waiting
            TelegramError(FailureLayer.NETWORK, "no route to host"),
        ]

        async def listening() -> None:
            listener = channel(api, sink=sink)
            await listener.connect()
            await until(lambda: len(api.method_calls("getUpdates")) >= 3, what="the reader retried")
            api.deliver(message("still here"))
            await until(lambda: sink.events, what="the text arrived after the blip")
            await listener.aclose()

        asyncio.run(listening())

        assert [event.text for event in sink.events] == ["still here"]
        assert [poll.get("offset") for poll in api.method_calls("getUpdates")].count(-1) == 1

    def test_an_unreachable_telegram_never_stops_the_engine_starting(self) -> None:
        """A text channel that is down must not take the voice path down with it."""
        api, sink = FakeTelegram(), Sink()
        api.refuse("getUpdates", TelegramError(FailureLayer.NETWORK, "no route to host"), times=1)

        async def listening() -> None:
            listener = channel(api, sink=sink)
            await listener.connect()  # must not raise
            await until(
                lambda: len(api.method_calls("getUpdates")) >= 2, what="the reader kept trying"
            )
            await listener.aclose()

        asyncio.run(listening())

    def test_closing_never_waits_out_a_poll_that_is_still_open(self) -> None:
        """The measured reason the reader is a daemon thread rather than `to_thread`.

        A poll parked on the network held `asyncio.run` open for as long as it
        lasted — 0.20s to close the adapter, 3.01s to leave the loop — which at
        the default 25s poll is a quit that hangs. Nothing here may wait on the
        poll: not `aclose`, and not the loop it returns to.
        """
        stuck = threading.Event()

        class Parked(FakeTelegram):
            def __call__(self, method: str, payload: dict[str, Any], **kwargs: Any) -> Any:
                if method == "getUpdates":
                    stuck.set()
                    threading.Event().wait(30)
                return super().__call__(method, payload, **kwargs)

        async def opened_and_closed() -> float:
            listener = channel(Parked(), sink=Sink())
            await listener.connect()
            await until(stuck.is_set, what="the poll parked itself on the network")
            started = asyncio.get_running_loop().time()
            await listener.aclose()
            return asyncio.get_running_loop().time() - started

        assert asyncio.run(opened_and_closed()) < 1.0

    def test_nothing_surfaces_after_closing_has_said_it_stopped(self) -> None:
        """A poll already in flight comes back after `aclose` returns. It says nothing.

        An adapter that reported it had stopped listening and then put
        control-plane text into Bridge Core would be worse than one that lost
        the message — and nothing is really lost: Telegram never had these
        acknowledged and re-serves them to whatever listens next.
        """
        parked, released = threading.Event(), threading.Event()
        sink = Sink()

        class Late(FakeTelegram):
            def __call__(self, method: str, payload: dict[str, Any], **kwargs: Any) -> Any:
                if method == "getUpdates" and payload.get("offset") != -1:
                    parked.set()
                    released.wait(PATIENCE_SECONDS)
                    return [message("sent while the engine was closing")]
                return super().__call__(method, payload, **kwargs)

        async def closing() -> None:
            listener = channel(Late(), sink=sink)
            await listener.connect()
            await until(parked.is_set, what="the poll parked itself on the network")
            await listener.aclose()
            released.set()
            # The loop is still running here on purpose: after `asyncio.run`
            # returns, a hand-off would fail for the wrong reason.
            await asyncio.sleep(0.5)

        asyncio.run(closing())

        assert sink.events == []

    def test_a_hand_off_already_in_flight_when_closing_ran_still_says_nothing(self) -> None:
        """The check that actually holds is the one on the loop, so it is tested there.

        The reader's own check is an early exit: between it and the hand-off it
        schedules, `aclose` can run to completion. What closes that gap is
        reading the stop signal on the loop, where `aclose` also runs — and the
        only honest way to exercise an ordering is to stand at the point the
        ordering is decided, which is why this reaches for the hand-off itself.
        """
        sink = Sink()

        async def closing() -> None:
            listener = channel(FakeTelegram(), sink=sink)
            await listener.connect()
            await listener.aclose()
            listener._surface(InboundText(text="scheduled before the close", origin=CHAT))

        asyncio.run(closing())

        assert sink.events == []

    def test_opening_and_closing_are_both_idempotent(self) -> None:
        async def twice() -> None:
            listener = channel(FakeTelegram(), sink=Sink())
            await listener.connect()
            await listener.connect()
            await listener.aclose()
            await listener.aclose()

        asyncio.run(twice())


class _FakeBotApi(BaseHTTPRequestHandler):
    """One canned answer, chosen by the method the adapter asked for."""

    answers: dict[str, tuple[int, dict[str, Any]]] = {}

    def do_POST(self) -> None:  # noqa: N802 - the name http.server dispatches on
        method = self.path.rsplit("/", 1)[-1]
        code, document = self.answers.get(method, (200, {"ok": True, "result": {}}))
        body = json.dumps(document).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        """Quiet: this server's noise is not this suite's output."""


@pytest.fixture
def bot_api():
    """A real HTTP server on a real socket — the only thing that proves the wire."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeBotApi)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=PATIENCE_SECONDS)


def root_of(server: ThreadingHTTPServer) -> str:
    host, port = server.server_address[:2]
    return f"http://{host}:{port}"


class TestTheWireItself:
    """The one file that speaks HTTP, against something that really answers."""

    def test_a_result_comes_back(self, bot_api) -> None:
        _FakeBotApi.answers = {"getMe": (200, {"ok": True, "result": {"username": "a_bot"}})}
        call = http_transport(token="123:abc", api_root=root_of(bot_api))

        assert call("getMe", {}, timeout_seconds=5.0) == {"username": "a_bot"}

    def test_a_rejected_token_is_the_credentials_layer(self, bot_api) -> None:
        _FakeBotApi.answers = {"getMe": (401, {"ok": False, "description": "Unauthorized"})}
        call = http_transport(token="123:abc", api_root=root_of(bot_api))

        with pytest.raises(TelegramError) as refused:
            call("getMe", {}, timeout_seconds=5.0)

        assert refused.value.layer is FailureLayer.CREDENTIALS

    def test_a_chat_that_is_not_there_is_the_destination_layer(self, bot_api) -> None:
        _FakeBotApi.answers = {
            "getChat": (400, {"ok": False, "description": "Bad Request: chat not found"})
        }
        call = http_transport(token="123:abc", api_root=root_of(bot_api))

        with pytest.raises(TelegramError) as refused:
            call("getChat", {"chat_id": CHAT}, timeout_seconds=5.0)

        assert refused.value.layer is FailureLayer.DESTINATION

    def test_a_refusal_inside_a_200_is_still_a_refusal(self, bot_api) -> None:
        """Telegram says no inside a 200 as readily as with a status code."""
        _FakeBotApi.answers = {
            "sendMessage": (200, {"ok": False, "error_code": 403, "description": "blocked"})
        }
        call = http_transport(token="123:abc", api_root=root_of(bot_api))

        with pytest.raises(TelegramError) as refused:
            call("sendMessage", {"chat_id": CHAT, "text": "hi"}, timeout_seconds=5.0)

        assert refused.value.layer is FailureLayer.CREDENTIALS

    def test_nothing_listening_is_the_network_layer(self, bot_api) -> None:
        host, port = bot_api.server_address[:2]
        bot_api.shutdown()
        bot_api.server_close()
        call = http_transport(token="123:abc", api_root=f"http://{host}:{port}")

        with pytest.raises(TelegramError) as unreachable:
            call("getMe", {}, timeout_seconds=1.0)

        assert unreachable.value.layer is FailureLayer.NETWORK

    def test_the_token_is_never_in_the_words_a_failure_carries(self, bot_api) -> None:
        """The token is in every URL, so an error that quoted one would log a credential."""
        _FakeBotApi.answers = {"getMe": (401, {"ok": False, "description": "Unauthorized"})}
        call = http_transport(token="secret-token-value", api_root=root_of(bot_api))

        with pytest.raises(TelegramError) as refused:
            call("getMe", {}, timeout_seconds=5.0)

        assert "secret-token-value" not in refused.value.detail

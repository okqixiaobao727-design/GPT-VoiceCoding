"""The Codex transport, against a real socket and real frames.

Every test here drives `tests/codex_fake.py`, which speaks the server half of the
same protocol. Nothing is mocked: the point of this layer is that it moves bytes
correctly, and a mock of it would assert only that the test author agrees with
themselves.

The awkward cases are the ones worth the socket — a server request that is
deliberately left unanswered (that is how `ask` is implemented), and an
app-server that dies with a request outstanding.
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from codex_fake import FakeAppServer, FakeRemoteError
from gpt_voicecoding.adapters.codex_app_server.wire import (
    METHOD_NOT_FOUND,
    AppServerConnection,
    RemoteError,
    WireClosed,
    WireError,
)

_names = itertools.count()


@pytest.fixture
def socket_path() -> Iterator[Path]:
    """Somewhere short enough to bind: Darwin caps an ``AF_UNIX`` path at 103 bytes."""
    path = Path("/tmp") / f"vc-wire-{next(_names)}-{id(object())}.sock"
    yield path
    path.unlink(missing_ok=True)


async def _until(condition: Callable[[], bool], timeout: float = 2.0) -> None:
    """Wait for something the far side will make true, or fail the test saying so."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if condition():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("the far side never got there")


async def _slowly(_params: dict) -> dict:
    """A method that will not answer inside any test's patience."""
    await asyncio.sleep(30)
    return {}


class TestCarryingOneCall:
    def test_a_request_carries_its_params_and_returns_the_result(self, socket_path: Path) -> None:
        async def scenario() -> tuple[dict, list[dict]]:
            async with FakeAppServer(socket_path) as server:
                server.answers("thread/read", {"thread": {"id": "t-1"}})
                connection = AppServerConnection(socket_path)
                await connection.connect()
                try:
                    return await connection.request("thread/read", {"threadId": "t-1"}), (
                        server.calls_to("thread/read")
                    )
                finally:
                    await connection.aclose()

        answer, calls = asyncio.run(scenario())
        assert answer == {"thread": {"id": "t-1"}}
        assert calls == [{"threadId": "t-1"}]

    def test_an_error_answer_is_raised_carrying_the_far_side_s_own_words(
        self, socket_path: Path
    ) -> None:
        """A receipt has to be able to quote the reason, so it must survive the trip."""

        def refuse(_params: dict) -> dict:
            raise FakeRemoteError("expected active turn id `x` but found `y`", code=-32600)

        async def scenario() -> RemoteError:
            async with FakeAppServer(socket_path) as server:
                server.answers("turn/steer", refuse)
                connection = AppServerConnection(socket_path)
                await connection.connect()
                try:
                    with pytest.raises(RemoteError) as raised:
                        await connection.request("turn/steer", {"threadId": "t-1"})
                    return raised.value
                finally:
                    await connection.aclose()

        error = asyncio.run(scenario())
        assert error.code == -32600
        assert "expected active turn id" in error.remote_message

    def test_concurrent_requests_each_get_their_own_answer(self, socket_path: Path) -> None:
        """Correlation is by id: a slow answer may never be handed to a fast caller."""

        async def answer(params: dict) -> dict:
            await asyncio.sleep(0.05 if params["threadId"] == "slow" else 0)
            return {"echoed": params["threadId"]}

        async def scenario() -> list[dict]:
            async with FakeAppServer(socket_path) as server:
                server.answers("thread/read", answer)
                connection = AppServerConnection(socket_path)
                await connection.connect()
                try:
                    return await asyncio.gather(
                        connection.request("thread/read", {"threadId": "slow"}),
                        connection.request("thread/read", {"threadId": "fast"}),
                    )
                finally:
                    await connection.aclose()

        assert asyncio.run(scenario()) == [{"echoed": "slow"}, {"echoed": "fast"}]

    def test_a_payload_larger_than_one_short_frame_survives(self, socket_path: Path) -> None:
        """A whole thread readback does not fit in a 125-byte length field."""
        big = "x" * 200_000

        async def scenario() -> dict:
            async with FakeAppServer(socket_path) as server:
                server.answers("thread/read", lambda params: {"echoed": params["text"]})
                connection = AppServerConnection(socket_path)
                await connection.connect()
                try:
                    return await connection.request("thread/read", {"text": big})
                finally:
                    await connection.aclose()

        assert asyncio.run(scenario())["echoed"] == big


class TestHearingBack:
    def test_a_notification_reaches_the_handler(self, socket_path: Path) -> None:
        heard: list[dict] = []

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                connection = AppServerConnection(socket_path, on_notification=heard.append)
                await connection.connect()
                try:
                    await server.notify_all("thread/status/changed", {"threadId": "t-1"})
                    await _until(lambda: bool(heard))
                finally:
                    await connection.aclose()

        asyncio.run(scenario())
        assert [note["method"] for note in heard] == ["thread/status/changed"]
        assert heard[0]["params"] == {"threadId": "t-1"}

    def test_a_server_request_reaches_the_handler_and_is_answered_later(
        self, socket_path: Path
    ) -> None:
        """An approval arrives this way, and is answered long after the call returned."""
        held: list[dict] = []

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                connection = AppServerConnection(socket_path, on_server_request=held.append)
                await connection.connect()
                try:
                    raised = await server.ask_all(
                        "item/commandExecution/requestApproval",
                        {"threadId": "t-1", "itemId": "i-1"},
                    )
                    await _until(lambda: bool(held))
                    assert not server.answered(raised)
                    await connection.respond(held[0]["id"], {"decision": "accept"})
                    await _until(lambda: server.answered(raised))
                finally:
                    await connection.aclose()

        asyncio.run(scenario())
        assert held[0]["params"] == {"threadId": "t-1", "itemId": "i-1"}

    def test_a_server_request_left_alone_stays_unanswered(self, socket_path: Path) -> None:
        """`ask` is implemented as silence, so silence has to really be silence."""
        held: list[dict] = []

        async def scenario() -> bool:
            async with FakeAppServer(socket_path) as server:
                connection = AppServerConnection(socket_path, on_server_request=held.append)
                await connection.connect()
                try:
                    raised = await server.ask_all(
                        "item/fileChange/requestApproval", {"itemId": "i-1"}
                    )
                    await _until(lambda: bool(held))
                    await asyncio.sleep(0.05)
                    return server.answered(raised)
                finally:
                    await connection.aclose()

        assert asyncio.run(scenario()) is False

    def test_a_server_request_nobody_handles_is_refused_rather_than_stalled(
        self, socket_path: Path
    ) -> None:
        """Silence is a decision this adapter makes, never one it falls into."""

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                connection = AppServerConnection(socket_path)
                await connection.connect()
                try:
                    raised = await server.ask_all("item/tool/requestUserInput", {})
                    await _until(lambda: server.answered(raised))
                finally:
                    await connection.aclose()

        asyncio.run(scenario())
        assert METHOD_NOT_FOUND == -32601


class TestWhenItGoesWrong:
    def test_the_app_server_dying_fails_every_outstanding_request(self, socket_path: Path) -> None:
        """A caller waiting forever cannot classify anything, which is the worst outcome."""

        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                server.answers("turn/start", _slowly)
                connection = AppServerConnection(socket_path)
                await connection.connect()
                try:
                    waiting = asyncio.ensure_future(
                        connection.request("turn/start", {"threadId": "t-1"})
                    )
                    await _until(lambda: bool(server.calls_to("turn/start")))
                    await server.drop_everyone()
                    with pytest.raises(WireClosed):
                        await asyncio.wait_for(waiting, 2)
                finally:
                    await connection.aclose()

        asyncio.run(scenario())

    def test_a_request_that_never_answers_times_out_naming_its_method(
        self, socket_path: Path
    ) -> None:
        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                server.answers("turn/start", _slowly)
                connection = AppServerConnection(socket_path)
                await connection.connect()
                try:
                    with pytest.raises(WireError, match="turn/start did not answer in time"):
                        await connection.request("turn/start", {}, timeout_seconds=0.05)
                finally:
                    await connection.aclose()

        asyncio.run(scenario())

    def test_closing_twice_never_raises(self, socket_path: Path) -> None:
        """A shutdown already under way must not be made worse by a second close."""

        async def scenario() -> bool:
            async with FakeAppServer(socket_path) as server:
                connection = AppServerConnection(server.path)
                await connection.connect()
                await connection.aclose()
                await connection.aclose()
                return connection.is_open

        assert asyncio.run(scenario()) is False

    def test_using_a_closed_connection_says_so_rather_than_hanging(self, socket_path: Path) -> None:
        async def scenario() -> None:
            async with FakeAppServer(socket_path) as server:
                connection = AppServerConnection(server.path)
                await connection.connect()
                await connection.aclose()
                with pytest.raises(WireClosed):
                    await connection.request("thread/read", {})

        asyncio.run(scenario())

    def test_a_socket_that_is_not_there_fails_with_the_path_named(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            connection = AppServerConnection(tmp_path / "nothing.sock")
            with pytest.raises(WireError, match="cannot reach the codex app-server"):
                await connection.connect()

        asyncio.run(scenario())

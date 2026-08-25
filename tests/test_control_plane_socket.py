"""The wire itself: one JSON object per line, over a Unix domain socket.

Every case here is one of the edge cases #3 names, and each is a failure that
has happened rather than one that might: an engine that is down must say so
instead of hanging, one bad line must cost one request rather than the server,
an unbounded line must not become an unbounded buffer, two surfaces must not
read each other's replies, and a socket this user does not own must be refused
rather than trusted.

No policy is exercised here — `ControlPlane` is driven with a stub so that a
failure in this file is always about the transport.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from gpt_voicecoding.control_plane.client import (
    DEFAULT_TIMEOUT_SECONDS,
    EngineUnreachable,
    ask,
    timeout_for,
)
from gpt_voicecoding.control_plane.ownership import SocketPathTooLong
from gpt_voicecoding.control_plane.server import AlreadyServing, ControlPlaneServer
from gpt_voicecoding.seams.control_plane import (
    MAX_REQUEST_BYTES,
    Action,
    ErrorCode,
    Reply,
    Request,
)


@pytest.fixture
def socket_dir() -> Iterator[Path]:
    """A directory short enough to hold a socket path.

    Darwin caps an `AF_UNIX` path at 103 bytes, and pytest's own temporary
    directory is already longer than that on macOS. The cap is the reason the
    engine's socket does not live beside its state file either.
    """
    base = Path(tempfile.mkdtemp(prefix="gvc-", dir="/tmp"))
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


class Held:
    """One action stopped inside the plane until the test lets it go.

    A sleep would only make the slow handler *probably* slower than the quick
    one; this makes it certainly unfinished, so nothing rests on a margin that
    a busy machine can eat.
    """

    def __init__(self, action: Action) -> None:
        self.action = action
        self.reached = asyncio.Event()
        self.release = asyncio.Event()


class StubPlane:
    """Answers every action with the action it was asked, holding one on request."""

    def __init__(self, *, held: Held | None = None) -> None:
        self.held = held
        self.handled: list[Action] = []

    async def handle(self, request: Request) -> Reply:
        if self.held is not None and request.action is self.held.action:
            self.held.reached.set()
            await self.held.release.wait()
        self.handled.append(request.action)
        return Reply.answered(request.action, {"echo": dict(request.payload)})


async def serving(socket_dir: Path, plane: StubPlane, **kwargs: object) -> ControlPlaneServer:
    server = ControlPlaneServer(plane=plane, path=socket_dir / "control.sock", **kwargs)
    await server.start()
    return server


async def raw(path: Path, line: bytes) -> bytes:
    """One connection, one line in, one line out — bypassing the client."""
    reader, writer = await asyncio.open_unix_connection(str(path))
    writer.write(line)
    await writer.drain()
    answer = await reader.readline()
    writer.close()
    await writer.wait_closed()
    return answer


class TestServingAndAnswering:
    def test_one_request_gets_one_reply(self, socket_dir: Path) -> None:
        async def scenario() -> Reply:
            server = await serving(socket_dir, StubPlane())
            try:
                return await ask(Request(action=Action.STATUS), path=server.path)
            finally:
                await server.aclose()

        reply = asyncio.run(scenario())

        assert reply.ok
        assert reply.action is Action.STATUS

    def test_one_connection_may_carry_several_requests(self, socket_dir: Path) -> None:
        """The Companion Channel adapter will hold one connection open."""
        plane = StubPlane()

        async def scenario() -> None:
            server = await serving(socket_dir, plane)
            try:
                reader, writer = await asyncio.open_unix_connection(str(server.path))
                for action in (Action.STATUS, Action.SESSIONS, Action.VERIFY):
                    writer.write(json.dumps(Request(action=action).as_document()).encode() + b"\n")
                    await writer.drain()
                    assert Reply.of(json.loads(await reader.readline())).action is action
                writer.close()
                await writer.wait_closed()
            finally:
                await server.aclose()

        asyncio.run(scenario())
        assert plane.handled == [Action.STATUS, Action.SESSIONS, Action.VERIFY]

    def test_the_socket_is_private_to_this_user(self, socket_dir: Path) -> None:
        async def scenario() -> int:
            server = await serving(socket_dir, StubPlane())
            try:
                return server.path.stat().st_mode & 0o777
            finally:
                await server.aclose()

        assert asyncio.run(scenario()) & 0o077 == 0

    def test_the_socket_file_is_cleaned_up(self, socket_dir: Path) -> None:
        async def scenario() -> bool:
            server = await serving(socket_dir, StubPlane())
            await server.aclose()
            return server.path.exists()

        assert asyncio.run(scenario()) is False


class TestAnEngineThatIsNotThere:
    def test_a_missing_socket_is_a_named_error_rather_than_a_hang(self, socket_dir: Path) -> None:
        with pytest.raises(EngineUnreachable) as unreachable:
            asyncio.run(
                ask(Request(action=Action.STATUS), path=socket_dir / "nothing.sock", timeout=0.5)
            )

        assert unreachable.value.code is ErrorCode.ENGINE_UNREACHABLE
        assert str(socket_dir / "nothing.sock") in str(unreachable.value)

    def test_a_socket_file_nobody_is_listening_on_is_the_same_answer(
        self, socket_dir: Path
    ) -> None:
        """A bound socket outlives its process, so the file proves nothing."""
        debris = socket_dir / "debris.sock"
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(debris))
        stale.close()

        with pytest.raises(EngineUnreachable):
            asyncio.run(ask(Request(action=Action.STATUS), path=debris, timeout=0.5))

    def test_a_socket_this_user_does_not_own_is_refused_before_anything_is_sent(
        self, socket_dir: Path
    ) -> None:
        async def scenario() -> None:
            server = await serving(socket_dir, StubPlane())
            try:
                await ask(
                    Request(action=Action.STATUS),
                    path=server.path,
                    owner_of=lambda path: os.geteuid() + 1,
                )
            finally:
                await server.aclose()

        with pytest.raises(EngineUnreachable) as refused:
            asyncio.run(scenario())

        assert "not private to this user" in str(refused.value)


class TestOneBadLineCostsOneRequest:
    def test_a_line_that_is_not_json_is_refused_and_the_server_keeps_serving(
        self, socket_dir: Path
    ) -> None:
        async def scenario() -> tuple[Reply, Reply]:
            server = await serving(socket_dir, StubPlane())
            try:
                bad = Reply.of(json.loads(await raw(server.path, b"{not json\n")))
                good = await ask(Request(action=Action.STATUS), path=server.path)
                return bad, good
            finally:
                await server.aclose()

        bad, good = asyncio.run(scenario())

        assert bad.error is not None
        assert bad.error.code is ErrorCode.MALFORMED_REQUEST
        assert bad.action is None
        assert good.ok

    def test_a_bad_line_does_not_close_the_connection_it_arrived_on(self, socket_dir: Path) -> None:
        async def scenario() -> tuple[Reply, Reply]:
            server = await serving(socket_dir, StubPlane())
            try:
                reader, writer = await asyncio.open_unix_connection(str(server.path))
                writer.write(b'{"action": "not-an-action"}\n')
                await writer.drain()
                bad = Reply.of(json.loads(await reader.readline()))
                asking = json.dumps(Request(action=Action.STATUS).as_document()).encode()
                writer.write(asking + b"\n")
                await writer.drain()
                good = Reply.of(json.loads(await reader.readline()))
                writer.close()
                await writer.wait_closed()
                return bad, good
            finally:
                await server.aclose()

        bad, good = asyncio.run(scenario())

        assert bad.error is not None
        assert bad.error.code is ErrorCode.UNKNOWN_ACTION
        assert good.ok

    def test_an_oversized_line_is_bounded_and_answered(self, socket_dir: Path) -> None:
        """A peer must not be able to make the engine hold an unbounded buffer."""

        async def scenario() -> tuple[Reply, Reply]:
            server = await serving(socket_dir, StubPlane())
            try:
                flood = b'{"action": "status", "payload": {"x": "' + b"a" * MAX_REQUEST_BYTES
                refusal = Reply.of(json.loads(await raw(server.path, flood)))
                after = await ask(Request(action=Action.STATUS), path=server.path)
                return refusal, after
            finally:
                await server.aclose()

        refusal, after = asyncio.run(scenario())

        assert refusal.error is not None
        assert refusal.error.code is ErrorCode.MALFORMED_REQUEST
        assert "bytes" in refusal.error.message
        assert after.ok

    def test_a_reply_larger_than_the_bound_is_refused_by_the_surface(
        self, socket_dir: Path
    ) -> None:
        """The bound binds both directions: a surface reads no more than it must."""

        class Flood:
            async def handle(self, request: Request) -> Reply:
                return Reply.answered(request.action, {"x": "a" * MAX_REQUEST_BYTES})

        async def scenario() -> None:
            server = ControlPlaneServer(plane=Flood(), path=socket_dir / "control.sock")
            await server.start()
            try:
                await ask(Request(action=Action.STATUS), path=server.path)
            finally:
                await server.aclose()

        with pytest.raises(EngineUnreachable):
            asyncio.run(scenario())


class TestTwoSurfacesAtOnce:
    def test_concurrent_clients_each_get_their_own_reply(self, socket_dir: Path) -> None:
        held = Held(Action.RELAY)
        plane = StubPlane(held=held)

        async def scenario() -> tuple[Reply, Reply]:
            server = await serving(socket_dir, plane)
            try:
                slow = asyncio.ensure_future(
                    ask(Request(action=Action.RELAY), path=server.path, timeout=5)
                )
                # Wait for the slow handler to be in flight — but never past the
                # slow request itself, or a RELAY that failed before it ever
                # reached the plane would hang the suite instead of failing it.
                reaching = asyncio.ensure_future(held.reached.wait())
                await asyncio.wait([reaching, slow], return_when=asyncio.FIRST_COMPLETED)
                reaching.cancel()
                if slow.done():
                    await slow  # it never arrived; let it say why
                quick = await ask(Request(action=Action.STATUS), path=server.path, timeout=5)
                # The quick one did not wait behind the slow one: one surface
                # cannot wedge another, which is what a single-threaded accept
                # loop would do. The slow handler is demonstrably still in
                # flight, because nothing has released it yet.
                assert not slow.done()
                assert plane.handled == [Action.STATUS]
                held.release.set()
                return await slow, quick
            finally:
                held.release.set()
                await server.aclose()

        slow, quick = asyncio.run(scenario())

        assert slow.action is Action.RELAY
        assert quick.action is Action.STATUS
        assert plane.handled == [Action.STATUS, Action.RELAY]

    def test_every_reply_names_the_action_it_answers(self, socket_dir: Path) -> None:
        """A surface that guessed which reply was its own is a surface that races."""

        async def scenario() -> list[Reply]:
            server = await serving(socket_dir, StubPlane())
            try:
                asked = [
                    ask(Request(action=action, payload={"n": index}), path=server.path, timeout=5)
                    for index, action in enumerate(Action)
                ]
                return list(await asyncio.gather(*asked))
            finally:
                await server.aclose()

        replies = asyncio.run(scenario())

        assert [reply.action for reply in replies] == list(Action)
        assert [reply.data["echo"]["n"] for reply in replies] == list(range(len(list(Action))))


class TestClaimingThePath:
    def test_a_live_engine_is_never_displaced(self, socket_dir: Path) -> None:
        async def scenario() -> None:
            first = await serving(socket_dir, StubPlane())
            try:
                await serving(socket_dir, StubPlane())
            finally:
                await first.aclose()

        with pytest.raises(AlreadyServing):
            asyncio.run(scenario())

    def test_debris_from_a_dead_engine_is_taken_over(self, socket_dir: Path) -> None:
        debris = socket_dir / "control.sock"
        dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        dead.bind(str(debris))
        dead.close()

        async def scenario() -> Reply:
            server = await serving(socket_dir, StubPlane())
            try:
                return await ask(Request(action=Action.STATUS), path=server.path)
            finally:
                await server.aclose()

        assert asyncio.run(scenario()).ok

    def test_a_socket_owned_by_another_user_is_never_unlinked(self, socket_dir: Path) -> None:
        """Debris is deleted; another account's socket is refused."""
        theirs = socket_dir / "control.sock"
        theirs.touch()

        async def scenario() -> None:
            await serving(socket_dir, StubPlane(), owner_of=lambda path: os.geteuid() + 1)

        with pytest.raises(PermissionError):
            asyncio.run(scenario())
        assert theirs.exists()


class TestAPathThatCannotBeBound:
    def test_a_path_past_the_platform_limit_is_refused_in_words(self, socket_dir: Path) -> None:
        """Otherwise this arrives as an errno from inside asyncio, at install time."""
        too_long = socket_dir / ("x" * 120) / "control.sock"

        with pytest.raises(SocketPathTooLong):
            asyncio.run(ControlPlaneServer(plane=StubPlane(), path=too_long).start())


class TestTheDeadlineIsReadOffTheAction:
    """`ask` takes its deadline from the action, never from the call site.

    #28 was a launch held to an ordinary action's patience, reported as a
    failure while it was in fact succeeding. Launch is parked, so every action
    left answers from state the hub already holds and one budget covers them
    all — but the *shape* of the fix is what stops #28 coming back: a caller
    that does not think about the deadline still gets the action's own one, so
    the next action that needs its own number only has to be given one in
    `timeout_for`.
    """

    def test_every_action_carries_a_deadline(self) -> None:
        for action in Action:
            assert timeout_for(action) == DEFAULT_TIMEOUT_SECONDS

    def test_a_client_that_names_no_deadline_gets_the_actions_own(self, socket_dir: Path) -> None:
        recorded: list[float] = []
        real = asyncio.timeout

        def watch(seconds: float) -> object:
            recorded.append(seconds)
            return real(seconds)

        async def scenario() -> None:
            server = await serving(socket_dir, StubPlane())
            try:
                with pytest.MonkeyPatch.context() as patched:
                    patched.setattr(asyncio, "timeout", watch)
                    for action in (Action.RELAY, Action.STATUS):
                        await ask(Request(action=action), path=server.path)
            finally:
                await server.aclose()

        asyncio.run(scenario())

        assert recorded == [timeout_for(Action.RELAY), timeout_for(Action.STATUS)]

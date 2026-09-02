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
)
from gpt_voicecoding.control_plane.ownership import SOCKET_MODE, SocketPathTooLong
from gpt_voicecoding.control_plane.server import AlreadyServing, ControlPlaneServer
from gpt_voicecoding.seams.control_plane import (
    MAX_REQUEST_BYTES,
    PROTOCOL_VERSION,
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
                for action in (Action.STATUS, Action.BRIEF, Action.VERIFY):
                    writer.write(json.dumps(Request(action=action).as_document()).encode() + b"\n")
                    await writer.drain()
                    assert Reply.of(json.loads(await reader.readline())).action is action
                writer.close()
                await writer.wait_closed()
            finally:
                await server.aclose()

        asyncio.run(scenario())
        assert plane.handled == [Action.STATUS, Action.BRIEF, Action.VERIFY]

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

    def test_a_reply_larger_than_the_bound_is_replaced_by_a_bounded_server_refusal(
        self, socket_dir: Path
    ) -> None:
        """The final outbound guard never writes a line a conforming client cannot read."""

        class Flood:
            async def handle(self, request: Request) -> Reply:
                return Reply.answered(request.action, {"x": '雪"\\' * 1_024})

        async def scenario() -> bytes:
            server = ControlPlaneServer(
                plane=Flood(),
                path=socket_dir / "control.sock",
                max_bytes=512,
            )
            await server.start()
            try:
                asking = json.dumps(Request(action=Action.STATUS).as_document()).encode() + b"\n"
                return await raw(server.path, asking)
            finally:
                await server.aclose()

        answer = asyncio.run(scenario())
        reply = Reply.of(json.loads(answer))

        assert answer.endswith(b"\n")
        assert len(answer) <= 512
        assert reply.error is not None
        assert reply.error.code is ErrorCode.REFUSED

    def test_the_physical_reply_declares_the_current_protocol(self, socket_dir: Path) -> None:
        async def scenario() -> bytes:
            server = await serving(socket_dir, StubPlane())
            try:
                asking = json.dumps(Request(action=Action.STATUS).as_document()).encode() + b"\n"
                return await raw(server.path, asking)
            finally:
                await server.aclose()

        answer = asyncio.run(scenario())

        assert answer.endswith(b"\n")
        assert json.loads(answer)["protocol"] == PROTOCOL_VERSION


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


class TestTheDeadlineIsNeverLeftToTheCallSite:
    """`ask` supplies the deadline itself, never leaving it to the call site.

    #28 was a launch held to an ordinary action's patience, reported as a
    failure while it was in fact succeeding. Launch is parked, so every action
    left answers from state the hub already holds and one budget covers them
    all — but the half of the fix that stops #28 coming back is not the
    per-action number, it is that a caller which does not think about the
    deadline is still given one.
    """

    def test_a_client_that_names_no_deadline_is_given_one(self, socket_dir: Path) -> None:
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

        assert recorded == [DEFAULT_TIMEOUT_SECONDS, DEFAULT_TIMEOUT_SECONDS]


class TestStoppingWithASurfaceStillConnected:
    """#96: a peer that is merely connected may not hold the engine open.

    Since Python 3.12 `asyncio.Server.wait_closed()` waits for every live
    connection handler, and this server's handler reads until the peer goes
    away — so one idle surface pinned `aclose` forever. It was not a slow
    shutdown but an unbounded one, and because the control plane closes before
    the adapters, the Codex app-server the engine had spawned was then never
    terminated: the engine outlived SIGTERM, was SIGKILLed, and left a process
    holding the socket that refused the acceptance run after it.

    The two states a handler can be in are covered separately, because the
    right answer differs: waiting for the next request means stop now, and
    writing a reply means finish it first.
    """

    def test_an_idle_peer_does_not_hold_the_stop_open(self, socket_dir: Path) -> None:
        async def scenario() -> None:
            server = await serving(socket_dir, StubPlane())
            reader, writer = await asyncio.open_unix_connection(str(server.path))
            try:
                # One exchange, so this is a real surface rather than a socket
                # nobody ever spoke on — and then silence, which is exactly what
                # a surface between commands looks like.
                writer.write(json.dumps(Request(action=Action.STATUS).as_document()).encode())
                writer.write(b"\n")
                await writer.drain()
                assert Reply.of(json.loads(await reader.readline())).ok
                async with asyncio.timeout(5):
                    await server.aclose()
            finally:
                writer.close()

        asyncio.run(scenario())

    def test_a_reply_being_written_is_finished_before_the_stop(self, socket_dir: Path) -> None:
        """A stop that cut a reply in half would answer a surface with a hole."""
        held = Held(Action.RELAY)
        plane = StubPlane(held=held)

        async def scenario() -> Reply:
            server = await serving(socket_dir, plane)
            reader, writer = await asyncio.open_unix_connection(str(server.path))
            try:
                writer.write(json.dumps(Request(action=Action.RELAY).as_document()).encode())
                writer.write(b"\n")
                await writer.drain()
                await asyncio.wait_for(held.reached.wait(), 5)

                # The stop begins while the handler is inside `handle`, which is
                # the moment the old code had no way to distinguish from idle.
                stopping = asyncio.ensure_future(server.aclose())
                await asyncio.sleep(0)
                held.release.set()
                async with asyncio.timeout(5):
                    answer = await reader.readline()
                    await stopping
                return Reply.of(json.loads(answer))
            finally:
                held.release.set()
                writer.close()

        assert asyncio.run(scenario()).action is Action.RELAY

    def test_the_stop_says_which_surfaces_were_still_connected(
        self, socket_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The one fact #96's artifacts could not recover, written down at the time."""

        async def scenario() -> None:
            server = await serving(socket_dir, StubPlane())
            reader, writer = await asyncio.open_unix_connection(str(server.path))
            try:
                writer.write(json.dumps(Request(action=Action.STATUS).as_document()).encode())
                writer.write(b"\n")
                await writer.drain()
                await reader.readline()
                with caplog.at_level("INFO", logger="gpt_voicecoding.control_plane.server"):
                    # Bounded like every other stop here: a test that hangs
                    # reads as `pending` and holds a macOS runner (#65).
                    async with asyncio.timeout(5):
                        await server.aclose()
            finally:
                writer.close()

        asyncio.run(scenario())

        stopped = [line for line in caplog.messages if "stopping the control plane" in line]
        assert stopped, caplog.messages
        # This process is on both ends of the socket, so the pid it names is
        # this one — which is what makes the reading verifiable at all.
        assert f"pid {os.getpid()}" in stopped[0]

    def test_a_server_that_stopped_may_serve_again(self, socket_dir: Path) -> None:
        """The stop is a state, so it has to be left as well as entered."""

        async def scenario() -> Reply:
            server = await serving(socket_dir, StubPlane())
            await server.aclose()
            await server.start()
            try:
                return await ask(Request(action=Action.STATUS), path=server.path, timeout=5)
            finally:
                await server.aclose()

        assert asyncio.run(scenario()).ok


def test_the_control_socket_is_private_from_the_moment_it_exists(
    socket_dir: Path, mode_at_bind: dict[str, int]
) -> None:
    """#116: bound through `start_private_unix_server`, so never wide even briefly."""

    async def scenario() -> Path:
        server = await serving(socket_dir, StubPlane())
        try:
            return server._path  # noqa: SLF001 - the premise is about this exact file
        finally:
            await server.aclose()

    path = asyncio.run(scenario())

    assert oct(mode_at_bind[str(path)]) == oct(SOCKET_MODE)

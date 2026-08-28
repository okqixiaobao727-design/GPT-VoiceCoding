"""Owning one app-server, and attaching to ones this engine does not own.

The distinction these tests protect is the topology decision behind the whole
adapter: the engine spawns exactly one app-server — the one the Call seam and
the Delegated Turn ride — and never the one a user's Codex TUI is a thin client
of. If that inverted, restarting the bridge would close every coding session the
user has open.

No real codex runs, but a real *process* does. The stand-in is a small script
that binds the socket it was told to bind and speaks the server half of the
protocol, because the ordering these tests are about — spawn, wait for the
socket, connect, reap — cannot be observed without a child that really appears
and really has to be cleaned up.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import inspect
import itertools
import os
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from codex_fake import FakeAppServer
from gpt_voicecoding.adapters.codex_app_server import process
from gpt_voicecoding.adapters.codex_app_server.process import (
    CLIENT_NAME,
    REALTIME_FEATURE,
    AppServerError,
    OwnedAppServer,
    attach,
    prepare_private_directory,
    verify_private_directory,
    verify_private_socket,
)
from gpt_voicecoding.adapters.codex_app_server.settings import CodexSettings
from gpt_voicecoding.private_socket import PRIVATE_SOCKET_MODE, PRIVATE_SOCKET_UMASK

_names = itertools.count()

#: A stand-in for `codex app-server`: binds `--listen unix://PATH` and answers
#: `initialize`. Small on purpose — it exists to be spawned and reaped.
STAND_IN = """\
import asyncio, sys
sys.path.insert(0, {tests!r})
from pathlib import Path
from codex_fake import FakeAppServer

async def main() -> None:
    listen = [a for a in sys.argv if a.startswith("unix://")][0]
    server = FakeAppServer(Path(listen[len("unix://"):]))
    server.answers("initialize", {{"codexHome": "/somewhere"}})
    await server.start()
    await asyncio.Event().wait()

asyncio.run(main())
"""

#: A child that first binds an ordinary Unix socket and records its mode before
#: anything can narrow it, then becomes the ordinary app-server stand-in. The
#: probe is separate from the server socket so the parent's readiness poll can
#: never race a bind-close-rebind sequence at the path it will attach to.
MODE_RECORDING_STAND_IN = """\
import asyncio, os, socket, stat, sys
sys.path.insert(0, {tests!r})
from pathlib import Path
from codex_fake import FakeAppServer

async def main() -> None:
    listen = [a for a in sys.argv if a.startswith("unix://")][0]
    socket_path = Path(listen[len("unix://"):])
    probe_path = socket_path.parent / "mode-at-bind.sock"
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.bind(str(probe_path))
        mode = stat.S_IMODE(os.stat(probe_path).st_mode)
        Path({mode_report!r}).write_text(f"{{mode:04o}}")
    finally:
        probe.close()
        probe_path.unlink(missing_ok=True)

    server = FakeAppServer(socket_path)
    server.answers("initialize", {{"codexHome": "/somewhere"}})
    await server.start()
    await asyncio.Event().wait()

asyncio.run(main())
"""

#: The same, except it keeps talking. Two components share the engine's own
#: app-server, so what matters here is that both of them hear it.
CHATTY = """\
import asyncio, sys
sys.path.insert(0, {tests!r})
from pathlib import Path
from codex_fake import FakeAppServer

async def main() -> None:
    listen = [a for a in sys.argv if a.startswith("unix://")][0]
    server = FakeAppServer(Path(listen[len("unix://"):]))
    server.answers("initialize", {{}})
    await server.start()
    while True:
        await asyncio.sleep(0.02)
        await server.notify_all("thread/status/changed", {{"threadId": "t-1"}})

asyncio.run(main())
"""


def stand_in(tmp_path: Path, *, body: str | None = None) -> str:
    """An executable that plays the part of ``codex`` for one test."""
    where = tmp_path / "codex"
    script = body if body is not None else STAND_IN.format(tests=str(Path(__file__).parent))
    if body is None:
        runner = tmp_path / "stand_in.py"
        runner.write_text(script)
        where.write_text(f'#!/bin/sh\nexec {sys.executable} "{runner}" "$@"\n')
    else:
        where.write_text(f"#!/bin/sh\n{script}\n")
    where.chmod(where.stat().st_mode | stat.S_IXUSR)
    return str(where)


def mode_recording_stand_in(tmp_path: Path, mode_report: Path) -> str:
    """A real child that reports the mode its inherited umask creates."""
    runner = tmp_path / "mode_recording_stand_in.py"
    runner.write_text(
        MODE_RECORDING_STAND_IN.format(
            tests=str(Path(__file__).parent), mode_report=str(mode_report)
        )
    )
    return stand_in(tmp_path, body=f'exec {sys.executable} "{runner}" "$@"')


@pytest.fixture
def socket_path() -> Iterator[Path]:
    """A private directory, under a root short enough to bind.

    Darwin caps an ``AF_UNIX`` path at 103 bytes, so it cannot live under
    pytest's ``tmp_path``; and it needs a directory of its own because the
    adapter refuses to put a coding session's socket anywhere every account on
    the machine can walk into.
    """
    home = Path("/tmp") / f"vc-proc-{next(_names)}-{id(object())}"
    home.mkdir(mode=0o700)
    yield home / "app-server.sock"
    shutil.rmtree(home, ignore_errors=True)


def quick(**overrides: object) -> CodexSettings:
    return CodexSettings(  # type: ignore[arg-type]
        startup_timeout_seconds=10.0, request_timeout_seconds=5.0, **overrides
    )


async def _until(condition, timeout: float = 2.0) -> None:
    """Wait for something the far side will do, or fail the test saying so."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if condition():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("the far side never got there")


def running(pid: int) -> bool:
    """Whether that process is still there. A reaped child answers ESRCH."""
    try:
        os.kill(pid, 0)
    except OSError as gone:
        return gone.errno != errno.ESRCH
    return True


class TestAttachingToSomebodyElsesServer:
    def test_attaching_initialises_and_says_who_holds_the_connection(
        self, socket_path: Path
    ) -> None:
        """The far side records the client name, so it must name this software."""

        async def scenario() -> list[str]:
            async with FakeAppServer(socket_path) as server:
                server.answers("initialize", {"codexHome": "/somewhere"})
                connection = await attach(socket_path, version="1.2.3", settings=quick())
                try:
                    initialised = server.calls_to("initialize")[0]
                    assert initialised["clientInfo"]["name"] == CLIENT_NAME
                    assert initialised["clientInfo"]["version"] == "1.2.3"
                    # `initialized` is a notification, so nothing waits for it
                    # here the way a request would.
                    await _until(lambda: len(server.calls) == 2)
                    return [call.method for call in server.calls]
                finally:
                    await connection.aclose()

        assert asyncio.run(scenario()) == ["initialize", "initialized"]

    def test_a_watching_connection_asks_for_no_experimental_surface(
        self, socket_path: Path
    ) -> None:
        """A connection that only watches a Session must not claim what it will not use."""

        async def scenario() -> dict:
            async with FakeAppServer(socket_path) as server:
                server.answers("initialize", {})
                connection = await attach(socket_path, version="1", settings=quick())
                try:
                    return server.calls_to("initialize")[0]
                finally:
                    await connection.aclose()

        assert asyncio.run(scenario())["capabilities"] == {"experimentalApi": False}

    def test_attaching_has_no_spawn_path_at_all(self) -> None:
        """The user's app-server is theirs; this engine is only ever a guest on it.

        Asserted against the source rather than by counting processes, because
        the guarantee is structural: there is no branch in `attach` that could
        start one, so there is no state in which it does.
        """
        assert "Popen" not in inspect.getsource(process.attach)
        assert "Popen" not in inspect.getsource(process.initialise)


class TestOwningOne:
    def test_the_child_exposes_no_socket_permissions_to_other_accounts(
        self, tmp_path: Path, socket_path: Path
    ) -> None:
        """The spawned app-server must never expose a pre-chmod permission window."""
        mode_report = tmp_path / "mode-at-bind.txt"

        async def scenario() -> str:
            owned = OwnedAppServer(
                settings=quick(executable=mode_recording_stand_in(tmp_path, mode_report)),
                socket_path=socket_path,
            )
            try:
                await owned.start()
                return mode_report.read_text()
            finally:
                await owned.aclose()

        mode_at_bind = int(asyncio.run(scenario()), 8)
        assert mode_at_bind & PRIVATE_SOCKET_UMASK == 0

    def test_the_spawn_passes_the_private_umask_to_popen(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        socket_path: Path,
    ) -> None:
        """The child receives the mask before exec, rather than chmodding after bind."""
        spawned_with: list[dict[str, object]] = []
        popen = subprocess.Popen

        def recording_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
            spawned_with.append(kwargs)
            return popen(*args, **kwargs)  # type: ignore[arg-type, return-value]

        monkeypatch.setattr(subprocess, "Popen", recording_popen)

        async def scenario() -> None:
            owned = OwnedAppServer(
                settings=quick(executable=stand_in(tmp_path)), socket_path=socket_path
            )
            try:
                await owned.start()
            finally:
                await owned.aclose()

        asyncio.run(scenario())

        assert spawned_with[0]["umask"] == PRIVATE_SOCKET_UMASK & 0o777

    def test_a_started_server_is_spawned_connected_and_experimental(
        self, tmp_path: Path, socket_path: Path
    ) -> None:
        """The engine's own server rides the realtime route, which is capability-gated."""

        async def scenario() -> tuple[bool, int]:
            owned = OwnedAppServer(
                settings=quick(executable=stand_in(tmp_path)),
                socket_path=socket_path,
                version="9",
            )
            try:
                connection = await owned.start()
                answer = await connection.request("thread/loaded/list", {})
                assert answer == {}
                assert owned.is_running
                assert owned.connection is connection
                assert owned.socket_path == socket_path
                child = owned._process
                assert child is not None
                return True, child.pid
            finally:
                await owned.aclose()

        started, pid = asyncio.run(scenario())
        assert started
        assert not running(pid), "the app-server outlived the engine that owned it"

    def test_every_listener_hears_it(self, tmp_path: Path, socket_path: Path) -> None:
        """One connection, one handler — but two components ride this process.

        The Agent seam's adapter owns the app-server and the Call seam's adapter
        rides it, so a fan-out that let only the owner listen would mean the
        Call adapter could never see a realtime notification at all.
        """
        runner = tmp_path / "chatty.py"
        runner.write_text(CHATTY.format(tests=str(Path(__file__).parent)))

        async def scenario() -> tuple[list[object], list[object]]:
            owned = OwnedAppServer(
                settings=quick(
                    executable=stand_in(tmp_path, body=f'exec {sys.executable} "{runner}" "$@"')
                ),
                socket_path=socket_path,
            )
            first: list[object] = []
            second: list[object] = []
            owned.listen(first.append)
            try:
                await owned.start(on_notification=second.append)
                await _until(lambda: bool(first) and bool(second))
            finally:
                await owned.aclose()
            return first, second

        first, second = asyncio.run(scenario())
        assert first and second, "a shared app-server that only one component hears"

    def test_a_listener_that_raises_does_not_silence_the_others(
        self, tmp_path: Path, socket_path: Path
    ) -> None:
        runner = tmp_path / "chatty.py"
        runner.write_text(CHATTY.format(tests=str(Path(__file__).parent)))

        async def scenario() -> list[object]:
            owned = OwnedAppServer(
                settings=quick(
                    executable=stand_in(tmp_path, body=f'exec {sys.executable} "{runner}" "$@"')
                ),
                socket_path=socket_path,
            )

            def objects(_message: object) -> None:
                raise RuntimeError("this listener is broken")

            heard: list[object] = []
            owned.listen(objects)
            owned.listen(heard.append)
            try:
                await owned.start()
                await _until(lambda: bool(heard))
            finally:
                await owned.aclose()
            return heard

        assert asyncio.run(scenario())

    def test_it_enables_the_realtime_feature(self, tmp_path: Path, socket_path: Path) -> None:
        """`experimentalApi` at initialize is not enough; the family must be on.

        The two are different things and the difference cost a real smoke run.
        Verified against codex 0.148.0 directly: `experimentalFeature/list`
        reports `realtime_conversation` as `enabled: false` without this flag and
        `enabled: true` with it. Without it `thread/realtime/start` is accepted
        and then refused with "thread ... does not support realtime
        conversation" — a per-thread-sounding message for a per-process cause,
        which is exactly the kind of thing nobody diagnoses twice cheaply.
        """
        argv = tmp_path / "argv.txt"
        runner = tmp_path / "recorded.py"
        runner.write_text(STAND_IN.format(tests=str(Path(__file__).parent)))
        recording = f'printf "%s\n" "$@" > "{argv}"\nexec {sys.executable} "{runner}" "$@"'

        async def scenario() -> None:
            owned = OwnedAppServer(
                settings=quick(executable=stand_in(tmp_path, body=recording)),
                socket_path=socket_path,
            )
            try:
                await owned.start()
            finally:
                await owned.aclose()

        asyncio.run(scenario())

        assert argv.read_text().split() == [
            "app-server",
            "--enable",
            REALTIME_FEATURE,
            "--listen",
            f"unix://{socket_path}",
        ]

    def test_starting_twice_returns_the_same_connection(
        self, tmp_path: Path, socket_path: Path
    ) -> None:
        async def scenario() -> bool:
            owned = OwnedAppServer(
                settings=quick(executable=stand_in(tmp_path)), socket_path=socket_path
            )
            try:
                return await owned.start() is await owned.start()
            finally:
                await owned.aclose()

        assert asyncio.run(scenario()) is True

    def test_closing_twice_never_raises_and_removes_the_socket(
        self, tmp_path: Path, socket_path: Path
    ) -> None:
        async def scenario() -> tuple[bool, bool]:
            owned = OwnedAppServer(
                settings=quick(executable=stand_in(tmp_path)), socket_path=socket_path
            )
            await owned.start()
            await owned.aclose()
            await owned.aclose()
            return owned.is_running, socket_path.exists()

        is_running, left_behind = asyncio.run(scenario())
        assert is_running is False
        assert left_behind is False

    def test_a_process_that_dies_during_startup_is_noticed_not_waited_out(
        self, tmp_path: Path, socket_path: Path
    ) -> None:
        """Otherwise a broken install costs the whole startup timeout to discover."""

        async def scenario() -> None:
            owned = OwnedAppServer(
                settings=CodexSettings(
                    executable=stand_in(tmp_path, body="exit 3"),
                    # Deliberately long: the point is that it is not spent.
                    startup_timeout_seconds=60.0,
                ),
                socket_path=socket_path,
            )
            with pytest.raises(AppServerError, match="exited during startup with 3"):
                await owned.start()
            assert not owned.is_running

        asyncio.run(asyncio.wait_for(scenario(), 15))

    def test_a_server_that_never_binds_gives_up_naming_the_socket(
        self, tmp_path: Path, socket_path: Path
    ) -> None:
        async def scenario() -> None:
            owned = OwnedAppServer(
                settings=CodexSettings(
                    executable=stand_in(tmp_path, body="sleep 30"),
                    startup_timeout_seconds=0.3,
                ),
                socket_path=socket_path,
            )
            with pytest.raises(AppServerError, match=str(socket_path)):
                await owned.start()
            assert not owned.is_running

        asyncio.run(scenario())

    def test_a_missing_executable_says_what_is_missing(self, socket_path: Path) -> None:
        async def scenario() -> None:
            owned = OwnedAppServer(
                settings=quick(executable="definitely-not-a-real-binary-xyz"),
                socket_path=socket_path,
            )
            with pytest.raises(AppServerError, match="on PATH"):
                await owned.start()

        asyncio.run(scenario())

    def test_its_output_goes_to_the_log_it_was_given(
        self, tmp_path: Path, socket_path: Path
    ) -> None:
        """A pipe nobody drains eventually blocks the child, so there is never one."""
        log = tmp_path / "logs" / "codex-app-server.log"

        async def scenario() -> str:
            owned = OwnedAppServer(
                # This one never binds, so `start` will eventually give up. The
                # wait below is what the assertion depends on, not that timeout:
                # tying them together made this test fail whenever the machine
                # was slow enough that SIGTERM beat the child's first write.
                settings=CodexSettings(
                    executable=stand_in(tmp_path, body="echo hello; sleep 30"),
                    startup_timeout_seconds=30.0,
                ),
                socket_path=socket_path,
                log_path=log,
            )
            starting = asyncio.ensure_future(owned.start())
            try:
                await _until(lambda: log.exists() and log.read_text().strip() != "")
                return log.read_text().strip()
            finally:
                starting.cancel()
                with contextlib.suppress(asyncio.CancelledError, AppServerError):
                    await starting
                await owned.aclose()

        assert asyncio.run(scenario()) == "hello"


class TestClaimingTheSocketPath:
    def _owned(self, tmp_path: Path, path: Path) -> OwnedAppServer:
        return OwnedAppServer(
            settings=quick(executable=stand_in(tmp_path, body="sleep 30")), socket_path=path
        )

    def test_a_socket_somebody_is_listening_on_is_never_stolen(
        self, tmp_path: Path, socket_path: Path
    ) -> None:
        """Unlinking a live socket is how one install silently disconnects another."""

        async def scenario() -> None:
            async with FakeAppServer(socket_path):
                with pytest.raises(AppServerError, match="already listening"):
                    self._owned(tmp_path, socket_path)._claim_socket_path()
                assert socket_path.exists()

        asyncio.run(scenario())

    def test_a_stale_socket_from_a_dead_run_is_cleared(
        self, tmp_path: Path, socket_path: Path
    ) -> None:
        import socket as _socket

        listener = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        listener.bind(str(socket_path))
        listener.close()
        assert socket_path.exists()

        self._owned(tmp_path, socket_path)._claim_socket_path()
        assert not socket_path.exists()

    def test_something_that_is_not_a_socket_is_refused_rather_than_removed(
        self, tmp_path: Path
    ) -> None:
        """A path holding a real file holds somebody's data, whoever they are."""
        occupied = tmp_path / "not-a-socket"
        occupied.write_text("someone's file")

        with pytest.raises(AppServerError, match="is not a socket"):
            self._owned(tmp_path, occupied)._claim_socket_path()
        assert occupied.read_text() == "someone's file"

    def test_a_free_path_is_simply_taken(self, tmp_path: Path, socket_path: Path) -> None:
        self._owned(tmp_path, socket_path)._claim_socket_path()
        assert not socket_path.exists()


class TestRefusingAPathSubstitution:
    """A shared runtime root is somewhere anyone can plant a name.

    `stat` follows symbolic links, so a link planted in `/tmp` and aimed at
    something this user really does own passes every ownership and mode check
    while the endpoint actually used belongs to whoever made the link. These
    are the tests that fail if the checks ever go back to following them.
    """

    def test_a_socket_reached_through_a_symlink_is_refused(self, socket_path: Path) -> None:
        async def scenario() -> None:
            async with FakeAppServer(socket_path):
                planted = socket_path.parent / "planted.sock"
                planted.symlink_to(socket_path)
                # Everything about the target is correct; only the path is a lie.
                with pytest.raises(AppServerError, match="symbolic link"):
                    verify_private_socket(planted)

        asyncio.run(scenario())

    def test_a_socket_in_a_directory_anyone_can_enter_is_refused(self, socket_path: Path) -> None:
        """The directory is the stronger half: 0600 means nothing in a shared root."""

        async def scenario() -> None:
            async with FakeAppServer(socket_path):
                socket_path.parent.chmod(0o755)
                try:
                    with pytest.raises(AppServerError, match="reachable by other accounts"):
                        verify_private_socket(socket_path)
                finally:
                    socket_path.parent.chmod(0o700)

        asyncio.run(scenario())

    def test_a_directory_reached_through_a_symlink_is_refused(
        self, tmp_path: Path, socket_path: Path
    ) -> None:
        pointing = tmp_path / "elsewhere"
        pointing.symlink_to(socket_path.parent)
        with pytest.raises(AppServerError, match="symbolic link"):
            verify_private_directory(pointing)

    def test_preparing_a_directory_that_is_a_symlink_is_refused(
        self, tmp_path: Path, socket_path: Path
    ) -> None:
        """`mkdir(exist_ok=True)` succeeds against a link to a directory."""
        pointing = tmp_path / "elsewhere"
        pointing.symlink_to(socket_path.parent)
        with pytest.raises(AppServerError, match="symbolic link"):
            prepare_private_directory(pointing)

    def test_binding_through_a_planted_symlink_is_refused(
        self, tmp_path: Path, socket_path: Path
    ) -> None:
        """Claiming a path must not follow a link either, or it binds elsewhere."""
        planted = socket_path.parent / "planted.sock"
        planted.symlink_to(socket_path.parent / "somewhere-else.sock")
        owned = OwnedAppServer(
            settings=quick(executable=stand_in(tmp_path, body="sleep 30")),
            socket_path=planted,
        )
        with pytest.raises(AppServerError, match="symbolic link"):
            owned._claim_socket_path()
        assert planted.is_symlink(), "the link is somebody else's; it is refused, not removed"

    def test_a_private_directory_and_socket_are_accepted(self, socket_path: Path) -> None:
        """The check has to pass for the ordinary case, or it is just an outage."""

        async def scenario() -> None:
            async with FakeAppServer(socket_path):
                verify_private_directory(socket_path.parent)
                verify_private_socket(socket_path)

        asyncio.run(scenario())

    def test_a_socket_with_a_special_mode_bit_is_refused(self, socket_path: Path) -> None:
        async def scenario() -> None:
            async with FakeAppServer(socket_path):
                socket_path.chmod(PRIVATE_SOCKET_MODE | stat.S_ISUID)
                with pytest.raises(AppServerError, match="reachable by other accounts"):
                    verify_private_socket(socket_path)

        asyncio.run(scenario())


#: A stand-in for the npm shim `codex` really is on this machine: a wrapper that
#: does **not** exec, so the process holding the socket is a *grandchild* of the
#: engine, and one that survives SIGTERM, so the fallback is the path under test.
#: `trap 'sleep 30' TERM` is deliberately a non-empty trap: an empty one would be
#: inherited by the child as SIG_IGN and the child would survive SIGTERM too,
#: which is a different shim from the one measured (`@openai/codex/bin/codex.js`
#: forwards SIGTERM and cannot forward SIGKILL).
SHIM = """\
trap 'sleep 30' TERM
{python} {runner} "$@" &
echo $! > {pidfile}
wait
"""


def shim(tmp_path: Path, pidfile: Path) -> str:
    """A `codex` whose real server is its child, and which will not die politely."""
    runner = tmp_path / "stand_in.py"
    runner.write_text(STAND_IN.format(tests=str(Path(__file__).parent)))
    where = tmp_path / "codex"
    where.write_text(
        "#!/bin/sh\n" + SHIM.format(python=sys.executable, runner=str(runner), pidfile=str(pidfile))
    )
    where.chmod(where.stat().st_mode | stat.S_IXUSR)
    return str(where)


class TestStoppingTheWholeTreeItSpawned:
    """#96: nothing this engine spawned may outlive the engine holding the socket.

    `codex` on `PATH` is an npm shim — a `node` process whose child is the real
    binary. The stop used to signal only the process this engine holds, so the
    SIGKILL fallback killed the shim and reparented the binary to launchd, still
    listening on `codex-app-server.sock`. The next engine's `_claim_socket_path`
    then refused to start, correctly and unhelpfully, and that is what ended the
    acceptance run after the one that leaked it.
    """

    def test_the_process_holding_the_socket_goes_with_the_shim(
        self, tmp_path: Path, socket_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pidfile = tmp_path / "server.pid"
        # Bounded well under the real one so the fallback is reached in a test's
        # patience rather than in a shutdown's. The path is the point, not the wait.
        monkeypatch.setattr(process, "STOP_TIMEOUT_SECONDS", 1.0)

        async def scenario() -> int:
            owned = OwnedAppServer(
                settings=quick(executable=shim(tmp_path, pidfile)), socket_path=socket_path
            )
            await owned.start()
            await _until(lambda: pidfile.exists() and pidfile.read_text().strip())
            server_pid = int(pidfile.read_text().strip())
            assert running(server_pid)
            await owned.aclose()
            return server_pid

        server_pid = asyncio.run(scenario())
        # The grandchild had to be reaped by somebody; give the kernel the
        # moment it needs rather than racing it.
        deadline = time.monotonic() + 5
        while running(server_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not running(server_pid), (
            f"pid {server_pid} outlived the engine that spawned it and is still "
            f"holding {socket_path}"
        )

    def test_the_spawn_takes_a_session_of_its_own(self, tmp_path: Path, socket_path: Path) -> None:
        """Which is what makes signalling the group reach the tree and nothing else.

        Without it the group is the *engine's* group, and killing it would take
        down whatever started the engine — so this is a safety property as much
        as a reach one.
        """

        async def scenario() -> tuple[int, int]:
            owned = OwnedAppServer(
                settings=quick(executable=stand_in(tmp_path)), socket_path=socket_path
            )
            await owned.start()
            try:
                spawned = owned._process  # noqa: SLF001 - the pid is not otherwise exposed
                assert spawned is not None
                return os.getpgid(spawned.pid), os.getpgid(os.getpid())
            finally:
                await owned.aclose()

        theirs, ours = asyncio.run(scenario())
        assert theirs != ours


#: A shim that starts the real server, records it, and then leaves — the case a
#: shim crashing, being OOM-killed, or simply exiting first produces. The wait is
#: only long enough that `start` completes against a live leader; nothing in the
#: adapter depends on how long it lives after that.
DYING_SHIM = """\
{python} {runner} "$@" &
echo $! > {pidfile}
sleep 1
"""


def dying_shim(tmp_path: Path, pidfile: Path) -> str:
    """A `codex` that hands the socket to a child and then dies itself."""
    runner = tmp_path / "stand_in.py"
    runner.write_text(STAND_IN.format(tests=str(Path(__file__).parent)))
    where = tmp_path / "codex"
    where.write_text(
        "#!/bin/sh\n"
        + DYING_SHIM.format(python=sys.executable, runner=str(runner), pidfile=str(pidfile))
    )
    where.chmod(where.stat().st_mode | stat.S_IXUSR)
    return str(where)


class TestAShimThatDiesBeforeItsChild:
    """#96: the group is signalled on its own account, not on the leader's.

    The first fix gated the whole signalling block on the *shim* still being
    alive, which is the one process that does not matter — the socket is held by
    its child. A shim that crashed, was OOM-killed, or was reaped by an earlier
    `poll()` meant `aclose` sent no signal at all and then unlinked the socket
    file out from under a process still bound to it, which is worse than the
    original leak: the next engine finds no file, so `_claim_socket_path` never
    runs its listener check and binds a fresh inode at the same name while the
    orphan keeps the old one.
    """

    def test_the_child_still_goes_when_the_shim_is_already_gone(
        self, tmp_path: Path, socket_path: Path
    ) -> None:
        pidfile = tmp_path / "server.pid"

        async def scenario() -> int:
            owned = OwnedAppServer(
                settings=quick(executable=dying_shim(tmp_path, pidfile)), socket_path=socket_path
            )
            await owned.start()
            await _until(lambda: pidfile.exists() and pidfile.read_text().strip())
            server_pid = int(pidfile.read_text().strip())

            leader = owned._process  # noqa: SLF001 - the premise is about this process
            assert leader is not None
            await _until(lambda: leader.poll() is not None, timeout=5.0)
            assert running(server_pid), "the child was meant to outlive its shim"

            await owned.aclose()
            return server_pid

        server_pid = asyncio.run(scenario())
        deadline = time.monotonic() + 5
        while running(server_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not running(server_pid), (
            f"pid {server_pid} outlived the engine because its shim died first, and is "
            f"still holding {socket_path}"
        )

    def test_the_socket_file_is_left_when_something_still_answers_on_it(
        self, socket_path: Path
    ) -> None:
        """The file is what makes the next engine refuse rather than shadow it.

        `_claim_socket_path` only reaches its listener check if the path exists.
        Removing a socket somebody is still bound to converts a loud refusal
        into two engines with one name, which is the failure mode #96's leak was
        *protecting* against.
        """

        async def scenario() -> bool:
            answering = await FakeAppServer(socket_path).start()
            try:
                owned = OwnedAppServer(
                    settings=quick(executable="/bin/true"), socket_path=socket_path
                )
                owned._owns_socket = True  # noqa: SLF001 - as if this run had bound it
                await owned.aclose()
                return socket_path.exists()
            finally:
                await answering.aclose()

        assert asyncio.run(scenario()) is True


#: A shim that behaves the way the real npm one does: it starts the binary, then
#: forwards SIGTERM to it and waits. Used to prove the *tree* case stops
#: promptly, not merely eventually.
FORWARDING_SHIM = """\
{python} {runner} "$@" &
child=$!
echo $child > {pidfile}
trap 'kill -TERM $child' TERM
wait $child
"""


def forwarding_shim(tmp_path: Path, pidfile: Path) -> str:
    """A `codex` shim that does its job: passes the signal on and goes."""
    runner = tmp_path / "stand_in.py"
    runner.write_text(STAND_IN.format(tests=str(Path(__file__).parent)))
    where = tmp_path / "codex"
    where.write_text(
        "#!/bin/sh\n"
        + FORWARDING_SHIM.format(python=sys.executable, runner=str(runner), pidfile=str(pidfile))
    )
    where.chmod(where.stat().st_mode | stat.S_IXUSR)
    return str(where)


class TestAnOrdinaryStopIsQuick:
    """A stop that always spends its whole budget is a bug, not a slow machine.

    Waiting on a *group* to empty and reaping the leader only afterwards did
    exactly that: a dead child nobody has waited on is a zombie, a zombie is
    still in its process group, so the wait never saw the group empty. Every
    stop cost `STOP_TIMEOUT_SECONDS` and then sent a pointless SIGKILL —
    measured at 6.03s against a real app-server that had gone in milliseconds,
    and invisible to every test here, because they all asked whether the
    process was gone and none asked how long it took.
    """

    #: What "prompt" means. Far below `STOP_TIMEOUT_SECONDS` so this can only
    #: fail on the bug rather than on a loaded machine, and far above the
    #: 0.05s poll so it is not a race with the interval itself.
    PROMPT_SECONDS = 2.0

    def test_a_server_that_takes_the_signal_stops_at_once(
        self, tmp_path: Path, socket_path: Path
    ) -> None:
        async def scenario() -> float:
            owned = OwnedAppServer(
                settings=quick(executable=stand_in(tmp_path)), socket_path=socket_path
            )
            await owned.start()
            began = time.monotonic()
            await owned.aclose()
            return time.monotonic() - began

        took = asyncio.run(scenario())
        assert took < self.PROMPT_SECONDS, (
            f"stopping took {took:.2f}s for a server that exits on SIGTERM; the stop is "
            f"spending its whole {process.STOP_TIMEOUT_SECONDS:.0f}s budget on every shutdown"
        )

    def test_a_shim_that_forwards_the_signal_stops_at_once_too(
        self, tmp_path: Path, socket_path: Path
    ) -> None:
        """The real topology: the socket is held by a grandchild, and both go."""
        pidfile = tmp_path / "server.pid"

        async def scenario() -> tuple[float, int]:
            owned = OwnedAppServer(
                settings=quick(executable=forwarding_shim(tmp_path, pidfile)),
                socket_path=socket_path,
            )
            await owned.start()
            await _until(lambda: pidfile.exists() and pidfile.read_text().strip())
            server_pid = int(pidfile.read_text().strip())
            began = time.monotonic()
            await owned.aclose()
            return time.monotonic() - began, server_pid

        took, server_pid = asyncio.run(scenario())
        assert took < self.PROMPT_SECONDS, f"stopping the tree took {took:.2f}s"
        assert not running(server_pid)


def test_the_fake_app_server_binds_as_privately_as_the_real_one(
    socket_path: Path, mode_at_bind: dict[str, int]
) -> None:
    """#116: the double is only honest while it has the property under inspection.

    `verify_private_socket` is the thing these tests exercise, so a fake that
    bound wide and narrowed afterwards left a window the adapter could land in —
    which is what turned a loaded runner red.
    """

    async def scenario() -> None:
        server = await FakeAppServer(socket_path).start()
        await server.aclose()

    asyncio.run(scenario())

    assert oct(mode_at_bind[str(socket_path)]) == oct(PRIVATE_SOCKET_MODE)

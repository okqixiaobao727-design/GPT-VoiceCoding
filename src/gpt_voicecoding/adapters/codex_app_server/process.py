"""The one app-server this engine spawns, and the client side of the ones it does not.

Two kinds of app-server exist in this system, and confusing them is the mistake
this module is shaped to prevent.

**The engine's own.** One process, spawned here, owned here, shut down here. It
is what the Call adapter's realtime route and the Delegated Turn ride on, which
is why it initialises with `experimentalApi` — the `thread/realtime/*` family is
gated behind that capability in codex 0.148.0. Nothing else spawns one: the Call
adapter consumes this component rather than starting a second server, which is
the single-ownership rule the Codex adapter issue fixes.

**A Session's own.** A Codex TUI is a thin client of an app-server
(`codex --remote unix://PATH`), so whoever owns that process owns the life of the
user's visible session. The engine therefore **never** owns one: the launch
wrapper spawns it in the user's own terminal and tells the engine where its
socket is, and this engine attaches as one more client. That is what keeps an
engine restart from taking every open Codex session down with it, and it is why
`attach` here spawns nothing at all.

Attaching is possible because of a fact established by probing codex 0.148.0
directly: one app-server accepts many concurrent clients, `thread/resume` against
a thread another live client already holds is non-destructive, and it subscribes
the resuming client to that thread's full turn and item stream.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import IO

from gpt_voicecoding.adapters.codex_app_server.settings import CodexSettings
from gpt_voicecoding.adapters.codex_app_server.wire import AppServerConnection, Message

#: What this client calls itself when it initialises. The far side records it,
#: so it says which software is holding the connection.
CLIENT_NAME = "gpt-voicecoding"


class AppServerError(Exception):
    """An app-server could not be started, or could not be reached."""


async def initialise(
    connection: AppServerConnection, *, experimental: bool, version: str
) -> Message:
    """Complete the app-server handshake on an open connection.

    `experimentalApi` is asked for only where it is needed. A connection that
    merely watches a user's Session has no business claiming a capability it
    will not use, and asking for less is what keeps a protocol change in the
    experimental surface from breaking the ordinary Relay path.
    """
    answer = await connection.request(
        "initialize",
        {
            "clientInfo": {"name": CLIENT_NAME, "title": CLIENT_NAME, "version": version},
            "capabilities": {"experimentalApi": experimental},
        },
    )
    await connection.notify("initialized", {})
    return answer


async def attach(
    socket_path: Path,
    *,
    version: str,
    settings: CodexSettings,
    on_notification: Callable[[Message], None] | None = None,
    on_server_request: Callable[[Message], Awaitable[None] | None] | None = None,
    experimental: bool = False,
) -> AppServerConnection:
    """Become one more client of an app-server somebody else owns.

    Spawns nothing and reaps nothing: the process on the other end of this
    socket belongs to the user's terminal, and this engine's only relationship
    to it is that it may talk to it while it is there.
    """
    connection = AppServerConnection(
        socket_path,
        on_notification=on_notification,
        on_server_request=on_server_request,
        request_timeout_seconds=settings.request_timeout_seconds,
    )
    await connection.connect()
    try:
        await initialise(connection, experimental=experimental, version=version)
    except BaseException:
        await connection.aclose()
        raise
    return connection


class OwnedAppServer:
    """The engine's own app-server: spawned, owned, and shut down in that order.

    Ownership is the whole point, so the failure paths get the attention. A
    start that gets partway leaves nothing running; a stop always closes the log
    handle even when the process objected to dying; and the socket file is
    removed only by the run that created it, because unlinking a socket some
    other process is listening on is how one install silently disconnects
    another.
    """

    def __init__(
        self,
        *,
        settings: CodexSettings,
        socket_path: Path,
        log_path: Path | None = None,
        version: str = "0",
    ) -> None:
        self._settings = settings
        self._socket_path = socket_path
        self._log_path = log_path
        self._version = version
        self._process: subprocess.Popen[bytes] | None = None
        self._log: IO[bytes] | None = None
        self._owns_socket = False
        self._connection: AppServerConnection | None = None

    @property
    def socket_path(self) -> Path:
        return self._socket_path

    @property
    def connection(self) -> AppServerConnection:
        """The one connection to it. Raises rather than opening a second."""
        if self._connection is None:
            raise AppServerError("the engine's own app-server is not running")
        return self._connection

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    async def start(
        self,
        *,
        on_notification: Callable[[Message], None] | None = None,
        on_server_request: Callable[[Message], Awaitable[None] | None] | None = None,
    ) -> AppServerConnection:
        """Spawn it, wait for its socket, and connect. Idempotent."""
        if self._connection is not None:
            return self._connection

        executable = shutil.which(self._settings.executable)
        if executable is None:
            raise AppServerError(
                f"no {self._settings.executable!r} on PATH: this adapter drives the codex "
                "CLI and cannot run without it"
            )
        self._claim_socket_path()
        self._socket_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            self._spawn(executable)
            await self._await_socket()
            self._connection = await attach(
                self._socket_path,
                version=self._version,
                settings=self._settings,
                on_notification=on_notification,
                on_server_request=on_server_request,
                # The engine's own server is the one the realtime route rides,
                # and that family is experimental-gated.
                experimental=True,
            )
        except BaseException:
            await self.aclose()
            raise
        return self._connection

    async def aclose(self) -> None:
        """Stop it. Idempotent, and never raises on a second call."""
        connection, self._connection = self._connection, None
        if connection is not None:
            with contextlib.suppress(Exception):
                await connection.aclose()

        process, self._process = self._process, None
        if process is not None and process.poll() is None:
            process.terminate()
            if not await self._waited_for(process):
                process.kill()
                await self._waited_for(process)

        log, self._log = self._log, None
        if log is not None:
            with contextlib.suppress(Exception):
                log.close()

        if self._owns_socket:
            self._socket_path.unlink(missing_ok=True)
            self._owns_socket = False

    def _spawn(self, executable: str) -> None:
        """Start the process, with its output going somewhere it cannot fill a pipe."""
        if self._log_path is not None:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log = self._log_path.open("ab")
            output: IO[bytes] | int = self._log
        else:
            # No log path means nowhere to put the output, and a pipe nobody
            # drains eventually blocks the child. Discard is the honest choice.
            output = subprocess.DEVNULL
        self._process = subprocess.Popen(
            [executable, "app-server", "--listen", f"unix://{self._socket_path}"],
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            env=dict(os.environ),
        )
        self._owns_socket = True

    async def _await_socket(self) -> None:
        """Wait for the socket, and notice a process that died instead of binding."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._settings.startup_timeout_seconds
        while loop.time() < deadline:
            process = self._process
            if process is None:
                raise AppServerError("the codex app-server was never started")
            if process.poll() is not None:
                raise AppServerError(
                    f"the codex app-server exited during startup with {process.returncode}"
                )
            if self._socket_path.is_socket():
                return
            await asyncio.sleep(0.05)
        raise AppServerError(
            f"the codex app-server did not create {self._socket_path} within "
            f"{self._settings.startup_timeout_seconds:.0f}s"
        )

    def _claim_socket_path(self) -> None:
        """Take the path only when nothing is listening on it."""
        path = self._socket_path
        if not path.exists():
            return
        if not path.is_socket():
            raise AppServerError(f"{path} exists and is not a socket; refusing to remove it")
        if _something_listens(path):
            raise AppServerError(f"a live process is already listening on {path}")
        path.unlink(missing_ok=True)

    async def _waited_for(self, process: subprocess.Popen[bytes]) -> bool:
        """Wait for a process to go, without blocking the whole event loop."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._settings.startup_timeout_seconds
        while loop.time() < deadline:
            if process.poll() is not None:
                return True
            await asyncio.sleep(0.05)
        return process.poll() is not None


def _something_listens(path: Path) -> bool:
    """Whether a live process answers on that socket. A refused dial means no."""
    import socket as _socket

    probe = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    probe.settimeout(0.5)
    try:
        probe.connect(str(path))
    except OSError:
        return False
    else:
        return True
    finally:
        probe.close()

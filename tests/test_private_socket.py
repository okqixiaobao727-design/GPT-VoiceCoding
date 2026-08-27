"""The one rule every socket this product listens on is bound by.

Each test here opens the umask all the way and then reads the mode in the moment
after the bind, because that is the only moment the defect is visible: a socket
bound wide and narrowed by a later chmod is 0600 by the time anyone ordinarily
looks, and was reachable by every account on the machine in between (#116).
"""

from __future__ import annotations

import asyncio
import itertools
import os
import shutil
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest

from gpt_voicecoding.private_socket import start_private_unix_server

_names = itertools.count()


@pytest.fixture
def socket_root() -> Iterator[Path]:
    """A short private root: Darwin caps an ``AF_UNIX`` path at 103 bytes."""
    home = Path("/tmp") / f"vc-priv-{next(_names)}-{id(object())}"
    home.mkdir(mode=0o700)
    yield home
    shutil.rmtree(home, ignore_errors=True)


async def _nothing(_reader: asyncio.StreamReader, _writer: asyncio.StreamWriter) -> None:
    """No test here sends a byte; the mode is settled before a byte could arrive."""


def _served(path: Path, *, mode: int) -> None:
    """Bind and close, so the recorded reading is the only thing left behind."""

    async def scenario() -> None:
        server = await start_private_unix_server(_nothing, path, mode=mode)
        server.close()
        await server.wait_closed()

    asyncio.run(scenario())


def test_the_socket_is_private_from_the_moment_it_exists(
    socket_root: Path, mode_at_bind: dict[str, int]
) -> None:
    """Read in the instant `bind` returned, which is where a chmod-after shows.

    Reading once the server is up would pass either way: that is late enough for
    a narrowing chmod to have already happened, and the whole defect is that the
    socket was reachable before it did.
    """
    path = socket_root / "s.sock"

    _served(path, mode=0o600)

    assert oct(mode_at_bind[str(path)]) == oct(0o600)


def test_the_mode_asked_for_is_the_mode_bound(
    socket_root: Path, mode_at_bind: dict[str, int]
) -> None:
    """`mode` is a parameter because three modules define their own; each passes its."""
    path = socket_root / "s.sock"

    _served(path, mode=0o660)

    assert oct(mode_at_bind[str(path)]) == oct(0o660)


def test_the_process_umask_is_given_back(socket_root: Path) -> None:
    """It is process-global: a bind that kept it would re-mode every later file."""
    path = socket_root / "s.sock"

    async def scenario() -> None:
        server = await start_private_unix_server(_nothing, path, mode=0o600)
        server.close()
        await server.wait_closed()

    before = os.umask(0o022)
    try:
        asyncio.run(scenario())
        after = os.umask(0o022)
    finally:
        os.umask(before)

    assert after == 0o022


def test_a_path_already_bound_raises_and_keeps_no_socket(socket_root: Path) -> None:
    """The failure path closes its own listener: the caller has nothing to close yet."""
    path = socket_root / "s.sock"

    async def scenario() -> None:
        first = await start_private_unix_server(_nothing, path, mode=0o600)
        try:
            with pytest.raises(OSError):
                await start_private_unix_server(_nothing, path, mode=0o600)
        finally:
            first.close()
            await first.wait_closed()

    asyncio.run(scenario())


def test_the_umask_is_given_back_even_when_the_bind_fails(socket_root: Path) -> None:
    """A refused bind that kept the mask would quietly re-mode the rest of the run."""

    async def scenario() -> None:
        with pytest.raises(OSError):
            await start_private_unix_server(_nothing, socket_root / "absent" / "s.sock", mode=0o600)

    before = os.umask(0o022)
    try:
        asyncio.run(scenario())
        after = os.umask(0o022)
    finally:
        os.umask(before)

    assert after == 0o022


def test_it_is_listening_before_it_ever_suspends(
    socket_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A path that exists but refuses connections is the mode window, moved along.

    `bind` publishes the path; `listen` is what makes a connection to it succeed.
    Between them the socket is findable and unusable, and the codex adapter polls
    for exactly this file and connects the instant it appears — so a suspension
    point in that gap is a `Connection refused` under load (#116). This connects
    at the one moment that gap would be open.
    """
    path = socket_root / "s.sock"
    reached: list[bool] = []
    bind_and_listen = asyncio.start_unix_server

    async def connecting(*arguments: object, **named: object) -> asyncio.Server:
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.connect(str(path))
            reached.append(True)
        except OSError:
            reached.append(False)
        finally:
            probe.close()
        return await bind_and_listen(*arguments, **named)  # type: ignore[arg-type]

    monkeypatch.setattr(asyncio, "start_unix_server", connecting)

    async def scenario() -> None:
        server = await start_private_unix_server(_nothing, path, mode=0o600)
        server.close()
        await server.wait_closed()

    asyncio.run(scenario())

    assert reached == [True]

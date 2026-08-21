"""The pseudo-terminal a headless Session runs on, and the draining that keeps it alive.

A terminal coding agent is a TUI: it wants a terminal, and it behaves differently
without one. The direct-child adapter has no terminal emulator to offer it — the
engine is a daemon — so it allocates a pseudo-terminal itself and keeps the
master end. That is what "headless" means here: the Session is real, running on a
real tty, and nobody is looking at it (ADR 0008).

**Owning the master end is an obligation, not a convenience.** Nothing reads a
pseudo-terminal that nobody is attached to, and when its buffer fills the kernel
blocks the writer. A TUI redraws constantly, so an undrained master is not a slow
leak — it is a Session that stops, invisibly, within seconds of starting, in a
system whose whole purpose is to notice when a Session needs its human. So this
module drains continuously and unconditionally, and keeps only the most recent
bytes.

**The tail is bounded and is not a log.** It exists so that a launch which fails
can quote what the child actually printed, which is the difference between "the
launch failed" and a real error message. ADR 0004's rule holds: the engine owns
one log, and an adapter never enumerates or rotates another. This is a ring
buffer in memory that dies with the Session.

**Obligation 2, discharged structurally.** The child's stdout and stderr are the
pseudo-terminal, never a descriptor on the engine's log file. It therefore holds
no log descriptor to be left writing to a rotated generation, and the
truncate-in-place fallback ADR 0004 authorises for un-notifiable writers has
nothing to do with it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import pty
import signal
from collections import deque
from collections.abc import Mapping, Sequence
from pathlib import Path

_log = logging.getLogger(__name__)

#: How much of the child's most recent output is kept, so a failure can quote it.
#: A screen's worth and then some: enough to carry a stack trace or a refusal,
#: far too little to be mistaken for a log.
TAIL_BYTES = 64 << 10

#: How much is read from the master in one go.
READ_CHUNK_BYTES = 1 << 16

#: How long a terminated child is given to go before it is killed outright.
TERMINATE_GRACE_SECONDS = 10.0


class ConsoleError(Exception):
    """The child could not be started on a pseudo-terminal."""


class Console:
    """One child process on a pseudo-terminal this engine owns and drains."""

    def __init__(self, *, tail_bytes: int = TAIL_BYTES) -> None:
        self._process: asyncio.subprocess.Process | None = None
        self._master: int | None = None
        self._draining: asyncio.Task[None] | None = None
        self._tail: deque[bytes] = deque()
        self._tail_bytes = tail_bytes
        self._held = 0

    @property
    def pid(self) -> int:
        """The process this console started. Asking before it started is a bug."""
        if self._process is None:
            raise ConsoleError("nothing has been started on this console")
        return self._process.pid

    def is_running(self) -> bool:
        """Whether the child is still there, without waiting for it."""
        return self._process is not None and self._process.returncode is None

    @property
    def returncode(self) -> int | None:
        return None if self._process is None else self._process.returncode

    def tail(self) -> str:
        """The child's most recent output, decoded leniently for a human to read.

        Lenient because this is a terminal's bytes: it carries escape sequences
        and may be cut mid-character, and a failure message that itself raised on
        a partial UTF-8 sequence would replace a real error with a spurious one.
        """
        return b"".join(self._tail).decode("utf-8", errors="replace")

    async def start(self, argv: Sequence[str], *, env: Mapping[str, str], cwd: Path) -> None:
        """Start one process on a fresh pseudo-terminal, and begin draining it.

        `start_new_session` is what makes the pseudo-terminal the child's
        *controlling* terminal, which a TUI needs in order to be one. It also
        means the child is not in the engine's process group, so this console's
        own `close` is what ends it — which is the reaping responsibility the
        launcher takes on by owning the child environment.
        """
        if self._process is not None:
            raise ConsoleError("this console has already started a process")
        master, slave = pty.openpty()
        try:
            self._process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                cwd=str(cwd),
                env=dict(env),
                start_new_session=True,
            )
        except (OSError, ValueError) as unstartable:
            os.close(master)
            raise ConsoleError(f"cannot start {argv[0]}: {unstartable}") from None
        finally:
            # The child holds its own copy; this end must go or the master never
            # sees end-of-file when the child exits.
            os.close(slave)
        self._master = master
        os.set_blocking(master, False)
        self._draining = asyncio.create_task(self._drain(), name=f"session-console-{self.pid}")

    async def wait(self) -> int:
        """Wait for the child, and reap it. Nothing here leaves a zombie."""
        if self._process is None:
            raise ConsoleError("nothing has been started on this console")
        return await self._process.wait()

    async def close(self) -> None:
        """End the child if it is still there, stop draining, and release the tty.

        Terminate first and kill only if that is ignored: a TUI given a chance to
        exit takes its own cleanup with it, and a launcher that always killed
        would be choosing the worse of two endings for no reason.
        """
        process = self._process
        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), TERMINATE_GRACE_SECONDS)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                with contextlib.suppress(Exception):
                    await process.wait()
        if process is not None and process.returncode is None:
            _log.warning("the Session on pid %s did not exit", process.pid)
        await self._stop_draining()
        self._release()

    async def _stop_draining(self) -> None:
        draining, self._draining = self._draining, None
        if draining is None:
            return
        draining.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await draining

    def _release(self) -> None:
        master, self._master = self._master, None
        if master is not None:
            loop = asyncio.get_running_loop()
            with contextlib.suppress(Exception):
                loop.remove_reader(master)
            with contextlib.suppress(OSError):
                os.close(master)

    async def _drain(self) -> None:
        """Read the master forever, keeping only the tail. Never stops on content.

        The read is driven by the event loop rather than by a thread, so a
        Session that says nothing for an hour costs nothing at all — while a
        Session that floods still cannot block, because every byte is consumed
        the moment it arrives and all but the newest are dropped.
        """
        master = self._master
        if master is None:
            return
        loop = asyncio.get_running_loop()
        readable = asyncio.Event()
        loop.add_reader(master, readable.set)
        try:
            while True:
                await readable.wait()
                readable.clear()
                if not self._consume(master):
                    return
        finally:
            with contextlib.suppress(Exception):
                loop.remove_reader(master)

    def _consume(self, master: int) -> bool:
        """Take everything available now. False once the far end is gone for good."""
        while True:
            try:
                chunk = os.read(master, READ_CHUNK_BYTES)
            except BlockingIOError:
                return True
            except OSError:
                # The child closed the slave end. On Darwin and Linux alike this
                # surfaces as an error on the master rather than as end-of-file.
                return False
            if not chunk:
                return False
            self._keep(chunk)

    def _keep(self, chunk: bytes) -> None:
        """Hold the newest `tail_bytes`, dropping whatever that pushes out."""
        self._tail.append(chunk)
        self._held += len(chunk)
        while self._held > self._tail_bytes and len(self._tail) > 1:
            self._held -= len(self._tail.popleft())
        if self._held > self._tail_bytes:
            only = self._tail.pop()
            self._tail.append(only[-self._tail_bytes :])
            self._held = len(self._tail[0])


def terminate_quietly(pid: int) -> None:
    """Ask one process to end, and say nothing if it is already gone."""
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.kill(pid, signal.SIGTERM)

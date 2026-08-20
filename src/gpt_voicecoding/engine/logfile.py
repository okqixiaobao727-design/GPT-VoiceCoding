"""The engine's own log: bounded, and owned by the process that writes it.

Ported from the reference implementation, which is the whole reason this file
reads as settled rather than exploratory — it is the one component ADR 0004's
measurement already paid for. That log reached 68,042,451 bytes in 49.5 hours,
a sustained 1 GB/month, and 98.1% of those bytes were a single libmalloc
diagnostic repeated 681,929 times by subprocesses that had inherited a
`MallocStackLogging` from whichever shell started the daemon. The 105 lines that
explained a real outage were underneath it.

**Ownership is what makes rotation possible at all.** A shell redirect hands a
process a descriptor and no way to be told the file moved, so the only rollover
that could reach it was one that kept the inode — and keeping the inode means
truncating, which loses whatever lands between the copy and the truncate.
`OwnedLogStream` opens the file itself and points this process's stdout and
stderr at it, so rollover becomes a rename plus a `dup2`: every byte written
before the rename is in the rotated generation, every byte after it is in the new
live file, and there is no window in which a write can be dropped.

**Two consequences of the `dup2`, both measured rather than assumed.** First, the
log then has two classes of writer — `logging` records, which pass through a
handler that can check the size, and raw writes to the standard streams, which do
not — and the second class is the 98%. A cap only the first class can trigger is
a cap that was never installed, so the size is checked on a clock as well:
`OwnedLogStream.watch`. Second, a child that inherited the descriptor *before* a
rotation cannot be told to reopen, so its output stays with the generation it was
writing to and ages out with retention. That is why a generation is trimmed on
its own inode and never by replacing the file — see `_trim_to_tail`. Removing
that limitation rather than bounding it means giving children a pipe instead of
the log descriptor, which belongs to the launcher's ticket.

**The cap binds every generation**, not only the one a rotation just created, and
rotation keeps the *newest* bytes of what it rotates: the tail is the part that
explains what just happened. `max_bytes` x (`retained_files` + 1) is a ceiling
rather than an aspiration, because nothing rations a burst and the live file is
routinely well past the cap by the time rotation runs.

**Not here, deliberately.** The reference implementation also carried a
copy-truncate rotation for a log held open for its whole life by a third-party
child that has no reopen path. That belongs to whichever adapter owns such a
process, if one exists in this topology at all — Bridge Core must never enumerate
adapter log paths, and such a log must never be the engine's own. See ADR 0004.

`strip_environment` lives here because it is the same fact seen twice: the
variable that filled the log and the file it filled. It is called once per
process that spawns others, not at each spawn site — the sites are many and each
new one would have to remember, while the environment they all inherit is a
single place. The engine calls it at start. **A Session launched into a tmux
server inherits that server's environment rather than the engine's**, so a
launcher adapter that spawns through one must call this on its own path too;
being a pure function over a mapping is what makes that cheap.
"""

from __future__ import annotations

import errno
import fcntl
import logging
import os
import sys
import threading
from collections.abc import Callable, MutableMapping, Sequence
from pathlib import Path

#: The suffix of the lock file that serialises rotation. Writers can be separate
#: processes, so the exclusion has to be one the kernel enforces rather than a
#: lock in one process's memory. It is a sibling of the log rather than the log
#: itself: taking the lock must not create, extend or truncate the file whose
#: size decides whether to rotate.
LOCK_SUFFIX = ".lock"

#: How a record reads once it is in the file. A format, not a bound: nothing
#: about it was measured, and nothing downstream parses it.
RECORD_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

#: What this log is for, expressed as a threshold. ADR 0004's value claim is the
#: 105 lines that explained a real outage, and those lines are mostly the
#: narrative *around* the failure rather than the failure itself — which log was
#: adopted, what was loaded, that a rotation happened. A warnings-only log keeps
#: the explosion and discards the fuse. This is not one of the four values ADR
#: 0004 refuses to hard-code: those are volume decisions a measurement settled,
#: while this is what the log is *for*, and the volume it admits is already
#: bounded by the cap above it and the environment stripping beside it.
DEFAULT_LEVEL = logging.INFO


class OwnedLogStream:
    """The engine's log, owned by the process that writes most of it.

    Owning it is not tidiness. It is the precondition for rename-based rotation
    reaching the biggest writer — this process's own stdout and stderr, and
    therefore every subprocess that inherits them.
    """

    def __init__(self, path: Path, *, max_bytes: int, retained_files: int) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self.retained_files = retained_files
        self._handle: object | None = None
        self._redirects_standard_streams = False
        # Rotation can now come from the clock as well as from a record, so two
        # threads reach the descriptor this object holds. The lock is around the
        # descriptor, not around the file: cross-process exclusion is the flock's
        # job and this one cannot do it.
        self._lock = threading.RLock()
        self._watcher: threading.Thread | None = None
        self._stopped = threading.Event()

    def adopt(self, *, redirect_standard_streams: bool = True) -> None:
        """Open the log, and make it this process's stdout and stderr.

        The redirect is skippable only for tests: a test process that pointed
        its own stdout at a scratch file would take the test runner's output
        with it.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("ab", buffering=0)
        self._redirects_standard_streams = redirect_standard_streams
        if redirect_standard_streams:
            self._point_standard_streams_here()

    def watch(self, seconds: float) -> None:
        """Check the size on a clock, because the biggest writer cannot ask.

        After `adopt` there are two classes of writer and only one of them can be
        made to trigger anything: a `logging` record goes through a handler that
        can check the size, while a raw write to stdout or stderr — this
        process's own, and every child that inherited the descriptor — goes
        straight to the file. That second class is the one ADR 0004 measured at
        98.1% of the volume, so a cap that only the first class can enforce is a
        cap that was never installed. The clock is the only trigger both classes
        share.

        A daemon thread rather than a task on the engine's loop: this starts
        before any loop exists, and a child flooding stderr is most likely
        precisely when that loop is busy or wedged — the moment the check has to
        survive.
        """
        if self._watcher is not None:
            return
        self._watcher = threading.Thread(
            target=self._watching, args=(seconds,), name="engine-log-rotation", daemon=True
        )
        self._watcher.start()

    def _watching(self, seconds: float) -> None:
        while not self._stopped.wait(seconds):
            try:
                self.rotate_if_needed()
            except Exception:
                # Nothing may fail because the logging did — least of all a
                # thread whose only job is to keep the logging bounded.
                continue

    def write(self, text: str) -> None:
        with self._lock:
            if self._handle is None:
                return
            self._handle.write(text.encode("utf-8", "replace"))

    def rotate_if_needed(self) -> bool:
        """Rotate if the log is over the cap, or follow it if it already moved.

        The second case is something else having rotated this path from its own
        process: this stream's descriptor would still refer to the file that was
        renamed, so every later write — including a subprocess's stderr — would
        land in a generation nobody reads instead of in the live log.
        """
        with self._lock:
            if self._handle is None:
                return False
            if self._points_elsewhere():
                self._reopen()
                return False
            return self._rotating()

    def _rotating(self) -> bool:
        # `_reopen` is handed to the rotation rather than run after it, because
        # the gap between the rename and the reopen is a gap in which this
        # process — and every child holding the inherited descriptor — is still
        # appending to the file that was just renamed. Trimming that file while
        # those writes are landing would copy its tail, replace it, and discard
        # whatever arrived in between: the copy-truncate loss this whole design
        # exists to avoid, reintroduced one step further along. Reopening first
        # means nothing is writing to the rotated generation when it is rewritten.
        return rotate_by_rename(
            self.path,
            max_bytes=self.max_bytes,
            retained_files=self.retained_files,
            on_renamed=self._reopen,
        )

    def close(self) -> None:
        self._stopped.set()
        watcher, self._watcher = self._watcher, None
        if watcher is not None:
            watcher.join(timeout=5)
        with self._lock:
            handle, self._handle = self._handle, None
            if handle is not None:
                handle.close()

    def _points_elsewhere(self) -> bool:
        """Whether the live path is no longer the file this stream writes to."""
        try:
            return os.fstat(self._handle.fileno()).st_ino != self.path.stat().st_ino
        except OSError:
            return True

    def _reopen(self) -> None:
        previous = self._handle
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = self.path.open("ab", buffering=0)
        except OSError:
            # A stream that cannot reopen keeps the descriptor it has. Writes
            # then land in a rotated generation, which is worse than the live
            # file and far better than an engine that fails because its logging
            # did.
            self._handle = previous
            return
        if self._redirects_standard_streams:
            self._point_standard_streams_here()
        if previous is not None:
            previous.close()

    def _point_standard_streams_here(self) -> None:
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
                os.dup2(self._handle.fileno(), stream.fileno())
            except (OSError, ValueError, AttributeError):
                # A process whose stdout is not a real descriptor still gets a
                # working log; it just does not get the redirect.
                continue


class OwnedLogHandler(logging.Handler):
    """Route `logging` into the owned stream, and let each record trigger rollover.

    This is the cheaper of the two triggers and the more precise one: a record
    checks the size at the moment it has just changed it, so a log filled by the
    engine's own records is bounded without waiting for a clock. It is not the
    only trigger, because it cannot be — a raw write to the standard streams
    never reaches this handler. `OwnedLogStream.watch` covers that writer.
    """

    def __init__(self, stream: OwnedLogStream) -> None:
        super().__init__()
        self.stream = stream
        self.setFormatter(logging.Formatter(RECORD_FORMAT))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.stream.write(self.format(record) + "\n")
            self.stream.rotate_if_needed()
        except Exception:
            self.handleError(record)


def own_the_log(
    path: Path,
    *,
    max_bytes: int,
    retained_files: int,
    check_seconds: float | None = None,
    redirect_standard_streams: bool = True,
) -> OwnedLogStream:
    """Adopt the log and point this process's `logging` at it. One call, at start.

    The three halves belong together: a destination nothing is routed to is not a
    log, a threshold with nowhere to write is not one either, and a cap nothing
    checks is not a cap. `DEFAULT_LEVEL` carries the reasoning for the level and
    `OwnedLogStream.watch` for the clock.

    `check_seconds` is optional only so that a test can own a log without a
    thread running behind it; a process that serves passes one.
    """
    stream = OwnedLogStream(path, max_bytes=max_bytes, retained_files=retained_files)
    stream.adopt(redirect_standard_streams=redirect_standard_streams)
    root = logging.getLogger()
    root.addHandler(OwnedLogHandler(stream))
    root.setLevel(DEFAULT_LEVEL)
    if check_seconds is not None:
        stream.watch(check_seconds)
    return stream


def rotate_by_rename(
    path: Path,
    *,
    max_bytes: int,
    retained_files: int,
    on_renamed: Callable[[], None] | None = None,
) -> bool:
    """Move the log aside once it reaches `max_bytes`, losing no writes.

    The live path is left absent rather than recreated: every writer either
    reopens it (`OwnedLogStream`) or opens it per write, and both create it on
    the way.

    `on_renamed` is called after the rename and *before* the rotated generation
    is trimmed, still under the lock. It exists because those two steps are not
    the same moment for a writer that holds the file open: between them, its
    descriptor still refers to the renamed file, and trimming that file — a copy
    followed by a replace — would drop whatever it appended in between. An owner
    passes its reopen here so that by the time the trim runs, nothing is writing
    to the file being rewritten. A caller that opens the log per write has
    nothing to reopen and passes nothing.

    A log that cannot be read or rotated is left alone and reported as not
    rotated. Every caller is a writer whose real work is something else, and
    none of them may fail because the logging did.
    """
    if not _at_or_over(path, max_bytes):
        return False

    lock_path = path.with_name(path.name + LOCK_SUFFIX)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            # Re-measured under the lock: two writers can pass the check above
            # together, and the second must not rotate an already-rotated log
            # into a generation of its own, which would discard the oldest kept
            # generation for nothing.
            if not _at_or_over(path, max_bytes):
                return False
            _shift_generations(path, retained_files, max_bytes)
            if retained_files <= 0:
                _unlink(path)
                if on_renamed is not None:
                    on_renamed()
            else:
                first = path.with_name(f"{path.name}.1")
                path.rename(first)
                if on_renamed is not None:
                    on_renamed()
                _trim_to_tail(first, max_bytes)
    except OSError:
        return False
    return True


def strip_environment(
    environ: MutableMapping[str, str], prefixes: Sequence[str]
) -> tuple[str, ...]:
    """Remove every variable whose name starts with one of `prefixes`.

    Returns the removed names in sorted order, so the caller can record what it
    took away — a variable that vanishes silently is the same kind of surprise as
    the one that filled the log.
    """
    removed = sorted(
        name for name in list(environ) if any(name.startswith(prefix) for prefix in prefixes)
    )
    for name in removed:
        del environ[name]
    return tuple(removed)


def _trim_to_tail(path: Path, max_bytes: int) -> None:
    """Cut a generation down to the newest `max_bytes` — on its own inode.

    Without the trim, an overshoot — the live file is only measured between
    writes, and nothing rations a burst — would be carried into the kept
    generation and the configured ceiling would stop being one.

    **On the same inode, not by replacing the file.** A child process that
    inherited this descriptor before the rename is still writing through it, and
    it can never be told to reopen. Writing a replacement over the path would
    leave that child appending to an unlinked inode for the rest of its life —
    every byte gone, silently, and not merely for the duration of a race. Keeping
    the inode means its output stays in the generation chain: it rides the
    renames and ages out with retention, which is the ADR's sanctioned way for
    log data to be dropped.

    The cost is stated rather than hidden: a raw write landing between the read
    and the truncate can be lost. That is ADR 0004's own fallback, and this is
    exactly the case the ADR grants it to — a log held by a writer the engine
    cannot tell to reopen. It is bounded to that writer, and to the milliseconds
    of one trim.
    """
    if path.stat().st_size <= max_bytes:
        return
    with path.open("r+b") as handle:
        size = os.fstat(handle.fileno()).st_size
        seeked = size > max_bytes
        if seeked:
            handle.seek(size - max_bytes)
        # Bounded by the cap rather than by end-of-file, and measured separately
        # from the guard above. The size is read once, and a writer appends
        # whenever it likes — including between that measurement and this read —
        # so reading to the end would let a write that landed mid-rotation carry
        # the kept generation over the cap, which is the ceiling failing in the
        # one moment it is load bearing.
        tail = handle.read(max_bytes)
        if seeked:
            # Drop the partial record the seek landed inside, but never at the
            # cost of keeping nothing: a stream with no newline in its final
            # `max_bytes` — one enormous line, a binary burst on stderr — still
            # has its tail kept rather than being silently discarded whole. A
            # file that fit is kept as it stands, because nothing was cut into.
            boundary = tail.find(b"\n")
            if boundary != -1 and boundary + 1 < len(tail):
                tail = tail[boundary + 1 :]
        # Written before the file is shortened, so it is never momentarily empty
        # for a reader — or for the child still appending to the end of it.
        handle.seek(0)
        handle.write(tail)
        handle.truncate()


def _at_or_over(path: Path, max_bytes: int) -> bool:
    try:
        return path.stat().st_size >= max_bytes
    except OSError:
        return False


def _shift_generations(path: Path, retained_files: int, max_bytes: int) -> None:
    """Make room for a new `.1` and drop whatever falls past the cap.

    Each generation is trimmed as it is moved, not only when it is created. A
    generation can be over the cap without this rotation having put it there — a
    file left by a runtime that predates the cap, or a `max_bytes` the user has
    since lowered — and carrying that excess down the chain would put the family
    over its ceiling with no writer ever having gone over it.
    """
    _unlink(path.with_name(f"{path.name}.{retained_files}"))
    for index in range(retained_files - 1, 0, -1):
        older = path.with_name(f"{path.name}.{index}")
        newer = path.with_name(f"{path.name}.{index + 1}")
        try:
            older.rename(newer)
        except OSError as error:
            if error.errno != errno.ENOENT:
                raise
            continue
        _trim_to_tail(newer, max_bytes)


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError as error:
        if error.errno != errno.ENOENT:
            raise

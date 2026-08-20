"""The engine's own log: bounded, rotated by rename, and never lost mid-rollover.

ADR 0004's measurement is the reason every case here exists. The reference
implementation's log reached 68,042,451 bytes in 49.5 hours because nothing
bounded it, and it could not be bounded because the daemon did not own it — a
shell redirect had handed it a descriptor with no way to be told the file had
moved. So the two halves are tested together: **ownership**, which is what makes
a rename reach the biggest writer at all, and **rename-and-reopen**, which is the
only rollover with no window a write can fall into.

Copy-truncate is what these tests exist to rule out. Its copy and its truncate
are two operations and a line appended between them is gone, which shows up here
as a hole in a sequence at the rollover boundary — so the sequence is asserted to
be contiguous rather than merely non-empty.

The cap binds **every generation**, not only the one a rotation just made: a file
can be over the cap without this rotation having put it there, and the ceiling is
`max_bytes` x (`retained_files` + 1) or it is not a ceiling.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from gpt_voicecoding.engine.logfile import (
    LOCK_SUFFIX,
    OwnedLogHandler,
    OwnedLogStream,
    own_the_log,
    rotate_by_rename,
    strip_environment,
)

#: Small enough that a handful of lines crosses it, so every case here is exact
#: rather than approximate. Nothing in the module knows this number.
CAP = 100
RETAINED = 3


def rotate(path: Path, *, max_bytes: int = CAP, retained_files: int = RETAINED) -> bool:
    return rotate_by_rename(path, max_bytes=max_bytes, retained_files=retained_files)


def family(directory: Path, name: str = "engine.log") -> list[Path]:
    """Every generation of the log, and never the lock that serialises rotation."""
    return sorted(
        path for path in directory.glob(f"{name}*") if not path.name.endswith(LOCK_SUFFIX)
    )


@pytest.fixture
def log_path(tmp_path: Path) -> Path:
    return tmp_path / "engine.log"


@pytest.fixture
def stream(log_path: Path):
    """An owned stream that does not take the test runner's stdout with it.

    The redirect is real and is tested — in a subprocess, where hijacking the
    standard streams is the point rather than a casualty.
    """
    owned = OwnedLogStream(log_path, max_bytes=CAP, retained_files=RETAINED)
    owned.adopt(redirect_standard_streams=False)
    yield owned
    owned.close()


class TestRotationByRename:
    def test_a_log_under_the_cap_is_left_alone(self, log_path: Path) -> None:
        log_path.write_bytes(b"x" * (CAP - 1))

        assert rotate(log_path) is False
        assert log_path.stat().st_size == CAP - 1
        assert not log_path.with_name("engine.log.1").exists()

    def test_a_log_at_the_cap_moves_aside_whole(self, log_path: Path) -> None:
        log_path.write_bytes(b"x" * CAP)

        assert rotate(log_path) is True
        # The live path is left absent rather than recreated: every writer
        # either reopens it or opens it per write, and both create it.
        assert not log_path.exists()
        assert log_path.with_name("engine.log.1").read_bytes() == b"x" * CAP

    def test_the_newest_bytes_are_kept_and_the_rest_discarded(self, log_path: Path) -> None:
        """The tail is the part that explains what just happened."""
        log_path.write_text("".join(f"line {index}\n" for index in range(500)))

        assert rotate(log_path) is True

        rotated = log_path.with_name("engine.log.1")
        assert rotated.stat().st_size <= CAP
        assert rotated.read_text().endswith("line 499\n")
        # The kept generation opens on a whole record rather than halfway
        # through the line the seek happened to land inside.
        assert not rotated.read_text().startswith("ine")

    def test_a_tail_with_no_line_boundary_is_kept_rather_than_discarded(
        self, log_path: Path
    ) -> None:
        """One enormous line — a binary burst on stderr — still leaves a tail."""
        log_path.write_bytes(b"y" * (CAP * 3))

        assert rotate(log_path) is True

        assert log_path.with_name("engine.log.1").read_bytes() == b"y" * CAP

    def test_retention_discards_the_oldest_generation(self, log_path: Path) -> None:
        for generation in range(5):
            # The marker goes at the end: rotation keeps the newest bytes, so
            # the tail is what identifies a generation.
            log_path.write_text("x" * CAP + f"\ngeneration {generation}\n")
            rotate(log_path)

        assert not log_path.exists()
        for index, generation in ((1, 4), (2, 3), (3, 2)):
            assert log_path.with_name(f"engine.log.{index}").read_text() == (
                f"generation {generation}\n"
            )
        assert not log_path.with_name("engine.log.4").exists()

    def test_the_whole_family_is_bounded_by_the_cap_and_the_retention(
        self, tmp_path: Path, log_path: Path
    ) -> None:
        """`max_bytes` x (`retained_files` + 1) is the ceiling on disk."""
        for _ in range(6):
            with log_path.open("ab") as handle:
                handle.write(b"x" * CAP)
            rotate(log_path)
        with log_path.open("ab") as handle:
            handle.write(b"x" * (CAP - 1))

        kept = family(tmp_path)
        assert [path.name for path in kept] == [
            "engine.log",
            "engine.log.1",
            "engine.log.2",
            "engine.log.3",
        ]
        assert sum(path.stat().st_size for path in kept) <= CAP * (RETAINED + 1)

    def test_the_lock_never_creates_or_extends_the_file_it_measures(
        self, tmp_path: Path, log_path: Path
    ) -> None:
        """The lock is a sibling of the log, never the log itself."""
        log_path.write_bytes(b"x" * CAP)

        rotate(log_path)

        assert log_path.with_name(f"engine.log{LOCK_SUFFIX}").stat().st_size == 0

    def test_an_already_oversized_generation_is_trimmed_as_it_ages(
        self, tmp_path: Path, log_path: Path
    ) -> None:
        """The ceiling binds every generation, not only the one just made.

        A generation can be over the cap without this rotation having put it
        there — a file left by a runtime that predates the cap, or a cap the
        user has since lowered — and carrying that excess down the chain would
        put the family over its ceiling with no writer ever having gone over it.
        """
        log_path.with_name("engine.log.1").write_text("one\n" * 200)
        log_path.with_name("engine.log.2").write_text("two\n" * 200)
        log_path.write_bytes(b"x" * CAP)

        assert rotate(log_path) is True

        kept = family(tmp_path)
        for path in kept:
            assert path.stat().st_size <= CAP, path.name
        assert sum(path.stat().st_size for path in kept) <= CAP * (RETAINED + 1)
        # Trimming an ageing generation keeps its newest lines too.
        assert log_path.with_name("engine.log.2").read_text().endswith("one\n")
        assert log_path.with_name("engine.log.3").read_text().endswith("two\n")

    def test_a_write_landing_mid_rotation_cannot_push_the_kept_copy_over(
        self, log_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The size is measured once; the read must not trust it afterwards.

        A writer appends whenever it likes, including between the measurement
        and the read that trims the rotated generation, so a read that simply
        ran to end-of-file would be bounded by nothing at all.
        """
        log_path.write_text("old\n" * 25 + "z" * 400)
        real_fstat = os.fstat

        def understating_fstat(fileno: int, **kwargs: object) -> os.stat_result:
            measured = real_fstat(fileno)
            # As if the file had been this size when it was measured and had
            # grown between that measurement and the read.
            return os.stat_result(tuple(measured)[:6] + (CAP,) + tuple(measured)[7:])

        monkeypatch.setattr("gpt_voicecoding.engine.logfile.os.fstat", understating_fstat)
        rotate(log_path)

        assert log_path.with_name("engine.log.1").stat().st_size <= CAP

    def test_retaining_nothing_simply_empties_the_log(self, tmp_path: Path, log_path: Path) -> None:
        log_path.write_bytes(b"x" * CAP)

        assert rotate(log_path, retained_files=0) is True

        assert family(tmp_path) == []

    def test_a_missing_log_is_not_an_error(self, tmp_path: Path) -> None:
        """Every caller is a writer whose real work is something else."""
        assert rotate(tmp_path / "absent.log") is False


class TestTheOwnedStream:
    def test_rollover_loses_no_writes(self, log_path: Path, stream: OwnedLogStream) -> None:
        for index in range(20):
            stream.write(f"line {index}\n")

        assert stream.rotate_if_needed() is True
        stream.write("line 20\n")

        live = log_path.read_text()
        rotated = log_path.with_name("engine.log.1").read_text()
        assert live == "line 20\n"
        # Retention deliberately discards the oldest lines, so not every line
        # survives — but the sequence that does must have no hole in it, and in
        # particular none at the rollover boundary. Copy-truncate drops whatever
        # is written between the copy and the truncate, which would show up here
        # as a missing number next to that boundary.
        written = [
            int(line.split()[1])
            for line in (rotated + live).splitlines()
            if line.startswith("line ")
        ]
        assert written[-1] == 20
        assert written == list(range(written[0], 21))

    def test_a_log_under_the_cap_is_not_rotated(
        self, log_path: Path, stream: OwnedLogStream
    ) -> None:
        stream.write("short\n")

        assert stream.rotate_if_needed() is False
        assert log_path.read_text() == "short\n"

    def test_the_rotated_generation_is_trimmed_to_the_cap(
        self, log_path: Path, stream: OwnedLogStream
    ) -> None:
        stream.write("line\n" * 200)

        stream.rotate_if_needed()

        assert log_path.with_name("engine.log.1").stat().st_size <= CAP

    def test_the_log_directory_is_created_at_adoption(self, tmp_path: Path) -> None:
        """Nothing has made this directory before the first write to it."""
        nested = tmp_path / "never" / "created" / "engine.log"
        owned = OwnedLogStream(nested, max_bytes=CAP, retained_files=RETAINED)

        owned.adopt(redirect_standard_streams=False)
        try:
            owned.write("first line\n")
        finally:
            owned.close()

        assert nested.read_text() == "first line\n"

    def test_a_pre_existing_oversized_log_is_rotated_at_startup(self, log_path: Path) -> None:
        """An engine can start onto a log a previous run left over the cap."""
        log_path.write_text("".join(f"old {index}\n" for index in range(500)))
        owned = OwnedLogStream(log_path, max_bytes=CAP, retained_files=RETAINED)
        owned.adopt(redirect_standard_streams=False)

        try:
            assert owned.rotate_if_needed() is True
            owned.write("new line\n")
        finally:
            owned.close()

        assert log_path.read_text() == "new line\n"
        rotated = log_path.with_name("engine.log.1")
        assert rotated.stat().st_size <= CAP
        assert rotated.read_text().endswith("old 499\n")

    def test_an_outside_rotation_is_followed_rather_than_written_past(
        self, log_path: Path, stream: OwnedLogStream
    ) -> None:
        """Something else rotated; this stream must not keep filling the old inode."""
        stream.write("first\n")
        # Exactly the size just written, so the outside rotation fires and keeps
        # that line whole rather than trimming it to a cap.
        rotate_by_rename(log_path, max_bytes=len("first\n"), retained_files=RETAINED)

        assert stream.rotate_if_needed() is False
        stream.write("second\n")

        assert log_path.read_text() == "second\n"
        assert log_path.with_name("engine.log.1").read_text() == "first\n"

    def test_writing_before_adoption_is_dropped_rather_than_raising(self, log_path: Path) -> None:
        """Nothing may fail because the logging did."""
        owned = OwnedLogStream(log_path, max_bytes=CAP, retained_files=RETAINED)

        owned.write("nowhere\n")

        assert owned.rotate_if_needed() is False
        assert not log_path.exists()

    def test_closing_twice_is_harmless(self, log_path: Path) -> None:
        owned = OwnedLogStream(log_path, max_bytes=CAP, retained_files=RETAINED)
        owned.adopt(redirect_standard_streams=False)

        owned.close()
        owned.close()


ADOPTING_SCRIPT = """
import subprocess
import sys
import time
from pathlib import Path
from gpt_voicecoding.engine.logfile import OwnedLogStream

stream = OwnedLogStream(Path(sys.argv[1]), max_bytes={cap}, retained_files={retained})
stream.adopt()
{body}
"""


def run_adopting(log: Path, body: str) -> subprocess.CompletedProcess[str]:
    """Run adoption where hijacking the standard streams is the point.

    In-process, pointing this runner's stdout at a scratch file would take every
    later line of test output with it.
    """
    script = ADOPTING_SCRIPT.format(cap=CAP, retained=RETAINED, body=body)
    return subprocess.run(
        [sys.executable, "-c", script, str(log)], capture_output=True, text=True
    )


class TestAdoptionTakesTheStandardStreams:
    def test_stdout_and_stderr_become_the_log(self, log_path: Path) -> None:
        """The streams carrying 98% of the volume, owned rather than inherited."""
        result = run_adopting(
            log_path,
            "print('on stdout')\nsys.stderr.write('on stderr\\n')\nsys.stderr.flush()\n",
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout == ""
        assert result.stderr == ""
        written = log_path.read_text()
        assert "on stdout" in written
        assert "on stderr" in written

    def test_after_rotation_they_follow_the_new_file(self, log_path: Path) -> None:
        result = run_adopting(
            log_path,
            "sys.stderr.write('x' * 200 + '\\n')\n"
            "sys.stderr.flush()\n"
            "stream.rotate_if_needed()\n"
            "sys.stderr.write('after rotation\\n')\n"
            "sys.stderr.flush()\n",
        )

        assert result.returncode == 0, result.stderr
        assert log_path.read_text() == "after rotation\n"


class TestTheWriterThatCannotAskForRotation:
    """After `dup2` the log has two classes of writer, and only one can trigger.

    A `logging` record goes through a handler that can check the size. A raw
    write to stdout or stderr — this process's own, and every child that
    inherited the descriptor — goes straight to the file and asks nothing. That
    second class is the one ADR 0004 measured at 98.1% of the volume, so a cap
    only the first class can enforce is a cap that was never installed. The clock
    is the only trigger both classes share.
    """

    def test_raw_stderr_alone_is_still_bounded(self, log_path: Path) -> None:
        result = run_adopting(
            log_path,
            (
                "stream.watch(0.02)\n"
                "for _ in range(100):\n"
                "    sys.stderr.write('x' * 10 + '\\n')\n"
                "    sys.stderr.flush()\n"
                "    time.sleep(0.005)\n"
            ),
        )

        assert result.returncode == 0, result.stderr
        # 1100 bytes were written against a 100-byte cap. Without a trigger the
        # whole 1100 sits in the live file and no generation exists at all.
        assert log_path.with_name("engine.log.1").exists(), "nothing ever rotated"
        assert log_path.stat().st_size <= CAP

    def test_owning_a_log_with_a_clock_bounds_a_writer_that_never_asks(
        self, tmp_path: Path
    ) -> None:
        """The wiring, in process: no record is ever emitted, and it still rotates."""
        path = tmp_path / "engine.log"
        root = logging.getLogger()
        before = list(root.handlers)
        level = root.level

        owned = own_the_log(
            path,
            max_bytes=CAP,
            retained_files=RETAINED,
            check_seconds=0.02,
            redirect_standard_streams=False,
        )
        try:
            # A foreign descriptor, exactly like a child's inherited stderr:
            # nothing it does passes through the handler.
            with path.open("ab", buffering=0) as foreign:
                foreign.write(b"x" * (CAP * 5))
            deadline = time.monotonic() + 5
            while not path.with_name("engine.log.1").exists() and time.monotonic() < deadline:
                time.sleep(0.02)
        finally:
            for handler in list(root.handlers):
                if handler not in before:
                    root.removeHandler(handler)
            root.setLevel(level)
            owned.close()

        assert path.with_name("engine.log.1").exists(), "the clock never rotated it"

    def test_a_child_holding_the_inherited_descriptor_is_not_lost(
        self, log_path: Path
    ) -> None:
        """A child cannot be told to reopen, so its output has to ride the chain.

        Trimming a rotated generation by replacing the file would leave that
        child appending to an unlinked inode for the rest of its life — every
        byte gone, and not merely for the duration of a race. Trimming on the
        same inode keeps its output in the generation chain, where it ages out
        with retention like everything else.
        """
        result = run_adopting(
            log_path,
            (
                "child = subprocess.Popen([sys.executable, '-c',\n"
                "    \"import sys, time\\n\"\n"
                "    \"sys.stderr.write('CHILD BEFORE\\\\n'); sys.stderr.flush()\\n\"\n"
                "    \"time.sleep(1.0)\\n\"\n"
                "    \"sys.stderr.write('CHILD AFTER\\\\n'); sys.stderr.flush()\\n\"])\n"
                "time.sleep(0.3)\n"
                "stream.write('z' * 200 + '\\n')\n"
                "stream.rotate_if_needed()\n"
                "child.wait()\n"
                "time.sleep(0.2)\n"
            ),
        )

        assert result.returncode == 0, result.stderr
        everywhere = "".join(
            path.read_text(errors="replace") for path in family(log_path.parent)
        )
        assert "CHILD AFTER" in everywhere, "the child's output went to an unlinked inode"


class TestRotationRacesAConcurrentWrite:
    """Rollover is a rename, so a write racing it lands on one side or the other.

    Two races are distinguishable and both are here: a writer appending while a
    rotation runs, which must lose nothing, and two rotations firing together,
    which must produce one generation rather than two — the second must not
    shift the family down a step for nothing, discarding the oldest generation
    to make room for a file that was already moved.
    """

    def test_a_writer_appending_through_a_rollover_loses_nothing(
        self, log_path: Path, stream: OwnedLogStream
    ) -> None:
        """The invariant is at the boundary, and only there.

        Lines *are* lost further back, on purpose: a generation that overshot
        the cap is trimmed to its newest bytes, so the run of numbers has holes
        wherever an overshoot was discarded. What may never have a hole is the
        seam the rename itself made — the last line of the rotated generation
        and the first line of the live file must be consecutive. That is the
        precise thing copy-truncate cannot promise, and asserting whole-history
        contiguity instead would assert something bounded logging never claimed.
        """
        lines = 400
        stop = threading.Event()
        running = threading.Event()
        rotations: list[bool] = []

        def rotating() -> None:
            running.set()
            while not stop.is_set():
                if stream.rotate_if_needed():
                    rotations.append(True)

        rotator = threading.Thread(target=rotating)
        rotator.start()
        try:
            # Two things have to be true before the assertions below mean
            # anything, and neither can be assumed from thread scheduling: the
            # rotator has to be in its loop, and it has to have actually rotated
            # while the writer was writing. So the writer keeps going until it
            # has been raced, with a ceiling so a rotation that never comes fails
            # the test rather than hanging it.
            assert running.wait(timeout=5), "the rotator never started"
            written = 0
            while written < lines or (not rotations and written < lines * 10):
                stream.write(f"line {written}\n")
                written += 1
        finally:
            stop.set()
            rotator.join(timeout=5)
        assert not rotator.is_alive()
        assert rotations, "no rotation ever raced the writer"

        rotated = log_path.with_name("engine.log.1").read_text()
        live = log_path.read_text()
        across = [
            int(line.split()[1])
            for line in (rotated + live).splitlines()
            if line.startswith("line ")
        ]
        assert across, "the rotation and the writer left nothing behind"
        assert across == list(range(across[0], across[0] + len(across)))
        # Every record is whole: a rename cannot cut one in half either.
        for line in (rotated + live).splitlines():
            assert line.startswith("line ") and line.split()[1].isdigit(), line

    def test_two_rotations_firing_together_make_one_generation(
        self, tmp_path: Path, log_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The exclusion has to be one the kernel enforces.

        Both callers measure an over-cap file before either has renamed it, so
        the check they share cannot be what decides: the second must re-measure
        under the lock and find the work already done.
        """
        log_path.with_name("engine.log.1").write_text("older\n")
        log_path.write_bytes(b"x" * CAP)

        import gpt_voicecoding.engine.logfile as logfile

        both_measured = threading.Barrier(2, timeout=5)
        measured_once: set[int] = set()
        at_or_over = logfile._at_or_over

        def measuring(path: Path, max_bytes: int) -> bool:
            answer = at_or_over(path, max_bytes)
            # Only the first measurement each thread makes — the one outside
            # the lock — is held until its partner has made the same one.
            if threading.get_ident() not in measured_once:
                measured_once.add(threading.get_ident())
                both_measured.wait()
            return answer

        monkeypatch.setattr(logfile, "_at_or_over", measuring)
        outcomes: list[bool] = []
        lock = threading.Lock()

        def rotating() -> None:
            rotated = rotate(log_path)
            with lock:
                outcomes.append(rotated)

        racers = [threading.Thread(target=rotating) for _ in range(2)]
        for racer in racers:
            racer.start()
        for racer in racers:
            racer.join(timeout=10)
            assert not racer.is_alive()

        assert sorted(outcomes) == [False, True], "exactly one rotation should have happened"
        assert log_path.with_name("engine.log.1").read_bytes() == b"x" * CAP
        assert log_path.with_name("engine.log.2").read_text() == "older\n"
        # The loser must not have shifted the family a second time.
        assert not log_path.with_name("engine.log.3").exists()


class TestTheHandlerTheEngineLogsThrough:
    def test_a_record_lands_in_the_log(self, log_path: Path, stream: OwnedLogStream) -> None:
        logger = logging.getLogger("test.records")
        logger.propagate = False
        logger.setLevel(logging.INFO)
        handler = OwnedLogHandler(stream)
        logger.addHandler(handler)
        try:
            logger.info("the engine said something")
        finally:
            logger.removeHandler(handler)

        written = log_path.read_text()
        assert "the engine said something" in written
        assert written.endswith("\n")

    def test_records_rotate_the_log_they_fill(self, log_path: Path, stream: OwnedLogStream) -> None:
        """The handler is the one writer, so it is also the rotation trigger."""
        logger = logging.getLogger("test.rotation")
        logger.propagate = False
        logger.setLevel(logging.INFO)
        handler = OwnedLogHandler(stream)
        logger.addHandler(handler)
        try:
            for index in range(40):
                logger.info("record %d", index)
        finally:
            logger.removeHandler(handler)

        assert log_path.with_name("engine.log.1").exists()
        assert log_path.stat().st_size <= CAP
        # The record that pushed the file over the cap is written before the
        # rotation it triggers, so it is in the generation that rotation made.
        assert "record 39" in log_path.with_name("engine.log.1").read_text()

    def test_owning_the_log_routes_the_root_logger_into_it(self, tmp_path: Path) -> None:
        nested = tmp_path / "engine" / "engine.log"
        root = logging.getLogger()
        before = list(root.handlers)
        level = root.level

        owned = own_the_log(
            nested, max_bytes=CAP, retained_files=RETAINED, redirect_standard_streams=False
        )
        try:
            logging.getLogger("gpt_voicecoding.test").error("something went wrong")
        finally:
            for handler in list(root.handlers):
                if handler not in before:
                    root.removeHandler(handler)
            root.setLevel(level)
            owned.close()

        assert "something went wrong" in nested.read_text()

    def test_the_narrative_around_a_failure_is_admitted_too(self, tmp_path: Path) -> None:
        """A warnings-only log would keep the explosion and discard the fuse.

        ADR 0004's value claim is the 105 lines that explained a real outage, and
        those are mostly the ordinary narrative around it. So the threshold comes
        with the destination rather than being left at Python's default.
        """
        path = tmp_path / "engine.log"
        root = logging.getLogger()
        level = root.level
        before = list(root.handlers)

        owned = own_the_log(
            path, max_bytes=CAP, retained_files=RETAINED, redirect_standard_streams=False
        )
        try:
            assert root.level == logging.INFO
            logging.getLogger("gpt_voicecoding.test").info("what led up to it")
        finally:
            for handler in list(root.handlers):
                if handler not in before:
                    root.removeHandler(handler)
            root.setLevel(level)
            owned.close()

        assert "what led up to it" in path.read_text()


class TestStrippingTheEnvironment:
    """The other half of the same fact: 98.1% of those bytes were one variable.

    `MallocStackLogging` was set nowhere in the reference implementation. It was
    inherited from whichever shell started the daemon, handed to every subprocess
    for the life of that daemon, and libmalloc answered each one on stderr —
    681,929 times. The strip is done once per spawning process rather than at
    each spawn site, because the spawn sites are many and the environment they
    all inherit is a single place.
    """

    def test_only_the_configured_prefixes_are_removed(self) -> None:
        environ = {
            "MallocStackLogging": "1",
            "MallocNanoZone": "0",
            "PATH": "/usr/bin",
            "HOME": "/somewhere",
        }

        removed = strip_environment(environ, ("Malloc",))

        assert removed == ("MallocNanoZone", "MallocStackLogging")
        assert environ == {"PATH": "/usr/bin", "HOME": "/somewhere"}

    def test_what_was_taken_away_is_reported(self) -> None:
        """A variable that vanishes silently is the same kind of surprise."""
        environ = {"DIAGNOSTIC_ONE": "1", "DIAGNOSTIC_TWO": "2"}

        assert strip_environment(environ, ("DIAGNOSTIC_",)) == (
            "DIAGNOSTIC_ONE",
            "DIAGNOSTIC_TWO",
        )

    def test_configuring_no_prefixes_removes_nothing(self) -> None:
        environ = {"MallocStackLogging": "1"}

        assert strip_environment(environ, ()) == ()
        assert environ == {"MallocStackLogging": "1"}

    def test_a_subprocess_inherits_the_cleaned_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The point of stripping the environment rather than each spawn call."""
        monkeypatch.setenv("MallocStackLogging", "1")

        strip_environment(os.environ, ("Malloc",))

        inherited = subprocess.run(
            [sys.executable, "-c", "import os; print(os.environ.get('MallocStackLogging'))"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert inherited.stdout.strip() == "None"

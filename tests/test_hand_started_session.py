"""A Session the harness hand-starts is one the process table can see.

The Codex roster follows ADR 0020: a thread the daemon holds becomes a row only
when a **live interactive `codex` with a controlling terminal** in the same
workspace vouches for it, and `processes._interactive_pids` reads that from
`ps`'s `tty` column, skipping `??` by #144's rule. Run `20260902T041923Z` failed
its codex `roster` step for exactly that reason: the harness opened a pty and
ran the TUI on it, but never made that pty the child's *controlling* terminal,
so `ps -o tty=` said `??` and the engine — correctly, by its own rule — saw no
terminal to vouch for the rollout record it had found (#208).

`start_new_session=True` is not enough and is not meant to be: it calls
`setsid()`, which puts the child in a fresh session **with no controlling
terminal at all**, and a session leader acquires one only by asking (`TIOCSCTTY`,
which `login_tty` wraps). Opening the pty slave as fds 0/1/2 gives the child a
terminal to read and write; it does not give it one `ps` will name.

So these tests ask the two questions the acceptance run asked and could not
answer until it was too expensive: does the child hold a controlling terminal
(it can open `/dev/tty`, and `ps` names it), and does stopping it still reach
everything it started. They start `sys.executable`, never `codex`: the rule
under test belongs to the harness's `start()`, and a test that needed the real
TUI could only ever run on the one machine that has it.

**Legacy citation (ADR 0010).** Legacy never enumerated Codex processes
(`adapters/agent/codex/processes.py` module docstring: "No legacy analogue"),
and its only pty-less child spawn — `legacy@1d32845:test_install_runtime.py:447-453`
— is `subprocess.Popen(..., start_new_session=True)` with no controlling
terminal, which is the shape this file rejects. *Dropped, because* the
requirement is this generation's, from ADR 0020, and legacy has no behaviour to
port here.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import hand_started
import pytest

#: A child that says whether it holds a controlling terminal. `/dev/tty` is the
#: question itself rather than a proxy for it: it is the one path that resolves
#: to "the terminal controlling *this* process", and a process without one gets
#: `ENXIO` — while `os.ttyname(0)` happily names the pty either way, which is why
#: the run's harness looked right and read as `??`.
REPORTS_ITS_TERMINAL = (
    "import os, sys, time\n"
    "try:\n"
    "    holder = os.open('/dev/tty', os.O_RDWR)\n"
    "except OSError as refused:\n"
    "    sys.stdout.write('TERMINAL none errno=%d\\n' % refused.errno)\n"
    "else:\n"
    "    os.close(holder)\n"
    "    sys.stdout.write('TERMINAL %s\\n' % os.ttyname(0))\n"
    "sys.stdout.flush()\n"
    "time.sleep(120)\n"
)

#: A child that starts a child of its own and names it, so a stop can be judged
#: on the whole tree rather than on the one pid the harness knows.
STARTS_A_CHILD_OF_ITS_OWN = (
    "import subprocess, sys, time\n"
    "own = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
    "sys.stdout.write('CHILD %d\\n' % own.pid)\n"
    "sys.stdout.flush()\n"
    "time.sleep(120)\n"
)

#: How long a child gets to reach its first line, and a stopped tree to go.
#: Generous on purpose: these are real interpreters starting on a machine that
#: may be running an acceptance walk beside them, and the assertions are about
#: what happened, never about how fast.
SPEAKS_WITHIN_SECONDS = 30.0
GOES_WITHIN_SECONDS = 15.0

#: What `ps -o tty=` prints for a process with no controlling terminal: `??` on
#: macOS (the form `processes.NO_CONTROLLING_TERMINAL` pins), `?` on Linux. Both
#: are listed because this test runs wherever CI runs, and the engine's own
#: reading is macOS's.
NO_TERMINAL_COLUMNS = frozenset({"??", "?", "-", ""})


def _nowhere(event: str, **fields: object) -> None:  # noqa: ARG001
    """A journal that writes nowhere: these tests grade the process, not the log."""


def _session(tmp_path: Path, program: str, journal=_nowhere) -> hand_started.HandStartedSession:
    """One hand-started Session running `program` under this interpreter."""
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return hand_started.HandStartedSession(
        lane="test",
        binary=Path(sys.executable),
        arguments=("-c", program),
        workspace=workspace,
        environment=hand_started.terminal_environment(os.environ.get("PATH", "")),
        journal=journal,
        transcript=tmp_path / "pty.log",
    )


def _first_line_with(session: hand_started.HandStartedSession, marker: str) -> str:
    """Wait for the child's own report, or fail with what the screen did say."""
    deadline = time.monotonic() + SPEAKS_WITHIN_SECONDS
    while time.monotonic() < deadline:
        for line in session.screen_tail().splitlines():
            if marker in line:
                return line.strip()
        time.sleep(0.05)
    raise AssertionError(
        f"no {marker!r} line within {SPEAKS_WITHIN_SECONDS}s: {session.screen_tail()!r}"
    )


def _terminal_column(pid: int) -> str:
    """The `tty` column the engine's own reading takes its answer from."""
    listed = subprocess.run(
        ["/bin/ps", "-o", "tty=", "-p", str(pid)], capture_output=True, text=True, check=False
    )
    return listed.stdout.strip()


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:  # pragma: no cover - a pid this user may not signal
        return False
    return False


@pytest.fixture
def started(tmp_path: Path):
    """Sessions started by a test, stopped whatever the test then did."""
    running: list[hand_started.HandStartedSession] = []

    def start(program: str, journal=_nowhere) -> hand_started.HandStartedSession:
        session = _session(tmp_path, program, journal)
        session.start()
        running.append(session)
        return session

    yield start
    for session in running:
        session.stop()


class TestTheControllingTerminal:
    def test_the_child_holds_the_pty_as_its_controlling_terminal(self, started) -> None:
        session = started(REPORTS_ITS_TERMINAL)

        reported = _first_line_with(session, "TERMINAL ")

        assert reported.startswith("TERMINAL /dev/"), reported

    def test_the_process_table_names_that_terminal(self, started) -> None:
        """The engine reads `ps`'s `tty` column and skips `??` (#144); so does this."""
        session = started(REPORTS_ITS_TERMINAL)
        _first_line_with(session, "TERMINAL ")

        assert session.pid is not None
        assert _terminal_column(session.pid) not in NO_TERMINAL_COLUMNS

    def test_the_session_is_still_its_own_process_group_leader(self, started) -> None:
        """`stop()` signals the group by the pid it knows, so the two must agree."""
        session = started(REPORTS_ITS_TERMINAL)
        _first_line_with(session, "TERMINAL ")

        assert session.pid is not None
        assert os.getpgid(session.pid) == session.pid

    def test_the_journal_records_the_command_a_person_typed(self, tmp_path: Path) -> None:
        """However the terminal is acquired, the evidence stays the ordinary command."""
        written: list[tuple[str, dict]] = []
        session = _session(
            tmp_path, REPORTS_ITS_TERMINAL, lambda event, **fields: written.append((event, fields))
        )
        try:
            session.start()
        finally:
            session.stop()

        started_events = [fields for event, fields in written if event == "session.hand_started"]
        assert started_events
        assert started_events[0]["command"] == [sys.executable, "-c", REPORTS_ITS_TERMINAL]


class TestStopping:
    def test_a_stop_reaches_the_session_and_what_it_started(self, started) -> None:
        session = started(STARTS_A_CHILD_OF_ITS_OWN)
        grandchild = int(_first_line_with(session, "CHILD ").split()[-1])
        assert session.pid is not None
        session_pid = session.pid

        session.stop()

        deadline = time.monotonic() + GOES_WITHIN_SECONDS
        while time.monotonic() < deadline and not (_gone(session_pid) and _gone(grandchild)):
            time.sleep(0.05)
        assert not session.alive
        assert _gone(grandchild), f"pid {grandchild} outlived the stop"

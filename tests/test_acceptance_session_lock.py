"""Fast regression coverage for the acceptance harness's one-run-per-machine lock.

Two acceptance runs on one machine collide over things no `--lane` separates: the
user-account session is an SQLite file backing one Telethon client, and
`support.TrustGate`'s guard over `~/.claude.json` and `~/.codex/config.toml` is a
*thread* lock. #203 makes preflight refuse the second run instead of leaving the
rule to a person, and this module is that behaviour at CI speed — no network, no
bot, a temporary person directory throughout.
"""

from __future__ import annotations

import errno
import importlib
import importlib.util
import json
import os
import signal
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest


def _load_telegram_person() -> types.ModuleType:
    """`telegram_person` on a checkout without the `[acceptance]` extra.

    The lock lives beside `session_path()` — the module that owns the session
    path owns the file that guards it — and that module's first import is
    `telethon`, which is an extra CI does not install. So the import needs a
    stand-in when the real one is absent, exactly as
    `tests/test_realtime_probe_preflight.py` puts one on a subprocess's
    `PYTHONPATH`. Nothing exercised here reaches Telegram: the lock is `fcntl`
    and a small JSON record.
    """
    if "telegram_person" in sys.modules:
        return sys.modules["telegram_person"]
    if importlib.util.find_spec("telethon") is None:
        for name, module in _telethon_stand_in().items():
            sys.modules.setdefault(name, module)
    return importlib.import_module("telegram_person")


def _telethon_stand_in() -> dict[str, types.ModuleType]:
    telethon = types.ModuleType("telethon")
    errors = types.ModuleType("telethon.errors")
    errors.SessionPasswordNeededError = type("SessionPasswordNeededError", (Exception,), {})
    telethon.TelegramClient = type("TelegramClient", (), {})
    telethon.errors = errors
    return {"telethon": telethon, "telethon.errors": errors}


person = _load_telegram_person()

REPOSITORY = Path(__file__).resolve().parents[1]

#: The holder a test starts instead of a real acceptance run: it takes the same
#: lock through the same helper, says so, and then does nothing at all. The
#: ticket's manual check is two terminals; this is that, automated.
HOLDER = (
    "import sys\n"
    "import telegram_person\n"
    "lock = telegram_person.PersonSessionLock(\n"
    "    run_directory=sys.argv[1], held_by=telegram_person.ACCEPTANCE_RUN_HOLDER\n"
    ")\n"
    "lock.acquire()\n"
    "print('held', flush=True)\n"
    # Blocked on its own stdin rather than asleep for a guessed number of
    # seconds: the test closes stdin (or kills the process) when it is done, so
    # the holder lives exactly as long as the test needs it to and not one
    # second longer.
    "sys.stdin.readline()\n"
)

#: Every wait below is a local process starting, exiting, or being reaped — none
#: of them is a far-side deadline, and none is a guess about how long the work
#: takes. They are the point at which a *hang* is worth failing rather than
#: waiting out, so they are generous and their only job is to end a stuck test.
PROCESS_SECONDS = 30.0

#: One `pytest` process that refuses in preflight. It imports the harness and
#: writes one JSON file; a minute is the hang boundary, not the expected cost.
REFUSING_RUN_SECONDS = 120.0

#: `flock(LOCK_NB)` returns on the spot. A blocking acquire would not return at
#: all while the holder lives, so anything under this proves it did not wait —
#: it does not measure how fast the refusal is.
NON_BLOCKING_SECONDS = 5.0


def _fake_dependencies(tmp_path: Path) -> Path:
    """A `telethon` a subprocess can import, for the same reason as above."""
    root = tmp_path / "fake-dependencies"
    package = root / "telethon"
    package.mkdir(parents=True, exist_ok=True)
    package.joinpath("__init__.py").write_text("class TelegramClient: pass\n")
    package.joinpath("errors.py").write_text("class SessionPasswordNeededError(Exception): pass\n")
    return root


def _environment(person_directory: Path, tmp_path: Path, **extra: str) -> dict[str, str]:
    python_path = os.pathsep.join(
        part
        for part in (
            str(_fake_dependencies(tmp_path)),
            str(REPOSITORY / "tests" / "acceptance"),
            os.environ.get("PYTHONPATH"),
        )
        if part
    )
    return {
        **os.environ,
        "PYTHONPATH": python_path,
        person.PERSON_DIRECTORY_VARIABLE: str(person_directory),
        **extra,
    }


def _start_holder(person_directory: Path, run_directory: Path, tmp_path: Path):
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(run_directory)],
        cwd=REPOSITORY,
        env=_environment(person_directory, tmp_path),
        # Its own pipe, not pytest's stdin: under capture the parent's stdin is
        # already at end of file, and a holder that inherited it would read that
        # end of file at once and release the lock the test is about.
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    ready = holder.stdout.readline()
    assert ready.strip() == "held", holder.communicate(timeout=PROCESS_SECONDS)
    return holder


def _stop_holder(holder: subprocess.Popen) -> None:
    """Close the holder's stdin, which is what its last line is waiting for."""
    holder.stdin.close()
    holder.wait(timeout=PROCESS_SECONDS)


def _lock(*, run_directory: Path) -> object:
    """The lock as a run takes it, so every test here names the same holder kind."""
    return person.PersonSessionLock(
        run_directory=run_directory, held_by=person.ACCEPTANCE_RUN_HOLDER
    )


@pytest.fixture
def person_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "person"
    directory.mkdir()
    monkeypatch.setenv(person.PERSON_DIRECTORY_VARIABLE, str(directory))
    return directory


def test_a_free_session_lock_is_taken_and_names_its_holder(person_directory: Path) -> None:
    run_directory = person_directory.parent / "run-20260902T000000Z"

    lock = _lock(run_directory=run_directory)
    lock.acquire()
    try:
        recorded = json.loads(person.session_lock_path().read_text())
    finally:
        lock.release()

    assert person.session_lock_path() == person_directory / "person.lock"
    assert recorded == {
        "pid": os.getpid(),
        "run_directory": str(run_directory),
        "held_by": person.ACCEPTANCE_RUN_HOLDER,
    }


def test_the_lock_lives_beside_the_session_the_override_moved(tmp_path: Path) -> None:
    elsewhere = tmp_path / "somewhere-else"

    assert person.session_lock_path(elsewhere).parent == person.session_path(elsewhere).parent


def test_a_lock_another_process_holds_is_refused_by_pid_and_run_directory(
    person_directory: Path, tmp_path: Path
) -> None:
    run_directory = tmp_path / "run-held"
    holder = _start_holder(person_directory, run_directory, tmp_path)
    try:
        with pytest.raises(person.SessionInUse) as refusal:
            _lock(run_directory=tmp_path / "run-second").acquire()
    finally:
        _stop_holder(holder)

    assert str(refusal.value) == (
        "another acceptance run holds the user-account session: "
        f"pid {holder.pid}, run directory {run_directory}"
    )
    assert refusal.value.holder.pid == holder.pid
    assert refusal.value.holder.run_directory == str(run_directory)


def test_a_released_lock_is_free_for_the_next_run(person_directory: Path, tmp_path: Path) -> None:
    first = _lock(run_directory=tmp_path / "run-first")
    first.acquire()
    first.release()

    second = _lock(run_directory=tmp_path / "run-second")
    second.acquire()
    try:
        recorded = json.loads(person.session_lock_path().read_text())
    finally:
        second.release()

    assert recorded["run_directory"] == str(tmp_path / "run-second")


def test_a_killed_holder_leaves_no_stale_lock(person_directory: Path, tmp_path: Path) -> None:
    holder = _start_holder(person_directory, tmp_path / "run-killed", tmp_path)
    holder.send_signal(signal.SIGKILL)
    holder.wait(timeout=PROCESS_SECONDS)

    following = _lock(run_directory=tmp_path / "run-following")
    following.acquire()
    following.release()


def test_status_names_the_run_in_flight_instead_of_a_locked_database(
    person_directory: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    person.session_path().write_text("not a real session, and never opened\n")
    person.store_credentials(person.ApiCredentials(1, "hash"), person_directory)
    run_directory = tmp_path / "run-in-flight"
    holder = _start_holder(person_directory, run_directory, tmp_path)
    try:
        code = person.status(person_directory)
    finally:
        _stop_holder(holder)

    assert code == 1
    assert capsys.readouterr().out.strip() == (
        "IN USE: another acceptance run holds the user-account session: "
        f"pid {holder.pid}, run directory {run_directory}"
    )


def test_a_second_acceptance_run_refuses_before_it_reaches_the_bots(
    person_directory: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole criterion in one run: refused, recorded, and refused *first*.

    The bot tokens are removed from this run's environment, so a refusal that
    named a token would be a refusal that had already walked past the lock.
    """
    run_root = tmp_path / "acceptance-runs"
    held_run = tmp_path / "run-in-flight"
    probe = tmp_path / "rt_prototype.py"
    probe.write_text("# a probe this run never reaches\n")
    holder = _start_holder(person_directory, held_run, tmp_path)
    environment = _environment(
        person_directory,
        tmp_path,
        GPTVOICECODING_ACCEPTANCE_ROOT=str(run_root),
        GPTVOICECODING_ACCEPTANCE_REALTIME_PROBE=str(probe),
    )
    environment.pop("GPTVOICECODING_TELEGRAM_TOKEN", None)
    environment.pop("GPTVOICECODING_TELEGRAM_TOKEN_2", None)

    try:
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "-m", "acceptance", "tests/acceptance", "-q"],
            cwd=REPOSITORY,
            env=environment,
            capture_output=True,
            text=True,
            timeout=REFUSING_RUN_SECONDS,
        )
    finally:
        _stop_holder(holder)

    assert completed.returncode != 0
    verdict_paths = tuple(run_root.glob("*/verdict.json"))
    assert len(verdict_paths) == 1, completed.stdout + completed.stderr
    verdict = json.loads(verdict_paths[0].read_text())
    assert verdict["result"] == "REFUSED"
    assert verdict["reason"] == (
        "another acceptance run holds the user-account session: "
        f"pid {holder.pid}, run directory {held_run}"
    )
    assert not tuple(run_root.glob("*/realtime-probe.log"))
    assert not tuple(run_root.glob("*/journal.jsonl"))


def test_the_holder_record_survives_a_reader_that_arrives_early(
    person_directory: Path, tmp_path: Path
) -> None:
    """An unwritten lock file refuses honestly rather than inventing a holder."""
    person.session_lock_path().write_text("")

    assert person.read_lock_holder() == person.LockHolder(
        pid=None, run_directory=None, held_by=None
    )
    assert str(person.LockHolder(pid=None, run_directory=None, held_by=None)) == (
        "something on this machine holds the user-account session: "
        "pid unknown, run directory unknown"
    )


def test_a_lock_taken_twice_in_one_process_is_still_one_run(
    person_directory: Path, tmp_path: Path
) -> None:
    """`flock` is per open file description, so the second handle must be refused."""
    first = _lock(run_directory=tmp_path / "run-first")
    first.acquire()
    try:
        with pytest.raises(person.SessionInUse):
            _lock(run_directory=tmp_path / "run-second").acquire()
    finally:
        first.release()


def test_the_wait_is_never_taken(person_directory: Path, tmp_path: Path) -> None:
    """The rule is refuse, not queue: the second attempt returns at once."""
    first = _lock(run_directory=tmp_path / "run-first")
    first.acquire()
    started = time.monotonic()
    try:
        with pytest.raises(person.SessionInUse):
            _lock(run_directory=tmp_path / "run-second").acquire()
    finally:
        first.release()

    assert time.monotonic() - started < NON_BLOCKING_SECONDS


def test_an_error_that_is_not_contention_is_raised_as_itself(
    person_directory: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal is evidence, not a catch-all: `ENOTSUP` proves nothing about a run."""

    def unsupported(*_: object) -> None:
        raise OSError(errno.ENOTSUP, "flock not supported on this filesystem")

    monkeypatch.setattr(person.fcntl, "flock", unsupported)

    with pytest.raises(OSError) as failure:
        _lock(run_directory=tmp_path / "run-first").acquire()

    assert not isinstance(failure.value, person.SessionInUse)
    assert failure.value.errno == errno.ENOTSUP


def test_a_holder_that_names_itself_late_is_still_named(person_directory: Path) -> None:
    """The window between taking the lock and writing the record is waited out."""
    person.session_lock_path().write_text("")
    record = json.dumps(
        {"pid": os.getpid(), "run_directory": "/tmp/run-late", "held_by": "a late run"}
    )
    writer = threading.Timer(0.05, person.session_lock_path().write_text, (record,))
    writer.start()
    try:
        holder = person.read_lock_holder()
    finally:
        writer.cancel()

    assert holder == person.LockHolder(os.getpid(), "/tmp/run-late", "a late run")


def test_a_record_naming_a_dead_process_is_never_quoted(
    person_directory: Path, tmp_path: Path
) -> None:
    """The record of a run that has already lost the lock is a ghost, not a holder.

    It is what a contender reads in the syscall between the new holder taking the
    lock and truncating the file, and what a `SIGKILL`ed run leaves behind.
    """
    dead = _a_pid_that_has_exited(person_directory, tmp_path)
    person.session_lock_path().write_text(
        json.dumps({"pid": dead, "run_directory": "/tmp/run-gone", "held_by": "a finished run"})
    )

    assert person.read_lock_holder() == person.LockHolder(None, None, None)


def test_a_record_naming_the_live_holder_is_quoted(person_directory: Path, tmp_path: Path) -> None:
    held = _lock(run_directory=tmp_path / "run-live")
    held.acquire()
    try:
        holder = person.read_lock_holder()
    finally:
        held.release()

    assert holder == person.LockHolder(
        os.getpid(), str(tmp_path / "run-live"), person.ACCEPTANCE_RUN_HOLDER
    )


def test_a_released_lock_leaves_no_identity_behind(person_directory: Path, tmp_path: Path) -> None:
    """A clean handover clears the record, so the next contender quotes nobody."""
    first = _lock(run_directory=tmp_path / "run-first")
    first.acquire()
    first.release()

    assert person.session_lock_path().read_text() == ""
    assert person.read_lock_holder() == person.LockHolder(None, None, None)


def _a_pid_that_has_exited(person_directory: Path, tmp_path: Path) -> int:
    """A pid the kernel has certainly reaped — a holder started and stopped here."""
    holder = _start_holder(person_directory, tmp_path / "run-gone", tmp_path)
    _stop_holder(holder)
    return holder.pid


def test_a_status_check_is_not_reported_as_a_run_that_never_existed(
    person_directory: Path,
) -> None:
    """`status` takes the same lock and has no run directory, and says so."""
    held = person.PersonSessionLock(held_by=person.STATUS_CHECK_HOLDER)
    held.acquire()
    try:
        with pytest.raises(person.SessionInUse) as refusal:
            _lock(run_directory=Path("/tmp/run-second")).acquire()
    finally:
        held.release()

    assert str(refusal.value) == (
        "a `telegram_person.py status` check holds the user-account session: "
        f"pid {os.getpid()}, run directory unknown"
    )


def test_the_preflight_fixture_releases_the_lock_when_it_tears_down(
    person_directory: Path, tmp_path: Path
) -> None:
    """The fixture itself, not a hand-rolled stand-in for it.

    A test that only called `acquire()` and `release()` would still pass if
    `conftest.person_session_lock` forgot its own teardown, which is the one
    thing this is meant to pin.
    """
    fixture = _acceptance_conftest().person_session_lock.__wrapped__
    held = fixture(tmp_path / "run-fixture")
    lock = next(held)
    try:
        with pytest.raises(person.SessionInUse):
            _lock(run_directory=tmp_path / "run-second").acquire()
    finally:
        assert next(held, None) is None

    assert lock.path == person.session_lock_path()
    following = _lock(run_directory=tmp_path / "run-following")
    following.acquire()
    following.release()


def _acceptance_conftest() -> types.ModuleType:
    """The acceptance `conftest` loaded under its own name.

    Under `conftest` it would collide in `sys.modules` with the fast suite's own
    — the collision `tests/conftest.py` records as #93 — so it is given a name of
    its own here.
    """
    location = REPOSITORY / "tests" / "acceptance" / "conftest.py"
    specification = importlib.util.spec_from_file_location("acceptance_conftest", location)
    module = importlib.util.module_from_spec(specification)
    # Registered before it is executed: the module defines dataclasses, and
    # `dataclasses` resolves their annotations through `sys.modules`.
    sys.modules.setdefault("acceptance_conftest", module)
    specification.loader.exec_module(module)
    return sys.modules["acceptance_conftest"]

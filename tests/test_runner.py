"""Starting the engine: what it owns before it exists, and what it refuses.

Two things happen between reading the configuration and building the engine, and
both are this file's subject. The process **takes ownership of its log** (ADR
0004: it opens the file and points its own stdout and stderr at it, so rotation
can rename rather than truncate) and it **cleans its environment once**, at the
one point every process it will ever spawn descends from.

The ordering is what the tests pin, because the ordering is what decides where a
failure can be read. A configuration that cannot be read is refused *before* the
log exists, so it goes to the terminal that started the engine. Everything after
adoption goes to stderr, which by then *is* the log.

The end-to-end case runs a real engine as a real subprocess, because that is the
only place adoption can be observed honestly: in-process, pointing this runner's
stdout at a scratch file would take every later line of test output with it.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from gpt_voicecoding.config import load
from gpt_voicecoding.engine.runner import EXIT_OK, EXIT_REFUSED, adopt_the_log, main

TESTS_DIR = Path(__file__).resolve().parent

#: A prefix no real environment carries, so a test can put noise in its own
#: environment and watch the engine drop it.
NOISE = "GVC_TEST_NOISE_"

CONFIG = """
[engine]
socket_path = "{socket}"
state_path = "{state}"

[adapters]
call = "fakes:FakeCall"
companion_channel = "fakes:FakeCompanionChannel"
session_launcher = "fakes:FakeSessionLauncher"

[adapters.agents]
codex = "fakes:FakeAgent"

[delegate]
model = "the-model-the-user-chose"

[log]
path = "{log}"
max_bytes = 4096
retained_files = 2
stripped_environment_prefixes = ["{noise}"]
"""


@pytest.fixture
def home() -> Iterator[Path]:
    """A short directory: Darwin caps an AF_UNIX path at 103 bytes."""
    base = Path(tempfile.mkdtemp(prefix="gvc-", dir="/tmp"))
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


@pytest.fixture
def configured(home: Path) -> Path:
    path = home / "config.toml"
    path.write_text(
        CONFIG.format(
            socket=home / "control.sock",
            state=home / "state.json",
            log=home / "engine.log",
            noise=NOISE,
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture(autouse=True)
def root_logger_left_as_found() -> Iterator[None]:
    """Adoption installs a handler on the root logger; a test may not keep it."""
    root = logging.getLogger()
    before = list(root.handlers)
    level = root.level
    yield
    for handler in list(root.handlers):
        if handler not in before:
            root.removeHandler(handler)
    root.setLevel(level)


class TestWhatIsOwnedBeforeTheEngineExists:
    def test_the_log_is_opened_and_the_environment_is_cleaned(
        self, home: Path, configured: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(f"{NOISE}ONE", "1")
        monkeypatch.setenv("PATH_LIKE_ANY_OTHER", "kept")

        adopt_the_log(load(configured), check_seconds=None, redirect_standard_streams=False)

        assert f"{NOISE}ONE" not in os.environ
        assert os.environ["PATH_LIKE_ANY_OTHER"] == "kept"
        # A variable that vanishes silently is the same kind of surprise as the
        # one that filled the reference implementation's log.
        assert f"{NOISE}ONE" in (home / "engine.log").read_text()

    def test_the_log_directory_does_not_have_to_exist_yet(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        nested = home / "never" / "created" / "engine.log"
        path = home / "config.toml"
        path.write_text(
            CONFIG.format(
                socket=home / "control.sock",
                state=home / "state.json",
                log=nested,
                noise=NOISE,
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv(f"{NOISE}TWO", "2")

        adopt_the_log(load(path), check_seconds=None, redirect_standard_streams=False)

        assert nested.exists()

    def test_a_clean_environment_writes_no_notice(
        self, home: Path, configured: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(f"{NOISE}ONE", raising=False)

        adopt_the_log(load(configured), check_seconds=None, redirect_standard_streams=False)

        assert "dropped inherited environment variables" not in (home / "engine.log").read_text()

    def test_the_log_opens_by_stating_its_own_bound(self, home: Path, configured: Path) -> None:
        """A truncated history should say, in itself, that it was bounded on purpose."""
        adopt_the_log(load(configured), check_seconds=None, redirect_standard_streams=False)

        written = (home / "engine.log").read_text()
        assert str(home / "engine.log") in written
        assert "4096" in written and "2" in written


class TestWhichSideOfAdoptionARefusalLandsOn:
    def test_an_unreadable_configuration_is_spoken_on_the_terminal(
        self, home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """There is no log yet, so the terminal is the only place it can be seen."""
        code = main(
            ["--config", str(home / "absent.toml")],
            check_seconds=None,
            redirect_standard_streams=False,
        )

        assert code == EXIT_REFUSED
        assert "the engine cannot start" in capsys.readouterr().err
        assert not (home / "engine.log").exists()

    def test_an_adapter_that_cannot_be_loaded_is_refused_once_the_log_is_owned(
        self, home: Path, configured: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        configured.write_text(
            configured.read_text().replace("fakes:FakeCall", "fakes:NoSuchCall"), encoding="utf-8"
        )

        code = main(
            ["--config", str(configured)],
            check_seconds=None,
            redirect_standard_streams=False,
        )

        assert code == EXIT_REFUSED
        assert "the engine cannot start" in capsys.readouterr().err
        # Adoption happened first, so the engine that died here had a log — which
        # is what a shell-restarted engine leaves behind to be read.
        assert (home / "engine.log").exists()

    def test_an_adapter_whose_far_side_is_absent_is_a_refusal_and_not_a_crash(
        self, home: Path, configured: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The docstring's promise: the exit code says the same thing either way.

        It did not. Only `OSError` was caught around the serve, so an adapter
        raising its own exception type — which every shipped one does; the Codex
        app-server says `no 'codex' on PATH` — left the engine exiting **1** with
        a traceback instead of **2** with a sentence.

        Found from the app bundle, where it is at its worst: after adoption
        stderr *is* the log, so the menu-bar shell's stderr panel gets nothing,
        and the shell restarts on every exit. A first-run misconfiguration
        presented as a silent crash loop.
        """
        configured.write_text(
            configured.read_text().replace("fakes:FakeCall", "fakes:RefusingCall"),
            encoding="utf-8",
        )

        code = main(
            ["--config", str(configured)],
            check_seconds=None,
            redirect_standard_streams=False,
        )

        printed = capsys.readouterr()
        assert code == EXIT_REFUSED
        assert "the engine cannot start" in printed.err
        # The adapter's own words, not a category. "Something went wrong" is the
        # least actionable sentence a refusal can carry.
        assert "the far side of this seam is not there" in printed.err

    def test_a_refusal_keeps_the_traceback_the_only_diagnostic_a_bug_would_have(
        self, home: Path, configured: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A sentence for the human, the whole traceback for the log.

        A `TypeError` inside an adapter's `connect` is a bug, not a refusal, and
        collapsing it to one line would throw away the only thing that could
        explain it. Both, on purpose: the last line is a sentence and the log
        keeps everything above it.
        """
        configured.write_text(
            configured.read_text().replace("fakes:FakeCall", "fakes:RefusingCall"),
            encoding="utf-8",
        )

        with caplog.at_level(logging.ERROR):
            main(["--config", str(configured)], check_seconds=None, redirect_standard_streams=False)

        recorded = [record for record in caplog.records if record.exc_info]
        assert recorded, "the refusal carried no traceback into the log"
        assert "UnreachableFarSide" in "".join(
            logging.Formatter().formatException(record.exc_info)  # type: ignore[arg-type]
            for record in recorded
        )


class TestARealEngineOwningARealLog:
    """The whole of ADR 0004's Done-when, in one process that is actually served."""

    def test_it_logs_through_the_file_it_owns_and_says_nothing_on_the_terminal(
        self, home: Path, configured: Path
    ) -> None:
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(TESTS_DIR)
        environment[f"{NOISE}INHERITED"] = "1"

        engine = subprocess.Popen(
            [sys.executable, "-m", "gpt_voicecoding.engine", "--config", str(configured)],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            socket_path = home / "control.sock"
            deadline = time.monotonic() + 20
            while not socket_path.exists() and time.monotonic() < deadline:
                assert engine.poll() is None, "the engine exited before it served"
                time.sleep(0.05)
            assert socket_path.exists(), "the engine never bound its socket"
        finally:
            engine.send_signal(signal.SIGTERM)
            stdout, stderr = engine.communicate(timeout=20)

        assert engine.returncode == EXIT_OK
        # Adoption took both streams, so the terminal that started it sees
        # nothing at all — every byte went to the file the engine owns.
        assert stdout == ""
        assert stderr == ""
        written = (home / "engine.log").read_text()
        assert f"{NOISE}INHERITED" in written
        assert "dropped inherited environment variables" in written

"""`bridgectl`, driven the way a person drives it: one line in, one answer out.

The surface is thin on purpose, so these tests are about the three things a thin
surface can still get wrong — asking the wrong engine, hiding a refusal, and
failing to tell "the engine said no" apart from "there is no engine".

It is exercised against a real assembled engine over a real socket, because a
surface tested against a mock of its own protocol proves only that the mock
agrees with it.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from fakes import FakeSessionLauncher
from gpt_voicecoding.cli import main
from gpt_voicecoding.config import load
from gpt_voicecoding.engine.composition import Engine
from gpt_voicecoding.seams.identity import AgentKind, SessionTarget

CODEX = SessionTarget(agent=AgentKind.CODEX, session_id="abc")


def one_session_launcher(*, sink: object = None) -> FakeSessionLauncher:
    """A Launcher with exactly one Session to hand out."""
    return FakeSessionLauncher(targets=[CODEX], sink=sink)  # type: ignore[arg-type]


CONFIG = """
[engine]
socket_path = "{socket}"
state_path = "{state}"

[adapters]
call = "fakes:FakeCall"
companion_channel = "fakes:FakeCompanionChannel"
session_launcher = "test_bridgectl:one_session_launcher"

[adapters.agents]
codex = "fakes:FakeAgent"

[delegate]
model = "the-model-the-user-chose"

[log]
path = "{log}"
max_bytes = 4096
retained_files = 2
stripped_environment_prefixes = ["GVC_TEST_NOISE_"]
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
def engine_at(home: Path) -> Iterator[Path]:
    """One engine, running in its own loop on another thread, and its config path.

    `bridgectl` runs its own `asyncio.run`, exactly as the console script does,
    so the engine it talks to cannot share this thread's loop.
    """
    config_path = home / "config.toml"
    config_path.write_text(
        CONFIG.format(
            socket=home / "control.sock",
            state=home / "state.json",
            log=home / "engine.log",
        ),
        encoding="utf-8",
    )
    engine = Engine.assemble(load(config_path))
    serving = threading.Event()
    stopping = asyncio.Event()

    async def serve() -> None:
        await engine.start()
        serving.set()
        await stopping.wait()
        await engine.aclose()

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_until_complete, args=(serve(),), daemon=True)
    thread.start()
    serving.wait(timeout=5)
    try:
        yield config_path
    finally:
        loop.call_soon_threadsafe(stopping.set)
        thread.join(timeout=5)
        loop.close()


class TestAskingARunningEngine:
    def test_status_prints_what_the_hub_answered(
        self, engine_at: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["--config", str(engine_at), "status"])

        assert code == 0
        assert "duty off" in capsys.readouterr().out

    def test_a_switch_is_flipped_and_the_previous_state_reported(
        self, engine_at: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["--config", str(engine_at), "switch", "duty", "on"]) == 0

        assert "duty is on (was off)" in capsys.readouterr().out
        assert main(["--config", str(engine_at), "status"]) == 0
        assert "duty on" in capsys.readouterr().out

    def test_the_live_toggle_is_one_command(
        self, engine_at: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["--config", str(engine_at), "live"]) == 0
        assert "the Live Call is up" in capsys.readouterr().out

        assert main(["--config", str(engine_at), "live"]) == 0
        assert "no Live Call is up" in capsys.readouterr().out

    def test_the_socket_may_be_named_directly(
        self, engine_at: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        socket_path = load(engine_at).socket_path

        assert main(["--socket", str(socket_path), "verify"]) == 0
        assert "call: pass" in capsys.readouterr().out


class TestTheWholeSessionCommandSet:
    """The chain #3 asks for end to end: CLI, socket, hub, Launcher, and back."""

    def test_launch_then_roster_then_relay_then_close(
        self, engine_at: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config = ["--config", str(engine_at)]
        workspace = engine_at.parent

        assert main([*config, "sessions"]) == 0
        assert "sessions: none" in capsys.readouterr().out

        assert main([*config, "launch", "codex", str(workspace), "a project · a task"]) == 0
        assert "launched codex:abc" in capsys.readouterr().out

        assert main([*config, "sessions"]) == 0
        roster = capsys.readouterr().out
        assert "a project · a task" in roster and "codex:abc" in roster
        assert str(workspace) in roster

        assert main([*config, "relay", "codex:abc", "carry", "on"]) == 0
        assert "deliver" in capsys.readouterr().out

        assert main([*config, "close", "codex:abc"]) == 0
        assert "closed" in capsys.readouterr().out

        assert main([*config, "close", "codex:abc"]) == 0
        assert "already closed" in capsys.readouterr().out

    def test_a_session_that_was_never_launched_cannot_be_closed(
        self, engine_at: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["--config", str(engine_at), "close", "codex:never-seen"])

        assert code == 1
        assert "unknown Session" in capsys.readouterr().err

    def test_a_claude_session_named_without_a_pid_is_refused(
        self, engine_at: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`--resume` forks a second process under the same session id."""
        code = main(["--config", str(engine_at), "close", "claude:abc"])

        assert code == 1
        assert "pid" in capsys.readouterr().err


class TestSayingNoOutLoud:
    def test_a_refusal_is_the_hubs_own_words_and_a_different_exit_code(
        self, engine_at: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["--config", str(engine_at), "switch", "sound", "on"])

        assert code == 1
        assert "unknown switch: 'sound'" in capsys.readouterr().err

    def test_an_unknown_command_names_the_ones_there_are(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["--socket", "/tmp/nothing.sock", "duty_toggle"])

        assert code == 2
        assert "status" in capsys.readouterr().err

    def test_a_command_written_wrongly_is_shown_how(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["--socket", "/tmp/nothing.sock", "switch", "duty"])

        assert code == 2
        assert "switch <name> on|off" in capsys.readouterr().err


class TestNoEngineAtAll:
    def test_an_engine_that_is_not_running_is_not_a_refusal(
        self, home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["--socket", str(home / "absent.sock"), "--timeout", "0.5", "status"])

        assert code == 2
        assert str(home / "absent.sock") in capsys.readouterr().err

    def test_no_configuration_and_no_socket_says_both(
        self, home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["--config", str(home / "absent.toml"), "status"])

        assert code == 2
        error = capsys.readouterr().err
        assert str(home / "absent.toml") in error
        assert "--socket" in error

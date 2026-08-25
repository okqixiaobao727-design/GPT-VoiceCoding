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

from fakes import FakeCall
from gpt_voicecoding.cli import main
from gpt_voicecoding.config import load
from gpt_voicecoding.control_plane.client import DEFAULT_TIMEOUT_SECONDS, EngineUnreachable
from gpt_voicecoding.engine.composition import Engine
from gpt_voicecoding.seams.call import CallSnapshot
from gpt_voicecoding.seams.control_plane import Reply

#: Longer than any deadline these tests hand the surface, and short enough that
#: the engine's own shutdown does not wait on it. It stands in for the real
#: thing #28 was found on: an action that outruns the client's patience.
SLOWER_THAN_ANY_DEADLINE_SECONDS = 3.0


class SlowCall(FakeCall):
    """A Call adapter that is slow, so an action can time out against it."""

    async def ensure_call(self, instructions: str) -> CallSnapshot:
        await asyncio.sleep(SLOWER_THAN_ANY_DEADLINE_SECONDS)
        return await super().ensure_call(instructions)


def slow_call(*, sink: object = None, **rest: object) -> SlowCall:
    return SlowCall(sink=sink)  # type: ignore[arg-type]


CONFIG = """
[engine]
socket_path = "{socket}"
state_path = "{state}"

[adapters]
call = "fakes:FakeCall"
companion_channel = "fakes:FakeCompanionChannel"

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


#: The same engine, wired to adapters that outlast the surface's patience.
SLOW_CONFIG = CONFIG.replace('call = "fakes:FakeCall"', 'call = "test_bridgectl:slow_call"')


@pytest.fixture
def slow_engine_at(home: Path) -> Iterator[Path]:
    """An engine that answers, but not before the surface has stopped listening."""
    yield from _engine_serving(home, SLOW_CONFIG)


@pytest.fixture
def engine_at(home: Path) -> Iterator[Path]:
    """One engine, running in its own loop on another thread, and its config path.

    `bridgectl` runs its own `asyncio.run`, exactly as the console script does,
    so the engine it talks to cannot share this thread's loop.
    """
    yield from _engine_serving(home, CONFIG)


def _engine_serving(home: Path, config: str) -> Iterator[Path]:
    """One assembled engine, served on its own thread, and torn down after."""
    config_path = home / "config.toml"
    config_path.write_text(
        config.format(
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


class TestActingOnASessionThatIsNotThere:
    """Addressing is exact, and an address nothing registered is refused.

    The launch-through-close chain this class used to walk went with the
    launcher (#72): nothing registers a Session at runtime until discovery
    lands, so the roster is empty here and the refusals are what remain
    reachable. They are the half that mattered — an address is never guessed at.
    """

    def test_a_session_that_was_never_registered_cannot_be_reached(
        self, engine_at: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["--config", str(engine_at), "relay", "codex:never-seen", "carry", "on"])

        assert code == 1
        assert "unknown Session" in capsys.readouterr().err

    def test_a_claude_session_named_without_a_pid_is_refused(
        self, engine_at: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`--resume` forks a second process under the same session id."""
        code = main(["--config", str(engine_at), "relay", "claude:abc", "carry", "on"])

        assert code == 1
        assert "pid" in capsys.readouterr().err

    def test_an_empty_roster_says_so(
        self, engine_at: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["--config", str(engine_at), "sessions"]) == 0
        assert "sessions: none" in capsys.readouterr().out


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


class TestAnEngineThatTakesTooLong:
    """A silent deadline is this surface's own, and is reported as its own.

    #28 was the opposite: an action still in flight reported as one that failed.
    Nothing is invented about the engine's side — it said nothing, so nothing
    is said on its behalf.
    """

    def test_a_silent_engine_is_not_reported_as_a_refusal(
        self, slow_engine_at: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["--config", str(slow_engine_at), "--timeout", "0.3", "live"])

        assert code == 2
        assert "did not answer within 0.3s" in capsys.readouterr().err

    def test_an_explicit_timeout_outranks_the_action_default(
        self, slow_engine_at: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Proved by the sentence naming the operator's number, not the default's."""
        code = main(["--config", str(slow_engine_at), "--timeout", "0.3", "live"])

        assert code == 2
        assert "did not answer within 0.3s" in capsys.readouterr().err

    def test_the_action_default_is_what_reaches_the_wire(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The deadline is read off the action rather than written at the call site."""
        waited: list[float] = []

        async def record(request: object, *, path: Path, timeout: float) -> Reply:
            waited.append(timeout)
            # Refused rather than answered: this test is about the deadline that
            # reached the wire, and rendering a reply is another test's business.
            raise EngineUnreachable("this engine is a stand-in")

        monkeypatch.setattr("gpt_voicecoding.cli.bridgectl.ask", record)
        socket = ["--socket", str(home / "control.sock")]
        main([*socket, "live"])
        main([*socket, "status"])

        assert waited == [DEFAULT_TIMEOUT_SECONDS, DEFAULT_TIMEOUT_SECONDS]

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

from fakes import FakeCall, FakeSessionLauncher
from gpt_voicecoding.cli import main
from gpt_voicecoding.config import load
from gpt_voicecoding.control_plane.client import (
    DEFAULT_TIMEOUT_SECONDS,
    LAUNCH_TIMEOUT_SECONDS,
    EngineUnreachable,
)
from gpt_voicecoding.control_plane.commands import CommandError, build_request
from gpt_voicecoding.engine.composition import Engine
from gpt_voicecoding.seams.call import CallSnapshot
from gpt_voicecoding.seams.control_plane import Action, Reply
from gpt_voicecoding.seams.identity import AgentKind, SessionTarget
from gpt_voicecoding.seams.session_launcher import LaunchOutcome, LaunchRequest

CODEX = SessionTarget(agent=AgentKind.CODEX, session_id="abc")
LAUNCH_REQUEST_ID = "21d73168-b1f0-4b18-977d-fba0d1f2cc13"


class TestTheLaunchCommand:
    def test_project_and_task_enter_the_launch_payload_without_an_agent(self) -> None:
        request = build_request(
            "launch",
            [
                "--request-id",
                LAUNCH_REQUEST_ID,
                "--project",
                "GPT Live",
                "--task",
                "build",
                "the control plane",
            ],
        )

        assert request.action is Action.LAUNCH
        assert dict(request.payload) == {
            "request_id": LAUNCH_REQUEST_ID,
            "project": "GPT Live",
            "task": "build the control plane",
        }

    def test_an_explicit_agent_enters_the_launch_payload(self) -> None:
        request = build_request(
            "launch",
            [
                "--request-id",
                LAUNCH_REQUEST_ID,
                "--project",
                "GPT Live",
                "--agent",
                "codex",
                "--task",
                "build the control plane",
            ],
        )

        assert dict(request.payload) == {
            "request_id": LAUNCH_REQUEST_ID,
            "project": "GPT Live",
            "task": "build the control plane",
            "agent": "codex",
        }

    def test_a_positional_request_identity_is_not_a_second_interface(self) -> None:
        with pytest.raises(CommandError):
            build_request(
                "launch",
                [LAUNCH_REQUEST_ID, "codex", "/tmp/workspace", "a project · a task"],
            )


def one_session_launcher(*, sink: object = None) -> FakeSessionLauncher:
    """A Launcher with exactly one Session to hand out."""
    return FakeSessionLauncher(targets=[CODEX], sink=sink)  # type: ignore[arg-type]


#: Longer than any deadline these tests hand the surface, and short enough that
#: the engine's own shutdown does not wait on it. It stands in for the real
#: thing #28 was found on: a cold launch that outruns the client's patience.
SLOWER_THAN_ANY_DEADLINE_SECONDS = 3.0


class SlowSessionLauncher(FakeSessionLauncher):
    """A Launcher that is still working when the surface has given up waiting."""

    async def launch(self, request: LaunchRequest) -> LaunchOutcome:
        await asyncio.sleep(SLOWER_THAN_ANY_DEADLINE_SECONDS)
        return await super().launch(request)


class SlowCall(FakeCall):
    """A Call adapter that is slow, so a *non*-launch action can time out too."""

    async def ensure_call(self, instructions: str) -> CallSnapshot:
        await asyncio.sleep(SLOWER_THAN_ANY_DEADLINE_SECONDS)
        return await super().ensure_call(instructions)


def slow_session_launcher(*, sink: object = None) -> SlowSessionLauncher:
    return SlowSessionLauncher(targets=[CODEX], sink=sink)  # type: ignore[arg-type]


def slow_call(*, sink: object = None, **rest: object) -> SlowCall:
    return SlowCall(sink=sink)  # type: ignore[arg-type]


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

[launch]
default_agent = "codex"

[[launch.projects]]
name = "a project"
workspace = "{workspace}"
spoken_aliases = ["spoken project"]

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
SLOW_CONFIG = CONFIG.replace(
    'call = "fakes:FakeCall"', 'call = "test_bridgectl:slow_call"'
).replace(
    'session_launcher = "test_bridgectl:one_session_launcher"',
    'session_launcher = "test_bridgectl:slow_session_launcher"',
)


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
            workspace=home,
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
        assert main([*config, "sessions"]) == 0
        assert "sessions: none" in capsys.readouterr().out

        assert (
            main(
                [
                    *config,
                    "launch",
                    "--request-id",
                    LAUNCH_REQUEST_ID,
                    "--project",
                    "spoken project",
                    "--agent",
                    "codex",
                    "--task",
                    "a task",
                ]
            )
            == 0
        )
        assert "launched codex:abc" in capsys.readouterr().out

        assert main([*config, "sessions"]) == 0
        roster = capsys.readouterr().out
        assert "a project · a task" in roster and "codex:abc" in roster
        assert str(engine_at.parent) in roster

        assert main([*config, "relay", "codex:abc", "carry", "on"]) == 0
        assert "deliver" in capsys.readouterr().out

        assert main([*config, "close", "codex:abc"]) == 0
        assert "closed" in capsys.readouterr().out

        assert main([*config, "close", "codex:abc"]) == 0
        assert "already closed" in capsys.readouterr().out

    def test_repeating_one_launch_command_returns_the_first_result_and_one_session(
        self, engine_at: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        command = [
            "--config",
            str(engine_at),
            "launch",
            "--request-id",
            LAUNCH_REQUEST_ID,
            "--project",
            "spoken project",
            "--agent",
            "codex",
            "--task",
            "a task",
        ]

        assert main(command) == 0
        first = capsys.readouterr().out
        assert main(command) == 0
        second = capsys.readouterr().out
        assert first == second

        assert main(["--config", str(engine_at), "sessions"]) == 0
        roster = capsys.readouterr().out
        assert roster.count("a project · a task") == 1
        assert roster.count("codex:abc") == 1

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


class TestALaunchThatOutrunsTheDeadline:
    """A launch that is still in flight is not a launch that failed (#28).

    The engine holds an in-flight launch under its request id and joins a repeat
    to it, so re-issuing the *identical* command is the safe recovery. That is
    worth nothing if the operator cannot learn it at the moment it is needed,
    and the obvious guess — retry with a fresh id — is precisely the one that
    starts a second agent in the same workspace.
    """

    def test_the_operator_is_told_the_launch_may_still_be_running(
        self, slow_engine_at: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(
            [
                "--config",
                str(slow_engine_at),
                "--timeout",
                "0.3",
                "launch",
                "--request-id",
                LAUNCH_REQUEST_ID,
                "--project",
                "a project",
                "--task",
                "say hello",
            ]
        )

        assert code == 2
        error = capsys.readouterr().err
        # Not "it failed": the launch's fate is genuinely unknown to this surface.
        assert "may still be in flight" in error

    def test_the_recovery_names_the_operator_s_own_request_id(
        self, slow_engine_at: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Quoted back, so the recovery is copyable rather than merely described."""
        main(
            [
                "--config",
                str(slow_engine_at),
                "--timeout",
                "0.3",
                "launch",
                "--request-id",
                LAUNCH_REQUEST_ID,
                "--project",
                "a project",
                "--task",
                "say hello",
            ]
        )

        error = capsys.readouterr().err
        assert LAUNCH_REQUEST_ID in error
        assert "--request-id" in error

    def test_it_warns_that_a_fresh_request_id_would_start_a_second_agent(
        self, slow_engine_at: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The wrong guess is named, because naming only the right move invites it."""
        main(
            [
                "--config",
                str(slow_engine_at),
                "--timeout",
                "0.3",
                "launch",
                "--request-id",
                LAUNCH_REQUEST_ID,
                "--project",
                "a project",
                "--task",
                "say hello",
            ]
        )

        assert "second agent" in capsys.readouterr().err

    def test_an_action_that_is_not_a_launch_keeps_the_plain_sentence(
        self, slow_engine_at: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """There is no in-flight launch to join, so there is nothing to advise."""
        code = main(
            [
                "--config",
                str(slow_engine_at),
                "--timeout",
                "0.3",
                "live",
            ]
        )

        assert code == 2
        error = capsys.readouterr().err
        assert "did not answer within 0.3s" in error
        assert "request-id" not in error

    def test_no_engine_at_all_is_never_told_a_launch_may_be_in_flight(
        self, home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Nothing was ever reached, so nothing is running — advising a rejoin would
        be the same untruth this ticket exists to remove, pointed the other way."""
        code = main(
            [
                "--socket",
                str(home / "absent.sock"),
                "launch",
                "--request-id",
                LAUNCH_REQUEST_ID,
                "--project",
                "a project",
                "--task",
                "say hello",
            ]
        )

        assert code == 2
        error = capsys.readouterr().err
        assert str(home / "absent.sock") in error
        assert "may still be in flight" not in error
        assert "second agent" not in error


class TestTheDeadlineTheOperatorAsked:
    """`--timeout` is the operator's, and it outranks the per-action default."""

    def test_an_explicit_timeout_overrides_the_launch_default(
        self, slow_engine_at: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Proved by the sentence naming the operator's number, not the default's:
        had the 150s launch deadline applied, this test would still be waiting."""
        code = main(
            [
                "--config",
                str(slow_engine_at),
                "--timeout",
                "0.3",
                "launch",
                "--request-id",
                LAUNCH_REQUEST_ID,
                "--project",
                "a project",
                "--task",
                "say hello",
            ]
        )

        assert code == 2
        assert "did not answer within 0.3s" in capsys.readouterr().err


class TestASlowLaunchIsNotAFailure:
    """The first thing #28 asks for: a launch that is merely slow still succeeds."""

    def test_a_launch_slower_than_an_ordinary_action_still_reports_success(
        self, slow_engine_at: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(
            [
                "--config",
                str(slow_engine_at),
                "launch",
                "--request-id",
                LAUNCH_REQUEST_ID,
                "--project",
                "a project",
                "--task",
                "say hello",
            ]
        )

        assert code == 0
        assert "launched codex:abc" in capsys.readouterr().out

    def test_a_launch_is_given_the_derived_deadline_and_other_actions_are_not(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The number reaches the wire. Asserted here rather than by spending it:
        a test that actually waited out the launch deadline would take 150s."""
        waited: list[float] = []

        async def record(request: object, *, path: Path, timeout: float) -> Reply:
            waited.append(timeout)
            # Refused rather than answered: this test is about the deadline that
            # reached the wire, and rendering a reply is another test's business.
            raise EngineUnreachable("this engine is a stand-in")

        monkeypatch.setattr("gpt_voicecoding.cli.bridgectl.ask", record)
        socket = ["--socket", str(home / "control.sock")]
        launch = ["launch", "--request-id", LAUNCH_REQUEST_ID, "--project", "p", "--task", "t"]
        main([*socket, *launch])
        main([*socket, "status"])

        assert waited == [LAUNCH_TIMEOUT_SECONDS, DEFAULT_TIMEOUT_SECONDS]

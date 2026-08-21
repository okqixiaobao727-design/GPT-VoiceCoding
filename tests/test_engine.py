"""The composition root: config in, one running engine out.

This is the file that proves headless operation is real. Nothing here knows
about a menu-bar shell: a configuration file names the adapters, the engine
assembles one Bridge Core behind them, serves the control plane, carries events
from adapters into the hub, and shuts down without leaving a socket behind.

Adapter selection is exercised the way it will actually be used — a factory
reference in a file, imported by the one thing allowed to import an adapter.
The fakes stand in for the real ones, which is exactly what ADR 0001's fourth
principle asks of every part of this system.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from fakes import FakeAgent, FakeCall, FakeCompanionChannel, FakeSessionLauncher
from gpt_voicecoding.config import load
from gpt_voicecoding.control_plane.client import ask
from gpt_voicecoding.control_plane.server import AlreadyServing
from gpt_voicecoding.engine.composition import Engine, EngineAssemblyError
from gpt_voicecoding.seams.agent import ReplyWindow, ReplyWindowChanged
from gpt_voicecoding.seams.companion_channel import InboundText
from gpt_voicecoding.seams.control_plane import Action, Reply, Request
from gpt_voicecoding.seams.identity import AgentKind, SessionTarget

CODEX = SessionTarget(agent=AgentKind.CODEX, session_id="abc")


def one_session_launcher(*, sink: object = None) -> FakeSessionLauncher:
    """A Launcher factory with exactly one Session to hand out.

    Named in a configuration file the way a real adapter's factory will be —
    which is the point: the composition root imports whatever the file names,
    and a test's own module is as legitimate a source as a shipped adapter.
    """
    return FakeSessionLauncher(targets=[CODEX], sink=sink)  # type: ignore[arg-type]


class RidingCall(FakeCall):
    """A Call adapter that rides an app-server some other spoke owns."""

    def __init__(self, **held: object) -> None:
        super().__init__(**held)  # type: ignore[arg-type]
        self.riding: object = None

    def use_app_server(self, server: object) -> None:
        self.riding = server


class ServerOwningAgent(FakeAgent):
    """An Agent adapter that owns one, as the Codex adapter really does."""

    app_server = "the one app-server this engine spawns"


def call_that_rides(*, sink: object = None) -> RidingCall:
    return RidingCall(sink=sink)


def agent_that_owns_one(*, sink: object = None) -> ServerOwningAgent:
    return ServerOwningAgent(sink=sink)  # type: ignore[arg-type]


CONFIG = """
[engine]
socket_path = "{socket}"
state_path = "{state}"

[adapters]
call = "fakes:FakeCall"
companion_channel = "fakes:FakeCompanionChannel"
session_launcher = "test_engine:one_session_launcher"

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


def configured(home: Path, text: str = CONFIG) -> Path:
    path = home / "config.toml"
    path.write_text(
        text.format(
            socket=home / "control.sock",
            state=home / "state.json",
            log=home / "engine.log",
        ),
        encoding="utf-8",
    )
    return path


def assembled(home: Path, text: str = CONFIG) -> Engine:
    return Engine.assemble(load(configured(home, text)))


async def running(engine: Engine, work) -> object:
    """Run the engine, do one thing against it, and shut it down."""
    await engine.start()
    try:
        return await work()
    finally:
        await engine.aclose()


class TestAssembly:
    def test_the_engine_serves_the_control_plane_it_was_configured_with(self, home: Path) -> None:
        engine = assembled(home)

        async def scenario() -> Reply:
            return await running(
                engine, lambda: ask(Request(action=Action.STATUS), path=engine.socket_path)
            )

        reply = asyncio.run(scenario())

        assert reply.ok
        assert reply.data["switches"]["duty"] is False

    def test_the_adapters_are_the_ones_the_file_named(self, home: Path) -> None:
        """Config-driven, not hard-coded: the engine reports what it loaded."""
        engine = assembled(home)

        async def scenario() -> Reply:
            return await running(
                engine, lambda: ask(Request(action=Action.VERIFY), path=engine.socket_path)
            )

        seams = {row["seam"]: row for row in asyncio.run(scenario()).data["seams"]}

        assert set(seams) == {"call", "companion_channel", "session_launcher", "agent.codex"}
        assert seams["call"]["configured"] == "fakes:FakeCall"
        # The adapter names itself; the engine never echoes the file back at you.
        assert seams["call"]["loaded"] == "tests.fakes.FakeCall"
        assert {row["outcome"] for row in seams.values()} == {"pass"}

    def test_a_factory_that_is_not_there_is_a_named_refusal(self, home: Path) -> None:
        text = CONFIG.replace("fakes:FakeCall", "fakes:NoSuchCall")

        with pytest.raises(EngineAssemblyError) as refusal:
            assembled(home, text)

        assert "fakes:NoSuchCall" in str(refusal.value)

    def test_a_module_that_is_not_there_is_a_named_refusal(self, home: Path) -> None:
        text = CONFIG.replace("fakes:FakeCall", "no_such_module:Anything")

        with pytest.raises(EngineAssemblyError) as refusal:
            assembled(home, text)

        assert "no_such_module" in str(refusal.value)

    def test_the_socket_is_gone_after_a_clean_shutdown(self, home: Path) -> None:
        engine = assembled(home)

        asyncio.run(running(engine, lambda: asyncio.sleep(0)))

        assert not engine.socket_path.exists()


#: What a `Connectable` adapter did, in order, across one engine's life.
LIFECYCLE: list[str] = []


class ConnectingCall(FakeCall):
    """A Call adapter with a connection of its own — the `Connectable` shape."""

    async def connect(self) -> None:
        LIFECYCLE.append("connect")

    async def aclose(self) -> None:
        LIFECYCLE.append("aclose")


class RefusingCall(ConnectingCall):
    """One whose far side is not there. Opening it must stop the engine."""

    async def connect(self) -> None:
        raise ConnectionError("the far side is not there")


class ConnectingChannel(FakeCompanionChannel):
    """A Companion Channel that also has something of its own to open."""

    async def connect(self) -> None:
        LIFECYCLE.append("channel connect")

    async def aclose(self) -> None:
        LIFECYCLE.append("channel aclose")


class RefusingChannel(ConnectingChannel):
    """Opens second, and fails — so the first adapter is already holding something."""

    async def connect(self) -> None:
        raise ConnectionError("this channel's far side is not there either")


class TestAdapterLifecycle:
    def test_an_adapter_with_a_connection_is_opened_and_closed(self, home: Path) -> None:
        LIFECYCLE.clear()
        engine = assembled(home, CONFIG.replace("fakes:FakeCall", "test_engine:ConnectingCall"))

        asyncio.run(running(engine, lambda: asyncio.sleep(0)))

        assert LIFECYCLE == ["connect", "aclose"]

    def test_an_adapter_that_cannot_open_stops_the_start(self, home: Path) -> None:
        """An engine answering over seams that never connected is the outage."""
        engine = assembled(home, CONFIG.replace("fakes:FakeCall", "test_engine:RefusingCall"))

        with pytest.raises(ConnectionError):
            asyncio.run(engine.start())

        assert not engine.socket_path.exists()

    def test_a_start_that_fails_closes_what_it_had_already_opened(self, home: Path) -> None:
        """Otherwise the caller holds an exception and no way to release anything."""
        LIFECYCLE.clear()
        engine = assembled(
            home,
            CONFIG.replace("fakes:FakeCall", "test_engine:ConnectingCall").replace(
                "fakes:FakeCompanionChannel", "test_engine:RefusingChannel"
            ),
        )

        with pytest.raises(ConnectionError):
            asyncio.run(engine.start())

        assert LIFECYCLE == ["connect", "aclose"]
        assert not engine.socket_path.exists()

    def test_a_start_refused_by_a_live_engine_leaves_its_socket_alone(self, home: Path) -> None:
        """Never displacing a live engine, applied to the failure path."""
        first = assembled(home)

        async def scenario() -> Reply:
            await first.start()
            try:
                second = assembled(home)
                with pytest.raises(AlreadyServing):
                    await second.start()
                await second.aclose()
                return await ask(Request(action=Action.STATUS), path=first.socket_path)
            finally:
                await first.aclose()

        assert asyncio.run(scenario()).ok


class TestTruthAcrossARestart:
    def test_what_was_written_down_is_adopted(self, home: Path) -> None:
        engine = assembled(home)

        async def flip() -> None:
            await running(
                engine,
                lambda: ask(
                    Request(action=Action.SWITCH, payload={"name": "duty", "on": True}),
                    path=engine.socket_path,
                ),
            )

        asyncio.run(flip())
        restarted = assembled(home)

        async def read() -> Reply:
            return await running(
                restarted, lambda: ask(Request(action=Action.STATUS), path=restarted.socket_path)
            )

        assert json.loads((home / "state.json").read_text())["switches"]["duty"] is True
        assert asyncio.run(read()).data["switches"]["duty"] is True


class TestEventsReachTheHub:
    def test_an_adapter_event_is_dispatched_while_the_engine_runs(self, home: Path) -> None:
        engine = assembled(home)

        async def scenario() -> Reply:
            await engine.start()
            try:
                await ask(
                    Request(
                        action=Action.LAUNCH,
                        payload={
                            "agent": "codex",
                            "workspace": str(home),
                            "label": {"project": "p", "task": "t"},
                        },
                    ),
                    path=engine.socket_path,
                )
                engine.core.events.emit(ReplyWindowChanged(target=CODEX, window=ReplyWindow.OPEN))
                await asyncio.sleep(0.05)  # the dispatch loop is the thing under test
                return await ask(Request(action=Action.SESSIONS), path=engine.socket_path)
            finally:
                await engine.aclose()

        sessions = asyncio.run(scenario()).data["sessions"]

        assert sessions[0]["reply_window"] == "open"

    def test_an_inbound_command_is_answered_through_the_channel(self, home: Path) -> None:
        """The Companion Channel is a control-plane surface: `/status` is `status`."""
        engine = assembled(home)

        async def scenario() -> list[str]:
            await engine.start()
            try:
                engine.core.events.emit(InboundText(text="/status"))
                await asyncio.sleep(0.05)
                return list(engine.adapters.channel.sent)
            finally:
                await engine.aclose()

        sent = asyncio.run(scenario())

        assert sent and "duty" in sent[0]

    def test_a_delegated_turn_carries_the_instructions_the_hub_generated(self, home: Path) -> None:
        """Bridge Core generates them; the root passes them at the call site."""
        engine = assembled(home)

        async def scenario() -> list[str]:
            await engine.start()
            try:
                engine.core.events.emit(InboundText(text="> summarise the diff"))
                await asyncio.sleep(0.05)
                return list(engine.adapters.call.delegated_on)
            finally:
                await engine.aclose()

        carried = asyncio.run(scenario())

        assert engine.core.instructions is not None
        assert carried == [engine.core.instructions.delegated.text]

    def test_a_delegated_turn_uses_the_model_the_user_configured(self, home: Path) -> None:
        """The cost lever comes from the file, never from code."""
        engine = assembled(home)

        async def scenario() -> list[tuple[str, str]]:
            await engine.start()
            try:
                engine.core.events.emit(InboundText(text="> summarise the diff"))
                await asyncio.sleep(0.05)
                return list(engine.adapters.call.delegated)
            finally:
                await engine.aclose()

        delegated = asyncio.run(scenario())

        assert delegated == [("summarise the diff", "the-model-the-user-chose")]


class TestSharingTheOneAppServer:
    """One `codex app-server` per engine, and the root is what introduces them."""

    RIDING = CONFIG.replace(
        'call = "fakes:FakeCall"', 'call = "test_engine:call_that_rides"'
    ).replace('codex = "fakes:FakeAgent"', 'codex = "test_engine:agent_that_owns_one"')

    def test_the_call_adapter_is_handed_the_one_the_agent_adapter_owns(self, home: Path) -> None:
        engine = assembled(home, self.RIDING)

        assert engine.adapters.call.riding is ServerOwningAgent.app_server

    def test_it_is_wired_before_anything_is_asked_to_open(self, home: Path) -> None:
        """Assembly does it, so `start` can never reach an adapter still missing one."""
        engine = assembled(home, self.RIDING)

        assert engine.adapters.call.riding is not None, "wired only at start would be too late"

    def test_a_call_adapter_with_no_provider_refuses_to_assemble(self, home: Path) -> None:
        """Named by seam, not degraded silently: the voice surface could never come up."""
        orphaned = self.RIDING.replace(
            'codex = "test_engine:agent_that_owns_one"', 'codex = "fakes:FakeAgent"'
        )

        with pytest.raises(EngineAssemblyError) as refusal:
            assembled(home, orphaned)

        assert "[adapters.agents] codex" in str(refusal.value)

    def test_a_call_adapter_that_wants_none_is_left_alone(self, home: Path) -> None:
        """A fake or null Call adapter needs to know nothing about any of this."""
        engine = assembled(home)

        assert not hasattr(engine.adapters.call, "riding")


class TestTheTick:
    def test_the_engine_advances_the_ceilings_on_its_own(self, home: Path) -> None:
        """Both budgets are time-driven, so something must drive them."""
        engine = assembled(home)
        ticks: list[int] = []
        original = engine.core.tick

        async def counted():  # type: ignore[no-untyped-def]
            ticks.append(1)
            return await original()

        engine.core.tick = counted  # type: ignore[method-assign]

        async def scenario() -> None:
            await engine.start(tick_seconds=0.01)
            try:
                await asyncio.sleep(0.06)
            finally:
                await engine.aclose()

        asyncio.run(scenario())

        assert ticks


def with_cli(stated: Path) -> str:
    """State `cli` inside `[delegate]`, which is no longer the file's last table."""
    return CONFIG.replace(
        'model = "the-model-the-user-chose"',
        f'model = "the-model-the-user-chose"\ncli = "{stated}"',
    )


class TestTheInstructionsThisEngineGenerates:
    """Bridge Core carries both instruction sets; this root tells it where the CLI is.

    The two facts a generated instruction must not invent — where the
    control-plane CLI is, and which engine it reaches — are exactly the two only
    this root knows. So they are stated here, verified here, and refused here.
    """

    def test_an_assembled_hub_carries_both_sets(self, home: Path) -> None:
        engine = Engine.assemble(load(configured(home)))
        instructions = engine.core.instructions
        assert instructions is not None
        assert instructions.voice.text and instructions.delegated.text

    def test_the_sets_name_the_socket_this_engine_serves(self, home: Path) -> None:
        engine = Engine.assemble(load(configured(home)))
        assert engine.core.instructions is not None
        assert str(engine.socket_path) in engine.core.instructions.delegated.text

    def test_the_console_script_beside_this_interpreter_is_the_default(self, home: Path) -> None:
        engine = Engine.assemble(load(configured(home)))
        assert engine.core.instructions is not None
        derived = Path(sys.executable).parent / "bridgectl"
        assert str(derived) in engine.core.instructions.delegated.text

    def test_configuration_may_state_where_a_bundle_put_it(self, home: Path) -> None:
        """The bundle moves the binary, so it is the one thing that can say where."""
        bundled = home / "bridgectl"
        bundled.write_text("#!/bin/sh\n", encoding="utf-8")
        bundled.chmod(0o755)
        engine = Engine.assemble(load(configured(home, with_cli(bundled))))
        assert engine.core.instructions is not None
        assert str(bundled) in engine.core.instructions.delegated.text

    def test_a_stated_cli_that_is_not_there_stops_assembly(self, home: Path) -> None:
        missing = home / "nowhere" / "bridgectl"
        with pytest.raises(EngineAssemblyError, match="cannot be run"):
            Engine.assemble(load(configured(home, with_cli(missing))))

    def test_a_stated_cli_that_cannot_be_run_stops_assembly(self, home: Path) -> None:
        unrunnable = home / "bridgectl.txt"
        unrunnable.write_text("not executable", encoding="utf-8")
        unrunnable.chmod(0o644)
        with pytest.raises(EngineAssemblyError, match="cannot be run"):
            Engine.assemble(load(configured(home, with_cli(unrunnable))))

    def test_no_cli_anywhere_is_a_refusal_rather_than_a_guess(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An instruction naming a CLI that is not there is an invented detail."""
        monkeypatch.setattr(sys, "executable", str(home / "python"))
        with pytest.raises(EngineAssemblyError, match="no control-plane CLI"):
            Engine.assemble(load(configured(home)))

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
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from fakes import FakeAgent, FakeCall, FakeCompanionChannel
from gpt_voicecoding.adapters.agent.claude import PROVEN_AGAINST_VERSION
from gpt_voicecoding.adapters.agent.claude.registry import PEER_PROTOCOL
from gpt_voicecoding.config import load
from gpt_voicecoding.control_plane.client import ask
from gpt_voicecoding.control_plane.server import AlreadyServing
from gpt_voicecoding.core.sessions import Session, SessionState
from gpt_voicecoding.engine.composition import Engine, EngineAssemblyError
from gpt_voicecoding.seams.agent import ReplyWindow, ReplyWindowChanged
from gpt_voicecoding.seams.companion_channel import InboundText
from gpt_voicecoding.seams.control_plane import Action, Reply, Request
from gpt_voicecoding.seams.identity import AgentKind, SessionLabel, SessionTarget

CODEX = SessionTarget(agent=AgentKind.CODEX, session_id="abc")
CLAUDE = SessionTarget(
    agent=AgentKind.CLAUDE,
    session_id="430b0def-38ef-4783-8d57-d800710d83bd",
    pid=os.getpid(),
)
EVENT_SETTLE_TIMEOUT_SECONDS = 10.0


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


def with_idle_claude(home: Path, *, poll_seconds: float = 0.02) -> str:
    """Use the real Claude spoke rather than the fake one.

    `poll_seconds` is the Reply-Window sweep's interval; a test that needs the
    sweep provably not to have run sets it longer than the test lives.
    """
    return CONFIG.replace(
        'codex = "fakes:FakeAgent"',
        'claude = "gpt_voicecoding.adapters.agent.claude:claude_agent"',
    ).replace(
        "[delegate]",
        "\n".join(
            (
                '[adapters.settings."agent.claude"]',
                f'registry_directory = "{home / "sessions"}"',
                f'socket_directory = "{home / "sockets"}"',
                f"reply_window_poll_seconds = {poll_seconds}",
                "",
                "[delegate]",
            )
        ),
    )


def write_idle_claude_record(home: Path, *, status: str = "idle") -> None:
    """Write the registry level the real Claude Reply-Window watcher reads.

    `status` is the level the record carries, so a test can stand an idle
    Session next to a busy one.
    """
    sessions = home / "sessions"
    sessions.mkdir()
    (sessions / f"{CLAUDE.pid}.json").write_text(
        json.dumps(
            {
                "pid": CLAUDE.pid,
                "sessionId": CLAUDE.session_id,
                "cwd": str(home),
                "version": PROVEN_AGAINST_VERSION,
                "peerProtocol": PEER_PROTOCOL,
                "messagingSocketPath": str(home / "claude.sock"),
                "status": status,
            }
        ),
        encoding="utf-8",
    )


def a_dead_pid() -> int:
    """A pid that really is gone: one this process started and has already reaped."""
    child = subprocess.Popen([sys.executable, "-c", ""])
    child.wait()
    return child.pid


def write_state_holding_a_live_session(home: Path, *, pid: int) -> None:
    """The shape #26 captured: a state file whose Session says `live`."""
    (home / "state.json").write_text(
        json.dumps(
            {
                "version": 1,
                "switches": {"duty": True, "message": False, "voice": True},
                "sessions": [
                    {
                        "target": {
                            "agent": "claude",
                            "session_id": CLAUDE.session_id,
                            "pid": pid,
                        },
                        "label": {"project": "GPT-VoiceCoding", "task": "acceptance step 3"},
                        "workspace": str(home),
                        "registered_at": 1787302123.276521,
                        "state": "live",
                        "reply_window": "closed",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def put_on_the_roster(engine: Engine, target: SessionTarget = CODEX) -> None:
    """Seed one Session row on a running engine.

    There is no public verb for this. The launch transaction that used to write
    the roster is parked (#72) and the discovery path that replaces it is not
    built yet, so a test that needs a row reaches past `BridgeCore` to put one
    there. What these tests are about is what happens once a row exists, not
    how it arrived — and the reach is written here once so #74 has one call
    site to replace.
    """
    engine.core._state.sessions.register(
        Session(
            target=target,
            label=SessionLabel(project="GPT-VoiceCoding", task="t"),
            workspace=Path("/tmp/workspace"),
            registered_at=0.0,
        )
    )


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

        assert set(seams) == {"call", "companion_channel", "agent.codex"}
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


class TestARestoredSessionIsOneTheEngineCanHonour:
    """#26, through the production wiring: assemble, restore, and ask the engine.

    `state.restore()` repopulates Bridge Core's roster only. Nothing on that path
    calls the Agent adapter's `register_session()`, which is the sole place a
    Session's channel *and* its Reply Window watch are established — and the
    channel's address arrives from the launch that minted it, so it cannot come
    back. A row that returned LIVE would claim a reachability the engine has no
    way to honour, and — being unwatched — could never be reported dead either.

    Both of the ticket's consequences are exercised here against the real Claude
    spoke and the real state file: the Session whose process is gone, and the
    Session whose process is perfectly healthy.

    The rows are written straight into the state file. They used to be made by
    launching one and stopping the engine, which is parked (#72) — and the file
    is the more faithful input anyway, because it is what a restart really
    reads. What went with the launch is the *premise* test, which proved a live
    Session is persisted as `live`; #74 owns proving that again once something
    registers a Session.
    """

    def test_a_restored_session_whose_process_is_alive_is_not_left_claiming_to_be_live(
        self, home: Path
    ) -> None:
        """Consequence 2, isolated: the agent never died — the restart is the whole cause.

        `CLAUDE.pid` is this test process, so the Session is genuinely healthy
        across the restart. It still may not come back LIVE, because the engine
        holds no channel to it and Bridge Core would queue the user's own words
        against a Reply Window nothing observes.
        """
        write_state_holding_a_live_session(home, pid=os.getpid())
        text = with_idle_claude(home)
        restarted = assembled(home, text)

        async def read() -> Reply:
            return await running(
                restarted,
                lambda: ask(Request(action=Action.SESSIONS), path=restarted.socket_path),
            )

        sessions = asyncio.run(read()).data["sessions"]

        assert [session["state"] for session in sessions] == ["ended"]
        assert [session["reply_window"] for session in sessions] == ["closed"]

    def test_the_roster_says_only_what_the_adapter_can_back_up(self, home: Path) -> None:
        """The point of the fix: core state and the adapter's real reach agree."""
        write_state_holding_a_live_session(home, pid=os.getpid())
        text = with_idle_claude(home)
        restarted = assembled(home, text)
        claude = restarted.adapters.agents[AgentKind.CLAUDE]

        assert restarted.core.status().sessions[0].state is SessionState.ENDED
        assert claude.reachable() == ()  # type: ignore[attr-defined]

    def test_a_restored_session_whose_process_is_dead_does_not_live_forever(
        self, home: Path
    ) -> None:
        """Consequence 1, from the captured shape: an unwatched row survived every restart.

        Nothing but the Reply Window sweep ever reports a Claude Session's death,
        and the sweep only ever visits Sessions `register_session()` entered into
        it. A restored row was in nobody's population, so `live` outlived the
        process by days. Ending it on the restore path is what stops that.
        """
        write_state_holding_a_live_session(home, pid=a_dead_pid())
        text = with_idle_claude(home)

        async def read(engine: Engine) -> Reply:
            return await running(
                engine, lambda: ask(Request(action=Action.SESSIONS), path=engine.socket_path)
            )

        first = asyncio.run(read(assembled(home, text))).data["sessions"]
        second = asyncio.run(read(assembled(home, text))).data["sessions"]

        assert [session["state"] for session in first] == ["ended"]
        assert [session["state"] for session in second] == ["ended"]

    def test_repeated_restarts_do_not_rewrite_a_row_that_is_already_ended(self, home: Path) -> None:
        """Idempotent: an ended Session is never ended a second time."""
        write_state_holding_a_live_session(home, pid=a_dead_pid())
        text = with_idle_claude(home)
        state = home / "state.json"

        async def read(engine: Engine) -> Reply:
            return await running(
                engine, lambda: ask(Request(action=Action.SESSIONS), path=engine.socket_path)
            )

        asyncio.run(read(assembled(home, text)))
        once = state.read_text(encoding="utf-8")
        asyncio.run(read(assembled(home, text)))

        assert state.read_text(encoding="utf-8") == once


class TestEventsReachTheHub:
    def test_an_adapter_event_is_dispatched_while_the_engine_runs(self, home: Path) -> None:
        engine = assembled(home)

        async def scenario() -> Reply:
            await engine.start()
            try:
                put_on_the_roster(engine)
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

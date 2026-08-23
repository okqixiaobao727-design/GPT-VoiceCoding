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

from fakes import FakeAgent, FakeCall, FakeCompanionChannel, FakeSessionLauncher
from gpt_voicecoding.adapters.agent.claude import PROVEN_AGAINST_VERSION, ClaudeAgentAdapter
from gpt_voicecoding.adapters.agent.claude.registry import PEER_PROTOCOL
from gpt_voicecoding.config import load
from gpt_voicecoding.control_plane.client import ask
from gpt_voicecoding.control_plane.commands import USAGE
from gpt_voicecoding.control_plane.server import AlreadyServing
from gpt_voicecoding.core.sessions import SessionState
from gpt_voicecoding.engine.composition import Engine, EngineAssemblyError
from gpt_voicecoding.seams.agent import ReplyWindow, ReplyWindowChanged
from gpt_voicecoding.seams.companion_channel import InboundText
from gpt_voicecoding.seams.control_plane import Action, Reply, Request
from gpt_voicecoding.seams.identity import AgentKind, SessionTarget, new_request_id
from gpt_voicecoding.seams.session_launcher import LaunchOutcome, LaunchRequest, LaunchStatus

CODEX = SessionTarget(agent=AgentKind.CODEX, session_id="abc")
CLAUDE = SessionTarget(
    agent=AgentKind.CLAUDE,
    session_id="430b0def-38ef-4783-8d57-d800710d83bd",
    pid=os.getpid(),
)


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


class IntroducedLauncher(FakeSessionLauncher):
    """A Launcher that asks to be introduced to the Agent adapters, as both real ones do.

    A launch carries things only an Agent spoke can name — where this engine
    parks permission dialogs, which byte budgets its Session Channel was
    configured with — so the real launchers take those adapters and the root is
    what hands them over.
    """

    def __post_init__(self) -> None:
        super().__post_init__()
        self.met_claude: object = None
        self.met_codex: object = None

    def use_claude(self, adapter: object) -> None:
        self.met_claude = adapter

    def use_codex(self, adapter: object) -> None:
        self.met_codex = adapter


def launcher_that_wants_introducing(*, sink: object = None) -> IntroducedLauncher:
    return IntroducedLauncher(targets=[CODEX], sink=sink)  # type: ignore[arg-type]


class ClaudeRegisteringLauncher(FakeSessionLauncher):
    """A launch that registers its channel before it reports success, as Claude does.

    **The yield after registering is the faithful part, not a nicety (#27).** The
    real `ClaudeSessionPreparation.confirm` registers the channel and then keeps
    awaiting — claiming a registry record, tearing down its preparation — before
    Bridge Core ever gets to write the Session into its roster. Every one of
    those awaits is a slice the dispatch loop can take, which is exactly how a
    report raised at registration reaches the hub *before* the hub knows the
    Session and is dropped as unknown.

    Without the yield this fake registers and returns in one uninterrupted step,
    so the dispatch loop never runs in the gap and the event waits harmlessly in
    the queue until the roster row exists. That is production's ordering defect
    papered over by the fake's own timing, and it is why this helper passed a
    Session's window through for as long as the defect was live.
    """

    def __post_init__(self) -> None:
        super().__post_init__()
        self.claude: ClaudeAgentAdapter | None = None

    def use_claude(self, adapter: ClaudeAgentAdapter) -> None:
        self.claude = adapter

    async def launch(self, request: LaunchRequest) -> LaunchOutcome:
        outcome = await super().launch(request)
        if outcome.status is LaunchStatus.LAUNCHED:
            assert outcome.target is not None
            assert self.claude is not None
            self.claude.register_session(
                outcome.target,
                Path(tempfile.gettempdir()) / f"gvc-test-{outcome.target.pid}.sock",
            )
            # Long enough that the dispatch loop certainly takes a slice, rather
            # than a bare `sleep(0)` that leaves the reproduction resting on how
            # many yields one queue hand-off happens to need.
            await asyncio.sleep(0.01)
        return outcome


def claude_registering_launcher(*, sink: object = None) -> ClaudeRegisteringLauncher:
    return ClaudeRegisteringLauncher(targets=[CLAUDE], sink=sink)  # type: ignore[arg-type]


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

[launch]
default_agent = "codex"

[[launch.projects]]
name = "GPT-VoiceCoding"
workspace = "{workspace}"
spoken_aliases = ["GPT Live"]

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
            workspace=home,
        ),
        encoding="utf-8",
    )
    return path


def assembled(home: Path, text: str = CONFIG) -> Engine:
    return Engine.assemble(load(configured(home, text)))


def with_idle_claude(home: Path, *, poll_seconds: float = 0.02) -> str:
    """Use the real Claude spoke and the launch order that introduces a Session to it.

    `poll_seconds` is normally fast enough for a test to watch a sweep work. Set
    it long enough to outlive the test and the sweep provably cannot have run,
    which is how #27's requirement — reachable *without* waiting for a
    subsequent window transition — is asserted rather than assumed.
    """
    return (
        CONFIG.replace(
            'session_launcher = "test_engine:one_session_launcher"',
            'session_launcher = "test_engine:claude_registering_launcher"',
        )
        .replace(
            'codex = "fakes:FakeAgent"',
            'claude = "gpt_voicecoding.adapters.agent.claude:claude_agent"',
        )
        .replace('default_agent = "codex"', 'default_agent = "claude"')
        .replace(
            "[delegate]",
            "\n".join(
                (
                    '[adapters.settings."agent.claude"]',
                    f'registry_directory = "{home / "sessions"}"',
                    f'socket_directory = "{home / "sockets"}"',
                    f'projects_directory = "{home / "projects"}"',
                    f'peer_socket_directory = "{home / "peers"}"',
                    f"reply_window_poll_seconds = {poll_seconds}",
                    "",
                    "[delegate]",
                )
            ),
        )
    )


def write_idle_claude_record(home: Path, *, status: str = "idle") -> None:
    """Write the registry level the real Claude Reply-Window watcher reads.

    `status` exists so a test can stand the already-idle case next to the
    busy-at-registration control (#27). The control matters because it is the
    case that passed even with the defect present — the dropped report happened
    to carry CLOSED, which is what Bridge Core defaults to anyway.
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


async def running(engine: Engine, work) -> object:
    """Run the engine, do one thing against it, and shut it down."""
    await engine.start()
    try:
        return await work()
    finally:
        await engine.aclose()


class TestAssembly:
    def test_generated_instructions_receive_the_launch_parsers_usage(self, home: Path) -> None:
        instructions = assembled(home).core.instructions

        assert instructions is not None
        assert USAGE[Action.LAUNCH] in instructions.voice.text
        assert USAGE[Action.LAUNCH] in instructions.delegated.text

    def test_launch_configuration_reaches_bridge_core(self, home: Path) -> None:
        engine = assembled(home)
        request = Request(
            action=Action.LAUNCH,
            payload={
                "request_id": new_request_id(),
                "project": "GPT Live",
                "task": "prove configuration composition",
            },
        )

        async def scenario() -> Reply:
            return await running(engine, lambda: ask(request, path=engine.socket_path))

        reply = asyncio.run(scenario())

        assert reply.ok
        launcher = engine.adapters.launcher
        assert isinstance(launcher, FakeSessionLauncher)
        assert len(launcher.requests) == 1
        launched = launcher.requests[0]
        assert launched.agent is AgentKind.CODEX
        assert launched.workspace == home
        assert launched.label.project == "GPT-VoiceCoding"
        assert launched.label.task == "prove configuration composition"

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
    """

    def a_launched_session(self, home: Path) -> str:
        """Launch one real Claude Session, stop the engine, and leave state behind."""
        write_idle_claude_record(home)
        text = with_idle_claude(home)
        engine = assembled(home, text)

        async def scenario() -> Reply:
            return await running(
                engine,
                lambda: ask(
                    Request(
                        action=Action.LAUNCH,
                        payload={
                            "request_id": new_request_id(),
                            "agent": "claude",
                            "project": "GPT Live",
                            "task": "t",
                        },
                    ),
                    path=engine.socket_path,
                ),
            )

        assert asyncio.run(scenario()).ok
        return text

    def test_a_live_session_is_what_the_stopped_engine_really_wrote_down(
        self, home: Path
    ) -> None:
        """The premise of the rest: a Session that was live is persisted as live."""
        self.a_launched_session(home)

        persisted = json.loads((home / "state.json").read_text(encoding="utf-8"))

        assert [row["state"] for row in persisted["sessions"]] == ["live"]

    def test_a_restored_session_whose_process_is_alive_is_not_left_claiming_to_be_live(
        self, home: Path
    ) -> None:
        """Consequence 2, isolated: the agent never died — the restart is the whole cause.

        `CLAUDE.pid` is this test process, so the Session is genuinely healthy
        across the restart. It still may not come back LIVE, because the engine
        holds no channel to it and Bridge Core would queue the user's own words
        against a Reply Window nothing observes.
        """
        text = self.a_launched_session(home)
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
        text = self.a_launched_session(home)
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

    def test_repeated_restarts_do_not_rewrite_a_row_that_is_already_ended(
        self, home: Path
    ) -> None:
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


class TestTheStartingReplyWindow:
    """A Session's *first* Reply Window, established when the roster learns of it (#27).

    Every test here runs the assembled engine over the real Claude spoke and the
    real launch/registration order — the adapter registered before Bridge Core
    holds the Session, which is the ordering that made the starting level
    undeliverable as an event.

    They run with the sweep parked beyond the life of the test, so nothing here
    can pass on the back of a later transition. What is asserted is what the
    launch itself established.
    """

    #: Longer than any test here lives, so a passing assertion cannot be the
    #: sweep having quietly fixed things up a poll interval later.
    NO_SWEEP = 3600.0

    async def _launch(self, engine: Engine) -> Reply:
        return await ask(
            Request(
                action=Action.LAUNCH,
                payload={
                    "request_id": new_request_id(),
                    "agent": "claude",
                    "project": "GPT Live",
                    "task": "t",
                },
            ),
            path=engine.socket_path,
        )

    def _launched_window(self, home: Path, *, status: str) -> Reply:
        write_idle_claude_record(home, status=status)
        engine = assembled(home, with_idle_claude(home, poll_seconds=self.NO_SWEEP))

        async def scenario() -> Reply:
            await engine.start()
            try:
                assert (await self._launch(engine)).ok
                return await ask(Request(action=Action.SESSIONS), path=engine.socket_path)
            finally:
                await engine.aclose()

        return asyncio.run(scenario())

    def test_a_session_already_idle_at_registration_is_reachable_at_once(
        self, home: Path
    ) -> None:
        """#27's defect, at the case that exposes it.

        The Session is idle *before* its launch confirms, which is the ordinary
        case — a Session is usually registered the moment it comes up. Its
        starting window was announced by the adapter at registration and dropped,
        because Bridge Core did not hold the Session yet, and the announcement
        was never repeated because the watcher had recorded it as sent. The
        Session sat CLOSED, unreachable while perfectly healthy.

        With the sweep parked for an hour, an OPEN here can only have come from
        the level Bridge Core pulled the instant its roster held the Session.
        """
        sessions = self._launched_window(home, status="idle").data["sessions"]

        assert sessions[0]["reply_window"] == "open"
        persisted = json.loads((home / "state.json").read_text(encoding="utf-8"))
        assert persisted["sessions"][0]["reply_window"] == "open"

    def test_a_session_still_busy_at_registration_starts_closed(self, home: Path) -> None:
        """The control — and the reason the acceptance run's checkpoint proved nothing.

        A Session busy at registration is the case that passed *even with the
        defect present*: the report that got dropped carried CLOSED, which is
        what Bridge Core fails closed to anyway. So this asserts the fix did not
        buy reachability by starting Sessions open, and on its own it would be
        no evidence at all — it earns its place only standing next to the idle
        case above.
        """
        sessions = self._launched_window(home, status="busy").data["sessions"]

        assert sessions[0]["reply_window"] == "closed"

    def test_a_healthy_launch_never_reports_a_window_on_an_unknown_session(
        self, home: Path, caplog
    ) -> None:
        """Keeps one log line load-bearing.

        "a Reply Window changed on an unknown Session" was decisive evidence in
        #21 and again in #27. Before the fix it was printed by *every* launch,
        healthy or not, which is precisely what a line has to stop doing to mean
        anything. Registration is silent now, so this asserts the line is absent
        from a launch that went perfectly — and that the level was established by
        the pull instead, stated as a fact rather than as a change.
        """
        caplog.set_level("INFO", logger="gpt_voicecoding.core.bridge")

        self._launched_window(home, status="idle")

        logged = [record.getMessage() for record in caplog.records]
        assert not [line for line in logged if "unknown Session" in line], logged
        assert [line for line in logged if line.startswith("established Reply Window")] == [
            f"established Reply Window at registration agent={CLAUDE.agent} "
            f"session_id={CLAUDE.session_id} pid={CLAUDE.pid} window=open"
        ]


class TestEventsReachTheHub:
    def test_an_idle_claude_session_opens_its_reply_window_in_core_state(
        self, home: Path
    ) -> None:
        """The hub establishes the level at registration; the sweep keeps it current."""
        write_idle_claude_record(home)
        engine = assembled(home, with_idle_claude(home))

        async def scenario() -> Reply:
            await engine.start()
            try:
                launched = await ask(
                    Request(
                        action=Action.LAUNCH,
                        payload={
                            "request_id": new_request_id(),
                            "agent": "claude",
                            "project": "GPT Live",
                            "task": "t",
                        },
                    ),
                    path=engine.socket_path,
                )
                assert launched.ok
                observed = await ask(Request(action=Action.SESSIONS), path=engine.socket_path)
                for _ in range(100):
                    if observed.data["sessions"][0]["reply_window"] == "open":
                        break
                    await asyncio.sleep(0.01)
                    observed = await ask(Request(action=Action.SESSIONS), path=engine.socket_path)
                return observed
            finally:
                await engine.aclose()

        sessions = asyncio.run(scenario()).data["sessions"]

        assert sessions[0]["reply_window"] == "open"
        persisted = json.loads((home / "state.json").read_text(encoding="utf-8"))
        assert persisted["sessions"][0]["reply_window"] == "open"

    def test_an_adapter_event_is_dispatched_while_the_engine_runs(self, home: Path) -> None:
        engine = assembled(home)

        async def scenario() -> Reply:
            await engine.start()
            try:
                await ask(
                    Request(
                        action=Action.LAUNCH,
                        payload={
                            "request_id": new_request_id(),
                            "agent": "codex",
                            "project": "GPT Live",
                            "task": "t",
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

    def test_a_launcher_is_introduced_to_the_agent_adapters_it_launches_for(
        self, home: Path
    ) -> None:
        """The launcher meets the spokes before anything opens, not at launch time."""
        introduced = CONFIG.replace(
            'session_launcher = "test_engine:one_session_launcher"',
            'session_launcher = "test_engine:launcher_that_wants_introducing"',
        )

        engine = assembled(home, introduced)

        assert engine.adapters.launcher.met_codex is engine.adapters.agents[AgentKind.CODEX]

    def test_a_launcher_is_not_introduced_to_an_agent_this_engine_has_none_of(
        self, home: Path
    ) -> None:
        """Per-agent rather than fatal.

        An engine configured for Codex only is a legitimate engine, and it must
        start. The launcher refuses a Claude launch by name when one is asked
        for, which is where that refusal belongs — the assembly is not the place
        to decide that half a configuration is no configuration.
        """
        introduced = CONFIG.replace(
            'session_launcher = "test_engine:one_session_launcher"',
            'session_launcher = "test_engine:launcher_that_wants_introducing"',
        )

        engine = assembled(home, introduced)

        assert engine.adapters.launcher.met_claude is None

    def test_a_launcher_that_wants_no_introduction_is_left_alone(self, home: Path) -> None:
        """A fake or null launcher needs to know nothing about any of this."""
        engine = assembled(home)

        assert not hasattr(engine.adapters.launcher, "use_codex")

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

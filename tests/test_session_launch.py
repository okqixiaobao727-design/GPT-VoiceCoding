"""Launching and closing a Session, from the hub.

Ruling on #3: only Bridge Core writes the Session registry, so `launch` and
`close` are hub verbs with the Launcher injected as a Protocol — never a surface
holding half the transaction, which is the shape the reference implementation
had and the shape this repository refuses.

The Launcher's own contract (exactly one session target, fail closed on a
missing or stale identity, idempotent repeats) is #1's and is not re-tested
here; what is tested is that the hub honours it and that the registry ends up
telling the truth either way.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from fakes import FakeAgent, FakeCall, FakeCompanionChannel, FakeSessionLauncher
from gpt_voicecoding.core.bridge import BridgeCore
from gpt_voicecoding.core.errors import SeamUnavailableError, UnknownSessionError
from gpt_voicecoding.core.projects import Project
from gpt_voicecoding.core.relay_queue import RelayQueue
from gpt_voicecoding.core.sessions import SessionRegistry, SessionState
from gpt_voicecoding.core.state import BridgeState
from gpt_voicecoding.core.switches import Switchboard
from gpt_voicecoding.seams.identity import AgentKind, SessionLabel, SessionTarget, new_request_id
from gpt_voicecoding.seams.session_launcher import CloseStatus, LaunchStatus

WORKSPACE = Path("/tmp/workspace")
LABEL = SessionLabel(project="gpt-voicecoding", task="build the control plane")
PROJECTS = (Project(name=LABEL.project, workspace=WORKSPACE),)
CODEX = SessionTarget(agent=AgentKind.CODEX, session_id="abc")


def launched(core: BridgeCore) -> object:
    """One launch of the one agent these tests use."""
    return asyncio.run(
        core.launch_session(
            request_id=new_request_id(),
            agent=AgentKind.CODEX,
            project=LABEL.project,
            task=LABEL.task,
        )
    )


def hub(launcher: FakeSessionLauncher | None = None) -> BridgeCore:
    state = BridgeState(switches=Switchboard(), sessions=SessionRegistry(), relays=RelayQueue())
    return BridgeCore(
        state=state,
        call=FakeCall(),
        channel=FakeCompanionChannel(),
        agents={AgentKind.CODEX: FakeAgent()},
        launcher=launcher,
        default_agent=AgentKind.CODEX,
        projects=PROJECTS,
    )


class TestLaunching:
    def test_a_launched_session_records_its_target_and_workspace(self, caplog) -> None:
        caplog.set_level("INFO", logger="gpt_voicecoding.core.bridge")
        core = hub(FakeSessionLauncher(targets=[CODEX]))

        launched(core)

        assert [record.getMessage() for record in caplog.records] == [
            # The level the hub pulled the instant its roster held the Session
            # (#27), stated as a fact rather than as a change: this is not a
            # `ReplyWindowChanged`, and must not read like one. It is what made
            # #27 undiagnosable from the log — the starting window was the one
            # thing a launch never wrote down.
            "established Reply Window at registration agent=codex "
            "session_id=abc pid=None window=closed",
            "launched Session agent=codex session_id=abc pid=None workspace=/tmp/workspace",
        ]

    def test_a_refused_launch_records_the_refusal_words(self, caplog) -> None:
        caplog.set_level("INFO", logger="gpt_voicecoding.core.bridge")
        core = hub(FakeSessionLauncher(targets=[]))

        launched(core)

        assert [record.getMessage() for record in caplog.records] == [
            "launch refused: 'this fake launcher has no target left to hand out'"
        ]

    def test_a_launched_session_is_registered_by_the_hub(self) -> None:
        core = hub(FakeSessionLauncher(targets=[CODEX]))

        outcome = launched(core)

        assert outcome.status is LaunchStatus.LAUNCHED
        assert outcome.target == CODEX
        registered = core.status().sessions
        assert [held.target for held in registered] == [CODEX]
        assert registered[0].label == LABEL
        assert registered[0].workspace == WORKSPACE

    def test_a_failed_launch_registers_nothing_and_keeps_the_real_error(self) -> None:
        core = hub(FakeSessionLauncher(targets=[]))

        outcome = launched(core)

        assert outcome.status is LaunchStatus.FAILED
        assert outcome.detail
        assert core.status().sessions == ()

    def test_an_engine_with_no_launcher_refuses_rather_than_pretending(self) -> None:
        core = hub(None)

        with pytest.raises(SeamUnavailableError):
            launched(core)

    def test_the_launched_session_is_written_down(self, tmp_path: Path) -> None:
        """A Session that survives a restart is one the registry persisted."""
        from gpt_voicecoding.core.persistence import StateStore

        store = StateStore(tmp_path / "state.json")
        state = BridgeState(
            switches=Switchboard(),
            sessions=SessionRegistry(),
            relays=RelayQueue(),
            store=store,
        )
        core = BridgeCore(
            state=state,
            call=FakeCall(),
            channel=FakeCompanionChannel(),
            agents={AgentKind.CODEX: FakeAgent()},
            launcher=FakeSessionLauncher(targets=[CODEX]),
            default_agent=AgentKind.CODEX,
            projects=PROJECTS,
        )

        launched(core)

        persisted = store.load()
        assert persisted is not None
        assert [held.target for held in persisted.sessions] == [CODEX]


class TestClosing:
    def _launched(self) -> BridgeCore:
        core = hub(FakeSessionLauncher(targets=[CODEX]))
        launched(core)
        return core

    def test_closing_ends_the_session_in_the_registry(self) -> None:
        core = self._launched()

        outcome = asyncio.run(core.close_session(CODEX))

        assert outcome.status is CloseStatus.CLOSED
        assert core.status().sessions[0].state is SessionState.ENDED

    def test_a_repeat_close_is_a_success_that_touches_nothing(self) -> None:
        """Idempotent: the caller asked for a state that already holds."""
        core = self._launched()
        asyncio.run(core.close_session(CODEX))

        outcome = asyncio.run(core.close_session(CODEX))

        assert outcome.status is CloseStatus.ALREADY_CLOSED

    def test_an_identity_that_was_never_registered_is_refused(self) -> None:
        core = self._launched()
        stranger = SessionTarget(agent=AgentKind.CODEX, session_id="never-seen")

        with pytest.raises(UnknownSessionError):
            asyncio.run(core.close_session(stranger))

    def test_a_failed_close_leaves_the_session_live(self) -> None:
        """A close that did not happen must not be recorded as one that did."""
        launcher = FakeSessionLauncher(targets=[CODEX])
        core = hub(launcher)
        launched(core)
        launcher.opened.clear()  # the child is gone from the launcher's view

        outcome = asyncio.run(core.close_session(CODEX))

        assert outcome.status is CloseStatus.FAILED
        assert core.status().sessions[0].state is SessionState.LIVE

    def test_an_engine_with_no_launcher_refuses_rather_than_pretending(self) -> None:
        core = hub(None)

        with pytest.raises(SeamUnavailableError):
            asyncio.run(core.close_session(CODEX))

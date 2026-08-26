"""One assembled Bridge Core over fakes, shared by every test that drives it end to end.

Bridge Core is the consumer half of several seams, so more than one test module
needs a real hub to prove that what an adapter raises actually lands. Keeping the
harness here rather than in whichever test module built it first is what stops a
test module from importing another test module — a shape that breaks silently the
moment collection changes.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fakes import FakeAgent, FakeCall, FakeCompanionChannel, instruction_context
from gpt_voicecoding.core.bridge import BridgeCore
from gpt_voicecoding.core.policy import CorePolicy
from gpt_voicecoding.core.relay_queue import RelayQueue
from gpt_voicecoding.core.router import TextGrammar
from gpt_voicecoding.core.sessions import Session, SessionRegistry
from gpt_voicecoding.core.state import BridgeState
from gpt_voicecoding.core.switches import Switchboard, SwitchName
from gpt_voicecoding.seams.agent import RelayRoute, ReplyWindow, SessionState
from gpt_voicecoding.seams.delivery import Delivery
from gpt_voicecoding.seams.identity import AgentKind, SessionName, SessionTarget

CODEX = SessionTarget(agent=AgentKind.CODEX, session_id="abc")
CLAUDE = SessionTarget(agent=AgentKind.CLAUDE, session_id="def", pid=100)

TEN_MINUTES = 600.0
COMMANDS = frozenset({"status", "stop"})


class Hub:
    """One assembled Bridge Core over fakes, and the knobs a test needs."""

    def __init__(
        self,
        *,
        duty: bool = True,
        voice: bool = True,
        message: bool = True,
        sessions: tuple[tuple[SessionTarget, str], ...] = ((CODEX, "port the log"),),
        window: ReplyWindow = ReplyWindow.CLOSED,
        control: object = None,
        delegate: object = None,
        instructions: bool = True,
        channel_outcome: Delivery = Delivery.DELIVERED,
        channel_reason: str = "fake channel",
    ) -> None:
        self.now = 1_000.0
        switches = Switchboard()
        switches.flip(SwitchName.DUTY, duty)
        switches.flip(SwitchName.VOICE, voice)
        switches.flip(SwitchName.MESSAGE, message)

        registry = SessionRegistry()
        # The Reply Window is derived, so a test that wants one open says what
        # the Session is *doing* — which is the only thing an adapter can see.
        state = SessionState.IDLE if window is ReplyWindow.OPEN else SessionState.RUNNING
        for target, task in sessions:
            registry.register(
                Session(
                    target=target,
                    name=SessionName("GPT-VoiceCoding", task),
                    workspace=Path("/tmp/workspace"),
                    first_seen=0.0,
                    state=state,
                )
            )

        self.call = FakeCall()
        self.channel = FakeCompanionChannel(outcome=channel_outcome, reason=channel_reason)
        self.agent = FakeAgent(routes=frozenset(RelayRoute))
        self.state = BridgeState(switches=switches, sessions=registry, relays=RelayQueue())
        self.core = BridgeCore(
            state=self.state,
            call=self.call,
            channel=self.channel,
            agents={AgentKind.CODEX: self.agent, AgentKind.CLAUDE: self.agent},
            policy=CorePolicy(),
            grammar=TextGrammar(control_commands=COMMANDS),
            clock=lambda: self.now,
            control=control,  # type: ignore[arg-type]
            delegate=delegate,  # type: ignore[arg-type]
            instruction_context=instruction_context() if instructions else None,
        )

    def emit(self, *events: object) -> int:
        for event in events:
            self.core.events.emit(event)  # type: ignore[arg-type]
        return asyncio.run(self.core.drain())

    def tick(self) -> object:
        return asyncio.run(self.core.tick())

    def flip(self, name: str, on: bool) -> bool:
        return asyncio.run(self.core.flip_switch(name, on))

    def toggle(self) -> object:
        return asyncio.run(self.core.live_toggle())

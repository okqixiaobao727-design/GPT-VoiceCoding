"""One request in, one Bridge Core verb called, one reply out.

The load-bearing test in this file is `TestWithEverySwitchOff`: ADR 0002 says
the control plane is never gated, by anything, ever, and the reference
implementation gated seven of these actions behind the Duty Switch. That
behaviour is dropped rather than ported, and this is what would catch it coming
back.

Everything else here is translation: the payload shapes the Swift shell will
implement against, and the refusals keeping their identity — Bridge Core's own
words, under a code a surface can branch on.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from fakes import (
    FakeAgent,
    FakeCall,
    FakeCompanionChannel,
    FakeSessionLauncher,
    instruction_context,
)
from gpt_voicecoding.control_plane.actions import ControlPlane
from gpt_voicecoding.core.bridge import BridgeCore
from gpt_voicecoding.core.relay_queue import RelayQueue
from gpt_voicecoding.core.sessions import SessionRegistry
from gpt_voicecoding.core.state import BridgeState
from gpt_voicecoding.core.switches import Switchboard, SwitchName
from gpt_voicecoding.core.verification import SeamLoad
from gpt_voicecoding.seams.agent import (
    ApprovalRequest,
    AwaitingApproval,
    ReplyWindow,
    ReplyWindowChanged,
)
from gpt_voicecoding.seams.control_plane import Action, ErrorCode, Reply, Request
from gpt_voicecoding.seams.identity import AgentKind, SessionLabel, SessionTarget

WORKSPACE = Path("/tmp/workspace")
LABEL = SessionLabel(project="gpt-voicecoding", task="build the control plane")
CODEX = SessionTarget(agent=AgentKind.CODEX, session_id="abc")
CODEX_ADDRESS = {"agent": "codex", "session_id": "abc", "pid": None}


class Surface:
    """One assembled engine-side control plane, and the knobs a test needs."""

    def __init__(self, *, duty: bool = True, launcher: bool = True) -> None:
        self.agent = FakeAgent()
        self.call = FakeCall()
        self.channel = FakeCompanionChannel()
        self.launcher = FakeSessionLauncher(targets=[CODEX]) if launcher else None
        state = BridgeState(switches=Switchboard(), sessions=SessionRegistry(), relays=RelayQueue())
        state.switches.flip(SwitchName.DUTY, duty)
        state.switches.flip(SwitchName.VOICE, duty)
        state.switches.flip(SwitchName.MESSAGE, duty)
        self.core = BridgeCore(
            state=state,
            call=self.call,
            channel=self.channel,
            agents={AgentKind.CODEX: self.agent},
            launcher=self.launcher,
            inventory=(SeamLoad(seam="call", configured="a.call"),),
            instruction_context=instruction_context(),
        )
        self.plane = ControlPlane(self.core)

    def ask(self, action: Action, **payload: object) -> Reply:
        return asyncio.run(self.plane.handle(Request(action=action, payload=payload)))

    def launch(self) -> Reply:
        return self.ask(
            Action.LAUNCH,
            agent="codex",
            workspace=str(WORKSPACE),
            label={"project": LABEL.project, "task": LABEL.task},
        )

    def open_window(self) -> None:
        """The Session says it will take a user turn now."""
        asyncio.run(self.core.dispatch(ReplyWindowChanged(target=CODEX, window=ReplyWindow.OPEN)))


class TestWithEverySwitchOff:
    """ADR 0002, absolute: every action answers with Duty off. All of them."""

    def test_every_action_succeeds_with_duty_voice_and_message_off(self) -> None:
        surface = Surface(duty=False)
        surface.launch()
        asyncio.run(
            surface.core.approvals.opened(
                ApprovalRequest(approval_id="a1", target=CODEX, tool_name="Bash")
            )
        )

        replies = {
            Action.STATUS: surface.ask(Action.STATUS),
            Action.SWITCH: surface.ask(Action.SWITCH, name="duty", on=False),
            Action.SESSIONS: surface.ask(Action.SESSIONS),
            Action.LIVE: surface.ask(Action.LIVE),
            Action.RELAY: surface.ask(Action.RELAY, target=CODEX_ADDRESS, text="carry on"),
            Action.APPROVE: surface.ask(Action.APPROVE, approval_id="a1", verdict="allow"),
            Action.VERIFY: surface.ask(Action.VERIFY),
            Action.CLOSE: surface.ask(Action.CLOSE, target=CODEX_ADDRESS),
        }

        refused = {action: reply.error for action, reply in replies.items() if not reply.ok}
        assert refused == {}

    def test_the_duty_switch_can_be_turned_back_on_from_a_surface(self) -> None:
        """The forcing scenario: locked out by the switch meant to protect you."""
        surface = Surface(duty=False)

        reply = surface.ask(Action.SWITCH, name="duty", on=True)

        assert reply.ok
        assert reply.data["on"] is True
        assert reply.data["previous"] is False
        assert surface.core.status().switches.as_mapping()["duty"] is True

    def test_the_live_toggle_ends_a_call_while_the_voice_switch_is_off(self) -> None:
        surface = Surface(duty=False)
        assert surface.ask(Action.LIVE).data["state"] == "up"

        reply = surface.ask(Action.LIVE)

        assert reply.ok
        assert reply.data["state"] == "down"
        assert reply.data["call_id"] is None


class TestStatus:
    def test_status_renders_the_whole_hub(self) -> None:
        surface = Surface()
        surface.launch()

        data = surface.ask(Action.STATUS).data

        assert data["switches"]["duty"] is True
        assert data["call_id"] is None
        assert data["sessions"][0]["target"] == CODEX_ADDRESS
        assert data["sessions"][0]["label"] == str(LABEL)
        assert data["sessions"][0]["state"] == "live"
        assert data["pending_relays"] == []
        assert data["pending_approvals"] == []

    def test_the_roster_is_answerable_on_its_own(self) -> None:
        surface = Surface()
        surface.launch()

        assert (
            surface.ask(Action.SESSIONS).data["sessions"]
            == (surface.ask(Action.STATUS).data["sessions"])
        )


class TestRefusalsKeepTheirIdentity:
    def test_an_unknown_switch(self) -> None:
        reply = Surface().ask(Action.SWITCH, name="sound", on=True)

        assert reply.error is not None
        assert reply.error.code is ErrorCode.UNKNOWN_SWITCH
        assert "sound" in reply.error.message

    def test_a_switch_state_that_is_not_a_state(self) -> None:
        reply = Surface().ask(Action.SWITCH, name="duty", on="off")

        assert reply.error is not None
        assert reply.error.code is ErrorCode.INVALID_PAYLOAD

    def test_an_unknown_session(self) -> None:
        reply = Surface().ask(Action.CLOSE, target=CODEX_ADDRESS)

        assert reply.error is not None
        assert reply.error.code is ErrorCode.UNKNOWN_SESSION

    def test_a_claude_target_without_a_pid_never_reaches_the_hub(self) -> None:
        """A resumed Claude session forks; a target without a pid is ambiguous."""
        reply = Surface().ask(
            Action.CLOSE, target={"agent": "claude", "session_id": "abc", "pid": None}
        )

        assert reply.error is not None
        assert reply.error.code is ErrorCode.INVALID_PAYLOAD
        assert "pid" in reply.error.message

    def test_an_engine_with_no_launcher_says_which_seam_is_empty(self) -> None:
        surface = Surface(launcher=False)

        reply = surface.launch()

        assert reply.error is not None
        assert reply.error.code is ErrorCode.SEAM_UNAVAILABLE

    def test_a_verdict_nothing_is_waiting_for(self) -> None:
        reply = Surface().ask(Action.APPROVE, approval_id="never", verdict="allow")

        assert reply.error is not None
        assert reply.error.code is ErrorCode.UNKNOWN_PENDING

    def test_a_verdict_that_is_not_one(self) -> None:
        reply = Surface().ask(Action.APPROVE, approval_id="a1", verdict="maybe")

        assert reply.error is not None
        assert reply.error.code is ErrorCode.INVALID_PAYLOAD

    def test_relayed_words_that_are_not_there(self) -> None:
        surface = Surface()
        surface.launch()

        reply = surface.ask(Action.RELAY, target=CODEX_ADDRESS, text="   ")

        assert reply.error is not None
        assert reply.error.code is ErrorCode.INVALID_PAYLOAD


class TestLaunchingAndClosing:
    def test_a_launch_returns_the_identity_the_hub_registered(self) -> None:
        surface = Surface()

        data = surface.launch().data

        assert data["status"] == "launched"
        assert data["target"] == CODEX_ADDRESS

    def test_a_failed_launch_is_an_answer_carrying_the_real_error(self) -> None:
        """The Launcher tried and failed: that is news, not a protocol refusal."""
        surface = Surface()
        surface.launch()

        second = surface.launch()  # the fake has one target and it is spent

        assert second.ok
        assert second.data["status"] == "failed"
        assert second.data["detail"]

    def test_closing_and_repeating_it(self) -> None:
        surface = Surface()
        surface.launch()

        assert surface.ask(Action.CLOSE, target=CODEX_ADDRESS).data["status"] == "closed"
        assert surface.ask(Action.CLOSE, target=CODEX_ADDRESS).data["status"] == "already_closed"


class TestRelayingAndApproving:
    def test_the_users_words_reach_the_session(self) -> None:
        surface = Surface()
        surface.launch()
        surface.open_window()

        data = surface.ask(Action.RELAY, target=CODEX_ADDRESS, text="carry on").data

        assert data["state"] == "delivered"
        assert [call.text for call in surface.agent.calls] == ["carry on"]

    def test_a_relay_names_the_route_it_took(self) -> None:
        surface = Surface()
        surface.launch()
        surface.open_window()

        data = surface.ask(
            Action.RELAY, target=CODEX_ADDRESS, text="carry on", route="deliver"
        ).data

        assert data["route"] == "deliver"

    def test_a_verdict_is_carried_and_the_loop_closed(self) -> None:
        surface = Surface()
        surface.launch()
        asyncio.run(
            surface.core.dispatch(
                AwaitingApproval(
                    request=ApprovalRequest(approval_id="a1", target=CODEX, tool_name="Bash")
                )
            )
        )

        data = surface.ask(Action.APPROVE, approval_id="a1", verdict="allow").data

        assert data["verdict"] == "allow"
        assert data["closing_notice"]
        assert [call.verdict for call in surface.agent.calls if call.verb == "approval_relay"]


class TestVerify:
    def test_verify_reports_every_seam_the_engine_was_asked_about(self) -> None:
        data = Surface().ask(Action.VERIFY).data

        assert data["seams"] == [
            {
                "seam": "call",
                "outcome": "pass",
                "configured": "a.call",
                "loaded": "tests.fakes.FakeCall",
                "detail": "",
            }
        ]


class TestTheCommandSetIsOne:
    def test_the_command_words_are_exactly_the_action_set(self) -> None:
        """The Companion Channel's `/` grammar and bridgectl are one command set."""
        assert ControlPlane(Surface().core).commands == {str(action) for action in Action}


@pytest.mark.parametrize("action", list(Action))
def test_every_action_is_dispatchable(action: Action) -> None:
    """A closed action set with a handler missing is a wire that lies."""
    assert action in ControlPlane(Surface().core).handlers

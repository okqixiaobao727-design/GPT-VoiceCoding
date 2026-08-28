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
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fakes import FakeAgent, FakeCall, FakeCompanionChannel, instruction_context
from gpt_voicecoding.adapters.agent._progress import encoded_size
from gpt_voicecoding.control_plane.actions import ControlPlane
from gpt_voicecoding.control_plane.payloads import progress_document
from gpt_voicecoding.core.bridge import BridgeCore
from gpt_voicecoding.core.relay_queue import RelayQueue
from gpt_voicecoding.core.sessions import Session, SessionRegistry
from gpt_voicecoding.core.state import BridgeState
from gpt_voicecoding.core.switches import Switchboard, SwitchName
from gpt_voicecoding.core.verification import SeamLoad
from gpt_voicecoding.seams.agent import (
    ApprovalRequest,
    AwaitingApproval,
    ChildClassification,
    ChildKind,
    LaneDiscovery,
    LaneUnavailable,
    Progress,
    ProgressEntry,
    ProgressRole,
    ReplyWindow,
    ReplyWindowChanged,
    SessionInspection,
    SessionState,
    SessionStopped,
    WaitingFor,
    WaitingKind,
)
from gpt_voicecoding.seams.control_plane import Action, ErrorCode, Reply, Request
from gpt_voicecoding.seams.identity import AgentKind, SessionName, SessionTarget

WORKSPACE = Path("/tmp/workspace")
SECOND_WORKSPACE = Path("/tmp/another-workspace")
NAME = SessionName(project="GPT-VoiceCoding", task="build the control plane")
CODEX = SessionTarget(agent=AgentKind.CODEX, session_id="abc")
CLAUDE = SessionTarget(agent=AgentKind.CLAUDE, session_id="claude-abc", pid=1234)
SECOND_CODEX = SessionTarget(agent=AgentKind.CODEX, session_id="def")
CODEX_ADDRESS = {"agent": "codex", "session_id": "abc", "pid": None}

#: One fixed moment, so a rendered reading is compared against a value and not a clock.
READ_AT = datetime(2026, 8, 26, 2, 44, 39, tzinfo=UTC)


class Surface:
    """One assembled engine-side control plane, and the knobs a test needs."""

    def __init__(self, *, duty: bool = True) -> None:
        self.agent = FakeAgent()
        self.call = FakeCall()
        self.channel = FakeCompanionChannel()
        self.state = BridgeState(
            switches=Switchboard(), sessions=SessionRegistry(), relays=RelayQueue()
        )
        self.state.switches.flip(SwitchName.DUTY, duty)
        self.state.switches.flip(SwitchName.VOICE, duty)
        self.state.switches.flip(SwitchName.MESSAGE, duty)
        self.core = BridgeCore(
            state=self.state,
            call=self.call,
            channel=self.channel,
            agents={AgentKind.CLAUDE: self.agent, AgentKind.CODEX: self.agent},
            inventory=(SeamLoad(seam="call", configured="a.call"),),
            instruction_context=instruction_context(),
        )
        self.plane = ControlPlane(self.core)

    def ask(self, action: Action, **payload: object) -> Reply:
        return asyncio.run(self.plane.handle(Request(action=action, payload=payload)))

    def register(self, target: SessionTarget = CODEX) -> Session:
        """Put one Session on the roster.

        Written straight into the registry because nothing else can: the launch
        transaction that used to seed these tests is parked (#72), and the
        discovery path that replaces it is not built yet. What these tests are
        about is what the control plane does with a roster, not how a row got
        onto one.
        """
        return self.state.sessions.register(
            Session(target=target, name=NAME, workspace=WORKSPACE, first_seen=0.0)
        )

    def open_window(self) -> None:
        """The Session says it will take a user turn now."""
        asyncio.run(self.core.dispatch(ReplyWindowChanged(target=CODEX, window=ReplyWindow.OPEN)))


class TestWithEverySwitchOff:
    """ADR 0002, absolute: every action answers with Duty off. All of them."""

    def test_every_action_succeeds_with_duty_voice_and_message_off(self) -> None:
        surface = Surface(duty=False)
        surface.register()
        # `progress` needs something to have been read, or it refuses for a real
        # reason — which is not this test's subject. The subject is that no
        # *switch* refuses anything, so the lane is given a reading to answer.
        surface.agent.discovery = LaneDiscovery(
            rows=(
                SessionInspection(
                    target=CODEX,
                    workspace=WORKSPACE,
                    state=SessionState.IDLE,
                    progress=Progress(read_at=READ_AT),
                ),
            )
        )
        asyncio.run(
            surface.core.approvals.opened(
                ApprovalRequest(approval_id="a1", target=CODEX, tool_name="Bash")
            )
        )

        replies = {
            Action.STATUS: surface.ask(Action.STATUS),
            Action.SWITCH: surface.ask(Action.SWITCH, name="duty", on=False),
            Action.SESSIONS: surface.ask(Action.SESSIONS),
            Action.PROGRESS: surface.ask(Action.PROGRESS, target=CODEX_ADDRESS),
            Action.LIVE: surface.ask(Action.LIVE),
            Action.RELAY: surface.ask(Action.RELAY, target=CODEX_ADDRESS, text="carry on"),
            Action.APPROVE: surface.ask(Action.APPROVE, approval_id="a1", verdict="allow"),
            Action.VERIFY: surface.ask(Action.VERIFY),
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
        surface.register()

        data = surface.ask(Action.STATUS).data

        assert data["switches"]["duty"] is True
        assert data["call_id"] is None
        assert data["sessions"][0]["target"] == CODEX_ADDRESS
        assert data["sessions"][0]["name"] == str(NAME)
        # Two facts, two fields: whether the Session is still there, and what
        # it is doing. They used to be one enum, which is how a busy Session and
        # a finished one read alike.
        assert data["sessions"][0]["lifecycle"] == "live"
        assert data["sessions"][0]["state"] == "running"
        assert data["pending_relays"] == []
        assert data["pending_approvals"] == []

    def test_the_roster_is_answerable_on_its_own(self) -> None:
        surface = Surface()
        surface.register()

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
        reply = Surface().ask(Action.RELAY, target=CODEX_ADDRESS, text="carry on")

        assert reply.error is not None
        assert reply.error.code is ErrorCode.UNKNOWN_SESSION

    def test_a_claude_target_without_a_pid_never_reaches_the_hub(self) -> None:
        """A resumed Claude session forks; a target without a pid is ambiguous."""
        reply = Surface().ask(
            Action.RELAY,
            target={"agent": "claude", "session_id": "abc", "pid": None},
            text="carry on",
        )

        assert reply.error is not None
        assert reply.error.code is ErrorCode.INVALID_PAYLOAD
        assert "pid" in reply.error.message

    def test_a_verdict_nothing_is_waiting_for(self) -> None:
        reply = Surface().ask(Action.APPROVE, approval_id="never", verdict="allow")

        assert reply.error is not None
        assert reply.error.code is ErrorCode.UNKNOWN_PENDING

    def test_a_question_prompt_id_is_not_an_approval_that_can_be_approved(self) -> None:
        surface = Surface()
        surface.register(CLAUDE)
        asyncio.run(
            surface.core.dispatch(
                SessionStopped(
                    target=CLAUDE,
                    waiting_for=WaitingFor(
                        kind=WaitingKind.QUESTION,
                        prompt="Which base?",
                        approval_id="question-prompt",
                    ),
                )
            )
        )

        reply = surface.ask(
            Action.APPROVE,
            approval_id="question-prompt",
            verdict="allow",
        )

        assert reply.error is not None
        assert reply.error.code is ErrorCode.UNKNOWN_PENDING
        assert surface.agent.calls == []

    def test_a_verdict_that_is_not_one(self) -> None:
        reply = Surface().ask(Action.APPROVE, approval_id="a1", verdict="maybe")

        assert reply.error is not None
        assert reply.error.code is ErrorCode.INVALID_PAYLOAD

    def test_relayed_words_that_are_not_there(self) -> None:
        surface = Surface()
        surface.register()

        reply = surface.ask(Action.RELAY, target=CODEX_ADDRESS, text="   ")

        assert reply.error is not None
        assert reply.error.code is ErrorCode.INVALID_PAYLOAD


class TestRelayingAndApproving:
    def test_the_users_words_reach_the_session(self) -> None:
        surface = Surface()
        surface.register()
        surface.open_window()

        data = surface.ask(Action.RELAY, target=CODEX_ADDRESS, text="carry on").data

        assert data["state"] == "delivered"
        assert [call.text for call in surface.agent.calls] == ["carry on"]

    def test_a_relay_names_the_route_it_took(self) -> None:
        surface = Surface()
        surface.register()
        surface.open_window()

        data = surface.ask(
            Action.RELAY, target=CODEX_ADDRESS, text="carry on", route="deliver"
        ).data

        assert data["route"] == "deliver"

    def test_a_verdict_is_carried_and_the_loop_closed(self) -> None:
        surface = Surface()
        surface.register()
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


class TestProgress:
    """#76's verb: one exact Session, read now, and never a turn."""

    def stopped(self, *, said: str = "done") -> LaneDiscovery:
        return LaneDiscovery(
            rows=(
                SessionInspection(
                    target=CODEX,
                    workspace=WORKSPACE,
                    state=SessionState.IDLE,
                    progress=Progress(
                        recent=(ProgressEntry(role=ProgressRole.ASSISTANT, text=said),),
                        truncated=True,
                        read_at=READ_AT,
                    ),
                    last_activity=READ_AT,
                ),
            )
        )

    def test_one_session_is_read_now_and_rendered_as_a_roster_row(self) -> None:
        """The same document `sessions` renders: a surface learns no second shape."""
        surface = Surface()
        surface.register()
        surface.agent.discovery = self.stopped()

        reply = surface.ask(Action.PROGRESS, target=CODEX_ADDRESS)

        assert reply.ok
        session = reply.data["session"]
        assert session["target"] == CODEX_ADDRESS
        assert session["progress"] == {
            "recent": [{"role": "assistant", "text": "done"}],
            "truncated": True,
            "read_at": READ_AT.isoformat(),
        }
        assert session["last_activity"] == READ_AT.isoformat()

    def test_progress_renders_the_live_reply_window_for_an_answerable_question(self) -> None:
        surface = Surface()
        surface.register()
        surface.agent.answerable_questions.add(CODEX)
        surface.agent.discovery = LaneDiscovery(
            rows=(
                SessionInspection(
                    target=CODEX,
                    workspace=WORKSPACE,
                    state=SessionState.WAITING,
                    waiting_for=WaitingFor(
                        kind=WaitingKind.QUESTION,
                        prompt="Which base?",
                    ),
                    progress=Progress(read_at=READ_AT),
                ),
            )
        )

        reply = surface.ask(Action.PROGRESS, target=CODEX_ADDRESS)

        assert reply.ok
        assert reply.data["session"]["reply_window"] == "open"

    def test_it_asks_the_lane_rather_than_answering_from_the_roster(self) -> None:
        """A cached answer would make the verb no fresher than the tick beside it."""
        surface = Surface()
        surface.register()
        surface.agent.discovery = self.stopped()

        surface.ask(Action.PROGRESS, target=CODEX_ADDRESS)

        assert surface.agent.inspections == [CODEX]

    def test_the_reading_becomes_the_rosters_truth(self) -> None:
        """Asking for progress then asking for status cannot say two things."""
        surface = Surface()
        surface.register()
        surface.agent.discovery = self.stopped(said="halfway")

        surface.ask(Action.PROGRESS, target=CODEX_ADDRESS)
        roster = surface.ask(Action.SESSIONS).data["sessions"]

        assert roster[0]["progress"]["recent"] == [{"role": "assistant", "text": "halfway"}]

    def test_it_ends_nothing_it_did_not_look_at(self) -> None:
        """A verb asked about one Session concludes nothing about the others."""
        surface = Surface()
        surface.register()
        surface.register(SECOND_CODEX)
        surface.agent.discovery = self.stopped()

        surface.ask(Action.PROGRESS, target=CODEX_ADDRESS)

        assert len(surface.ask(Action.SESSIONS).data["sessions"]) == 2

    def test_a_session_nobody_registered_is_refused_by_identity(self) -> None:
        surface = Surface()

        reply = surface.ask(Action.PROGRESS, target=CODEX_ADDRESS)

        assert reply.error is not None
        assert reply.error.code is ErrorCode.UNKNOWN_SESSION

    def test_a_lane_that_could_not_look_refuses_rather_than_saying_nothing_happened(
        self,
    ) -> None:
        """ "I could not look" and "it has said nothing" are different facts."""
        surface = Surface()
        surface.register()
        surface.agent.inspect_raises = LaneUnavailable(AgentKind.CODEX, "`codex` is not on PATH")

        reply = surface.ask(Action.PROGRESS, target=CODEX_ADDRESS)

        assert reply.error is not None
        assert reply.error.code is ErrorCode.REFUSED
        assert "`codex` is not on PATH" in reply.error.message

    def test_a_lane_that_could_not_look_leaves_the_row_as_it_was(self) -> None:
        surface = Surface()
        surface.register()
        surface.agent.inspect_raises = LaneUnavailable(AgentKind.CODEX, "`codex` is not on PATH")

        surface.ask(Action.PROGRESS, target=CODEX_ADDRESS)

        assert surface.ask(Action.SESSIONS).data["sessions"][0]["lifecycle"] == "live"

    def test_a_session_with_no_readable_progress_refuses_rather_than_says_nothing(
        self,
    ) -> None:
        """An unattached Codex row, or one whose first turn wrote no record (#73).

        The refusal is #76's "honest error for an unattached or ended row": a
        surface handed a successful reply carrying no progress would render a
        Session nobody could read as one that has said nothing.
        """
        surface = Surface()
        surface.register()
        surface.agent.discovery = LaneDiscovery(
            rows=(SessionInspection(target=CODEX, workspace=WORKSPACE, state=SessionState.IDLE),)
        )

        reply = surface.ask(Action.PROGRESS, target=CODEX_ADDRESS)

        assert reply.error is not None
        assert reply.error.code is ErrorCode.REFUSED
        assert "never infers one" in reply.error.message

    def test_a_session_read_and_found_silent_answers_rather_than_refuses(self) -> None:
        """The other side of that line, and the whole reason it is drawn."""
        surface = Surface()
        surface.register()
        surface.agent.discovery = LaneDiscovery(
            rows=(
                SessionInspection(
                    target=CODEX,
                    workspace=WORKSPACE,
                    state=SessionState.IDLE,
                    progress=Progress(read_at=READ_AT),
                ),
            )
        )

        reply = surface.ask(Action.PROGRESS, target=CODEX_ADDRESS)

        assert reply.ok
        assert reply.data["session"]["progress"]["recent"] == []

    def test_a_session_that_has_ended_is_a_stale_target_not_an_empty_answer(self) -> None:
        """#76's other honest error."""
        surface = Surface()
        surface.register()
        surface.agent.discovery = LaneDiscovery()  # a lane that looked and found nothing

        reply = surface.ask(Action.PROGRESS, target=CODEX_ADDRESS)

        assert reply.error is not None
        assert reply.error.code is ErrorCode.STALE_SESSION

    def test_it_does_not_end_the_row_itself(self) -> None:
        """Ending a row is `observe`'s, and only `observe`'s.

        The value an `inspect` answers for a Session it could not find carries no
        workspace and no name, so folding it into the roster would strip the very
        fields a surface needs to say what happened to it. The next discovery
        ends it properly, within one cadence.
        """
        surface = Surface()
        held = surface.register()
        surface.agent.discovery = LaneDiscovery()

        surface.ask(Action.PROGRESS, target=CODEX_ADDRESS)

        assert surface.state.sessions.all() == (held,)

    def test_a_child_process_is_refused_before_any_lane_is_touched(self) -> None:
        """Seen, never spoken to — and never asked on its own behalf either (#68)."""
        surface = Surface()
        surface.state.sessions.register(
            Session(
                target=CODEX,
                workspace=WORKSPACE,
                first_seen=0.0,
                child=ChildClassification(kind=ChildKind.CHILD, parent=SECOND_CODEX),
            )
        )

        reply = surface.ask(Action.PROGRESS, target=CODEX_ADDRESS)

        assert reply.error is not None
        assert surface.agent.inspections == []

    def test_the_budget_is_measured_on_the_document_that_travels(self) -> None:
        """`_progress.encoded_size` and `progress_document` are one shape (#47's lesson)."""
        entries = (
            ProgressEntry(role=ProgressRole.USER, text="do the thing"),
            ProgressEntry(role=ProgressRole.ASSISTANT, text="done"),
        )
        rendered = progress_document(Progress(recent=entries))

        assert encoded_size(entries) == len(
            json.dumps(rendered["recent"], ensure_ascii=False).encode("utf-8")
        )

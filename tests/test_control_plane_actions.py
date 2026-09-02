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
from gpt_voicecoding.control_plane.actions import ControlPlane
from gpt_voicecoding.control_plane.progress_publication import ProgressPublication
from gpt_voicecoding.core.bridge import BridgeCore
from gpt_voicecoding.core.policy import CorePolicy
from gpt_voicecoding.core.relay_queue import RelayQueue
from gpt_voicecoding.core.sessions import Session, SessionRegistry
from gpt_voicecoding.core.state import BridgeState
from gpt_voicecoding.core.switches import Switchboard, SwitchName
from gpt_voicecoding.core.verification import SeamLoad
from gpt_voicecoding.seams.agent import (
    ChildClassification,
    ChildKind,
    LaneDiscovery,
    LaneUnavailable,
    Option,
    ProgressEntry,
    ProgressObservation,
    ProgressOmission,
    ProgressRole,
    ReplyWindow,
    ReplyWindowChanged,
    SessionInspection,
    SessionState,
    SessionStopped,
    WaitingFor,
    WaitingKind,
)
from gpt_voicecoding.seams.control_plane import (
    Action,
    ErrorCode,
    MalformedRequest,
    Reply,
    Request,
)
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


def wire(reply: Reply) -> bytes:
    return json.dumps(reply.as_document(), ensure_ascii=False).encode("utf-8") + b"\n"


class Surface:
    """One assembled engine-side control plane, and the knobs a test needs."""

    def __init__(
        self,
        *,
        duty: bool = True,
        max_bytes: int = 65_536,
        page_entries: int = CorePolicy().history_page_entries,
    ) -> None:
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
            policy=CorePolicy(history_page_entries=page_entries),
        )
        self.plane = ControlPlane(
            self.core,
            progress_publication=ProgressPublication(max_bytes=max_bytes),
        )

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

    def dialog_on_screen(self, target: SessionTarget = CODEX, approval_id: str = "a1") -> None:
        """One Session stopped on a permission its lane is still holding.

        The product's only path to that state since #191: the Stop carries the
        dialog's handle in its `WaitingFor`, and the roster row is where the
        Approval Relay finds it.
        """
        asyncio.run(
            self.core.dispatch(
                SessionStopped(
                    target=target,
                    waiting_for=WaitingFor(
                        kind=WaitingKind.PERMISSION,
                        tool_name="Bash",
                        approval_id=approval_id,
                    ),
                )
            )
        )


class TestWithEverySwitchOff:
    """ADR 0002, absolute: every action answers with Duty off. All of them."""

    def test_every_action_succeeds_with_duty_voice_and_message_off(self) -> None:
        surface = Surface(duty=False)
        surface.register()
        # `history` needs a lane that holds a record, or it refuses for a real
        # reason — which is not this test's subject. The subject is that no
        # *switch* refuses anything, so the lane is given one to answer from.
        surface.agent.records[CODEX] = ()
        surface.agent.discovery = LaneDiscovery(
            rows=(
                SessionInspection(
                    target=CODEX,
                    workspace=WORKSPACE,
                    state=SessionState.IDLE,
                    progress=ProgressObservation.readable(
                        has_history=False,
                        read_at=READ_AT,
                    ),
                ),
            )
        )
        surface.dialog_on_screen()

        replies = {
            Action.STATUS: surface.ask(Action.STATUS),
            Action.SWITCH: surface.ask(Action.SWITCH, name="duty", on=False),
            Action.BRIEF: surface.ask(Action.BRIEF),
            Action.HISTORY: surface.ask(Action.HISTORY, target=CODEX_ADDRESS),
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
    def test_question_option_descriptions_cross_the_control_plane(self) -> None:
        surface = Surface()
        surface.state.sessions.register(
            Session(
                target=CODEX,
                workspace=WORKSPACE,
                first_seen=0.0,
                state=SessionState.WAITING,
                waiting_for=WaitingFor(
                    kind=WaitingKind.QUESTION,
                    prompt="Which base?",
                    options=(
                        Option(
                            text="main",
                            description="Merge into the default branch",
                        ),
                    ),
                ),
            )
        )

        options = surface.ask(Action.STATUS).data["sessions"][0]["waiting_for"]["options"]

        assert options == [
            {
                "text": "main",
                "description": "Merge into the default branch",
                "recommended": False,
            }
        ]

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
        # Protocol 8: no second list beside the rows. A pending permission is
        # the row's own `waiting_for`, and the panel counts those (#191).
        assert "pending_approvals" not in data

    def test_the_roster_is_answerable_on_its_own(self) -> None:
        """The Roster Brief names the same Sessions `status` holds rows for."""
        surface = Surface()
        surface.register()

        rows = surface.ask(Action.BRIEF).data["roster"]["rows"]

        assert [row["target"] for row in rows] == [
            row["target"] for row in surface.ask(Action.STATUS).data["sessions"]
        ]

    def test_a_session_not_yet_read_is_not_published_as_empty_history(self) -> None:
        surface = Surface()
        surface.register()

        progress = surface.ask(Action.STATUS).data["sessions"][0]["progress"]

        assert progress == {
            "availability": "not_read",
            "has_history": None,
            "omission": "none",
            "read_at": None,
            "recent": [],
        }

    def test_status_reports_an_unreadable_source_as_lane_degradation(self) -> None:
        surface = Surface()
        surface.register()
        reason = "the daemon dropped the progress read"
        surface.state.sessions.observe(
            AgentKind.CODEX,
            LaneDiscovery(
                rows=(
                    SessionInspection(
                        target=CODEX,
                        workspace=WORKSPACE,
                        progress=ProgressObservation.unreadable(reason),
                    ),
                ),
                degraded=reason,
            ),
            now=1.0,
        )

        data = surface.ask(Action.STATUS).data

        assert data["degraded_lanes"] == {"codex": reason}
        assert data["sessions"][0]["progress"]["availability"] == "not_read"

    def test_thirty_eight_large_histories_publish_every_compact_row_within_the_limit(
        self,
    ) -> None:
        surface = Surface()
        for index in range(38):
            surface.state.sessions.register(
                Session(
                    target=SessionTarget(
                        agent=AgentKind.CODEX,
                        session_id=f"session-{index}",
                    ),
                    workspace=WORKSPACE,
                    first_seen=float(index),
                    progress=ProgressObservation.readable(
                        has_history=True,
                        recent=(
                            ProgressEntry(
                                ordinal=0,
                                role=ProgressRole.ASSISTANT,
                                text="x" * 20_000,
                            ),
                        ),
                        omission=ProgressOmission.NONE,
                        read_at=READ_AT,
                    ),
                )
            )

        reply = surface.ask(Action.STATUS)

        assert reply.ok
        assert len(reply.data["sessions"]) == 38
        assert all(
            row["progress"]["recent"] == [] and row["progress"]["omission"] == "status_summary"
            for row in reply.data["sessions"]
        )
        assert len(wire(reply)) <= 65_536

    def test_an_over_limit_status_skeleton_returns_a_bounded_refusal_without_losing_rows(
        self,
    ) -> None:
        surface = Surface(max_bytes=512)
        surface.state.sessions.register(
            Session(
                target=CODEX,
                workspace=Path("/" + "large-workspace/" * 100),
                first_seen=0.0,
            )
        )

        reply = surface.ask(Action.STATUS)

        assert reply.error is not None
        assert reply.error.code is ErrorCode.REFUSED
        assert len(wire(reply)) <= 512
        assert len(surface.state.sessions.all()) == 1


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

    def test_a_verdict_is_carried_and_answered_with_its_receipt(self) -> None:
        """The receipt is the Relay's: a state, a grade and a reason (#192)."""
        surface = Surface()
        surface.register()
        surface.dialog_on_screen()

        data = surface.ask(Action.APPROVE, approval_id="a1", verdict="allow").data

        assert data["verdict"] == "allow"
        assert data["approval_id"] == "a1"
        assert data["state"] == "delivered"
        assert data["receipt"]["outcome"] == "delivered"
        assert data["reason"] == "delivered"
        assert "closing_notice" not in data
        assert [call.verdict for call in surface.agent.calls if call.verb == "approval_relay"]

    def test_a_verdict_for_a_spawned_target_is_refused_in_its_own_words(self) -> None:
        """ "Never spoken to" includes never answered, and it reads differently."""
        child = SessionTarget(agent=AgentKind.CODEX, session_id="child-1", pid=77)
        surface = Surface()
        surface.register()
        surface.state.sessions.register(
            Session(
                target=child,
                name=NAME,
                workspace=WORKSPACE,
                first_seen=0.0,
                child=ChildClassification(kind=ChildKind.CHILD, parent=CODEX),
            )
        )
        surface.state.sessions.set_stop_reading(
            child,
            waiting_for=WaitingFor(kind=WaitingKind.PERMISSION, tool_name="Bash", approval_id="c1"),
            progress=ProgressObservation(),
        )

        reply = surface.ask(Action.APPROVE, approval_id="c1", verdict="allow")

        assert reply.error is not None
        assert reply.error.code is ErrorCode.REFUSED
        assert "Child Process" in reply.error.message
        assert surface.agent.calls == []


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


class TestHistory:
    """#171's verb: one page of one Session's own words, older on request."""

    SAID = (
        "the first thing",
        "the second thing",
        "the third thing",
        "the fourth thing",
        "the fifth thing",
        "the sixth thing",
        "the seventh thing",
    )

    def record(self, said: tuple[str, ...] | None = None) -> tuple[ProgressEntry, ...]:
        """One Session's whole visible record, oldest first and numbered."""
        return tuple(
            ProgressEntry(
                ordinal=index,
                role=ProgressRole.USER if index % 2 == 0 else ProgressRole.ASSISTANT,
                text=text,
            )
            for index, text in enumerate(self.SAID if said is None else said)
        )

    def surface(self, said: tuple[str, ...] | None = None, **kwargs: object) -> Surface:
        surface = Surface(**kwargs)  # type: ignore[arg-type]
        surface.register()
        surface.agent.records[CODEX] = self.record(said)
        return surface

    def test_the_newest_page_includes_the_newest_entry(self) -> None:
        """Every page is complete on its own; the engine remembers nothing (#171)."""
        surface = self.surface()

        reply = surface.ask(Action.HISTORY, target=CODEX_ADDRESS)

        assert reply.ok
        assert [entry["text"] for entry in reply.data["entries"]] == [
            "the seventh thing",
            "the sixth thing",
            "the fifth thing",
            "the fourth thing",
            "the third thing",
        ]
        assert [entry["ordinal"] for entry in reply.data["entries"]] == [6, 5, 4, 3, 2]
        assert reply.data["older"] is True

    def test_the_page_size_is_the_engines_dial_and_never_the_callers(self) -> None:
        surface = self.surface(page_entries=2)

        entries = surface.ask(Action.HISTORY, target=CODEX_ADDRESS).data["entries"]

        assert [entry["ordinal"] for entry in entries] == [6, 5]

    def test_the_cursor_asks_for_the_entries_before_it(self) -> None:
        surface = self.surface()

        reply = surface.ask(Action.HISTORY, target=CODEX_ADDRESS, before=2)

        assert [entry["ordinal"] for entry in reply.data["entries"]] == [1, 0]
        assert reply.data["older"] is False

    def test_a_cursor_past_the_oldest_entry_is_an_empty_page_rather_than_a_refusal(
        self,
    ) -> None:
        """An answer, not a refusal: there is simply nothing before the first thing said."""
        surface = self.surface()

        reply = surface.ask(Action.HISTORY, target=CODEX_ADDRESS, before=0)

        assert reply.ok
        assert reply.data["entries"] == []
        assert reply.data["older"] is False
        assert reply.data["read_at"] is not None

    def test_a_cursor_above_every_ordinal_is_the_newest_page(self) -> None:
        surface = self.surface()

        reply = surface.ask(Action.HISTORY, target=CODEX_ADDRESS, before=9_999)

        assert [entry["ordinal"] for entry in reply.data["entries"]] == [6, 5, 4, 3, 2]

    def test_fewer_entries_than_a_page_is_the_whole_history(self) -> None:
        surface = self.surface(said=("only this", "and this"))

        reply = surface.ask(Action.HISTORY, target=CODEX_ADDRESS)

        assert [entry["ordinal"] for entry in reply.data["entries"]] == [1, 0]
        assert reply.data["older"] is False

    def test_an_ordinal_names_the_same_entry_across_a_read_while_the_session_appends(
        self,
    ) -> None:
        """Both sources are append-only for what this seam keeps, so a cursor holds."""
        surface = self.surface()
        first = surface.ask(Action.HISTORY, target=CODEX_ADDRESS).data

        surface.agent.records[CODEX] = self.record((*self.SAID, "and one more"))
        after = surface.ask(Action.HISTORY, target=CODEX_ADDRESS, before=2).data

        assert [entry["ordinal"] for entry in first["entries"]] == [6, 5, 4, 3, 2]
        assert [entry["text"] for entry in after["entries"]] == [
            "the second thing",
            "the first thing",
        ]

    def test_an_oversize_entry_keeps_its_slot_and_the_page_advances(self) -> None:
        """ADR 0016: named as omitted, never cut, and never silently dropped."""
        surface = self.surface(
            said=("small one", "x" * 4_000, "another small one"),
            max_bytes=2_048,
        )

        reply = surface.ask(Action.HISTORY, target=CODEX_ADDRESS)

        assert reply.ok
        assert reply.data["entries"] == [
            {"ordinal": 2, "role": "user", "text": "another small one"},
            {"ordinal": 1, "role": "assistant", "omission": "oversize"},
            {"ordinal": 0, "role": "user", "text": "small one"},
        ]
        assert len(wire(reply)) <= 2_048

    def test_a_page_is_never_folded_into_the_roster(self) -> None:
        """`inspect` is the roster's read; a page is a separate one (ADR 0016)."""
        surface = self.surface()

        surface.ask(Action.HISTORY, target=CODEX_ADDRESS)

        assert surface.agent.inspections == []
        assert surface.agent.calls == []
        assert surface.ask(Action.STATUS).data["sessions"][0]["progress"] == {
            "availability": "not_read",
            "has_history": None,
            "omission": "none",
            "read_at": None,
            "recent": [],
        }

    def test_one_lane_read_per_page(self) -> None:
        """A second `inspect` for a fresher staleness check would fold it back in."""
        surface = self.surface()

        surface.ask(Action.HISTORY, target=CODEX_ADDRESS, before=4)

        assert surface.agent.pages == [(CODEX, 4, 5)]

    def test_a_session_nobody_registered_is_refused_by_identity(self) -> None:
        surface = Surface()

        reply = surface.ask(Action.HISTORY, target=CODEX_ADDRESS)

        assert reply.error is not None
        assert reply.error.code is ErrorCode.UNKNOWN_SESSION
        assert surface.agent.pages == []

    def test_a_session_that_has_ended_is_a_stale_target_not_an_empty_page(self) -> None:
        surface = self.surface()
        surface.state.sessions.observe(AgentKind.CODEX, LaneDiscovery(), now=1.0)

        reply = surface.ask(Action.HISTORY, target=CODEX_ADDRESS)

        assert reply.error is not None
        assert reply.error.code is ErrorCode.STALE_SESSION
        assert surface.agent.pages == []

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

        reply = surface.ask(Action.HISTORY, target=CODEX_ADDRESS)

        assert reply.error is not None
        assert surface.agent.pages == []

    def test_a_lane_that_could_not_look_refuses_rather_than_saying_nothing_was_said(
        self,
    ) -> None:
        """ "I could not look" and "it has said nothing" are different facts."""
        surface = self.surface()
        surface.agent.history_raises = LaneUnavailable(AgentKind.CODEX, "`codex` is not on PATH")

        reply = surface.ask(Action.HISTORY, target=CODEX_ADDRESS)

        assert reply.error is not None
        assert reply.error.code is ErrorCode.REFUSED
        assert "`codex` is not on PATH" in reply.error.message

    def test_a_lane_holding_no_record_refuses_rather_than_answering_an_empty_page(
        self,
    ) -> None:
        """A Codex thread the daemon does not hold, or a transcript nobody named."""
        surface = Surface()
        surface.register()

        reply = surface.ask(Action.HISTORY, target=CODEX_ADDRESS)

        assert reply.error is not None
        assert reply.error.code is ErrorCode.REFUSED
        assert "never infers one" in reply.error.message

    def test_a_lane_that_could_not_look_leaves_the_row_as_it_was(self) -> None:
        surface = self.surface()
        surface.agent.history_raises = LaneUnavailable(AgentKind.CODEX, "`codex` is not on PATH")

        surface.ask(Action.HISTORY, target=CODEX_ADDRESS)

        assert surface.ask(Action.STATUS).data["sessions"][0]["lifecycle"] == "live"

    def test_a_cursor_that_is_not_an_ordinal_is_an_unusable_payload(self) -> None:
        surface = self.surface()

        reply = surface.ask(Action.HISTORY, target=CODEX_ADDRESS, before="newest")

        assert reply.error is not None
        assert reply.error.code is ErrorCode.INVALID_PAYLOAD

    def test_unicode_and_json_escaping_are_measured_as_actual_wire_bytes(self) -> None:
        text = ('雪"\\\n' * 90) + "done"
        measuring = self.surface(said=("small one", text))
        complete = measuring.ask(Action.HISTORY, target=CODEX_ADDRESS)
        exact_capacity = len(wire(complete))

        fitting = self.surface(said=("small one", text), max_bytes=exact_capacity)
        fits = fitting.ask(Action.HISTORY, target=CODEX_ADDRESS)

        too_small = self.surface(said=("small one", text), max_bytes=exact_capacity - 1)
        omitted = too_small.ask(Action.HISTORY, target=CODEX_ADDRESS)

        assert fits.data["entries"][0]["text"] == text
        assert len(wire(fits)) == exact_capacity
        assert omitted.data["entries"][0] == {
            "ordinal": 1,
            "role": "assistant",
            "omission": "oversize",
        }
        assert omitted.data["entries"][1]["text"] == "small one"
        assert len(wire(omitted)) <= exact_capacity - 1


class TestBrief:
    """The Briefing verb on the wire — one address, or none at all."""

    def stopped_on_a_question(self) -> LaneDiscovery:
        return LaneDiscovery(
            rows=(
                SessionInspection(
                    target=CODEX,
                    workspace=WORKSPACE,
                    state=SessionState.WAITING,
                    waiting_for=WaitingFor(
                        kind=WaitingKind.QUESTION,
                        prompt="Which base?",
                        options=(Option(text="main", recommended=True),),
                        recommendation="main",
                    ),
                    progress=ProgressObservation.readable(
                        has_history=True,
                        recent=(
                            ProgressEntry(
                                ordinal=0, role=ProgressRole.ASSISTANT, text="I got this far"
                            ),
                        ),
                        read_at=READ_AT,
                    ),
                    last_activity=READ_AT,
                ),
            )
        )

    def test_with_no_address_it_answers_the_roster_brief(self) -> None:
        surface = Surface()
        surface.register()

        data = surface.ask(Action.BRIEF).data

        assert data["kind"] == "roster"
        assert data["roster"]["rows"][0]["target"] == CODEX_ADDRESS
        assert data["text"]

    def test_with_no_address_it_touches_no_lane(self) -> None:
        """The Roster Brief is a read of what the hub already holds."""
        surface = Surface()
        surface.register()

        surface.ask(Action.BRIEF)

        assert surface.agent.inspections == []

    def test_an_address_reads_that_one_session_now_through_one_inspect(self) -> None:
        surface = Surface()
        surface.register()
        surface.agent.discovery = self.stopped_on_a_question()

        data = surface.ask(Action.BRIEF, target=CODEX_ADDRESS).data

        assert surface.agent.inspections == [CODEX]
        assert surface.agent.calls == []
        assert data["kind"] == "session"
        assert data["session"]["state"] == "decision"
        assert data["session"]["newest"] == {"state": "said", "text": "I got this far"}
        assert data["session"]["decision"] == {
            "prompt": "Which base?",
            "options": [{"text": "main", "description": None, "recommended": True}],
            "recommendation": "main",
            "tool": None,
            "summary": None,
        }
        assert data["session"]["last_activity_at"] == READ_AT.isoformat()

    def test_the_rendered_text_travels_beside_the_structure(self) -> None:
        """One renderer (#166 B6): `bridgectl` prints this rather than composing."""
        surface = Surface()
        surface.register()
        surface.agent.discovery = self.stopped_on_a_question()

        data = surface.ask(Action.BRIEF, target=CODEX_ADDRESS).data

        assert "Which base?" in data["text"]
        assert "waiting for your decision" in data["text"]

    def test_the_reading_becomes_the_rosters_truth(self) -> None:
        """Asking for a brief then for status cannot say two things."""
        surface = Surface()
        surface.register()
        surface.agent.discovery = self.stopped_on_a_question()

        surface.ask(Action.BRIEF, target=CODEX_ADDRESS)

        assert surface.ask(Action.STATUS).data["sessions"][0]["state"] == "waiting"

    def test_a_session_nobody_registered_is_refused_by_identity(self) -> None:
        surface = Surface()

        reply = surface.ask(Action.BRIEF, target=CODEX_ADDRESS)

        assert reply.error is not None
        assert reply.error.code is ErrorCode.UNKNOWN_SESSION

    def test_a_session_that_has_ended_is_a_stale_target(self) -> None:
        surface = Surface()
        surface.register()
        surface.agent.discovery = LaneDiscovery()  # a lane that looked and found nothing

        reply = surface.ask(Action.BRIEF, target=CODEX_ADDRESS)

        assert reply.error is not None
        assert reply.error.code is ErrorCode.STALE_SESSION

    def test_a_lane_that_could_not_look_refuses_in_the_lanes_own_words(self) -> None:
        surface = Surface()
        surface.register()
        surface.agent.inspect_raises = LaneUnavailable(AgentKind.CODEX, "`codex` is not on PATH")

        reply = surface.ask(Action.BRIEF, target=CODEX_ADDRESS)

        assert reply.error is not None
        assert reply.error.code is ErrorCode.REFUSED
        assert "`codex` is not on PATH" in reply.error.message

    def test_a_spawned_child_is_refused_and_the_refusal_names_it(self) -> None:
        """Seen, never spoken to (#68) — and never briefed about either."""
        surface = Surface()
        surface.state.sessions.observe(
            AgentKind.CODEX,
            LaneDiscovery(
                rows=(
                    SessionInspection(
                        target=CODEX,
                        workspace=WORKSPACE,
                        child=ChildClassification(kind=ChildKind.CHILD, parent=SECOND_CODEX),
                    ),
                )
            ),
            now=1.0,
        )

        reply = surface.ask(Action.BRIEF, target=CODEX_ADDRESS)

        assert reply.error is not None
        assert reply.error.code is ErrorCode.REFUSED
        assert str(CODEX) in reply.error.message
        assert "Child Process" in reply.error.message

    def test_a_read_that_failed_now_is_reported_now_and_not_from_the_last_tick(self) -> None:
        """A brief is one reading taken at the moment the user is spoken to.

        The roster keeps its last readable observation on purpose; a verb that
        answers *now* must not borrow it, or the Voice reads out an earlier
        tick's message as though it had just been said.
        """
        surface = Surface()
        surface.register()
        surface.agent.discovery = self.stopped_on_a_question()
        surface.ask(Action.BRIEF, target=CODEX_ADDRESS)

        surface.agent.discovery = LaneDiscovery(
            rows=(
                SessionInspection(
                    target=CODEX,
                    workspace=WORKSPACE,
                    state=SessionState.IDLE,
                    progress=ProgressObservation.unreadable("the rollout could not be decoded"),
                ),
            )
        )
        reply = surface.ask(Action.BRIEF, target=CODEX_ADDRESS)

        assert reply.ok
        assert reply.data["session"]["newest"] == {"state": "unreadable", "text": None}
        assert reply.data["session"]["state"] == "unreadable"
        # The roster still holds what it last read: a standing account does not
        # lose a fact because one pass could not answer.
        assert (
            surface.ask(Action.STATUS).data["sessions"][0]["progress"]["availability"] == "readable"
        )

    def test_a_running_session_whose_progress_failed_now_stays_running(self) -> None:
        surface = Surface()
        surface.register()
        surface.agent.discovery = LaneDiscovery(
            rows=(
                SessionInspection(
                    target=CODEX,
                    workspace=WORKSPACE,
                    state=SessionState.RUNNING,
                    progress=ProgressObservation.unreadable("the daemon dropped the read"),
                ),
            )
        )

        reply = surface.ask(Action.BRIEF, target=CODEX_ADDRESS)

        assert reply.ok
        assert reply.data["session"]["state"] == "running"
        assert reply.data["session"]["newest"] == {"state": "unreadable", "text": None}

    def test_a_session_whose_progress_could_not_be_read_is_briefed_not_refused(self) -> None:
        """Where `brief` and `progress` part, and why.

        `history` exists to answer with a Session's own words and has nothing
        to say without them. A brief still has a state, a wait and a name, so an
        unreadable reading becomes a state the user is told about rather than a
        refusal that tells them nothing.
        """
        surface = Surface()
        surface.register()
        surface.agent.discovery = LaneDiscovery(
            rows=(
                SessionInspection(
                    target=CODEX,
                    workspace=WORKSPACE,
                    state=SessionState.IDLE,
                    waiting_for=WaitingFor(kind=WaitingKind.UNKNOWN, caught_up=False),
                    progress=ProgressObservation.unreadable("the rollout could not be decoded"),
                ),
            )
        )

        brief = surface.ask(Action.BRIEF, target=CODEX_ADDRESS)
        history = surface.ask(Action.HISTORY, target=CODEX_ADDRESS)

        assert brief.ok
        assert brief.data["session"]["state"] == "unreadable"
        assert history.error is not None

    def test_a_newest_message_too_large_for_the_line_is_named_rather_than_sliced(self) -> None:
        """ADR 0016 at the wire: the brief still answers, and says what it dropped.

        `newest` travels twice — as a field and inside `text` — so a message that
        fits one publication can still overflow this one. Slicing it would hand
        the user half a sentence; refusing would hand them nothing at all, when
        the state and the decision they act on both still fit.
        """
        surface = Surface(max_bytes=2_048)
        surface.register()
        surface.agent.discovery = LaneDiscovery(
            rows=(
                SessionInspection(
                    target=CODEX,
                    workspace=WORKSPACE,
                    state=SessionState.WAITING,
                    waiting_for=WaitingFor(kind=WaitingKind.QUESTION, prompt="Which base?"),
                    progress=ProgressObservation.readable(
                        has_history=True,
                        recent=(
                            ProgressEntry(ordinal=0, role=ProgressRole.ASSISTANT, text="x" * 4_000),
                        ),
                        read_at=READ_AT,
                    ),
                ),
            )
        )

        reply = surface.ask(Action.BRIEF, target=CODEX_ADDRESS)

        assert reply.ok
        assert reply.data["session"]["newest"] == {"state": "oversize", "text": None}
        assert reply.data["session"]["decision"]["prompt"] == "Which base?"
        assert len(wire(reply)) <= 2_048

    def test_a_malformed_address_is_refused_rather_than_read_as_no_address(self) -> None:
        """Absent and unusable are two answers, and only one is the whole roster."""
        surface = Surface()
        surface.register()

        reply = surface.ask(Action.BRIEF, target={"agent": "codex"})

        assert reply.error is not None
        assert reply.error.code is ErrorCode.INVALID_PAYLOAD

    def test_the_retired_roster_verb_is_an_unknown_action(self) -> None:
        reply = asyncio.run(surface_asking_raw("sessions"))

        assert reply.error is not None
        assert reply.error.code is ErrorCode.UNKNOWN_ACTION


async def surface_asking_raw(action: str) -> Reply:
    """One line naming an action, read the way the socket reads it."""
    try:
        request = Request.of({"action": action})
    except MalformedRequest as refused:
        return Reply.refused(None, refused.code, str(refused))
    return await Surface().plane.handle(request)


class TestTheFocusSession:
    """One pointer, moved by the user replying and by nothing else (#165 Q2)."""

    def test_relaying_into_a_session_makes_it_the_focus(self) -> None:
        surface = Surface()
        surface.register()
        surface.open_window()

        surface.ask(Action.RELAY, target=CODEX_ADDRESS, text="carry on")

        assert surface.state.sessions.focus == CODEX

    def test_answering_a_permission_makes_it_the_focus(self) -> None:
        surface = Surface()
        surface.register()
        surface.dialog_on_screen()

        surface.ask(Action.APPROVE, approval_id="a1", verdict="allow")

        assert surface.state.sessions.focus == CODEX

    def test_a_verdict_that_found_nothing_moves_nothing(self) -> None:
        surface = Surface()
        surface.register()

        surface.ask(Action.APPROVE, approval_id="a1", verdict="allow")

        assert surface.state.sessions.focus is None

    def test_briefing_a_session_never_makes_it_the_focus(self) -> None:
        """Asking about a Session is not replying to one."""
        surface = Surface()
        surface.register()
        surface.agent.discovery = LaneDiscovery(
            rows=(SessionInspection(target=CODEX, workspace=WORKSPACE, state=SessionState.IDLE),)
        )

        surface.ask(Action.BRIEF)
        surface.ask(Action.BRIEF, target=CODEX_ADDRESS)

        assert surface.state.sessions.focus is None

    def test_the_focus_clears_when_that_session_ends(self) -> None:
        surface = Surface()
        surface.register()
        surface.open_window()
        surface.ask(Action.RELAY, target=CODEX_ADDRESS, text="carry on")

        surface.state.sessions.observe(AgentKind.CODEX, LaneDiscovery(), now=1.0)

        assert surface.state.sessions.focus is None

    def test_the_focus_session_is_spoken_first_and_the_counts_are_the_others(self) -> None:
        surface = Surface()
        surface.register()
        surface.register(SECOND_CODEX)
        surface.open_window()
        surface.ask(Action.RELAY, target=CODEX_ADDRESS, text="carry on")

        roster = surface.ask(Action.BRIEF).data["roster"]

        assert roster["focus"] == CODEX_ADDRESS
        assert roster["rows"][0]["focus"] is True
        assert sum(roster["counts"].values()) == 1

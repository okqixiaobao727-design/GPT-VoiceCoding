"""Bridge Core assembled, driven end to end by events, against fakes only.

ADR 0001 principle 4 in one file: a fake call, fake agents and a fake channel,
no network and no audio, and every one of the five pipelines reachable from an
event an adapter could actually raise.

The cases here are the ones the pipelines issue was asked to prove, and each is
a defect that has happened rather than one that might: a stop arriving while the
system owns a call, a notice with nowhere to go, an approval nobody answered, a
Relay whose Session never opened its window, and Duty going off in the middle of
all of it while the control plane keeps answering.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fakes import FakeCall
from gpt_voicecoding.core.bridge import (
    NO_CONTROL_SURFACE,
    NO_DELEGATE_HANDLER,
    stop_notice_for,
)
from gpt_voicecoding.core.errors import VoiceInstructionsMissing
from gpt_voicecoding.core.lifecycle import Lifecycle
from gpt_voicecoding.core.router import Classification
from gpt_voicecoding.core.sessions import Session
from gpt_voicecoding.core.switches import SwitchName
from gpt_voicecoding.seams.agent import (
    ApprovalRequest,
    ApprovalVerdict,
    AwaitingApproval,
    ChildClassification,
    ChildKind,
    LaneDiscovery,
    Option,
    ProgressEntry,
    ProgressObservation,
    ProgressOmission,
    ProgressRole,
    ReplyWindow,
    ReplyWindowChanged,
    SessionEnded,
    SessionInspection,
    SessionLifecycle,
    SessionState,
    SessionStopped,
    WaitingFor,
    WaitingKind,
)
from gpt_voicecoding.seams.call import CallDropped, CallStarted, CallState, UserSpeech
from gpt_voicecoding.seams.companion_channel import InboundText
from gpt_voicecoding.seams.delivery import Delivery
from gpt_voicecoding.seams.identity import AgentKind, SessionTarget
from hub import CLAUDE, CODEX, TEN_MINUTES, Hub


class EndRefusingCall(FakeCall):
    async def end_call(self):
        self.calls_ended += 1
        raise RuntimeError("the call transport refused to end")


class TestTheStopNoticePipelineEndToEnd:
    def test_a_stops_progress_is_announced_and_folded_into_the_roster(self) -> None:
        hub = Hub()
        observed = ProgressObservation.readable(
            has_history=True,
            recent=(
                ProgressEntry(
                    role=ProgressRole.ASSISTANT,
                    text="The registry status is the root cause.",
                ),
            ),
            read_at=datetime(2026, 8, 31, 2, 44, 39, tzinfo=UTC),
        )

        hub.emit(SessionStopped(target=CODEX, progress=observed))

        assert "The registry status is the root cause." in hub.call.spoken[0]
        assert hub.core.status().sessions[0].progress == observed

    def test_a_failed_stop_read_does_not_replace_a_readable_roster_observation(self) -> None:
        hub = Hub()
        observed = ProgressObservation.readable(
            has_history=True,
            recent=(
                ProgressEntry(
                    role=ProgressRole.ASSISTANT,
                    text="The readable observation remains authoritative.",
                ),
            ),
            read_at=datetime(2026, 8, 31, 2, 44, 39, tzinfo=UTC),
        )
        hub.emit(SessionStopped(target=CODEX, progress=observed))

        for unavailable in (
            ProgressObservation(),
            ProgressObservation.unreadable("the daemon dropped the read"),
        ):
            hub.emit(
                SessionStopped(
                    target=CODEX,
                    progress=unavailable,
                )
            )
            assert hub.core.status().sessions[0].progress == observed

    def test_a_stop_notice_speaks_the_newest_assistant_entry(self) -> None:
        notice = stop_notice_for(
            None,
            CODEX,
            progress=ProgressObservation.readable(
                has_history=True,
                recent=(
                    ProgressEntry(
                        role=ProgressRole.ASSISTANT,
                        text="The diagnosis points to the session registry.",
                    ),
                ),
                read_at=datetime(2026, 8, 31, 2, 44, 39, tzinfo=UTC),
            ),
        )

        assert "The diagnosis points to the session registry." in notice

    def test_a_stop_with_no_history_says_nothing_was_said_yet(self) -> None:
        notice = stop_notice_for(
            None,
            CODEX,
            progress=ProgressObservation.readable(
                has_history=False,
                read_at=datetime(2026, 8, 31, 2, 44, 39, tzinfo=UTC),
            ),
        )

        assert "nothing said yet" in notice
        assert "it said:" not in notice

    def test_an_oversize_newest_entry_is_reported_as_existing_but_not_carried(self) -> None:
        notice = stop_notice_for(
            None,
            CODEX,
            progress=ProgressObservation.readable(
                has_history=True,
                omission=ProgressOmission.NEWEST_OVERSIZE,
                read_at=datetime(2026, 8, 31, 2, 44, 39, tzinfo=UTC),
            ),
        )

        assert "history exists" in notice
        assert "newest entry is too large to carry" in notice
        assert "nothing said yet" not in notice

    def test_a_question_notice_carries_each_options_description(self) -> None:
        notice = stop_notice_for(
            None,
            CODEX,
            WaitingFor(
                kind=WaitingKind.QUESTION,
                prompt="Which base?",
                options=(
                    Option(
                        text="main",
                        description="Merge into the default branch",
                    ),
                ),
            ),
            answerable_here=True,
        )

        assert "main" in notice
        assert "Merge into the default branch" in notice

    def test_an_answerable_question_tells_the_user_to_reply_here(self) -> None:
        notice = stop_notice_for(
            None,
            CODEX,
            WaitingFor(
                kind=WaitingKind.QUESTION,
                prompt="Which base?",
                options=(Option("main"), Option("feature")),
            ),
            answerable_here=True,
        )

        assert "reply with your answer" in notice
        assert "answer it in the terminal" not in notice

    def test_several_questions_tell_the_user_to_answer_all_in_one_reply(self) -> None:
        notice = stop_notice_for(
            None,
            CODEX,
            WaitingFor(
                kind=WaitingKind.QUESTION,
                prompt="Tabs or spaces?\nWhich base?",
                options=(Option("tabs"), Option("spaces"), Option("main")),
            ),
            answerable_here=True,
        )

        assert "answer all of them in one reply" in notice

    def test_a_stop_notice_never_relays_words_back_into_a_session(self) -> None:
        """The system tells the user about a stop; it does not address an agent."""
        hub = Hub()

        hub.emit(SessionStopped(target=CODEX))

        assert hub.agent.calls == []

    def test_a_stopped_session_is_announced_by_its_name(self) -> None:
        hub = Hub()

        hub.emit(SessionStopped(target=CODEX))

        assert "port the log" in hub.call.spoken[0]

    def test_a_stop_while_the_system_owns_a_call_opens_no_second_call(self) -> None:
        """The reference implementation's loop, made unreachable."""
        hub = Hub()
        started = hub.toggle()
        assert started.call_id is not None
        hub.emit(CallStarted(call_id=started.call_id))

        hub.emit(SessionStopped(target=CODEX))

        assert hub.call.calls_started == 1
        assert hub.call.spoken

    def test_with_no_call_and_message_on_the_channel_send_is_actually_invoked(self) -> None:
        hub = Hub(voice=False)

        hub.emit(SessionStopped(target=CODEX))

        assert hub.channel.sent
        assert hub.call.calls_started == 0

    def test_an_unreachable_channel_drops_the_attempt_instead_of_replaying_it(
        self,
    ) -> None:
        hub = Hub(voice=False)
        hub.channel.outcome = Delivery.FAILED
        hub.channel.reason = "the chat is unreachable"

        hub.emit(SessionStopped(target=CODEX))
        hub.channel.outcome = Delivery.DELIVERED
        asyncio.run(hub.core.outlets_changed())
        asyncio.run(hub.core.discover())

        assert len(hub.channel.sent) == 1
        assert hub.state.relays.pending() == ()

    def test_a_current_question_surfaces_when_duty_comes_back_on(self) -> None:
        hub = Hub(duty=False)
        hub.emit(SessionStopped(target=CODEX))
        hub.agent.discovery = LaneDiscovery(
            rows=(
                SessionInspection(
                    target=CODEX,
                    workspace=Path("/tmp/workspace"),
                    state=SessionState.WAITING,
                    waiting_for=WaitingFor(
                        kind=WaitingKind.QUESTION,
                        prompt="Which base?",
                    ),
                ),
            )
        )

        hub.flip(SwitchName.DUTY, True)

        assert hub.agent.inspections == []
        assert hub.call.spoken == []

        asyncio.run(hub.core.discover())

        assert [notice for notice in hub.call.spoken if "Which base?" in notice]
        assert hub.state.relays.pending() == ()

    def test_message_off_and_voice_on_never_pushes_text(self) -> None:
        hub = Hub(message=False)

        hub.emit(SessionStopped(target=CODEX))

        assert hub.channel.sent == []


class TestTheApprovalPipelineEndToEnd:
    def test_tick_sweeps_question_holds_with_the_configured_budget_and_closes_once(
        self,
    ) -> None:
        hub = Hub(voice=False, approval_budget_seconds=17.0)
        question = WaitingFor(
            kind=WaitingKind.QUESTION,
            prompt="Which base?",
            approval_id="p-1",
        )
        hub.agent.question_releases.append((CODEX, question))

        hub.tick()
        first = tuple(hub.channel.sent)
        hub.tick()

        assert hub.agent.question_budgets == [17.0, 17.0, 17.0, 17.0]
        assert len(first) == 1
        assert hub.channel.sent == list(first)
        assert "answer it in the terminal" in first[0]

    def test_an_answer_after_budget_release_is_refused_at_the_public_relay(self) -> None:
        hub = Hub(voice=False, approval_budget_seconds=17.0)
        question = WaitingFor(kind=WaitingKind.QUESTION, prompt="Which base?")
        hub.agent.discovery = LaneDiscovery(
            rows=(
                SessionInspection(
                    target=CODEX,
                    workspace=Path("/tmp/workspace"),
                    state=SessionState.WAITING,
                    waiting_for=question,
                ),
            )
        )
        hub.agent.answerable_questions.add(CODEX)
        asyncio.run(hub.core.discover())
        hub.agent.question_releases.append((CODEX, question))

        hub.tick()
        outcome = asyncio.run(hub.core.relay(CODEX, "main"))

        assert outcome.state is Lifecycle.REPORTED_FAILED
        assert outcome.report
        assert hub.agent.calls == []
        assert hub.state.relays.pending() == ()

    def test_an_awaiting_approval_event_announces_on_every_outlet(self) -> None:
        hub = Hub()

        hub.emit(AwaitingApproval(request=ApprovalRequest("a1", CODEX, "Bash")))

        assert hub.call.spoken
        assert hub.channel.sent

    def test_an_unanswered_approval_expires_to_ask_and_never_to_deny(self) -> None:
        hub = Hub()
        hub.emit(AwaitingApproval(request=ApprovalRequest("a1", CODEX, "Bash")))

        hub.now += TEN_MINUTES
        hub.tick()

        assert [call.verdict for call in hub.agent.calls] == [ApprovalVerdict.ASK]

    def test_a_verdict_after_expiry_is_discarded_safely(self) -> None:
        hub = Hub()
        hub.emit(AwaitingApproval(request=ApprovalRequest("a1", CODEX, "Bash")))
        hub.now += TEN_MINUTES
        hub.tick()

        late = asyncio.run(hub.core.approvals.answer("a1", ApprovalVerdict.ALLOW))

        assert late is None
        assert [call.verdict for call in hub.agent.calls] == [ApprovalVerdict.ASK]


class TestTheLiveCallSilenceCeiling:
    def test_tick_ends_an_owned_call_once_when_the_silence_threshold_arrives(self, caplog) -> None:
        caplog.set_level("INFO", logger="gpt_voicecoding.core.bridge")
        hub = Hub(silence_end_seconds=60.0)
        started = hub.toggle()
        assert started.call_id is not None
        hub.emit(CallStarted(call_id=started.call_id))

        hub.now += 60.0
        hub.tick()
        hub.tick()

        assert hub.call.calls_ended == 1
        assert hub.core.interlock.owns_call() is False
        assert hub.call.spoken == []
        assert hub.channel.sent == []
        assert "ended the Live Call after 60 seconds without call activity" in [
            record.getMessage() for record in caplog.records
        ]

    def test_user_speech_restarts_the_owned_calls_silence_window(self) -> None:
        hub = Hub(silence_end_seconds=60.0)
        started = hub.toggle()
        assert started.call_id is not None
        hub.emit(CallStarted(call_id=started.call_id))

        hub.now += 50.0
        hub.emit(UserSpeech(text="still here"))
        hub.now += 59.9
        hub.tick()
        assert hub.call.calls_ended == 0

        hub.now += 0.1
        hub.tick()
        assert hub.call.calls_ended == 1

    def test_a_notice_spoken_into_the_call_restarts_its_silence_window(self) -> None:
        hub = Hub(silence_end_seconds=60.0)
        started = hub.toggle()
        assert started.call_id is not None
        hub.emit(CallStarted(call_id=started.call_id))

        hub.now += 50.0
        hub.emit(SessionStopped(target=CODEX))
        hub.now += 59.9
        hub.tick()

        assert hub.call.spoken
        assert hub.call.calls_ended == 0

    def test_the_silence_ceiling_is_not_gated_by_any_switch(self) -> None:
        hub = Hub(
            duty=False,
            voice=False,
            message=False,
            silence_end_seconds=60.0,
        )
        started = hub.toggle()
        assert started.call_id is not None
        hub.emit(CallStarted(call_id=started.call_id))

        hub.now += 60.0
        hub.tick()

        assert hub.call.calls_ended == 1

    def test_a_call_that_ended_by_itself_is_not_ended_again(self) -> None:
        hub = Hub(silence_end_seconds=60.0)
        started = hub.toggle()
        assert started.call_id is not None
        hub.emit(CallStarted(call_id=started.call_id))
        hub.emit(CallDropped(call_id=started.call_id, detail="the far side left"))

        hub.now += 60.0
        hub.tick()

        assert hub.call.calls_ended == 0

    def test_an_end_failure_is_logged_and_not_retried_for_that_call(self, caplog) -> None:
        caplog.set_level("ERROR", logger="gpt_voicecoding.core.bridge")
        call = EndRefusingCall()
        hub = Hub(call=call, silence_end_seconds=60.0)
        started = hub.toggle()
        assert started.call_id is not None
        hub.emit(CallStarted(call_id=started.call_id))

        hub.now += 60.0
        hub.tick()
        hub.emit(UserSpeech(text="the failed call is still here"))
        hub.now += 60.0
        hub.tick()
        hub.emit(CallStarted(call_id="call-2"))
        hub.now += 60.0
        hub.tick()

        assert call.calls_ended == 2
        assert [record.getMessage() for record in caplog.records] == [
            "could not end the silent Live Call; not trying again until the call changes",
            "could not end the silent Live Call; not trying again until the call changes",
        ]


class TestAChildProcessIsNeverAnnounced:
    """Seen, never spoken to, and never spoken *about* (#79, `CONTEXT.md`).

    Both halves are in Bridge Core rather than in a lane, because the rule is
    the hub's: **a lane that raises a stop for a child is not wrong**, it is
    reporting what it saw. A Codex subagent thread really does transition out of
    `active`, and the Codex adapter really does watch every thread the daemon
    holds. What must not happen is the hub turning that into a notice the user
    is asked to act on — a Stop Notice names a Session the user can answer, and
    the answer to a child would be refused by `resolve` a moment later.

    **The refusal is asked of `resolve`, not re-derived**, so there is one place
    that decides what a Child Process is and this one obeys it. Unknown is
    deliberately not treated as child: a Stop can arrive for a Session the
    roster has not observed yet, and silence about it would be the notice the
    engine exists to send going missing.

    **Legacy is the shape being adapted, not ported**
    (`legacy@1d32845:bridge/__main__.py:876-899`, `bridge/hook.py:1-19,68-75`):
    it suppressed the *registration*, so a child had no row to raise anything
    from. v1.0 lists the row and suppresses the announcement instead.
    """

    def spawned(self, hub: Hub) -> SessionTarget:
        """One Child Process of the roster's Session, in the roster (#79)."""
        child = SessionTarget(agent=AgentKind.CODEX, session_id="a891a18f447827175")
        hub.state.sessions.register(
            Session(
                target=child,
                workspace=Path("/tmp/workspace"),
                first_seen=0.0,
                state=SessionState.RUNNING,
                child=ChildClassification(kind=ChildKind.CHILD, parent=CODEX),
            )
        )
        return child

    def test_a_stop_on_a_child_reaches_no_outlet(self) -> None:
        hub = Hub()

        hub.emit(SessionStopped(target=self.spawned(hub)))

        assert hub.channel.sent == []
        assert hub.call.spoken == []

    def test_a_stop_on_a_child_says_so_rather_than_going_quiet(self, caplog) -> None:
        """A notice that was never sent is otherwise a notice that failed."""
        hub = Hub()
        with caplog.at_level("INFO"):
            hub.emit(SessionStopped(target=self.spawned(hub)))

        assert any("Child Process" in record.message for record in caplog.records)

    def test_its_parents_stop_is_announced_as_it_always_was(self) -> None:
        """The rule is about the child. A parent working is the product working."""
        hub = Hub()
        self.spawned(hub)

        hub.emit(SessionStopped(target=CODEX))

        assert "port the log" in hub.call.spoken[0]

    def test_a_stop_on_a_session_the_roster_has_not_seen_is_still_announced(self) -> None:
        """Unknown is not child, and the asymmetry is the point.

        Discovery runs on a cadence, so a Session can stop before the roster has
        a row for it. Refusing to announce that would lose the notice entirely,
        while announcing a child costs one message about something that is about
        to be refused anyway.
        """
        hub = Hub()

        hub.emit(SessionStopped(target=SessionTarget(agent=AgentKind.CODEX, session_id="new")))

        assert hub.call.spoken

    def test_a_permission_a_child_raises_is_not_escalated(self) -> None:
        """A Codex subagent thread can raise a real `requestApproval`.

        Accepted for v1.0 (advisor, 2026-08-27): "never spoken to" includes
        never answered, so that dialog is the keyboard's. The alternative is an
        Approval Relay carrying the user's authority into a Session `resolve`
        refuses to address.
        """
        hub = Hub()
        child = self.spawned(hub)

        hub.emit(AwaitingApproval(request=ApprovalRequest("a1", child, "Bash")))

        assert hub.channel.sent == []
        assert hub.core.approvals.pending() == ()

    def test_its_parents_permission_is_escalated_as_it_always_was(self) -> None:
        hub = Hub()
        self.spawned(hub)

        hub.emit(AwaitingApproval(request=ApprovalRequest("a1", CODEX, "Bash")))

        assert hub.channel.sent
        assert len(hub.core.approvals.pending()) == 1


class TestTheRelayPipelineEndToEnd:
    def test_an_answerable_question_opens_the_reply_window_without_changing_waiting_state(
        self,
    ) -> None:
        hub = Hub(voice=False)
        hub.agent.discovery = LaneDiscovery(
            rows=(
                SessionInspection(
                    target=CODEX,
                    workspace=Path("/tmp/workspace"),
                    state=SessionState.WAITING,
                    waiting_for=WaitingFor(
                        kind=WaitingKind.QUESTION,
                        prompt="Which base?",
                        approval_id="p-1",
                    ),
                ),
            )
        )
        hub.agent.answerable_questions.add(CODEX)
        asyncio.run(hub.core.discover())

        outcome = asyncio.run(hub.core.relay(CODEX, "main"))

        assert outcome.outcome is Delivery.DELIVERED
        assert [call.text for call in hub.agent.calls] == ["main"]
        assert hub.core.status().reply_windows[CODEX] is ReplyWindow.OPEN
        assert hub.state.sessions.resolve(CODEX).state is SessionState.WAITING

    def test_the_question_open_event_delivers_queued_words_without_changing_waiting_state(
        self,
    ) -> None:
        hub = Hub(voice=False)
        hub.emit(InboundText(text="main"))
        hub.agent.discovery = LaneDiscovery(
            rows=(
                SessionInspection(
                    target=CODEX,
                    workspace=Path("/tmp/workspace"),
                    state=SessionState.WAITING,
                    waiting_for=WaitingFor(
                        kind=WaitingKind.QUESTION,
                        prompt="Which base?",
                    ),
                ),
            )
        )
        asyncio.run(hub.core.discover())
        hub.agent.answerable_questions.add(CODEX)

        hub.emit(ReplyWindowChanged(target=CODEX, window=ReplyWindow.OPEN))

        assert [call.text for call in hub.agent.calls] == ["main"]
        assert hub.state.sessions.resolve(CODEX).state is SessionState.WAITING

    def test_a_question_open_event_does_not_invent_idle_before_discovery_catches_up(
        self,
    ) -> None:
        hub = Hub(voice=False)
        hub.emit(InboundText(text="main"))
        hub.agent.answerable_questions.add(CODEX)

        hub.emit(ReplyWindowChanged(target=CODEX, window=ReplyWindow.OPEN))

        assert [call.text for call in hub.agent.calls] == ["main"]
        assert hub.state.sessions.resolve(CODEX).state is SessionState.RUNNING

    def test_words_for_a_busy_session_wait_and_are_confirmed_once(self) -> None:
        hub = Hub()

        hub.emit(InboundText(text="ship it"))

        assert hub.agent.calls == []
        assert hub.channel.sent == ["got it, it'll go when this turn ends"]

    def test_the_open_window_delivers_them_without_announcing_again(self) -> None:
        hub = Hub()
        hub.emit(InboundText(text="ship it"))
        confirmations = len(hub.channel.sent)

        hub.emit(ReplyWindowChanged(target=CODEX, window=ReplyWindow.OPEN))

        assert [call.text for call in hub.agent.calls] == ["ship it"]
        assert len(hub.channel.sent) == confirmations

    def test_ten_minutes_of_waiting_becomes_one_reported_failure(self) -> None:
        hub = Hub()
        hub.emit(InboundText(text="ship it"))

        hub.now += TEN_MINUTES
        hub.tick()

        assert hub.state.relays.pending() == ()
        assert any("never reached the session" in spoken for spoken in hub.call.spoken)

    def test_a_session_that_ends_reports_the_words_still_waiting_for_it(self) -> None:
        hub = Hub()
        hub.emit(InboundText(text="ship it"))

        hub.emit(SessionEnded(target=CODEX))

        assert hub.state.relays.pending() == ()
        assert hub.state.sessions.all()[0].lifecycle is SessionLifecycle.ENDED


class TestTheInboundRouterEndToEnd:
    def test_an_inbound_command_records_its_classification(self, caplog) -> None:
        """Ticket #48's coordinator ruling pins this exact log format."""
        caplog.set_level("INFO", logger="gpt_voicecoding.core.bridge")
        hub = Hub()

        hub.emit(InboundText(text="/status"))

        assert [record.getMessage() for record in caplog.records] == [
            "handled inbound Companion Channel message kind=control"
        ]

    def test_an_inbound_answer_relay_records_its_target(self, caplog) -> None:
        """The ruling appends the SessionTarget only for an Answer Relay."""
        caplog.set_level("INFO", logger="gpt_voicecoding.core.bridge")
        hub = Hub()

        hub.emit(InboundText(text="ship it"))

        assert [record.getMessage() for record in caplog.records] == [
            f"handled inbound Companion Channel message kind=answer_relay target={CODEX}"
        ]

    def test_a_command_reaches_the_wired_control_surface(self) -> None:
        seen: list[Classification] = []

        async def control(found: Classification) -> str:
            seen.append(found)
            return "duty is on"

        hub = Hub(control=control)

        hub.emit(InboundText(text="/status"))

        assert [found.command for found in seen] == ["status"]
        assert hub.channel.sent == ["duty is on"]

    def test_a_command_with_nothing_wired_says_so_rather_than_guessing(self) -> None:
        hub = Hub()

        hub.emit(InboundText(text="/status"))

        assert hub.channel.sent == [NO_CONTROL_SURFACE]

    def test_a_delegation_reaches_the_wired_handler(self) -> None:
        async def delegate(found: Classification) -> str:
            return f"about {found.text}: it says so"

        hub = Hub(delegate=delegate)

        hub.emit(InboundText(text=">what does ADR 0002 say"))

        assert hub.channel.sent == ["about what does ADR 0002 say: it says so"]

    def test_a_delegation_with_nothing_wired_says_so(self) -> None:
        hub = Hub()

        hub.emit(InboundText(text=">summarise the diff"))

        assert hub.channel.sent == [NO_DELEGATE_HANDLER]

    def test_unknown_input_fails_closed_with_an_honest_reply(self) -> None:
        hub = Hub(sessions=((CODEX, "port the log"), (CLAUDE, "build the shell")))

        hub.emit(InboundText(text="ship it"))

        assert hub.agent.calls == []
        (reply,) = hub.channel.sent
        assert "port the log" in reply
        assert "build the shell" in reply

    def test_the_reply_goes_out_with_every_switch_off(self) -> None:
        """A reply is not a push. ADR 0002 covers the surface it came in on."""
        hub = Hub(duty=False, voice=False, message=False)

        hub.emit(InboundText(text="/status"))

        assert hub.channel.sent == [NO_CONTROL_SURFACE]

    def test_words_for_a_session_that_ended_are_refused_not_queued(self) -> None:
        hub = Hub()
        hub.emit(SessionEnded(target=CODEX))

        hub.emit(InboundText(text="ship it"))

        assert hub.state.relays.pending() == ()
        assert hub.channel.sent


class TestTheOneCallInvariantEndToEnd:
    def test_the_live_toggle_opens_a_call_when_none_is_up(self) -> None:
        hub = Hub()

        snapshot = hub.toggle()

        assert snapshot.state is CallState.UP
        assert hub.core.interlock.owns_call() is True

    def test_a_live_toggle_that_opens_a_call_reconciles_current_stops(self) -> None:
        hub = Hub()
        hub.agent.discovery = LaneDiscovery(
            rows=(
                SessionInspection(
                    target=CODEX,
                    workspace=Path("/tmp/workspace"),
                    state=SessionState.WAITING,
                    waiting_for=WaitingFor(
                        kind=WaitingKind.QUESTION,
                        prompt="Which base?",
                    ),
                ),
            )
        )

        snapshot = hub.toggle()

        assert snapshot.is_up
        assert hub.agent.inspections == []
        assert [notice for notice in hub.call.spoken if "Which base?" in notice] == []

        asyncio.run(hub.core.discover())

        assert [notice for notice in hub.call.spoken if "Which base?" in notice]

    def test_a_call_started_event_reconciles_current_stops(self) -> None:
        hub = Hub(voice=False)
        hub.agent.discovery = LaneDiscovery(
            rows=(
                SessionInspection(
                    target=CODEX,
                    workspace=Path("/tmp/workspace"),
                    state=SessionState.WAITING,
                    waiting_for=WaitingFor(
                        kind=WaitingKind.QUESTION,
                        prompt="Which base?",
                    ),
                ),
            )
        )

        hub.emit(CallStarted(call_id="call-the-user-started"))

        assert hub.agent.inspections == []
        assert hub.channel.sent == []

        asyncio.run(hub.core.discover())

        assert [notice for notice in hub.channel.sent if "Which base?" in notice]

    def test_the_live_toggle_ends_the_call_the_system_owns(self) -> None:
        hub = Hub()
        hub.toggle()

        assert hub.toggle().state is CallState.DOWN
        assert hub.core.interlock.owns_call() is False

    def test_a_hub_that_generated_no_house_rules_opens_no_call(self) -> None:
        """The refusal comes from the interlock, which is the one door.

        The hub does not carry its own copy of this check, so what a caller sees
        here is the same refusal, worded once, that the escalation pipeline sees.
        """
        hub = Hub(instructions=False)

        with pytest.raises(VoiceInstructionsMissing):
            hub.toggle()

        assert hub.call.calls_started == 0

    def test_ending_a_call_never_needs_house_rules(self) -> None:
        """Opening is refusable; ending is not. A call that is up must be endable."""
        hub = Hub(instructions=False)
        hub.core.interlock.note_started("call-1")

        assert hub.toggle().state is CallState.DOWN

    def test_the_live_toggle_works_with_every_switch_off(self) -> None:
        """It is a control-plane action: the user touching the call, not the system."""
        hub = Hub(duty=False, voice=False, message=False)

        assert hub.toggle().state is CallState.UP
        assert hub.toggle().state is CallState.DOWN

    def test_the_toggle_ends_a_call_the_user_started_rather_than_opening_a_second(
        self,
    ) -> None:
        """One toggle, one voice surface — whoever brought the call up."""
        hub = Hub()
        started = hub.toggle()
        assert started.call_id is not None
        hub.emit(CallStarted(call_id=started.call_id))

        assert hub.toggle().state is CallState.DOWN
        assert hub.call.calls_started == 1

    def test_a_dropped_call_frees_escalation_to_open_the_next_one(self) -> None:
        hub = Hub()
        hub.emit(CallStarted(call_id="call-1"))
        hub.emit(CallDropped(call_id="call-1", detail="the network went away"))

        hub.emit(SessionStopped(target=CODEX))

        assert hub.call.calls_started == 1

    def test_a_call_release_reconciles_current_stops(self) -> None:
        hub = Hub(voice=False)
        hub.core.interlock.note_started("call-the-user-started")
        hub.agent.discovery = LaneDiscovery(
            rows=(
                SessionInspection(
                    target=CODEX,
                    workspace=Path("/tmp/workspace"),
                    state=SessionState.WAITING,
                    waiting_for=WaitingFor(
                        kind=WaitingKind.QUESTION,
                        prompt="Which base?",
                    ),
                ),
            )
        )

        hub.emit(CallDropped(call_id="call-the-user-started", detail="the network went away"))

        assert hub.agent.inspections == []
        assert hub.channel.sent == []

        asyncio.run(hub.core.discover())

        assert [notice for notice in hub.channel.sent if "Which base?" in notice]

    def test_a_stale_call_event_re_offers_nothing(self) -> None:
        """Only the interlock actually clearing is an outlet transition."""
        hub = Hub(voice=False)
        hub.core.interlock.note_started("current-call")
        hub.agent.discovery = LaneDiscovery(
            rows=(
                SessionInspection(
                    target=CODEX,
                    workspace=Path("/tmp/workspace"),
                    state=SessionState.WAITING,
                    waiting_for=WaitingFor(
                        kind=WaitingKind.QUESTION,
                        prompt="Which base?",
                    ),
                ),
            )
        )

        hub.emit(CallDropped(call_id="stale-old-call", detail="a late report"))

        assert hub.agent.inspections == []
        assert hub.channel.sent == []
        assert hub.core.interlock.call_id() == "current-call"

    def test_a_channel_that_came_back_reconciles_what_is_actionable_now(self) -> None:
        """The one outlet transition no event announces."""
        hub = Hub(voice=False)
        hub.agent.discovery = LaneDiscovery(
            rows=(
                SessionInspection(
                    target=CODEX,
                    workspace=Path("/tmp/workspace"),
                    state=SessionState.WAITING,
                    waiting_for=WaitingFor(
                        kind=WaitingKind.QUESTION,
                        prompt="Which base?",
                    ),
                ),
            )
        )

        asyncio.run(hub.core.outlets_changed())

        assert hub.agent.inspections == []
        assert hub.channel.sent == []

        asyncio.run(hub.core.discover())

        assert [notice for notice in hub.channel.sent if "Which base?" in notice]

    def test_switch_transitions_reconcile_current_waiting_state(self) -> None:
        hub = Hub(duty=False)
        hub.emit(SessionStopped(target=CODEX))
        hub.agent.discovery = LaneDiscovery(
            rows=(
                SessionInspection(
                    target=CODEX,
                    workspace=Path("/tmp/workspace"),
                    state=SessionState.WAITING,
                    waiting_for=WaitingFor(
                        kind=WaitingKind.QUESTION,
                        prompt="Which base?",
                    ),
                ),
            )
        )
        hub.flip(SwitchName.VOICE, True)
        hub.flip(SwitchName.MESSAGE, False)

        hub.flip(SwitchName.DUTY, True)

        assert hub.agent.inspections == []
        assert hub.call.spoken == []

        asyncio.run(hub.core.discover())

        assert [notice for notice in hub.call.spoken if "Which base?" in notice]
        assert hub.state.relays.pending() == ()


class TestSwitchAdjudicationEndToEnd:
    def test_duty_turning_on_announces_a_question_the_session_is_still_waiting_on(
        self,
    ) -> None:
        hub = Hub(duty=False, voice=False)
        hub.agent.discovery = LaneDiscovery(
            rows=(
                SessionInspection(
                    target=CODEX,
                    workspace=Path("/tmp/workspace"),
                    state=SessionState.WAITING,
                    waiting_for=WaitingFor(
                        kind=WaitingKind.QUESTION,
                        prompt="Which base?",
                    ),
                ),
            )
        )

        hub.flip(SwitchName.DUTY, True)

        assert hub.agent.inspections == []
        assert hub.channel.sent == []

        asyncio.run(hub.core.discover())

        assert hub.agent.inspections == []
        assert [notice for notice in hub.channel.sent if "Which base?" in notice]

        asyncio.run(hub.core.discover())

        assert len(hub.channel.sent) == 1

    def test_duty_turning_on_does_not_announce_a_session_that_moved_on(self) -> None:
        hub = Hub(duty=False, voice=False)
        hub.agent.discovery = LaneDiscovery(
            rows=(
                SessionInspection(
                    target=CODEX,
                    workspace=Path("/tmp/workspace"),
                    state=SessionState.IDLE,
                ),
            )
        )

        hub.flip(SwitchName.DUTY, True)

        assert hub.agent.inspections == []
        assert hub.channel.sent == []

        asyncio.run(hub.core.discover())

        assert hub.agent.inspections == []
        assert hub.channel.sent == []

    def test_a_transition_with_no_effective_outlet_leaves_nothing_owed(self) -> None:
        hub = Hub(duty=False, voice=False, message=False)
        hub.agent.discovery = LaneDiscovery(
            rows=(
                SessionInspection(
                    target=CODEX,
                    workspace=Path("/tmp/workspace"),
                    state=SessionState.WAITING,
                    waiting_for=WaitingFor(
                        kind=WaitingKind.QUESTION,
                        prompt="Which base?",
                    ),
                ),
            )
        )

        hub.flip(SwitchName.DUTY, True)

        assert hub.agent.inspections == []

        asyncio.run(hub.core.discover())
        hub.state.switches.flip(SwitchName.MESSAGE, True)
        asyncio.run(hub.core.discover())

        assert hub.channel.sent == []

    def test_reconciliation_never_announces_a_child_process(self) -> None:
        hub = Hub(duty=False, voice=False)
        child = SessionTarget(agent=AgentKind.CODEX, session_id="child")
        hub.state.sessions.register(
            Session(
                target=child,
                workspace=Path("/tmp/workspace"),
                first_seen=1.0,
                state=SessionState.WAITING,
                waiting_for=WaitingFor(
                    kind=WaitingKind.QUESTION,
                    prompt="May I act?",
                ),
                child=ChildClassification(kind=ChildKind.CHILD, parent=CODEX),
            )
        )
        hub.agent.discovery = LaneDiscovery(
            rows=(
                SessionInspection(
                    target=CODEX,
                    workspace=Path("/tmp/workspace"),
                    state=SessionState.IDLE,
                ),
                SessionInspection(
                    target=child,
                    workspace=Path("/tmp/workspace"),
                    state=SessionState.WAITING,
                    waiting_for=WaitingFor(
                        kind=WaitingKind.QUESTION,
                        prompt="May I act?",
                    ),
                    child=ChildClassification(kind=ChildKind.CHILD, parent=CODEX),
                ),
            )
        )

        hub.flip(SwitchName.DUTY, True)

        assert hub.agent.inspections == []
        assert hub.channel.sent == []

        asyncio.run(hub.core.discover())

        assert hub.agent.inspections == []
        assert hub.channel.sent == []

    def test_duty_turning_on_reoffers_the_same_pending_permission(self) -> None:
        hub = Hub(duty=False, voice=False)
        hub.emit(
            AwaitingApproval(
                request=ApprovalRequest(
                    approval_id="a1",
                    target=CODEX,
                    tool_name="Bash",
                    detail="push the branch",
                )
            )
        )
        (opened,) = hub.core.approvals.pending()
        pending = SessionInspection(
            target=CODEX,
            workspace=Path("/tmp/workspace"),
            state=SessionState.RUNNING,
            waiting_for=WaitingFor(
                kind=WaitingKind.PERMISSION,
                tool_name="Bash",
                detail="push the branch",
                approval_id="a1",
            ),
        )
        hub.agent.discovery = LaneDiscovery(rows=(pending,))

        hub.flip(SwitchName.DUTY, True)

        assert hub.channel.sent == []

        asyncio.run(hub.core.discover())

        assert hub.channel.sent == [
            "GPT-VoiceCoding · port the log is waiting for your permission to use Bash "
            "— push the branch"
        ]
        assert hub.core.approvals.pending() == (opened,)

        hub.flip(SwitchName.VOICE, True)
        asyncio.run(hub.core.discover())

        assert len(hub.channel.sent) == 1

        hub.agent.discovery = LaneDiscovery(
            rows=(
                SessionInspection(
                    target=CODEX,
                    workspace=Path("/tmp/workspace"),
                    state=SessionState.RUNNING,
                ),
            )
        )
        asyncio.run(hub.core.outlets_changed())
        asyncio.run(hub.core.discover())

        hub.agent.discovery = LaneDiscovery(rows=(pending,))
        asyncio.run(hub.core.outlets_changed())
        asyncio.run(hub.core.discover())

        assert len(hub.channel.sent) == 2
        assert hub.channel.sent[1] == hub.channel.sent[0]

    def test_an_expired_permission_is_reconciled_as_answerable_only_in_the_terminal(
        self,
    ) -> None:
        hub = Hub(duty=False, voice=False)
        hub.emit(
            AwaitingApproval(
                request=ApprovalRequest(
                    approval_id="a1",
                    target=CODEX,
                    tool_name="Bash",
                    detail="push the branch",
                )
            )
        )
        hub.now += TEN_MINUTES
        hub.tick()
        hub.agent.discovery = LaneDiscovery(
            rows=(
                SessionInspection(
                    target=CODEX,
                    workspace=Path("/tmp/workspace"),
                    state=SessionState.WAITING,
                    waiting_for=WaitingFor(
                        kind=WaitingKind.PERMISSION,
                        tool_name="Bash",
                        detail="push the branch",
                        approval_id="a1",
                    ),
                ),
            )
        )

        hub.flip(SwitchName.DUTY, True)

        assert hub.channel.sent == []

        asyncio.run(hub.core.discover())

        assert hub.core.approvals.pending() == ()
        assert len(hub.channel.sent) == 1
        assert "terminal" in hub.channel.sent[0]

    def test_consecutive_outlet_transitions_do_not_repeat_one_delivered_wait(self) -> None:
        hub = Hub(duty=False, voice=False)
        hub.agent.discovery = LaneDiscovery(
            rows=(
                SessionInspection(
                    target=CODEX,
                    workspace=Path("/tmp/workspace"),
                    state=SessionState.WAITING,
                    waiting_for=WaitingFor(
                        kind=WaitingKind.QUESTION,
                        prompt="Which base?",
                    ),
                ),
            )
        )

        hub.flip(SwitchName.DUTY, True)
        asyncio.run(hub.core.discover())
        hub.flip(SwitchName.VOICE, True)
        asyncio.run(hub.core.discover())

        assert hub.agent.inspections == []
        assert len(hub.channel.sent) == 1
        assert hub.call.calls_started == 0

    def test_a_wait_can_be_announced_again_after_the_session_moves_on(self) -> None:
        hub = Hub(duty=False, voice=False)
        hub.agent.discovery = LaneDiscovery(
            rows=(
                SessionInspection(
                    target=CODEX,
                    workspace=Path("/tmp/workspace"),
                    state=SessionState.WAITING,
                    waiting_for=WaitingFor(
                        kind=WaitingKind.QUESTION,
                        prompt="Which base?",
                    ),
                ),
            )
        )
        hub.flip(SwitchName.DUTY, True)
        asyncio.run(hub.core.discover())
        hub.agent.discovery = LaneDiscovery(
            rows=(
                SessionInspection(
                    target=CODEX,
                    workspace=Path("/tmp/workspace"),
                    state=SessionState.IDLE,
                ),
            )
        )
        asyncio.run(hub.core.outlets_changed())
        asyncio.run(hub.core.discover())
        hub.agent.discovery = LaneDiscovery(
            rows=(
                SessionInspection(
                    target=CODEX,
                    workspace=Path("/tmp/workspace"),
                    state=SessionState.WAITING,
                    waiting_for=WaitingFor(
                        kind=WaitingKind.QUESTION,
                        prompt="Which release?",
                    ),
                ),
            )
        )

        asyncio.run(hub.core.outlets_changed())
        asyncio.run(hub.core.discover())

        assert hub.agent.inspections == []
        assert len(hub.channel.sent) == 2
        assert "Which release?" in hub.channel.sent[-1]

    def test_discovery_clears_dedup_when_the_session_moves_on_between_transitions(
        self,
    ) -> None:
        hub = Hub(duty=False, voice=False)
        hub.agent.discovery = LaneDiscovery(
            rows=(
                SessionInspection(
                    target=CODEX,
                    workspace=Path("/tmp/workspace"),
                    state=SessionState.WAITING,
                    waiting_for=WaitingFor(
                        kind=WaitingKind.QUESTION,
                        prompt="Which base?",
                    ),
                ),
            )
        )
        hub.flip(SwitchName.DUTY, True)
        asyncio.run(hub.core.discover())
        hub.agent.discovery = LaneDiscovery(
            rows=(
                SessionInspection(
                    target=CODEX,
                    workspace=Path("/tmp/workspace"),
                    state=SessionState.IDLE,
                ),
            )
        )
        asyncio.run(hub.core.discover())
        hub.agent.discovery = LaneDiscovery(
            rows=(
                SessionInspection(
                    target=CODEX,
                    workspace=Path("/tmp/workspace"),
                    state=SessionState.WAITING,
                    waiting_for=WaitingFor(
                        kind=WaitingKind.QUESTION,
                        prompt="Which release?",
                    ),
                ),
            )
        )

        asyncio.run(hub.core.outlets_changed())
        asyncio.run(hub.core.discover())

        assert len(hub.channel.sent) == 2
        assert "Which release?" in hub.channel.sent[-1]

    def test_an_open_reply_window_clears_the_previous_wait(self) -> None:
        hub = Hub(voice=False)
        hub.emit(
            SessionStopped(
                target=CODEX,
                waiting_for=WaitingFor(
                    kind=WaitingKind.QUESTION,
                    prompt="Which base?",
                ),
            )
        )
        hub.emit(ReplyWindowChanged(target=CODEX, window=ReplyWindow.OPEN))

        hub.emit(
            SessionStopped(
                target=CODEX,
                waiting_for=WaitingFor(
                    kind=WaitingKind.QUESTION,
                    prompt="Which release?",
                ),
            )
        )

        assert len(hub.channel.sent) == 2
        assert "Which release?" in hub.channel.sent[-1]

    def test_duty_off_neither_speaks_nor_pushes_but_still_records_the_event(self) -> None:
        hub = Hub(duty=False)

        handled = hub.emit(SessionStopped(target=CODEX))

        assert handled == 1
        assert hub.call.spoken == []
        assert hub.channel.sent == []
        assert hub.state.relays.pending() == ()

    def test_the_control_plane_answers_with_every_switch_off(self) -> None:
        """ADR 0002 is absolute."""
        hub = Hub(duty=False, voice=False, message=False)

        status = hub.core.status()

        assert status.switches.as_mapping()[SwitchName.DUTY] is False
        assert len(status.sessions) == 1

    def test_switches_can_be_flipped_back_on_with_duty_off(self) -> None:
        hub = Hub(duty=False, voice=False, message=False)

        assert hub.flip(SwitchName.DUTY, True) is False
        assert hub.core.status().switches.as_mapping()[SwitchName.DUTY] is True

    def test_voice_off_and_message_on_is_a_working_system(self) -> None:
        hub = Hub(voice=False)

        hub.emit(SessionStopped(target=CODEX))
        hub.emit(InboundText(text="ship it"))

        assert hub.channel.sent
        assert hub.call.spoken == []

    def test_duty_flipping_off_mid_escalation_halts_but_keeps_answering(self) -> None:
        """A Stop Notice tries one outlet at a time, so permission is re-read."""
        hub = Hub()
        hub.call.reachable = False
        original = hub.call.ensure_call

        async def go_off_duty_while_connecting(instructions: str) -> object:
            hub.state.switches.flip(SwitchName.DUTY, False)
            return await original(instructions)

        hub.call.ensure_call = go_off_duty_while_connecting  # type: ignore[method-assign]

        handled = hub.emit(SessionStopped(target=CODEX))

        assert hub.channel.sent == []
        assert handled == 1
        assert hub.state.relays.pending() == ()
        assert hub.core.status().sessions

    def test_a_pending_approval_pushes_without_waiting_on_the_voice_attempt(self) -> None:
        """ "In parallel" is literal: a stalled call must not hold the text back."""
        released = asyncio.Event()
        hub = Hub()
        original = hub.call.speak

        async def speak_slowly(text: str, **kwargs: object) -> object:
            await released.wait()
            return await original(text, **kwargs)  # type: ignore[arg-type]

        hub.call.speak = speak_slowly  # type: ignore[method-assign]

        async def watch() -> list[str]:
            escalating = asyncio.ensure_future(
                hub.core.dispatch(AwaitingApproval(request=ApprovalRequest("a1", CODEX, "Bash")))
            )
            # Let the fan-out start both attempts before the call is unblocked.
            for _ in range(4):
                await asyncio.sleep(0)
            pushed = list(hub.channel.sent)
            released.set()
            await escalating
            return pushed

        assert asyncio.run(watch()) == [
            "GPT-VoiceCoding \u00b7 port the log is waiting for your permission to use Bash"
        ]


class TestEventsThatDecideNothing:
    def test_the_in_call_transcript_is_recorded_and_never_relayed(self) -> None:
        """Bridge Core never parses speech; the voice thread acts through commands."""
        hub = Hub()

        handled = hub.emit(UserSpeech(text="stop the log one"))

        assert handled == 1
        assert hub.agent.calls == []
        assert hub.channel.sent == []

    def test_events_arrive_once_and_in_order(self) -> None:
        hub = Hub(voice=False)

        hub.emit(
            SessionStopped(target=CODEX, waiting_for=WaitingFor(prompt="first", detail="first")),
            SessionStopped(target=CODEX, waiting_for=WaitingFor(prompt="second", detail="second")),
        )

        assert [sent.endswith("first") for sent in hub.channel.sent] == [True, False]
        assert hub.channel.sent[1].endswith("second")


class TestWhatDiscoveryCallsAnEnding:
    """The hub announces a death, and the announcement is not free.

    `discover` returning a target ends every Relay queued for it. So the hub may
    only report a Session that really went — and the two readings that look like
    a departure without being one are the ordinary Codex path, not an edge: a
    TUI has no thread id until its first turn (#73), and `/new` gives a running
    TUI a different one.
    """

    def seeing(self, *rows: SessionInspection) -> Hub:
        hub = Hub(sessions=())
        hub.agent.discovery = LaneDiscovery(rows=rows)
        asyncio.run(hub.core.discover())
        return hub

    def again(self, hub: Hub, *rows: SessionInspection) -> tuple[object, ...]:
        hub.agent.discovery = LaneDiscovery(rows=rows)
        return asyncio.run(hub.core.discover())

    def codex(self, *, session_id: str | None, pid: int) -> SessionInspection:
        return SessionInspection(
            target=SessionTarget(agent=AgentKind.CODEX, session_id=session_id, pid=pid),
            workspace=Path("/tmp/workspace"),
        )

    def test_a_session_that_stopped_being_seen_is_announced(self) -> None:
        hub = self.seeing(self.codex(session_id="abc", pid=10))

        assert [target.session_id for target in self.again(hub)] == ["abc"]

    def test_one_that_took_its_first_turn_and_gained_a_thread_id_is_not(self) -> None:
        hub = self.seeing(self.codex(session_id=None, pid=10))

        assert self.again(hub, self.codex(session_id="abc", pid=10)) == ()
        assert len(hub.state.sessions.live()) == 1

    def test_nor_is_one_whose_user_typed_new_into_it(self) -> None:
        hub = self.seeing(self.codex(session_id="abc", pid=10))

        assert self.again(hub, self.codex(session_id="xyz", pid=10)) == ()
        assert len(hub.state.sessions.live()) == 1

    def test_re_keying_drops_delivered_wait_memory_for_the_old_address(self) -> None:
        old = SessionTarget(agent=AgentKind.CODEX, session_id=None, pid=10)
        hub = Hub(duty=False, voice=False, sessions=())
        hub.agent.discovery = LaneDiscovery(
            rows=(
                SessionInspection(
                    target=old,
                    workspace=Path("/tmp/workspace"),
                    state=SessionState.WAITING,
                    waiting_for=WaitingFor(kind=WaitingKind.QUESTION, prompt="Which base?"),
                ),
            )
        )
        hub.flip(SwitchName.DUTY, True)
        asyncio.run(hub.core.discover())

        self.again(hub, self.codex(session_id="abc", pid=10))

        assert hub.core._delivered_waits == set()  # noqa: SLF001 - the bounded cache is the contract

    def test_a_lane_that_could_not_look_announces_nothing(self) -> None:
        hub = self.seeing(self.codex(session_id="abc", pid=10))

        hub.agent.discovery = LaneDiscovery(error="the shared daemon is not answering")

        assert asyncio.run(hub.core.discover()) == ()
        assert len(hub.state.sessions.live()) == 1


class TestWhatBecomesOfACompanionReplyThatDidNotLand:
    """P15 (#61 C2): one attempt, the grade written down, and never sent again.

    Ported from `legacy@1d32845:bridge/channel.py:11-13,75-86` and
    `legacy@1d32845:bridge/daemon.py:830-879`, whose rule is exactly this: an
    outbound attempt ends `sent` / `failed` / `indeterminate` / `suppressed`, the
    grade is logged, and an indeterminate one is **never** resent — "a duplicate
    notification costs the user more than a missing one". **Simplified storage:**
    legacy recorded the grade in a durable ledger
    (`legacy@1d32845:bridge/store.py:1517-1614`); this reply is a direct answer
    to text the user just sent, so it is graded, said out loud in the log, and
    forgotten.
    """

    def test_a_reply_that_failed_is_reported_rather_than_swallowed(self, caplog) -> None:
        caplog.set_level("INFO", logger="gpt_voicecoding.core.bridge")
        hub = Hub(channel_outcome=Delivery.FAILED, channel_reason="the bot token was rejected")

        hub.emit(InboundText(text="/status"))

        assert any("the bot token was rejected" in one.getMessage() for one in caplog.records)

    def test_the_grade_is_named_so_a_reader_can_tell_which_failure_it_was(self, caplog) -> None:
        caplog.set_level("INFO", logger="gpt_voicecoding.core.bridge")
        hub = Hub(channel_outcome=Delivery.UNKNOWN, channel_reason="1 of 3 parts reached the chat")

        hub.emit(InboundText(text="/status"))

        assert any(Delivery.UNKNOWN in one.getMessage() for one in caplog.records)

    def test_an_undelivered_reply_is_never_sent_again(self) -> None:
        hub = Hub(channel_outcome=Delivery.UNKNOWN, channel_reason="the write may have landed")

        hub.emit(InboundText(text="/status"))

        assert len(hub.channel.sent) == 1

    def test_an_undelivered_reply_is_not_retained_for_a_later_outlet(self) -> None:
        """A reply is not a notice. Nothing holds it, so nothing can replay it."""
        hub = Hub(channel_outcome=Delivery.FAILED, channel_reason="the chat id points nowhere")

        hub.emit(InboundText(text="/status"))

        assert hub.state.relays.pending() == ()

    def test_the_words_themselves_never_reach_the_log(self, caplog) -> None:
        """The reply carries the user's own business; the diagnostic carries none."""
        caplog.set_level("INFO", logger="gpt_voicecoding.core.bridge")
        hub = Hub(
            sessions=((CODEX, "port the log"),),
            channel_outcome=Delivery.FAILED,
            channel_reason="the chat id points nowhere",
        )

        hub.emit(InboundText(text="ship it"))

        said = " ".join(one.getMessage() for one in caplog.records)
        assert hub.channel.sent
        for reply in hub.channel.sent:
            assert reply not in said

    def test_a_delivered_reply_says_nothing_at_all(self, caplog) -> None:
        caplog.set_level("INFO", logger="gpt_voicecoding.core.bridge")
        hub = Hub()

        hub.emit(InboundText(text="/status"))

        assert [one.getMessage() for one in caplog.records] == [
            "handled inbound Companion Channel message kind=control"
        ]


class TestWhichStopIsAnnouncedWhenAPermissionRaisesTwo:
    """One dialog, two events, one announcement (#77, from #75's review).

    A Session entering `waiting` raises `SessionStopped`; `AskUserQuestion` also
    raises the `PermissionRequest` hook that produces `AwaitingApproval`. That
    pair opens a policy Bridge Core owns and the adapter does not: one question
    or permission would otherwise announce twice, once through each event.

    The `AwaitingApproval` notice wins wherever it exists, because it is the one
    with a budget, a fallback and a closing notice — it can actually be answered
    — and it already asks for `EVERY_OUTLET`, which is the wider reach. Where it
    does *not* exist, the Stop Notice is all there is, and it says so.

    Voice is off throughout, so every announcement lands on one surface and can
    simply be counted.
    """

    def permission(self, approval_id: str | None) -> WaitingFor:
        return WaitingFor(
            kind=WaitingKind.PERMISSION,
            tool_name="Bash",
            detail="push the branch",
            approval_id=approval_id,
        )

    def dialog(self) -> AwaitingApproval:
        return AwaitingApproval(
            request=ApprovalRequest(
                approval_id="a1",
                target=CODEX,
                tool_name="Bash",
                detail="push the branch",
            )
        )

    def test_a_dialog_the_approval_pipeline_holds_announces_once(self) -> None:
        hub = Hub(voice=False)

        hub.emit(
            self.dialog(),
            SessionStopped(target=CODEX, waiting_for=self.permission("a1")),
        )

        assert hub.channel.sent == [
            "GPT-VoiceCoding \u00b7 port the log is waiting for your permission to use Bash "
            "\u2014 push the branch"
        ]

    def test_the_suppressed_stop_is_written_down_rather_than_dropped_silently(self, caplog) -> None:
        caplog.set_level("INFO", logger="gpt_voicecoding.core.bridge")
        hub = Hub(voice=False)

        hub.emit(SessionStopped(target=CODEX, waiting_for=self.permission("a1")))

        assert any("a1" in one.getMessage() for one in caplog.records)
        assert hub.channel.sent == []

    def test_a_permission_nothing_is_holding_is_announced_after_all(self) -> None:
        """The roster saw `waiting`; no hook is parked. Nothing else will say it."""
        hub = Hub(voice=False)

        hub.emit(SessionStopped(target=CODEX, waiting_for=self.permission(None)))

        assert len(hub.channel.sent) == 1

    def test_that_announcement_sends_the_user_to_the_terminal(self) -> None:
        """A notice the user tries to answer from here and cannot is worse than none."""
        hub = Hub(voice=False)

        hub.emit(SessionStopped(target=CODEX, waiting_for=self.permission(None)))

        (notice,) = hub.channel.sent
        assert "terminal" in notice

    def test_a_question_sends_the_user_to_the_terminal_too(self) -> None:
        """Without #128's held-hook route there is no way to answer one here.

        The same reasoning as the permission with no handle: a notice that reads
        out a question and its options, and does not say where it is answered,
        invites the user to try a voice menu that does not exist.
        """
        hub = Hub(voice=False)

        hub.emit(
            SessionStopped(
                target=CODEX,
                waiting_for=WaitingFor(kind=WaitingKind.QUESTION, prompt="Which base?"),
            )
        )

        (notice,) = hub.channel.sent
        assert "terminal" in notice

    def test_a_question_is_always_announced(self) -> None:
        """It has no other route at all — that is the whole point of the new edge."""
        hub = Hub(voice=False)

        hub.emit(
            SessionStopped(
                target=CODEX,
                waiting_for=WaitingFor(kind=WaitingKind.QUESTION, prompt="Which base?"),
            )
        )

        assert [notice for notice in hub.channel.sent if "Which base?" in notice]

    def test_a_stop_that_needs_nobody_is_announced_as_before(self) -> None:
        hub = Hub(voice=False)

        hub.emit(SessionStopped(target=CODEX))

        assert len(hub.channel.sent) == 1


class TestEveryNoticeNamesTheSessionItIsAbout:
    """#109: on a machine bridging every Session, an unnamed notice is unanswerable.

    The Stop Notice always named its Session; the Approval Relay's announcement
    opened "a session is waiting…" and named only the tool. That is the notice
    carrying a budget and a `bridgectl approve` — the *most* answerable thing the
    product says, and the only one that did not say which Session it was about.
    It cost an acceptance run (`20260826T213402Z`), where a stranger's permission
    prompt was indistinguishable from the lane's own.

    Legacy: **ported** from `legacy@1d32845:bridge/host.py:213-235`, which
    rendered `Session: {session_label}` above "This session is waiting for
    permission."
    """

    #: The one target the Hub does not register, so `_known` answers None and the
    #: address floor is what the notice has to fall back on.
    STRANGER = SessionTarget(agent=AgentKind.CODEX, session_id="not-in-the-roster")

    def test_the_announcement_names_the_session_the_way_the_user_does(self) -> None:
        hub = Hub(voice=False)

        hub.emit(AwaitingApproval(request=ApprovalRequest("a1", CODEX, "Bash")))

        (announcement,) = hub.channel.sent
        assert announcement.startswith("GPT-VoiceCoding · port the log is waiting")

    def test_both_notices_call_one_session_the_same_thing(self) -> None:
        """One answer to "what is this called", or the user hears two Sessions."""
        hub = Hub(voice=False)

        hub.emit(AwaitingApproval(request=ApprovalRequest("a1", CODEX, "Bash")))
        hub.emit(SessionStopped(target=CODEX))

        announcement, stop_notice = hub.channel.sent
        called = "GPT-VoiceCoding · port the log"
        assert announcement.startswith(called) and stop_notice.startswith(called)

    def test_a_session_the_roster_does_not_hold_is_named_by_its_address(self) -> None:
        """The floor `spoken_name` itself falls back to — never "a session"."""
        hub = Hub(voice=False)

        hub.emit(AwaitingApproval(request=ApprovalRequest("a1", self.STRANGER, "Bash")))

        (announcement,) = hub.channel.sent
        assert announcement.startswith("codex not-in-the-roster is waiting")

    def test_the_detail_still_travels_after_the_name(self) -> None:
        hub = Hub(voice=False)

        hub.emit(
            AwaitingApproval(request=ApprovalRequest("a1", CODEX, "Bash", detail="push the branch"))
        )

        (announcement,) = hub.channel.sent
        assert announcement.endswith("your permission to use Bash — push the branch")

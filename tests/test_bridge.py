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
import re
from datetime import UTC, datetime
from pathlib import Path

import journey
import pytest

from claude_adapter_fake import ParkedApproval, claude_waiting_roster
from fakes import PROGRESS_CAPTURE, FakeCall, UnreachableFarSide, handed_over, spoken_words
from gpt_voicecoding.adapters.agent.claude import adapter as claude_adapter
from gpt_voicecoding.adapters.agent.claude.adapter import ClaudeAgentAdapter, SessionReport
from gpt_voicecoding.core import briefing
from gpt_voicecoding.core.bridge import (
    NO_CONTROL_SURFACE,
    NO_DELEGATE_HANDLER,
    VOICE_QUIET_LINE,
    VOICE_SPEAKING_LINE,
)
from gpt_voicecoding.core.briefing import BriefState
from gpt_voicecoding.core.call_keeper import USER_OPENED
from gpt_voicecoding.core.errors import CallInstructionsMissing, ChildSessionError
from gpt_voicecoding.core.lifecycle import Lifecycle
from gpt_voicecoding.core.relays import RelayReason
from gpt_voicecoding.core.router import Classification
from gpt_voicecoding.core.sessions import Session, UndeliveredRelay
from gpt_voicecoding.core.switches import SwitchName
from gpt_voicecoding.seams.agent import (
    ApprovalRequest,
    ApprovalVerdict,
    ChildClassification,
    ChildKind,
    LaneDiscovery,
    Option,
    ProgressEntry,
    ProgressObservation,
    ProgressOmission,
    ProgressRole,
    RelayReceipt,
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
from gpt_voicecoding.seams.call import (
    CallDropped,
    CallEnded,
    CallStarted,
    CallState,
    Cue,
    DialReason,
    SpokenBrief,
    SpokenRosterBrief,
    UserSpeech,
    VoiceSpeech,
)
from gpt_voicecoding.seams.companion_channel import InboundText
from gpt_voicecoding.seams.delivery import Delivery, DeliveryReceipt
from gpt_voicecoding.seams.identity import AgentKind, SessionTarget
from hub import CLAUDE, CODEX, TEN_MINUTES, Hub


def _field_lines(caplog) -> list[str]:  # noqa: ANN001
    """Every Bridge Core line about a change to a row's `undelivered` field (#226).

    Both lines say `its brief`, because both are about what the Session's brief
    will say from here on; nothing else the hub logs does.
    """
    said = (record.getMessage() for record in caplog.records)
    return [line for line in said if ", and its brief " in line]


class DeafCall(FakeCall):
    """A Call adapter whose speakers raise instead of playing (#186).

    The shipped adapter swallows its own playback failures, so this is a
    defective adapter rather than a missing device — and a defective adapter is
    exactly what the hub's own guard is for.
    """

    async def play_cue(self, cue: Cue) -> None:
        raise UnreachableFarSide(f"no output device for the {cue} cue")


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
                    ordinal=0,
                    role=ProgressRole.ASSISTANT,
                    text="The registry status is the root cause.",
                ),
            ),
            read_at=datetime(2026, 8, 31, 2, 44, 39, tzinfo=UTC),
        )

        hub.emit(SessionStopped(target=CODEX, progress=observed))

        # The words go out on the notice's own text — the log line and, where
        # Message is on, the Companion Channel. What the *call* is handed comes
        # off the roster, which the Stop has just moved out of `running`
        # (`test_the_session_a_stop_dialled_about_is_briefed_from_its_own_row`).
        assert hub.call.calls_started == 1
        assert "port the log" in handed_over(hub.call)
        assert hub.core.status().sessions[0].progress == observed

    def test_a_stop_that_read_a_question_puts_it_on_the_roster_row(self) -> None:
        """The Stop path reads the whole state and used to write back only half of it.

        `_session_stopped` reads a Session at the moment it stopped — through the
        adapter's overlay, which consults the dialog parked on the approval
        socket — and that reading is the freshest the engine ever holds. It went
        into the announcement and then to the announcement alone: `set_progress`
        folded the progress in and the `waiting_for` beside it was dropped. So
        `status` kept answering from whatever the last discovery pass had stored,
        which in run `20260902T065340Z` was a reading that had not caught up
        (#209), and a parked, answerable question read as `unknown` with a closed
        Reply Window until the next cadence.

        The Stop already waits until it is caught up (`window.py::_settle`, #150).
        Writing what it read back is what makes that wait reach `status`.
        """
        hub = Hub()
        question = WaitingFor(
            kind=WaitingKind.QUESTION,
            prompt="Which marker should be written?",
            options=(Option(text="ALPHA"), Option(text="DELTA")),
            approval_id="8664d1fc-dc6c-44eb-8800-fa8acf0e0c31",
        )

        # The window is derived with the lane's own answer to "do you still hold
        # that exact question" (`derive_reply_window`), so the fake holds it: the
        # roster kind alone never opens one.
        hub.agent.answerable_questions.add(CODEX)

        hub.emit(SessionStopped(target=CODEX, waiting_for=question))

        status = hub.core.status()
        assert status.sessions[0].waiting_for == question
        # The surface's answer, which is the roster payload's own `reply_window`
        # field: derived per read, with the lane's answer folded in.
        assert status.reply_windows[CODEX] is ReplyWindow.OPEN

    def test_a_claude_turn_that_ended_is_counted_as_finished_at_once(self) -> None:
        """Every reader of the roster, not only the notice (#213).

        The Stop used to leave the row `RUNNING` until the next discovery pass —
        one cadence in which the notice said the turn had finished and the Roster
        Brief counted the same Session as running. The row now says what the Stop
        implies, so the count agrees with the notice from the moment it is read.
        """
        hub = Hub(sessions=((CLAUDE, "port the log"),))

        hub.emit(SessionStopped(target=CLAUDE))

        summary = briefing.roster(hub.state.sessions.live(), None)
        assert summary.counts == {BriefState.FINISHED: 1}
        assert hub.state.sessions.resolve(CLAUDE).state is SessionState.IDLE

    def test_a_stop_for_a_session_the_roster_never_saw_is_still_briefed_as_stopped(
        self,
    ) -> None:
        """The stand-in row, which no registry fold can have reached (#213).

        A Stop can arrive for a Session no discovery pass has landed yet, and
        `stop_brief` builds a row for it from the address alone. The state a Stop
        implies is derived there too — by the same rule the registry now applies
        to a row it holds — so the notice reads `finished` rather than the
        `Session` default of `running`.
        """
        hub = Hub(voice=False)
        stranger = SessionTarget(agent=AgentKind.CLAUDE, session_id="stranger", pid=999)

        hub.emit(SessionStopped(target=stranger))

        (notice,) = hub.channel.sent
        assert notice.startswith("claude:stranger:999 — finished")

    def test_a_failed_stop_read_does_not_replace_a_readable_roster_observation(self) -> None:
        hub = Hub()
        observed = ProgressObservation.readable(
            has_history=True,
            recent=(
                ProgressEntry(
                    ordinal=0,
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

    def test_a_decision_stop_pushes_the_briefs_text(self) -> None:
        """The Stop Notice is a Session Brief published as text (CONTEXT.md).

        Whole-text equality rather than a substring: what the channel receives is
        `Briefing.text` and nothing wrapped around it, and a substring assertion
        would pass on a Core sentence that happened to quote the brief.
        """
        hub = Hub(voice=False)

        hub.emit(
            SessionStopped(
                target=CODEX,
                waiting_for=WaitingFor(
                    kind=WaitingKind.QUESTION,
                    prompt="Which base?",
                    options=(
                        Option(text="main", description="Merge into the default branch"),
                        Option(text="feature"),
                    ),
                    recommendation="main",
                ),
            )
        )

        assert hub.channel.sent == [
            "GPT-VoiceCoding · port the log — codex:abc — waiting for your decision\n"
            "  newest: not read\n"
            "  asked: Which base?\n"
            "  option: main — Merge into the default branch\n"
            "  option: feature\n"
            "  recommends: main\n"
            "  answer: at the terminal\n"
            "  last activity: not read"
        ]

    def test_a_finished_stop_pushes_the_briefs_text(self) -> None:
        """A Claude turn that ended asking nothing is FINISHED, in Briefing's words."""
        hub = Hub(voice=False, sessions=((CLAUDE, "port the log"),))

        hub.emit(SessionStopped(target=CLAUDE))

        assert hub.channel.sent == [
            "GPT-VoiceCoding · port the log — claude:def:100 — finished\n"
            "  newest: not read\n"
            "  answer: from here\n"
            "  last activity: not read"
        ]

    def test_an_unreadable_stop_pushes_the_briefs_text(self) -> None:
        """A Stop that could not say what it stopped on is never a decision (#166 B7)."""
        hub = Hub(voice=False)

        hub.emit(SessionStopped(target=CODEX, waiting_for=WaitingFor(kind=WaitingKind.UNKNOWN)))

        assert hub.channel.sent == [
            "GPT-VoiceCoding · port the log — codex:abc — unreadable\n"
            "  newest: not read\n"
            "  answer: at the terminal\n"
            "  last activity: not read"
        ]

    def test_a_permission_stop_with_no_held_handle_pushes_the_briefs_text(self) -> None:
        """Nothing is parked on the approval socket, so the Stop Notice is all there is."""
        hub = Hub(voice=False)

        hub.emit(
            SessionStopped(
                target=CODEX,
                waiting_for=WaitingFor(
                    kind=WaitingKind.PERMISSION, tool_name="Bash", detail="push the branch"
                ),
            )
        )

        assert hub.channel.sent == [
            "GPT-VoiceCoding · port the log — codex:abc — requesting permission\n"
            "  newest: not read\n"
            "  permission: Bash — push the branch\n"
            "  answer: at the terminal\n"
            "  last activity: not read"
        ]

    def test_the_stops_newest_message_reaches_the_channel_whole(self) -> None:
        """ADR 0016: the engine never condenses, and `newest` is Briefing's field."""
        hub = Hub(voice=False)

        hub.emit(
            SessionStopped(
                target=CODEX,
                progress=ProgressObservation.readable(
                    has_history=True,
                    recent=(
                        ProgressEntry(
                            ordinal=0,
                            role=ProgressRole.ASSISTANT,
                            text="The diagnosis points to the session registry.",
                        ),
                    ),
                    read_at=datetime(2026, 8, 31, 2, 44, 39, tzinfo=UTC),
                ),
            )
        )

        (notice,) = hub.channel.sent
        assert "  newest: The diagnosis points to the session registry." in notice

    def test_a_stop_that_said_nothing_yet_says_so_in_briefings_words(self) -> None:
        hub = Hub(voice=False)

        hub.emit(
            SessionStopped(
                target=CODEX,
                progress=ProgressObservation.readable(
                    has_history=False,
                    read_at=datetime(2026, 8, 31, 2, 44, 39, tzinfo=UTC),
                ),
            )
        )

        (notice,) = hub.channel.sent
        assert "  newest: nothing said yet" in notice

    def test_an_oversize_newest_entry_is_named_as_omitted_in_briefings_words(self) -> None:
        hub = Hub(voice=False)

        hub.emit(
            SessionStopped(
                target=CODEX,
                progress=ProgressObservation.readable(
                    has_history=True,
                    omission=ProgressOmission.NEWEST_OVERSIZE,
                    read_at=datetime(2026, 8, 31, 2, 44, 39, tzinfo=UTC),
                ),
            )
        )

        (notice,) = hub.channel.sent
        assert "  newest: the newest entry is too large to carry" in notice
        assert "nothing said yet" not in notice

    def test_a_stop_for_a_session_the_roster_never_saw_is_briefed_from_its_address(self) -> None:
        """No row to brief, so the stand-in carries the address and nothing invented."""
        hub = Hub(voice=False)
        stranger = SessionTarget(agent=AgentKind.CODEX, session_id="not-in-the-roster")

        hub.emit(
            SessionStopped(
                target=stranger,
                waiting_for=WaitingFor(kind=WaitingKind.QUESTION, prompt="Which base?"),
            )
        )

        assert hub.channel.sent == [
            "codex:not-in-the-roster — waiting for your decision\n"
            "  newest: not read\n"
            "  asked: Which base?\n"
            "  answer: at the terminal\n"
            "  last activity: not read"
        ]

    def test_the_log_line_carries_the_briefs_text(self, caplog) -> None:
        """`ENGINE_STOP_LINE` still matches its first line (`tests/acceptance/journey.py`)."""
        caplog.set_level("INFO", logger="gpt_voicecoding.core.bridge")
        hub = Hub(voice=False)

        hub.emit(
            SessionStopped(
                target=CODEX,
                waiting_for=WaitingFor(kind=WaitingKind.QUESTION, prompt="Which base?"),
            )
        )

        (line,) = [
            one.getMessage()
            for one in caplog.records
            if one.getMessage().startswith("Session stopped:")
        ]
        assert re.search(journey.ENGINE_STOP_LINE, line.splitlines()[0])
        assert "Which base?" in line

    def test_a_stop_notice_never_relays_words_back_into_a_session(self) -> None:
        """The system tells the user about a stop; it does not address an agent."""
        hub = Hub()

        hub.emit(SessionStopped(target=CODEX))

        assert hub.agent.calls == []

    def test_a_stopped_session_is_announced_by_its_name(self) -> None:
        hub = Hub()

        hub.emit(SessionStopped(target=CODEX))

        assert "port the log" in handed_over(hub.call)

    def test_a_stop_while_the_system_owns_a_call_opens_no_second_call(self) -> None:
        """The reference implementation's loop, made unreachable.

        Nothing is spoken into the call either: with one up, a `wake` is nothing
        on the voice side, because mid-call news is #196's. The text side is
        unaffected, so the Stop still reaches the Companion Channel.
        """
        hub = Hub()
        started = hub.toggle()
        assert started.call_id is not None
        hub.emit(CallStarted(call_id=started.call_id))

        hub.emit(SessionStopped(target=CODEX))

        assert hub.call.calls_started == 1
        assert hub.call.spoken == []
        assert hub.channel.sent

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
        hub.flip(SwitchName.DUTY, False)
        hub.flip(SwitchName.DUTY, True)
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

        asyncio.run(hub.core.discover())

        assert hub.agent.inspections == []
        assert handed_over(hub.call) == ""

        hub.flip(SwitchName.DUTY, True)

        assert "Which base?" in handed_over(hub.call)
        assert hub.state.relays.pending() == ()

    def test_the_auto_hangup_switch_is_no_outlet_transition(self) -> None:
        """It opens no way to reach the user, so turning it on re-announces nothing."""
        hub = Hub(auto_hangup=False)
        hub.emit(SessionStopped(target=CODEX))
        hub.agent.discovery = LaneDiscovery(
            rows=(
                SessionInspection(
                    target=CODEX,
                    workspace=Path("/tmp/workspace"),
                    state=SessionState.WAITING,
                    waiting_for=WaitingFor(kind=WaitingKind.QUESTION, prompt="Which base?"),
                ),
            )
        )
        hub.call.spoken.clear()
        hub.call.opened_on.clear()
        hub.channel.sent.clear()

        hub.flip(SwitchName.AUTO_HANGUP, True)
        asyncio.run(hub.core.discover())

        assert hub.call.spoken == []
        assert handed_over(hub.call) == ""
        assert hub.channel.sent == []

    def test_message_off_and_voice_on_never_pushes_text(self) -> None:
        hub = Hub(message=False)

        hub.emit(SessionStopped(target=CODEX))

        assert hub.channel.sent == []


class TestTheApprovalRelayCarriesAndNothingMore:
    """The verb, and the four ways it can end (#191).

    No pipeline, no budget, no announcement of its own: a permission is one of
    the three Session states, so it is announced as a Stop Notice like every
    other wait and this verb only carries the verdict back.
    """

    def waiting_row(self, target: SessionTarget = CODEX, approval_id: str | None = "a1") -> None:
        return SessionInspection(
            target=target,
            workspace=Path("/tmp/workspace"),
            state=SessionState.WAITING,
            waiting_for=WaitingFor(
                kind=WaitingKind.PERMISSION,
                tool_name="Bash",
                detail="push the branch",
                approval_id=approval_id,
            ),
        )

    def hub_with_dialog(self, **kwargs: object) -> Hub:
        hub = Hub(**kwargs)  # type: ignore[arg-type]
        hub.agent.discovery = LaneDiscovery(rows=(self.waiting_row(),))
        asyncio.run(hub.core.discover())
        return hub

    def answer(self, hub: Hub, approval_id: str = "a1", verdict=ApprovalVerdict.ALLOW):
        return asyncio.run(hub.core.answer_approval(approval_id, verdict))

    def test_a_verdict_for_a_handle_the_roster_carries_reaches_the_lane(self) -> None:
        hub = self.hub_with_dialog(voice=False)

        outcome = self.answer(hub)

        assert [(call.verb, call.verdict) for call in hub.agent.calls] == [
            ("approval_relay", ApprovalVerdict.ALLOW)
        ]
        assert outcome is not None
        assert outcome.target == CODEX
        assert outcome.state is Lifecycle.DELIVERED
        assert outcome.reason is RelayReason.DELIVERED
        assert outcome.receipt is not None and outcome.receipt.request_id == outcome.request_id

    def test_answering_a_permission_takes_the_focus_as_an_answer_relay_does(self) -> None:
        hub = self.hub_with_dialog(voice=False)

        self.answer(hub)

        assert hub.state.sessions.focus == CODEX

    def test_a_verdict_for_a_handle_no_row_carries_is_refused_and_sends_nothing(self) -> None:
        hub = self.hub_with_dialog(voice=False)
        hub.channel.sent.clear()

        assert self.answer(hub, "a-gone") is None
        assert hub.agent.calls == []
        assert hub.channel.sent == []

    def test_a_verdict_for_a_row_whose_hook_ended_is_refused(self) -> None:
        """The hook released the dialog, so the row carries no handle any more."""
        hub = Hub(voice=False)
        hub.agent.discovery = LaneDiscovery(rows=(self.waiting_row(approval_id=None),))
        asyncio.run(hub.core.discover())

        assert self.answer(hub) is None
        assert hub.agent.calls == []

    def test_a_verdict_for_a_spawned_target_is_refused_in_its_own_words(self) -> None:
        """ "Never spoken to" includes never answered (advisor, 2026-08-27)."""
        hub = Hub(voice=False)
        child = SessionTarget(agent=AgentKind.CODEX, session_id="child-1", pid=99)
        hub.agent.discovery = LaneDiscovery(
            rows=(
                self.waiting_row(),
                SessionInspection(
                    target=child,
                    workspace=Path("/tmp/workspace"),
                    state=SessionState.WAITING,
                    waiting_for=WaitingFor(
                        kind=WaitingKind.PERMISSION,
                        tool_name="Bash",
                        approval_id="c1",
                    ),
                    child=ChildClassification(kind=ChildKind.CHILD, parent=CODEX),
                ),
            )
        )
        asyncio.run(hub.core.discover())

        with pytest.raises(ChildSessionError):
            self.answer(hub, "c1")
        assert hub.agent.calls == []

    def test_an_unproven_receipt_returns_its_grade_and_announces_nothing(self) -> None:
        """`HELD`, `FAILED` and `UNKNOWN` are the verb's answer, not a notice."""
        for grade, reason in (
            (Delivery.HELD, RelayReason.HELD_FAR_SIDE),
            (Delivery.FAILED, RelayReason.AWAITING_REPLY_WINDOW),
            (Delivery.UNKNOWN, RelayReason.DUPLICATE_RISK),
        ):
            hub = self.hub_with_dialog(voice=False)
            hub.agent.outcome = grade
            hub.channel.sent.clear()

            outcome = self.answer(hub)

            assert outcome is not None
            assert outcome.state is Lifecycle.REPORTED_FAILED
            assert outcome.reason is reason
            assert outcome.grade == str(grade)
            assert hub.channel.sent == []

    def test_a_row_still_running_under_its_dialog_stays_answerable(self) -> None:
        """The Codex shape: the thread is `active`, the prompt is up (#191).

        A Codex thread does not leave `active` while its prompt is on screen, so
        the Stop that dialog raised is read onto a row that keeps saying
        `running`. The next discovery pass must not erase that reading (#209's
        write-back), or the handle is gone and the verdict has nothing to reach.
        """
        hub = Hub(voice=False)
        running_with_a_dialog = SessionInspection(
            target=CODEX,
            workspace=Path("/tmp/workspace"),
            state=SessionState.RUNNING,
            waiting_for=self.waiting_row().waiting_for,
        )
        hub.emit(SessionStopped(target=CODEX, waiting_for=running_with_a_dialog.waiting_for))
        hub.agent.discovery = LaneDiscovery(rows=(running_with_a_dialog,))
        asyncio.run(hub.core.discover())

        (row,) = hub.core.status().sessions
        assert row.waiting_for.approval_id == "a1"
        assert self.answer(hub) is not None
        assert [call.verb for call in hub.agent.calls] == ["approval_relay"]

    def test_the_engine_keeps_no_clock_on_a_held_dialog(self) -> None:
        """No budget, no sweep: the wire ends the hook, not `tick` (ADR 0015).

        Voice is on and a call is up, because the release this test proves does
        not happen is the one that used to fire mid-sentence: an engine-timed
        expiry spoke into whatever call was live and pushed a closing notice
        beside it. Nothing is pushed and nothing rings.
        """
        hub = self.hub_with_dialog()
        started = hub.toggle()
        assert started.call_id is not None
        hub.call.spoken.clear()
        hub.channel.sent.clear()

        hub.now += TEN_MINUTES
        hub.tick()
        hub.tick()

        assert hub.agent.calls == []
        assert hub.channel.sent == []
        assert hub.call.spoken == []


class TestTheLiveCallSilenceCeiling:
    def test_tick_ends_an_owned_call_once_when_the_silence_threshold_arrives(self, caplog) -> None:
        caplog.set_level("INFO", logger="gpt_voicecoding.core.call_keeper")
        hub = Hub(silence_end_seconds=60.0)
        started = hub.toggle()
        assert started.call_id is not None
        hub.emit(CallStarted(call_id=started.call_id))

        hub.now += 60.0
        hub.tick()
        hub.tick()

        assert hub.call.calls_ended == 1
        assert hub.core.keeper.status().call_id is None
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

    def test_news_about_a_session_does_not_restart_the_owned_calls_window(self) -> None:
        """A Stop is not activity *on the call*, and since #195 it is not spoken into one.

        Mid-call news is #196's: with a call up, a `wake` is nothing on the
        voice side, so the only thing a Stop does here is reach the Companion
        Channel. What keeps the call alive is somebody speaking, which arrives
        on its own as a speaking edge (#184).
        """
        hub = Hub(silence_end_seconds=60.0)
        started = hub.toggle()
        assert started.call_id is not None
        hub.emit(CallStarted(call_id=started.call_id))

        hub.now += 50.0
        hub.emit(SessionStopped(target=CODEX))
        hub.now += 10.0
        hub.tick()

        assert hub.call.spoken == []
        assert hub.call.calls_started == 1
        assert hub.channel.sent
        assert hub.call.calls_ended == 1

    def test_the_voice_speaking_holds_the_ceiling_open(self) -> None:
        """A 75 s answer generated in 10 s is still a call somebody is talking on."""
        hub = Hub(silence_end_seconds=60.0)
        started = hub.toggle()
        assert started.call_id is not None
        hub.emit(CallStarted(call_id=started.call_id))

        # Deltas every 5 s for 75 s: one span, one start edge, no end edge yet.
        hub.emit(VoiceSpeech(speaking=True))
        for _ in range(15):
            hub.now += 5.0
            hub.tick()

        assert hub.call.calls_ended == 0

        # The settle window sits between the stop edge and the ceiling: for
        # `speech_settle_seconds` after the last speaker stops, the pause is
        # still a pause (`core/policy.py::DEFAULT_SPEECH_SETTLE_SECONDS`).
        hub.emit(VoiceSpeech(speaking=False))
        hub.now += 64.9
        hub.tick()
        assert hub.call.calls_ended == 0

        hub.now += 0.1
        hub.tick()
        assert hub.call.calls_ended == 1

    def test_a_voice_that_stopped_and_then_silence_ends_the_call(self) -> None:
        """The stop edge is activity too: the window runs from the end of the answer."""
        hub = Hub(silence_end_seconds=60.0)
        started = hub.toggle()
        assert started.call_id is not None
        hub.emit(CallStarted(call_id=started.call_id))

        hub.now += 50.0
        hub.emit(VoiceSpeech(speaking=True), VoiceSpeech(speaking=False))
        hub.now += 64.9
        hub.tick()
        assert hub.call.calls_ended == 0

        hub.now += 0.1
        hub.tick()
        assert hub.call.calls_ended == 1

    def test_a_call_dropped_mid_answer_leaves_the_next_one_unheld(self) -> None:
        """A start edge with no stop, then the call goes away. No flag outlives it."""
        hub = Hub(silence_end_seconds=60.0)
        started = hub.toggle()
        assert started.call_id is not None
        hub.emit(CallStarted(call_id=started.call_id))
        hub.emit(VoiceSpeech(speaking=True))
        hub.emit(CallDropped(call_id=started.call_id, detail="the far side left"))

        hub.emit(CallStarted(call_id="call-2"))
        hub.now += 60.0
        hub.tick()

        assert hub.call.calls_ended == 1

    def test_a_ceiling_is_not_measured_while_news_is_unread(self) -> None:
        """The two loops are two tasks, so an emitted edge is not a noted one.

        `_ticking` and `_dispatching` are separate (`engine/composition.py`), so
        a `VoiceSpeech(True)` sitting in the queue has not reached the interlock.
        A tick that measured silence then would end the call one event before
        being told the Voice was speaking on it — which is #184's bug arriving by
        a different door.
        """
        hub = Hub(silence_end_seconds=60.0)
        started = hub.toggle()
        assert started.call_id is not None
        hub.emit(CallStarted(call_id=started.call_id))

        hub.now += 60.0
        # Emitted, deliberately not drained: this is the state the tick loop can
        # find the hub in.
        hub.core.events.emit(VoiceSpeech(speaking=True))
        hub.tick()
        assert hub.call.calls_ended == 0

        # Read, and now the flag itself is what holds the ceiling open.
        hub.emit()
        hub.now += 600.0
        hub.tick()
        assert hub.call.calls_ended == 0

    def test_news_about_a_session_does_not_hold_a_silent_call_open(self) -> None:
        """The ceiling's question is about the call, so only the call can defer it.

        Asking the queue whether it held *anything* was the first shape of the
        guard above, and it was too wide: a `SessionStopped` waiting to be read
        is not activity on a call, and a lane that kept producing events could
        have held a forgotten call open for as long as it kept producing them.
        """
        hub = Hub(silence_end_seconds=60.0)
        started = hub.toggle()
        assert started.call_id is not None
        hub.emit(CallStarted(call_id=started.call_id))

        hub.now += 60.0
        hub.core.events.emit(SessionStopped(target=CODEX))
        hub.tick()

        assert hub.call.calls_ended == 1

    def test_a_call_with_nothing_unread_is_still_ended_when_it_is_silent(self) -> None:
        """The guard is about unread news, not about being cautious in general."""
        hub = Hub(silence_end_seconds=60.0)
        started = hub.toggle()
        assert started.call_id is not None
        hub.emit(CallStarted(call_id=started.call_id))

        hub.now += 60.0
        hub.tick()

        assert hub.call.calls_ended == 1

    def test_both_voice_edges_are_written_down(self, caplog) -> None:
        """The engine's log is where a run says the Voice held the call open."""
        caplog.set_level("INFO", logger="gpt_voicecoding.core.bridge")
        hub = Hub(silence_end_seconds=60.0)
        started = hub.toggle()
        assert started.call_id is not None
        hub.emit(CallStarted(call_id=started.call_id))

        hub.emit(VoiceSpeech(speaking=True), VoiceSpeech(speaking=False))

        said = [record.getMessage() for record in caplog.records]
        assert VOICE_SPEAKING_LINE in said
        assert VOICE_QUIET_LINE in said

    def test_the_ceiling_holds_with_duty_off_on_a_call_the_user_opened(self) -> None:
        """Only the Auto Hang-up Switch governs it; Duty and Voice do not reach it."""
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

    def test_the_auto_hangup_switch_off_leaves_a_silent_call_up(self) -> None:
        hub = Hub(auto_hangup=False, silence_end_seconds=60.0)
        started = hub.toggle()
        assert started.call_id is not None
        hub.emit(CallStarted(call_id=started.call_id))

        hub.now += 600.0
        hub.tick()
        hub.tick()

        assert hub.call.calls_ended == 0
        assert hub.core.keeper.status().call_id is not None

    def test_turning_the_switch_back_on_ends_a_call_already_past_the_ceiling(self) -> None:
        """The one ending attempt per call is not spent while the switch is off."""
        hub = Hub(auto_hangup=False, silence_end_seconds=60.0)
        started = hub.toggle()
        assert started.call_id is not None
        hub.emit(CallStarted(call_id=started.call_id))

        hub.now += 600.0
        hub.tick()
        assert hub.call.calls_ended == 0

        hub.flip("auto_hangup", True)
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
        caplog.set_level("ERROR", logger="gpt_voicecoding.core.call_keeper")
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
            "the Call adapter refused an operation the Keeper asked for",
            "the Call adapter refused an operation the Keeper asked for",
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

    def permission(self) -> WaitingFor:
        """A wait carrying a live dialog handle — the answerable kind."""
        return WaitingFor(kind=WaitingKind.PERMISSION, tool_name="Bash", approval_id="a1")

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

        assert "port the log" in handed_over(hub.call)

    def test_a_stop_on_a_session_the_roster_has_not_seen_reaches_both_outlets(self) -> None:
        """Unknown is not child, and the asymmetry is the point.

        Discovery runs on a cadence, so a Session can stop before the roster has
        a row for it. Refusing to announce that would lose the notice entirely,
        while announcing a child costs one message about something that is about
        to be refused anyway — and since #216 it costs a call too, which is the
        price of never losing the notice a real Session's Stop is.

        **One Stop, both outlets, in one test**, because the defect #216 names is
        exactly that the two disagreed: the text named the Session and the call
        never came. What a call comes up holding is the *roster*'s reading at the
        moment of dialling (ADR 0017), so the fix is not to hand the dial this
        notice's own brief — the second source of truth #213 deleted — but to
        put the row the Stop stands in for on the roster, where the fresh reading
        finds it.
        """
        hub = Hub()

        hub.emit(SessionStopped(target=SessionTarget(agent=AgentKind.CODEX, session_id="new")))

        assert hub.channel.sent
        assert "codex:new" in hub.channel.sent[0]
        assert hub.call.calls_started == 1
        assert "codex:new" in handed_over(hub.call)

    def test_the_question_an_unseen_session_stopped_on_is_what_the_call_carries(self) -> None:
        """The stand-in row carries the Stop's own reading, so the dial says it.

        The wait is the whole point of the notice: a call that rang about a
        Session and could not say what it was waiting on would be #209's defect
        under a different cause.
        """
        hub = Hub()

        hub.emit(
            SessionStopped(
                target=SessionTarget(agent=AgentKind.CODEX, session_id="new"),
                waiting_for=WaitingFor(
                    kind=WaitingKind.QUESTION, prompt="Which base?", approval_id="q-1"
                ),
            )
        )

        assert hub.call.calls_started == 1
        assert "Which base?" in handed_over(hub.call)

    def test_the_session_a_stop_stood_in_for_is_on_the_roster_afterwards(self) -> None:
        """One roster, so `status` answers about it like every other reader."""
        hub = Hub()

        hub.emit(
            SessionStopped(
                target=SessionTarget(agent=AgentKind.CODEX, session_id="new"),
                waiting_for=self.permission(),
            )
        )

        listed = [row for row in hub.core.status().sessions if row.target.session_id == "new"]
        assert len(listed) == 1
        assert listed[0].state is SessionState.WAITING

    def test_a_permission_a_child_raises_is_not_announced(self) -> None:
        """A Codex subagent thread can raise a real `requestApproval`.

        Accepted for v1.0 (advisor, 2026-08-27): "never spoken to" includes
        never answered, so that dialog is the keyboard's. The alternative is an
        Approval Relay carrying the user's authority into a Session `resolve`
        refuses to address — which is why the same rule is said again where the
        verdict would be carried (`answer_approval`).
        """
        hub = Hub()
        child = self.spawned(hub)

        hub.emit(SessionStopped(target=child, waiting_for=self.permission()))

        assert hub.channel.sent == []

    def test_its_parents_permission_is_announced_as_it_always_was(self) -> None:
        hub = Hub()
        self.spawned(hub)

        hub.emit(SessionStopped(target=CODEX, waiting_for=self.permission()))

        assert "requesting permission" in handed_over(hub.call)


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

        assert outcome.receipt is not None
        assert outcome.receipt.outcome is Delivery.DELIVERED
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

    def test_words_for_a_busy_session_wait_and_the_channel_gets_the_receipt(self) -> None:
        """The inbound path answers with the same structured receipt the CLI prints."""
        hub = Hub()

        hub.emit(InboundText(text="ship it"))

        assert hub.agent.calls == []
        assert hub.channel.sent == ["state=retained grade=none reason=awaiting_reply_window"]

    def test_the_open_window_delivers_them_without_answering_again(self) -> None:
        """The receipt answers the words the user sent. The flush is not an answer."""
        hub = Hub()
        hub.emit(InboundText(text="ship it"))
        receipts = len(hub.channel.sent)

        hub.emit(ReplyWindowChanged(target=CODEX, window=ReplyWindow.OPEN))

        assert [call.text for call in hub.agent.calls] == ["ship it"]
        assert len(hub.channel.sent) == receipts

    def test_ten_minutes_of_waiting_lands_on_the_row_and_not_in_the_channel(self) -> None:
        """#197: the news travels as a brief field, never as a line pushed beside it."""
        hub = Hub()
        hub.emit(InboundText(text="ship it"))
        pushed = list(hub.channel.sent)

        hub.now += TEN_MINUTES
        hub.tick()

        assert hub.state.relays.pending() == ()
        assert hub.state.sessions.resolve(CODEX).undelivered == UndeliveredRelay(
            reason=str(RelayReason.CEILING_PASSED)
        )
        assert hub.channel.sent == pushed, "no terminal line is pushed at the user any more"

    def test_an_expired_relay_that_was_attempted_does_not_read_like_one_that_was_not(
        self,
    ) -> None:
        """The grade travels onto the row, so `UNKNOWN` is not read as `FAILED`.

        This is what the four deleted ceiling reports came in proven/unproven
        pairs for: "it never reached the session" is true of words that never
        went and a guess about an attempt that proved nothing. The code says
        what happened here; the grade says what was proved, and Briefing's verb
        is chosen from the pair.
        """
        hub = Hub()
        hub.agent.outcome = Delivery.UNKNOWN
        hub.agent.reason = "no readback"
        hub.emit(InboundText(text="ship it"))
        hub.emit(ReplyWindowChanged(target=CODEX, window=ReplyWindow.OPEN))

        hub.now += TEN_MINUTES
        hub.tick()

        undelivered = hub.state.sessions.resolve(CODEX).undelivered
        assert undelivered == UndeliveredRelay(
            reason=str(RelayReason.CEILING_PASSED), grade=Delivery.UNKNOWN
        )
        assert (
            "may not have arrived"
            in briefing.spoken(briefing.session(hub.state.sessions.resolve(CODEX))).undelivered
        )

    def test_a_second_failure_replaces_the_reason_the_first_one_left(self) -> None:
        hub = Hub()
        hub.emit(InboundText(text="ship it"))
        hub.now += TEN_MINUTES
        hub.tick()
        hub.agent.outcome = Delivery.UNKNOWN
        hub.agent.reason = "no readback"
        hub.emit(InboundText(text="and this"))
        hub.emit(ReplyWindowChanged(target=CODEX, window=ReplyWindow.OPEN))

        hub.now += TEN_MINUTES
        hub.tick()

        assert hub.state.sessions.resolve(CODEX).undelivered == UndeliveredRelay(
            reason=str(RelayReason.CEILING_PASSED), grade=Delivery.UNKNOWN
        )

    def test_a_relay_that_reaches_the_session_afterwards_clears_it(self) -> None:
        hub = Hub()
        hub.emit(InboundText(text="ship it"))
        hub.now += TEN_MINUTES
        hub.tick()
        assert hub.state.sessions.resolve(CODEX).undelivered is not None

        hub.emit(InboundText(text="and this"))
        hub.emit(ReplyWindowChanged(target=CODEX, window=ReplyWindow.OPEN))

        assert hub.state.sessions.resolve(CODEX).undelivered is None

    def test_a_verdict_that_lands_clears_it_too(self) -> None:
        """An Approval Relay is the user's own words arriving (#165 Q2, #197)."""
        hub = Hub(voice=False)
        hub.emit(InboundText(text="ship it"))
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
        asyncio.run(hub.core.discover())
        assert hub.state.sessions.resolve(CODEX).undelivered is not None

        asyncio.run(hub.core.answer_approval("a1", ApprovalVerdict.ALLOW))

        assert hub.state.sessions.resolve(CODEX).undelivered is None

    def test_a_receipt_that_arrives_late_and_proves_delivery_clears_it(self) -> None:
        """The proof came back minutes later on the inbox's own route (ADR 0013)."""
        hub = Hub(voice=False)
        hub.agent.outcome = Delivery.UNKNOWN
        hub.agent.reason = "no readback"
        hub.emit(InboundText(text="ship it"))
        hub.emit(ReplyWindowChanged(target=CODEX, window=ReplyWindow.OPEN))
        hub.now += TEN_MINUTES
        hub.tick()
        assert hub.state.sessions.resolve(CODEX).undelivered is not None
        # A second Relay is queued and still waiting; its proof arrives late.
        hub.emit(InboundText(text="and this"))
        pending = hub.state.relays.pending()

        hub.emit(
            RelayReceipt(
                target=CODEX,
                receipt=DeliveryReceipt(
                    request_id=pending[0].request_id,
                    outcome=Delivery.DELIVERED,
                    reason="the Session acknowledged it",
                ),
            )
        )

        assert hub.state.sessions.resolve(CODEX).undelivered is None

    def test_a_ceiling_that_lands_on_the_row_says_so_in_the_log(self, caplog) -> None:
        """#226: the write the field's two readers disagree over is on the record.

        `brief` and the Stop Notice read one row at two moments, so a run that
        finds them disagreeing has to be able to ask what happened in between.
        Only a line at the write can answer that.
        """
        caplog.set_level("INFO", logger="gpt_voicecoding.core.bridge")
        hub = Hub()
        hub.emit(InboundText(text="ship it"))
        (queued,) = hub.state.relays.pending()

        hub.now += TEN_MINUTES
        hub.tick()

        assert _field_lines(caplog) == [
            f"a Relay to {CODEX} did not arrive, and its brief now says so: "
            f"relay={queued.request_id} reason={RelayReason.CEILING_PASSED} grade=None"
        ]

    def test_a_receipt_that_clears_the_row_says_so_in_the_log(self, caplog) -> None:
        """The other half of #226, and the one the `relay` acceptance reads."""
        hub = Hub(voice=False)
        hub.agent.outcome = Delivery.UNKNOWN
        hub.agent.reason = "no readback"
        hub.emit(InboundText(text="ship it"))
        hub.emit(ReplyWindowChanged(target=CODEX, window=ReplyWindow.OPEN))
        hub.now += TEN_MINUTES
        hub.tick()
        hub.emit(InboundText(text="and this"))
        (queued,) = hub.state.relays.pending()
        caplog.set_level("INFO", logger="gpt_voicecoding.core.bridge")

        hub.emit(
            RelayReceipt(
                target=CODEX,
                receipt=DeliveryReceipt(
                    request_id=queued.request_id,
                    outcome=Delivery.DELIVERED,
                    reason="the Session acknowledged it",
                ),
            )
        )

        assert _field_lines(caplog) == [
            f"a Relay to {CODEX} arrived after all, and its brief no longer says so: "
            f"relay={queued.request_id}"
        ]

    def test_a_write_that_changes_nothing_says_nothing(self, caplog) -> None:
        """The common case stays silent: only a change to the field is news."""
        hub = Hub(window=ReplyWindow.OPEN)
        caplog.set_level("INFO", logger="gpt_voicecoding.core.bridge")

        hub.emit(InboundText(text="ship it"))

        assert hub.state.sessions.resolve(CODEX).undelivered is None
        assert _field_lines(caplog) == []

    def test_a_relay_still_queued_leaves_the_field_exactly_as_it_stands(self) -> None:
        hub = Hub()
        hub.emit(InboundText(text="ship it"))
        hub.now += TEN_MINUTES
        hub.tick()
        stood = hub.state.sessions.resolve(CODEX).undelivered

        hub.emit(InboundText(text="and this"))

        assert hub.state.sessions.resolve(CODEX).undelivered == stood

    def test_a_relay_held_in_front_of_a_person_leaves_it_alone_too(self) -> None:
        hub = Hub()
        hub.agent.outcome = Delivery.HELD
        hub.agent.reason = "parked for a human"
        hub.emit(InboundText(text="ship it"))
        hub.emit(ReplyWindowChanged(target=CODEX, window=ReplyWindow.OPEN))

        assert hub.state.sessions.resolve(CODEX).undelivered is None

    def test_a_session_that_ends_reports_the_words_still_waiting_for_it(self) -> None:
        """No wake and no field: an exited Session appears nowhere, so the log has it."""
        hub = Hub()
        hub.emit(InboundText(text="ship it"))

        hub.emit(SessionEnded(target=CODEX))

        assert hub.state.relays.pending() == ()
        ended = hub.state.sessions.all()[0]
        assert ended.lifecycle is SessionLifecycle.ENDED
        assert ended.undelivered is None
        assert hub.call.calls_started == 0


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
        assert hub.core.keeper.status().call_id is not None

    def test_a_call_the_user_opened_announces_nothing_and_stays_silent(self) -> None:
        """A user-opened call is not a reconciliation, and since #195 nothing is.

        They pressed the toggle in order to talk. Briefing them on the roster
        they were already looking at would be the system speaking first (#167
        Q6), and speaking into the call afterwards is mid-call behaviour, which
        is #196's. A discovery pass that follows changes none of it: the pass
        announces nothing on its own.
        """
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
        assert "Which base?" not in handed_over(hub.call)

        asyncio.run(hub.core.discover())

        assert "Which base?" not in handed_over(hub.call)
        assert spoken_words(hub.call) == ""
        assert hub.channel.sent == []

    def test_a_call_started_event_announces_nothing(self) -> None:
        """A call coming up is not an outlet transition; it is a call coming up.

        The reconciliation the interlock's release and adoption used to owe is
        gone with the flag that carried it (#195): both surfaces read the roster
        at the moment they act, and a call the user opened is not a moment the
        system was asked to say anything at.
        """
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
        asyncio.run(hub.core.discover())

        assert hub.agent.inspections == []
        assert hub.channel.sent == []

    def test_the_live_toggle_ends_the_call_the_system_owns(self) -> None:
        hub = Hub()
        hub.toggle()

        assert hub.toggle().state is CallState.DOWN
        assert hub.core.keeper.status().call_id is None

    def test_a_call_opens_addressing_both_audiences_by_name(self) -> None:
        """Each half gets its own set, and the hub names the audience, not the slot.

        `realtimeStartInstructions` was proved by slot-swap to reach the Call
        Agent and never the Voice, and `prompt` to reach the Voice (ADR 0018,
        #175 Q4). The hub knows none of that: it hands over a `Dial` whose fields
        are *voice* and *agent*, and which wire slot each lands in is the realtime
        adapter's alone (#194). Before the Dial, one string carried the Agent set
        and the Voice's prose had no carrier at all.
        """
        hub = Hub()
        assert hub.core.instructions is not None

        hub.toggle()

        dialled = hub.call.opened_on[0]
        assert dialled.voice == hub.core.instructions.voice.text
        assert dialled.agent == hub.core.instructions.agent.text

    def test_a_call_the_user_opened_carries_one_item_and_no_hand_over(self) -> None:
        """#167 Q6. They pressed the toggle to talk; the system does not talk first.

        Briefing them on the roster they were looking at when they pressed it
        would be the system opening the conversation on a call the user opened to
        open it themselves.
        """
        hub = Hub()

        hub.toggle()

        assert hub.call.opened_on[0].hand_over == (DialReason(text=USER_OPENED),)
        assert "wait to be spoken to" in USER_OPENED.lower()

    def test_a_system_dialled_call_comes_up_holding_the_roster_and_the_waiting(
        self,
    ) -> None:
        """The hand-over is the briefing, and it is read now rather than replayed.

        ADR 0017: a missed call is briefed from a fresh reading. The notice names
        which Session and the moment; every word about state comes off the roster
        as it stands when the dial is built (#194).
        """
        hub = Hub()

        hub.emit(
            SessionStopped(
                target=CODEX,
                waiting_for=WaitingFor(kind=WaitingKind.QUESTION, prompt="Which base?"),
            )
        )

        kinds = [type(item) for item in hub.call.opened_on[0].hand_over]
        assert kinds[0] is DialReason
        assert kinds[1] is SpokenRosterBrief
        assert SpokenBrief in kinds
        assert hub.call.spoken == []
        assert "Which base?" in handed_over(hub.call)

    def test_the_session_a_stop_dialled_about_is_briefed_from_its_own_row(
        self,
    ) -> None:
        """The row the roster used to be knowingly stale about (#213).

        `sessions.set_stop_reading` used to leave a Stop that merely ended a turn
        in `RUNNING` (#209), and a hand-over briefs no running Session — so the
        call came up saying a Session needed the user and never said which, until
        `_system_dial` passed the notice's own brief in beside the roster (#194).
        The row now carries the state the Stop implies, so the fresh reading ADR
        0017 asks for covers this Session like any other and nothing is passed
        alongside.
        """
        hub = Hub()

        hub.emit(SessionStopped(target=CODEX))

        assert [type(item) for item in hub.call.opened_on[0].hand_over] == [
            DialReason,
            SpokenRosterBrief,
            SpokenBrief,
        ]
        briefed = hub.call.opened_on[0].hand_over[-1]
        assert isinstance(briefed, SpokenBrief)
        # The codex lane reads a turn that ended without a final answer as a
        # decision (#166 B2); what matters here is that it is not `running`.
        assert briefed.state == "waiting for your decision"
        assert "port the log" in briefed.name

    def test_the_session_a_stop_dialled_about_is_briefed_exactly_once(self) -> None:
        """One Session, one brief: the roster is the only thing briefed from.

        The Stop moved the row to `WAITING` and `handover` briefed it from there.
        Nothing is added beside the roster any more, so there is no second copy
        for a Session to be announced twice by on the same call.
        """
        hub = Hub()

        hub.emit(
            SessionStopped(
                target=CODEX,
                waiting_for=WaitingFor(kind=WaitingKind.QUESTION, prompt="Which base?"),
            )
        )

        briefs = [item for item in hub.call.opened_on[0].hand_over if isinstance(item, SpokenBrief)]
        assert len(briefs) == 1
        assert briefs[0].state == "waiting for your decision"

    def test_a_hub_that_generated_no_house_rules_opens_no_call(self) -> None:
        """The refusal comes from the interlock, which is the one door.

        The hub does not carry its own copy of this check, so what a caller sees
        here is the same refusal, worded once, that the escalation pipeline sees.
        """
        hub = Hub(instructions=False)

        with pytest.raises(CallInstructionsMissing):
            hub.toggle()

        assert hub.call.calls_started == 0

    def test_ending_a_call_never_needs_house_rules(self) -> None:
        """Opening is refusable; ending is not. A call that is up must be endable."""
        hub = Hub(instructions=False)
        hub.emit(CallStarted(call_id="call-1"))

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

    def test_a_dropped_call_paces_the_next_one_rather_than_barring_it(self) -> None:
        """A drop is an end of a call, so what follows it is a Cool-down (#195).

        The interlock used to free the next dial the moment it released; the
        Keeper paces it instead. The event inside the Cool-down buys one dial,
        and it is paid from a fresh reading when the Cool-down elapses.
        """
        hub = Hub()
        hub.emit(CallStarted(call_id="call-1"))
        hub.emit(CallDropped(call_id="call-1", detail="the network went away"))

        hub.emit(SessionStopped(target=CODEX))

        assert hub.call.calls_started == 0
        assert hub.core.keeper.status().dial_owed is True

        hub.now += 31.0
        hub.tick()

        assert hub.call.calls_started == 1

    def test_the_control_plane_reads_all_three_facts_the_keeper_holds(self) -> None:
        """`status()` is call id, Cool-down remaining and dial owed (#195).

        Published rather than kept internal, because Cool-down is the one rule
        with no surface of its own: a call that does *not* happen leaves no cue,
        no snapshot and no wrapper run, so an operator asking why nothing rang
        has nothing else to read.
        """
        hub = Hub()
        hub.toggle()
        hub.toggle()
        hub.emit(SessionStopped(target=CODEX))

        answered = hub.core.status()

        assert answered.call_id is None
        assert answered.cool_down_remaining == 30.0
        assert answered.dial_owed is True

    def test_the_message_switch_opening_pushes_text_and_rings_nothing(self) -> None:
        """A wake is the *Voice* outlet opening. Message opening is a text route.

        `wake` is named for "a Duty/Voice off→on transition" (#195). Turning the
        Message Switch on opens a way to reach the user in text, and reaching
        them in text is what the push does; ringing on it would dial a call the
        user asked for messages instead of.
        """
        hub = Hub(voice=False, message=False)
        hub.agent.discovery = LaneDiscovery(
            rows=(
                SessionInspection(
                    target=CODEX,
                    workspace=Path("/tmp/workspace"),
                    state=SessionState.WAITING,
                    waiting_for=WaitingFor(kind=WaitingKind.QUESTION, prompt="Which base?"),
                ),
            )
        )
        asyncio.run(hub.core.discover())

        hub.flip(SwitchName.MESSAGE, True)

        assert [notice for notice in hub.channel.sent if "Which base?" in notice]
        assert hub.call.calls_started == 0
        assert hub.core.keeper.status().dial_owed is False

    def test_a_call_release_starts_a_cool_down_and_announces_nothing(self) -> None:
        """Releasing a call is not a moment the user is owed a report of the roster.

        It used to be one: the interlock clearing set the reconciliation flag.
        What replaces it is the Cool-down — the call ended, so the voice side is
        paced — and an event arriving inside it, which is what buys the next
        dial (#195, `CONTEXT.md` *Cool-down*).
        """
        hub = Hub(voice=False)
        hub.emit(CallStarted(call_id="call-the-user-started"))

        hub.emit(CallDropped(call_id="call-the-user-started", detail="the network went away"))
        asyncio.run(hub.core.discover())

        assert hub.agent.inspections == []
        assert hub.channel.sent == []
        assert hub.core.keeper.status().cool_down_remaining == 30.0

    def test_a_stale_call_event_re_offers_nothing(self) -> None:
        """Only the interlock actually clearing is an outlet transition."""
        hub = Hub(voice=False)
        hub.emit(CallStarted(call_id="current-call"))
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
        assert hub.core.keeper.status().call_id == "current-call"

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
        asyncio.run(hub.core.discover())

        assert hub.agent.inspections == []
        assert handed_over(hub.call) == ""

        # Duty coming back on is one more `wake`, and the Keeper dials on a
        # reading it takes at that moment (#195, ADR 0017).
        hub.flip(SwitchName.DUTY, True)

        assert "Which base?" in handed_over(hub.call)
        assert hub.state.relays.pending() == ()


class TestSwitchAdjudicationEndToEnd:
    @pytest.mark.parametrize(
        ("label", "named_wait"),
        [
            ("permission prompt", "  permission: a tool"),
            ("sandbox request", "  permission: sandbox network access"),
        ],
    )
    def test_reconcile_announces_a_roster_only_named_wait(
        self, label: str, named_wait: str
    ) -> None:
        """A Session whose hook never ran has no other path to a Stop Notice."""
        hub = Hub(voice=False, sessions=((CLAUDE, "inspect the roster"),))
        hub.agent.discovery = claude_waiting_roster(CLAUDE, label)
        asyncio.run(hub.core.discover())

        hub.flip(SwitchName.DUTY, False)
        hub.flip(SwitchName.DUTY, True)

        (session,) = hub.core.status().sessions
        assert session.waiting_for.kind is WaitingKind.PERMISSION
        assert len(hub.channel.sent) == 1
        assert named_wait in hub.channel.sent[0]
        # Briefing's word for a stop nobody could read (#166 B7). A roster-only
        # permission is a wait that *was* read, so it is never that.
        assert "unreadable" not in hub.channel.sent[0]

    def test_one_reconcile_pass_reoffers_one_promoted_wait_with_its_parked_handle_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The promoted base reaches Core with the dialog handle and no duplicate notice."""
        hub = Hub(duty=False, voice=False, sessions=((CLAUDE, "inspect the roster"),))
        request = ApprovalRequest(
            approval_id="a1",
            target=CLAUDE,
            tool_name="Bash",
            detail="inspect the roster",
        )
        lane = claude_waiting_roster(CLAUDE, "sandbox request")

        async def roster(**_asked: object) -> LaneDiscovery:
            return lane

        monkeypatch.setattr(claude_adapter.claude_discovery, "discover", roster)
        adapter = ClaudeAgentAdapter(progress_capture=PROGRESS_CAPTURE)
        adapter._reported[CLAUDE] = SessionReport(  # noqa: SLF001 - registration fact
            session_id=CLAUDE.session_id or "",
            pid=CLAUDE.pid,
        )
        adapter._approvals._waiting["a1"] = ParkedApproval(  # noqa: SLF001 - parked hook fact
            request
        )
        waiting = asyncio.run(adapter.discover()).rows[0].waiting_for
        assert waiting.approval_id == "a1"
        hub.core._agents[AgentKind.CLAUDE] = adapter  # noqa: SLF001 - real lane under test

        hub.emit(SessionStopped(target=CLAUDE, waiting_for=waiting))
        assert hub.channel.sent == []
        hub.state.switches.flip(SwitchName.DUTY, True)
        asyncio.run(hub.core.discover())

        hub.flip(SwitchName.DUTY, False)
        hub.flip(SwitchName.DUTY, True)

        assert len(hub.channel.sent) == 1
        assert "  permission: Bash" in hub.channel.sent[0]
        assert "  answer: from here" in hub.channel.sent[0]

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

        asyncio.run(hub.core.discover())

        assert hub.agent.inspections == []
        assert hub.channel.sent == []

        hub.flip(SwitchName.DUTY, True)

        assert hub.agent.inspections == []
        assert [notice for notice in hub.channel.sent if "Which base?" in notice]

        # A discovery pass announces nothing on its own. Since #195 the reading
        # is taken *at* the transition, so the pass that follows one is an
        # ordinary pass — there is no deferred flag left for it to consume.
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

    def test_duty_turning_on_re_announces_the_same_pending_permission(self) -> None:
        hub = Hub(duty=False, voice=False)
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
        asyncio.run(hub.core.discover())

        assert hub.channel.sent == []

        hub.flip(SwitchName.DUTY, True)

        (announced,) = hub.channel.sent
        assert announced.startswith("GPT-VoiceCoding · port the log")
        assert "  permission: Bash — push the branch" in announced
        assert "  answer: from here" in announced

        # The second outlet transition re-announces the same still-held
        # permission. Bridge Core keeps no memory of the first announcement, so
        # the user coming back is told what is still waiting for them (#161).
        hub.flip(SwitchName.DUTY, False)
        hub.flip(SwitchName.DUTY, True)

        assert len(hub.channel.sent) == 2
        assert hub.channel.sent[1] == hub.channel.sent[0]

        hub.agent.discovery = LaneDiscovery(
            rows=(
                SessionInspection(
                    target=CODEX,
                    workspace=Path("/tmp/workspace"),
                    state=SessionState.RUNNING,
                ),
            )
        )
        asyncio.run(hub.core.discover())
        hub.flip(SwitchName.DUTY, False)
        hub.flip(SwitchName.DUTY, True)

        hub.agent.discovery = LaneDiscovery(rows=(pending,))
        asyncio.run(hub.core.discover())
        hub.flip(SwitchName.DUTY, False)
        hub.flip(SwitchName.DUTY, True)

        assert len(hub.channel.sent) == 3
        assert hub.channel.sent[2] == hub.channel.sent[0]

    def test_a_released_permission_is_reconciled_as_answerable_only_in_the_terminal(
        self,
    ) -> None:
        """The hook ended, so the fresh reading carries no handle (ADR 0015)."""
        hub = Hub(duty=False, voice=False)
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
                    ),
                ),
            )
        )

        asyncio.run(hub.core.discover())

        assert hub.channel.sent == []

        hub.flip(SwitchName.DUTY, True)

        assert len(hub.channel.sent) == 1
        assert "terminal" in hub.channel.sent[0]

    def test_consecutive_outlet_transitions_each_re_announce_the_same_wait(self) -> None:
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

        asyncio.run(hub.core.discover())
        hub.flip(SwitchName.DUTY, True)
        # The second transition, on a roster nothing has moved.
        hub.flip(SwitchName.DUTY, False)
        hub.flip(SwitchName.DUTY, True)

        # Nothing holds this wait, so it is answerable only at the terminal.
        # Each outlet transition reports it again, in the same words: a notice
        # is a report of the current reading, and the reading did not move (#161).
        assert hub.agent.inspections == []
        assert len(hub.channel.sent) == 2
        assert hub.channel.sent[1] == hub.channel.sent[0]
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
        asyncio.run(hub.core.discover())
        hub.flip(SwitchName.DUTY, True)
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
        hub.flip(SwitchName.DUTY, False)
        hub.flip(SwitchName.DUTY, True)
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
        asyncio.run(hub.core.discover())

        hub.flip(SwitchName.DUTY, False)
        hub.flip(SwitchName.DUTY, True)

        assert hub.agent.inspections == []
        assert len(hub.channel.sent) == 2
        assert "Which release?" in hub.channel.sent[-1]

    def test_a_wait_raised_after_the_session_moved_on_is_announced_on_the_next_transition(
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
        asyncio.run(hub.core.discover())
        hub.flip(SwitchName.DUTY, True)
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

        asyncio.run(hub.core.discover())

        hub.flip(SwitchName.DUTY, False)
        hub.flip(SwitchName.DUTY, True)

        assert len(hub.channel.sent) == 2
        assert "Which release?" in hub.channel.sent[-1]

    def test_each_stop_on_one_session_is_announced_in_its_turn(self) -> None:
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

    def test_duty_going_off_while_a_dial_is_in_flight_loses_no_session(self) -> None:
        """The route matrix is gone; what is left is one push and one wake (#195).

        The two outlets are no longer tried in turn, so there is no "between
        routes" left to re-read a switch at: the Companion Channel push is
        adjudicated where it is made, and the call is the Keeper's, which reads
        Duty ∧ Voice at the moment it dials. What this proves is what survives
        the flip — a Session that is still on the roster and a hub that is still
        answering — rather than an ordering that no longer exists.
        """
        hub = Hub()
        hub.call.reachable = False
        original = hub.call.ensure_call

        async def go_off_duty_while_connecting(dial: object) -> object:
            hub.state.switches.flip(SwitchName.DUTY, False)
            return await original(dial)

        hub.call.ensure_call = go_off_duty_while_connecting  # type: ignore[method-assign]

        handled = hub.emit(SessionStopped(target=CODEX))

        assert handled == 1
        assert len(hub.channel.sent) == 1
        assert hub.state.relays.pending() == ()
        assert hub.core.status().sessions


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

        asked = [
            WaitingFor(kind=WaitingKind.QUESTION, prompt="first"),
            WaitingFor(kind=WaitingKind.QUESTION, prompt="second"),
        ]

        hub.emit(*(SessionStopped(target=CODEX, waiting_for=one) for one in asked))

        assert [("  asked: first" in sent) for sent in hub.channel.sent] == [True, False]
        assert "  asked: second" in hub.channel.sent[1]


class TestWhatTheUserHearsAtEachEndOfACall:
    """The two cues that have callers, played from the arms that already existed.

    Bridge Core says which *moment* it is and never which sound; the fake opens
    no device and records the moments in order, which is what lets the ordering
    be graded with no audio anywhere (#186). The Call Keeper takes these calls
    over later (#195), and `EVENT` is the cue that waits for it.
    """

    def test_a_call_coming_up_is_heard(self) -> None:
        hub = Hub()
        started = hub.toggle()
        assert started.call_id is not None

        hub.emit(CallStarted(call_id=started.call_id))

        assert hub.call.cues == [Cue.CONNECTED]

    def test_a_call_ending_as_asked_is_heard(self) -> None:
        hub = Hub()
        started = hub.toggle()
        assert started.call_id is not None
        hub.emit(CallStarted(call_id=started.call_id))

        hub.emit(CallEnded(call_id=started.call_id))

        assert hub.call.cues == [Cue.CONNECTED, Cue.ENDED]

    def test_a_call_that_went_away_by_itself_is_heard_the_same_way(self) -> None:
        """The user is owed the same news either way: what they hear is that the
        call is over, not whose idea it was."""
        hub = Hub()
        started = hub.toggle()
        assert started.call_id is not None
        hub.emit(CallStarted(call_id=started.call_id))

        hub.emit(CallDropped(call_id=started.call_id, detail="the far side left"))

        assert hub.call.cues == [Cue.CONNECTED, Cue.ENDED]

    def test_a_call_that_dropped_the_moment_it_came_up_is_heard_in_order(self) -> None:
        """Both ends of a call inside one cue's wall time, and the order holds.

        Bridge Core asks in order and the adapter keeps that order (#186); this
        is the hub's half of the claim, with no audio and no threads in it.
        """
        hub = Hub()
        started = hub.toggle()
        assert started.call_id is not None

        hub.emit(
            CallStarted(call_id=started.call_id),
            CallDropped(call_id=started.call_id, detail="the far side left"),
        )

        assert hub.call.cues == [Cue.CONNECTED, Cue.ENDED]

    def test_the_mid_call_cue_has_no_caller_yet(self) -> None:
        """`EVENT` ships implemented and unrung. The three sounds were chosen as
        one set (#174), and the Call Keeper is what will use the third (#170)."""
        hub = Hub()
        started = hub.toggle()
        assert started.call_id is not None

        hub.emit(CallStarted(call_id=started.call_id), CallEnded(call_id=started.call_id))
        hub.now += 600.0
        hub.tick()

        assert Cue.EVENT not in hub.call.cues

    def test_a_cue_that_raises_never_stops_the_arm_it_was_asked_from(self) -> None:
        """The interlock still hears about the call. A sound is commentary, and
        commentary may not take down the thing it is commenting on."""
        hub = Hub(call=DeafCall())
        started = hub.toggle()
        assert started.call_id is not None

        assert hub.emit(CallStarted(call_id=started.call_id)) == 1
        assert hub.emit(CallEnded(call_id=started.call_id)) == 1


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


class TestHowAPermissionIsAnnounced:
    """One dialog, one event, one announcement (#191).

    A Session entering `waiting` on a permission raises `SessionStopped` and
    nothing else — both adapters fold the dialog handle into that Stop's wait —
    so the permission is briefed exactly as every other wait is, with no second
    event, no parallel announcement and no reach of its own.

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

    def test_a_dialog_the_lane_still_holds_announces_once_as_a_stop_notice(self) -> None:
        hub = Hub(voice=False)

        hub.emit(SessionStopped(target=CODEX, waiting_for=self.permission("a1")))

        (announced,) = hub.channel.sent
        assert announced.startswith("GPT-VoiceCoding · port the log")
        assert "  permission: Bash — push the branch" in announced

    def test_a_dialog_the_lane_still_holds_is_answerable_from_here(self) -> None:
        """The handle on the row is what `answer_approval` needs, and Briefing says so."""
        hub = Hub(voice=False)

        hub.emit(SessionStopped(target=CODEX, waiting_for=self.permission("a1")))

        (announced,) = hub.channel.sent
        assert "  answer: from here" in announced

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

    The Stop Notice always named its Session; the retired approval announcement
    opened "a session is waiting…" and named only the tool. It cost an acceptance
    run (`20260826T213402Z`), where a stranger's permission prompt was
    indistinguishable from the lane's own. Since #191 there is one notice for a
    permission — the Stop Notice — and Briefing's own header is what names it, so
    the rule holds by construction rather than by a second renderer remembering it.

    Legacy: **ported** from `legacy@1d32845:bridge/host.py:213-235`, which
    rendered `Session: {session_label}` above "This session is waiting for
    permission."
    """

    #: The one target the Hub does not register, so the brief has no name to use
    #: and the address floor is what the notice has to fall back on.
    STRANGER = SessionTarget(agent=AgentKind.CODEX, session_id="not-in-the-roster")

    def permission(self, detail: str = "") -> WaitingFor:
        return WaitingFor(
            kind=WaitingKind.PERMISSION,
            tool_name="Bash",
            detail=detail,
            approval_id="a1",
        )

    def test_the_permission_notice_names_the_session_the_way_the_user_does(self) -> None:
        hub = Hub(voice=False)

        hub.emit(SessionStopped(target=CODEX, waiting_for=self.permission()))

        (announcement,) = hub.channel.sent
        assert announcement.startswith("GPT-VoiceCoding · port the log")

    def test_both_notices_call_one_session_the_same_thing(self) -> None:
        """One answer to "what is this called", or the user hears two Sessions."""
        hub = Hub(voice=False)

        hub.emit(SessionStopped(target=CODEX, waiting_for=self.permission()))
        hub.emit(SessionStopped(target=CODEX))

        announcement, stop_notice = hub.channel.sent
        called = "GPT-VoiceCoding · port the log"
        assert announcement.startswith(called) and stop_notice.startswith(called)

    def test_a_session_the_roster_does_not_hold_is_named_by_its_address(self) -> None:
        """The floor a brief's header falls back to — never "a session"."""
        hub = Hub(voice=False)

        hub.emit(SessionStopped(target=self.STRANGER, waiting_for=self.permission()))

        (announcement,) = hub.channel.sent
        assert announcement.startswith("codex:not-in-the-roster")

    def test_the_detail_still_travels_beside_the_tool(self) -> None:
        hub = Hub(voice=False)

        hub.emit(SessionStopped(target=CODEX, waiting_for=self.permission("push the branch")))

        (announcement,) = hub.channel.sent
        assert "  permission: Bash — push the branch" in announcement


class TestMidCallNewsThroughTheWholeHub:
    """#196, end to end: the Focus Session is spoken to, the rest rings.

    The Keeper's own tests prove the timing on a fake Briefer. What is proved
    here is the half only a hub has: that the Focus Session's brief the Keeper
    speaks is the one `RosterBriefer` reads off the roster at that instant, and
    that a Session which is not the Focus one earns the EVENT cue and no words.
    """

    OTHER = SessionTarget(agent=AgentKind.CLAUDE, session_id="def", pid=100)

    def hub_on_a_call(self, **kwargs: object) -> Hub:
        hub = Hub(  # type: ignore[arg-type]
            sessions=((CODEX, "port the log"), (self.OTHER, "the other one")),
            **kwargs,
        )
        hub.toggle()
        hub.state.sessions.set_focus(CODEX)
        return hub

    def gap(self, hub: Hub) -> None:
        hub.now += 5.0
        hub.tick()

    def test_the_focus_session_is_spoken_about_in_the_first_gap(self) -> None:
        hub = self.hub_on_a_call()

        hub.emit(
            SessionStopped(
                target=CODEX,
                waiting_for=WaitingFor(kind=WaitingKind.QUESTION, prompt="Which base?"),
            )
        )
        assert hub.call.spoken == [], "the settle window had not run out"

        self.gap(hub)

        (spoken,) = hub.call.spoken
        assert "port the log" in spoken.name
        assert "Which base?" in " ".join(spoken.decision)
        assert hub.call.calls_started == 1, "mid-call news never dials a second call"

    def test_the_brief_spoken_is_the_reading_taken_at_the_gap(self) -> None:
        """ADR 0017 mid-call: the Session answered in between is not announced."""
        hub = self.hub_on_a_call()

        hub.emit(
            SessionStopped(
                target=CODEX,
                waiting_for=WaitingFor(kind=WaitingKind.QUESTION, prompt="Which base?"),
            )
        )
        hub.agent.discovery = LaneDiscovery(
            rows=(
                SessionInspection(
                    target=CODEX,
                    workspace=Path("/tmp/workspace"),
                    state=SessionState.RUNNING,
                ),
            )
        )
        asyncio.run(hub.core.discover())
        self.gap(hub)

        assert hub.call.spoken == []

    def test_another_session_rings_and_is_not_spoken_about(self) -> None:
        hub = self.hub_on_a_call()

        hub.emit(SessionStopped(target=self.OTHER))
        self.gap(hub)

        assert hub.call.spoken == []
        assert hub.call.cues.count(Cue.EVENT) == 1

    def test_the_focus_session_ending_clears_what_was_owed_to_it(self) -> None:
        """#196's last test: a word owed to a Session that has gone is owed to nobody.

        Cleared by the *reading* and not by a second entry on the Keeper: the
        row is gone, so `focus_brief` answers `None` at the gap and the flag goes
        with the answer. ADR 0017 all the way down — the Keeper holds no target,
        it asks who the Focus Session is at the moment of sounding.
        """
        hub = self.hub_on_a_call()

        hub.emit(
            SessionStopped(
                target=CODEX,
                waiting_for=WaitingFor(kind=WaitingKind.QUESTION, prompt="Which base?"),
            )
        )
        hub.agent.discovery = LaneDiscovery(rows=())
        asyncio.run(hub.core.discover())
        self.gap(hub)

        assert hub.call.spoken == []
        assert hub.call.calls_started == 1


class TestARelayThatFinallyFailedReachesTheUser:
    """#197, end to end: the reason travels as a brief field, through the Keeper.

    Bridge Core folds one field onto the Session's row and wakes the Keeper with
    the Focus judged *now* — a relay can pass its ceiling minutes after it was
    queued, and the user may have answered another Session since (ADR 0017).
    """

    OTHER = SessionTarget(agent=AgentKind.CLAUDE, session_id="def", pid=100)

    def waiting(self, hub: Hub, target: SessionTarget) -> None:
        """Put a stopped, question-shaped reading on that row, as a Stop would."""
        hub.state.sessions.set_stop_reading(
            target,
            waiting_for=WaitingFor(kind=WaitingKind.QUESTION, prompt="Which base?"),
            progress=ProgressObservation.readable(
                has_history=True,
                read_at=datetime(2026, 9, 3, 1, 2, 3, tzinfo=UTC),
                recent=(ProgressEntry(ordinal=0, role=ProgressRole.ASSISTANT, text="I got here"),),
            ),
            now=hub.now,
        )

    def failed_relay(self, hub: Hub, target: SessionTarget) -> None:
        """The user's words, attempted and proven not to have arrived."""
        hub.agent.answerable_questions.add(target)
        hub.agent.outcome = Delivery.FAILED
        hub.agent.reason = "the far side refused"
        asyncio.run(hub.core.relay(target, "main"))

    def test_a_failure_with_no_call_up_dials_and_hands_the_reason_over(self) -> None:
        hub = Hub()
        self.waiting(hub, CODEX)
        self.failed_relay(hub, CODEX)

        hub.now += TEN_MINUTES
        hub.tick()

        assert hub.call.calls_started == 1
        briefs = [item for item in hub.call.opened_on[0].hand_over if isinstance(item, SpokenBrief)]
        assert [brief.undelivered for brief in briefs] == [
            "your last reply did not arrive, because ceiling_passed"
        ]

    def test_a_failure_under_cool_down_owes_a_dial_rather_than_dialling_now(self) -> None:
        hub = Hub(cool_down_seconds=3_600.0)
        hub.toggle()
        hub.toggle()  # the call the user opened ends, and a Cool-down begins
        self.waiting(hub, CODEX)
        self.failed_relay(hub, CODEX)
        dialled = hub.call.calls_started

        hub.now += TEN_MINUTES
        hub.tick()

        assert hub.call.calls_started == dialled, "the Cool-down had not elapsed"
        assert hub.core.status().dial_owed

    def hub_on_a_call(self) -> Hub:
        hub = Hub(
            sessions=((CODEX, "port the log"), (self.OTHER, "the other one")),
            silence_end_seconds=3_600.0,
        )
        hub.toggle()
        return hub

    def gap(self, hub: Hub) -> None:
        hub.now += 5.0
        hub.tick()

    def test_a_failure_mid_call_is_spoken_at_the_gap_as_the_focus_brief(self) -> None:
        hub = self.hub_on_a_call()
        self.waiting(hub, CODEX)
        self.failed_relay(hub, CODEX)

        hub.now += TEN_MINUTES
        hub.tick()
        self.gap(hub)

        (spoken,) = hub.call.spoken
        assert "port the log" in spoken.name
        assert spoken.undelivered == "your last reply did not arrive, because ceiling_passed"

    def test_a_session_the_user_has_since_left_only_rings(self) -> None:
        """`focus` is judged at the wake, not when the words were queued."""
        hub = self.hub_on_a_call()
        self.waiting(hub, self.OTHER)
        self.failed_relay(hub, self.OTHER)
        self.waiting(hub, CODEX)
        hub.state.sessions.set_focus(CODEX)

        hub.now += TEN_MINUTES
        hub.tick()
        self.gap(hub)

        assert hub.call.spoken == []
        assert hub.call.cues.count(Cue.EVENT) == 1
        assert hub.state.sessions.resolve(self.OTHER).undelivered is not None

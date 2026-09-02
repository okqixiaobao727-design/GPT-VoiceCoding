"""Briefing — the one source of words about Session state.

The behaviours under test are the five states, the Focus Session's place in a
Roster Brief, and the rule that `text` carries every field: the engine hands
facts whole and never condenses, so a brief that dropped a field would be the
engine deciding what the user is told (#166).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from gpt_voicecoding.core import briefing
from gpt_voicecoding.core.briefing import BriefState, Newest, NewestState
from gpt_voicecoding.core.sessions import Session
from gpt_voicecoding.seams.agent import (
    SANDBOX_TOOL_NAME,
    ChildClassification,
    ChildKind,
    Option,
    ProgressEntry,
    ProgressObservation,
    ProgressOmission,
    ProgressRole,
    SessionLifecycle,
    SessionState,
    WaitingFor,
    WaitingKind,
)
from gpt_voicecoding.seams.identity import AgentKind, SessionName, SessionTarget

WORKSPACE = Path(__file__).resolve().parents[1]
READ_AT = datetime(2026, 9, 2, 3, 4, 5, tzinfo=UTC)

CLAUDE = SessionTarget(agent=AgentKind.CLAUDE, session_id="abc", pid=1234)
CODEX = SessionTarget(agent=AgentKind.CODEX, session_id="def")


def said(text: str) -> ProgressObservation:
    return ProgressObservation.readable(
        has_history=True,
        read_at=READ_AT,
        recent=(ProgressEntry(role=ProgressRole.ASSISTANT, text=text),),
    )


def row(
    target: SessionTarget = CLAUDE,
    *,
    state: SessionState = SessionState.IDLE,
    waiting_for: WaitingFor | None = None,
    progress: ProgressObservation | None = None,
    name: SessionName | None = None,
    child: ChildClassification | None = None,
    last_activity: datetime | None = READ_AT,
) -> Session:
    return Session(
        target=target,
        workspace=WORKSPACE,
        first_seen=0.0,
        name=name if name is not None else SessionName(project="gpt-voicecoding", task="a task"),
        state=state,
        waiting_for=waiting_for or WaitingFor(),
        progress=progress if progress is not None else said("done"),
        last_activity=last_activity,
        child=child or ChildClassification(),
    )


QUESTION = WaitingFor(
    kind=WaitingKind.QUESTION,
    prompt="Which base?",
    options=(
        Option(text="main", description="the default branch", recommended=True),
        Option(text="develop"),
    ),
    recommendation="main",
)
PERMISSION = WaitingFor(
    kind=WaitingKind.PERMISSION,
    tool_name="Bash",
    detail="rm -rf build",
    approval_id="ap-1",
)


class TestTheFiveStates:
    def test_a_question_wait_is_a_decision(self) -> None:
        brief = briefing.session(row(state=SessionState.WAITING, waiting_for=QUESTION))
        assert brief.state is BriefState.DECISION

    def test_a_permission_wait_is_a_permission(self) -> None:
        brief = briefing.session(row(state=SessionState.WAITING, waiting_for=PERMISSION))
        assert brief.state is BriefState.PERMISSION

    def test_a_stopped_claude_session_with_no_wait_is_finished(self) -> None:
        """Legacy's "finished its turn and is waiting for the user", ported."""
        assert briefing.session(row()).state is BriefState.FINISHED

    def test_a_running_session_is_running(self) -> None:
        assert briefing.session(row(state=SessionState.RUNNING)).state is BriefState.RUNNING

    def test_a_running_session_whose_progress_is_unreadable_stays_running(self) -> None:
        """The state is the lifecycle's; the read only fills the fields."""
        brief = briefing.session(
            row(
                state=SessionState.RUNNING,
                progress=ProgressObservation.unreadable("no daemon holds it"),
            )
        )
        assert brief.state is BriefState.RUNNING
        assert brief.newest.state is NewestState.UNREADABLE

    def test_a_stopped_session_whose_wait_is_unclassified_is_unreadable(self) -> None:
        brief = briefing.session(
            row(
                state=SessionState.WAITING,
                waiting_for=WaitingFor(kind=WaitingKind.UNKNOWN, caught_up=False),
            )
        )
        assert brief.state is BriefState.UNREADABLE

    def test_a_stopped_session_whose_progress_is_unreadable_is_unreadable(self) -> None:
        brief = briefing.session(
            row(progress=ProgressObservation.unreadable("the transcript could not be read"))
        )
        assert brief.state is BriefState.UNREADABLE

    def test_a_codex_turn_end_is_a_decision_until_the_finished_heuristic(self) -> None:
        """#166 B2: DECISION by default; #188 adds the promotion to FINISHED."""
        assert briefing.session(row(CODEX)).state is BriefState.DECISION

    def test_an_exited_session_never_appears_in_a_roster_brief(self) -> None:
        ended = replace(row(), lifecycle=SessionLifecycle.ENDED)
        brief = briefing.roster((ended,), focus=None)
        assert brief.rows == ()
        assert sum(brief.counts.values()) == 0


class TestWhatADecisionCarries:
    def test_a_decision_carries_its_options_and_the_recommendation(self) -> None:
        """Legacy's single recommendation, ported into `decision.recommendation`."""
        decision = briefing.session(row(state=SessionState.WAITING, waiting_for=QUESTION)).decision
        assert decision is not None
        assert decision.prompt == "Which base?"
        assert [(one.text, one.description, one.recommended) for one in decision.options] == [
            ("main", "the default branch", True),
            ("develop", None, False),
        ]
        assert decision.recommendation == "main"

    def test_a_permission_carries_the_tool_and_a_one_line_summary(self) -> None:
        decision = briefing.session(
            row(state=SessionState.WAITING, waiting_for=PERMISSION)
        ).decision
        assert decision is not None
        assert (decision.tool, decision.summary) == ("Bash", "rm -rf build")

    def test_a_sandbox_request_is_named_by_the_seam_s_own_wording(self) -> None:
        """The wait whose tool has no name still names something the user can act on."""
        brief = briefing.session(
            row(
                state=SessionState.WAITING,
                waiting_for=WaitingFor(kind=WaitingKind.PERMISSION, tool_name=SANDBOX_TOOL_NAME),
            )
        )
        assert brief.decision is not None
        assert brief.decision.tool == "sandbox network access"
        assert "sandbox network access" in briefing.text(brief)

    def test_an_unreadable_session_keeps_whatever_was_read(self) -> None:
        """B7: never counted as a decision, and never emptied either."""
        brief = briefing.session(
            row(
                state=SessionState.WAITING,
                waiting_for=WaitingFor(kind=WaitingKind.UNKNOWN, caught_up=False),
                progress=said("halfway through"),
            )
        )
        assert brief.state is BriefState.UNREADABLE
        assert brief.newest == Newest(state=NewestState.SAID, text="halfway through")


class TestAnswerableHere:
    def test_a_question_is_answerable_only_when_the_lane_still_holds_the_route(self) -> None:
        waiting = row(state=SessionState.WAITING, waiting_for=QUESTION)
        assert briefing.session(waiting, question_answerable=True).answerable_here is True
        assert briefing.session(waiting, question_answerable=False).answerable_here is False

    def test_a_permission_is_answerable_only_while_a_handle_holds_it_open(self) -> None:
        held = row(state=SessionState.WAITING, waiting_for=PERMISSION)
        released = row(
            state=SessionState.WAITING,
            waiting_for=WaitingFor(kind=WaitingKind.PERMISSION, tool_name="Bash"),
        )
        assert briefing.session(held).answerable_here is True
        assert briefing.session(released).answerable_here is False

    def test_a_running_session_takes_no_reply(self) -> None:
        assert briefing.session(row(state=SessionState.RUNNING)).answerable_here is False


class TestNewest:
    def test_the_newest_assistant_message_travels_whole(self) -> None:
        """The engine never condenses: the conclusion and the detail are one field."""
        whole = "I rebuilt the index and every test passes. " * 20
        brief = briefing.session(row(progress=said(whole)))
        assert brief.newest == Newest(state=NewestState.SAID, text=whole)

    def test_a_session_that_has_said_nothing_says_so(self) -> None:
        brief = briefing.session(
            row(progress=ProgressObservation.readable(has_history=False, read_at=READ_AT))
        )
        assert brief.newest.state is NewestState.NOTHING_SAID
        assert "nothing said yet" in briefing.text(brief)

    def test_an_oversize_newest_entry_is_named_rather_than_dropped(self) -> None:
        brief = briefing.session(
            row(
                progress=ProgressObservation.readable(
                    has_history=True,
                    read_at=READ_AT,
                    omission=ProgressOmission.NEWEST_OVERSIZE,
                )
            )
        )
        assert brief.newest.state is NewestState.OVERSIZE
        assert "too large to carry" in briefing.text(brief)

    def test_nobody_having_looked_is_not_the_same_as_having_failed_to_read(self) -> None:
        assert (
            briefing.session(row(progress=ProgressObservation())).newest.state
            is NewestState.NOT_READ
        )
        assert (
            briefing.session(row(progress=ProgressObservation.unreadable("gone"))).newest.state
            is NewestState.UNREADABLE
        )


class TestTheRosterBrief:
    def test_the_focus_session_comes_first_and_the_counts_are_the_others(self) -> None:
        """Q6: the Roster Brief says how many are running, and counts the others."""
        focus = row(CODEX, state=SessionState.WAITING, waiting_for=QUESTION)
        others = (
            row(CLAUDE, state=SessionState.RUNNING),
            row(
                SessionTarget(agent=AgentKind.CLAUDE, session_id="ghi", pid=99),
                state=SessionState.WAITING,
                waiting_for=PERMISSION,
            ),
        )
        brief = briefing.roster((*others, focus), focus=CODEX)
        assert [one.target for one in brief.rows] == [CODEX, CLAUDE, others[1].target]
        assert brief.rows[0].focus is True
        assert brief.counts == {BriefState.RUNNING: 1, BriefState.PERMISSION: 1}

    def test_with_no_focus_the_counts_are_every_live_session(self) -> None:
        brief = briefing.roster((row(), row(CODEX, state=SessionState.RUNNING)), focus=None)
        assert brief.counts == {BriefState.FINISHED: 1, BriefState.RUNNING: 1}
        assert brief.focus is None

    def test_a_header_row_carries_the_name_the_agent_and_the_state(self) -> None:
        header = briefing.roster((row(),), focus=None).rows[0]
        assert header.name == SessionName(project="gpt-voicecoding", task="a task")
        assert header.agent is AgentKind.CLAUDE
        assert header.state is BriefState.FINISHED

    def test_a_child_process_is_not_a_row_the_voice_can_ask_about(self) -> None:
        """Seen, never spoken to: every row must be one `brief <address>` answers."""
        child = row(
            CODEX,
            child=ChildClassification(kind=ChildKind.CHILD, parent=CLAUDE),
            name=None,
        )
        assert briefing.roster((row(), child), focus=None).rows == (
            briefing.roster((row(),), focus=None).rows[0],
        )

    def test_an_empty_roster_says_so(self) -> None:
        assert "none" in briefing.text(briefing.roster((), focus=None))


class TestText:
    def test_the_only_renderer_carries_every_field_a_session_brief_holds(self) -> None:
        brief = briefing.session(
            row(state=SessionState.WAITING, waiting_for=QUESTION, progress=said("I got this far")),
            question_answerable=True,
        )
        rendered = briefing.text(brief)
        assert "gpt-voicecoding · a task" in rendered
        assert "claude:abc:1234" in rendered
        assert "waiting for your decision" in rendered
        assert "I got this far" in rendered
        assert "Which base?" in rendered
        assert "the default branch" in rendered
        assert "develop" in rendered
        assert "main" in rendered
        assert READ_AT.isoformat() in rendered

    def test_a_session_with_no_name_is_rendered_by_its_address(self) -> None:
        rendered = briefing.text(briefing.session(row(name=None)))
        assert "claude:abc:1234" in rendered

    def test_the_roster_brief_renders_the_counts_and_every_header_row(self) -> None:
        rendered = briefing.text(
            briefing.roster(
                (row(), row(CODEX, state=SessionState.WAITING, waiting_for=PERMISSION)),
                focus=CODEX,
            )
        )
        assert "gpt-voicecoding · a task" in rendered
        assert "codex:def" in rendered
        assert "requesting permission" in rendered
        assert "finished" in rendered

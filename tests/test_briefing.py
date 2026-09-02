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

    def test_a_codex_turn_end_whose_answer_carries_no_phase_is_a_decision(self) -> None:
        """#166 B2's default, and #188's fallback: no `phase`, no promotion."""
        assert briefing.session(row(CODEX)).state is BriefState.DECISION

    def test_an_exited_session_never_appears_in_a_roster_brief(self) -> None:
        ended = replace(row(), lifecycle=SessionLifecycle.ENDED)
        brief = briefing.roster((ended,), focus=None)
        assert brief.rows == ()
        assert sum(brief.counts.values()) == 0


FINAL_ANSWER = "final_answer"
COMMENTARY = "commentary"


def codex_said(*said: tuple[str, str | None]) -> ProgressObservation:
    """A Codex reading: what the agent said, each with the `phase` it carried."""
    return ProgressObservation.readable(
        has_history=True,
        read_at=READ_AT,
        recent=tuple(
            ProgressEntry(role=ProgressRole.ASSISTANT, text=text, phase=phase)
            for text, phase in said
        ),
    )


def told(text: str) -> ProgressEntry:
    """What the Session was told — and, in a tail, where a turn begins."""
    return ProgressEntry(role=ProgressRole.USER, text=text)


def codex_tail(*entries: ProgressEntry) -> ProgressObservation:
    """A reading of both sides, in the order they happened."""
    return ProgressObservation.readable(has_history=True, read_at=READ_AT, recent=entries)


def answer(text: str, phase: str | None = FINAL_ANSWER) -> ProgressEntry:
    return ProgressEntry(role=ProgressRole.ASSISTANT, text=text, phase=phase)


def codex_state(*said: tuple[str, str | None]) -> BriefState:
    return briefing.session(row(CODEX, progress=codex_said(*said))).state


def answered(text: str) -> BriefState:
    return codex_state((text, FINAL_ANSWER))


class TestACodexTurnThatAskedNothing:
    """#188: FINISHED only when the final answer shows no sign of an ask.

    A promotion gate, not a classifier: the default stays #166 B2's DECISION and
    evidence is required to leave it, so every uncertain shape below stays a
    decision. The rule and its measurements are #176
    (`docs/research/2026-09-01-codex-turn-end-classification.md` §5).
    """

    def test_a_final_answer_that_reports_a_result_is_finished(self) -> None:
        assert answered("已完成并提交 1a15cb0，工作树干净。") is BriefState.FINISHED

    def test_a_final_answer_that_asks_is_a_decision(self) -> None:
        assert answered("我建议按方案处理。同意吗?") is BriefState.DECISION

    def test_a_full_width_question_mark_asks_too(self) -> None:
        """The corpus is 98% Chinese, so `？` is the common spelling of an ask."""
        assert answered("你是否拍板按上述方案处理 #140 和 #141？") is BriefState.DECISION

    def test_a_question_mark_the_user_never_saw_as_one_does_not_ask(self) -> None:
        """A fenced block is machinery being shown, not a question being put."""
        said = "已完成。\n\n```sh\ngit status --short   # 是否还有残留?\n```\n"
        assert answered(said) is BriefState.FINISHED

    def test_a_question_mark_inside_an_inline_code_span_does_not_ask(self) -> None:
        """The measured false positive `A → B` removes: a literal `??`."""
        assert answered("`git status --short` 仅显示 `?? uv.lock`。") is BriefState.FINISHED

    def test_a_question_mark_in_a_link_target_does_not_ask(self) -> None:
        """The target is an address; only the label is words the user read."""
        said = "已完成，见 [运行记录](https://ci.example/runs?id=7)。"
        assert answered(said) is BriefState.FINISHED

    def test_a_labelled_option_block_asks_without_a_question_mark(self) -> None:
        """`B → C`: a menu is an ask even when the interrogative is missing."""
        said = "两条路:\n\nA) 保留当前实现\nB) 换成统一观察器\nC) 全部回退\n"
        assert codex_state((said, FINAL_ANSWER)) is BriefState.DECISION

    def test_a_named_option_asks(self) -> None:
        assert answered("➡️ 我建议采用方案 B。") is BriefState.DECISION
        assert answered("选项 A 最省事。") is BriefState.DECISION

    def test_a_numbered_list_is_not_a_menu(self) -> None:
        """No numeric clause, deliberately: numbered lists are how findings are
        enumerated, which is the most common *done* shape in the corpus
        (`codex-rs/core/gpt_5_codex_prompt.md:47`, #176 §3)."""
        said = "未发现 Spec findings。\n\n1. 读了票\n2. 读了 diff\n3. 跑了测试\n"
        assert codex_state((said, FINAL_ANSWER)) is BriefState.FINISHED

    def test_the_answer_is_classified_even_when_commentary_came_after_it(self) -> None:
        """3 of 669 turns end on commentary; the answer before it is the answer."""
        assert (
            codex_state(("同意吗？", FINAL_ANSWER), ("正在收尾…", COMMENTARY))
            is BriefState.DECISION
        )
        assert (
            codex_state(("已完成。", FINAL_ANSWER), ("正在收尾…", COMMENTARY))
            is BriefState.FINISHED
        )

    def test_commentary_alone_is_never_promoted(self) -> None:
        """A non-blocking mid-turn question is not the turn's answer, and a turn
        with no answer at all is not evidence that it finished."""
        assert codex_state(("先看一下目录。", COMMENTARY)) is BriefState.DECISION

    def test_the_answer_to_an_earlier_turn_is_not_this_turn_s_answer(self) -> None:
        """The tail carries several turns, and only this one ended (or did not).

        Without the boundary a turn still working — or one that produced only
        commentary — would be briefed FINISHED on the *previous* turn's answer,
        which is the ticket's "no final answer → DECISION" turned inside out.
        """
        working = codex_tail(
            told("do the first thing"),
            answer("已完成第一件事。"),
            told("now do the second"),
            answer("先看一下目录。", COMMENTARY),
        )
        assert briefing.session(row(CODEX, progress=working)).state is BriefState.DECISION

    def test_a_turn_that_has_said_nothing_yet_is_not_the_turn_before_it(self) -> None:
        silent = codex_tail(told("do the thing"), answer("已完成。"), told("now do the next"))
        assert briefing.session(row(CODEX, progress=silent)).state is BriefState.DECISION

    def test_this_turn_s_answer_is_read_across_the_boundary_behind_it(self) -> None:
        """The boundary stops the search; it does not stop this turn being read."""
        done = codex_tail(
            told("do the first thing"),
            answer("同意吗？"),
            told("now do the second"),
            answer("已完成第二件事。"),
        )
        assert briefing.session(row(CODEX, progress=done)).state is BriefState.FINISHED

    def test_a_link_target_that_holds_brackets_is_still_a_target(self) -> None:
        """A URL may carry balanced parentheses, and cutting at the first one
        leaves the query string in the prose (`?run=7` reads as an ask)."""
        said = "已完成，见 [运行记录](https://ci.example/path_(part)?run=7)。"
        assert answered(said) is BriefState.FINISHED

    def test_a_phase_this_build_cannot_read_is_not_an_answer(self) -> None:
        assert codex_state(("已完成。", "somethingNewIn0152")) is BriefState.DECISION

    def test_a_session_nobody_read_stays_a_decision(self) -> None:
        assert (
            briefing.session(row(CODEX, progress=ProgressObservation())).state
            is BriefState.DECISION
        )

    def test_the_claude_lane_is_untouched(self) -> None:
        """Claude's question is structural, so its turn end is FINISHED whatever
        the words were (`legacy@1d32845:bridge/host.py:226-234`)."""
        finished = briefing.session(row(CLAUDE, progress=codex_said(("同意吗？", FINAL_ANSWER))))
        assert finished.state is BriefState.FINISHED

    def test_a_promotion_never_outranks_a_question_the_lane_reported(self) -> None:
        """The heuristic reads a turn that stopped on nothing; a typed wait wins."""
        brief = briefing.session(
            row(
                CODEX,
                state=SessionState.WAITING,
                waiting_for=QUESTION,
                progress=codex_said(("已完成。", FINAL_ANSWER)),
            )
        )
        assert brief.state is BriefState.DECISION


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

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
    ProgressPhase,
    ProgressRole,
    SessionLifecycle,
    SessionState,
    WaitingFor,
    WaitingKind,
)
from gpt_voicecoding.seams.call import (
    HANDOVER_BUDGET_BYTES,
    MAX_HANDOVER_ITEMS,
    Dial,
    DialReason,
    SpokenBrief,
    SpokenRosterBrief,
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
        recent=(ProgressEntry(ordinal=0, role=ProgressRole.ASSISTANT, text=text),),
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


FINAL_ANSWER = ProgressPhase.FINAL_ANSWER
COMMENTARY = ProgressPhase.COMMENTARY


def codex_said(*said: tuple[str, ProgressPhase | None]) -> ProgressObservation:
    """A Codex reading: what the agent said, each with the `phase` it carried."""
    return ProgressObservation.readable(
        has_history=True,
        read_at=READ_AT,
        recent=tuple(
            ProgressEntry(ordinal=0, role=ProgressRole.ASSISTANT, text=text, phase=phase)
            for text, phase in said
        ),
    )


def told(text: str, *, turn_id: str | None = None) -> ProgressEntry:
    """What the Session was told — and, in a tail, where a turn begins."""
    return ProgressEntry(ordinal=0, role=ProgressRole.USER, text=text, turn_id=turn_id)


def codex_tail(*entries: ProgressEntry) -> ProgressObservation:
    """A reading of both sides, in the order they happened."""
    return ProgressObservation.readable(has_history=True, read_at=READ_AT, recent=entries)


def answer(
    text: str, phase: ProgressPhase | None = FINAL_ANSWER, *, turn_id: str | None = None
) -> ProgressEntry:
    return ProgressEntry(
        ordinal=0, role=ProgressRole.ASSISTANT, text=text, phase=phase, turn_id=turn_id
    )


def codex_state(*said: tuple[str, ProgressPhase | None]) -> BriefState:
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

        These three name no turn, which is the **fallback** rule (#210): a
        Claude reading, or a Codex build whose turns carried no `id`, still
        stops at the newest thing the user said. The three below them are the
        same three boundaries when the source did name its turns.
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

    def test_a_turn_opened_by_an_image_alone_is_not_the_turn_before_it(self) -> None:
        """#210: a `userMessage` carrying only an image leaves no entry, so the
        boundary the search stops at is the turn each entry names, not the
        newest thing the user said. Without it this reads the previous turn's
        answer and briefs FINISHED where the turn has produced only commentary.
        """
        wordless = codex_tail(
            told("do the first thing", turn_id="turn_one"),
            answer("Done. All tests pass.", turn_id="turn_one"),
            answer("先看一下目录。", COMMENTARY, turn_id="turn_two"),
        )
        assert briefing.session(row(CODEX, progress=wordless)).state is BriefState.DECISION

    def test_a_turn_named_by_the_source_bounds_the_search_at_both_ends(self) -> None:
        """The named boundary stops the search; it does not stop this turn being
        read, and an entry the user put mid-turn does not end it either."""
        done = codex_tail(
            told("do the first thing", turn_id="turn_one"),
            answer("同意吗？", turn_id="turn_one"),
            told("now do the second", turn_id="turn_two"),
            answer("已完成第二件事。", turn_id="turn_two"),
        )
        assert briefing.session(row(CODEX, progress=done)).state is BriefState.FINISHED

    def test_a_link_target_that_holds_brackets_is_still_a_target(self) -> None:
        """A URL may carry balanced parentheses, and cutting at the first one
        leaves the query string in the prose (`?run=7` reads as an ask)."""
        said = "已完成，见 [运行记录](https://ci.example/path_(part)?run=7)。"
        assert answered(said) is BriefState.FINISHED

    def test_a_phase_this_build_cannot_read_is_not_an_answer(self) -> None:
        """The adapter maps an unrecognised codex word to `UNKNOWN`, and this is
        what that member means here: the turn did not end on its answer."""
        assert codex_state(("已完成。", ProgressPhase.UNKNOWN)) is BriefState.DECISION

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


class TestTheSpokenBrief:
    """One Session Brief as the Call seam carries it — this module's words, as data.

    A Core type may not cross a seam (ADR 0001), so what crosses is the seam's
    own carrier. What it must *not* become is a second vocabulary: the adapter on
    the far side assembles these strings and chooses none of them, so every one
    of them is filled from the same tables `text` prints from.
    """

    def test_every_field_is_a_word_this_module_chose(self) -> None:
        brief = briefing.session(
            row(state=SessionState.WAITING, waiting_for=QUESTION, progress=said("I got this far")),
            question_answerable=True,
        )

        spoken = briefing.spoken(brief)

        assert spoken.name == "gpt-voicecoding · a task"
        assert spoken.agent == "claude"
        assert spoken.state == "waiting for your decision"
        assert spoken.newest == "I got this far"
        assert spoken.decision == (
            "asked: Which base?",
            "option: main — the default branch (recommended)",
            "option: develop",
            "recommends: main",
        )
        assert spoken.answerable_here == "from here"
        assert spoken.last_activity_at == READ_AT.isoformat()

    def test_a_session_with_no_name_is_carried_by_its_address(self) -> None:
        """The rule `_headline` follows: the address only where there is no name."""
        spoken = briefing.spoken(briefing.session(replace(row(), name=None)))

        assert spoken.name == str(CLAUDE)

    def test_an_omitted_newest_carries_the_reason_and_not_a_blank(self) -> None:
        brief = briefing.omitting_newest(briefing.session(row(progress=said("a long answer"))))

        assert briefing.spoken(brief).newest == "the newest entry is too large to carry"

    def test_a_running_session_carries_no_decision_lines(self) -> None:
        assert briefing.spoken(briefing.session(row(state=SessionState.RUNNING))).decision == ()


class TestTheHandover:
    """What a system-dialled call comes up already holding (#194, ADR 0018).

    Three kinds of item in one order: why the call was dialled, the roster, then
    the Sessions that need the user. The wire refuses an over-budget or
    over-count request outright rather than truncating it, so what is asserted
    here is that this function gives things back in the right order and never
    hands `Dial` something it would refuse.
    """

    def test_the_first_item_says_why_the_call_was_dialled(self) -> None:
        items = briefing.handover((row(),), focus=None, reason="Sessions need you")

        assert items[0] == DialReason(text="Sessions need you")
        assert isinstance(items[1], SpokenRosterBrief)

    def test_a_running_session_gets_a_header_row_and_no_brief(self) -> None:
        items = briefing.handover(
            (row(CODEX, state=SessionState.RUNNING),), focus=None, reason="dialled"
        )

        summary = items[1]
        assert isinstance(summary, SpokenRosterBrief)
        assert summary.rows == ("gpt-voicecoding · a task — codex:def — running",)
        assert [item for item in items if isinstance(item, SpokenBrief)] == []

    def test_the_focus_session_is_briefed_first(self) -> None:
        focus = row(CODEX, state=SessionState.WAITING, waiting_for=QUESTION)
        other = row(CLAUDE, state=SessionState.WAITING, waiting_for=PERMISSION)

        items = briefing.handover((other, focus), focus=CODEX, reason="dialled")

        briefs = [item for item in items if isinstance(item, SpokenBrief)]
        assert [item.state for item in briefs] == [
            "waiting for your decision",
            "requesting permission",
        ]

    def test_which_sessions_are_briefed_is_the_rosters_answer_alone(self) -> None:
        """No caller may name one *into the list*, whatever it thinks it knows.

        An earlier draft took the Session the call was dialled about and briefed
        it ahead of the Focus Session whatever the row said. That put a `running`
        brief inside the list of Sessions needing the user, because
        `sessions.set_stop_reading` then left a row `RUNNING` unless the wait
        needed the user — a third module's staleness papered over here, at the
        cost of two of this function's own rules.

        There is no way back in. The row a caller used to compensate for — the
        Session a Stop dialled the call about — now says it stopped
        (`sessions.set_stop_reading`, #213), so the roster briefs it itself and
        the tests below are what say so.
        """
        running = row(CODEX, state=SessionState.RUNNING, progress=said("it stopped here"))

        items = briefing.handover((running,), focus=None, reason="dialled")

        assert [item for item in items if isinstance(item, SpokenBrief)] == []

    def test_a_question_is_answerable_here_only_when_the_lane_says_so(self) -> None:
        waiting = row(CODEX, state=SessionState.WAITING, waiting_for=QUESTION)

        without = briefing.handover((waiting,), focus=None, reason="dialled")
        with_route = briefing.handover(
            (waiting,), focus=None, reason="dialled", answerable=(CODEX,)
        )

        assert _only_brief(without).answerable_here == "at the terminal"
        assert _only_brief(with_route).answerable_here == "from here"

    def test_a_decision_is_never_given_up_to_keep_a_header_row(self) -> None:
        """The ladder's own order, on the case that exposed the wrong one.

        Two hundred waiting Sessions: the roster's rows alone are over budget, so
        something has to go. Giving up briefs first produced one hundred and
        fifty-four names and not one decision — a hand-over that told the user
        which Sessions exist and nothing about what any of them is asking.
        """
        sessions = tuple(
            row(
                SessionTarget(agent=AgentKind.CODEX, session_id=f"s{index}"),
                state=SessionState.WAITING,
                waiting_for=QUESTION,
                progress=said("y" * 400),
            )
            for index in range(200)
        )

        items = briefing.handover(sessions, focus=None, reason="dialled")

        briefs = [item for item in items if isinstance(item, SpokenBrief)]
        assert briefs, "every decision was given up to keep a list of names"
        assert all(item.decision for item in briefs)
        assert _fits(items)

    def test_over_budget_the_newest_bodies_go_from_the_back_and_are_named(self) -> None:
        """Named as omitted and never sliced (ADR 0016), and from the back (#166)."""
        sessions = tuple(
            row(
                SessionTarget(agent=AgentKind.CODEX, session_id=f"s{index}"),
                state=SessionState.WAITING,
                waiting_for=QUESTION,
                progress=said("x" * 900),
            )
            for index in range(12)
        )

        items = briefing.handover(sessions, focus=None, reason="dialled")

        briefs = [item for item in items if isinstance(item, SpokenBrief)]
        assert len(briefs) == 12
        carried = [item.newest for item in briefs if item.newest.startswith("x")]
        omitted = [
            item.newest
            for item in briefs
            if item.newest == "the newest entry is too large to carry"
        ]
        assert carried and omitted
        # The ones that kept their body are the ones the roster ordered first.
        assert [item.newest for item in briefs] == carried + omitted
        # Everything else stays: the header and the whole decision are what the
        # user acts on, and they are small.
        assert all(item.decision for item in briefs)
        assert _fits(items)

    def test_a_hand_over_never_exceeds_either_ceiling(self) -> None:
        """Both are hard refusals on the wire, so `Dial` accepts what this returns."""
        sessions = tuple(
            row(
                SessionTarget(agent=AgentKind.CODEX, session_id=f"s{index}"),
                state=SessionState.WAITING,
                waiting_for=QUESTION,
                progress=said("y" * 400),
            )
            for index in range(200)
        )

        items = briefing.handover(sessions, focus=None, reason="dialled")

        assert len(items) <= MAX_HANDOVER_ITEMS
        assert _fits(items)
        Dial(voice="prose", agent="rules", hand_over=items)

    def test_the_counts_survive_any_scale_and_the_focus_is_still_briefed_first(self) -> None:
        """The one thing that never goes, and the one order that never changes.

        Two hundred waiting Sessions fit under neither ceiling, so most of this
        roster is given back — and what is left still says how many there were.
        The counts are the summary ADR 0016 asks for: they are what tells the
        Voice that the call could not carry them all, and they say it without a
        fourth kind of item on the seam #195 and #196 build on. The Focus Session
        is still the first brief, because trimming takes from the back and the
        order the roster chose is the order that survives.
        """
        focus = row(
            CODEX,
            state=SessionState.WAITING,
            waiting_for=PERMISSION,
            progress=said("z" * 400),
        )
        others = tuple(
            row(
                SessionTarget(agent=AgentKind.CODEX, session_id=f"s{index}"),
                state=SessionState.WAITING,
                waiting_for=QUESTION,
                progress=said("y" * 400),
            )
            for index in range(200)
        )

        items = briefing.handover((*others, focus), focus=CODEX, reason="dialled")

        summary = items[1]
        assert isinstance(summary, SpokenRosterBrief)
        assert summary.counts == "the others: 200 waiting for your decision"
        briefs = [item for item in items if isinstance(item, SpokenBrief)]
        assert briefs[0].state == "requesting permission"
        assert len(items) <= MAX_HANDOVER_ITEMS
        assert _fits(items)
        Dial(voice="prose", agent="rules", hand_over=items)

    def test_a_session_whose_row_says_it_stopped_is_briefed_from_the_roster(self) -> None:
        """The row a caller used to have to compensate for (#213).

        A Stop that merely ended a turn used to leave its row `RUNNING`, and a
        running Session is briefed by nothing here — so a call dialled by that
        Stop said a Session needed the user and never said which, until the
        caller passed the Stop's own brief in beside the roster. The registry now
        writes the state the Stop implies, so the row arrives here as `IDLE` and
        is briefed like any other Session that stopped, in its place in the
        roster's own order.
        """
        stopped = row(CODEX, state=SessionState.IDLE, progress=said("it stopped here"))
        waiting = row(CLAUDE, state=SessionState.WAITING, waiting_for=PERMISSION)

        items = briefing.handover((stopped, waiting), focus=None, reason="dialled")

        briefs = [item for item in items if isinstance(item, SpokenBrief)]
        assert [item.state for item in briefs] == [
            "waiting for your decision",
            "requesting permission",
        ]
        assert briefs[0].newest == "it stopped here"

    def test_a_session_that_stopped_is_briefed_exactly_once(self) -> None:
        """One Session, one brief. The roster's reading is the only one there is."""
        stopped = row(CODEX, state=SessionState.WAITING, waiting_for=QUESTION)

        items = briefing.handover((stopped,), focus=None, reason="dialled")

        briefs = [item for item in items if isinstance(item, SpokenBrief)]
        assert len(briefs) == 1
        assert briefs[0].state == "waiting for your decision"


def _only_brief(items: tuple[object, ...]) -> SpokenBrief:
    briefs = [item for item in items if isinstance(item, SpokenBrief)]
    assert len(briefs) == 1
    return briefs[0]


def _fits(items: tuple[object, ...]) -> bool:
    return sum(item.size_in_bytes for item in items) <= HANDOVER_BUDGET_BYTES  # type: ignore[attr-defined]

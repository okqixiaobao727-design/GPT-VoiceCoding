"""What Claude Code's own `waitingFor` label says a `waiting` Session is waiting for.

`status: "waiting"` has five distinct causes and only one of them is a permission
dialog (#150). Claude Code writes which one it is, in the same record write, and
this module is the one place that reads it. The tests are about the three
answers it can give and about the two ways it could be got wrong: calling a
dialog the user is driving a Stop, and guessing a kind off a label nobody has
measured.
"""

from __future__ import annotations

import pytest

from gpt_voicecoding.adapters.agent.claude.waiting_labels import (
    LABELS,
    PROVEN_AGAINST_VERSION,
    StopDisposition,
    classify,
)
from gpt_voicecoding.seams.agent import SANDBOX_TOOL_NAME, WaitingKind


class TestTheThreeAnswers:
    """Every measured label lands on exactly one disposition, and they differ."""

    @pytest.mark.parametrize("label", ["dialog open", "goal proposal"])
    def test_a_dialog_the_user_is_driving_is_never_a_stop(self, label: str) -> None:
        reading = classify(label)
        assert reading.disposition is StopDisposition.NEVER_A_STOP
        assert reading.waiting_for.kind is WaitingKind.NONE
        assert reading.waiting_for.needs_the_user is False

    def test_a_permission_prompt_is_a_permission(self) -> None:
        reading = classify("permission prompt")
        assert reading.disposition is StopDisposition.NAMED_NOW
        assert reading.waiting_for.kind is WaitingKind.PERMISSION

    def test_a_sandbox_request_is_a_permission_that_names_the_sandbox(self) -> None:
        reading = classify("sandbox request")
        assert reading.disposition is StopDisposition.NAMED_NOW
        assert reading.waiting_for.kind is WaitingKind.PERMISSION
        assert reading.waiting_for.tool_name == SANDBOX_TOOL_NAME

    @pytest.mark.parametrize("label", ["input needed", "worker request"])
    def test_a_wait_whose_content_lives_elsewhere_is_not_decided_here(self, label: str) -> None:
        """`input needed` is a question, and the question is in the transcript.

        The label proves a wait and names nothing the user could answer from, so
        this reader says *ask again* and the caller re-reads within its budget.
        """
        reading = classify(label)
        assert reading.disposition is StopDisposition.CATCH_UP
        assert reading.waiting_for.kind is WaitingKind.UNKNOWN
        assert reading.waiting_for.caught_up is False


class TestWhatItRefusesToGuess:
    @pytest.mark.parametrize("label", ["", "   ", "something 2.2 invented"])
    def test_no_label_and_an_unmeasured_label_are_the_same_answer(self, label: str) -> None:
        """An older build writes nothing, and a newer one may write a new word.

        Neither is evidence of a kind, and both fall to the caller's catch-up
        rule rather than being guessed either way.
        """
        reading = classify(label)
        assert reading.disposition is StopDisposition.CATCH_UP
        assert reading.waiting_for.kind is WaitingKind.UNKNOWN
        assert reading.waiting_for.caught_up is False

    def test_every_label_this_build_measured_is_in_the_table(self) -> None:
        assert set(LABELS) == {
            "input needed",
            "permission prompt",
            "sandbox request",
            "worker request",
            "dialog open",
            "goal proposal",
        }

    def test_the_build_the_labels_were_measured_on_is_written_down(self) -> None:
        """Documentation for the next re-probe, exactly as the readers' pins are."""
        assert PROVEN_AGAINST_VERSION == "2.1.251"

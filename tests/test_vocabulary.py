"""The shared vocabulary the seams publish, and the rules it enforces by shape.

Two locked rules are structural here rather than remembered: a Session Name can
never be passed where a target is expected, and a Claude Session cannot be
addressed without a pid. Both are enforced by the types, so an adapter that gets
it wrong fails at construction rather than at delivery time.
"""

from __future__ import annotations

import pytest

from gpt_voicecoding.seams.delivery import Delivery, DeliveryReceipt
from gpt_voicecoding.seams.identity import (
    AgentKind,
    SessionName,
    SessionTarget,
    new_request_id,
)
from gpt_voicecoding.seams.verify import VerifyOutcome, VerifyResult


class TestRequestId:
    def test_each_minted_id_is_unique(self) -> None:
        assert new_request_id() != new_request_id()

    def test_a_request_id_is_a_plain_string_so_every_route_can_carry_it(self) -> None:
        """Claude sends it as `uuid` and `msg_id`; Codex as `clientUserMessageId`."""
        assert isinstance(new_request_id(), str)


class TestSessionName:
    def test_a_name_renders_as_project_then_task(self) -> None:
        assert str(SessionName("GPT-VoiceCoding", "Implement the seam contracts")) == (
            "GPT-VoiceCoding · Implement the seam contracts"
        )

    def test_a_rendered_name_parses_back(self) -> None:
        name = SessionName("GPT-VoiceCoding", "Implement the seam contracts")
        assert SessionName.parse(str(name)) == name

    def test_a_name_is_not_a_target(self) -> None:
        """The locked rule, made structural: no attribute a command could carry."""
        name = SessionName("GPT-VoiceCoding", "a task")
        assert not hasattr(name, "session_id")
        assert not hasattr(name, "pid")

    def test_an_empty_half_is_refused(self) -> None:
        with pytest.raises(ValueError):
            SessionName("", "a task")
        with pytest.raises(ValueError):
            SessionName("GPT-VoiceCoding", "   ")

    def test_text_without_the_separator_does_not_parse(self) -> None:
        with pytest.raises(ValueError):
            SessionName.parse("GPT-VoiceCoding")

    def test_text_with_two_separators_does_not_parse(self) -> None:
        with pytest.raises(ValueError):
            SessionName.parse("a · b · c")


class TestSessionTarget:
    def test_a_claude_target_needs_a_pid(self) -> None:
        """`--resume` forks a second process under the same session id."""
        with pytest.raises(ValueError):
            SessionTarget(agent=AgentKind.CLAUDE, session_id="abc")

    def test_a_codex_target_does_not(self) -> None:
        target = SessionTarget(agent=AgentKind.CODEX, session_id="abc")
        assert target.pid is None

    def test_two_claude_pids_under_one_session_id_are_different_targets(self) -> None:
        forked = SessionTarget(agent=AgentKind.CLAUDE, session_id="abc", pid=101)
        original = SessionTarget(agent=AgentKind.CLAUDE, session_id="abc", pid=100)
        assert forked != original

    def test_an_empty_session_id_is_refused(self) -> None:
        """Empty is not the same as absent: it is a name nobody wrote."""
        with pytest.raises(ValueError):
            SessionTarget(agent=AgentKind.CODEX, session_id="", pid=101)

    def test_a_nonsense_pid_is_refused(self) -> None:
        with pytest.raises(ValueError):
            SessionTarget(agent=AgentKind.CLAUDE, session_id="abc", pid=0)

    def test_a_target_is_hashable_so_it_can_key_the_registry(self) -> None:
        assert {SessionTarget(agent=AgentKind.CODEX, session_id="abc")}

    def test_a_codex_session_before_its_first_turn_has_no_session_id(self) -> None:
        """Measured 2026-08-26 (#73): `codex` writes the rollout that names it
        when the first *turn* starts, so a fresh TUI is nameable only by pid."""
        target = SessionTarget(agent=AgentKind.CODEX, pid=6548)
        assert target.session_id is None
        assert not target.named

    def test_a_target_with_a_session_id_is_named(self) -> None:
        assert SessionTarget(agent=AgentKind.CODEX, session_id="abc").named

    def test_a_target_that_names_nothing_at_all_is_refused(self) -> None:
        with pytest.raises(ValueError):
            SessionTarget(agent=AgentKind.CODEX)

    def test_a_claude_target_always_carries_a_session_id(self) -> None:
        """The official roster always gives one, so an anonymous Claude row is a bug."""
        with pytest.raises(ValueError):
            SessionTarget(agent=AgentKind.CLAUDE, pid=3538)


class TestDelivery:
    def test_the_vocabulary_has_exactly_four_states(self) -> None:
        assert {member.value for member in Delivery} == {
            "delivered",
            "failed",
            "held",
            "unknown",
        }

    def test_held_is_never_delivered(self) -> None:
        """Parked in front of the human, possibly forever."""
        assert Delivery.HELD.is_delivered is False

    def test_unknown_is_never_delivered(self) -> None:
        assert Delivery.UNKNOWN.is_delivered is False

    def test_only_delivered_is_delivered(self) -> None:
        assert [member for member in Delivery if member.is_delivered] == [Delivery.DELIVERED]


class TestDeliveryReceipt:
    def test_a_non_delivery_must_carry_a_positive_reason(self) -> None:
        for outcome in (Delivery.FAILED, Delivery.HELD, Delivery.UNKNOWN):
            with pytest.raises(ValueError):
                DeliveryReceipt(request_id=new_request_id(), outcome=outcome)

    def test_a_delivery_may_carry_its_evidence(self) -> None:
        receipt = DeliveryReceipt(
            request_id=new_request_id(),
            outcome=Delivery.DELIVERED,
            reason="acknowledge_answer receipt",
        )
        assert receipt.is_delivered is True

    def test_a_receipt_carries_no_clock(self) -> None:
        """Bridge Core stamps time when it records; adapters never disagree with it."""
        receipt = DeliveryReceipt(request_id=new_request_id(), outcome=Delivery.DELIVERED)
        assert not hasattr(receipt, "at")


class TestVerifyResult:
    def test_the_null_implementation_reports_an_empty_module_string(self) -> None:
        """ADR 0003: empty is a *known* state, not an absent field."""
        result = VerifyResult(outcome=VerifyOutcome.MANUAL, loaded="")
        assert result.loaded == ""

    def test_a_failure_must_name_the_layer(self) -> None:
        with pytest.raises(ValueError):
            VerifyResult(outcome=VerifyOutcome.FAIL, loaded="some.adapter")

    def test_a_manual_outcome_means_nothing_real_is_loaded(self) -> None:
        with pytest.raises(ValueError):
            VerifyResult(outcome=VerifyOutcome.MANUAL, loaded="some.adapter")

    def test_the_check_has_three_outcomes_not_two(self) -> None:
        assert {member.value for member in VerifyOutcome} == {"pass", "fail", "manual"}

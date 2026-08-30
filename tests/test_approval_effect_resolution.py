"""Fast tests for the acceptance harness's approval/effect resolution interface."""

from __future__ import annotations

import approval_effect
import pytest


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.waits: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def wait(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.now += seconds


def test_optional_approval_completes_from_an_already_verified_effect() -> None:
    clock = FakeClock()
    answered: list[str] = []
    journal: list[tuple[str, dict[str, object]]] = []

    result = approval_effect.resolve(
        requirement=approval_effect.ApprovalRequirement.OPTIONAL,
        session_address="codex:mine",
        deadlines=approval_effect.Deadlines(
            resolution_seconds=180.0,
            announcement_seconds=90.0,
            effect_seconds=180.0,
            poll_seconds=2.0,
        ),
        collaborators=approval_effect.Collaborators(
            effect=lambda: True,
            pending_approvals=lambda: (),
            await_announcement=lambda _deadline: None,
            answer_approval=lambda approval_id: answered.append(approval_id),
            journal=lambda event, **fields: journal.append((event, fields)),
            monotonic=clock.monotonic,
            wait=clock.wait,
        ),
    )

    assert result.succeeded is True
    assert result.terminal_reason is approval_effect.TerminalReason.EFFECT
    assert result.effect_observed is True
    assert result.elapsed_seconds == 0.0
    assert answered == []
    assert clock.waits == []


def test_required_approval_fails_promptly_when_the_effect_has_no_approval() -> None:
    clock = FakeClock()
    answered: list[str] = []
    journal: list[tuple[str, dict[str, object]]] = []

    result = approval_effect.resolve(
        requirement=approval_effect.ApprovalRequirement.REQUIRED,
        session_address="claude:mine",
        deadlines=approval_effect.Deadlines(180.0, 90.0, 180.0, 2.0),
        collaborators=approval_effect.Collaborators(
            effect=lambda: True,
            pending_approvals=lambda: (),
            await_announcement=lambda _deadline: None,
            answer_approval=lambda approval_id: answered.append(approval_id),
            journal=lambda event, **fields: journal.append((event, fields)),
            monotonic=clock.monotonic,
            wait=clock.wait,
        ),
    )

    assert result.succeeded is False
    assert result.terminal_reason is approval_effect.TerminalReason.EFFECT
    assert result.effect_observed is True
    assert result.failure == (
        "the verified effect appeared without the required approval for Session claude:mine"
    )
    assert answered == []
    assert clock.waits == []
    assert journal[0][0] == "approval_effect.resolved"
    assert journal[0][1]["terminal_reason"] == "effect"
    assert journal[0][1]["elapsed_seconds"] == 0.0
    assert journal[0][1]["failure"] == result.failure


def test_approval_first_requires_announcement_then_answer_then_effect() -> None:
    clock = FakeClock()
    authority_events: list[str] = []
    effect_observed = False

    def announce(_deadline: float) -> approval_effect.Announcement:
        authority_events.append("announcement")
        return approval_effect.Announcement("chat message 42")

    def answer(approval_id: str) -> approval_effect.ApprovalAnswer:
        nonlocal effect_observed
        authority_events.append(f"answer {approval_id}")
        effect_observed = True
        return approval_effect.ApprovalAnswer(True, "approved")

    result = approval_effect.resolve(
        requirement=approval_effect.ApprovalRequirement.REQUIRED,
        session_address="claude:mine",
        deadlines=approval_effect.Deadlines(180.0, 90.0, 180.0, 2.0),
        collaborators=approval_effect.Collaborators(
            effect=lambda: effect_observed,
            pending_approvals=lambda: (
                approval_effect.PendingApproval("approval-7", "claude:mine"),
            ),
            await_announcement=announce,
            answer_approval=answer,
            journal=lambda _event, **_fields: None,
            monotonic=clock.monotonic,
            wait=clock.wait,
        ),
    )

    assert result.succeeded is True
    assert result.terminal_reason is approval_effect.TerminalReason.APPROVAL
    assert result.effect_observed is True
    assert result.approval_id == "approval-7"
    assert result.authority_evidence == "chat message 42; approved"
    assert authority_events == ["announcement", "answer approval-7"]


def test_an_unannounced_approval_fails_without_answering_it() -> None:
    clock = FakeClock()
    answered: list[str] = []
    journal: list[tuple[str, dict[str, object]]] = []
    effects = iter((False, True))

    result = approval_effect.resolve(
        requirement=approval_effect.ApprovalRequirement.OPTIONAL,
        session_address="codex:mine",
        deadlines=approval_effect.Deadlines(180.0, 90.0, 180.0, 2.0),
        collaborators=approval_effect.Collaborators(
            effect=lambda: next(effects),
            pending_approvals=lambda: (
                approval_effect.PendingApproval("approval-8", "codex:mine"),
            ),
            await_announcement=lambda _deadline: None,
            answer_approval=lambda approval_id: answered.append(approval_id),
            journal=lambda event, **fields: journal.append((event, fields)),
            monotonic=clock.monotonic,
            wait=clock.wait,
        ),
    )

    assert result.succeeded is False
    assert result.terminal_reason is approval_effect.TerminalReason.APPROVAL
    assert result.approval_id == "approval-8"
    assert result.effect_observed is True
    assert result.failure == (
        "approval approval-8 for Session codex:mine did not reach the Companion Channel within 90s"
    )
    assert answered == []
    assert journal[0][1]["terminal_reason"] == "approval"
    assert journal[0][1]["elapsed_seconds"] == 0.0
    assert journal[0][1]["approval_id"] == "approval-8"
    assert journal[0][1]["failure"] == result.failure


def test_a_refused_approval_relay_answer_is_reported_precisely() -> None:
    clock = FakeClock()
    journal: list[tuple[str, dict[str, object]]] = []
    effects = iter((False, True))

    result = approval_effect.resolve(
        requirement=approval_effect.ApprovalRequirement.REQUIRED,
        session_address="claude:mine",
        deadlines=approval_effect.Deadlines(180.0, 90.0, 180.0, 2.0),
        collaborators=approval_effect.Collaborators(
            effect=lambda: next(effects),
            pending_approvals=lambda: (
                approval_effect.PendingApproval("approval-9", "claude:mine"),
            ),
            await_announcement=lambda _deadline: approval_effect.Announcement("chat message 43"),
            answer_approval=lambda _approval_id: approval_effect.ApprovalAnswer(
                False, "approval expired"
            ),
            journal=lambda event, **fields: journal.append((event, fields)),
            monotonic=clock.monotonic,
            wait=clock.wait,
        ),
    )

    assert result.succeeded is False
    assert result.terminal_reason is approval_effect.TerminalReason.APPROVAL
    assert result.approval_id == "approval-9"
    assert result.effect_observed is True
    assert result.authority_evidence == "chat message 43; approval expired"
    assert result.failure == (
        "Approval Relay refused approval approval-9 for Session claude:mine: approval expired"
    )
    assert journal[0][1]["terminal_reason"] == "approval"
    assert journal[0][1]["approval_id"] == "approval-9"
    assert journal[0][1]["failure"] == result.failure


def test_an_answered_approval_fails_when_its_effect_never_appears() -> None:
    clock = FakeClock()
    journal: list[tuple[str, dict[str, object]]] = []

    result = approval_effect.resolve(
        requirement=approval_effect.ApprovalRequirement.REQUIRED,
        session_address="claude:mine",
        deadlines=approval_effect.Deadlines(180.0, 90.0, 6.0, 2.0),
        collaborators=approval_effect.Collaborators(
            effect=lambda: False,
            pending_approvals=lambda: (
                approval_effect.PendingApproval("approval-10", "claude:mine"),
            ),
            await_announcement=lambda _deadline: approval_effect.Announcement("chat message 44"),
            answer_approval=lambda _approval_id: approval_effect.ApprovalAnswer(True, "approved"),
            journal=lambda event, **fields: journal.append((event, fields)),
            monotonic=clock.monotonic,
            wait=clock.wait,
        ),
    )

    assert result.succeeded is False
    assert result.terminal_reason is approval_effect.TerminalReason.APPROVAL
    assert result.approval_id == "approval-10"
    assert result.effect_observed is False
    assert result.elapsed_seconds == 6.0
    assert result.failure == (
        "approval approval-10 for Session claude:mine was announced and answered, but the "
        "verified effect did not appear within 6s"
    )
    assert journal[0][1]["terminal_reason"] == "approval"
    assert journal[0][1]["elapsed_seconds"] == 6.0
    assert journal[0][1]["approval_id"] == "approval-10"
    assert journal[0][1]["failure"] == result.failure


def test_neither_effect_nor_own_approval_exhausts_the_unchanged_deadline() -> None:
    clock = FakeClock()
    answered: list[str] = []
    journal: list[tuple[str, dict[str, object]]] = []

    result = approval_effect.resolve(
        requirement=approval_effect.ApprovalRequirement.OPTIONAL,
        session_address="codex:mine",
        deadlines=approval_effect.Deadlines(6.0, 90.0, 4.0, 2.0),
        collaborators=approval_effect.Collaborators(
            effect=lambda: False,
            pending_approvals=lambda: (),
            await_announcement=lambda _deadline: None,
            answer_approval=lambda approval_id: answered.append(approval_id),
            journal=lambda event, **fields: journal.append((event, fields)),
            monotonic=clock.monotonic,
            wait=clock.wait,
        ),
    )

    assert result.succeeded is False
    assert result.terminal_reason is approval_effect.TerminalReason.TIMEOUT
    assert result.effect_observed is False
    assert result.elapsed_seconds == 10.0
    assert result.failure == (
        "neither a verified effect nor an approval for Session codex:mine appeared within 10s "
        "(6s approval observation + 4s effect fallback)"
    )
    assert answered == []
    assert journal[0][0] == "approval_effect.resolved"
    assert journal[0][1]["terminal_reason"] == "timeout"
    assert journal[0][1]["elapsed_seconds"] == 10.0
    assert journal[0][1]["failure"] == result.failure


def test_two_own_approvals_fail_closed_and_journal_every_ambiguous_id() -> None:
    clock = FakeClock()
    answered: list[str] = []
    journal: list[tuple[str, dict[str, object]]] = []

    result = approval_effect.resolve(
        requirement=approval_effect.ApprovalRequirement.OPTIONAL,
        session_address="codex:mine",
        deadlines=approval_effect.Deadlines(180.0, 90.0, 180.0, 2.0),
        collaborators=approval_effect.Collaborators(
            effect=lambda: True,
            pending_approvals=lambda: (
                approval_effect.PendingApproval("foreign", "claude:other"),
                approval_effect.PendingApproval("approval-11", "codex:mine"),
                approval_effect.PendingApproval("approval-12", "codex:mine"),
            ),
            await_announcement=lambda _deadline: None,
            answer_approval=lambda approval_id: answered.append(approval_id),
            journal=lambda event, **fields: journal.append((event, fields)),
            monotonic=clock.monotonic,
            wait=clock.wait,
        ),
    )

    assert result.succeeded is False
    assert result.terminal_reason is approval_effect.TerminalReason.APPROVAL
    assert result.effect_observed is True
    assert result.failure == (
        "Session codex:mine has 2 pending approvals and none can be uniquely correlated: "
        "approval-11, approval-12"
    )
    assert answered == []
    assert journal == [
        (
            "approval_effect.resolved",
            {
                "requirement": "optional",
                "terminal_reason": "approval",
                "elapsed_seconds": 0.0,
                "succeeded": False,
                "effect_observed": True,
                "approval_id": None,
                "authority_evidence": None,
                "own_approval_count": 2,
                "approval_ids": ["approval-11", "approval-12"],
                "failure": result.failure,
            },
        )
    ]


def test_an_own_approval_without_an_id_fails_closed_before_authority_is_sent() -> None:
    clock = FakeClock()
    answered: list[str] = []
    journal: list[tuple[str, dict[str, object]]] = []

    result = approval_effect.resolve(
        requirement=approval_effect.ApprovalRequirement.REQUIRED,
        session_address="claude:mine",
        deadlines=approval_effect.Deadlines(180.0, 90.0, 180.0, 2.0),
        collaborators=approval_effect.Collaborators(
            effect=lambda: True,
            pending_approvals=lambda: (approval_effect.PendingApproval("", "claude:mine"),),
            await_announcement=lambda _deadline: approval_effect.Announcement("chat message 46"),
            answer_approval=lambda approval_id: (
                answered.append(approval_id) or approval_effect.ApprovalAnswer(True, "approved")
            ),
            journal=lambda event, **fields: journal.append((event, fields)),
            monotonic=clock.monotonic,
            wait=clock.wait,
        ),
    )

    assert result.succeeded is False
    assert result.terminal_reason is approval_effect.TerminalReason.APPROVAL
    assert result.effect_observed is True
    assert result.failure == "a pending approval for Session claude:mine has no approval id"
    assert answered == []
    assert journal[0][1]["approval_ids"] == [""]
    assert journal[0][1]["failure"] == result.failure


@pytest.mark.parametrize(
    "requirement",
    (
        approval_effect.ApprovalRequirement.REQUIRED,
        approval_effect.ApprovalRequirement.OPTIONAL,
    ),
)
def test_own_approval_wins_a_snapshot_that_also_contains_the_effect(
    requirement: approval_effect.ApprovalRequirement,
) -> None:
    clock = FakeClock()
    answered: list[str] = []
    journal: list[tuple[str, dict[str, object]]] = []

    result = approval_effect.resolve(
        requirement=requirement,
        session_address="codex:mine",
        deadlines=approval_effect.Deadlines(180.0, 90.0, 180.0, 2.0),
        collaborators=approval_effect.Collaborators(
            effect=lambda: True,
            pending_approvals=lambda: (
                approval_effect.PendingApproval("approval-13", "codex:mine"),
            ),
            await_announcement=lambda _deadline: approval_effect.Announcement("chat message 45"),
            answer_approval=lambda approval_id: (
                answered.append(approval_id) or approval_effect.ApprovalAnswer(True, "approved")
            ),
            journal=lambda event, **fields: journal.append((event, fields)),
            monotonic=clock.monotonic,
            wait=clock.wait,
        ),
    )

    assert result.succeeded is True
    assert result.terminal_reason is approval_effect.TerminalReason.APPROVAL
    assert answered == ["approval-13"]
    assert journal == [
        (
            "approval_effect.resolved",
            {
                "requirement": requirement.value,
                "terminal_reason": "approval",
                "elapsed_seconds": 0.0,
                "succeeded": True,
                "effect_observed": True,
                "approval_id": "approval-13",
                "authority_evidence": "chat message 45; approved",
                "own_approval_count": 1,
                "approval_ids": ["approval-13"],
                "failure": None,
            },
        )
    ]


def test_foreign_approvals_are_ignored_and_never_answered() -> None:
    clock = FakeClock()
    answered: list[str] = []
    journal: list[tuple[str, dict[str, object]]] = []

    result = approval_effect.resolve(
        requirement=approval_effect.ApprovalRequirement.OPTIONAL,
        session_address="codex:mine",
        deadlines=approval_effect.Deadlines(180.0, 90.0, 180.0, 2.0),
        collaborators=approval_effect.Collaborators(
            effect=lambda: clock.now >= 4.0,
            pending_approvals=lambda: (
                approval_effect.PendingApproval("foreign-1", "claude:other"),
                approval_effect.PendingApproval("foreign-2", "codex:other"),
            ),
            await_announcement=lambda _deadline: None,
            answer_approval=lambda approval_id: answered.append(approval_id),
            journal=lambda event, **fields: journal.append((event, fields)),
            monotonic=clock.monotonic,
            wait=clock.wait,
        ),
    )

    assert result.succeeded is True
    assert result.terminal_reason is approval_effect.TerminalReason.EFFECT
    assert result.elapsed_seconds == 4.0
    assert answered == []
    assert journal == [
        (
            "approval_effect.resolved",
            {
                "requirement": "optional",
                "terminal_reason": "effect",
                "elapsed_seconds": 4.0,
                "succeeded": True,
                "effect_observed": True,
                "approval_id": None,
                "authority_evidence": None,
                "own_approval_count": 0,
                "approval_ids": [],
                "failure": None,
            },
        )
    ]


def test_effect_keeps_its_own_fallback_after_the_approval_window_expires() -> None:
    clock = FakeClock()
    journal: list[tuple[str, dict[str, object]]] = []

    result = approval_effect.resolve(
        requirement=approval_effect.ApprovalRequirement.OPTIONAL,
        session_address="codex:mine",
        deadlines=approval_effect.Deadlines(6.0, 90.0, 4.0, 2.0),
        collaborators=approval_effect.Collaborators(
            effect=lambda: clock.now >= 8.0,
            pending_approvals=lambda: (),
            await_announcement=lambda _deadline: None,
            answer_approval=lambda _approval_id: approval_effect.ApprovalAnswer(True, "approved"),
            journal=lambda event, **fields: journal.append((event, fields)),
            monotonic=clock.monotonic,
            wait=clock.wait,
        ),
    )

    assert result.succeeded is True
    assert result.terminal_reason is approval_effect.TerminalReason.EFFECT
    assert result.elapsed_seconds == 8.0
    assert journal[0][1]["elapsed_seconds"] == 8.0

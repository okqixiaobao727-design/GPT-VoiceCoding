"""Resolve one acceptance instruction from its effect or correlated approval."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class ApprovalRequirement(StrEnum):
    """Whether the scenario's claim includes an authority round trip."""

    REQUIRED = "required"
    OPTIONAL = "optional"


class TerminalReason(StrEnum):
    """The correlated observation that ended resolution."""

    EFFECT = "effect"
    APPROVAL = "approval"
    TIMEOUT = "timeout"


@dataclass(frozen=True)
class Deadlines:
    """Existing far-side ceilings used by the resolution."""

    resolution_seconds: float
    announcement_seconds: float
    effect_seconds: float
    poll_seconds: float


@dataclass(frozen=True)
class PendingApproval:
    """The authority identity needed to correlate one pending approval."""

    approval_id: str
    session_address: str


@dataclass(frozen=True)
class Announcement:
    """Durable evidence that the Session's approval reached the Companion Channel."""

    evidence: str


@dataclass(frozen=True)
class ApprovalAnswer:
    """The Approval Relay's observable reply."""

    succeeded: bool
    evidence: str


class Journal(Protocol):
    def __call__(self, event: str, **fields: object) -> object: ...


@dataclass(frozen=True)
class Collaborators:
    """Observable far sides supplied by the acceptance walk or a fast test."""

    effect: Callable[[], bool]
    pending_approvals: Callable[[], Sequence[PendingApproval]]
    await_announcement: Callable[[float], Announcement | None]
    answer_approval: Callable[[str], ApprovalAnswer]
    journal: Journal
    monotonic: Callable[[], float]
    wait: Callable[[float], None]


@dataclass(frozen=True)
class Resolution:
    """Everything a caller needs to grade its own acceptance claim."""

    succeeded: bool
    terminal_reason: TerminalReason
    elapsed_seconds: float
    effect_observed: bool
    approval_id: str | None = None
    authority_evidence: str | None = None
    failure: str | None = None


def resolve(
    *,
    requirement: ApprovalRequirement,
    session_address: str,
    deadlines: Deadlines,
    collaborators: Collaborators,
) -> Resolution:
    """Resolve one instruction through the module's single public interface."""
    started = collaborators.monotonic()

    def finish(
        *,
        succeeded: bool,
        terminal_reason: TerminalReason,
        effect_observed: bool,
        own_approvals: Sequence[PendingApproval] = (),
        approval_id: str | None = None,
        authority_evidence: str | None = None,
        failure: str | None = None,
    ) -> Resolution:
        result = Resolution(
            succeeded=succeeded,
            terminal_reason=terminal_reason,
            elapsed_seconds=collaborators.monotonic() - started,
            effect_observed=effect_observed,
            approval_id=approval_id,
            authority_evidence=authority_evidence,
            failure=failure,
        )
        approval_ids = [pending.approval_id for pending in own_approvals]
        collaborators.journal(
            "approval_effect.resolved",
            requirement=requirement.value,
            terminal_reason=result.terminal_reason.value,
            elapsed_seconds=result.elapsed_seconds,
            succeeded=result.succeeded,
            effect_observed=result.effect_observed,
            approval_id=result.approval_id,
            authority_evidence=result.authority_evidence,
            own_approval_count=len(own_approvals),
            approval_ids=approval_ids,
            failure=result.failure,
        )
        return result

    def finish_effect(
        own_approvals: Sequence[PendingApproval] = (),
    ) -> Resolution:
        optional = requirement is ApprovalRequirement.OPTIONAL
        return finish(
            succeeded=optional,
            terminal_reason=TerminalReason.EFFECT,
            effect_observed=True,
            own_approvals=own_approvals,
            failure=(
                None
                if optional
                else (
                    "the verified effect appeared without the required approval for "
                    f"Session {session_address}"
                )
            ),
        )

    def resolve_approval(
        approval: PendingApproval,
        *,
        effect_observed: bool,
    ) -> Resolution:
        own_approvals = (approval,)

        def effect_now() -> bool:
            return effect_observed or collaborators.effect()

        announcement = collaborators.await_announcement(deadlines.announcement_seconds)
        if announcement is None:
            return finish(
                succeeded=False,
                terminal_reason=TerminalReason.APPROVAL,
                effect_observed=effect_now(),
                own_approvals=own_approvals,
                approval_id=approval.approval_id,
                failure=(
                    f"approval {approval.approval_id} for Session {session_address} did not reach "
                    f"the Companion Channel within {deadlines.announcement_seconds:g}s"
                ),
            )
        answer = collaborators.answer_approval(approval.approval_id)
        authority_evidence = f"{announcement.evidence}; {answer.evidence}"
        if not answer.succeeded:
            return finish(
                succeeded=False,
                terminal_reason=TerminalReason.APPROVAL,
                effect_observed=effect_now(),
                own_approvals=own_approvals,
                approval_id=approval.approval_id,
                authority_evidence=authority_evidence,
                failure=(
                    f"Approval Relay refused approval {approval.approval_id} for Session "
                    f"{session_address}: {answer.evidence}"
                ),
            )

        def success() -> Resolution:
            return finish(
                succeeded=True,
                terminal_reason=TerminalReason.APPROVAL,
                effect_observed=True,
                own_approvals=own_approvals,
                approval_id=approval.approval_id,
                authority_evidence=authority_evidence,
            )

        if collaborators.effect():
            return success()
        effect_deadline = collaborators.monotonic() + deadlines.effect_seconds
        while collaborators.monotonic() < effect_deadline:
            collaborators.wait(
                min(deadlines.poll_seconds, effect_deadline - collaborators.monotonic())
            )
            if collaborators.effect():
                return success()
        return finish(
            succeeded=False,
            terminal_reason=TerminalReason.APPROVAL,
            effect_observed=False,
            own_approvals=own_approvals,
            approval_id=approval.approval_id,
            authority_evidence=authority_evidence,
            failure=(
                f"approval {approval.approval_id} for Session {session_address} was announced and "
                "answered, but the verified effect did not appear within "
                f"{deadlines.effect_seconds:g}s"
            ),
        )

    approval_deadline = started + deadlines.resolution_seconds
    while True:
        own_approvals = tuple(
            approval
            for approval in collaborators.pending_approvals()
            if approval.session_address == session_address
        )
        effect_observed = collaborators.effect()

        if any(not approval.approval_id.strip() for approval in own_approvals):
            return finish(
                succeeded=False,
                terminal_reason=TerminalReason.APPROVAL,
                effect_observed=effect_observed,
                own_approvals=own_approvals,
                failure=f"a pending approval for Session {session_address} has no approval id",
            )
        if len(own_approvals) == 1:
            return resolve_approval(own_approvals[0], effect_observed=effect_observed)
        if len(own_approvals) > 1:
            approval_ids = [approval.approval_id for approval in own_approvals]
            return finish(
                succeeded=False,
                terminal_reason=TerminalReason.APPROVAL,
                effect_observed=effect_observed,
                own_approvals=own_approvals,
                failure=(
                    f"Session {session_address} has {len(own_approvals)} pending approvals and "
                    f"none can be uniquely correlated: {', '.join(approval_ids)}"
                ),
            )
        if effect_observed:
            return finish_effect(own_approvals)
        if collaborators.monotonic() >= approval_deadline:
            break
        collaborators.wait(
            min(deadlines.poll_seconds, approval_deadline - collaborators.monotonic())
        )

    effect_deadline = approval_deadline + deadlines.effect_seconds
    while True:
        if collaborators.effect():
            return finish_effect()
        if collaborators.monotonic() >= effect_deadline:
            total_seconds = deadlines.resolution_seconds + deadlines.effect_seconds
            return finish(
                succeeded=False,
                terminal_reason=TerminalReason.TIMEOUT,
                effect_observed=False,
                failure=(
                    f"neither a verified effect nor an approval for Session {session_address} "
                    f"appeared within {total_seconds:g}s "
                    f"({deadlines.resolution_seconds:g}s approval observation + "
                    f"{deadlines.effect_seconds:g}s effect fallback)"
                ),
            )
        collaborators.wait(min(deadlines.poll_seconds, effect_deadline - collaborators.monotonic()))

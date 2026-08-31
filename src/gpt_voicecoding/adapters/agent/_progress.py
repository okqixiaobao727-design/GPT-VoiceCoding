"""Shared Agent progress helpers that do not choose publication policy."""

from __future__ import annotations

from collections.abc import Sequence

from gpt_voicecoding.seams.agent import (
    ProgressAvailability,
    SessionInspection,
)


def source_degradation(
    rows: Sequence[SessionInspection],
    existing: str | None = None,
) -> str | None:
    """Combine existing lane news with source reasons from unreadable observations."""
    reasons = [existing] if existing else []
    for row in rows:
        if row.progress.availability is not ProgressAvailability.UNREADABLE:
            continue
        assert row.progress.reason is not None
        if row.progress.reason not in reasons:
            reasons.append(row.progress.reason)
    return "; ".join(reasons) or None

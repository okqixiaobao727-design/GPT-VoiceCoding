"""Shared Agent progress helpers that do not choose publication policy."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from gpt_voicecoding.seams.agent import (
    HistoryPage,
    ProgressAvailability,
    ProgressEntry,
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


def page(
    entries: Sequence[ProgressEntry],
    *,
    before: int | None,
    count: int,
    read_at: datetime,
) -> HistoryPage:
    """One History page over the full list of visible entries (#171).

    **The one windowing function, and both lanes call it.** Each lane builds
    every entry it can see before anything trims that list — the Claude records
    walk, the Codex thread's turns — and hands the whole list here, so the two
    lanes cannot page differently over the same record. Nothing about a
    transcript or a thread is known in this module; the ordinals the lanes
    assigned are the only cursor.

    `before` is exclusive: the page holds the `count` entries immediately before
    it, newest-first. `None`, or a value above every ordinal, is the newest page
    — which *includes* the newest entry, because every page is complete on its
    own and the engine remembers nothing between reads. Past the oldest entry
    the page is empty with `older=False`, which is an answer rather than a
    refusal.
    """
    if count <= 0:
        raise ValueError("a History page holds at least one entry")
    candidates = [entry for entry in entries if before is None or entry.ordinal < before]
    kept = candidates[-count:] if candidates else []
    return HistoryPage(
        entries=tuple(reversed(kept)),
        older=len(candidates) > len(kept),
        read_at=read_at,
    )

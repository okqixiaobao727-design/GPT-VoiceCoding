"""How much of what a Session said travels — one bound, both lanes (#76).

The rule is ported whole from `legacy@1d32845:bridge/transcript.py:2828-2860`;
the numbers are not, and the reason is the shape of the thing that carries them.
Legacy bounded a per-Session verb's answer (12 entries / 32 KB,
`config.plist:449-452`); this bounds **every row** of a roster reply with a 64 KB
ceiling on it. Each lane's own reader is tested beside it
(`test_claude_transcript_tail.py`, `test_codex_thread_tail.py`); this is the
bound they share, tested where it lives so neither lane can quietly grow one of
its own.
"""

from __future__ import annotations

import pytest

from gpt_voicecoding.adapters.agent._progress import (
    RECENT_LIMIT,
    RECENT_MAX_BYTES,
    bounded,
    encoded_size,
)
from gpt_voicecoding.seams.agent import ProgressEntry, ProgressRole


def said(text: str, *, role: ProgressRole = ProgressRole.ASSISTANT) -> ProgressEntry:
    return ProgressEntry(role=role, text=text)


class TestKeepingTheNewest:
    def test_nothing_said_is_nothing_dropped(self) -> None:
        assert bounded([]) == ((), False)

    def test_a_tail_that_fits_is_carried_whole(self) -> None:
        entries = [said("one"), said("two")]

        assert bounded(entries) == (tuple(entries), False)

    def test_the_oldest_go_first_and_the_drop_is_admitted(self) -> None:
        entries = [said(f"step {index}") for index in range(RECENT_LIMIT + 2)]

        kept, truncated = bounded(entries)

        assert [entry.text for entry in kept] == [
            f"step {index}" for index in range(2, RECENT_LIMIT + 2)
        ]
        assert truncated is True


class TestTheByteBudget:
    def test_a_tail_too_large_is_widened_down_until_it_fits(self) -> None:
        """Built from the budget rather than from a guess at it.

        `overhead` is what one entry costs besides its own text, so `big` fills
        the budget exactly on its own and cannot share it with anything.
        """
        overhead = encoded_size([said("x")]) - len("x")
        big = said("x" * (RECENT_MAX_BYTES - overhead))
        newest = said("the last word")
        assert encoded_size([big]) == RECENT_MAX_BYTES

        kept, truncated = bounded([big, newest])

        assert kept == (newest,)
        assert truncated is True

    def test_a_single_oversize_entry_leaves_the_row_saying_nothing(self) -> None:
        """Legacy's `drop_oversize_tail`, and here it is the only behaviour.

        A reply nobody can read is a worse failure than a row that admits it is
        empty: the byte ceiling is on the *whole* roster, so one Session's very
        long paragraph must not take every other Session's row down with it.
        """
        assert bounded([said("y" * (RECENT_MAX_BYTES * 2))]) == ((), True)

    def test_what_is_kept_is_never_cut(self) -> None:
        """`stop_analysis.SUMMARY_MAX_CHARS`'s rule: whole, or not at all."""
        entries = [said("a" * 40), said("b" * 40)]

        kept, _ = bounded(entries)

        assert [len(entry.text) for entry in kept] == [40, 40]

    def test_the_role_counts_because_the_role_travels(self) -> None:
        """Measured on the document `progress_document` renders, not on the text."""
        entry = said("hello", role=ProgressRole.USER)

        assert encoded_size([entry]) > len(entry.text)
        assert encoded_size([]) == len(b"[]")


class TestABoundThatBoundsNothing:
    @pytest.mark.parametrize(("limit", "max_bytes"), [(0, 1), (-1, 1), (1, 0), (1, -1)])
    def test_is_refused_rather_than_read_as_no_bound_at_all(
        self, limit: int, max_bytes: int
    ) -> None:
        """Legacy validated its limits instead of silently reading everything."""
        with pytest.raises(ValueError):
            bounded([said("anything")], limit, max_bytes=max_bytes)


class TestTheNumbersFitTheReplyTheyRideOn:
    def test_ten_sessions_at_the_budget_stay_inside_the_protocol_ceiling(self) -> None:
        """Why 3 KB and not 12 entries: `status` carries one of these per row."""
        from gpt_voicecoding.seams.control_plane import MAX_REQUEST_BYTES

        assert RECENT_MAX_BYTES * 10 < MAX_REQUEST_BYTES

"""The History page: one windowing function, one cursor, and both lanes (#171).

ADR 0016's amendment gives the one canonical observation a third publication —
a page bounded by a **count** rather than by bytes, with an ordinal cursor. This
file holds the two halves that publication rests on: the shared window that both
lanes call, and each lane's own `history`, proved to page identically over
identical records.

The rendering is `test_bridgectl.py`'s and the action is
`test_control_plane_actions.py`'s; what is here is the read.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from gpt_voicecoding.adapters.agent import _progress
from gpt_voicecoding.adapters.agent.claude import transcript_tail
from gpt_voicecoding.adapters.agent.claude.adapter import ClaudeAgentAdapter
from gpt_voicecoding.adapters.agent.codex import CodexAgentAdapter, thread_tail
from gpt_voicecoding.seams.agent import (
    AgentAdapter,
    HistoryPage,
    LaneUnavailable,
    ProgressEntry,
    ProgressRole,
)
from gpt_voicecoding.seams.identity import AgentKind, SessionTarget
from test_claude_stop_analysis import said
from test_claude_stop_wiring import TARGET, adapter_holding, transcript
from test_codex_discovery import THREAD
from test_codex_progress import TEST_HOME, TurnedDaemon, adapter_over, stopped
from test_codex_thread_tail import spoke, told, turn
from test_seam_contracts import _shape

#: Six things said, alternating sides, which is more than one page holds.
SAID = (
    "the first thing",
    "the second thing",
    "the third thing",
    "the fourth thing",
    "the fifth thing",
    "the sixth thing",
)

READ_AT_SECONDS = 1_787_712_279


def numbered(said_: tuple[str, ...] = SAID) -> tuple[ProgressEntry, ...]:
    return tuple(
        ProgressEntry(
            ordinal=index,
            role=ProgressRole.USER if index % 2 == 0 else ProgressRole.ASSISTANT,
            text=text,
        )
        for index, text in enumerate(said_)
    )


def at() -> datetime:
    return datetime.fromtimestamp(READ_AT_SECONDS, UTC)


class TestTheOneWindow:
    """Neither lane owns this: they hand it a list and it hands back a page."""

    def page(self, before: int | None = None, count: int = 5, **kwargs: object) -> HistoryPage:
        return _progress.page(
            kwargs.get("entries", numbered()),  # type: ignore[arg-type]
            before=before,
            count=count,
            read_at=at(),
        )

    def test_the_newest_page_includes_the_newest_entry(self) -> None:
        """#171: every page is complete on its own and the engine remembers nothing."""
        page = self.page()

        assert [entry.ordinal for entry in page.entries] == [5, 4, 3, 2, 1]
        assert page.older is True

    def test_a_page_is_newest_first(self) -> None:
        assert [entry.text for entry in self.page(count=2).entries] == [
            "the sixth thing",
            "the fifth thing",
        ]

    def test_the_cursor_is_exclusive(self) -> None:
        page = self.page(before=1)

        assert [entry.ordinal for entry in page.entries] == [0]
        assert page.older is False

    def test_past_the_oldest_entry_is_an_empty_page_rather_than_a_refusal(self) -> None:
        page = self.page(before=0)

        assert page.entries == ()
        assert page.older is False
        assert page.read_at is not None

    def test_a_cursor_above_every_ordinal_is_the_newest_page(self) -> None:
        assert self.page(before=9_999).entries == self.page().entries

    def test_fewer_entries_than_a_page_is_the_whole_history(self) -> None:
        page = self.page(entries=numbered(SAID[:2]))

        assert [entry.ordinal for entry in page.entries] == [1, 0]
        assert page.older is False

    def test_no_entries_at_all_is_an_answer(self) -> None:
        page = self.page(entries=())

        assert page.entries == ()
        assert page.older is False

    def test_a_page_of_nothing_is_refused_as_a_configuration(self) -> None:
        """A page that holds no entries would page through a history forever."""
        with pytest.raises(ValueError, match="at least one entry"):
            self.page(count=0)


class TestTheClaudeLane:
    """The records walk, windowed — the same walk the roster tail reads."""

    def visible(self, records: list[dict[str, object]]) -> tuple[ProgressEntry, ...]:
        return transcript_tail.visible(records)

    def test_ordinals_count_from_the_oldest_visible_entry(self) -> None:
        entries = self.visible([said("first", role="user"), said("second")])

        assert [(entry.ordinal, entry.text) for entry in entries] == [(0, "first"), (1, "second")]

    def test_a_sidechain_record_takes_no_ordinal(self) -> None:
        """It is excluded from the ordinals exactly as it is excluded from the tail."""
        entries = self.visible(
            [
                said("first", role="user"),
                said("a subagent", isSidechain=True),
                said("second"),
            ]
        )

        assert [(entry.ordinal, entry.text) for entry in entries] == [(0, "first"), (1, "second")]

    def test_appending_does_not_renumber_what_was_already_there(self) -> None:
        records = [said(text, role="user") for text in SAID]
        before_append = self.visible(records)
        after_append = self.visible([*records, said("and one more")])

        assert after_append[: len(before_append)] == before_append

    def test_one_page_of_a_real_transcript(self, tmp_path: Path) -> None:
        adapter = adapter_holding(transcript(tmp_path, [said(text, role="user") for text in SAID]))

        page = asyncio.run(adapter.history(TARGET, before=None, count=2))

        assert [entry.text for entry in page.entries] == ["the sixth thing", "the fifth thing"]
        assert page.older is True
        assert page.read_at is not None

    def test_a_first_turn_that_has_written_no_record_yet_is_not_an_empty_page(
        self, tmp_path: Path
    ) -> None:
        """#73: the file appears at the first turn, and until then nobody can read it."""
        adapter = adapter_holding(tmp_path / "not-written-yet.jsonl")

        page = asyncio.run(adapter.history(TARGET, before=None, count=5))

        assert page == HistoryPage()

    def test_a_transcript_this_engine_was_never_told_about_is_not_an_empty_page(self) -> None:
        """`read_at=None` is the stated contract for "this lane holds no record"."""
        adapter = adapter_holding(None)

        page = asyncio.run(adapter.history(TARGET, before=None, count=5))

        assert page == HistoryPage()

    def test_a_transcript_that_could_not_be_read_raises(self, tmp_path: Path) -> None:
        """A source that exists and cannot be read is the lane's own words, not a page."""
        adapter = adapter_holding(tmp_path)  # a directory: it stats, and will not read

        with pytest.raises(LaneUnavailable):
            asyncio.run(adapter.history(TARGET, before=None, count=5))


class TestTheCodexLane:
    """The thread's turns, windowed — the same walk the roster tail reads."""

    def target(self) -> SessionTarget:
        return SessionTarget(agent=AgentKind.CODEX, session_id=THREAD, pid=6548)

    def turns(self) -> list[dict]:
        return [
            turn(told(SAID[0]), spoke(SAID[1])),
            turn(told(SAID[2]), spoke(SAID[3])),
            turn(told(SAID[4]), spoke(SAID[5])),
        ]

    def test_ordinals_count_from_the_oldest_entry_across_every_turn(self) -> None:
        entries = thread_tail.visible({"turns": self.turns()})

        assert [(entry.ordinal, entry.text) for entry in entries] == list(enumerate(SAID))

    def test_one_page_of_a_real_thread(self) -> None:
        daemon = TurnedDaemon({THREAD: stopped()}, {THREAD: self.turns()})
        adapter = adapter_over(daemon, home=TEST_HOME)

        page = asyncio.run(adapter.history(self.target(), before=None, count=2))

        assert [entry.text for entry in page.entries] == ["the sixth thing", "the fifth thing"]
        assert page.older is True

    def test_the_page_is_read_from_the_thread_and_nothing_else(self) -> None:
        """Not a roster read: no enumeration, and one `thread/read` with turns."""
        daemon = TurnedDaemon({THREAD: stopped()}, {THREAD: self.turns()})
        adapter = adapter_over(daemon, home=TEST_HOME)

        asyncio.run(adapter.history(self.target(), before=None, count=5))

        assert daemon.deep == [THREAD]

    def test_a_thread_the_daemon_does_not_hold_refuses_rather_than_pages_emptily(self) -> None:
        """Its rollout is on disk; reading that would be a second, worse source."""
        daemon = TurnedDaemon({}, {})
        adapter = adapter_over(daemon, home=TEST_HOME)

        with pytest.raises(LaneUnavailable):
            asyncio.run(adapter.history(self.target(), before=None, count=5))

    def test_a_session_with_no_thread_id_yet_holds_no_record_to_read(self) -> None:
        daemon = TurnedDaemon({THREAD: stopped()}, {THREAD: self.turns()})
        adapter = adapter_over(daemon, home=TEST_HOME)

        page = asyncio.run(
            adapter.history(
                SessionTarget(agent=AgentKind.CODEX, session_id=None, pid=6548),
                before=None,
                count=5,
            )
        )

        assert page == HistoryPage()


class TestBothRealAdaptersCarryTheVerb:
    """The seam's shape, on the two things that really implement it.

    `test_seam_contracts.py` holds the fake to the same rule; this holds the
    adapters, because a lane that quietly spelled `before` differently would
    only fail where the hub calls it.
    """

    @pytest.mark.parametrize(
        "adapter",
        [ClaudeAgentAdapter, CodexAgentAdapter],
        ids=["claude", "codex"],
    )
    def test_the_lane_implements_history_the_way_the_seam_publishes_it(self, adapter: type) -> None:
        assert _shape(adapter.history) == _shape(AgentAdapter.history)


class TestBothLanesPageIdentically:
    """One windowing function is only worth having if this holds."""

    def test_identical_records_give_identical_pages(self, tmp_path: Path) -> None:
        claude = adapter_holding(
            transcript(
                tmp_path,
                [
                    said(text, role="user" if index % 2 == 0 else "assistant")
                    for index, text in enumerate(SAID)
                ],
            )
        )
        daemon = TurnedDaemon(
            {THREAD: stopped()},
            {
                THREAD: [
                    turn(told(SAID[0]), spoke(SAID[1])),
                    turn(told(SAID[2]), spoke(SAID[3])),
                    turn(told(SAID[4]), spoke(SAID[5])),
                ]
            },
        )
        codex = adapter_over(daemon, home=TEST_HOME)
        codex_target = SessionTarget(agent=AgentKind.CODEX, session_id=THREAD, pid=6548)

        for before in (None, 4, 2, 0):
            from_claude = asyncio.run(claude.history(TARGET, before=before, count=2))
            from_codex = asyncio.run(codex.history(codex_target, before=before, count=2))

            assert [
                (entry.ordinal, str(entry.role), entry.text) for entry in from_claude.entries
            ] == [(entry.ordinal, str(entry.role), entry.text) for entry in from_codex.entries]
            assert from_claude.older == from_codex.older

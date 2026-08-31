"""Where the Claude lane's `Progress` and `last_activity` come from (#76).

The reading itself is pure and tested against fragments in
`test_claude_transcript_tail.py`. This is the wiring: the roster row carries what
the same one transcript read already knows, and the per-target verb answers about
a Session the cadence deliberately skips.

The two are deliberately different, and both are here: `discover` runs every five
seconds over the whole machine and reads nothing for a Session mid-turn, while
`inspect` is asked about one Session because somebody wants to know now — which
is most often exactly while it works.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fakes import FakeCall, FakeCompanionChannel, capture_for
from gpt_voicecoding.adapters.agent.claude.adapter import ClaudeAgentAdapter, SessionReport
from gpt_voicecoding.config import of
from gpt_voicecoding.engine.composition import Engine
from gpt_voicecoding.seams.agent import (
    LaneDiscovery,
    ProgressAvailability,
    ProgressCapture,
    ProgressOmission,
    ProgressRole,
    SessionInspection,
    SessionState,
    WaitingFor,
    WaitingKind,
)
from gpt_voicecoding.seams.events import EventSink
from gpt_voicecoding.seams.identity import AgentKind
from test_claude_stop_analysis import called, said, turn
from test_claude_stop_wiring import ROSTER_WAITING, TARGET, adapter_holding, roster, transcript

__all__ = ["roster"]  # the fixture is imported, and ruff must see it used


def stamped(text: str, *, role: str = "assistant", at: str) -> dict[str, object]:
    """One visible record with the `timestamp` Claude Code writes on every one."""
    record = said(text, role=role)
    record["timestamp"] = at
    return record


#: What the roster alone says about a Session it is not calling `waiting`.
NOT_WAITING = WaitingFor()


def row(state: SessionState, waiting: WaitingFor = NOT_WAITING) -> SessionInspection:
    return SessionInspection(
        target=TARGET, workspace=Path("/tmp/workspace"), state=state, waiting_for=waiting
    )


class TestTheRosterRow:
    """The cheap projection: what the cadence already read, carried on the row."""

    def test_a_stopped_row_says_what_the_session_has_been_saying(
        self, tmp_path: Path, roster
    ) -> None:
        adapter = adapter_holding(transcript(tmp_path, list(turn())))
        roster(LaneDiscovery(rows=(row(SessionState.IDLE),)))

        found = asyncio.run(adapter.discover()).rows[0]

        assert found.progress.availability is ProgressAvailability.READABLE
        assert [(entry.role, entry.text) for entry in found.progress.recent] == [
            (ProgressRole.USER, "do the thing"),
            (ProgressRole.ASSISTANT, "done"),
        ]
        assert found.progress.omission is ProgressOmission.NONE

    def test_a_5482_byte_newest_entry_is_captured_whole(self, tmp_path: Path, roster) -> None:
        """#147's deterministic regression: 5,482 encoded bytes exceed the old 3 KB."""
        text = "x" * 5_447  # JSON role/text/list overhead is 35 bytes.
        adapter = adapter_holding(transcript(tmp_path, [said(text)]))
        roster(LaneDiscovery(rows=(row(SessionState.IDLE),)))

        found = asyncio.run(adapter.discover()).rows[0]

        assert found.progress.availability is ProgressAvailability.READABLE
        assert found.progress.has_history is True
        assert [entry.text for entry in found.progress.recent] == [text]
        assert found.progress.omission is ProgressOmission.NONE

    def test_a_newest_entry_larger_than_capture_is_history_not_silence(
        self, tmp_path: Path, roster
    ) -> None:
        adapter = adapter_holding(
            transcript(tmp_path, [said("x" * 2_000)]),
            progress_capture=capture_for(1_024),
        )
        roster(LaneDiscovery(rows=(row(SessionState.IDLE),)))

        found = asyncio.run(adapter.discover()).rows[0]

        assert found.progress.availability is ProgressAvailability.READABLE
        assert found.progress.has_history is True
        assert found.progress.recent == ()
        assert found.progress.omission is ProgressOmission.NEWEST_OVERSIZE

    def test_a_transcript_that_cannot_be_opened_is_unreadable(self, tmp_path: Path, roster) -> None:
        adapter = adapter_holding(tmp_path)  # a directory cannot be read as a JSONL file
        roster(LaneDiscovery(rows=(row(SessionState.IDLE),)))

        lane = asyncio.run(adapter.discover())
        found = lane.rows[0]

        assert found.progress.availability is ProgressAvailability.UNREADABLE
        assert "could not read" in (found.progress.reason or "")
        assert "could not read" in (lane.degraded or "")

    def test_the_reading_says_when_it_was_taken(self, tmp_path: Path, roster) -> None:
        """`read_at` is on the value: a progress line's meaning is when it was true."""
        adapter = adapter_holding(transcript(tmp_path, list(turn())))
        roster(LaneDiscovery(rows=(row(SessionState.IDLE),)))

        before = datetime.now(UTC)
        found = asyncio.run(adapter.discover()).rows[0]

        assert found.progress is not None
        assert found.progress.read_at is not None
        assert before <= found.progress.read_at <= datetime.now(UTC)

    def test_last_activity_comes_off_the_record_not_off_the_roster(
        self, tmp_path: Path, roster
    ) -> None:
        """`startedAt` is on the roster row and is deliberately not read as this."""
        adapter = adapter_holding(
            transcript(tmp_path, [stamped("done", at="2026-08-26T04:30:09.000Z")])
        )
        roster(LaneDiscovery(rows=(row(SessionState.IDLE),)))

        found = asyncio.run(adapter.discover()).rows[0]

        assert found.last_activity == datetime(2026, 8, 26, 4, 30, 9, tzinfo=UTC)

    def test_a_session_mid_turn_carries_no_progress_and_no_time(
        self, tmp_path: Path, roster
    ) -> None:
        """The gate is #75's, and #76 rides on it rather than widening it.

        `None` is "not read" — a surface may not render it as a Session that has
        said nothing, and it may certainly not render a time nobody read.
        """
        adapter = adapter_holding(transcript(tmp_path, list(turn())))
        roster(LaneDiscovery(rows=(row(SessionState.RUNNING),)))

        found = asyncio.run(adapter.discover()).rows[0]

        assert found.progress.availability is ProgressAvailability.NOT_READ
        assert found.last_activity is None

    def test_a_session_with_no_transcript_yet_is_unread_rather_than_empty(self, roster) -> None:
        """A Session has no transcript file at all until it takes a turn (#73)."""
        adapter = adapter_holding(None)
        roster(LaneDiscovery(rows=(row(SessionState.IDLE),)))

        found = asyncio.run(adapter.discover()).rows[0]

        assert found.progress.availability is ProgressAvailability.NOT_READ

    def test_a_session_that_has_written_nothing_readable_is_read_and_empty(
        self, tmp_path: Path, roster
    ) -> None:
        """The other side of that: a file that was read and held no conversation."""
        adapter = adapter_holding(
            transcript(tmp_path, [called("Bash", "b1", {"description": "push"})])
        )
        roster(LaneDiscovery(rows=(row(SessionState.WAITING, ROSTER_WAITING),)))

        found = asyncio.run(adapter.discover()).rows[0]

        assert found.progress is not None
        assert found.progress.recent == ()


class TestThePerTargetRead:
    """The verb beside it: asked about one Session, whatever it is doing."""

    def test_a_running_session_can_still_be_asked_how_far_along_it_is(
        self, tmp_path: Path, roster
    ) -> None:
        """The cadence skips it; the verb does not. That is the whole difference."""
        adapter = adapter_holding(transcript(tmp_path, list(turn())))
        roster(LaneDiscovery(rows=(row(SessionState.RUNNING),)))

        found = asyncio.run(adapter.inspect(TARGET))

        assert found.progress is not None
        assert [entry.text for entry in found.progress.recent] == ["do the thing", "done"]

    def test_a_5482_byte_entry_is_captured_by_exact_inspect(self, tmp_path: Path, roster) -> None:
        text = "x" * 5_447
        adapter = adapter_holding(transcript(tmp_path, [said(text)]))
        roster(LaneDiscovery(rows=(row(SessionState.RUNNING),)))

        found = asyncio.run(adapter.inspect(TARGET))

        assert [entry.text for entry in found.progress.recent] == [text]

    def test_composition_supplies_the_capture_used_by_real_inspect(
        self,
        tmp_path: Path,
        roster,
    ) -> None:
        """Prove the derived ceiling through composition and the public Agent seam."""

        def real_agent(
            *,
            progress_capture: ProgressCapture,
            sink: EventSink | None = None,
        ) -> ClaudeAgentAdapter:
            return ClaudeAgentAdapter(progress_capture=progress_capture, sink=sink)

        factories = {
            "test:call": FakeCall,
            "test:channel": FakeCompanionChannel,
            "test:agent": real_agent,
        }
        engine = Engine.assemble(
            of(
                {
                    "engine": {},
                    "adapters": {
                        "call": "test:call",
                        "companion_channel": "test:channel",
                        "agents": {"claude": "test:agent"},
                    },
                    "delegate": {"model": "a-model"},
                    "log": {
                        "max_bytes": 1,
                        "retained_files": 0,
                        "stripped_environment_prefixes": [],
                    },
                }
            ),
            factory_of=factories.__getitem__,
        )
        adapter = engine.adapters.agents[AgentKind.CLAUDE]
        assert isinstance(adapter, ClaudeAgentAdapter)
        path = transcript(tmp_path, [said("x" * 5_447)])
        adapter._reported[TARGET] = SessionReport(  # noqa: SLF001 - seed registration
            session_id=TARGET.session_id,
            pid=TARGET.pid,
            transcript_path=path,
        )
        roster(LaneDiscovery(rows=(row(SessionState.RUNNING),)))

        fitting = asyncio.run(adapter.inspect(TARGET)).progress

        assert [entry.text for entry in fitting.recent] == ["x" * 5_447]

        transcript(tmp_path, [said("x" * 65_000)])  # 65,035 encoded bytes.
        oversize = asyncio.run(adapter.inspect(TARGET)).progress

        assert oversize.has_history is True
        assert oversize.recent == ()
        assert oversize.omission is ProgressOmission.NEWEST_OVERSIZE

    def test_exact_inspect_preserves_oversize_history(self, tmp_path: Path, roster) -> None:
        adapter = adapter_holding(
            transcript(tmp_path, [said("x" * 2_000)]),
            progress_capture=capture_for(1_024),
        )
        roster(LaneDiscovery(rows=(row(SessionState.RUNNING),)))

        found = asyncio.run(adapter.inspect(TARGET))

        assert found.progress.has_history is True
        assert found.progress.recent == ()
        assert found.progress.omission is ProgressOmission.NEWEST_OVERSIZE

    def test_one_inspect_is_one_source_read(
        self, tmp_path: Path, roster, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The enumeration inside inspect already read this stopped Session."""
        adapter = adapter_holding(transcript(tmp_path, list(turn())))
        roster(LaneDiscovery(rows=(row(SessionState.IDLE),)))
        reads = 0
        original = adapter._transcripts.records  # noqa: SLF001

        def counted(path: Path | None) -> object:
            nonlocal reads
            reads += 1
            return original(path)

        monkeypatch.setattr(adapter._transcripts, "records", counted)  # noqa: SLF001
        found = asyncio.run(adapter.inspect(TARGET))

        assert reads == 1
        assert found.progress is not None

    def test_a_transcript_that_moved_between_the_two_is_read_again(
        self, tmp_path: Path, roster
    ) -> None:
        """The other half of the same rule: a changed file is a changed answer."""
        path = transcript(tmp_path, list(turn()))
        adapter = adapter_holding(path)
        roster(LaneDiscovery(rows=(row(SessionState.IDLE),)))

        asyncio.run(adapter.discover())
        transcript(tmp_path, [*turn(), said("and one more thing", role="user")])
        found = asyncio.run(adapter.inspect(TARGET))

        assert found.progress is not None
        assert [entry.text for entry in found.progress.recent][-1] == "and one more thing"

    def test_a_session_that_is_gone_is_ended_and_says_nothing_about_progress(self, roster) -> None:
        """`ENDED` carries no reading, because nobody read one."""
        adapter = adapter_holding(None)
        roster(LaneDiscovery())

        found = asyncio.run(adapter.inspect(TARGET))

        assert found.progress.availability is ProgressAvailability.NOT_READ
        assert found.last_activity is None

    def test_a_working_session_is_read_for_progress_and_not_for_a_stop(
        self, tmp_path: Path, roster
    ) -> None:
        """An outstanding tool call mid-turn is a tool running, not a dialog (#75).

        The two questions have different answers here, and reading the tail for
        both would report a working Session as one waiting on the user.
        """
        adapter = adapter_holding(
            transcript(tmp_path, [*turn(), called("Bash", "b1", {"description": "push"})])
        )
        roster(LaneDiscovery(rows=(row(SessionState.RUNNING),)))

        found = asyncio.run(adapter.inspect(TARGET))

        assert found.waiting_for.kind is WaitingKind.NONE
        assert found.progress is not None

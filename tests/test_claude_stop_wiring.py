"""Where the two sources of "what it stopped on" meet, and who wins (#75).

The parser is pure and tested against fragments in `test_claude_stop_analysis.py`.
This is the other half: the adapter reads a Session's own transcript, ranks what
it found against any dialog parked on this engine's approval socket, and puts the
answer on both paths Bridge Core actually reads — the roster row it renders in
`sessions`, and the `SessionStopped` it renders the Stop Notice from.

The ranking is a table because the two sources can disagree, and the four rows
are the only judgment #75 has outside the parser.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from gpt_voicecoding.adapters.agent.claude import adapter as claude_adapter
from gpt_voicecoding.adapters.agent.claude.adapter import ClaudeAgentAdapter, SessionReport
from gpt_voicecoding.adapters.agent.claude.transcript import TranscriptReader
from gpt_voicecoding.seams.agent import (
    ApprovalRequest,
    LaneDiscovery,
    LaneUnavailable,
    SessionInspection,
    SessionState,
    WaitingFor,
    WaitingKind,
)
from gpt_voicecoding.seams.identity import AgentKind, SessionTarget
from test_claude_stop_analysis import asked, called, turn

SESSION = "d3a776ae-3b60-437d-bc70-ba57a2b280c6"
TARGET = SessionTarget(agent=AgentKind.CLAUDE, session_id=SESSION, pid=3538)

#: What the roster alone says about a Session it calls `waiting`: something is
#: being waited on and the command does not carry what (`discovery.py`).
ROSTER_WAITING = WaitingFor(kind=WaitingKind.UNKNOWN, caught_up=False)


class _Parked:
    """One dialog held open, as `ApprovalListener._waiting` holds it."""

    def __init__(self, request: ApprovalRequest) -> None:
        self.request = request


def transcript(tmp_path: Path, records: list[dict[str, Any]]) -> Path:
    path = tmp_path / "session.jsonl"
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    return path


def adapter_holding(
    transcript_path: Path | None = None, *, parked: tuple[ApprovalRequest, ...] = ()
) -> ClaudeAgentAdapter:
    """An adapter that has heard one Session's registration and holds `parked`.

    The registration is seeded rather than driven through the hook, because what
    is under test is the ranking and not the `SessionStart` wire — which
    `test_claude_registration.py` owns. `pending()` is stubbed for the same
    reason: parking a real dialog needs a real hook process on a real socket, and
    `test_claude_approval.py` already proves that half.
    """
    adapter = ClaudeAgentAdapter()
    adapter._reported[TARGET] = SessionReport(  # noqa: SLF001 - seeding one registration
        session_id=SESSION, pid=TARGET.pid, transcript_path=transcript_path
    )
    for index, request in enumerate(parked):
        # Parked the way a hook parks one, minus the socket: `newest_for` reads
        # this dict, and `test_claude_approval.py` owns proving a real hook gets
        # a request into it.
        adapter._approvals._waiting[request.approval_id] = _Parked(request)  # noqa: SLF001
        del index
    return adapter


@pytest.fixture
def roster(monkeypatch: pytest.MonkeyPatch):
    """Make `claude agents --json` answer with exactly this lane, for one test.

    The command itself is `discovery.py`'s and is tested there; what is under
    test here is what the adapter adds to the rows it comes back with.
    """

    def answering(lane: LaneDiscovery) -> None:
        async def stub() -> LaneDiscovery:
            return lane

        monkeypatch.setattr(claude_adapter.claude_discovery, "discover", stub)

    return answering


def dialog(tool_name: str = "Bash", detail: str = "push the branch") -> ApprovalRequest:
    """One `PermissionRequest` hook's dialog, as `approval.request_from` builds it."""
    return ApprovalRequest(approval_id="a-1", target=TARGET, tool_name=tool_name, detail=detail)


class TestTheRanking:
    """Four rows. The transcript and the parked dialog are ranked, never merged."""

    def test_a_readable_question_wins_outright(self, tmp_path: Path) -> None:
        """A dialog held open by a hook never overrides a decision the user was asked.

        The reference implementation's precedence
        (`legacy@1d32845:bridge/transcript.py:1691-1692`), carried across the
        second source v2 has and legacy did not.
        """
        adapter = adapter_holding(
            transcript(tmp_path, [*turn(), asked("q1", ("Which base?", ["main", "feature"]))]),
            parked=(dialog(),),
        )
        waiting = adapter.stopped_on(TARGET, ROSTER_WAITING)
        assert waiting.kind is WaitingKind.QUESTION
        assert waiting.approval_id is None
        assert waiting.tool_name is None

    def test_a_permission_read_from_the_record_keeps_its_fields_and_gains_the_handle(
        self, tmp_path: Path
    ) -> None:
        """The handle is the one thing a transcript can never carry."""
        adapter = adapter_holding(
            transcript(tmp_path, [*turn(), called("Edit", "e1", {"file_path": "/tmp/notes.md"})]),
            parked=(dialog(tool_name="Bash", detail="something else"),),
        )
        waiting = adapter.stopped_on(TARGET, ROSTER_WAITING)
        assert waiting.kind is WaitingKind.PERMISSION
        # The record's own reading is not overwritten by the dialog's.
        assert waiting.tool_name == "Edit"
        assert waiting.detail == "/tmp/notes.md"
        assert waiting.approval_id == "a-1"
        assert waiting.caught_up is True

    def test_a_call_the_record_could_not_describe_takes_the_dialog_s_words(
        self, tmp_path: Path
    ) -> None:
        """Filling a gap is not overwriting: the parser said nothing on this field."""
        adapter = adapter_holding(
            transcript(tmp_path, [*turn(), called("Bash", "b1", {"prompt": "unreadable"})]),
            parked=(dialog(tool_name="Bash", detail="push the branch"),),
        )
        waiting = adapter.stopped_on(TARGET, ROSTER_WAITING)
        assert waiting.tool_name == "Bash"
        assert waiting.detail == "push the branch"

    def test_a_dialog_the_record_has_not_caught_up_with_is_still_a_stop(
        self, tmp_path: Path
    ) -> None:
        """The first-turn case, **adapted** from the Notification sentence.

        `PermissionRequest` fires when the dialog opens, before the `tool_use`
        record is flushed. Legacy scraped the tool name out of English
        (`legacy@1d32845:bridge/daemon.py:143-145,2049-2051`); the hook carries
        the same fact plus a handle, from the process holding the dialog.
        """
        adapter = adapter_holding(transcript(tmp_path, turn()), parked=(dialog(),))
        waiting = adapter.stopped_on(TARGET, ROSTER_WAITING)
        assert waiting.kind is WaitingKind.PERMISSION
        assert waiting.tool_name == "Bash"
        assert waiting.detail == "push the branch"
        assert waiting.approval_id == "a-1"
        # `caught_up=True` is what the seam's own invariant forces and what it
        # means: the reader read the record that says so, and that record is the
        # hook payload rather than the transcript (`seams/agent.py:180-186`).
        assert waiting.caught_up is True

    def test_a_dialog_on_a_session_the_roster_calls_finished_is_still_a_stop(
        self, tmp_path: Path
    ) -> None:
        """A dialog on screen is a stop whatever the roster and the record say."""
        adapter = adapter_holding(transcript(tmp_path, turn()), parked=(dialog(),))
        waiting = adapter.stopped_on(TARGET, WaitingFor())
        assert waiting.kind is WaitingKind.PERMISSION
        assert waiting.approval_id == "a-1"

    def test_with_no_dialog_the_parser_s_answer_stands_untouched(self, tmp_path: Path) -> None:
        adapter = adapter_holding(
            transcript(tmp_path, [*turn(), called("Bash", "b1", {"description": "push"})])
        )
        waiting = adapter.stopped_on(TARGET, ROSTER_WAITING)
        assert waiting.kind is WaitingKind.PERMISSION
        assert waiting.approval_id is None

    def test_the_newest_of_two_dialogs_is_the_one_it_is_held_up_on(self, tmp_path: Path) -> None:
        adapter = adapter_holding(
            transcript(tmp_path, turn()),
            parked=(
                dialog(tool_name="Read", detail="/tmp/first"),
                replace(dialog(tool_name="Edit", detail="/tmp/second"), approval_id="a-2"),
            ),
        )
        waiting = adapter.stopped_on(TARGET, ROSTER_WAITING)
        assert waiting.tool_name == "Edit"
        assert waiting.approval_id == "a-2"

    def test_another_session_s_dialog_is_not_this_session_s_stop(self, tmp_path: Path) -> None:
        """`--resume` forks two processes under one session id, with two dialogs."""
        other = replace(TARGET, pid=9999)
        adapter = adapter_holding(
            transcript(tmp_path, turn()), parked=(replace(dialog(), target=other),)
        )
        assert adapter.stopped_on(TARGET, ROSTER_WAITING) == ROSTER_WAITING


class TestWhenTheRecordSaysNothing:
    """The roster's own word stands, and `NONE` is not `UNKNOWN`."""

    def test_a_finished_turn_stays_a_finished_turn(self, tmp_path: Path) -> None:
        adapter = adapter_holding(transcript(tmp_path, turn()))
        assert adapter.stopped_on(TARGET, WaitingFor()).kind is WaitingKind.NONE

    def test_a_waiting_session_whose_record_says_nothing_is_asked_again(
        self, tmp_path: Path
    ) -> None:
        """`UNKNOWN` with `caught_up=False` is the seam's word for *ask again*."""
        adapter = adapter_holding(transcript(tmp_path, turn()))
        waiting = adapter.stopped_on(TARGET, ROSTER_WAITING)
        assert waiting.kind is WaitingKind.UNKNOWN
        assert waiting.caught_up is False

    def test_a_session_that_never_registered_leaves_the_roster_s_word_alone(self) -> None:
        """No `SessionStart` hook ran, so there is no path and nothing was read."""
        adapter = ClaudeAgentAdapter()
        assert adapter.stopped_on(TARGET, ROSTER_WAITING) == ROSTER_WAITING

    def test_a_transcript_that_does_not_exist_yet_is_not_an_empty_one(self, tmp_path: Path) -> None:
        """A Session's first turn creates the file (#73); before that, nobody looked."""
        adapter = adapter_holding(tmp_path / "never-written.jsonl")
        assert adapter.stopped_on(TARGET, ROSTER_WAITING) == ROSTER_WAITING


class TestTheRosterRow:
    """`discover` is the verb Bridge Core calls, so it is where this lands."""

    def row(self, state: SessionState, waiting: WaitingFor) -> SessionInspection:
        return SessionInspection(
            target=TARGET, workspace=Path("/tmp/workspace"), state=state, waiting_for=waiting
        )

    def test_a_stopped_row_says_what_it_stopped_on(self, tmp_path: Path, roster) -> None:
        adapter = adapter_holding(
            transcript(tmp_path, [*turn(), asked("q1", ("Which base?", ["main", "feature"]))])
        )
        roster(LaneDiscovery(rows=(self.row(SessionState.WAITING, ROSTER_WAITING),)))
        lane = asyncio.run(adapter.discover())
        assert lane.rows[0].waiting_for.kind is WaitingKind.QUESTION
        assert [option.text for option in lane.rows[0].waiting_for.options] == [
            "main",
            "feature",
        ]

    def test_a_session_mid_turn_is_not_read_at_all(self, tmp_path: Path, roster) -> None:
        """A Session that is working is not stopped on anything, so no file is opened.

        This is what keeps the five-second cadence off the hot path: on a machine
        of busy Sessions it costs one roster command and no reads.
        """
        path = transcript(tmp_path, [*turn(), called("Bash", "b1", {"description": "push"})])
        adapter = adapter_holding(path)
        opened: list[Path | None] = []
        original = adapter._transcripts.records  # noqa: SLF001

        def watched(argument: Path | None) -> Any:
            opened.append(argument)
            return original(argument)

        adapter._transcripts.records = watched  # type: ignore[method-assign]  # noqa: SLF001
        roster(LaneDiscovery(rows=(self.row(SessionState.RUNNING, WaitingFor()),)))
        lane = asyncio.run(adapter.discover())
        assert opened == []
        assert lane.rows[0].waiting_for.kind is WaitingKind.NONE

    def test_a_lane_that_could_not_look_is_passed_through_whole(self, roster) -> None:
        """A failed enumeration has no rows to read, and must not gain any."""
        adapter = adapter_holding()
        roster(LaneDiscovery(error="`claude` is not on the PATH"))
        lane = asyncio.run(adapter.discover())
        assert lane.error == "`claude` is not on the PATH"
        assert lane.rows == ()

    def test_inspect_answers_from_the_same_rows(self, tmp_path: Path, roster) -> None:
        """One reader, one shape — `inspect` reads what `discover` produced."""
        adapter = adapter_holding(
            transcript(tmp_path, [*turn(), called("Bash", "b1", {"description": "push"})])
        )
        roster(LaneDiscovery(rows=(self.row(SessionState.WAITING, ROSTER_WAITING),)))
        found = asyncio.run(adapter.inspect(TARGET))
        assert found.waiting_for.kind is WaitingKind.PERMISSION
        assert found.waiting_for.detail == "push"

    def test_a_lane_that_could_not_look_still_raises_from_inspect(self, roster) -> None:
        """#74's rule survives the overlay: `LaneUnavailable` is not `UNKNOWN`."""
        adapter = adapter_holding()
        roster(LaneDiscovery(error="`claude` is not on the PATH"))
        with pytest.raises(LaneUnavailable):
            asyncio.run(adapter.inspect(TARGET))


class TestTheStopNotice:
    """The other path: the event Bridge Core renders the Stop Notice from."""

    def test_a_stop_carries_what_it_stopped_on(self, tmp_path: Path) -> None:
        """`SessionStopped.waiting_for` replaces the free-text `detail` (#74, #75)."""
        from gpt_voicecoding.adapters.agent.claude.settings import ClaudeSettings
        from gpt_voicecoding.adapters.agent.claude.window import ReplyWindowWatcher

        raised: list[Any] = []
        watcher = ReplyWindowWatcher(
            settings=ClaudeSettings(),
            emit=raised.append,
            stopped_on=lambda target: WaitingFor(
                kind=WaitingKind.PERMISSION, tool_name="Bash", detail="push the branch"
            ),
        )
        assert watcher._what_for(TARGET).tool_name == "Bash"  # noqa: SLF001

    def test_a_reader_that_raises_costs_the_words_and_never_the_notice(self) -> None:
        """A Stop is already proven at that point; silence would be the worse loss."""
        from gpt_voicecoding.adapters.agent.claude.settings import ClaudeSettings
        from gpt_voicecoding.adapters.agent.claude.window import ReplyWindowWatcher

        def raising(target: SessionTarget) -> WaitingFor:
            raise RuntimeError("the transcript reader is broken")

        watcher = ReplyWindowWatcher(
            settings=ClaudeSettings(), emit=lambda event: None, stopped_on=raising
        )
        assert watcher._what_for(TARGET) == WaitingFor()  # noqa: SLF001


class TestReadingTheFileOnce:
    """The lane's one opener of a transcript, shared with #76."""

    def test_it_parses_once_until_the_file_changes(self, tmp_path: Path) -> None:
        path = transcript(tmp_path, turn())
        reader = TranscriptReader()
        first = reader.records(path)
        assert first is not None
        assert reader.records(path) is first

    def test_a_grown_transcript_is_read_again(self, tmp_path: Path) -> None:
        path = transcript(tmp_path, turn())
        reader = TranscriptReader()
        before = reader.records(path)
        assert before is not None
        with path.open("a", encoding="utf-8") as growing:
            growing.write(json.dumps(called("Bash", "b1", {"description": "push"})) + "\n")
        after = reader.records(path)
        assert after is not None
        assert len(after) == len(before) + 1

    def test_a_half_written_last_line_costs_itself_and_nothing_else(self, tmp_path: Path) -> None:
        """The record being appended right now, which is the ordinary case."""
        path = transcript(tmp_path, turn())
        with path.open("a", encoding="utf-8") as growing:
            growing.write('{"type": "assistant", "mess')
        records = TranscriptReader().records(path)
        assert records is not None
        assert len(records) == 2

    @pytest.mark.parametrize("line", ["not json at all", "[1, 2, 3]", '"a string"', "null", "   "])
    def test_a_line_that_is_not_a_record_is_skipped(self, tmp_path: Path, line: str) -> None:
        path = transcript(tmp_path, turn())
        with path.open("a", encoding="utf-8") as growing:
            growing.write(line + "\n")
        records = TranscriptReader().records(path)
        assert records is not None
        assert len(records) == 2

    def test_no_path_and_no_file_are_both_none_rather_than_empty(self, tmp_path: Path) -> None:
        reader = TranscriptReader()
        assert reader.records(None) is None
        assert reader.records(tmp_path / "absent.jsonl") is None

    def test_a_file_that_vanishes_drops_its_cache(self, tmp_path: Path) -> None:
        path = transcript(tmp_path, turn())
        reader = TranscriptReader()
        assert reader.records(path) is not None
        path.unlink()
        assert reader.records(path) is None

    def test_forgetting_one_session_leaves_the_others(self, tmp_path: Path) -> None:
        one = transcript(tmp_path, turn())
        other = tmp_path / "other.jsonl"
        other.write_text(json.dumps(turn()[0]) + "\n", encoding="utf-8")
        reader = TranscriptReader()
        kept = reader.records(other)
        reader.records(one)
        reader.forget(one)
        reader.forget(None)
        assert reader.records(other) is kept

"""The transcript tail — an incremental read of a file another program is writing.

Built here for the Notice Relay's readback and deliberately kept general, because
the Stop Notice will want the same instrument. The behaviours under test are the
ones that make a tail *trustworthy* rather than merely working: a half-written
line is not a record, a pre-send offset never skips what was appended after it,
and a file that moves out from under the reader does not silently become an empty
transcript.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gpt_voicecoding.adapters.agent.claude.transcript import (
    TranscriptError,
    TranscriptTail,
    locate_transcript,
)

SESSION = "430b0def-38ef-4783-8d57-d800710d83bd"


def project(root: Path, name: str = "-Users-someone-work") -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def append(path: Path, *records: dict[str, object]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


# -- locating ------------------------------------------------------------


def test_the_transcript_is_found_by_session_id_without_encoding_the_cwd(tmp_path: Path) -> None:
    """Globbing sidesteps re-implementing Claude Code's cwd-to-directory encoding."""
    wanted = project(tmp_path) / f"{SESSION}.jsonl"
    wanted.touch()
    (project(tmp_path, "-Users-someone-elsewhere") / "another-session.jsonl").touch()

    assert locate_transcript(tmp_path, SESSION) == wanted


def test_no_transcript_at_all_is_refused_by_session_id(tmp_path: Path) -> None:
    with pytest.raises(TranscriptError) as refused:
        locate_transcript(tmp_path, SESSION)

    assert SESSION in str(refused.value)


def test_two_transcripts_for_one_session_are_refused_rather_than_picked_between(
    tmp_path: Path,
) -> None:
    """Exactly one hit or none. Choosing would be guessing which one is current."""
    (project(tmp_path, "-Users-someone-here") / f"{SESSION}.jsonl").touch()
    (project(tmp_path, "-Users-someone-there") / f"{SESSION}.jsonl").touch()

    with pytest.raises(TranscriptError) as refused:
        locate_transcript(tmp_path, SESSION)

    assert "2" in str(refused.value)


# -- tailing -------------------------------------------------------------


def test_a_tail_opened_at_the_end_sees_only_what_arrives_after_it(tmp_path: Path) -> None:
    path = project(tmp_path) / f"{SESSION}.jsonl"
    append(path, {"before": True})

    tail = TranscriptTail.opened_at_end(tmp_path, SESSION)
    append(path, {"after": True})

    assert list(tail.records()) == [{"after": True}]


def test_a_tail_does_not_re_read_what_it_has_already_returned(tmp_path: Path) -> None:
    path = project(tmp_path) / f"{SESSION}.jsonl"
    path.touch()
    tail = TranscriptTail.opened_at_end(tmp_path, SESSION)

    append(path, {"one": 1})
    assert list(tail.records()) == [{"one": 1}]
    assert list(tail.records()) == []

    append(path, {"two": 2})
    assert list(tail.records()) == [{"two": 2}]


def test_a_half_written_line_is_not_a_record_and_is_read_once_it_is_whole(
    tmp_path: Path,
) -> None:
    """The writer is another process, so a trailing partial line is an ordinary state."""
    path = project(tmp_path) / f"{SESSION}.jsonl"
    path.touch()
    tail = TranscriptTail.opened_at_end(tmp_path, SESSION)

    path.write_text('{"whole": 1}\n{"partial": ', encoding="utf-8")
    assert list(tail.records()) == [{"whole": 1}]

    with path.open("a", encoding="utf-8") as handle:
        handle.write('2}\n')
    assert list(tail.records()) == [{"partial": 2}]


def test_an_unreadable_line_is_stepped_over_rather_than_stopping_the_tail(
    tmp_path: Path,
) -> None:
    path = project(tmp_path) / f"{SESSION}.jsonl"
    path.touch()
    tail = TranscriptTail.opened_at_end(tmp_path, SESSION)

    path.write_text('not json\n{"good": 1}\n[1,2]\n', encoding="utf-8")

    assert list(tail.records()) == [{"good": 1}]


def test_a_transcript_that_moves_is_followed_rather_than_read_as_empty(tmp_path: Path) -> None:
    """A Session that changes cwd gets a new project directory. Same file, new path."""
    here = project(tmp_path, "-Users-someone-here")
    path = here / f"{SESSION}.jsonl"
    append(path, {"before": True})
    tail = TranscriptTail.opened_at_end(tmp_path, SESSION)
    append(path, {"after": True})

    there = project(tmp_path, "-Users-someone-there")
    path.rename(there / f"{SESSION}.jsonl")

    assert list(tail.records()) == [{"after": True}]


def test_a_transcript_that_shrinks_is_re_read_from_the_start(tmp_path: Path) -> None:
    """Truncation means the offset is meaningless; keeping it would skip real records."""
    path = project(tmp_path) / f"{SESSION}.jsonl"
    append(path, {"one": 1}, {"two": 2})
    tail = TranscriptTail.opened_at_end(tmp_path, SESSION)

    path.write_text(json.dumps({"fresh": True}) + "\n", encoding="utf-8")

    assert list(tail.records()) == [{"fresh": True}]


def test_a_tail_over_a_transcript_that_does_not_exist_yet_yields_nothing(
    tmp_path: Path,
) -> None:
    """Refusing to open is the caller's decision; a tail that cannot read is just quiet."""
    path = project(tmp_path) / f"{SESSION}.jsonl"
    path.touch()
    tail = TranscriptTail.opened_at_end(tmp_path, SESSION)
    path.unlink()

    assert list(tail.records()) == []


def test_a_tail_opens_on_a_session_that_has_written_nothing_yet(tmp_path: Path) -> None:
    """A freshly launched Session has no transcript file until its first record.

    The record this tail exists to read may be the one that creates the file, so
    refusing here would make a brand-new Session permanently unprovable.
    """
    tail = TranscriptTail.opened_at_end(tmp_path, SESSION)
    assert list(tail.records()) == []

    path = project(tmp_path) / f"{SESSION}.jsonl"
    append(path, {"first": True})

    assert list(tail.records()) == [{"first": True}]


def test_a_tail_cannot_be_opened_when_two_transcripts_claim_one_session(
    tmp_path: Path,
) -> None:
    """Ambiguity stays a refusal: there is no honest way to pick between them."""
    (project(tmp_path, "-Users-someone-here") / f"{SESSION}.jsonl").touch()
    (project(tmp_path, "-Users-someone-there") / f"{SESSION}.jsonl").touch()

    with pytest.raises(TranscriptError):
        TranscriptTail.opened_at_end(tmp_path, SESSION)

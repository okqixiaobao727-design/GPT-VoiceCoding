"""How far along a Codex thread is, read out of the daemon's own answer (#76).

Every shape here was **measured**, not assumed: `thread/read` was called against
the real shared daemon on codex 0.149.1 on 2026-08-26 and its answer copied down.
That matters twice. The item types below (`reasoning`, `commandExecution` beside
`agentMessage` and `userMessage`) are what a real turn actually holds, and the
times are integers — `updatedAt: 1787712279` for a thread last touched at
2026-08-26T02:44:39Z — where the reference implementation's own reader never read
one at all.

The bound itself is `test_progress_bound.py`'s, because both lanes share it;
this file is what a Codex turn contributes to it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fakes import PROGRESS_CAPTURE, capture_for
from gpt_voicecoding.adapters.agent.codex.thread_tail import last_activity, moment, recent
from gpt_voicecoding.seams.agent import (
    ProgressEntry,
    ProgressOmission,
    ProgressPhase,
    ProgressRole,
)

#: The moment the live probe read, and the integer the daemon spelled it as.
MEASURED_SECONDS = 1787712279
MEASURED = datetime(2026, 8, 26, 2, 44, 39, tzinfo=UTC)


def spoke(text: str, *, phase: str | None = None) -> dict[str, Any]:
    """An `agentMessage`, as the daemon writes it.

    `phase` is optional here because it is optional on the wire: the item is
    `{ id, text, phase: Option<MessagePhase>, ... }`
    (`codex-rs/app-server-protocol/src/protocol/v2/item.rs:249-258`), and a
    build old enough to omit it is the case #188 briefs as a decision.
    """
    item: dict[str, Any] = {"type": "agentMessage", "id": "msg_0e4f", "text": text}
    if phase is not None:
        item["phase"] = phase
    return item


def told(text: str) -> dict[str, Any]:
    """A `userMessage`, whose words live one level down in `content`."""
    return {
        "type": "userMessage",
        "id": "01a03bf3",
        "clientId": None,
        "content": [{"type": "text", "text": text}],
    }


def turn(
    *items: dict[str, Any], status: str = "completed", turn_id: Any = "01a03bf0"
) -> dict[str, Any]:
    document: dict[str, Any] = {"status": status, "items": list(items)}
    if turn_id is not None:
        document["id"] = turn_id
    return document


def thread(*turns: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"id": "01a03be8", "status": {"type": "idle"}, "turns": list(turns), **extra}


def texts(document: dict[str, Any]) -> list[str]:
    entries, _ = recent(document, capture=PROGRESS_CAPTURE)
    return [entry.text for entry in entries]


def _entries(*turns: dict[str, Any]) -> tuple[ProgressEntry, ...]:
    entries, _ = recent(thread(*turns), capture=PROGRESS_CAPTURE)
    return entries


class TestWhatCountsAsProgress:
    """Ported selection: what was said, never the machinery of doing the work."""

    def test_both_sides_are_carried_and_each_says_which_it_is(self) -> None:
        entries, omission = recent(
            thread(turn(told("do the thing"), spoke("done"))),
            capture=PROGRESS_CAPTURE,
        )

        assert [(entry.role, entry.text) for entry in entries] == [
            (ProgressRole.USER, "do the thing"),
            (ProgressRole.ASSISTANT, "done"),
        ]
        assert omission is ProgressOmission.NONE

    def test_the_machinery_of_doing_the_work_is_not_a_report_of_it(self) -> None:
        """`reasoning` and `commandExecution` are the two a real turn is full of."""
        document = thread(
            turn(
                told("look at the diff"),
                {"type": "reasoning", "id": "rs_0e4f", "summary": [], "content": []},
                {
                    "type": "commandExecution",
                    "id": "exec-33579ceb",
                    "command": '/bin/zsh -lc "git show"',
                },
                spoke("here is what I found"),
            )
        )

        assert texts(document) == ["look at the diff", "here is what I found"]

    def test_an_item_type_this_build_has_never_seen_costs_itself(self) -> None:
        """**Adapted** from legacy, which raised: a roster row must not blank."""
        document = thread(turn(spoke("done"), {"type": "somethingNewIn0150", "id": "x"}))

        assert texts(document) == ["done"]

    def test_a_user_message_that_is_only_an_image_says_nothing(self) -> None:
        """`image` and `localImage` are the other two content shapes; neither speaks."""
        picture = told("")
        picture["content"] = [{"type": "image", "imageUrl": "data:..."}]

        assert texts(thread(turn(picture, spoke("I see it")))) == ["I see it"]

    def test_turns_are_read_in_the_order_they_happened(self) -> None:
        document = thread(turn(spoke("first")), turn(spoke("second")))

        assert texts(document) == ["first", "second"]

    def test_a_turn_still_running_contributes_what_it_has(self) -> None:
        """`turn_status` is dropped, so an in-progress turn is not a special case."""
        assert texts(thread(turn(spoke("halfway"), status="inProgress"))) == ["halfway"]

    def test_which_message_is_the_answer_is_carried_through(self) -> None:
        """#188: the reader carries `phase`; classifying it is Briefing's job."""
        document = thread(
            turn(
                told("do the thing"),
                spoke("looking at it now", phase="commentary"),
                spoke("done", phase="final_answer"),
            )
        )

        entries, _ = recent(document, capture=PROGRESS_CAPTURE)

        assert [(entry.text, entry.phase) for entry in entries] == [
            ("do the thing", None),
            ("looking at it now", ProgressPhase.COMMENTARY),
            ("done", ProgressPhase.FINAL_ANSWER),
        ]

    def test_a_build_that_names_no_phase_carries_none(self) -> None:
        """`phase` is `Option<MessagePhase>` on the wire, and absent is a value."""
        entries, _ = recent(thread(turn(spoke("done"))), capture=PROGRESS_CAPTURE)

        assert [entry.phase for entry in entries] == [None]

    def test_a_phase_this_build_has_never_seen_is_unknown(self) -> None:
        """#210: an unrecognised phase costs the reading nothing and is not the
        answer — `UNKNOWN` is what a seam enum uses instead of raising here."""
        entries, _ = recent(
            thread(turn(spoke("done", phase="somethingNewIn0152"))), capture=PROGRESS_CAPTURE
        )

        assert [entry.phase for entry in entries] == [ProgressPhase.UNKNOWN]

    def test_a_phase_that_is_not_a_string_is_unknown_rather_than_no_phase(self) -> None:
        """The source said *something*, which is a different fact from saying
        nothing — and only a build that marks no phase at all leaves `None`."""
        item = spoke("done")
        item["phase"] = 7

        entries, _ = recent(thread(turn(item)), capture=PROGRESS_CAPTURE)

        assert [entry.phase for entry in entries] == [ProgressPhase.UNKNOWN]

    def test_every_entry_names_the_turn_it_came_from(self) -> None:
        """**Ported** from `legacy@1d32845:bridge/codex.py:1405,1484-1492`: the
        turn document's `id`, attached to every message that turn held — and to
        an `agentMessage` in a turn whose opening message left no entry."""
        picture = told("")
        picture["content"] = [{"type": "image", "imageUrl": "data:..."}]
        document = thread(
            turn(told("do the thing"), spoke("done"), turn_id="turn_one"),
            turn(picture, spoke("looking at it now"), turn_id="turn_two"),
        )

        entries, _ = recent(document, capture=PROGRESS_CAPTURE)

        assert [(entry.text, entry.turn_id) for entry in entries] == [
            ("do the thing", "turn_one"),
            ("done", "turn_one"),
            ("looking at it now", "turn_two"),
        ]

    def test_a_turn_that_names_no_id_names_no_turn(self) -> None:
        """**Adapted** from legacy, which raised (`:1405-1411`): one malformed
        turn must not blank the roster row this reading is on."""
        assert [entry.turn_id for entry in _entries(turn(spoke("done"), turn_id=None))] == [None]
        assert [entry.turn_id for entry in _entries(turn(spoke("done"), turn_id=7))] == [None]
        assert [entry.turn_id for entry in _entries(turn(spoke("done"), turn_id="  "))] == [None]

    def test_the_bound_is_the_shared_one(self) -> None:
        """One bound, one type, whichever lane the row came from."""
        document = thread(turn(spoke("x" * 1_000), spoke("newest")))

        entries, omission = recent(document, capture=capture_for(1_024))

        assert [entry.text for entry in entries] == ["newest"]
        assert omission is ProgressOmission.OLDER


class TestAThreadWithNoTurns:
    """The cheap read the roster takes every tick answers no turn list at all."""

    def test_a_document_read_without_turns_is_not_an_error(self) -> None:
        document = thread()
        del document["turns"]

        assert recent(document, capture=PROGRESS_CAPTURE) == ((), ProgressOmission.NONE)

    def test_a_thread_that_has_taken_no_turn_says_nothing_yet(self) -> None:
        assert recent(thread(), capture=PROGRESS_CAPTURE) == ((), ProgressOmission.NONE)


class TestLastActivity:
    """Epoch seconds, and only what the thread itself said."""

    def test_updated_at_is_when_the_thread_last_moved(self) -> None:
        assert last_activity(thread(updatedAt=MEASURED_SECONDS)) == MEASURED

    def test_a_thread_that_named_no_time_has_none(self) -> None:
        assert last_activity(thread()) is None

    def test_a_boolean_is_not_a_time(self) -> None:
        """`True` is an `int` in Python, and one second past the epoch is nonsense."""
        assert moment(True) is None

    def test_a_shape_this_build_cannot_read_is_no_time_at_all(self) -> None:
        assert moment("2026-08-26T02:44:39Z") is None
        assert moment(None) is None

    def test_a_number_no_calendar_can_hold_is_no_time_either(self) -> None:
        """A field that moved to milliseconds would arrive as one of these."""
        assert moment(10**30) is None

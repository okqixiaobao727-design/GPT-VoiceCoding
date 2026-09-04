"""The `relay` step's #226 decision, and the engine line it rests on.

`tests/acceptance/journey.py` grades the second half of #197's `relay`
observation: `bridgectl brief` carries a Session's `undelivered` field, and the
Stop Notice published afterwards is a second reading of the same row. The two
may honestly disagree — a late proof of delivery clears the field between them
by design (`core/bridge.py::_relay_receipt`) — so what the step reads is not
"did the field survive" but "can one receipt state explain both readings", and
the engine's own clearing line is what answers it.

Two things are pinned here, because an acceptance run is an expensive and
occasional place to find either broken:

* **the decision**, over the three outcomes the step has to tell apart, and the
  fourth where no Stop was published at all;
* **the mirror**. `_undelivered_cleared_pattern` spells out a line
  `core/bridge.py` formats, the same way `UNDELIVERED_PATTERN` spells out one
  `core/briefing.py` formats and for the same reason (see
  `tests/test_journey_attribution.py`): a harness that asked the product what it
  had logged would agree with the product by construction. So the clearing line
  used below is a **real** one, taken off a real Bridge Core driven to clear a
  real row.
"""

from __future__ import annotations

import journey
import pytest

from gpt_voicecoding.seams.agent import RelayReceipt, ReplyWindow, ReplyWindowChanged
from gpt_voicecoding.seams.companion_channel import InboundText
from gpt_voicecoding.seams.delivery import Delivery, DeliveryReceipt
from hub import CLAUDE, CODEX, TEN_MINUTES, Hub

ADDRESS = str(CODEX)

#: A Stop Notice's header line, about this walk's own Session. The brief's
#: labelled lines follow it, indented, as continuation lines of one log record.
A_STOP = "2026-09-04 21:19:03,731 INFO Session stopped: /tmp/workspace — codex:abc — finished"

#: The same, about a Session this walk has nothing to do with. The engine bridges
#: every Session on the machine, so the log always carries other people's (#109).
A_STRANGERS_STOP = (
    "2026-09-04 21:19:01,004 INFO Session stopped: /tmp/elsewhere — claude:9f10:64312 — finished"
)

#: The field, as `core/briefing.py` renders it into a notice: a labelled line
#: indented under the header it belongs to.
THE_FIELD = "  undelivered: your last reply may not have arrived, because ceiling_passed"


def _a_real_clearing_line(caplog, target=CODEX) -> str:  # noqa: ANN001
    """One engine line, produced by clearing a real row on a real Bridge Core.

    The shape run `20260904T091550Z` had no line for: a Relay passes its ceiling
    and lands on the row, a second Relay's proof of delivery arrives late, and
    the field goes. Everything the step greps for has to come out of *this*,
    never out of a string this module made up.
    """
    hub = Hub(voice=False, sessions=((target, "port the log"),))
    hub.agent.outcome = Delivery.UNKNOWN
    hub.agent.reason = "no readback"
    hub.emit(InboundText(text="ship it"))
    hub.emit(ReplyWindowChanged(target=target, window=ReplyWindow.OPEN))
    hub.now += TEN_MINUTES
    hub.tick()
    assert hub.state.sessions.resolve(target).undelivered is not None
    hub.emit(InboundText(text="and this"))
    (queued,) = hub.state.relays.pending()

    caplog.set_level("INFO", logger="gpt_voicecoding.core.bridge")
    hub.emit(
        RelayReceipt(
            target=target,
            receipt=DeliveryReceipt(
                request_id=queued.request_id,
                outcome=Delivery.DELIVERED,
                reason="the Session acknowledged it",
            ),
        )
    )
    assert hub.state.sessions.resolve(target).undelivered is None

    said = (record.getMessage() for record in caplog.records)
    (line,) = [one for one in said if "no longer says so" in one]
    return line


@pytest.fixture
def a_real_clearing_line(caplog) -> str:  # noqa: ANN001
    """The clearing line for the Session this walk's `address` names."""
    return _a_real_clearing_line(caplog)


@pytest.fixture
def a_strangers_real_clearing_line(caplog) -> str:  # noqa: ANN001
    """The same line, about a Session this walk has nothing to do with."""
    return _a_real_clearing_line(caplog, target=CLAUDE)


def test_a_stop_that_carries_the_field_is_the_two_readings_agreeing() -> None:
    reading = journey._stop_notice_reading([A_STOP, THE_FIELD], address=ADDRESS)

    assert reading is journey._StopNoticeReading.CARRIED
    assert reading in journey._STOP_NOTICE_PASSES


def test_a_stop_without_the_field_passes_when_the_engine_cleared_the_row(
    a_real_clearing_line: str,
) -> None:
    """#226's whole point: the field is gone because the words arrived after all."""
    reading = journey._stop_notice_reading([a_real_clearing_line, A_STOP], address=ADDRESS)

    assert reading is journey._StopNoticeReading.CLEARED
    assert reading in journey._STOP_NOTICE_PASSES


def test_a_stop_without_the_field_and_nothing_to_explain_it_still_fails() -> None:
    """The defect the step exists to catch is not softened by the new outcome."""
    reading = journey._stop_notice_reading([A_STOP], address=ADDRESS)

    assert reading is journey._StopNoticeReading.DISAGREED
    assert reading not in journey._STOP_NOTICE_PASSES


def test_a_clearing_for_somebody_elses_session_explains_nothing(
    a_strangers_real_clearing_line: str,
) -> None:
    """#109's rule, applied to the engine's log: only this Session's row counts."""
    reading = journey._stop_notice_reading(
        [a_strangers_real_clearing_line, A_STOP], address=ADDRESS
    )

    assert reading is journey._StopNoticeReading.DISAGREED


def test_no_stop_notice_at_all_is_reported_as_its_own_thing(
    a_real_clearing_line: str,
) -> None:
    """A cleared row is not a pass on its own — the step grades a Stop Notice."""
    assert (
        journey._stop_notice_reading([], address=ADDRESS) is journey._StopNoticeReading.UNPUBLISHED
    )
    assert (
        journey._stop_notice_reading([a_real_clearing_line], address=ADDRESS)
        is journey._StopNoticeReading.UNPUBLISHED
    )


def test_a_strangers_stop_carrying_the_field_is_not_this_sessions_reading() -> None:
    """#109 on the log: the walk grades notices that name its own target, only.

    A line-by-line grep reads these two lines as one notice and passes a step
    whose Session never published anything.
    """
    reading = journey._stop_notice_reading([A_STRANGERS_STOP, THE_FIELD], address=ADDRESS)

    assert reading is journey._StopNoticeReading.UNPUBLISHED


def test_a_strangers_field_does_not_carry_this_sessions_fieldless_stop() -> None:
    reading = journey._stop_notice_reading([A_STOP, A_STRANGERS_STOP, THE_FIELD], address=ADDRESS)

    assert reading is journey._StopNoticeReading.DISAGREED


def test_a_clearing_after_the_fieldless_stop_explains_nothing_about_it(
    a_real_clearing_line: str,
) -> None:
    """The row still said the words had not arrived when that notice was written.

    The engine clears the row and renders a Stop on one event loop, so the order
    of the two lines is the order the two things happened in.
    """
    reading = journey._stop_notice_reading([A_STOP, a_real_clearing_line], address=ADDRESS)

    assert reading is journey._StopNoticeReading.DISAGREED


def test_a_stranger_whose_workspace_merely_contains_the_address_is_a_stranger() -> None:
    """Attribution is the header's address **field**, never a substring of it.

    Neither the workspace path nor the Session Name is a string this run gets to
    choose, so a stranger holding this address inside one of them is an ordinary
    state of a shared machine rather than a hypothetical (#109).
    """
    impostor = (
        "2026-09-04 21:19:02,100 INFO Session stopped: "
        f"/tmp/backup-of-{ADDRESS} — claude:9f10:64312 — finished"
    )

    reading = journey._stop_notice_reading([impostor, THE_FIELD], address=ADDRESS)

    assert reading is journey._StopNoticeReading.UNPUBLISHED


def test_a_later_stop_carrying_the_field_does_not_excuse_the_first_one() -> None:
    """The graded reading is the next publication of that row after `brief`.

    A second Stop belongs to a later turn, by which time anything may have set
    the field again — accepting it would pass the very disagreement the step
    exists to catch.
    """
    a_second_stop = (
        "2026-09-04 21:21:40,006 INFO Session stopped: /tmp/workspace — codex:abc — finished"
    )

    reading = journey._stop_notice_reading([A_STOP, a_second_stop, THE_FIELD], address=ADDRESS)

    assert reading is journey._StopNoticeReading.DISAGREED

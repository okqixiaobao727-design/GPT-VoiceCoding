"""The `switches` step's #227 anchor, and the two surfaces one notice is written on.

`tests/acceptance/journey.py` grades, in step 7, that a Session says nothing to
the chat while Duty is off. The read spans the switch: the step flips Duty off,
gives the Session a turn, and then watches the chat over an absence window. A
Stop Notice the engine published **while Duty was still on** takes longer to
reach Telegram than the flip takes, so it can land inside that window and be
read as a push it never was. That is #227, and it cost two lanes a red — run
`20260904T091550Z` on the boot notice, run `20260904T113245Z` on the
`companion inbound` step's.

The anchor is that the notices already published are read off the engine's log
the moment Duty is acknowledged off, and a message whose *words* are one of them
is in flight rather than intruding. Two things are pinned here, because an
acceptance run is an expensive and occasional place to find either broken:

* **the decision**, over a notice from before the switch, one from after it, and
  one about somebody else's Session;
* **the mirror**. `_stop_notice_wordings` reads the log the way the chat reads
  the carrier, and it can only do that while the two really are one set of words
  written twice (`core/briefing.py::text`). So both sides below come out of a
  **real** Bridge Core publishing a real Stop — never out of a pair of strings
  this module made up to agree with each other.
"""

from __future__ import annotations

import journey
import pytest

from gpt_voicecoding.seams.agent import SessionStopped, WaitingFor, WaitingKind
from hub import CLAUDE, CODEX, Hub

ADDRESS = str(CODEX)

#: The log's own prefix ahead of a Stop Notice's header. `_STOP_HEADLINE`
#: searches rather than anchors, so the file's form and `caplog`'s bare message
#: read the same; this is here to prove that, not to be matched on.
LOG_PREFIX = "2026-09-04 23:36:27,048 INFO gpt_voicecoding.core.bridge: "


def _a_real_notice(caplog, target=CODEX, waiting_for=None) -> tuple[str, str]:  # noqa: ANN001
    """One Stop Notice off a real Bridge Core, as both surfaces carry it.

    Returns the engine's own log record and the text handed to the Companion
    Channel — the log line the acceptance greps and the chat message it reads,
    for one publication of one notice.
    """
    hub = Hub(voice=False)
    caplog.clear()
    caplog.set_level("INFO", logger="gpt_voicecoding.core.bridge")

    hub.emit(SessionStopped(target=target, waiting_for=waiting_for or WaitingFor()))

    (logged,) = [
        one.getMessage()
        for one in caplog.records
        if one.getMessage().startswith("Session stopped:")
    ]
    (sent,) = hub.channel.sent
    return logged, sent


@pytest.fixture
def a_real_notice(caplog) -> tuple[str, str]:  # noqa: ANN001
    """The Session this walk is walking, stopping with nothing else to say."""
    return _a_real_notice(caplog)


@pytest.fixture
def a_real_notice_about_a_permission(caplog) -> tuple[str, str]:  # noqa: ANN001
    """The shape the switches turn itself ends in — a dialog nobody has answered."""
    return _a_real_notice(
        caplog,
        waiting_for=WaitingFor(kind=WaitingKind.PERMISSION, prompt="Write switches.txt"),
    )


@pytest.fixture
def a_strangers_real_notice(caplog) -> tuple[str, str]:  # noqa: ANN001
    """The same, about a Session this walk has nothing to do with (#109)."""
    return _a_real_notice(caplog, target=CLAUDE)


def test_the_log_and_the_chat_word_one_notice_the_same_way(
    a_real_notice: tuple[str, str],
) -> None:
    """The mirror: drop the log's header and the two surfaces say one thing."""
    logged, sent = a_real_notice

    assert journey._stop_notice_wordings(logged.splitlines(), address=ADDRESS) == frozenset(
        {journey._notice_wording(sent)}
    )


def test_the_logs_own_prefix_does_not_change_the_words(
    a_real_notice: tuple[str, str],
) -> None:
    """`engine.log_lines()` carries a timestamp the `caplog` record does not."""
    logged, sent = a_real_notice
    header, *rest = logged.splitlines()

    published = journey._stop_notice_wordings([LOG_PREFIX + header, *rest], address=ADDRESS)

    assert published == frozenset({journey._notice_wording(sent)})


def test_a_notice_published_before_the_switch_is_not_an_intruder(
    a_real_notice: tuple[str, str],
) -> None:
    """#227 itself: the words landed late, the engine published them on time."""
    logged, sent = a_real_notice

    published = journey._stop_notice_wordings(logged.splitlines(), address=ADDRESS)

    assert journey._notice_wording(sent) in published


def test_a_notice_the_engine_publishes_after_the_switch_is_still_an_intruder(
    a_real_notice: tuple[str, str],
    a_real_notice_about_a_permission: tuple[str, str],
) -> None:
    """The defect the step exists to catch is not softened by the anchor.

    The turn the step drives ends on a permission, so a push that really did
    outlive Duty going off carries the dialog — words nothing published before
    the switch could have carried.
    """
    logged, _ = a_real_notice
    _, pushed_after = a_real_notice_about_a_permission

    published = journey._stop_notice_wordings(logged.splitlines(), address=ADDRESS)

    assert journey._notice_wording(pushed_after) not in published


def test_a_strangers_notice_never_exonerates_this_sessions_message(
    a_real_notice: tuple[str, str],
    a_strangers_real_notice: tuple[str, str],
) -> None:
    """#109's rule, applied to the anchor: only this Session's notices count."""
    _, sent = a_real_notice
    strangers_log, _ = a_strangers_real_notice

    published = journey._stop_notice_wordings(strangers_log.splitlines(), address=ADDRESS)

    assert published == frozenset()
    assert journey._notice_wording(sent) not in published


def test_an_empty_log_exonerates_nothing(a_real_notice: tuple[str, str]) -> None:
    """Before the engine has published anything, every message is an intruder."""
    _, sent = a_real_notice

    published = journey._stop_notice_wordings([], address=ADDRESS)

    assert journey._notice_wording(sent) not in published

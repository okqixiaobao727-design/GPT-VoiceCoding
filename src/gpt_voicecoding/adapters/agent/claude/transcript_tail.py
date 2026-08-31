"""How far along one Claude Session is, read out of its own transcript records.

One function, no I/O. It is handed records — already parsed, in the order the
Session wrote them, by the lane's one opener of a transcript file
(`transcript.py`) — and answers the two fields #74 locked beside each other:
`Progress` and `last_activity`.

**They are two fields because they are two facts.** A Session that spent a
minute running tools has moved without saying anything a reader would show, so a
`last_activity` derived from the newest *visible* entry would report it as
stalled. The time therefore comes off any record in the file, and the entries
come only off the conversation.

**Including a sidechain record, and that is a departure from #76's own
sentence.** The ticket says the helper excludes "child/sidechain, system and
command-plumbing records as legacy does", and `recent`'s *entries* do exactly
that. `last_activity` deliberately does not, ruled on #76 by the Advisor, for
three reasons worth keeping together:

* The field means *this Session moved* (`seams/agent.py:314-316`). A Task
  subagent writes into **this** transcript and has no roster row of its own, so
  no other row could own that time; excluding it would report a Session four
  minutes into a subagent as one nobody has heard from, which is the exact
  failure the field was split off from `progress` to prevent.
* Two lanes, one meaning. The Codex lane's `last_activity` is the thread's own
  `updatedAt`, which moves for any work at all (`codex/thread_tail.py`), and the
  Claude lane must not say something narrower under the same name.
* A sidechain record is **not** #68's Child Process. That rule is about a
  separate `claude` *process*, which gets a row of its own and is never spoken
  to; a sidechain record is one Session's own subagent, in one Session's own
  file. Conflating them is what makes this look like an oversight.

**The visibility rules are not restated here.** `is_visible`,
`is_pipeline_noise` and `visible_text` live in `stop_analysis` and are consumed,
not re-implemented — the same records, the same exclusions, one reading. Legacy
ran two scans of one file for these two questions and they could describe two
different moments of a file the Session is still appending to
(`legacy@1d32845:bridge/transcript.py:1184-1208`).

**Against legacy** (ADR 0010, `CLAUDE.md`). The read is **ported** from
`legacy@1d32845:bridge/transcript.py:1184-1246` (walk the records, keep what the
user can see, bound the tail) with its text extraction at `:1568-1607` and its
bounding walk at `:2828-2860`. **Adapted**: legacy's sidechain exclusion
(`_top_level_records`, `:1125-1142`) is a **visibility** rule — what to show, and
what counts as a stop — and it is ported whole into `recent`'s entries and into
`stop_analysis.analyse`. `last_activity` is not a visibility fact, and legacy is
not a citation for it either way: gen 1 had no such field at all. **Adapted**:
legacy's entry was
`TranscriptMessage(role, text)` and this is the seam's `ProgressEntry`, which
carries the same two facts; legacy's bounds were a per-Session verb's (12
entries / 32 KB, `config.plist:449-452`) and these are a roster row's, for the
reason beside them. **Dropped**: the read deadline, the whole-file identity
check and the `TranscriptReadError` taxonomy — all three left with the reader
(`transcript.py`), which answers `None` and is asked again on the next tick.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Final

from gpt_voicecoding.adapters.agent.claude.stop_analysis import (
    QUESTION_TOOL,
    is_pipeline_noise,
    is_visible,
    question_in,
    visible_text,
)
from gpt_voicecoding.seams.agent import (
    ProgressCapture,
    ProgressEntry,
    ProgressOmission,
    ProgressRole,
    WaitingFor,
)

#: What Claude Code writes a record's time as. Read with `fromisoformat`, which
#: takes the trailing `Z` and answers a timezone-aware value, so nothing here
#: has to decide which zone an unmarked time was in.
_TIMESTAMP = "timestamp"

#: The two record types that are the conversation. `ProgressRole` is spelled the
#: same way, so the record says which side spoke and nothing infers it.
_ROLES: Final = {"user": ProgressRole.USER, "assistant": ProgressRole.ASSISTANT}


def recent(
    records: Sequence[Mapping[str, Any]],
    *,
    capture: ProgressCapture,
) -> tuple[tuple[ProgressEntry, ...], ProgressOmission, datetime | None]:
    """The newest of what was said, whether anything older was dropped, and when.

    History presence is determined before this projection. If the newest entry
    alone exceeds the supplied capture ceiling, the empty tail is paired with
    `newest_oversize`; it can never be mistaken for empty history.
    """
    entries: list[ProgressEntry] = []
    moved: datetime | None = None
    for record in records:
        if not isinstance(record, Mapping):
            continue
        when = _moment(record.get(_TIMESTAMP))
        if when is not None:
            # Any record at all, this Session's own subagents included: a
            # subagent writing here is this Session running, the field exists
            # to say so when nothing was said, and no other row could own that
            # time. Deliberately wider than the entries below — the module
            # docstring says why, and #76's Advisor ruling is what settled it.
            moved = when
        entry = _entry(record)
        if entry is not None:
            entries.append(entry)

    kept, omission = capture.select(entries)
    return kept, omission, moved


def recent_before_question(
    records: Sequence[Mapping[str, Any]],
    *,
    question: WaitingFor,
    capture: ProgressCapture,
) -> tuple[tuple[ProgressEntry, ...], ProgressOmission, datetime | None]:
    """Progress at a question Stop: everything before its tool-call record (#151)."""
    for index in range(len(records) - 1, -1, -1):
        if _question_in(records[index]) == question:
            return recent(records[:index], capture=capture)
    return recent(records, capture=capture)


def _question_in(record: Mapping[str, Any]) -> WaitingFor | None:
    """The complete `AskUserQuestion` call held by this assistant record."""
    if record.get("type") != "assistant":
        return None
    message = record.get("message")
    if not isinstance(message, Mapping):
        return None
    content = message.get("content")
    if not isinstance(content, list):
        return None
    for item in reversed(content):
        if (
            isinstance(item, Mapping)
            and item.get("type") == "tool_use"
            and item.get("name") == QUESTION_TOOL
        ):
            found = question_in(item.get("input"))
            if found is not None:
                return found
    return None


def _entry(record: Mapping[str, Any]) -> ProgressEntry | None:
    """One record as something that was said, or `None` if it was not."""
    role = _ROLES.get(str(record.get("type")))
    if role is None:
        return None
    message = record.get("message")
    if not isinstance(message, Mapping) or message.get("role") != str(record.get("type")):
        return None
    content = message.get("content")
    if not is_visible(record) or is_pipeline_noise(record, content):
        return None
    text = visible_text(content)
    return ProgressEntry(role=role, text=text) if text.strip() else None


def _moment(stamp: Any) -> datetime | None:
    """When a record says it was written, or nothing.

    A record being appended right now routinely has no readable time, and the
    answer to that is the same one the reader gives a half-written line: it
    costs itself. Blanking the time the rest of the file proves would report a
    busy Session as one nobody has heard from.
    """
    if not isinstance(stamp, str) or not stamp.strip():
        return None
    try:
        return datetime.fromisoformat(stamp.strip())
    except ValueError:
        return None

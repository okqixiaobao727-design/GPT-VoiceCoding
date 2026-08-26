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

**The visibility rules are not restated here.** `is_visible`,
`is_pipeline_noise` and `visible_text` live in `stop_analysis` and are consumed,
not re-implemented — the same records, the same exclusions, one reading. Legacy
ran two scans of one file for these two questions and they could describe two
different moments of a file the Session is still appending to
(`legacy@1d32845:bridge/transcript.py:1184-1208`).

**Against legacy** (ADR 0010, `CLAUDE.md`). The read is **ported** from
`legacy@1d32845:bridge/transcript.py:1184-1246` (walk the records, keep what the
user can see, bound the tail) with its text extraction at `:1568-1607` and its
bounding walk at `:2828-2860`. **Adapted**: legacy's entry was
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

from gpt_voicecoding.adapters.agent._progress import RECENT_LIMIT, RECENT_MAX_BYTES, bounded
from gpt_voicecoding.adapters.agent.claude.stop_analysis import (
    is_pipeline_noise,
    is_visible,
    visible_text,
)
from gpt_voicecoding.seams.agent import ProgressEntry, ProgressRole

#: What Claude Code writes a record's time as. Read with `fromisoformat`, which
#: takes the trailing `Z` and answers a timezone-aware value, so nothing here
#: has to decide which zone an unmarked time was in.
_TIMESTAMP = "timestamp"

#: The two record types that are the conversation. `ProgressRole` is spelled the
#: same way, so the record says which side spoke and nothing infers it.
_ROLES: Final = {"user": ProgressRole.USER, "assistant": ProgressRole.ASSISTANT}


def recent(
    records: Sequence[Mapping[str, Any]],
    limit: int = RECENT_LIMIT,
    *,
    max_bytes: int = RECENT_MAX_BYTES,
) -> tuple[tuple[ProgressEntry, ...], bool, datetime | None]:
    """The newest of what was said, whether anything older was dropped, and when.

    An empty tuple with `truncated=False` is a Session that has genuinely said
    nothing yet; with `truncated=True` it is one whose newest entry alone is over
    the budget. The caller reports either as `Progress`; what it may never do is
    read the pair as "nothing is happening", which is `None` from the reader
    above and a different fact.
    """
    entries: list[ProgressEntry] = []
    moved: datetime | None = None
    for record in records:
        if not isinstance(record, Mapping):
            continue
        when = _moment(record.get(_TIMESTAMP))
        if when is not None:
            # Any record at all, this Session's child processes included: a
            # subagent writing here is this Session running, and the field
            # exists to say so when nothing was said.
            moved = when
        entry = _entry(record)
        if entry is not None:
            entries.append(entry)

    kept, truncated = bounded(entries, limit, max_bytes=max_bytes)
    return kept, truncated, moved


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

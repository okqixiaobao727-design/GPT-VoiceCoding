"""How far along one Codex thread is, read out of what the daemon says about it.

No I/O. It is handed the `thread` document a `thread/read` answered and returns
the same two facts the Claude lane's `transcript_tail` returns, bounded by the
same shared rule (`adapters/agent/_progress.py`) — one bound, one type, whichever
lane the row came from.

**Attached threads only, and that is a rule rather than a limitation.** A Codex
Session the shared daemon does not hold has no turns to read: its rollout is on
disk, but reading it would be a second source answering the same question with
worse evidence, and the port table left that behind explicitly ("no rollout
reading for daemon-attached threads" — and for unattached ones, no manufactured
progress at all). Such a row carries `progress=None`, which is "not read", not
"read and found nothing".

**Against legacy** (ADR 0010, `CLAUDE.md`). The item selection is **ported**
whole from `legacy@1d32845:bridge/codex.py:1465-1520`: `agentMessage` is what
the agent said, a `userMessage`'s `text` content is what it was told, and every
other item type is the machinery of doing the work rather than a report of it.
**Adapted** in one place, deliberately: legacy *raised* on an item type it did
not recognise (`:1519`), which was right for a verb answering about one Session
and is wrong here, because the same reading now rides on a roster row and one
unknown item would blank a row the user is looking at. So an unreadable item
costs itself and nothing else — which is the rule legacy already used on its
Claude side (`bridge/transcript.py:1568-1580`), applied to both lanes.
**Dropped**: `turn_id` and `turn_status` beside each entry (`:1484-1492,
1516-1520`), because no v1.0 consumer reads them — the Live Call, the Companion
Channel and the Control Panel ask what a Session last said and what it was last
told, never which turn that was — and `ProgressEntry` can gain a field later
without `Progress` widening twice.

**Times are epoch seconds, measured not assumed.** `thread/read` on codex
0.149.1 answers `updatedAt`, `recencyAt` and `createdAt` as integers —
`1787712279` for a thread last touched at 2026-08-26T02:44:39Z — read off the
live daemon on 2026-08-26. `updatedAt` is `last_activity`: it moves when the
thread does, including while a turn is running, which is exactly the case the
field exists for.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Final

from gpt_voicecoding.adapters.agent._progress import RECENT_LIMIT, RECENT_MAX_BYTES, bounded
from gpt_voicecoding.seams.agent import ProgressEntry, ProgressRole

#: What the agent said, and what it was told. Everything else a turn holds —
#: `reasoning`, `commandExecution`, `fileChange`, `plan`, the tool calls, the
#: review-mode markers, `contextCompaction` — is the machinery of doing the work
#: and never something the user was shown
#: (`legacy@1d32845:bridge/codex.py:71-86,1479-1520`).
AGENT_ITEM: Final = "agentMessage"
USER_ITEM: Final = "userMessage"

#: Which parts of a user message are words. `image` and `localImage` are the
#: other two shapes a `userMessage` carries and neither has any to contribute.
USER_TEXT: Final = "text"

#: When the thread was last touched, as codex spells it.
UPDATED_AT: Final = "updatedAt"


def recent(
    thread: Mapping[str, Any],
    limit: int = RECENT_LIMIT,
    *,
    max_bytes: int = RECENT_MAX_BYTES,
) -> tuple[tuple[ProgressEntry, ...], bool]:
    """The newest of what this thread said, and whether anything older was dropped.

    A document with no `turns` list yields nothing rather than raising: it is
    what a `thread/read` answers when turns were not asked for, and a caller that
    took that as an error would turn the cheap roster read into a failure.
    """
    turns = thread.get("turns")
    if not isinstance(turns, list):
        return (), False
    entries: list[ProgressEntry] = []
    for turn in turns:
        if not isinstance(turn, Mapping):
            continue
        items = turn.get("items")
        if not isinstance(items, list):
            continue
        entries.extend(entry for entry in map(_entry, items) if entry is not None)
    return bounded(entries, limit, max_bytes=max_bytes)


def moment(value: Any) -> datetime | None:
    """One of the thread's own times as a moment, or nothing.

    Epoch seconds, timezone-aware in UTC. A `bool` is refused before the number
    check because `True` is an `int` in Python and would read as one second past
    the epoch — a Session last active in 1970 is the kind of nonsense a surface
    renders without blinking.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        return datetime.fromtimestamp(value, UTC)
    except (OSError, OverflowError, ValueError):
        return None


def last_activity(thread: Mapping[str, Any]) -> datetime | None:
    """When this thread last moved, by its own account."""
    return moment(thread.get(UPDATED_AT))


def _entry(item: Any) -> ProgressEntry | None:
    """One turn item as something that was said, or `None` if it was not."""
    if not isinstance(item, Mapping):
        return None
    kind = item.get("type")
    if kind == AGENT_ITEM:
        text = item.get("text")
        return _said(ProgressRole.ASSISTANT, text if isinstance(text, str) else "")
    if kind == USER_ITEM:
        return _said(ProgressRole.USER, _user_text(item.get("content")))
    return None


def _user_text(content: Any) -> str:
    """The words in one `userMessage`, and nothing else it carried."""
    if not isinstance(content, list):
        return ""
    parts = [
        part.get("text")
        for part in content
        if isinstance(part, Mapping)
        and part.get("type") == USER_TEXT
        and isinstance(part.get("text"), str)
    ]
    return "".join(part for part in parts if part)


def _said(role: ProgressRole, text: str) -> ProgressEntry | None:
    return ProgressEntry(role=role, text=text) if text.strip() else None

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
progress at all). Such a row carries explicit `not_read` progress, never "read
and found nothing".

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
**Ported**: `turn_id` beside each entry (`:1405, 1484-1492, 1516-1520`), which
legacy read off the turn document and attached to every message that turn held.
**Adapted** the same way the item types were: legacy *raised* when a turn named
no `id` or named a non-string one (`:1405-1411`), and here such a turn simply
yields entries that name no turn, because a roster row must not blank over one
malformed turn. It is the only boundary that survives a turn opened by a message
with no words in it (#210): a `userMessage` carrying only an image leaves no
entry, so the turn a reader could otherwise find by looking for the newest thing
the user said is not there to find. **Dropped**: `turn_status` (`:1484-1492,
1516-1520`), because no v1.0 consumer reads it — the Live Call, the Companion
Channel and the Control Panel ask what a Session last said and what it was last
told, never how the turn ended.

#188 added the other field: an `agentMessage`'s `phase`, because it is the
record's own answer to *which message was the turn's answer* and nothing else
here can reconstruct it. It is normalised into the seam's `ProgressPhase` here
and never read here — whether an answer reads as an ask is Briefing's reading,
not this reader's observation.

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

from gpt_voicecoding.seams.agent import (
    ProgressCapture,
    ProgressEntry,
    ProgressOmission,
    ProgressPhase,
    ProgressRole,
)

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

#: Which part of the turn one `agentMessage` was, as codex spells it
#: (`codex-rs/app-server-protocol/src/protocol/v2/item.rs:249-258`).
PHASE: Final = "phase"

#: Codex's own words for a phase, and the seam facts they are. **This table is
#: the only place either string is written** (ADR 0001): the phase reaches
#: Briefing as a `ProgressPhase`, so Bridge Core never compares a codex word.
#: Anything absent from it — a phase a later build invents, or a value that is
#: not a string at all — reads as `UNKNOWN` rather than raising, for the same
#: reason an unknown item type costs only itself: this reading rides on a roster
#: row. `UNKNOWN` is not `None`, deliberately (#210): the source said something,
#: and "said something this build cannot read" is a different fact from "said
#: nothing", even though both read as *not the answer*.
PHASES: Final[Mapping[str, ProgressPhase]] = {
    "commentary": ProgressPhase.COMMENTARY,
    "final_answer": ProgressPhase.FINAL_ANSWER,
}

#: Which turn a thread document says one of its turns is, as codex spells it.
TURN_ID: Final = "id"

#: When the thread was last touched, as codex spells it.
UPDATED_AT: Final = "updatedAt"


def recent(
    thread: Mapping[str, Any],
    *,
    capture: ProgressCapture,
) -> tuple[tuple[ProgressEntry, ...], ProgressOmission]:
    """The newest of what this thread said, and whether anything older was dropped.

    A document with no `turns` list yields nothing rather than raising: it is
    what a `thread/read` answers when turns were not asked for, and a caller that
    took that as an error would turn the cheap roster read into a failure.
    """
    return capture.select(visible(thread))


def visible(thread: Mapping[str, Any]) -> tuple[ProgressEntry, ...]:
    """Every entry this thread shows, oldest first and numbered (#171).

    The whole list, before anything trims it — what `recent` hands its capture
    and what the History page windows (`adapters/agent/_progress.page`). One
    walk of the turns defines both, so the entry a page names and the entry a
    tail carries are the same entry.
    """
    turns = thread.get("turns")
    if not isinstance(turns, list):
        return ()
    entries: list[ProgressEntry] = []
    for turn in turns:
        if not isinstance(turn, Mapping):
            continue
        items = turn.get("items")
        if not isinstance(items, list):
            continue
        turn_id = _turn_id(turn.get(TURN_ID))
        for item in items:
            entry = _entry(item, ordinal=len(entries), turn_id=turn_id)
            if entry is not None:
                entries.append(entry)
    return tuple(entries)


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


def _entry(item: Any, *, ordinal: int, turn_id: str | None) -> ProgressEntry | None:
    """One turn item as something that was said, or `None` if it was not.

    The ordinal is the count of entries already kept, so it numbers the visible
    conversation from its oldest entry across every turn, and an item that is
    not something said costs no number (#171). The turn is the enclosing turn's,
    whatever the item is: an entry names the turn it came from even when the
    turn's own opening message left no entry beside it (#210).
    """
    if not isinstance(item, Mapping):
        return None
    kind = item.get("type")
    if kind == AGENT_ITEM:
        text = item.get("text")
        return _said(
            ordinal,
            ProgressRole.ASSISTANT,
            text if isinstance(text, str) else "",
            phase=_phase(item.get(PHASE)),
            turn_id=turn_id,
        )
    if kind == USER_ITEM:
        return _said(ordinal, ProgressRole.USER, _user_text(item.get("content")), turn_id=turn_id)
    return None


def _phase(value: Any) -> ProgressPhase | None:
    """Which part of the turn one `agentMessage` was, as this seam says it.

    A build old enough to omit the field said nothing, which is `None`; a build
    that said a word this one does not know said *something*, which is
    `UNKNOWN`. Both read as not the answer, and neither raises.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        return ProgressPhase.UNKNOWN
    return PHASES.get(value, ProgressPhase.UNKNOWN)


def _turn_id(value: Any) -> str | None:
    """Which turn this is, by its own account, or nothing this reader can group by.

    **Adapted** from `legacy@1d32845:bridge/codex.py:1405-1411`, which raised on
    a turn that named no `id`: here the turn keeps its entries and they name no
    turn, so one malformed turn cannot blank the roster row this reading is on.
    """
    return value if isinstance(value, str) and value.strip() else None


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


def _said(
    ordinal: int,
    role: ProgressRole,
    text: str,
    *,
    phase: ProgressPhase | None = None,
    turn_id: str | None = None,
) -> ProgressEntry | None:
    if not text.strip():
        return None
    return ProgressEntry(ordinal=ordinal, role=role, text=text, phase=phase, turn_id=turn_id)

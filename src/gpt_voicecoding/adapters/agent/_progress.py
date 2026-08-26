"""How much of what a Session said travels. One bound, both lanes, no exceptions.

Each lane finds its entries its own way — Claude walks a transcript it owns,
Codex asks the shared daemon for a thread's turns — but *how many* of them a
`Progress` carries is a property of the type and of the wire it crosses, not of
either lane. Two lanes bounding themselves would be two answers to one question,
and a Control Panel row whose length depended on which agent wrote it.

**Why these two numbers.** `Progress` rides on **every** roster row of a reply
with a 64 KB ceiling (`seams/control_plane.py:44`, enforced on the reply too at
`control_plane/client.py:88-94`). The reference implementation's 12 entries and
32 KB were a per-Session verb's, asked about one Session at a time
(`legacy@1d32845:config.plist:449-452`), and one such answer would spend half
this reply on one row. Three entries is what a spoken line or a menu-bar row can
use — the last thing said, and enough before it to place it — and 3 KB is the
smallest budget that still holds an ordinary paragraph, so a row does not go
silent over a sentence somebody wrote at length.

The **rule** is ported whole from `legacy@1d32845:bridge/transcript.py:2828-2860`:
keep the newest, widen down until it fits, and say so when anything was dropped.
Legacy's `drop_oversize_tail` is the roster case here rather than an option — a
single entry over the budget yields no entries and `truncated`, because a reply
nobody can read is a worse failure than a row that admits it is empty.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Final

from gpt_voicecoding.seams.agent import ProgressEntry

#: How many entries one reading carries.
RECENT_LIMIT: Final = 3

#: And how many bytes they may encode to, on the document that travels.
RECENT_MAX_BYTES: Final = 3 * 1024


def bounded(
    entries: Sequence[ProgressEntry],
    limit: int = RECENT_LIMIT,
    *,
    max_bytes: int = RECENT_MAX_BYTES,
) -> tuple[tuple[ProgressEntry, ...], bool]:
    """The newest of these that fit, and whether anything older was dropped.

    An entry goes whole or stays whole. Half a sentence read aloud says less
    than nothing, and a cut lands mid-secret as readily as mid-word — the same
    rule `stop_analysis.SUMMARY_MAX_CHARS` follows for a tool summary.
    """
    if limit <= 0 or max_bytes <= 0:
        raise ValueError("a bound that bounds nothing is a bound nobody set")
    kept = list(entries[-limit:])
    truncated = len(kept) < len(entries)
    while kept and encoded_size(kept) > max_bytes:
        kept.pop(0)
        truncated = True
    return tuple(kept), truncated


def encoded_size(entries: Sequence[ProgressEntry]) -> int:
    """How many bytes these entries take on the wire.

    Measured on the document `control_plane.payloads.progress_document` renders,
    rather than on the text alone, because the budget exists to keep a roster
    reply under the protocol's ceiling and the role travels inside it.
    `tests/test_control_plane_actions.py` holds the two spellings together, so
    this cannot quietly start measuring a shape nobody sends.
    """
    return len(
        json.dumps(
            [{"role": str(entry.role), "text": entry.text} for entry in entries],
            ensure_ascii=False,
        ).encode("utf-8")
    )

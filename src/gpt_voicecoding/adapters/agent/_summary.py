"""The one readable line a permission request says about itself. One rule, both lanes.

Each lane finds its own fields — Claude reads a tool call's input, Codex reads an
approval request's params — but **what a summary may contain** is not either
lane's business. It is a safety rule about text that gets read aloud into a Live
Call and pushed to a phone, and a safety rule that lived twice would be enforced
on one path and not the other. It already was: `stop_analysis.summarise` said in
as many words that it was "the **only** extractor of this field in the product",
and by then the Codex lane had its own, reading the shell command verbatim.

Two halves, and both are `legacy@1d32845:bridge/transcript.py:1779-1790`,
*ported*:

* **Description-class fields only.** The arguments proper — `command`, `content`,
  `old_string` — are deliberately excluded. They are the code and shell text the
  reference implementation always kept out, on the grounds that reading them
  aloud is neither safe nor useful. Which fields *are* description-class is the
  lane's, because only the lane knows what its far side writes for a human to
  read; that they are the only ones is not.
* **Whole, or not at all.** Something over `SUMMARY_MAX_CHARS` is not the
  one-line summary this reads for, so it is passed over rather than cut: half a
  sentence read aloud says less than the tool's name does, and a cut lands
  mid-secret as readily as mid-word. `_progress.bounded` follows the same rule
  for a different unit.

**Empty is an answer, and the honest one.** An input carrying none of these
fields summarises to nothing, and the announcement then names the tool and no
more, rather than describing an action from a guess. `ApprovalVerdict.ASK` is how
a user hands a dialog they cannot judge from that back to the screen.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

#: Longer than any description-class field as either product writes one. Ported
#: whole from `legacy@1d32845:bridge/transcript.py:1789`.
SUMMARY_MAX_CHARS: Final = 200


def summarise(source: Any, fields: Sequence[str]) -> str:
    """The first of `fields` that carries a short human-readable line, else nothing.

    `fields` is in preference order and is the lane's to choose; everything else
    here is the shared rule. A field that is absent, is not a string, is empty
    after stripping, or is longer than `SUMMARY_MAX_CHARS` is passed over and the
    next is tried — over-length included, because it is passed over *whole*.
    """
    if not isinstance(source, Mapping):
        return ""
    for field in fields:
        value = source.get(field)
        if not isinstance(value, str):
            continue
        summary = value.strip()
        if summary and len(summary) <= SUMMARY_MAX_CHARS:
            return summary
    return ""

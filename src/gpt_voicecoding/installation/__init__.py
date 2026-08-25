"""Everything this product puts on a machine that is not its own — ADR 0012.

Two artifacts have to exist outside this repository before the bridge can reach
anything: the fingerprinted hook block in a Claude config directory's
``settings.json`` (ADR 0011), and the login ``LaunchAgent`` that starts Codex's
shared app-server daemon (#82). Both live in files the **user** owns, and both
have to be placeable when no engine is running — so none of this is Bridge Core's
and none of it goes through the control plane.

**This is not a seam.** ADR 0001 puts a seam where something varies and names two
adapters when it does. Here there are two artifacts, not two implementations of
one interface, and no wire between any of the parts — so there is no vocabulary
to share and no protocol to declare. ``__main__`` names the items explicitly, the
way the composition root names adapters, and this module holds the one result
type they all answer with and the one write they all go through.

**Every write is atomic, read back, and reported.** The atomicity is what keeps a
half-written ``settings.json`` off the disk. It cannot prevent a *lost update* —
another writer's change between our read and our write is simply gone — and ADR
0011 records concurrent writers of ``~/.claude/settings.json`` as a real and
untested exposure. So the read-back is the honest part: a mismatch is reported as
a failed item naming the file, and never retried into a loop with whoever else is
writing. Legacy wrote in place (``legacy@1d32845:bridge/hookconfig.py:153-155``);
this is adapted, not copied.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from gpt_voicecoding.locations import installation_path

__all__ = [
    "Intent",
    "Outcome",
    "State",
    "read_intent",
    "replace_text",
    "write_intent",
]


class State(StrEnum):
    """What is on the machine, from this product's point of view."""

    #: Nothing of ours is there.
    ABSENT = "absent"
    #: Ours is there and is what this build would write.
    CURRENT = "current"
    #: Ours is there and differs — an older build, or a moved bundle.
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class Outcome:
    """What one item reports after being inspected, installed or removed.

    ``ok`` and ``state`` answer different questions and both are needed. An item
    with no config directory to install into is ``ok`` and ``ABSENT``: nothing
    went wrong, and a user who does not run Claude Code is not a failed install.
    """

    item: str
    state: State
    #: Whether this run wrote anything. A reconcile that agrees writes nothing.
    changed: bool = False
    ok: bool = True
    #: One sentence, in the failure's own words when there is one.
    note: str = ""

    def line(self) -> str:
        """One line for an operator, and for the shell's log."""
        if not self.ok:
            head = f"{self.item}: FAILED"
        else:
            head = f"{self.item}: {self.state}" + (" (changed)" if self.changed else "")
        return f"{head} — {self.note}" if self.note else head


def replace_text(path: Path, contents: str) -> str:
    """Write ``contents`` at ``path`` atomically and read it back.

    Returns the empty string when what landed is what we wrote, and a sentence
    naming the file when it is not. See the module note for why the read-back is
    the part that matters.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".new"
        )
    except OSError as refusal:
        return f"{path}: {refusal}"

    try:
        with os.fdopen(handle, "w", encoding="utf-8") as writing:
            writing.write(contents)
            writing.flush()
            os.fsync(writing.fileno())
        os.replace(temporary, path)
    except OSError as refusal:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        return f"{path}: {refusal}"

    try:
        landed = path.read_text(encoding="utf-8")
    except OSError as refusal:
        return f"{path}: written, and unreadable immediately after: {refusal}"
    if landed != contents:
        return (
            f"{path}: another process wrote this file at the same time, so what is "
            "there now is not what this run wrote. Nothing was retried."
        )
    return ""


#: The one field of the intent file. A missing file is a third answer and is not
#: written down: nobody has installed on this machine yet.
WANTED_FIELD: Final = "wanted"


@dataclass(frozen=True, slots=True)
class Intent:
    """Whether the user wants these artifacts on this machine.

    Recorded in this product's own directory rather than in any file the user
    owns, because it is our bookkeeping and not their configuration. It exists so
    that a reconcile at every launch and a meaningful uninstall can both be true:
    without it, the next launch would put back what the user just took away.
    """

    #: ``None`` when nothing was ever recorded — nobody has installed here yet.
    wanted: bool | None

    @property
    def first_run(self) -> bool:
        return self.wanted is None

    @property
    def install_wanted(self) -> bool:
        """A first run installs; after that, the user's recorded answer decides."""
        return self.wanted is not False


def read_intent(base_dir: Path | None = None) -> Intent:
    """The recorded intent. Anything unreadable reads as never recorded."""
    try:
        document: Any = json.loads(installation_path(base_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return Intent(wanted=None)
    if not isinstance(document, dict) or not isinstance(document.get(WANTED_FIELD), bool):
        return Intent(wanted=None)
    return Intent(wanted=document[WANTED_FIELD])


def write_intent(wanted: bool, base_dir: Path | None = None) -> str:
    """Record the user's answer. Returns a sentence when it could not be written."""
    return replace_text(
        installation_path(base_dir), json.dumps({WANTED_FIELD: wanted}, indent=2) + "\n"
    )

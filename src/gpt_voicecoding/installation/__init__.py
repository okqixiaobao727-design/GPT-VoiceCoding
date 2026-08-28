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

`installation.json` carries two pieces of product-owned bookkeeping: whether the
user wants installation, and #132's evidence of which Codex LaunchAgent render
launchd loaded. Each writer preserves the other field; neither is user config.

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
import re
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from gpt_voicecoding.locations import installation_path

__all__ = [
    "BootstrappedRender",
    "Intent",
    "Outcome",
    "State",
    "read_bootstrapped_render",
    "read_intent",
    "remove_file",
    "replace_text",
    "write_bootstrapped_render",
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


def remove_file(path: Path) -> str:
    """Take a file this product wrote back off the disk, and check it is gone.

    The Claude item never needs this: its artifact is a block *inside* a file the
    user owns, so taking it back is a rewrite. The Codex item's artifact **is** a
    file, wholly ours, so taking it back is a removal — and the read-back that
    `replace_text` does after a write is an existence check after this one, for
    the same reason. Returns the empty string when nothing of ours is left.
    """
    try:
        path.unlink()
    except FileNotFoundError:
        return ""
    except OSError as refusal:
        return f"{path}: {refusal}"
    if path.exists():
        return f"{path}: removed, and still there immediately after. Nothing was retried."
    return ""


#: The user intent and Codex loaded-render evidence share ``installation.json``.
#: Both are this product's bookkeeping, not files the user owns; each writer
#: preserves the other field when it updates its own answer.
WANTED_FIELD: Final = "wanted"
CODEX_LAUNCH_AGENT_FIELD: Final = "codex_launch_agent"
RENDER_SHA256_FIELD: Final = "render_sha256"
LOGIN_ASID_FIELD: Final = "login_asid"


def _read_record(path: Path) -> dict[str, Any]:
    """The installation record, or an empty document when none is usable."""
    try:
        document: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return document if isinstance(document, dict) else {}


def _write_record(path: Path, document: dict[str, Any]) -> str:
    return replace_text(path, json.dumps(document, indent=2) + "\n")


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


@dataclass(frozen=True, slots=True)
class BootstrappedRender:
    """The Codex job definition known to be loaded in one GUI login.

    ``render_sha256`` is ``None`` when a loaded job exists but this product has
    no evidence of which render it holds. ``login_asid`` is launchd's audit
    session identifier: macOS creates a new one for each GUI login, so a change
    proves that launchd had another opportunity to load the plist from disk.
    """

    render_sha256: str | None
    login_asid: int | None


def read_intent(base_dir: Path | None = None) -> Intent:
    """The recorded intent. Anything unreadable reads as never recorded."""
    document = _read_record(installation_path(base_dir))
    if not isinstance(document.get(WANTED_FIELD), bool):
        return Intent(wanted=None)
    return Intent(wanted=document[WANTED_FIELD])


def write_intent(wanted: bool, base_dir: Path | None = None) -> str:
    """Record the user's answer. Returns a sentence when it could not be written."""
    path = installation_path(base_dir)
    document = _read_record(path)
    document[WANTED_FIELD] = wanted
    return _write_record(path, document)


def read_bootstrapped_render(path: Path) -> BootstrappedRender | None:
    """The loaded Codex render evidence, or ``None`` when it is absent/invalid."""
    candidate = _read_record(path).get(CODEX_LAUNCH_AGENT_FIELD)
    if not isinstance(candidate, dict):
        return None
    render_sha256 = candidate.get(RENDER_SHA256_FIELD)
    login_asid = candidate.get(LOGIN_ASID_FIELD)
    if render_sha256 is not None and (
        not isinstance(render_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", render_sha256) is None
    ):
        return None
    if login_asid is not None and (
        not isinstance(login_asid, int) or isinstance(login_asid, bool) or login_asid < 0
    ):
        return None
    return BootstrappedRender(render_sha256=render_sha256, login_asid=login_asid)


def write_bootstrapped_render(path: Path, loaded: BootstrappedRender) -> str:
    """Record which Codex render launchd holds while preserving user intent."""
    document = _read_record(path)
    document[CODEX_LAUNCH_AGENT_FIELD] = {
        RENDER_SHA256_FIELD: loaded.render_sha256,
        LOGIN_ASID_FIELD: loaded.login_asid,
    }
    return _write_record(path, document)

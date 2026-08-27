"""Codex's own record of a thread, located by id and read for the two facts we need.

Ported from `legacy@1d32845:bridge/transcript.py:913-1005` (locating exactly one
rollout by exact id) and `legacy@1d32845:bridge/codex.py:972-1126` (the rollout
index and its `thread_source` reader), **narrowed** to what P13 asks for: a
locator for unattached Codex candidates, and `session_meta.thread_source` as
child evidence. What is left behind, deliberately: version-string equality
(legacy disabled it in 2026-08 for the reason its own comment gives — an upgrade
rewrites `cli_version` in every new rollout and made every post-upgrade
transcript unreadable), host-thread filtering, path persistence, the mmap'd
bounded-token reader, and any reading at all of a thread the daemon is attached
to, which answers for itself.

**Not finding one is the normal case, not an error.** Measured on 2026-08-26
(#73): `codex` writes its rollout when the first *turn* starts, not when the
Session does — a full acceptance run watched one sit for 180 s with none. So
`locate` answers `NotYet`, and a caller that treated that as a failure would be
treating every fresh TUI as broken.

**Nothing here crosses the seam.** A `Path` and the bytes behind it stay in this
lane; what leaves is a session id and a source word. That is legacy's own rule
for this index, kept: "neither consumer receives a path or transcript content".
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

_log = logging.getLogger(__name__)

#: Where Codex keeps its rollouts, and the variable that moves it.
CODEX_HOME_VARIABLE: Final = "CODEX_HOME"
DEFAULT_CODEX_HOME_NAME: Final = ".codex"

#: The two trees a rollout can be in. `sessions` is nested by date, so it is
#: walked; `archived_sessions` is flat. Both, because a thread archived while we
#: were not looking is still the thread the user is asking about.
SESSIONS_DIRECTORY: Final = "sessions"
ARCHIVED_DIRECTORY: Final = "archived_sessions"

ROLLOUT_PREFIX: Final = "rollout-"
ROLLOUT_SUFFIX: Final = ".jsonl"

#: Verified by legacy against all 1361 rollouts on this machine: the file name
#: always ends with the thread's own canonical UUID, so the identity needs no
#: guesswork. Kept as a regex rather than a suffix match so a name that merely
#: *ends with* the id by coincidence cannot pass.
THREAD_ID_IN_NAME: Final = re.compile(
    r"-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
)

#: The record type the first line of every rollout carries.
SESSION_META: Final = "session_meta"

#: What `session_meta` calls the thread's id. Measured on 0.149.1: the payload
#: carries **both** `session_id` and `id`, with the same value; a rollout from
#: 0.130.0 on this machine carries only `id`. Read in that order, so this works
#: across the builds that are actually on disk.
SESSION_ID_FIELDS: Final = ("session_id", "id")

#: What `session_meta` calls a thread the user started themselves. Anything else
#: is P13's child evidence — a thread something spawned.
USER_THREAD_SOURCE: Final = "user"


@dataclass(frozen=True, slots=True)
class NotYet:
    """No rollout for that thread exists yet, which is a state and not a failure."""

    thread_id: str


@dataclass(frozen=True, slots=True)
class Ambiguous:
    """More than one rollout claims that thread. Refuse rather than pick."""

    thread_id: str
    count: int


#: What `locate` answers with. A `Path` only when there is exactly one.
Located = Path | NotYet | Ambiguous


def codex_home(home: Path | None = None) -> Path:
    """Where this run looks for rollouts."""
    if home is not None:
        return home
    stated = os.environ.get(CODEX_HOME_VARIABLE)
    return Path(stated) if stated else Path.home() / DEFAULT_CODEX_HOME_NAME


def locate(thread_id: str, *, home: Path | None = None) -> Located:
    """The one rollout that is exactly this thread's, or why there is not one.

    Matched on the id in the file name, never on proximity or recency: legacy's
    rule, and the reason it holds is that the name carries the canonical UUID.
    Two matches refuse, because a rollout read for the wrong thread reports one
    Session's work under another's name.
    """
    candidates = [path for path in _rollout_files(codex_home(home)) if _named(path) == thread_id]
    if not candidates:
        return NotYet(thread_id)
    if len(candidates) > 1:
        _log.info("%s rollouts claim thread %s; refusing to pick", len(candidates), thread_id)
        return Ambiguous(thread_id, len(candidates))
    return candidates[0]


def thread_source(path: Path) -> str | None:
    """What the thread's own record says started it, or `None` if it cannot say.

    P13's child evidence. `user` is a thread the person started; anything else
    is one something spawned. `None` covers a first line still being written —
    which is an ordinary momentary state, not a malformed rollout.
    """
    meta = session_meta(path)
    if meta is None:
        return None
    source = meta.get("thread_source")
    return source.strip() if isinstance(source, str) and source.strip() else None


def started_by_the_user(path: Path) -> bool | None:
    """Whether this thread is the user's own. `None` means the record cannot say."""
    source = thread_source(path)
    return None if source is None else source == USER_THREAD_SOURCE


def session_meta(path: Path) -> dict[str, Any] | None:
    """The first line's payload, if the first line is a `session_meta` and parses.

    **Only the first line is ever considered.** Legacy validated `session_meta`
    at line 1 and nothing else, and the reason is worth keeping: a rollout being
    appended to while it is read has a well-defined beginning and no well-defined
    end, so anything that scans for a record is a reader that behaves differently
    depending on when it looked.
    """
    try:
        with path.open(encoding="utf-8") as lines:
            first = next(iter(lines), "")
    except OSError:
        return None
    if not first.strip():
        return None
    try:
        record: Any = json.loads(first)
    except json.JSONDecodeError:
        # A first line still being written parses as nothing. That is the "not
        # flushed yet" window, and it closes by itself.
        return None
    if not isinstance(record, dict) or record.get("type") != SESSION_META:
        return None
    payload = record.get("payload")
    return payload if isinstance(payload, dict) else None


def session_id_in(meta: dict[str, Any]) -> str | None:
    """The thread's own id, under whichever of its two names this build wrote."""
    for field in SESSION_ID_FIELDS:
        value = meta.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def workspace_in(meta: dict[str, Any]) -> Path | None:
    """Where the thread is running, as its own record resolved it."""
    cwd = meta.get("cwd")
    return Path(cwd) if isinstance(cwd, str) and cwd.strip() else None


def newest_for(workspace: Path, *, home: Path | None = None, since: float = 0.0) -> Path | None:
    """The most recently written rollout **a running TUI could be running**.

    How a Session that has taken its first turn is tied back to the process that
    is running it: the pid is not in the rollout and the workspace is, so the
    workspace is the join. Compared by realpath because `session_meta.cwd` is
    already resolved (#73), and a process's cwd read from the system is not.

    `since` exists so a caller can refuse to match a rollout written before the
    process it is looking at started.

    **A rollout the record says a person did not start is skipped, and that is
    what P13's `thread_source` is for** (#79). A subagent runs in its parent's
    own workspace and writes its rollout there *after* the TUI started, so on
    `cwd` and mtime alone it is the newer match and wins — and the user's own
    Session is then addressed by its child's thread id, which carries the user's
    words into the child. The join is over *processes*, and a spawned thread has
    none, so it is never the answer to "which thread is this TUI running".

    Absence fails open: a rollout written by a codex older than the field says
    nothing, and refusing those would stop this recognising Sessions it has
    always recognised. Only an explicit non-`user` word disqualifies one.
    """
    wanted = os.path.realpath(workspace)
    found: Path | None = None
    newest = since
    for path in _rollout_files(codex_home(home)):
        try:
            written = path.stat().st_mtime
        except OSError:
            continue
        if written < newest:
            continue
        meta = session_meta(path)
        if meta is None:
            continue
        cwd = workspace_in(meta)
        if cwd is None or os.path.realpath(cwd) != wanted:
            continue
        if _spawned(meta):
            continue
        found, newest = path, written
    return found


def _spawned(meta: dict[str, Any]) -> bool:
    """Whether this record says something other than a person started the thread.

    Reads the same field `thread_source` does, off a `session_meta` the caller
    has already parsed, so the locator costs no second read of the file.
    """
    source = meta.get("thread_source")
    return isinstance(source, str) and source.strip() != "" and source.strip() != USER_THREAD_SOURCE


def _rollout_files(home: Path) -> list[Path]:
    """Every rollout on disk, live and archived. Unreadable trees yield nothing."""
    found: list[Path] = []
    for root, walk in ((home / SESSIONS_DIRECTORY, True), (home / ARCHIVED_DIRECTORY, False)):
        try:
            if not root.is_dir():
                continue
            pattern = f"{ROLLOUT_PREFIX}*{ROLLOUT_SUFFIX}"
            found.extend(root.rglob(pattern) if walk else root.glob(pattern))
        except OSError:
            continue
    return found


def _named(path: Path) -> str | None:
    """The thread id a rollout's file name carries, or `None` if it carries none."""
    name = path.name
    if not name.startswith(ROLLOUT_PREFIX) or not name.endswith(ROLLOUT_SUFFIX):
        return None
    match = THREAD_ID_IN_NAME.search(name[len(ROLLOUT_PREFIX) : -len(ROLLOUT_SUFFIX)])
    return match.group(1) if match else None

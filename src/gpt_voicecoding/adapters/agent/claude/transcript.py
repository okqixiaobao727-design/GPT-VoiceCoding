"""Reading a Session's transcript as it is written. Spoke-internal, and reusable.

Claude Code appends one JSON record per line to
`~/.claude/projects/<encoded-cwd>/<sessionId>.jsonl`. This module turns that into
an incremental read from a remembered offset — the instrument the Notice Relay's
delivery readback needs, and the one the Stop Notice will want for the same
reason: both are questions of the form "what has this Session written *since* a
moment I care about".

It stays inside this spoke. The transcript is an undocumented file belonging to
another program (ADR 0005), so nothing outside the Claude adapter should learn
that it exists.

Three things make a tail over somebody else's file trustworthy rather than merely
working, and each is a behaviour rather than a comment:

- **A pre-send offset, taken before the words go out.** The offset is remembered
  first and the message sent second, so a record written in between is inside the
  window rather than behind it. `opened_at_end` exists to be called at exactly
  that moment.
- **A trailing partial line is not a record.** The writer is a live process, so
  a half-written final line is an ordinary state and not corruption. The offset
  advances only over lines that ended, which is what lets the rest be read whole
  on the next pass.
- **The file may move.** A Session that changes cwd gets a different encoded
  project directory, and the path resolved at open time then names nothing. The
  path is therefore re-resolved when it disappears, and the offset survives the
  move — it is the same content under a new name. A file that has *shrunk* is a
  different story: the offset means nothing, so the read restarts.

The cwd encoding is never re-implemented. The transcript is found by globbing for
the session id and requiring exactly one hit, because two answers is a question
this module has no honest way to settle and one it must not settle by guessing.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

#: Where Claude Code keeps transcripts. A location, so it defaults.
DEFAULT_PROJECTS_DIRECTORY = Path.home() / ".claude" / "projects"


class TranscriptError(Exception):
    """A Session's transcript cannot be found, or cannot be identified uniquely."""


class TranscriptNotFound(TranscriptError):
    """There is no transcript for that session id — not yet, or not any more.

    Separate from the ambiguous case because the two deserve opposite answers. A
    session that has not written its first record yet has no file, and a tail
    over it is perfectly well defined: it starts at nothing and reads everything.
    Two files claiming one session id is a question with no honest answer, and
    stays a refusal.
    """


def locate_transcript(projects_directory: Path, session_id: str) -> Path:
    """The one transcript for one session id, or a refusal saying how many there were."""
    try:
        found = sorted(projects_directory.glob(f"*/{session_id}.jsonl"))
    except OSError as unreadable:
        raise TranscriptError(
            f"cannot search {projects_directory} for session {session_id}: {unreadable}"
        ) from None
    if not found:
        raise TranscriptNotFound(
            f"no transcript for session {session_id} under {projects_directory}"
        )
    if len(found) > 1:
        raise TranscriptError(
            f"{len(found)} transcripts claim session {session_id} "
            f"({', '.join(str(path) for path in found)}); refusing to guess which is current"
        )
    return found[0]


class TranscriptTail:
    """One Session's transcript, read forward from a remembered byte offset."""

    def __init__(self, projects_directory: Path, session_id: str, *, offset: int = 0) -> None:
        self._projects_directory = projects_directory
        self._session_id = session_id
        self._path: Path | None = None
        self._offset = offset

    @classmethod
    def opened_at_end(cls, projects_directory: Path, session_id: str) -> TranscriptTail:
        """A tail that starts where the transcript stands right now.

        Call this *before* the thing whose effect you intend to read back, so
        nothing can land in the gap between deciding to watch and watching.

        **A session with no transcript yet is not an error.** Claude Code writes
        the file when the session writes its first record, so a freshly launched
        session has none — and the first thing to land in it may well be the very
        record this tail was opened to read. Starting at offset zero is not a
        fallback there, it is the correct pre-send offset: an absent file has no
        prior content to skip past.
        """
        tail = cls(projects_directory, session_id)
        try:
            path = locate_transcript(projects_directory, session_id)
        except TranscriptNotFound:
            return tail
        tail._path = path
        try:
            tail._offset = path.stat().st_size
        except OSError:
            tail._offset = 0
        return tail

    @property
    def offset(self) -> int:
        """How far this tail has read. Bytes, because that is what a seek takes."""
        return self._offset

    def records(self) -> Iterator[dict[str, Any]]:
        """Every whole record appended since the last call. Never the same one twice.

        Quiet rather than loud when the file cannot be read at this instant: a
        transcript that has moved, or that has not been recreated yet, is a state
        this tail is expected to survive, and the caller's own budget is what
        decides when "still nothing" becomes an answer.
        """
        path = self._resolve()
        if path is None:
            return
        try:
            size = path.stat().st_size
        except OSError:
            return
        if size < self._offset:
            # Truncated or replaced: the offset points at nothing meaningful, and
            # holding it would silently skip everything the new file contains.
            self._offset = 0
        if size == self._offset:
            return

        try:
            with path.open("rb") as handle:
                handle.seek(self._offset)
                block = handle.read(size - self._offset)
        except OSError:
            return

        # Only the part up to the last newline is complete. Anything after it is
        # a line the writer has not finished, and the offset must stay behind it.
        end = block.rfind(b"\n")
        if end == -1:
            return
        self._offset += end + 1

        for line in block[: end + 1].splitlines():
            record = _record(line)
            if record is not None:
                yield record

    def _resolve(self) -> Path | None:
        """Where the transcript is now, re-globbing only when the known path is gone."""
        if self._path is not None and self._path.exists():
            return self._path
        try:
            self._path = locate_transcript(self._projects_directory, self._session_id)
        except TranscriptError:
            self._path = None
        return self._path


def _record(line: bytes) -> dict[str, Any] | None:
    """One transcript line, or `None` for a line that is not a record.

    A line this module cannot read is stepped over rather than raised on. The
    transcript is another program's format and it may grow shapes this build has
    never seen; a reader that stopped at the first of them would turn an unknown
    record into an unreadable transcript.
    """
    if not line.strip():
        return None
    try:
        document: Any = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return document if isinstance(document, dict) else None

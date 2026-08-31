"""One Session's transcript, read once per change and handed to every reader.

**This is the lane's only opener of a transcript file, and that is the point.**
What a Session stopped on (`stop_analysis`, #75) and how far along it is
(`ProgressObservation`, #76) are two questions with one answer source, asked at the same
moment about the same Session. Two readers would mean two whole-file reads per
tick and two answers describing two moments of a file the Session is still
appending to — the same argument that put P3, P4 and P5 in one pass.

**The path is not derivable, so it is not derived.** It arrives on the
`SessionStart` registration (`registration.py`), which exists for that one field:
`claude agents --json` does not carry it, and the directory-name flattening
replaces `/`, `.` *and* `_` with `-`, which #73 rediscovered the hard way.

**Against legacy** (ADR 0010, `CLAUDE.md`). The read itself is **ported** from
`legacy@1d32845:bridge/transcript.py:1125-1140` and its `_read_jsonl`: one pass
over the JSONL in the order the Session wrote it, skipping a line that does not
parse. The **cache is new, and legacy had no analogue on this lane** — it read a
Claude transcript only when a stop asked it to, on demand
(`legacy@1d32845:bridge/daemon.py:2115-2167`), so there was nothing to cache
against. v2 reads on a five-second discovery cadence over the whole machine, and
that cadence is what makes an unchanged file worth not re-parsing. Legacy's one
cache of this shape is on the *Codex* rollout reader, which #74 left behind with
the rest of the durable state. **Dropped** with the rest of that reader: the
whole-file identity check, the read deadline, and the `TranscriptReadError`
taxonomy — a caller that gets `None` asks again on the next tick.

**Cached on `(size, mtime_ns)` rather than on time.** A transcript that has not
changed cannot have changed what its tail says. The key is the file's own
identity as the filesystem reports it, so the cache is invalidated by the writer
rather than by a clock this module would have to guess the right length for.

**What that costs, measured on 2026-08-26** over the 1,489 real transcripts in
`~/.claude/projects` (150,681 records parsed across the 400 most recent): a cold
read of the largest file on the machine — 186 MB, 722 records, because a record
may hold an image or a large tool result — is **0.878 s**, and the same file
re-read warm is **0.6 ms**. The whole file is read, as the reference
implementation read it: a bound on the bytes would be a guess at how far back the
tail can reach, and 258 KB per record on that file says the guess would be wrong.
What keeps the cost off the cadence instead is the caller — a `RUNNING` Session
is not read at all, and a stopped one is not appending, so the expensive read
happens once per stop rather than once per tick.

**An unreadable line is skipped; an unreadable file raises.** The last line of
a live transcript is routinely half-written, and refusing the whole file over the
record being appended right now would blank a Session's stop at exactly the
moment it has one. `None` means no path or no file yet (`not_read`); an `OSError`
raises `TranscriptUnavailable` (`unreadable`). Neither means *nothing is
happening* (`legacy@1d32845:bridge/transcript.py:1213-1240`, whose lesson was that
treating format drift as an error failed ~99% of real transcripts).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

#: One parsed transcript record, as Claude Code wrote it. Deliberately the raw
#: mapping: this module's job is to read the file once, not to decide which of
#: its fields the two readers above it care about.
Record = Mapping[str, Any]


class TranscriptUnavailable(Exception):
    """The authoritative transcript exists but could not be read."""


class TranscriptReader:
    """Every registered Session's transcript, parsed at most once per change."""

    def __init__(self) -> None:
        #: path → (size, mtime_ns, records). One entry per Session, replaced
        #: rather than appended to, so the memory is bounded by the roster.
        self._cache: dict[Path, tuple[int, int, tuple[Record, ...]]] = {}

    def records(self, path: Path | None) -> tuple[Record, ...] | None:
        """This Session's records in the order it wrote them, or `None`.

        `None` says there is no registered path or the first turn has not created
        the file yet (#73). A source that exists but cannot be read raises
        `TranscriptUnavailable`. Neither is an empty transcript.
        """
        if path is None:
            return None
        try:
            stat = path.stat()
        except FileNotFoundError:
            self._cache.pop(path, None)
            return None
        except OSError as unreadable:
            self._cache.pop(path, None)
            raise TranscriptUnavailable(
                f"could not read the transcript at {path}: {unreadable}"
            ) from None
        cached = self._cache.get(path)
        if cached is not None and cached[0] == stat.st_size and cached[1] == stat.st_mtime_ns:
            return cached[2]
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as unreadable:
            _log.info("could not read the transcript at %s: %s", path, unreadable)
            self._cache.pop(path, None)
            raise TranscriptUnavailable(
                f"could not read the transcript at {path}: {unreadable}"
            ) from None
        records = _parse(text)
        self._cache[path] = (stat.st_size, stat.st_mtime_ns, records)
        return records

    def forget(self, path: Path | None) -> None:
        """Drop one Session's cached records. Its file is untouched."""
        if path is not None:
            self._cache.pop(path, None)


def _parse(text: str) -> tuple[Record, ...]:
    """Every line that is a JSON object, in order, skipping the ones that are not.

    A line that does not parse is the record being written right now far more
    often than it is a broken file, so it costs itself and nothing else. Anything
    that parses to something other than an object is not a record either.

    **Split on `"\n"` and on nothing else.** JSONL is newline-delimited, while
    `str.splitlines` also breaks on U+2028, U+2029 and U+0085 — and
    `JSON.stringify`, which writes these files, leaves all three raw inside a
    string. A record carrying one would be cut in two, neither half would parse,
    and "costs itself and nothing else" would quietly cost the whole record: a
    dropped `tool_result` leaves the call it closes outstanding, which reads as a
    permanent false `PERMISSION`. Measured on 2026-08-26 over the 200 most recent
    transcripts on this machine: 4 files, 31 records (#98).
    """
    records: list[Record] = []
    for line in text.split("\n"):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return tuple(records)

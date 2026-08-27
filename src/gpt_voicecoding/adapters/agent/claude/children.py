"""What a Claude Session has spawned. Seen in the roster, and nothing more (#79).

A **Child Process** is a process a Session spawns — a subagent, a review crew.
It appears in the roster under its Session and nothing more: no Relay, no Stop
Notice, no name (`CONTEXT.md`). Bridge Core enforces all three; this module's
whole job is the first word, *appears*: finding the children of one Session, so
there is something for the rule to be about.

**Neither source the brief named answers, and that is a measurement rather than
a difficulty.** #79 asked for `claude agents --json` `kind`/parent evidence and
process ancestry. Probed on 2026-08-27 against claude 2.1.246 — one `claude`
driven through one turn that started a subagent which slept 45 s, sampled every
3 s for 73 s, with the agent markers scrubbed — the roster showed **one row**
for the whole 52 s the child ran, and `ps` showed **one process**. A Task
subagent is not a process and is not on the official roster. The advisor
withdrew that line of the brief on the strength of this.

**What the child does have is a file in a tree named after its parent**:
`<parent transcript stem>/subagents/agent-<agentId>.jsonl`, beside an
`agent-<agentId>.meta.json` written once at launch — `{"agentType",
"description", "toolUseId", "spawnDepth"}`. The parent's transcript path is not
derivable and is not derived; it arrives on the `SessionStart` registration
(`registration.py`), and this module is handed it.

**Liveness is two conditions, and this file holds one.** A child row exists
while (a) the parent is `RUNNING` and (b) the parent's own record does not yet
carry the result of the tool call that started the child. (a) is the caller's,
because it is already on the parent's inspection and costs nothing; alone, (b)
would keep a child abandoned mid-turn — Esc on the parent — in the roster until
the parent exited. A finished child is **dropped, not kept as an ended row**:
Claude's own roster has no row for it, so *listed* can only mean *alive*.

**Condition (b) is the parent's record, and that took three probes to get
right.** The obvious marker — the child's own last record carrying
`stop_reason: "end_turn"` — is **not reliable, measured**:

- 2026-08-27, claude 2.1.246: a finished child's last record carried `end_turn`.
- The same day, claude 2.1.247: a finished child's last record carried
  `stop_reason: null`. The child had answered; the field simply was not there.
- A third run traced one child's tail every 2 s across its whole 56 s life: the
  file's last record was an assistant `text` or `thinking` record with no
  `stop_reason` at **three separate points while it was still working**, each
  lasting seconds. So "the last record is not a tool call" is not the marker
  either — it is a state a working child sits in while it thinks.

The parent's record has no such ambiguity. `meta.json` names the `toolUseId`
that started the child, and the parent writes a `tool_result` for exactly that
id when the child ends — carrying `{"status": "completed", "agentId": …}`. One
id, one answer, no shape to guess at.

**The parent's transcript is read backward from its end, and never whole.** The
lane's `TranscriptReader` parses the whole file, which is what `_row_with_stop`'s
gate exists to keep off the cadence — 186 MB in the worst case measured on this
machine. That gate cannot be borrowed here, because the case where the answer is
wanted most is the one where the parent *is* writing: a subagent started
`run_in_background` leaves its parent working while it runs, so the file grows
every tick and a whole-file parse would be repeated every tick. Scanning back
from EOF costs the child's lifetime instead of the Session's — **the answer is
always written after the spawn**, so the scan stops at the `tool_use` record
that started the child and never goes past it (advisor, 2026-08-27).

Two more gates sit in front of it. The scan happens only when this Session has a
child whose fate is still unknown; and the moment a child is known finished it
is remembered here and never asked about again. A Session whose children are all
over never opens its transcript at all, and one that spawned nothing costs a
single failed `iterdir`.

**Version-bound, and said out loud.** This layout is claude 2.1.246/2.1.247
on-disk behaviour, and it is not what the reference implementation saw:
`legacy@1d32845:bridge/transcript.py:1477-1500` filtered `isSidechain` records
out of the parent's *own* file, because that is where they were written then —
**adapted**, same rule ("a child's work is not the parent's turn"), different
place. `legacy@1d32845:bridge/hook.py:1-19,68-75` and `bridge/claude.py:396-409`
suppressed a child by an inherited environment variable — **dropped, because** a
Task subagent is not a process and inherits nothing. A layout that moves shows up
here as a Session with no children, which is the same answer this gives for every
Session that spawned none.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any, Final

from gpt_voicecoding.seams.agent import (
    ChildClassification,
    ChildKind,
    SessionInspection,
    SessionLifecycle,
    SessionState,
    WaitingFor,
)
from gpt_voicecoding.seams.identity import AgentKind, SessionTarget

_log = logging.getLogger(__name__)

#: The tree, as the probes found it. The directory is named for the parent's
#: transcript file without its suffix, and every child in it is one transcript
#: plus one metadata file sharing a stem.
CHILD_DIRECTORY: Final = "subagents"
CHILD_PREFIX: Final = "agent-"
CHILD_SUFFIX: Final = ".jsonl"
META_SUFFIX: Final = ".meta.json"

#: The builds every shape here was read off, on Simon's machine on 2026-08-27.
#: Documentation for the next re-probe, never a gate — the same decision
#: `claude/discovery.py` took, for the same reason.
PROVEN_AGAINST_VERSIONS: Final = ("2.1.246", "2.1.247")

#: The field of `meta.json` that ties a child to the tool call that started it.
#: The only one this module reads, apart from `spawnDepth`.
TOOL_USE_ID: Final = "toolUseId"

#: What the parent's answer says when the tool call has **not** finished the
#: child: a subagent started `run_in_background` gets its `tool_result` at once,
#: carrying this, while the child goes on working. Recorded from an acceptance
#: run of 2026-08-26 (`isAsync: true`), and the reason this is a value test
#: rather than "a result exists".
#:
#: **What ends an asynchronous child is unmeasured**, so one is listed for as
#: long as its parent's turn runs. That is the safe direction: a child listed is
#: a child every Relay and every Stop Notice already refuses, while a child
#: dropped is one the roster stopped mentioning while it was still working.
LAUNCHED_NOT_FINISHED: Final = "async_launched"

#: The `spawnDepth` of a subagent the Session itself started. Deeper than this
#: is unmeasured — the probes drove one subagent, at depth 1 — so a deeper child
#: names no parent rather than naming the wrong one.
DIRECT_SPAWN_DEPTH: Final = 1

#: How much of the parent's transcript is read at a time, scanning back from its
#: end. One record can be large — the lane's whole-file reader measured 258 KB
#: per record on the biggest transcript on this machine — so a block is sized to
#: hold several ordinary ones and the reader simply asks for another when a
#: record straddles the edge.
BLOCK_BYTES: Final = 64 * 1024

#: How far back the scan will go before giving up. It normally stops far sooner,
#: at the `tool_use` record that started the child; this is the backstop for a
#: parent whose spawn record is missing or unreadable, so a malformed file costs
#: a bounded read rather than the whole transcript. **Hitting it reads as *not
#: finished***, which is the safe direction: a child listed is a child every
#: Relay and every Stop Notice already refuses, while a child dropped is one the
#: roster stopped mentioning while it was still working.
SCAN_LIMIT_BYTES: Final = 4 * 1024 * 1024


class Children:
    """Every Session's live Child Processes, and the ones already known to be over.

    **The memory is the whole reason this is a class.** A child that has
    finished cannot start again, so the answer is worth keeping — and keeping it
    is what stops a Session with old subagent files from re-reading its
    transcript every five seconds for a question that was settled the first
    time. It holds one short string per child ever seen under this engine.
    """

    def __init__(self) -> None:
        #: Every `agentId` the parent's own record has answered for.
        self._finished: set[str] = set()
        #: `agent-<agentId>.meta.json` by the path it was read from. The file is
        #: written once, at launch, and never again, so this is one read per
        #: child per engine rather than one per tick — the claim the docstring
        #: below makes, held by this dict rather than by hope.
        self._meta: dict[Path, dict[str, Any]] = {}

    def under(
        self,
        parent: SessionInspection,
        transcript: Path | None,
    ) -> tuple[SessionInspection, ...]:
        """Every Child Process this Session is running right now, as roster rows.

        `transcript` is the parent's own transcript path as its registration
        named it. Without one there is nothing to look under, and nothing is
        guessed: the directory-name flattening replaces `/`, `.` *and* `_` with
        `-`, which #73 rediscovered the hard way.

        Nothing here raises. A tree that cannot be read is a Session with no
        children — the same answer as a Session that spawned none, and the right
        one either way, because the alternative is a whole lane's discovery
        failing over one unreadable directory.
        """
        if transcript is None or parent.target.pid is None:
            return ()
        directory = transcript.parent / transcript.stem / CHILD_DIRECTORY
        unsettled = [
            (agent_id, path)
            for agent_id, path in _candidates(directory)
            if agent_id not in self._finished
        ]
        if not unsettled:
            # The ordinary case for a Session that never spawned anything, and
            # for one whose children are all over: the parent's transcript is
            # never opened.
            return ()

        documents = {agent_id: self._describe(directory, path) for agent_id, path in unsettled}
        calls = {
            agent_id: document[TOOL_USE_ID]
            for agent_id, document in documents.items()
            if isinstance(document.get(TOOL_USE_ID), str)
        }
        self._finished |= _answered(transcript, calls)
        return tuple(
            _row(parent, agent_id, documents[agent_id].get("spawnDepth"))
            for agent_id, _ in unsettled
            if agent_id not in self._finished
        )

    def _describe(self, directory: Path, path: Path) -> dict[str, Any]:
        """`agent-<agentId>.meta.json`, read once it says anything, then remembered.

        It is written at launch and never again, so a document that was read is
        worth keeping for the life of the child.

        **An empty one is not, and keeping it was a bug.** The two files are not
        written together: the acceptance run `20260827T015022Z` created
        `agent-a0cfe094d970fc749.jsonl` at 13:53:34 and its `.meta.json` at
        13:53:58, 24 seconds later. The five-second cadence lands inside that
        window routinely. Remembering the emptiness meant never learning the
        `toolUseId`, and without it nothing the parent later writes can settle
        the child — so a finished child returned as a live row on every tick its
        parent was RUNNING, for the life of the engine, against this ticket's
        "a finished child is dropped, not kept as a dead row".

        Re-reading costs one 126-byte file per *unsettled* child per tick, and a
        child that is settled is never asked about again. That is the smaller
        cost by far: the one this cache was written to avoid was re-parsing the
        parent's growing transcript, which `_settled` still prevents.
        """
        meta = directory / f"{path.stem}{META_SUFFIX}"
        if meta not in self._meta:
            document = _read_meta(meta)
            if not document:
                # Nothing to remember yet. Answer with it, ask again next tick.
                return document
            self._meta[meta] = document
        return self._meta[meta]

    def forget(self, agent_ids: Iterable[str]) -> None:
        """Drop what is remembered about these children. For tests and for restarts."""
        self._finished.difference_update(agent_ids)


def _candidates(directory: Path) -> list[tuple[str, Path]]:
    """Every subagent transcript in this tree, by the agent id its name carries.

    The `.meta.json` beside it names an `agentType`, a `description` and a
    `toolUseId`, and no id — the id is the filename. A file that is not one of
    these is not a child, and is skipped rather than guessed at.
    """
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return []
    found: list[tuple[str, Path]] = []
    for path in entries:
        if path.suffix != CHILD_SUFFIX or not path.name.startswith(CHILD_PREFIX):
            continue
        agent_id = path.name[len(CHILD_PREFIX) : -len(CHILD_SUFFIX)].strip()
        if agent_id:
            found.append((agent_id, path))
    return found


def _read_meta(path: Path) -> dict[str, Any]:
    """One `meta.json`, or an empty document if it cannot be read or is not one."""
    try:
        document: Any = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return document if isinstance(document, dict) else {}


def _answered(transcript: Path, calls: Mapping[str, str]) -> set[str]:
    """Which of these children the parent's own record says are over.

    `calls` is `agentId → toolUseId`. The parent's transcript is scanned
    **backward from its end**, and each child is settled by the first thing said
    about its call, reading newest first:

    - a `tool_result` for it — the call came back, so the child is over, unless
      it says `LAUNCHED_NOT_FINISHED`, which is a background subagent answering
      the *launch* and going on working;
    - the `tool_use` that started it — nothing later mentioned it, so it is
      still out. **This is what bounds the scan**: the answer is always written
      after the spawn, so once the spawn is reached there is nothing further
      back to find.

    A child neither record mentions stays unsettled, and an unsettled child is
    listed. That covers the transcript that cannot be read at all, the one whose
    spawn record lies beyond `SCAN_LIMIT_BYTES`, and the child launched by a
    build that writes something this one does not recognise.
    """
    if not calls:
        return set()
    waiting = {call: agent for agent, call in calls.items()}
    finished: set[str] = set()
    for record in _backward(transcript):
        for call, over in _mentions(record):
            agent = waiting.pop(call, None)
            if agent is None:
                continue
            if over:
                finished.add(agent)
            if not waiting:
                return finished
    return finished


def _mentions(record: Mapping[str, Any]) -> Iterator[tuple[str, bool]]:
    """Every tool call this record speaks about, and whether it says it is over."""
    message = record.get("message")
    content = message.get("content") if isinstance(message, Mapping) else None
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, Mapping):
            continue
        kind = block.get("type")
        if kind == "tool_result":
            called = block.get("tool_use_id")
            if isinstance(called, str):
                yield called, not _merely_launched(record)
        elif kind == "tool_use":
            called = block.get("id")
            if isinstance(called, str):
                # Reached the spawn without having seen a result: still out.
                yield called, False


def _merely_launched(record: Mapping[str, Any]) -> bool:
    """Whether this result says the call was only *started*, not finished."""
    outcome = record.get("toolUseResult")
    return isinstance(outcome, Mapping) and outcome.get("status") == LAUNCHED_NOT_FINISHED


def _backward(path: Path) -> Iterator[dict[str, Any]]:
    """This transcript's records, newest first, in blocks from the end.

    A line that does not parse is skipped rather than ending the scan — the last
    line of a live transcript is routinely half-written, and the reference
    implementation's lesson was that treating format drift as an error failed
    ~99% of real transcripts (`legacy@1d32845:bridge/transcript.py:1213-1240`,
    *ported* as a rule if not as code).
    """
    try:
        with path.open("rb") as handle:
            end = handle.seek(0, 2)
            carry = b""
            read = 0
            while end > 0 and read < SCAN_LIMIT_BYTES:
                block = min(BLOCK_BYTES, end)
                end -= block
                handle.seek(end)
                carry = handle.read(block) + carry
                read += block
                lines = carry.split(b"\n")
                # The first piece may be half a record, because a block boundary
                # lands wherever it lands. It is carried into the next read, or
                # yielded below once the file's own beginning is reached.
                carry = lines[0]
                for line in reversed(lines[1:]):
                    record = _parsed(line)
                    if record is not None:
                        yield record
            if end == 0:
                record = _parsed(carry)
                if record is not None:
                    yield record
    except OSError as unreadable:
        _log.info("could not read %s to settle its children: %s", path, unreadable)


def _parsed(line: bytes) -> dict[str, Any] | None:
    if not line.strip():
        return None
    try:
        record = json.loads(line)
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def _row(parent: SessionInspection, agent_id: str, depth: object) -> SessionInspection:
    """One live child, as the seam holds it.

    **`RUNNING`, because it is.** A child is only a row while it is working, so
    there is no other state it could honestly be in — and `RUNNING` is also what
    closes the Reply Window for anything that later forgets to ask about the
    classification (`seams/agent.derive_reply_window`), which is a second lock
    on the same door.

    The workspace is the parent's. Measured rather than assumed: every record in
    both probes' child transcripts carried the parent's own `cwd`, because a
    subagent runs where its Session runs.
    """
    direct = depth == DIRECT_SPAWN_DEPTH and not isinstance(depth, bool)
    return SessionInspection(
        target=SessionTarget(agent=AgentKind.CLAUDE, session_id=agent_id, pid=parent.target.pid),
        workspace=parent.workspace,
        lifecycle=SessionLifecycle.LIVE,
        state=SessionState.RUNNING,
        waiting_for=WaitingFor(),
        # Not read, rather than read and empty. #76's progress reader answers
        # for Sessions the user can ask about, and this is not one of them.
        progress=None,
        last_activity=None,
        child=ChildClassification(
            kind=ChildKind.CHILD,
            # Carried where it can be established and `None` where it cannot,
            # which is the locked type's own rule: a child whose parent we
            # failed to identify is still a child, because demoting it over a
            # missing link would open the very Relay this closes.
            parent=parent.target if direct else None,
        ),
        # A Child Process is never named (#78). Said here as well as in the
        # registry because a name composed and then dropped is a name that
        # existed for one hop, and the rule reads better where the row is made.
        name=None,
    )

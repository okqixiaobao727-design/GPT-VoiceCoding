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

**Liveness is two conditions.** A classic child's row exists while (a) the
parent is `RUNNING` and (b) the parent's own record does not yet carry the
result of the tool call that started the child. Alone, (b) would keep a child
abandoned mid-turn — Esc on the parent — in the roster until the parent exited.
A finished child is **dropped, not kept as an ended row**: Claude's own roster
has no row for it, so *listed* can only mean *alive*. The second shape below
answers both conditions differently, and that is the whole of #231.

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

**A Session spawns children in two shapes, and everything above describes one
of them** (#231). A parent that passes `name` to the `Agent` tool does not get a
Task subagent; it gets an addressable in-process teammate, written into the same
tree and answered for in a different place. Measured on claude 2.1.260, the
acceptance run `20260904T124243Z`:

| | classic subagent | named teammate |
| --- | --- | --- |
| `meta.json` | `toolUseId`, `spawnDepth` 1 | `name`, `spawnDepth` **0**, `taskKind` |
| the spawn's `tool_result` | arrives when the child is over | arrives in **99 ms** |
| the parent, meanwhile | frozen for the child's whole life | idle, Stop hook and all |

All three readings above fail on it at once, which is why no teammate ever
reached the roster: the caller's `RUNNING` gate hid it for its whole life, the
absent `toolUseId` left `calls` empty so nothing could settle it, and depth 0
would have produced a row naming nobody. It is **shape-dependent, not
version-dependent** — the same 2.1.261 that offered a classic subagent
correctly writes this shape the moment the parent names one.

So the two liveness conditions are read per shape. A classic child still needs
its parent `RUNNING`, because a subagent abandoned mid-turn — Esc on the parent
— never gets its `tool_result` written and would sit in the roster until its
Session exited. A teammate needs no such backstop and could not survive one: its
Session goes idle *while it works*, and it is its Session that later records its
idle notification either way. Both conditions now live here, because both are
answered from the same two documents — the child's `meta.json` and the parent's
record — and splitting them across two modules is what let one shape fall
between them.

**The teammate's marker is the parent's record again, and it is legacy's.**
`<teammate-message teammate_id="…">` wraps a body whose `type` is
`idle_notification`; the wrapper is not the marker, because a teammate reports
through the same envelope it goes idle through. Read newest-first, the first
thing the parent says about a name answers for it: an idle notification or an
approved shutdown means stopped, any `SendMessage` addressed to that name — the
one that asks it to stop included, because a request is a wish and not a state —
means its Session set it going again, and the `Agent` call that named it bounds
the scan the way a `tool_use` id bounds the classic one. *Adapted* from
`legacy@1d32845:bridge/transcript.py:1339-1355,1435-1455,1078-1110`, which
tracked exactly these three markers from the parent's own transcript — same
place, same markers, different question: gen-1 asked whether a Stop Notice could
sound, this asks whether a roster row still exists.

**The newest marker wins on every read, and that is the difference between the
two kinds of memory here** (#236). A classic subagent's `tool_result` is the end
of a tool call, and a tool call that has come back cannot come back again, so
remembering it is remembering a fact. A teammate's idle notification is a *state
it is in*, and a state changes: #231 remembered it the classic way and the #231
reviewer measured the cost in `61537e10-…jsonl` — `probe-two` idle at 22:42:34Z,
working again from 22:42:55Z, never re-listed, because the `SendMessage` that
woke it reached a name nothing would ask about again. Legacy rebuilt its answer
from the file on every read for exactly this reason
(`legacy@1d32845:bridge/transcript.py:1078-1110`), and that is now *ported*
rather than adapted.

**What is remembered instead is where the file was read to, and legacy has no
such behaviour — that is a citation too.** Rebuilding per read must not mean
re-parsing a growing transcript per tick, the cost the #231 memory was bought to
avoid. Legacy paid that cost: `legacy@1d32845:bridge/transcript.py:1934-1957`
starts from `size` on every read and keeps no offset between two of them, and
what bounded it was a wall-clock deadline checked inside the loop
(`legacy@1d32845:bridge/transcript.py:1894-1896`, `_check_read_deadline`), which
abandoned the read where it stood. That is **dropped, because** a deadline makes
the roster's answer depend on how busy the machine was — the same file read
twice gives two answers — where this reads the same bytes every time. In its
place: a parent's teammate verdicts are kept beside the offset they were read
from, and the next read starts there, so a tick pays for what the parent wrote
since the last one and never for the Session's history. A parent that has
written nothing since is not opened at all.

The two ways that bound can be wrong are answered rather than assumed. A file
shorter than the offset is not the file the offset was about, and is read
afresh. And `SCAN_LIMIT_BYTES` is still the budget legacy's deadline was —
*adapted* from it, in bytes rather than seconds so that hitting it is a fact
about the file — so a read that gives up before reaching the offset leaves a gap
no memory may be trusted across, and every name it did not settle goes back to
*listed*.

**A cross-session peer is addressed through the identical field.** `SendMessage`
carries `to` for a teammate and for a Session on another socket alike, so the
recipient is matched against the names this Session's own tree says it started
and nothing else — the distinction legacy drew in as many words
(`legacy@1d32845:bridge/transcript.py:1412-1420`).

**Version-bound, and said out loud.** This layout is claude 2.1.246/2.1.247
on-disk behaviour for the classic shape and 2.1.260/2.1.261 for the teammate
one, and it is not what the reference implementation saw:
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
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from gpt_voicecoding.seams.agent import (
    ChildClassification,
    ChildKind,
    ProgressObservation,
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

#: The builds every shape here was read off: the classic subagent on Simon's
#: machine on 2026-08-27, the named teammate from the acceptance runs
#: `20260904T124243Z` and `20260904T202319Z` on 2026-09-05. Documentation for
#: the next re-probe, never a gate — the same decision `claude/discovery.py`
#: took, for the same reason.
PROVEN_AGAINST_VERSIONS: Final = ("2.1.246", "2.1.247", "2.1.260", "2.1.261")

#: The field of `meta.json` that ties a classic subagent to the tool call that
#: started it. A teammate's `meta.json` does not carry one.
TOOL_USE_ID: Final = "toolUseId"

#: What tells the two shapes apart, and the value that says "named in-process
#: teammate". Read rather than inferred from the absence of `TOOL_USE_ID`,
#: because absence is also what a `meta.json` written a moment ago looks like.
TASK_KIND: Final = "taskKind"
TEAMMATE_TASK_KIND: Final = "in_process_teammate"

#: The field of a teammate's `meta.json` that carries the address its Session
#: reaches it by. It is what the parent's record names it as, and so the only
#: thing that can tie a teammate to anything the parent later says.
TEAMMATE_NAME: Final = "name"

#: The tool that starts a child in either shape, and the field of its input that
#: is present only when the child being asked for is a teammate.
SPAWN_TOOL: Final = "Agent"

#: The tool a Session addresses a teammate through, and the two fields carrying
#: the recipient. The same tool reaches a Session on another
#: socket, so a recipient is only a teammate if this Session's own tree says it
#: started one by that name.
MESSAGE_TOOL: Final = "SendMessage"
RECIPIENT: Final = "to"

#: How a teammate's own message arrives in the record of the Session that
#: started it, and the one body `type` that says it has stopped working. Prose
#: is an ordinary report and a structured body of any other type is this
#: marker's wording having moved; neither settles anything, which leaves the row
#: listed — the safe direction, as everywhere else here.
TEAMMATE_MESSAGE: Final = re.compile(
    r'<teammate-message\b[^>]*\bteammate_id="([^"]*)"[^>]*>(.*?)</teammate-message>',
    re.DOTALL,
)

#: The two bodies that mean a teammate has stopped working, and they are not the
#: same kind of stop. `idle_notification` is a teammate resting between pieces of
#: work, and `shutdown_approved` is one agreeing to end — measured 2026-08-12 on
#: claude 2.1.235 in `61537e10-…jsonl`, where a Session wound down two teammates
#: and each answered its own shutdown by name. Both are *not listed*, because
#: this module asks one question and it is not "will it work again" but "is it
#: working now".
IDLE_NOTIFICATION: Final = "idle_notification"
SHUTDOWN_APPROVED: Final = "shutdown_approved"
STOPPED_WORKING: Final = frozenset({IDLE_NOTIFICATION, SHUTDOWN_APPROVED})

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

#: The `spawnDepth` of a subagent the Session itself started, and the one a
#: teammate of that Session carries. Deeper than either is unmeasured — the
#: probes drove one subagent at depth 1 and one teammate at depth 0 — so a
#: deeper child names no parent rather than naming the wrong one.
DIRECT_SPAWN_DEPTH: Final = 1
DIRECT_TEAMMATE_DEPTH: Final = 0

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

    **The memory is the whole reason this is a class**, and there are two of
    them because the two shapes stop differently (#236).

    A **classic subagent** that has finished cannot start again — its
    `tool_result` is the end of a tool call — so the answer is kept for good, and
    that is what stops a Session with old subagent files from re-reading its
    transcript every five seconds for a question that was settled the first time.
    One short string per such child ever seen under this engine.

    A **teammate** only ever *rests*, and its Session can set it going again, so
    no verdict about one is final: the newest marker for its name is the answer,
    every read. What is kept for it is that verdict beside the offset it was read
    from, so re-reading costs what the parent's file gained since the last tick
    rather than the whole of it. One entry per parent transcript, holding one
    boolean per teammate under it.
    """

    def __init__(self) -> None:
        #: Every `agentId` the parent's own record has answered for. Classic
        #: subagents only: a teammate is never settled for good.
        self._finished: set[str] = set()
        #: `agent-<agentId>.meta.json` by the path it was read from. The file is
        #: written once, at launch, and never again, so this is one read per
        #: child per engine rather than one per tick — the claim the docstring
        #: below makes, held by this dict rather than by hope.
        self._meta: dict[Path, dict[str, Any]] = {}
        #: What each parent's record last said about its teammates, and how far
        #: into that record it was read from.
        self._standing: dict[Path, _Standing] = {}

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

        **The parent's own state is one of the two liveness conditions, and it
        applies to one shape only.** A classic subagent is offered only while its
        Session is `RUNNING`, because that is the backstop for the child whose
        `tool_result` is never written — the parent was interrupted mid-turn. A
        teammate is offered whatever its Session is doing, because its Session
        goes idle *while it works* (#231) and its own marker arrives either way.
        The rule reads here rather than at the caller because it is a fact about
        the child's shape, and the shape is a document this method already holds.
        A child whose `meta.json` has not landed yet has no shape to read, and is
        treated as the classic one — the stricter of the two, so an unread
        document can only hide a row and never invent one. That window is the
        classic shape's own: the run that measured it wrote the transcript 24
        seconds before the metadata, while the teammate measured here had its
        metadata first.

        Nothing here raises. A tree that cannot be read is a Session with no
        children — the same answer as a Session that spawned none, and the right
        one either way, because the alternative is a whole lane's discovery
        failing over one unreadable directory.
        """
        if transcript is None or parent.target.pid is None:
            return ()
        directory = transcript.parent / transcript.stem / CHILD_DIRECTORY
        unsettled = (
            _Child(agent_id, self._describe(directory, path))
            for agent_id, path in _candidates(directory)
            if agent_id not in self._finished
        )
        working = parent.state is SessionState.RUNNING
        listed = [child for child in unsettled if working or not child.needs_a_working_parent]
        if not listed:
            # The ordinary case for a Session that never spawned anything, for
            # one whose classic children are all over, and for a stopped Session
            # whose only children are classic: the transcript is never opened.
            # A Session whose only children are *resting teammates* reaches the
            # line below instead, and is not opened there either — `_settled`
            # answers from what it remembers when the file has not grown.
            return ()

        calls = {child.agent_id: child.call for child in listed if child.call is not None}
        teammates = {child.agent_id: child.name for child in listed if child.name is not None}
        over = self._settled(transcript, calls, teammates)
        self._finished |= over & calls.keys()
        return tuple(_row(parent, child) for child in listed if child.agent_id not in over)

    def _settled(
        self,
        transcript: Path,
        calls: Mapping[str, str],
        teammates: Mapping[str, str],
    ) -> set[str]:
        """Which of these children are over, reading no more of the file than it must.

        `calls` is `agentId → toolUseId` for classic subagents and `teammates` is
        `agentId → name` for teammates. A classic subagent is over once and for
        all, and the caller keeps that; a teammate is over for as long as the
        newest thing said about its name says so, and that is kept here.

        How much of the file that costs is `_floor`'s answer and what to keep of
        it is `_Standing.record`'s; both live where they do so that this method
        reads as the four steps it is.
        """
        if not calls and not teammates:
            return set()
        size = _size(transcript)
        standing = self._standing_for(transcript, size)
        floor = _floor(standing, size, mid_call=bool(calls), agents=teammates)
        if floor is None:
            return standing.over(teammates)

        named: dict[str, set[str]] = {}
        for agent, name in teammates.items():
            named.setdefault(name, set()).add(agent)
        tail = _Tail(transcript, floor)
        finished, spoke_of = _read(tail, calls, named.keys())
        standing.record(named, spoke_of, tail)
        return finished | standing.over(teammates)

    def _standing_for(self, transcript: Path, size: int | None) -> _Standing:
        """What is remembered about this parent's teammates, if it is still about it.

        An offset only means anything against the file it was taken from, and a
        transcript shorter than one it was already read past is not that file.
        Nothing measured rewrites a Claude transcript backwards, so this is the
        safe direction rather than a case: what cannot be trusted is read afresh.
        """
        standing = self._standing.setdefault(transcript, _Standing())
        if size is not None and size < standing.read_through:
            standing = self._standing[transcript] = _Standing()
        return standing

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

        Re-reading costs one 126-byte file per child per tick whose document has
        still not landed — a settled classic child is never asked about again,
        and a resting teammate is asked but answered from this dict. That is the
        smaller cost by far: the one this cache was written to avoid was
        re-parsing the parent's growing transcript, which `_settled` still
        prevents.
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
        """Drop what is remembered about these children. For tests and for restarts.

        Both memories, because a child could be in either: a classic subagent's
        settlement and a teammate's standing verdict are dropped alike. The
        offset each parent was read to is kept — it is a fact about a file rather
        than about a child, and a name with no verdict is read for from the
        beginning anyway.
        """
        dropped = set(agent_ids)
        self._finished.difference_update(dropped)
        for standing in self._standing.values():
            for agent in dropped:
                standing.stopped.pop(agent, None)


@dataclass
class _Standing:
    """What one parent's record last said about its teammates, and how far in.

    Held per `agentId` rather than per name, though the marker names only a name:
    one name can belong to two files (Claude Code's rule for a reused teammate
    name is that the latest wins), and a file that has never been read for is
    what sends the next read back to the beginning. Keyed by the thing that
    appears and disappears, the two follow from each other.
    """

    #: `agentId → that teammate has stopped working`, as of `read_through`.
    #: Absent means never read for, which is not the same as read for and found
    #: working: the first sends the next read to the file's beginning.
    stopped: dict[str, bool] = field(default_factory=dict)
    #: The offset every record below which has been read whole. Where the next
    #: read starts, and what keeps it costing the file's growth rather than its
    #: length.
    read_through: int = 0

    def over(self, agents: Iterable[str]) -> set[str]:
        """Which of these teammates the newest marker read so far says has stopped."""
        return {agent for agent in agents if self.stopped.get(agent, False)}

    def unread(self, agents: Iterable[str]) -> bool:
        """Whether any of these has never been read for, and so has no offset to resume from."""
        return any(agent not in self.stopped for agent in agents)

    def record(
        self,
        named: Mapping[str, set[str]],
        spoke_of: Mapping[str, bool],
        tail: _Tail,
    ) -> None:
        """Keep what one read said, and how far it got. The whole scheme is these three arms.

        `named` is every teammate the read was asked about, as `name → the
        agentIds holding it`; `spoke_of` is what it found for the names the
        stretch it read mentioned. A name it did not mention is answered by which
        stretch that was:

        - a read that gave up at `SCAN_LIMIT_BYTES` never reached the offset it
          was resuming from, so records newer than everything here lie unread in
          between. Trusting the memory across that gap could hide a teammate
          woken inside it — the failure #236 exists to remove — so it is dropped,
          and the name is listed until a read reaches it;
        - a read from the file's beginning found no word about the name at all,
          which is a teammate working;
        - a read of what the file gained simply says nothing about the name, and
          what was already known still stands.
        """
        for name, agents in named.items():
            if name in spoke_of:
                self.stopped.update(dict.fromkeys(agents, spoke_of[name]))
            elif tail.gave_up:
                for agent in agents:
                    self.stopped.pop(agent, None)
            elif tail.floor == 0:
                self.stopped.update(dict.fromkeys(agents, False))
        self.read_through = max(self.read_through, tail.complete_through)


def _floor(
    standing: _Standing,
    size: int | None,
    *,
    mid_call: bool,
    agents: Iterable[str],
) -> int | None:
    """Where the next read of this parent starts, or `None` for no read at all.

    Three answers, and the middle one is the whole saving. A classic subagent
    still unsettled, or a teammate this parent has never been read for, needs the
    scan that runs back to the record that started it — there is no offset either
    could resume from. A file that has gained nothing since needs no read at all.
    Everything else needs only what it gained.
    """
    if mid_call or standing.unread(agents):
        return 0
    if size is None or size <= standing.read_through:
        return None
    return standing.read_through


def _size(path: Path) -> int | None:
    """How long this transcript is, or `None` if that cannot be asked.

    A file that cannot be stat'ed has not grown as far as this is concerned: it
    is gone, or unreachable, and neither is a marker. What is remembered about it
    stands until something newer is actually read.
    """
    try:
        return path.stat().st_size
    except OSError:
        return None


def _candidates(directory: Path) -> list[tuple[str, Path]]:
    """Every subagent transcript in this tree, by the agent id its name carries.

    The `.meta.json` beside it names an `agentType`, a `description` and — by
    shape — either a `toolUseId` or a `name`, and no id: the id is the filename,
    in both shapes. A file that is not one of these is not a child, and is
    skipped rather than guessed at.
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


@dataclass(frozen=True)
class _Child:
    """One `agent-<agentId>` pair on disk, read as the shape its metadata says.

    **The document is read for its shape in one place — `teammate` — and every
    other difference is expressed in terms of that one predicate**: what can
    settle this child, what `spawnDepth` means "started by the Session that owns
    this directory", and whether the parent has to be mid-turn for it to be
    offered at all. They stay properties of one type rather than two
    implementations because a caller never chooses between them: it holds
    whatever the directory gave it and asks the same four questions either way.
    What #231 fixed was those answers being spread across the four call sites
    that needed them, which is how one shape fell between two of them.

    `document` is the child's `meta.json`, which is empty until it lands. An
    unread document has no shape, and every property below answers for it the
    way it answers for a document that names nothing: as the classic shape,
    which is the stricter of the two.
    """

    agent_id: str
    document: Mapping[str, Any]

    @property
    def teammate(self) -> bool:
        """Whether this is a named in-process teammate rather than a Task subagent."""
        return self.document.get(TASK_KIND) == TEAMMATE_TASK_KIND

    @property
    def call(self) -> str | None:
        """The tool call that started a classic subagent, if its document names one."""
        called = self.document.get(TOOL_USE_ID)
        return called if not self.teammate and isinstance(called, str) else None

    @property
    def name(self) -> str | None:
        """The address a teammate answers to, if its document names one.

        A child that has neither this nor a `call` can be settled by nothing —
        and an unsettled child is listed, which is the safe way round and the
        same answer #79 gave a `meta.json` that named no `toolUseId`.
        """
        named = self.document.get(TEAMMATE_NAME)
        return named if self.teammate and isinstance(named, str) and named.strip() else None

    @property
    def own_depth(self) -> int:
        """The `spawnDepth` that means "started by the Session that owns this tree"."""
        return DIRECT_TEAMMATE_DEPTH if self.teammate else DIRECT_SPAWN_DEPTH

    @property
    def needs_a_working_parent(self) -> bool:
        """Whether this child is offered only while its Session is mid-turn.

        The classic subagent is, because a `tool_result` that never arrives —
        Esc on the parent — leaves nothing else to end it. A teammate is not,
        and could not be: its Session goes idle while it works, which is the
        whole of #231.
        """
        return not self.teammate


def _read(
    tail: _Tail,
    calls: Mapping[str, str],
    names: Iterable[str],
) -> tuple[set[str], dict[str, bool]]:
    """What the stretch of the parent's record `tail` covers says about these children.

    `calls` is `agentId → toolUseId` for classic subagents and `names` is the
    teammate names to watch for. The records arrive **newest first**, and each
    child is answered for by the first thing said about it. The two answers are
    returned apart because they are kept apart: the classic one is which
    subagents have finished for good, and the teammate one is what the newest
    marker said about each name — `True` for stopped, `False` for working, and
    absent for a name this stretch never mentions, which is
    `_Standing.record`'s cue to leave what it already knew alone.

    A teammate answer is by name and not by `agentId` because that is what the
    record offers; `_Standing` is where the two are tied together, and it is
    also what reads `tail`'s two marks — how far this got, and whether it gave
    up — once this has driven it.

    For a classic subagent the marker is its tool call:

    - a `tool_result` for it — the call came back, so the child is over, unless
      it says `LAUNCHED_NOT_FINISHED`, which is a background subagent answering
      the *launch* and going on working;
    - the `tool_use` that started it — nothing later mentioned it, so it is
      still out. **This is what bounds the scan**: the answer is always written
      after the spawn, so once the spawn is reached there is nothing further
      back to find.

    For a teammate it is its name (`_teammate_mentions`), and the two are kept
    in separate namespaces rather than one: a `toolUseId` is Claude's to mint and
    a teammate name is the parent model's to invent, so nothing rules out a
    teammate called `toolu_…` and one namespace would let it answer for a call.

    **One name can belong to more than one file, and then it answers for all of
    them.** Claude Code's own rule for a reused teammate name is that the latest
    wins, so a long Session can leave two `agent-…` files claiming one address.
    The name is the only handle the parent's record offers, so a marker for it
    settles every child holding it: the alternative is that the older file, which
    nothing can ever name again, stays listed for the life of the engine — a dead
    row, which is the one outcome this module exists to prevent.

    A child neither record mentions stays unsettled, and an unsettled child is
    listed. That covers the transcript that cannot be read at all, the one whose
    spawn record lies beyond `SCAN_LIMIT_BYTES`, and the child launched by a
    build that writes something this one does not recognise.
    """
    waiting = {call: agent for agent, call in calls.items()}
    unanswered = set(names)
    finished: set[str] = set()
    spoke_of: dict[str, bool] = {}
    for record in tail:
        # Each namespace stops reading the moment it has nothing left to settle.
        # The ordinary Session has children of one shape only, so the other scan
        # is pure cost — and it is not small: one record measured 258 KB on this
        # machine, and the teammate reading is a regular expression over all of it.
        if waiting:
            for call, over in _mentions(record):
                agent = waiting.pop(call, None)
                if agent is not None and over:
                    finished.add(agent)
        if unanswered:
            for name, over in _teammate_mentions(record):
                if name in unanswered:
                    unanswered.discard(name)
                    spoke_of[name] = over
        if not waiting and not unanswered:
            break
    return finished, spoke_of


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


def _teammate_mentions(record: Mapping[str, Any]) -> Iterator[tuple[str, bool]]:
    """Every teammate this record speaks about, and whether it says one has stopped.

    Three markers, all of them things the Session itself did or was told, which
    is why one file answers for all of them
    (`legacy@1d32845:bridge/transcript.py:1078-1110`, *adapted*):

    - a `<teammate-message>` from that name whose body is an `idle_notification`
      or a `shutdown_approved` — it has stopped working. Any other body is that
      teammate *speaking*, which is a teammate working: prose is an ordinary
      report, and a structured body of some other type is this marker's wording
      having moved;
    - a `SendMessage` addressed to that name — its Session set it going again;
    - the `Agent` call that named it — the spawn, which bounds the scan exactly
      as the classic path's `tool_use` id does.

    **Two markers this record may carry are deliberately not read.**

    A `SendMessage` whose body is `{"type": "shutdown_request"}` is terminal in
    the reference implementation (`legacy@1d32845:bridge/transcript.py:1409-1414`)
    — *dropped* here, and #231's stated reason for dropping it no longer holds.
    That reason was the memory: settlement there was rebuilt from the whole file
    on every read and here it was remembered, so a request read as terminal
    would have dropped for good a teammate that never agreed — measured, the
    first request to one probe teammate at 00:04:07Z did not take and a retry
    drew its approval 50 s later. #236 gave the teammate shape legacy's
    rebuild-per-read back, so a request read as terminal could now be taken back
    by the very next thing that teammate said. **Reading it is therefore an open
    question again, and it is not this ticket's** (#236 out of scope): what it
    would change is `test_a_teammate_that_never_agreed_stays_listed`, which #231
    pinned deliberately, and unpinning it wants its own measurement of what a
    teammate that never answers actually does. Until then a request stays the
    parent's *wish* rather than the child's state, and one ended without ever
    approving stays listed — the direction everything else here already chose.

    `{"type": "teammate_terminated"}` is **dropped, because** it cannot be
    attributed: it arrives under `teammate_id="system"` and names its teammate
    only in the prose of its `message`. Costless — it was measured in the *same
    record* as that teammate's own `shutdown_approved`, which names itself.

    Every one of them answers, and reading newest-first is what makes the newest
    answer win. A recipient this Session's tree never named is a cross-session
    peer reached through the identical field, and the caller's `pop` is what
    keeps one from moving a teammate's row.

    The wrapper arrives in a `user` record whose `message.content` is a **string**
    — the Session is told what its teammate said the way it is told what a person
    said — while the two tool calls are blocks in a list. Measured on 2.1.258
    and 2.1.260; a `last-prompt` record echoing the same text carries no
    `message` at all and so is never read twice.
    """
    message = record.get("message")
    content = message.get("content") if isinstance(message, Mapping) else None
    if isinstance(content, str):
        for name, body in TEAMMATE_MESSAGE.findall(content):
            yield name, _protocol_type(body) in STOPPED_WORKING
        return
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, Mapping) or block.get("type") != "tool_use":
            continue
        spoken = block.get("input")
        if not isinstance(spoken, Mapping):
            continue
        called = block.get("name")
        if called == MESSAGE_TOOL:
            addressed = spoken.get(RECIPIENT)
        elif called == SPAWN_TOOL:
            addressed = spoken.get(TEAMMATE_NAME)
        else:
            continue
        if isinstance(addressed, str):
            yield addressed, False


def _protocol_type(body: str) -> str | None:
    """The `type` of a teammate's structured message, or `None` if it is prose.

    A teammate's own message is quoted into its Session's record as text, so the
    body arrives as text and is read as text. Prose is what a teammate reporting
    looks like, and it says nothing about whether that teammate has stopped.
    """
    text = body.strip()
    if not text.startswith("{"):
        return None
    try:
        document: Any = json.loads(text)
    except ValueError:
        return None
    kind = document.get("type") if isinstance(document, Mapping) else None
    return kind if isinstance(kind, str) else None


def _merely_launched(record: Mapping[str, Any]) -> bool:
    """Whether this result says the call was only *started*, not finished."""
    outcome = record.get("toolUseResult")
    return isinstance(outcome, Mapping) and outcome.get("status") == LAUNCHED_NOT_FINISHED


class _Tail:
    """This transcript's records, newest first, in blocks from the end.

    A line that does not parse is skipped rather than ending the read — the last
    line of a live transcript is routinely half-written, and the reference
    implementation's lesson was that treating format drift as an error failed
    ~99% of real transcripts (`legacy@1d32845:bridge/transcript.py:1213-1240`,
    *ported* as a rule if not as code).

    **`floor` is where this stops, and it is why re-reading a growing transcript
    stays cheap** (#236). Given the offset a previous read finished at, this
    reads only what the file has gained since; given 0, it is the whole scan back
    to the record that started the child. The floor is always the first byte of a
    record, because that is what `complete_through` reports — a half-written last
    line lies *above* it and is read again, whole, next time.

    That last fact is legacy's: `legacy@1d32845:bridge/transcript.py:1934-1957`
    measured the same boundary on every read, marking its newest range an
    `incomplete_tail` "when the writer has not appended its newline yet", and
    skipped what did not parse there. **Adapted**, because legacy needed it only
    to forgive one unparsable record *inside* a read and here it is also the
    offset the *next* read starts at — the same measurement, carrying twice the
    weight, which is why it is reported rather than merely acted on.

    Two facts about the read itself are left behind for the caller, and neither
    can be had before it: how far in it may next start (`complete_through`) and
    whether it stopped at `SCAN_LIMIT_BYTES` rather than where it was told to
    (`gave_up`), which is the caller's warning that a stretch of the file went
    unread and no older answer covers it.
    """

    def __init__(self, path: Path, floor: int = 0) -> None:
        self.path = path
        self.floor = max(0, floor)
        #: The offset every record below which has now been read whole. It
        #: stays `floor` for a read that found no line ending at all, and for
        #: one the file refused — either way, nothing new was read whole.
        self.complete_through = self.floor
        #: Whether the read ran out of budget before reaching `floor`.
        self.gave_up = False

    def __iter__(self) -> Iterator[dict[str, Any]]:
        try:
            with self.path.open("rb") as handle:
                size = handle.seek(0, 2)
                end = size
                carry = b""
                read = 0
                measured = False
                while end > self.floor:
                    if read >= SCAN_LIMIT_BYTES:
                        self.gave_up = True
                        return
                    block = min(BLOCK_BYTES, end - self.floor)
                    end -= block
                    handle.seek(end)
                    carry = handle.read(block) + carry
                    read += block
                    lines = carry.split(b"\n")
                    if not measured and len(lines) > 1:
                        # The bytes after the file's last line ending are as much
                        # of a record as has been written, and no more. The next
                        # read starts where they start.
                        measured = True
                        self.complete_through = size - len(lines[-1])
                    # The first piece may be half a record, because a block
                    # boundary lands wherever it lands. It is carried into the
                    # next read, or yielded below once the floor is reached.
                    carry = lines[0]
                    for line in reversed(lines[1:]):
                        record = _parsed(line)
                        if record is not None:
                            yield record
                if end == self.floor:
                    record = _parsed(carry)
                    if record is not None:
                        yield record
        except OSError as unreadable:
            _log.info("could not read %s to settle its children: %s", self.path, unreadable)


def _parsed(line: bytes) -> dict[str, Any] | None:
    if not line.strip():
        return None
    try:
        record = json.loads(line)
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def _row(parent: SessionInspection, child: _Child) -> SessionInspection:
    """One live child, as the seam holds it.

    **`RUNNING`, because it is.** A child is only a row while it is working, so
    there is no other state it could honestly be in — and `RUNNING` is also what
    closes the Reply Window for anything that later forgets to ask about the
    classification (`seams/agent.derive_reply_window`), which is a second lock
    on the same door.

    The workspace is the parent's. Measured rather than assumed: every record in
    both probes' child transcripts carried the parent's own `cwd`, because a
    subagent runs where its Session runs.

    **The depth that means "this Session's own" differs by shape**, which is the
    third of #231's three facts: a classic subagent the Session started is at
    depth 1 and a teammate it started is at depth 0. Read against the one
    constant, a teammate produced a row naming nobody, and the acceptance step's
    next assertion failed on it.
    """
    depth = child.document.get("spawnDepth")
    direct = depth == child.own_depth and not isinstance(depth, bool)
    return SessionInspection(
        target=SessionTarget(
            agent=AgentKind.CLAUDE, session_id=child.agent_id, pid=parent.target.pid
        ),
        workspace=parent.workspace,
        lifecycle=SessionLifecycle.LIVE,
        state=SessionState.RUNNING,
        waiting_for=WaitingFor(),
        # Not read, rather than read and empty. #76's progress reader answers
        # for Sessions the user can ask about, and this is not one of them.
        progress=ProgressObservation(),
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
        # A teammate is the one shape that carries an address of its own —
        # `meta.json`'s `name`, the handle its Session reaches it by — and that
        # is not a Session Name (`CONTEXT.md`), so here the rule is applied
        # rather than merely inherited.
        name=None,
    )

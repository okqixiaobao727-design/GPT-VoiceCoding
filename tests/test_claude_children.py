"""What a Claude Session has spawned, found the only way there is to find it (#79).

**Every shape here was measured, and the measurement is the ticket.** #79's
brief named two sources for a Claude Child Process — `claude agents --json`
`kind`/parent evidence, and process ancestry — and `docs/acceptance-design.md`
parked the question of whether either would answer. A probe on 2026-08-27
against claude 2.1.246 settled it: one `claude` was given one turn that started
a subagent which slept 45 s, and was sampled every 3 s for 73 s with the agent
markers scrubbed the way `tests/acceptance/hand_started.py` scrubs them.

| source | while the child ran |
| --- | --- |
| `claude agents --json` | one row — the parent. Never two |
| `ps -eo pid,ppid,command` | one `claude` process. A subagent is **not a process** |
| the parent's own transcript | frozen for the whole 52 s the child ran |
| `<stem>/subagents/agent-<agentId>.jsonl` | appeared, then grew 27,977 → 50,922 bytes |
| `…/agent-<agentId>.meta.json` | written once at launch |

**Liveness is the parent's record, and three probes were needed to know why.**
The obvious marker is the child's own last record carrying `stop_reason:
"end_turn"`, and it is not reliable: 2.1.246 wrote it, 2.1.247 wrote
`stop_reason: null` on an equally finished child, and a third probe that traced
one child's tail every 2 s across its whole 56 s life caught the file ending on
an assistant `text` or `thinking` record with no `stop_reason` at **three
separate points while it was still working**. So the child's own file cannot say
when it is over. The parent's can: `meta.json` names the `toolUseId` that
started the child, and the parent writes a `tool_result` for exactly that id
when it ends.

The documents below are **verbatim from those probes**, trimmed only where a
field is long enough to bury the shape.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import test_claude_stop_wiring as wiring
from gpt_voicecoding.adapters.agent.claude import children
from gpt_voicecoding.seams.agent import (
    ChildKind,
    LaneDiscovery,
    SessionInspection,
    SessionState,
)
from gpt_voicecoding.seams.identity import AgentKind, SessionName, SessionTarget
from test_claude_stop_wiring import adapter_holding, roster

__all__ = ["roster"]  # the fixture is imported, and ruff must see it used

PARENT_SESSION_ID = "b6e7725c-9248-49cd-b436-c9ee3d5562f4"
PARENT_PID = 9231
AGENT_ID = "a891a18f447827175"
TOOL_USE_ID = "toolu_01GLoT2tCo9m9HGvL1CCzvoJ"
WORKSPACE = Path("/private/tmp/gvc79-probe/workspace")

#: Verbatim, and every field of it: this is the whole `meta.json`.
RECORDED_META = {
    "agentType": "general-purpose",
    "description": "Sleep then write child.txt",
    "toolUseId": TOOL_USE_ID,
    "spawnDepth": 1,
}

#: The parent's record of **starting** the child. On its own it says a child
#: exists; it never says one is over.
STARTED = {
    "type": "assistant",
    "message": {
        "role": "assistant",
        "content": [
            {
                "type": "tool_use",
                "id": TOOL_USE_ID,
                "name": "Agent",
                "input": {"description": "Sleep then write child.txt"},
            }
        ],
    },
    "timestamp": "2026-08-26T23:58:36.792Z",
}

#: The parent's record of the child **finishing**, 61 s later — the one marker
#: that turned out to be trustworthy. `toolUseResult` is trimmed of its usage
#: and token counts; `status` and `agentId` are exactly as recorded.
FINISHED = {
    "type": "user",
    "message": {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": TOOL_USE_ID}],
    },
    "toolUseResult": {
        "status": "completed",
        "agentId": AGENT_ID,
        "agentType": "general-purpose",
        "totalDurationMs": 60598,
    },
    "timestamp": "2026-08-26T23:59:37.394Z",
}

#: The same shape for a subagent started `run_in_background`, from an acceptance
#: run of 2026-08-26. The result arrives at once and the child goes on working,
#: which is why "a result exists" is not the test.
LAUNCHED = {
    "type": "user",
    "message": {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": TOOL_USE_ID}],
    },
    "toolUseResult": {
        "isAsync": True,
        "status": "async_launched",
        "agentId": AGENT_ID,
        "description": "Write child.txt",
    },
    "timestamp": "2026-08-26T23:20:42.438Z",
}

#: The parent saying something that is not about a child at all.
SAID = {
    "type": "assistant",
    "message": {"role": "assistant", "content": [{"type": "text", "text": "on it"}]},
    "timestamp": "2026-08-26T23:58:30.000Z",
}


def parent_row(state: SessionState = SessionState.RUNNING) -> SessionInspection:
    """The parent as `claude agents --json` gave it, named the way #78 names it."""
    return SessionInspection(
        target=SessionTarget(agent=AgentKind.CLAUDE, session_id=PARENT_SESSION_ID, pid=PARENT_PID),
        workspace=WORKSPACE,
        state=state,
        name=SessionName(project="workspace", task="workspace-1c"),
    )


def transcript_for(root: Path, records: list[dict] | None = None) -> Path:
    """The parent's own transcript, as `registration.py` reports its path."""
    project = root / "-private-tmp-gvc79-probe-workspace"
    project.mkdir(parents=True, exist_ok=True)
    path = project / f"{PARENT_SESSION_ID}.jsonl"
    path.write_text(
        "".join(f"{json.dumps(record)}\n" for record in (records or [])), encoding="utf-8"
    )
    return path


def write_child(
    transcript: Path,
    agent_id: str = AGENT_ID,
    *,
    meta: dict | None = RECORDED_META,
) -> Path:
    """One subagent on disk, in the tree the probes found it in.

    Its own transcript is written because one exists, and deliberately never
    read: three probes established that it cannot say when the child is over.
    """
    directory = transcript.parent / transcript.stem / "subagents"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"agent-{agent_id}.jsonl"
    path.write_text(
        json.dumps({"isSidechain": True, "agentId": agent_id, "type": "assistant"}) + "\n",
        encoding="utf-8",
    )
    if meta is not None:
        (directory / f"agent-{agent_id}.meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return path


def found(transcript: Path, reader: children.Children | None = None) -> tuple:
    return (reader or children.Children()).under(parent_row(), transcript)


class TestAChildIsSeen:
    """The roster lists it, under the Session that started it."""

    def test_a_running_child_is_a_row(self, tmp_path: Path) -> None:
        transcript = transcript_for(tmp_path, [STARTED])
        write_child(transcript)
        assert len(found(transcript)) == 1

    def test_it_is_addressed_by_its_agent_id_inside_its_parents_process(
        self, tmp_path: Path
    ) -> None:
        """A subagent has no process of its own, so the pid it runs under is its parent's.

        Both #74 invariants hold without touching the seam: a Claude target
        needs a session id (`agentId` is one the agent itself minted) and a pid
        (the process the child really is running inside).
        """
        transcript = transcript_for(tmp_path, [STARTED])
        write_child(transcript)
        assert found(transcript)[0].target == SessionTarget(
            agent=AgentKind.CLAUDE, session_id=AGENT_ID, pid=PARENT_PID
        )

    def test_it_is_listed_under_the_session_that_started_it(self, tmp_path: Path) -> None:
        transcript = transcript_for(tmp_path, [STARTED])
        write_child(transcript)
        row = found(transcript)[0]
        assert row.child.kind is ChildKind.CHILD
        assert row.child.parent == parent_row().target

    def test_it_is_never_named(self, tmp_path: Path) -> None:
        """#78: a name is what the user says to reach a Session, and this is unreachable."""
        transcript = transcript_for(tmp_path, [STARTED])
        write_child(transcript)
        assert found(transcript)[0].name is None

    def test_it_claims_no_progress_and_no_stop(self, tmp_path: Path) -> None:
        """`progress=None` is "not read", which is the truth: #76's reader is the parent's."""
        transcript = transcript_for(tmp_path, [STARTED])
        write_child(transcript)
        row = found(transcript)[0]
        assert row.progress is None
        assert row.last_activity is None
        assert row.state is SessionState.RUNNING

    def test_two_children_are_two_rows(self, tmp_path: Path) -> None:
        transcript = transcript_for(tmp_path, [STARTED])
        write_child(transcript, "a1111111111111111", meta=dict(RECORDED_META, toolUseId="t1"))
        write_child(transcript, "a2222222222222222", meta=dict(RECORDED_META, toolUseId="t2"))
        assert sorted(row.target.session_id or "" for row in found(transcript)) == [
            "a1111111111111111",
            "a2222222222222222",
        ]


class TestAChildThatIsOverIsNotAChild:
    """Claude's own roster has no row for a finished subagent, so neither has this.

    Liveness is two conditions and this file holds the second: the parent's
    record does not yet answer the tool call that started the child. The first —
    the parent is `RUNNING` — is the caller's, because it is already on the
    parent's inspection and costs nothing (advisor, 2026-08-27). Together they
    close the case the second alone leaves open: a child abandoned mid-turn
    never gets its result written, and would sit in the roster until its parent
    exited.
    """

    def test_a_child_whose_tool_call_came_back_is_dropped(self, tmp_path: Path) -> None:
        transcript = transcript_for(tmp_path, [STARTED, FINISHED])
        write_child(transcript)
        assert found(transcript) == ()

    def test_a_child_whose_tool_call_is_still_out_is_kept(self, tmp_path: Path) -> None:
        transcript = transcript_for(tmp_path, [SAID, STARTED])
        write_child(transcript)
        assert len(found(transcript)) == 1

    def test_a_result_for_some_other_call_settles_nothing(self, tmp_path: Path) -> None:
        """One id, one answer. A `Bash` coming back is not a subagent coming back."""
        elsewhere = json.loads(json.dumps(FINISHED))
        elsewhere["message"]["content"][0]["tool_use_id"] = "toolu_someBashCall"
        transcript = transcript_for(tmp_path, [STARTED, elsewhere])
        write_child(transcript)
        assert len(found(transcript)) == 1

    def test_an_asynchronous_launch_is_not_a_finish(self, tmp_path: Path) -> None:
        """`run_in_background` answers at once and leaves the child working.

        Read as an answer, every background subagent would vanish from the
        roster the instant it started — which is the whole population of
        children on the run that recorded this document.
        """
        transcript = transcript_for(tmp_path, [STARTED, LAUNCHED])
        write_child(transcript)
        assert len(found(transcript)) == 1

    def test_a_parent_transcript_that_cannot_be_read_settles_nothing(self, tmp_path: Path) -> None:
        """The roster's own word stands — what `None` records mean everywhere on this lane."""
        transcript = transcript_for(tmp_path, [STARTED, FINISHED])
        write_child(transcript)
        transcript.unlink()
        assert len(found(transcript)) == 1

    def test_a_child_the_parent_never_recorded_starting_is_kept(self, tmp_path: Path) -> None:
        """A file exists, so a child was launched; nothing here says it is over."""
        transcript = transcript_for(tmp_path, [SAID])
        write_child(transcript)
        assert len(found(transcript)) == 1

    def test_a_child_whose_meta_names_no_tool_call_is_kept(self, tmp_path: Path) -> None:
        """Nothing to match, so nothing can answer it — and listed is the safe way round."""
        transcript = transcript_for(tmp_path, [STARTED, FINISHED])
        write_child(transcript, meta={"agentType": "general-purpose", "spawnDepth": 1})
        assert len(found(transcript)) == 1


class TestNotAskingTwiceAboutAChildThatIsOver:
    """The memory that keeps a busy Session's transcript off the five-second cadence.

    A parent with a **live** child is not writing — measured twice, its file
    stayed frozen for the whole 52 s its child ran — so the lane's
    `(size, mtime_ns)` cache carries the repeated read. A parent whose children
    are all *over* is a different case: it may be writing steadily, and asking
    about settled children every tick would re-parse a growing transcript for an
    answer that cannot change.
    """

    def test_a_settled_child_is_never_asked_about_again(self, tmp_path: Path) -> None:
        transcript = transcript_for(tmp_path, [STARTED, FINISHED])
        write_child(transcript)
        reader = children.Children()
        assert found(transcript, reader) == ()

        # The parent's record now says nothing at all. A reader that had not
        # remembered would list the child again, because nothing answers for it.
        transcript.write_text(json.dumps(SAID) + "\n", encoding="utf-8")
        assert found(transcript, reader) == ()

    def test_a_session_whose_children_are_all_over_never_opens_its_transcript(
        self, tmp_path: Path
    ) -> None:
        transcript = transcript_for(tmp_path, [STARTED, FINISHED])
        write_child(transcript)
        reader = children.Children()
        found(transcript, reader)

        # Not "reads it and finds the same answer" — does not open it at all.
        # The file is removed, which a reader that still opened it would notice.
        transcript.unlink()
        assert reader.under(parent_row(), transcript) == ()

    def test_a_session_that_spawned_nothing_never_opens_its_transcript(
        self, tmp_path: Path
    ) -> None:
        transcript = transcript_for(tmp_path, [SAID])
        transcript.unlink()
        assert children.Children().under(parent_row(), transcript) == ()

    def test_the_metadata_beside_a_child_is_read_once_and_remembered(self, tmp_path: Path) -> None:
        """It is written at launch and never again, so re-reading it is pure cost."""
        transcript = transcript_for(tmp_path, [STARTED])
        write_child(transcript)
        reader = children.Children()
        assert len(found(transcript, reader)) == 1

        meta = transcript.parent / transcript.stem / "subagents" / f"agent-{AGENT_ID}.meta.json"
        meta.unlink()
        row = reader.under(parent_row(), transcript)[0]

        # Had it re-read, `spawnDepth` would be gone and the parent with it.
        assert row.child.parent == parent_row().target

    def test_a_child_seen_before_its_metadata_exists_is_still_settled_afterwards(
        self, tmp_path: Path
    ) -> None:
        """Remembering an *absent* `meta.json` is remembering nothing, forever.

        **Measured, not supposed.** The acceptance run `20260827T015022Z` created
        `agent-a0cfe094d970fc749.jsonl` at 13:53:34 and its `.meta.json` at
        13:53:58 — 24 seconds apart. The five-second cadence lands inside that
        window routinely, and a tick that did read no `toolUseId`, and could
        therefore never recognise the parent's `tool_result` for the call.

        The child then outlived its own completion: condition (a) hides it while
        the parent is idle, so it came back as a live row every time that Session
        ran again, for the life of the engine — against this ticket's "a finished
        child is dropped, not kept as a dead row".

        A document that names nothing is not an answer worth keeping. A *read*
        one still is, which `test_the_metadata_beside_a_child_is_read_once_and
        _remembered` above pins.
        """
        transcript = transcript_for(tmp_path, [STARTED])
        write_child(transcript, meta=None)
        reader = children.Children()
        assert len(found(transcript, reader)) == 1

        directory = transcript.parent / transcript.stem / "subagents"
        (directory / f"agent-{AGENT_ID}.meta.json").write_text(
            json.dumps(RECORDED_META), encoding="utf-8"
        )
        transcript.write_text(
            "".join(f"{json.dumps(record)}\n" for record in (STARTED, FINISHED)), encoding="utf-8"
        )
        assert found(transcript, reader) == ()


class TestReadingTheParentBackwardFromItsEnd:
    """The bound the advisor made non-negotiable (2026-08-27).

    The lane's whole-file reader cannot be borrowed here. `_row_with_stop`'s
    gate exists because a transcript on this machine measured 186 MB, and the
    case where a child's fate is wanted most is exactly the one where the parent
    *is* writing: a subagent started `run_in_background` leaves its parent
    working while it runs, so a whole-file parse would repeat every five
    seconds. Scanning back from EOF costs the child's lifetime instead of the
    Session's, because **the answer is always written after the spawn**.
    """

    def noise(self, count: int) -> list[dict]:
        """Enough of the parent talking to itself to span several read blocks."""
        return [
            dict(
                SAID,
                message={"role": "assistant", "content": [{"type": "text", "text": "x" * 4096}]},
            )
            for _ in range(count)
        ]

    def test_the_scan_stops_at_the_record_that_started_the_child(self, tmp_path: Path) -> None:
        """Everything before the spawn is unread, and the file says so.

        The head of the file is deliberate nonsense — bytes that are not JSON at
        all. A scan that reached them would have to skip them silently; a scan
        that stops at the spawn never sees them, and this is how that is told
        apart from "read it and coped".
        """
        transcript = transcript_for(tmp_path, [STARTED])
        write_child(transcript)
        head = b"\x00" * (128 * 1024) + b"\n"
        transcript.write_bytes(head + transcript.read_bytes())
        assert len(found(transcript)) == 1

    def test_an_answer_is_found_across_many_read_blocks(self, tmp_path: Path) -> None:
        """The result is old and the parent has written megabytes since.

        The `run_in_background` case in miniature: the child came back long ago
        and the parent has been working ever since, so the answer is nowhere
        near the end of the file.
        """
        transcript = transcript_for(tmp_path, [STARTED, FINISHED, *self.noise(200)])
        write_child(transcript)
        assert found(transcript) == ()

    def test_a_record_straddling_a_block_boundary_is_still_read(self, tmp_path: Path) -> None:
        """Blocks land where they land; a record cut in half by one is not half a record."""
        padding = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "y" * (children.BLOCK_BYTES + 4321)}],
            },
        }
        transcript = transcript_for(tmp_path, [STARTED, FINISHED, padding])
        write_child(transcript)
        assert found(transcript) == ()

    def test_a_half_written_last_line_costs_nothing(self, tmp_path: Path) -> None:
        """A live transcript routinely ends mid-record, and this reads one every tick."""
        transcript = transcript_for(tmp_path, [STARTED, FINISHED])
        with transcript.open("a", encoding="utf-8") as handle:
            handle.write('{"type": "assis')
        write_child(transcript)
        assert found(transcript) == ()

    def test_a_spawn_it_never_reaches_leaves_the_child_listed(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The backstop, and it fails the safe way round.

        A parent whose spawn record is missing or unreadable would otherwise be
        scanned to its first byte. Giving up reads as *not finished*: a child
        listed is refused by every Relay and every Stop Notice anyway, while a
        child dropped is one the roster stopped mentioning while it worked.
        """
        monkeypatch.setattr(children, "SCAN_LIMIT_BYTES", 1024)
        transcript = transcript_for(tmp_path, [STARTED, FINISHED, *self.noise(50)])
        write_child(transcript)
        assert len(found(transcript)) == 1


class TestAChildOfAChild:
    """Deeper than one is still a child; it is just not one this build can place.

    `spawnDepth` is the only thing on disk that says how deep a subagent is, and
    what a depth-2 tree looks like is **unmeasured** — the probes drove one
    subagent, at depth 1. So depth 1 names the Session that owns the directory
    and anything else names nobody, which is what the locked type asks for: "a
    child whose parent we failed to identify is still a child, and demoting it
    to `main` over a missing link would open exactly the Relay the
    classification exists to close" (`seams/agent.py`).
    """

    def test_a_deeper_child_is_still_a_child(self, tmp_path: Path) -> None:
        transcript = transcript_for(tmp_path, [STARTED])
        write_child(transcript, meta=dict(RECORDED_META, spawnDepth=2))
        assert found(transcript)[0].child.kind is ChildKind.CHILD

    def test_but_it_is_listed_under_nobody(self, tmp_path: Path) -> None:
        transcript = transcript_for(tmp_path, [STARTED])
        write_child(transcript, meta=dict(RECORDED_META, spawnDepth=2))
        assert found(transcript)[0].child.parent is None

    def test_a_child_whose_depth_cannot_be_read_is_listed_under_nobody(
        self, tmp_path: Path
    ) -> None:
        """Not established is not established, whatever the reason."""
        transcript = transcript_for(tmp_path, [STARTED])
        write_child(transcript, meta=None)
        row = found(transcript)[0]
        assert row.child.kind is ChildKind.CHILD
        assert row.child.parent is None


class TestWhenThereIsNothingToFind:
    """The ordinary case, and it has to cost nothing and raise nothing."""

    def test_a_session_that_spawned_nothing_has_no_subagents_directory(
        self, tmp_path: Path
    ) -> None:
        assert found(transcript_for(tmp_path, [SAID])) == ()

    def test_a_session_whose_transcript_was_never_registered_is_not_guessed_at(self) -> None:
        """The path is not derivable, so it is not derived (`claude/transcript.py`)."""
        assert children.Children().under(parent_row(), None) == ()

    def test_a_directory_that_cannot_be_read_yields_nothing_rather_than_raising(
        self, tmp_path: Path
    ) -> None:
        transcript = transcript_for(tmp_path, [STARTED])
        directory = transcript.parent / transcript.stem / "subagents"
        directory.mkdir(parents=True)
        directory.chmod(0o000)
        try:
            assert found(transcript) == ()
        finally:
            directory.chmod(0o755)

    def test_a_file_that_is_not_a_subagent_transcript_is_ignored(self, tmp_path: Path) -> None:
        transcript = transcript_for(tmp_path, [STARTED])
        write_child(transcript)
        directory = transcript.parent / transcript.stem / "subagents"
        (directory / "notes.txt").write_text("", encoding="utf-8")
        assert len(found(transcript)) == 1


def wired_transcript(root: Path, records: list[dict]) -> Path:
    """A transcript path shaped like the wiring helpers' Session, not the probes'.

    `adapter_holding` seeds one registration for `test_claude_stop_wiring.TARGET`,
    so the tree a child is written into has to hang off *that* Session's file.
    """
    path = root / f"{wiring.SESSION}.jsonl"
    path.write_text("".join(f"{json.dumps(record)}\n" for record in records), encoding="utf-8")
    return path


def wired_parent(state: SessionState) -> SessionInspection:
    """That same Session as a roster row, in whatever state the test needs."""
    return SessionInspection(target=wiring.TARGET, workspace=Path("/tmp/workspace"), state=state)


class TestWhatTheLaneComesBackWith:
    """The wiring: children reach the roster on the same verb the parents do.

    `discover` is the verb Bridge Core actually calls, every five seconds over
    the whole machine, so this is where a child becomes visible at all. The two
    reads it can do are **mutually exclusive**, which is what keeps this off the
    hot path: a stopped Session has its own transcript read for what it stopped
    on (#75) and can have no live child, while a `RUNNING` one is never opened
    for a stop and is the only kind that can.
    """

    def test_a_child_is_listed_immediately_under_its_parent(self, tmp_path: Path, roster) -> None:
        """ "Under" is the roster's order as well as the `child` document's parent.

        The acceptance reads the parent off `child.parent` (`journey.py`), which
        is the claim that matters; the order is what a person reading the
        Control Panel sees, and it costs nothing to get right here.
        """
        transcript = wired_transcript(tmp_path, [STARTED])
        write_child(transcript)
        roster(LaneDiscovery(rows=(wired_parent(SessionState.RUNNING),)))

        rows = asyncio.run(adapter_holding(transcript).discover()).rows

        assert [row.target.session_id for row in rows] == [wiring.SESSION, AGENT_ID]
        assert rows[1].child.parent == rows[0].target

    def test_a_stopped_parent_is_not_searched_for_children(self, tmp_path: Path, roster) -> None:
        """Condition (a). A Session that is not mid-turn has no child mid-turn.

        Cheap where it matters: this is the row whose whole transcript the lane
        *does* open for a stop, so searching it too would put the child read on
        the expensive side of the only gate the lane has.
        """
        transcript = wired_transcript(tmp_path, [STARTED])
        write_child(transcript)
        roster(LaneDiscovery(rows=(wired_parent(SessionState.IDLE),)))

        rows = asyncio.run(adapter_holding(transcript).discover()).rows

        assert [row.target.session_id for row in rows] == [wiring.SESSION]

    def test_a_lane_that_could_not_look_gains_no_children(self, tmp_path: Path, roster) -> None:
        """An error carries no rows, and a child is a row (`LaneDiscovery`)."""
        roster(LaneDiscovery(error="`claude` is not on the PATH"))
        lane = asyncio.run(adapter_holding(wired_transcript(tmp_path, [])).discover())
        assert lane.rows == ()
        assert lane.error is not None

    def test_the_adapter_stops_listing_a_child_when_its_call_comes_back(
        self, tmp_path: Path, roster
    ) -> None:
        """One adapter across two ticks, which is how the roster actually moves."""
        transcript = wired_transcript(tmp_path, [STARTED])
        write_child(transcript)
        roster(LaneDiscovery(rows=(wired_parent(SessionState.RUNNING),)))
        adapter = adapter_holding(transcript)
        assert len(asyncio.run(adapter.discover()).rows) == 2

        transcript.write_text(
            "".join(f"{json.dumps(record)}\n" for record in (STARTED, FINISHED)),
            encoding="utf-8",
        )
        rows = asyncio.run(adapter.discover()).rows

        assert [row.target.session_id for row in rows] == [wiring.SESSION]

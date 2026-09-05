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


# --- the other shape: a named in-process teammate (#231) ---------------------

#: The teammate's own address, which is what the parent calls it from then on,
#: and the id its file carries. Both verbatim from the acceptance run
#: `20260904T124243Z` on claude 2.1.260 — the run whose `child` step this
#: ticket was opened for.
TEAMMATE = "delta-writer"
TEAMMATE_AGENT_ID = "adelta-writer-30fdecef2eb339de"
TEAMMATE_TOOL_USE_ID = "toolu_01LCK4U4xiKcpQ7edMdSNVJZ"

#: Verbatim, and every field of it: this is the whole teammate `meta.json`.
#: Set beside `RECORDED_META` above, the three differences that broke #79's
#: reading are the whole ticket — no `toolUseId`, `spawnDepth` **0**, and a
#: `taskKind` that says why.
RECORDED_TEAMMATE_META = {
    "agentType": "delta-writer",
    "description": "Delayed child.txt write",
    "name": TEAMMATE,
    "spawnDepth": 0,
    "model": "opus[1m]",
    "taskKind": "in_process_teammate",
    "teamName": "session-1fc2b9cb",
    "color": "blue",
    "planModeRequired": False,
    "permissionMode": "default",
}

#: The parent's record of **starting** a teammate. The `Agent` call that starts
#: one is told apart from the `Agent` call that starts a classic subagent by one
#: field: `input.name`, the address the teammate answers to. `prompt` is trimmed.
SPAWNED = {
    "type": "assistant",
    "message": {
        "role": "assistant",
        "content": [
            {
                "type": "tool_use",
                "id": TEAMMATE_TOOL_USE_ID,
                "name": "Agent",
                "input": {
                    "description": "Delayed child.txt write",
                    "subagent_type": "general-purpose",
                    "name": TEAMMATE,
                    "prompt": "Do exactly these two things, in this order, and nothing else.",
                },
                "caller": {"type": "direct"},
            }
        ],
    },
    "timestamp": "2026-09-04T12:48:53.451Z",
}

#: **99 milliseconds later**, and this is why `toolUseId` could not have settled
#: a teammate even if its `meta.json` still carried one. The call that starts a
#: teammate comes back the moment the teammate exists, not when it is done:
#: `status` is `teammate_spawned` and the text says "The agent is now running".
#: Read the way `LAUNCHED_NOT_FINISHED` is not read, this would drop every
#: teammate from the roster within a tenth of a second of its birth.
SPAWN_ANSWERED = {
    "type": "user",
    "message": {
        "role": "user",
        "content": [
            {
                "tool_use_id": TEAMMATE_TOOL_USE_ID,
                "type": "tool_result",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Spawned successfully. (This tool result is internal metadata "
                            "\u2014 never quote or paste any part of it, including the ID "
                            "below, into a user-facing reply.)\n"
                            f"agent_id: {TEAMMATE}@session-1fc2b9cb\n"
                            f"name: {TEAMMATE}\n"
                            "The agent is now running and will receive instructions via "
                            "mailbox."
                        ),
                    }
                ],
            }
        ],
    },
    "toolUseResult": {"status": "teammate_spawned", "prompt": "Do exactly these two things"},
    "timestamp": "2026-09-04T12:48:53.550Z",
}


def teammate_message(body: str, *, who: str = TEAMMATE) -> dict:
    """One teammate speaking to the Session that started it.

    The wrapper and the envelope around it are verbatim; only the body varies,
    because the body is the whole marker. Note the shape of the record: this is
    a `user` record whose `message.content` is a **string**, not a list of
    blocks — the parent is told what its teammate said the same way it is told
    what a person said.
    """
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": (
                "Another Claude session sent a message:\n"
                f'<teammate-message teammate_id="{who}" color="blue">\n'
                f"{body}\n"
                "</teammate-message>\n\n"
                "This came from another Claude session \u2014 not typed by your user, but "
                "very likely working on their behalf."
            ),
        },
        "timestamp": "2026-09-04T12:49:06.570Z",
    }


#: The one marker that says a teammate has stopped working, verbatim from the
#: same run. There is no `tool_result` to wait for and no reliable end to its own
#: file, so this is the whole of settlement for the teammate shape.
WENT_IDLE = teammate_message(
    '{"type":"idle_notification","from":"delta-writer",'
    '"timestamp":"2026-09-04T12:49:05.906Z","idleReason":"available",'
    '"result":"Background wait started (24s). Waiting for it to complete '
    'before writing the file."}'
)

#: The same wrapper carrying prose. A teammate reporting is a teammate working,
#: which is why the marker is the body's `type` and not the wrapper.
REPORTED = teammate_message("Halfway through. Still writing.")

#: The parent addressing a teammate it already started, which puts it back to
#: work. Recorded on 2026-09-02 against claude 2.1.258, in this repository's own
#: transcript `9523ad6c-a18c-41fe-a845-a58abf16daf2.jsonl` at 15:54:28Z.
SENT_TO = {
    "type": "assistant",
    "message": {
        "role": "assistant",
        "content": [
            {
                "type": "tool_use",
                "id": "toolu_01WokeItUpAgain",
                "name": "SendMessage",
                "input": {
                    "to": TEAMMATE,
                    "summary": "Request the facts report",
                    "message": "Please send me your full report now.",
                },
            }
        ],
    },
    "timestamp": "2026-09-04T12:50:10.000Z",
}

#: The Session winding its teammate down. Recorded 2026-08-12 on claude 2.1.235
#: in `61537e10-0bce-4823-88c6-edda270fb209.jsonl` at 00:04:07Z, where one
#: Session shut down two probe teammates. The body is a **document** here,
#: because this is what the Session sent rather than what it was told.
ASKED_TO_STOP = {
    "type": "assistant",
    "message": {
        "role": "assistant",
        "content": [
            {
                "type": "tool_use",
                "id": "toolu_01ShutTheTeammateDown",
                "name": "SendMessage",
                "input": {
                    "to": TEAMMATE,
                    "summary": "shut down delta writer",
                    "message": {"type": "shutdown_request", "reason": "Probe finished."},
                    "type": "shutdown_request",
                    "recipient": TEAMMATE,
                },
            }
        ],
    },
    "timestamp": "2026-08-12T00:04:07.330Z",
}

#: The teammate agreeing, from the same transcript at 00:04:57Z. It arrives in
#: the same envelope an idle notification does and means something stronger, so
#: the body's `type` has to be read for both or a wound-down teammate is a
#: roster row for the life of the engine.
APPROVED_ITS_SHUTDOWN = teammate_message(
    '{"type":"shutdown_approved","requestId":"shutdown-1786493091848@delta-writer",'
    '"from":"delta-writer","timestamp":"2026-08-12T00:04:57.600Z",'
    '"paneId":"in-process","backendType":"in-process"}'
)

#: The announcement that rides alongside it, verbatim from 00:04:36Z. It is
#: **dropped, because** it cannot be attributed: it comes from `system` and
#: names its teammate only in prose. Costless to drop \u2014 it was measured in
#: the same record as that teammate's own `shutdown_approved`, which names itself.
ANNOUNCED_TERMINATED = teammate_message(
    f'{{"type":"teammate_terminated","message":"{TEAMMATE} has shut down."}}', who="system"
)

#: The same tool, addressed to a Session on another socket. Cross-session peers
#: are reached through the identical field, and one must never be able to move a
#: teammate's row \u2014 the distinction legacy drew in as many words
#: (`legacy@1d32845:bridge/transcript.py:1412-1420`).
SENT_ELSEWHERE = {
    "type": "assistant",
    "message": {
        "role": "assistant",
        "content": [
            {
                "type": "tool_use",
                "id": "toolu_01ToAPeerNotATeammate",
                "name": "SendMessage",
                "input": {"to": "uds:/tmp/cc-socks/20725.sock", "message": "a ruling"},
            }
        ],
    },
    "timestamp": "2026-09-04T12:50:11.000Z",
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


def found(
    transcript: Path,
    reader: children.Children | None = None,
    state: SessionState = SessionState.RUNNING,
) -> tuple:
    return (reader or children.Children()).under(parent_row(state), transcript)


def append(transcript: Path, *records: dict) -> None:
    """The parent writing another record, which is the only way its file grows."""
    with transcript.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(f"{json.dumps(record)}\n")


def noise(count: int) -> list[dict]:
    """Enough of the parent talking to itself to span several read blocks."""
    return [
        dict(
            SAID,
            message={"role": "assistant", "content": [{"type": "text", "text": "x" * 4096}]},
        )
        for _ in range(count)
    ]


def teammate_on_disk(transcript: Path, **overrides) -> Path:
    """One named in-process teammate on disk, in the same tree a subagent uses."""
    return write_child(
        transcript,
        TEAMMATE_AGENT_ID,
        meta=dict(RECORDED_TEAMMATE_META, **overrides) if overrides else RECORDED_TEAMMATE_META,
    )


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
        """The child was not read; #76's progress reader belongs to the parent."""
        transcript = transcript_for(tmp_path, [STARTED])
        write_child(transcript)
        row = found(transcript)[0]
        assert str(row.progress.availability) == "not_read"
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
        transcript = transcript_for(tmp_path, [STARTED, FINISHED, *noise(200)])
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
        transcript = transcript_for(tmp_path, [STARTED, FINISHED, *noise(50)])
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


class TestATeammateIsAChildToo:
    """The other shape a Claude Session spawns a child in, and the one that was invisible (#231).

    **Measured on claude 2.1.260, acceptance run `20260904T124243Z`.** A parent
    that passes `name` to the `Agent` tool does not get a Task subagent; it gets
    an addressable in-process teammate, and all three things #79 read off a
    child are different — the shapes are set side by side in `children.py`'s own
    docstring, and the documents below are what that table was read off.

    The shape defeated every one of #79's three readings at once: the
    caller's `RUNNING` gate hid it, `toolUseId` was absent so nothing could
    settle it, and depth 0 would have left the row naming nobody. It is
    shape-dependent and not version-dependent \u2014 the same 2.1.261 that
    passed the acceptance `child` step with a classic subagent writes this
    shape the moment the parent names one.
    """

    def test_a_teammate_is_a_row_while_its_parent_is_idle(self, tmp_path: Path) -> None:
        """The gate #79 put on the caller, measured against the shape that outlives it.

        The parent's own turn ended at 12:48:56Z \u2014 Stop hook and all \u2014
        while the teammate worked on to 12:49:05Z and beyond. Under the old rule
        the whole of the child's life fell outside the window it was looked for in.
        """
        transcript = transcript_for(tmp_path, [SPAWNED, SPAWN_ANSWERED])
        teammate_on_disk(transcript)
        assert len(found(transcript, state=SessionState.IDLE)) == 1

    def test_it_is_listed_under_the_session_that_started_it(self, tmp_path: Path) -> None:
        """`spawnDepth` 0 is a teammate of this Session, not a child of nobody."""
        transcript = transcript_for(tmp_path, [SPAWNED, SPAWN_ANSWERED])
        teammate_on_disk(transcript)
        row = found(transcript, state=SessionState.IDLE)[0]
        assert row.child.kind is ChildKind.CHILD
        assert row.child.parent == parent_row().target

    def test_it_is_addressed_by_its_agent_id_and_never_by_its_name(self, tmp_path: Path) -> None:
        """A teammate has an address its Session says out loud, and the roster still refuses it.

        This is the one shape where a child really does carry a name of its own
        (`meta.json`'s `name`, which is what `SendMessage` addresses), so #78's
        rule has to be applied here rather than merely inherited: a Child
        Process is seen and never named.
        """
        transcript = transcript_for(tmp_path, [SPAWNED, SPAWN_ANSWERED])
        teammate_on_disk(transcript)
        row = found(transcript, state=SessionState.IDLE)[0]
        assert row.name is None
        assert row.target.session_id == TEAMMATE_AGENT_ID

    def test_the_answer_to_the_spawn_is_not_the_teammate_finishing(self, tmp_path: Path) -> None:
        """99 ms, and the teammate had 24 seconds of waiting still to do.

        `LAUNCHED_NOT_FINISHED` covers the same trap for a background subagent by
        reading one status value. This shape needs no such test because a
        teammate's call is not what settles it \u2014 but the record is here so
        that a future build putting `toolUseId` back cannot quietly reintroduce it.
        """
        transcript = transcript_for(tmp_path, [SPAWNED, SPAWN_ANSWERED])
        teammate_on_disk(transcript, toolUseId=TEAMMATE_TOOL_USE_ID)
        assert len(found(transcript, state=SessionState.IDLE)) == 1

    def test_a_teammate_that_reported_itself_idle_is_dropped(self, tmp_path: Path) -> None:
        """The whole of settlement for this shape, and it is the parent's record again.

        *Adapted* from `legacy@1d32845:bridge/transcript.py:1339-1355,1435-1455`,
        which settled a teammate on exactly this marker. The place is the same
        (the parent's own transcript) and the marker is the same; what differs is
        what it settles \u2014 gen-1 asked whether a Stop Notice could sound,
        this asks whether a roster row still exists.
        """
        transcript = transcript_for(tmp_path, [SPAWNED, SPAWN_ANSWERED, WENT_IDLE])
        teammate_on_disk(transcript)
        assert found(transcript, state=SessionState.IDLE) == ()

    def test_a_teammate_talking_is_a_teammate_working(self, tmp_path: Path) -> None:
        """The wrapper is not the marker; the body's `type` is.

        A teammate reports through the same `<teammate-message>` envelope it
        goes idle through, and reading the envelope would drop a working child
        the first time it said anything.
        """
        transcript = transcript_for(tmp_path, [SPAWNED, SPAWN_ANSWERED, REPORTED])
        teammate_on_disk(transcript)
        assert len(found(transcript, state=SessionState.IDLE)) == 1

    def test_a_teammate_the_parent_has_spoken_to_since_is_working_again(
        self, tmp_path: Path
    ) -> None:
        """An idle teammate is not a finished one \u2014 its Session can set it going again.

        Reading newest-first is what makes this fall out: the `SendMessage` is
        the newest thing said about that name, so it is the one that answers.
        """
        transcript = transcript_for(tmp_path, [SPAWNED, SPAWN_ANSWERED, WENT_IDLE, SENT_TO])
        teammate_on_disk(transcript)
        assert len(found(transcript, state=SessionState.IDLE)) == 1

    def test_a_message_to_a_peer_is_not_a_message_to_a_teammate(self, tmp_path: Path) -> None:
        """Cross-session peers are addressed through the identical field.

        A ruling sent down a socket must not raise a teammate that has gone idle,
        which is only true because the recipient is matched against the names
        this Session's own `subagents` tree says it started.
        """
        transcript = transcript_for(tmp_path, [SPAWNED, SPAWN_ANSWERED, WENT_IDLE, SENT_ELSEWHERE])
        teammate_on_disk(transcript)
        assert found(transcript, state=SessionState.IDLE) == ()

    def test_an_idle_report_naming_another_teammate_settles_nothing(self, tmp_path: Path) -> None:
        """One name, one answer \u2014 the teammate shape's version of one id, one answer."""
        someone_else = teammate_message('{"type":"idle_notification"}', who="scout")
        transcript = transcript_for(tmp_path, [SPAWNED, SPAWN_ANSWERED, someone_else])
        teammate_on_disk(transcript)
        assert len(found(transcript, state=SessionState.IDLE)) == 1

    def test_a_structured_message_this_build_does_not_know_settles_nothing(
        self, tmp_path: Path
    ) -> None:
        """Wording that has moved leaves the row listed, which is the safe way round."""
        transcript = transcript_for(
            tmp_path,
            [SPAWNED, SPAWN_ANSWERED, teammate_message('{"type":"went_to_sleep_notification"}')],
        )
        teammate_on_disk(transcript)
        assert len(found(transcript, state=SessionState.IDLE)) == 1

    def test_a_teammate_whose_meta_names_no_name_is_kept(self, tmp_path: Path) -> None:
        """Nothing to match, so nothing can answer it. The same rule the classic path has."""
        transcript = transcript_for(tmp_path, [SPAWNED, SPAWN_ANSWERED, WENT_IDLE])
        teammate_on_disk(transcript, name=None)
        assert len(found(transcript, state=SessionState.IDLE)) == 1

    def test_a_teammate_is_never_asked_about_twice(self, tmp_path: Path) -> None:
        """The memory the classic path has, held for the same reason and by the same set."""
        transcript = transcript_for(tmp_path, [SPAWNED, SPAWN_ANSWERED, WENT_IDLE])
        teammate_on_disk(transcript)
        reader = children.Children()
        assert found(transcript, reader, SessionState.IDLE) == ()

        transcript.unlink()
        assert reader.under(parent_row(SessionState.IDLE), transcript) == ()

    def test_the_scan_stops_at_the_record_that_spawned_the_teammate(self, tmp_path: Path) -> None:
        """The bound the classic path gets from the `tool_use` id, by the teammate's name.

        Everything before the spawn is bytes that are not JSON at all, so a scan
        that read them would have to skip them silently and this would not tell
        the two apart.
        """
        transcript = transcript_for(tmp_path, [SPAWNED, SPAWN_ANSWERED])
        teammate_on_disk(transcript)
        transcript.write_bytes(b"\x00" * (128 * 1024) + b"\n" + transcript.read_bytes())
        assert len(found(transcript, state=SessionState.IDLE)) == 1

    def test_a_teammate_that_approved_its_own_shutdown_is_dropped(self, tmp_path: Path) -> None:
        """A teammate wound down has stopped working, and it says so by name.

        `shutdown_approved` rides the same envelope an idle notification does,
        so reading only the one body left every teammate its Session ever ended
        listed for the life of the engine \u2014 the dead row #79 removed for
        classic children.
        """
        transcript = transcript_for(
            tmp_path, [SPAWNED, SPAWN_ANSWERED, ASKED_TO_STOP, APPROVED_ITS_SHUTDOWN]
        )
        teammate_on_disk(transcript)
        assert found(transcript, state=SessionState.IDLE) == ()

    def test_being_asked_to_stop_is_not_the_teammate_having_stopped(self, tmp_path: Path) -> None:
        """A request is the parent's wish; only the teammate reports its own state.

        Reading newest-first, the request can only be the answering marker when
        no approval lies newer than it — exactly the case where the teammate has
        **not** agreed. Measured in the transcript this fixture comes from: the
        first request to one probe teammate at 00:04:07Z did not take, and a
        retry drew its approval 50 s later. Under a settlement that is remembered
        and cannot be taken back, reading the wish would drop a live teammate for
        good — *adapted* from `legacy@1d32845:bridge/transcript.py:1409-1414`,
        where the same marker was terminal because every read rebuilt the answer
        from the whole file and could take it back.
        """
        transcript = transcript_for(tmp_path, [SPAWNED, SPAWN_ANSWERED, ASKED_TO_STOP])
        teammate_on_disk(transcript)
        assert len(found(transcript, state=SessionState.IDLE)) == 1

    def test_a_teammate_that_never_agreed_stays_listed(self, tmp_path: Path) -> None:
        """The cost of the line above, said out loud: this is the direction it fails in.

        A teammate its Session wound down that never answered is listed until the
        engine restarts. That is the same trade every other unsettled child here
        makes — a child listed is refused by every Relay and every Stop Notice
        anyway, while a child dropped is one the roster stopped mentioning while
        it was still working.
        """
        transcript = transcript_for(tmp_path, [SPAWNED, SPAWN_ANSWERED, ASKED_TO_STOP, REPORTED])
        teammate_on_disk(transcript)
        assert len(found(transcript, state=SessionState.IDLE)) == 1

    def test_an_announcement_from_system_settles_nobody(self, tmp_path: Path) -> None:
        """`teammate_terminated` names its teammate in prose, so it names nobody here."""
        transcript = transcript_for(tmp_path, [SPAWNED, SPAWN_ANSWERED, ANNOUNCED_TERMINATED])
        teammate_on_disk(transcript)
        assert len(found(transcript, state=SessionState.IDLE)) == 1

    def test_a_name_used_twice_answers_for_every_file_holding_it(self, tmp_path: Path) -> None:
        """Claude Code's own rule for a reused teammate name is that the latest wins.

        A long Session can leave two `agent-\u2026` files claiming one address,
        and the name is the only handle the parent's record offers. Settling both
        is the only reading that does not leave the older file \u2014 which
        nothing can ever name again \u2014 listed for the life of the engine.
        """
        transcript = transcript_for(tmp_path, [SPAWNED, SPAWN_ANSWERED, WENT_IDLE])
        teammate_on_disk(transcript)
        write_child(transcript, "aearlier-delta-writer-0000", meta=RECORDED_TEAMMATE_META)
        assert found(transcript, state=SessionState.IDLE) == ()

    def test_both_files_of_a_reused_name_are_listed_while_it_works(self, tmp_path: Path) -> None:
        transcript = transcript_for(tmp_path, [SPAWNED, SPAWN_ANSWERED])
        teammate_on_disk(transcript)
        write_child(transcript, "aearlier-delta-writer-0000", meta=RECORDED_TEAMMATE_META)
        assert len(found(transcript, state=SessionState.IDLE)) == 2


class TestATeammateWokenAgainComesBack:
    """A teammate's rest is not its end, and the roster has to be able to say so (#236).

    **Measured, and the measurement opened this ticket.** The #231 reviewer read
    `61537e10-0bce-4823-88c6-edda270fb209.jsonl`: `probe-two` reported itself
    idle at 22:42:34Z, was working again from 22:42:55Z, and never returned to
    the roster. #231 settled a teammate the way a classic subagent is settled —
    once, and remembered for the life of the engine — so the `SendMessage` that
    woke it reached a name nothing would ask about again.

    So the two kinds settle differently, and each for its own reason. A classic
    subagent's `tool_result` is the end of a tool call, and a tool call that came
    back cannot come back again: remembering it is remembering a fact. A
    teammate's idle notification is a state it is in, and a state can change —
    the newest marker for its name is the answer, on every read, which is what
    the reference implementation did (`legacy@1d32845:bridge/transcript.py:1078-1110`,
    now *ported* rather than adapted).

    The cost the memory was bought to avoid does not come back with it: what is
    remembered here is not the verdict alone but **how far into the file it was
    read from**, so a tick pays for what the parent wrote since the last one and
    never for the file's whole history. Everything from
    `test_a_record_half_written_when_it_was_read_is_read_whole_next_time` down is
    that bound: what it saves, and the three ways an offset into a growing file
    can be wrong.
    """

    def settled_idle(self, tmp_path: Path) -> tuple[Path, children.Children]:
        """One teammate that has rested, and the reader that watched it do so."""
        transcript = transcript_for(tmp_path, [SPAWNED, SPAWN_ANSWERED, WENT_IDLE])
        teammate_on_disk(transcript)
        reader = children.Children()
        assert found(transcript, reader, SessionState.IDLE) == ()
        return transcript, reader

    def test_a_teammate_its_session_wakes_is_listed_again(self, tmp_path: Path) -> None:
        """The `SendMessage` that measured this, arriving a tick after the rest.

        #231 already listed a teammate woken *before* the tick that would have
        settled it (`test_a_teammate_the_parent_has_spoken_to_since_is_working
        _again`), which is the same file read in one pass. This is the same file
        read in two, and it is the case the reviewer caught.
        """
        transcript, reader = self.settled_idle(tmp_path)
        append(transcript, SENT_TO)
        assert len(found(transcript, reader, SessionState.IDLE)) == 1

    def test_a_teammate_that_speaks_again_is_listed_again(self, tmp_path: Path) -> None:
        """A teammate reporting is a teammate working, whenever it reports."""
        transcript, reader = self.settled_idle(tmp_path)
        append(transcript, REPORTED)
        assert len(found(transcript, reader, SessionState.IDLE)) == 1

    def test_the_row_it_comes_back_as_is_the_row_it_left_as(self, tmp_path: Path) -> None:
        """Re-listing is the same row, not a new kind of one.

        Everything #231 pinned about a teammate's row is a fact about its
        `meta.json` and its parent, and neither changed while it rested — so this
        would be hard to break. It is asserted because "the roster row for a
        re-listed teammate is the same row shape as before it settled" is a claim
        this ticket makes, and a claim nothing reads is a claim nobody keeps.
        """
        transcript = transcript_for(tmp_path, [SPAWNED, SPAWN_ANSWERED])
        teammate_on_disk(transcript)
        reader = children.Children()
        before = found(transcript, reader, SessionState.IDLE)[0]

        append(transcript, WENT_IDLE)
        assert found(transcript, reader, SessionState.IDLE) == ()
        append(transcript, SENT_TO)
        assert found(transcript, reader, SessionState.IDLE) == (before,)

    def test_a_teammate_with_nothing_newer_than_its_rest_stays_unlisted(
        self, tmp_path: Path
    ) -> None:
        """The parent has gone on working; none of it was about this teammate."""
        transcript, reader = self.settled_idle(tmp_path)
        append(transcript, SAID)
        assert found(transcript, reader, SessionState.IDLE) == ()

    def test_a_teammate_that_rests_again_is_unlisted_again(self, tmp_path: Path) -> None:
        """Newest-marker-wins runs in both directions, or it is just a slower memory."""
        transcript, reader = self.settled_idle(tmp_path)
        append(transcript, SENT_TO)
        assert len(found(transcript, reader, SessionState.IDLE)) == 1
        append(transcript, WENT_IDLE)
        assert found(transcript, reader, SessionState.IDLE) == ()

    def test_a_classic_child_that_finished_cannot_come_back(self, tmp_path: Path) -> None:
        """The other kind, and the line between them: a finished tool call is final.

        Its `tool_use` record spoken of again would list it under the teammate
        rule. It does not, because the two memories are different memories.
        """
        transcript = transcript_for(tmp_path, [STARTED, FINISHED])
        write_child(transcript)
        reader = children.Children()
        assert found(transcript, reader) == ()
        append(transcript, STARTED)
        assert found(transcript, reader) == ()

    def test_a_record_half_written_when_it_was_read_is_read_whole_next_time(
        self, tmp_path: Path
    ) -> None:
        """The parent is writing while this reads, so its last line is routinely half a record.

        Reading only what the file gained is only safe if "gained" starts at the
        last *complete* record. Starting it at the previous end of file would cut
        the record in two and lose it for good — and the marker lost would be
        exactly the one that wakes a teammate.
        """
        transcript, reader = self.settled_idle(tmp_path)
        woken = json.dumps(SENT_TO)
        with transcript.open("a", encoding="utf-8") as handle:
            handle.write(woken[: len(woken) // 2])
        assert found(transcript, reader, SessionState.IDLE) == ()

        with transcript.open("a", encoding="utf-8") as handle:
            handle.write(f"{woken[len(woken) // 2 :]}\n")
        assert len(found(transcript, reader, SessionState.IDLE)) == 1

    def test_a_transcript_shorter_than_what_was_read_is_read_afresh(self, tmp_path: Path) -> None:
        """An offset into a file is only about that file, and a shorter one is another.

        Nothing measured writes a Claude transcript backwards, so this is the
        safe direction rather than a case: what cannot be trusted is re-read.
        """
        transcript, reader = self.settled_idle(tmp_path)
        transcript_for(tmp_path, [SPAWNED, SPAWN_ANSWERED])
        assert len(found(transcript, reader, SessionState.IDLE)) == 1

    def test_a_parent_that_has_not_written_since_is_not_read_at_all(self, tmp_path: Path) -> None:
        """The bound, at its cheapest end: no growth, no read.

        Not "reads it and finds the same answer" — does not open it. The file is
        removed, which a reader that still opened it would notice.
        """
        transcript, reader = self.settled_idle(tmp_path)
        transcript.unlink()
        assert reader.under(parent_row(SessionState.IDLE), transcript) == ()

    def test_a_parent_still_there_and_still_the_same_length_is_not_read_either(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The same claim about the file that is still there, which the answer cannot show.

        A read of a file that has not grown finds nothing and changes nothing, so
        the answer above is the answer either way and the test before this one
        would stay green with the saving deleted. What is being claimed is that
        no read is set up at all, so that is what is watched: `_Tail` is the only
        thing here that opens a transcript.
        """
        transcript, reader = self.settled_idle(tmp_path)
        prepared: list[Path] = []

        class Watched(children._Tail):
            def __init__(self, path: Path, floor: int = 0) -> None:
                prepared.append(path)
                super().__init__(path, floor)

        monkeypatch.setattr(children, "_Tail", Watched)
        assert reader.under(parent_row(SessionState.IDLE), transcript) == ()
        assert prepared == []

        # And the watch itself works: one byte more and it reads again.
        append(transcript, SENT_TO)
        assert len(found(transcript, reader, SessionState.IDLE)) == 1
        assert prepared == [transcript]

    def test_what_was_already_read_is_never_read_again(self, tmp_path: Path, monkeypatch) -> None:
        """The bound, at the end that matters: a parent writing steadily.

        The idle notification sits 800 KB back, and the parent has written every
        one of those bytes since. `SCAN_LIMIT_BYTES` is then cut to a thousandth
        of that distance, which puts the marker out of reach of any scan that
        starts at the end of the file — the control below is a fresh reader
        failing to reach it. A reader that remembers where it read to answers
        from a couple of hundred bytes.
        """
        transcript = transcript_for(tmp_path, [SPAWNED, SPAWN_ANSWERED, WENT_IDLE, *noise(200)])
        teammate_on_disk(transcript)
        reader = children.Children()
        assert found(transcript, reader, SessionState.IDLE) == ()

        monkeypatch.setattr(children, "SCAN_LIMIT_BYTES", 1024)
        append(transcript, SAID)
        assert found(transcript, reader, SessionState.IDLE) == ()
        assert len(found(transcript, children.Children(), SessionState.IDLE)) == 1

    def test_a_scan_that_gave_up_short_of_what_it_remembered_trusts_nothing(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The one case where the memory is dropped rather than kept, and why.

        A parent that wrote more between two ticks than a scan will read leaves
        a gap, and a marker in the gap is newer than anything remembered. Reading
        the memory over it could hide a teammate that had been woken inside it,
        which is the very failure this ticket removes — so a scan that gave up
        answers the way every other unfinished reading here answers: listed.
        """
        transcript, reader = self.settled_idle(tmp_path)
        monkeypatch.setattr(children, "SCAN_LIMIT_BYTES", 1024)
        append(transcript, *noise(50))
        assert len(found(transcript, reader, SessionState.IDLE)) == 1


class TestTheTwoShapesDoNotReachIntoEachOther:
    """A build that writes both must read both, and neither may move the other's rows."""

    def test_a_classic_child_still_needs_its_parent_running(self, tmp_path: Path) -> None:
        """#79's condition (a), kept exactly where it was for the shape it was measured on.

        A classic subagent abandoned mid-turn \u2014 Esc on the parent \u2014
        never gets its `tool_result` written, so without this it would sit in the
        roster until its Session exited. A teammate needs no such backstop: its
        Session records an idle notification for it either way.
        """
        transcript = transcript_for(tmp_path, [STARTED])
        write_child(transcript)
        assert found(transcript, state=SessionState.IDLE) == ()

    def test_both_shapes_side_by_side_under_one_running_parent(self, tmp_path: Path) -> None:
        transcript = transcript_for(tmp_path, [STARTED, SPAWNED, SPAWN_ANSWERED])
        write_child(transcript)
        teammate_on_disk(transcript)
        assert sorted(row.target.session_id or "" for row in found(transcript)) == sorted(
            [AGENT_ID, TEAMMATE_AGENT_ID]
        )

    def test_each_shape_is_settled_by_its_own_marker_alone(self, tmp_path: Path) -> None:
        """The classic child's call comes back; the teammate is still working."""
        transcript = transcript_for(tmp_path, [STARTED, SPAWNED, SPAWN_ANSWERED, FINISHED])
        write_child(transcript)
        teammate_on_disk(transcript)
        assert [row.target.session_id for row in found(transcript)] == [TEAMMATE_AGENT_ID]

    def test_a_teammate_going_idle_does_not_settle_a_classic_child(self, tmp_path: Path) -> None:
        transcript = transcript_for(tmp_path, [STARTED, SPAWNED, SPAWN_ANSWERED, WENT_IDLE])
        write_child(transcript)
        teammate_on_disk(transcript)
        assert [row.target.session_id for row in found(transcript)] == [AGENT_ID]


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

    def test_an_idle_parent_still_offers_its_teammate(self, tmp_path: Path, roster) -> None:
        """The acceptance `child` step's own case, end to end (#231).

        The step reads the parent off `child.parent` (`journey.py`), and this is
        the row the whole ticket is about: a Session whose turn has ended while
        the teammate it started works on.
        """
        transcript = wired_transcript(tmp_path, [SPAWNED, SPAWN_ANSWERED])
        teammate_on_disk(transcript)
        roster(LaneDiscovery(rows=(wired_parent(SessionState.IDLE),)))

        rows = asyncio.run(adapter_holding(transcript).discover()).rows

        assert [row.target.session_id for row in rows] == [wiring.SESSION, TEAMMATE_AGENT_ID]
        assert rows[1].child.parent == rows[0].target

    def test_the_adapter_stops_listing_a_teammate_when_it_reports_itself_idle(
        self, tmp_path: Path, roster
    ) -> None:
        """One adapter across two ticks, the way the roster actually moves."""
        transcript = wired_transcript(tmp_path, [SPAWNED, SPAWN_ANSWERED])
        teammate_on_disk(transcript)
        roster(LaneDiscovery(rows=(wired_parent(SessionState.IDLE),)))
        adapter = adapter_holding(transcript)
        assert len(asyncio.run(adapter.discover()).rows) == 2

        transcript.write_text(
            "".join(f"{json.dumps(record)}\n" for record in (SPAWNED, SPAWN_ANSWERED, WENT_IDLE)),
            encoding="utf-8",
        )
        rows = asyncio.run(adapter.discover()).rows

        assert [row.target.session_id for row in rows] == [wiring.SESSION]

    def test_a_stopped_parent_offers_no_classic_child(self, tmp_path: Path, roster) -> None:
        """Condition (a), for the one shape it was measured on.

        The tree *is* looked in now — that is what tells a teammate apart from a
        subagent — but a classic child of a stopped Session is not offered, and
        its parent's transcript is not opened to ask about one. What the gate
        costs moved from "no read at all" to one `iterdir` and one 126-byte
        `meta.json` per child; what it saves, the backward scan of a transcript
        measured at 186 MB on this machine, is untouched.
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

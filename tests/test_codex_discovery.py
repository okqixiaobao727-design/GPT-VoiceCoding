"""Merging the Codex daemon's roster with the machine's own process table.

The daemon shapes here are #82's measurements on 0.149.1 (`661d3d9`):
`thread/loaded/list` answers `{"data": [id, …]}` and `thread/read` answers
`{"thread": {"id", "name", "cwd", "status"}}`.

The case that drives the whole module is the one #82 also measured: a TUI
started while the daemon was down is **never adopted** by a daemon that starts
later. So "the daemon is up" and "the daemon knows this Session" are two
questions, and the roster has to answer the second one for itself.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

from gpt_voicecoding.adapters.agent._project import ProjectNames
from gpt_voicecoding.adapters.agent.codex import discovery
from gpt_voicecoding.adapters.agent.codex.discovery import discover
from gpt_voicecoding.adapters.agent.codex.processes import Candidate
from gpt_voicecoding.seams.agent import SessionState, WaitingKind

THREAD = "01a03b06-f995-7b60-bc9f-e2152ee4ed32"
OTHER_THREAD = "01a0385e-4872-7353-bdc5-8966c6165a8e"

#: When the processes in this file started. Rollouts written before it belong to
#: a Session that is over; ones written after it belong to the process reading.
STARTED_AT = 1_787_700_000.0


def running(pid: int, workspace: Path | str, *, started_at: float = STARTED_AT) -> Candidate:
    """One TUI in the process table, as `processes.enumerate_sessions` yields it."""
    return Candidate(pid=pid, workspace=Path(workspace), started_at=started_at)


class FakeDaemon:
    """A stand-in speaking the two methods, in the shapes #82 measured."""

    def __init__(self, threads: dict[str, dict], *, raises: Exception | None = None) -> None:
        self.threads = threads
        self.raises = raises
        self.asked: list[str] = []

    async def request(self, method: str, params: dict | None = None) -> dict:
        self.asked.append(method)
        if self.raises is not None:
            raise self.raises
        if method == "thread/loaded/list":
            return {"data": list(self.threads)}
        thread_id = (params or {}).get("threadId")
        return {"thread": self.threads[str(thread_id)]}


def thread(thread_id: str, *, cwd: str, status: str = "idle", name: str | None = None) -> dict:
    return {"id": thread_id, "cwd": cwd, "status": {"type": status}, "name": name}


def write_rollout(home: Path, thread_id: str, workspace: Path) -> Path:
    """One rollout on disk, in the 0.149.1 shape, written now unless moved."""
    directory = home / "sessions"
    directory.mkdir(exist_ok=True)
    path = directory / f"rollout-2026-08-26T10-25-08-{thread_id}.jsonl"
    path.write_text(
        json.dumps(
            {"type": "session_meta", "payload": {"session_id": thread_id, "cwd": str(workspace)}}
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def listing(*candidates: Candidate) -> object:
    async def processes() -> tuple[Candidate, ...]:
        return candidates

    return processes


def refusing_processes(error: Exception) -> object:
    async def processes() -> tuple[Candidate, ...]:
        raise error

    return processes


def not_a_repository() -> object:
    """A `git` that says a workspace belongs to no repository.

    The default here, so a name in this file is the workspace's own directory
    and the assertions do not depend on where the checkout running them lives.
    """

    async def ask(asked: Path) -> str | None:
        del asked
        return None

    return ask


def found(
    client: object | None,
    *candidates: Candidate,
    home: Path | None = None,
    daemon_note: str = "",
    git: object = None,
) -> object:
    return asyncio.run(
        discover(
            client,  # type: ignore[arg-type]
            processes=listing(*candidates),
            home=home,
            daemon_note=daemon_note,
            projects=ProjectNames(ask=git or not_a_repository()),  # type: ignore[arg-type]
        )
    )


class TestTheDaemonIsTheAuthorityWhenItIsUp:
    def test_a_loaded_thread_becomes_a_row(self) -> None:
        lane = found(FakeDaemon({THREAD: thread(THREAD, cwd="/tmp/w", name="a-thread")}))
        assert len(lane.rows) == 1
        row = lane.rows[0]
        assert row.target.session_id == THREAD
        assert row.workspace == Path("/tmp/w")
        assert str(row.name) == "w · a-thread"

    def test_rows_from_the_daemon_are_not_degraded(self) -> None:
        assert found(FakeDaemon({THREAD: thread(THREAD, cwd="/tmp/w")})).degraded is None

    def test_idle_is_idle_and_active_is_running(self) -> None:
        idle = found(FakeDaemon({THREAD: thread(THREAD, cwd="/tmp/w")})).rows[0]
        busy = found(FakeDaemon({THREAD: thread(THREAD, cwd="/tmp/w", status="active")})).rows[0]
        assert idle.state is SessionState.IDLE
        assert busy.state is SessionState.RUNNING

    def test_a_thread_whose_turn_errored_is_flagged_for_a_closer_look(self) -> None:
        row = found(FakeDaemon({THREAD: thread(THREAD, cwd="/tmp/w", status="systemError")})).rows[
            0
        ]
        assert row.state is SessionState.IDLE  # still reachable, still takes the next Relay
        assert row.waiting_for.kind is WaitingKind.UNKNOWN
        assert row.waiting_for.caught_up is False

    def test_a_status_word_this_build_has_not_seen_fails_closed(self) -> None:
        row = found(FakeDaemon({THREAD: thread(THREAD, cwd="/tmp/w", status="compacting")})).rows[0]
        assert row.state is SessionState.RUNNING


class TestJoiningAThreadToItsProcess:
    def test_the_tui_running_a_thread_is_found_by_its_workspace(self) -> None:
        lane = found(
            FakeDaemon({THREAD: thread(THREAD, cwd="/tmp/w")}),
            running(101, "/tmp/w"),
        )
        assert len(lane.rows) == 1
        assert lane.rows[0].target.pid == 101

    def test_a_claimed_process_does_not_also_get_a_row_of_its_own(self) -> None:
        lane = found(
            FakeDaemon({THREAD: thread(THREAD, cwd="/tmp/w")}),
            running(101, "/tmp/w"),
        )
        assert [row.target.pid for row in lane.rows] == [101]

    def test_two_tuis_in_one_directory_are_not_guessed_between(self) -> None:
        """A row addressed by the wrong pid is worse than one addressed by its id."""
        lane = found(
            FakeDaemon({THREAD: thread(THREAD, cwd="/tmp/w")}),
            running(101, "/tmp/w"),
            running(102, "/tmp/w"),
        )
        by_id = next(row for row in lane.rows if row.target.session_id == THREAD)
        assert by_id.target.pid is None
        # Both processes are still listed; neither was swallowed by the thread.
        assert sorted(row.target.pid or 0 for row in lane.rows) == [0, 101, 102]

    def test_a_thread_with_no_process_is_still_a_row(self) -> None:
        lane = found(FakeDaemon({THREAD: thread(THREAD, cwd="/tmp/w")}))
        assert lane.rows[0].target.pid is None


class TestWhenTheDaemonIsNotThere:
    def test_a_running_tui_is_still_listed(self) -> None:
        """#82: a TUI started while the daemon was down is never adopted later."""
        lane = found(None, running(101, "/tmp/w"))
        assert [row.target.pid for row in lane.rows] == [101]

    def test_those_rows_say_where_they_came_from(self) -> None:
        lane = found(None, running(101, "/tmp/w"))
        assert lane.degraded is not None
        assert lane.error is None  # the lane looked; it just looked with less

    def test_a_lane_with_no_client_says_so_rather_than_blaming_the_daemon(self) -> None:
        """#96: "did not answer" was said without a byte being sent.

        The wording moved when #76 built the client — this build does dial now,
        so "this build does not connect yet" stopped being true. It says only
        what this process can see, which is the reading the Advisor fixed on #96
        ("consequence for part B"). **#96's rule did not move**, and it is what
        is asserted here rather than any spelling: a lane with no client may not
        claim the daemon was silent, because being unable to reach it and it
        having nothing to say are two facts and only one of them was observed.
        The consequence is identical; the claim is not.
        """
        lane = found(None, running(101, "/tmp/w"))
        assert lane.degraded == discovery.NO_CLIENT
        assert "holds no connection" in lane.degraded
        assert "did not answer" not in lane.degraded
        # The sentence #96 was told not to write, held so it cannot come back.
        assert "does not connect" not in lane.degraded

    def test_a_dial_that_failed_says_why_once_and_in_its_own_words(self) -> None:
        """The dial's reason replaces the fallback rather than following it (#76).

        `SharedDaemon` sets a reason on every path that answers `None`, and that
        reason is always more precise than `NO_CLIENT`. Printed one after the
        other, a roster would make two claims about one failure — the shape #96
        is the record of.
        """
        lane = found(
            None, running(101, "/tmp/w"), daemon_note="codex could not be run: no such file"
        )

        assert lane.degraded is not None
        assert lane.degraded.startswith("codex could not be run: no such file")
        assert discovery.FROM_THE_MACHINE in lane.degraded
        assert "holds no connection" not in lane.degraded

    def test_a_daemon_that_refuses_is_still_a_daemon_that_did_not_answer(self) -> None:
        """The other sentence, and the only one that may claim the daemon was silent."""
        lane = found(
            FakeDaemon({}, raises=ConnectionRefusedError("no socket")),
            running(101, "/tmp/w"),
        )
        assert [row.target.pid for row in lane.rows] == [101]
        assert lane.degraded is not None
        assert lane.degraded.startswith(discovery.NO_DAEMON)
        assert "no socket" in lane.degraded  # the daemon's own words, not a summary

    def test_a_roster_shape_this_build_cannot_read_falls_back_rather_than_failing(self) -> None:
        class Odd(FakeDaemon):
            async def request(self, method: str, params: dict | None = None) -> dict:
                return {"data": "not a list"}

        lane = found(Odd({}), running(101, "/tmp/w"))
        assert [row.target.pid for row in lane.rows] == [101]
        assert lane.degraded is not None

    def test_a_daemon_that_answered_unreadably_is_not_reported_as_silent(self) -> None:
        """The third sentence. #96's part B, in the branch it was first missed in.

        This used to read "the daemon did not answer … (`thread/loaded/list`
        answered a shape this build cannot read)" — a sentence that contradicts
        itself inside one parenthesis, and whose first half is the half a reader
        carries away. The daemon answered; this build could not read it.
        """

        class Odd(FakeDaemon):
            async def request(self, method: str, params: dict | None = None) -> dict:
                return {"data": "not a list"}

        lane = found(Odd({}), running(101, "/tmp/w"))
        assert lane.degraded == discovery.UNREADABLE_ROSTER
        assert "did not answer" not in lane.degraded
        assert "cannot read" in lane.degraded

    def test_no_daemon_and_no_process_is_an_empty_answer_not_a_failure(self) -> None:
        """It has to be able to end rows: an empty machine is a real reading."""
        lane = found(None)
        assert lane.rows == ()
        assert lane.error is None


class TestASessionNobodyHasSpokenToYet:
    def test_it_is_addressed_by_its_pid_alone(self, tmp_path: Path) -> None:
        """Measured (#73): `codex` writes the rollout naming it at its first turn."""
        lane = found(None, running(101, "/tmp/w"), home=tmp_path)
        assert lane.rows[0].target.session_id is None
        assert lane.rows[0].target.pid == 101

    def test_once_it_has_a_rollout_the_row_carries_its_thread_id(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        write_rollout(tmp_path, THREAD, workspace)

        lane = found(None, running(101, workspace), home=tmp_path)
        assert lane.rows[0].target.session_id == THREAD
        assert lane.rows[0].target.pid == 101

    def test_it_holds_its_relay_rather_than_delivering_into_a_turn_it_cannot_see(self) -> None:
        """A process is not evidence of a Reply Window."""
        assert found(None, running(101, "/tmp/w")).rows[0].state is (SessionState.RUNNING)

    def test_it_does_not_inherit_the_thread_the_last_session_here_left_behind(
        self, tmp_path: Path
    ) -> None:
        """A workspace outlives the Sessions run in it, and a rollout stays on disk.

        The failure this closes: a fresh TUI in a directory somebody worked in
        yesterday takes yesterday's thread id, so the roster addresses an
        un-spoken-to Session as a conversation that is over — and `_better_known`
        then refuses to let a later, honest `None` correct it.
        """
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        rollout = write_rollout(tmp_path, THREAD, workspace)
        os.utime(rollout, (STARTED_AT - 3600, STARTED_AT - 3600))

        lane = found(None, running(101, workspace), home=tmp_path)
        assert lane.rows[0].target.session_id is None
        assert lane.rows[0].target.pid == 101

    def test_but_it_does_claim_the_rollout_it_wrote_itself(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        rollout = write_rollout(tmp_path, THREAD, workspace)
        os.utime(rollout, (STARTED_AT + 60, STARTED_AT + 60))

        lane = found(None, running(101, workspace), home=tmp_path)
        assert lane.rows[0].target.session_id == THREAD


class TestWhatEachRowIsCalled:
    """#78: `<project> · <title>`, and the title is whatever this lane can honestly say.

    The amended #67 port table (2026-08-25) dropped the route where a Session
    reported a title of its own (`legacy@1d32845:bridge/hook.py:215-253`,
    `bridge/daemon.py:1504-1544`), so every title here is composed from a fact
    the lane already holds and nothing is asked of the Session.
    """

    def test_a_thread_the_daemon_named_is_called_that(self) -> None:
        lane = found(FakeDaemon({THREAD: thread(THREAD, cwd="/tmp/w", name="port the log")}))
        assert str(lane.rows[0].name) == "w · port the log"

    def test_a_thread_the_daemon_did_not_name_is_called_by_its_short_id(self) -> None:
        """Eight characters of the thread id: short enough to say out loud."""
        lane = found(FakeDaemon({THREAD: thread(THREAD, cwd="/tmp/w")}))
        assert str(lane.rows[0].name) == f"w · {THREAD[:8]}"

    def test_a_row_read_off_the_process_table_is_named_the_same_way(self, tmp_path: Path) -> None:
        """The daemon is where a thread name comes from, so these rows take the id."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        write_rollout(tmp_path, THREAD, workspace)

        lane = found(None, running(101, workspace), home=tmp_path)
        assert str(lane.rows[0].name) == f"workspace · {THREAD[:8]}"

    def test_a_session_with_no_thread_id_yet_has_no_name_yet(self) -> None:
        """#73: `codex` writes the id at the first turn, and there is nothing else to use."""
        assert found(None, running(101, "/tmp/w")).rows[0].name is None

    def test_the_project_half_is_the_repository_when_the_workspace_is_in_one(self) -> None:
        async def inside_a_repository(asked: Path) -> str | None:
            del asked
            return "/src/GPT-VoiceCoding/.git\n"

        lane = found(
            FakeDaemon({THREAD: thread(THREAD, cwd="/tmp/w", name="port the log")}),
            git=inside_a_repository,
        )
        assert str(lane.rows[0].name) == "GPT-VoiceCoding · port the log"

    def test_a_thread_named_with_the_separator_is_left_unnamed(self) -> None:
        """A name with a `·` in it cannot be read back as two halves, so it is not one."""
        lane = found(FakeDaemon({THREAD: thread(THREAD, cwd="/tmp/w", name="a · b")}))
        assert lane.rows[0].name is None


class TestWhenTheLaneCannotLookAtAll:
    def test_a_process_table_that_cannot_be_read_is_a_lane_error(self) -> None:
        lane = asyncio.run(
            discover(None, processes=refusing_processes(OSError("no ps")))  # type: ignore[arg-type]
        )
        assert lane.rows == ()
        assert lane.error is not None

    def test_and_it_carries_no_rows_so_core_leaves_the_roster_alone(self) -> None:
        lane = asyncio.run(
            discover(None, processes=refusing_processes(TimeoutError()))  # type: ignore[arg-type]
        )
        assert lane.rows == ()
        assert not lane.enumerated


#: The two documents 0.150.0's title generation produced in one workspace, as
#: **the daemon described them** — recorded on #79 from #110's live probe
#: (2026-08-27, codex-cli 0.150.0 over the 0.149.1 managed daemon, read through
#: this module's own `discover`). Not invented, and not a shape read off the
#: Rust source: this is what came back over the wire that day.
#:
#: The phantom is verbatim from that record. The Session beside it carries the
#: fields the same record states — its id, the name the daemon composed from the
#: raw first characters of the user's message, its `thread_source: "user"` — plus
#: the two every `Thread` answer carries (`ephemeral` is a plain `bool`, and a
#: status is what makes a row): `rust-v0.149.1:codex-rs/app-server-protocol/src/
#: protocol/v2/thread_data.rs:196-266`. The record elides the workspace's parent
#: directory; the leaf is its own.
PROBE_WORKSPACE = "/tmp/gvc-110-probe.c45yj3u_"

RECORDED_SESSION = {
    "id": "01a0403a-0b5d-7242-af18-ca696a6af2fb",
    "name": "Reply with the single word READY. Do",
    "cwd": PROBE_WORKSPACE,
    "status": {"type": "idle"},
    "ephemeral": False,
    "threadSource": "user",
    "cliVersion": "0.149.1",
}

RECORDED_PHANTOM = {
    "id": "01a0403a-18ea-7d51-8e37-462951604d59",
    "ephemeral": True,
    "threadSource": "system",
    "name": None,
    "path": None,
    "parentThreadId": None,
    "preview": "",
    "status": {"type": "idle"},
    "cwd": PROBE_WORKSPACE,
    "cliVersion": "0.149.1",
    "source": "vscode",
}


def daemon_holding(*threads: dict) -> FakeDaemon:
    """A daemon whose roster is these documents, in this order."""
    return FakeDaemon({str(held["id"]): held for held in threads})


def sourced(thread_id: str, source: str | None) -> dict:
    """One thread the daemon holds, described only by where it came from."""
    described = thread(thread_id, cwd="/tmp/w")
    if source is not None:
        described["threadSource"] = source
    return described


class TestThreadsTheDaemonRunsForItself:
    """#112: a thread codex started for its own errand is not a Session.

    **Measured, not deduced.** One hand-started `codex` 0.150.0 put *two* rows
    in one workspace during its first turn (#79's measurement, above): the
    user's Session, and an ephemeral thread the TUI starts to generate a title
    for it — `rust-v0.150.0:codex-rs/tui/src/temporary_structured_request.rs:
    103-104`, reached from `tui/src/app/thread_title.rs:57`, which starts it with
    exactly `ephemeral: true` and `thread_source: Feature("system")`. The product
    listed it as a Session, gave it #78's id-prefix name, and would have let a
    voice command resolve to it.

    **Why a keep-list and not a block-list.** `ThreadSource` is a string with an
    open tail, not a closed enum: `FromStr`'s last arm turns *any* unrecognised
    word into `Feature(word)` (`rust-v0.150.0:codex-rs/protocol/src/protocol.rs:
    2604-2657`), so blocking `"system"` by name would leave the next feature
    string to be discovered the same way this one was.

    **Legacy has no rule of this kind** — the word `ephemeral` does not appear
    in it. Its Codex roster was the rollout index
    (`legacy@1d32845:bridge/codex.py:1063-1129`) and an ephemeral thread writes
    no rollout, so this phantom could not reach it. What is **adapted** is the
    technique: reading a thread's own `thread_source` to decide what it is, as
    `realtime_thread_ids` does (`:1020-1026`). Legacy's nearest *rule* —
    `thread_source == "subagent"` refusing registration
    (`legacy@1d32845:bridge/__main__.py:893-898`) — is #79's, not this one, and
    it excludes the very value this keeps.
    """

    def test_the_recorded_phantom_is_dropped_and_the_recorded_session_is_kept(self) -> None:
        """The whole ticket, on the two documents the daemon really answered."""
        lane = found(daemon_holding(RECORDED_SESSION, RECORDED_PHANTOM))
        assert [row.target.session_id for row in lane.rows] == [RECORDED_SESSION["id"]]

    def test_the_phantom_never_reaches_the_naming_rule(self) -> None:
        """It was named `<project> · 01a0403a`, and that name is what made it a Session."""
        lane = found(daemon_holding(RECORDED_SESSION, RECORDED_PHANTOM))
        assert [str(row.name) for row in lane.rows] == [
            "gvc-110-probe.c45yj3u_ · Reply with the single word READY. Do"
        ]

    def test_the_session_keeps_its_process_even_when_the_phantom_is_listed_first(self) -> None:
        """Both threads name the same `cwd`, and the pid join is first-come.

        On the recorded run the Session happened to be listed first and took the
        pid. Nothing promised that order: listed the other way round, the
        phantom claims the workspace's only TUI and the real Session goes
        pidless — a Session addressable by id alone, because of a thread that is
        not one. Dropping it before the join is what makes the order stop
        mattering.
        """
        lane = found(
            daemon_holding(RECORDED_PHANTOM, RECORDED_SESSION), running(43261, PROBE_WORKSPACE)
        )
        assert [(row.target.session_id, row.target.pid) for row in lane.rows] == [
            (RECORDED_SESSION["id"], 43261)
        ]

    def test_an_ephemeral_thread_is_dropped_whatever_it_says_it_came_from(self) -> None:
        """`ephemeral` means the daemon will not even write it to disk."""
        lane = found(daemon_holding(dict(RECORDED_SESSION, ephemeral=True)))
        assert lane.rows == ()

    def test_a_thread_source_this_build_does_not_know_is_dropped(self) -> None:
        """The open tail: a feature name nobody has read yet is still not a Session."""
        lane = found(daemon_holding(sourced(THREAD, "compaction")))
        assert lane.rows == ()

    def test_memory_consolidation_is_the_daemons_own_errand(self) -> None:
        lane = found(daemon_holding(sourced(THREAD, "memory_consolidation")))
        assert lane.rows == ()

    def test_a_subagent_thread_is_kept_for_the_child_process_rule(self) -> None:
        """#79 classifies it; it cannot classify a row this module deleted."""
        lane = found(daemon_holding(sourced(THREAD, "subagent")))
        assert [row.target.session_id for row in lane.rows] == [THREAD]

    def test_a_guardian_review_thread_is_kept_for_the_same_reason(self) -> None:
        """One delegate class split by a boolean: `codex-rs/core/src/codex_delegate.rs:111`."""
        lane = found(daemon_holding(sourced(THREAD, "guardian_review")))
        assert [row.target.session_id for row in lane.rows] == [THREAD]

    def test_a_daemon_too_old_to_say_changes_nothing(self) -> None:
        """Absent is not "not user". A 0.130-era daemon names no source at all."""
        lane = found(daemon_holding(sourced(THREAD, None)))
        assert [row.target.session_id for row in lane.rows] == [THREAD]

    def test_a_thread_the_daemon_declined_to_classify_is_kept_too(self) -> None:
        """`null` is how a 0.149.1 daemon spells the same absence.

        `thread_source` is an `Option` that is always serialised, so "no source
        recorded" reaches this build as `"threadSource": null` — the ordinary
        state of every thread codex started without classifying, and of every
        older thread a current daemon loads. Read as a *value* rather than as an
        absence, it would empty the roster of all of them.
        """
        lane = found(daemon_holding(dict(sourced(THREAD, None), threadSource=None)))
        assert [row.target.session_id for row in lane.rows] == [THREAD]

    def test_a_source_field_of_a_shape_this_build_cannot_read_is_kept(self) -> None:
        """Fails open, like every other unreadable field here: a roster lists."""
        lane = found(daemon_holding(dict(sourced(THREAD, None), threadSource=7)))
        assert [row.target.session_id for row in lane.rows] == [THREAD]

    def test_the_keep_list_is_the_vocabulary_79_shares(self) -> None:
        """Named once, here, so the two rules cannot drift apart (#79's coordination)."""
        assert discovery.SESSION_THREAD_SOURCES == frozenset(
            {"user", "subagent", "guardian_review"}
        )


class TestSayingSoWithoutSayingItTwelveTimesAMinute:
    """A dropped row is a row somebody may come looking for, so the log keeps it.

    Once per thread id, not once per tick. The filter runs on the five-second
    discovery cadence and a phantom outlives the TUI that made it — #79's
    `thread/read` for this one succeeded *after* the probe had stopped the TUI's
    process group — so a line per pass would be twelve lines a minute, for as
    long as the daemon holds the thread, saying the same thing.
    """

    def test_the_phantom_is_never_read_for_its_turns(self) -> None:
        """The expensive read (558,875 bytes, #76) is not spent on a non-Session.

        Dropping it after the row was built would still cost this, every tick,
        for a row nobody ever sees. Dropping it in `_threads` is what makes the
        saving real.
        """

        class Counting(FakeDaemon):
            def __init__(self, threads: dict[str, dict]) -> None:
                super().__init__(threads)
                self.deep_reads: list[str] = []

            async def request(self, method: str, params: dict | None = None) -> dict:
                if method == "thread/read" and (params or {}).get("includeTurns"):
                    self.deep_reads.append(str((params or {}).get("threadId")))
                return await super().request(method, params)

        daemon = Counting({str(held["id"]): held for held in (RECORDED_SESSION, RECORDED_PHANTOM)})
        asyncio.run(
            discover(
                daemon,  # type: ignore[arg-type]
                processes=listing(),
                turns=discovery.TurnCache(),
                projects=ProjectNames(ask=not_a_repository()),  # type: ignore[arg-type]
            )
        )
        assert daemon.deep_reads == [str(RECORDED_SESSION["id"])]

    def test_it_is_said_once_across_ticks_not_once_per_tick(self, caplog) -> None:
        daemon = daemon_holding(RECORDED_SESSION, RECORDED_PHANTOM)
        skipped: set[str] = set()

        with caplog.at_level(logging.INFO, logger=discovery.__name__):
            for _ in range(3):
                asyncio.run(
                    discover(
                        daemon,  # type: ignore[arg-type]
                        processes=listing(),
                        reported_non_sessions=skipped,
                        projects=ProjectNames(ask=not_a_repository()),  # type: ignore[arg-type]
                    )
                )

        said = [line for line in caplog.messages if str(RECORDED_PHANTOM["id"]) in line]
        assert len(said) == 1
        # Why it was dropped, not just that it was: `ephemeral` is the first of
        # the phantom's two disqualifications, and the decisive one.
        assert "ephemeral" in said[0]

    def test_a_thread_dropped_for_its_source_alone_says_which_source(self, caplog) -> None:
        with caplog.at_level(logging.INFO, logger=discovery.__name__):
            found(daemon_holding(sourced(THREAD, "compaction")))
        assert any("compaction" in line for line in caplog.messages)

    def test_a_thread_the_daemon_has_let_go_is_forgotten(self) -> None:
        """Roster-sized, like `TurnCache.retain`: this may not grow all day."""
        skipped = {"a-thread-from-yesterday"}
        asyncio.run(
            discover(
                daemon_holding(RECORDED_SESSION, RECORDED_PHANTOM),  # type: ignore[arg-type]
                processes=listing(),
                reported_non_sessions=skipped,
                projects=ProjectNames(ask=not_a_repository()),  # type: ignore[arg-type]
            )
        )
        assert skipped == {str(RECORDED_PHANTOM["id"])}

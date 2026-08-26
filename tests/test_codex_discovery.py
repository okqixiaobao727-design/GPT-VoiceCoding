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
from pathlib import Path

from gpt_voicecoding.adapters.agent.codex.discovery import discover
from gpt_voicecoding.adapters.agent.codex.processes import Candidate
from gpt_voicecoding.seams.agent import SessionState, WaitingKind

THREAD = "01a03b06-f995-7b60-bc9f-e2152ee4ed32"
OTHER_THREAD = "01a0385e-4872-7353-bdc5-8966c6165a8e"


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


def listing(*candidates: Candidate) -> object:
    async def processes() -> tuple[Candidate, ...]:
        return candidates

    return processes


def refusing_processes(error: Exception) -> object:
    async def processes() -> tuple[Candidate, ...]:
        raise error

    return processes


def found(client: object | None, *candidates: Candidate, home: Path | None = None) -> object:
    return asyncio.run(
        discover(client, processes=listing(*candidates), home=home)  # type: ignore[arg-type]
    )


class TestTheDaemonIsTheAuthorityWhenItIsUp:
    def test_a_loaded_thread_becomes_a_row(self) -> None:
        lane = found(FakeDaemon({THREAD: thread(THREAD, cwd="/tmp/w", name="a-thread")}))
        assert len(lane.rows) == 1
        row = lane.rows[0]
        assert row.target.session_id == THREAD
        assert row.workspace == Path("/tmp/w")
        assert row.name == "a-thread"

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
            Candidate(pid=101, workspace=Path("/tmp/w")),
        )
        assert len(lane.rows) == 1
        assert lane.rows[0].target.pid == 101

    def test_a_claimed_process_does_not_also_get_a_row_of_its_own(self) -> None:
        lane = found(
            FakeDaemon({THREAD: thread(THREAD, cwd="/tmp/w")}),
            Candidate(pid=101, workspace=Path("/tmp/w")),
        )
        assert [row.target.pid for row in lane.rows] == [101]

    def test_two_tuis_in_one_directory_are_not_guessed_between(self) -> None:
        """A row addressed by the wrong pid is worse than one addressed by its id."""
        lane = found(
            FakeDaemon({THREAD: thread(THREAD, cwd="/tmp/w")}),
            Candidate(pid=101, workspace=Path("/tmp/w")),
            Candidate(pid=102, workspace=Path("/tmp/w")),
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
        lane = found(None, Candidate(pid=101, workspace=Path("/tmp/w")))
        assert [row.target.pid for row in lane.rows] == [101]

    def test_those_rows_say_where_they_came_from(self) -> None:
        lane = found(None, Candidate(pid=101, workspace=Path("/tmp/w")))
        assert lane.degraded is not None
        assert lane.error is None  # the lane looked; it just looked with less

    def test_a_daemon_that_refuses_is_the_same_fact_as_one_that_is_absent(self) -> None:
        lane = found(
            FakeDaemon({}, raises=ConnectionRefusedError("no socket")),
            Candidate(pid=101, workspace=Path("/tmp/w")),
        )
        assert [row.target.pid for row in lane.rows] == [101]
        assert lane.degraded is not None

    def test_a_roster_shape_this_build_cannot_read_falls_back_rather_than_failing(self) -> None:
        class Odd(FakeDaemon):
            async def request(self, method: str, params: dict | None = None) -> dict:
                return {"data": "not a list"}

        lane = found(Odd({}), Candidate(pid=101, workspace=Path("/tmp/w")))
        assert [row.target.pid for row in lane.rows] == [101]
        assert lane.degraded is not None

    def test_no_daemon_and_no_process_is_an_empty_answer_not_a_failure(self) -> None:
        """It has to be able to end rows: an empty machine is a real reading."""
        lane = found(None)
        assert lane.rows == ()
        assert lane.error is None


class TestASessionNobodyHasSpokenToYet:
    def test_it_is_addressed_by_its_pid_alone(self, tmp_path: Path) -> None:
        """Measured (#73): `codex` writes the rollout naming it at its first turn."""
        lane = found(None, Candidate(pid=101, workspace=Path("/tmp/w")), home=tmp_path)
        assert lane.rows[0].target.session_id is None
        assert lane.rows[0].target.pid == 101

    def test_once_it_has_a_rollout_the_row_carries_its_thread_id(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        directory = tmp_path / "sessions"
        directory.mkdir()
        (directory / f"rollout-2026-08-26T10-25-08-{THREAD}.jsonl").write_text(
            json.dumps(
                {
                    "type": "session_meta",
                    "payload": {"session_id": THREAD, "cwd": str(workspace)},
                }
            )
            + "\n",
            encoding="utf-8",
        )

        lane = found(None, Candidate(pid=101, workspace=workspace), home=tmp_path)
        assert lane.rows[0].target.session_id == THREAD
        assert lane.rows[0].target.pid == 101

    def test_it_holds_its_relay_rather_than_delivering_into_a_turn_it_cannot_see(self) -> None:
        """A process is not evidence of a Reply Window."""
        assert found(None, Candidate(pid=101, workspace=Path("/tmp/w"))).rows[0].state is (
            SessionState.RUNNING
        )


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

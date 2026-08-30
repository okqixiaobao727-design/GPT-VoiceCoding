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
import tempfile
from pathlib import Path

from gpt_voicecoding.adapters.agent._project import ProjectNames
from gpt_voicecoding.adapters.agent.codex import discovery, rollouts
from gpt_voicecoding.adapters.agent.codex.discovery import discover
from gpt_voicecoding.adapters.agent.codex.processes import Candidate
from gpt_voicecoding.seams.agent import (
    ChildClassification,
    ChildKind,
    SessionState,
    WaitingKind,
)

THREAD = "01a03b06-f995-7b60-bc9f-e2152ee4ed32"
OTHER_THREAD = "01a0385e-4872-7353-bdc5-8966c6165a8e"

#: When the processes in this file started. Rollouts written before it belong to
#: a Session that is over; ones written after it belong to the process reading.
STARTED_AT = 1_787_700_000.0


def running(
    pid: int,
    workspace: Path | str,
    *,
    session_id: str | None = None,
) -> Candidate:
    """One TUI in the process table, with an exact native id only when observed."""
    return Candidate(
        pid=pid,
        workspace=Path(workspace),
        session_id=session_id,
    )


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


def thread(
    thread_id: str,
    *,
    cwd: str,
    status: str = "idle",
    name: str | None = None,
    preview: str | None = None,
) -> dict:
    """One thread as the daemon describes it, with `preview` said only when asked.

    Omitted rather than empty by default, because the two are different claims
    to `_thread_name` (#113) and most of this file is about neither.
    """
    described = {
        "id": thread_id,
        "cwd": cwd,
        "status": {"type": status},
        "name": name,
        "threadSource": "user",
        "sessionId": thread_id,
    }
    if preview is not None:
        described["preview"] = preview
    return described


def write_rollout(
    home: Path, thread_id: str, workspace: Path, *, source: str | None = None
) -> Path:
    """One rollout on disk, in the 0.149.1 shape, written now unless moved.

    `source` writes `session_meta.thread_source` — P13's child evidence, and the
    only thing on disk that says whether a rollout is a person's Session or a
    thread one spawned. Omitted by default, because a 0.130-era rollout carries
    no such field and every test that does not care about it should read like
    one.
    """
    directory = home / "sessions"
    directory.mkdir(exist_ok=True)
    payload: dict[str, object] = {"session_id": thread_id, "cwd": str(workspace)}
    if source is not None:
        payload["thread_source"] = source
    path = directory / f"rollout-2026-08-26T10-25-08-{thread_id}.jsonl"
    path.write_text(
        json.dumps({"type": "session_meta", "payload": payload}) + "\n",
        encoding="utf-8",
    )
    return path


def write_live_user_rollout(
    home: Path,
    thread_id: str,
    workspace: Path | str,
    *,
    written_at: float = STARTED_AT + 60,
) -> Path:
    """An explicit user rollout carrying the fixture process's exact native id."""
    path = write_rollout(home, thread_id, Path(workspace), source=rollouts.USER_THREAD_SOURCE)
    os.utime(path, (written_at, written_at))
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
            evidence=discovery.ProcessEvidence(
                list_sessions=listing(*candidates),  # type: ignore[arg-type]
                home=home,
            ),
            daemon_note=daemon_note,
            projects=ProjectNames(ask=git or not_a_repository()),  # type: ignore[arg-type]
        )
    )


def found_with_tuis(client: FakeDaemon, *, git: object = None) -> object:
    """Discover daemon roots with exact process and native-rollout identity."""
    workspaces = {
        Path(str(described["cwd"]))
        for described in client.threads.values()
        if isinstance(described.get("cwd"), str) and str(described["cwd"]).strip()
    }
    with tempfile.TemporaryDirectory(prefix="gvc-codex-discovery-") as directory:
        home = Path(directory)
        written_workspaces: set[Path] = set()
        candidates: list[Candidate] = []
        for described in client.threads.values():
            source = described.get("threadSource")
            workspace = Path(str(described.get("cwd", "")))
            if (
                described.get("ephemeral") is True
                or source in discovery.CHILD_THREAD_SOURCES
                or (isinstance(source, str) and source not in discovery.SESSION_THREAD_SOURCES)
                or workspace not in workspaces
                or workspace in written_workspaces
            ):
                continue
            thread_id = str(described["id"])
            write_live_user_rollout(home, thread_id, workspace)
            candidates.append(running(101 + len(candidates), workspace, session_id=thread_id))
            written_workspaces.add(workspace)
        return found(client, *candidates, home=home, git=git)


class TestTheDaemonIsTheAuthorityWhenItIsUp:
    def test_a_loaded_thread_becomes_a_row(self) -> None:
        lane = found_with_tuis(FakeDaemon({THREAD: thread(THREAD, cwd="/tmp/w", name="a-thread")}))
        assert len(lane.rows) == 1
        row = lane.rows[0]
        assert row.target.session_id == THREAD
        assert row.workspace == Path("/tmp/w")
        assert str(row.name) == "w · a-thread"

    def test_rows_from_the_daemon_are_not_degraded(self) -> None:
        assert found_with_tuis(FakeDaemon({THREAD: thread(THREAD, cwd="/tmp/w")})).degraded is None

    def test_idle_is_idle_and_active_is_running(self) -> None:
        idle = found_with_tuis(FakeDaemon({THREAD: thread(THREAD, cwd="/tmp/w")})).rows[0]
        busy = found_with_tuis(
            FakeDaemon({THREAD: thread(THREAD, cwd="/tmp/w", status="active")})
        ).rows[0]
        assert idle.state is SessionState.IDLE
        assert busy.state is SessionState.RUNNING

    def test_a_thread_whose_turn_errored_is_flagged_for_a_closer_look(self) -> None:
        row = found_with_tuis(
            FakeDaemon({THREAD: thread(THREAD, cwd="/tmp/w", status="systemError")})
        ).rows[0]
        assert row.state is SessionState.IDLE  # still reachable, still takes the next Relay
        assert row.waiting_for.kind is WaitingKind.UNKNOWN
        assert row.waiting_for.caught_up is False

    def test_a_status_word_this_build_has_not_seen_fails_closed(self) -> None:
        row = found_with_tuis(
            FakeDaemon({THREAD: thread(THREAD, cwd="/tmp/w", status="compacting")})
        ).rows[0]
        assert row.state is SessionState.RUNNING


class TestJoiningAThreadToItsProcess:
    def test_live_tui_without_a_shared_id_does_not_impersonate_an_exited_root(
        self, tmp_path: Path
    ) -> None:
        """A historical rollout plus an unidentified live TUI is not a 1:1 identity join."""
        write_live_user_rollout(tmp_path, THREAD, "/tmp/w")

        lane = found(
            FakeDaemon({THREAD: thread(THREAD, cwd="/tmp/w")}),
            running(101, "/tmp/w"),
            home=tmp_path,
        )

        assert lane.rows == ()

    def test_the_tui_running_a_thread_is_found_by_its_exact_rollout(self, tmp_path: Path) -> None:
        write_live_user_rollout(tmp_path, THREAD, "/tmp/w")
        unclassified = thread(THREAD, cwd="/tmp/w")
        unclassified.pop("threadSource")
        lane = found(
            FakeDaemon({THREAD: unclassified}),
            running(101, "/tmp/w", session_id=THREAD),
            home=tmp_path,
        )
        assert len(lane.rows) == 1
        assert lane.rows[0].target.pid == 101

    def test_exact_daemon_and_process_evidence_produce_one_row(self, tmp_path: Path) -> None:
        write_live_user_rollout(tmp_path, THREAD, "/tmp/w")
        lane = found(
            FakeDaemon({THREAD: thread(THREAD, cwd="/tmp/w")}),
            running(101, "/tmp/w", session_id=THREAD),
            home=tmp_path,
        )
        assert [row.target.pid for row in lane.rows] == [101]

    def test_two_live_roots_in_one_workspace_remain_two_logical_sessions(
        self, tmp_path: Path
    ) -> None:
        """Each exact argv id joins its own native root without workspace pairing."""
        write_live_user_rollout(tmp_path, THREAD, "/tmp/w", written_at=STARTED_AT + 60)
        write_live_user_rollout(tmp_path, OTHER_THREAD, "/tmp/w", written_at=STARTED_AT + 120)
        roots = {
            THREAD: dict(thread(THREAD, cwd="/tmp/w"), threadSource="user", sessionId=THREAD),
            OTHER_THREAD: dict(
                thread(OTHER_THREAD, cwd="/tmp/w"),
                threadSource="user",
                sessionId=OTHER_THREAD,
            ),
        }

        lane = found(
            FakeDaemon(roots),
            running(101, "/tmp/w", session_id=THREAD),
            running(102, "/tmp/w", session_id=OTHER_THREAD),
            home=tmp_path,
        )

        assert {(row.target.session_id, row.target.pid) for row in lane.rows} == {
            (THREAD, 101),
            (OTHER_THREAD, 102),
        }

    def test_multiple_post_start_roots_fail_closed_to_the_live_process(
        self, tmp_path: Path
    ) -> None:
        """One TUI cannot prove which of two roots is current, even in one workspace."""
        write_live_user_rollout(tmp_path, THREAD, "/tmp/w", written_at=STARTED_AT + 60)
        write_live_user_rollout(tmp_path, OTHER_THREAD, "/tmp/w", written_at=STARTED_AT + 120)
        roots = {
            THREAD: dict(thread(THREAD, cwd="/tmp/w"), threadSource="user", sessionId=THREAD),
            OTHER_THREAD: dict(
                thread(OTHER_THREAD, cwd="/tmp/w"),
                threadSource="user",
                sessionId=OTHER_THREAD,
            ),
        }

        lane = found(FakeDaemon(roots), running(101, "/tmp/w"), home=tmp_path)

        assert lane.rows == ()

    def test_unclassified_daemon_and_rollout_evidence_never_confirm_a_root(
        self, tmp_path: Path
    ) -> None:
        """TTY proves a live TUI; silence about native identity proves no thread id."""
        rollout = write_rollout(tmp_path, THREAD, Path("/tmp/w"))
        os.utime(rollout, (STARTED_AT + 60, STARTED_AT + 60))
        unclassified = thread(THREAD, cwd="/tmp/w")
        unclassified.pop("threadSource")

        lane = found(
            FakeDaemon({THREAD: unclassified}),
            running(101, "/tmp/w", session_id=THREAD),
            home=tmp_path,
        )

        assert lane.rows == ()

    def test_one_root_and_two_unidentified_live_tuis_fail_closed(self, tmp_path: Path) -> None:
        """TTY liveness without a shared native key is not a confirmed main Session."""
        write_live_user_rollout(tmp_path, THREAD, "/tmp/w")
        lane = found(
            FakeDaemon({THREAD: thread(THREAD, cwd="/tmp/w")}),
            running(101, "/tmp/w"),
            running(102, "/tmp/w"),
            home=tmp_path,
        )
        assert lane.rows == ()

    def test_a_thread_with_no_process_is_not_a_session(self) -> None:
        lane = found(FakeDaemon({THREAD: thread(THREAD, cwd="/tmp/w")}))
        assert lane.rows == ()

    def test_a_resumed_thread_becomes_a_session_again(self, tmp_path: Path) -> None:
        daemon = FakeDaemon({THREAD: thread(THREAD, cwd="/tmp/w")})
        write_live_user_rollout(tmp_path, THREAD, "/tmp/w")

        assert found(daemon).rows == ()
        assert [
            row.target.session_id
            for row in found(
                daemon,
                running(101, "/tmp/w", session_id=THREAD),
                home=tmp_path,
            ).rows
        ] == [THREAD]


class TestWhenTheDaemonIsNotThere:
    def test_an_unidentified_running_tui_is_not_a_confirmed_session(self) -> None:
        """TTY liveness without a shared native identity stays outside the roster."""
        lane = found(None, running(101, "/tmp/w"))
        assert lane.rows == ()

    def test_the_lane_still_reports_why_daemon_evidence_is_absent(self) -> None:
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
        assert lane.rows == ()
        assert lane.degraded is not None
        assert lane.degraded.startswith(discovery.NO_DAEMON)
        assert "no socket" in lane.degraded  # the daemon's own words, not a summary

    def test_a_roster_shape_this_build_cannot_read_falls_back_rather_than_failing(self) -> None:
        class Odd(FakeDaemon):
            async def request(self, method: str, params: dict | None = None) -> dict:
                return {"data": "not a list"}

        lane = found(Odd({}), running(101, "/tmp/w"))
        assert lane.rows == ()
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
    def test_it_is_not_a_confirmed_session_without_a_shared_native_id(self, tmp_path: Path) -> None:
        """A PID and workspace are liveness evidence, not native root identity."""
        lane = found(None, running(101, "/tmp/w"), home=tmp_path)
        assert lane.rows == ()

    def test_once_it_has_a_rollout_the_row_carries_its_thread_id(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        write_live_user_rollout(tmp_path, THREAD, workspace)

        lane = found(None, running(101, workspace, session_id=THREAD), home=tmp_path)
        assert lane.rows[0].target.session_id == THREAD
        assert lane.rows[0].target.pid == 101

    def test_it_holds_its_relay_rather_than_delivering_into_a_turn_it_cannot_see(
        self, tmp_path: Path
    ) -> None:
        """A process is not evidence of a Reply Window."""
        write_live_user_rollout(tmp_path, THREAD, "/tmp/w")
        lane = found(
            None,
            running(101, "/tmp/w", session_id=THREAD),
            home=tmp_path,
        )
        assert lane.rows[0].state is SessionState.RUNNING

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
        assert lane.rows == ()

    def test_but_it_does_claim_the_rollout_it_wrote_itself(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        write_live_user_rollout(tmp_path, THREAD, workspace)

        lane = found(None, running(101, workspace, session_id=THREAD), home=tmp_path)
        assert lane.rows[0].target.session_id == THREAD

    def test_it_does_not_take_the_thread_id_of_a_child_it_spawned(self, tmp_path: Path) -> None:
        """#79, and the reason P13 reads `thread_source` at all.

        A subagent runs in the parent's own workspace and writes its rollout
        there, *after* the TUI started — so it is newer than the TUI's own and
        wins a join made on `cwd` and mtime alone. The user's Session would then
        be addressed by its child's thread id: a Relay aimed at the parent,
        carried into the child, under the user's authority. That is the failure
        the Child Process rule exists to prevent, arriving through the back door
        of identity rather than through the roster.
        """
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        mine = write_rollout(tmp_path, THREAD, workspace, source="user")
        os.utime(mine, (STARTED_AT + 60, STARTED_AT + 60))
        spawned = write_rollout(tmp_path, OTHER_THREAD, workspace, source="subagent")
        os.utime(spawned, (STARTED_AT + 120, STARTED_AT + 120))

        lane = found(None, running(101, workspace, session_id=THREAD), home=tmp_path)
        assert lane.rows[0].target.session_id == THREAD

    def test_a_rollout_that_cannot_classify_its_root_is_not_a_session(self, tmp_path: Path) -> None:
        """An older rollout remains useful history, but is not positive root identity."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        rollout = write_rollout(tmp_path, THREAD, workspace)
        os.utime(rollout, (STARTED_AT + 60, STARTED_AT + 60))

        lane = found(
            None,
            running(101, workspace, session_id=THREAD),
            home=tmp_path,
        )
        assert lane.rows == ()


class TestWhatEachRowIsCalled:
    """#78: `<project> · <title>`, and the title is whatever this lane can honestly say.

    The amended #67 port table (2026-08-25) dropped the route where a Session
    reported a title of its own (`legacy@1d32845:bridge/hook.py:215-253`,
    `bridge/daemon.py:1504-1544`), so every title here is composed from a fact
    the lane already holds and nothing is asked of the Session.
    """

    def test_a_thread_the_daemon_named_is_called_that(self) -> None:
        lane = found_with_tuis(
            FakeDaemon({THREAD: thread(THREAD, cwd="/tmp/w", name="port the log")})
        )
        assert str(lane.rows[0].name) == "w · port the log"

    def test_a_thread_the_daemon_did_not_name_is_called_by_its_short_id(self) -> None:
        """Eight characters of the thread id: short enough to say out loud."""
        lane = found_with_tuis(FakeDaemon({THREAD: thread(THREAD, cwd="/tmp/w")}))
        assert str(lane.rows[0].name) == f"w · {THREAD[:8]}"

    def test_a_row_read_off_the_process_table_is_named_the_same_way(self, tmp_path: Path) -> None:
        """The daemon is where a thread name comes from, so these rows take the id."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        write_live_user_rollout(tmp_path, THREAD, workspace)

        lane = found(None, running(101, workspace, session_id=THREAD), home=tmp_path)
        assert str(lane.rows[0].name) == f"workspace · {THREAD[:8]}"

    def test_a_tui_with_no_thread_id_has_no_roster_name(self) -> None:
        """No identity means no Session row for the naming rule to decorate."""
        assert found(None, running(101, "/tmp/w")).rows == ()

    def test_the_project_half_is_the_repository_when_the_workspace_is_in_one(self) -> None:
        async def inside_a_repository(asked: Path) -> str | None:
            del asked
            return "/src/GPT-VoiceCoding/.git\n"

        lane = found_with_tuis(
            FakeDaemon({THREAD: thread(THREAD, cwd="/tmp/w", name="port the log")}),
            git=inside_a_repository,
        )
        assert str(lane.rows[0].name) == "GPT-VoiceCoding · port the log"

    def test_a_thread_named_with_the_separator_is_left_unnamed(self) -> None:
        """A name with a `·` in it cannot be read back as two halves, so it is not one."""
        lane = found_with_tuis(FakeDaemon({THREAD: thread(THREAD, cwd="/tmp/w", name="a · b")}))
        assert lane.rows[0].name is None


#: The thread the acceptance run of record drove, **as the shared daemon
#: describes it** — read back verbatim over `thread/read` on 2026-08-27 against
#: the running daemon (cli 0.150.0 / app-server 0.149.1), turns dropped and
#: nothing else touched. `01a040ee…` is the Session of run `20260827T015022Z`
#: (its `verdict.json` names the same id and the same rollout).
#:
#: **`name` here is the title the daemon settled on, and it is the second one.**
#: #79 recorded the product frozen on `workspace-codex · Reply with the single
#: word READY. Do` for this exact thread — the raw first characters — and this
#: readback of the same thread says `回复 READY`. So the rename #107 predicted
#: does happen; what could not see it was #78's freeze, which is why the roster
#: never showed it.
SETTLED_SESSION = {
    "id": "01a040ee-e08e-7e83-9a53-bac0531157f6",
    "sessionId": "01a040ee-e08e-7e83-9a53-bac0531157f6",
    "preview": "Reply with the single word READY. Do not use any tools, and do not ask anything.",
    "ephemeral": False,
    "historyMode": "paginated",
    "status": {"type": "idle"},
    "cwd": "/Users/simon/Library/Application Support/GPT-VoiceCoding/"
    "acceptance/20260827T015022Z/workspace-codex",
    "cliVersion": "0.149.1",
    "source": "vscode",
    "threadSource": "user",
    "name": "回复 READY",
}

#: The same thread while its **provisional** title was live: the document above
#: with the name #79 recorded the product freezing. Derived rather than captured,
#: and it is honest to derive it — `preview` is written once, from the first user
#: message, and later messages never overwrite it (`rust-v0.150.0:codex-rs/
#: thread-store/src/thread_metadata_sync.rs:316-324`), so the `preview` read back
#: today is the `preview` that stood beside that name.
PROVISIONAL_SESSION = SETTLED_SESSION | {"name": "Reply with the single word READY. Do"}


class TestANameThatIsOnlyThePromptReadBack:
    """#113: the daemon's first title is the user's own words, and it is not a name.

    **What 0.150.0 does.** On the first `UserMessage` of an unnamed thread the
    TUI whitespace-collapses that message, takes 36 characters of it, and calls
    `thread/name/set` with the result (`rust-v0.150.0:codex-rs/tui/src/app/
    thread_routing.rs:1800-1854`, `tui/src/app/thread_title.rs:22`). A generated
    title then replaces it — see `SETTLED_SESSION` below, which is that swap
    caught after the fact on the run of record's own thread. How long the first
    name is live is *not* measured and is observed on #80's run of record; it
    cannot be read back, because the thread that generates the title is
    ephemeral and an ephemeral thread's timestamps are stamped when it is read
    (`rust-v0.150.0:codex-rs/app-server/src/request_processors/
    thread_processor.rs:5999-6016`).

    #78 froze the first name per target, so the product kept the truncated
    fragment for the Session's whole life — said back in every Stop Notice and
    typed after every `@` (#79's run of record).

    **The test is the daemon's own field, not a shape.** `Thread.preview` is
    "usually the first user message in the thread" (`rust-v0.150.0:codex-rs/
    app-server-protocol/src/protocol/v2/thread_data.rs:211`; present identically
    at `@0.149.1:thread_data.rs:209`), written from that same first message
    (`codex-rs/thread-store/src/thread_metadata_sync.rs:316-324`). It rides the
    cheap `thread/read` this lane already makes, so the rule costs no round trip,
    no `includeTurns` and no rollout — which matters, because the provisional
    name is set mid-turn, exactly when `TurnCache` declines to read turns.

    **Upstream draws the same line**: resuming a thread refuses a stored title
    equal to its preview rather than showing it as a name
    (`rust-v0.150.0:codex-rs/app-server/src/request_processors/
    thread_processor.rs:5783-5788`).

    **Legacy has no behaviour of this kind, and that is the citation.** Gen 1
    never read a daemon `Thread.name` — its Codex titles came from a Session's
    own self-report (`legacy@1d32845:bridge/labels.py:97-106`) and its transcript
    `ai-title`, both *dropped* from the #67 port table — so there was no
    product-composed name for codex to overwrite. The rule is new because the
    behaviour it answers is new.
    """

    def test_the_recorded_provisional_title_is_not_a_name(self) -> None:
        """The whole ticket, on the run of record's own document."""
        lane = found_with_tuis(daemon_holding(PROVISIONAL_SESSION))
        assert str(lane.rows[0].name) == "workspace-codex · 01a040ee"

    def test_the_title_the_daemon_settled_on_is_a_name(self) -> None:
        """And the good one is kept, which is what makes this a filter and not a ban."""
        lane = found_with_tuis(daemon_holding(SETTLED_SESSION))
        assert str(lane.rows[0].name) == "workspace-codex · 回复 READY"

    def test_a_prompt_short_enough_to_become_the_whole_name_is_still_not_a_name(self) -> None:
        """Under 36 characters nothing is truncated, and it is the same provisional title."""
        lane = found_with_tuis(
            daemon_holding(
                thread(THREAD, cwd="/tmp/w", name="fix the login bug", preview="fix the login bug")
            )
        )
        assert str(lane.rows[0].name) == f"w · {THREAD[:8]}"

    def test_the_prompt_is_matched_the_way_codex_collapsed_and_cut_it(self) -> None:
        """`split_whitespace().join(" ")` then 36 characters, over a prompt that had both."""
        lane = found_with_tuis(
            daemon_holding(
                thread(
                    THREAD,
                    cwd="/tmp/w",
                    name="port the log, then stop and tell me",
                    preview="port   the log,\n\tthen stop and tell me what you found",
                )
            )
        )
        assert str(lane.rows[0].name) == f"w · {THREAD[:8]}"

    def test_a_generated_title_that_merely_opens_the_prompt_is_kept(self) -> None:
        """The rule catches codex's own cut, not everything the prompt begins with.

        A generated title is told to start with an imperative verb
        (`rust-v0.150.0:codex-rs/tui/src/app/thread_title.rs:206-214`) and a
        prompt very often does too, so "the prompt starts with this name" would
        throw away good titles — this is the one that would have gone.
        """
        lane = found_with_tuis(
            daemon_holding(
                thread(
                    THREAD,
                    cwd="/tmp/w",
                    name="Fix the login bug",
                    preview="Fix the login bug in the auth module and add a test",
                )
            )
        )
        assert str(lane.rows[0].name) == "w · Fix the login bug"

    def test_a_name_the_prompt_does_not_begin_with_is_kept(self) -> None:
        """A title generated from the conversation is not a slice of the first message."""
        lane = found_with_tuis(
            daemon_holding(
                thread(
                    THREAD,
                    cwd="/tmp/w",
                    name="Port the discovery log",
                    preview="port the log, then stop and tell me",
                )
            )
        )
        assert str(lane.rows[0].name) == "w · Port the discovery log"

    def test_a_name_longer_than_the_prompt_is_kept(self) -> None:
        """The provisional title is a *slice*, so it can never outrun what it was cut from."""
        lane = found_with_tuis(
            daemon_holding(thread(THREAD, cwd="/tmp/w", name="port the log now", preview="port"))
        )
        assert str(lane.rows[0].name) == "w · port the log now"

    def test_a_daemon_that_states_no_preview_keeps_the_name(self) -> None:
        """Absent is not a claim — the same reading `threadSource` already gets (#112)."""
        lane = found_with_tuis(daemon_holding(thread(THREAD, cwd="/tmp/w", name="port the log")))
        assert str(lane.rows[0].name) == "w · port the log"

    def test_an_empty_preview_keeps_the_name_too(self) -> None:
        """`""` is how the daemon spells "no first message recorded", not "it matched"."""
        lane = found_with_tuis(
            daemon_holding(thread(THREAD, cwd="/tmp/w", name="port the log", preview=""))
        )
        assert str(lane.rows[0].name) == "w · port the log"


class TestWhenTheLaneCannotLookAtAll:
    def test_a_process_table_that_cannot_be_read_is_a_lane_error(self) -> None:
        lane = asyncio.run(
            discover(
                None,
                evidence=discovery.ProcessEvidence(
                    list_sessions=refusing_processes(OSError("no ps"))  # type: ignore[arg-type]
                ),
            )
        )
        assert lane.rows == ()
        assert lane.error is not None

    def test_and_it_carries_no_rows_so_core_leaves_the_roster_alone(self) -> None:
        lane = asyncio.run(
            discover(
                None,
                evidence=discovery.ProcessEvidence(
                    list_sessions=refusing_processes(TimeoutError())  # type: ignore[arg-type]
                ),
            )
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
    if source is None:
        described.pop("threadSource")
    else:
        described["threadSource"] = source
    return described


def in_native_tree(child: dict, *, child_first: bool = False) -> FakeDaemon:
    """One child and its proven live native root, sharing Codex's tree identity."""
    root = dict(sourced(OTHER_THREAD, "user"), sessionId=OTHER_THREAD)
    described = dict(child, sessionId=OTHER_THREAD)
    held = (described, root) if child_first else (root, described)
    return daemon_holding(*held)


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
        lane = found_with_tuis(daemon_holding(RECORDED_SESSION, RECORDED_PHANTOM))
        assert [row.target.session_id for row in lane.rows] == [RECORDED_SESSION["id"]]

    def test_the_phantom_never_reaches_the_naming_rule(self) -> None:
        """It was named `<project> · 01a0403a`, and that name is what made it a Session."""
        lane = found_with_tuis(daemon_holding(RECORDED_SESSION, RECORDED_PHANTOM))
        assert [str(row.name) for row in lane.rows] == [
            "gvc-110-probe.c45yj3u_ · Reply with the single word READY. Do"
        ]

    def test_the_session_keeps_its_process_even_when_the_phantom_is_listed_first(
        self, tmp_path: Path
    ) -> None:
        """The exact user rollout composes with its thread; daemon order is irrelevant."""
        write_live_user_rollout(tmp_path, str(RECORDED_SESSION["id"]), PROBE_WORKSPACE)
        lane = found(
            daemon_holding(RECORDED_PHANTOM, RECORDED_SESSION),
            running(
                43261,
                PROBE_WORKSPACE,
                session_id=str(RECORDED_SESSION["id"]),
            ),
            home=tmp_path,
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
        root = dict(sourced(OTHER_THREAD, "user"), sessionId=OTHER_THREAD)
        child = dict(sourced(THREAD, "subagent"), sessionId=OTHER_THREAD)
        lane = found_with_tuis(daemon_holding(root, child))
        assert [row.target.session_id for row in lane.rows if not row.child.is_main] == [THREAD]

    def test_a_guardian_review_thread_is_kept_for_the_same_reason(self) -> None:
        """One delegate class split by a boolean: `codex-rs/core/src/codex_delegate.rs:111`."""
        root = dict(sourced(OTHER_THREAD, "user"), sessionId=OTHER_THREAD)
        child = dict(sourced(THREAD, "guardian_review"), sessionId=OTHER_THREAD)
        lane = found_with_tuis(daemon_holding(root, child))
        assert [row.target.session_id for row in lane.rows if not row.child.is_main] == [THREAD]

    def test_an_exact_user_rollout_supplies_an_old_daemons_missing_source(self) -> None:
        """A 0.130-era daemon can omit source; exact rollout evidence supplies `user`."""
        lane = found_with_tuis(daemon_holding(sourced(THREAD, None)))
        assert [row.target.session_id for row in lane.rows] == [THREAD]

    def test_an_exact_user_rollout_supplies_a_null_daemon_source(self) -> None:
        """`null` is how a 0.149.1 daemon spells the same absence.

        `thread_source` is an `Option` that is always serialised, so "no source
        recorded" reaches this build as `"threadSource": null`. It is not root
        classification by itself; the fixture's exact user rollout is.
        """
        lane = found_with_tuis(daemon_holding(dict(sourced(THREAD, None), threadSource=None)))
        assert [row.target.session_id for row in lane.rows] == [THREAD]

    def test_an_exact_user_rollout_supplies_an_unreadable_daemon_source(self) -> None:
        """An unreadable daemon field says nothing; the exact user rollout says root."""
        lane = found_with_tuis(daemon_holding(dict(sourced(THREAD, None), threadSource=7)))
        assert [row.target.session_id for row in lane.rows] == [THREAD]

    def test_the_keep_list_is_the_vocabulary_79_shares(self) -> None:
        """Named once, here, so the two rules cannot drift apart (#79's coordination)."""
        assert discovery.SESSION_THREAD_SOURCES == frozenset(
            {"user", "subagent", "guardian_review"}
        )


class TestTheChildProcessRule:
    """#79: a thread another thread spawned is seen, and it is not a Session.

    The evidence is the same cheap `thread/read` #112 already filters on, and
    the same keep-list: the two values it keeps and #112 does not need —
    `subagent` and `guardian_review` — are exactly this rule's child rows
    (advisor, 2026-08-27). They are one delegate class split by a boolean
    (`rust-v0.150.0:codex-rs/core/src/codex_delegate.rs:111`), which is why one
    classification covers both.

    **Adapted from legacy, and the adaptation is the point** (ADR 0010).
    `legacy@1d32845:bridge/__main__.py:876-899` read the same fact from
    `session_meta.thread_source` and used it to *refuse registration*, so a
    child had no row at all; `bridge/claude.py:396-409` gave it no channel and
    `bridge/transcript.py:1477-1500` filtered its records out of the parent's
    view. v1.0 keeps the safety outcome — no Relay, no Stop Notice, no name —
    and drops the invisibility: the row is listed, under its parent.
    """

    def test_only_the_live_native_tree_becomes_roster_rows(self, tmp_path: Path) -> None:
        """Exact shared identity makes one root live; `sessionId` carries its children."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        write_live_user_rollout(tmp_path, THREAD, workspace)
        children = (
            ("01a03b06-f995-7b60-bc9f-e2152ee4ed33", "subagent"),
            ("01a03b06-f995-7b60-bc9f-e2152ee4ed34", "subagent"),
            ("01a03b06-f995-7b60-bc9f-e2152ee4ed35", "guardian_review"),
        )
        root = dict(sourced(THREAD, "user"), cwd=str(workspace), sessionId=THREAD)
        child_threads = [
            dict(
                sourced(thread_id, source),
                cwd=str(workspace),
                sessionId=THREAD,
                parentThreadId=THREAD,
            )
            for thread_id, source in children
        ]
        historical = dict(sourced(OTHER_THREAD, "user"), cwd=str(workspace), sessionId=OTHER_THREAD)
        historical_child = dict(
            sourced("01a0385e-4872-7353-bdc5-8966c6165a8f", "subagent"),
            cwd=str(workspace),
            sessionId=OTHER_THREAD,
            parentThreadId=OTHER_THREAD,
        )

        lane = found(
            daemon_holding(root, *child_threads, historical, historical_child),
            running(101, workspace, session_id=THREAD),
            home=tmp_path,
        )

        assert [
            (
                row.target.session_id,
                row.target.pid,
                row.child.kind,
                row.child.parent.session_id if row.child.parent is not None else None,
                row.child.parent.pid if row.child.parent is not None else None,
            )
            for row in lane.rows
        ] == [
            (THREAD, 101, ChildKind.MAIN, None, None),
            *[(thread_id, None, ChildKind.CHILD, THREAD, 101) for thread_id, _source in children],
        ]

    def test_exact_user_rollout_supplies_a_daemon_roots_missing_source(
        self, tmp_path: Path
    ) -> None:
        """Exact live identity keeps native children when daemon root classification is absent."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        write_live_user_rollout(tmp_path, THREAD, workspace)
        root = dict(sourced(THREAD, None), cwd=str(workspace), sessionId=THREAD)
        child_id = "01a03b06-f995-7b60-bc9f-e2152ee4ed33"
        child = dict(
            sourced(child_id, "subagent"),
            cwd=str(workspace),
            sessionId=THREAD,
            parentThreadId=THREAD,
        )

        lane = found(
            daemon_holding(root, child),
            running(101, workspace, session_id=THREAD),
            home=tmp_path,
        )

        assert [row.target.session_id for row in lane.rows] == [THREAD, child_id]
        assert lane.rows[0].target.pid == 101
        assert lane.rows[1].child.parent == lane.rows[0].target

    def test_a_subagent_thread_is_a_child(self) -> None:
        lane = found_with_tuis(in_native_tree(sourced(THREAD, "subagent")))
        child = next(row for row in lane.rows if row.target.session_id == THREAD)
        assert child.child.kind is ChildKind.CHILD

    def test_a_guardian_review_thread_is_a_child(self) -> None:
        lane = found_with_tuis(in_native_tree(sourced(THREAD, "guardian_review")))
        child = next(row for row in lane.rows if row.target.session_id == THREAD)
        assert child.child.kind is ChildKind.CHILD

    def test_a_child_disappears_when_its_parents_tui_has_exited(self) -> None:
        parent = sourced(OTHER_THREAD, "user")
        child = dict(sourced(THREAD, "subagent"), parentThreadId=OTHER_THREAD)

        assert found(daemon_holding(parent, child)).rows == ()

    def test_a_users_own_thread_is_main(self) -> None:
        lane = found_with_tuis(daemon_holding(sourced(THREAD, "user")))
        assert [row.child.kind for row in lane.rows] == [ChildKind.MAIN]

    def test_a_thread_that_names_no_source_is_main(self) -> None:
        """Absent is not a claim, and the roster's ordinary row is the user's.

        The same reading #112 gives the field: an older daemon classifies
        nothing, and reading its silence as "child" would make every Session on
        that machine unaddressable — the failure mode this rule is the mirror
        image of.
        """
        lane = found_with_tuis(daemon_holding(sourced(THREAD, None)))
        assert [row.child.kind for row in lane.rows] == [ChildKind.MAIN]

    def test_a_child_is_listed_under_the_thread_that_spawned_it(self) -> None:
        """`parentThreadId` rides on the same read the classification does."""
        described = dict(sourced(THREAD, "subagent"), parentThreadId=OTHER_THREAD)
        lane = found_with_tuis(in_native_tree(described))
        rows = {row.target.session_id: row for row in lane.rows}
        assert rows[THREAD].child.parent == rows[OTHER_THREAD].target

    def test_a_child_whose_parent_the_daemon_does_not_name_is_still_a_child(self) -> None:
        """The locked type's own rule: demoting it over a missing link opens the Relay.

        `parentThreadId` is `null` on every thread the daemon did not record one
        for — it is `null` on the recorded phantom above — so a child that
        arrives without one is the ordinary case and not a malformed row.
        """
        lane = found_with_tuis(
            in_native_tree(dict(sourced(THREAD, "subagent"), parentThreadId=None))
        )
        child = next(row for row in lane.rows if row.target.session_id == THREAD)
        assert child.child == ChildClassification(kind=ChildKind.CHILD, parent=None)

    def test_a_child_is_never_named(self) -> None:
        """#78's rule, held where the row is made as well as where it is kept.

        The registry drops a child's name (`core/sessions.py:_named_as`), so a
        lane composing one would be composing something nobody sees. Not
        composing it is the honest half of the same rule: a Session Name is what
        the user says to reach a Session, and there is nothing here to reach.
        """
        lane = found_with_tuis(
            in_native_tree(dict(sourced(THREAD, "subagent"), name="tidy the tests"))
        )
        child = next(row for row in lane.rows if row.target.session_id == THREAD)
        assert child.name is None

    def test_the_child_list_is_the_keep_list_without_the_user(self) -> None:
        """Derived, not written out beside it, so the two cannot disagree (#112, #79).

        The rule is one sentence: a thread that reaches the roster and is not
        the person's own is a Child Process. Spelled as two literals, a source
        added to the keep-list one day would have to be remembered here on the
        same day — and the day it was not, a subagent would have become
        addressable.
        """
        assert discovery.CHILD_THREAD_SOURCES == discovery.SESSION_THREAD_SOURCES - {"user"}
        assert "user" not in discovery.CHILD_THREAD_SOURCES

    def test_a_child_never_takes_the_roots_tui(self, tmp_path: Path) -> None:
        """Only the exact user rollout composes with the process; a child has no PID."""
        child = dict(
            sourced(THREAD, "subagent"),
            cwd="/tmp/w",
            sessionId=OTHER_THREAD,
            parentThreadId=OTHER_THREAD,
        )
        parent = dict(sourced(OTHER_THREAD, "user"), cwd="/tmp/w", sessionId=OTHER_THREAD)
        write_live_user_rollout(tmp_path, OTHER_THREAD, "/tmp/w")
        lane = found(
            daemon_holding(child, parent),
            running(4321, "/tmp/w", session_id=OTHER_THREAD),
            home=tmp_path,
        )
        held = {row.target.session_id: row.target.pid for row in lane.rows}
        assert held == {THREAD: None, OTHER_THREAD: 4321}

    def test_a_child_names_its_parent_by_the_address_that_parents_row_carries(
        self, tmp_path: Path
    ) -> None:
        """One Session, one address — and this one has the workspace's pid in it.

        `parentThreadId` names a thread, but a Session's address is that thread
        *and* the pid its exact rollout joined to it, so a parent named from the field
        alone points at an address no row in the roster holds. #79's acceptance
        `child` step reads exactly this link, and failed on exactly this
        difference: it saw the child listed under `codex:01a040cc-…` while the
        Session that spawned it was `codex:01a040cc-…:36628`.

        The child is listed *before* its parent here, because the daemon orders
        threads however it likes and the answer may not depend on that order.
        """
        child = dict(
            sourced(THREAD, "subagent"),
            cwd="/tmp/w",
            sessionId=OTHER_THREAD,
            parentThreadId=OTHER_THREAD,
        )
        parent = dict(sourced(OTHER_THREAD, "user"), cwd="/tmp/w", sessionId=OTHER_THREAD)
        write_live_user_rollout(tmp_path, OTHER_THREAD, "/tmp/w")
        lane = found(
            daemon_holding(child, parent),
            running(4321, "/tmp/w", session_id=OTHER_THREAD),
            home=tmp_path,
        )
        rows = {row.target.session_id: row for row in lane.rows}
        assert rows[THREAD].child.parent == rows[OTHER_THREAD].target
        assert rows[THREAD].child.parent.pid == 4321


class TestSayingSoWithoutSayingItTwelveTimesAMinute:
    """A dropped row is a row somebody may come looking for, so the log keeps it.

    Once per thread id, not once per tick. The filter runs on the five-second
    discovery cadence and a phantom outlives the TUI that made it — #79's
    `thread/read` for this one succeeded *after* the probe had stopped the TUI's
    process group — so a line per pass would be twelve lines a minute, for as
    long as the daemon holds the thread, saying the same thing.
    """

    def test_the_phantom_is_never_read_for_its_turns(self, tmp_path: Path) -> None:
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
        write_live_user_rollout(tmp_path, str(RECORDED_SESSION["id"]), PROBE_WORKSPACE)
        asyncio.run(
            discover(
                daemon,  # type: ignore[arg-type]
                evidence=discovery.ProcessEvidence(
                    list_sessions=listing(
                        running(
                            101,
                            PROBE_WORKSPACE,
                            session_id=str(RECORDED_SESSION["id"]),
                        )
                    ),  # type: ignore[arg-type]
                    home=tmp_path,
                ),
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
                        evidence=discovery.ProcessEvidence(
                            list_sessions=listing()  # type: ignore[arg-type]
                        ),
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
                evidence=discovery.ProcessEvidence(
                    list_sessions=listing()  # type: ignore[arg-type]
                ),
                reported_non_sessions=skipped,
                projects=ProjectNames(ask=not_a_repository()),  # type: ignore[arg-type]
            )
        )
        assert skipped == {str(RECORDED_PHANTOM["id"])}

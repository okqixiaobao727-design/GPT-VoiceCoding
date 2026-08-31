"""The Claude lane's roster: `claude agents --json`, mapped onto the seam.

The shapes here are the ones measured on Simon's machine on 2026-08-26 against
Claude Code 2.1.246 (#73) — a real row, verbatim, and the real failure modes.
The command is injected, because what is under test is the mapping and the
refusals, and shelling out to whatever `claude` this machine has would test that
machine instead.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from gpt_voicecoding.adapters.agent._project import ProjectNames
from gpt_voicecoding.adapters.agent.claude.discovery import CommandResult, discover
from gpt_voicecoding.seams.agent import SessionState, WaitingKind
from gpt_voicecoding.seams.identity import AgentKind

#: One row of `claude agents --json`, exactly as it was measured (#73).
IDLE_ROW = {
    "pid": 3538,
    "cwd": "/tmp/workspace-claude",
    "kind": "interactive",
    "startedAt": 1787693113762,
    "sessionId": "d3a776ae-3b60-437d-bc70-ba57a2b280c6",
    "name": "workspace-claude-ed",
    "status": "idle",
}


def answering(rows: object, *, code: int = 0, stderr: str = "") -> object:
    """A stand-in for the roster command that answers with exactly this."""
    import json

    async def run(argv: list[str]) -> CommandResult:
        del argv
        return CommandResult(code=code, stdout=json.dumps(rows), stderr=stderr)

    return run


def refusing(reason: str) -> object:
    async def run(argv: list[str]) -> CommandResult:
        del argv
        raise FileNotFoundError(reason)

    return run


def not_a_repository() -> object:
    """A `git` that says a workspace belongs to no repository."""

    async def ask(asked: Path) -> str | None:
        del asked
        return None

    return ask


def inside(repository: str) -> object:
    """A `git` answering with the common directory of exactly this repository."""

    async def ask(asked: Path) -> str | None:
        del asked
        return f"{repository}/.git\n"

    return ask


def found(rows: object, *, git: object = None, **kwargs: object) -> object:
    projects = ProjectNames(ask=git or not_a_repository())  # type: ignore[arg-type]
    return asyncio.run(
        discover(run=answering(rows, **kwargs), projects=projects)  # type: ignore[arg-type]
    )


class TestMappingOneRow:
    def test_the_row_becomes_one_inspection(self) -> None:
        lane = found([IDLE_ROW])
        assert len(lane.rows) == 1
        assert lane.error is None

    def test_the_target_carries_both_the_session_id_and_the_pid(self) -> None:
        """A resumed Session forks two processes under one id, so both travel."""
        target = found([IDLE_ROW]).rows[0].target
        assert target.agent is AgentKind.CLAUDE
        assert target.session_id == IDLE_ROW["sessionId"]
        assert target.pid == 3538

    def test_the_workspace_is_the_cwd_the_roster_reported(self) -> None:
        assert found([IDLE_ROW]).rows[0].workspace == Path("/tmp/workspace-claude")

    def test_the_row_is_named_for_its_project_and_the_agents_own_name(self) -> None:
        """#78: `<project> · <title>`, with the roster's own `name` as the title."""
        named = found([IDLE_ROW], git=inside("/src/GPT-VoiceCoding")).rows[0].name
        assert str(named) == "GPT-VoiceCoding · workspace-claude-ed"

    def test_a_workspace_outside_a_repository_is_named_for_its_directory(self) -> None:
        """*Adapted*: legacy left such a Session unnamed and unspeakable."""
        assert str(found([IDLE_ROW]).rows[0].name) == "workspace-claude · workspace-claude-ed"

    def test_a_row_the_roster_did_not_name_stays_unnamed(self) -> None:
        """No title, no name. An unnamed row is listed like any other."""
        assert found([IDLE_ROW | {"name": "   "}]).rows[0].name is None

    def test_a_row_with_no_workspace_stays_unnamed(self) -> None:
        """Half a name is not a name, and there is nothing to fill the other half with."""
        without_cwd = {key: value for key, value in IDLE_ROW.items() if key != "cwd"}
        lane = found([without_cwd])
        assert len(lane.rows) == 1
        assert lane.rows[0].name is None

    def test_the_project_is_read_once_however_many_rows_share_a_workspace(self) -> None:
        """The cadence asks this per row, every few seconds; `git` is asked once."""
        asked: list[Path] = []

        async def counting(workspace: Path) -> str | None:
            asked.append(workspace)
            return "/src/GPT-VoiceCoding/.git"

        second = IDLE_ROW | {"pid": 3539, "sessionId": "another-session-id"}
        projects = ProjectNames(ask=counting)
        lane = asyncio.run(
            discover(run=answering([IDLE_ROW, second]), projects=projects)  # type: ignore[arg-type]
        )

        assert [str(row.name) for row in lane.rows] == [
            "GPT-VoiceCoding · workspace-claude-ed",
            "GPT-VoiceCoding · workspace-claude-ed",
        ]
        assert len(asked) == 1

    def test_a_row_in_the_roster_is_live(self) -> None:
        assert found([IDLE_ROW]).rows[0].lifecycle == "live"


class TestTheStatusVocabulary:
    """Measured: `status` walks `idle → busy → waiting → idle` across one turn."""

    def test_idle_is_idle(self) -> None:
        assert found([IDLE_ROW]).rows[0].state is SessionState.IDLE

    def test_busy_is_running(self) -> None:
        assert found([IDLE_ROW | {"status": "busy"}]).rows[0].state is SessionState.RUNNING

    def test_waiting_is_waiting(self) -> None:
        assert found([IDLE_ROW | {"status": "waiting"}]).rows[0].state is SessionState.WAITING

    def test_a_status_this_build_does_not_know_is_not_guessed_at(self) -> None:
        """An unknown word is a Session doing *something*, which is `running`."""
        assert found([IDLE_ROW | {"status": "compacting"}]).rows[0].state is SessionState.RUNNING


class TestWhatItRefusesToInvent:
    def test_a_waiting_session_is_not_claimed_to_be_a_permission(self) -> None:
        """The roster says a Session waits; it never says what for.

        #73 measured that `waiting` is the permission state, but not that it is
        *only* that, and a question dialog has never been observed from here.
        `UNKNOWN` with `caught_up=False` is the seam's word for "ask again"; #75
        answers it from the transcript.
        """
        waiting_for = found([IDLE_ROW | {"status": "waiting"}]).rows[0].waiting_for
        assert waiting_for.kind is WaitingKind.UNKNOWN
        assert waiting_for.caught_up is False

    def test_an_idle_session_is_waiting_for_nothing(self) -> None:
        assert found([IDLE_ROW]).rows[0].waiting_for.kind is WaitingKind.NONE

    def test_progress_is_unread_rather_than_empty(self) -> None:
        """The roster did not read progress; it does not pretend that history is empty."""
        assert str(found([IDLE_ROW]).rows[0].progress.availability) == "not_read"

    def test_last_activity_is_not_taken_from_the_start_time(self) -> None:
        """`startedAt` is when it began, which is not when it last did anything."""
        assert found([IDLE_ROW]).rows[0].last_activity is None

    def test_every_row_the_roster_lists_is_a_main_session(self) -> None:
        """A child inherits `CLAUDE_CODE_*` and is absent from the roster (#73)."""
        assert found([IDLE_ROW]).rows[0].child.is_main


class TestWhenTheLaneCannotLook:
    def test_a_missing_roster_command_is_a_lane_error_not_an_empty_machine(self) -> None:
        lane = asyncio.run(discover(run=refusing("no claude on PATH")))  # type: ignore[arg-type]
        assert lane.rows == ()
        assert lane.error is not None
        assert "claude" in lane.error

    def test_a_non_zero_exit_carries_what_the_command_said(self) -> None:
        lane = found([], code=1, stderr="unknown flag --json")
        assert lane.rows == ()
        assert lane.error is not None
        assert "unknown flag --json" in lane.error

    def test_output_that_is_not_json_is_a_lane_error(self) -> None:
        async def run(argv: list[str]) -> CommandResult:
            del argv
            return CommandResult(code=0, stdout="Welcome to Claude Code!", stderr="")

        lane = asyncio.run(discover(run=run))
        assert lane.rows == ()
        assert lane.error is not None

    def test_json_that_is_not_a_list_of_rows_is_a_lane_error(self) -> None:
        assert found({"agents": []}).error is not None

    def test_an_empty_roster_is_an_answer_not_a_failure(self) -> None:
        lane = found([])
        assert lane.rows == ()
        assert lane.error is None


class TestOneBadRowAmongGoodOnes:
    def test_a_row_missing_its_session_id_is_skipped_not_fatal(self) -> None:
        """One unreadable row must not hide every Session on the machine."""
        lane = found([{"pid": 1, "cwd": "/tmp", "status": "idle"}, IDLE_ROW])
        assert [row.target.pid for row in lane.rows] == [3538]
        assert lane.error is None

    def test_a_row_missing_its_pid_is_skipped(self) -> None:
        """A Claude target without a pid is ambiguous: `--resume` forks."""
        lane = found([{k: v for k, v in IDLE_ROW.items() if k != "pid"}])
        assert lane.rows == ()
        assert lane.error is None


class TestTheKindField:
    def test_a_stated_non_interactive_kind_is_not_a_session(self) -> None:
        """`--all` adds completed background agents; those are not Sessions here."""
        assert found([IDLE_ROW | {"kind": "background"}]).rows == ()

    def test_a_row_that_does_not_state_its_kind_is_kept(self) -> None:
        """A field that moved must not blank the roster — the worse mistake."""
        lane = found([{k: v for k, v in IDLE_ROW.items() if k != "kind"}])
        assert len(lane.rows) == 1


class TestTheLabelOnAWaitingRow:
    """`claude agents --json` copies `waitingFor` onto a `waiting` row (#150).

    The roster reads it for the one thing the roster can settle on its own: a
    dialog the user is driving is not a wait on anybody. It does **not** promote
    `permission prompt` to `PERMISSION` here, and that is deliberate — see the
    reader's own note. The roster carries no dialog handle, so a row that
    claimed `needs_the_user` would produce a second, unanswerable notice beside
    the Approval Relay's for one decision.
    """

    def test_a_dialog_the_user_is_driving_is_waiting_on_nobody(self) -> None:
        """The one thing this reader does act on, and the gate it keeps clear.

        `needs_the_user` is what Bridge Core's reconcile pass announces from
        (`core/bridge.py:480`), so a `/model` picker reading false here is what
        keeps the reported notice off that path as well as off the sweep's.
        """
        row = IDLE_ROW | {"status": "waiting", "waitingFor": "dialog open"}
        waiting_for = found([row]).rows[0].waiting_for

        assert waiting_for.kind is WaitingKind.NONE
        assert waiting_for.caught_up is True
        assert waiting_for.needs_the_user is False

    def test_a_goal_proposal_is_waiting_on_nobody_either(self) -> None:
        row = IDLE_ROW | {"status": "waiting", "waitingFor": "goal proposal"}

        assert found([row]).rows[0].waiting_for.kind is WaitingKind.NONE

    def test_the_row_still_says_the_session_is_waiting(self) -> None:
        """The registry's own word for the state stands; only the wait is answered."""
        row = IDLE_ROW | {"status": "waiting", "waitingFor": "dialog open"}

        assert found([row]).rows[0].state is SessionState.WAITING

    def test_a_permission_prompt_row_reads_exactly_as_it_did_before_this_ticket(self) -> None:
        """The one narrowing, pinned: a named wait is not promoted on this reader.

        `classify` calls `permission prompt` a `PERMISSION` and the Reply Window
        sweep announces it as one. This row must stay `UNKNOWN` with
        `caught_up=False` — what the projection reported for every `waiting` row
        before #150 — because a roster row carries no `approval_id`, so a
        `PERMISSION` here would key Bridge Core's delivered-wait dedup
        `(target, PERMISSION)`, miss the live path's `(target, approval_id)`,
        and announce the same dialog a second time. See `_waiting_for`.
        """
        row = IDLE_ROW | {"status": "waiting", "waitingFor": "permission prompt"}
        waiting_for = found([row]).rows[0].waiting_for

        assert waiting_for.kind is WaitingKind.UNKNOWN
        assert waiting_for.kind is not WaitingKind.PERMISSION
        assert waiting_for.caught_up is False
        assert waiting_for.approval_id is None
        assert waiting_for.needs_the_user is False

    def test_a_sandbox_request_row_is_narrowed_the_same_way(self) -> None:
        """Every named disposition, not just the one — the rule is the reader's."""
        row = IDLE_ROW | {"status": "waiting", "waitingFor": "sandbox request"}
        waiting_for = found([row]).rows[0].waiting_for

        assert waiting_for.kind is WaitingKind.UNKNOWN
        assert waiting_for.tool_name is None

    def test_a_row_carrying_no_label_reads_exactly_as_it_did(self) -> None:
        """Every build before this field, and every row this reader mis-reads."""
        waiting_for = found([IDLE_ROW | {"status": "waiting"}]).rows[0].waiting_for

        assert waiting_for.kind is WaitingKind.UNKNOWN
        assert waiting_for.caught_up is False

    def test_a_label_on_a_row_that_is_not_waiting_is_not_read(self) -> None:
        """`waiting` is what makes the label mean anything; an idle row waits on nothing."""
        row = IDLE_ROW | {"waitingFor": "permission prompt"}

        assert found([row]).rows[0].waiting_for.kind is WaitingKind.NONE

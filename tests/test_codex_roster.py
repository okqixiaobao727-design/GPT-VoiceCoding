"""The Codex roster composition rule, with dictionaries in and rows out.

No fake app-server and no fake process table: the whole point of #201's move is
that the rule which decides what a Codex Session *is* can be asked directly.
Four acceptance runs (`20260902T002414Z`, `…005111Z`, `…005521Z`, `…013516Z`)
failed at the lane's `roster` step because that rule was five branches of one
long I/O function with no surface of its own.

The rule under test, in one sentence: **a Codex Session is a daemon-held user
root thread that a live terminal in its own workspace vouches for.**
"""

from __future__ import annotations

from pathlib import Path

from gpt_voicecoding.adapters.agent.codex import roster
from gpt_voicecoding.adapters.agent.codex.processes import (
    START_TIME_RESOLUTION_SECONDS,
    Candidate,
)
from gpt_voicecoding.adapters.agent.codex.roster import ProcessObservation, compose
from gpt_voicecoding.seams.agent import SessionState

THREAD = "01a05fc1-b5ca-7f12-b288-ac99c70f6a03"
OTHER_THREAD = "01a05fb3-6306-7d61-b0ea-1c9b0b56a3f1"

#: The run of record, `20260902T013516Z`: the terminal started at 13:35:16 and
#: the thread it opened was created at 13:35:25, nine seconds later.
TERMINAL_STARTED_AT = 1_787_751_316.0
THREAD_CREATED_AT = 1_787_751_325.0

#: The previous run's root in the same workspace, created at 13:19:46 — before
#: this terminal existed, so this terminal cannot be the one holding it.
EARLIER_CREATED_AT = 1_787_750_386.0

WORKSPACE = "/tmp/workspace"


def thread(
    thread_id: str = THREAD,
    *,
    cwd: str = WORKSPACE,
    status: str = "idle",
    source: str | None = "user",
    created_at: float | None = THREAD_CREATED_AT,
    parent: str | None = None,
) -> dict:
    """One thread as `thread/read` describes it, in the shape #82 measured."""
    described: dict = {
        "id": thread_id,
        "cwd": cwd,
        "status": {"type": status},
        "sessionId": thread_id,
    }
    if source is not None:
        described["threadSource"] = source
    if created_at is not None:
        described["createdAt"] = created_at
    if parent is not None:
        described["parentThreadId"] = parent
        described["sessionId"] = parent
    return described


def terminal(
    pid: int = 68633,
    *,
    workspace: str = WORKSPACE,
    session_id: str | None = None,
    started_at: float | None = TERMINAL_STARTED_AT,
    rollout_root: bool = False,
) -> ProcessObservation:
    """One live interactive `codex`, as the process table reads it."""
    return ProcessObservation(
        candidate=Candidate(
            pid=pid,
            workspace=Path(workspace),
            session_id=session_id,
            started_at=started_at,
        ),
        rollout_root=rollout_root,
    )


def targets(composed: roster.Roster) -> list[tuple[str, int | None]]:
    return [(row.inspection.target.session_id, row.inspection.target.pid) for row in composed.rows]


def reasons(composed: roster.Roster) -> dict[str, str]:
    return {drop.thread_id: drop.reason for drop in composed.drops}


class TestTheHandStartedSession:
    """#201's whole reason: `codex "<prompt>"` carries no thread id anywhere."""

    def test_a_terminal_in_the_workspace_vouches_for_the_root_it_opened(self) -> None:
        composed = compose([thread()], [terminal()])

        assert targets(composed) == [(THREAD, 68633)]

    def test_the_row_is_a_main_session_read_from_the_daemon(self) -> None:
        """Identity, workspace and state all come from the daemon, not the process."""
        row = compose([thread()], [terminal()]).rows[0].inspection

        assert row.child.is_main
        assert row.workspace == Path(WORKSPACE)
        assert row.state is SessionState.IDLE

    def test_a_thread_created_in_the_same_second_the_terminal_started(self) -> None:
        """`etime` is truncated whole seconds, so the fast start must survive it."""
        composed = compose(
            [thread(created_at=TERMINAL_STARTED_AT - START_TIME_RESOLUTION_SECONDS)],
            [terminal()],
        )

        assert targets(composed) == [(THREAD, 68633)]

    def test_two_terminals_in_one_workspace_leave_the_row_without_a_pid(self) -> None:
        """Still a row: which terminal it is is unknown, that there is one is not."""
        composed = compose([thread()], [terminal(pid=1), terminal(pid=2)])

        assert targets(composed) == [(THREAD, None)]


class TestWhatIsNotARow:
    def test_a_root_no_terminal_vouches_for_is_not_a_row(self) -> None:
        """#123's ghost: the daemon holds it for half an hour after the TUI exits."""
        composed = compose([thread()], [])

        assert composed.rows == ()
        assert roster.NO_TERMINAL in reasons(composed)[THREAD]

    def test_a_root_created_before_the_terminal_started_is_not_that_terminals(self) -> None:
        composed = compose(
            [thread(OTHER_THREAD, created_at=EARLIER_CREATED_AT), thread()],
            [terminal()],
        )

        assert targets(composed) == [(THREAD, 68633)]
        assert roster.NO_TERMINAL in reasons(composed)[OTHER_THREAD]

    def test_a_terminal_in_another_workspace_vouches_for_nothing(self) -> None:
        composed = compose([thread()], [terminal(workspace="/tmp/elsewhere")])

        assert composed.rows == ()

    def test_a_thread_the_daemon_says_is_not_loaded_is_not_live(self) -> None:
        composed = compose([thread(status=roster.NOT_LOADED)], [terminal()])

        assert composed.rows == ()
        assert roster.NOT_LOADED in reasons(composed)[THREAD]

    def test_a_root_the_daemon_gave_no_creation_time_cannot_be_vouched_for(self) -> None:
        """Without it, nothing can show the terminal did not predate the thread."""
        composed = compose([thread(created_at=None)], [terminal()])

        assert composed.rows == ()
        assert roster.CREATED_AT in reasons(composed)[THREAD]

    def test_a_terminal_alone_is_never_a_session(self) -> None:
        """Process-only evidence still never enters the roster (#144)."""
        assert compose([], [terminal()]).rows == ()

    def test_a_thread_the_daemon_did_not_classify_is_not_a_row(self) -> None:
        composed = compose([thread(source=None)], [terminal()])

        assert composed.rows == ()
        assert reasons(composed)[THREAD]


class TestTheDaemonsOwnErrands:
    """#112's filter, applied here rather than by the caller (#201).

    It lives inside the rule so that the rule is the whole rule: a caller that
    filtered first would be a second copy of it, and this function could not
    tell a list that had been filtered from one that had not.
    """

    def test_an_ephemeral_thread_is_never_a_session(self) -> None:
        """0.150.0's title generation, which is `ephemeral` and `user`-adjacent."""
        errand = dict(thread(), ephemeral=True)
        composed = compose([errand], [terminal()])

        assert composed.rows == ()
        assert roster.EPHEMERAL in reasons(composed)[THREAD]

    def test_a_thread_source_this_build_does_not_know_is_an_errand(self) -> None:
        composed = compose([thread(source="compaction")], [terminal()])

        assert composed.rows == ()
        assert "compaction" in reasons(composed)[THREAD]

    def test_a_subagent_survives_the_errand_filter_for_the_child_rule(self) -> None:
        """#79 needs these rows; #112 must not delete them first."""
        composed = compose(
            [thread(), thread(OTHER_THREAD, source="subagent", parent=THREAD)],
            [terminal()],
        )

        assert [row.inspection.target.session_id for row in composed.rows] == [
            THREAD,
            OTHER_THREAD,
        ]


class TestEveryDropLeavesAReason:
    def test_no_thread_is_dropped_silently(self) -> None:
        """The absence of this is why #201's first diagnosis was wrong."""
        composed = compose(
            [
                thread(),
                thread(OTHER_THREAD, created_at=EARLIER_CREATED_AT),
                thread("01a05fb4-0000-7000-8000-000000000000", status=roster.NOT_LOADED),
                thread("01a05fb5-0000-7000-8000-000000000000", source=None),
            ],
            [terminal()],
        )

        rowed = {row.inspection.target.session_id for row in composed.rows}
        dropped = set(reasons(composed))
        assert rowed == {THREAD}
        assert dropped == {
            OTHER_THREAD,
            "01a05fb4-0000-7000-8000-000000000000",
            "01a05fb5-0000-7000-8000-000000000000",
        }


class TestTheDegradationNote:
    def test_a_terminal_that_vouches_for_nothing_is_reported(self) -> None:
        """`codex --last` and the picker land here: an accepted, stated gap."""
        composed = compose([thread(created_at=EARLIER_CREATED_AT)], [terminal()])

        assert composed.note is not None
        assert "68633" in composed.note
        assert WORKSPACE in composed.note

    def test_a_terminal_that_vouches_for_a_row_says_nothing(self) -> None:
        assert compose([thread()], [terminal()]).note is None

    def test_a_terminal_in_a_workspace_with_no_daemon_roots_says_nothing(self) -> None:
        """There is nothing under-reported there, so there is nothing to report."""
        assert compose([], [terminal()]).note is None


class TestWhatNumberOneFourFourKeeps:
    def test_an_exact_resume_still_composes_without_a_workspace_vouch(self) -> None:
        composed = compose(
            [thread(created_at=EARLIER_CREATED_AT)],
            [terminal(session_id=THREAD, started_at=TERMINAL_STARTED_AT)],
        )

        assert targets(composed) == [(THREAD, 68633)]

    def test_one_session_seen_twice_is_one_row(self) -> None:
        composed = compose(
            [thread()],
            [terminal(pid=1, session_id=THREAD), terminal(pid=2)],
        )

        assert [row.inspection.target.session_id for row in composed.rows] == [THREAD]
        assert composed.rows[0].inspection.target.pid is None

    def test_a_terminal_that_could_hold_either_root_vouches_for_neither(self) -> None:
        """One terminal cannot be sitting in two Sessions, so it proves neither live."""
        composed = compose([thread(), thread(OTHER_THREAD)], [terminal()])

        assert composed.rows == ()
        assert reasons(composed)[THREAD] == roster.AMBIGUOUS_TERMINAL
        assert composed.note is not None

    def test_an_exact_resume_beside_an_ambiguous_terminal_is_still_a_row(self) -> None:
        """An argv id names one thread, so it is never one of the ambiguous ones."""
        composed = compose(
            [thread(), thread(OTHER_THREAD)],
            [terminal(pid=1, session_id=THREAD), terminal(pid=2)],
        )

        assert targets(composed) == [(THREAD, 1)]

    def test_a_not_loaded_thread_is_not_readmitted_by_its_own_rollout(self) -> None:
        """The drop above and the process-only rule below must not disagree."""
        composed = compose(
            [thread(status=roster.NOT_LOADED)],
            [terminal(session_id=THREAD, rollout_root=True)],
        )

        assert composed.rows == ()

    def test_an_exact_resume_with_a_user_rollout_still_composes_alone(self) -> None:
        """The daemon that never adopted a TUI started before it (#82)."""
        composed = compose([], [terminal(session_id=THREAD, rollout_root=True)])

        assert targets(composed) == [(THREAD, 68633)]


class TestChildrenStayWhereTheyWere:
    def test_a_child_of_a_live_root_is_listed_under_it(self) -> None:
        composed = compose(
            [thread(), thread(OTHER_THREAD, source="subagent", parent=THREAD)],
            [terminal()],
        )

        child = composed.rows[1].inspection
        assert not child.child.is_main
        assert child.child.parent is not None
        assert child.child.parent.session_id == THREAD
        assert child.child.parent.pid == 68633

    def test_a_child_carries_no_pid_of_its_own(self) -> None:
        composed = compose(
            [thread(), thread(OTHER_THREAD, source="subagent", parent=THREAD)],
            [terminal()],
        )

        assert composed.rows[1].inspection.target.pid is None

    def test_a_child_of_a_tree_no_root_holds_is_dropped_with_a_reason(self) -> None:
        composed = compose(
            [thread(OTHER_THREAD, source="subagent", parent=THREAD)],
            [terminal()],
        )

        assert composed.rows == ()
        assert reasons(composed)[OTHER_THREAD]


class TestATerminalTheDaemonHoldsNothingFor:
    """#233: a live TUI outside the shared daemon left no row and no sentence.

    The degradation note above speaks only when the daemon holds user roots in
    the terminal's workspace, so a terminal running its own core — a TUI started
    with a `-c` override — fell between the two rules and was never mentioned at
    all. These are the terminals the other sentence cannot reach; together the
    two account for every live terminal that composed no row.
    """

    def test_a_terminal_in_a_workspace_with_no_daemon_root_is_carried_back(self) -> None:
        composed = compose([], [terminal()])

        assert [(held.pid, str(held.workspace)) for held in composed.unheld] == [(68633, WORKSPACE)]

    def test_a_workspace_holding_only_threads_that_dropped_is_carried_back_too(self) -> None:
        """And the sentence stays true, which is why it is about rows.

        Each of these is a workspace the daemon holds *something* in, so "the
        daemon holds no user root there" would be a claim the rule cannot make:
        the errand may be one it mislabelled, the `notLoaded` thread was a root
        until the daemon let it go, and the sourceless one is unclassified
        rather than shown to be an errand. What is observed is that none of them
        is a row.
        """
        for held in (
            thread(source="compaction"),
            thread(status=roster.NOT_LOADED),
            thread(source=None),
        ):
            composed = compose([held], [terminal()])

            assert composed.rows == ()
            assert [unheld.pid for unheld in composed.unheld] == [68633]
            assert composed.note is None

    def test_a_terminal_that_composed_a_row_is_not_carried_back(self) -> None:
        assert compose([thread()], [terminal()]).unheld == ()

    def test_a_terminal_behind_a_process_only_row_is_not_carried_back(self) -> None:
        """A row the daemon never offered still accounts for the terminal under it."""
        composed = compose([], [terminal(session_id=THREAD, rollout_root=True)])

        assert targets(composed) == [(THREAD, 68633)]
        assert composed.unheld == ()

    def test_the_terminal_the_degradation_note_speaks_for_is_not_said_twice(self) -> None:
        """The two sentences partition the terminals; neither says the other's."""
        composed = compose([thread(created_at=EARLIER_CREATED_AT)], [terminal()])

        assert composed.note is not None
        assert composed.unheld == ()

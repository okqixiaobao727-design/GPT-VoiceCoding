"""Rows arrive by observation, and one process stays one row while it does.

The registry's hardest job is not holding Sessions — it is deciding that two
readings, taken seconds apart by two different sources, are about the same
Session. Every case here is one this product actually meets: a `codex` that has
not been named yet, a shared daemon that goes away mid-run, a `/new` typed into
a TUI, and a lane that cannot look at all.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest

from gpt_voicecoding.core import sessions as sessions_module
from gpt_voicecoding.core.sessions import NoNameMatchError, SessionRegistry
from gpt_voicecoding.seams.agent import (
    ChildClassification,
    ChildKind,
    LaneDiscovery,
    ProgressAvailability,
    ProgressEntry,
    ProgressObservation,
    ProgressOmission,
    ProgressRole,
    SessionInspection,
    SessionLifecycle,
    SessionState,
)
from gpt_voicecoding.seams.identity import AgentKind, SessionName, SessionTarget

WORKSPACE = Path("/tmp/workspace")
NOW = 1_000.0


def codex_row(*, session_id: str | None, pid: int | None, **fields: object) -> SessionInspection:
    return SessionInspection(
        target=SessionTarget(agent=AgentKind.CODEX, session_id=session_id, pid=pid),
        workspace=WORKSPACE,
        **fields,  # type: ignore[arg-type]
    )


def claude_row(*, session_id: str, pid: int, **fields: object) -> SessionInspection:
    return SessionInspection(
        target=SessionTarget(agent=AgentKind.CLAUDE, session_id=session_id, pid=pid),
        workspace=WORKSPACE,
        **fields,  # type: ignore[arg-type]
    )


def named(task: str) -> SessionName:
    """One composed Session Name, as a lane hands it over."""
    return SessionName(project="GPT-VoiceCoding", task=task)


def seeing(*rows: SessionInspection, **lane: object) -> LaneDiscovery:
    return LaneDiscovery(rows=rows, **lane)  # type: ignore[arg-type]


class TestFirstSighting:
    def test_a_row_a_lane_saw_becomes_a_roster_entry(self) -> None:
        registry = SessionRegistry()
        registry.observe(AgentKind.CODEX, seeing(codex_row(session_id="abc", pid=10)), now=NOW)

        assert [held.target.session_id for held in registry.live()] == ["abc"]

    def test_when_we_first_saw_it_is_the_registrys_own_fact(self) -> None:
        """No agent knows when *we* first saw it, so no reading may overwrite it."""
        registry = SessionRegistry()
        registry.observe(AgentKind.CODEX, seeing(codex_row(session_id="abc", pid=10)), now=NOW)
        registry.observe(
            AgentKind.CODEX,
            seeing(codex_row(session_id="abc", pid=10, state=SessionState.IDLE)),
            now=NOW + 500,
        )

        assert registry.live()[0].first_seen == NOW

    def test_a_lane_speaks_only_for_its_own_agent(self) -> None:
        registry = SessionRegistry()
        registry.observe(AgentKind.CODEX, seeing(claude_row(session_id="def", pid=20)), now=NOW)

        assert registry.all() == ()


class TestOneProcessStaysOneRow:
    """The join #73 forced: `codex` has no session id until its first turn."""

    def test_a_session_that_gets_named_is_the_same_row_re_keyed(self) -> None:
        registry = SessionRegistry()
        registry.observe(AgentKind.CODEX, seeing(codex_row(session_id=None, pid=10)), now=NOW)
        registry.observe(AgentKind.CODEX, seeing(codex_row(session_id="abc", pid=10)), now=NOW + 5)

        assert len(registry.all()) == 1
        assert registry.live()[0].target.session_id == "abc"
        assert registry.live()[0].first_seen == NOW

    def test_a_tick_with_only_process_evidence_does_not_un_name_it(self) -> None:
        """The daemon went away; the Session did not. `None` never overwrites an id."""
        registry = SessionRegistry()
        registry.observe(AgentKind.CODEX, seeing(codex_row(session_id="abc", pid=10)), now=NOW)
        registry.observe(
            AgentKind.CODEX,
            seeing(
                codex_row(session_id=None, pid=10, state=SessionState.IDLE),
                degraded="shared daemon absent",
            ),
            now=NOW + 5,
        )

        assert len(registry.all()) == 1
        held = registry.live()[0]
        assert held.target.session_id == "abc"
        assert held.state is SessionState.IDLE  # the rest of the weaker reading still lands

    def test_a_new_thread_in_the_same_tui_replaces_the_id_without_moving_the_row(self) -> None:
        """`/new` keeps the process and changes the thread. Whoever read an id decides."""
        registry = SessionRegistry()
        registry.observe(AgentKind.CODEX, seeing(codex_row(session_id="abc", pid=10)), now=NOW)
        registry.observe(AgentKind.CODEX, seeing(codex_row(session_id="xyz", pid=10)), now=NOW + 5)

        assert len(registry.all()) == 1
        assert registry.live()[0].target.session_id == "xyz"
        assert registry.live()[0].first_seen == NOW

    def test_a_cleared_claude_session_ends_and_a_new_row_starts(self) -> None:
        """`/clear` is a new session id under the same process — a new Session (#79).

        Decided rather than inherited: before #79 the pid join re-keyed the held
        row and carried its `first_seen` across, so a Session the user had just
        cleared kept the age, and could have kept the name, of the conversation
        it replaced. The join is gone for Claude, and this is the shape that
        replaced it — the old row ends once (so a surface can still say what
        happened to it) and the new one starts from now.

        **No legacy behaviour to cite**: gen-1 registered a Session from its
        `SessionStart` hook and had no notion of one session id succeeding
        another under one process, so there is nothing here that was ported or
        dropped. Advisor-approved on 2026-08-27 with the consequence read:
        Relays queued for the cleared id are answered by `relays.session_ended`
        with honest failure receipts, which is correct, because that
        conversation is gone.
        """
        registry = SessionRegistry()
        registry.observe(AgentKind.CLAUDE, seeing(claude_row(session_id="def", pid=20)), now=NOW)
        registry.observe(
            AgentKind.CLAUDE, seeing(claude_row(session_id="ghi", pid=20)), now=NOW + 5
        )

        held = {row.target.session_id: row for row in registry.all()}
        assert held["def"].lifecycle is SessionLifecycle.ENDED
        assert held["ghi"].lifecycle is SessionLifecycle.LIVE
        assert held["ghi"].first_seen == NOW + 5

    def test_two_claude_processes_under_one_session_id_stay_two_rows(self) -> None:
        """`--resume` forks, and the fork is a Session of its own."""
        registry = SessionRegistry()
        registry.observe(
            AgentKind.CLAUDE,
            seeing(claude_row(session_id="def", pid=20), claude_row(session_id="def", pid=21)),
            now=NOW,
        )

        assert sorted(held.target.pid or 0 for held in registry.live()) == [20, 21]

    def test_a_child_never_takes_over_its_parents_row(self) -> None:
        """#79: a Claude Child Process runs **inside** its parent's process.

        A Task subagent is not a process of its own, so its row carries its
        parent's pid — which is the honest address and also the exact shape this
        join was built to collapse. Left to collapse it, the child's reading
        would replace the parent's row: one tick later the user's own Session
        has become an unrelayable child, under a target nothing can address, and
        `_better_known` would have logged it as the process moving threads.

        The pid join exists for a Session that gains or changes its *own* id
        (`codex` at its first turn, and `/new`), and in both of those the two
        readings are the same kind of thing. Two readings that disagree about
        whether they are a Session at all are not about one row.
        """
        registry = SessionRegistry()
        parent = SessionTarget(agent=AgentKind.CLAUDE, session_id="def", pid=20)
        registry.observe(
            AgentKind.CLAUDE,
            seeing(
                claude_row(session_id="def", pid=20),
                claude_row(
                    session_id="a891a18f447827175",
                    pid=20,
                    child=ChildClassification(kind=ChildKind.CHILD, parent=parent),
                ),
            ),
            now=NOW,
        )

        held = {row.target.session_id: row.child.kind for row in registry.live()}
        assert held == {"def": ChildKind.MAIN, "a891a18f447827175": ChildKind.CHILD}

    def test_and_a_parent_never_takes_over_a_childs_row(self) -> None:
        """The same refusal read from the other end, because ordering is not promised.

        Nothing says which of the two a lane lists first, and a rule that only
        held in one direction would hold or not hold by accident.
        """
        registry = SessionRegistry()
        parent = SessionTarget(agent=AgentKind.CLAUDE, session_id="def", pid=20)
        registry.observe(
            AgentKind.CLAUDE,
            seeing(
                claude_row(
                    session_id="a891a18f447827175",
                    pid=20,
                    child=ChildClassification(kind=ChildKind.CHILD, parent=parent),
                ),
                claude_row(session_id="def", pid=20),
            ),
            now=NOW,
        )

        assert len(registry.live()) == 2

    def test_two_children_of_one_session_stay_two_rows(self) -> None:
        """They share a pid with each other as well as with their parent."""
        registry = SessionRegistry()
        parent = SessionTarget(agent=AgentKind.CLAUDE, session_id="def", pid=20)
        spawned = ChildClassification(kind=ChildKind.CHILD, parent=parent)
        registry.observe(
            AgentKind.CLAUDE,
            seeing(
                claude_row(session_id="a1", pid=20, child=spawned),
                claude_row(session_id="a2", pid=20, child=spawned),
            ),
            now=NOW,
        )

        assert sorted(row.target.session_id or "" for row in registry.live()) == ["a1", "a2"]

    def test_a_daemon_thread_with_no_process_is_keyed_by_its_id(self) -> None:
        registry = SessionRegistry()
        registry.observe(AgentKind.CODEX, seeing(codex_row(session_id="abc", pid=None)), now=NOW)
        registry.observe(AgentKind.CODEX, seeing(codex_row(session_id="abc", pid=None)), now=NOW)

        assert len(registry.all()) == 1


class TestASessionThatStoppedBeingSeen:
    def test_it_ends_once_then_is_forgotten(self) -> None:
        registry = SessionRegistry()
        registry.observe(AgentKind.CODEX, seeing(codex_row(session_id="abc", pid=10)), now=NOW)

        registry.observe(AgentKind.CODEX, seeing(), now=NOW + 5)
        assert registry.all()[0].lifecycle is SessionLifecycle.ENDED
        assert registry.live() == ()

        registry.observe(AgentKind.CODEX, seeing(), now=NOW + 10)
        assert registry.all() == ()

    def test_an_empty_enumeration_is_an_answer_even_from_a_weaker_source(self) -> None:
        """Otherwise a row that really went away is offered as a target forever."""
        registry = SessionRegistry()
        registry.observe(AgentKind.CODEX, seeing(codex_row(session_id="abc", pid=10)), now=NOW)
        registry.observe(AgentKind.CODEX, seeing(degraded="no daemon"), now=NOW + 5)

        assert registry.all()[0].lifecycle is SessionLifecycle.ENDED

    def test_the_other_lane_is_untouched(self) -> None:
        registry = SessionRegistry()
        registry.observe(AgentKind.CODEX, seeing(codex_row(session_id="abc", pid=10)), now=NOW)
        registry.observe(AgentKind.CLAUDE, seeing(claude_row(session_id="def", pid=20)), now=NOW)

        registry.observe(AgentKind.CODEX, seeing(), now=NOW + 5)

        assert [held.target.agent for held in registry.live()] == [AgentKind.CLAUDE]


class TestALaneThatCouldNotLook:
    def test_its_rows_are_left_exactly_as_they_were(self) -> None:
        registry = SessionRegistry()
        registry.observe(AgentKind.CODEX, seeing(codex_row(session_id="abc", pid=10)), now=NOW)

        registry.observe(AgentKind.CODEX, LaneDiscovery(error="no daemon socket"), now=NOW + 5)

        assert registry.live()[0].target.session_id == "abc"

    def test_status_can_say_which_lane_is_unavailable_and_why(self) -> None:
        registry = SessionRegistry()
        registry.observe(AgentKind.CODEX, LaneDiscovery(error="no daemon socket"), now=NOW)

        assert registry.lane_errors() == {AgentKind.CODEX: "no daemon socket"}

    def test_a_lane_that_comes_back_stops_being_reported_as_unavailable(self) -> None:
        registry = SessionRegistry()
        registry.observe(AgentKind.CODEX, LaneDiscovery(error="no daemon socket"), now=NOW)
        registry.observe(AgentKind.CODEX, seeing(), now=NOW + 5)

        assert registry.lane_errors() == {}

    def test_status_can_also_say_a_lane_is_running_on_weaker_evidence(self) -> None:
        """Which is the Codex lane's ordinary state until #83 installs the daemon."""
        registry = SessionRegistry()
        registry.observe(
            AgentKind.CODEX,
            seeing(codex_row(session_id=None, pid=10), degraded="shared daemon absent"),
            now=NOW,
        )

        assert registry.lane_degradations() == {AgentKind.CODEX: "shared daemon absent"}
        assert registry.lane_errors() == {}

    def test_a_lane_that_gets_its_daemon_back_stops_being_reported_as_degraded(self) -> None:
        registry = SessionRegistry()
        registry.observe(
            AgentKind.CODEX,
            seeing(codex_row(session_id=None, pid=10), degraded="shared daemon absent"),
            now=NOW,
        )
        registry.observe(AgentKind.CODEX, seeing(codex_row(session_id="abc", pid=10)), now=NOW + 5)

        assert registry.lane_degradations() == {}

    def test_a_lane_that_could_not_look_says_nothing_about_how_well_it_reads(self) -> None:
        """`error` and `degraded` are different news and never collapse into one."""
        registry = SessionRegistry()
        registry.observe(
            AgentKind.CODEX,
            seeing(codex_row(session_id=None, pid=10), degraded="shared daemon absent"),
            now=NOW,
        )
        registry.observe(AgentKind.CODEX, LaneDiscovery(error="the process table is shut"), now=NOW)

        assert registry.lane_errors() == {AgentKind.CODEX: "the process table is shut"}
        assert registry.lane_degradations() == {AgentKind.CODEX: "shared daemon absent"}

    def test_an_unreadable_progress_source_keeps_the_last_observation(self) -> None:
        registry = SessionRegistry()
        observed = ProgressObservation.readable(
            has_history=True,
            recent=(ProgressEntry(role=ProgressRole.ASSISTANT, text="done"),),
            omission=ProgressOmission.NONE,
            read_at=datetime.fromtimestamp(NOW, UTC),
        )
        registry.observe(
            AgentKind.CODEX,
            seeing(codex_row(session_id="abc", pid=10, progress=observed)),
            now=NOW,
        )

        registry.observe(
            AgentKind.CODEX,
            seeing(
                codex_row(
                    session_id="abc",
                    pid=10,
                    progress=ProgressObservation.unreadable("the daemon dropped the read"),
                ),
                degraded="the daemon dropped the read",
            ),
            now=NOW + 5,
        )

        held = registry.live()[0].progress
        assert held == observed
        assert held.availability is ProgressAvailability.READABLE
        assert registry.lane_degradations() == {AgentKind.CODEX: "the daemon dropped the read"}


class TestTheNameARowKeeps:
    """#78, amended by Simon on #113: composed once, and changed only by a rename.

    The rule the reference implementation enforced in its store — first write
    wins, an exact repeat is a no-op, a different one is refused
    (`legacy@1d32845:bridge/store.py:1875-1902`) — moved here, where the writes
    now come from: a lane composing a name off every reading, five seconds
    apart, forever. **Adapted** on #113: a different name is now taken rather
    than refused, because the source it comes from changed character. Legacy's
    was a one-shot self-report, so a second one was a contradiction; a lane's
    `SessionInspection.name` is composed from the agent's *official* name for the
    Session and nothing else (`adapters/agent/_naming.py`), so a second one is
    the agent renaming its own Session.

    What made the amendment necessary is on the Codex lane: codex 0.150.0 names
    a thread with the first 36 characters of the user's first message and then
    replaces that with a generated title (#113, measured; the delay is not
    measured and is observed on #80's run of record).
    Frozen, the product kept the fragment for good.
    """

    def test_the_first_name_a_lane_composes_is_taken(self) -> None:
        registry = SessionRegistry()
        registry.observe(
            AgentKind.CODEX,
            seeing(codex_row(session_id="abc", pid=10, name=named("a task"))),
            now=NOW,
        )

        assert str(registry.live()[0].name) == "GPT-VoiceCoding · a task"

    def test_a_row_seen_unnamed_takes_the_name_that_arrives_later(self) -> None:
        """Filling an empty name is not changing one; an unnamed row is ordinary."""
        registry = SessionRegistry()
        registry.observe(AgentKind.CODEX, seeing(codex_row(session_id="abc", pid=10)), now=NOW)
        assert registry.live()[0].name is None

        registry.observe(
            AgentKind.CODEX,
            seeing(codex_row(session_id="abc", pid=10, name=named("a task"))),
            now=NOW + 5,
        )

        assert str(registry.live()[0].name) == "GPT-VoiceCoding · a task"

    def test_a_rename_by_the_lanes_source_is_followed(self) -> None:
        """#113: the agent renamed its own Session, and the roster says so."""
        registry = SessionRegistry()
        registry.observe(
            AgentKind.CODEX,
            seeing(codex_row(session_id="abc", pid=10, name=named("a task"))),
            now=NOW,
        )
        registry.observe(
            AgentKind.CODEX,
            seeing(codex_row(session_id="abc", pid=10, name=named("something else"))),
            now=NOW + 5,
        )

        assert str(registry.live()[0].name) == "GPT-VoiceCoding · something else"

    def test_the_name_it_was_renamed_from_stops_addressing_it(self) -> None:
        """There is one name at a time, and the old one refuses with the existing error."""
        registry = SessionRegistry()
        registry.observe(
            AgentKind.CODEX,
            seeing(codex_row(session_id="abc", pid=10, name=named("a task"))),
            now=NOW,
        )
        registry.observe(
            AgentKind.CODEX,
            seeing(codex_row(session_id="abc", pid=10, name=named("something else"))),
            now=NOW + 5,
        )

        assert registry.match_name("something else").target.session_id == "abc"
        with pytest.raises(NoNameMatchError):
            registry.match_name("a task")

    def test_a_rename_is_said_once_and_not_once_a_tick(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The held name becomes the new one, so the next reading has nothing to say."""
        registry = SessionRegistry()
        registry.observe(
            AgentKind.CODEX,
            seeing(codex_row(session_id="abc", pid=10, name=named("a task"))),
            now=NOW,
        )
        renamed = seeing(codex_row(session_id="abc", pid=10, name=named("something else")))
        with caplog.at_level(logging.INFO, logger=sessions_module.__name__):
            registry.observe(AgentKind.CODEX, renamed, now=NOW + 5)
            registry.observe(AgentKind.CODEX, renamed, now=NOW + 10)

        said = [record.getMessage() for record in caplog.records if "is now called" in record.msg]
        assert len(said) == 1
        assert said[0].endswith(
            "is now called GPT-VoiceCoding · something else by its lane; "
            "it was GPT-VoiceCoding · a task"
        )

    def test_the_project_half_moving_on_its_own_is_not_a_rename(self) -> None:
        """Only the task half is the agent's. The project half is this side's `git`.

        `_project.ProjectNames` resolves it by running `git` against the
        workspace, so it moves for reasons that are not renames — a `git` that
        answered once and failed the next tick, a workspace that becomes a
        repository under a Session already running in it. CONTEXT.md says
        nothing but the official source may move a Session Name, and this half
        is not it.
        """
        registry = SessionRegistry()
        registry.observe(
            AgentKind.CODEX,
            seeing(codex_row(session_id="abc", pid=10, name=named("a task"))),
            now=NOW,
        )
        registry.observe(
            AgentKind.CODEX,
            seeing(
                codex_row(
                    session_id="abc",
                    pid=10,
                    name=SessionName(project="somewhere-else", task="a task"),
                )
            ),
            now=NOW + 5,
        )

        assert str(registry.live()[0].name) == "GPT-VoiceCoding · a task"

    def test_a_stale_name_the_new_one_contains_still_reaches_that_session(self) -> None:
        """Renamed `ship` → `ship it`, the old name is a fragment of the new one.

        `match_name` matches fragments and always has (its own docstring is the
        record of why), so the old name is not refused here — it resolves, to the
        very Session it used to name. That is the fragment rule doing its job:
        the name it once had cannot reach anything else, because nothing else
        answers to it.
        """
        registry = SessionRegistry()
        registry.observe(
            AgentKind.CODEX,
            seeing(codex_row(session_id="abc", pid=10, name=named("ship"))),
            now=NOW,
        )
        registry.observe(
            AgentKind.CODEX,
            seeing(codex_row(session_id="abc", pid=10, name=named("ship it"))),
            now=NOW + 5,
        )

        assert str(registry.live()[0].name) == "GPT-VoiceCoding · ship it"
        assert registry.match_name("GPT-VoiceCoding · ship").target.session_id == "abc"

    def test_a_reading_that_states_no_name_does_not_erase_the_one_it_has(self) -> None:
        """A degraded Codex pass names none of its rows, and that is not a rename."""
        registry = SessionRegistry()
        registry.observe(
            AgentKind.CODEX,
            seeing(codex_row(session_id="abc", pid=10, name=named("a task"))),
            now=NOW,
        )
        registry.observe(AgentKind.CODEX, seeing(codex_row(session_id="abc", pid=10)), now=NOW + 5)

        assert str(registry.live()[0].name) == "GPT-VoiceCoding · a task"

    def test_a_codex_row_that_gains_its_thread_id_is_named_for_it(self) -> None:
        """A new exact identity re-composes: #73's row had no id to be named from."""
        registry = SessionRegistry()
        registry.observe(AgentKind.CODEX, seeing(codex_row(session_id=None, pid=10)), now=NOW)
        registry.observe(
            AgentKind.CODEX,
            seeing(codex_row(session_id="abc", pid=10, name=named("abc12345"))),
            now=NOW + 5,
        )

        assert str(registry.live()[0].name) == "GPT-VoiceCoding · abc12345"

    def test_a_new_thread_under_the_same_pid_is_named_for_the_new_thread(self) -> None:
        """`/new` in that TUI (#77): same process, different Session, different name."""
        registry = SessionRegistry()
        registry.observe(
            AgentKind.CODEX,
            seeing(codex_row(session_id="abc", pid=10, name=named("the first thread"))),
            now=NOW,
        )
        registry.observe(
            AgentKind.CODEX,
            seeing(codex_row(session_id="def", pid=10, name=named("the second thread"))),
            now=NOW + 5,
        )

        assert str(registry.live()[0].name) == "GPT-VoiceCoding · the second thread"

    def test_a_child_process_is_listed_and_never_named(self) -> None:
        """#78's table risk 1: a name the user could say and nothing could answer."""
        registry = SessionRegistry()
        registry.observe(
            AgentKind.CODEX,
            seeing(
                codex_row(
                    session_id="abc",
                    pid=10,
                    name=named("a task"),
                    child=ChildClassification(kind=ChildKind.CHILD, parent="the parent"),
                )
            ),
            now=NOW,
        )

        assert len(registry.live()) == 1
        assert registry.live()[0].name is None

    def test_a_child_seen_again_is_still_not_named(self) -> None:
        registry = SessionRegistry()
        child = ChildClassification(kind=ChildKind.CHILD, parent="the parent")
        for tick in (NOW, NOW + 5):
            registry.observe(
                AgentKind.CODEX,
                seeing(codex_row(session_id="abc", pid=10, name=named("a task"), child=child)),
                now=tick,
            )

        assert registry.live()[0].name is None

    def test_a_tick_with_only_process_evidence_keeps_the_name(self) -> None:
        """The identity did not change, so neither does the name it was given."""
        registry = SessionRegistry()
        registry.observe(
            AgentKind.CODEX,
            seeing(codex_row(session_id="abc", pid=10, name=named("a task"))),
            now=NOW,
        )
        registry.observe(AgentKind.CODEX, seeing(codex_row(session_id=None, pid=10)), now=NOW + 5)

        assert str(registry.live()[0].name) == "GPT-VoiceCoding · a task"


class TestWhatTheRowCarriesAcrossReadings:
    def test_the_state_a_lane_read_replaces_the_one_held(self) -> None:
        registry = SessionRegistry()
        registry.observe(
            AgentKind.CODEX,
            seeing(codex_row(session_id="abc", pid=10, state=SessionState.IDLE)),
            now=NOW,
        )
        registry.observe(
            AgentKind.CODEX,
            seeing(codex_row(session_id="abc", pid=10, state=SessionState.RUNNING)),
            now=NOW + 5,
        )

        assert registry.live()[0].state is SessionState.RUNNING


class TestWhatObserveReportsAsEnded:
    """The registry is the only place that knows a re-keyed row is not a dead one.

    `BridgeCore.discover` has to tell the user when a Session goes, and the news
    it sends is not free: it terminates every Relay queued for that target. So
    "which rows ended" cannot be recovered by diffing targets outside this class
    — the Codex row that gains its thread id at its first turn changes target
    without anything having ended, and that is the ordinary path (#73), not an
    edge.
    """

    def test_a_row_that_stopped_being_seen_is_reported_once(self) -> None:
        registry = SessionRegistry()
        registry.observe(AgentKind.CODEX, seeing(codex_row(session_id="abc", pid=10)), now=NOW)

        gone = registry.observe(AgentKind.CODEX, seeing(), now=NOW + 5)

        assert [target.session_id for target in gone] == ["abc"]

    def test_and_not_again_when_it_is_forgotten(self) -> None:
        registry = SessionRegistry()
        registry.observe(AgentKind.CODEX, seeing(codex_row(session_id="abc", pid=10)), now=NOW)
        registry.observe(AgentKind.CODEX, seeing(), now=NOW + 5)

        assert registry.observe(AgentKind.CODEX, seeing(), now=NOW + 10) == ()

    def test_a_session_learning_its_own_id_has_not_ended(self) -> None:
        """The measured Codex path: no id until the first turn, then one."""
        registry = SessionRegistry()
        registry.observe(AgentKind.CODEX, seeing(codex_row(session_id=None, pid=10)), now=NOW)

        gone = registry.observe(
            AgentKind.CODEX, seeing(codex_row(session_id="abc", pid=10)), now=NOW + 5
        )

        assert gone == ()

    def test_nor_has_one_whose_tui_was_given_a_new_thread(self) -> None:
        """`/new`: the thread is over, the Session the user is sitting in is not."""
        registry = SessionRegistry()
        registry.observe(AgentKind.CODEX, seeing(codex_row(session_id="abc", pid=10)), now=NOW)

        gone = registry.observe(
            AgentKind.CODEX, seeing(codex_row(session_id="xyz", pid=10)), now=NOW + 5
        )

        assert gone == ()

    def test_a_lane_that_could_not_look_ends_nothing(self) -> None:
        registry = SessionRegistry()
        registry.observe(AgentKind.CODEX, seeing(codex_row(session_id="abc", pid=10)), now=NOW)

        assert registry.observe(AgentKind.CODEX, seeing(error="no daemon"), now=NOW + 5) == ()

    def test_one_lane_going_quiet_does_not_end_the_others_rows(self) -> None:
        registry = SessionRegistry()
        registry.observe(AgentKind.CLAUDE, seeing(claude_row(session_id="c", pid=1)), now=NOW)
        registry.observe(AgentKind.CODEX, seeing(codex_row(session_id="abc", pid=10)), now=NOW)

        gone = registry.observe(AgentKind.CODEX, seeing(), now=NOW + 5)

        assert [target.session_id for target in gone] == ["abc"]

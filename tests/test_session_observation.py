"""Rows arrive by observation, and one process stays one row while it does.

The registry's hardest job is not holding Sessions — it is deciding that two
readings, taken seconds apart by two different sources, are about the same
Session. Every case here is one this product actually meets: a `codex` that has
not been named yet, a shared daemon that goes away mid-run, a `/new` typed into
a TUI, and a lane that cannot look at all.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from gpt_voicecoding.core.sessions import SessionRegistry
from gpt_voicecoding.seams.agent import (
    LaneDiscovery,
    SessionInspection,
    SessionLifecycle,
    SessionState,
)
from gpt_voicecoding.seams.identity import AgentKind, SessionTarget

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

    def test_two_claude_processes_under_one_session_id_stay_two_rows(self) -> None:
        """`--resume` forks, and the fork is a Session of its own."""
        registry = SessionRegistry()
        registry.observe(
            AgentKind.CLAUDE,
            seeing(claude_row(session_id="def", pid=20), claude_row(session_id="def", pid=21)),
            now=NOW,
        )

        assert sorted(held.target.pid or 0 for held in registry.live()) == [20, 21]

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


class TestWhatTheRowCarriesAcrossReadings:
    def test_the_users_own_name_for_it_is_not_overwritten_by_a_lane(self) -> None:
        """A lane knows the agent's name for a Session; it does not know the user's."""
        from gpt_voicecoding.seams.identity import SessionLabel

        registry = SessionRegistry()
        registry.observe(AgentKind.CODEX, seeing(codex_row(session_id="abc", pid=10)), now=NOW)
        target = registry.live()[0].target
        registry._sessions[target] = replace(  # noqa: SLF001 - #78 owns the public path
            registry._sessions[target], label=SessionLabel("GPT-VoiceCoding", "a task")
        )

        registry.observe(
            AgentKind.CODEX,
            seeing(codex_row(session_id="abc", pid=10, name="something-else")),
            now=NOW + 5,
        )

        assert str(registry.live()[0].label) == "GPT-VoiceCoding · a task"
        assert registry.live()[0].name == "something-else"

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

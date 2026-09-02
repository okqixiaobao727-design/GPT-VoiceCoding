"""The Session registry — Bridge Core state, deliberately not a module.

The behaviours under test are the ones the reference implementation got wrong or
left to prose: a target is exact or it is refused, a Session Name disambiguates or asks,
and a stale identity fails closed rather than resolving to something plausible.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from gpt_voicecoding.core.errors import (
    AmbiguousNameError,
    ChildSessionError,
    DuplicateSessionError,
    NoNameMatchError,
    StaleSessionError,
    UnknownSessionError,
)
from gpt_voicecoding.core.sessions import Session, SessionRegistry
from gpt_voicecoding.seams.agent import (
    ChildClassification,
    ChildKind,
    LaneDiscovery,
    ReplyWindow,
    SessionInspection,
    SessionLifecycle,
    SessionState,
    WaitingFor,
    WaitingKind,
    derive_reply_window,
)
from gpt_voicecoding.seams.identity import AgentKind, SessionName, SessionTarget

WORKSPACE = Path(__file__).resolve().parents[1]


def claude(session_id: str, pid: int, task: str = "a task") -> Session:
    return Session(
        target=SessionTarget(agent=AgentKind.CLAUDE, session_id=session_id, pid=pid),
        name=SessionName("GPT-VoiceCoding", task),
        workspace=WORKSPACE,
        first_seen=1_000.0,
    )


def codex(session_id: str, task: str = "a task") -> Session:
    return Session(
        target=SessionTarget(agent=AgentKind.CODEX, session_id=session_id),
        name=SessionName("GPT-VoiceCoding", task),
        workspace=WORKSPACE,
        first_seen=1_000.0,
    )


class TestRegistering:
    def test_a_registered_session_resolves_by_its_exact_target(self) -> None:
        registry = SessionRegistry()
        session = codex("abc")
        registry.register(session)
        assert registry.resolve(session.target) == session

    def test_registering_the_same_target_twice_is_refused(self) -> None:
        registry = SessionRegistry()
        registry.register(codex("abc"))
        with pytest.raises(DuplicateSessionError):
            registry.register(codex("abc"))

    def test_a_resumed_claude_session_forks_a_second_pid_under_one_session_id(self) -> None:
        """Both are live, and they are two different Sessions."""
        registry = SessionRegistry()
        registry.register(claude("abc", pid=100))
        registry.register(claude("abc", pid=101))
        assert len(registry.live()) == 2

    def test_a_non_pid_agent_may_not_hold_one_session_id_twice(self) -> None:
        registry = SessionRegistry()
        registry.register(codex("abc"))
        with pytest.raises(DuplicateSessionError):
            registry.register(codex("abc", task="another task"))


class TestResolving:
    def test_an_unknown_session_id_fails_closed(self) -> None:
        registry = SessionRegistry()
        with pytest.raises(UnknownSessionError):
            registry.resolve(SessionTarget(agent=AgentKind.CODEX, session_id="nope"))

    def test_the_wrong_pid_under_a_known_session_id_is_stale_not_unknown(self) -> None:
        """The distinction is load-bearing: one is a typo, the other is a fork."""
        registry = SessionRegistry()
        registry.register(claude("abc", pid=100))
        with pytest.raises(StaleSessionError) as raised:
            registry.resolve(SessionTarget(agent=AgentKind.CLAUDE, session_id="abc", pid=999))
        assert 100 in raised.value.live_pids

    def test_a_fork_resolves_to_the_pid_that_was_asked_for(self) -> None:
        registry = SessionRegistry()
        registry.register(claude("abc", pid=100))
        registry.register(claude("abc", pid=101))
        resolved = registry.resolve(
            SessionTarget(agent=AgentKind.CLAUDE, session_id="abc", pid=101)
        )
        assert resolved.target.pid == 101

    def test_an_ended_session_fails_closed_rather_than_resolving(self) -> None:
        registry = SessionRegistry()
        session = codex("abc")
        registry.register(session)
        registry.mark_ended(session.target)
        with pytest.raises(StaleSessionError):
            registry.resolve(session.target)

    def test_a_forgotten_session_is_unknown_again(self) -> None:
        registry = SessionRegistry()
        session = codex("abc")
        registry.register(session)
        registry.forget(session.target)
        with pytest.raises(UnknownSessionError):
            registry.resolve(session.target)

    def test_a_pid_carried_on_a_codex_target_does_not_change_the_answer(self) -> None:
        """Only agents addressed by pid are matched on it."""
        registry = SessionRegistry()
        registry.register(codex("abc"))
        resolved = registry.resolve(SessionTarget(agent=AgentKind.CODEX, session_id="abc", pid=777))
        assert resolved.target.pid is None


class TestRefusingAChildProcess:
    """Seen, not spoken to — refused here rather than remembered by a caller (#79).

    Structural on purpose: a crew's reviewer answering a question meant for the
    Session that spawned it is the user's own words landing under somebody
    else's authority, and a rule each caller had to remember is a rule one
    caller will forget.
    """

    def spawned(self, agent_id: str = "a891a18f447827175") -> Session:
        return Session(
            target=SessionTarget(agent=AgentKind.CLAUDE, session_id=agent_id, pid=9231),
            workspace=WORKSPACE,
            first_seen=1_000.0,
            child=ChildClassification(
                kind=ChildKind.CHILD,
                parent=SessionTarget(agent=AgentKind.CLAUDE, session_id="parent", pid=9231),
            ),
        )

    def test_a_child_is_refused_as_a_target(self) -> None:
        registry = SessionRegistry()
        child = self.spawned()
        registry.register(child)
        with pytest.raises(ChildSessionError):
            registry.resolve(child.target)

    def test_the_refusal_names_the_child_in_the_words_a_surface_typed(self) -> None:
        """The acceptance reads this sentence and looks for the address in it.

        A non-zero exit is not by itself a refusal — the surface exits non-zero
        for an engine that never answered too — so the address is what proves
        the rule was applied rather than the call merely failing.
        """
        registry = SessionRegistry()
        child = self.spawned()
        registry.register(child)
        with pytest.raises(ChildSessionError) as raised:
            registry.resolve(child.target)
        assert "claude:a891a18f447827175:9231" in str(raised.value)

    def test_it_names_the_session_that_spawned_it_too(self) -> None:
        registry = SessionRegistry()
        child = self.spawned()
        registry.register(child)
        with pytest.raises(ChildSessionError) as raised:
            registry.resolve(child.target)
        assert "claude:parent:9231" in str(raised.value)
        assert raised.value.parent == child.child.parent

    def test_it_is_still_listed(self) -> None:
        """The whole difference from the reference implementation, in one line."""
        registry = SessionRegistry()
        registry.register(self.spawned())
        assert len(registry.live()) == 1

    def test_a_spoken_name_never_finds_one(self) -> None:
        """It has no name to be found by, and the roster is searched anyway."""
        registry = SessionRegistry()
        registry.register(self.spawned())
        with pytest.raises(NoNameMatchError):
            registry.match_name("a891")

    def test_it_can_still_be_recorded_as_ended(self) -> None:
        """`resolve` guards addressing; `mark_ended` records what happened.

        Routing this through the refusal would leave the roster claiming a dead
        process is running, which is the one thing worse than listing it.
        """
        registry = SessionRegistry()
        child = self.spawned()
        registry.register(child)
        assert registry.mark_ended(child.target).lifecycle is SessionLifecycle.ENDED


class TestMatchingNames:
    def test_a_name_matches_the_one_session_that_carries_it(self) -> None:
        registry = SessionRegistry()
        session = codex("abc", task="Implement the seam contracts")
        registry.register(session)
        assert registry.match_name("GPT-VoiceCoding · Implement the seam contracts") == session

    def test_a_fragment_matches(self) -> None:
        registry = SessionRegistry()
        session = codex("abc", task="Implement the seam contracts")
        registry.register(session)
        assert registry.match_name("seam contracts") == session

    def test_matching_ignores_case_and_extra_whitespace(self) -> None:
        registry = SessionRegistry()
        session = codex("abc", task="Implement the seam contracts")
        registry.register(session)
        assert registry.match_name("  SEAM   CONTRACTS ") == session

    def test_two_candidates_refuse_rather_than_pick(self) -> None:
        registry = SessionRegistry()
        first = codex("abc", task="Implement the seam contracts")
        second = claude("def", pid=100, task="Implement the seam contracts, part two")
        registry.register(first)
        registry.register(second)
        with pytest.raises(AmbiguousNameError) as raised:
            registry.match_name("seam contracts")
        assert set(raised.value.candidates) == {first, second}

    def test_a_whole_name_that_is_also_a_fragment_of_another_still_refuses(self) -> None:
        """ "ship it" names both "ship it" and "ship it later". Ask, do not prefer."""
        registry = SessionRegistry()
        exact = codex("abc", task="ship it")
        longer = claude("def", pid=100, task="ship it later")
        registry.register(exact)
        registry.register(longer)
        with pytest.raises(AmbiguousNameError) as raised:
            registry.match_name("GPT-VoiceCoding · ship it")
        assert set(raised.value.candidates) == {exact, longer}

    def test_a_name_shared_by_two_sessions_refuses_even_though_it_is_exact(self) -> None:
        registry = SessionRegistry()
        first = codex("abc", task="ship it")
        second = claude("def", pid=100, task="ship it")
        registry.register(first)
        registry.register(second)
        with pytest.raises(AmbiguousNameError):
            registry.match_name("GPT-VoiceCoding · ship it")

    def test_no_match_fails_closed(self) -> None:
        registry = SessionRegistry()
        registry.register(codex("abc"))
        with pytest.raises(NoNameMatchError):
            registry.match_name("something else entirely")

    def test_an_ended_session_is_not_a_candidate(self) -> None:
        registry = SessionRegistry()
        ended = codex("abc", task="Implement the seam contracts")
        registry.register(ended)
        registry.mark_ended(ended.target)
        with pytest.raises(NoNameMatchError):
            registry.match_name("seam contracts")

    def test_a_match_returns_a_session_never_a_target_built_from_the_name(self) -> None:
        registry = SessionRegistry()
        session = codex("abc", task="Implement the seam contracts")
        registry.register(session)
        assert registry.match_name("seam contracts").target == session.target


class TestReplyWindow:
    def test_a_held_question_is_open_only_while_the_lane_can_answer_it(self) -> None:
        question = WaitingFor(kind=WaitingKind.QUESTION, prompt="Which layout?")

        assert (
            derive_reply_window(
                SessionState.WAITING,
                question,
                ChildClassification(),
                question_answerable=True,
            )
            is ReplyWindow.OPEN
        )
        assert (
            derive_reply_window(
                SessionState.WAITING,
                question,
                ChildClassification(),
                question_answerable=False,
            )
            is ReplyWindow.CLOSED
        )

    def test_a_session_starts_with_its_reply_window_closed(self) -> None:
        registry = SessionRegistry()
        session = codex("abc")
        registry.register(session)
        assert registry.resolve(session.target).reply_window is ReplyWindow.CLOSED

    def test_the_window_follows_what_the_session_is_doing(self) -> None:
        """Derived, never set: one field, so nothing can disagree with it."""
        registry = SessionRegistry()
        session = codex("abc")
        registry.register(session)

        registry.set_state(session.target, SessionState.IDLE)
        assert registry.resolve(session.target).reply_window is ReplyWindow.OPEN

        registry.set_state(session.target, SessionState.RUNNING)
        assert registry.resolve(session.target).reply_window is ReplyWindow.CLOSED

    def test_a_session_waiting_on_a_dialog_is_closed(self) -> None:
        """A dialog on screen blocks every other Relay until it is answered."""
        registry = SessionRegistry()
        session = codex("abc")
        registry.register(session)

        registry.set_state(session.target, SessionState.WAITING)
        assert registry.resolve(session.target).reply_window is ReplyWindow.CLOSED

    def test_a_child_process_is_never_open_however_idle_it_is(self) -> None:
        """Seen, not spoken to (#68) — and the window says so, not just `resolve`."""
        registry = SessionRegistry()
        session = replace(
            codex("abc"),
            state=SessionState.IDLE,
            child=ChildClassification(kind=ChildKind.CHILD),
        )
        registry.register(session)

        assert registry.all()[0].reply_window is ReplyWindow.CLOSED

    def test_setting_the_state_of_an_unknown_session_fails_closed(self) -> None:
        registry = SessionRegistry()
        with pytest.raises(UnknownSessionError):
            registry.set_state(
                SessionTarget(agent=AgentKind.CODEX, session_id="nope"), SessionState.IDLE
            )

    def test_ending_a_session_closes_its_reply_window(self) -> None:
        registry = SessionRegistry()
        session = codex("abc")
        registry.register(session)
        registry.set_state(session.target, SessionState.IDLE)

        ended = registry.mark_ended(session.target)
        assert ended.lifecycle is SessionLifecycle.ENDED
        assert ended.reply_window is ReplyWindow.CLOSED


class TestRoster:
    def test_the_roster_lists_live_sessions_in_registration_order(self) -> None:
        registry = SessionRegistry()
        first = codex("abc")
        second = claude("def", pid=100)
        registry.register(first)
        registry.register(second)
        assert registry.live() == (first, second)

    def test_the_roster_excludes_ended_sessions_but_all_still_holds_them(self) -> None:
        registry = SessionRegistry()
        session = codex("abc")
        registry.register(session)
        registry.mark_ended(session.target)
        assert registry.live() == ()
        assert len(registry.all()) == 1


class TestTheFocusSession:
    """One pointer, cleared by the Session ending and by nothing else (#165 Q2)."""

    def test_the_focus_is_held_as_the_identity_the_roster_addresses_it_by(self) -> None:
        """A surface may write a weaker address than the roster holds."""
        registry = SessionRegistry()
        held = registry.register(claude("abc", pid=100))

        registry.set_focus(SessionTarget(agent=AgentKind.CLAUDE, session_id="abc", pid=100))

        assert registry.focus == held.target

    def test_a_session_the_roster_does_not_hold_leaves_the_focus_where_it_was(self) -> None:
        """A verdict carried for a row that ended is still a verdict carried.

        Refusing here would turn the answer landing into the user being told it
        did not — the opposite of what happened.
        """
        registry = SessionRegistry()
        registry.register(codex("abc"))
        registry.set_focus(SessionTarget(agent=AgentKind.CODEX, session_id="abc"))

        registry.set_focus(SessionTarget(agent=AgentKind.CODEX, session_id="gone"))

        assert registry.focus == SessionTarget(agent=AgentKind.CODEX, session_id="abc")

    def test_the_focus_clears_when_that_session_is_marked_ended(self) -> None:
        registry = SessionRegistry()
        session = registry.register(codex("abc"))
        registry.set_focus(session.target)

        registry.mark_ended(session.target)

        assert registry.focus is None

    def test_the_focus_clears_when_a_discovery_stops_seeing_that_session(self) -> None:
        registry = SessionRegistry()
        session = registry.register(codex("abc"))
        registry.set_focus(session.target)

        registry.observe(AgentKind.CODEX, LaneDiscovery(), now=2_000.0)

        assert registry.focus is None

    def test_the_focus_clears_when_that_session_is_forgotten(self) -> None:
        registry = SessionRegistry()
        session = registry.register(codex("abc"))
        registry.set_focus(session.target)

        registry.forget(session.target)

        assert registry.focus is None

    def test_another_session_ending_leaves_the_focus_alone(self) -> None:
        registry = SessionRegistry()
        focused = registry.register(codex("abc"))
        other = registry.register(codex("def"))
        registry.set_focus(focused.target)

        registry.mark_ended(other.target)

        assert registry.focus == focused.target

    def test_the_focus_does_not_follow_a_new_thread_on_the_same_process(self) -> None:
        """`/new` in a Codex TUI is a different Session under one pid (#77).

        Following it would make the Focus Session one the user has never replied
        to, which is the one way #165 Q2 says it must not be set.
        """
        registry = SessionRegistry()
        held = SessionTarget(agent=AgentKind.CODEX, session_id="abc", pid=6548)
        registry.register(Session(target=held, workspace=WORKSPACE, first_seen=1_000.0))
        registry.set_focus(held)

        registry.observed_one(
            SessionInspection(
                target=SessionTarget(agent=AgentKind.CODEX, session_id="def", pid=6548),
                workspace=WORKSPACE,
            ),
            now=2_000.0,
        )

        assert registry.focus is None

    def test_the_focus_follows_a_codex_row_that_gains_its_thread_id(self) -> None:
        """A better-known identity is the same Session, and nothing ended (#73)."""
        registry = SessionRegistry()
        anonymous = SessionTarget(agent=AgentKind.CODEX, pid=6548)
        registry.register(Session(target=anonymous, workspace=WORKSPACE, first_seen=1_000.0))
        registry.set_focus(anonymous)

        named = SessionTarget(agent=AgentKind.CODEX, session_id="abc", pid=6548)
        registry.observed_one(SessionInspection(target=named, workspace=WORKSPACE), now=2_000.0)

        assert registry.focus == named

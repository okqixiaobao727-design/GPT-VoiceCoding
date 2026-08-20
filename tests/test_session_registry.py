"""The Session registry — Bridge Core state, deliberately not a module.

The behaviours under test are the ones the reference implementation got wrong or
left to prose: a target is exact or it is refused, a label disambiguates or asks,
and a stale identity fails closed rather than resolving to something plausible.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gpt_voicecoding.core.errors import (
    AmbiguousLabelError,
    DuplicateSessionError,
    NoLabelMatchError,
    StaleSessionError,
    UnknownSessionError,
)
from gpt_voicecoding.core.sessions import Session, SessionRegistry, SessionState
from gpt_voicecoding.seams.agent import ReplyWindow
from gpt_voicecoding.seams.identity import AgentKind, SessionLabel, SessionTarget

WORKSPACE = Path(__file__).resolve().parents[1]


def claude(session_id: str, pid: int, task: str = "a task") -> Session:
    return Session(
        target=SessionTarget(agent=AgentKind.CLAUDE, session_id=session_id, pid=pid),
        label=SessionLabel("GPT-VoiceCoding", task),
        workspace=WORKSPACE,
        registered_at=1_000.0,
    )


def codex(session_id: str, task: str = "a task") -> Session:
    return Session(
        target=SessionTarget(agent=AgentKind.CODEX, session_id=session_id),
        label=SessionLabel("GPT-VoiceCoding", task),
        workspace=WORKSPACE,
        registered_at=1_000.0,
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


class TestMatchingLabels:
    def test_a_label_matches_the_one_session_that_carries_it(self) -> None:
        registry = SessionRegistry()
        session = codex("abc", task="Implement the seam contracts")
        registry.register(session)
        assert registry.match_label("GPT-VoiceCoding · Implement the seam contracts") == session

    def test_a_fragment_matches(self) -> None:
        registry = SessionRegistry()
        session = codex("abc", task="Implement the seam contracts")
        registry.register(session)
        assert registry.match_label("seam contracts") == session

    def test_matching_ignores_case_and_extra_whitespace(self) -> None:
        registry = SessionRegistry()
        session = codex("abc", task="Implement the seam contracts")
        registry.register(session)
        assert registry.match_label("  SEAM   CONTRACTS ") == session

    def test_two_candidates_refuse_rather_than_pick(self) -> None:
        registry = SessionRegistry()
        first = codex("abc", task="Implement the seam contracts")
        second = claude("def", pid=100, task="Implement the seam contracts, part two")
        registry.register(first)
        registry.register(second)
        with pytest.raises(AmbiguousLabelError) as raised:
            registry.match_label("seam contracts")
        assert set(raised.value.candidates) == {first, second}

    def test_a_whole_label_that_is_also_a_fragment_of_another_still_refuses(self) -> None:
        """ "ship it" names both "ship it" and "ship it later". Ask, do not prefer."""
        registry = SessionRegistry()
        exact = codex("abc", task="ship it")
        longer = claude("def", pid=100, task="ship it later")
        registry.register(exact)
        registry.register(longer)
        with pytest.raises(AmbiguousLabelError) as raised:
            registry.match_label("GPT-VoiceCoding · ship it")
        assert set(raised.value.candidates) == {exact, longer}

    def test_a_label_shared_by_two_sessions_refuses_even_though_it_is_exact(self) -> None:
        registry = SessionRegistry()
        first = codex("abc", task="ship it")
        second = claude("def", pid=100, task="ship it")
        registry.register(first)
        registry.register(second)
        with pytest.raises(AmbiguousLabelError):
            registry.match_label("GPT-VoiceCoding · ship it")

    def test_no_match_fails_closed(self) -> None:
        registry = SessionRegistry()
        registry.register(codex("abc"))
        with pytest.raises(NoLabelMatchError):
            registry.match_label("something else entirely")

    def test_an_ended_session_is_not_a_candidate(self) -> None:
        registry = SessionRegistry()
        ended = codex("abc", task="Implement the seam contracts")
        registry.register(ended)
        registry.mark_ended(ended.target)
        with pytest.raises(NoLabelMatchError):
            registry.match_label("seam contracts")

    def test_a_match_returns_a_session_never_a_target_built_from_the_label(self) -> None:
        registry = SessionRegistry()
        session = codex("abc", task="Implement the seam contracts")
        registry.register(session)
        assert registry.match_label("seam contracts").target == session.target


class TestReplyWindow:
    def test_a_session_starts_with_its_reply_window_closed(self) -> None:
        registry = SessionRegistry()
        session = codex("abc")
        registry.register(session)
        assert registry.resolve(session.target).reply_window is ReplyWindow.CLOSED

    def test_the_window_can_be_opened_and_closed(self) -> None:
        registry = SessionRegistry()
        session = codex("abc")
        registry.register(session)

        registry.set_reply_window(session.target, ReplyWindow.OPEN)
        assert registry.resolve(session.target).reply_window is ReplyWindow.OPEN

        registry.set_reply_window(session.target, ReplyWindow.CLOSED)
        assert registry.resolve(session.target).reply_window is ReplyWindow.CLOSED

    def test_opening_the_window_on_an_unknown_session_fails_closed(self) -> None:
        registry = SessionRegistry()
        with pytest.raises(UnknownSessionError):
            registry.set_reply_window(
                SessionTarget(agent=AgentKind.CODEX, session_id="nope"), ReplyWindow.OPEN
            )

    def test_ending_a_session_closes_its_reply_window(self) -> None:
        registry = SessionRegistry()
        session = codex("abc")
        registry.register(session)
        registry.set_reply_window(session.target, ReplyWindow.OPEN)

        ended = registry.mark_ended(session.target)
        assert ended.state is SessionState.ENDED
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

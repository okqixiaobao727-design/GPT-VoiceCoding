"""The inbound-text router — classification is Bridge Core's, never the channel's.

The Companion Channel hands up text and no opinion about it. Deciding whether
that text is a control-plane command, an Answer Relay or a delegation happens
here, and anything unknown or ambiguous **fails closed with an honest reply**
rather than being guessed into a command.

The grammar has four forms and all of its vocabulary is injected, because the
command set belongs to the control-plane surface and not to this module:

    /<command> …      a control-plane command
    ><prompt>         a Delegated Turn
    @<label>: words   the user's own words, for the Session that label names
    words             the user's own words, when exactly one Session is live

The collision that has to be got right in both directions: a bare `stop` must
not silently become the control command, and `/stop` must not silently become
text injected into a coding session.
"""

from __future__ import annotations

from pathlib import Path

from gpt_voicecoding.core.router import InboundClass, InboundRouter, TextGrammar
from gpt_voicecoding.core.sessions import Session, SessionRegistry
from gpt_voicecoding.seams.identity import AgentKind, SessionLabel, SessionTarget

CODEX = SessionTarget(agent=AgentKind.CODEX, session_id="abc")
CLAUDE = SessionTarget(agent=AgentKind.CLAUDE, session_id="def", pid=100)

COMMANDS = frozenset({"status", "stop", "switches"})


def registry_of(*sessions: tuple[SessionTarget, str]) -> SessionRegistry:
    registry = SessionRegistry()
    for target, task in sessions:
        registry.register(
            Session(
                target=target,
                label=SessionLabel("GPT-VoiceCoding", task),
                workspace=Path("/tmp/workspace"),
                registered_at=0.0,
            )
        )
    return registry


def router_over(registry: SessionRegistry) -> InboundRouter:
    return InboundRouter(sessions=registry, grammar=TextGrammar(control_commands=COMMANDS))


def router(*sessions: tuple[SessionTarget, str]) -> InboundRouter:
    return router_over(registry_of(*sessions))


class TestControlCommands:
    def test_a_marked_command_is_a_control_command(self) -> None:
        found = router((CODEX, "port the log")).classify("/status")

        assert found.kind is InboundClass.CONTROL
        assert found.command == "status"

    def test_a_command_carries_its_arguments_through_unparsed(self) -> None:
        """The control-plane surface owns the payload schema; this only routes."""
        found = router((CODEX, "port the log")).classify("/stop the log one")

        assert found.command == "stop"
        assert found.text == "the log one"

    def test_asking_for_progress_never_touches_a_session(self) -> None:
        """A progress question is a read, not a Relay."""
        found = router((CODEX, "port the log")).classify("/status")

        assert found.target is None

    def test_a_command_works_with_no_session_running_at_all(self) -> None:
        found = router().classify("/switches")

        assert found.kind is InboundClass.CONTROL

    def test_an_unregistered_command_fails_closed_rather_than_being_guessed(self) -> None:
        found = router((CODEX, "port the log")).classify("/deploy production")

        assert found.kind is InboundClass.UNKNOWN
        assert "deploy" in found.reply

    def test_the_command_set_is_injected_not_known_here(self) -> None:
        empty = InboundRouter(sessions=SessionRegistry(), grammar=TextGrammar())

        assert empty.classify("/status").kind is InboundClass.UNKNOWN


class TestDelegation:
    def test_a_marked_prompt_is_a_delegated_turn(self) -> None:
        found = router((CODEX, "port the log")).classify(">what does ADR 0002 say")

        assert found.kind is InboundClass.DELEGATION
        assert found.text == "what does ADR 0002 say"

    def test_an_empty_delegation_fails_closed(self) -> None:
        found = router((CODEX, "port the log")).classify(">   ")

        assert found.kind is InboundClass.UNKNOWN

    def test_a_delegation_is_never_relayed_into_a_session(self) -> None:
        found = router((CODEX, "port the log")).classify(">summarise the diff")

        assert found.target is None


class TestAddressingASessionByLabel:
    def test_a_labelled_relay_resolves_to_that_session(self) -> None:
        found = router((CODEX, "port the log"), (CLAUDE, "build the shell")).classify(
            "@shell: ship it"
        )

        assert found.kind is InboundClass.ANSWER_RELAY
        assert found.target == CLAUDE
        assert found.text == "ship it"

    def test_a_label_matching_nothing_fails_closed(self) -> None:
        found = router((CODEX, "port the log")).classify("@nothing: ship it")

        assert found.kind is InboundClass.UNKNOWN
        assert found.reply

    def test_a_label_matching_two_sessions_refuses_and_names_both(self) -> None:
        """A label disambiguates or asks — never picks."""
        found = router((CODEX, "port the log"), (CLAUDE, "port the shell")).classify(
            "@port: ship it"
        )

        assert found.kind is InboundClass.UNKNOWN
        assert "port the log" in found.reply
        assert "port the shell" in found.reply

    def test_a_labelled_relay_with_no_words_fails_closed(self) -> None:
        found = router((CODEX, "port the log")).classify("@log:   ")

        assert found.kind is InboundClass.UNKNOWN


class TestBareText:
    def test_bare_text_goes_to_the_one_live_session(self) -> None:
        found = router((CODEX, "port the log")).classify("yes, go ahead")

        assert found.kind is InboundClass.ANSWER_RELAY
        assert found.target == CODEX
        assert found.text == "yes, go ahead"

    def test_bare_text_with_two_live_sessions_asks_which(self) -> None:
        found = router((CODEX, "port the log"), (CLAUDE, "build the shell")).classify(
            "yes, go ahead"
        )

        assert found.kind is InboundClass.UNKNOWN
        assert "port the log" in found.reply
        assert "build the shell" in found.reply

    def test_bare_text_with_nothing_running_says_so(self) -> None:
        found = router().classify("yes, go ahead")

        assert found.kind is InboundClass.UNKNOWN
        assert found.reply

    def test_an_ended_session_is_not_a_candidate(self) -> None:
        registry = registry_of((CODEX, "port the log"), (CLAUDE, "build the shell"))
        registry.mark_ended(CLAUDE)

        assert router_over(registry).classify("yes, go ahead").target == CODEX

    def test_empty_text_is_never_classified_as_anything(self) -> None:
        found = router((CODEX, "port the log")).classify("   ")

        assert found.kind is InboundClass.UNKNOWN


class TestTheCommandWordCollision:
    def test_a_bare_command_word_is_never_guessed_into_a_command(self) -> None:
        """The locked rule: never guessed into a command."""
        found = router((CODEX, "port the log")).classify("stop")

        assert found.kind is InboundClass.UNKNOWN

    def test_the_refusal_offers_both_readings_rather_than_picking_one(self) -> None:
        found = router((CODEX, "port the log")).classify("stop")

        assert "/stop" in found.reply
        assert "port the log" in found.reply

    def test_the_marked_command_is_never_injected_into_a_session(self) -> None:
        """The other direction of the same collision."""
        found = router((CODEX, "port the log")).classify("/stop")

        assert found.kind is InboundClass.CONTROL
        assert found.target is None

    def test_a_command_word_inside_a_sentence_is_ordinary_words(self) -> None:
        """The collision is exact-match only; the guard must not swallow speech."""
        found = router((CODEX, "port the log")).classify("stop after the tests pass")

        assert found.kind is InboundClass.ANSWER_RELAY
        assert found.text == "stop after the tests pass"

    def test_a_labelled_command_word_is_unambiguous_and_goes_through(self) -> None:
        found = router((CODEX, "port the log")).classify("@log: stop")

        assert found.kind is InboundClass.ANSWER_RELAY
        assert found.text == "stop"

    def test_the_collision_only_exists_where_the_word_is_registered(self) -> None:
        found = router((CODEX, "port the log")).classify("continue")

        assert found.kind is InboundClass.ANSWER_RELAY


class TestTheGrammarIsConfiguration:
    def test_the_markers_are_injected_rather_than_baked_in(self) -> None:
        custom = InboundRouter(
            sessions=registry_of((CODEX, "port the log")),
            grammar=TextGrammar(
                control_prefix="!",
                delegate_prefix="~",
                relay_marker="#",
                control_commands=COMMANDS,
            ),
        )

        assert custom.classify("!status").kind is InboundClass.CONTROL
        assert custom.classify("~summarise").kind is InboundClass.DELEGATION
        assert custom.classify("#log: ship it").kind is InboundClass.ANSWER_RELAY
        assert custom.classify("/status").kind is InboundClass.ANSWER_RELAY

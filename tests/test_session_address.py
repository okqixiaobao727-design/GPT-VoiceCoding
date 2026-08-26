"""`agent:session_id[:pid]`, written and read back — including for a Session with no id.

The address is what makes a roster row a *target*: a surface reads it off the
row and hands it straight back as a command. That round trip is the contract,
and it has to survive the one Session shape #73 measured and nothing before it
expected — a `codex` that is running, is listed, and has not been named yet.
"""

from __future__ import annotations

import pytest

from gpt_voicecoding.control_plane.commands import CommandError, format_address, parse_address
from gpt_voicecoding.control_plane.payloads import InvalidPayload, read_target, target_document
from gpt_voicecoding.seams.identity import AgentKind, SessionTarget


def round_trip(target: SessionTarget) -> SessionTarget:
    """Row → address → command → target, the way a surface actually drives it."""
    return read_target({"target": parse_address(format_address(target_document(target)))})


class TestAnOrdinaryAddress:
    def test_a_claude_target_survives_the_round_trip(self) -> None:
        target = SessionTarget(agent=AgentKind.CLAUDE, session_id="d3a776ae", pid=3538)
        assert round_trip(target) == target

    def test_a_named_codex_target_survives_the_round_trip(self) -> None:
        target = SessionTarget(agent=AgentKind.CODEX, session_id="019917", pid=6548)
        assert round_trip(target) == target


class TestASessionThatHasNotBeenNamedYet:
    """`codex` writes the rollout carrying its session id at the first *turn* (#73)."""

    def test_it_writes_out_as_an_address_with_an_empty_id_and_a_pid(self) -> None:
        target = SessionTarget(agent=AgentKind.CODEX, pid=6548)
        assert format_address(target_document(target)) == "codex::6548"

    def test_that_address_reads_back_as_the_same_target(self) -> None:
        target = SessionTarget(agent=AgentKind.CODEX, pid=6548)
        assert round_trip(target) == target

    def test_the_address_is_not_the_degenerate_one_the_acceptance_refuses(self) -> None:
        """`journey.py:403` fails a row whose address ends in `:None`."""
        address = format_address(target_document(SessionTarget(agent=AgentKind.CODEX, pid=6548)))
        assert "<no target>" not in address
        assert not address.endswith(":None")


class TestAnAddressThatNamesNothing:
    def test_an_empty_id_with_no_pid_is_refused(self) -> None:
        with pytest.raises(CommandError):
            parse_address("codex:")

    def test_an_empty_id_and_an_empty_pid_is_refused(self) -> None:
        with pytest.raises(CommandError):
            parse_address("codex::")

    def test_a_payload_naming_neither_is_refused(self) -> None:
        with pytest.raises(InvalidPayload):
            read_target({"target": {"agent": "codex", "session_id": None, "pid": None}})

    def test_an_anonymous_claude_target_is_refused(self) -> None:
        """Its official roster always carries an id, so an unnamed row is a defect."""
        with pytest.raises(InvalidPayload):
            read_target({"target": {"agent": "claude", "session_id": None, "pid": 3538}})

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


class TestATargetSaidOutLoudIsTheSameAddress:
    """A refusal names the Session in the words a surface can hand back (#79).

    Every refusal in `core/errors.py` interpolates the target, and until #79 that
    was the dataclass repr — `SessionTarget(agent=<AgentKind.CLAUDE: 'claude'>,
    …)`. The acceptance's `child` step reads the refusal and looks for the
    Session it refused, and a person reading one wants the thing they typed. So
    `str(target)` is the address, and these assert it is **the** address rather
    than a second spelling that happens to look similar today.
    """

    def test_a_claude_target_says_itself_as_its_address(self) -> None:
        target = SessionTarget(agent=AgentKind.CLAUDE, session_id="d3a776ae", pid=3538)
        assert str(target) == "claude:d3a776ae:3538"

    def test_a_child_process_target_says_itself_as_its_address(self) -> None:
        """The shape #79 puts in the roster: an agent id inside its parent's process."""
        target = SessionTarget(agent=AgentKind.CLAUDE, session_id="a891a18f447827175", pid=9231)
        assert str(target) == "claude:a891a18f447827175:9231"

    def test_a_session_with_no_id_says_the_address_the_control_plane_writes(self) -> None:
        assert str(SessionTarget(agent=AgentKind.CODEX, pid=6548)) == "codex::6548"

    @pytest.mark.parametrize(
        "target",
        [
            SessionTarget(agent=AgentKind.CLAUDE, session_id="d3a776ae", pid=3538),
            SessionTarget(agent=AgentKind.CODEX, session_id="019917", pid=6548),
            SessionTarget(agent=AgentKind.CODEX, session_id="019917"),
            SessionTarget(agent=AgentKind.CODEX, pid=6548),
        ],
    )
    def test_it_agrees_with_the_control_plane_on_every_shape(self, target: SessionTarget) -> None:
        """Two writers of one format is one format only while something says so.

        `format_address` writes the wire document and this writes the type, so
        they cannot be the same function — the control plane never sees a
        `SessionTarget`, only the dict it was rendered into. This is what keeps
        them one format anyway.
        """
        assert str(target) == format_address(target_document(target))

    def test_what_it_says_reads_back_as_itself(self) -> None:
        target = SessionTarget(agent=AgentKind.CLAUDE, session_id="d3a776ae", pid=3538)
        assert read_target({"target": parse_address(str(target))}) == target


class TestThePidIsReadByAskingInt:
    """A spelling test lets `int` raise behind it — the shape #190 fixed on `--before`.

    `str.isdigit` is true of every decimal digit Unicode has, `"²"` among
    them, and it is also true of 4,301 ASCII digits, which `int` refuses under
    CPython's own conversion limit. Both passed the spelling check and reached
    `int`, so an address a surface could have been told about came back as a
    traceback instead.
    """

    @pytest.mark.parametrize(
        "pid",
        ["²", "1" * 4_301, "12.5", "six", "-5", "0"],
        ids=[
            "a superscript digit",
            "past the int-conversion limit",
            "a fraction",
            "a word",
            "a pid below zero",
            "a pid of zero",
        ],
    )
    def test_a_pid_that_is_not_one_is_refused_in_the_words_a_surface_prints(self, pid: str) -> None:
        with pytest.raises(CommandError, match="not a process id"):
            parse_address(f"codex:abc:{pid}")

    def test_a_plain_pid_still_parses(self) -> None:
        assert parse_address("codex:abc:6548") == {
            "agent": "codex",
            "session_id": "abc",
            "pid": 6548,
        }

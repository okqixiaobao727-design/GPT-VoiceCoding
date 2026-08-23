"""What the generated instruction sets owe, and how that is proved.

Coverage is asserted against the ids the generators *emit*, never against the
words they chose. That is the whole point of the catalogue: the prose is free to
be rewritten, translated or restructured, and the suite still fails the moment a
rule stops being carried, moves to a set that was never meant to have it, or
comes back after being dropped.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gpt_voicecoding.control_plane.commands import USAGE
from gpt_voicecoding.core.instructions import (
    ACTION_GIST,
    MAX_VOICE_INSTRUCTION_BYTES,
    RULES,
    VOICE_INSTRUCTION_TOKEN_BUDGET,
    Audience,
    Block,
    ControlPlaneCli,
    InstructionContext,
    InstructionError,
    InstructionSet,
    Rule,
    Section,
    delegated_instructions,
    generate,
    ids_for,
    rules_for,
    voice_instructions,
)
from gpt_voicecoding.core.instructions import catalogue as catalogue_module
from gpt_voicecoding.core.instructions import delegated as delegated_module
from gpt_voicecoding.seams.control_plane import Action

CLI = ControlPlaneCli(
    command=Path("/Applications/GPT-VoiceCoding.app/Contents/MacOS/bridgectl"),
    version="1.4.2",
    socket_path=Path("/tmp/gpt-voicecoding-501/control.sock"),
)
CONTEXT = InstructionContext(cli=CLI, launch_usage=USAGE[Action.LAUNCH])


@pytest.fixture(scope="module")
def instructions():
    return generate(CONTEXT)


class TestCatalogue:
    def test_every_rule_id_is_unique(self) -> None:
        ids = [rule.id for rule in RULES]
        assert len(ids) == len(set(ids))

    def test_every_rule_names_where_it_came_from(self) -> None:
        """Provenance survives migration and later product decisions."""
        for rule in RULES:
            assert rule.source.startswith(("skill/", "issue/")), rule.id

    def test_a_rule_carried_by_code_names_where(self) -> None:
        for rule in RULES:
            if rule.audience.is_code:
                assert rule.enforced_by.strip(), rule.id

    def test_a_prose_rule_claims_no_enforcer(self) -> None:
        """Naming code for a rule that is only ever prose would claim what is not there."""
        with pytest.raises(ValueError):
            Rule(
                id="voice.invented",
                audience=Audience.VOICE,
                source="skill/SKILL.md:1-2",
                gist="something",
                enforced_by="core/bridge.py",
            )

    def test_a_code_rule_without_an_enforcer_is_refused(self) -> None:
        with pytest.raises(ValueError):
            Rule(
                id="core.invented",
                audience=Audience.CORE,
                source="skill/SKILL.md:1-2",
                gist="something",
            )

    def test_the_dropped_rows_are_recorded_rather_than_omitted(self) -> None:
        """A deleted rule and a forgotten one look identical unless one is written down."""
        assert rules_for(Audience.DROPPED)

    def test_the_tmux_row_is_owned_by_an_adapter_and_by_nothing_shared(self) -> None:
        rule = catalogue_module.BY_ID["adapter.launcher.tmux-destinations-are-tmux-only"]
        assert rule.audience is Audience.ADAPTER

    def test_issue_25_launch_rules_name_their_actual_source(self) -> None:
        assert (
            catalogue_module.BY_ID[
                "voice.start.complete-request-launches-directly"
            ].source
            == "issue/25"
        )
        assert (
            catalogue_module.BY_ID[
                "delegated.start.complete-request-launches-directly"
            ].source
            == "issue/25"
        )


class TestTheTableLandedWhole:
    """Exhaustiveness over the disposition tables, not just over what got written.

    Coverage tests iterate the catalogue, so a row nobody transcribed is a row no
    test can miss. This one checks the other direction: every line of every old
    skill file is claimed by at least one rule — kept, enforced elsewhere, or
    explicitly dropped. A forgotten row shows up as a gap.

    The line counts are the deleted files' own, at the commit the migration
    inventory read. They are provenance, like the `source` strings themselves.
    """

    MIGRATED = {
        "skill/SKILL.md": 96,
        "skill/announcing.md": 121,
        "skill/checking-and-talking.md": 119,
        "skill/closing.md": 60,
        "skill/retrying.md": 57,
        "skill/starting.md": 151,
    }

    def test_every_line_of_every_migrated_file_is_accounted_for(self) -> None:
        claimed: dict[str, set[int]] = {name: set() for name in self.MIGRATED}
        for rule in RULES:
            if not rule.source.startswith("skill/"):
                continue
            name, _, lines = rule.source.partition(":")
            assert name in claimed, rule.source
            first, _, last = lines.partition("-")
            claimed[name].update(range(int(first), int(last or first) + 1))

        for name, length in self.MIGRATED.items():
            missing = sorted(set(range(1, length + 1)) - claimed[name])
            assert not missing, f"{name} lines {missing} were migrated by nobody"


class TestCoverage:
    def test_natural_launch_is_carried_without_the_superseded_preflight_rule(
        self, instructions
    ) -> None:
        assert (
            instructions.carrier_of("voice.start.complete-request-launches-directly")
            is Audience.VOICE
        )
        assert (
            instructions.carrier_of("delegated.start.complete-request-launches-directly")
            is Audience.DELEGATED
        )
        assert (
            instructions.carrier_of("voice.start.needs-an-explicit-agent-and-workspace")
            is None
        )

    def test_each_retained_rule_lands_in_exactly_its_own_set(self, instructions) -> None:
        for rule in RULES:
            if rule.audience is Audience.DROPPED:
                continue
            assert instructions.carrier_of(rule.id) is rule.audience, rule.id

    def test_every_voice_rule_is_carried_by_the_voice_set(self, instructions) -> None:
        assert instructions.voice.covers == ids_for(Audience.VOICE)

    def test_every_delegated_rule_is_carried_by_the_delegated_set(self, instructions) -> None:
        assert instructions.delegated.covers == ids_for(Audience.DELEGATED)

    def test_no_dropped_rule_maps_anywhere(self, instructions) -> None:
        for rule in rules_for(Audience.DROPPED):
            assert instructions.carrier_of(rule.id) is None, rule.id

    def test_a_rule_enforced_in_code_wears_no_prose(self, instructions) -> None:
        """Core and adapter rules are proved by their own tests, never by a prompt."""
        for rule in RULES:
            if rule.audience.is_code:
                assert rule.id not in instructions.voice.covers
                assert rule.id not in instructions.delegated.covers

    def test_generation_refuses_a_set_that_lost_a_rule(self, monkeypatch) -> None:
        added = Rule(
            id="voice.rule.nobody.wrote",
            audience=Audience.VOICE,
            source="skill/SKILL.md:1-1",
            gist="a rule the generator does not carry",
        )
        monkeypatch.setattr(catalogue_module, "RULES", (*RULES, added))
        with pytest.raises(InstructionError, match="voice.rule.nobody.wrote"):
            generate(CONTEXT)


class TestInstructionSet:
    def test_claiming_a_rule_that_does_not_exist_is_refused(self) -> None:
        with pytest.raises(InstructionError, match="not a rule"):
            InstructionSet(
                audience=Audience.VOICE,
                sections=(Section("t", (Block(text="x", covers=("voice.no.such.rule",)),)),),
            )

    def test_claiming_the_same_rule_twice_is_refused(self) -> None:
        rule_id = next(iter(sorted(ids_for(Audience.VOICE))))
        with pytest.raises(InstructionError, match="twice"):
            InstructionSet(
                audience=Audience.VOICE,
                sections=(
                    Section(
                        "t",
                        (Block(text="x", covers=(rule_id,)), Block(text="y", covers=(rule_id,))),
                    ),
                ),
            )

    def test_a_set_may_not_carry_another_audiences_rule(self) -> None:
        rule_id = next(iter(sorted(ids_for(Audience.CORE))))
        with pytest.raises(InstructionError, match="may not carry it"):
            InstructionSet(
                audience=Audience.VOICE,
                sections=(Section("t", (Block(text="x", covers=(rule_id,)),)),),
            )

    def test_there_is_no_instruction_set_for_rules_code_carries(self) -> None:
        with pytest.raises(InstructionError, match="carried by code"):
            InstructionSet(
                audience=Audience.CORE,
                sections=(Section("t", (Block(text="x"),)),),
            )

    def test_connective_prose_may_cover_nothing(self) -> None:
        """A heading sentence is allowed, and proves nothing."""
        built = InstructionSet(
            audience=Audience.VOICE,
            sections=(Section("t", (Block(text="an example"),)),),
        )
        assert built.covers == frozenset()


class TestVoiceBudget:
    """The budget is measured in bytes, and that is what makes it a proof.

    A byte-level BPE token never costs less than one byte of input, so a byte
    count is an upper bound on a token count — in any script, with no tokenizer
    and no average-characters-per-token figure to be wrong about.
    """

    def test_the_budget_is_the_tickets_figure_in_the_unit_that_proves_it(self) -> None:
        assert VOICE_INSTRUCTION_TOKEN_BUDGET == 8_000
        assert MAX_VOICE_INSTRUCTION_BYTES == VOICE_INSTRUCTION_TOKEN_BUDGET

    def test_the_voice_set_fits_the_budget(self, instructions) -> None:
        assert instructions.voice.size_in_bytes <= MAX_VOICE_INSTRUCTION_BYTES

    def test_the_budget_is_counted_in_utf8_bytes_not_characters(self) -> None:
        """One CJK character is three bytes, and three bytes is what it may cost."""
        built = InstructionSet(
            audience=Audience.VOICE,
            sections=(Section("t", (Block(text="你好"),)),),
        )
        assert built.size_in_bytes > len(built.text)

    def test_an_oversized_voice_set_stops_generation(self, monkeypatch) -> None:
        monkeypatch.setattr("gpt_voicecoding.core.instructions.MAX_VOICE_INSTRUCTION_BYTES", 100)
        with pytest.raises(InstructionError, match="over the"):
            generate(CONTEXT)


class TestTheDelegatedSetNamesTheRealCli:
    def test_it_names_the_command_the_context_gave_it(self, instructions) -> None:
        assert str(CLI.command) in instructions.delegated.text

    def test_it_names_the_engine_it_reaches(self, instructions) -> None:
        assert str(CLI.socket_path) in instructions.delegated.text

    def test_it_names_the_engines_version(self, instructions) -> None:
        assert CLI.version in instructions.delegated.text

    def test_a_different_installation_produces_different_text(self, instructions) -> None:
        """The CLI is generated, so nothing about it can be a hard-coded string."""
        elsewhere = delegated_instructions(
            InstructionContext(
                cli=ControlPlaneCli(
                    command=Path("/opt/homebrew/bin/bridgectl"),
                    version="9.9.9",
                    socket_path=Path("/tmp/other.sock"),
                ),
                launch_usage=USAGE[Action.LAUNCH],
            )
        )
        assert "/opt/homebrew/bin/bridgectl" in elsewhere.text
        assert str(CLI.command) not in elsewhere.text
        assert elsewhere.text != instructions.delegated.text

    def test_a_path_with_spaces_survives_a_shell(self) -> None:
        spaced = delegated_instructions(
            InstructionContext(
                cli=ControlPlaneCli(
                    command=Path("/Application Support/GPT-VoiceCoding/bridgectl"),
                    version="1.0",
                    socket_path=Path("/tmp/s.sock"),
                ),
                launch_usage=USAGE[Action.LAUNCH],
            )
        )
        assert "'/Application Support/GPT-VoiceCoding/bridgectl'" in spaced.text

    def test_the_voice_set_names_it_too(self, instructions) -> None:
        """The voice thread reaches the engine through the same one door."""
        assert str(CLI.command) in instructions.voice.text

    def test_complete_launch_uses_the_parsers_exact_syntax(self, instructions) -> None:
        launch = f"{CLI.invocation} {USAGE[Action.LAUNCH]}"

        assert launch in instructions.voice.text
        assert launch in instructions.delegated.text

    def test_a_cli_that_is_not_where_it_really_is_gets_refused(self) -> None:
        """A bare name resolves against a PATH the generated thread may not share."""
        with pytest.raises(InstructionError, match="really is"):
            ControlPlaneCli(
                command=Path("bridgectl"), version="1.0", socket_path=Path("/tmp/s.sock")
            )

    def test_a_cli_without_a_version_is_refused(self) -> None:
        with pytest.raises(InstructionError):
            ControlPlaneCli(
                command=Path("/x/bridgectl"), version="  ", socket_path=Path("/tmp/s.sock")
            )

    def test_a_context_without_the_parsers_launch_usage_is_refused(self) -> None:
        with pytest.raises(InstructionError, match="launch usage"):
            InstructionContext(cli=CLI, launch_usage="  ")


class TestTheActionSetIsGeneratedFromTheClosedSet:
    def test_every_action_has_a_line(self) -> None:
        assert set(ACTION_GIST) == set(Action)

    def test_every_action_appears_in_the_delegated_set(self, instructions) -> None:
        for action in Action:
            assert str(action) in instructions.delegated.text

    def test_an_action_nobody_explained_stops_generation(self, monkeypatch) -> None:
        """The forcing function: a new action fails the build until someone writes its line."""
        thinned = {
            action: gist for action, gist in ACTION_GIST.items() if action is not Action.LIVE
        }
        monkeypatch.setattr(delegated_module, "ACTION_GIST", thinned)
        with pytest.raises(InstructionError, match="live"):
            delegated_instructions(CONTEXT)


class TestBothSetsAreOrdinaryData:
    def test_generation_touches_no_file(self, tmp_path, monkeypatch) -> None:
        """No skill is installed, no pointer is written, nothing is read from disk."""
        monkeypatch.chdir(tmp_path)
        generate(CONTEXT)
        assert list(tmp_path.iterdir()) == []

    def test_the_same_context_generates_the_same_text(self) -> None:
        assert voice_instructions(CONTEXT).text == voice_instructions(CONTEXT).text

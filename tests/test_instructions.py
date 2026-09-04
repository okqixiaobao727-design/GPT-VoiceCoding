"""What the three generated instruction sets owe, and how that is proved.

Coverage is asserted against the ids the generators *emit*, never against the
words they chose. That is the whole point of the catalogue: the prose is free to
be rewritten, translated or restructured, and the suite still fails the moment a
rule stops being carried or moves to a set that was never meant to have it.

Two things here are asserted about words rather than ids, and both are the
product decision of ADR 0018 rather than a preference about style. The Voice
hears **prose** — a heading, a bullet or a backtick in that set is the code
language Simon ruled out of the `prompt` slot. And the Voice is **never told a
verb exists**: a Voice handed something it cannot do invents rather than refuses
(#179), so every control-plane word in its text is an invitation to fabricate.
Neither claim survives as an id, so each is read off the rendered text.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from gpt_voicecoding.core.instructions import (
    ACTION_GIST,
    AGENT_ACTIONS,
    AGENT_INSTRUCTION_TOKEN_BUDGET,
    MAX_AGENT_INSTRUCTION_BYTES,
    MAX_VOICE_INSTRUCTION_BYTES,
    RULES,
    VOICE_INSTRUCTION_TOKEN_BUDGET,
    WITHHELD_ACTIONS,
    Audience,
    Block,
    ControlPlaneCli,
    InstructionContext,
    InstructionError,
    InstructionSet,
    Rule,
    Section,
    agent_instructions,
    delegated_instructions,
    generate,
    ids_for,
    voice_instructions,
)
from gpt_voicecoding.core.instructions import catalogue as catalogue_module
from gpt_voicecoding.core.instructions import delegated as delegated_module
from gpt_voicecoding.seams.control_plane import USAGE, Action

CLI = ControlPlaneCli(
    command=Path("/Applications/GPT-VoiceCoding.app/Contents/MacOS/bridgectl"),
    version="1.4.2",
    socket_path=Path("/tmp/gpt-voicecoding-501/control.sock"),
)
CONTEXT = InstructionContext(cli=CLI)


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

    def test_every_rule_id_is_prefixed_by_the_audience_that_carries_it(self) -> None:
        """A `voice.` id in the Agent set would be a lie about who hears it (#190)."""
        for rule in RULES:
            assert rule.id.startswith(f"{rule.audience}."), rule.id

    def test_nothing_is_recorded_as_dropped_any_more(self) -> None:
        """Retired rules are deleted rows; git history and #173's table are the record."""
        assert not hasattr(Audience, "DROPPED")
        assert not [rule for rule in RULES if rule.id.startswith("dropped.")]


class TestTheTableIsSettled:
    """#173's disposition of the twenty-one `voice.*` rules, one by one.

    Nine survive as Voice rules, one became a Call Agent rule, and eleven were
    deleted. Written down as three sets rather than three counts, because a
    count passes while the wrong rule is missing.
    """

    SURVIVING = frozenset(
        {
            "voice.identity.speak-names",
            "voice.instruction.one-clean-instruction",
            "voice.attribution.judgement-keeps-its-owner",
            "voice.notice.is-natural-speech",
            "voice.notice.invents-no-detail",
            "voice.notice.says-what-could-not-be-read",
            "voice.notice.speaks-in-this-shape",
            "voice.delivery.tells-the-truth-about-arrival",
            "voice.delivery.a-refusal-is-an-answer",
        }
    )

    #: Retired by #173's criterion — the 0901 flow does not ask for them.
    RETIRED = (
        "voice.orientation.no-screen",
        "voice.target.disambiguate-or-ask",
        "voice.conversation.no-action",
        "voice.authority.no-identity-from-the-screen",
        "voice.notice.asks-for-no-decision-nobody-awaits",
        "voice.roster.withheld-sessions-are-real",
        "voice.retry.failed-delivery-is-not-a-retry",
        "voice.retry.queued-is-not-delivered",
        "voice.retry.no-compensating-action",
        "voice.start.an-empty-read-is-not-a-failed-read",
        "voice.start.no-substitute-after-a-failure",
        "voice.notice.reads-progress-when-asked-for-more",
    )

    def test_exactly_the_nine_voice_rules_survive(self) -> None:
        assert ids_for(Audience.VOICE) == self.SURVIVING

    def test_every_retired_rule_is_gone_from_the_catalogue(self) -> None:
        known = {rule.id for rule in RULES}
        assert not known & set(self.RETIRED)

    #: Every rule the catalogue still owes, by id. The audit that replaces the
    #: file-line totality #193 retired: a rule deleted without a decision fails
    #: here on its name, and no retired rule's line numbers are written down to
    #: do it — those are a seam into the catalogue's own layout, which is why
    #: the ticket removes their spans rather than parking them.
    RETAINED = frozenset(
        {
            "core.delivery.four-states-and-one-request-identity",
            "core.identity.native-ids-stay-in-calls",
            "core.identity.validates-exact-target",
            "core.notice.owns-the-facts",
            "core.notice.owns-the-stop-detail",
            "core.relay.chooses-the-route",
            "core.relay.owns-the-reply-window-and-the-target",
            "core.retry.holds-the-canonical-notice-state",
            "core.retry.owns-escalation-and-eligibility",
            "core.retry.refusals-name-their-reason",
            "core.retry.requeues-exactly-one",
            "core.roster.is-the-registry",
            "core.roster.rejects-stale-or-ambiguous",
            "core.start.exact-identity-until-a-name-exists",
            "core.start.no-automatic-retry-after-a-terminal-failure",
            "delegated.authority.acts-only-through-the-control-plane",
            "delegated.cli.one-generated-command",
            "delegated.delivery.repeats-nothing-without-consent",
            "delegated.delivery.stays-inside-one-attempt",
            "delegated.identity.exact-structured",
            "delegated.notice.answers-the-exact-request",
            "delegated.notice.reads-before-it-reports",
            "delegated.notice.reports-a-failed-read-as-a-failed-read",
            "delegated.outcome.only-a-successful-call-is-success",
            "delegated.progress.reports-only-what-came-back",
            "delegated.relay.takes-the-route-as-given",
            "delegated.retry.only-a-retryable-notice",
            "delegated.start.distinguishes-empty-from-unread",
            "agent.cli.one-generated-command",
            "agent.history.pages-older-on-request",
            "agent.identity.copies-the-address-unchanged",
            "agent.live.ends-the-call",
            "agent.outcome.only-a-successful-call-is-success",
            "agent.output.returns-it-whole",
            "agent.read.now-every-time",
            "agent.relay.carries-the-users-words",
            "agent.verbs.only-the-six-forms",
        }
    )

    def test_the_catalogue_owes_exactly_these_rules_and_no_others(self) -> None:
        """The proof that nothing left unread, carried by ids rather than by lines.

        `SURVIVING` pins the nine Voice rules #173 kept; this pins everything
        else the catalogue still holds. A rule deleted because a diff was
        convenient fails here by name, and adding one is a decision somebody
        writes down in the same commit.
        """
        assert {rule.id for rule in RULES} == self.RETAINED | self.SURVIVING

    def test_the_history_rule_moved_to_the_call_agent_under_its_own_name(self) -> None:
        """#190's deferred key: a read belongs to the acting half, so the id says so."""
        moved = next(rule for rule in RULES if rule.id == "agent.history.pages-older-on-request")
        assert moved.audience is Audience.AGENT
        assert moved.source == "issue/151"


class TestEveryRuleStillNamesRealProvenance:
    """The line-accounting audit, over the rules that are still here.

    The full-file totality assertion left with the eleven retired rules: their
    spans are deleted rather than parked, and a test that read a deleted rule's
    line numbers would be a seam into the catalogue's own layout. What stays is
    the direction that still has meaning — a retained rule points at lines that
    really existed in the file it names.
    """

    MIGRATED = {
        "skill/SKILL.md": 96,
        "skill/announcing.md": 121,
        "skill/checking-and-talking.md": 119,
        "skill/closing.md": 60,
        "skill/retrying.md": 57,
        "skill/starting.md": 151,
    }

    #: Lines whose rules left with launch and close (#72). Written down rather
    #: than deleted from `MIGRATED`, for the same reason: they come back with the
    #: actions they describe, and are readable at the `parked/launch-close` tag.
    PARKED = {
        "skill/closing.md": ((1, 60),),
        "skill/starting.md": ((1, 24), (42, 96), (127, 138)),
    }

    @staticmethod
    def _span(source: str) -> tuple[str, int, int]:
        name, _, lines = source.partition(":")
        first, _, last = lines.partition("-")
        return name, int(first), int(last or first)

    def test_every_retained_rule_points_inside_the_file_it_names(self) -> None:
        for rule in RULES:
            if not rule.source.startswith("skill/"):
                continue
            name, first, last = self._span(rule.source)
            assert name in self.MIGRATED, rule.source
            assert 1 <= first <= last <= self.MIGRATED[name], rule.source

    def test_a_parked_span_names_no_line_a_rule_still_claims(self) -> None:
        """Parking is a record of absence, so it may not paper over a live rule."""
        live: dict[str, set[int]] = {name: set() for name in self.MIGRATED}
        for rule in RULES:
            if not rule.source.startswith("skill/"):
                continue
            name, first, last = self._span(rule.source)
            live[name].update(range(first, last + 1))

        for name, spans in self.PARKED.items():
            for first, last in spans:
                overlap = sorted(set(range(first, last + 1)) & live[name])
                assert not overlap, f"{name} lines {overlap} are parked and still claimed"


class TestCoverage:
    def test_each_retained_rule_lands_in_exactly_its_own_set(self, instructions) -> None:
        for rule in RULES:
            assert instructions.carrier_of(rule.id) is rule.audience, rule.id

    def test_every_voice_rule_is_carried_by_the_voice_set(self, instructions) -> None:
        assert instructions.voice.covers == ids_for(Audience.VOICE)

    def test_every_agent_rule_is_carried_by_the_agent_set(self, instructions) -> None:
        assert instructions.agent.covers == ids_for(Audience.AGENT)

    def test_every_delegated_rule_is_carried_by_the_delegated_set(self, instructions) -> None:
        assert instructions.delegated.covers == ids_for(Audience.DELEGATED)

    def test_a_rule_enforced_in_code_wears_no_prose(self, instructions) -> None:
        """Core and adapter rules are proved by their own tests, never by a prompt."""
        for rule in RULES:
            if rule.audience.is_code:
                assert rule.id not in instructions.voice.covers
                assert rule.id not in instructions.agent.covers
                assert rule.id not in instructions.delegated.covers

    @pytest.mark.parametrize(
        ("audience", "prefix"),
        [(Audience.VOICE, "voice"), (Audience.AGENT, "agent"), (Audience.DELEGATED, "delegated")],
    )
    def test_generation_refuses_a_set_that_lost_a_rule(self, audience, prefix, monkeypatch) -> None:
        added = Rule(
            id=f"{prefix}.rule.nobody.wrote",
            audience=audience,
            source="skill/SKILL.md:1-1",
            gist="a rule the generator does not carry",
        )
        monkeypatch.setattr(catalogue_module, "RULES", (*RULES, added))
        with pytest.raises(InstructionError, match=f"{prefix}.rule.nobody.wrote"):
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

    def test_a_set_may_be_rendered_without_its_headings(self) -> None:
        """The Voice hears prose; the title is navigation for whoever reads the code."""
        with_headings = InstructionSet(
            audience=Audience.VOICE,
            sections=(Section("A title", (Block(text="a paragraph"),)),),
        )
        without = InstructionSet(
            audience=Audience.VOICE,
            sections=(Section("A title", (Block(text="a paragraph"),)),),
            headings=False,
        )
        assert with_headings.text == "## A title\n\na paragraph\n"
        assert without.text == "a paragraph\n"


class TestTheTwoBudgets:
    """Two budgets, for two reasons, both measured in bytes.

    A byte-level BPE token never costs less than one byte of input, so a byte
    count is an upper bound on a token count — in any script, with no tokenizer
    and no average-characters-per-token figure to be wrong about. The Voice keeps
    this engine's own 8,000, which codex does not impose and which stands as the
    measure of "terse"; the Agent set is capped at the 8,192 codex caps that slot
    at (ADR 0018).
    """

    def test_the_budgets_are_the_two_figures_adr_0018_names(self) -> None:
        assert VOICE_INSTRUCTION_TOKEN_BUDGET == 8_000
        assert MAX_VOICE_INSTRUCTION_BYTES == VOICE_INSTRUCTION_TOKEN_BUDGET
        assert AGENT_INSTRUCTION_TOKEN_BUDGET == 8_192
        assert MAX_AGENT_INSTRUCTION_BYTES == AGENT_INSTRUCTION_TOKEN_BUDGET

    def test_the_voice_set_fits_its_budget(self, instructions) -> None:
        assert instructions.voice.size_in_bytes <= MAX_VOICE_INSTRUCTION_BYTES

    def test_the_agent_set_fits_its_budget(self, instructions) -> None:
        assert instructions.agent.size_in_bytes <= MAX_AGENT_INSTRUCTION_BYTES

    def test_the_budget_is_counted_in_utf8_bytes_not_characters(self) -> None:
        """One CJK character is three bytes, and three bytes is what it may cost."""
        built = InstructionSet(
            audience=Audience.VOICE,
            sections=(Section("t", (Block(text="你好"),)),),
        )
        assert built.size_in_bytes > len(built.text)

    def test_an_oversized_voice_set_stops_generation(self, monkeypatch) -> None:
        monkeypatch.setattr("gpt_voicecoding.core.instructions.MAX_VOICE_INSTRUCTION_BYTES", 100)
        with pytest.raises(InstructionError, match="voice instructions are"):
            generate(CONTEXT)

    def test_an_oversized_agent_set_stops_generation(self, monkeypatch) -> None:
        monkeypatch.setattr("gpt_voicecoding.core.instructions.MAX_AGENT_INSTRUCTION_BYTES", 100)
        with pytest.raises(InstructionError, match="agent instructions are"):
            generate(CONTEXT)


class TestTheVoiceHearsProse:
    """ADR 0018: the Voice's set is natural language, and names no mechanism."""

    #: A heading, a bullet, a fenced or inline code span, and a `key: value`
    #: line — the four shapes of "code language" the `prompt` slot must not
    #: carry. Matched per line, because that is what each of them is.
    NOT_PROSE = {
        "a markdown heading": re.compile(r"^\s*#"),
        "a list marker": re.compile(r"^\s*[-*+•]\s"),
        "a code span": re.compile(r"`"),
        "a key: value line": re.compile(r"^\s*\S[^:\n]{0,30}:\s"),
    }

    #: Every control-plane word the Voice may not be told about, as a word.
    #: `live` has to be a word: `delivered` contains it, and a substring test
    #: would have failed on a sentence about delivery.
    #:
    #: **`brief` and `relay` are deliberately not here.** Both are also ordinary
    #: English for what this half does, and the ticket fixes the sentences that
    #: use them that way — the Session Brief and the Roster Brief are Briefing's
    #: own nouns (#166), and "you relay what the engine handed you" is #173 §3.1's
    #: own wording. What this test can still prove is that no command is named:
    #: no CLI, and none of the words below.
    VERBS = re.compile(r"\b(bridgectl|status|switch|history|approve|verify|live)\b", re.IGNORECASE)

    @pytest.mark.parametrize("shape", sorted(NOT_PROSE))
    def test_the_voice_text_carries_none_of_the_code_shapes(self, shape, instructions) -> None:
        pattern = self.NOT_PROSE[shape]
        offenders = [line for line in instructions.voice.text.splitlines() if pattern.search(line)]
        assert not offenders, f"the voice set contains {shape}: {offenders}"

    def test_the_voice_is_never_told_a_verb_exists(self, instructions) -> None:
        """A Voice handed something it cannot do invents rather than refuses (#179)."""
        found = self.VERBS.findall(instructions.voice.text)
        assert not found, f"the voice set names control-plane verbs: {sorted(set(found))}"

    def test_the_voice_set_names_no_cli(self, instructions) -> None:
        """The invocation moved to the half that can run it."""
        assert str(CLI.command) not in instructions.voice.text
        assert str(CLI.socket_path) not in instructions.voice.text

    def test_the_voice_text_is_the_same_on_every_machine(self) -> None:
        """The sharper form of "names no CLI": nothing about this install is in it.

        The other two sets differ per machine by design — they name the binary
        they run. A Voice set that varied at all would mean some mechanism had
        got in, and this catches the ones a path check would not, like a version
        string or a socket name worked into a sentence.
        """
        elsewhere = voice_instructions(
            InstructionContext(
                cli=ControlPlaneCli(
                    command=Path("/opt/homebrew/bin/bridgectl"),
                    version="9.9.9",
                    socket_path=Path("/tmp/other.sock"),
                ),
            )
        )
        assert elsewhere.text == voice_instructions(CONTEXT).text

    def test_it_is_nine_paragraphs_in_the_order_the_ticket_fixes(self, instructions) -> None:
        paragraphs = [part for part in instructions.voice.text.split("\n\n") if part.strip()]
        assert len(paragraphs) == 9

    def test_the_paragraphs_run_in_the_0901_order(self, instructions) -> None:
        """#173 §3: who you are, the opened call, the two briefs, and so on to hanging up."""
        marks = (
            "what it did not hand you, you do not have",
            "connect tone",
            "Session Brief",
            "Roster Brief",
            "five at a time",
            "their own words",
            "已转达",
            "wait to be asked",
            "the half behind you",
        )
        at = [instructions.voice.text.find(mark) for mark in marks]
        assert all(place >= 0 for place in at), dict(zip(marks, at, strict=True))
        assert at == sorted(at), dict(zip(marks, at, strict=True))

    def test_the_undelivered_reply_is_spoken_between_the_state_and_the_newest(
        self, instructions
    ) -> None:
        """#224: the shape rule names the field, so it is not left to the model."""
        marks = (
            "stopped on something the engine could not read",
            "did not arrive",
            "what it most recently said",
        )
        at = [instructions.voice.text.find(mark) for mark in marks]
        assert all(place >= 0 for place in at), dict(zip(marks, at, strict=True))
        assert at == sorted(at), dict(zip(marks, at, strict=True))

    def test_both_forms_of_the_undelivered_sentence_are_named(self, instructions) -> None:
        """Briefing words one of two arrivals (#197), and neither may be invented."""
        assert "did not arrive" in instructions.voice.text
        assert "may not have arrived" in instructions.voice.text
        assert "whichever of the two it said" in instructions.voice.text
        assert "say that it did not and" not in instructions.voice.text

    def test_the_post_relay_sentences_are_the_ones_round_1_settled(self, instructions) -> None:
        assert "已转达" in instructions.voice.text
        assert "收到，等它这轮结束送进去" in instructions.voice.text


class TestTheAgentSetIsTheActingHalf:
    """#173 §4: six forms, the CLI that runs them, and nothing it may not run."""

    def test_the_action_split_is_total_over_the_closed_set(self) -> None:
        """A ninth action fails the build until somebody decides who may run it."""
        assert set(AGENT_ACTIONS) | set(WITHHELD_ACTIONS) == set(Action)
        assert not set(AGENT_ACTIONS) & set(WITHHELD_ACTIONS)

    def test_it_is_given_exactly_the_five_actions_173_names(self) -> None:
        assert set(AGENT_ACTIONS) == {
            Action.BRIEF,
            Action.HISTORY,
            Action.RELAY,
            Action.APPROVE,
            Action.LIVE,
        }

    def test_the_rendered_set_shows_all_six_forms_173_names(self, instructions) -> None:
        """Six names, five lines — and read off the text, not off the action tuple.

        #173 §4 lists `brief`, `brief <address>`, `history <address> [--before
        N]`, `relay`, `approve` and `live`. Five of those are actions: `brief`
        and `brief <address>` are one action whose address is optional, and its
        one usage line shows both forms, which is why hand-splitting it would be
        retyping a grammar the parser already owns.

        Asserted against `instructions.agent.text` because what is graded is what
        the Call Agent hears. A test that read `AGENT_ACTIONS` would pass on a
        renderer that emitted nothing at all.
        """
        rendered = instructions.agent.text
        for action in AGENT_ACTIONS:
            assert USAGE[action] in rendered, action

        brief = next(line for line in rendered.splitlines() if USAGE[Action.BRIEF] in line)
        assert re.search(r"\bbrief \[.+\]", brief), brief

    def test_the_rendered_set_names_no_action_it_may_not_run(self, instructions) -> None:
        """The voice call neither queries nor flips switches (#173)."""
        for action in WITHHELD_ACTIONS:
            found = re.search(rf"\b{action}\b", instructions.agent.text)
            assert found is None, f"the agent set names {action}: {found}"

    def test_it_names_the_cli_the_context_gave_it(self, instructions) -> None:
        assert str(CLI.command) in instructions.agent.text
        assert str(CLI.socket_path) in instructions.agent.text
        assert CLI.version in instructions.agent.text

    def test_a_different_installation_produces_different_text(self, instructions) -> None:
        elsewhere = agent_instructions(
            InstructionContext(
                cli=ControlPlaneCli(
                    command=Path("/opt/homebrew/bin/bridgectl"),
                    version="9.9.9",
                    socket_path=Path("/tmp/other.sock"),
                ),
            )
        )
        assert "/opt/homebrew/bin/bridgectl" in elsewhere.text
        assert str(CLI.command) not in elsewhere.text
        assert elsewhere.text != instructions.agent.text

    def test_it_says_which_verb_ends_the_call(self, instructions) -> None:
        """#179, 3 of 3: told this, the Call Agent ran it on every spoken request."""
        assert re.search(r"\blive\b[^\n]*end", instructions.agent.text) or re.search(
            r"end[^\n]*\blive\b", instructions.agent.text
        )

    def test_an_action_nobody_explained_stops_generation(self, monkeypatch) -> None:
        from gpt_voicecoding.core.instructions import agent as agent_module

        thinned = {
            action: gist
            for action, gist in agent_module.AGENT_GIST.items()
            if action is not Action.LIVE
        }
        monkeypatch.setattr(agent_module, "AGENT_GIST", thinned)
        with pytest.raises(InstructionError, match="live"):
            agent_instructions(CONTEXT)


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
            )
        )
        assert "'/Application Support/GPT-VoiceCoding/bridgectl'" in spaced.text

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


class TestAllThreeSetsAreOrdinaryData:
    def test_generation_touches_no_file(self, tmp_path, monkeypatch) -> None:
        """No skill is installed, no pointer is written, nothing is read from disk."""
        monkeypatch.chdir(tmp_path)
        generate(CONTEXT)
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.parametrize("generator", [voice_instructions, agent_instructions])
    def test_the_same_context_generates_the_same_text(self, generator) -> None:
        assert generator(CONTEXT).text == generator(CONTEXT).text

"""The instructions Bridge Core generates, and the coverage mapping that audits them.

Three sets are produced from one catalogue, because a Live Call is two models and
a Delegated Turn is a third (ADR 0018): the prose the **Voice** speaks by, the
rules the **Call Agent** acts by, and the action discipline a Delegated Turn's
thread starts with. All three are plain data — the Call adapter and the Codex
adapter consume them, Bridge Core never installs a file, and nothing anywhere
reads a skill.

`generate` is fail-closed twice over. A rule the catalogue says belongs in one of
the three prose sets, which that set does not cover, stops generation: a
half-written set would otherwise ship silently and only a test would ever know.
And each of the two call-side sets is measured against its own budget — the
Voice's 8,000 bytes, which is this engine's own measure of "terse", and the Call
Agent's 8,192, which is the cap codex puts on the slot it travels in.

The coverage mapping is the audit trail. It names, for every rule that survived,
which set really carries it — the three prose sets by what they emitted, Core and
the adapters by what the catalogue records.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from gpt_voicecoding.core.instructions.agent import (
    AGENT_ACTIONS,
    AGENT_GIST,
    AGENT_INSTRUCTION_TOKEN_BUDGET,
    MAX_AGENT_INSTRUCTION_BYTES,
    WITHHELD_ACTIONS,
    agent_instructions,
)
from gpt_voicecoding.core.instructions.blocks import (
    Block,
    InstructionError,
    InstructionSet,
    Section,
)
from gpt_voicecoding.core.instructions.catalogue import (
    BY_ID,
    RULES,
    Audience,
    Rule,
    ids_for,
    rules_for,
)
from gpt_voicecoding.core.instructions.context import ControlPlaneCli, InstructionContext
from gpt_voicecoding.core.instructions.delegated import ACTION_GIST, delegated_instructions
from gpt_voicecoding.core.instructions.voice import (
    MAX_VOICE_INSTRUCTION_BYTES,
    VOICE_INSTRUCTION_TOKEN_BUDGET,
    voice_instructions,
)

__all__ = [
    "ACTION_GIST",
    "AGENT_ACTIONS",
    "AGENT_GIST",
    "AGENT_INSTRUCTION_TOKEN_BUDGET",
    "BY_ID",
    "MAX_AGENT_INSTRUCTION_BYTES",
    "MAX_VOICE_INSTRUCTION_BYTES",
    "RULES",
    "VOICE_INSTRUCTION_TOKEN_BUDGET",
    "WITHHELD_ACTIONS",
    "Audience",
    "Block",
    "ControlPlaneCli",
    "InstructionContext",
    "InstructionError",
    "InstructionSet",
    "Instructions",
    "Rule",
    "Section",
    "agent_instructions",
    "delegated_instructions",
    "generate",
    "ids_for",
    "rules_for",
    "voice_instructions",
]


@dataclass(frozen=True, slots=True)
class Instructions:
    """All three generated sets, and who carries every rule that survived."""

    voice: InstructionSet
    agent: InstructionSet
    delegated: InstructionSet
    coverage: Mapping[str, Audience]

    def carrier_of(self, rule_id: str) -> Audience | None:
        """Which set carries one rule, or None when nothing does."""
        return self.coverage.get(rule_id)


def generate(context: InstructionContext) -> Instructions:
    """Every instruction set this engine needs, or a refusal naming what is missing."""
    voice = voice_instructions(context)
    agent = agent_instructions(context)
    delegated = delegated_instructions(context)

    for produced in (voice, agent, delegated):
        owed = ids_for(produced.audience) - produced.covers
        if owed:
            raise InstructionError(
                f"the {produced.audience} instructions do not carry "
                + ", ".join(sorted(owed))
                + " — every retained rule has to land somewhere"
            )

    _within_budget(voice, MAX_VOICE_INSTRUCTION_BYTES, "a Live Call's Voice")
    _within_budget(agent, MAX_AGENT_INSTRUCTION_BYTES, "the Call Agent's start slot")

    return Instructions(
        voice=voice,
        agent=agent,
        delegated=delegated,
        coverage=_coverage(voice, agent, delegated),
    )


def _within_budget(produced: InstructionSet, budget: int, whose: str) -> None:
    """One set against its own cap. Bytes, because tokens are made of them."""
    if produced.size_in_bytes > budget:
        raise InstructionError(
            f"the {produced.audience} instructions are {produced.size_in_bytes} bytes, over "
            f"the {budget} {whose} allows — and a byte is the floor on what one token costs"
        )


def _coverage(*produced: InstructionSet) -> Mapping[str, Audience]:
    """Rule id to the set that carries it."""
    mapping: dict[str, Audience] = {}
    for one in produced:
        mapping.update({rule_id: one.audience for rule_id in one.covers})
    mapping.update({rule.id: rule.audience for rule in RULES if rule.audience.is_code})
    return MappingProxyType(mapping)

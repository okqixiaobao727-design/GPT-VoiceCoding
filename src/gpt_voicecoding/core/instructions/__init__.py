"""The instructions Bridge Core generates, and the coverage mapping that audits them.

Two sets are produced from one catalogue: the house rules a Live Call's voice
thread starts with, and the action discipline a Delegated Turn's thread starts
with. Both are plain data — the Call adapter and the Codex adapter consume them,
Bridge Core never installs a file, and nothing anywhere reads a skill.

`generate` is fail-closed: a rule the catalogue says belongs in one of the two
prose sets, which that set does not cover, stops generation. A half-written set
would otherwise ship silently and only a test would ever know.

The coverage mapping is the audit trail. It names, for every rule that survived,
which set really carries it — the two prose sets by what they emitted, Core and
the adapters by what the catalogue records. Rules that were dropped appear in it
nowhere, which is the whole of what "dropped" means here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

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
    "BY_ID",
    "MAX_VOICE_INSTRUCTION_BYTES",
    "RULES",
    "VOICE_INSTRUCTION_TOKEN_BUDGET",
    "Audience",
    "Block",
    "ControlPlaneCli",
    "InstructionContext",
    "InstructionError",
    "InstructionSet",
    "Instructions",
    "Rule",
    "Section",
    "delegated_instructions",
    "generate",
    "ids_for",
    "rules_for",
    "voice_instructions",
]


@dataclass(frozen=True, slots=True)
class Instructions:
    """Both generated sets, and who carries every rule that survived."""

    voice: InstructionSet
    delegated: InstructionSet
    coverage: Mapping[str, Audience]

    def carrier_of(self, rule_id: str) -> Audience | None:
        """Which set carries one rule, or None when nothing does — a dropped rule."""
        return self.coverage.get(rule_id)


def generate(context: InstructionContext) -> Instructions:
    """Both instruction sets for this engine, or a refusal naming what is missing."""
    voice = voice_instructions(context)
    delegated = delegated_instructions(context)

    for produced in (voice, delegated):
        owed = ids_for(produced.audience) - produced.covers
        if owed:
            raise InstructionError(
                f"the {produced.audience} instructions do not carry "
                + ", ".join(sorted(owed))
                + " — every retained rule has to land somewhere"
            )

    if voice.size_in_bytes > MAX_VOICE_INSTRUCTION_BYTES:
        raise InstructionError(
            f"the voice instructions are {voice.size_in_bytes} bytes, over the "
            f"{MAX_VOICE_INSTRUCTION_BYTES} a Live Call's start budget allows — and a byte "
            "is the floor on what one token costs"
        )

    return Instructions(voice=voice, delegated=delegated, coverage=_coverage(voice, delegated))


def _coverage(voice: InstructionSet, delegated: InstructionSet) -> Mapping[str, Audience]:
    """Rule id to the set that carries it. Dropped rules are in no set at all."""
    mapping: dict[str, Audience] = {}
    for produced in (voice, delegated):
        mapping.update({rule_id: produced.audience for rule_id in produced.covers})
    mapping.update({rule.id: rule.audience for rule in RULES if rule.audience.is_code})
    return MappingProxyType(mapping)

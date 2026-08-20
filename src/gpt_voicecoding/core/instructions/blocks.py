"""How a generated instruction set is put together, and what it owes.

A generator writes prose in blocks, and each block declares the rule ids it
discharges. The set is therefore able to *say* what it covers — coverage is
emitted as data alongside the text, rather than recovered afterwards by
searching the text for words that might mean the right thing. Rewriting a
sentence changes nothing about coverage; deleting a rule changes everything,
and that is the asymmetry the tests need.

Three things are refused at construction rather than left to a test, because
they are the mistakes that would make the coverage claim meaningless:

- covering an id the catalogue does not have — a typo would otherwise read as
  a discharged obligation;
- covering the same id twice — "appears once" has to mean once;
- covering an id that belongs to another audience — a Core rule wearing prose
  is exactly what the split of audiences exists to prevent, and a dropped one
  reappearing is what the DROPPED rows exist to catch.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from gpt_voicecoding.core.instructions.catalogue import BY_ID, Audience


class InstructionError(Exception):
    """An instruction set cannot be generated, or does not say what it claims."""


@dataclass(frozen=True, slots=True)
class Block:
    """One passage of prose, and the rules it discharges.

    A block with no ids is connective tissue — a heading sentence, an example.
    It is allowed, and it proves nothing.
    """

    text: str
    covers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise InstructionError("an empty block covers nothing and says nothing")


@dataclass(frozen=True, slots=True)
class Section:
    """A titled run of blocks. Structure for the reader, not for the coverage test."""

    title: str
    blocks: tuple[Block, ...]

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise InstructionError("a section needs a title the reader can navigate by")
        if not self.blocks:
            raise InstructionError(f"section {self.title!r} has nothing in it")


@dataclass(frozen=True, slots=True)
class InstructionSet:
    """One generated set of instructions: its prose, and the coverage it claims."""

    audience: Audience
    sections: tuple[Section, ...]
    covers: frozenset[str] = field(init=False)
    text: str = field(init=False)

    def __post_init__(self) -> None:
        if not self.audience.is_spoken:
            raise InstructionError(
                f"{self.audience} rules are carried by code, not by prose; generating a "
                "set for them would claim enforcement that is not there"
            )
        object.__setattr__(self, "covers", frozenset(self._claimed()))
        object.__setattr__(self, "text", self._rendered())

    def _claimed(self) -> tuple[str, ...]:
        claimed: list[str] = []
        for section in self.sections:
            for block in section.blocks:
                for rule_id in block.covers:
                    rule = BY_ID.get(rule_id)
                    if rule is None:
                        raise InstructionError(
                            f"{self.audience} instructions claim {rule_id!r}, which is not "
                            "a rule in the catalogue"
                        )
                    if rule.audience is not self.audience:
                        raise InstructionError(
                            f"{rule_id} is a {rule.audience} rule; the {self.audience} set "
                            "may not carry it"
                        )
                    if rule_id in claimed:
                        raise InstructionError(
                            f"{rule_id} is covered twice in the {self.audience} set; a rule "
                            "said twice is a rule that can be deleted once and still pass"
                        )
                    claimed.append(rule_id)
        return tuple(claimed)

    def _rendered(self) -> str:
        parts: list[str] = []
        for section in self.sections:
            parts.append(f"## {section.title}")
            parts.extend(block.text.strip() for block in section.blocks)
        return "\n\n".join(parts) + "\n"

    @property
    def size_in_bytes(self) -> int:
        """What a budget is measured in. Bytes, because tokens are made of them."""
        return len(self.text.encode("utf-8"))

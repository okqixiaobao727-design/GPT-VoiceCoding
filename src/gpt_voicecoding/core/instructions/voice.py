"""What the speaking half of a Live Call is told, in the language it speaks.

This set addresses the Voice and nobody else, and it **replaces the backend's
own default voice persona outright** (ADR 0018). So it is not a supplement to
something already in place: everything the Voice knows about how to behave on
this call is here. Which field on the wire carries it to that half is the
realtime adapter's to know and this module's never to name.

**Prose, and only prose.** No headings, no bullets, no code, no key-and-value
lines — Simon, 2026-09-01: 控制 voice 一定要用自然语言而不是代码语言. The section
below has a title for whoever reads this file; the set is rendered without it.

**The Voice is never told a verb exists.** Anything that needs the engine goes
to the half behind it. That is not tidiness: asked for something it had no
answer to, a Voice under this engine's own prompt invented a system clock eleven
hours out and kept advancing it as the call went on (#179). Every control-plane
word in this text would be one more thing it could be asked to do and would
answer for. So the acting rules live in the Call Agent's set, and the one
sentence standing between this half and an invented answer — you relay what the
engine handed you; what it did not hand you, you do not have — is a Voice rule
of its own.

**Written against the Session Brief as the fields reach the wire**, not against
the sentence Briefing renders for the Companion Channel: the Voice is handed the
facts and speaks them, and a second renderer for the same facts is a second
answer waiting to disagree with the first (#166).

The budget is enforced in **bytes**, and that is not an approximation of the
token budget — it is a proof of it. A byte-level BPE tokeniser, which is what
these models use, never emits a token for less than one byte of input, so the
UTF-8 byte count is an upper bound on the token count for any text in any
script. Eight thousand bytes therefore cannot be more than eight thousand
tokens, with no tokenizer, no dependency and no assumption about how many
characters a token averages — an average would be a guess, and one that a single
CJK character or emoji would quietly invalidate. The backend imposes no budget
on what the Voice is given; this one is the engine's own, and it is the measure
of "terse".
"""

from __future__ import annotations

from gpt_voicecoding.core.instructions.blocks import Block, InstructionSet, Section
from gpt_voicecoding.core.instructions.catalogue import Audience
from gpt_voicecoding.core.instructions.context import InstructionContext

#: This engine's own cap on what the Voice starts with. The backend imposes
#: none of its own on this audience (ADR 0018), so raising this is a product
#: decision about terseness, asked with the text in hand.
VOICE_INSTRUCTION_TOKEN_BUDGET = 8_000

#: The same number in the unit that proves it: one token costs at least one byte.
MAX_VOICE_INSTRUCTION_BYTES = VOICE_INSTRUCTION_TOKEN_BUDGET


def voice_instructions(context: InstructionContext) -> InstructionSet:
    """The prose the Voice starts with. Takes no context: it names no mechanism."""
    del context  # The Voice is told nothing about this machine — deliberately.
    return InstructionSet(audience=Audience.VOICE, sections=_sections(), headings=False)


def _sections() -> tuple[Section, ...]:
    return (
        Section(
            title="The voice of the engine",
            blocks=(
                Block(
                    covers=("voice.notice.invents-no-detail",),
                    text=(
                        "You are the voice of an engine that watches the coding sessions "
                        "running on this person's machine. You speak for it and never as one "
                        "of those sessions, so always the third person — it says, it "
                        "recommends, it is waiting. Be terse; say the thing and stop. Speak "
                        "slowly. You relay what the engine handed you; what it did not hand "
                        "you, you do not have, and saying so is a complete answer. Speak "
                        "whatever language the person is speaking."
                    ),
                ),
                Block(
                    text=(
                        "This person opened the call themselves, so they know why they are "
                        "here. After the connect tone, stay quiet until they say something. "
                        "Then do what they asked and nothing beside it."
                    ),
                ),
                Block(
                    covers=(
                        "voice.notice.is-natural-speech",
                        "voice.identity.speak-names",
                        "voice.notice.speaks-in-this-shape",
                    ),
                    text=(
                        "When you speak about one session, use ordinary sentences and this "
                        "order. Its project and its task, which is how a person names it out "
                        "loud, then which coding agent it is, then where it stands — waiting "
                        "on a decision from them, waiting on permission, finished, still "
                        "working, or stopped on something the engine could not read. Then one "
                        "sentence of what it most recently said. Then, if it is asking "
                        "something, the question in one sentence, each choice by name, and "
                        "whose recommendation it is if one is marked. Close by saying whether "
                        "they can answer it from here or have to go to the keyboard. That "
                        "whole shape, about one session, is the Session Brief."
                    ),
                ),
                Block(
                    text=(
                        "Asked what is going on generally, give the counts rather than the "
                        "list. How many are waiting on them, how many are waiting on "
                        "permission, how many have finished, how many are still working, and "
                        "how many stopped on something that could not be read — then ask "
                        "which one they want. A state with none in it is left unsaid. When "
                        "they narrow it, by name or by state, speak each one that matches in "
                        "the order above, one after another. That counted answer is the "
                        "Roster Brief."
                    ),
                ),
                Block(
                    covers=("voice.notice.says-what-could-not-be-read",),
                    text=(
                        "If they want more than that one sentence, tell them the newest "
                        "message whole, and the question whole — every choice with what it "
                        "means, and the recommendation — still as speech, never as a list "
                        "read out. Older messages come five at a time, and there are more "
                        "before those if they ask. When part of it could not be read, say "
                        "which part and why; a partial answer spoken as a whole one is the "
                        "failure they have no way to catch."
                    ),
                ),
                Block(
                    covers=(
                        "voice.instruction.one-clean-instruction",
                        "voice.attribution.judgement-keeps-its-owner",
                    ),
                    text=(
                        "When they decide something, what goes back is their own words, "
                        "tidied of the false starts and nothing more. Add nothing, decide "
                        "nothing for them, and choose nothing they left open — if you cannot "
                        "tell what they chose, ask. Expand on it only if they tell you to. "
                        "The judgement in the other direction keeps its owner too: a "
                        "recommendation is that session's opinion, and you say so."
                    ),
                ),
                Block(
                    covers=(
                        "voice.delivery.tells-the-truth-about-arrival",
                        "voice.delivery.a-refusal-is-an-answer",
                    ),
                    text=(
                        "Once their words have gone back, the engine tells you how it went, "
                        "and you say one thing. If it arrived, 已转达. If it is waiting for "
                        "that session to finish this turn, 收到，等它这轮结束送进去. If it was "
                        "held, or failed, or nobody can tell, one clause of the reason the "
                        "engine gave. Then stop talking. Do not go back and check on it "
                        "unless they ask you to."
                    ),
                ),
                Block(
                    text=(
                        "A short tone during the call means news has come in. It is not a cue "
                        "to start talking — wait to be asked. If the engine hands you "
                        "something about a session while you are on the call, speak it in the "
                        "same order as any other single session."
                    ),
                ),
                # **Told as an action, because a rule told as a prohibition made
                # this Voice do nothing at all.** The first wording opened "that
                # is not yours to do", and on the wire (#194, runs
                # `20260902T212231Z` and `20260902T213650Z`) the Voice answered a
                # spoken hang-up request with silence: the user's transcript
                # deltas arrived, no hand-off followed, and the Silence Ceiling
                # ended the call sixty seconds later. Replacing the whole prose
                # with the probe's own — which carries no delegation sentence at
                # all — routed nothing either (`20260902T214953Z`: the Voice
                # spoke, and no `bridgectl` ran in 120s). So what the slot needed
                # was never fewer words but the *general* instruction to pass a
                # request on, which `scripts/realtime_text_entry_probe.py`'s
                # `VOICE_PROMPT_DELEGATING` predicted would restore it and which
                # the earlier text only ever gave for hanging up.
                Block(
                    text=(
                        "When they ask for something to be done rather than told — hang the "
                        "call up, run something, change something — pass the request to the "
                        "half behind you rather than answering it yourself. It has the means "
                        "and you do not. Then let it happen: never announce that the call has "
                        "ended, and never say goodbye as though you had ended it."
                    ),
                ),
            ),
        ),
    )

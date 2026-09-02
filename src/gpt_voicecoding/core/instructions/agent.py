"""What the acting half of a Live Call is told: its verbs, and its discipline.

A codex v3 realtime call is two models, and this is the one with tools (ADR
0018). It hears the user's speech, runs the control plane, and hands what came
back to the Voice. It is told nothing about tone, order or pacing, because it
never speaks to anybody — and that costs nothing, while a speaking rule here
would be a rule in the set that cannot act on it.

**Five actions, six forms, and three the call does not get.** `status`, the
switch flip and the seam report are withheld: the voice call neither queries the
engine's switches nor flips them (#173), and an action in this text is an action
the model will find a reason to run. The split is total over the closed action
set by construction, so a ninth action fails generation until somebody decides
which side of the line it is on — the same forcing function the Delegated set
has for its own card.

**The forms come from the shared vocabulary, never from memory.** `USAGE` sits
in `seams/control_plane.py` beside `Action`, and it is what `bridgectl` prints
as its help and what the Companion Channel's `/` grammar refuses against — so a
form written out here by hand would be a third spelling, free to drift from the
parser that has to accept it. Reading it from the seam rather than from the
parser is what keeps Bridge Core off the mechanism package: that module imports
`core.relays`, so the convenient import would have closed a cycle across ADR
0001's boundary. `brief` carries its address in brackets, which is why #173's
six forms are five usage lines: `brief` and `brief <address>` are one line with
an optional argument, and hand-splitting it would be exactly the retyping this
avoids.

**The budget is 8,192 bytes** because the backend caps what this audience is
given at 8,192 tokens and a byte is the floor on what one token costs — the same
proof the Voice's own cap rests on, against a limit that is somebody else's
rather than ours. Which wire field the cap belongs to is the realtime adapter's
to know; here it is a number about this half.
"""

from __future__ import annotations

from gpt_voicecoding.core.instructions.blocks import (
    Block,
    InstructionError,
    InstructionSet,
    Section,
)
from gpt_voicecoding.core.instructions.catalogue import Audience
from gpt_voicecoding.core.instructions.context import InstructionContext
from gpt_voicecoding.seams.control_plane import USAGE, Action

#: The backend's cap on what the acting half is started with, in tokens
#: (ADR 0018).
AGENT_INSTRUCTION_TOKEN_BUDGET = 8_192

#: The same number in the unit that proves it: one token costs at least one byte.
MAX_AGENT_INSTRUCTION_BYTES = AGENT_INSTRUCTION_TOKEN_BUDGET

#: The actions a Live Call's acting half is given, in the order #173 §4 lists
#: them. Five actions, rendered as that section's six forms.
AGENT_ACTIONS: tuple[Action, ...] = (
    Action.BRIEF,
    Action.HISTORY,
    Action.RELAY,
    Action.APPROVE,
    Action.LIVE,
)

#: The actions it is not given, written down rather than merely absent — the
#: two look identical otherwise, and only one of them is a decision.
WITHHELD_ACTIONS: tuple[Action, ...] = (Action.STATUS, Action.SWITCH, Action.VERIFY)

#: What each given action answers, in one line. Total over `AGENT_ACTIONS`.
#:
#: **What it answers, never how to use it.** The discipline around a verb — that
#: history pages backwards, that a relay carries the user's own words, that
#: nothing but the toggle ends a call — is a rule with an id, and lives in the
#: blocks below. Saying it in both places would cost the budget twice and leave
#: two wordings free to drift apart, which is the finding that shortened these.
AGENT_GIST: dict[Action, str] = {
    Action.BRIEF: (
        "what the sessions are doing — all of them with no address, one of them "
        "whole with an address"
    ),
    Action.HISTORY: "one page of what an exact session said and was told, newest first",
    Action.RELAY: "put words into one exact session",
    Action.APPROVE: "answer one pending permission request",
    Action.LIVE: "ends the call that is up",
}


def agent_instructions(context: InstructionContext) -> InstructionSet:
    """The Call Agent's rules, for this engine and this machine."""
    return InstructionSet(audience=Audience.AGENT, sections=_sections(context))


def _command_card() -> str:
    """The forms this half may run, from the seam's own usage lines."""
    unsorted = set(AGENT_ACTIONS) | set(WITHHELD_ACTIONS)
    if unsorted != set(Action) or set(AGENT_ACTIONS) & set(WITHHELD_ACTIONS):
        raise InstructionError(
            "every control-plane action is either given to the Call Agent or withheld "
            "from it, and these are on neither side or on both: "
            + ", ".join(sorted(str(action) for action in set(Action) ^ unsorted))
        )
    missing = [action for action in AGENT_ACTIONS if not AGENT_GIST.get(action, "").strip()]
    if missing:
        raise InstructionError(
            "the Call Agent is given actions nothing here explains: "
            + ", ".join(str(action) for action in missing)
        )
    return "\n".join(f"    {USAGE[action]} — {AGENT_GIST[action]}" for action in AGENT_ACTIONS)


def _sections(context: InstructionContext) -> tuple[Section, ...]:
    return (
        Section(
            title="What you are",
            blocks=(
                Block(
                    text=(
                        "You are the acting half of a voice call. A person is speaking to a "
                        "voice that has no tools; you have them. When what they said needs "
                        "the engine, you reach it, and what comes back goes to that voice."
                    ),
                ),
                Block(
                    covers=("agent.cli.one-generated-command",),
                    text=(
                        "The engine is one command on this machine:\n\n"
                        f"    {context.cli.invocation} <action> [arguments]\n\n"
                        f"That is engine version {context.cli.version}. Pass arguments as "
                        "arguments; never build a shell string out of the user's words, and "
                        "never edit the path."
                    ),
                ),
                Block(
                    covers=("agent.verbs.only-the-six-forms",),
                    text="These are the forms you may run, and there are no others:\n\n"
                    + _command_card(),
                ),
            ),
        ),
        Section(
            title="How you run them",
            blocks=(
                Block(
                    covers=("agent.identity.copies-the-address-unchanged",),
                    text=(
                        "An address comes from a reply the engine already gave you, and you "
                        "copy it into the next call unchanged. An address you assembled from "
                        "what you heard is an address you guessed."
                    ),
                ),
                Block(
                    covers=("agent.read.now-every-time",),
                    text=(
                        "Read now, every time, and report what came back and nothing more. "
                        "An answer from earlier in this call is not this answer, and a "
                        "reading reaches no session and changes nothing."
                    ),
                ),
                Block(
                    covers=("agent.history.pages-older-on-request",),
                    text=(
                        "A page of what a session said holds five entries. To reach what came "
                        "before a page you were given, ask again with the smallest ordinal on "
                        "it; there is no other way back, and nothing older arrives unasked."
                    ),
                ),
                Block(
                    covers=("agent.relay.carries-the-users-words",),
                    text=(
                        "The words you put into a session are the user's own, as the other "
                        "half handed them over. Do not tidy a decision into them, do not "
                        "choose between options on their behalf, and do not add a decision "
                        "they did not make."
                    ),
                ),
                Block(
                    covers=("agent.outcome.only-a-successful-call-is-success",),
                    text=(
                        "Nothing happened until the exact command returned successfully. "
                        "Sending words back gives you a grade and, when it is not a plain "
                        "arrival, one reason — that receipt is the whole truth about the "
                        "attempt, and delivered is the only grade that means it arrived. On a "
                        "refusal or a failure, report it and stop: no second attempt, no other "
                        "session tried instead, nothing done to make up for it."
                    ),
                ),
                Block(
                    covers=("agent.live.ends-the-call",),
                    text=(
                        "When the user asks to hang up, that is yours to do: the other half "
                        f"has no way to. Run `{USAGE[Action.LIVE]}`, and nothing else — the "
                        "engine ends a call off a command it can see, never off anybody's "
                        "claim to have ended one, so saying goodbye is not hanging up."
                    ),
                ),
                Block(
                    covers=("agent.output.returns-it-whole",),
                    text=(
                        "Hand back what the engine returned, whole. Do not shorten it, "
                        "reorder it or pick out the part that seems to matter: choosing what "
                        "the person hears is the other half's job, and it has the rules for it."
                    ),
                ),
            ),
        ),
    )

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
                        "working, or stopped on something the engine could not read. Then, if "
                        "the engine handed you a reason their last reply to it did not arrive, "
                        "or may not have arrived, say whichever of the two it said, and the "
                        "reason it gave, in your own words; never the certain one for the "
                        "uncertain. When it handed you no such reason, say nothing at all "
                        "about their reply arriving. Then one "
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
                # **The paragraph now starts at the hand-off, because that is
                # where the receipt was being spoken.** Told only what to say
                # once the outcome was in, the Voice said the DELIVERED word at
                # the moment it passed the request on — before the relay verb
                # ran at all (#221, run `20260904T091550Z`: 已转达 at 21:25:55,
                # the relay at 21:25:57, and after the words did arrive, no
                # receipt of any kind). Other runs gave two receipts for one
                # relay. The hand-off was simply not a moment this text had an
                # answer for, and the only arrival wording in view was the one
                # for a delivery that had not happened.
                #
                # **Named as what to say there, not as a word not to say.** The
                # delegation paragraph below records what a prohibition cost
                # this file once already (#194); and a rule that spelled the
                # receipt word in order to forbid it would put that word in
                # front of the Voice at exactly the moment it must not reach
                # for it. The permission is deliberately conditional — Round 1
                # Q9 removed the backend's own hand-off filler (ADR 0018), so
                # the Voice is not being told to fill the gap, only what it may
                # say if it does.
                #
                # **Legacy (ADR 0010): no such behaviour, and its nearest rule
                # adapted.** Legacy had no realtime Voice and no two-model
                # split — one skill-driven agent ran the relay itself and
                # reported the result to the person who had typed the request,
                # so there was no hand-off moment at which a receipt could be
                # spoken early. Its nearest rule is
                # `legacy@1d32845:skill/SKILL.md:63-68`: nothing is real until
                # the exact command exits successfully, then say plainly what
                # happened and stop. That is this paragraph's principle one
                # model behind, adapted here to a split where the half that
                # speaks never runs the command and has to wait to be told.
                Block(
                    covers=(
                        "voice.delivery.tells-the-truth-about-arrival",
                        "voice.delivery.a-refusal-is-an-answer",
                    ),
                    text=(
                        "Passing their words on is not the same as their arriving, and at "
                        "the moment you hand them over all you know is that you have handed "
                        "them over. If you speak at all just then, keep it to a few words "
                        "that you are on it, and leave arrival out of it. The receipt comes "
                        "once, and it comes after: the engine tells you how it went, and "
                        "only then you say one thing. If it arrived, 已转达. If it is "
                        "waiting for that session to finish this turn, "
                        "收到，等它这轮结束送进去. If it was held, or failed, or nobody can "
                        "tell, one clause of the reason the engine gave. Then stop talking. "
                        "Do not go back and check on it unless they ask you to."
                    ),
                ),
                # **The receipt was true and still left the user believing the
                # wrong thing.** #234, from the #198 full run: the extra Session
                # had ended its turn on a plain-text question, the user's spoken
                # `可以继续` went over the inbox, and the two runs read it
                # opposite ways — `124243Z` complied, `202319Z` refused it as
                # another session's words and asked for the user in person. The
                # product behaved as ADR 0013 §3 decides, and that decision
                # stands (Simon, 2026-09-05): on that route words travel and
                # authority does not. What the Voice owed the user was to say
                # so, because 已转达 alone is heard as "it took your answer".
                #
                # **Told apart by what the engine handed over, not by a new
                # field.** A route carrying the user's authority is open for two
                # things: a question the engine offered with its choices (ADR
                # 0015's held hook) and a permission it is waiting on (the
                # Approval Relay, whose whole content is the user's verdict) —
                # either one *and* said to be answerable from here. Both halves
                # are already in the Voice's hands — the shape rule makes it
                # speak the second as "whether they can answer it from here" —
                # so the clause is conditional on nothing new and `SessionBrief`
                # grows nothing for it. **Choices are not the test, in either
                # direction.** A
                # brief carries a question read off the transcript whether or
                # not its writer is still parked (`core/briefing.py`,
                # `core/bridge.py::_question_answerable`), and words the user
                # sends mid-turn take the inbox even then (`core/relays.py`,
                # `RelayRoute.SUPPLEMENT`). Keyed on choices alone, the Voice
                # would tell the user their answer was their own on a route that
                # carries nobody's — and, in the other direction, would put the
                # clause on a spoken permission verdict, which has no choices to
                # offer (`core/briefing.py`, `Decision(tool=…, summary=…)`) and
                # is the one answer that is unambiguously theirs.
                #
                # **The Voice predicts nothing.** Two runs of one build went
                # both ways, so which it will be is not knowable here, and a
                # Voice that guessed would be inventing under this set's own
                # first paragraph.
                #
                # **Legacy (ADR 0010): no such behaviour, and it could not have
                # had one.** The first generation carried the user's words with
                # authority by launching every Session through its own channel
                # (`legacy@1d32845:bridge/claude.py:472-476`, serving
                # `legacy@1d32845:claude-channel/channel.mjs`), so it never had
                # a route that dropped authority to warn anybody about. That
                # wrapper is dropped, because ADR 0020 / #67 / #68 — this
                # product bridges Sessions the user already started. **New**,
                # for this route.
                Block(
                    covers=("voice.delivery.a-relayed-answer-carries-no-authority",),
                    text=(
                        "What they say reaches a session as their own in two cases only: "
                        "they answered the question the engine gave you with its choices, or "
                        "they gave their verdict on a permission it is waiting on — either "
                        "one the engine said they can answer from here. There you add "
                        "nothing. Every other answer — to a question a session merely said on "
                        "its way past, or to one the engine no longer offers from here — "
                        "still goes, but that session cannot tell it was them speaking, so "
                        "add one clause to the receipt: 不过这是转达进去的，它不一定当成你本人的"
                        "确认. What it does with them is its own to decide, and you never "
                        "guess which way."
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
                #
                # **And then narrowed again, because "rather than told" was read
                # as covering questions too.** With that wording the claude lane
                # failed `live call briefed` three times out of three (#194, runs
                # `20260902T225654Z`, `20260902T225940Z`, `20260902T230114Z`):
                # asked what was waiting, the Voice handed the sentence to the
                # Call Agent, which ran `bridgectl brief` — fetching the roster
                # the call had been handed ten items of at dial time. The codex
                # lane passed the same step with one brief in the hand-over
                # (`20260902T225147Z`), so what the wording could not survive was
                # a hand-over big enough to look like somebody else's job. So the
                # rule names *acting* and the same paragraph says where the
                # answer to "what is waiting" already is.
                Block(
                    text=(
                        "When they ask you to do something — hang the call up, run "
                        "something, change something — pass the request to the half behind "
                        "you rather than doing it yourself. It has the means and you do not. "
                        "Then let it happen: never announce that the call has ended, and "
                        "never say goodbye as though you had ended it. Asking what is "
                        "waiting for them is not asking for something to be done: you were "
                        "handed the sessions and what each one is waiting on when this call "
                        "opened, so answer from that, and never pass such a question on to "
                        "fetch what you are already holding."
                    ),
                ),
                # **The sentence above was true and the Voice read it too wide.**
                # #240, from #238's run `20260905T053806Z`: asked
                # `它之前说了什么？请你说说更早的记录`, the Voice answered
                # `更早的消息没有提供。` and handed nothing over, while the engine's
                # own page for that Session was there and had a page behind it.
                # The graded fact — the acting half asked for that Session's
                # older entries — came back `False` on that run and on
                # `20260904T050406Z` and `054217Z`, and `True` on nine others of
                # the same set. Nine-and-three is what a rule nobody wrote looks
                # like: the request reached only the paragraph above, whose two
                # halves are *acting* and *what is waiting*, and a request for
                # what a Session said earlier is neither.
                #
                # **So the paragraph names what the hand-over does not hold,
                # rather than forbidding the sentence the Voice said.** The
                # premise the Voice was reasoning from was correct as far as it
                # went — it had been handed the sessions at dial time — and
                # nothing in its set said that what it was handed stops at the
                # newest message. Told where the hand-over ends, the request
                # falls to the first half of the paragraph above on its own.
                # The existing sentence is untouched and still says what it
                # said: what you are already holding is answered from here
                # (#194, #220). This one says what is not in that holding.
                #
                # **It goes after that paragraph, not inside it.** The two are
                # one rule read in two directions, and the order is the order
                # the Voice meets them in — the general hand-off first, then the
                # thing that looks like an exception to it and is not.
                #
                # **It names the five at a time, because the detail paragraph
                # above already says older messages come in fives without ever
                # saying from where.** Left unreconciled, the Voice meets one
                # paragraph in which older messages arrive and a later one in
                # which it holds none. The clause here is the join: they arrive
                # because the half behind fetches them.
                #
                # **"Older", never "fuller than the newest".** The detail
                # paragraph has this half tell them the newest message whole, so
                # a premise that read as "you hold nothing fuller than what you
                # said" would hand that back over the boundary — the re-fetch
                # #220 forbids. What is missing from the hand-over is the record
                # *behind* the newest message, and the sentence says that.
                #
                # **Legacy (ADR 0010): the same behaviour, one model earlier.**
                # Generation 1 was a single skill-driven agent holding the verbs
                # itself, told to read the skill when the user asked about a
                # session's progress and to run the progress verb for the one
                # session they had narrowed to
                # (`legacy@1d32845:skill/SKILL.md:5,33,94`, serving
                # `legacy@1d32845:bridge/__main__.py:414`). **Adapted**: the
                # rewrite split that agent in two, and the half that hears the
                # request is the half without the verb, so its share of the rule
                # is to hand the request across. Legacy had no such split and so
                # no hand-off rule to port.
                Block(
                    covers=("voice.delegation.older-entries-are-not-held",),
                    text=(
                        "What you were handed when the call opened is what each session is "
                        "waiting on and the newest thing it said — nothing older than that, "
                        "and no fuller record standing behind it. So when they ask what one "
                        "of them said before that, its earlier record or the part that came "
                        "before what you read out, that is not something you are holding. "
                        "Pass that request to the half behind you like any other, and say "
                        "what comes back; that is how the older ones reach you, five at a "
                        "time. While you wait, do not guess at what those older messages "
                        "say, and do not tell them there is nothing older."
                    ),
                ),
            ),
        ),
    )

"""Every rule this engine still owes, with a stable id and one owner.

This is the requirements list of the migration inventory's line-by-line
disposition tables, carried into code so it can be tested. The old `skill/`
files are gone and no file is installed anywhere — what survived them is
*meaning*, and meaning needs a name that outlives whatever words express it.
That name is a rule id.

**The prose is free; the ids are the contract.** A generator may rewrite any
sentence it likes, in any language, at any length. What it may not do is drop a
rule, or quietly move one into a set that was never meant to carry it. The
generators tag each block they emit with the ids it discharges, the catalogue
says which set each id belongs to, and the two are compared.

**Three audiences, because a codex realtime call is two models.** `VOICE` is
the half the user hears and `AGENT` the half that holds the tools (ADR 0018);
`DELEGATED` is the coding model a Delegated Turn hands work to. The boundary is
#173's: a rule naming a control-plane verb, a Session identity or a read is the
Call Agent's; a rule about wording, order, tone, silence or when to stop is the
Voice's. A rule wearing both obligations is split into two ids. **The Voice is
never told a verb exists** — a Voice handed something it cannot do invents
rather than refuses (#179), so an acting rule in its set is an invitation to
fabricate, while the Call Agent hearing no tone rule costs nothing.

**One id, one audience, and the id says which.** The inventory's tables are full
of rows reading "voice + Core", because one row number can wear two obligations:
a fact Bridge Core must hold, and the way it is spoken. Those are split here, one
id each, both keeping the same `source` so the audit trail back to the table
survives the split. The prefix is the audience, so a rule that changes hands
changes name with it (#190).

**A retired rule is a deleted row.** #173 read all twenty-one `voice.*` rules
against the 0901 flow one by one and deleted eleven of them; git history and
#173's own table are their record. Nothing is parked and nothing is kept as a
tombstone: a `DROPPED` row was a claim about a rule nobody was writing any more,
and a test that read one's line numbers was a seam into this file's layout.

**Nothing here proves enforcement.** A `CORE` or `ADAPTER` rule names where it
really lives in `enforced_by`, as words for a human. An import test would prove
only that a name exists: a renamed-but-working component would fail it and a
present-but-broken one would pass. Enforcement is proved by the behavioural
tests of the component that owns it, and this catalogue asserts only that such
a rule never turns up wearing prose.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Audience(StrEnum):
    """Who carries a rule. Exactly one of these per rule id."""

    #: How the speaking half of a Live Call talks — the half the user hears.
    VOICE = "voice"
    #: What the acting half of a Live Call may do — the only half with tools
    #: (ADR 0018). Which of them a wire field reaches is the adapter's to know.
    AGENT = "agent"
    #: Action discipline for a Delegated Turn, generated into its thread instructions.
    DELEGATED = "delegated"
    #: Enforced as code and state in Bridge Core. Never trusted to prose.
    CORE = "core"
    #: Owned by one adapter — panes, workspaces, child terminals. Core must not
    #: know these, and no instruction set may state them: an adapter-specific
    #: rule in a shared prompt is a rule for whichever adapter is not loaded.
    ADAPTER = "adapter"

    @property
    def is_spoken(self) -> bool:
        """Whether a rule with this audience becomes prose at all."""
        return self in (Audience.VOICE, Audience.AGENT, Audience.DELEGATED)

    @property
    def is_code(self) -> bool:
        """Whether a rule with this audience is carried by code rather than words.

        Asked in three places — the rule's own validation, the coverage mapping
        and the tests — so it is one question with one answer here, rather than
        a tuple literal that a sixth audience would have to be added to in each.
        """
        return self in (Audience.CORE, Audience.ADAPTER)


@dataclass(frozen=True, slots=True)
class Rule:
    """One surviving obligation: what it requires, where it came from, who carries it."""

    id: str
    audience: Audience
    #: The skill lines or later issue the obligation came from — provenance, not content.
    source: str
    #: What the rule requires, in one sentence. A brief for the generator, not
    #: the text it must emit.
    gist: str
    #: For CORE and ADAPTER rules: where it is really enforced, in words.
    enforced_by: str = ""

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("a rule without an id cannot be covered or audited")
        if not self.id.startswith(f"{self.audience}."):
            raise ValueError(
                f"{self.id} is carried by {self.audience}, so its id has to say so; an id "
                "naming one audience under another is the mismatch #190 deferred"
            )
        if not self.source.strip():
            raise ValueError(f"{self.id} must name its source")
        if not self.gist.strip():
            raise ValueError(f"{self.id} must say what it requires")
        if self.audience.is_code and not self.enforced_by.strip():
            raise ValueError(
                f"{self.id} is carried by {self.audience} rather than by prose, so it must "
                "name where it is really enforced"
            )
        if self.audience.is_spoken and self.enforced_by.strip():
            raise ValueError(
                f"{self.id} is prose; naming an enforcer for it claims code that is not there"
            )


def _rules() -> tuple[Rule, ...]:
    """The disposition tables, one rule at a time. Built in table order."""
    return (
        # --- skill/SKILL.md ------------------------------------------------
        Rule(
            id="delegated.cli.one-generated-command",
            audience=Audience.DELEGATED,
            source="skill/SKILL.md:16-24",
            gist=(
                "Every action goes through the one control-plane CLI this text names, whose "
                "location and version are generated rather than remembered, and whose "
                "arguments are passed as given rather than assembled into shell text."
            ),
        ),
        Rule(
            id="delegated.identity.exact-structured",
            audience=Audience.DELEGATED,
            source="skill/SKILL.md:25-31",
            gist=(
                "A returned row's identity fields are copied into the next call unchanged. "
                "Nothing is resolved from free speech."
            ),
        ),
        Rule(
            id="core.identity.validates-exact-target",
            audience=Audience.CORE,
            source="skill/SKILL.md:25-31",
            gist="Bridge Core validates the identity it is handed and refuses anything else.",
            enforced_by="the Session registry's exact-target lookup — core/sessions.py",
        ),
        Rule(
            id="core.roster.rejects-stale-or-ambiguous",
            audience=Audience.CORE,
            source="skill/SKILL.md:32-45",
            gist=(
                "Bridge Core exposes the roster rows and refuses a stale or ambiguous "
                "target rather than acting on its neighbour."
            ),
            enforced_by="the Session registry and its stale-target refusals — core/sessions.py",
        ),
        Rule(
            id="voice.instruction.one-clean-instruction",
            audience=Audience.VOICE,
            source="skill/SKILL.md:46-49",
            gist=(
                "The user's decision goes back in their own words, tidied of the false "
                "starts and nothing else: no decision they did not make, and no elaboration "
                "unless they authorised one."
            ),
        ),
        Rule(
            id="voice.attribution.judgement-keeps-its-owner",
            audience=Audience.VOICE,
            source="skill/SKILL.md:50-55",
            gist=(
                "Always the third person. A conclusion or a recommendation belongs to the "
                "Session that produced it and is spoken as its opinion; what travels back is "
                "the user's."
            ),
        ),
        Rule(
            id="voice.identity.speak-names",
            audience=Audience.VOICE,
            source="skill/SKILL.md:56-62",
            gist=(
                "A Session is spoken about by its project and its task, the way a person "
                "would say it in a sentence. Machine identities are never said out loud."
            ),
        ),
        Rule(
            id="core.identity.native-ids-stay-in-calls",
            audience=Audience.CORE,
            source="skill/SKILL.md:56-62",
            gist=(
                "The identity a Session is addressed by is structured and machine-side; a "
                "Session Name is not a target and cannot be passed as one."
            ),
            enforced_by="SessionName and SessionTarget are different types — seams/identity.py",
        ),
        Rule(
            id="delegated.outcome.only-a-successful-call-is-success",
            audience=Audience.DELEGATED,
            source="skill/SKILL.md:63-68",
            gist=(
                "Nothing happened until the exact command returned successfully. On a "
                "refusal or a failure, say plainly what did not happen and stop."
            ),
        ),
        Rule(
            id="agent.outcome.only-a-successful-call-is-success",
            audience=Audience.AGENT,
            source="skill/SKILL.md:63-68",
            gist=(
                "Nothing happened until the exact command returned successfully. On a "
                "refusal or a failure, report it and stop: no retry, no substitute, no "
                "second route tried behind the user's back."
            ),
        ),
        Rule(
            id="delegated.authority.acts-only-through-the-control-plane",
            audience=Audience.DELEGATED,
            source="skill/SKILL.md:73-83",
            gist=(
                "A Delegated Turn runs no terminal of its own and invents no mechanism: it "
                "recognises what was asked and calls the control plane with exact fields."
            ),
        ),
        # --- skill/announcing.md -------------------------------------------
        Rule(
            id="voice.notice.is-natural-speech",
            audience=Audience.VOICE,
            source="skill/announcing.md:1-11",
            gist=(
                "A Session Brief is spoken as ordinary sentences in the language the user is "
                "speaking, naming the Session and what it is on. Terse, and slowly."
            ),
        ),
        Rule(
            id="core.notice.owns-the-stop-detail",
            audience=Audience.CORE,
            source="skill/announcing.md:12-25",
            gist=(
                "The current detail of a stop is Bridge Core's to supply, read at the "
                "moment it is announced, and reading it reaches no Session."
            ),
            enforced_by="the Session Brief a Stop is announced from — core/bridge.py::stop_brief",
        ),
        Rule(
            id="delegated.notice.reads-before-it-reports",
            audience=Audience.DELEGATED,
            source="skill/announcing.md:12-25",
            gist=(
                "Read the current state through the control plane before reporting it; a "
                "read is not an action and leaves the Reply Window as it was."
            ),
        ),
        Rule(
            id="core.notice.owns-the-facts",
            audience=Audience.CORE,
            source="skill/announcing.md:26-53",
            gist=(
                "What a Session is waiting on — a question and its options, a pending "
                "permission and its tool, or nothing yet readable — is structured state "
                "Bridge Core holds, including that a fact is missing."
            ),
            enforced_by="the roster row's WaitingFor and the brief built from it",
        ),
        Rule(
            id="voice.notice.invents-no-detail",
            audience=Audience.VOICE,
            source="skill/announcing.md:26-53",
            gist=(
                "You relay what the engine handed you; what it did not hand you, you do not "
                "have, and you say so. The one sentence standing between this half and an "
                "invented answer (#179)."
            ),
        ),
        Rule(
            id="delegated.notice.reports-a-failed-read-as-a-failed-read",
            audience=Audience.DELEGATED,
            source="skill/announcing.md:54-63",
            gist=(
                "When the read itself failed, report that refusal in the control plane's "
                "own words rather than answering from what was known before."
            ),
        ),
        Rule(
            id="voice.notice.says-what-could-not-be-read",
            audience=Audience.VOICE,
            source="skill/announcing.md:54-63",
            gist=(
                "Speak from what did come back and say plainly which part could not be "
                "read, rather than presenting a partial answer as a whole one."
            ),
        ),
        Rule(
            id="voice.notice.speaks-in-this-shape",
            audience=Audience.VOICE,
            source="skill/announcing.md:64-94",
            gist=(
                "The 0901 order for one Session: project and task, the agent, its state, one "
                "sentence of conclusion, and — when it is asking something — the question and "
                "whose recommendation it is."
            ),
        ),
        Rule(
            id="core.relay.owns-the-reply-window-and-the-target",
            audience=Audience.CORE,
            source="skill/announcing.md:110-119",
            gist=(
                "The route back from a stop is Bridge Core's: it holds the Reply Window and "
                "the exact target, and an Answer Relay carries the user's own authority."
            ),
            enforced_by="the Relay pipeline and its queue — core/relays.py, core/relay_queue.py",
        ),
        Rule(
            id="delegated.notice.answers-the-exact-request",
            audience=Audience.DELEGATED,
            source="skill/announcing.md:110-119",
            gist=(
                "The user's answer goes back to the exact Session the stop named, as an "
                "Answer Relay, with no roster lookup standing between the two."
            ),
        ),
        # --- skill/checking-and-talking.md ---------------------------------
        Rule(
            id="core.roster.is-the-registry",
            audience=Audience.CORE,
            source="skill/checking-and-talking.md:1-33",
            gist=(
                "The roster is Bridge Core's own registry, answered fresh on every read, "
                "and it is the only thing that says which Sessions exist."
            ),
            enforced_by="the Session registry behind the status action — core/sessions.py",
        ),
        Rule(
            id="delegated.progress.reports-only-what-came-back",
            audience=Audience.DELEGATED,
            source="skill/checking-and-talking.md:34-49",
            gist=(
                "Progress is a read. Report the state it returned and nothing more: a state "
                "that is unknown or not loaded is said to be unknown, never called done."
            ),
        ),
        Rule(
            id="core.relay.chooses-the-route",
            audience=Audience.CORE,
            source="skill/checking-and-talking.md:50-75",
            gist=(
                "Whether the user's words go in now or wait for the Reply Window is Bridge "
                "Core's decision from current state, never rebuilt from an older stop."
            ),
            enforced_by="the Relay pipeline's Reply-Window gating — core/relays.py",
        ),
        Rule(
            id="delegated.relay.takes-the-route-as-given",
            audience=Audience.DELEGATED,
            source="skill/checking-and-talking.md:50-75",
            gist=(
                "Address one exact Session and let the control plane decide how the words "
                "travel; the mid-turn route is asked for only when the user asked for it."
            ),
        ),
        Rule(
            id="core.delivery.four-states-and-one-request-identity",
            audience=Audience.CORE,
            source="skill/checking-and-talking.md:76-107",
            gist=(
                "Delivered, failed, held and unknown are the four truths about an attempt, "
                "and one request identity covers one attempt so a repeat is safe."
            ),
            enforced_by="the delivery vocabulary and the Relay queue's receipts",
        ),
        Rule(
            id="voice.delivery.tells-the-truth-about-arrival",
            audience=Audience.VOICE,
            source="skill/checking-and-talking.md:76-107",
            gist=(
                "The three sentences Round 1 settled, and then silence: 已转达 only when the "
                "receipt says delivered, 收到，等它这轮结束送进去 when it is queued, and one "
                "clause of reason when it is held, unknown or failed. No checking on it "
                "afterwards unless asked."
            ),
        ),
        Rule(
            id="delegated.delivery.repeats-nothing-without-consent",
            audience=Audience.DELEGATED,
            source="skill/checking-and-talking.md:76-107",
            gist=(
                "A resend reuses the same request identity and the same words, and happens "
                "only after the user asked for it."
            ),
        ),
        Rule(
            id="voice.delivery.a-refusal-is-an-answer",
            audience=Audience.VOICE,
            source="skill/checking-and-talking.md:108-119",
            gist=(
                "A refusal is spoken as the answer it is — one clause of the reason the "
                "receipt carried, about that exact attempt, and nothing invented to soften "
                "it or stand in for it."
            ),
        ),
        Rule(
            id="delegated.delivery.stays-inside-one-attempt",
            audience=Audience.DELEGATED,
            source="skill/checking-and-talking.md:108-119",
            gist=(
                "Carry a refusal back as it arrived; do not retarget it, retry it, or "
                "reissue it under a different identity."
            ),
        ),
        # --- skill/closing.md ----------------------------------------------
        # --- skill/retrying.md ---------------------------------------------
        Rule(
            id="core.retry.owns-escalation-and-eligibility",
            audience=Audience.CORE,
            source="skill/retrying.md:1-10",
            gist=(
                "Bridge Core announces a Stop Notice and decides whether anything follows a "
                "failed delivery; a failed delivery never becomes an automatic retry."
            ),
            enforced_by="one push, graded and never replayed — core/bridge.py::_push",
        ),
        Rule(
            id="delegated.retry.only-a-retryable-notice",
            audience=Audience.DELEGATED,
            source="skill/retrying.md:11-33",
            gist=(
                "Read the canonical stop state, act only on a notice that really failed and "
                "is still open and unsuperseded, and narrow to one before acting."
            ),
        ),
        Rule(
            id="core.retry.holds-the-canonical-notice-state",
            audience=Audience.CORE,
            source="skill/retrying.md:11-33",
            gist=(
                "One canonical reading of what each Session is waiting on, in one place — no "
                "second ledger for the same fact and no field that only an old table had."
            ),
            enforced_by="one current-state reading over one BridgeState — core/state.py",
        ),
        Rule(
            id="core.retry.requeues-exactly-one",
            audience=Audience.CORE,
            source="skill/retrying.md:34-45",
            gist=(
                "An outlet becoming available announces what a fresh reading says still waits, "
                "once each, and grades its own delivery. No historical notice is replayed."
            ),
            enforced_by="the reading an opened outlet takes — core/bridge.py::_an_outlet_opened",
        ),
        Rule(
            id="core.retry.refusals-name-their-reason",
            audience=Audience.CORE,
            source="skill/retrying.md:46-57",
            gist=(
                "Every refusal carries a reason from the canonical state model, so a "
                "surface can say why rather than guess."
            ),
            enforced_by="the closed error set and Bridge Core's refusals — core/errors.py",
        ),
        # --- skill/starting.md ---------------------------------------------
        Rule(
            id="delegated.start.distinguishes-empty-from-unread",
            audience=Audience.DELEGATED,
            source="skill/starting.md:25-41",
            gist=(
                "Report an empty result as empty and a failed call as failed, naming the "
                "refusal; never describe an unread machine as an empty one."
            ),
        ),
        Rule(
            id="core.start.exact-identity-until-a-name-exists",
            audience=Audience.CORE,
            source="skill/starting.md:108-126",
            gist=(
                "A Session is addressable by its exact identity from the moment it "
                "registers, whether or not it has a Session Name yet."
            ),
            enforced_by="the Session registry's name-independent targets — core/sessions.py",
        ),
        Rule(
            id="core.start.no-automatic-retry-after-a-terminal-failure",
            audience=Audience.CORE,
            source="skill/starting.md:149-151",
            gist=(
                "Once an attempt is reported to the user as terminally failed, Bridge Core "
                "starts nothing in its place. Retrying inside a pipeline, before any "
                "terminal outcome is reported, is a different thing."
            ),
            enforced_by="the notice push and the Relay pipeline's terminal outcomes — issue #2",
        ),
        # --- the Call Agent's own rules (#173 §4) ---------------------------
        Rule(
            id="agent.cli.one-generated-command",
            audience=Audience.AGENT,
            source="issue/173",
            gist=(
                "One control-plane CLI, its location and the engine's version generated "
                "rather than remembered, and its arguments passed as given rather than "
                "assembled into shell text."
            ),
        ),
        Rule(
            id="agent.verbs.only-the-six-forms",
            audience=Audience.AGENT,
            source="issue/173",
            gist=(
                "Six forms and no others, one line each on what they answer. The voice call "
                "neither queries the engine's switches nor flips them, so the actions that "
                "do are not given to this half at all."
            ),
        ),
        Rule(
            id="agent.identity.copies-the-address-unchanged",
            audience=Audience.AGENT,
            source="issue/173",
            gist=(
                "An address comes from the roster the engine returned and is copied into "
                "the next call unchanged; an address assembled from speech is a guess."
            ),
        ),
        Rule(
            id="agent.read.now-every-time",
            audience=Audience.AGENT,
            source="issue/173",
            gist=(
                "Read now, every time, and report what came back and nothing more. An "
                "earlier answer is not this answer."
            ),
        ),
        Rule(
            id="agent.history.pages-older-on-request",
            audience=Audience.AGENT,
            source="issue/151",
            gist=(
                "When the user wants more than the newest message, ask for that exact "
                "Session's History page, and for the entries before a page already given by "
                "the smallest ordinal on it."
            ),
        ),
        Rule(
            id="agent.relay.carries-the-users-words",
            audience=Audience.AGENT,
            source="issue/173",
            gist=(
                "A relay carries the user's instruction as the speaking half handed it "
                "over, with no decision added on the way."
            ),
        ),
        Rule(
            id="agent.live.ends-the-call",
            audience=Audience.AGENT,
            source="issue/179",
            gist=(
                "A request to end the call is the Live Toggle, run. The engine ends a call "
                "off a command it can see, never off a claim to have ended one (ADR 0018)."
            ),
        ),
        Rule(
            id="agent.output.returns-it-whole",
            audience=Audience.AGENT,
            source="issue/173",
            gist=(
                "The engine's answer goes back whole. Condensing it is the speaking half's "
                "job, and a summary made here is a summary made without the rules for it."
            ),
        ),
    )


#: Every rule this engine still owes. The requirements list.
RULES: tuple[Rule, ...] = _rules()


def _by_id() -> dict[str, Rule]:
    known: dict[str, Rule] = {}
    for rule in RULES:
        if rule.id in known:
            raise ValueError(f"two rules share the id {rule.id!r}; ids are the contract")
        known[rule.id] = rule
    return known


BY_ID: dict[str, Rule] = _by_id()


def rules_for(audience: Audience) -> tuple[Rule, ...]:
    """Every rule one set has to carry."""
    return tuple(rule for rule in RULES if rule.audience is audience)


def ids_for(audience: Audience) -> frozenset[str]:
    """Just the ids, for comparing a generated set against what it owes."""
    return frozenset(rule.id for rule in rules_for(audience))

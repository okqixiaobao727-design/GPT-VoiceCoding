"""Every rule that survived the old skill, with a stable id and one owner.

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

**One id, one audience.** The inventory's tables are full of rows reading
"voice + Core" or "Core + voice + delegated", because one row number can wear
two obligations: a fact Bridge Core must hold, and the way the voice thread is
told to speak it. Those are split here, one id each, both keeping the same
`source` so the audit trail back to the table survives the split.

**A dropped rule is recorded, not omitted.** An id with `Audience.DROPPED` is
how "this must not come back" becomes a thing the suite can fail on. Silence
would prove nothing — a rule nobody wrote and a rule deliberately deleted look
identical when both are absent.

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

    #: House rules for the voice thread, generated into `realtimeStartInstructions`.
    VOICE = "voice"
    #: Action discipline for a Delegated Turn, generated into its thread instructions.
    DELEGATED = "delegated"
    #: Enforced as code and state in Bridge Core. Never trusted to prose.
    CORE = "core"
    #: Owned by one adapter — panes, workspaces, child terminals. Core must not
    #: know these, and neither instruction set may state them: an adapter-specific
    #: rule in a shared prompt is a rule for whichever adapter is not loaded.
    ADAPTER = "adapter"
    #: Deleted on purpose. Must appear in no instruction set, ever.
    DROPPED = "dropped"

    @property
    def is_spoken(self) -> bool:
        """Whether a rule with this audience becomes prose at all."""
        return self in (Audience.VOICE, Audience.DELEGATED)

    @property
    def is_code(self) -> bool:
        """Whether a rule with this audience is carried by code rather than words.

        Asked in three places — the rule's own validation, the coverage mapping
        and the tests — so it is one question with one answer here, rather than
        a tuple literal that a fifth audience would have to be added to in each.
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
            id="voice.orientation.no-screen",
            audience=Audience.VOICE,
            source="skill/SKILL.md:9-15",
            gist=(
                "The user is listening, not looking. Bridge Core is the only authority on "
                "what exists and what state it is in; nothing is answered from memory."
            ),
        ),
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
            id="voice.target.disambiguate-or-ask",
            audience=Audience.VOICE,
            source="skill/SKILL.md:32-45",
            gist=(
                "A spoken name acts only when a freshly read roster narrows it to exactly "
                "one row. Zero or several means ask, and act on nothing until one matches. "
                "An identity a command already returned needs no lookup."
            ),
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
                "Spoken words ramble; what reaches a Session is one clean instruction in "
                "the user's own language, with no decision in it the user did not make."
            ),
        ),
        Rule(
            id="voice.attribution.judgement-keeps-its-owner",
            audience=Audience.VOICE,
            source="skill/SKILL.md:50-55",
            gist=(
                "A Session's options and recommendations are announced as that Session's "
                "own judgement; the decision carried back is the user's."
            ),
        ),
        Rule(
            id="voice.identity.speak-names",
            audience=Audience.VOICE,
            source="skill/SKILL.md:56-62",
            gist=(
                "Sessions are spoken about by Session Name. A position — 'the third one' — "
                "refers to the row just read out, and is resolved to that row's identity "
                "before anything acts."
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
            id="voice.conversation.no-action",
            audience=Audience.VOICE,
            source="skill/SKILL.md:71-72",
            gist="Pure conversation performs no control-plane action.",
        ),
        Rule(
            id="voice.authority.no-identity-from-the-screen",
            audience=Audience.VOICE,
            source="skill/SKILL.md:73-83",
            gist=(
                "Session identity and status come from Bridge Core alone — never from a "
                "screen capture, OCR, the clipboard, a terminal listing or a keystroke."
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
        Rule(
            id="dropped.skill.frontmatter-and-trigger",
            audience=Audience.DROPPED,
            source="skill/SKILL.md:1-8",
            gist="Installed-skill identity, frontmatter and its read-this-first trigger.",
        ),
        Rule(
            id="dropped.skill.spoken-duty-toggle",
            audience=Audience.DROPPED,
            source="skill/SKILL.md:69-70",
            gist=(
                "The spoken Duty-toggle phrase, and the idea that a switch is anything "
                "other than a control-plane action that is never gated."
            ),
        ),
        Rule(
            id="dropped.skill.file-router-choreography",
            audience=Audience.DROPPED,
            source="skill/SKILL.md:84-96",
            gist="Branch tables pointing at other files, and read-this-skill-first ordering.",
        ),
        # --- skill/announcing.md -------------------------------------------
        Rule(
            id="voice.notice.is-natural-speech",
            audience=Audience.VOICE,
            source="skill/announcing.md:1-11",
            gist=(
                "A Stop Notice is spoken as ordinary sentences in the user's language, "
                "naming the Session by its name and the task it is on."
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
            enforced_by="the escalation pipeline's Notice and its state — core/escalation.py",
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
            enforced_by="the approval pipeline's PendingApproval and the escalation Notice",
        ),
        Rule(
            id="voice.notice.invents-no-detail",
            audience=Audience.VOICE,
            source="skill/announcing.md:26-53",
            gist=(
                "Speak only the facts that came back. A missing recommendation means the "
                "Session recommended nothing; a missing detail is said to be missing, "
                "never reconstructed from older material."
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
                "Announce from what did come back and say plainly which part could not be "
                "read, rather than presenting a partial answer as a whole one."
            ),
        ),
        Rule(
            id="voice.notice.speaks-in-this-shape",
            audience=Audience.VOICE,
            source="skill/announcing.md:64-94",
            gist=(
                "Name and state first, then the question in one sentence, then each option "
                "by name, then whose recommendation it is, then what is needed from the "
                "user — and when the detail is known to be stale, say the gap out loud."
            ),
        ),
        Rule(
            id="voice.notice.asks-for-no-decision-nobody-awaits",
            audience=Audience.VOICE,
            source="skill/announcing.md:95-109",
            gist=(
                "When the Reply Window is closed or the stop was superseded, say so instead "
                "of asking for a decision no Session is waiting on."
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
        Rule(
            id="dropped.announcing.cross-file-pointers",
            audience=Audience.DROPPED,
            source="skill/announcing.md:120-121",
            gist="Pointers to sibling skill files; the generated sets carry their rules directly.",
        ),
        # --- skill/checking-and-talking.md ---------------------------------
        Rule(
            id="voice.roster.withheld-sessions-are-real",
            audience=Audience.VOICE,
            source="skill/checking-and-talking.md:1-33",
            gist=(
                "Sessions the roster holds back are running and are counted when the user "
                "asks what is going on; each is described by why it cannot be pointed at, "
                "and none of them can be acted on."
            ),
        ),
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
                "Only a delivered attempt is reported as arrived. Held means it is parked "
                "in front of a human, unknown means unknown — and when a resend risks the "
                "Session seeing the words twice, say that risk and let the user decide."
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
                "Report a refusal in its own words, about that exact Session and that exact "
                "attempt — no substitute target, and no earlier success standing in for it."
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
                "Bridge Core escalates a Stop Notice and decides whether one may be retried; "
                "a failed delivery never becomes an automatic retry on its own."
            ),
            enforced_by="the escalation pipeline's notice states — core/escalation.py",
        ),
        Rule(
            id="voice.retry.failed-delivery-is-not-a-retry",
            audience=Audience.VOICE,
            source="skill/retrying.md:1-10",
            gist=(
                "Say that a notice failed to reach the user; a replay happens because they "
                "asked for one, not because the failure invited it."
            ),
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
                "One canonical stop and notice state, in one place — no second ledger for "
                "the same fact and no field that only an old table had."
            ),
            enforced_by="the escalation pipeline over one BridgeState — core/state.py",
        ),
        Rule(
            id="voice.retry.queued-is-not-delivered",
            audience=Audience.VOICE,
            source="skill/retrying.md:34-45",
            gist=(
                "A requeued notice is queued for another attempt, and saying it was "
                "delivered is a lie the user cannot check."
            ),
        ),
        Rule(
            id="core.retry.requeues-exactly-one",
            audience=Audience.CORE,
            source="skill/retrying.md:34-45",
            gist="A retry puts exactly the named notice back, and grades its own delivery.",
            enforced_by="the escalation pipeline's requeue path — core/escalation.py",
        ),
        Rule(
            id="voice.retry.no-compensating-action",
            audience=Audience.VOICE,
            source="skill/retrying.md:46-57",
            gist=(
                "Say the refusal's own reason. A refused retry is not retried, and a "
                "different notice is never replayed to make up for it."
            ),
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
            id="voice.start.an-empty-read-is-not-a-failed-read",
            audience=Audience.VOICE,
            source="skill/starting.md:25-41",
            gist=(
                "A read that succeeded and found nothing is a fact about the machine; a "
                "read that failed is no reading at all, and the two are never spoken alike."
            ),
        ),
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
            id="dropped.starting.duty-gated-reads",
            audience=Audience.DROPPED,
            source="skill/starting.md:139-148",
            gist=(
                "A read refused because voice coordination was switched off, and the "
                "spoken toggle that undid it. The control plane is never gated, so this "
                "failure no longer exists to report."
            ),
        ),
        Rule(
            id="voice.start.no-substitute-after-a-failure",
            audience=Audience.VOICE,
            source="skill/starting.md:149-151",
            gist=(
                "After a reported failure there is no automatic retry and no substitute "
                "action — no second workspace, no other agent, nothing to compensate."
            ),
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
            enforced_by="the escalation and Relay pipelines' terminal outcomes — issue #2",
        ),
        Rule(
            id="dropped.starting.the-removed-roster-read",
            audience=Audience.DROPPED,
            source="skill/starting.md:97-107",
            gist=(
                "The historical explanation of a roster read that was removed; it is not "
                "runtime discipline."
            ),
        ),
    )


#: Every surviving rule, and every deliberately dropped one. The requirements list.
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

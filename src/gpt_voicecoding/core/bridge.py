"""Bridge Core assembled — the hub, and the one loop that drains its events.

Every seam's events land on one queue and one dispatch drains it (`core.events`),
which is what makes ordering, serialisation and Reply-Window queueing naturally
the hub's business. This is that dispatch: it owns no policy of its own, it
turns each event into a call on the pipeline that owns the decision.

**Adapters are injected as Protocols.** Bridge Core never imports
`gpt_voicecoding.adapters`, so the whole hub runs against a fake call, fake
agents and a fake channel with no network and no audio (ADR 0001, principle 4).
Assembling the *real* adapters from configuration is the composition root's job
and lives with the control-plane surface, not here.

**ADR 0002 is honoured by two verbs that consult nothing.** `status` and
`flip_switch` never ask the adjudicator, so they answer with every switch off,
including Duty. Replying to text the user just sent is the same category: a
reply is not a push, and the Companion Channel is one of the control-plane's
surfaces, so an inbound message always gets an answer. If that were gated, the
one way to turn Duty back on from away from the computer would be gated too.

Three things are recorded here rather than decided here. `UserSpeech` is the
in-call transcript, and Bridge Core never parses one: spoken intent arrives as
structured control-plane calls the voice thread makes, so the event is written
to the log and nothing else. `VoiceSpeech` is the other side of the same
conversation and is treated the same way, except that the interlock is told —
the Silence Ceiling counts both speakers (#184). The control-plane command set
and the Delegated Turn's execution belong to the surfaces that own them, so both
arrive as injected handlers with honest defaults rather than being invented here.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Final

from gpt_voicecoding.core import briefing
from gpt_voicecoding.core.adjudication import SwitchAdjudicator
from gpt_voicecoding.core.briefing import RosterBrief, SessionBrief
from gpt_voicecoding.core.clock import Clock, default_clock, wall_clock
from gpt_voicecoding.core.errors import (
    BridgeCoreError,
    CallInstructionsMissing,
    ChildSessionError,
    LaneUnreadable,
    ProgressUnavailable,
    StaleSessionError,
    UnknownRelayError,
)
from gpt_voicecoding.core.escalation import EscalationPipeline, Notice, NoticeOutcome
from gpt_voicecoding.core.events import EventQueue
from gpt_voicecoding.core.instructions import InstructionContext, Instructions, generate
from gpt_voicecoding.core.interlock import CallInterlock
from gpt_voicecoding.core.lifecycle import Lifecycle
from gpt_voicecoding.core.policy import CorePolicy
from gpt_voicecoding.core.relay_queue import PendingRelay
from gpt_voicecoding.core.relays import RelayOutcome, RelayPipeline, reason_for, terminal_line
from gpt_voicecoding.core.router import Classification, InboundClass, InboundRouter, TextGrammar
from gpt_voicecoding.core.sessions import Session
from gpt_voicecoding.core.state import BridgeState
from gpt_voicecoding.core.switches import SwitchSnapshot
from gpt_voicecoding.core.verification import (
    AGENT_SEAM_PREFIX,
    CALL_SEAM,
    CHANNEL_SEAM,
    SeamLoad,
    SeamVerification,
    Verifiable,
    compare,
)
from gpt_voicecoding.seams.agent import (
    AgentAdapter,
    ApprovalRequest,
    ApprovalVerdict,
    HistoryPage,
    LaneUnavailable,
    ProgressAvailability,
    ProgressObservation,
    RelayReceipt,
    RelayRoute,
    ReplyWindow,
    ReplyWindowChanged,
    SessionEnded,
    SessionInspection,
    SessionLifecycle,
    SessionState,
    SessionStopped,
    WaitingFor,
    WaitingKind,
    derive_reply_window,
)
from gpt_voicecoding.seams.call import (
    CallAdapter,
    CallDropped,
    CallEnded,
    CallSnapshot,
    CallStarted,
    Cue,
    Dial,
    DialReason,
    HandoverItem,
    UserSpeech,
    VoiceSpeech,
)
from gpt_voicecoding.seams.companion_channel import CompanionChannel, InboundText
from gpt_voicecoding.seams.events import Event
from gpt_voicecoding.seams.identity import (
    AgentKind,
    SessionTarget,
    new_request_id,
)

_log = logging.getLogger(__name__)

#: Answers an inbound command when no control-plane surface is wired to this hub.
NO_CONTROL_SURFACE = "I recognised that command, but no control surface is wired up here"

#: Answers an inbound delegation when no Delegated Turn handler is wired.
NO_DELEGATE_HANDLER = "I can't take a delegated turn right now — nothing is wired to answer it"

#: The two lines a run is told the Voice held its call open by. Fixed strings
#: rather than a formatted one, because the acceptance step matches them: an
#: engine that says nothing when the ceiling is held leaves a whole-lane run no
#: way to tell "the call outlived the ceiling" from "the ceiling never ran"
#: (#184). One per edge, never per delta — a long answer is hundreds of those.
VOICE_SPEAKING_LINE = "the call's own Voice started speaking"
VOICE_QUIET_LINE = "the call's own Voice stopped speaking"

#: Why a call the user opened exists, and the *whole* hand-over it gets (#167
#: Q6). A user who pressed the toggle is about to say what they want; briefing
#: them on the roster they were looking at when they pressed it would be the
#: system talking first, on a call the user opened to talk.
USER_OPENED = "The user opened this call. Wait to be spoken to, then act on what they ask for."

#: Why a call the *system* dialled exists. The items after it are the roster and
#: the Sessions waiting, so this says what they are for and nothing they say.
SYSTEM_DIALLED = (
    "This call was dialled because Sessions need the user. "
    "What follows is the roster and each Session that is waiting; "
    "speak from it, and do not invent anything it does not say."
)


def stop_brief(
    session: Session | None,
    target: SessionTarget,
    waiting_for: WaitingFor,
    *,
    progress: ProgressObservation | None = None,
    question_answerable: bool = False,
) -> SessionBrief:
    """The Session Brief a Stop announces — this reading, on the row it is about.

    **Bridge Core words nothing about a Session.** `CONTEXT.md`'s *Stop Notice*
    is "a Session Brief published as text", so what a stop produces here is the
    brief and `briefing.text` is what renders it — one vocabulary for the
    channel, the log and `bridgectl brief`, which is the defect #166 named. The
    five renderers that used to live here are gone: the composer, the "it said /
    nothing said yet / oversize" line, the per-wait-kind sentence with its
    options and recommendation, the "answer it in the terminal" constant, and
    their use of a `spoken_reference` helper, which went with the approval
    announcement that was its last caller (#191). The omission wording lives in
    `briefing.NEWEST_WORDING` and the state wording in `briefing.STATE_WORDING`.

    Legacy (ADR 0010): `legacy@1d32845:bridge/host.py:213-235` announced a
    content-free notice — **adapted**, the notice now carries the brief. Naming
    the Session on it (ported in #109) is carried by the brief's name field.

    **The reading this path is announcing wins over the row's.** A Stop is read
    at the moment the Session stopped, and the sweep and reconcile paths each
    carry the wait they are about; the roster row supplies everything else — the
    Session Name, the workspace, the last activity — that the reading itself does
    not know.

    **The state is the row's, and only a row nobody holds derives it here.** A
    Stop is not a Session running, and this path used to be the one place that
    said so: it derived the state from the wait because
    `SessionRegistry.set_stop_reading` left a row that merely ended a turn in
    whatever state the last discovery pass found (#209). Since #213 the registry
    derives it, by the same rule in the one place it now lives
    (`WaitingFor.stopped_state`), so a registered row is briefed as the registry
    holds it and nothing here overrides it. The derivation is still read for the
    stand-in below — a Stop for a Session no discovery pass has landed yet has no
    row to have been folded into.
    """
    row = (
        session
        if session is not None
        else replace(_stand_in(target), state=waiting_for.stopped_state)
    )
    row = replace(
        row,
        waiting_for=waiting_for,
        progress=progress if progress is not None else row.progress,
    )
    return briefing.session(row, question_answerable=question_answerable)


#: The two `Session` fields a brief never reads, for the one row nobody observed.
#: `Session` is the roster's record and requires both; `briefing.session` reads
#: neither (`core/briefing.py::session` takes `target`, `name`, `state`,
#: `waiting_for`, `progress`, `last_activity`, `child`). Named here, once, and
#: named as unobserved rather than spelled at the call site as a plausible-looking
#: `Path("/")` and `0.0` — a stand-in that reads like a fact is the thing to
#: avoid. Nothing consumes either: this row is never registered, never persisted
#: and never answered from.
UNOBSERVED_WORKSPACE: Final = Path()
UNOBSERVED_FIRST_SEEN: Final = 0.0


def _stand_in(target: SessionTarget) -> Session:
    """A row for a Stop the roster holds no Session for, carrying only its address.

    A Stop can arrive for a Session no discovery pass has landed yet, and a
    notice nobody receives is worse than one naming the Session by the address
    the user can still answer it by — which is `spoken_name`'s own floor
    (`core/sessions.py:773-785`) and the shape `Briefing` prints for a row with
    no name. **Nothing a brief reads is invented**: there is no name, the
    progress stays the default `NOT_READ` — Briefing's word for "nobody looked" —
    and the wait is the one the Stop itself carried. The two fields `Session`
    requires and no brief reads are the constants above, and they are unobserved
    rather than assumed.
    """
    return Session(
        target=target,
        workspace=UNOBSERVED_WORKSPACE,
        first_seen=UNOBSERVED_FIRST_SEEN,
    )


def _as_read_now(read: Session, fresh: ProgressObservation) -> Session:
    """The folded row, but saying what *this* read found about its progress.

    `Session.with_progress` keeps a readable observation when a newer pass could
    not answer, which is the right rule for a roster: a row that says nothing
    where it used to say something has lost a fact rather than gained one, and
    the roster is a standing account.

    It is the wrong rule for a verb that answers *now*. `brief <address>` is one
    fresh reading taken at the moment the user is spoken to, so a read that
    failed has to reach them as a failure — otherwise the Voice reads out a
    message from some earlier tick as though it had just been said, which is the
    one thing "read at the moment you speak" exists to prevent. The roster keeps
    what it had; the brief says what it found.
    """
    if fresh.availability is not ProgressAvailability.UNREADABLE:
        return read
    return replace(read, progress=fresh)


def _state_behind(window: ReplyWindow, held: SessionState) -> SessionState:
    """The Session state a Reply Window report implies, given what we already hold.

    An open window is a Session that will take the next turn, which is `IDLE`.
    A closed one has two causes and the report cannot tell them apart — mid-turn,
    or holding a dialog — so a Session already known to be `WAITING` keeps that,
    and anything else becomes `RUNNING`. Guessing the other way would erase a
    permission dialog from the roster while it is still on the user's screen.
    """
    if held is SessionState.WAITING:
        return held
    return SessionState.IDLE if window is ReplyWindow.OPEN else SessionState.RUNNING


@dataclass(frozen=True, slots=True)
class Status:
    """Everything the control plane can ask for. Answered with any switch off."""

    switches: SwitchSnapshot
    sessions: tuple[Session, ...]
    #: Why a lane could not be enumerated at its last attempt, by agent. Empty
    #: is the ordinary case. It is here and not on a row because it is news
    #: about the lane: an unavailable lane's Sessions are not *missing*, they
    #: are unknown, and a roster that showed nothing without saying so would be
    #: claiming the machine is empty.
    lanes: Mapping[AgentKind, str]
    #: Which lanes are reading from a weaker source than usual, and which source.
    #: Distinct from `lanes`: these lanes *do* have Sessions to show, so folding
    #: the two together would hide a working lane behind a warning shaped like
    #: an outage. The Codex lane sits here whenever no shared daemon is up.
    degraded_lanes: Mapping[AgentKind, str]
    #: The call the system owns, or None. One voice surface, so one id.
    call_id: str | None
    pending_relays: tuple[PendingRelay, ...]
    #: Reply Window levels include the lane's live question-route fact, which is
    #: deliberately not copied onto the roster row.
    reply_windows: Mapping[SessionTarget, ReplyWindow] = field(default_factory=dict)


class BridgeCore:
    """The hub: one truth, five pipelines, and the loop that feeds them."""

    def __init__(
        self,
        *,
        state: BridgeState,
        call: CallAdapter,
        channel: CompanionChannel,
        agents: Mapping[AgentKind, AgentAdapter],
        events: EventQueue | None = None,
        policy: CorePolicy | None = None,
        grammar: TextGrammar | None = None,
        clock: Clock = default_clock,
        stamp: Clock = wall_clock,
        control: Callable[[Classification], Awaitable[str]] | None = None,
        delegate: Callable[[Classification], Awaitable[str]] | None = None,
        inventory: tuple[SeamLoad, ...] = (),
        instruction_context: InstructionContext | None = None,
    ) -> None:
        self._state = state
        self._call = call
        self._channel = channel
        self._agents = dict(agents)
        self._events = events if events is not None else EventQueue()
        self._policy = policy or CorePolicy()
        self._control = control
        self._delegate = delegate
        self._inventory = inventory
        #: Both generated instruction sets, made once from facts only the
        #: composition root knows — where the control-plane CLI is, and which
        #: engine it reaches. None until a root supplies them; a hub assembled
        #: for a test has no CLI to name and does not pretend to.
        self._instructions = generate(instruction_context) if instruction_context else None
        #: Durations are measured with `clock`; anything read outside this
        #: process is stamped with `stamp`. A Session's `first_seen` travels to
        #: every surface in the `sessions` payload, and a monotonic reading
        #: would name no moment on the far side.
        self._stamp = stamp
        #: An outlet transition asks the next discovery pass to reconcile its
        #: fresh rows. The transition itself performs no lane I/O (#80).
        self._reconcile_owed = False

        self.interlock = CallInterlock(call, clock=clock)
        self.adjudicator = SwitchAdjudicator(state.switches)
        self.escalation = EscalationPipeline(
            channel=channel,
            interlock=self.interlock,
            adjudicator=self.adjudicator,
            system_dial=self._system_dial,
        )
        self.relays = RelayPipeline(
            agents=agents,
            sessions=state.sessions,
            relays=state.relays,
            policy=self._policy,
            clock=clock,
        )
        self.router = InboundRouter(sessions=state.sessions, grammar=grammar)

    @property
    def instructions(self) -> Instructions | None:
        """All three instruction sets, as plain data.

        Generated once, from the catalogue and this engine's own installation.
        A Live Call is two audiences (ADR 0018): the Call adapter starts its
        realtime thread with the **agent** set, which is the half that acts, and
        the voice set waits for the Dial to give that seam a payload per
        audience. The Codex adapter starts a Delegated Turn with the delegated
        one. None of them rewrites a set, and none reads anything from disk to
        get one.
        """
        return self._instructions

    @property
    def events(self) -> EventQueue:
        """The sink every adapter is handed. One queue, one drain."""
        return self._events

    # ------------------------------------------------------------------
    # The control plane. ADR 0002: never gated, by anything, ever.
    # ------------------------------------------------------------------

    def status(self) -> Status:
        """What the system is doing. Consults no switch, so it always answers."""
        return Status(
            switches=self._state.switches.snapshot(),
            sessions=self._state.sessions.all(),
            lanes=self._state.sessions.lane_errors(),
            degraded_lanes=self._state.sessions.lane_degradations(),
            call_id=self.interlock.call_id(),
            pending_relays=self._state.relays.pending(),
            reply_windows={
                session.target: self._reply_window(session)
                for session in self._state.sessions.all()
            },
        )

    def _question_answerable(self, target: SessionTarget) -> bool:
        adapter = self._agents.get(target.agent)
        if adapter is None:
            return False
        try:
            return adapter.question_answerable(target)
        except Exception:  # noqa: BLE001 - a query can only close, never drop, a Session
            _log.exception("the %s lane could not report its question route", target.agent)
            return False

    def _reply_window(self, session: Session) -> ReplyWindow:
        if session.lifecycle is not SessionLifecycle.LIVE:
            return ReplyWindow.CLOSED
        return derive_reply_window(
            session.state,
            session.waiting_for,
            session.child,
            question_answerable=self._question_answerable(session.target),
        )

    async def history(self, target: SessionTarget, *, before: int | None = None) -> HistoryPage:
        """One page of what an exact Session said and was told, read now (#171).

        A hub verb, and a *read*: it resolves one identity, asks that lane and
        no other, and returns a page of `history_page_entries` entries
        newest-first with `older` saying whether more remain. The reference
        implementation's rule for its one-Session read, ported —
        `legacy@1d32845:bridge/daemon.py:2202-2271` and
        `legacy@1d32845:bridge/codex.py:1319-1348` resolved one exact registered
        identity, asked only that agent's own authority, and never fell back to
        another lane, a terminal or a screen. Legacy had **no paging**: its tail
        was a fixed 12 entries / 32 KB (`legacy@1d32845:config.plist:449-452`,
        `bridge/transcript.py:2841`), **dropped, because** a fixed tail cannot
        answer "the five before those". The count-bounded page and the ordinal
        cursor are new.

        **It is not a Relay and it costs no turn**, and it is **not folded into
        the roster** (ADR 0016's amendment). `inspect` keeps answering the
        newest tail and folding; a page is a separate read and is not a roster
        fact, so nothing here observes the row.

        **One lane read per page.** The registry's `resolve` supplies three of
        the four refusals — an identity nobody registered, a stale one, and a
        Child Process, which is seen and never spoken to (#68) — and the lane's
        own read supplies the fourth. A second `inspect` for a fresher staleness
        check would fold this read back into the cadence it is kept out of.

        The fourth refusal is two facts under one code:

        - *The lane could not be read.* `LaneUnreadable`, carrying the lane's
          own words.
        - *Nothing could read what it said.* `ProgressUnavailable` — a Codex
          thread the shared daemon does not hold, or a Claude Session whose
          transcript this engine was never told about. A Session that *was* read
          and had nothing before the cursor is not this: it answers with an empty
          page and `older=False`, which is an answer.
        """
        session = self._state.sessions.resolve(target)
        adapter = self._agents.get(target.agent)
        if adapter is None:
            raise LaneUnreadable(str(target.agent), "this engine has no adapter for that agent")
        try:
            page = await adapter.history(
                session.target,
                before=before,
                count=self._policy.history_page_entries,
            )
        except LaneUnavailable as unread:
            raise LaneUnreadable(str(unread.agent), unread.reason) from None
        if page.read_at is None:
            raise ProgressUnavailable(target)
        return page

    async def brief(self, target: SessionTarget | None = None) -> RosterBrief | SessionBrief:
        """The Roster Brief, or one Session Brief with Detail — read now.

        One verb with an optional address, because they are one question at two
        widths: *what is everything doing* and *what is that one doing*. The
        words come from Briefing and from nowhere else (#166), so the Voice, the
        Companion Channel and `bridgectl` are told the same thing.

        With no address it is a read of state the hub already holds — no lane is
        touched, so it answers as fast as `status` and cannot be made to hang by
        a lane that is down. With an address it is exactly one `inspect`,
        legacy's "exactly one fetch, read at the moment you speak"
        (`legacy@1d32845:skill/announcing.md` step 1, `bridge/host.py:399-405`),
        **ported**.

        **It never sets the Focus Session** (#165 Q2): asking about a Session is
        not replying to one, and a read that moved the focus would let the Voice
        change what it speaks first merely by looking.

        Its refusals are `history`'s, minus one. An unknown identity, a stale
        one, a Child Process and a lane that could not be read all refuse here
        exactly as they do there. What does **not** refuse is a Session whose
        *progress* could not be read: `history` exists to answer with a
        Session's own words and has nothing to say without them, while a brief
        still has a state, a wait and a name — so an unreadable reading becomes
        the UNREADABLE state or an unreadable `newest`, which is the honest
        answer and the one the five states were drawn to carry.
        """
        if target is None:
            return briefing.roster(self._state.sessions.all(), self._state.sessions.focus)
        row = await self._inspect_now(target)
        read = self._state.sessions.observed_one(row, now=self._stamp())
        return briefing.session(
            _as_read_now(read, row.progress),
            question_answerable=self._question_answerable(read.target),
        )

    async def _inspect_now(self, target: SessionTarget) -> SessionInspection:
        """One exact Session, read now through its own lane and no other.

        `brief`'s reading: resolve one identity, ask the one lane that owns it,
        and refuse rather than answer from somewhere else. The row is returned
        unfolded, because the caller folds it on its own terms. `history` does
        not come through here — a page is a separate read that observes nothing
        (ADR 0016).
        """
        session = self._state.sessions.resolve(target)
        adapter = self._agents.get(target.agent)
        if adapter is None:
            raise LaneUnreadable(str(target.agent), "this engine has no adapter for that agent")
        try:
            row = await adapter.inspect(session.target)
        except LaneUnavailable as unread:
            raise LaneUnreadable(str(unread.agent), unread.reason) from None
        if row.lifecycle is not SessionLifecycle.LIVE:
            raise StaleSessionError(target, reason=f"that Session is {row.lifecycle}")
        return row

    async def flip_switch(self, name: str, on: bool) -> bool:
        """Flip a switch and report the state it held before.

        The flip itself is never gated. What follows it is: turning an outlet on
        is an outlet transition, and a transition is the *only* thing that asks
        the next discovery pass to reconcile still-actionable waits.

        Which flips are transitions is read off the outlets themselves rather
        than from the flip's direction, because not every switch is an outlet.
        The Auto Hang-up Switch is the plain case: it opens no way to reach the
        user, so turning it on owes nobody a re-announcement of what they are
        already waiting on.
        """
        opened_before = set(self.adjudicator.outlets())
        previous = self._state.switches.flip(name, on)
        self._state.persist()
        if set(self.adjudicator.outlets()) - opened_before:
            self._owe_reconciliation()
        return previous

    async def live_toggle(self) -> CallSnapshot:
        """The one action: end the call the system owns, or start one if none is up.

        **Never gated, by any switch.** The Live Toggle is a control-plane
        action, and ADR 0002 is absolute. The switches read the other way round:
        Duty, Voice and Message constrain what *the system* may do on its own —
        speak, push, touch the call unbidden — and this is the user touching the
        call with the system as the instrument, exactly like flipping a switch.
        Gating it would produce the indefensible case: Voice is flipped off while
        a call is up, and the user's explicit "end this call" is refused by the
        very switch that says the system should be quiet.

        Only the interlock constrains it, in both directions: one call at a time.
        There is one path here and every surface calls it — a surface holding its
        own call state is how two toggles once opened two calls.
        """
        if self.interlock.owns_call():
            return await self.interlock.end_call()
        # Ending is always allowed; whether opening is, is the interlock's to
        # say — in both directions, and for both of its reasons.
        snapshot = await self.interlock.open_call(self._dial((DialReason(text=USER_OPENED),)))
        if snapshot.is_up:
            self._owe_reconciliation()
        return snapshot

    def _dial(self, hand_over: tuple[HandoverItem, ...]) -> Dial:
        """What a call this hub opens is opened on: two audiences and a hand-over.

        The one place a `Dial` is built, and therefore the one place that can say
        which half is missing when this engine generated nothing. The refusal is
        here rather than in the interlock because that is a door and this is a
        source: the interlock decides *whether* a call may open, and only the hub
        knows what it would be opened on (ADR 0018; #193's deferred note on the
        error's old name).
        """
        instructions = self._instructions
        if instructions is None:
            raise CallInstructionsMissing("prose for the Voice or rules for the Call Agent")
        if not instructions.voice.text.strip():
            raise CallInstructionsMissing("prose for the Voice")
        if not instructions.agent.text.strip():
            raise CallInstructionsMissing("rules for the Call Agent")
        return Dial(
            voice=instructions.voice.text, agent=instructions.agent.text, hand_over=hand_over
        )

    def _system_dial(self, notice: Notice) -> Dial:
        """What a call the *system* dials for one notice is opened on.

        The hand-over is read from the roster **now**, not assembled from the
        notice that provoked it: ADR 0017's rule is that a missed call is briefed
        from a fresh reading and never from replayed events, and by the time the
        matrix reaches this route the wait that started it may have been answered
        at the terminal. So the notice names the moment and the roster supplies
        the content.

        **Every row, including the one that just stopped.** A Stop used to leave a
        row that merely ended a turn in `RUNNING` (#209), and a hand-over briefs
        no running Session — so a call dialled by that Stop came up saying a
        Session needs the user without mentioning which, and this method passed
        the notice's own brief alongside for `handover` to place last. Since #213
        the roster holds a stopped Session as stopped, so the fresh reading
        covers it like any other and the compensation is gone. Which leaves
        `notice` naming the moment and nothing else: it stays in the signature
        because it is the escalation's own callback contract for this route
        (`core/escalation.py`), and reading nothing off it is exactly ADR 0017.
        """
        sessions = self._state.sessions.live()
        return self._dial(
            briefing.handover(
                sessions,
                self._state.sessions.focus,
                reason=SYSTEM_DIALLED,
                answerable=tuple(
                    session.target
                    for session in sessions
                    if session.waiting_for.kind is WaitingKind.QUESTION
                    and self._question_answerable(session.target)
                ),
            )
        )

    async def outlets_changed(self) -> None:
        """An outlet the switches already allow became reachable again.

        The named entry point for the one transition no event describes: a
        Companion Channel whose far side has come back, noticed by a liveness
        check rather than announced. Call transitions and switch flips already
        reconcile on their own events.

        This is deliberately the *only* other reconciliation trigger. The next
        ordinary discovery pass does the read; a failed notice attempt never
        triggers another attempt.
        """
        self._owe_reconciliation()

    def _owe_reconciliation(self) -> None:
        """Ask the next discovery pass to act, when an outlet is effective now."""
        if self.adjudicator.outlets():
            self._reconcile_owed = True

    async def _announce_current_stops(self, fresh: set[AgentKind]) -> None:
        """Announce actionable main rows from lanes this pass actually read."""
        for session in self._state.sessions.live():
            if session.target.agent not in fresh or not session.child.is_main:
                continue
            if session.waiting_for.needs_the_user:
                await self._announce_waiting(session, session.target, session.waiting_for)

    async def relay(
        self, target: SessionTarget, text: str, *, route: RelayRoute = RelayRoute.DELIVER
    ) -> RelayOutcome:
        """An Answer Relay: the user's own words, for one exact Session.

        A hub verb rather than a pipeline a surface reaches into. Outsiders see
        one Bridge Core (ADR 0001), and a surface that knew which pipeline owned
        which decision would be a surface that has to be changed when the hub
        rearranges itself.
        """
        outcome = await self.relays.relay(target, text, route=route)
        # The user has just spoken to this Session, so it becomes the Focus
        # Session (#165 Q2) — whatever the receipt says. Focus follows the
        # user's attention, and a Relay that failed to land is precisely the
        # Session they are still waiting on.
        self._state.sessions.set_focus(outcome.target)
        return outcome

    async def answer_approval(
        self, approval_id: str, verdict: ApprovalVerdict
    ) -> RelayOutcome | None:
        """Carry the user's verdict. None when no live row carries that handle.

        **The Approval Relay carries and nothing more** (#191). There is no
        pending-approval ledger to consult: the roster's current reading is the
        one truth about which dialogs are open, and the row that carries this
        handle in its wait is the Session the verdict belongs to. A handle no
        row carries is a hook that has ended — the dialog is the keyboard's
        again — and the receipt for that is the refusal this None becomes.

        **A spawned target is refused in its own words.** A Codex subagent
        thread can raise a real permission prompt; answering it would carry the
        user's authority into a Session `resolve` refuses to address a moment
        later, so it stays the keyboard's — "never spoken to" includes never
        answered (advisor, 2026-08-27). It reads differently from the refusal
        above because the user acts on it differently.

        Answering a permission is replying to that Session, so it takes the
        focus exactly as an Answer Relay does — whatever the receipt says.

        The receipt is `RelayOutcome`, the same shape `relay` answers with
        (#192): the state, the attempt's grade, and one reason code. `DELIVERED`
        exactly when the adapter proved it; every other grade is terminal here,
        because a verdict is never retried on this system's own authority and
        the dialog on screen is still the thing that can resolve it.
        """
        found = self._dialog_on_the_roster(approval_id)
        if found is None:
            _log.info("no live Session carries the dialog %s; the verdict is refused", approval_id)
            return None
        session, request = found
        if not session.child.is_main:
            raise ChildSessionError(session.target, session.child.parent)

        adapter = self._agents.get(session.target.agent)
        if adapter is None:
            _log.info("no %s lane is loaded to carry the verdict", session.target.agent)
            return None

        request_id = new_request_id()
        receipt = await adapter.approval_relay(request, verdict, request_id=request_id)
        self._state.sessions.set_focus(session.target)
        return RelayOutcome(
            request_id=request_id,
            target=session.target,
            state=Lifecycle.DELIVERED if receipt.is_delivered else Lifecycle.REPORTED_FAILED,
            route=RelayRoute.DELIVER,
            reason=reason_for(receipt),
            receipt=receipt,
        )

    def _dialog_on_the_roster(self, approval_id: str) -> tuple[Session, ApprovalRequest] | None:
        """The live row whose current wait carries that handle, and the request for it.

        Read off `WaitingFor.as_approval_request`, which is the one place a wait
        becomes the request the Approval Relay addresses, so the roster row the
        user was briefed from and the request the adapter is handed are the same
        fact. Child rows are found here and refused by the caller: they are on
        the roster, and a handle nobody could match would be the wrong refusal.
        """
        for session in self._state.sessions.live():
            request = session.waiting_for.as_approval_request(session.target)
            if request is not None and request.approval_id == approval_id:
                return session, request
        return None

    async def verify(self) -> tuple[SeamVerification, ...]:
        """What configuration named, against what this engine actually loaded.

        ADR 0003: the comparison is the hub's because only the hub knows the
        configured side, and **every** pluggable seam is asked for itself. The
        ADR generalises past the Companion Channel deliberately — `verify` is a
        seam verb on all four for exactly this reason — so a Call adapter whose
        far side is down reports that, rather than the engine reciting the
        configuration back and calling it an observation.
        """
        reports: list[SeamVerification] = []
        for load in self._inventory:
            adapter = self._behind(load.seam)
            reports.append(compare(load, await adapter.verify() if adapter is not None else None))
        return tuple(reports)

    def _behind(self, seam: str) -> Verifiable | None:
        """The adapter this engine actually holds for one seam name, if any."""
        if seam == CALL_SEAM:
            return self._call
        if seam == CHANNEL_SEAM:
            return self._channel
        if seam.startswith(AGENT_SEAM_PREFIX):
            try:
                return self._agents.get(AgentKind(seam[len(AGENT_SEAM_PREFIX) :]))
            except ValueError:
                return None
        return None

    # ------------------------------------------------------------------
    # The dispatch loop.
    # ------------------------------------------------------------------

    async def drain(self) -> int:
        """Dispatch everything waiting, in arrival order. Returns how many."""
        waiting = self._events.drain()
        for event in waiting:
            await self.dispatch(event)
        return len(waiting)

    async def tick(self) -> tuple[RelayOutcome, ...]:
        """Advance the time-driven ceilings. The composition root calls this on a timer.

        Deliberately the only time-driven thing in the hub, and there are two of
        them left: the undelivered Relay ceiling and the Live Call's silence
        ceiling. Stop Notices are not replayed here; current state is reconciled
        only on outlet transitions.

        **No clock runs on a held dialog** (ADR 0015, amended by #191). A parked
        permission or question is bounded by the wire that holds it — Claude Code
        ends its hook at the installed block's timeout, the listener releases the
        entry when that socket closes, and a Codex dialog stays answerable from
        the TUI — so an engine-side sweep here was a second clock racing the
        first. What a release does is unchanged, and nothing is pushed on it: the
        next brief reads a row with no handle and says `answer: at the terminal`
        (ADR 0017, a fresh reading).
        """
        expired = self.relays.sweep_expired()
        for outcome in expired:
            await self._announce(outcome.target, terminal_line(outcome))
        # Asked before the Call Keeper is, and not inside it: with the Auto
        # Hang-up Switch off the ceiling is never measured, so the Keeper's one
        # ending attempt per call stays unspent and turning the switch back on
        # ends a call that has been silent all along.
        #
        # **And never measured while unread call activity is waiting.** This runs
        # on its own task and the dispatch loop runs on another
        # (`engine/composition.py`), so a `VoiceSpeech` that has been emitted but
        # not yet taken has not reached the interlock — and measuring silence
        # then is how a call gets ended in the middle of the answer that was
        # about to say it was not silent (#184). Waiting for the next pass costs
        # a second on a sixty-second ceiling.
        #
        # The two kinds are named rather than the queue being asked whether it
        # holds anything: this is the ceiling's own question — *was there
        # activity on the call* — and a `SessionStopped` waiting to be read is
        # not an answer to it. Asking the wider question let news about a
        # Session hold a silent call open.
        if self.adjudicator.may_auto_hangup() and not self.events.unread(UserSpeech, VoiceSpeech):
            try:
                ended_silent_call = await self.interlock.end_silent_call(
                    self._policy.silence_end_seconds
                )
            except Exception:  # noqa: BLE001 - the Call Keeper spends one attempt per call
                _log.exception(
                    "could not end the silent Live Call; not trying again until the call changes"
                )
            else:
                if ended_silent_call:
                    _log.info(
                        "ended the Live Call after %g seconds without call activity",
                        self._policy.silence_end_seconds,
                    )
        return expired

    async def discover(self) -> tuple[SessionTarget, ...]:
        """Ask every lane what Sessions exist, and make the roster agree.

        **This is how a Session gets onto the roster at all.** v1.0 bridges the
        Sessions the *user* starts (#68), so nothing announces one — the hub
        goes and looks, on the cadence the composition root sets, and each lane
        answers for itself.

        **One lane raising does not stop the others.** A lane is supposed to
        report its own trouble as `LaneDiscovery(error=...)`; one that raises
        instead is a defect in that adapter, and the answer to a defective lane
        is to leave its rows alone and keep asking the other one — which is
        exactly what the seam's own contract already says an error means.

        Returns the Sessions that ended on this pass, having already answered
        whatever was queued for them: a Session that disappears between two
        ticks owes the user the same news as one that reported its own death,
        and the roster is the only witness to the first kind.

        **Which rows ended is the registry's answer, not a diff taken here.** A
        Codex row is re-keyed when its Session takes its first turn and gains a
        thread id, and again when the user types `/new` (#73) — the same row,
        under a new `SessionTarget`. Comparing the roster before and after would
        read both as a departure and terminate the Relays queued for a Session
        that is sitting there waiting for them, so the question is asked of the
        one component that can tell a re-keying from a death.
        """
        reconcile = self._reconcile_owed
        self._reconcile_owed = False
        gone: list[SessionTarget] = []
        fresh: set[AgentKind] = set()
        for kind, adapter in self._agents.items():
            try:
                lane = await adapter.discover()
            except Exception:  # noqa: BLE001 - a defective lane must not stop the rest
                _log.exception("the %s lane raised instead of reporting its trouble", kind)
                continue
            gone.extend(self._state.sessions.observe(kind, lane, now=self._stamp()))
            if lane.enumerated:
                fresh.add(kind)

        for target in gone:
            _log.info("Session %s is no longer running", target)
            for outcome in self.relays.session_ended(target):
                await self._announce(outcome.target, terminal_line(outcome))
        if reconcile and self.adjudicator.outlets():
            await self._announce_current_stops(fresh)
        return tuple(gone)

    async def dispatch(self, event: Event) -> None:
        """Turn one event into a call on whichever pipeline owns the decision."""
        match event:
            case SessionStopped():
                await self._session_stopped(event)
            case SessionEnded():
                await self._session_ended(event)
            case ReplyWindowChanged():
                await self._reply_window_changed(event)
            case RelayReceipt():
                self._relay_receipt(event)
            case CallStarted():
                self.interlock.note_started(event.call_id)
                await self._cue(Cue.CONNECTED)
                self._owe_reconciliation()
            case CallEnded() | CallDropped():
                # The cue is not conditional on the interlock's answer the way
                # the reconciliation below is: what the user is owed is the
                # sound of the call they were on ending, and whether this hub
                # was still holding that call is a bookkeeping question they
                # cannot hear (#186).
                await self._cue(Cue.ENDED)
                # Only a release is an outlet transition. A late event about a
                # call the system was not holding changes nothing, so it cannot
                # justify another inspection and announcement.
                if self.interlock.note_ended(event.call_id):
                    self._owe_reconciliation()
            case InboundText():
                await self._inbound_text(event)
            case VoiceSpeech():
                # Both edges are activity, and the ceiling is held between them.
                # Recorded, never read back: this system does not listen to
                # itself, and the words are the Voice's own (#184).
                self.interlock.note_voice_speech(speaking=event.speaking)
                _log.info("%s", VOICE_SPEAKING_LINE if event.speaking else VOICE_QUIET_LINE)
            case UserSpeech():
                self.interlock.note_activity()
                # Recorded, never parsed. Spoken intent reaches Bridge Core as
                # structured control-plane calls the voice thread makes (#5),
                # over the transport the Call adapter raises (#6) — the router's
                # marker grammar would collapse every utterance to bare text and
                # relay the user talking *to* the system into a coding Session.
                # Written down rather than dropped: no-loss applies to events too.
                _log.info("user speech, for the voice thread to act on: %r", event.text)
            case _:
                _log.info("no pipeline consumes %s here", type(event).__name__)

    async def _session_stopped(self, event: SessionStopped) -> None:
        """A Session stopped. Say so in the log, then announce it.

        **The log line is the run's only way to attribute a notice to this
        engine.** Until #75 the escalation path wrote nothing when it *worked* —
        only its old retention and failure paths wrote — so a Stop Notice that
        reached the user left no trace at all, and the acceptance's
        `stop notice` step could satisfy its attribution check only on the
        failure path. An engine silent about the one event it exists to produce
        is the gap #48 named on the inbound side, on the outbound side.
        """
        if self._spawned(event.target):
            return
        session = self._known(event.target)
        if session is not None:
            session = self._state.sessions.set_stop_reading(
                event.target, waiting_for=event.waiting_for, progress=event.progress
            )
        await self._announce_waiting(
            session,
            event.target,
            event.waiting_for,
            progress=event.progress,
        )

    async def _announce_waiting(
        self,
        session: Session | None,
        target: SessionTarget,
        waiting_for: WaitingFor,
        *,
        progress: ProgressObservation | None = None,
    ) -> NoticeOutcome | None:
        """Announce one current wait through the same producer as its live event.

        **Bridge Core keeps no memory of what it has already announced (#161).**
        Whether to announce is a function of this reading alone, so a wait that
        still needs the user is reported on every outlet transition, in the same
        words. A set of delivered waits used to suppress the repeat; it could not
        work, because the action that invalidates it — an adapter handing an
        unanswered dialog back to the terminal and dropping its handle — happens
        where Bridge Core cannot see it, so any key Bridge Core computes goes
        stale unseen.

        **One wait, one notice, and the handle is not read here at all.** A
        permission used to reach this method twice — once as the Stop, once as
        the announcement its own pipeline made — and the tiebreak between them
        lived here. Since #191 a dialog travels on the Stop alone, so what the
        handle is for is `Briefing`'s question of whether the user can answer
        from here, read off the row it is given.

        Legacy (ADR 0010) — **dropped, because** its record of what a Session was
        last announced on is `CurrentSessionStop`
        (`legacy@1d32845:bridge/store.py:870-897`), with its identity derived
        from live state (`legacy@1d32845:bridge/daemon.py:1420-1493`). It is one
        of the durable ledgers #67's port table leaves behind, and the rule that
        replaces it is #80's — reconcile the current state and replay nothing.
        """
        brief = stop_brief(
            session,
            target,
            waiting_for,
            progress=progress,
            question_answerable=(
                waiting_for.kind is WaitingKind.QUESTION and self._question_answerable(target)
            ),
        )
        # The log carries the brief's text too, so the one wording is what the
        # run's own record shows (#166 B5/B6). `Session stopped:` opens it
        # unchanged: `tests/acceptance/journey.py::ENGINE_STOP_LINE` greps the
        # first line of the record, and `drain_boot_notice` reads that grep.
        _log.info("Session stopped: %s", briefing.text(brief))
        outcome = await self.escalation.escalate(
            Notice(
                request_id=new_request_id(),
                target=target,
                text=briefing.text(brief),
                # The same brief, twice, for the two kinds of surface: the
                # Companion Channel and the log render words, and the Live Call
                # is handed the brief itself and speaks from it (`CONTEXT.md`,
                # *Stop Notice*). One reading, so the two can never disagree.
                spoken=briefing.spoken(brief),
            )
        )
        return outcome

    async def _session_ended(self, event: SessionEnded) -> None:
        try:
            self._state.sessions.mark_ended(event.target)
        except BridgeCoreError:
            _log.info("a Session ended that was never registered: %s", event.target)
        self._state.persist()
        for outcome in self.relays.session_ended(event.target):
            await self._announce(outcome.target, terminal_line(outcome))

    async def _reply_window_changed(self, event: ReplyWindowChanged) -> None:
        """An adapter saw the window move between two discoveries. Land it on the state.

        The window is derived, so there is nothing here to set directly: this
        event is a coarser reading of the same fact and it lands on the field
        the fine-grained one lands on. The next discovery overwrites both, which
        is what makes this a shortcut rather than a second source of truth.

        A held question is the exception to the state shortcut. Its listener can
        open the route before the roster has reported `WAITING`; the event proves
        the route, not `IDLE`, so only a discovery pass or a Stop — the two
        readings that looked at the Session itself — may change the state there.
        """
        try:
            held = self._state.sessions.resolve(event.target)
            question_route_open = event.window is ReplyWindow.OPEN and self._question_answerable(
                event.target
            )
            if not question_route_open:
                self._state.sessions.set_state(
                    event.target, _state_behind(event.window, held.state)
                )
        except BridgeCoreError:
            _log.info("a Reply Window changed on an unknown Session: %s", event.target)
            return
        if event.window is ReplyWindow.OPEN:
            await self.relays.reply_window_opened(event.target)

    async def _cue(self, cue: Cue) -> None:
        """Ask the Call adapter to mark one moment with a sound.

        The hub names the moment and never the sound: which notes, how loud and
        how long were chosen by ear against one machine's speakers (#174), and
        none of that is policy. The Call Keeper takes these two calls over when
        it arrives (#195); until then the lifecycle arms are where the moments
        are known.

        A shipped adapter swallows its own playback failures, so this guard is
        for a **defective** one — and a defective adapter may not stop the arm
        it was called from. What follows a `CallEnded` is the interlock being
        released, and a missing tone is not a reason to keep a call held.
        """
        try:
            await self._call.play_cue(cue)
        except Exception:  # noqa: BLE001 - a sound may not take down the call it marks
            _log.exception("the Call adapter raised on the %s cue", cue)

    def _relay_receipt(self, event: RelayReceipt) -> None:
        """A receipt that arrived after the call returned. The ledger records it."""
        try:
            self._state.relays.classify(event.receipt.request_id, event.receipt)
        except UnknownRelayError:
            _log.info("a receipt arrived for a Relay that is no longer pending")

    async def _inbound_text(self, event: InboundText) -> None:
        """Classify one inbound line, act on it, and always answer the user."""
        found = self.router.classify(event.text)
        match found.kind:
            case InboundClass.CONTROL:
                await self._reply(
                    await self._control(found) if self._control else NO_CONTROL_SURFACE
                )
            case InboundClass.DELEGATION:
                await self._reply(
                    await self._delegate(found) if self._delegate else NO_DELEGATE_HANDLER
                )
            case InboundClass.ANSWER_RELAY:
                await self._relay_inbound(found)
            case InboundClass.UNKNOWN:
                await self._reply(found.reply)
        if found.kind is InboundClass.ANSWER_RELAY:
            assert found.target is not None  # the router sets one for every ANSWER_RELAY
            _log.info(
                "handled inbound Companion Channel message kind=%s target=%s",
                found.kind,
                found.target,
            )
        else:
            _log.info("handled inbound Companion Channel message kind=%s", found.kind)

    async def _relay_inbound(self, found: Classification) -> None:
        """Carry a typed relay in, and answer it with the receipt the CLI prints.

        **Every inbound relay is answered**, and with the same three codes, not
        only the ones that had to wait. The channel used to hear a sentence when
        the words queued and silence when they went, which made "it worked" and
        "nothing was read" the same observation. A receipt is a grade and a
        reason; how it is said aloud is the Voice's business (#175).
        """
        assert found.target is not None  # the router sets one for every ANSWER_RELAY
        try:
            outcome = await self.relays.relay(found.target, found.text)
        except BridgeCoreError as refusal:
            await self._reply(str(refusal))
            return
        await self._reply(outcome.line)

    async def _announce(self, target: SessionTarget, text: str) -> None:
        """Tell the user something the pipelines decided they need to know."""
        if not text:
            return
        await self.escalation.escalate(
            Notice(request_id=new_request_id(), target=target, text=text)
        )

    async def _reply(self, text: str) -> None:
        """Answer text the user sent. **Never gated** — a reply is not a push.

        ADR 0002 is absolute, and the Companion Channel is one of the surfaces it
        names. Gating this would gate the one way to flip Duty back on from away
        from the computer, using the switch that is off.

        **One attempt, graded, and never sent again** (P15, #61 C2). The
        reference implementation settled every outbound attempt to `sent`,
        `failed`, `indeterminate` or `suppressed` and refused to resend an
        indeterminate one, because a duplicate notification costs the user more
        than a missing one (`legacy@1d32845:bridge/channel.py:11-13,75-86`;
        `legacy@1d32845:bridge/daemon.py:830-879`). That rule is **ported**. Its
        storage is **simplified**: legacy wrote the grade to a durable ledger
        (`legacy@1d32845:bridge/store.py:1517-1614`) and this is a direct answer
        to text the user just sent, so the grade is said here and forgotten. It
        does not enter the Answer Relay queue either — it is a direct reply, and
        queueing it would replay an answer to a question the user asked minutes
        ago when its Reply Window next opened.

        **The words never enter the diagnostic.** The reply carries whatever the
        user's own business is; the log carries the grade and the adapter's
        reason, and the Telegram adapter already guarantees its token appears in
        no error message (`telegram/api.py`).
        """
        if not text:
            return
        receipt = await self._channel.send(text, request_id=new_request_id())
        if receipt.is_delivered:
            return
        _log.warning(
            "the reply to the user was not delivered (%s: %s); it is not sent again",
            receipt.outcome,
            receipt.reason,
        )

    def _known(self, target: SessionTarget) -> Session | None:
        try:
            return self._state.sessions.resolve(target)
        except BridgeCoreError:
            return None

    def _spawned(self, target: SessionTarget) -> bool:
        """Whether the roster **positively says** this is a Child Process (#79).

        A Child Process is seen, never spoken to — and never spoken *about*: a
        Stop Notice names a Session the user is invited to answer, and the
        answer to a child is refused. Suppressing the announcement is therefore
        the same rule as refusing the Relay, said one step earlier so the user
        is never asked for something the system will not carry.

        **Asked of `resolve`, so there is one definition.** The registry is the
        only thing that decides what a child is (`core/sessions.py`), and it
        already refuses one by raising. Re-deriving the test here would be a
        second answer to a question that has one.

        **Unknown is not child, and the asymmetry is deliberate.** Discovery
        runs on a cadence, so a Session can stop before the roster holds a row
        for it; reading that silence as "child" would drop the one notice the
        engine exists to send. A child wrongly announced costs one message about
        something that is refused anyway — the cheaper mistake by far.

        It is here rather than in a lane because a lane raising the event is not
        wrong: a Codex subagent thread really does leave `active`, and its
        adapter really does watch every thread the daemon holds. What the hub
        does with that is the hub's.
        """
        try:
            self._state.sessions.resolve(target)
        except ChildSessionError as spawned:
            # Said out loud, because a notice that was never sent is otherwise
            # indistinguishable in the log from one that failed to reach anybody.
            _log.info("%s is a Child Process, so nothing is announced about it", spawned.target)
            return True
        except BridgeCoreError:
            return False
        return False

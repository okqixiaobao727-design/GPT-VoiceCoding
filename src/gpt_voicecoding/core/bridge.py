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
to the log and nothing else. The control-plane command set and the Delegated
Turn's execution belong to the surfaces that own them, so both arrive as
injected handlers with honest defaults rather than being invented here.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Final

from gpt_voicecoding.core.adjudication import SwitchAdjudicator
from gpt_voicecoding.core.approvals import ApprovalOutcome, ApprovalPipeline, PendingApproval
from gpt_voicecoding.core.clock import Clock, default_clock, wall_clock
from gpt_voicecoding.core.errors import (
    BridgeCoreError,
    LaneUnreadable,
    ProgressUnavailable,
    StaleSessionError,
    UnknownRelayError,
)
from gpt_voicecoding.core.escalation import EscalationPipeline, Notice
from gpt_voicecoding.core.events import EventQueue
from gpt_voicecoding.core.instructions import InstructionContext, Instructions, generate
from gpt_voicecoding.core.interlock import CallInterlock
from gpt_voicecoding.core.policy import CorePolicy
from gpt_voicecoding.core.relay_queue import PendingRelay
from gpt_voicecoding.core.relays import RelayOutcome, RelayPipeline
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
    ApprovalVerdict,
    AwaitingApproval,
    LaneUnavailable,
    RelayReceipt,
    RelayRoute,
    ReplyWindow,
    ReplyWindowChanged,
    SessionEnded,
    SessionLifecycle,
    SessionState,
    SessionStopped,
    WaitingFor,
    WaitingKind,
)
from gpt_voicecoding.seams.call import (
    CallAdapter,
    CallDropped,
    CallEnded,
    CallSnapshot,
    CallStarted,
    UserSpeech,
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


def name_for(session: Session | None, target: SessionTarget) -> str:
    """What to call one Session out loud, best name first.

    The Session Label if the user has one for it, else the agent's own Session
    Name, else the address — because a notice that names nothing is a notice the
    user cannot answer. #78 stabilises the middle one.
    """
    if session is not None and session.label is not None:
        return str(session.label)
    if session is not None and session.name:
        return session.name
    return f"{target.agent} {target.session_id or f'pid {target.pid}'}"


def stop_notice_for(
    session: Session | None, target: SessionTarget, waiting_for: WaitingFor | None = None
) -> str:
    """The words a stopped Session is announced with.

    Names the Session the way the user names it, because that is what they will
    say back when they answer, and carries **what it stopped on** — the question
    with its options and any recommendation, or the tool awaiting permission.
    Rendering happens here rather than in the adapter because the words are
    Bridge Core's policy; the adapter's job was to keep the structure.
    """
    stopped_on = _stopped_on(waiting_for) if waiting_for is not None else ""
    tail = f" — {stopped_on}" if stopped_on else ""
    return f"{name_for(session, target)} stopped and may need you{tail}"


def _state_behind(window: ReplyWindow, held: SessionState) -> SessionState:
    """The Session state a Reply Window report implies, given what we already hold.

    An open window is a Session that will take the next turn, which is `IDLE`.
    A closed one has two causes and the report cannot tell them apart — mid-turn,
    or holding a dialog — so a Session already known to be `WAITING` keeps that,
    and anything else becomes `RUNNING`. Guessing the other way would erase a
    permission dialog from the roster while it is still on the user's screen.
    """
    if window is ReplyWindow.OPEN:
        return SessionState.IDLE
    return held if held is SessionState.WAITING else SessionState.RUNNING


#: What a notice says about something this engine can announce and cannot answer.
#: One sentence, used by both such cases, because they are one fact: a notice the
#: user tries to answer remotely and cannot is worse than no notice at all.
ANSWER_IT_AT_THE_TERMINAL: Final = "answer it in the terminal; it cannot be answered from here"


def _stopped_on(waiting_for: WaitingFor) -> str:
    """One line describing what a Session is waiting for, or nothing to add."""
    match waiting_for.kind:
        case WaitingKind.QUESTION:
            parts = [waiting_for.prompt or "it asked you something"]
            if waiting_for.options:
                parts.append("options: " + ", ".join(option.text for option in waiting_for.options))
            if waiting_for.recommendation:
                parts.append(f"it recommends {waiting_for.recommendation}")
            # A question has no answering route at all on this build: the inbox
            # carries words and never authority (#71), so a Relay cannot answer
            # one, and the hook that holds the dialog open is deliberately not
            # announced as an approval — a spoken "deny" there would be consumed
            # by the Session as the user's answer (#77). #103 gives the question
            # its own route, and takes this clause off when it does.
            return "; ".join(parts) + f" — {ANSWER_IT_AT_THE_TERMINAL}"
        case WaitingKind.PERMISSION:
            named = waiting_for.tool_name or "a tool"
            asked = f"{named} needs your permission" + (
                f": {waiting_for.detail}" if waiting_for.detail else ""
            )
            if waiting_for.approval_id:
                return asked
            # No handle means no Approval Relay: the roster saw `waiting` and
            # nothing is parked on the approval socket to answer into. Saying so
            # is the whole difference between a notice the user can act on and
            # one they try to answer from their phone and cannot.
            return f"{asked} — {ANSWER_IT_AT_THE_TERMINAL}"
        case WaitingKind.UNKNOWN:
            # The honest answer while the record has not flushed (#73): say that
            # rather than invent a reason the Session never gave.
            return "it has not said what it is waiting for yet"
        case _:
            return waiting_for.detail or ""


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
    pending_approvals: tuple[PendingApproval, ...]


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
        #: The voice thread's house rules, as the one string a call starts with.
        #: Empty when this hub generated none, and the interlock refuses to open
        #: a call on an empty one rather than this being checked at each caller.
        self._voice_instructions = self._instructions.voice.text if self._instructions else ""
        #: Durations are measured with `clock`; anything read outside this
        #: process is stamped with `stamp`. A Session's `first_seen` travels to
        #: every surface in the `sessions` payload, and a monotonic reading
        #: would name no moment on the far side.
        self._stamp = stamp

        self.interlock = CallInterlock(call)
        self.adjudicator = SwitchAdjudicator(state.switches)
        self.escalation = EscalationPipeline(
            call=call,
            channel=channel,
            interlock=self.interlock,
            adjudicator=self.adjudicator,
            relays=state.relays,
            voice_instructions=self._voice_instructions,
            clock=clock,
        )
        self.relays = RelayPipeline(
            agents=agents,
            sessions=state.sessions,
            relays=state.relays,
            policy=self._policy,
            clock=clock,
        )
        self.approvals = ApprovalPipeline(
            agents=agents,
            escalation=self.escalation,
            policy=self._policy,
            clock=clock,
        )
        self.router = InboundRouter(sessions=state.sessions, grammar=grammar)

    @property
    def instructions(self) -> Instructions | None:
        """The voice and delegated-turn instruction sets, as plain data.

        Generated once, from the catalogue and this engine's own installation.
        The Call adapter starts its realtime thread with the voice set and the
        Codex adapter starts a Delegated Turn with the delegated one; neither
        rewrites them, and neither reads anything from disk to get them.
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
            pending_approvals=self.approvals.pending(),
        )

    async def progress(self, target: SessionTarget) -> Session:
        """How far along one exact Session is, read now. Never starts a turn.

        A hub verb, and a *read*: it resolves one identity, asks that lane and
        no other, and returns the same `Session` row `status` renders. The
        reference implementation's own rule, ported —
        `legacy@1d32845:bridge/daemon.py:2202-2271` resolved one exact registered
        identity, asked only that agent's own authority, and never fell back to
        another lane, a terminal or a screen when its source could not answer.

        **It is not a Relay and it costs no turn.** The router says so of the
        whole class (`core/router.py:31-32`) and the seam keeps it true:
        `inspect` reads what the agent has already written down.

        **Three more refusals, and each is a different fact** — #76 asks for an
        honest error rather than an answer that says nothing:

        - *The lane could not be read.* `LaneUnreadable`, carrying the lane's own
          words. The roster's row is left exactly as it stood: not being able to
          look is not a sighting.
        - *The Session has ended.* `StaleSessionError`. The row is **not** ended
          here: `SessionRegistry.observe` is the one component that ends rows,
          and the value an `inspect` returns for a Session it could not find
          carries no workspace and no name — folding it in would strip the very
          fields a surface needs to say what happened to it. The next discovery
          ends it properly, within one cadence.
        - *Nothing could read how far it has got.* `ProgressUnavailable` — an
          unattached Codex Session, or one whose first turn has written no record
          yet. A Session that *was* read and had said nothing is not this: it
          answers normally, with an empty reading.

        Two further refusals are the resolver's and are not restated here — an
        identity nobody registered, and a Child Process, which is seen and never
        spoken to (#68).

        Whatever is read is folded back into the roster before it is answered, so
        a surface that asks for progress and then asks for `status` cannot be
        told two different things about one Session.
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
        read = self._state.sessions.observed_one(row, now=self._stamp())
        if read.progress is None:
            raise ProgressUnavailable(target)
        return read

    async def flip_switch(self, name: str, on: bool) -> bool:
        """Flip a switch and report the state it held before.

        The flip itself is never gated. What follows it is: turning an outlet on
        is an outlet transition, and a transition is the *only* thing that
        re-offers a retained notice.
        """
        previous = self._state.switches.flip(name, on)
        self._state.persist()
        if on and not previous:
            await self.escalation.sweep()
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
        snapshot = await self.interlock.open_call(self._voice_instructions)
        if snapshot.is_up:
            await self.escalation.sweep()
        return snapshot

    async def outlets_changed(self) -> None:
        """An outlet the switches already allow became reachable again.

        The named entry point for the one transition no event describes: a
        Companion Channel whose far side has come back, noticed by a liveness
        check rather than announced. Call transitions and switch flips already
        sweep on their own events.

        This is deliberately the *only* other way a retained notice is
        re-offered. Nothing polls, and nothing retries off the back of its own
        failure — that is what keeps uncapped retention from becoming a
        livelock.
        """
        await self.escalation.sweep()

    async def relay(
        self, target: SessionTarget, text: str, *, route: RelayRoute = RelayRoute.DELIVER
    ) -> RelayOutcome:
        """An Answer Relay: the user's own words, for one exact Session.

        A hub verb rather than a pipeline a surface reaches into. Outsiders see
        one Bridge Core (ADR 0001), and a surface that knew which pipeline owned
        which decision would be a surface that has to be changed when the hub
        rearranges itself.
        """
        return await self.relays.relay(target, text, route=route)

    async def answer_approval(
        self, approval_id: str, verdict: ApprovalVerdict
    ) -> ApprovalOutcome | None:
        """Carry the user's verdict. None when nothing is waiting under that id."""
        return await self.approvals.answer(approval_id, verdict)

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

    async def tick(self) -> tuple[tuple[RelayOutcome, ...], tuple[ApprovalOutcome, ...]]:
        """Advance both ceilings. The composition root calls this on a timer.

        Deliberately the only time-driven thing in the hub. Retained notices are
        not swept here: retention has no cap, and a timer that re-offered them
        would be the livelock the outlet-transition rule exists to prevent.
        """
        expired = self.relays.sweep_expired()
        for outcome in expired:
            await self._announce(outcome.target, outcome.report)
        return expired, await self.approvals.sweep_expired()

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
        gone: list[SessionTarget] = []
        for kind, adapter in self._agents.items():
            try:
                lane = await adapter.discover()
            except Exception:  # noqa: BLE001 - a defective lane must not stop the rest
                _log.exception("the %s lane raised instead of reporting its trouble", kind)
                continue
            gone.extend(self._state.sessions.observe(kind, lane, now=self._stamp()))

        for target in gone:
            _log.info("Session %s is no longer running", target)
            for outcome in self.relays.session_ended(target):
                await self._announce(outcome.target, outcome.report)
        return tuple(gone)

    async def dispatch(self, event: Event) -> None:
        """Turn one event into a call on whichever pipeline owns the decision."""
        match event:
            case SessionStopped():
                await self._session_stopped(event)
            case SessionEnded():
                await self._session_ended(event)
            case AwaitingApproval():
                await self.approvals.opened(event.request)
            case ReplyWindowChanged():
                await self._reply_window_changed(event)
            case RelayReceipt():
                self._relay_receipt(event)
            case CallStarted():
                self.interlock.note_started(event.call_id)
                await self.escalation.sweep()
            case CallEnded() | CallDropped():
                # Only a release is an outlet transition. A late event about a
                # call the system was not holding changes nothing, and sweeping
                # on it would attempt a retained notice again for no reason —
                # the guard that keeps uncapped retention from livelocking.
                if self.interlock.note_ended(event.call_id):
                    await self.escalation.sweep()
            case InboundText():
                await self._inbound_text(event)
            case UserSpeech():
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
        only when a notice was retained, retired or failed — so a Stop Notice
        that reached the user left no trace at all, and the acceptance's
        `stop notice` step could satisfy its attribution check only on the
        failure path. An engine silent about the one event it exists to produce
        is the gap #48 named on the inbound side, on the outbound side.
        """
        session = self._known(event.target)
        _log.info(
            "Session stopped: %s waiting on %s%s",
            event.target,
            event.waiting_for.kind,
            f" ({event.waiting_for.tool_name})" if event.waiting_for.tool_name else "",
        )
        held = event.waiting_for.as_approval_request(event.target)
        if held is not None:
            # One dialog, two events, one announcement. A Session entering
            # `waiting` raises this Stop, and the same dialog reached
            # `AwaitingApproval` through the hook that is holding it open — so
            # announcing both would ask the user twice for one decision. The
            # approval notice wins wherever it exists: it is the one carrying a
            # budget, a never-deny fallback and a closing notice, so it is the
            # one that can actually be answered, and `approvals.opened` already
            # asks for `Reach.EVERY_OUTLET`, which is the wider reach of the two.
            #
            # Said out loud rather than dropped: a Stop that produced no notice
            # is otherwise indistinguishable in the log from a Stop that failed
            # to reach every outlet.
            _log.info(
                "the Stop on %s is the dialog %s, which the Approval Relay announces; "
                "not announced twice",
                event.target,
                held.approval_id,
            )
            return
        await self.escalation.escalate(
            Notice(
                request_id=new_request_id(),
                target=event.target,
                text=stop_notice_for(session, event.target, event.waiting_for),
            )
        )

    async def _session_ended(self, event: SessionEnded) -> None:
        try:
            self._state.sessions.mark_ended(event.target)
        except BridgeCoreError:
            _log.info("a Session ended that was never registered: %s", event.target)
        self._state.persist()
        for outcome in self.relays.session_ended(event.target):
            await self._announce(outcome.target, outcome.report)

    async def _reply_window_changed(self, event: ReplyWindowChanged) -> None:
        """An adapter saw the window move between two discoveries. Land it on the state.

        The window is derived, so there is nothing here to set directly: this
        event is a coarser reading of the same fact and it lands on the field
        the fine-grained one lands on. The next discovery overwrites both, which
        is what makes this a shortcut rather than a second source of truth.
        """
        try:
            held = self._state.sessions.resolve(event.target)
            self._state.sessions.set_state(event.target, _state_behind(event.window, held.state))
        except BridgeCoreError:
            _log.info("a Reply Window changed on an unknown Session: %s", event.target)
            return
        if event.window is ReplyWindow.OPEN:
            await self.relays.reply_window_opened(event.target)

    def _relay_receipt(self, event: RelayReceipt) -> None:
        """A receipt that arrived after the call returned. The ledger records it."""
        try:
            self._state.relays.classify(event.receipt.request_id, event.receipt.outcome)
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
        assert found.target is not None  # the router sets one for every ANSWER_RELAY
        try:
            outcome = await self.relays.relay(found.target, found.text)
        except BridgeCoreError as refusal:
            await self._reply(str(refusal))
            return
        if outcome.confirmation:
            await self._reply(outcome.confirmation)

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
        is deliberately not retained on the escalation ledger either — a reply is
        not a notice, and retaining it would replay an answer to a question the
        user asked minutes ago the next time an outlet came up.

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

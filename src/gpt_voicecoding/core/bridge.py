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

**Launching and closing are hub verbs, not surface ones.** The Session registry
is the hub's truth, so the hub is what writes it: the Launcher is injected here
like every other adapter, and a surface asks the hub to launch rather than
calling the Launcher and registering the result itself. A surface that held
that transaction would hold half of it the moment the second half failed.

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
from pathlib import Path

from gpt_voicecoding.core.adjudication import SwitchAdjudicator
from gpt_voicecoding.core.approvals import ApprovalOutcome, ApprovalPipeline, PendingApproval
from gpt_voicecoding.core.clock import Clock, default_clock, wall_clock
from gpt_voicecoding.core.errors import (
    BridgeCoreError,
    SeamUnavailableError,
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
from gpt_voicecoding.core.sessions import Session, SessionState
from gpt_voicecoding.core.state import BridgeState
from gpt_voicecoding.core.switches import SwitchSnapshot
from gpt_voicecoding.core.verification import (
    AGENT_SEAM_PREFIX,
    CALL_SEAM,
    CHANNEL_SEAM,
    LAUNCHER_SEAM,
    SeamLoad,
    SeamVerification,
    Verifiable,
    compare,
)
from gpt_voicecoding.seams.agent import (
    AgentAdapter,
    ApprovalVerdict,
    AwaitingApproval,
    RelayReceipt,
    RelayRoute,
    ReplyWindow,
    ReplyWindowChanged,
    SessionEnded,
    SessionStopped,
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
    SessionLabel,
    SessionTarget,
    new_request_id,
)
from gpt_voicecoding.seams.session_launcher import (
    CloseOutcome,
    CloseRequest,
    CloseStatus,
    LaunchOutcome,
    LaunchRequest,
    LaunchStatus,
    SessionLauncher,
)

_log = logging.getLogger(__name__)

#: Answers an inbound command when no control-plane surface is wired to this hub.
NO_CONTROL_SURFACE = "I recognised that command, but no control surface is wired up here"

#: Answers an inbound delegation when no Delegated Turn handler is wired.
NO_DELEGATE_HANDLER = "I can't take a delegated turn right now — nothing is wired to answer it"


def stop_notice_for(session: Session | None, target: SessionTarget, detail: str = "") -> str:
    """The words a stopped Session is announced with.

    Names the Session the way the user named it, because a Session Label is what
    they will say back when they answer.
    """
    named = str(session.label) if session is not None else f"{target.agent} {target.session_id}"
    tail = f" — {detail}" if detail.strip() else ""
    return f"{named} stopped and may need you{tail}"


@dataclass(frozen=True, slots=True)
class Status:
    """Everything the control plane can ask for. Answered with any switch off."""

    switches: SwitchSnapshot
    sessions: tuple[Session, ...]
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
        launcher: SessionLauncher | None = None,
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
        self._launcher = launcher
        self._events = events or EventQueue()
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
        #: Durations are measured with `clock`; anything written to disk is
        #: stamped with `stamp`. A Session's `registered_at` is read back by the
        #: next engine, and a monotonic reading would come back as the future.
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
            call_id=self.interlock.call_id(),
            pending_relays=self._state.relays.pending(),
            pending_approvals=self.approvals.pending(),
        )

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

    async def launch_session(
        self,
        *,
        agent: AgentKind,
        workspace: Path,
        label: SessionLabel,
        env: Mapping[str, str] | None = None,
    ) -> LaunchOutcome:
        """Bring one Session into existence, and record the one that arrived.

        The registry is the hub's truth, so the hub is what writes it. A surface
        that called the Launcher itself and then registered the result would be
        holding half a transaction — the shape the reference implementation had,
        and the one that leaves a live child nothing knows about the moment the
        second half fails.

        Only a `LAUNCHED` outcome registers anything: an outcome is
        authoritative, and a failed launch that wrote a row would be the system
        inventing a Session to Relay into.
        """
        launcher = self._require_launcher()
        outcome = await launcher.launch(
            LaunchRequest(
                request_id=new_request_id(),
                agent=agent,
                workspace=workspace,
                label=label,
                env=env or {},
            )
        )
        if outcome.status is not LaunchStatus.LAUNCHED:
            return outcome

        assert outcome.target is not None  # the seam refuses a LAUNCHED without one
        self._state.sessions.register(
            Session(
                target=outcome.target,
                label=label,
                workspace=workspace,
                registered_at=self._stamp(),
            )
        )
        self._state.persist()
        return outcome

    async def close_session(self, target: SessionTarget) -> CloseOutcome:
        """Close exactly one Session, by exact identity, and record that it ended.

        Fails closed on an identity this engine never registered, and on a wrong
        pid under a known session id — that is a fork, not a typo. A Session the
        registry already holds as ended answers `already_closed` without
        touching the Launcher: the caller asked for a state that already holds,
        which is what idempotent means, and dialling the Launcher again to be
        told the same thing risks reaping whatever now owns that identity.
        """
        launcher = self._require_launcher()
        try:
            self._state.sessions.resolve(target)
        except StaleSessionError:
            if self._already_ended(target):
                return CloseOutcome(request_id=new_request_id(), status=CloseStatus.ALREADY_CLOSED)
            raise

        outcome = await launcher.close(CloseRequest(request_id=new_request_id(), target=target))
        if outcome.status in (CloseStatus.CLOSED, CloseStatus.ALREADY_CLOSED):
            self._state.sessions.mark_ended(target)
            self._state.persist()
        return outcome

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
        if seam == LAUNCHER_SEAM:
            return self._launcher
        if seam.startswith(AGENT_SEAM_PREFIX):
            try:
                return self._agents.get(AgentKind(seam[len(AGENT_SEAM_PREFIX) :]))
            except ValueError:
                return None
        return None

    def _require_launcher(self) -> SessionLauncher:
        if self._launcher is None:
            raise SeamUnavailableError("Session Launcher")
        return self._launcher

    def _already_ended(self, target: SessionTarget) -> bool:
        """Whether this exact identity is one the registry holds as finished."""
        return any(
            held.target == target and held.state is SessionState.ENDED
            for held in self._state.sessions.all()
        )

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
        session = self._known(event.target)
        await self.escalation.escalate(
            Notice(
                request_id=new_request_id(),
                target=event.target,
                text=stop_notice_for(session, event.target, event.detail),
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
        try:
            self._state.sessions.set_reply_window(event.target, event.window)
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
        """
        if not text:
            return
        await self._channel.send(text, request_id=new_request_id())

    def _known(self, target: SessionTarget) -> Session | None:
        try:
            return self._state.sessions.resolve(target)
        except BridgeCoreError:
            return None

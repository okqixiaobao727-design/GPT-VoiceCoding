"""The Call seam, over ``codex app-server``'s realtime route. The one voice surface.

**This adapter owns its call and grades itself on it.** Every verdict it returns
comes from its own signalling conversation and its own peer connection — never
from matching against another surface's records. The reference implementation
graded an audibly spoken notice FAILED by comparing fragile fields on somebody
else's turn object, and the retries that followed opened duplicate calls
(legacy issue #16, § 4). A `speak` here is DELIVERED when the app-server
accepted it *and* this adapter's own audio path is up at that moment, and
nothing else is consulted.

**It owns no process.** The `codex app-server` the realtime route rides is the
engine's own, spawned and reaped by the Codex Agent adapter, and handed here by
the composition root. This adapter cannot start one, cannot restart one, and
does not try: when that process goes, what it sees is its own connection ending,
which it reports upward as a dropped call. Reconnection is Bridge Core's
decision to make, and Bridge Core is where the one-call invariant lives.

**Two threads, both bridge-owned, both told what they are.** A Live Call runs on
a thread started for it, whose `realtimeStartInstructions` are the voice house
rules Bridge Core generated. A Delegated Turn runs on a thread of its own,
started with the caller's model — the cost lever, which is a user-facing setting
and is never defaulted here — and with the delegated action discipline as its
developer instructions. Both run `approvalPolicy: "never"` in
`danger-full-access`, which is the trade recorded in legacy issue #19: the
threads act only through the control-plane CLI, and that CLI needs an `AF_UNIX`
connect that no narrower sandbox permits. The user's own coding Sessions are
untouched by any of this — they keep their approval rules, and this adapter
never goes near them.

**A Delegated Turn's thread does not outlive it.** Fresh thread, one turn, then
gone, on every path including the failing ones. Keeping one alive per model
would be a second ledger of which thread is the real one, and nothing in the
locked decisions asks for continuity between delegated turns.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from gpt_voicecoding.adapters.call.realtime.settings import RealtimeCallSettings
from gpt_voicecoding.adapters.call.realtime.transport import (
    CallTransport,
    TransportError,
    TransportFactory,
)
from gpt_voicecoding.adapters.codex_app_server.process import AppServerError, OwnedAppServer
from gpt_voicecoding.adapters.codex_app_server.wire import Message, RemoteError, WireError
from gpt_voicecoding.seams.call import (
    CallDropped,
    CallEnded,
    CallSnapshot,
    CallStarted,
    CallState,
    DelegatedReply,
    UserSpeech,
    VoiceSpeech,
)
from gpt_voicecoding.seams.delivery import Delivery, DeliveryReceipt
from gpt_voicecoding.seams.events import EventSink
from gpt_voicecoding.seams.identity import RequestId
from gpt_voicecoding.seams.verify import VerifyOutcome, VerifyResult

_log = logging.getLogger(__name__)

#: Approval-free, and confined to the bridge's own threads. See the module
#: docstring and `settings.py` for why this is pinned rather than configurable.
APPROVAL_POLICY = "never"

#: The one sandbox in which the control-plane CLI's `AF_UNIX` connect succeeds.
SANDBOX = "danger-full-access"

#: The realtime conversation version the WebRTC route was proven against.
REALTIME_VERSION = "v3"

#: A voice call is audio out. `text` is the same route with the point removed.
OUTPUT_MODALITY = "audio"

#: Which side of a realtime transcript is the user speaking.
USER_ROLE = "user"

#: The other side: this system's own Voice. The wire's word for it, translated
#: here into the seam's `VoiceSpeech` and never carried upward as it is — above
#: this adapter the glossary has no unqualified *assistant* (`CONTEXT.md`).
ASSISTANT_ROLE = "assistant"


class _Abandoned(Exception):
    """This call attempt was hung up or dropped while the handshake was running."""


class DelegatedTurnError(Exception):
    """One Delegated Turn could not be completed. Carries why, in the model's words."""


@dataclass
class _LiveCall:
    """One call attempt, registered before anything is asked of the far side.

    It exists from the moment `ensure_call` commits to trying, which is earlier
    than it has a thread to name — deliberately, because `end_call` has to be
    able to abandon an attempt that is still waiting on `thread/start`. A
    `thread_id` of None therefore means "asked for, not yet answered", and every
    step of the handshake re-reads `ending` before doing the next thing.
    """

    transport: CallTransport
    sdp: asyncio.Future[str]
    started: asyncio.Future[None]
    #: What codex called the thread, once it has said. None until then.
    thread_id: str | None = None
    #: Set when *this side* asked for the end, so the far side going quiet
    #: afterwards is not reported as a loss.
    ending: bool = False
    #: Whether the Voice is in the middle of an utterance on *this* call. Per
    #: attempt rather than per adapter, so a call that drops between the first
    #: delta and the `done` that would have cleared it cannot leave the latch
    #: set over the call that follows.
    speaking: bool = False

    def fail(self, reason: str) -> None:
        """Stop anything waiting on this attempt, with a reason rather than a hang."""
        for waiting in (self.sdp, self.started):
            if not waiting.done():
                waiting.set_exception(TransportError(reason))
                # Read straight back, which marks it retrieved. Whether anything
                # was waiting depends on how far the handshake had got; a caller
                # that is waiting still receives it, and one that never got that
                # far no longer produces an asyncio warning standing where the
                # real reason — already reported upward — belongs.
                waiting.exception()


@dataclass
class _DelegatedTurn:
    """One delegated thread's turn, while it is running."""

    thread_id: str
    messages: list[str] = field(default_factory=list)
    done: asyncio.Future[Message] | None = None
    #: What codex called the turn, so an abandoned one can actually be stopped.
    #: None until `turn/start` answers.
    turn_id: str | None = None
    #: Whether codex has said the turn is over. Recorded as its own fact rather
    #: than inferred from `turn_id` being absent, because the two arrive on
    #: different paths: `turn/completed` is a notification the reader task
    #: delivers, and it can land *before* the `turn/start` response this side is
    #: still awaiting. Clearing the id there would only have it written back a
    #: moment later, and a finished turn would be interrupted on the way out.
    completed: bool = False


class RealtimeCallAdapter:
    """The bridge-owned Live Call. Implements `CallAdapter` and `Connectable`."""

    def __init__(
        self,
        *,
        sink: EventSink | None = None,
        settings: RealtimeCallSettings | None = None,
        transport_factory: TransportFactory,
    ) -> None:
        self._sink = sink
        self._settings = settings or RealtimeCallSettings()
        self._new_transport = transport_factory
        self._server: OwnedAppServer | None = None
        self._call: _LiveCall | None = None
        self._state = CallState.DOWN
        self._delegating: dict[str, _DelegatedTurn] = {}
        #: Teardowns started from a notification callback, so none outlives this
        #: adapter. The sink is non-blocking by contract; closing a transport is
        #: not, so it cannot happen inline.
        self._closing: set[asyncio.Task[None]] = set()
        #: One attempt at a time. `ensure_call` is idempotent, and two callers
        #: racing through it is exactly how one becomes two.
        self._opening = asyncio.Lock()

    # -- the transport this adapter is lent -------------------------------

    def use_app_server(self, server: OwnedAppServer) -> None:
        """Take the app-server the Codex Agent adapter owns. Once, before opening.

        Wired by the composition root, which is the only thing allowed to know
        two adapters at once. Called twice, it raises rather than quietly
        swapping: the second server would be one this adapter's live call is not
        on, and a transport that changes underneath a call is not a state worth
        having a recovery path for.
        """
        if self._server is not None:
            raise AppServerError("this Call adapter already has an app-server to ride")
        self._server = server
        server.listen(self._heard)

    async def connect(self) -> None:
        """Check this adapter has what it needs. Opens nothing of its own.

        The app-server is started by the component that owns it, and adapters
        open in an order this adapter does not control, so there is deliberately
        nothing to open here — only the wiring to insist on, at the one moment
        the engine can still refuse to start over it.
        """
        if self._server is None:
            raise AppServerError(
                "this Call adapter was never handed the codex app-server it rides; "
                "the composition root wires it from the Codex Agent adapter"
            )

    async def aclose(self) -> None:
        """End the call, drop the delegated threads, leave the server alone."""
        for task in list(self._closing):
            task.cancel()
        self._closing.clear()
        await self.end_call()
        for thread_id in list(self._delegating):
            await self._retire(thread_id)

    # -- the seam ---------------------------------------------------------

    async def ensure_call(self, instructions: str) -> CallSnapshot:
        """Start a Live Call on those house rules, or report the one already up."""
        async with self._opening:
            if self._call is not None:
                return self.snapshot()
            if not instructions.strip():
                _log.warning("refusing to start a voice thread with no instructions")
                return CallSnapshot(state=CallState.DOWN)
            try:
                self._connection()
                live = self._registered()
            except (AppServerError, TransportError) as unavailable:
                _log.warning("no call could be started: %s", unavailable)
                return CallSnapshot(state=CallState.DOWN)
            return await self._opened(live, instructions)

    async def end_call(self) -> CallSnapshot:
        """End the current call. Idempotent, and a call already gone is not an error.

        Deliberately not serialised behind the opening lock: ending a call that
        is still connecting has to be possible, and taking that lock would make
        it wait for the very handshake it is trying to abandon.
        """
        live, self._call = self._call, None
        if live is None:
            self._state = CallState.DOWN
            return CallSnapshot(state=CallState.DOWN)

        was_up = self._state is CallState.UP
        self._state = CallState.DOWN
        live.ending = True
        detail = ""
        if live.thread_id is not None:
            try:
                await self._request(
                    "thread/realtime/stop",
                    {"threadId": live.thread_id},
                    timeout=self._settings.request_timeout_seconds,
                )
            except (WireError, AppServerError) as gone:
                # The call being already gone is the ordinary way a call ends.
                # This verb's promise is that the call is over afterwards, not
                # that this side was the one that ended it.
                detail = f"the call was already gone: {gone}"
        # An attempt with no thread yet is one `thread/start` has not answered.
        # Marking it ending is what stops it: the handshake checks before every
        # step, and the step that learns the thread id is also the one that
        # cleans it up.
        live.fail("this call was ended")
        await live.transport.aclose()
        if was_up and live.thread_id is not None:
            self._emit(CallEnded(call_id=live.thread_id, detail=detail))
        return CallSnapshot(state=CallState.DOWN)

    async def call_state(self) -> CallSnapshot:
        """What this adapter's own connection state says, right now."""
        live = self._call
        if live is not None and self._state is CallState.UP and not live.transport.is_connected:
            # The peer connection went away without this adapter having heard
            # about it yet. Reporting UP because a flag says so would be the
            # adapter believing its own bookkeeping over its own connection.
            return CallSnapshot(state=CallState.CONNECTING)
        return self.snapshot()

    async def speak(self, text: str, *, request_id: RequestId) -> DeliveryReceipt:
        """Say something into the call, graded on this adapter's own audio path."""
        live = self._call
        if live is None or self._state is not CallState.UP:
            return _failed(request_id, "no call is up to speak into")
        try:
            await self._request(
                "thread/realtime/appendSpeech",
                {"threadId": live.thread_id, "text": text},
                timeout=self._settings.request_timeout_seconds,
            )
        except RemoteError as refused:
            # A refused request never reached the realtime session, so nothing
            # was said and Bridge Core is free to route this notice elsewhere.
            return _failed(request_id, f"codex refused the speech: {refused.remote_message}")
        except (WireError, AppServerError) as lost:
            # Past the dial with no answer: the words may or may not have gone
            # out. UNKNOWN is what stops that becoming a duplicate.
            return _unknown(request_id, f"codex never answered the speech: {lost}")

        if self._call is not live or not live.transport.is_connected:
            return _unknown(
                request_id,
                "codex accepted the speech, but this call's audio had already gone",
            )
        return DeliveryReceipt(request_id=request_id, outcome=Delivery.DELIVERED)

    async def delegate(
        self, text: str, *, model: str, instructions: str, request_id: RequestId
    ) -> DelegatedReply:
        """Hand work to a coding model on the user's behalf — the Delegated Turn."""
        started = await self._request(
            "thread/start",
            self._thread_parameters(model=model, developer_instructions=instructions),
            timeout=self._settings.request_timeout_seconds,
        )
        thread_id = _thread_id_in(started)
        # What the server says it is actually running, not what was asked for.
        # A caller reading back the model it already named would learn nothing.
        produced = started.get("model")
        turn = _DelegatedTurn(thread_id=thread_id)
        turn.done = asyncio.get_running_loop().create_future()
        self._delegating[thread_id] = turn
        try:
            return await self._delegated(turn, text, request_id, produced or model)
        finally:
            # Every path, including the failing ones. A thread left behind is a
            # thread nothing will ever close.
            await self._retire(thread_id)

    async def verify(self) -> VerifyResult:
        """Report what is loaded, and whether the app-server this rides answers."""
        loaded = f"{type(self).__module__}:{type(self).__name__}"
        try:
            await self._request(
                "thread/loaded/list", {}, timeout=self._settings.request_timeout_seconds
            )
        except (WireError, AppServerError) as unreachable:
            return VerifyResult(
                outcome=VerifyOutcome.FAIL,
                loaded=loaded,
                detail=f"the codex app-server did not answer: {unreachable}",
            )
        return VerifyResult(
            outcome=VerifyOutcome.PASS, loaded=loaded, detail=f"the call is {self._state}"
        )

    def snapshot(self) -> CallSnapshot:
        """This adapter's own answer about its own call, without asking anything."""
        if self._state is CallState.UP and self._call is not None:
            return CallSnapshot(state=CallState.UP, call_id=self._call.thread_id)
        if self._call is not None:
            return CallSnapshot(state=CallState.CONNECTING)
        return CallSnapshot(state=CallState.DOWN)

    # -- bringing a call up ------------------------------------------------

    def _registered(self) -> _LiveCall:
        """Claim the attempt before anything is asked of the far side.

        This is what makes `end_call` during connection setup work at all: an
        attempt that only became visible once `thread/start` had answered would
        leave a window in which a hang-up found nothing to hang up, and the
        handshake carried on to bring a call up that nobody wanted.
        """
        loop = asyncio.get_running_loop()
        live = _LiveCall(
            transport=self._new_transport(), sdp=loop.create_future(), started=loop.create_future()
        )
        live.transport.on_lost(lambda reason, held=live: self._lost(held, reason))
        self._call = live
        self._state = CallState.CONNECTING
        return live

    async def _opened(self, live: _LiveCall, instructions: str) -> CallSnapshot:
        """The whole handshake. Anything that goes wrong leaves nothing running."""
        deadline = self._settings.connect_timeout_seconds
        try:
            started = await self._request(
                "thread/start",
                self._thread_parameters(),
                timeout=self._settings.request_timeout_seconds,
            )
            # Recorded before the abandonment check, so a hang-up that raced
            # `thread/start` still has a thread to name when it cleans up.
            live.thread_id = _thread_id_in(started)
            self._still_wanted(live)

            offer = await live.transport.offer()
            self._still_wanted(live)
            await self._request(
                "thread/realtime/start",
                {
                    "threadId": live.thread_id,
                    "version": REALTIME_VERSION,
                    # Top level, beside the version — not nested in a `session`
                    # object. codex overrides its own default with this (#35).
                    "model": self._settings.realtime_model,
                    "outputModality": OUTPUT_MODALITY,
                    "transport": {"type": "webrtc", "sdp": offer},
                    "realtimeStartInstructions": instructions,
                },
                timeout=self._settings.request_timeout_seconds,
            )
            answer = await asyncio.wait_for(live.sdp, deadline)
            await live.transport.accept_answer(answer)
            await asyncio.wait_for(live.started, deadline)
            await live.transport.wait_connected(deadline)
            self._still_wanted(live)
        except _Abandoned:
            # `end_call` already took it out of `self._call`; this only has to
            # stop what the handshake itself started.
            await self._abandon(live, "this call was hung up while it was connecting")
            return CallSnapshot(state=CallState.DOWN)
        except (
            TimeoutError,
            TransportError,
            WireError,
            AppServerError,
            asyncio.CancelledError,
        ) as failed:
            # The model is named because the failure that cost two days was an
            # upstream refusal of the model value reported as a refusal of the
            # field, from a message that never said which value had been sent
            # (#35). Upstream's own words still come through verbatim.
            await self._abandon(
                live,
                f"the call did not come up: {failed} "
                f"(asking for realtime model {self._settings.realtime_model})",
            )
            return CallSnapshot(state=CallState.DOWN)

        self._state = CallState.UP
        assert live.thread_id is not None  # set above, before anything could use it
        self._emit(CallStarted(call_id=live.thread_id))
        return CallSnapshot(state=CallState.UP, call_id=live.thread_id)

    def _still_wanted(self, live: _LiveCall) -> None:
        """Refuse to take the next step on an attempt somebody has abandoned."""
        if live.ending or self._call is not live:
            raise _Abandoned()

    async def _abandon(self, live: _LiveCall, reason: str) -> None:
        """Leave nothing running behind a call that never came up."""
        _log.info("%s", reason)
        if self._call is live:
            self._call = None
            self._state = CallState.DOWN
        live.fail(reason)
        with suppress(WireError, AppServerError):
            await self._request(
                "thread/realtime/stop",
                {"threadId": live.thread_id},
                timeout=self._settings.request_timeout_seconds,
            )
        await live.transport.aclose()

    def _lost(self, live: _LiveCall, reason: str) -> None:
        """The audio path went away by itself. Never blocks: called from a callback."""
        self._drop(live, reason)

    def _drop(self, live: _LiveCall, reason: str) -> None:
        """Report a call that ended without being asked to, exactly once."""
        if live.ending or self._call is not live:
            return
        was_up = self._state is CallState.UP
        self._call = None
        self._state = CallState.DOWN
        live.fail(reason)
        if was_up:
            self._emit(CallDropped(call_id=live.thread_id, detail=reason))
        self._spawn(live.transport.aclose())

    # -- one Delegated Turn -------------------------------------------------

    async def _delegated(
        self, turn: _DelegatedTurn, text: str, request_id: RequestId, model: str
    ) -> DelegatedReply:
        """Run the turn and read its answer, or say in words why there is none."""
        assert turn.done is not None
        try:
            accepted = await self._request(
                "turn/start",
                {
                    "threadId": turn.thread_id,
                    "clientUserMessageId": str(request_id),
                    "input": [{"type": "text", "text": text}],
                },
                timeout=self._settings.request_timeout_seconds,
            )
            turn.turn_id = _turn_id_in(accepted)
        except RemoteError as refused:
            raise DelegatedTurnError(
                f"codex refused the delegated turn: {refused.remote_message}"
            ) from None
        except (WireError, AppServerError) as lost:
            raise DelegatedTurnError(f"codex never answered the delegated turn: {lost}") from None

        try:
            completed = await asyncio.wait_for(
                turn.done, self._settings.delegated_turn_timeout_seconds
            )
        except TimeoutError:
            raise DelegatedTurnError(
                "the delegated turn did not finish within "
                f"{self._settings.delegated_turn_timeout_seconds:g}s"
            ) from None

        status = completed.get("status")
        if status != "completed":
            error = completed.get("error")
            detail = error.get("message") if isinstance(error, dict) else None
            raise DelegatedTurnError(f"the delegated turn {status}: {detail or 'no reason given'}")
        if not turn.messages:
            raise DelegatedTurnError("the delegated turn finished without saying anything")
        return DelegatedReply(text="\n\n".join(turn.messages), model=model)

    async def _retire(self, thread_id: str) -> None:
        """Stop one delegated turn and let its thread go. Never raises.

        **Interrupted before unsubscribed, and in that order.** Unsubscribing
        only stops this engine hearing about the turn; the turn itself keeps
        running, and a bridge-owned thread runs approval-free in
        `danger-full-access`, so a turn abandoned on a timeout would go on
        acting on the user's machine and spending their money with nothing left
        watching it. Interrupting is what actually ends it, and it is only sent
        for a turn codex has not already said is over.

        This runs on the failing paths, so it swallows everything: a cleanup
        that raises would replace the classified failure the caller is about to
        report with an unrelated one.
        """
        turn = self._delegating.pop(thread_id, None)
        if turn is not None and turn.turn_id is not None and not turn.completed:
            with suppress(Exception):
                await self._request(
                    "turn/interrupt",
                    {"threadId": thread_id, "turnId": turn.turn_id},
                    timeout=self._settings.request_timeout_seconds,
                )
        if turn is not None and turn.done is not None and not turn.done.done():
            turn.done.cancel()
        with suppress(Exception):
            await self._request(
                "thread/unsubscribe",
                {"threadId": thread_id},
                timeout=self._settings.request_timeout_seconds,
            )

    # -- what the app-server says -------------------------------------------

    def _heard(self, message: Message) -> None:
        """One notification. Never blocks: the event sink is non-blocking by contract."""
        method = message.get("method")
        params = message.get("params")
        if not isinstance(params, dict):
            return
        thread_id = params.get("threadId")

        live = self._call
        if live is not None and thread_id == live.thread_id:
            self._call_heard(live, str(method), params)
        turn = self._delegating.get(thread_id) if isinstance(thread_id, str) else None
        if turn is not None:
            self._turn_heard(turn, str(method), params)

    def _call_heard(self, live: _LiveCall, method: str, params: Message) -> None:
        match method:
            case "thread/realtime/sdp":
                sdp = params.get("sdp")
                if isinstance(sdp, str) and not live.sdp.done():
                    live.sdp.set_result(sdp)
            case "thread/realtime/started":
                if not live.started.done():
                    live.started.set_result(None)
            case "thread/realtime/transcript/delta":
                self._voice_started(live, params)
            case "thread/realtime/transcript/done":
                self._transcribed(live, params)
            case "thread/realtime/error":
                self._drop(live, f"the realtime session failed: {params.get('message')}")
            case "thread/realtime/closed":
                reason = params.get("reason") or "no reason given"
                self._drop(live, f"the realtime session closed: {reason}")

    def _voice_started(self, live: _LiveCall, params: Message) -> None:
        """The Voice began an utterance. Only the first delta of one is news.

        A long answer is hundreds of these — 604 in the probe records — and the
        seam publishes a *span*, so every delta after the first says nothing the
        span does not already say.
        """
        if params.get("role") != ASSISTANT_ROLE or live.speaking:
            return
        live.speaking = True
        self._emit(VoiceSpeech(speaking=True))

    def _transcribed(self, live: _LiveCall, params: Message) -> None:
        """Raise what was said, from whichever side of the transcript said it.

        The Voice's half is raised as the end of a span rather than as words:
        this system does not read its own speech back to itself. It is raised on
        every assistant `done`, latch or no latch, because `done` is emitted
        once per *turn* — an answer codex splits in two produces two of them,
        and a `done` with no delta before it is still an utterance that ended.
        """
        role = params.get("role")
        if role == ASSISTANT_ROLE:
            live.speaking = False
            self._emit(VoiceSpeech(speaking=False))
            return
        if role != USER_ROLE:
            return
        text = params.get("text")
        if isinstance(text, str) and text.strip():
            self._emit(UserSpeech(text=text))

    def _turn_heard(self, turn: _DelegatedTurn, method: str, params: Message) -> None:
        if method == "item/completed":
            item = params.get("item")
            if isinstance(item, dict) and item.get("type") == "agentMessage":
                said = item.get("text")
                if isinstance(said, str) and said.strip():
                    turn.messages.append(said)
            return
        if method == "turn/completed":
            completed = params.get("turn")
            if isinstance(completed, dict):
                # It is over, so there is nothing left to interrupt on the way out.
                turn.completed = True
                if turn.done is not None and not turn.done.done():
                    turn.done.set_result(completed)

    # -- talking to the app-server ------------------------------------------

    def _thread_parameters(
        self, *, model: str | None = None, developer_instructions: str | None = None
    ) -> Message:
        """One bridge-owned thread's start parameters. See the module docstring."""
        parameters: Message = {
            "cwd": str(self._settings.cwd),
            "approvalPolicy": APPROVAL_POLICY,
            "sandbox": SANDBOX,
        }
        if model is not None:
            parameters["model"] = model
        if developer_instructions is not None:
            parameters["developerInstructions"] = developer_instructions
        return parameters

    async def _request(self, method: str, params: Message, *, timeout: float) -> Message:
        return await self._connection().request(method, params, timeout_seconds=timeout)

    def _connection(self) -> Any:
        server = self._server
        if server is None:
            raise AppServerError("this Call adapter was never handed the codex app-server it rides")
        return server.connection

    def _spawn(self, work: Any) -> None:
        """Run a teardown without letting it outlive the adapter that started it."""
        task = asyncio.ensure_future(work)
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    def _emit(self, event: Any) -> None:
        if self._sink is not None:
            self._sink.emit(event)


def _thread_id_in(started: Message) -> str:
    """The id codex gave the thread it just started, or a refusal to guess one."""
    thread = started.get("thread")
    thread_id = thread.get("id") if isinstance(thread, dict) else None
    if not isinstance(thread_id, str) or not thread_id:
        raise AppServerError("codex started a thread without naming it")
    return thread_id


def _turn_id_in(accepted: Message) -> str | None:
    """What codex called the turn it just started, or None if it did not say.

    None rather than a raise: the turn is running either way, and refusing the
    whole Delegated Turn over a missing id would throw away an answer that is
    about to arrive. What is lost is only the ability to interrupt it.
    """
    turn = accepted.get("turn")
    found = turn.get("id") if isinstance(turn, dict) else None
    return found if isinstance(found, str) and found else None


def _failed(request_id: RequestId, reason: str) -> DeliveryReceipt:
    return DeliveryReceipt(request_id=request_id, outcome=Delivery.FAILED, reason=reason)


def _unknown(request_id: RequestId, reason: str) -> DeliveryReceipt:
    return DeliveryReceipt(request_id=request_id, outcome=Delivery.UNKNOWN, reason=reason)

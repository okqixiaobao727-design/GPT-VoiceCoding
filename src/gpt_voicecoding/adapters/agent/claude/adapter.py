"""The Agent seam, over the Claude Session Channel. Mechanism only; no queueing.

**What proves delivery.** The channel route cannot mint the hub's `request_id`
into the transcript the way `turn/start` does on the Codex side, so the proof is
the other direction: the correlation id rides inside the message, and the
Session calls `acknowledge_answer` with it. That tool call is the only positive
proof there is. The channel accepting the line is not — it says the notification
was pushed, not that the Session read it.

**A late acknowledgement is an upgrade, and that is why it exists.** The wait is
bounded, and a spent wait is UNKNOWN, never DELIVERED. But Bridge Core retains
anything not proven delivered and sends it again when the Reply Window next
opens — so an acknowledgement that arrives after the wait and is thrown away
becomes the hub re-delivering words that provably arrived. The connection is
therefore kept, for a second and longer budget, purely so a late receipt can be
raised upward and the hub can drop what it was holding. Only DELIVERED is ever
raised that way; a late anything-else is logged and changes nothing.

**Two verbs over two different wires.** This package is the shared Claude
adapter. The Answer Relay rides the MCP Session Channel, described above. The
Approval Relay rides the **`PermissionRequest` hook**, delegated to
`approval.py`, and it is the route where *we* are the server: a hook process
dials in holding a displayed dialog open, and the verdict travels back down the
connection it is waiting on.

The Approval Relay is also the one verb whose route this adapter cannot start.
The hook only exists for a Session launched with `--plugin-dir` naming the
rendered hook plugin, and only reaches us when the launch also carried the
approval socket's address — so a Session launched without either has no Approval
Relay, and the honest report for a verdict aimed at it is a classified failure
naming what is not there.

**The Reply Window is reported from the registry**, by `window.py`. Without it
nothing ever tells Bridge Core that a Claude Session is ready for a user turn, so
every Relay queues against a window that is fail-closed and never observed to
open. That is the mechanism working correctly and a Session nothing can reach;
watching the registry is what makes the two stop being the same thing.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from pathlib import Path

from gpt_voicecoding.adapters.agent.claude.approval import ApprovalError, ApprovalListener
from gpt_voicecoding.adapters.agent.claude.bootstrap import bootstrap_value
from gpt_voicecoding.adapters.agent.claude.protocol import (
    ACKNOWLEDGED,
    CHANNEL_ERROR,
    KIND_FIELD,
    QUEUED,
    REQUEST_ID_FIELD,
    TEXT_FIELD,
    channel_kind_for,
)
from gpt_voicecoding.adapters.agent.claude.settings import ClaudeSettings
from gpt_voicecoding.adapters.agent.claude.window import ReplyWindowWatcher
from gpt_voicecoding.adapters.agent.claude.wire import (
    ChannelConnection,
    ChannelError,
)
from gpt_voicecoding.seams.agent import (
    AgentEvent,
    ApprovalRequest,
    ApprovalVerdict,
    RelayReceipt,
    RelayRoute,
    ReplyWindow,
)
from gpt_voicecoding.seams.delivery import Delivery, DeliveryReceipt
from gpt_voicecoding.seams.events import EventSink
from gpt_voicecoding.seams.identity import AgentKind, RequestId, SessionTarget
from gpt_voicecoding.seams.verify import VerifyOutcome, VerifyResult

_log = logging.getLogger(__name__)

#: What a verdict aimed at a Session with no hook route is told. It names the two
#: things a launch has to have done, because "no request is waiting" read alone
#: looks like a race that was lost rather than a route that was never there.
APPROVAL_UNROUTED = (
    "no permission dialog is parked on this engine for that Session; a Claude Session "
    "answers by voice only when its launch carried both the hook plugin (--plugin-dir) "
    "and this engine's approval socket address"
)
SUPPLEMENT_UNAVAILABLE = (
    "the Claude Session Channel has no mid-turn route: a channel message delivered inside "
    "a turn is framed as not being from the user and is refused"
)


class ClaudeAgentAdapter:
    """Claude, behind the Agent seam. Implements `AgentAdapter` and `Connectable`."""

    def __init__(
        self, *, sink: EventSink | None = None, settings: ClaudeSettings | None = None
    ) -> None:
        self._sink = sink
        self._settings = settings or ClaudeSettings()
        #: The channel socket each registered Session's launch wrapper reported.
        self._channels: dict[SessionTarget, Path] = {}
        #: Late-receipt listeners in flight, so none outlives this adapter.
        self._listening: set[asyncio.Task[None]] = set()
        self._windows = ReplyWindowWatcher(settings=self._settings, emit=self._emit)
        #: The socket this adapter owns: hook processes dial in here holding a
        #: dialog open, so this adapter is the server on this route.
        self._approvals = ApprovalListener(
            settings=self._settings, resolve=self._registered_as, emit=self._emit
        )

    # -- lifecycle --------------------------------------------------------

    async def connect(self) -> None:
        """Start watching Reply Windows, and open the socket dialogs are parked on.

        The approval socket is bound here because it lives in a directory of
        this engine's own, and its address has to be knowable *before* any Session
        launches: the launch is what carries it to the hook. A socket bound on
        first use would be one no launch could ever have named.

        A socket that will not bind is logged and not raised. It costs the
        Approval Relay and nothing else — every verdict aimed at this adapter is
        then a classified failure — and taking the whole Agent seam down over it
        would trade one lost route for two working ones.
        """
        await self._windows.start()
        try:
            await self._approvals.start()
        except ApprovalError as refused:
            _log.warning("no Approval Relay this run: %s", refused)

    async def aclose(self) -> None:
        """Stop everything this adapter started, and take its socket back out.

        A channel is a process Claude Code owns. Closing this adapter lets go of
        connections to it and nothing more — there is nothing there to reap.

        **Each cancelled task is waited for, not merely cancelled.** A
        cancellation is a request, delivered the next time the task runs, so
        cancelling and returning would make `aclose` mean "asked them to stop"
        while a listener was still holding a connection to a Session's own
        process. Waiting is what makes it mean "they have stopped": nothing this
        adapter started is still running when this returns.
        """
        listening = list(self._listening)
        for task in listening:
            task.cancel()
        for task in listening:
            with suppress(asyncio.CancelledError):
                await task
        self._listening.clear()
        await self._windows.aclose()
        # Closes last, and releases every parked dialog to its human
        # on the way out: an engine shutting down must never be the reason a
        # permission prompt resolves without the person it was asked of.
        await self._approvals.aclose()
        self._channels.clear()

    # -- the Session roster this adapter can reach ------------------------

    def register_session(self, target: SessionTarget, socket_path: Path) -> None:
        """Record where one Session's channel listens.

        The path is the registration this adapter needs and cannot discover: the
        channel is spawned by Claude Code from an environment variable the
        launch wrapper generated, so its address arrives from the launcher.
        """
        if target.agent is not AgentKind.CLAUDE:
            raise ValueError(f"{target.agent} sessions are not this adapter's to reach")
        self._channels[target] = socket_path
        # Registering is also what starts reporting this Session's Reply Window,
        # which is what makes it reachable at all: until a window is observed,
        # Bridge Core holds every Relay against the fail-closed default.
        self._windows.watch(target)
        _log.info(
            "registered Session channel agent=%s session_id=%s pid=%s socket=%s",
            target.agent,
            target.session_id,
            target.pid,
            socket_path,
        )

    def forget_session(self, target: SessionTarget) -> None:
        """Stop holding a route to one Session. The Session itself is untouched."""
        self._channels.pop(target, None)
        self._windows.forget(target)

    def reachable(self) -> tuple[SessionTarget, ...]:
        """Every Session this adapter holds a channel address for."""
        return tuple(self._channels)

    def reply_window(self, target: SessionTarget) -> ReplyWindow:
        """Where this Session's Reply Window stands right now, read from the registry.

        The seam's level query, and how a Session's *starting* window reaches
        Bridge Core at all: registration cannot announce it, because it runs
        before Bridge Core holds the Session (#27), so Bridge Core asks instead.

        A Session this adapter holds no channel for is CLOSED, whatever its
        registry record happens to say. The window is a claim about reachability,
        and reading someone else's record is not the same as being able to reach
        them — the same fail-closed rule the whole seam runs on.
        """
        if target not in self._channels:
            return ReplyWindow.CLOSED
        return self._windows.level(target)

    # -- the seam ---------------------------------------------------------

    def supported_routes(self) -> frozenset[RelayRoute]:
        """Deliver only, and honestly so: the channel is refused mid-turn."""
        return frozenset({RelayRoute.DELIVER})

    async def answer_relay(
        self,
        target: SessionTarget,
        text: str,
        *,
        request_id: RequestId,
        route: RelayRoute = RelayRoute.DELIVER,
    ) -> DeliveryReceipt:
        """Carry the user's own words in, with the user's authority."""
        if route is RelayRoute.SUPPLEMENT:
            # Bridge Core decides what to do instead — queueing is its policy,
            # never this adapter's.
            return _failed(request_id, SUPPLEMENT_UNAVAILABLE)
        return await self._deliver(target, text, request_id=request_id, verb="answer_relay")

    async def approval_relay(
        self, request: ApprovalRequest, verdict: ApprovalVerdict, *, request_id: RequestId
    ) -> DeliveryReceipt:
        """Carry one verdict into the hook that is holding the dialog open.

        A verdict for a dialog nothing is parked on is a failure and not a
        silence, because there are several ways to arrive here with nothing
        waiting — the human answered on screen, the request already resolved,
        or the Session never had a hook route at all — and Bridge Core can only
        record what it is told.
        """
        if request.target.agent is not AgentKind.CLAUDE:
            return _failed(
                request_id, f"{request.target.agent} sessions are not this adapter's to reach"
            )
        if not self._approvals.listening:
            return _failed(request_id, APPROVAL_UNROUTED)
        return await self._approvals.answer(request.approval_id, verdict, request_id=request_id)

    def registry_directory(self) -> Path:
        """Where launches find the Session records this adapter later observes."""
        return self._settings.registry_directory

    def approval_socket_path(self) -> Path:
        """Where a launch should tell this Session's hook to find us.

        Answered whether or not the socket is bound, because it is derived rather
        than discovered and a launcher asking where to point a hook is asking a
        question about this engine's identity, not about its current state.
        """
        return self._approvals.path

    def launch_bootstrap(self, channel_socket_path: Path) -> str:
        """What one launch must set the bootstrap variable to for this engine.

        The Session Launcher owns the child environment but not the contents of
        this value: the byte budgets inside it are this adapter's settings, and
        the approval address is this adapter's socket. So the launcher says where
        the channel should listen — that path is per-launch and only it can mint
        one — and asks here for everything else.

        Answered before anything is bound, for the same reason
        `approval_socket_path` is: a launch has to carry the address into the
        Session that will dial it, so the address must exist first.
        """
        return bootstrap_value(
            channel_socket_path,
            self._settings,
            approval_socket_path=self.approval_socket_path(),
        )

    def _registered_as(self, session_id: str) -> SessionTarget | None:
        """The authority check behind the approval socket, in this adapter's own terms.

        A Claude target is addressed by pid and the hook payload carries only a
        session id, so the two are matched here against the roster the launcher
        registered — the same roster every other verb addresses. A session id
        this adapter holds no channel for is not this engine's Session, and its
        dialog is not this engine's to answer.

        **An ambiguous session id is refused rather than guessed.** `--resume`
        forks a second process under one session id, which is the whole reason a
        Claude target carries a pid, and a hook payload carries no pid to break
        the tie with. Answering anyway would still deliver the verdict — it
        travels back down the hook's own connection either way — but it would
        announce the dialog against the wrong process, and a notice naming the
        wrong Session is the truthfulness failure, not the lost approval. The
        dialog stays with its human, which is the safe direction to be wrong in.
        """
        matches = [target for target in self._channels if target.session_id == session_id]
        if len(matches) == 1:
            return matches[0]
        if matches:
            _log.warning(
                "session id %s names %d registered Claude processes; refusing to guess which "
                "one raised the dialog",
                session_id,
                len(matches),
            )
        return None

    async def verify(self) -> VerifyResult:
        """Report what is loaded, and whether any registered channel really answers."""
        loaded = f"{type(self).__module__}:{type(self).__name__}"
        if not self._channels:
            return VerifyResult(
                outcome=VerifyOutcome.PASS,
                loaded=loaded,
                detail="no Claude Session is registered, so there is no channel to reach",
            )

        answered: list[SessionTarget] = []
        refusals: list[str] = []
        for target, path in self._channels.items():
            try:
                connection = await self._dial(path)
            except ChannelError as unreachable:
                refusals.append(f"{target.session_id}: {unreachable}")
                continue
            answered.append(target)
            await connection.aclose()

        if not answered:
            return VerifyResult(
                outcome=VerifyOutcome.FAIL,
                loaded=loaded,
                detail="no registered Claude Session Channel answered: " + "; ".join(refusals),
            )
        return VerifyResult(
            outcome=VerifyOutcome.PASS,
            loaded=loaded,
            detail=f"{len(answered)} of {len(self._channels)} Claude Session Channel(s) answered",
        )

    # -- carrying words ---------------------------------------------------

    async def _deliver(
        self, target: SessionTarget, text: str, *, request_id: RequestId, verb: str
    ) -> DeliveryReceipt:
        """One attempt, classified into the hub's four states and nothing else."""
        socket_path = self._channels.get(target)
        if socket_path is None:
            return _failed(request_id, f"no Claude Session is registered as {target}")
        spent = len(text.encode("utf-8"))
        if spent > self._settings.max_text_bytes:
            return _failed(
                request_id,
                f"the words are {spent} bytes and both ends of the channel cap one Relay at "
                f"{self._settings.max_text_bytes}",
            )

        # Everything up to and including the dial happens before a byte of the
        # user's words is on the wire, so a failure here proves they never left.
        try:
            connection = await self._dial(socket_path)
        except ChannelError as unreachable:
            return _failed(request_id, f"the Session's channel is unreachable: {unreachable}")

        keep_open = False
        try:
            message = {
                REQUEST_ID_FIELD: str(request_id),
                KIND_FIELD: channel_kind_for(verb),
                TEXT_FIELD: text,
            }
            try:
                await connection.send(message)
            except ChannelError as broken:
                # Past the write: the line may or may not have been read.
                return _unknown(request_id, f"the channel write failed: {broken}")

            receipt = await self._await_acknowledgement(connection, request_id)
            keep_open = receipt.outcome is Delivery.UNKNOWN and receipt.request_id == request_id
            if keep_open:
                self._listen_late(target, connection, request_id)
            return receipt
        finally:
            if not keep_open:
                await connection.aclose()

    async def _await_acknowledgement(
        self, connection: ChannelConnection, request_id: RequestId
    ) -> DeliveryReceipt:
        """Wait, bounded, for the Session to say it has the words.

        `queued_for_claude` is not an answer, so it keeps waiting. A reply about
        a *different* request id contradicts the attempt, and a contradiction is
        UNKNOWN rather than a failure: these words may well have arrived.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._settings.ack_timeout_seconds
        while True:
            try:
                reply = await connection.read_message(
                    timeout_seconds=deadline - loop.time(),
                )
            except TimeoutError:
                return _unknown(
                    request_id,
                    "the Session did not acknowledge the words within "
                    f"{self._settings.ack_timeout_seconds:.0f}s",
                )
            except ChannelError as broken:
                return _unknown(request_id, f"the channel stopped answering: {broken}")

            outcome = _classify(reply, request_id)
            if outcome is not None:
                return outcome

    def _listen_late(
        self, target: SessionTarget, connection: ChannelConnection, request_id: RequestId
    ) -> None:
        """Keep listening on a spent attempt, so a late acknowledgement is still heard."""
        task = asyncio.ensure_future(self._late(target, connection, request_id))
        self._listening.add(task)
        task.add_done_callback(self._listening.discard)

    async def _late(
        self, target: SessionTarget, connection: ChannelConnection, request_id: RequestId
    ) -> None:
        """One spent attempt's second budget. Only DELIVERED is ever raised from here."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._settings.late_ack_timeout_seconds
        try:
            while True:
                try:
                    reply = await connection.read_message(
                        timeout_seconds=deadline - loop.time(),
                    )
                except (TimeoutError, ChannelError):
                    return
                outcome = _classify(reply, request_id)
                if outcome is None:
                    continue
                if outcome.outcome is Delivery.DELIVERED:
                    self._emit(RelayReceipt(target=target, receipt=outcome))
                    return
                # An upgrade is the only thing a late receipt may be. Anything
                # else would re-grade an attempt Bridge Core has already
                # recorded, from a connection nobody is waiting on.
                _log.info(
                    "a late channel reply for %s said %s; the recorded grade stands",
                    request_id,
                    outcome.outcome,
                )
                return
        except asyncio.CancelledError:
            raise
        finally:
            await connection.aclose()

    async def _dial(self, socket_path: Path) -> ChannelConnection:
        return await ChannelConnection.dial(
            socket_path,
            timeout_seconds=self._settings.request_timeout_seconds,
            max_message_bytes=self._settings.max_message_bytes,
        )

    def _emit(self, event: AgentEvent) -> None:
        if self._sink is not None:
            self._sink.emit(event)


def _classify(reply: dict[str, object], request_id: RequestId) -> DeliveryReceipt | None:
    """What one channel reply says about this attempt, or `None` for "keep waiting"."""
    kind = reply.get("type")
    answered = reply.get(REQUEST_ID_FIELD)

    if kind == CHANNEL_ERROR:
        # This channel only ever refuses a line *before* pushing it, so its
        # refusal is proof of non-delivery rather than the reference
        # implementation's ambiguous "it failed, possibly after arriving".
        return _failed(request_id, f"the channel refused the words: {reply.get('message')}")
    if kind == QUEUED:
        if answered != str(request_id):
            return _unknown(request_id, "the channel queued a different request")
        return None
    if kind == ACKNOWLEDGED:
        if answered != str(request_id):
            return _unknown(request_id, "the channel acknowledged a different request")
        return DeliveryReceipt(request_id=request_id, outcome=Delivery.DELIVERED)
    return _unknown(request_id, f"the channel said something unexpected: {kind!r}")


def _failed(request_id: RequestId, reason: str) -> DeliveryReceipt:
    return DeliveryReceipt(request_id=request_id, outcome=Delivery.FAILED, reason=reason)


def _unknown(request_id: RequestId, reason: str) -> DeliveryReceipt:
    return DeliveryReceipt(request_id=request_id, outcome=Delivery.UNKNOWN, reason=reason)

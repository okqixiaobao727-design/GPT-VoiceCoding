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

**One verb of three.** This package is the shared Claude adapter, and this issue
lays it down with the Answer Relay in it. The Notice Relay rides the peer socket
and the Approval Relay rides the `PermissionRequest` hook — different routes,
not different framings of this one — so both answer here with a classified
refusal naming what is missing, rather than pretending the channel can carry
them.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from pathlib import Path

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
from gpt_voicecoding.adapters.agent.claude.wire import (
    ChannelConnection,
    ChannelError,
)
from gpt_voicecoding.seams.agent import (
    ApprovalRequest,
    ApprovalVerdict,
    RelayReceipt,
    RelayRoute,
)
from gpt_voicecoding.seams.delivery import Delivery, DeliveryReceipt
from gpt_voicecoding.seams.events import EventSink
from gpt_voicecoding.seams.identity import AgentKind, RequestId, SessionTarget
from gpt_voicecoding.seams.verify import VerifyOutcome, VerifyResult

_log = logging.getLogger(__name__)

#: What a Relay that has no route here is told. Named by issue, because "not
#: implemented" without a way to find out when is a dead end for whoever reads
#: the log.
NOTICE_UNAVAILABLE = (
    "this build carries the Answer Relay only; the Notice Relay rides the Claude peer "
    "socket and arrives with issue #13"
)
APPROVAL_UNAVAILABLE = (
    "this build carries the Answer Relay only; the Approval Relay rides the "
    "PermissionRequest hook and arrives with issue #14"
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

    # -- lifecycle --------------------------------------------------------

    async def connect(self) -> None:
        """Nothing to open: every channel belongs to a Session, not to this engine."""

    async def aclose(self) -> None:
        """Stop listening for late receipts. The Sessions are left running.

        A channel is a process Claude Code owns. Closing this adapter lets go of
        connections to it and nothing more — there is nothing here to reap.

        **Each cancelled listener is waited for, not merely cancelled.** A
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

    def forget_session(self, target: SessionTarget) -> None:
        """Stop holding a route to one Session. The Session itself is untouched."""
        self._channels.pop(target, None)

    def reachable(self) -> tuple[SessionTarget, ...]:
        """Every Session this adapter holds a channel address for."""
        return tuple(self._channels)

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

    async def notice_relay(
        self, target: SessionTarget, text: str, *, request_id: RequestId
    ) -> DeliveryReceipt:
        """Not this route's to carry. Refused by name rather than mis-sent."""
        return _failed(request_id, NOTICE_UNAVAILABLE)

    async def approval_relay(
        self, request: ApprovalRequest, verdict: ApprovalVerdict, *, request_id: RequestId
    ) -> DeliveryReceipt:
        """Not this route's to carry. Refused by name rather than mis-sent."""
        return _failed(request_id, APPROVAL_UNAVAILABLE)

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

    def _emit(self, event: RelayReceipt) -> None:
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

"""The Notice Relay: one system-authored message in, graded by what can prove it.

**Readback is primary, and that is forced rather than chosen.** Receipts fire
only for messages the receiver *held*, so on the common `crossSessionInbound:
"accept"` configuration a perfectly successful Relay is completely silent. A
design that waited for a receipt would hang forever on the happy path. What is
left is the target's own transcript, and the sender-minted id echoed into it.

**Two record shapes prove delivery, and the second is load-bearing.** A message
that arrives between turns lands as a `user` record. One spliced into a *running*
turn writes **no `user` record at all** — it appears as a `queued_command`
attachment. A readback checking only the first shape would report UNKNOWN for
precisely the deliveries that matter most, and would do it silently.

**A contradiction is not a failure.** A record carrying our `uuid` but somebody
else's `origin.msg_id` says the transcript does not agree with itself about this
attempt. That is UNKNOWN — these words may well have arrived — and never
DELIVERED.

**The grading vocabulary is the seam's four states**, and every non-delivery
carries a positive reason. The six receipt statuses map onto them: `delivered` is
DELIVERED, `held` is HELD, and `denied` / `expired` / `refused` / `dropped` are
all FAILED — each one a reason worth surfacing rather than a silence to time out
on. `refused` in particular was previously assumed not to exist: the peer-socket
research observed only `held` and `expired` live and recorded that a refusing
config drops silently. Re-probing the pinned build showed the receiver's inbound
gate calling `sendPeerReceipt(…, "refused")`, so what was modelled as absence of
signal is a positive one, and grading it UNKNOWN would throw away evidence.

**The wait is bounded and a spent wait is UNKNOWN, never DELIVERED** — the same
shape the Answer Relay uses, for the same reason. The uuid-bearing record lands
only once the running turn ends, which may be minutes, so a single blocking wait
would hold the caller hostage to somebody else's turn. Instead the attempt
returns, and a background watcher keeps reading for a second, longer budget so a
late proof can still be raised upward. Only an upgrade to DELIVERED is ever
raised that way; a late anything-else would re-grade an attempt Bridge Core has
already recorded.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from enum import StrEnum
from typing import Any

from gpt_voicecoding.adapters.agent.claude.peer import (
    PeerError,
    PeerWriteError,
    Receipt,
    ReceiptFrame,
    ReceiptListener,
    notice_frame,
    send_frame,
)
from gpt_voicecoding.adapters.agent.claude.registry import (
    RegistryError,
    pid_is_live,
    read_record,
)
from gpt_voicecoding.adapters.agent.claude.settings import ClaudeSettings
from gpt_voicecoding.adapters.agent.claude.transcript import TranscriptError, TranscriptTail
from gpt_voicecoding.seams.agent import RelayReceipt
from gpt_voicecoding.seams.delivery import Delivery, DeliveryReceipt
from gpt_voicecoding.seams.identity import RequestId, SessionTarget

_log = logging.getLogger(__name__)

#: What each terminal receipt is reported as. `held` is absent on purpose: it is
#: not terminal, and the message may still be released or expire.
_FAILED_BY_RECEIPT = {
    Receipt.DENIED: "the Session's user denied the message",
    Receipt.EXPIRED: "the message was parked and then expired without being read",
    Receipt.REFUSED: "the Session's configuration refuses inbound peer messages",
    Receipt.DROPPED: "the Session dropped the message",
}


class Readback(StrEnum):
    """What one transcript record says about one attempt."""

    #: Both ids agree: these words are in that Session's transcript.
    DELIVERED = "delivered"
    #: Our id appears, but the record disagrees with itself about whose it is.
    CONTRADICTED = "contradicted"


def readback_in(record: dict[str, Any], request_id: str) -> Readback | None:
    """This record's verdict on this attempt, or `None` if it is about something else.

    Both shapes require the sender-minted id **twice** — once as the record's own
    identity and once inside `origin` — because either alone is weaker than it
    looks. `origin.msg_id` alone would match a record the receiver rewrote; the
    record id alone would match a uuid collision the receiver minted itself.
    """
    if record.get("type") == "user":
        return _agreement(record.get("uuid"), _origin_msg_id(record), request_id)

    attachment = record.get("attachment")
    if isinstance(attachment, dict) and attachment.get("type") == "queued_command":
        return _agreement(
            attachment.get("source_uuid"), _origin_msg_id(attachment), request_id
        )
    return None


def _agreement(own_id: Any, msg_id: Any, request_id: str) -> Readback | None:
    if own_id != request_id:
        return None
    return Readback.DELIVERED if msg_id == request_id else Readback.CONTRADICTED


def _origin_msg_id(document: dict[str, Any]) -> Any:
    origin = document.get("origin")
    return origin.get("msg_id") if isinstance(origin, dict) else None


class NoticeRelay:
    """One Relay over the peer socket, and everything that decides what it proved."""

    def __init__(
        self,
        *,
        settings: ClaudeSettings,
        listener: ReceiptListener,
        emit: Callable[[RelayReceipt], None],
    ) -> None:
        self._settings = settings
        self._listener = listener
        self._emit = emit
        #: Late watchers in flight, so none outlives the adapter that made it.
        self._watching: set[asyncio.Task[None]] = set()

    async def aclose(self) -> None:
        """Stop every late watcher, and wait for it — cancelling is only a request."""
        watching = list(self._watching)
        for task in watching:
            task.cancel()
        for task in watching:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._watching.clear()

    async def send(
        self, target: SessionTarget, text: str, *, request_id: RequestId
    ) -> DeliveryReceipt:
        """Carry system-authored words into one Session, and grade what happened."""
        if target.pid is None:  # pragma: no cover - SessionTarget refuses this already
            return _failed(request_id, "a Claude Session is addressed by pid, and this has none")

        # Everything down to the write is positive proof of non-delivery: not one
        # byte has left this process while any of it can still fail.
        try:
            record = read_record(self._settings.registry_directory, target.pid)
        except RegistryError as unknown:
            return _failed(request_id, f"the Session is not reachable: {unknown}")
        if record.session_id != target.session_id:
            return _failed(
                request_id,
                f"pid {target.pid} is now session {record.session_id}, not "
                f"{target.session_id}; refusing to carry words to whoever inherited the pid",
            )
        if not pid_is_live(record.pid):
            return _failed(request_id, f"the Session's process {record.pid} is gone")

        try:
            await self._listener.start()
        except PeerError as unbindable:
            return _failed(
                request_id,
                f"the receipt listener could not be bound, so a held or refused message "
                f"would be indistinguishable from silence: {unbindable}",
            )

        # Opened *before* the send, so a record written between the two is inside
        # the window rather than behind it.
        tail: TranscriptTail | None = None
        unprovable = ""
        try:
            tail = TranscriptTail.opened_at_end(
                self._settings.projects_directory, record.session_id
            )
        except TranscriptError as unreadable:
            unprovable = f"the Session's transcript could not be read back: {unreadable}"

        try:
            frame = notice_frame(
                text=text,
                request_id=str(request_id),
                session_id=record.session_id,
                reply_address=self._listener.address,
            )
        except PeerError as refused:
            return _failed(request_id, str(refused))

        with self._listener.watch(str(request_id)) as inbox:
            try:
                await send_frame(
                    record.socket_path,
                    frame,
                    timeout_seconds=self._settings.request_timeout_seconds,
                )
            except PeerWriteError as broken:
                # Past the write: the frame may or may not have been read.
                return _unknown(request_id, f"the peer write failed: {broken}")
            except PeerError as unreachable:
                return _failed(
                    request_id, f"the Session's peer socket is unreachable: {unreachable}"
                )

            if tail is None:
                return _unknown(request_id, unprovable)

            receipt = await self._settle(
                inbox, tail, request_id, seconds=self._settings.readback_timeout_seconds
            )
            if receipt.outcome is not Delivery.DELIVERED:
                # Registered here, inside the wait's own watch, so no receipt can
                # land in the gap between giving up and starting to listen again.
                self._watch_late(target, request_id, tail)
        return receipt

    async def _settle(
        self,
        inbox: asyncio.Queue[ReceiptFrame],
        tail: TranscriptTail,
        request_id: RequestId,
        *,
        seconds: float,
    ) -> DeliveryReceipt:
        """Watch both proofs at once until one answers or the budget is spent.

        **A hold is not an answer while the budget still runs.** `held` says the
        message is parked in front of the Session's user, and what happens next —
        released and delivered, denied, or expired — is the fact worth reporting.
        So a hold is remembered and the watch continues; HELD is what comes back
        only if the budget runs out while it is still parked, which is the honest
        report of a message that is sitting in front of somebody who has not
        looked at it. It is never delivery either way.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        held = False
        while True:
            graded = self._read_back(tail, request_id)
            if graded is not None:
                return graded
            graded, saw_hold = self._drain(inbox, request_id)
            held = held or saw_hold
            if graded is not None:
                return graded
            if loop.time() >= deadline:
                if held:
                    return DeliveryReceipt(
                        request_id=request_id,
                        outcome=Delivery.HELD,
                        reason="the Session parked the message in front of its user, and it was "
                        f"still parked {seconds:.0f}s later",
                    )
                return _unknown(
                    request_id,
                    "neither the Session's transcript nor a receipt showed the message within "
                    f"{seconds:.0f}s",
                )
            await asyncio.sleep(min(self._settings.readback_poll_seconds, deadline - loop.time()))

    def _read_back(self, tail: TranscriptTail, request_id: RequestId) -> DeliveryReceipt | None:
        """What the transcript has to say, if anything, about this attempt."""
        for record in tail.records():
            match readback_in(record, str(request_id)):
                case Readback.DELIVERED:
                    return DeliveryReceipt(request_id=request_id, outcome=Delivery.DELIVERED)
                case Readback.CONTRADICTED:
                    return _unknown(
                        request_id,
                        "the Session's transcript carries this id but attributes it to another "
                        "message; that contradicts the attempt rather than proving it",
                    )
                case None:
                    continue
        return None

    def _drain(
        self, inbox: asyncio.Queue[ReceiptFrame], request_id: RequestId
    ) -> tuple[DeliveryReceipt | None, bool]:
        """Every receipt waiting right now: a terminal grade if one came, and whether
        any of them was a hold."""
        held = False
        while not inbox.empty():
            receipt = inbox.get_nowait()
            if receipt.status is Receipt.DELIVERED:
                return DeliveryReceipt(request_id=request_id, outcome=Delivery.DELIVERED), held
            if receipt.status is Receipt.HELD:
                held = True
                continue
            reason = _FAILED_BY_RECEIPT.get(receipt.status)
            if reason is not None:
                detail = f" ({receipt.detail})" if receipt.detail else ""
                return _failed(request_id, reason + detail), held
        return None, held

    def _watch_late(
        self, target: SessionTarget, request_id: RequestId, tail: TranscriptTail
    ) -> None:
        """Keep reading a spent attempt, so a proof that arrives late is still heard.

        The inbox is taken here, synchronously, rather than inside the task: the
        task first runs some time after this returns, and a receipt arriving in
        between would find nobody registered for it.
        """
        inbox = self._listener.add_watcher(str(request_id))
        task = asyncio.ensure_future(self._late(target, request_id, tail, inbox))
        self._watching.add(task)
        task.add_done_callback(self._watching.discard)

    async def _late(
        self,
        target: SessionTarget,
        request_id: RequestId,
        tail: TranscriptTail,
        inbox: asyncio.Queue[ReceiptFrame],
    ) -> None:
        """One spent attempt's second budget. Only DELIVERED is ever raised from here."""
        try:
            receipt = await self._settle(
                inbox, tail, request_id, seconds=self._settings.late_readback_timeout_seconds
            )
        finally:
            self._listener.remove_watcher(str(request_id), inbox)
        if receipt.outcome is Delivery.DELIVERED:
            self._emit(RelayReceipt(target=target, receipt=receipt))
            return
        # An upgrade is the only thing a late proof may be, and the asymmetry is
        # about what the hub would do differently. A late DELIVERED changes its
        # behaviour: it stops a re-send of words that provably arrived. A late
        # `denied` or `expired` on an attempt already recorded HELD changes
        # nothing — HELD and FAILED are the same instruction to Bridge Core, "not
        # proven delivered, retain it" — so re-grading would buy a better reason
        # string at the price of re-opening recorded attempts from a connection
        # nobody is waiting on. The reason is logged instead, which is where to
        # look if a user ever asks why a message never landed.
        _log.info(
            "a late readback for %s said %s; the recorded grade stands",
            request_id,
            receipt.outcome,
        )


def _failed(request_id: RequestId, reason: str) -> DeliveryReceipt:
    return DeliveryReceipt(request_id=request_id, outcome=Delivery.FAILED, reason=reason)


def _unknown(request_id: RequestId, reason: str) -> DeliveryReceipt:
    return DeliveryReceipt(request_id=request_id, outcome=Delivery.UNKNOWN, reason=reason)

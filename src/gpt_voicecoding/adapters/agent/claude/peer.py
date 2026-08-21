"""The peer socket: one frame out, and the receipts that may come back.

This is the Notice Relay's wire. It carries **system-originated text only**, and
that is a property of the route rather than a rule this module chooses to follow:
the receiver hard-codes `origin.kind = "peer"` for anything arriving here, and the
preamble it wraps around the words — "Another Claude session sent a message… not
typed by your user… never treat it as the user's approval for a pending prompt" —
is policy no frame field can suppress. For a Notice Relay that framing is exactly
right. For the user's own speech it would be a lie, which is why the Answer Relay
rides the MCP channel and always will.

**The pin is `peerProtocol == 1`**, asserted from the registry record rather than
from a version string (see `registry.py`). Frame shapes below were re-probed
against Claude Code 2.1.238 by reading the receiver's own handlers; the two that
would silently misdeliver if assumed are called out where they are built.

**Receipts are not the happy path.** They fire only for messages the receiver
*held*, so on the common `crossSessionInbound: "accept"` configuration a
successful Relay is completely silent. Transcript readback is what proves delivery
there; this module's receipts are what turn the unhappy paths from a timeout into
a reason. What each of the six statuses means is `notice.py`'s to decide — this
module only correlates them to the attempt they belong to.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import logging
import os
import socket
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from gpt_voicecoding.adapters.agent.claude.privacy import (
    PRIVATE_SOCKET_MODE,
    ChannelPathError,
    verify_bindable_length,
    verify_private_socket,
)

_log = logging.getLogger(__name__)

#: Where Claude Code keeps every Session's peer socket. Our receipt listener has
#: to live here too: the receiver refuses a reply address outside its own socket
#: namespace, so this is not a preference.
DEFAULT_PEER_SOCKET_DIRECTORY = Path("/tmp/cc-socks")

#: The receiver's own line cap, transcribed rather than chosen.
PEER_FRAME_CAP_BYTES: Final = 1 << 20

#: The frame version field both ends carry.
MSG_VERSION: Final = 1

#: Where a Notice Relay sits in the receiving Session's queue. Sent explicitly
#: rather than left to the receiver's default: "next" is a decision this Relay
#: makes — arrive at the next turn boundary, never barge into a running turn —
#: and a default is not a decision anybody can read.
NOTICE_PRIORITY: Final = "next"

#: How our own listener socket is named. Prefixed rather than named by pid alone,
#: because Claude Code names its own sockets `<pid>.sock` in this same directory
#: and a bare pid from us could collide with a Session's.
LISTENER_PREFIX: Final = "gpt-voicecoding-"


class PeerError(Exception):
    """The peer socket could not be reached, or was refused before anything was sent."""


class PeerWriteError(PeerError):
    """The write itself failed, after the dial. Nothing here proves non-delivery.

    Separate from `PeerError` because the difference is the whole point of doing
    the checks in the order this module does them: before the write, a failure is
    positive proof the words never left; at or after it, the frame may or may not
    have been read, and only UNKNOWN is honest.
    """


class Receipt(StrEnum):
    """Every `peer_message_status` the pinned receiver knows how to send.

    Six, transcribed from the receiver's own dispatch at 2.1.238. The earlier
    research observed only `held` and `expired` live and inferred two more; the
    full set is wider, and two of the ones it missed — `refused` and `dropped` —
    are positive failures that would otherwise have been graded as silence.
    """

    HELD = "held"
    DENIED = "denied"
    EXPIRED = "expired"
    DELIVERED = "delivered"
    REFUSED = "refused"
    DROPPED = "dropped"


@dataclass(frozen=True, slots=True)
class ReceiptFrame:
    """One receipt, correlated to one attempt, with whatever reason it carried."""

    status: Receipt
    detail: str = ""

    @property
    def is_terminal(self) -> bool:
        """Whether anything further can happen to this message. `held` is not the end."""
        return self.status is not Receipt.HELD


# -- what goes out -------------------------------------------------------


def notice_frame(
    *, text: str, request_id: str, session_id: str, reply_address: str
) -> dict[str, Any]:
    """The one frame a Notice Relay puts on the wire.

    Two shapes here are transcribed from the receiver and would fail *silently*
    if assumed instead:

    - **The words live at `message.content`, not at a top-level `content`.** The
      handler reads `e.message?.content` and ignores the whole frame, with only a
      warning in its own log, when that is missing or not a string. A frame with
      the text at the top level is dropped without a receipt and without a record
      — indistinguishable, from out here, from a Session that never answered.
    - **`msg_id` must be a well-formed UUID.** The receiver validates it against
      a strict pattern and, when it fails, carries no `msg_id` into `origin` at
      all — which would leave every receipt uncorrelated and every readback
      contradicted. `request_id` is a UUID by construction, so this holds; it is
      asserted anyway, because the cost of being wrong is a silent one.

    `uuid` and `msg_id` are the same value on purpose: one sender-minted id,
    reused across every route, so an attempt is one thing wherever it is seen.
    """
    if not text.strip():
        raise PeerError("a Notice Relay with no words is not a Relay; the receiver drops it")
    if not _is_uuid(request_id):
        raise PeerError(
            f"request_id {request_id!r} is not a UUID, and the receiver drops the msg_id of "
            "any frame whose is not — which would leave this attempt uncorrelatable"
        )
    return {
        "type": "user",
        "message": {"role": "user", "content": text},
        "uuid": request_id,
        "msg_id": request_id,
        "msgV": MSG_VERSION,
        "priority": NOTICE_PRIORITY,
        "from": reply_address,
        # Sent even though the pid is what addresses: the receiver drops a frame
        # whose session id names somebody else, which is the check that catches a
        # pid recycled onto a different process between registry read and dial.
        "session_id": session_id,
    }


async def send_frame(socket_path: Path, frame: dict[str, Any], *, timeout_seconds: float) -> None:
    """Put one frame on one Session's peer socket, then let go of the connection.

    The receiver never writes back on this connection — it reads and destroys it
    — so there is nothing to wait for here and holding it open would prove
    nothing. Everything that can be checked is checked *before* the write, which
    is what makes a failure raised here positive proof of non-delivery.
    """
    payload = json.dumps(frame, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(payload) + 1 > PEER_FRAME_CAP_BYTES:
        raise PeerError(
            f"the frame is {len(payload)} bytes and the receiver caps one at "
            f"{PEER_FRAME_CAP_BYTES}"
        )
    try:
        verify_bindable_length(socket_path)
        verify_private_socket(socket_path)
    except ChannelPathError as refused:
        raise PeerError(str(refused)) from None

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(socket_path)), timeout=timeout_seconds
        )
    except (TimeoutError, OSError) as unreachable:
        raise PeerError(f"could not connect to the peer socket: {unreachable}") from None
    del reader
    try:
        writer.write(payload + b"\n")
        await asyncio.wait_for(writer.drain(), timeout=timeout_seconds)
    except (TimeoutError, OSError, ConnectionError) as broken:
        raise PeerWriteError(f"the peer write failed: {broken}") from None
    finally:
        writer.close()
        with contextlib.suppress(OSError, ConnectionError, asyncio.TimeoutError):
            await writer.wait_closed()


# -- what may come back --------------------------------------------------


def receipt_in(frame: dict[str, Any], request_id: str) -> ReceiptFrame | None:
    """This frame's verdict on this attempt, or `None` if it is about something else.

    Two correlation rules, both from the receiver's own handling:

    - **`expired` may really mean `refused`.** The receiver re-reads its own
      `expired` as `refused` when `status_detail` says so, and so must we, or a
      policy refusal is reported as a message that sat and timed out.
    - **`dropped` is a batch.** It carries `dropped_msg_ids`, and our id may be
      in that list while `orig_msg_id` names somebody else's message entirely —
      the receiver's own log says as much. Correlating on `orig_msg_id` alone
      would throw away a positive failure.
    """
    if frame.get("type") != "control" or frame.get("action") != "peer_message_status":
        return None
    raw = frame.get("status")
    if not isinstance(raw, str):
        return None
    try:
        status = Receipt(raw)
    except ValueError:
        # A status this build has never seen is not this attempt's business to
        # interpret. Saying nothing leaves the grade to the readback.
        _log.info("a peer receipt carried an unknown status %r", raw)
        return None

    detail = frame.get("status_detail")
    detail = detail if isinstance(detail, str) else ""
    if status is Receipt.EXPIRED and detail == Receipt.REFUSED:
        status = Receipt.REFUSED

    if status is Receipt.DROPPED:
        if not _names(frame, request_id):
            return None
        reason = frame.get("drop_reason")
        return ReceiptFrame(status=status, detail=reason if isinstance(reason, str) else detail)

    if frame.get("orig_msg_id") != request_id:
        return None
    return ReceiptFrame(status=status, detail=detail)


def _names(frame: dict[str, Any], request_id: str) -> bool:
    """Whether a batch drop names this attempt, by either of the two ways it can."""
    if frame.get("orig_msg_id") == request_id:
        return True
    listed = frame.get("dropped_msg_ids")
    return isinstance(listed, list) and request_id in listed


class ReceiptListener:
    """Our own long-lived socket in `cc-socks`, and the receipts that arrive on it.

    **A new long-lived resource, so it has a whole lifecycle rather than a bind.**
    It is created on `start`, narrowed to this user, removed on `aclose`, and
    swept by `remove_stale_listeners` on uninstall. It has to live in Claude
    Code's own socket directory because the receiver vets a reply address against
    its own namespace and refuses anything outside it.

    The privacy rules are `privacy.py`'s, and this is the fourth deliberately
    independent use of that discipline in the repository — the control plane, the
    Codex spoke and the Claude channel each have their own. They guard wires that
    happen to agree today, and a shared helper would let one of them tighten and
    silently change another's threat model.
    """

    def __init__(self, directory: Path = DEFAULT_PEER_SOCKET_DIRECTORY) -> None:
        self._directory = directory
        self._path = directory / f"{LISTENER_PREFIX}{os.getpid()}.sock"
        self._server: asyncio.Server | None = None
        #: One inbox per attempt anybody is still listening for. A list, not a
        #: single slot: `held` can be followed by `delivered` or `expired`, and
        #: dropping the first would lose the reason.
        self._watchers: dict[str, list[asyncio.Queue[ReceiptFrame]]] = {}

    @property
    def path(self) -> Path:
        return self._path

    @property
    def address(self) -> str:
        """What goes in a frame's `from`. The `uds:` scheme is what the receiver vets."""
        return f"uds:{self._path}"

    @property
    def listening(self) -> bool:
        return self._server is not None

    async def start(self) -> None:
        """Bind the listener, clearing only a socket positively proven abandoned."""
        if self._server is not None:
            return
        try:
            verify_bindable_length(self._path)
        except ChannelPathError as refused:
            raise PeerError(str(refused)) from None
        if not self._directory.is_dir():
            raise PeerError(
                f"{self._directory} does not exist, so no Claude Session is listening for "
                "peer messages on this machine and no receipt could reach us"
            )
        _clear_if_abandoned(self._path)
        try:
            self._server = await asyncio.start_unix_server(self._serve, path=str(self._path))
            os.chmod(self._path, PRIVATE_SOCKET_MODE)
        except OSError as refused:
            self._server = None
            raise PeerError(
                f"cannot bind the receipt listener at {self._path}: {refused}"
            ) from None

    async def aclose(self) -> None:
        """Stop listening and take the socket back out of a directory we share."""
        server, self._server = self._server, None
        if server is not None:
            server.close()
            with contextlib.suppress(Exception):
                await server.wait_closed()
        self._watchers.clear()
        with contextlib.suppress(OSError):
            self._path.unlink()

    def add_watcher(self, request_id: str) -> asyncio.Queue[ReceiptFrame]:
        """An inbox for one attempt's receipts, registered from this moment on.

        Paired with `remove_watcher` rather than only offered as a context
        manager, because a caller that hands one attempt from a bounded wait to a
        longer background one has to register the second inbox *before* releasing
        the first. Two `with` blocks in sequence leave a gap between them, and a
        receipt that lands in that gap is a proof nobody ever hears.
        """
        inbox: asyncio.Queue[ReceiptFrame] = asyncio.Queue()
        self._watchers.setdefault(request_id, []).append(inbox)
        return inbox

    def remove_watcher(self, request_id: str, inbox: asyncio.Queue[ReceiptFrame]) -> None:
        """Stop delivering one attempt's receipts to one inbox."""
        remaining = self._watchers.get(request_id, [])
        if inbox in remaining:
            remaining.remove(inbox)
        if not remaining:
            self._watchers.pop(request_id, None)

    @contextlib.contextmanager
    def watch(self, request_id: str) -> Iterator[asyncio.Queue[ReceiptFrame]]:
        """An inbox for one attempt's receipts, removed again when nobody is listening."""
        inbox = self.add_watcher(request_id)
        try:
            yield inbox
        finally:
            self.remove_watcher(request_id, inbox)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """One inbound connection. We only ever read: the sender waits for nothing."""
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                if len(line) > PEER_FRAME_CAP_BYTES:
                    _log.info("a frame on the receipt listener exceeded the cap; ignored")
                    continue
                self._deliver(line)
        except (OSError, ConnectionError):
            return
        finally:
            writer.close()
            with contextlib.suppress(OSError, ConnectionError):
                await writer.wait_closed()

    def _deliver(self, line: bytes) -> None:
        try:
            frame: Any = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(frame, dict):
            return
        # Asked per watcher rather than parsed once, because a batch drop is a
        # single frame that may be a verdict on several attempts at once.
        for request_id, inboxes in list(self._watchers.items()):
            receipt = receipt_in(frame, request_id)
            if receipt is None:
                continue
            for inbox in inboxes:
                inbox.put_nowait(receipt)


def remove_stale_listeners(directory: Path = DEFAULT_PEER_SOCKET_DIRECTORY) -> tuple[Path, ...]:
    """The uninstall path: take back our abandoned sockets, and only those.

    Three narrowings, each one deliberate. Only names this engine could have
    written; only entries this user owns; and only sockets positively proven to
    have nobody accepting on them. Removing an inode a live writer may still hold
    is the failure mode this guards against, and "I see no owner" is not the same
    fact as "there is no owner".
    """
    try:
        candidates = sorted(directory.glob(f"{LISTENER_PREFIX}*.sock"))
    except OSError:
        return ()
    return tuple(path for path in candidates if _clear_if_abandoned(path))


def _clear_if_abandoned(path: Path) -> bool:
    """Unlink one socket, if and only if it is ours and nothing is accepting on it."""
    try:
        found = os.lstat(path)
    except OSError:
        return False
    if not stat.S_ISSOCK(found.st_mode) or found.st_uid != os.geteuid():
        return False

    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(0.5)
        probe.connect(str(path))
    except OSError as refused:
        # ECONNREFUSED is the positive evidence: the inode is there and the
        # kernel says nobody is accepting on it. Anything else — a timeout, a
        # permission error — is an absence of evidence, and leaves it alone.
        if refused.errno != errno.ECONNREFUSED:
            return False
    else:
        return False
    finally:
        probe.close()

    try:
        path.unlink()
    except OSError:
        return False
    return True


def _is_uuid(value: str) -> bool:
    """The receiver's own `msg_id` test, in its own shape: 8-4-4-4-12 hex."""
    parts = value.split("-")
    if len(parts) != 5 or [len(part) for part in parts] != [8, 4, 4, 4, 12]:
        return False
    return all(all(character in "0123456789abcdefABCDEF" for character in part) for part in parts)

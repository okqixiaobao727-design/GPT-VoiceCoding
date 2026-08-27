"""A Session's own inbox socket, and the only two things that prove a Relay arrived.

**This replaces the channel server, and the receipt is why.** ADR 0006 built a
process of our own inside every Session so that the Session could call a tool
back and say it had the words. It bought a real acknowledgement and cost the one
thing #67 exists for: a channel is loaded at launch, so it can only reach Sessions
this product started. Claude Code has since grown an inbox socket of its own,
bound by default, that any Session already has — including the ones the user
started by hand before this engine was running. #71 chose it knowingly, on private
surface, and this module is that choice.

**What earns `DELIVERED` here is #71's ruling, and it is narrower than it looks.**
A socket write that is accepted proves *nothing*: the line was taken by a socket,
not read by a Session. There are exactly two honest sources, and this module has
both:

1. **The `held → delivered` receipt.** A receiver that holds inbound peer messages
   for its user answers the sender with `peer_message_status`, correlated by
   `orig_msg_id`. To be answered at all the sender has to be a peer the receiver
   can resolve, which means binding a reply socket inside the receiver's own
   socket namespace and publishing a key for it — `ReplyInbox` below.
2. **The target's own transcript.** The record Claude Code writes when it injects
   a peer message carries `origin.from` — our reply address, exactly as we spelled
   it — and `origin.msg_id`, the id we minted. An exact correlator, verified on
   every #71 probe and again on the combined proof.

Everything weaker is `UNKNOWN`, and P9 never re-sends an `UNKNOWN` on this
system's own authority. That is not caution for its own sake: on a machine with
`crossSessionInbound: "accept"` — Simon's, today — a message is never held and
therefore never receipted, so most `UNKNOWN`s here are Relays that did arrive.

**An immediately-accepted message yields no receipt at all**, and #71 recorded
why so it is not re-tried: the receiver logs `[peer-cred] peer pid unavailable`
for an external process, and the documented notion of verification is *own-child*
— a process posting to its parent's socket. Presenting the receiver's token,
presenting our own, and frame correctness each changed nothing. So on that path
the transcript is the whole of the evidence.

**The socket path is read, never built.** 2.1.245 derives the socket directory
from `CLAUDE_CODE_TMPDIR` or `$XDG_RUNTIME_DIR` and accepts
`--messaging-socket-path`, so a constructed path is a guess. It arrives on the
`SessionStart` registration and is held per exact target by the adapter.

**No version pin, by Simon's decision (#71).** Claude Code auto-updates; the
safeguard is not a pin but the rule the evidence forces — never infer delivery
from a successful write — so an upstream change surfaces as a missing receipt
rather than as words the user believes were delivered.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import secrets
import subprocess
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from gpt_voicecoding.adapters.agent.claude.privacy import (
    PRIVATE_SOCKET_MODE,
    ChannelPathError,
    verify_bindable_length,
    verify_private_directory,
    verify_private_socket,
)
from gpt_voicecoding.private_socket import start_private_unix_server

_log = logging.getLogger(__name__)

# -- the frames, as 2.1.245 logs them to itself --------------------------

#: Not reverse-engineered: 2.1.245 prints the pair at `[uds-messaging] Inject
#: messages`. The auth line first, then one user message per line.
AUTH_TYPE: Final = "auth"
USER_TYPE: Final = "user"

#: The protocol version the receiver stamps on its own frames.
MESSAGE_VERSION: Final = 1

#: How a reply address is spelled. The receiver resolves it back to a pid through
#: the key file published for that exact path, which is where `verifiedPeerPid`
#: comes from — so the address we send and the path we bind must be the same
#: string, byte for byte.
ADDRESS_PREFIX: Final = "uds:"

#: The control frame a receiver answers a resolvable peer with.
STATUS_ACTION: Final = "peer_message_status"

#: Upstream's own word for "it reached the Session". The only status that earns
#: `DELIVERED`, and it only ever follows a `held`.
DELIVERED_STATUS: Final = "delivered"

#: Parked in front of a person, and not yet anything else. It settles later —
#: to `delivered` when they release it, to `denied` when they refuse, and to
#: `expired` when they do neither, because a held message expires after about
#: five minutes and the hold queue caps at 100, dropping the oldest past that.
HELD_STATUS: Final = "held"

#: Every status that proves the words did **not** reach the Session. Proven
#: non-delivery is the one grade P9 allows another attempt for, so the difference
#: between these and a spent wait is the difference between a Relay that goes
#: again and one that waits for the user to say the words themselves.
REFUSED_STATUSES: Final = ("denied", "refused", "expired", "dropped")

#: How the sender's key file is named: `<pid>.<sha256(socket path)>.key`, beside
#: the Session records in the same registry directory. Verified against a live
#: Session's own key file on 2.1.245.
KEY_SUFFIX: Final = ".key"

#: Our reply socket's name. Prefixed rather than named after the pid alone, so it
#: cannot be mistaken for — or collide with — a Session's own socket in the
#: directory we are binding into, which is not ours.
REPLY_SOCKET_PREFIX: Final = "vc-relay-"


class InboxError(Exception):
    """The inbox could not be reached, or our own reply socket could not be bound."""


def user_frame(text: str, *, msg_id: str, reply_to: str) -> dict[str, Any]:
    """One Relay on the wire.

    `msg_id` is validated by the receiver against a UUID pattern, and an id of
    another shape is dropped from the `origin` record it writes — which would
    silently remove the transcript correlator, the one source of `DELIVERED` that
    works on an accepting receiver. So it is minted as a UUID and never derived
    from the hub's `RequestId`, whose shape is Bridge Core's business.

    No `priority` and no `from_mode`. The first would let this engine push in
    front of what a person queued; the second is an attestation an external
    process cannot make — #71 proved a `from_mode` line is not attestation, and
    the message was held identically until Simon released it by hand.
    """
    return {
        "type": USER_TYPE,
        "msgV": MESSAGE_VERSION,
        "message": {"role": "user", "content": text},
        "from": reply_to,
        "msg_id": msg_id,
    }


def auth_frame(token: str) -> dict[str, Any]:
    """The own-child line, carrying the *receiver's* own messaging token.

    Its one documented meaning is a process posting to its parent's socket, and
    the token that says so is `CLAUDE_CODE_MESSAGING_TOKEN` — which is the
    Session's own, reported by its `SessionStart` hook and held per exact target.
    It is the documented way past a `bypassPermissions` receiver's hold for a
    sender that asserts no permission class.

    It is not what earns a receipt. #71 tried this token and our own, and neither
    changed anything on the accepted path; the receipt hangs on the reply key
    below. Sent because a Relay that is held when it need not be is a Relay the
    user has to release by hand.
    """
    return {"type": AUTH_TYPE, "token": token}


def new_message_id() -> str:
    return str(uuid.uuid4())


def correlated(records: tuple[Mapping[str, Any], ...] | None, *, msg_id: str, address: str) -> bool:
    """Whether the target's own transcript records this exact Relay arriving.

    Both halves of the correlator are checked, though the id alone would do: it
    is a UUID this process minted, so a match is already conclusive. Checking the
    address too is what makes the claim readable as the thing #71 proved — the
    record names *our* socket — rather than as a coincidence nobody can audit.
    """
    if not records:
        return False
    for record in records:
        origin = record.get("origin")
        if not isinstance(origin, Mapping):
            continue
        if origin.get("msg_id") == msg_id and origin.get("from") == address:
            return True
    return False


def own_process_start() -> str:
    """This process's start time, in the shape Claude Code publishes it.

    The receiver checks a reply address's key file against the real start time of
    the pid that published it, because a pid outlives nothing but a pid. A key
    whose `procStart` does not match is a key it will not trust, and nothing says
    so: the message goes and no receipt ever comes back. So every character here
    is load-bearing, and every one of them was **measured on 2026-08-26 against
    the eleven live Sessions' own key files on this machine**, not assumed.

    Three things, and the first corrects the record:

    * **The time is UTC.** #71 recorded a "12-hour clock with no AM/PM" from a
      Session started at 21:57 appearing as `09:57:51`. It is not a 12-hour
      clock — this machine is UTC+12, and what it saw was the offset. The two
      readings are indistinguishable for a local afternoon and differ by a whole
      *day* for a local morning: a Session started at 11:15 on the 26th publishes
      `Tue Aug 25 23:15:21`, which no 12-hour clock produces. Formatting a local
      morning with `%I` would therefore have been wrong on the hour and the date
      at once, and silently.
    * **The order is the C locale's.** `ps` prints `Tue 25 Aug` under this
      machine's locale and `Tue Aug 25` under C.
    * **The shape is `asctime`'s**, which is what all eleven samples are, so the
      day is space-padded in a three-wide field (`%e`) rather than zero-padded.
      Identical on every sample, because every one fell on the 25th or 26th; a
      single-digit day is the one character here that inference rather than
      measurement chose, and it is inferred from `asctime` rather than guessed.

    Verified by reconstructing all eleven published keys from `ps` and comparing:
    eleven matches, no mismatches.
    """
    return published_start(
        subprocess.run(
            ["ps", "-p", str(os.getpid()), "-o", "lstart="],
            capture_output=True,
            text=True,
            env={**os.environ, "LC_ALL": "C"},
            check=False,
        ).stdout.strip()
    )


def published_start(printed: str) -> str:
    """One `ps -o lstart=` line, as the key file spells the same moment.

    Split out from the `ps` call because the interesting half is pure and its
    interesting cases are ones this process cannot be made to have: a start time
    whose UTC form falls on the *previous day*, and a single-digit day. Tested
    against a fixed zone rather than whatever the machine is set to, so a suite
    run at a different hour cannot pass a wrong conversion.
    """
    # Naive, and read as local because that is what `ps` prints. `astimezone`
    # applies the offset in force *at that moment* rather than today's, so a
    # process started on the other side of a daylight-saving change converts
    # correctly.
    started = datetime.strptime(printed, "%a %b %d %H:%M:%S %Y").astimezone()
    return started.astimezone(UTC).strftime("%a %b %e %H:%M:%S %Y")


class ReplyInbox:
    """Our own socket beside a Session's, so this engine is a peer it can answer.

    One per socket directory rather than one per Session: the receipt namespace
    is the directory, and every Session sharing it can reach one address. A
    second socket would be a second key to publish and a second thing to leave
    behind on a crash, for no fact this one cannot carry.

    Publishing a key is what makes an external process a first-class peer, and it
    is deliberately *only* a key: no `<pid>.json`, so nothing this engine does
    puts a phantom row in anybody's Session roster.
    """

    def __init__(
        self, *, directory: Path, registry_directory: Path, pid: int | None = None
    ) -> None:
        self._pid = pid or os.getpid()
        self.path = directory / f"{REPLY_SOCKET_PREFIX}{self._pid}.sock"
        self.address = f"{ADDRESS_PREFIX}{self.path}"
        self._registry_directory = registry_directory
        self._key_path = registry_directory / (
            f"{self._pid}.{hashlib.sha256(str(self.path).encode()).hexdigest()}{KEY_SUFFIX}"
        )
        self._token = secrets.token_hex(16)
        self._server: asyncio.Server | None = None
        #: Every status frame that has arrived, in order. Bounded by the Relays
        #: this engine sends, and read by `statuses` rather than consumed, so two
        #: waiters for one message cannot take each other's answer.
        self._frames: list[dict[str, Any]] = []

    async def start(self) -> None:
        """Bind the socket and publish the key. Raises `InboxError` if either fails.

        A failure is raised rather than swallowed because a Relay written with no
        reply address is a Relay no receipt can ever settle: on a receiver that
        holds, it would be parked and never heard of again.
        """
        if self._server is not None:
            return
        try:
            verify_bindable_length(self.path)
            # The directory is Claude Code's, not ours, so it is checked and
            # never narrowed. A reply socket in a directory anyone could enter
            # is a socket anyone could feed a forged `delivered` into, and this
            # engine would report words as arrived on a stranger's say-so.
            verify_private_directory(self.path.parent)
        except ChannelPathError as refused:
            raise InboxError(str(refused)) from None
        # Our own name in a directory that is not ours, so anything at that exact
        # path is this engine's leftover and nobody else's live socket.
        with contextlib.suppress(OSError):
            self.path.unlink()
        try:
            self._server = await start_private_unix_server(
                self._serve, self.path, mode=PRIVATE_SOCKET_MODE
            )
            self._publish_key()
        except OSError as refused:
            await self.aclose()
            raise InboxError(f"no reply inbox could be bound at {self.path}: {refused}") from None

    def _publish_key(self) -> None:
        self._key_path.parent.mkdir(parents=True, exist_ok=True)
        self._key_path.write_text(
            json.dumps({"peerToken": self._token, "procStart": own_process_start()}),
            encoding="utf-8",
        )
        self._key_path.chmod(PRIVATE_SOCKET_MODE)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """One receiver's status connection: read its lines, keep what parses."""
        try:
            while line := await reader.readline():
                try:
                    frame = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(frame, dict):
                    self._frames.append(frame)
        except (OSError, ConnectionError, ValueError) as broken:
            _log.info("a reply-inbox connection failed: %s", broken)
        finally:
            writer.close()
            with contextlib.suppress(OSError, ConnectionError):
                await writer.wait_closed()

    def statuses(self, msg_id: str) -> tuple[dict[str, Any], ...]:
        """Every `peer_message_status` seen for one Relay, in arrival order."""
        return tuple(
            frame
            for frame in self._frames
            if frame.get("action") == STATUS_ACTION and frame.get("orig_msg_id") == msg_id
        )

    async def aclose(self) -> None:
        """Close the socket and take the key back out. Both, whatever failed."""
        server, self._server = self._server, None
        if server is not None:
            server.close()
            with contextlib.suppress(Exception):
                await server.wait_closed()
        for path in (self.path, self._key_path):
            with contextlib.suppress(OSError):
                path.unlink()


async def send(socket_path: Path, frames: tuple[dict[str, Any], ...], *, timeout: float) -> None:
    """Open one connection and put every frame on it, whole and at once.

    Whole and at once because 2.1.245 closes a connection that has not sent a
    complete line inside its first-line deadline, and its 2.1.243 changelog
    advises connecting only once the data is ready.

    The dial and the write raise separately in the caller's hands on purpose:
    everything up to the connection happens before a byte of the user's words is
    on the wire, so a failure there proves they never left — while a failure
    after it says only that this end stopped, never that the far end did not
    read.
    """
    reader, writer = await _connect(socket_path, timeout=timeout)
    del reader
    try:
        for frame in frames:
            writer.write((json.dumps(frame) + "\n").encode("utf-8"))
        await writer.drain()
    finally:
        writer.close()
        with contextlib.suppress(OSError, ConnectionError):
            await writer.wait_closed()


async def dial(socket_path: Path, *, timeout: float) -> None:
    """Prove a Session's inbox is there and let go of it again."""
    _, writer = await _connect(socket_path, timeout=timeout)
    writer.close()
    with contextlib.suppress(OSError, ConnectionError):
        await writer.wait_closed()


async def _connect(
    socket_path: Path, *, timeout: float
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Reach one Session's inbox, refusing a path that is not privately its own.

    The check is kept from the route this replaced, and the real thing satisfies
    it: Claude Code binds `srw-------` inside a `drwx------` directory. It is not
    advice — the user's own words travel over this socket, so a path another
    account could swap out from under us, or reach through a symlink, is not one
    to carry them over.
    """
    try:
        verify_bindable_length(socket_path)
        verify_private_socket(socket_path)
    except ChannelPathError as refused:
        raise InboxError(str(refused)) from None
    try:
        return await asyncio.wait_for(
            asyncio.open_unix_connection(str(socket_path)), timeout=timeout
        )
    except TimeoutError:
        raise InboxError(f"{socket_path} did not answer within {timeout:.0f}s") from None
    except (OSError, ConnectionError) as unreachable:
        raise InboxError(f"{socket_path} could not be reached: {unreachable}") from None

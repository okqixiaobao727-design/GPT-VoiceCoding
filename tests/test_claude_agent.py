"""The Claude Agent adapter, against a scripted Session inbox.

The edge cases here are the ones that matter on this route, because each is a way
a Relay can look successful and not be — and on the inbox socket the commonest of
them is the ordinary case: **an accepted write proves nothing**, so a Relay to a
receiver that neither holds nor records is honestly UNKNOWN. The others are a
message parked in front of a person, one the person refused, and one whose
arrival only the target's own transcript can attest.

No real Claude Code runs. The frames are the ones 2.1.245 logs to itself and #71
proved live; `claude_inbox_fake.py` is the far end and says what it does and does
not reproduce.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from claude_inbox_fake import FakeInbox
from fakes import PROGRESS_CAPTURE
from gpt_voicecoding.adapters.agent.claude import (
    PROVEN_AGAINST_VERSION,
    ClaudeAgentAdapter,
    claude_agent,
    inbox,
)
from gpt_voicecoding.adapters.agent.claude.adapter import APPROVAL_UNROUTED, SessionReport
from gpt_voicecoding.adapters.agent.claude.registry import PEER_PROTOCOL
from gpt_voicecoding.adapters.agent.claude.settings import ClaudeSettings, SettingsError
from gpt_voicecoding.core.lifecycle import Lifecycle
from gpt_voicecoding.core.relay_queue import RelayQueue
from gpt_voicecoding.core.relays import RelayPipeline
from gpt_voicecoding.core.sessions import Session, SessionRegistry
from gpt_voicecoding.seams.agent import (
    ApprovalRequest,
    ApprovalVerdict,
    RelayReceipt,
    RelayRoute,
    ReplyWindow,
    SessionState,
)
from gpt_voicecoding.seams.delivery import Delivery
from gpt_voicecoding.seams.identity import AgentKind, RequestId, SessionName, SessionTarget
from gpt_voicecoding.seams.verify import VerifyOutcome

SESSION = "0b7cf6f2-0f3c-4f5e-9d1f-8a2b3c4d5e6f"
TARGET = SessionTarget(agent=AgentKind.CLAUDE, session_id=SESSION, pid=4321)

#: A target whose pid really is alive, which the Reply Window watcher insists on
#: before it will read a record as belonging to this Session. `TARGET`'s pid is a
#: literal and cannot serve, so the level tests build their own.
LIVE_TARGET = SessionTarget(agent=AgentKind.CLAUDE, session_id=SESSION, pid=os.getpid())

_names = itertools.count()


def write_record(registry_directory: Path, *, status: str) -> None:
    """The registry record Claude Code publishes, and the watcher reads the level from."""
    registry_directory.mkdir(parents=True, exist_ok=True)
    (registry_directory / f"{LIVE_TARGET.pid}.json").write_text(
        json.dumps(
            {
                "pid": LIVE_TARGET.pid,
                "sessionId": SESSION,
                "cwd": str(registry_directory),
                "version": PROVEN_AGAINST_VERSION,
                "peerProtocol": PEER_PROTOCOL,
                "messagingSocketPath": str(registry_directory / "claude.sock"),
                "status": status,
            }
        ),
        encoding="utf-8",
    )


class Sink:
    """The event sink, recording what the adapter raised upward."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    def emit(self, event: Any) -> None:
        self.events.append(event)

    def of(self, kind: type) -> list[Any]:
        return [event for event in self.events if isinstance(event, kind)]


@pytest.fixture
def socket_path() -> Iterator[Path]:
    """A private directory, under a root short enough to bind.

    Darwin caps an ``AF_UNIX`` path at 103 bytes, so it cannot live under
    pytest's ``tmp_path``; and it needs a directory only this user can enter,
    twice over — the adapter refuses to carry the user's words over a socket
    sitting where every account could swap it out, and it binds its own reply
    socket in that same directory, where a stranger could otherwise forge a
    `delivered` into it.
    """
    home = Path("/tmp") / f"vc-claude-{next(_names)}-{id(object())}"
    home.mkdir(mode=0o700)
    yield home / "claude.sock"
    shutil.rmtree(home, ignore_errors=True)


def quick(**overrides: Any) -> ClaudeSettings:
    """Settings whose waits are short enough for a test to actually spend them.

    `registry_directory` is always overridden by the callers that bind a reply
    inbox: publishing a peer key into the real one is refused by `conftest`,
    loudly, because that directory holds the live Sessions of whoever is running
    this suite.
    """
    defaults: dict[str, Any] = {
        "request_timeout_seconds": 2.0,
        "ack_timeout_seconds": 0.3,
        "late_ack_timeout_seconds": 2.0,
        "receipt_poll_seconds": 0.02,
    }
    defaults.update(overrides)
    return ClaudeSettings(**defaults)


def reaching(
    path: Path,
    sink: Sink,
    settings: ClaudeSettings | None = None,
    *,
    transcript_path: Path | None = None,
) -> ClaudeAgentAdapter:
    """An adapter that already knows where one Session's inbox listens.

    The registry directory defaults beside the socket, which is what makes the
    reply key land somewhere this test owns. `transcript_path` seeds the other
    half of the registration — the Session's own record, which is where the only
    proof of delivery available on an accepting receiver appears.
    """
    adapter = ClaudeAgentAdapter(
        progress_capture=PROGRESS_CAPTURE,
        sink=sink,
        settings=settings or quick(registry_directory=path.parent),
    )
    adapter.register_session(TARGET, path)
    adapter._reported[TARGET] = SessionReport(  # noqa: SLF001 - seeding one registration
        session_id=SESSION, pid=TARGET.pid, transcript_path=transcript_path
    )
    return adapter


def rid(text: str = "r-1") -> RequestId:
    return RequestId(text)


def arriving(transcript_path: Path, inbox_fake: FakeInbox) -> None:
    """Write what Claude Code writes when it injects the peer message it was sent.

    The `origin` block is verbatim from a real transcript (#71's combined proof):
    `kind`, our reply address exactly as we spelled it, the pid it resolved us to,
    and the `msg_id` we minted. It is the whole correlator.
    """
    frame = inbox_fake.relays[-1]
    transcript_path.write_text(
        json.dumps(
            {
                "type": "user",
                "isMeta": True,
                "message": {"role": "user", "content": "Another Claude session sent a message:"},
                "origin": {
                    "kind": "peer",
                    "from": frame["from"],
                    "verifiedPeerPid": os.getpid(),
                    "msg_id": frame["msg_id"],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )


async def _until(settled) -> None:
    """Wait for one observable fact, or fail saying which one never became true."""
    for _ in range(300):
        if settled():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the state this test waits for never arrived")


class TestRegisteringSessions:
    def test_a_registered_session_inbox_is_recorded(self, socket_path: Path, caplog) -> None:
        caplog.set_level("INFO", logger="gpt_voicecoding.adapters.agent.claude.adapter")
        adapter = ClaudeAgentAdapter(
            progress_capture=PROGRESS_CAPTURE, sink=Sink(), settings=quick()
        )

        adapter.register_session(TARGET, socket_path)

        assert [record.getMessage() for record in caplog.records] == [
            "registered Session inbox "
            f"agent=claude session_id={SESSION} pid=4321 socket={socket_path}"
        ]

    def test_registering_raises_nothing_upward(self, socket_path: Path) -> None:
        """Silent since #27 — the seam's `reply_window` carries the starting level.

        Registration runs before Bridge Core holds the Session, so anything
        raised here is dropped as belonging to a Session nobody knows. The window
        report that used to come out of it was lost on every launch this way.
        """
        sink = Sink()

        ClaudeAgentAdapter(
            progress_capture=PROGRESS_CAPTURE, sink=sink, settings=quick()
        ).register_session(TARGET, socket_path)

        assert sink.events == []


class TestTheLevelItIsAskedFor:
    """The Agent seam's `reply_window` — how a Session's starting level reaches the hub (#27).

    What a registry status *means* is settled in `test_claude_reply_window.py`
    against the watcher's own `level`. What is new here, and only observable at
    the adapter, is the reachability gate: this adapter answers for the Sessions
    it holds an inbox address for, and CLOSED for everything else.
    """

    def _adapter(self, tmp_path: Path, *, status: str = "idle") -> ClaudeAgentAdapter:
        write_record(tmp_path, status=status)
        return ClaudeAgentAdapter(
            progress_capture=PROGRESS_CAPTURE,
            sink=Sink(),
            settings=quick(registry_directory=tmp_path),
        )

    def test_a_session_this_adapter_holds_no_inbox_for_is_closed(self, tmp_path: Path) -> None:
        """Reading someone's record is not the same as being able to reach them.

        The record says `idle` and the honest answer is still CLOSED: a Reply
        Window is a claim about reachability, and this adapter has no way in to
        this Session. Fail closed, exactly as the rest of the seam does.
        """
        assert self._adapter(tmp_path).reply_window(LIVE_TARGET) is ReplyWindow.CLOSED

    def test_a_registered_session_that_is_idle_is_open(
        self, tmp_path: Path, socket_path: Path
    ) -> None:
        """The already-idle-at-registration case, answered without waiting for a sweep."""
        adapter = self._adapter(tmp_path)
        adapter.register_session(LIVE_TARGET, socket_path)

        assert adapter.reply_window(LIVE_TARGET) is ReplyWindow.OPEN

    def test_a_registered_session_that_is_busy_is_closed(
        self, tmp_path: Path, socket_path: Path
    ) -> None:
        adapter = self._adapter(tmp_path, status="busy")
        adapter.register_session(LIVE_TARGET, socket_path)

        assert adapter.reply_window(LIVE_TARGET) is ReplyWindow.CLOSED

    def test_a_forgotten_session_goes_back_to_closed(
        self, tmp_path: Path, socket_path: Path
    ) -> None:
        """The record still says `idle`; this adapter no longer has any way in."""
        adapter = self._adapter(tmp_path)
        adapter.register_session(LIVE_TARGET, socket_path)

        adapter.forget_session(LIVE_TARGET)

        assert adapter.reply_window(LIVE_TARGET) is ReplyWindow.CLOSED


class TestCarryingTheUsersWords:
    """What each of the four grades is earned by, on this route (#71's ruling)."""

    def test_an_accepted_write_alone_is_never_delivery(self, socket_path: Path) -> None:
        """The ordinary case on an accepting receiver, and the whole rule.

        The socket took the line. Nothing says a Session read it: no status
        frame, because a message that is never held is never receipted, and no
        transcript record, because none was written. UNKNOWN is the honest
        answer, and P9 will not send it again on this system's own authority.
        """

        async def scenario():
            async with FakeInbox(socket_path) as session:
                adapter = reaching(socket_path, Sink())
                try:
                    receipt = await adapter.answer_relay(TARGET, "ship it", request_id=rid())
                    return receipt, session.relays
                finally:
                    await adapter.aclose()

        receipt, relays = asyncio.run(scenario())
        assert receipt.outcome is Delivery.UNKNOWN
        assert "nothing proved the words arrived" in receipt.reason
        assert [frame["message"]["content"] for frame in relays] == ["ship it"]

    def test_the_frame_carries_a_uuid_and_our_own_reply_address(self, socket_path: Path) -> None:
        """Both halves of the correlator, and the id's shape is load-bearing.

        The receiver validates `msg_id` against a UUID pattern and drops an id of
        any other shape from the `origin` record — which would silently remove
        the one proof of delivery that works on an accepting receiver. So it is
        minted as a UUID here rather than derived from the hub's `RequestId`.
        """

        async def scenario():
            async with FakeInbox(socket_path) as session:
                adapter = reaching(socket_path, Sink())
                try:
                    await adapter.answer_relay(TARGET, "ship it", request_id=rid())
                    return session.relays[-1]
                finally:
                    await adapter.aclose()

        frame = asyncio.run(scenario())
        assert uuid.UUID(frame["msg_id"]).version == 4
        assert frame["from"].startswith(inbox.ADDRESS_PREFIX)
        assert Path(frame["from"][len(inbox.ADDRESS_PREFIX) :]).parent == socket_path.parent
        assert "priority" not in frame and "from_mode" not in frame

    def test_the_targets_own_transcript_proves_delivery(self, socket_path: Path) -> None:
        """The only source that works where nothing is ever held (#71)."""
        transcript_path = socket_path.parent / "session.jsonl"

        async def scenario():
            async with FakeInbox(socket_path) as session:
                adapter = reaching(socket_path, Sink(), transcript_path=transcript_path)
                try:
                    relaying = asyncio.ensure_future(
                        adapter.answer_relay(TARGET, "ship it", request_id=rid())
                    )
                    await _until(lambda: bool(session.relays))
                    arriving(transcript_path, session)
                    return await relaying
                finally:
                    await adapter.aclose()

        receipt = asyncio.run(scenario())
        assert receipt.outcome is Delivery.DELIVERED

    def test_a_record_naming_another_message_proves_nothing(self, socket_path: Path) -> None:
        """The correlator is exact: somebody else's peer message is not ours."""
        transcript_path = socket_path.parent / "session.jsonl"
        transcript_path.write_text(
            json.dumps(
                {
                    "type": "user",
                    "origin": {
                        "kind": "peer",
                        "from": "uds:/tmp/cc-socks/somebody-else.sock",
                        "msg_id": "8b1f0e0e-0000-4000-8000-000000000000",
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

        async def scenario():
            async with FakeInbox(socket_path):
                adapter = reaching(socket_path, Sink(), transcript_path=transcript_path)
                try:
                    return await adapter.answer_relay(TARGET, "ship it", request_id=rid())
                finally:
                    await adapter.aclose()

        assert asyncio.run(scenario()).outcome is Delivery.UNKNOWN

    def test_a_held_message_is_reported_as_parked_at_once(self, socket_path: Path) -> None:
        """The person it is parked in front of may take minutes; say so now.

        HELD is its own grade because "parked for someone to release" and "we
        cannot tell" are different things to be told, and P9 refuses to re-send
        either of them.
        """

        async def scenario():
            async with FakeInbox(socket_path, statuses=((0.0, "held"),)):
                adapter = reaching(socket_path, Sink())
                try:
                    return await adapter.answer_relay(TARGET, "ship it", request_id=rid())
                finally:
                    await adapter.aclose()

        receipt = asyncio.run(scenario())
        assert receipt.outcome is Delivery.HELD
        assert "parked" in receipt.reason

    def test_a_refused_message_never_reached_the_session_so_it_failed(
        self, socket_path: Path
    ) -> None:
        """Proven non-delivery, which is the one grade P9 allows another attempt for."""

        async def scenario():
            async with FakeInbox(socket_path, statuses=((0.0, "denied"),)):
                adapter = reaching(socket_path, Sink())
                try:
                    return await adapter.answer_relay(TARGET, "ship it", request_id=rid())
                finally:
                    await adapter.aclose()

        receipt = asyncio.run(scenario())
        assert receipt.outcome is Delivery.FAILED
        assert "denied" in receipt.reason

    def test_a_held_message_released_late_is_raised_as_delivered(self, socket_path: Path) -> None:
        """The upgrade that stops Bridge Core re-delivering words that arrived."""

        async def scenario():
            sink = Sink()
            async with FakeInbox(socket_path, statuses=((0.0, "held"), (0.4, "delivered"))):
                adapter = reaching(socket_path, sink)
                try:
                    receipt = await adapter.answer_relay(TARGET, "ship it", request_id=rid())
                    await _until(lambda: bool(sink.of(RelayReceipt)))
                    return receipt, sink.of(RelayReceipt)
                finally:
                    await adapter.aclose()

        receipt, late = asyncio.run(scenario())
        assert receipt.outcome is Delivery.HELD
        assert [event.receipt.outcome for event in late] == [Delivery.DELIVERED]
        assert late[0].receipt.request_id == rid()
        assert late[0].target == TARGET

    def test_a_held_message_that_expires_is_raised_as_a_failure(self, socket_path: Path) -> None:
        """The other direction, and #71 asked for it by name.

        A held message expires after about five minutes. An engine that raised
        only upgrades would leave it recorded as parked long after it was thrown
        away — words the user believes are waiting for somebody, which is the
        implied delivery this route exists to refuse.
        """

        async def scenario():
            sink = Sink()
            async with FakeInbox(socket_path, statuses=((0.0, "held"), (0.4, "expired"))):
                adapter = reaching(socket_path, sink)
                try:
                    receipt = await adapter.answer_relay(TARGET, "ship it", request_id=rid())
                    await _until(lambda: bool(sink.of(RelayReceipt)))
                    return receipt, sink.of(RelayReceipt)
                finally:
                    await adapter.aclose()

        receipt, late = asyncio.run(scenario())
        assert receipt.outcome is Delivery.HELD
        assert [event.receipt.outcome for event in late] == [Delivery.FAILED]
        assert "expired" in late[0].receipt.reason

    def test_a_record_written_after_the_wait_is_still_raised(self, socket_path: Path) -> None:
        """The late path watches the transcript too, and it has to.

        A Relay into a Session that has started a turn is injected when the turn
        *ends*, which can be minutes — and on an accepting receiver there is no
        status frame to settle it. Watching only the receipts would leave those
        UNKNOWN for ever, and the hub would say the words a second time.
        """
        transcript_path = socket_path.parent / "session.jsonl"

        async def scenario():
            sink = Sink()
            async with FakeInbox(socket_path) as session:
                adapter = reaching(socket_path, sink, transcript_path=transcript_path)
                try:
                    receipt = await adapter.answer_relay(TARGET, "ship it", request_id=rid())
                    arriving(transcript_path, session)
                    await _until(lambda: bool(sink.of(RelayReceipt)))
                    return receipt, sink.of(RelayReceipt)
                finally:
                    await adapter.aclose()

        receipt, late = asyncio.run(scenario())
        assert receipt.outcome is Delivery.UNKNOWN, "the wait was spent before it arrived"
        assert [event.receipt.outcome for event in late] == [Delivery.DELIVERED]

    def test_a_status_about_another_message_settles_nothing(self, socket_path: Path) -> None:
        """Correlated by `orig_msg_id`, so somebody else's receipt is not ours."""

        async def scenario():
            async with FakeInbox(socket_path, statuses=((0.0, "delivered"),)) as session:
                adapter = reaching(socket_path, Sink())
                try:
                    replies = await adapter._reply_inbox(socket_path.parent)  # noqa: SLF001
                    await session._status(  # noqa: SLF001
                        replies.address[len(inbox.ADDRESS_PREFIX) :], "not-ours", "delivered", 0.0
                    )
                    await _until(lambda: bool(replies.statuses("not-ours")))
                    return replies.statuses("00000000-0000-4000-8000-000000000000")
                finally:
                    await adapter.aclose()

        assert asyncio.run(scenario()) == ()

    def test_closing_the_adapter_ends_every_late_listener(self, socket_path: Path) -> None:
        """No watcher of a Session's own record may outlive this adapter."""

        async def scenario():
            async with FakeInbox(socket_path):
                adapter = reaching(socket_path, Sink())
                await adapter.answer_relay(TARGET, "ship it", request_id=rid())
                listening = len(adapter._listening)  # noqa: SLF001
                await adapter.aclose()
                await asyncio.sleep(0)
                return listening, len(adapter._listening)  # noqa: SLF001

        before, after = asyncio.run(scenario())
        assert (before, after) == (1, 0)

    def test_closing_the_adapter_takes_the_reply_socket_and_its_key_away(
        self, socket_path: Path
    ) -> None:
        """Both live in directories that are not ours, so both are removed.

        A key left in Claude Code's registry is this process still claiming to be
        a peer after it is gone, and a stale socket beside a Session's own is
        rubbish in somebody else's directory.
        """

        async def scenario():
            async with FakeInbox(socket_path):
                adapter = reaching(socket_path, Sink())
                await adapter.answer_relay(TARGET, "ship it", request_id=rid())
                replies = await adapter._reply_inbox(socket_path.parent)  # noqa: SLF001
                during = (replies.path.exists(), replies._key_path.exists())  # noqa: SLF001
                await adapter.aclose()
                return during, (
                    replies.path.exists(),
                    replies._key_path.exists(),  # noqa: SLF001
                )

        during, after = asyncio.run(scenario())
        assert during == (True, True)
        assert after == (False, False)

    def test_one_reply_socket_serves_every_session_in_a_directory(self, socket_path: Path) -> None:
        """The receipt namespace is the directory, so a second socket buys nothing."""
        second = socket_path.parent / "other.sock"

        async def scenario():
            async with FakeInbox(socket_path), FakeInbox(second):
                adapter = reaching(socket_path, Sink())
                other = SessionTarget(agent=AgentKind.CLAUDE, session_id="other", pid=999)
                adapter.register_session(other, second)
                try:
                    await adapter.answer_relay(TARGET, "first", request_id=rid("r-1"))
                    await adapter.answer_relay(other, "second", request_id=rid("r-2"))
                    return len(adapter._replies)  # noqa: SLF001
                finally:
                    await adapter.aclose()

        assert asyncio.run(scenario()) == 1

    def test_two_rapid_relays_keep_their_order_and_their_own_receipts(
        self, socket_path: Path
    ) -> None:
        async def scenario():
            async with FakeInbox(socket_path, statuses=((0.0, "denied"),)) as session:
                adapter = reaching(socket_path, Sink())
                try:
                    first, second = await asyncio.gather(
                        adapter.answer_relay(TARGET, "first", request_id=rid("r-1")),
                        adapter.answer_relay(TARGET, "second", request_id=rid("r-2")),
                    )
                    return first, second, session.relays
                finally:
                    await adapter.aclose()

        first, second, relays = asyncio.run(scenario())
        assert first.outcome is Delivery.FAILED
        assert second.outcome is Delivery.FAILED
        assert sorted(frame["message"]["content"] for frame in relays) == ["first", "second"]
        assert (first.request_id, second.request_id) == ("r-1", "r-2")
        assert len({frame["msg_id"] for frame in relays}) == 2, "two Relays, two correlators"


class TestTheAuthLine:
    """Whose token rides the auth frame, and why it is the receiver's own."""

    def test_the_receivers_own_token_is_presented_when_it_reported_one(
        self, socket_path: Path
    ) -> None:
        """The own-child line: the documented way past a bypass receiver's hold."""

        async def scenario():
            async with FakeInbox(socket_path) as session:
                adapter = reaching(socket_path, Sink())
                adapter._reported[TARGET] = SessionReport(  # noqa: SLF001
                    session_id=SESSION, pid=TARGET.pid, messaging_token="t-1"
                )
                try:
                    await adapter.answer_relay(TARGET, "ship it", request_id=rid())
                    return session.received
                finally:
                    await adapter.aclose()

        received = asyncio.run(scenario())
        assert received[0] == {"type": "auth", "token": "t-1"}
        assert received[1]["type"] == "user"

    def test_a_session_that_reported_no_token_is_still_relayed_to(self, socket_path: Path) -> None:
        """Delivery never needed it: #71's first probe authenticated nothing."""

        async def scenario():
            async with FakeInbox(socket_path) as session:
                adapter = reaching(socket_path, Sink())
                try:
                    await adapter.answer_relay(TARGET, "ship it", request_id=rid())
                    return session.received
                finally:
                    await adapter.aclose()

        received = asyncio.run(scenario())
        assert [frame["type"] for frame in received] == ["user"]


class TestTheRoutesThisBuildReallyHas:
    def test_supplement_is_declared_absent_and_refuses_rather_than_pretending(
        self, socket_path: Path
    ) -> None:
        """Deciding what to do instead is Bridge Core's policy, not this adapter's."""

        async def scenario():
            async with FakeInbox(socket_path) as session:
                adapter = reaching(socket_path, Sink())
                try:
                    receipt = await adapter.answer_relay(
                        TARGET, "and one more thing", request_id=rid(), route=RelayRoute.SUPPLEMENT
                    )
                    return receipt, session.received
                finally:
                    await adapter.aclose()

        receipt, received = asyncio.run(scenario())
        assert (
            RelayRoute.SUPPLEMENT
            not in claude_agent(
                progress_capture=PROGRESS_CAPTURE,
            ).supported_routes()
        )
        assert receipt.outcome is Delivery.FAILED
        assert received == [], "a route this adapter lacks must put nothing on the wire"

    def test_the_approval_relay_refuses_by_name_when_no_hook_route_exists(
        self, socket_path: Path
    ) -> None:
        """It rides the PermissionRequest hook, which this Session's launch never opened.

        A registered Session is not automatically an answerable one: the hook
        arrives only for a launch that carried both the hook plugin and the
        approval socket's address, so the refusal names those rather than
        reporting an empty race.
        """

        async def scenario():
            async with FakeInbox(socket_path) as session:
                adapter = reaching(socket_path, Sink())
                try:
                    verdict = await adapter.approval_relay(
                        ApprovalRequest(
                            approval_id="a-1",
                            target=TARGET,
                            tool_name="Bash",
                        ),
                        ApprovalVerdict.ALLOW,
                        request_id=rid("r-2"),
                    )
                    return verdict, session.received
                finally:
                    await adapter.aclose()

        verdict, received = asyncio.run(scenario())
        assert verdict.outcome is Delivery.FAILED and verdict.reason == APPROVAL_UNROUTED
        assert received == [], "a verdict never travels over the inbox: it carries no authority"


class TestFailingBeforeTheWordsLeave:
    def test_an_unregistered_session_fails_closed(self, socket_path: Path) -> None:
        async def scenario():
            adapter = ClaudeAgentAdapter(
                progress_capture=PROGRESS_CAPTURE, sink=Sink(), settings=quick()
            )
            try:
                return await adapter.answer_relay(TARGET, "ship it", request_id=rid())
            finally:
                await adapter.aclose()

        receipt = asyncio.run(scenario())
        assert receipt.outcome is Delivery.FAILED
        assert "no Claude Session is registered" in receipt.reason

    def test_an_inbox_nothing_listens_on_fails_and_names_the_layer(self, socket_path: Path) -> None:
        async def scenario():
            adapter = reaching(socket_path, Sink())
            try:
                return await adapter.answer_relay(TARGET, "ship it", request_id=rid())
            finally:
                await adapter.aclose()

        receipt = asyncio.run(scenario())
        assert receipt.outcome is Delivery.FAILED
        assert "inbox is unreachable" in receipt.reason

    def test_a_write_that_fails_after_the_connection_is_indeterminate(
        self, socket_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Past the connection the line may or may not have been read.

        Driven from the seam rather than from a race, because the interleave that
        produces it — the far side going away between the connect and the drain —
        cannot be scheduled reliably, and what is under test is the grading.
        """

        async def broken(*_: object, **__: object) -> None:
            raise ConnectionResetError("the Session went away")

        async def scenario():
            async with FakeInbox(socket_path):
                adapter = reaching(socket_path, Sink())
                monkeypatch.setattr(inbox, "send", broken)
                try:
                    return await adapter.answer_relay(TARGET, "ship it", request_id=rid())
                finally:
                    await adapter.aclose()

        receipt = asyncio.run(scenario())
        assert receipt.outcome is Delivery.UNKNOWN
        assert "the write to the Session's inbox failed" in receipt.reason

    def test_words_larger_than_this_engine_allows_never_reach_the_wire(
        self, socket_path: Path
    ) -> None:
        async def scenario():
            async with FakeInbox(socket_path) as session:
                adapter = reaching(
                    socket_path,
                    Sink(),
                    quick(max_text_bytes=8, registry_directory=socket_path.parent),
                )
                try:
                    receipt = await adapter.answer_relay(
                        TARGET, "far more than eight bytes", request_id=rid()
                    )
                    return receipt, session.received
                finally:
                    await adapter.aclose()

        receipt, received = asyncio.run(scenario())
        assert receipt.outcome is Delivery.FAILED
        assert received == []

    def test_a_socket_other_accounts_can_reach_is_refused(self, socket_path: Path) -> None:
        """The privacy check is not advice: a widened socket is not spoken to."""

        async def scenario():
            async with FakeInbox(socket_path):
                os.chmod(socket_path, 0o666)
                adapter = reaching(socket_path, Sink())
                try:
                    return await adapter.answer_relay(TARGET, "ship it", request_id=rid())
                finally:
                    await adapter.aclose()

        receipt = asyncio.run(scenario())
        assert receipt.outcome is Delivery.FAILED
        assert "reachable by other accounts" in receipt.reason

    def test_a_socket_reached_through_a_symlink_is_refused(self, socket_path: Path) -> None:
        """`lstat`, not `stat`: a link may aim at something this user really owns."""

        async def scenario():
            async with FakeInbox(socket_path):
                link = socket_path.parent / "link.sock"
                link.symlink_to(socket_path)
                adapter = reaching(link, Sink())
                try:
                    return await adapter.answer_relay(TARGET, "ship it", request_id=rid())
                finally:
                    await adapter.aclose()

        receipt = asyncio.run(scenario())
        assert receipt.outcome is Delivery.FAILED
        assert "symbolic link" in receipt.reason

    def test_a_path_too_long_to_bind_is_refused_with_its_byte_count(
        self, socket_path: Path
    ) -> None:
        """The one thing that separates "could not bind" from "the path is too long"."""

        async def scenario():
            adapter = reaching(socket_path.parent / ("x" * 120), Sink())
            try:
                return await adapter.answer_relay(TARGET, "ship it", request_id=rid())
            finally:
                await adapter.aclose()

        receipt = asyncio.run(scenario())
        assert receipt.outcome is Delivery.FAILED
        assert "may not exceed 103" in receipt.reason

    def test_a_codex_session_is_not_this_adapters_to_reach(self) -> None:
        adapter = ClaudeAgentAdapter(
            progress_capture=PROGRESS_CAPTURE,
        )
        with pytest.raises(ValueError, match="not this adapter's"):
            adapter.register_session(
                SessionTarget(agent=AgentKind.CODEX, session_id="t-1"), Path("/tmp/x.sock")
            )


class TestReportingWhatIsLoaded:
    def test_verify_names_this_implementation_and_passes_with_nothing_registered(self) -> None:
        result = asyncio.run(
            ClaudeAgentAdapter(
                progress_capture=PROGRESS_CAPTURE,
            ).verify()
        )
        assert result.outcome is VerifyOutcome.PASS
        assert result.loaded.endswith(":ClaudeAgentAdapter")
        assert "no Claude Session is registered" in result.detail

    def test_verify_passes_when_a_registered_inbox_answers(self, socket_path: Path) -> None:
        """A dial and an immediate close: anything written would be a real message."""

        async def scenario():
            async with FakeInbox(socket_path) as session:
                adapter = reaching(socket_path, Sink())
                try:
                    return await adapter.verify(), session.received
                finally:
                    await adapter.aclose()

        result, received = asyncio.run(scenario())
        assert result.outcome is VerifyOutcome.PASS
        assert "1 of 1" in result.detail
        assert received == [], "verify must never put a line in somebody's conversation"

    def test_verify_fails_and_names_the_layer_when_no_inbox_answers(
        self, socket_path: Path
    ) -> None:
        async def scenario():
            adapter = reaching(socket_path, Sink())
            try:
                return await adapter.verify()
            finally:
                await adapter.aclose()

        result = asyncio.run(scenario())
        assert result.outcome is VerifyOutcome.FAIL
        assert SESSION in result.detail


class TestWhatThisSpokeMayBeTold:
    def test_an_unknown_key_refuses_to_start_and_lists_what_there_is(self) -> None:
        with pytest.raises(SettingsError, match="ack_timeout_secondz"):
            claude_agent(progress_capture=PROGRESS_CAPTURE, settings={"ack_timeout_secondz": 5})

    def test_a_text_budget_larger_than_the_line_budget_can_never_be_spent(self) -> None:
        with pytest.raises(SettingsError, match="must fit inside"):
            ClaudeSettings(max_message_bytes=100, max_text_bytes=200)

    def test_a_timeout_that_is_not_a_number_of_seconds_is_refused(self) -> None:
        with pytest.raises(SettingsError, match="number of seconds"):
            claude_agent(
                progress_capture=PROGRESS_CAPTURE, settings={"ack_timeout_seconds": "soon"}
            )


class TestTheHubStopsHoldingWhatArrived:
    """The reason the late receipt exists, asserted against the real Bridge Core.

    Everything not proven delivered is retained and sent again when the Reply
    Window next opens. So the question the late receipt answers is not "was the
    grade tidy" — it is whether the hub is still holding words the Session has
    already acted on, and would say them a second time.
    """

    def test_a_late_release_takes_the_retained_relay_out_of_the_queue(
        self, socket_path: Path
    ) -> None:
        async def scenario():
            async with FakeInbox(socket_path, statuses=((0.0, "held"), (0.3, "delivered"))):
                sink = Sink()
                adapter = reaching(socket_path, sink)
                registry = SessionRegistry()
                registry.register(
                    Session(
                        target=TARGET,
                        name=SessionName("GPT-VoiceCoding", "port the log"),
                        workspace=Path("/tmp/workspace"),
                        first_seen=0.0,
                        state=SessionState.IDLE,
                    )
                )
                queue = RelayQueue()
                pipeline = RelayPipeline(
                    agents={AgentKind.CLAUDE: adapter}, sessions=registry, relays=queue
                )
                try:
                    outcome = await pipeline.relay(TARGET, "ship it", request_id=rid())
                    retained = queue.pending_for(TARGET)

                    await _until(lambda: bool(sink.of(RelayReceipt)))
                    for event in sink.of(RelayReceipt):
                        queue.classify(event.receipt.request_id, event.receipt.outcome)
                    return outcome, retained, queue.pending_for(TARGET)
                finally:
                    await adapter.aclose()

        outcome, retained, left = asyncio.run(scenario())
        assert outcome.state is Lifecycle.RETAINED, "an unproven Relay is held, not delivered"
        assert [held.request_id for held in retained] == ["r-1"]
        assert left == (), "the hub must stop holding words the Session was given"

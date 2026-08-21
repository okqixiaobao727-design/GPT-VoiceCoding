"""The peer wire: the frame that goes out, and the receipts that correlate back.

Everything here runs against real Unix sockets in a temporary directory. The
frame shapes are the ones the pinned receiver really reads, and the tests assert
the two that fail *silently* when assumed wrong — the words' position and the
`msg_id` format — because a silent drop is exactly what this suite exists to stop
shipping.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from gpt_voicecoding.adapters.agent.claude.peer import (
    LISTENER_PREFIX,
    MSG_VERSION,
    NOTICE_PRIORITY,
    PEER_FRAME_CAP_BYTES,
    PeerError,
    Receipt,
    ReceiptListener,
    notice_frame,
    receipt_in,
    remove_stale_listeners,
    send_frame,
)

SESSION = "430b0def-38ef-4783-8d57-d800710d83bd"


def a_request_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def socks() -> Iterator[Path]:
    """A private directory standing in for `cc-socks`, under a root short enough to bind.

    Not `tmp_path`: Darwin caps an `AF_UNIX` path at 103 bytes and pytest's
    temporary paths are far longer — the very limit `privacy.py` exists to name.
    Private, because these rules refuse a socket in a directory others can enter.
    """
    home = Path("/tmp") / f"vc-peer-{os.getpid()}-{id(object())}"
    home.mkdir(mode=0o700)
    yield home
    shutil.rmtree(home, ignore_errors=True)


def status_frame(**fields: object) -> dict[str, object]:
    return {"type": "control", "action": "peer_message_status", **fields}


def abandoned_socket(path: Path) -> None:
    """A real socket inode with nobody accepting — the shape a dead Session leaves."""
    held = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    held.bind(str(path))
    held.close()


async def push(path: Path, frame: dict[str, object]) -> None:
    """Send one frame the way a receiving Session sends a receipt back to us."""
    _, writer = await asyncio.open_unix_connection(str(path))
    writer.write(json.dumps(frame).encode("utf-8") + b"\n")
    await writer.drain()
    writer.close()
    await writer.wait_closed()


# -- the frame that goes out ---------------------------------------------


class TestTheFrame:
    def test_the_words_ride_at_message_content_where_the_receiver_reads_them(self) -> None:
        """`e.message?.content`. At the top level they are dropped with only a warning."""
        rid = a_request_id()

        frame = notice_frame(
            text="the build finished",
            request_id=rid,
            session_id=SESSION,
            reply_address="uds:/tmp/cc-socks/gpt-voicecoding-1.sock",
        )

        assert frame["message"] == {"role": "user", "content": "the build finished"}
        assert "content" not in frame
        assert frame["type"] == "user"
        assert frame["uuid"] == rid
        assert frame["msg_id"] == rid
        assert frame["msgV"] == MSG_VERSION
        assert frame["priority"] == NOTICE_PRIORITY
        assert frame["session_id"] == SESSION

    def test_one_id_is_both_uuid_and_msg_id_so_an_attempt_is_one_thing(self) -> None:
        """The readback matches on `uuid`; the receipt matches on `msg_id`. Same value."""
        rid = a_request_id()

        frame = notice_frame(
            text="hello", request_id=rid, session_id=SESSION, reply_address="uds:/tmp/x.sock"
        )

        assert frame["uuid"] == frame["msg_id"] == rid

    def test_a_request_id_that_is_not_a_uuid_is_refused_before_the_wire(self) -> None:
        """A malformed msg_id is silently discarded, leaving nothing to correlate."""
        with pytest.raises(PeerError) as refused:
            notice_frame(
                text="hello",
                request_id="not-a-uuid",
                session_id=SESSION,
                reply_address="uds:/tmp/x.sock",
            )

        assert "UUID" in str(refused.value)

    def test_words_that_are_only_whitespace_are_refused_rather_than_dropped_silently(
        self,
    ) -> None:
        with pytest.raises(PeerError):
            notice_frame(
                text="   ",
                request_id=a_request_id(),
                session_id=SESSION,
                reply_address="uds:/tmp/x.sock",
            )


# -- putting it on the wire ----------------------------------------------


class TestSending:
    def test_a_frame_reaches_a_listening_socket(self, socks: Path) -> None:
        path = socks / "4242.sock"

        async def scenario():
            received: list[dict[str, object]] = []
            taken = asyncio.Event()

            async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
                line = await reader.readline()
                received.append(json.loads(line))
                taken.set()
                writer.close()

            server = await asyncio.start_unix_server(serve, path=str(path))
            os.chmod(path, 0o600)
            try:
                frame = notice_frame(
                    text="the build finished",
                    request_id=a_request_id(),
                    session_id=SESSION,
                    reply_address="uds:/tmp/x.sock",
                )
                await send_frame(path, frame, timeout_seconds=5.0)
                await asyncio.wait_for(taken.wait(), timeout=5.0)
                return received
            finally:
                server.close()
                await server.wait_closed()

        received = asyncio.run(scenario())
        assert received[0]["message"] == {"role": "user", "content": "the build finished"}

    def test_a_socket_nothing_listens_on_is_a_refusal_before_any_byte(self, socks: Path) -> None:
        path = socks / "4242.sock"
        abandoned_socket(path)
        os.chmod(path, 0o600)

        async def scenario():
            await send_frame(
                path,
                notice_frame(
                    text="hello",
                    request_id=a_request_id(),
                    session_id=SESSION,
                    reply_address="uds:/tmp/x.sock",
                ),
                timeout_seconds=1.0,
            )

        with pytest.raises(PeerError):
            asyncio.run(scenario())

    def test_a_socket_this_user_does_not_own_is_refused(self, socks: Path) -> None:
        """The privacy rules are checked before the dial, not instead of it."""
        path = socks / "4242.sock"
        path.write_text("not a socket at all", encoding="utf-8")

        async def scenario():
            await send_frame(
                path,
                notice_frame(
                    text="hello",
                    request_id=a_request_id(),
                    session_id=SESSION,
                    reply_address="uds:/tmp/x.sock",
                ),
                timeout_seconds=1.0,
            )

        with pytest.raises(PeerError) as refused:
            asyncio.run(scenario())
        assert "not a socket" in str(refused.value)

    def test_a_frame_larger_than_the_receivers_cap_never_leaves(self, socks: Path) -> None:
        async def scenario():
            await send_frame(
                socks / "4242.sock",
                notice_frame(
                    text="x" * (PEER_FRAME_CAP_BYTES + 1),
                    request_id=a_request_id(),
                    session_id=SESSION,
                    reply_address="uds:/tmp/x.sock",
                ),
                timeout_seconds=1.0,
            )

        with pytest.raises(PeerError) as refused:
            asyncio.run(scenario())
        assert str(PEER_FRAME_CAP_BYTES) in str(refused.value)


# -- correlating receipts ------------------------------------------------


class TestCorrelatingReceipts:
    def test_a_receipt_for_this_attempt_is_correlated_by_orig_msg_id(self) -> None:
        rid = a_request_id()

        found = receipt_in(status_frame(status="held", orig_msg_id=rid), rid)

        assert found is not None
        assert found.status is Receipt.HELD
        assert not found.is_terminal

    def test_a_receipt_for_another_attempt_says_nothing_about_this_one(self) -> None:
        frame = status_frame(status="denied", orig_msg_id=a_request_id())

        assert receipt_in(frame, a_request_id()) is None

    def test_an_expired_receipt_detailed_as_refused_is_read_as_refused(self) -> None:
        """The receiver re-reads its own `expired` this way, and so must we."""
        rid = a_request_id()

        found = receipt_in(
            status_frame(status="expired", status_detail="refused", orig_msg_id=rid), rid
        )

        assert found is not None
        assert found.status is Receipt.REFUSED

    def test_a_batch_drop_is_correlated_by_the_list_when_orig_msg_id_names_another(self) -> None:
        """The receiver's own log warns that `orig_msg_id` may name somebody else's."""
        rid = a_request_id()

        found = receipt_in(
            status_frame(
                status="dropped",
                orig_msg_id=a_request_id(),
                dropped_msg_ids=[a_request_id(), rid],
                drop_reason="queue-full",
            ),
            rid,
        )

        assert found is not None
        assert found.status is Receipt.DROPPED
        assert found.detail == "queue-full"

    def test_a_batch_drop_naming_nobody_we_know_says_nothing(self) -> None:
        frame = status_frame(
            status="dropped", orig_msg_id=a_request_id(), dropped_msg_ids=[a_request_id()]
        )

        assert receipt_in(frame, a_request_id()) is None

    def test_a_status_this_build_has_never_seen_is_not_interpreted(self) -> None:
        rid = a_request_id()

        assert receipt_in(status_frame(status="teleported", orig_msg_id=rid), rid) is None

    def test_a_frame_that_is_not_a_receipt_at_all_says_nothing(self) -> None:
        rid = a_request_id()

        assert receipt_in({"type": "user", "message": {"content": "hi"}}, rid) is None
        assert receipt_in({"type": "control", "action": "rename", "name": "x"}, rid) is None


# -- the listener --------------------------------------------------------


class TestTheReceiptListener:
    def test_it_binds_privately_and_hands_a_receipt_to_its_watcher(self, socks: Path) -> None:
        listener = ReceiptListener(socks)

        async def scenario():
            await listener.start()
            try:
                rid = a_request_id()
                with listener.watch(rid) as inbox:
                    await push(listener.path, status_frame(status="delivered", orig_msg_id=rid))
                    return (
                        await asyncio.wait_for(inbox.get(), timeout=5.0),
                        listener.path.stat().st_mode & 0o777,
                        listener.address,
                    )
            finally:
                await listener.aclose()

        receipt, mode, address = asyncio.run(scenario())
        assert receipt.status is Receipt.DELIVERED
        assert mode == 0o600
        assert address == f"uds:{listener.path}"
        assert listener.path.name.startswith(LISTENER_PREFIX)
        assert not listener.path.exists()

    def test_one_batch_drop_answers_every_attempt_it_names(self, socks: Path) -> None:
        listener = ReceiptListener(socks)

        async def scenario():
            await listener.start()
            try:
                mine, theirs = a_request_id(), a_request_id()
                with listener.watch(mine) as one, listener.watch(theirs) as two:
                    await push(
                        listener.path,
                        status_frame(
                            status="dropped",
                            orig_msg_id=a_request_id(),
                            dropped_msg_ids=[mine, theirs],
                            drop_reason="queue-full",
                        ),
                    )
                    return (
                        await asyncio.wait_for(one.get(), timeout=5.0),
                        await asyncio.wait_for(two.get(), timeout=5.0),
                    )
            finally:
                await listener.aclose()

        first, second = asyncio.run(scenario())
        assert first.status is second.status is Receipt.DROPPED

    def test_a_watcher_that_has_gone_leaves_nothing_behind(self, socks: Path) -> None:
        listener = ReceiptListener(socks)

        async def scenario():
            await listener.start()
            try:
                rid = a_request_id()
                with listener.watch(rid):
                    pass
                # Nothing to deliver to, and delivering must not raise.
                await push(listener.path, status_frame(status="expired", orig_msg_id=rid))
                await asyncio.sleep(0.05)
                return listener.listening
            finally:
                await listener.aclose()

        assert asyncio.run(scenario())

    def test_a_malformed_frame_does_not_stop_the_listener(self, socks: Path) -> None:
        listener = ReceiptListener(socks)

        async def scenario():
            await listener.start()
            try:
                rid = a_request_id()
                with listener.watch(rid) as inbox:
                    _, writer = await asyncio.open_unix_connection(str(listener.path))
                    writer.write(b"not json\n")
                    await writer.drain()
                    writer.close()
                    await writer.wait_closed()
                    await push(listener.path, status_frame(status="denied", orig_msg_id=rid))
                    return await asyncio.wait_for(inbox.get(), timeout=5.0)
            finally:
                await listener.aclose()

        assert asyncio.run(scenario()).status is Receipt.DENIED

    def test_it_rebinds_over_a_socket_positively_proven_abandoned(self, socks: Path) -> None:
        abandoned_socket(socks / f"{LISTENER_PREFIX}{os.getpid()}.sock")
        listener = ReceiptListener(socks)

        async def scenario():
            await listener.start()
            try:
                return listener.listening
            finally:
                await listener.aclose()

        assert asyncio.run(scenario())

    def test_it_refuses_when_there_is_no_socket_namespace_to_join(self, tmp_path: Path) -> None:
        """No `cc-socks` means no Session is reachable and no receipt could route back."""
        listener = ReceiptListener(tmp_path / "absent")

        with pytest.raises(PeerError):
            asyncio.run(listener.start())


class TestTheUninstallSweep:
    def test_a_live_listener_is_never_swept(self, socks: Path) -> None:
        """The sweep must not sever an inode somebody is still accepting on."""
        listener = ReceiptListener(socks)

        async def scenario():
            await listener.start()
            try:
                return remove_stale_listeners(socks), listener.path.exists()
            finally:
                await listener.aclose()

        swept, still_there = asyncio.run(scenario())
        assert swept == ()
        assert still_there

    def test_it_takes_only_our_own_abandoned_sockets(self, socks: Path) -> None:
        ours = socks / f"{LISTENER_PREFIX}4242.sock"
        claudes = socks / "4243.sock"
        abandoned_socket(ours)
        abandoned_socket(claudes)
        ordinary = socks / f"{LISTENER_PREFIX}4244.sock"
        ordinary.write_text("not a socket", encoding="utf-8")

        swept = remove_stale_listeners(socks)

        assert swept == (ours,)
        assert not ours.exists()
        assert claudes.exists()
        assert ordinary.exists()

    def test_sweeping_a_directory_that_does_not_exist_removes_nothing(self, tmp_path: Path) -> None:
        assert remove_stale_listeners(tmp_path / "absent") == ()

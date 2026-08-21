"""The Notice Relay end to end, against a stand-in for a real Claude Session.

Every scenario the issue named as its contract is here, and each one is a
statement about *proof* rather than about plumbing: what the adapter is entitled
to call delivered, and what it must refuse to call delivered no matter how much
it looks like success.

The fake Session is a registry record, a transcript file and a peer socket — the
three surfaces a real one exposes — so these run the adapter's real classifier
over the real shapes, with only the Session's own behaviour standing in.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from gpt_voicecoding.adapters.agent.claude.notice import NoticeRelay, Readback, readback_in
from gpt_voicecoding.adapters.agent.claude.peer import ReceiptListener
from gpt_voicecoding.adapters.agent.claude.registry import PEER_PROTOCOL
from gpt_voicecoding.adapters.agent.claude.settings import ClaudeSettings
from gpt_voicecoding.seams.agent import RelayReceipt
from gpt_voicecoding.seams.delivery import Delivery
from gpt_voicecoding.seams.identity import AgentKind, RequestId, SessionTarget

SESSION = "430b0def-38ef-4783-8d57-d800710d83bd"
#: A pid that is certainly not running. Zero and negatives are refused as targets,
#: so this is a plausible-looking one nothing owns.
DEAD_PID = 4_000_000


def rid() -> RequestId:
    return RequestId(str(uuid.uuid4()))


def target(pid: int, session_id: str = SESSION) -> SessionTarget:
    return SessionTarget(agent=AgentKind.CLAUDE, session_id=session_id, pid=pid)


@pytest.fixture
def home() -> Iterator[Path]:
    """A short private root: sockets live here, and `AF_UNIX` caps the path length."""
    root = Path("/tmp") / f"vc-notice-{os.getpid()}-{id(object())}"
    (root / "socks").mkdir(mode=0o700, parents=True)
    (root / "sessions").mkdir(mode=0o700)
    (root / "projects" / "-a-workspace").mkdir(mode=0o700, parents=True)
    yield root
    shutil.rmtree(root, ignore_errors=True)


def settings_for(home: Path, **overrides: object) -> ClaudeSettings:
    return ClaudeSettings(
        registry_directory=home / "sessions",
        projects_directory=home / "projects",
        peer_socket_directory=home / "socks",
        request_timeout_seconds=2.0,
        readback_timeout_seconds=float(overrides.pop("readback_timeout_seconds", 2.0)),
        late_readback_timeout_seconds=float(overrides.pop("late_readback_timeout_seconds", 2.0)),
        readback_poll_seconds=0.02,
        **overrides,  # type: ignore[arg-type]
    )


@dataclass
class Sink:
    """Collects what the adapter raises upward."""

    events: list[object] = field(default_factory=list)

    def emit(self, event: object) -> None:
        self.events.append(event)


class FakeSession:
    """A registry record, a transcript and a peer socket — a Session's three surfaces.

    Its `answer` decides what a real Session would have done with the frame:
    write a transcript record, send a receipt, both, or nothing at all. That is
    the whole variable these tests turn.
    """

    def __init__(
        self,
        home: Path,
        *,
        pid: int,
        session_id: str = SESSION,
        status: str = "idle",
        registered: bool = True,
    ) -> None:
        self.home = home
        self.pid = pid
        self.session_id = session_id
        self.path = home / "socks" / f"{pid}.sock"
        self.transcript = home / "projects" / "-a-workspace" / f"{session_id}.jsonl"
        self.received: list[dict[str, object]] = []
        self.taken = asyncio.Event()
        self._server: asyncio.Server | None = None
        self._answer = None
        if registered:
            (home / "sessions" / f"{pid}.json").write_text(
                json.dumps(
                    {
                        "pid": pid,
                        "sessionId": session_id,
                        "cwd": "/a/workspace",
                        "version": "2.1.238",
                        "peerProtocol": PEER_PROTOCOL,
                        "messagingSocketPath": str(self.path),
                        "status": status,
                        "name": f"session-{pid}",
                    }
                ),
                encoding="utf-8",
            )

    def touch_transcript(self) -> None:
        self.transcript.write_text("", encoding="utf-8")

    async def __aenter__(self) -> FakeSession:
        self.touch_transcript()

        async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            line = await reader.readline()
            if line:
                frame = json.loads(line)
                self.received.append(frame)
                self.taken.set()
                if self._answer is not None:
                    await self._answer(self, frame)
            writer.close()

        self._server = await asyncio.start_unix_server(serve, path=str(self.path))
        os.chmod(self.path, 0o600)
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    def answers(self, answer) -> FakeSession:
        self._answer = answer
        return self

    # -- the two things a real Session does about a peer message ----------

    def write_user_record(self, request_id: str, *, msg_id: str | None = None) -> None:
        """The between-turns shape: a `user` record carrying both sender-minted ids."""
        self._append(
            {
                "type": "user",
                "message": {"role": "user", "content": "wrapped words"},
                "uuid": request_id,
                "isMeta": True,
                "origin": {"kind": "peer", "msg_id": msg_id or request_id},
            }
        )

    def write_queued_command(self, request_id: str, *, msg_id: str | None = None) -> None:
        """The mid-turn shape: no `user` record is written at all, only an attachment."""
        self._append(
            {
                "type": "attachment",
                "attachment": {
                    "type": "queued_command",
                    "prompt": "wrapped words",
                    "source_uuid": request_id,
                    "origin": {"kind": "peer", "msg_id": msg_id or request_id},
                },
            }
        )

    def _append(self, record: dict[str, object]) -> None:
        with self.transcript.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

    async def send_receipt(self, reply_address: str, **fields: object) -> None:
        """A `peer_message_status` frame, back to whatever address the sender named."""
        path = reply_address.removeprefix("uds:")
        _, writer = await asyncio.open_unix_connection(path)
        writer.write(
            json.dumps({"type": "control", "action": "peer_message_status", **fields}).encode(
                "utf-8"
            )
            + b"\n"
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()


def relaying(home: Path, sink: Sink, **overrides: object) -> tuple[NoticeRelay, ReceiptListener]:
    settings = settings_for(home, **overrides)
    listener = ReceiptListener(settings.peer_socket_directory)
    return (
        NoticeRelay(settings=settings, listener=listener, emit=sink.events.append),
        listener,
    )


# -- what one transcript record says -------------------------------------


class TestReadingBack:
    def test_a_user_record_with_both_ids_agreeing_is_delivery(self) -> None:
        request_id = str(uuid.uuid4())
        record = {
            "type": "user",
            "uuid": request_id,
            "origin": {"kind": "peer", "msg_id": request_id},
        }

        assert readback_in(record, request_id) is Readback.DELIVERED

    def test_a_queued_command_attachment_is_delivery_too(self) -> None:
        """A mid-turn splice writes no `user` record at all. This is the only proof."""
        request_id = str(uuid.uuid4())
        record = {
            "type": "attachment",
            "attachment": {
                "type": "queued_command",
                "source_uuid": request_id,
                "origin": {"msg_id": request_id},
            },
        }

        assert readback_in(record, request_id) is Readback.DELIVERED

    def test_our_id_under_another_msg_id_contradicts_rather_than_proves(self) -> None:
        request_id = str(uuid.uuid4())
        record = {
            "type": "user",
            "uuid": request_id,
            "origin": {"msg_id": str(uuid.uuid4())},
        }

        assert readback_in(record, request_id) is Readback.CONTRADICTED

    def test_a_record_missing_its_origin_entirely_contradicts(self) -> None:
        """A `user` record carrying our uuid but no origin does not attribute it to us."""
        request_id = str(uuid.uuid4())

        record = {"type": "user", "uuid": request_id}

        assert readback_in(record, request_id) is Readback.CONTRADICTED

    def test_somebody_elses_record_says_nothing(self) -> None:
        other = str(uuid.uuid4())
        record = {"type": "user", "uuid": other, "origin": {"msg_id": other}}

        assert readback_in(record, str(uuid.uuid4())) is None

    def test_an_attachment_that_is_not_a_queued_command_says_nothing(self) -> None:
        request_id = str(uuid.uuid4())
        record = {
            "type": "attachment",
            "attachment": {"type": "task-notification", "source_uuid": request_id},
        }

        assert readback_in(record, request_id) is None


# -- the whole Relay -----------------------------------------------------


class TestProvenDelivery:
    def test_the_silent_accept_path_is_proven_by_the_transcript(self, home: Path) -> None:
        """The common configuration sends no receipt at all. Readback is the only proof."""
        sink = Sink()
        request_id = rid()

        async def scenario():
            async def answer(session: FakeSession, frame: dict) -> None:
                session.write_user_record(frame["uuid"])

            relay, listener = relaying(home, sink)
            async with FakeSession(home, pid=os.getpid()).answers(answer) as session:
                try:
                    return await relay.send(
                        target(session.pid), "the build finished", request_id=request_id
                    )
                finally:
                    await relay.aclose()
                    await listener.aclose()

        receipt = asyncio.run(scenario())
        assert receipt.outcome is Delivery.DELIVERED
        assert receipt.request_id == request_id

    def test_a_mid_turn_splice_is_proven_by_the_attachment_shape(self, home: Path) -> None:
        """No `user` record is ever written for these. Checking one shape would time out."""
        sink = Sink()

        async def scenario():
            async def answer(session: FakeSession, frame: dict) -> None:
                session.write_queued_command(frame["uuid"])

            relay, listener = relaying(home, sink)
            async with FakeSession(home, pid=os.getpid()).answers(answer) as session:
                try:
                    return await relay.send(target(session.pid), "it stopped", request_id=rid())
                finally:
                    await relay.aclose()
                    await listener.aclose()

        assert asyncio.run(scenario()).outcome is Delivery.DELIVERED

    def test_a_delivered_receipt_alone_also_proves_it(self, home: Path) -> None:
        """A released hold reports itself, and nothing is written for us to read back."""
        sink = Sink()

        async def scenario():
            relay, listener = relaying(home, sink)

            async def answer(session: FakeSession, frame: dict) -> None:
                await session.send_receipt(
                    frame["from"], status="delivered", orig_msg_id=frame["msg_id"]
                )

            async with FakeSession(home, pid=os.getpid()).answers(answer) as session:
                try:
                    return await relay.send(target(session.pid), "it stopped", request_id=rid())
                finally:
                    await relay.aclose()
                    await listener.aclose()

        assert asyncio.run(scenario()).outcome is Delivery.DELIVERED


class TestRefusingToClaimDelivery:
    def test_no_receipt_and_no_record_is_unknown_and_never_delivered(self, home: Path) -> None:
        """A refusing config may drop with nothing at all. Silence proves nothing."""
        sink = Sink()

        async def scenario():
            relay, listener = relaying(home, sink, readback_timeout_seconds=0.2)
            async with FakeSession(home, pid=os.getpid()) as session:
                try:
                    return await relay.send(target(session.pid), "it stopped", request_id=rid())
                finally:
                    await relay.aclose()
                    await listener.aclose()

        receipt = asyncio.run(scenario())
        assert receipt.outcome is Delivery.UNKNOWN
        assert not receipt.is_delivered
        assert receipt.reason

    def test_a_refused_receipt_is_a_positive_failure_with_its_reason(self, home: Path) -> None:
        """Re-probing the pinned build showed `refuse` does send a receipt after all."""
        sink = Sink()

        async def scenario():
            relay, listener = relaying(home, sink)

            async def answer(session: FakeSession, frame: dict) -> None:
                await session.send_receipt(
                    frame["from"], status="refused", orig_msg_id=frame["msg_id"]
                )

            async with FakeSession(home, pid=os.getpid()).answers(answer) as session:
                try:
                    return await relay.send(target(session.pid), "it stopped", request_id=rid())
                finally:
                    await relay.aclose()
                    await listener.aclose()

        receipt = asyncio.run(scenario())
        assert receipt.outcome is Delivery.FAILED
        assert "refuses inbound peer messages" in receipt.reason

    def test_a_hold_that_then_expires_is_a_failure_with_the_reason(self, home: Path) -> None:
        """A hold is not an answer while the budget runs. What it became is."""
        sink = Sink()

        async def scenario():
            relay, listener = relaying(home, sink)

            async def answer(session: FakeSession, frame: dict) -> None:
                await session.send_receipt(
                    frame["from"], status="held", orig_msg_id=frame["msg_id"]
                )
                await asyncio.sleep(0.1)
                await session.send_receipt(
                    frame["from"], status="expired", orig_msg_id=frame["msg_id"]
                )

            async with FakeSession(home, pid=os.getpid()).answers(answer) as session:
                try:
                    return await relay.send(target(session.pid), "it stopped", request_id=rid())
                finally:
                    await relay.aclose()
                    await listener.aclose()

        receipt = asyncio.run(scenario())
        assert receipt.outcome is Delivery.FAILED
        assert "expired" in receipt.reason

    def test_a_message_still_parked_when_the_budget_runs_out_is_held(self, home: Path) -> None:
        """HELD is its own state: in front of a human, possibly forever, never delivered."""
        sink = Sink()

        async def scenario():
            relay, listener = relaying(home, sink, readback_timeout_seconds=0.3)

            async def answer(session: FakeSession, frame: dict) -> None:
                await session.send_receipt(
                    frame["from"], status="held", orig_msg_id=frame["msg_id"]
                )

            async with FakeSession(home, pid=os.getpid()).answers(answer) as session:
                try:
                    return await relay.send(target(session.pid), "it stopped", request_id=rid())
                finally:
                    await relay.aclose()
                    await listener.aclose()

        receipt = asyncio.run(scenario())
        assert receipt.outcome is Delivery.HELD
        assert not receipt.is_delivered
        assert receipt.reason

    def test_a_contradicted_readback_is_not_delivery(self, home: Path) -> None:
        """Our uuid under somebody else's msg_id disagrees with the attempt."""
        sink = Sink()

        async def scenario():
            relay, listener = relaying(home, sink, readback_timeout_seconds=1.0)

            async def answer(session: FakeSession, frame: dict) -> None:
                session.write_user_record(frame["uuid"], msg_id=str(uuid.uuid4()))

            async with FakeSession(home, pid=os.getpid()).answers(answer) as session:
                try:
                    return await relay.send(target(session.pid), "it stopped", request_id=rid())
                finally:
                    await relay.aclose()
                    await listener.aclose()

        receipt = asyncio.run(scenario())
        assert receipt.outcome is Delivery.UNKNOWN
        assert "contradicts" in receipt.reason


class TestFailingBeforeAnythingLeaves:
    def test_a_session_in_no_registry_is_a_positive_non_delivery(self, home: Path) -> None:
        sink = Sink()

        async def scenario():
            relay, listener = relaying(home, sink)
            try:
                return await relay.send(target(DEAD_PID), "it stopped", request_id=rid())
            finally:
                await relay.aclose()
                await listener.aclose()

        receipt = asyncio.run(scenario())
        assert receipt.outcome is Delivery.FAILED
        assert "not reachable" in receipt.reason

    def test_a_dead_process_is_a_positive_non_delivery(self, home: Path) -> None:
        """The registry outlives the process it describes, so liveness is asked separately."""
        sink = Sink()
        FakeSession(home, pid=DEAD_PID)

        async def scenario():
            relay, listener = relaying(home, sink)
            try:
                return await relay.send(target(DEAD_PID), "it stopped", request_id=rid())
            finally:
                await relay.aclose()
                await listener.aclose()

        receipt = asyncio.run(scenario())
        assert receipt.outcome is Delivery.FAILED
        assert str(DEAD_PID) in receipt.reason

    def test_a_pid_now_owned_by_another_session_is_refused(self, home: Path) -> None:
        """A recycled pid must never inherit the words meant for whoever had it."""
        sink = Sink()

        async def scenario():
            relay, listener = relaying(home, sink)
            async with FakeSession(home, pid=os.getpid(), session_id="who-i-am-now") as session:
                try:
                    return await relay.send(
                        target(session.pid, session_id="who-i-used-to-be"),
                        "it stopped",
                        request_id=rid(),
                    )
                finally:
                    await relay.aclose()
                    await listener.aclose()

        receipt = asyncio.run(scenario())
        assert receipt.outcome is Delivery.FAILED
        assert "who-i-am-now" in receipt.reason

    def test_a_socket_nothing_listens_on_is_a_positive_non_delivery(self, home: Path) -> None:
        sink = Sink()
        session = FakeSession(home, pid=os.getpid())
        abandoned = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        abandoned.bind(str(session.path))
        os.chmod(session.path, 0o600)
        abandoned.close()

        async def scenario():
            relay, listener = relaying(home, sink)
            try:
                return await relay.send(target(session.pid), "it stopped", request_id=rid())
            finally:
                await relay.aclose()
                await listener.aclose()

        receipt = asyncio.run(scenario())
        assert receipt.outcome is Delivery.FAILED
        assert "unreachable" in receipt.reason


class TestAddressingAFork:
    def test_two_pids_under_one_session_id_are_told_apart_by_pid(self, home: Path) -> None:
        """`--resume` forks a second process under the same session id."""
        sink = Sink()
        wanted_pid = os.getpid()

        async def scenario():
            relay, listener = relaying(home, sink)
            # The fork: a second registry record, same session id, different pid,
            # different socket. Only the pid distinguishes them.
            FakeSession(home, pid=DEAD_PID)

            async def answer(session: FakeSession, frame: dict) -> None:
                session.write_user_record(frame["uuid"])

            async with FakeSession(home, pid=wanted_pid).answers(answer) as session:
                try:
                    receipt = await relay.send(target(wanted_pid), "it stopped", request_id=rid())
                    return receipt, session.received
                finally:
                    await relay.aclose()
                    await listener.aclose()

        receipt, received = asyncio.run(scenario())
        assert receipt.outcome is Delivery.DELIVERED
        assert len(received) == 1, "only the addressed fork may be written to"
        assert received[0]["session_id"] == SESSION


class TestALateProof:
    def test_a_proof_that_arrives_after_the_budget_is_still_raised(self, home: Path) -> None:
        """Otherwise Bridge Core re-sends a notice that provably arrived."""
        sink = Sink()
        request_id = rid()

        async def scenario():
            relay, listener = relaying(home, sink, readback_timeout_seconds=0.2)

            async def answer(session: FakeSession, frame: dict) -> None:
                await asyncio.sleep(0.5)
                session.write_user_record(frame["uuid"])

            async with FakeSession(home, pid=os.getpid()).answers(answer) as session:
                try:
                    receipt = await relay.send(
                        target(session.pid), "it stopped", request_id=request_id
                    )
                    await asyncio.sleep(1.0)
                    return receipt
                finally:
                    await relay.aclose()
                    await listener.aclose()

        receipt = asyncio.run(scenario())
        assert receipt.outcome is Delivery.UNKNOWN, "the bounded wait was honestly spent"
        raised = [event for event in sink.events if isinstance(event, RelayReceipt)]
        assert len(raised) == 1
        assert raised[0].receipt.outcome is Delivery.DELIVERED
        assert raised[0].receipt.request_id == request_id

    def test_a_late_non_delivery_is_never_raised(self, home: Path) -> None:
        """Only an upgrade may arrive late; anything else re-grades a recorded attempt."""
        sink = Sink()

        async def scenario():
            relay, listener = relaying(
                home, sink, readback_timeout_seconds=0.2, late_readback_timeout_seconds=0.5
            )

            async def answer(session: FakeSession, frame: dict) -> None:
                await asyncio.sleep(0.3)
                await session.send_receipt(
                    frame["from"], status="denied", orig_msg_id=frame["msg_id"]
                )

            async with FakeSession(home, pid=os.getpid()).answers(answer) as session:
                try:
                    receipt = await relay.send(target(session.pid), "it stopped", request_id=rid())
                    await asyncio.sleep(0.8)
                    return receipt
                finally:
                    await relay.aclose()
                    await listener.aclose()

        receipt = asyncio.run(scenario())
        assert receipt.outcome is Delivery.UNKNOWN
        assert [event for event in sink.events if isinstance(event, RelayReceipt)] == []

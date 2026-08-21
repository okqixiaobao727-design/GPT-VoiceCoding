"""The Session Channel server: the MCP side, the socket side, and the receipt.

The MCP half is checked against the shapes `protocol.py` transcribes from the
implementation proven live against Claude Code 2.1.235, because that is the only
thing that makes a hand-rolled handshake safe (ADR 0006). The whole loop is then
run once as a real subprocess — a real interpreter, real pipes, a real socket —
since the parts that only exist when the process is one are exactly the parts a
unit test cannot reach.
"""

from __future__ import annotations

import asyncio
import io
import itertools
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from gpt_voicecoding import __version__
from gpt_voicecoding.adapters.agent.claude import channel as channel_module
from gpt_voicecoding.adapters.agent.claude.bootstrap import (
    CHANNEL_CONFIG_VARIABLE,
    BootstrapError,
    ChannelBootstrap,
    bootstrap_value,
    read_bootstrap,
    socket_path_in,
)
from gpt_voicecoding.adapters.agent.claude.channel import ChannelServer
from gpt_voicecoding.adapters.agent.claude.privacy import ChannelPathError
from gpt_voicecoding.adapters.agent.claude.protocol import (
    ACKNOWLEDGE_TOOL,
    ACKNOWLEDGED,
    CHANNEL_CAPABILITY,
    CHANNEL_ERROR,
    CHANNEL_NOTIFICATION,
    LATEST_PROTOCOL_VERSION,
    QUEUED,
    SERVER_NAME,
)
from gpt_voicecoding.adapters.agent.claude.settings import ClaudeSettings
from gpt_voicecoding.adapters.agent.claude.wire import ChannelConnection

_names = itertools.count()


@pytest.fixture
def socket_path() -> Iterator[Path]:
    """A private directory short enough to bind, as `AF_UNIX` requires on Darwin."""
    home = Path("/tmp") / f"vc-channel-{next(_names)}-{id(object())}"
    home.mkdir(mode=0o700)
    yield home / "channel.sock"
    shutil.rmtree(home, ignore_errors=True)


def bootstrap(path: Path) -> ChannelBootstrap:
    return ChannelBootstrap(socket_path=path, max_message_bytes=1 << 16, max_text_bytes=64)


def server_of(path: Path) -> tuple[ChannelServer, io.BytesIO]:
    out = io.BytesIO()
    return ChannelServer(bootstrap(path), out=out), out


def pushed(out: io.BytesIO) -> list[dict]:
    return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


class TestTheHandshakeClaudeCodeExpects:
    def test_initialize_declares_the_channel_capability_and_the_receipt_obligation(
        self, socket_path: Path
    ) -> None:
        """The experimental key is what makes this a channel rather than a plain server."""
        server, _ = server_of(socket_path)
        answer = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        assert answer is not None
        result = answer["result"]
        assert result["capabilities"]["experimental"] == {CHANNEL_CAPABILITY: {}}
        assert result["serverInfo"] == {"name": SERVER_NAME, "version": __version__}
        assert ACKNOWLEDGE_TOOL in result["instructions"]

    def test_a_version_the_server_knows_is_echoed_back(self, socket_path: Path) -> None:
        """Negotiation, exactly as the pinned SDK negotiates it."""
        server, _ = server_of(socket_path)
        answer = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            }
        )
        assert answer is not None
        assert answer["result"]["protocolVersion"] == "2025-06-18"

    def test_a_version_the_server_does_not_know_is_answered_with_its_newest(
        self, socket_path: Path
    ) -> None:
        server, _ = server_of(socket_path)
        answer = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "1999-01-01"},
            }
        )
        assert answer is not None
        assert answer["result"]["protocolVersion"] == LATEST_PROTOCOL_VERSION

    def test_the_one_tool_is_declared_the_way_the_reference_declares_it(
        self, socket_path: Path
    ) -> None:
        server, _ = server_of(socket_path)
        answer = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert answer is not None
        tool = answer["result"]["tools"][0]
        assert tool["name"] == ACKNOWLEDGE_TOOL
        assert tool["inputSchema"]["required"] == ["request_id"]
        assert tool["inputSchema"]["additionalProperties"] is False

    def test_a_notification_is_never_answered(self, socket_path: Path) -> None:
        """An answer to something that carried no id would be a protocol error."""
        server, _ = server_of(socket_path)
        assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None

    def test_an_unknown_method_is_answered_with_an_error_rather_than_silence(
        self, socket_path: Path
    ) -> None:
        server, _ = server_of(socket_path)
        answer = server.handle({"jsonrpc": "2.0", "id": 3, "method": "resources/list"})
        assert answer is not None
        assert "Unknown method" in answer["error"]["message"]


class TestTheReceipt:
    def test_acknowledging_a_request_nobody_sent_is_an_error(self, socket_path: Path) -> None:
        """A receipt for words that never came would be the one lie that matters."""
        server, _ = server_of(socket_path)
        answer = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": ACKNOWLEDGE_TOOL, "arguments": {"request_id": "r-1"}},
            }
        )
        assert answer is not None
        assert "No pending request" in answer["error"]["message"]

    def test_a_relay_is_queued_then_pushed_then_acknowledged(self, socket_path: Path) -> None:
        """The whole loop over a real socket, with the session's tool call standing in."""

        async def scenario():
            server, out = server_of(socket_path)
            await server.listen()
            connection = await ChannelConnection.dial(
                socket_path, timeout_seconds=2.0, max_message_bytes=1 << 16
            )
            try:
                await connection.send(
                    {"request_id": "r-1", "kind": "user_message", "text": "ship it"}
                )
                queued = await connection.read_message(timeout_seconds=2.0)
                answer = server.handle(
                    {
                        "jsonrpc": "2.0",
                        "id": 5,
                        "method": "tools/call",
                        "params": {
                            "name": ACKNOWLEDGE_TOOL,
                            "arguments": {"request_id": "r-1"},
                        },
                    }
                )
                acknowledged = await connection.read_message(timeout_seconds=2.0)
                return queued, acknowledged, pushed(out), answer
            finally:
                await connection.aclose()
                await server.aclose()

        queued, acknowledged, notifications, answer = asyncio.run(scenario())
        assert queued == {"type": QUEUED, "request_id": "r-1"}
        assert acknowledged == {"type": ACKNOWLEDGED, "request_id": "r-1"}
        assert notifications[0]["method"] == CHANNEL_NOTIFICATION
        assert notifications[0]["params"]["content"] == "ship it"
        assert notifications[0]["params"]["meta"] == {"request_id": "r-1", "kind": "user_message"}
        assert answer is not None and "Acknowledged r-1" in answer["result"]["content"][0]["text"]

    def test_the_queued_line_is_written_before_the_push(
        self, socket_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A session fast enough to acknowledge inside the push must not overtake it.

        The reference implementation pushed first and wrote the queued line
        after, so a receipt could reach the bridge before the acceptance it
        answers. The order is asserted rather than described, because nothing
        about reading the two lines later can tell which was written first.
        """
        order: list[str] = []

        async def scenario():
            server, _ = server_of(socket_path)
            monkeypatch.setattr(
                channel_module,
                "_write_line",
                lambda writer, message: order.append(str(message.get("type"))),
            )
            monkeypatch.setattr(
                ChannelServer,
                "notify",
                lambda *_arguments: order.append("push"),
            )
            await server.listen()
            reader, writer = await asyncio.open_unix_connection(str(socket_path))
            try:
                writer.write(
                    json.dumps({"request_id": "r-1", "kind": "user_message", "text": "hi"}).encode()
                    + b"\n"
                )
                await writer.drain()
                await asyncio.sleep(0.05)
            finally:
                writer.close()
                await server.aclose()

        asyncio.run(scenario())
        assert order == [QUEUED, "push"]

    def test_a_resend_while_the_first_is_still_waiting_is_queued_again_not_refused(
        self, socket_path: Path
    ) -> None:
        """A refusal here would be read as proof of non-delivery, and it is not.

        Bridge Core resends anything it could not prove landed, including a
        Relay whose acknowledgement simply has not come yet. The first push may
        already be in front of the session, so this must neither push a second
        copy nor claim the words never arrived — and the acknowledgement has to
        reach whichever connection is waiting now.
        """

        async def scenario():
            server, out = server_of(socket_path)
            await server.listen()
            relay = {"request_id": "r-1", "kind": "user_message", "text": "ship it"}
            first = await ChannelConnection.dial(
                socket_path, timeout_seconds=2.0, max_message_bytes=1 << 16
            )
            second = await ChannelConnection.dial(
                socket_path, timeout_seconds=2.0, max_message_bytes=1 << 16
            )
            try:
                await first.send(relay)
                await first.read_message(timeout_seconds=2.0)
                await second.send(relay)
                again = await second.read_message(timeout_seconds=2.0)
                server.handle(
                    {
                        "jsonrpc": "2.0",
                        "id": 7,
                        "method": "tools/call",
                        "params": {"name": ACKNOWLEDGE_TOOL, "arguments": {"request_id": "r-1"}},
                    }
                )
                acknowledged = await second.read_message(timeout_seconds=2.0)
                return again, acknowledged, pushed(out)
            finally:
                await first.aclose()
                await second.aclose()
                await server.aclose()

        again, acknowledged, notifications = asyncio.run(scenario())
        assert again == {"type": QUEUED, "request_id": "r-1"}
        assert acknowledged == {"type": ACKNOWLEDGED, "request_id": "r-1"}
        assert len(notifications) == 1, "the words must not be put in front of the session twice"

    def test_a_resent_relay_is_answered_with_the_same_proof_and_pushed_no_second_time(
        self, socket_path: Path
    ) -> None:
        """Bridge Core resends anything unproven. That must not become two messages."""

        async def scenario():
            server, out = server_of(socket_path)
            await server.listen()
            connection = await ChannelConnection.dial(
                socket_path, timeout_seconds=2.0, max_message_bytes=1 << 16
            )
            try:
                relay = {"request_id": "r-1", "kind": "user_message", "text": "ship it"}
                await connection.send(relay)
                await connection.read_message(timeout_seconds=2.0)
                server.handle(
                    {
                        "jsonrpc": "2.0",
                        "id": 6,
                        "method": "tools/call",
                        "params": {"name": ACKNOWLEDGE_TOOL, "arguments": {"request_id": "r-1"}},
                    }
                )
                await connection.read_message(timeout_seconds=2.0)
                await connection.send(relay)
                again = await connection.read_message(timeout_seconds=2.0)
                return again, pushed(out)
            finally:
                await connection.aclose()
                await server.aclose()

        again, notifications = asyncio.run(scenario())
        assert again == {"type": ACKNOWLEDGED, "request_id": "r-1"}
        assert len(notifications) == 1, "a resend must never become a second real message"


class TestRefusingWhatItCannotCarry:
    @pytest.mark.parametrize(
        "line, expected",
        [
            (b"not json\n", "not JSON"),
            (b'{"request_id":"r-1","kind":"user_message"}\n', "text must be"),
            (b'{"request_id":"","kind":"user_message","text":"hi"}\n', "request_id must be"),
            (b'{"request_id":"r-1","text":"hi"}\n', "kind must name"),
            (
                b'{"request_id":"r-1","kind":"user_message","text":"hi","extra":1}\n',
                "unexpected field",
            ),
        ],
    )
    def test_a_malformed_line_is_refused_in_words(
        self, socket_path: Path, line: bytes, expected: str
    ) -> None:
        async def scenario():
            server, out = server_of(socket_path)
            await server.listen()
            reader, writer = await asyncio.open_unix_connection(str(socket_path))
            try:
                writer.write(line)
                await writer.drain()
                reply = json.loads(await asyncio.wait_for(reader.readline(), timeout=2.0))
                return reply, pushed(out)
            finally:
                writer.close()
                await server.aclose()

        reply, notifications = asyncio.run(scenario())
        assert reply["type"] == CHANNEL_ERROR
        assert expected in reply["message"]
        assert notifications == []

    def test_words_past_the_byte_budget_are_refused_and_never_pushed(
        self, socket_path: Path
    ) -> None:
        """A UTF-8 byte budget, because that is the unit both ends spend."""

        async def scenario():
            server, out = server_of(socket_path)
            await server.listen()
            reader, writer = await asyncio.open_unix_connection(str(socket_path))
            try:
                writer.write(
                    json.dumps(
                        {"request_id": "r-1", "kind": "user_message", "text": "字" * 40},
                        ensure_ascii=False,
                    ).encode("utf-8")
                    + b"\n"
                )
                await writer.drain()
                reply = json.loads(await asyncio.wait_for(reader.readline(), timeout=2.0))
                return reply, pushed(out)
            finally:
                writer.close()
                await server.aclose()

        reply, notifications = asyncio.run(scenario())
        assert reply["type"] == CHANNEL_ERROR and "exceeds 64 bytes" in reply["message"]
        assert notifications == []

    def test_an_existing_path_is_refused_rather_than_replaced(self, socket_path: Path) -> None:
        """Whatever is already there is somebody's, and not this process's to remove."""

        async def scenario():
            socket_path.write_text("not yours")
            server, _ = server_of(socket_path)
            with pytest.raises(ChannelPathError, match="refusing to replace"):
                await server.listen()

        asyncio.run(scenario())

    def test_a_directory_other_accounts_can_enter_is_narrowed_before_anything_binds(
        self, socket_path: Path
    ) -> None:
        async def scenario():
            os.chmod(socket_path.parent, 0o755)
            server, _ = server_of(socket_path)
            await server.listen()
            try:
                return socket_path.parent.stat().st_mode & 0o777, socket_path.stat().st_mode & 0o777
            finally:
                await server.aclose()

        directory, socket = asyncio.run(scenario())
        assert (directory, socket) == (0o700, 0o600)


class TestWhatTheLauncherMustTellIt:
    def test_the_bootstrap_round_trips_through_one_environment_variable(self) -> None:
        settings = ClaudeSettings()
        environ = {CHANNEL_CONFIG_VARIABLE: bootstrap_value(Path("/tmp/x.sock"), settings)}
        read = read_bootstrap(environ)
        assert read.socket_path == Path("/tmp/x.sock")
        assert read.max_message_bytes == settings.max_message_bytes
        assert read.max_text_bytes == settings.max_text_bytes
        assert socket_path_in(environ) == Path("/tmp/x.sock")

    @pytest.mark.parametrize(
        "value, expected",
        [
            ("", "is required"),
            ("{", "is not JSON"),
            ("[]", "must hold a JSON object"),
            ('{"maxMessageBytes":1,"maxTextBytes":1}', "socketPath"),
            ('{"socketPath":"/tmp/x.sock","maxTextBytes":1}', "maxMessageBytes"),
            (
                '{"socketPath":"/tmp/x.sock","maxMessageBytes":0,"maxTextBytes":1}',
                "maxMessageBytes",
            ),
        ],
    )
    def test_a_bootstrap_that_does_not_say_everything_is_refused(
        self, value: str, expected: str
    ) -> None:
        """Fail closed on every field: a default here is a channel nobody can find."""
        with pytest.raises(BootstrapError, match=expected):
            read_bootstrap({CHANNEL_CONFIG_VARIABLE: value})

    def test_a_session_with_no_channel_reports_none_rather_than_raising(self) -> None:
        assert socket_path_in({}) is None


class TestAsARealProcess:
    def test_a_real_interpreter_runs_the_whole_loop(self, socket_path: Path) -> None:
        """One spawn, one handshake, one Relay, one receipt — over real pipes.

        This is what a hand-rolled MCP server has to earn: everything above
        tests the shapes, and this tests that a process built out of them
        actually starts, binds, speaks and answers.
        """
        environment = dict(os.environ)
        environment[CHANNEL_CONFIG_VARIABLE] = json.dumps(
            {"socketPath": str(socket_path), "maxMessageBytes": 1 << 16, "maxTextBytes": 4096}
        )
        child = subprocess.Popen(
            [sys.executable, "-m", "gpt_voicecoding.adapters.agent.claude.channel"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        )
        try:
            assert child.stdin is not None and child.stdout is not None
            _tell(child, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
            handshake = json.loads(child.stdout.readline())
            assert handshake["result"]["capabilities"]["experimental"] == {CHANNEL_CAPABILITY: {}}

            deadline = time.monotonic() + 5.0
            while not socket_path.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert socket_path.exists(), "the channel never bound its socket"

            queued, acknowledged, notification = asyncio.run(_relay(child, socket_path))
            assert queued["type"] == QUEUED
            assert notification["method"] == CHANNEL_NOTIFICATION
            assert notification["params"]["content"] == "ship it"
            assert acknowledged == {"type": ACKNOWLEDGED, "request_id": "r-1"}
        finally:
            child.terminate()
            child.wait(timeout=5)

    def test_a_process_that_cannot_bind_says_why_and_stops(self, socket_path: Path) -> None:
        """A channel that outlived its failure to listen is three healthy-looking lies."""
        environment = dict(os.environ)
        environment[CHANNEL_CONFIG_VARIABLE] = json.dumps(
            {
                "socketPath": str(socket_path.parent / ("x" * 120)),
                "maxMessageBytes": 1 << 16,
                "maxTextBytes": 4096,
            }
        )
        finished = subprocess.run(
            [sys.executable, "-m", "gpt_voicecoding.adapters.agent.claude.channel"],
            input=b"",
            capture_output=True,
            env=environment,
            timeout=10,
        )
        assert finished.returncode == 1
        assert b"may not exceed 103" in finished.stderr


def _tell(child: subprocess.Popen[bytes], message: dict) -> None:
    assert child.stdin is not None
    child.stdin.write(json.dumps(message).encode() + b"\n")
    child.stdin.flush()


async def _relay(child: subprocess.Popen[bytes], socket_path: Path) -> tuple[dict, dict, dict]:
    """Send one Relay in, read the push out, and acknowledge it as the session would."""
    connection = await ChannelConnection.dial(
        socket_path, timeout_seconds=5.0, max_message_bytes=1 << 16
    )
    try:
        await connection.send({"request_id": "r-1", "kind": "user_message", "text": "ship it"})
        queued = await connection.read_message(timeout_seconds=5.0)
        assert child.stdout is not None
        notification = json.loads(child.stdout.readline())
        _tell(
            child,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": ACKNOWLEDGE_TOOL, "arguments": {"request_id": "r-1"}},
            },
        )
        acknowledged = await connection.read_message(timeout_seconds=5.0)
        return queued, acknowledged, notification
    finally:
        await connection.aclose()

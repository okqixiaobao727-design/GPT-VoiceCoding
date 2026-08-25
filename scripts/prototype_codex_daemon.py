#!/usr/bin/env python3
"""PROTOTYPE: prove discovery and reach through Codex's shared local daemon.

Question: can GPT-VoiceCoding discover a hand-started, unwrapped ``codex`` TUI
and positively prove one Relay plus one approval through the same app-server
daemon?  This throwaway probe talks to the daemon's documented WebSocket control
socket using only the Python standard library.  It is evidence for issue 82,
not production transport code.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

APPROVAL_METHODS = frozenset(
    {
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
    }
)


class PrototypeError(RuntimeError):
    """The route failed one of the prototype's explicit proof gates."""


class RemoteRefusal(PrototypeError):
    """Codex returned a JSON-RPC error for a named method."""

    def __init__(self, method: str, message: object) -> None:
        self.method = method
        self.remote_message = str(message)
        super().__init__(f"{method} was refused: {self.remote_message}")


def daemon_info(codex: str, expected_version: str) -> dict[str, Any]:
    completed = subprocess.run(
        [codex, "app-server", "daemon", "version"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise PrototypeError(f"the managed daemon is unavailable: {detail}")
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise PrototypeError("daemon version did not return JSON") from error
    for field in ("cliVersion", "appServerVersion"):
        if result.get(field) != expected_version:
            raise PrototypeError(f"{field} is {result.get(field)!r}, expected {expected_version!r}")
    return result


class DaemonClient:
    """One minimal synchronous JSON-RPC client over a Unix WebSocket."""

    def __init__(self, socket_path: Path, *, timeout_seconds: float) -> None:
        self._socket_path = socket_path
        self._timeout_seconds = timeout_seconds
        self._socket: socket.socket | None = None
        self._next_id = 1
        self._backlog: list[dict[str, Any]] = []

    def __enter__(self) -> DaemonClient:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self._timeout_seconds)
        connection.connect(str(self._socket_path))
        self._socket = connection
        self._handshake()
        initialized = self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "gpt_voicecoding_codex_daemon_prototype",
                    "title": "GPT-VoiceCoding Codex daemon prototype",
                    "version": "0.0.0",
                }
            },
        )
        if not isinstance(initialized.get("codexHome"), str):
            raise PrototypeError("initialize did not identify the daemon's Codex home")
        self.notify("initialized", {})
        return self

    def __exit__(self, *_: object) -> None:
        if self._socket is not None:
            try:
                self._send_frame(b"", opcode=0x8)
            except OSError:
                pass
            self._socket.close()
            self._socket = None

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        self._send_json({"method": method, "id": request_id, "params": params})
        while True:
            message = self._receive_json()
            if message.get("id") != request_id or "method" in message:
                self._backlog.append(message)
                continue
            error = message.get("error")
            if isinstance(error, dict):
                raise RemoteRefusal(method, error.get("message", error))
            result = message.get("result")
            if not isinstance(result, dict):
                raise PrototypeError(f"{method} returned a non-object result")
            return result

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send_json({"method": method, "params": params})

    def respond(self, request_id: object, result: dict[str, Any]) -> None:
        self._send_json({"id": request_id, "result": result})

    def next_message(
        self,
        predicate: Callable[[dict[str, Any]], bool],
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        for index, message in enumerate(self._backlog):
            if predicate(message):
                return self._backlog.pop(index)
        assert self._socket is not None
        previous_timeout = self._socket.gettimeout()
        self._socket.settimeout(timeout_seconds or self._timeout_seconds)
        try:
            while True:
                message = self._receive_json()
                if predicate(message):
                    return message
                self._backlog.append(message)
        except TimeoutError as error:
            raise PrototypeError("timed out waiting for daemon evidence") from error
        finally:
            self._socket.settimeout(previous_timeout)

    def _handshake(self) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            "GET / HTTP/1.1\r\n"
            "Host: localhost\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self._send_all(request.encode("ascii"))
        response = bytearray()
        while b"\r\n\r\n" not in response:
            response.extend(self._receive_exact(1))
            if len(response) > 16_384:
                raise PrototypeError("daemon WebSocket handshake exceeded the header limit")
        header = response.decode("iso-8859-1")
        if not header.startswith("HTTP/1.1 101"):
            raise PrototypeError(
                f"daemon refused the WebSocket handshake: {header.splitlines()[0]}"
            )
        expected = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        ).decode("ascii")
        if f"sec-websocket-accept: {expected}".lower() not in header.lower():
            raise PrototypeError("daemon returned the wrong WebSocket accept token")

    def _send_json(self, message: dict[str, Any]) -> None:
        self._send_frame(json.dumps(message, separators=(",", ":")).encode(), opcode=0x1)

    def _receive_json(self) -> dict[str, Any]:
        payload = self._receive_message()
        try:
            message = json.loads(payload)
        except json.JSONDecodeError as error:
            raise PrototypeError("daemon sent a non-JSON text frame") from error
        if not isinstance(message, dict):
            raise PrototypeError("daemon sent a non-object JSON-RPC message")
        return message

    def _send_frame(self, payload: bytes, *, opcode: int) -> None:
        mask = os.urandom(4)
        length = len(payload)
        header = bytearray([0x80 | opcode])
        if length < 126:
            header.append(0x80 | length)
        elif length < 65_536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        header.extend(mask)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self._send_all(bytes(header) + masked)

    def _receive_message(self) -> str:
        fragments = bytearray()
        while True:
            first, second = self._receive_exact(2)
            final = bool(first & 0x80)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._receive_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._receive_exact(8))[0]
            mask = self._receive_exact(4) if masked else b""
            payload = self._receive_exact(length)
            if masked:
                payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
            if opcode == 0x8:
                raise PrototypeError("daemon closed the WebSocket")
            if opcode == 0x9:
                self._send_frame(payload, opcode=0xA)
                continue
            if opcode not in (0x0, 0x1):
                continue
            fragments.extend(payload)
            if final:
                return fragments.decode("utf-8")

    def _send_all(self, payload: bytes) -> None:
        assert self._socket is not None
        self._socket.sendall(payload)

    def _receive_exact(self, length: int) -> bytes:
        assert self._socket is not None
        chunks = bytearray()
        while len(chunks) < length:
            chunk = self._socket.recv(length - len(chunks))
            if not chunk:
                raise PrototypeError("daemon closed the socket mid-message")
            chunks.extend(chunk)
        return bytes(chunks)


def loaded_threads(client: DaemonClient) -> list[str]:
    result = client.request("thread/loaded/list", {})
    data = result.get("data")
    if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
        raise PrototypeError("thread/loaded/list returned an unexpected shape")
    return data


def describe_thread(client: DaemonClient, thread_id: str) -> dict[str, Any]:
    result = client.request("thread/read", {"threadId": thread_id, "includeTurns": False})
    thread = result.get("thread")
    if not isinstance(thread, dict) or thread.get("id") != thread_id:
        raise PrototypeError(f"thread/read did not return {thread_id}")
    return thread


def resume_thread(
    client: DaemonClient, thread_id: str, *, wait_seconds: float = 0
) -> dict[str, Any]:
    """Resume once, or wait only while Codex says its rollout is not readable yet."""
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            resumed = client.request("thread/resume", {"threadId": thread_id})
        except RemoteRefusal as error:
            not_ready = "no rollout found" in error.remote_message or (
                "failed to read session metadata" in error.remote_message
                and "rollout" in error.remote_message
                and "is empty" in error.remote_message
            )
            if not not_ready or time.monotonic() >= deadline:
                raise
            time.sleep(0.05)
            continue
        thread = resumed.get("thread")
        if not isinstance(thread, dict) or thread.get("id") != thread_id:
            raise PrototypeError("thread/resume returned a different thread")
        return thread


def receipt_count(readback: dict[str, Any], client_message_id: str) -> int:
    thread = readback.get("thread")
    if not isinstance(thread, dict):
        raise PrototypeError("thread/read has no thread object")
    turns = thread.get("turns")
    if not isinstance(turns, list):
        raise PrototypeError("thread/read has no turn list")
    found = 0
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        items = turn.get("items")
        if not isinstance(items, list):
            continue
        found += sum(
            1
            for item in items
            if isinstance(item, dict)
            and item.get("type") == "userMessage"
            and item.get("clientId") == client_message_id
        )
    return found


def print_roster(client: DaemonClient) -> None:
    threads = loaded_threads(client)
    print(f"loaded threads: {len(threads)}")
    for thread_id in threads:
        thread = describe_thread(client, thread_id)
        print(
            json.dumps(
                {
                    "id": thread_id,
                    "name": thread.get("name"),
                    "cwd": thread.get("cwd"),
                    "status": thread.get("status"),
                },
                sort_keys=True,
            )
        )


def prove_relay_and_approval(
    client: DaemonClient, thread_id: str, *, event_timeout_seconds: float
) -> None:
    if thread_id not in loaded_threads(client):
        raise PrototypeError(f"{thread_id} is not loaded on the shared daemon")
    subscribed = True
    try:
        resume_thread(client, thread_id)
    except RemoteRefusal as error:
        if "no rollout found" not in error.remote_message:
            raise
        subscribed = False
        print("subscription deferred: the blank thread has no rollout yet")

    proof_path = Path(tempfile.gettempdir()) / f"codex-daemon-proof-{uuid.uuid4().hex}"
    client_message_id = f"codex-daemon-prototype-{uuid.uuid4()}"
    print(f"Relay client id: {client_message_id}")
    prompt = (
        "This is a transport proof. Use the shell exactly once to run "
        f"`printf codex-daemon-approval-proof > {proof_path}`. "
        "Do not use any other tool and reply with only `proof complete`."
    )
    started = client.request(
        "turn/start",
        {
            "threadId": thread_id,
            "clientUserMessageId": client_message_id,
            "approvalPolicy": "on-request",
            "approvalsReviewer": "user",
            "sandboxPolicy": {"type": "readOnly"},
            "input": [{"type": "text", "text": prompt}],
        },
    )
    turn = started.get("turn")
    if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
        raise PrototypeError("turn/start did not return a turn id")
    turn_id = turn["id"]
    print(f"turn accepted: {turn_id}")

    if not subscribed:
        resume_thread(client, thread_id, wait_seconds=event_timeout_seconds)
        print("subscription established after the first turn created a rollout")

    approval = client.next_message(
        lambda message: (
            message.get("method") in APPROVAL_METHODS
            and isinstance(message.get("params"), dict)
            and message["params"].get("threadId") == thread_id
        ),
        timeout_seconds=event_timeout_seconds,
    )
    approval_id = approval.get("id")
    if approval_id is None:
        raise PrototypeError("approval request has no JSON-RPC id")
    print(f"approval observed: {approval.get('method')} id={approval_id}")
    client.respond(approval_id, {"decision": "accept"})

    client.next_message(
        lambda message: (
            message.get("method") == "serverRequest/resolved"
            and isinstance(message.get("params"), dict)
            and message["params"].get("requestId") == approval_id
        ),
        timeout_seconds=event_timeout_seconds,
    )
    print("approval receipt: serverRequest/resolved")

    client.next_message(
        lambda message: (
            message.get("method") == "turn/completed"
            and isinstance(message.get("params"), dict)
            and isinstance(message["params"].get("turn"), dict)
            and message["params"]["turn"].get("id") == turn_id
        ),
        timeout_seconds=event_timeout_seconds,
    )
    readback = client.request("thread/read", {"threadId": thread_id, "includeTurns": True})
    copies = receipt_count(readback, client_message_id)
    print(f"relay readback copies: {copies}")
    if copies != 1:
        raise PrototypeError(f"Relay receipt requires exactly one readback copy, found {copies}")
    if proof_path.read_text() != "codex-daemon-approval-proof":
        raise PrototypeError("the approved command did not create the expected proof")
    proof_path.unlink()
    print("verdict: Relay DELIVERED; approval DELIVERED")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("roster", "prove"))
    parser.add_argument(
        "--expected-version",
        required=True,
        help="ticket-pinned Codex CLI and daemon version",
    )
    parser.add_argument("--thread", help="loaded daemon thread id for the prove action")
    parser.add_argument("--timeout", type=float, default=90.0)
    return parser.parse_args()


def main() -> int:
    arguments = parse_args()
    codex = shutil.which("codex")
    if codex is None:
        raise PrototypeError("codex is not on PATH")
    info = daemon_info(codex, arguments.expected_version)
    print(
        json.dumps(
            {
                "codex": codex,
                "cliVersion": info["cliVersion"],
                "appServerVersion": info["appServerVersion"],
                "socketPath": info["socketPath"],
            },
            sort_keys=True,
        )
    )
    with DaemonClient(Path(info["socketPath"]), timeout_seconds=arguments.timeout) as client:
        if arguments.action == "roster":
            print_roster(client)
        else:
            if not arguments.thread:
                raise PrototypeError("--thread is required for the prove action")
            prove_relay_and_approval(
                client, arguments.thread, event_timeout_seconds=arguments.timeout
            )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PrototypeError as error:
        print(f"verdict: FAILED; reason: {error}", file=sys.stderr)
        raise SystemExit(1) from None

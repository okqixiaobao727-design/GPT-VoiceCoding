"""Claude-lane Relay probe for #71 — one message into one throwaway Session.

This is the half `claude_lane_probe.py` refuses to do: it opens a Session's inbox
socket and writes to it. Anything written there lands in a real conversation, so
the target is constrained structurally rather than by care:

    a Session is targetable only if its cwd is under SANDBOX.

Every Session the user actually works in fails that test, so the accident that
#70's probe had — a probe message reaching two real Sessions — cannot repeat.
You start the target yourself:

    mkdir -p /tmp/gptvc-71-probe && cd /tmp/gptvc-71-probe && command claude

`command` is what makes it a *bare* claude: this machine's `~/.zshrc:174` still
defines a gen-1 shell function that would otherwise wrap it.

What the probe establishes, and how it knows:

The inbox protocol is newline-delimited JSON, and Claude Code documents it to
itself — 2.1.245 logs, at `[uds-messaging] Inject messages`, the exact frame pair
`{"type":"auth","token":...}` then `{"type":"user","message":{"role":"user",
"content":"hello"}}`. Its `type:"control"` handler accepts `rename`,
`peer_message_status`, `notify_when_idle` and `peer_idle_notice`, and it answers
a sender with `peer_message_status` — statuses `delivered`, `held`, `denied`,
`expired`, `refused`, `dropped` — but only when the sender's reply address is
`uds:<path>` *inside the receiver's own socket namespace*. So the probe binds its
own socket beside the target's and listens there: a receipt is the proof, and
`delivered` is upstream's own word for it, not one this product invented.

    python3 scripts/claude_lane_relay_probe.py --list
    python3 scripts/claude_lane_relay_probe.py --target-pid <pid>
    python3 scripts/claude_lane_relay_probe.py --target-pid <pid> --notify-idle

Version pin: Claude Code 2.1.245 on macOS, 2026-08-25.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

#: The only cwd a target may live under. This is the safety boundary.
#: Resolved, because macOS reports `/tmp` back as `/private/tmp` and a boundary
#: that can be walked around by a symlink is not a boundary.
SANDBOX = Path("/tmp/gptvc-71-probe").resolve()

#: Claude Code's own per-Session registry. Undocumented, and — as this probe
#: found — the only surface that lists every live Session: `claude agents --json`
#: omitted a freshly started, bare, same-version interactive Session that had a
#: bound socket and a complete entry here. Each entry carries that Session's real
#: `messagingSocketPath`, which is the path the product must use: 2.1.245 derives
#: the socket directory from `CLAUDE_CODE_TMPDIR` or `$XDG_RUNTIME_DIR` and accepts
#: `--messaging-socket-path`, so a built path is a guess and a read path is a fact.
REGISTRY_DIR = (
    Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")).expanduser() / "sessions"
)


def registry() -> list[dict]:
    entries = []
    for path in sorted(REGISTRY_DIR.glob("*.json")):
        try:
            entries.append(json.loads(path.read_text()))
        except (OSError, json.JSONDecodeError):
            continue
    return entries


def own_proc_start() -> str:
    """This process's start time, in the shape Claude Code publishes.

    The receiver checks a reply target's key file against the real start time of
    the pid that published it — a pid outlives nothing but a pid, so a stale key
    must not be trusted. Matching the shape matters twice over: `ps` prints
    `Tue 25 Aug ...` under this machine's locale but `Tue Aug 25 ...` under C, and
    2.1.245 writes the hour on a *12-hour* clock with no AM/PM (a Session started
    at 21:57 is published as `09:57:51`). Both differences were found by
    comparing against a live Session's own key file rather than assumed.
    """
    printed = subprocess.run(
        ["ps", "-p", str(os.getpid()), "-o", "lstart="],
        capture_output=True,
        text=True,
        env={**os.environ, "LC_ALL": "C"},
    ).stdout.strip()
    started = datetime.strptime(printed, "%a %b %d %H:%M:%S %Y")
    return started.strftime("%a %b %d %I:%M:%S %Y")


def peer_token(pid: int, proc_start: str | None) -> str | None:
    """The Session's inbox token, published beside its registry entry.

    A receipt is only sent to a sender the receiver treats as a *peer*, and an
    unauthenticated connection is not one — the first Relay this probe sent was
    delivered and answered by the model, yet drew no `peer_message_status` at
    all. The token that fixes that is on disk in `<pid>.<hash>.key`, readable by
    the owning user, so the product can authenticate without a hook and without
    the Session's environment. The file carries `procStart` alongside it; the
    probe checks it, because a pid outlives nothing but a pid.
    """
    for path in REGISTRY_DIR.glob(f"{pid}.*.key"):
        try:
            published = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if proc_start and published.get("procStart") != proc_start:
            continue
        token = published.get("peerToken")
        if isinstance(token, str):
            return token
    return None


def alive(pid: int) -> bool:
    probe = subprocess.run(["ps", "-p", str(pid), "-o", "pid="], capture_output=True, text=True)
    return bool(probe.stdout.strip())


def sandbox_targets() -> list[dict]:
    """Live Sessions inside the sandbox, with the socket path they registered."""
    found = []
    for entry in registry():
        cwd = Path(entry.get("cwd", "/nonexistent")).resolve()
        if cwd != SANDBOX and SANDBOX not in cwd.parents:
            continue
        if not alive(entry["pid"]) or not entry.get("messagingSocketPath"):
            continue
        found.append(
            dict(
                entry,
                socket=entry["messagingSocketPath"],
                token=peer_token(entry["pid"], entry.get("procStart")),
            )
        )
    return found


def resolve_target(pid: int) -> dict:
    for agent in sandbox_targets():
        if agent["pid"] == pid:
            return agent
    raise SystemExit(
        f"pid {pid} is not a Session under {SANDBOX}. The probe only talks to Sessions "
        f"it cannot mistake for your real work. Start one with:\n"
        f"    mkdir -p {SANDBOX} && cd {SANDBOX} && command claude"
    )


class ReplyInbox:
    """Our own socket, beside the target's, so receipts pass its namespace check."""

    #: How Claude Code names a published inbox key: `<pid>.<sha256(path)>.key`,
    #: verified against a live Session's own key file on 2.1.245. The receiver
    #: resolves a sender's reply address the same way round — path to hash to
    #: key file to owning pid — which is where `verifiedPeerPid` comes from, and
    #: no `verifiedPeerPid` means no receipt. Publishing a key is therefore how a
    #: non-Session participates in cross-session messaging as a first-class peer.
    #: It is only a key: no `<pid>.json`, so nothing appears in any Session roster.

    def __init__(self, target_socket: str) -> None:
        # Beside the target's socket: the receiver only sends a receipt to a
        # reply address inside its own socket namespace.
        self.path = Path(target_socket).parent / f"{os.getpid()}.sock"
        self.address = f"uds:{self.path}"
        self.frames: list[dict] = []
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.path.unlink(missing_ok=True)
        self._server.bind(str(self.path))
        self.path.chmod(0o600)
        self._server.listen(8)
        self._server.settimeout(0.5)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        self.token = secrets.token_hex(16)
        self.key_path = self._publish_key()

    def _publish_key(self) -> Path:
        digest = hashlib.sha256(str(self.path).encode()).hexdigest()
        key_path = REGISTRY_DIR / f"{os.getpid()}.{digest}.key"
        proc_start = own_proc_start()
        key_path.write_text(
            json.dumps(
                {
                    "peerToken": self.token,
                    "procStart": proc_start,
                }
            )
        )
        key_path.chmod(0o600)
        return key_path

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except (TimeoutError, OSError):
                continue
            with conn:
                conn.settimeout(2.0)
                buffer = b""
                try:
                    while chunk := conn.recv(65536):
                        buffer += chunk
                except (TimeoutError, OSError):
                    pass
                for line in buffer.decode("utf-8", "replace").splitlines():
                    if line.strip():
                        try:
                            self.frames.append(json.loads(line))
                        except json.JSONDecodeError:
                            self.frames.append({"unparsed": line})

    #: Statuses that end a message's life. `held` is not one of them: a held
    #: message is waiting on the receiving user, and settles later as `delivered`
    #: or `denied`. A probe that stops at `held` never sees how it ended.
    TERMINAL = ("delivered", "denied", "expired", "refused", "dropped")

    def wait_for(self, action: str, timeout: float) -> dict | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for frame in self.frames:
                if frame.get("action") == action:
                    return frame
            time.sleep(0.1)
        return None

    def wait_for_settlement(self, msg_id: str, timeout: float) -> list[dict]:
        """Every status for one message, until it reaches a terminal one."""
        deadline = time.monotonic() + timeout
        seen: list[dict] = []
        while time.monotonic() < deadline:
            for frame in self.frames:
                if frame.get("action") != "peer_message_status":
                    continue
                if frame.get("orig_msg_id") != msg_id or frame in seen:
                    continue
                seen.append(frame)
                if frame.get("status") in self.TERMINAL:
                    return seen
            time.sleep(0.2)
        return seen

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self._server.close()
        self.path.unlink(missing_ok=True)
        self.key_path.unlink(missing_ok=True)


def send(socket_path: str, frames: list[dict]) -> None:
    """One connection, one complete line per frame.

    2.1.245 closes a connection that sends no complete line inside its
    first-line deadline, so every frame is written whole and immediately.
    """
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(5.0)
    client.connect(socket_path)
    with client:
        for frame in frames:
            client.sendall((json.dumps(frame) + "\n").encode("utf-8"))


def auth_line(target: dict, inbox: ReplyInbox, mode: str) -> list[dict]:
    """The auth frame — and *whose* token it carries is the open question.

    Delivery does not need it: the first probe authenticated nothing and the
    model still answered. A receipt does, because the receiver only replies to a
    sender whose pid it verified. Two readings of the contract, both testable:

    `target`  the receiver's own published token, proving we may speak to it;
    `self`    our own published token, letting the receiver resolve *us* — it
              looks a token up by finding the `<pid>.<sha256(path)>.key` file
              that published it, which yields a pid, which is exactly the shape
              of the `verifiedPeerPid` it wants.
    """
    token = {"target": target.get("token"), "self": inbox.token}.get(mode)
    return [{"type": "auth", "token": token}] if token else []


def relay(
    target: dict, text: str, inbox: ReplyInbox, timeout: float, auth: str, from_mode: str
) -> None:
    # `msgV` is the protocol version the receiver stamps on its own frames, and
    # `msg_id` is validated against a UUID pattern — an id of our own shape is
    # dropped from the origin record, which is how the first probes lost theirs.
    msg_id = str(uuid.uuid4())
    frame = {
        "type": "user",
        "msgV": 1,
        "message": {"role": "user", "content": text},
        "from": inbox.address,
        "msg_id": msg_id,
        **({"from_mode": from_mode} if from_mode else {}),
    }
    print(f"→ Relay to pid {target['pid']} ({target['name']}), msg_id {msg_id}")
    print(f"  frame: {json.dumps(frame)}")
    lines = auth_line(target, inbox, auth)
    send(target["socket"], lines + [frame])
    print(
        f"  socket write accepted at {time.strftime('%H:%M:%S')} (auth={auth}, sent={bool(lines)})"
    )
    receipts = inbox.wait_for_settlement(msg_id, timeout)
    if not receipts:
        print(
            f"  no peer_message_status within {timeout:.0f}s — on this route a socket "
            "write that is accepted is NOT evidence of delivery"
        )
    for receipt in receipts:
        print(f"  receipt: {receipt['status']} — {json.dumps(receipt)}")
    if receipts and receipts[-1]["status"] not in ReplyInbox.TERMINAL:
        print(f"  still unsettled after {timeout:.0f}s: the receiving user has not answered")


def notify_idle(target: dict, inbox: ReplyInbox, timeout: float, auth: str) -> None:
    msg_id = str(uuid.uuid4())
    frame = {
        "type": "control",
        "msgV": 1,
        "action": "notify_when_idle",
        "from": inbox.address,
        "msg_id": msg_id,
        "from_mode": "default",
    }
    print(f"→ notify_when_idle to pid {target['pid']}, msg_id {msg_id}")
    send(target["socket"], auth_line(target, inbox, auth) + [frame])
    notice = inbox.wait_for("peer_idle_notice", timeout)
    if notice is None:
        print(f"  no peer_idle_notice within {timeout:.0f}s")
    else:
        print(f"  notice: {json.dumps(notice)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="show targetable Sessions and exit")
    parser.add_argument("--target-pid", type=int)
    parser.add_argument("--text", default="Probe for GPT-VoiceCoding issue 71. No action needed.")
    parser.add_argument(
        "--notify-idle", action="store_true", help="also ask to be told when it goes idle"
    )
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--auth", choices=("target", "self", "none"), default="self")
    parser.add_argument(
        "--from-mode",
        default="default",
        help="permission mode to attest; empty string attests nothing, which is what a "
        "bypassPermissions receiver holds messages for",
    )
    args = parser.parse_args()

    if args.list or args.target_pid is None:
        targets = sandbox_targets()
        if not targets:
            print(f"No Session under {SANDBOX}. Start one with:")
            print(f"    mkdir -p {SANDBOX} && cd {SANDBOX} && command claude")
            sys.exit(1)
        for agent in targets:
            print(
                f"pid {agent['pid']}  {agent['name']}  "
                f"status={agent['status']}  {agent['socket']}  "
                f"token={'yes' if agent.get('token') else 'no'}"
            )
        sys.exit(0)

    target = resolve_target(args.target_pid)
    inbox = ReplyInbox(target["socket"])
    print(f"reply inbox bound at {inbox.address}")
    print(f"inbox key published at {inbox.key_path}\n")
    try:
        relay(target, args.text, inbox, args.timeout, args.auth, args.from_mode)
        if args.notify_idle:
            print()
            notify_idle(target, inbox, args.timeout, args.auth)
        print(f"\nall frames received on our inbox: {json.dumps(inbox.frames, indent=2)}")
    finally:
        inbox.close()


if __name__ == "__main__":
    main()

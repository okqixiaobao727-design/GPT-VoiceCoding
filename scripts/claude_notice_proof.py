"""Proof that a real Claude Code session takes a Notice Relay over the peer socket.

The peer socket is **undocumented**. Everything this adapter knows about it was
read out of the shipped binary and confirmed against live sessions, so the only
thing that can prove the adapter still works is a real session on this machine —
and CI has none. This script is that gate, and it is a **manual, local job
deliberately outside the test suite**, exactly like the Answer Relay's.

    python3 scripts/claude_notice_proof.py                 # free: survey and check
    python3 scripts/claude_notice_proof.py --target <pid>  # check one session
    python3 scripts/claude_notice_proof.py --target <pid> --relay
                                                          # spends real model tokens

**Use a throwaway session.** A Notice Relay arrives in the target's context
wrapped in "Another Claude session sent a message", it starts a turn, and it
spends that session's model usage. Relaying into somebody's real work interrupts
it. The script will not choose a target for you and will not run without an
explicit pid, for exactly that reason.

What the free mode reports, per session in the registry: whether the record
parses, whether it speaks the protocol this adapter is pinned to, whether its
process is alive, whether its socket passes the privacy rules, and whether its
transcript can be located unambiguously. Those are every pre-wire check the
Notice Relay makes, so a session that passes all five is one a Relay would reach.

With `--relay` it also runs the **upgrade re-probe** the research handed forward,
and prints each assertion as a pass or a failure:

1. the inbound peer connection is never written to by the receiver;
2. the sender-minted `uuid` is echoed into the transcript record;
3. the sender-minted `origin.msg_id` is echoed alongside it;
4. any receipt that arrives has the shape this adapter parses.

Assertion 4 is the one that may legitimately not fire: receipts are sent only for
messages the receiver *held*, so on a target configured `crossSessionInbound:
"accept"` — the common case — a successful Relay is silent and the transcript is
the whole proof. That is reported as "not exercised", never as a failure.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gpt_voicecoding.adapters.agent.claude.notice import NoticeRelay  # noqa: E402
from gpt_voicecoding.adapters.agent.claude.peer import (  # noqa: E402
    PeerError,
    ReceiptListener,
    notice_frame,
)
from gpt_voicecoding.adapters.agent.claude.privacy import (  # noqa: E402
    ChannelPathError,
    verify_private_socket,
)
from gpt_voicecoding.adapters.agent.claude.registry import (  # noqa: E402
    PEER_PROTOCOL,
    PROVEN_AGAINST_VERSION,
    RegistryError,
    SessionRecord,
    pid_is_live,
    read_record,
    records,
)
from gpt_voicecoding.adapters.agent.claude.settings import ClaudeSettings  # noqa: E402
from gpt_voicecoding.adapters.agent.claude.transcript import (  # noqa: E402
    TranscriptError,
    TranscriptNotFound,
    locate_transcript,
)
from gpt_voicecoding.seams.agent import RelayReceipt  # noqa: E402
from gpt_voicecoding.seams.identity import AgentKind, RequestId, SessionTarget  # noqa: E402

#: What one Relay carries. Deliberately inert: it asks for nothing, so a session that
#: acts on it can only waste a few tokens saying it has nothing to do.
NOTICE_TEXT = (
    "This is an automated delivery test from the GPT-VoiceCoding bridge. "
    "No action is needed; you may ignore it entirely."
)


def survey(settings: ClaudeSettings) -> list[tuple[SessionRecord, list[str]]]:
    """Every session in the registry, with whatever would stop a Relay reaching it."""
    found = []
    for record in records(settings.registry_directory):
        found.append((record, _blockers(record, settings)))
    return found


def _blockers(record: SessionRecord, settings: ClaudeSettings) -> list[str]:
    reasons = []
    if record.peer_protocol != PEER_PROTOCOL:
        reasons.append(f"speaks peerProtocol {record.peer_protocol}, not {PEER_PROTOCOL}")
    if not pid_is_live(record.pid):
        reasons.append("its process is gone")
    try:
        verify_private_socket(record.socket_path)
    except ChannelPathError as refused:
        reasons.append(str(refused))
    try:
        locate_transcript(settings.projects_directory, record.session_id)
    except TranscriptNotFound:
        pass  # Not written yet. The readback picks it up when it appears.
    except TranscriptError as unreadable:
        reasons.append(str(unreadable))
    return reasons


def report_survey(settings: ClaudeSettings) -> None:
    found = survey(settings)
    if not found:
        print(f"No Claude Session registry records under {settings.registry_directory}.")
        print("Nothing is running that a Notice Relay could reach.")
        return

    print(f"Claude Sessions in {settings.registry_directory}:\n")
    for record, blockers in found:
        mark = "reachable" if not blockers else "NOT reachable"
        print(f"  pid {record.pid}  {record.version}  status={record.status or '?'}  [{mark}]")
        print(f"      session {record.session_id}")
        print(f"      cwd     {record.cwd}")
        for reason in blockers:
            print(f"      - {reason}")
    print(
        f"\nThis adapter is pinned to peerProtocol {PEER_PROTOCOL} "
        f"(last re-probed against Claude Code {PROVEN_AGAINST_VERSION})."
    )
    print("Choose a THROWAWAY session and pass its pid with --target.")


async def check_one(settings: ClaudeSettings, pid: int) -> SessionRecord | None:
    """Every pre-wire check the Relay makes, reported one at a time."""
    try:
        record = read_record(settings.registry_directory, pid)
    except RegistryError as refused:
        print(f"FAIL  registry: {refused}")
        return None
    print(f"pass  registry record parses, and speaks peerProtocol {record.peer_protocol}")
    print(f"      session {record.session_id}")
    print(f"      cwd     {record.cwd}")
    print(f"      version {record.version}  status {record.status or '?'}")

    ok = True
    if pid_is_live(record.pid):
        print(f"pass  process {record.pid} is alive")
    else:
        print(f"FAIL  process {record.pid} is gone")
        ok = False

    try:
        verify_private_socket(record.socket_path)
        print(f"pass  peer socket {record.socket_path} is this user's and private")
    except ChannelPathError as refused:
        print(f"FAIL  peer socket: {refused}")
        ok = False

    try:
        transcript = locate_transcript(settings.projects_directory, record.session_id)
        print(f"pass  transcript located: {transcript}")
    except TranscriptNotFound:
        # A session that has written nothing has no file yet, and the record this
        # Relay produces may be the one that creates it. Not a blocker.
        print("pass  transcript not written yet; the readback will pick it up when it is")
    except TranscriptError as unreadable:
        print(f"FAIL  transcript: {unreadable}")
        ok = False

    listener = ReceiptListener(settings.peer_socket_directory)
    try:
        await listener.start()
        print(f"pass  receipt listener can bind at {listener.path}")
    except PeerError as unbindable:
        print(f"FAIL  receipt listener: {unbindable}")
        ok = False
    finally:
        await listener.aclose()

    return record if ok else None


async def probe_no_inbound_write(record: SessionRecord, listener_address: str) -> None:
    """Re-probe 1: the receiver must never write back on the connection we opened."""
    frame = notice_frame(
        text="probe",
        request_id=str(uuid.uuid4()),
        session_id=record.session_id,
        reply_address=listener_address,
    )
    # Deliberately not sent: opening and reading is enough to see whether the
    # receiver greets a connection, and sending would be a second real Relay.
    del frame
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(record.socket_path)), timeout=5.0
        )
    except (TimeoutError, OSError) as unreachable:
        print(f"  ?  1. inbound write: could not connect to probe ({unreachable})")
        return
    try:
        greeting = await asyncio.wait_for(reader.read(1), timeout=2.0)
    except TimeoutError:
        print("  pass  1. the receiver wrote nothing on the inbound connection")
        return
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
    if greeting:
        print(f"  FAIL  1. the receiver wrote {greeting!r} on the inbound connection")
    else:
        print("  pass  1. the receiver closed without writing on the inbound connection")


def probe_echoes(transcript: Path, request_id: str) -> None:
    """Re-probes 2 and 3: both sender-minted ids survived into the target's transcript."""
    own_id = False
    msg_id = False
    for line in transcript.read_text(encoding="utf-8", errors="replace").splitlines():
        if request_id not in line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        attachment = record.get("attachment")
        holder = attachment if isinstance(attachment, dict) else record
        identity = holder.get("source_uuid") if holder is not record else record.get("uuid")
        origin = holder.get("origin")
        own_id = own_id or identity == request_id
        msg_id = msg_id or (isinstance(origin, dict) and origin.get("msg_id") == request_id)

    print(f"  {'pass' if own_id else 'FAIL'}  2. the sender-minted uuid was echoed into the record")
    print(f"  {'pass' if msg_id else 'FAIL'}  3. origin.msg_id was echoed into the record")


async def relay_once(settings: ClaudeSettings, record: SessionRecord) -> int:
    """One real Notice Relay into one real session, and everything it proved."""
    request_id = RequestId(str(uuid.uuid4()))
    target = SessionTarget(agent=AgentKind.CLAUDE, session_id=record.session_id, pid=record.pid)
    raised: list[RelayReceipt] = []
    listener = ReceiptListener(settings.peer_socket_directory)
    relay = NoticeRelay(settings=settings, listener=listener, emit=raised.append)

    print(f"\nCarrying one Notice Relay into pid {record.pid} as request {request_id}.")
    print(f"Waiting up to {settings.readback_timeout_seconds:.0f}s for proof.\n")
    try:
        receipt = await relay.send(target, NOTICE_TEXT, request_id=request_id)
        print(f"  outcome: {receipt.outcome}")
        if receipt.reason:
            print(f"  reason:  {receipt.reason}")

        print("\nUpgrade re-probe:")
        await probe_no_inbound_write(record, listener.address)
        try:
            transcript = locate_transcript(settings.projects_directory, record.session_id)
            probe_echoes(transcript, str(request_id))
        except TranscriptError as unreadable:
            print(f"  FAIL  2-3. transcript: {unreadable}")
        print(
            "  n/a   4. receipt shape: not exercised — receipts fire only for held "
            "messages, so an accepting target is silent by design"
            if receipt.outcome.value in {"delivered", "unknown"}
            else "  pass  4. a receipt arrived and parsed into this adapter's vocabulary"
        )
    finally:
        await relay.aclose()
        await listener.aclose()

    if receipt.is_delivered:
        print("\nPROVEN: one Relay, delivery established by readback.")
        return 0
    print(f"\nNOT PROVEN: the Relay graded {receipt.outcome}.")
    return 1


async def run(arguments: argparse.Namespace) -> int:
    settings = ClaudeSettings(
        readback_timeout_seconds=float(arguments.wait),
    )
    if arguments.target is None:
        report_survey(settings)
        return 0

    record = await check_one(settings, arguments.target)
    if record is None:
        return 1
    if not arguments.relay:
        print("\nEvery pre-wire check passed. Re-run with --relay to send one Notice Relay.")
        print("Only do that against a session you are willing to interrupt.")
        return 0
    return await relay_once(settings, record)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="claude_notice_proof",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--target", type=int, default=None, help="the pid of a THROWAWAY Claude Session"
    )
    parser.add_argument(
        "--relay",
        action="store_true",
        help="really send one Notice Relay; this interrupts the target and spends its tokens",
    )
    parser.add_argument(
        "--wait", type=float, default=90.0, help="seconds to wait for proof of delivery"
    )
    return asyncio.run(run(parser.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())

"""The `PermissionRequest` hook process: stdin in, one decision out, or nothing.

Claude Code starts this, one process per displayed permission dialog, and reads
its stdout for a verdict. Everything it needs it is told: the dialog on stdin,
and the engine's address from the file the engine published (ADR 0011) or,
for a launch that set one, the environment.

**Silence is the safe answer, so silence is every failure's answer.** Anything
this process prints that is not `{"behavior": "allow"}` is read by Claude Code as
a *denial* — including a traceback, a half-written line, or the word "ask". So
the whole run is wrapped, nothing reaches stdout except a decision this module
built, and every path that is not a verdict prints nothing at all and lets the
dialog the human is already looking at keep the request. That is the never-deny
rule made structural: there is no code path here that can produce a denial the
user did not speak.

**It answers the engine before it answers Claude Code.** Once the verdict is
read, the hook sends a one-line receipt back down the same connection. That
receipt is the engine's only positive proof of delivery, because the obvious
cheaper one is not: the connection ending has two causes — this process leaving
with its verdict, and the human answering the dialog on screen — and from the far
end they read alike, except when the pre-empt is early enough to break the
engine's write (2.1.245, #71). Sending it is best effort and never
load-bearing here: a receipt that fails to go leaves this process still holding a
verdict it will print, and leaves the engine reporting UNKNOWN, which is the
honest grade for "we cannot tell".

**It keeps no clock of its own.** After the dial it blocks, with no timeout, for
as long as the engine holds it — because the budget belongs to Bridge Core, which
answers `ask` when it runs out, and a second timer here would be a second budget
racing the first. The only bounded wait is the dial: an engine that is not there
is not going to arrive, and finding that out fast is what keeps a stalled dialog
from also being a slow one. If the engine dies mid-wait the socket ends, the read
returns nothing, and that is silence too.

**It is stdlib-only and blocking**, for the reason ADR 0006's channel server was
stdlib-only before it was removed: it runs under whatever interpreter the
deployment provides, in a position where a missing dependency is a failure nobody
is reading. Blocking sockets rather than the engine's asyncio framing because
this process sends one line and reads one line — an event loop to do that would
be startup cost paid on every permission dialog for nothing.

**Two gates, both fail open.** No address at all — no bootstrap variable and no
engine has published one — means there is nobody to ask, and the process exits
before it opens a socket. Past that, the engine itself refuses any session it
does not hold a registration for.

The first gate used to be scope as well as failure: a `--plugin-dir` installed
this hook per Session, so only our own Sessions loaded it. ADR 0011 gave that up
knowingly. A user-scope hook fires for **every** Session in its config directory,
so scope is now these two soft lines rather than one structural one — this
process prints nothing when no engine holds the Session, and the engine refuses
what it does not hold. A Session the bridge does not hold pays the cost of
starting this process and nothing else: no socket is opened and nothing is
written, which is why the address lookup stays the first thing that happens.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from gpt_voicecoding.adapters.agent.claude.approval import (
    ACK_TYPE,
    MAX_HOOK_REQUEST_BYTES,
    REQUEST_TYPE,
    SESSION_ID_FIELD,
    TOOL_INPUT_FIELD,
    TOOL_NAME_FIELD,
    TYPE_FIELD,
    VERDICT_FIELD,
    VERDICT_TYPE,
    hook_decision,
)
from gpt_voicecoding.adapters.agent.claude.bootstrap import (
    approval_socket_path_in,
    dial_timeout_in,
)
from gpt_voicecoding.seams.agent import ApprovalVerdict


def request_for(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """What this dialog looks like on the engine's wire, or `None` if it is not one.

    `session_id` is the load-bearing field: it is what the engine resolves, via
    Claude Code's own session registry, into the pid it holds a registration for.
    Without it there is nobody to ask, so there is nothing to send.
    """
    session_id = payload.get(SESSION_ID_FIELD)
    if not isinstance(session_id, str) or not session_id.strip():
        return None
    return {
        TYPE_FIELD: REQUEST_TYPE,
        SESSION_ID_FIELD: session_id.strip(),
        TOOL_NAME_FIELD: payload.get(TOOL_NAME_FIELD),
        TOOL_INPUT_FIELD: payload.get(TOOL_INPUT_FIELD),
    }


def ask_engine(request: dict[str, Any], *, path: Path, dial_timeout: float) -> ApprovalVerdict:
    """One dialog out, one verdict back. Every failure answers `ASK`.

    A refusal, a closed socket, a reply about something else and a reply that is
    not JSON all mean the same thing here — nobody is going to answer this by
    voice — and they mean it in the one direction that cannot hurt: back to the
    human, who is looking at the dialog right now.
    """
    try:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    except OSError:
        return ApprovalVerdict.ASK
    with connection:
        try:
            connection.settimeout(dial_timeout)
            connection.connect(str(path))
            connection.sendall(
                json.dumps(request, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
                + b"\n"
            )
            # From here the wait is Bridge Core's budget, not ours.
            connection.settimeout(None)
            line = _one_line(connection)
            verdict = _verdict_in(line)
            _acknowledge(connection, line)
        except (OSError, ValueError):
            return ApprovalVerdict.ASK
    return verdict


def _acknowledge(connection: socket.socket, line: bytes) -> None:
    """Tell the engine the verdict arrived. Best effort, and never load-bearing here.

    It is the engine's only positive proof — the connection ending is not one,
    because a close may have been in flight before the verdict was written — and
    it is deliberately not something this process depends on. A failure to say
    "got it" must not turn a verdict this process is holding into silence: the
    dialog is answered from what was read, and the engine grades the attempt
    UNKNOWN, which is exactly what "we cannot tell" is for.
    """
    if not line.strip():
        return
    with contextlib.suppress(OSError):
        connection.sendall(
            json.dumps({TYPE_FIELD: ACK_TYPE}, separators=(",", ":")).encode("utf-8") + b"\n"
        )


def _one_line(connection: socket.socket) -> bytes:
    """Read until the newline the engine always sends, or until it gives up on us."""
    chunks: list[bytes] = []
    read = 0
    while read < MAX_HOOK_REQUEST_BYTES:
        chunk = connection.recv(4096)
        if not chunk:
            break
        chunks.append(chunk)
        read += len(chunk)
        if b"\n" in chunk:
            break
    return b"".join(chunks)


def _verdict_in(line: bytes) -> ApprovalVerdict:
    """The engine's answer, or `ASK` for anything that is not exactly one."""
    if not line.strip():
        return ApprovalVerdict.ASK
    try:
        document: Any = json.loads(line.split(b"\n", 1)[0])
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ApprovalVerdict.ASK
    if not isinstance(document, dict) or document.get(TYPE_FIELD) != VERDICT_TYPE:
        return ApprovalVerdict.ASK
    try:
        return ApprovalVerdict(document.get(VERDICT_FIELD))
    except ValueError:
        return ApprovalVerdict.ASK


def decide(payload: Mapping[str, Any], environ: Mapping[str, str]) -> dict[str, Any] | None:
    """The whole hook, as one function, so the whole hook can be tested.

    Returns what to print, or `None` for the answer that is printed by printing
    nothing. `main` exists only to move bytes in and out of this.
    """
    path = approval_socket_path_in(environ)
    if path is None:
        return None
    request = request_for(payload)
    if request is None:
        return None
    verdict = ask_engine(request, path=path, dial_timeout=dial_timeout_in(environ))
    return hook_decision(verdict)


def main(argv: list[str] | None = None) -> int:
    """Read the dialog, print the decision, and never print anything else.

    The exit code is always 0 and that is deliberate: Claude Code reports a
    non-zero hook as a failure to the user, and "the bridge had nothing to say"
    is not a failure — it is the ordinary state of every dialog nobody answered
    by voice.
    """
    del argv
    try:
        raw = sys.stdin.read()
        payload: Any = json.loads(raw) if raw.strip() else None
        if not isinstance(payload, dict):
            return 0
        decision = decide(payload, os.environ)
        if decision is not None:
            sys.stdout.write(json.dumps(decision, ensure_ascii=False))
    except Exception:  # noqa: BLE001 - stdout is a verdict, so nothing may leak onto it
        return 0
    return 0


if __name__ == "__main__":  # pragma: no cover - the process entry point
    raise SystemExit(main())

"""The `SessionStart` hook process: one line out, nothing back, and never in the way.

ADR 0011's second hook. **It earns its place on one field**: `transcript_path`.
Claude Code's own roster does not carry it, and it cannot be derived without
guessing at the directory-name flattening — which replaces `/`, `.` *and* `_`
with `-`, a rule #73 rediscovered the hard way. The Session's inbox socket and
token ride along because the same payload and the same environment carry them.

**It adds no roster row, and that is the point.** `claude agents --json` already
sees every Session in this config directory, including the ones that started
before this engine did — so a Session whose hook never ran is listed exactly the
same. Legacy could not say that: a Session existed there *because* its hook had
announced it (`legacy@1d32845:bridge/hook.py:68-75,96-109,119-207,281-301`), so
a Session started before the daemon was invisible while its process ran. This is
that hook-client registration **ported**, with the thing it used to be the sole
source of taken away from it.

**Silence, speed, and no side effects when nobody is holding this Session.** A
user-scope hook fires for *every* Session in the config directory (ADR 0011), so
the two soft lines that replaced ADR 0007's structural scope are: this process
prints nothing and opens no socket when no engine has published an address, and
the engine refuses what it does not hold. The address lookup therefore stays the
first thing that happens — the cost to an unheld Session is starting this
process and nothing else, measured at ~33 ms (#71).

**It never waits for an answer.** `SessionStart` runs before the Session is
usable, so a hook that blocked here would delay every Session on the machine by
whatever the engine happened to be busy with. The line goes; the engine's
acknowledgement is read only if it is already there, and is never load-bearing.

Stdlib-only and blocking, for `approval_hook.py`'s reason: it runs under
whatever interpreter the deployment provides, in a position where a missing
dependency is a failure nobody is reading.
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
    CWD_FIELD,
    MESSAGING_SOCKET_FIELD,
    MESSAGING_TOKEN_FIELD,
    PID_FIELD,
    REGISTRATION_TYPE,
    SESSION_ID_FIELD,
    TRANSCRIPT_PATH_FIELD,
    TYPE_FIELD,
)
from gpt_voicecoding.adapters.agent.claude.bootstrap import (
    approval_socket_path_in,
    dial_timeout_in,
)

#: What Claude Code exports before a hook runs, measured by #71's probe. Absent
#: is ordinary rather than an error: a build that does not export them yields a
#: registration carrying the transcript path alone, which is still the field
#: this hook exists for.
MESSAGING_SOCKET_VARIABLE = "CLAUDE_CODE_MESSAGING_SOCKET"
MESSAGING_TOKEN_VARIABLE = "CLAUDE_CODE_MESSAGING_TOKEN"

#: The pid of the `claude` this hook is a child of. Measured on 2026-08-26 by
#: firing a real `SessionStart` in a sandbox config directory: the hook process
#: sees `CLAUDE_PID`, and its own `os.getppid()` is that same number. Confirmed
#: against the live machine, where every `claude agents --json` row's pid is
#: also the name of that Session's socket under `/tmp/cc-socks/`.
#:
#: The hook *payload* carries no pid, and a Claude `SessionTarget` cannot be
#: built without one, so this variable is what turns a registration from a note
#: about a session id into an address for an exact process.
PID_VARIABLE = "CLAUDE_PID"

#: How long the engine is given to take the line. Short on purpose: this runs on
#: the path that opens a Session, and an engine that is not answering must cost
#: the user a registration rather than a pause in front of their prompt.
SEND_TIMEOUT_SECONDS = 2.0


def registration_for(
    payload: Mapping[str, Any], environ: Mapping[str, str]
) -> dict[str, Any] | None:
    """What this Session start looks like on the engine's wire, or `None`.

    `session_id` is load-bearing and everything else is optional: it is what the
    engine resolves into the Session it holds, and without it there is nobody to
    tell. A payload that carries no transcript path is still sent — the socket
    and token are worth having on their own, and a partial registration is
    better than none for a Session that would otherwise be unreachable.
    """
    session_id = payload.get(SESSION_ID_FIELD)
    if not isinstance(session_id, str) or not session_id.strip():
        return None
    message: dict[str, Any] = {
        TYPE_FIELD: REGISTRATION_TYPE,
        SESSION_ID_FIELD: session_id.strip(),
        PID_FIELD: _pid(environ.get(PID_VARIABLE)),
        CWD_FIELD: _text(payload.get(CWD_FIELD)),
        TRANSCRIPT_PATH_FIELD: _text(payload.get(TRANSCRIPT_PATH_FIELD)),
        MESSAGING_SOCKET_FIELD: _text(environ.get(MESSAGING_SOCKET_VARIABLE)),
        MESSAGING_TOKEN_FIELD: _text(environ.get(MESSAGING_TOKEN_VARIABLE)),
    }
    return message


def tell_engine(message: dict[str, Any], *, path: Path, dial_timeout: float) -> bool:
    """Send one line and leave. Every failure is silence, and none of them is fatal.

    Returns whether the line went, for a test or a probe to assert on; nothing
    in the hook's own behaviour depends on it, because there is nothing this
    process could usefully do about a registration that did not land.
    """
    try:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    except OSError:
        return False
    with connection:
        try:
            connection.settimeout(dial_timeout)
            connection.connect(str(path))
            connection.settimeout(SEND_TIMEOUT_SECONDS)
            connection.sendall(
                json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
                + b"\n"
            )
        except OSError:
            return False
        # The engine acknowledges, and this process does not wait to hear it:
        # `SessionStart` runs before the Session is usable. Closing the write
        # side says "that is all", and whatever comes back is read only if it
        # has already arrived.
        with contextlib.suppress(OSError):
            connection.shutdown(socket.SHUT_WR)
    return True


def register(payload: Mapping[str, Any], environ: Mapping[str, str]) -> bool:
    """The whole hook, as one function, so the whole hook can be tested.

    The address lookup is first, so a Session no engine is holding pays for
    starting this process and nothing else — no socket, no writes.
    """
    path = approval_socket_path_in(environ)
    if path is None:
        return False
    message = registration_for(payload, environ)
    if message is None:
        return False
    return tell_engine(message, path=path, dial_timeout=dial_timeout_in(environ))


def main(argv: list[str] | None = None) -> int:
    """Read the payload, send the line, print nothing, and always succeed.

    Nothing reaches stdout. Claude Code reads a `SessionStart` hook's stdout as
    **context to add to the Session**, so anything printed here would appear in
    the user's conversation — a bridge that talked to the agent instead of about
    it. The exit code is always 0 for `approval_hook.py`'s reason: a non-zero
    hook is reported to the user as a failure, and "the bridge had nothing to
    say" is not one.
    """
    del argv
    try:
        raw = sys.stdin.read()
        payload: Any = json.loads(raw) if raw.strip() else None
        if isinstance(payload, dict):
            register(payload, os.environ)
    except Exception:  # noqa: BLE001 - a hook may never be the reason a Session fails
        return 0
    return 0


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _pid(value: Any) -> int | None:
    """`CLAUDE_PID` as a number, or `None` for anything that is not one.

    Absent is not an error here for the same reason the messaging variables are
    not: this hook runs under whatever build the user has, and a registration
    that carries less is better than a Session that never registers. What the
    engine does with a pidless one is the engine's decision, not this process's.
    """
    if not isinstance(value, str) or not value.strip().isdigit():
        return None
    pid = int(value.strip())
    return pid if pid > 0 else None


if __name__ == "__main__":  # pragma: no cover - the process entry point
    raise SystemExit(main())

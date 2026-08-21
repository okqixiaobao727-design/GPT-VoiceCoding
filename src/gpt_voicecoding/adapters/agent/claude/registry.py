"""Where a Claude Session says it can be reached. Read-only, and fail-closed.

Claude Code writes one JSON file per live process into `~/.claude/sessions`,
naming its pid, its session id, its cwd, the peer socket it listens on, and what
it is currently doing. This engine **only ever reads** it: the file belongs to
another program, and a bridge that wrote there would be inventing a Session that
Claude Code does not believe in.

**The pin is `peerProtocol`, not the version string.** The peer socket is
undocumented, so this adapter is proven against exactly one wire — but the field
that governs that wire is the protocol number the record itself carries. A
version string moves every week for reasons that have nothing to do with this
socket; the protocol number moves when the socket changes. Refusing on the
number is the check that means something, and refusing on the string would
refuse every live Session on the machine the day after an upgrade.

Static re-probe of the currently pinned build (2.1.238) against the protocol
this module assumes: `peerProtocol`, `msgV`, `orig_msg_id`, `peer_message_status`,
`queued_command`, `source_uuid` and `crossSessionInbound` all still present, and
the receiver's inbound dispatch still keys on a top-level `type` of `user` or
`control`. See `peer.py` for the frame shapes those govern.

**Liveness is a separate question and is asked separately.** A record parses
whether or not its process still exists, because "the registry says this" and
"that process is still there" are two different facts and a reader that conflates
them cannot tell a stale record from a malformed one.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: The registry's default home. A location, so it defaults; see `settings.py`.
DEFAULT_REGISTRY_DIRECTORY = Path.home() / ".claude" / "sessions"

#: The one peer-socket wire this adapter has been proven against. The record
#: carries this number, and a record carrying another one is refused by it.
PEER_PROTOCOL = 1

#: The Claude Code build the pin was last re-probed against. Documentation, not
#: a gate — `PEER_PROTOCOL` is the gate. Written down so the next re-probe knows
#: what its baseline was.
PROVEN_AGAINST_VERSION = "2.1.238"


class RegistryError(Exception):
    """The registry does not say what this adapter needs, or does not say it clearly."""


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """One live Claude process, as its own registry entry describes it."""

    pid: int
    session_id: str
    cwd: Path
    version: str
    peer_protocol: int
    #: The peer socket this Session listens on, inside the shared `cc-socks`
    #: directory. Both the address a Notice Relay is sent to and — because the
    #: receiver only accepts reply addresses from its own socket namespace — the
    #: directory our own receipt listener has to bind in.
    socket_path: Path
    #: What Claude Code says this Session is doing right now: `idle`, `busy` or
    #: `waiting`. Left as the registry's own word; translating it into a Reply
    #: Window is `window.py`'s job, not this reader's.
    status: str
    name: str = ""


def read_record(directory: Path, pid: int) -> SessionRecord:
    """Read one Session's registry entry, refusing every shape that is not exactly one."""
    path = directory / f"{pid}.json"
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as unreadable:
        raise RegistryError(
            f"no Claude Session registry record for pid {pid} at {path}: {unreadable}"
        ) from None
    return _record(path, raw, expected_pid=pid)


def records(directory: Path) -> tuple[SessionRecord, ...]:
    """Every record in the registry that can be read, skipping the ones that cannot.

    One unreadable file is not a broken registry. Claude Code writes these
    entries live, so a half-written file is an ordinary momentary state — and
    refusing the whole directory over one would make every reader flaky. A
    caller that needs *one* Session asks for it by pid and gets a refusal.
    """
    try:
        entries = sorted(directory.glob("*.json"))
    except OSError:
        return ()
    found: list[SessionRecord] = []
    for path in entries:
        try:
            found.append(_record(path, path.read_text(encoding="utf-8"), expected_pid=None))
        except (OSError, RegistryError):
            continue
    return tuple(found)


def pid_is_live(pid: int) -> bool:
    """Whether that process still exists, asked the only way that does not disturb it.

    Signal 0 performs the permission and existence checks and delivers nothing.
    `EPERM` means the process exists and belongs to somebody else — which is
    still "alive", and is not this function's business to judge.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _record(path: Path, raw: str, *, expected_pid: int | None) -> SessionRecord:
    try:
        document: Any = json.loads(raw)
    except json.JSONDecodeError as unreadable:
        raise RegistryError(f"{path} is not JSON: {unreadable}") from None
    if not isinstance(document, dict):
        raise RegistryError(f"{path} must hold a JSON object")

    pid = _int(document, "pid", path)
    if expected_pid is not None and pid != expected_pid:
        raise RegistryError(
            f"{path} names pid {pid}; a registry record whose pid disagrees with its own "
            "filename is not a record this adapter will address"
        )

    protocol = _int(document, "peerProtocol", path)
    if protocol != PEER_PROTOCOL:
        raise RegistryError(
            f"{path} speaks peerProtocol {protocol}; this adapter is proven against "
            f"{PEER_PROTOCOL} only (last re-probed against Claude Code "
            f"{PROVEN_AGAINST_VERSION}) and will not guess at another wire"
        )

    return SessionRecord(
        pid=pid,
        session_id=_text(document, "sessionId", path),
        cwd=Path(_text(document, "cwd", path, default=str(path.parent))),
        version=_text(document, "version", path, default=""),
        peer_protocol=protocol,
        socket_path=Path(_text(document, "messagingSocketPath", path)),
        status=_text(document, "status", path, default=""),
        name=_text(document, "name", path, default=""),
    )


def _int(document: dict[str, Any], field: str, path: Path) -> int:
    value = document.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise RegistryError(f"{path} does not carry a whole number {field}, got {value!r}")
    return value


def _text(document: dict[str, Any], field: str, path: Path, *, default: str | None = None) -> str:
    value = document.get(field)
    if isinstance(value, str) and value.strip():
        return value
    if default is not None:
        return default
    raise RegistryError(f"{path} does not carry a non-empty {field}, got {value!r}")

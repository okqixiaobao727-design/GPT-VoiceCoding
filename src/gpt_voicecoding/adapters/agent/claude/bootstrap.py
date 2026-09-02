"""The one thing a process Claude Code starts is told before it can read anything.

The **`PermissionRequest` hook** is a process Claude Code starts, one per
displayed permission dialog, and nothing in this engine is its parent — so it has
to be told where this engine is before it can ask anything. Exactly one name is
hard-coded on both sides of that boundary, and everything configurable travels
inside its JSON value, which keeps the settings table the single source of truth
for values.

**Every absence reads the same way, and that is the never-deny rule arriving as a
parsing decision.** No value, an unreadable one, a value that names no approval
address: all of them mean there is nobody to ask, so the hook prints nothing and
the dialog stays with the human in front of it.

**And the variable is no longer where the address usually comes from.** v1.0 is a
bridge over Sessions the *user* starts (#67), so there is no launch wrapper and
no variable in a Session the engine did not launch. ADR 0011's answer, built
here: the engine **publishes** its approval address in a file at a location both
sides derive from `locations.py`, and the hook reads it when nothing was handed
to it. A missing or unreadable file is silence, which is the same answer the
missing variable already gave.

The name still says "channel" because it is a wire constant a released hook
process may already be reading; ADR 0006's channel server, which shared it, was
removed with #77.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import socket
import tempfile
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from gpt_voicecoding.adapters.agent.claude.settings import ClaudeSettings
from gpt_voicecoding.locations import address_path

#: The name both halves of the boundary know. A versioned protocol constant
#: rather than configuration: the server must know one name before it can read
#: anything at all.
CHANNEL_CONFIG_VARIABLE = "GPT_VOICECODING_CLAUDE_CHANNEL_CONFIG"


class BootstrapError(Exception):
    """The bootstrap value is absent, unreadable, or does not say what it must."""


class AddressHeld(Exception):
    """Another live engine on this machine already holds the approval address.

    **Not a `BootstrapError`.** That one is a *reading* failure and the hook
    answers every one of them with silence (`approval_socket_path_in` catches it
    and prints nothing). This is a *writing* refusal raised at an engine's start,
    and the file it refused to write is perfectly readable — by the hook, which
    should go on reaching the engine that holds it. Sharing a base class would
    put a live route one stray `except BootstrapError` away from being read as
    no route at all.
    """

    def __init__(self, holder: Path) -> None:
        super().__init__(
            f"another engine holds the Claude approval address; its approval socket is {holder}"
        )
        #: The holder's approval socket path, so the refused engine can name in
        #: its log and its loaded-report *which* engine it stood down for.
        self.holder = holder


#: What the hook waits for a dial to succeed in when the launch did not say.
#: Present because the two halves of this variable fail in opposite directions:
#: the channel refuses a missing budget, because measuring in different units is
#: worse than not measuring — while the hook's every refusal is a dialog handed
#: back, so declining to dial over an absent timeout would lose real approvals to
#: a missing number.
DEFAULT_DIAL_TIMEOUT_SECONDS = 10.0


def publish_address(
    approval_socket_path: Path, settings: ClaudeSettings, *, base_dir: Path | None = None
) -> Path:
    """Claim where this engine parks permission dialogs, for hooks to read.

    Best effort by construction: the caller is an adapter's `connect`, and an
    engine that cannot write this file still relays, still watches, and still
    answers every surface. What it loses is the Approval Relay into Sessions
    nobody handed a variable to — which is every Session in v1.0.

    **Publishing is a claim, not a broadcast (#202).** This file is per user per
    machine and the hook is a process Claude Code starts with no configuration,
    so it can only ever read this one path: two engines cannot both own the
    route. Whoever is already here is dialled before anything is written. A
    socket nobody answers is debris and is taken over; a socket that answers and
    is not this engine's own is a live peer, and `AddressHeld` is raised rather
    than displacing it. **Ported** from legacy `bridge/daemon.py:711`
    (`_claim_socket_path`, reference state `1d32845`) — "take over a stale socket
    file, but never displace a live Bridge" — which legacy applied to its control
    socket. Legacy never needed it here, because it injected the address into
    every Session it launched itself (`bridge/claude.py:468`), which ADR 0011
    adapted away.
    """
    path = address_path(base_dir)
    document = {
        "approvalSocketPath": str(approval_socket_path),
        "dialTimeoutSeconds": settings.request_timeout_seconds,
    }
    with _claiming(path):
        holder = _held_by_another(path, approval_socket_path, settings.request_timeout_seconds)
        if holder is not None:
            raise AddressHeld(holder)
        # A name of its own per write, never one fixed name beside the address:
        # two engines publishing in the same millisecond collided on the shared
        # temporary and one of them lost the file between write and rename,
        # measured on acceptance run `20260902T012313Z`.
        handle, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".writing")
        temporary = Path(name)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as writing:
                writing.write(json.dumps(document, indent=2) + "\n")
            temporary.chmod(0o600)
            os.replace(temporary, path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    return path


#: The lock every claimant takes before it looks at the address, so that reading
#: who is there and acting on the answer are one step.
CLAIM_LOCK_SUFFIX = ".claim"


@contextlib.contextmanager
def _claiming(path: Path) -> Iterator[None]:
    """Hold the right to decide who owns the address, for as long as deciding takes.

    Probing and writing are two syscalls, and so are reading and unlinking. Two
    engines interleaved between the halves of either pair put the file back
    exactly where #202 found it: two publishers that both see no holder both
    write, and a withdrawing engine that read its own address unlinks the one a
    newer engine wrote in between. The probe cannot be folded into the write —
    the answer takes a dial — so the pair is made atomic instead.

    An `flock` rather than an exclusive create, because a lock has to survive the
    process that holds it dying: a lock file left behind by a killed engine is
    unlocked by the kernel when its descriptor closes, while an `O_EXCL` marker
    left behind is a machine that can never publish again. The file itself is
    never unlinked, for the same reason — unlinking it is how two holders end up
    locking two different inodes.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(
        path.with_name(f".{path.name}{CLAIM_LOCK_SUFFIX}"), os.O_CREAT | os.O_RDWR, 0o600
    )
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        os.close(handle)


def _held_by_another(path: Path, own_socket_path: Path, timeout: float) -> Path | None:
    """The live peer holding this address, or `None` if nobody is.

    `None` covers the three ways this engine may write: nobody published, what
    was published is this engine's own address, and what was published names a
    socket nobody answers.
    """
    published = published_address_at(path).get("approvalSocketPath")
    if not isinstance(published, str) or not published.strip():
        return None
    holder = Path(published)
    if holder == own_socket_path:
        return None
    return holder if _answers(holder, timeout) else None


def _answers(socket_path: Path, timeout: float) -> bool:
    """Whether anybody is listening there, from one dial that says nothing.

    A dial and an immediate close, because a byte written here would land in a
    live engine's approval protocol. `timeout` bounds a listener that accepts and
    then hangs; a socket file whose engine is gone refuses at once.
    """
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(timeout)
    try:
        probe.connect(str(socket_path))
    except OSError:
        return False
    finally:
        probe.close()
    return True


def withdraw_address(approval_socket_path: Path, *, base_dir: Path | None = None) -> None:
    """Take back *this engine's* address when it stops.

    A stale address is a dial into nothing, which costs a hook its dial timeout
    on every dialog — and that is why the file goes. But it goes only if it still
    names this engine (#202): the engine that stops first must not take the route
    away from a peer that is still up, and after a refused publish the file here
    is somebody else's. Legacy has no equivalent — one bridge per socket path
    made the comparison moot — so this is **dropped there, needed here**.
    """
    path = address_path(base_dir)
    with _claiming(path):
        published = published_address_at(path).get("approvalSocketPath")
        if published != str(approval_socket_path):
            return
        with contextlib.suppress(OSError):
            path.unlink()


def published_address(base_dir: Path | None = None) -> dict[str, Any]:
    """What the engine published, or an empty mapping if nobody published."""
    return published_address_at(address_path(base_dir))


def published_address_at(path: Path) -> dict[str, Any]:
    """The same reading, from a path already derived. An unreadable file reads as
    nobody published — which is silence to a hook, and *not this engine's* to a
    withdrawal, because a file this engine cannot attribute is one it leaves."""
    try:
        document: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return document if isinstance(document, dict) else {}


def _told(environ: Mapping[str, str], base_dir: Path | None) -> dict[str, Any]:
    """What this hook was told, from the variable if there is one, else the file."""
    raw = environ.get(CHANNEL_CONFIG_VARIABLE)
    if isinstance(raw, str) and raw.strip():
        try:
            document: Any = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return document if isinstance(document, dict) else {}
    return published_address(base_dir)


def approval_socket_path_in(
    environ: Mapping[str, str], *, base_dir: Path | None = None
) -> Path | None:
    """Where this engine parks permission dialogs, or `None` if nowhere.

    `None` is the hook's first gate and it covers every absence with one answer:
    no variable and no published address (no engine is holding this machine), a
    value this build cannot read, and an engine that published no approval
    address. All of them mean the same thing to a hook — there is nobody to ask —
    and a hook with nobody to ask prints nothing.
    """
    try:
        return _optional_path(_told(environ, base_dir), "approvalSocketPath")
    except BootstrapError:
        return None


def dial_timeout_in(environ: Mapping[str, str], *, base_dir: Path | None = None) -> float:
    """How long the hook waits to reach the engine, or the default if unstated."""
    try:
        return _optional_seconds(_told(environ, base_dir), "dialTimeoutSeconds")
    except BootstrapError:
        return DEFAULT_DIAL_TIMEOUT_SECONDS


def _optional_path(document: dict[str, Any], field: str) -> Path | None:
    """A path if the launch stated one, `None` if it did not, a refusal if it lied."""
    value = document.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise BootstrapError(
            f"{CHANNEL_CONFIG_VARIABLE}.{field} must be a non-empty string when it is present"
        )
    return Path(value)


def _optional_seconds(document: dict[str, Any], field: str) -> float:
    value = document.get(field)
    if value is None:
        return DEFAULT_DIAL_TIMEOUT_SECONDS
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise BootstrapError(
            f"{CHANNEL_CONFIG_VARIABLE}.{field} must be a positive number of seconds, got {value!r}"
        )
    return float(value)

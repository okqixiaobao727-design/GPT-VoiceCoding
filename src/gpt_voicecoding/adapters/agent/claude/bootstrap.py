"""The one thing a process Claude Code starts is told before it can read anything.

The channel server is a process **Claude Code** starts, from a plugin manifest,
when the user launches a Session. Nothing in this engine is its parent, so the
only way to tell it which socket to bind is the environment it inherits — which
the launch wrapper sets, one fresh value per launch.

**The `PermissionRequest` hook is in the same position and reads the same
variable.** It is a second process Claude Code starts, one per displayed
permission dialog, with the same inherited environment and the same problem: it
has to be told where this engine is before it can ask anything. So the approval
socket's address rides here rather than in a variable of its own — one boundary,
one name, one place a launch is described.

The two halves fail in opposite directions, on purpose. The channel's fields are
required and a malformed one refuses, because a channel that binds a default path
is a channel nobody can find. The hook's fields are optional and every absence
reads the same: no address, no engine to ask, print nothing, and the dialog stays
with the human — which is the never-deny rule, arriving as a parsing decision.

So exactly one name is hard-coded on both sides of that boundary, and everything
configurable travels inside its JSON value. That keeps the settings table the
single source of truth for values, and keeps the duplicated surface at one
string this module and the server share by import rather than by transcription.

**Whose job is what.** This module owns the contract: the variable's name, the
shape of its value, and what a malformed one does (refuse — a channel that
silently binds a default path is a channel nobody can find). Setting it, and
choosing the per-launch socket path, belongs to the Session Launcher; providing
the interpreter that runs the server belongs to the bundle.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gpt_voicecoding.adapters.agent.claude.settings import ClaudeSettings

#: The name both halves of the boundary know. A versioned protocol constant
#: rather than configuration: the server must know one name before it can read
#: anything at all.
CHANNEL_CONFIG_VARIABLE = "GPT_VOICECODING_CLAUDE_CHANNEL_CONFIG"


class BootstrapError(Exception):
    """The bootstrap value is absent, unreadable, or does not say what it must."""


def bootstrap_value(
    socket_path: Path, settings: ClaudeSettings, *, approval_socket_path: Path | None = None
) -> str:
    """The value of `CHANNEL_CONFIG_VARIABLE` for one Session launch.

    The approval socket is optional here and required nowhere else, because the
    two routes fail independently: a launch that carries no approval address
    still has a working Answer Relay, and the hook's answer to "there is no
    address" is the same silence as its answer to every other absence.
    """
    document: dict[str, Any] = {
        "socketPath": str(socket_path),
        "maxMessageBytes": settings.max_message_bytes,
        "maxTextBytes": settings.max_text_bytes,
        "dialTimeoutSeconds": settings.request_timeout_seconds,
    }
    if approval_socket_path is not None:
        document["approvalSocketPath"] = str(approval_socket_path)
    return json.dumps(document, separators=(",", ":"))


#: What the hook waits for a dial to succeed in when the launch did not say.
#: Present because the two halves of this variable fail in opposite directions:
#: the channel refuses a missing budget, because measuring in different units is
#: worse than not measuring — while the hook's every refusal is a dialog handed
#: back, so declining to dial over an absent timeout would lose real approvals to
#: a missing number.
DEFAULT_DIAL_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class ChannelBootstrap:
    """What one channel server was told, read back out of the environment."""

    socket_path: Path
    max_message_bytes: int
    max_text_bytes: int
    #: Where this engine parks permission dialogs. Optional, and separately so:
    #: the Answer Relay and the Approval Relay are two routes with two failures,
    #: and a launch that carries only the first is a working Session.
    approval_socket_path: Path | None = None
    dial_timeout_seconds: float = DEFAULT_DIAL_TIMEOUT_SECONDS


def read_bootstrap(environ: Mapping[str, str]) -> ChannelBootstrap:
    """Read the bootstrap value, refusing every shape that is not exactly one.

    Fail closed on every field. A missing budget that fell back to a default
    would be the two ends measuring in different units; a missing socket path
    that fell back to a default would be a server listening where nobody dials.
    """
    raw = environ.get(CHANNEL_CONFIG_VARIABLE)
    if not isinstance(raw, str) or not raw.strip():
        raise BootstrapError(f"{CHANNEL_CONFIG_VARIABLE} is required and is not set")
    try:
        document: Any = json.loads(raw)
    except json.JSONDecodeError as unreadable:
        raise BootstrapError(f"{CHANNEL_CONFIG_VARIABLE} is not JSON: {unreadable}") from None
    if not isinstance(document, dict):
        raise BootstrapError(f"{CHANNEL_CONFIG_VARIABLE} must hold a JSON object")

    socket_path = document.get("socketPath")
    if not isinstance(socket_path, str) or not socket_path.strip():
        raise BootstrapError(f"{CHANNEL_CONFIG_VARIABLE}.socketPath must be a non-empty string")
    return ChannelBootstrap(
        socket_path=Path(socket_path),
        max_message_bytes=_positive(document, "maxMessageBytes"),
        max_text_bytes=_positive(document, "maxTextBytes"),
        approval_socket_path=_optional_path(document, "approvalSocketPath"),
        dial_timeout_seconds=_optional_seconds(document, "dialTimeoutSeconds"),
    )


def approval_socket_path_in(environ: Mapping[str, str]) -> Path | None:
    """Where this launch's engine parks permission dialogs, or `None` if nowhere.

    `None` is the hook's first gate and it covers three cases with one answer: no
    bootstrap variable at all (a Session this engine did not launch), a variable
    this build cannot read, and a launch that carried no approval address. All
    three mean the same thing to a hook — there is nobody to ask — and a hook
    with nobody to ask prints nothing.
    """
    try:
        return read_bootstrap(environ).approval_socket_path
    except BootstrapError:
        return None


def dial_timeout_in(environ: Mapping[str, str]) -> float:
    """How long the hook waits to reach the engine, or the default if unstated."""
    try:
        return read_bootstrap(environ).dial_timeout_seconds
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


def _positive(document: dict[str, Any], field: str) -> int:
    value = document.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise BootstrapError(
            f"{CHANNEL_CONFIG_VARIABLE}.{field} must be a positive whole number of bytes, "
            f"got {value!r}"
        )
    return value

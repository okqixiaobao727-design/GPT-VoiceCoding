"""The one thing the channel server is told before it can read anything else.

The server is a process **Claude Code** starts, from a plugin manifest, when the
user launches a Session. Nothing in this engine is its parent, so the only way
to tell it which socket to bind is the environment it inherits — which the
launch wrapper sets, one fresh value per launch.

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


def bootstrap_value(socket_path: Path, settings: ClaudeSettings) -> str:
    """The value of `CHANNEL_CONFIG_VARIABLE` for one Session launch."""
    return json.dumps(
        {
            "socketPath": str(socket_path),
            "maxMessageBytes": settings.max_message_bytes,
            "maxTextBytes": settings.max_text_bytes,
        },
        separators=(",", ":"),
    )


@dataclass(frozen=True, slots=True)
class ChannelBootstrap:
    """What one channel server was told, read back out of the environment."""

    socket_path: Path
    max_message_bytes: int
    max_text_bytes: int


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
    )


def socket_path_in(environ: Mapping[str, str]) -> Path | None:
    """Where this launch's channel was told to listen, or `None` if it has none.

    The Session Launcher's hook subprocess inherits the same variable the
    wrapper set, which is how a registering Session reports its channel without
    anything else needing to know the concept exists.
    """
    try:
        return read_bootstrap(environ).socket_path
    except BootstrapError:
        return None


def _positive(document: dict[str, Any], field: str) -> int:
    value = document.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise BootstrapError(
            f"{CHANNEL_CONFIG_VARIABLE}.{field} must be a positive whole number of bytes, "
            f"got {value!r}"
        )
    return value

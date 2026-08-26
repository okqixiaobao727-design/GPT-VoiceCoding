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
import json
import os
from collections.abc import Mapping
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
    """Write where this engine parks permission dialogs, for hooks to read.

    Best effort by construction: the caller is an adapter's `connect`, and an
    engine that cannot write this file still relays, still watches, and still
    answers every surface. What it loses is the Approval Relay into Sessions
    nobody handed a variable to — which is every Session in v1.0.
    """
    path = address_path(base_dir)
    document = {
        "approvalSocketPath": str(approval_socket_path),
        "dialTimeoutSeconds": settings.request_timeout_seconds,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.writing")
    temporary.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return path


def withdraw_address(base_dir: Path | None = None) -> None:
    """Take the address back when the engine stops. A stale address is a dial
    into nothing, which costs a hook its dial timeout on every dialog."""
    with contextlib.suppress(OSError):
        address_path(base_dir).unlink()


def published_address(base_dir: Path | None = None) -> dict[str, Any]:
    """What the engine published, or an empty mapping if nobody published."""
    try:
        document: Any = json.loads(address_path(base_dir).read_text(encoding="utf-8"))
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

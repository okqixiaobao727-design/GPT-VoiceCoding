"""What this spoke is told, read out of one opaque table.

The composition root forwards `[adapters.settings.<seam>]` without looking
inside it: only the adapter knows what its own keys mean, and a root that parsed
them would be Bridge Core growing adapter-shaped knowledge (ADR 0001).

**An unknown key refuses to start.** A misspelled timeout that silently falls
back to a default is the configuration-shaped version of the silent fallback
this repository bans — the operator believes they set something, the engine
believes they did not, and nothing says so until it matters. Every key is either
recognised or named in a refusal.

**Locations and mechanism identity default; decisions do not.** This is
`config.py`'s own rule, applied one level down. Where the codex executable is,
where a socket directory goes, how long a wire waits for a frame — those are
locations and protocol mechanics, and an in-code default for them is honest. The
Relay ceiling and the approval budget are *policy*, they belong to Bridge Core's
own configuration, and nothing here may restate them.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from gpt_voicecoding.installation import codex_launch_agent


def default_executable(environ: Mapping[str, str]) -> str:
    """Derive the executable from Installation's managed binary for ``CODEX_HOME``.

    #82 chose that binary over PATH. This is the first ``adapters -> installation``
    import under ADR 0012's one-way dependency rule; Installation never imports
    back into the engine.
    """
    return str(codex_launch_agent.managed_binary(codex_launch_agent.default_codex_home(environ)))


#: A short runtime root: Darwin caps an `AF_UNIX` path at 103 bytes, and a
#: socket under a long application-support path simply cannot be bound.
DEFAULT_SOCKET_DIRECTORY = Path("/tmp")

#: How long one JSON-RPC call waits. Protocol mechanics, not policy.
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0

#: How long the engine's own app-server is given to create its socket.
DEFAULT_STARTUP_TIMEOUT_SECONDS = 30.0

#: How long a Relay waits for its `clientUserMessageId` to appear in the thread
#: before the attempt is graded UNKNOWN. Bounded on purpose: an unbounded wait
#: turns "we cannot tell" into "we never answer".
DEFAULT_RECEIPT_TIMEOUT_SECONDS = 20.0

#: How often the thread is re-read while waiting for that receipt.
DEFAULT_RECEIPT_POLL_SECONDS = 0.5

#: How long a verdict waits for `serverRequest/resolved` to prove it landed.
DEFAULT_VERDICT_TIMEOUT_SECONDS = 10.0


class SettingsError(Exception):
    """The settings table names something this adapter does not have."""


@dataclass(frozen=True, slots=True)
class CodexSettings:
    """Everything this spoke may be told. Nothing policy-shaped appears here."""

    executable: str = field(default_factory=lambda: default_executable(os.environ))
    socket_directory: Path = DEFAULT_SOCKET_DIRECTORY
    request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS
    startup_timeout_seconds: float = DEFAULT_STARTUP_TIMEOUT_SECONDS
    receipt_timeout_seconds: float = DEFAULT_RECEIPT_TIMEOUT_SECONDS
    receipt_poll_seconds: float = DEFAULT_RECEIPT_POLL_SECONDS
    verdict_timeout_seconds: float = DEFAULT_VERDICT_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if not self.executable.strip():
            raise SettingsError("executable must name the codex binary to run")
        for name in (
            "request_timeout_seconds",
            "startup_timeout_seconds",
            "receipt_timeout_seconds",
            "receipt_poll_seconds",
            "verdict_timeout_seconds",
        ):
            if getattr(self, name) <= 0:
                raise SettingsError(f"{name} must be a positive number of seconds")

    @classmethod
    def of(
        cls,
        table: dict[str, Any] | None,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> CodexSettings:
        """Read one settings table, refusing every key it does not recognise."""
        table = table or {}
        known = {field.name for field in fields(cls)}
        unknown = sorted(set(table) - known)
        if unknown:
            raise SettingsError(
                f"[adapters.settings.agent.codex] does not have "
                f"{', '.join(unknown)}. It has: {', '.join(sorted(known))}"
            )
        read: dict[str, Any] = {}
        for key, value in table.items():
            read[key] = _typed(key, value)
        if environ is not None:
            read.setdefault("executable", default_executable(environ))
        return cls(**read)


def _typed(key: str, value: Any) -> Any:
    """Turn one TOML value into what the field holds, or refuse in the operator's words."""
    if key == "executable":
        if not isinstance(value, str):
            raise SettingsError(f"{key} must be the name or path of an executable")
        return value.strip()
    if key == "socket_directory":
        if not isinstance(value, str) or not value.strip():
            raise SettingsError(f"{key} must be a directory path")
        return Path(value.strip()).expanduser()
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise SettingsError(f"{key} must be a number of seconds")
    return float(value)

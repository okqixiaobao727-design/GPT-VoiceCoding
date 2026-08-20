"""What the Call spoke is told, read out of one opaque table.

The same rule `[adapters.settings.agent.codex]` follows, applied to this seam:
the composition root forwards `[adapters.settings.call]` without looking inside
it, an unrecognised key refuses to start rather than falling back silently, and
**locations and mechanism identity default while decisions do not**.

Two of this adapter's three thread parameters are deliberately *absent* from
this table, and their absence is the point.

- `approvalPolicy` is pinned to `never` in the adapter. That the bridge-owned
  voice thread runs approval-free is a decision already taken and recorded
  (legacy issue #19, resolution point 3), traded against the thread acting only
  through the control-plane CLI's closed set of verbs. A configuration key for
  it would invite a deployment to break one half of that trade while keeping
  the other.
- `sandbox` is pinned to `danger-full-access` for a narrower reason: the CLI
  those threads act through connects to the engine over an `AF_UNIX` socket, and
  `workspace-write` does not permit that connect. There is exactly one workable
  value, and a "decision" with one workable value is mechanism identity, not a
  choice. If codex ever grants a socket exemption to a narrower sandbox,
  tightening this is an obligation rather than an option.

`workspace` *is* a location, so it defaults — to the user's home directory,
which always exists and assumes no project. Deliberately **not** the engine's
own state directory: an adapter that derived it would have to reach into Bridge
Core's persistence for the layout, and the dependency between a spoke and the
hub runs one way, through the seams (ADR 0001). A deployment that wants these
threads to start somewhere narrower states `workspace` and gets it.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

#: How long the whole handshake gets — offer, SDP answer, `started`, and the
#: peer connection actually reaching `connected`. Generous because a real ICE
#: exchange over a network is what it is waiting on.
DEFAULT_CONNECT_TIMEOUT_SECONDS = 45.0

#: How long one JSON-RPC call to the app-server waits. Protocol mechanics.
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0

#: How long one Delegated Turn may take before the attempt is a classified
#: failure. Bounded on purpose: an unbounded wait turns "the coding model is
#: slow" into "the user is never answered".
DEFAULT_DELEGATED_TURN_TIMEOUT_SECONDS = 300.0


class SettingsError(Exception):
    """The settings table names something this adapter does not have."""


def default_workspace() -> Path:
    """Where the bridge's own threads run when nothing says otherwise.

    The user's home directory: it is always there, and it carries no assumption
    that the user has one project rather than ten. The prototype ran in a source
    checkout, which was a development convenience and is not a default anything
    should inherit.
    """
    return Path.home()


@dataclass(frozen=True, slots=True)
class RealtimeCallSettings:
    """Everything this spoke may be told. Nothing policy-shaped appears here."""

    workspace: Path | None = None
    connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS
    request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS
    delegated_turn_timeout_seconds: float = DEFAULT_DELEGATED_TURN_TIMEOUT_SECONDS
    #: Which audio devices to open, by the index the host audio library uses.
    #: `None` means the machine's own default, which is what a laptop wants.
    input_device: int | None = None
    output_device: int | None = None

    def __post_init__(self) -> None:
        for name in (
            "connect_timeout_seconds",
            "request_timeout_seconds",
            "delegated_turn_timeout_seconds",
        ):
            if getattr(self, name) <= 0:
                raise SettingsError(f"{name} must be a positive number of seconds")
        for name in ("input_device", "output_device"):
            device = getattr(self, name)
            if device is not None and device < 0:
                raise SettingsError(f"{name} must be an audio device index")

    @property
    def cwd(self) -> Path:
        """The directory the bridge's own threads run in, stated or defaulted."""
        return self.workspace if self.workspace is not None else default_workspace()

    @classmethod
    def of(cls, table: dict[str, Any] | None) -> RealtimeCallSettings:
        """Read one settings table, refusing every key it does not recognise."""
        if not table:
            return cls()
        known = {field.name for field in fields(cls)}
        unknown = sorted(set(table) - known)
        if unknown:
            raise SettingsError(
                f"[adapters.settings.call] does not have {', '.join(unknown)}. "
                f"It has: {', '.join(sorted(known))}"
            )
        return cls(**{key: _typed(key, value) for key, value in table.items()})


def _typed(key: str, value: Any) -> Any:
    """Turn one TOML value into what the field holds, or refuse in the operator's words."""
    if key == "workspace":
        if not isinstance(value, str) or not value.strip():
            raise SettingsError(f"{key} must be a directory path")
        return Path(value.strip()).expanduser()
    if key in ("input_device", "output_device"):
        if isinstance(value, bool) or not isinstance(value, int):
            raise SettingsError(f"{key} must be an audio device index")
        return value
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise SettingsError(f"{key} must be a number of seconds")
    return float(value)

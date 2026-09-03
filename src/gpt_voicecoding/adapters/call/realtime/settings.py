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

`realtime_model` *is* here, and it is the one key that looks like it should have
been pinned beside those two. The rule that separates them is **who controls the
value**. `approvalPolicy`, `sandbox` and `REALTIME_VERSION` are values this
repository verifies and then pins; they move only when our code moves, which is
what makes them mechanism identity. The realtime model's validity is granted and
withdrawn by the backend: on 2026-08-22 the value codex sends by default fell out
of an allowlist we do not see, with no client change, and every call failed for
two days (#35). A value the far side can revoke at will is not mechanism
identity — it is an **environment fact with a default**, and the operator is owed
a one-line escape hatch rather than a wait for our next release. The default is
the single pin point; there is no second constant holding the same fact.

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

#: How long the user's transcript may go without a delta before this adapter
#: calls the utterance over — the fourth end of `UserSpeaking`'s span, and the
#: only one this side invents (`seams/call.py`). Dialled rather than fixed
#: because the number is **unmeasured**: whether user deltas arrive during or
#: after the speech is what #212's timestamped record settles, and a value
#: nobody can change without a release is a value that measurement cannot
#: correct. A second and a half is longer than the gap between two deltas of one
#: sentence and shorter than a pause a listener hears as the end of one.
DEFAULT_USER_QUIET_SECONDS = 1.5

#: How long the Voice's stop edge waits for the audio to finish playing before it
#: is published anyway (#195). A safety net rather than a schedule: an answer
#: generated in ten seconds and spoken over seventy-five trails its own
#: generation by more than a minute, and the ceiling is *held* while it does,
#: which is correct — a call somebody is still hearing is not idle. What this
#: bounds is a wait that would otherwise outlive its call if the transport
#: stopped answering, so it is dialled for the machine whose audio path is
#: slower than this one's.
DEFAULT_VOICE_PLAYOUT_WAIT_SECONDS = 180.0


#: The realtime model the backend still accepts. codex populates `session.model`
#: on this path no matter how it is configured, and its own default
#: (`gpt-live-1-boulder-alpha`) is refused for ChatGPT-authenticated sessions —
#: reported, misleadingly, as the *field* not being allowed (#35,
#: openai/codex#40140). Stating an accepted model is what brings the call up.
#: Granted by the far side, so it can expire again: re-run the probe in #35 and
#: re-derive it.
DEFAULT_REALTIME_MODEL = "gpt-live-1-codex"


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
    #: Which realtime model the call asks for. Defaulted, not pinned: see the
    #: module docstring for why this one key is the operator's to state.
    realtime_model: str = DEFAULT_REALTIME_MODEL
    #: Which audio devices to open, by the index the host audio library uses.
    #: `None` means the machine's own default, which is what a laptop wants.
    input_device: int | None = None
    output_device: int | None = None
    #: The two timings the speaking spans are derived with. See their defaults.
    user_quiet_seconds: float = DEFAULT_USER_QUIET_SECONDS
    voice_playout_wait_seconds: float = DEFAULT_VOICE_PLAYOUT_WAIT_SECONDS

    def __post_init__(self) -> None:
        for name in (
            "connect_timeout_seconds",
            "request_timeout_seconds",
            "delegated_turn_timeout_seconds",
            "user_quiet_seconds",
            "voice_playout_wait_seconds",
        ):
            if getattr(self, name) <= 0:
                raise SettingsError(f"{name} must be a positive number of seconds")
        if not self.realtime_model.strip():
            raise SettingsError("realtime_model must be a model name")
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
    if key == "realtime_model":
        if not isinstance(value, str) or not value.strip():
            raise SettingsError(f"{key} must be a model name")
        return value.strip()
    if key in ("input_device", "output_device"):
        if isinstance(value, bool) or not isinstance(value, int):
            raise SettingsError(f"{key} must be an audio device index")
        return value
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise SettingsError(f"{key} must be a number of seconds")
    return float(value)

"""What the engine is told before it exists — one TOML file, read once.

The composition root cannot decide anything without this, and nothing else in
the system reads it: adapters are handed what they need, and Bridge Core is
handed its policy durations. Configuration is not a second source of truth, it
is what the truth is assembled *from*.

**TOML, via the standard library.** A person edits this file, so it is not JSON;
`tomllib` reads it and cannot write it, which is the right shape for a file the
engine never owns.

**A seam names a factory as `module:attribute`, and the composition root is the
only thing that imports it.** That is deliberate reach: a deployment's own
wiring is private, and a compiled-in table of allowed names could not name it.
The consequence is stated plainly rather than hidden — *this file is executed
with the privileges of the user who wrote it, and is exactly as trusted as the
engine itself*. It lives in that user's own application-support directory for
the same reason.

**Some things have no default.** An unconfigured Call, Companion Channel or
Session Launcher seam refuses to start, because an engine that silently loaded
nothing behind one is precisely ADR 0003's outage. The Delegated Turn's model is
a user-facing setting — the cost lever — so a default here would be the
hard-coding this repository forbids. And ADR 0004's three log *decisions* — the
cap, the retained generations, the stripped environment prefixes — are numbers a
68 MB outage measured, so an in-code fallback would quietly reinstate a value
that measurement proved matters.

**The control-plane CLI is a location that may be stated.** A generated thread
is told where the CLI really is, and the engine derives that from its own
installation. A bundle moves it, so `[delegate] cli` exists for the bundle to
say where — an override, read as a path and never as a default.

Paths *do* have defaults, because they are locations rather than decisions, and
they are three different locations on purpose: the durable state and the log live
in Application Support, and the socket lives in a short runtime root because
Darwin caps an `AF_UNIX` path at 103 bytes. See `docs/control-plane.md`. The log
path is a location by that same rule, which is why it has a default while the
three log decisions beside it do not.

Keys this file does not know about are left for the tickets that own them:
packaging keys arrive with the bundle. Reserving names for other people's work is
designing it for them.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from gpt_voicecoding.core.persistence import default_state_path
from gpt_voicecoding.core.policy import CorePolicy
from gpt_voicecoding.seams.identity import AgentKind

#: Where the engine looks when nothing tells it otherwise.
CONFIG_FILE_NAME = "config.toml"

#: The short runtime root the socket lives in. Cleared on reboot, which is right
#: for a socket, so the engine creates it at start rather than assuming it.
RUNTIME_ROOT = Path("/tmp")
SOCKET_FILE_NAME = "control.sock"

#: The engine's own log, beside its own state. ADR 0004: it owns this file.
LOG_FILE_NAME = "engine.log"

#: Every seam the composition root must fill before the engine may serve.
REQUIRED_SEAMS = ("call", "companion_channel", "session_launcher")


class ConfigError(Exception):
    """The configuration cannot be read, or does not say enough to start."""


def default_socket_path(uid: int | None = None) -> Path:
    """A path short enough to bind, and private to one user.

    Per-uid rather than shared, so two accounts on one machine each get their
    own engine rather than one refusing the other's socket.
    """
    owner = os.geteuid() if uid is None else uid
    return RUNTIME_ROOT / f"gpt-voicecoding-{owner}" / SOCKET_FILE_NAME


def default_config_path(base_dir: Path | None = None) -> Path:
    """Beside the state file, in the engine's own application-support directory."""
    return default_state_path(base_dir).with_name(CONFIG_FILE_NAME)


def default_log_path(base_dir: Path | None = None) -> Path:
    """Also beside the state file. A location, so it defaults; see the module note."""
    return default_state_path(base_dir).with_name(LOG_FILE_NAME)


@dataclass(frozen=True, slots=True)
class AdapterSelection:
    """Which implementation fills each seam, as `module:attribute` references.

    Each is called with the event sink and returns the adapter, so the engine
    hands every adapter the one way it speaks upward and nothing else.
    """

    call: str
    companion_channel: str
    session_launcher: str
    agents: dict[AgentKind, str] = field(default_factory=dict)
    #: `[adapters.settings.<seam>]`, forwarded to that seam's factory untouched.
    #: The root never reads a key: only the adapter knows what its own keys mean,
    #: and a root that parsed them would be growing adapter-shaped knowledge.
    #: A seam with no table is handed nothing at all, so an adapter that takes
    #: only the sink stays constructible exactly as before.
    settings: dict[str, dict[str, Any]] = field(default_factory=dict)

    def settings_for(self, seam: str) -> dict[str, Any] | None:
        """The table this seam was given, or None when it was given none."""
        return self.settings.get(seam)

    def as_mapping(self) -> dict[str, str]:
        """Every seam name to what configuration named for it — the configured side."""
        named = {
            "call": self.call,
            "companion_channel": self.companion_channel,
            "session_launcher": self.session_launcher,
        }
        named.update({f"agent.{agent}": reference for agent, reference in self.agents.items()})
        return named


@dataclass(frozen=True, slots=True)
class LogConfig:
    """ADR 0004's four values: one location, and three decisions with no default.

    The engine opens `path` itself and rotates it by rename, so this is the whole
    of what it needs to be told about its own log. Nothing here describes an
    adapter's log: a log the engine cannot tell to reopen belongs to whoever owns
    the process holding it, and Bridge Core never enumerates such paths.
    """

    path: Path
    #: The cap every generation is held to. `max_bytes` x (`retained_files` + 1)
    #: is the ceiling the family occupies on disk.
    max_bytes: int
    retained_files: int
    #: Environment variables whose names start with one of these are dropped once,
    #: by the process that spawns others, rather than at each spawn site.
    stripped_environment_prefixes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EngineConfig:
    """Everything the composition root needs, and nothing it does not."""

    adapters: AdapterSelection
    #: The Delegated Turn's model — the cost lever, and the user's to set.
    delegated_turn_model: str
    #: Where the control-plane CLI really is, when this installation moved it.
    #: None means the engine derives it from its own interpreter's scripts.
    control_plane_cli: Path | None
    socket_path: Path
    state_path: Path
    policy: CorePolicy
    log: LogConfig


def load(path: Path) -> EngineConfig:
    """Read one configuration file, refusing anything it cannot start from."""
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        raise ConfigError(f"no engine configuration at {path}") from None
    except OSError as error:
        raise ConfigError(f"cannot read {path}: {error}") from None

    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"{path} is not readable as TOML: {error}") from None

    return of(document, source=Path(path))


def of(document: dict[str, Any], *, source: Path | None = None) -> EngineConfig:
    """Read an already-decoded document. `load` is this, plus the file."""
    where = f" in {source}" if source is not None else ""
    engine = _section(document, "engine", where)
    adapters = _adapters(_section(document, "adapters", where), where)
    delegate = _section(document, "delegate", where)

    model = delegate.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ConfigError(
            f"no delegated-turn model{where}: set [delegate] model — it is the cost "
            "lever and the engine has no default for it"
        )

    return EngineConfig(
        adapters=adapters,
        delegated_turn_model=model.strip(),
        control_plane_cli=_optional_path(delegate, "cli", where),
        socket_path=_path(engine, "socket_path", default_socket_path(), where),
        state_path=_path(engine, "state_path", default_state_path(), where),
        policy=_policy(_section(document, "policy", where), where),
        log=_log(_section(document, "log", where), where),
    )


def _section(document: dict[str, Any], name: str, where: str) -> dict[str, Any]:
    section = document.get(name, {})
    if not isinstance(section, dict):
        raise ConfigError(f"[{name}]{where} must be a table")
    return section


def _adapters(section: dict[str, Any], where: str) -> AdapterSelection:
    filled: dict[str, str] = {}
    for seam in REQUIRED_SEAMS:
        reference = section.get(seam)
        if not isinstance(reference, str) or not reference.strip():
            raise ConfigError(_nothing_behind(seam, where))
        filled[seam] = _reference(reference, f"[adapters] {seam}", where)

    raw_agents = section.get("agents", {})
    if not isinstance(raw_agents, dict) or not raw_agents:
        raise ConfigError(
            f"no agent adapter{where}: set at least one of [adapters.agents] "
            + ", ".join(str(kind) for kind in AgentKind)
        )

    agents: dict[AgentKind, str] = {}
    for name, reference in raw_agents.items():
        try:
            agent = AgentKind(name)
        except ValueError:
            known = ", ".join(str(kind) for kind in AgentKind)
            raise ConfigError(
                f"[adapters.agents] {name}{where} is not an agent this system runs: {known}"
            ) from None
        if not isinstance(reference, str) or not reference.strip():
            raise ConfigError(f"[adapters.agents] {name}{where} names no implementation")
        agents[agent] = _reference(reference, f"[adapters.agents] {name}", where)

    chosen = AdapterSelection(
        call=filled["call"],
        companion_channel=filled["companion_channel"],
        session_launcher=filled["session_launcher"],
        agents=agents,
    )
    return replace(chosen, settings=_adapter_settings(section, chosen, where))


def _adapter_settings(
    section: dict[str, Any], chosen: AdapterSelection, where: str
) -> dict[str, dict[str, Any]]:
    """Read `[adapters.settings]`, checking only that each table names a real seam.

    What is *inside* a table is never inspected here — that is the adapter's, and
    it refuses the keys it does not have. What is checked is the name on the
    table, because settings addressed to a seam this engine has no adapter for
    are settings that will silently never be applied.
    """
    raw = section.get("settings", {})
    if not isinstance(raw, dict):
        raise ConfigError(f"[adapters.settings]{where} must be a table")

    seams = set(chosen.as_mapping())
    read: dict[str, dict[str, Any]] = {}
    for seam, table in raw.items():
        if seam not in seams:
            raise ConfigError(
                f'[adapters.settings."{seam}"]{where} names no seam this engine fills: '
                + ", ".join(sorted(seams))
            )
        if not isinstance(table, dict):
            raise ConfigError(f'[adapters.settings."{seam}"]{where} must be a table')
        read[seam] = table
    return read


def _nothing_behind(seam: str, where: str) -> str:
    """Why an empty seam stops the engine, and what the operator should do."""
    if seam == "companion_channel":
        return (
            f"nothing is configured behind the companion_channel seam{where}. Running "
            "without one is legitimate, but the null implementation ships with the "
            "Companion Channel adapter and is not built yet, so this engine refuses to "
            "start rather than pretend it can reach you"
        )
    return (
        f"nothing is configured behind the {seam} seam{where}: an engine that starts with "
        f"an empty {seam} looks exactly like a healthy one until it is needed"
    )


def _reference(reference: str, key: str, where: str) -> str:
    """A factory reference is `module:attribute`; anything else fails now, not later."""
    module, separator, attribute = reference.strip().partition(":")
    if not separator or not module.strip() or not attribute.strip():
        raise ConfigError(
            f"{key}{where} must be written module:attribute; {reference!r} is not"
        )
    return reference.strip()


def _optional_path(section: dict[str, Any], key: str, where: str) -> Path | None:
    """A path this file may state and usually does not. Absent is not empty."""
    value = section.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"[delegate] {key}{where} must be a path, or be left out entirely")
    return Path(value.strip()).expanduser()


def _path(
    section: dict[str, Any], key: str, fallback: Path, where: str, *, table: str = "engine"
) -> Path:
    """A location, so it has a fallback. Decisions elsewhere in this file do not."""
    value = section.get(key)
    if value is None:
        return fallback
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"[{table}] {key}{where} must be a path")
    return Path(value.strip()).expanduser()


def _log(section: dict[str, Any], where: str) -> LogConfig:
    """ADR 0004's log, read whole. Its three decisions must be said out loud."""
    return LogConfig(
        path=_path(section, "path", default_log_path(), where, table="log"),
        max_bytes=_whole_number(section, "max_bytes", where, least=1),
        retained_files=_whole_number(section, "retained_files", where, least=0),
        stripped_environment_prefixes=_prefixes(section, where),
    )


def _whole_number(section: dict[str, Any], key: str, where: str, *, least: int) -> int:
    """A log bound the operator set. There is no fallback: see ADR 0004."""
    value = section.get(key)
    if value is None:
        raise ConfigError(
            f"no [log] {key}{where}: ADR 0004 gives the engine its own log and the "
            "bound is measured, not guessed, so this engine has no default for it"
        )
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"[log] {key}{where} must be a whole number of {_unit(key)}")
    if value < least:
        raise ConfigError(f"[log] {key}{where} must be at least {least}")
    return value


def _unit(key: str) -> str:
    return "bytes" if key == "max_bytes" else "files"


def _prefixes(section: dict[str, Any], where: str) -> tuple[str, ...]:
    """The noise to drop from the environment. An empty list is an answer; absent is not."""
    value = section.get("stripped_environment_prefixes")
    if value is None:
        raise ConfigError(
            f"no [log] stripped_environment_prefixes{where}: one inherited variable was "
            "98.1% of the reference implementation's log, so this list is stated even "
            "when it is empty"
        )
    if not isinstance(value, list) or any(
        not isinstance(prefix, str) or not prefix.strip() for prefix in value
    ):
        raise ConfigError(
            f"[log] stripped_environment_prefixes{where} must be a list of variable-name "
            "prefixes"
        )
    return tuple(prefix.strip() for prefix in value)


def _policy(section: dict[str, Any], where: str) -> CorePolicy:
    """The locked durations, dialled. The pipelines own what they mean."""
    numbers: dict[str, float] = {}
    for key in ("relay_ceiling_seconds", "approval_budget_seconds"):
        value = section.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ConfigError(f"[policy] {key}{where} must be a number of seconds")
        numbers[key] = float(value)
    try:
        return CorePolicy(**numbers)
    except ValueError as refusal:
        raise ConfigError(f"[policy]{where}: {refusal}") from None

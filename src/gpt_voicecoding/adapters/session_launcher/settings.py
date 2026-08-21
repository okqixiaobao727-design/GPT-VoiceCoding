"""What the Session Launcher is told, read out of one opaque table.

The composition root forwards `[adapters.settings.session_launcher]` without
looking inside it, and an unknown key refuses to start — the same two rules the
Agent spokes' settings modules state, for the same reason.

Locations and mechanics default; decisions do not. Where the `claude` and
`codex` binaries are, where this engine's per-launch runtime files go, and what
terminal type a Session is given are all locations or mechanics. Which
permission mode a Session runs in is **not** a setting, and its absence here is
the decision: see `plan.py`.

**Every binary is named as a path, never as a word.** A launcher that shells out
by name inherits every function and alias in the user's environment, and inherits
them quietly — this machine defines a `claude` shell function that rewrites the
invocation into a different program, and the only symptom was an unexpected
channel name in a session banner. Discovery here is `shutil.which`, which reads
`PATH` and finds files; it cannot see a shell function, and what it finds is
recorded as an absolute path.
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from gpt_voicecoding.seams.identity import AgentKind

#: A short runtime root. The same length reasoning the Claude spoke's socket
#: directory states: a Unix socket path under a long application-support
#: directory cannot be bound on Darwin, and this is where per-launch sockets go.
DEFAULT_RUNTIME_DIRECTORY = Path("/tmp")

#: Where Claude Code writes one record per live process. A location, so it
#: defaults — and it is not ours to write to, only to read.
DEFAULT_REGISTRY_DIRECTORY = Path.home() / ".claude" / "sessions"

#: What a launched Session is told its terminal is.
#:
#: This is a mechanic rather than a decision, and it is unavoidable rather than
#: optional: the direct-child adapter allocates the pseudo-terminal itself, so
#: there is no terminal emulator anywhere in the picture to inherit a `TERM`
#: from. A TUI given no `TERM` degrades to something nobody would want to look
#: at, and a TUI given a `TERM` its terminal cannot honour renders garbage, so
#: the launcher states it and the operator may restate it.
DEFAULT_TERMINAL_TYPE = "xterm-256color"


class SettingsError(Exception):
    """The settings table names something this adapter does not have."""


@dataclass(frozen=True, slots=True)
class LauncherSettings:
    """Everything this seam may be told. Nothing policy-shaped appears here."""

    #: The real `claude` and `codex` executables. `None` means "find it on PATH
    #: at launch time", which is the right default for an installation that has
    #: not moved them and the wrong one for a bundle, which states them.
    claude_binary: Path | None = None
    codex_binary: Path | None = None
    #: The real `tmux`. Read only by the tmux adapter; the direct-child adapter
    #: never looks at it.
    tmux_binary: Path | None = None
    #: Where this engine puts what one launch needs: the Session Channel socket,
    #: the per-TUI app-server socket, and the rendered hook plugin.
    runtime_directory: Path = DEFAULT_RUNTIME_DIRECTORY
    #: Where a launched Claude Session says who it is. Read, never written.
    registry_directory: Path = DEFAULT_REGISTRY_DIRECTORY
    #: Which Python runs the `PermissionRequest` hook. A property of the
    #: deployment (ADR 0006), which is why the bundle may state it.
    interpreter: Path = Path(sys.executable)
    terminal_type: str = DEFAULT_TERMINAL_TYPE
    #: The tmux session every launched window is created in, so an operator has
    #: one name to attach to rather than one per Session.
    tmux_session_name: str = "gpt-voicecoding"

    def __post_init__(self) -> None:
        if not self.terminal_type.strip():
            raise SettingsError(
                "terminal_type must name a terminal; a Session with no TERM has no display"
            )
        if not self.tmux_session_name.strip():
            raise SettingsError("tmux_session_name must be a name")

    @classmethod
    def of(cls, table: dict[str, Any] | None) -> LauncherSettings:
        """Read one settings table, refusing every key it does not recognise."""
        if not table:
            return cls()
        known = {field.name for field in fields(cls)}
        unknown = sorted(set(table) - known)
        if unknown:
            raise SettingsError(
                f"[adapters.settings.session_launcher] does not have "
                f"{', '.join(unknown)}. It has: {', '.join(sorted(known))}"
            )
        return cls(**{key: _typed(key, value) for key, value in table.items()})

    def binary_for(self, agent: AgentKind) -> Path:
        """The real executable for one agent, as an absolute path, or a refusal.

        Never a bare word handed to a shell: see the module note. A refusal names
        what was looked for, because "launch failed" without the name is the
        least actionable sentence a launcher can produce.
        """
        stated = {AgentKind.CLAUDE: self.claude_binary, AgentKind.CODEX: self.codex_binary}[agent]
        return _resolved(stated, str(agent), f"{agent}_binary")

    def tmux(self) -> Path:
        """The real `tmux`, or a refusal naming it. The tmux adapter's alone."""
        return _resolved(self.tmux_binary, "tmux", "tmux_binary")


def _resolved(stated: Path | None, name: str, key: str) -> Path:
    """A stated path checked, or a discovered one, or a refusal naming both."""
    if stated is not None:
        if not stated.is_file():
            raise SettingsError(
                f"[adapters.settings.session_launcher] {key} names {stated}, which is not there"
            )
        return stated.resolve()
    found = shutil.which(name)
    if found is None:
        raise SettingsError(
            f"no `{name}` on PATH, and [adapters.settings.session_launcher] {key} names none"
        )
    return Path(found).resolve()


def _typed(key: str, value: Any) -> Any:
    """Turn one TOML value into what the field holds, or refuse in the operator's words."""
    if key.endswith(("_binary", "_directory", "interpreter")):
        if not isinstance(value, str) or not value.strip():
            raise SettingsError(f"{key} must be a path")
        return Path(value.strip()).expanduser()
    if not isinstance(value, str) or not value.strip():
        raise SettingsError(f"{key} must be a non-empty string")
    return value.strip()

"""The durable subset of Bridge Core's truth, and the only path it travels.

**The Session roster is not durable, and that is #74's decision.** A Session is
one the *user* started, so the roster is whatever is running on this machine
right now — a fact discovery re-reads every few seconds, and one no file can be
right about. Persisting it bought a restart a roster of rows it had no route to,
which is how a `live` row outlived its process by days (#26). What survives a
restart is switch state, and the first discovery after start-up rebuilds the
rest.

**Format: one JSON file, replaced atomically.** The alternative considered was
SQLite, which the reference implementation used. Every advantage SQLite brings —
concurrent writers, indexed queries, a schema — is something this architecture
has already ruled out: the durable subset is switch state alone,
there is exactly one writer because persistence is an internal component only
Bridge Core touches, and nothing queries it. What SQLite would add is a
migration story, a file nobody can read during an outage, and the standing
temptation to keep a second, richer copy of the truth beside the first — which is
precisely how the reference implementation grew two live ledgers. Crash safety
comes from writing a temporary file and renaming it over the target, so a reader
sees either the whole previous state or the whole new one, never half of either.
If a durable *history* is ever needed, that is a different store and a different
decision.

**Location.** `~/Library/Application Support/GPT-VoiceCoding/engine/state.json` by
default, and the base directory is a parameter so tests point it somewhere else
and nothing is hard-coded. The `engine/` subdirectory is not decoration: the
first-generation bridge already owns `runtime/` under that same application
directory, and the two must not tread on each other while both are installed.

**Nothing else may read this file.** Not `bridgectl`, not the menu-bar shell, not
an adapter. Every surface asks the hub.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gpt_voicecoding.core.errors import BridgeCoreError, StateFormatError
from gpt_voicecoding.core.switches import SwitchSnapshot
from gpt_voicecoding.locations import engine_directory

#: Kept apart from the first-generation bridge's `runtime/` in the same directory.
STATE_FILE_NAME = "state.json"


def default_state_path(base_dir: Path | None = None) -> Path:
    """Where the durable subset lives. `base_dir` exists so tests can move it."""
    return engine_directory(base_dir) / STATE_FILE_NAME


@dataclass(frozen=True, slots=True)
class PersistedState:
    """Exactly what survives a restart: switch state, and nothing else.

    The Session roster used to be here. It was removed with the restore path it
    fed (#74): the roster is what is running right now, discovery re-reads it on
    a cadence, and a file cannot be right about it.
    """

    #: Bumped when the shape below changes incompatibly. An unrecognised version
    #: fails closed rather than being read optimistically.
    #:
    #: **Unchanged when the Session roster left** (#74). Dropping a key is not an
    #: incompatible change for a reader: a file this engine wrote before the
    #: roster left still carries every switch, and its `sessions` key is simply
    #: not read. Bumping would have refused the switch state of every engine
    #: already installed on the user's machine — the master switch among it —
    #: over a key that no longer matters.
    VERSION = 1

    switches: SwitchSnapshot


class StateStore:
    """Reads and writes the one state file. Bridge Core's component, no one else's."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> PersistedState | None:
        """The persisted state, or None on a first run. Refuses anything it cannot read."""
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as error:
            raise StateFormatError(self._path, str(error)) from error

        try:
            document = json.loads(raw)
        except json.JSONDecodeError as error:
            raise StateFormatError(self._path, f"not JSON: {error}") from error

        if not isinstance(document, dict):
            raise StateFormatError(self._path, "expected a JSON object")

        version = document.get("version")
        if version != PersistedState.VERSION:
            raise StateFormatError(
                self._path,
                f"written by version {version}, this engine reads {PersistedState.VERSION}",
            )

        try:
            switches = _read_switches(document["switches"])
        except (BridgeCoreError, KeyError, TypeError, ValueError) as error:
            raise StateFormatError(self._path, str(error)) from error

        return PersistedState(switches=switches)

    def save(self, state: PersistedState) -> None:
        """Replace the state file atomically. A reader never sees a half-written file."""
        document = {
            "version": PersistedState.VERSION,
            "switches": state.switches.as_mapping(),
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(f".{self._path.name}.writing")

        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(temporary, self._path)
        _fsync_directory(self._path.parent)


def _fsync_directory(directory: Path) -> None:
    """Make the rename itself durable, not only the bytes it points at."""
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_switches(raw: Any) -> SwitchSnapshot:
    """Every switch has exactly two states, so anything but a bool is corruption.

    Read optimistically, a `"duty": "off"` would restore as *on* — a truthy
    string is the quietest way for the master switch to flip itself.
    """
    if not isinstance(raw, dict):
        raise TypeError("switches must be a JSON object")
    for name, state in raw.items():
        if not isinstance(state, bool):
            raise TypeError(f"switch {name!r} is {state!r}, which is not on or off")
    return SwitchSnapshot.of(dict(raw))

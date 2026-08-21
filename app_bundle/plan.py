"""One build, decided in full before any of it happens.

Everything here is resolved from configuration and from files on disk: which
interpreter this host needs, which lock governs it, what the bundle is called,
and where each piece lands. Nothing here downloads, spawns or writes — so the
whole of it can be held still by a test, which is the point.

The signing order is deliberately **not** resolved at this moment. It is a
function of the tree that assembly produces, and reading it before assembly ran
would be reading a directory that does not exist yet. `BuildPlan.signing()` is
called once, after the last file is in place and before the first signature is
made.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app_bundle import inputs, lock, signing

#: Where the built bundle lands. Beside SwiftPM's own output, and ignored by git
#: for the same reason that is.
BUILD_ROOT = inputs.SHELL_PACKAGE / ".build"

#: The SwiftPM configuration a release build uses. `debug` exists for the
#: developer loop and is not what is shipped.
DEFAULT_CONFIGURATION = "release"


@dataclass(frozen=True, slots=True)
class BuildPlan:
    """What this build is, before any of it has happened."""

    triple: str
    interpreter: inputs.Interpreter
    locked: tuple[lock.Locked, ...]
    identity: inputs.BundleIdentity
    app: Path
    configuration: str
    #: Set when the engine is left out — the developer bundle the shell's own
    #: resolver falls through past, which is a stated feature rather than a
    #: degraded build.
    without_engine: bool

    @property
    def engine_root(self) -> Path:
        """Where the interpreter tree goes. ADR 0005: inside the bundle, or nowhere."""
        return self.app / inputs.RESOURCES / inputs.ENGINE_ROOT

    @property
    def engine_cli(self) -> Path:
        """What `[delegate] cli` names. There is no second copy in `Contents/MacOS`."""
        return self.app / inputs.RESOURCES / inputs.ENGINE_CLI

    @property
    def executable(self) -> Path:
        return self.app / inputs.MACOS / self.identity.executable

    @property
    def lock_path(self) -> Path:
        return inputs.lock_for(self.triple)

    def signing(self) -> signing.SigningPlan:
        """Enumerate the assembled bundle and put it in signing order."""
        return signing.plan_for(self.app, entitlements=inputs.ENTITLEMENTS)

    @classmethod
    def resolve(
        cls,
        *,
        machine: str | None = None,
        configuration: str = DEFAULT_CONFIGURATION,
        build_root: Path = BUILD_ROOT,
        without_engine: bool = False,
    ) -> BuildPlan:
        """Decide the whole build, or refuse by name.

        A host with no pinned interpreter, or no lock for its triple, is turned
        away here — before a byte is downloaded — with the sentence that says
        what to do about it. An unpinned fallback would mean signing a set of
        binaries nobody reviewed, which is the one thing this pipeline exists to
        prevent.
        """
        triple = inputs.host_triple(machine)
        interpreter = inputs.interpreter_for(triple)
        identity = inputs.identity()
        locked = () if without_engine else lock.read(inputs.lock_for(triple))
        return cls(
            triple=triple,
            interpreter=interpreter,
            locked=locked,
            identity=identity,
            app=build_root / identity.app_directory_name,
            configuration=configuration,
            without_engine=without_engine,
        )

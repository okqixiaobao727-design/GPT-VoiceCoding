"""Every external input this pipeline has, named in one place.

A build script that scatters a release tag, a checksum and a lock path through
its steps has three things to keep in step and no way to see them at once. They
are all here, and the doing side reads them from here rather than carrying its
own copy.

**What is deliberately *not* here: the bundle's identity.** The identifier, the
display name, the microphone usage string and `LSUIElement` live in
`shell/Resources/Info.plist`, which is the one file that holds them — this module
reads that file rather than repeating any of it. The microphone grant attaches to
`CFBundleIdentifier` (ADR 0005), and a second copy of that string is a second
thing that can be wrong about what the user already granted.
"""

from __future__ import annotations

import platform
import plistlib
from dataclasses import dataclass
from pathlib import Path

#: This package sits at the repository root, beside `src/` and `shell/`.
REPO_ROOT = Path(__file__).resolve().parent.parent

# --- python-build-standalone ------------------------------------------------
#
# Route (a), locked by ADR 0005 and the packaging research: a relocatable
# `install_only` build under `Contents/Resources/engine/`. Not the framework
# CPython that Homebrew ships and not a virtual environment over one — those
# re-execute `Python.app/Contents/MacOS/Python` from its original location, so
# the process that ends up running is *outside* the bundle even though the shell
# spawned the one inside it, and bundle containment is the whole mechanism.

PBS_REPOSITORY = "astral-sh/python-build-standalone"

#: The release these checksums were taken from. Pinned, because a pipeline whose
#: job is to sign a set of Mach-O files must not let an unpinned upstream change
#: that set underneath it.
PBS_RELEASE_TAG = "20260814"

#: Not `install_only_stripped`: the stripped build saves tens of megabytes and
#: takes the symbols a crash report needs with it, and v0's users are developers.
PBS_FLAVOUR = "install_only"


@dataclass(frozen=True, slots=True)
class Interpreter:
    """One python-build-standalone asset, pinned by version and by content."""

    triple: str
    version: str
    sha256: str

    @property
    def asset(self) -> str:
        return f"cpython-{self.version}+{PBS_RELEASE_TAG}-{self.triple}-{PBS_FLAVOUR}.tar.gz"

    @property
    def url(self) -> str:
        return (
            f"https://github.com/{PBS_REPOSITORY}/releases/download/{PBS_RELEASE_TAG}/{self.asset}"
        )


#: The interpreters this repository is prepared to bundle, by host triple.
#:
#: v0 ships one. python-build-standalone publishes no `universal2` build, so a
#: universal `.app` would mean lipo-ing two interpreters and two sets of wheels —
#: adoption-era work that route (a) accommodates later without architectural
#: change (charter decision 9, ADR 0005). Adding an architecture is one entry
#: here plus one lock file, and both are deliberate reviewed acts.
INTERPRETERS: dict[str, Interpreter] = {
    "aarch64-apple-darwin": Interpreter(
        triple="aarch64-apple-darwin",
        version="3.12.14",
        sha256="4572133a5542f306b9bdb155da5800f9e38950cd0a98d469b832ce256fe299ea",
    ),
}

#: `platform.machine()` speaks Apple's names; python-build-standalone speaks the
#: target triple. One translation, in one place.
HOST_TRIPLES = {
    "arm64": "aarch64-apple-darwin",
    "x86_64": "x86_64-apple-darwin",
}


class InputError(Exception):
    """An input this pipeline needs is missing, and it will not invent one."""


class UnsupportedHost(InputError):
    """This machine has no pinned interpreter, and the build will not guess one."""


def host_triple(machine: str | None = None) -> str:
    """The python-build-standalone triple for this machine."""
    found = platform.machine() if machine is None else machine
    try:
        return HOST_TRIPLES[found]
    except KeyError:
        raise UnsupportedHost(f"no python-build-standalone triple is known for {found!r}") from None


def interpreter_for(triple: str) -> Interpreter:
    """The pinned interpreter for a triple, or a refusal that names the gap.

    A fallback to an unpinned download would be the silent-fallback shape this
    project bans, in the one place where it would also mean signing a Mach-O set
    nobody reviewed.
    """
    try:
        return INTERPRETERS[triple]
    except KeyError:
        raise UnsupportedHost(
            f"no interpreter is pinned for {triple}. v0 bundles "
            f"{', '.join(sorted(INTERPRETERS))} only; adding one means an entry in "
            f"app_bundle/inputs.py and a lock file in {LOCKS.relative_to(REPO_ROOT)}"
        ) from None


# --- The locked dependency set ----------------------------------------------

#: One lock per triple, because a hash-pinned lock is platform-specific: an
#: `aarch64` wheel's SHA256 is not an `x86_64` wheel's.
LOCKS = REPO_ROOT / "app_bundle" / "locks"

#: The extras the bundle resolves against. `voice` is optional for a `pip
#: install` — the control plane, the Relays and the whole test suite run without
#: a compiled media stack — and it is not optional here: an `.app` whose Live
#: Call cannot open a microphone is the product with its one feature missing.
BUNDLED_EXTRAS = ("voice",)


def bundled_requirement(root: Path = REPO_ROOT) -> str:
    """This checkout, with the extras the bundle needs, as pip spells it."""
    return f"{root}[{','.join(BUNDLED_EXTRAS)}]"


def lock_for(triple: str) -> Path:
    return LOCKS / f"{triple}.lock"


# --- The bundle's own layout ------------------------------------------------

APP_SUFFIX = ".app"
CONTENTS = Path("Contents")
MACOS = CONTENTS / "MacOS"
RESOURCES = CONTENTS / "Resources"

#: Where the engine's interpreter sits, relative to `Contents/Resources`. This
#: string is also `ShellCore.BundleLayout.engineInterpreterRelativePath`, and a
#: test holds the two to each other: the shell looks here first, and a bundle
#: that put it somewhere else would silently take the developer path instead.
ENGINE_INTERPRETER = Path("engine/bin/python3")

#: The engine tree's root inside the bundle.
ENGINE_ROOT = ENGINE_INTERPRETER.parent.parent

#: The control-plane CLI, as the bundle really lays it out. `pip` puts a console
#: script beside the interpreter that installed it, and its shebang names that
#: interpreter — so this is where `[delegate] cli` points, and there is no second
#: copy in `Contents/MacOS/`.
ENGINE_CLI = ENGINE_ROOT / "bin" / "bridgectl"

#: Shipped, not written: the engine's configuration is a file the user owns and
#: the engine only reads, so the bundle carries an example and the user copies it.
CONFIG_EXAMPLE = "config.example.toml"

#: The rights the bundled interpreter is signed with. The app's Info.plist holds
#: the sentence macOS shows; this holds what the process is allowed to open.
ENTITLEMENTS = REPO_ROOT / "app_bundle" / "engine.entitlements"

#: Where the shell's identity lives, and the only place it lives.
INFO_PLIST = REPO_ROOT / "shell" / "Resources" / "Info.plist"

#: The SwiftPM product the shell builds.
SHELL_PRODUCT = "GPTVoiceCodingShell"
SHELL_PACKAGE = REPO_ROOT / "shell"


@dataclass(frozen=True, slots=True)
class BundleIdentity:
    """The three strings the bundle is named by, read from the one file that has them."""

    identifier: str
    executable: str
    name: str

    @property
    def app_directory_name(self) -> str:
        return f"{self.name}{APP_SUFFIX}"


def identity(plist: Path = INFO_PLIST) -> BundleIdentity:
    """Read the bundle's identity out of `Info.plist`. Never invents a value."""
    with plist.open("rb") as handle:
        read = plistlib.load(handle)
    missing = [
        key
        for key in ("CFBundleIdentifier", "CFBundleExecutable", "CFBundleName")
        if not read.get(key)
    ]
    if missing:
        raise InputError(f"{plist} does not say {', '.join(missing)}")
    return BundleIdentity(
        identifier=read["CFBundleIdentifier"],
        executable=read["CFBundleExecutable"],
        name=read["CFBundleName"],
    )

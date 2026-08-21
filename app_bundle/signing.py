"""The order the bundle is signed in, decided before anything is signed.

`codesign --deep` is deprecated for signing and, more to the point, never
discovers `Contents/Resources` at all — so the set of things to sign is
enumerated here rather than delegated to a flag. `--deep --strict` remains the
right thing to *verify* with, and the pipeline still runs it; it is simply not
the thing that finds the files.

Two properties make this worth being a plan rather than a loop:

* **Inside-out.** Signing a bundle seals it against its contents at that moment.
  Anything re-signed underneath afterwards leaves a seal that no longer matches,
  and `--verify` agrees with the result right up until Gatekeeper does not. So
  the deepest files go first and the `.app` goes last, always.
* **Once each.** The tree is full of symlinks — `bin/python3` points at
  `bin/python3.12` — and signing a file twice under two names means the second
  signature replaces the first, silently discarding the entitlements the first
  one carried.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app_bundle import inputs, mach_o


@dataclass(frozen=True, slots=True)
class SigningStep:
    """One `codesign` invocation: what is signed, and what it is signed with."""

    path: Path
    #: The entitlements plist, for the one executable that needs rights of its
    #: own. `None` everywhere else — the locked decision is to start with zero
    #: extra entitlements and add `allow-jit` only if the audio callback proves
    #: it needs one.
    entitlements: Path | None = None

    @property
    def is_bundle(self) -> bool:
        """A `.app` is signed as a bundle; everything else is a loose Mach-O file."""
        return self.path.suffix == inputs.APP_SUFFIX


@dataclass(frozen=True, slots=True)
class SigningPlan:
    """Every `codesign` this build will run, in the order it will run them."""

    steps: tuple[SigningStep, ...]

    @property
    def entitled(self) -> tuple[Path, ...]:
        """The executables signed with rights of their own. Should be exactly one."""
        return tuple(step.path for step in self.steps if step.entitlements is not None)


def plan_for(app: Path, *, entitlements: Path) -> SigningPlan:
    """Enumerate the bundle and put it in signing order. Reads; never writes.

    The bundled interpreter is resolved through its symlink before it is matched,
    because the path the shell spawns (`engine/bin/python3`) and the path that
    exists as a file (`engine/bin/python3.12`) are not the same one, and it is the
    file that gets a signature.
    """
    interpreter = _bundled_interpreter(app)
    nested = sorted(
        mach_o.signable(app),
        key=lambda path: (-len(path.parts), path.as_posix()),
    )
    steps = [
        SigningStep(path=path, entitlements=entitlements if path == interpreter else None)
        for path in nested
    ]
    # The bundle last, and unconditionally: it is what the microphone grant
    # attaches to (ADR 0005), so it is never the thing that gets skipped.
    steps.append(SigningStep(path=app))
    return SigningPlan(steps=tuple(steps))


def _bundled_interpreter(app: Path) -> Path | None:
    """The engine's interpreter as a real file, or nothing if none is bundled.

    A bundle with no engine is the developer build, not an error: the shell's
    resolver falls through to `GPTVOICECODING_ENGINE_PYTHON` or `PATH`, which is
    a stated feature. It simply has nothing here to entitle.
    """
    named = app / inputs.RESOURCES / inputs.ENGINE_INTERPRETER
    if not named.exists():
        return None
    return named.resolve()

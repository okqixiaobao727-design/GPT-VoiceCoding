"""The doing side: execute the plan, in order, and stop at the first failure.

Nothing here decides anything. Which interpreter, which lock, what is signed and
in what order are all settled in `plan` and `signing` before this module is
called — so a step that looks surprising here is a plan that was wrong there,
and the plan is what the tests hold.

Every step is loud on failure. A build pipeline that carried on past a step is a
signed bundle missing a piece, which is the artefact that looks fine until the
one moment it matters.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

from app_bundle import console_script, inputs, lock
from app_bundle.plan import BuildPlan

#: Downloaded archives, kept between builds. Keyed by content, so a cache hit is
#: only ever the file the checksum names.
CACHE = Path(inputs.REPO_ROOT / "shell" / ".build" / "cache")

#: How the bundle is signed. Ad-hoc, because v0 ships no Developer ID and is not
#: notarized — a charter decision, with the known cost that the signature changes
#: per build and macOS may ask for the microphone again after one (ADR 0005).
AD_HOC_IDENTITY = "-"

#: The hardened runtime is **off** for v0. It is what a notarized build needs,
#: and v0 is explicitly not notarized; meanwhile it is the single most likely
#: cause of the CFFI audio-callback crash the `allow-jit` escape hatch was
#: reserved for, and its usual companion fix — `disable-library-validation` — is
#: forbidden. One constant, so turning it on later is one line.
HARDENED_RUNTIME = False

#: Python is told to use the bytecode that is already there and never to check a
#: timestamp, which is what stops it wanting to write into a signed bundle.
INVALIDATION_MODE = "unchecked-hash"


class BuildFailed(Exception):
    """A step did not do what it was asked. The build stops here."""


def run(argv: list[str], *, why: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """One external command, with its own words carried through on failure."""
    finished = subprocess.run(  # noqa: S603 - every argv here is built from pinned inputs
        argv,
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if finished.returncode != 0:
        raise BuildFailed(
            f"{why} failed ({' '.join(argv)}) with status {finished.returncode}\n"
            f"{finished.stderr.strip() or finished.stdout.strip()}"
        )
    return finished


# --- the interpreter --------------------------------------------------------


def fetch(interpreter: inputs.Interpreter, *, cache: Path = CACHE) -> Path:
    """Download the pinned interpreter, and refuse anything that is not it.

    The checksum is checked on a cache hit as well as on a fresh download: a
    cache is a place where a file can be replaced, and this pipeline's whole
    claim is that it signs a reviewed set of binaries.
    """
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / interpreter.asset
    if not archive.exists():
        with urllib.request.urlopen(interpreter.url) as response:  # noqa: S310 - a pinned https URL
            archive.write_bytes(response.read())
    found = hashlib.sha256(archive.read_bytes()).hexdigest()
    if found != interpreter.sha256:
        archive.unlink()
        raise BuildFailed(
            f"{interpreter.asset} hashes to {found}, and {interpreter.sha256} was pinned. "
            "The cached copy has been removed; run the build again"
        )
    return archive


def extract(archive: Path, *, into: Path) -> Path:
    """Unpack python-build-standalone's `python/` tree to where the bundle wants it."""
    if into.exists():
        shutil.rmtree(into)
    into.parent.mkdir(parents=True, exist_ok=True)
    staging = into.parent / f"{into.name}.staging"
    if staging.exists():
        shutil.rmtree(staging)
    with tarfile.open(archive) as tar:
        tar.extractall(staging, filter="tar")
    (staging / "python").rename(into)
    shutil.rmtree(staging)
    return into


# --- what goes in it --------------------------------------------------------


def install(plan: BuildPlan) -> None:
    """The locked third-party set, then the engine itself, and nothing else.

    `--require-hashes` with `--no-deps` means pip installs exactly the reviewed
    set: it cannot resolve one more distribution than the lock names, and it
    cannot accept an artefact whose content differs from the one that was
    reviewed. `--only-binary` because a source distribution would compile against
    this machine rather than against the bundled interpreter.
    """
    python = plan.engine_root / "bin" / "python3"
    run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-cache-dir",
            "--only-binary",
            ":all:",
            "--no-deps",
            "--require-hashes",
            "--requirement",
            str(plan.lock_path),
        ],
        why="installing the locked dependencies",
    )
    run(
        [str(python), "-m", "pip", "install", "--no-cache-dir", "--no-deps", str(inputs.REPO_ROOT)],
        why="installing the engine from this checkout",
    )


def relocate_cli(plan: BuildPlan) -> None:
    """Replace pip's absolute-shebang console script with one that moves.

    Written after `install`, because that is what put the script there, and
    before `sign`, because it is bundle content and the signature seals it.
    """
    plan.engine_cli.write_text(console_script.WRAPPER)
    plan.engine_cli.chmod(console_script.MODE)


def precompile(plan: BuildPlan) -> None:
    """Write every `.pyc` now, so the interpreter never wants to write one later.

    Nothing may write into the bundle at runtime, and there are two ways to hold
    that. The shell already takes one: `ProcessLauncher` sets
    `PYTHONDONTWRITEBYTECODE` when it spawns the *bundled* interpreter. This is
    the other, and it is not redundant — that variable covers only the engine the
    shell spawns, and the bundle is also run headless from a terminal and through
    the relocated `bridgectl`, neither of which the shell is anywhere near.

    `unchecked-hash` means the bytecode is used without a timestamp comparison,
    so a bundle whose mtimes moved in transit still starts without recompiling —
    which also happens to be the difference between the two escapes: with
    bytecode writing off and nothing pre-compiled, every start recompiles from
    source into memory.
    """
    python = plan.engine_root / "bin" / "python3"
    run(
        [
            str(python),
            "-m",
            "compileall",
            "-q",
            "-f",
            "--invalidation-mode",
            INVALIDATION_MODE,
            str(plan.engine_root / "lib"),
        ],
        why="pre-compiling the bundled library",
    )


# --- the bundle -------------------------------------------------------------


def assemble(plan: BuildPlan) -> None:
    """Build the shell and lay the bundle out around it."""
    run(
        [
            "swift",
            "build",
            "--package-path",
            str(inputs.SHELL_PACKAGE),
            "-c",
            plan.configuration,
            "--product",
            inputs.SHELL_PRODUCT,
        ],
        why="building the menu-bar shell",
    )
    built = (
        Path(
            run(
                [
                    "swift",
                    "build",
                    "--package-path",
                    str(inputs.SHELL_PACKAGE),
                    "-c",
                    plan.configuration,
                    "--show-bin-path",
                ],
                why="locating the shell's build products",
            ).stdout.strip()
        )
        / inputs.SHELL_PRODUCT
    )

    if plan.app.exists():
        shutil.rmtree(plan.app)
    plan.executable.parent.mkdir(parents=True)
    (plan.app / inputs.RESOURCES).mkdir(parents=True)
    shutil.copy2(built, plan.executable)
    shutil.copy2(inputs.INFO_PLIST, plan.app / inputs.CONTENTS / "Info.plist")
    shutil.copy2(
        inputs.REPO_ROOT / "app_bundle" / inputs.CONFIG_EXAMPLE,
        plan.app / inputs.RESOURCES / inputs.CONFIG_EXAMPLE,
    )


# --- signing ----------------------------------------------------------------


def sign(plan: BuildPlan) -> None:
    """Every Mach-O inside out, the bundle last, one `codesign` per file."""
    for step in plan.signing().steps:
        argv = ["codesign", "--force", "--sign", AD_HOC_IDENTITY, "--timestamp=none"]
        if HARDENED_RUNTIME:
            argv += ["--options", "runtime"]
        if step.entitlements is not None:
            argv += ["--entitlements", str(step.entitlements)]
        argv.append(str(step.path))
        run(argv, why=f"signing {step.path.name}")


def verify(plan: BuildPlan) -> None:
    """Two checks, because neither one alone covers the bundle.

    `--deep --strict` is the right thing to verify a bundle with — it is only
    deprecated for *signing* — but it does not walk `Contents/Resources`, which
    is where the engine lives. So the enumeration that produced the signing plan
    is run again and every file it names is verified on its own.
    """
    run(["codesign", "--verify", "--deep", "--strict", str(plan.app)], why="verifying the bundle")
    for step in plan.signing().steps:
        if step.path == plan.app:
            continue
        run(["codesign", "--verify", str(step.path)], why=f"verifying {step.path.name}")


def verify_relocatable(plan: BuildPlan) -> None:
    """Copy the bundle somewhere else and run its CLI from there.

    This is the guard for a whole class, not for one bug: everything inside the
    bundle that names an absolute path built at build time works perfectly where
    it was built and nowhere else, and the user's very first action is to drag
    the `.app` into `/Applications`. `bridgectl --help` is the cheapest question
    that makes the bundled interpreter actually resolve itself and import the
    engine from its new location.
    """
    with tempfile.TemporaryDirectory() as elsewhere:
        moved = Path(elsewhere) / plan.app.name
        shutil.copytree(plan.app, moved, symlinks=True)
        run(
            [str(moved / inputs.RESOURCES / inputs.ENGINE_CLI), "--help"],
            why="running the bundled bridgectl from a path it was not built at",
        )


# --- the whole thing --------------------------------------------------------


def build(plan: BuildPlan) -> Path:
    """One command's worth of work, in the only order that is correct."""
    assemble(plan)
    if not plan.without_engine:
        extract(fetch(plan.interpreter), into=plan.engine_root)
        install(plan)
        relocate_cli(plan)
        precompile(plan)
    sign(plan)
    verify(plan)
    if not plan.without_engine:
        verify_relocatable(plan)
    return plan.app


# --- regenerating the lock --------------------------------------------------


def generate_lock(plan: BuildPlan, *, into: Path | None = None) -> Path:
    """Resolve the bundle's dependency set against the bundled interpreter.

    A deliberate, reviewed act rather than a build step: it is what changes the
    set of binaries this pipeline will sign. It runs against the *bundled*
    interpreter's own version and platform, because a lock resolved by some other
    Python is a lock for some other set of wheels.
    """
    destination = into or inputs.lock_for(plan.triple)
    python = plan.engine_root / "bin" / "python3"
    if not python.exists():
        extract(fetch(plan.interpreter), into=plan.engine_root)
    report = plan.engine_root.parent / "resolution.json"
    run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--dry-run",
            "--ignore-installed",
            "--no-cache-dir",
            "--only-binary",
            ":all:",
            "--report",
            str(report),
            inputs.bundled_requirement(),
        ],
        why="resolving the bundle's dependency set",
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(_as_lock(json.loads(report.read_text()), plan.triple))
    report.unlink()
    return destination


def _as_lock(report: dict, triple: str) -> str:
    """Turn pip's resolution report into a hash-pinned requirements file."""
    lines = [
        f"# Generated by `{lock.REGENERATE}` for {triple}.",
        "# Every entry is pinned and hashed: this is the set of binaries the",
        "# signing plan enumerates, so changing it is a reviewed act.",
        "#",
        "# The engine itself is not here — it is installed from the checkout with",
        "# --no-deps, because it is not published.",
    ]
    for installed in sorted(report["install"], key=lambda one: one["metadata"]["name"].lower()):
        if installed.get("is_direct") or installed.get("requested"):
            if installed["metadata"]["name"].replace("_", "-") == "gpt-voicecoding":
                continue
        name = installed["metadata"]["name"]
        version = installed["metadata"]["version"]
        digest = installed["download_info"]["archive_info"]["hashes"]["sha256"]
        lines.append(f"{name}=={version} \\")
        lines.append(f"    --hash=sha256:{digest}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    """`python -m app_bundle [build|lock] [--debug] [--without-engine]`."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    verb = arguments.pop(0) if arguments and not arguments[0].startswith("-") else "build"
    configuration = "debug" if "--debug" in arguments else "release"
    without_engine = "--without-engine" in arguments

    if verb == "lock":
        plan = BuildPlan.resolve(configuration=configuration, without_engine=True)
        print(f"wrote {generate_lock(plan)}")
        return 0
    if verb != "build":
        print(f"unknown command {verb!r}: expected `build` or `lock`", file=sys.stderr)
        return 2

    plan = BuildPlan.resolve(configuration=configuration, without_engine=without_engine)
    app = build(plan)
    print(f'\nBuilt {app}\n\n  open "{app}"\n')
    return 0

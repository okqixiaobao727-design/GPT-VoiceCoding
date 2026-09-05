"""The login `LaunchAgent` that starts Codex's shared app-server daemon — #82, #83.

Codex's shared daemon has to be running **before** the user opens a `codex`, or
that TUI settles on its own embedded app-server and can never be adopted
afterwards (#82, proved: a daemon started later left its loaded-thread roster at
zero). Engine start is too late and first Relay is later still, so the start is a
macOS login item. `daemon bootstrap`'s own updater loop is documented as not
reboot-persistent in Codex 0.149.1, which is why the job runs `daemon start`.

**No wrapper.** This job starts a daemon beside the user's `codex`; it does not
stand in front of it, rename it, or own any Session it serves (#68, #71, #82).

**One process action, and it is `bootstrap`.** ADR 0012's principle is *act, read
back, report*, not *write files only* — so this item renders the plist, writes it
through the boundary's atomic write, and then asks launchd to load the job now
rather than leaving the user's Codex half dead until they next log out. The
read-back is `launchctl print`, not the exit code: "already loaded" is not a
failure and launchd says so with a status nobody should have to interpret.

**Nothing here ever runs `bootout`, and that asymmetry is the rule.** *The
product starts a daemon the user's TUIs will join; it never stops one they are
attached to.* By the time an uninstall runs, the user's own `codex` sessions are
thin clients of this daemon, and a `bootout` would take every one of them down —
which is exactly what #83 forbids in the words "without stopping or deleting user
Sessions". So an uninstall removes the plist and lets the running daemon live out
the login session, and a changed render is written but not reloaded.

**A reconcile is not a supervisor.** #83's scope forbids a polling supervisor, and
this is not one: `KeepAlive` is absent from the job, and the only thing that ever
re-bootstraps a job that died is the next app launch (ADR 0012), which is an
event and not a timer.

**The loaded render is this product's own read-back — #132.** `launchctl print`
exposes the program but not the whole loaded definition, so the same program can
hide an older resource limit or log path. When this item bootstraps a job it
records the exact plist SHA and launchd's GUI-login ASID in `installation.json`.
Status compares that SHA with the file on disk and keeps the program comparison
as the foreign-origin guard. A changed ASID proves a new login loaded the bytes
that were on disk before this reconcile; a second reconcile in the same ASID
proves no reload at all. Missing evidence is an unknown render, never `current`.
Legacy has no equivalent: `legacy@1d32845:install.sh:174-195` loaded its job and
never recorded or read back the loaded render, so this behavior is **not ported**.

**Nothing in the rendered job is hard-coded.** The user, their home, `CODEX_HOME`
and the Codex version all come from the environment this runs in — the binary is
reached through the `current` symlink Codex's own updater moves, so the job
follows the version rather than pinning it. A rendered artifact naming a path
that was true only on the machine that rendered it is #38.

**Legacy: adapted.** `legacy@1d32845:scripts/launch-agent.py:53-70` is the job
render (`plistlib`, `RunAtLoad`, `StandardOutPath`/`StandardErrorPath`) and
`legacy@1d32845:install.sh:174-195` is the launchd handling (`bootout` /
`bootstrap` in the per-login `gui/<uid>` domain). Two things are dropped on the
way across: legacy's `KeepAlive`, because that job was a supervised daemon and
this one is a one-shot start, and legacy's whole `stop_launch_agent` path, for
the reason above. The **shared Codex daemon itself is dropped from porting,
because** gen 1 had no such daemon — `legacy@1d32845:bridge/codex.py` drove a
launched, wrapped, per-Session app-server, and its launch marker is not adapted.
"""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from xml.parsers.expat import ExpatError

from gpt_voicecoding.installation import (
    BootstrappedRender,
    Outcome,
    State,
    read_bootstrapped_render,
    remove_file,
    replace_text,
    write_bootstrapped_render,
)

#: How this item is named in a report.
NAME: Final = "codex-launch-agent"

#: The job's label, which is also its filename. Deliberately not the gen-1
#: `com.gpt-voicecoding.bridge` still sitting in real `~/Library/LaunchAgents`
#: directories: that one is #54's to dispose of, and a collision would have this
#: install silently replace a job it never wrote.
LABEL: Final = "com.gpt-voicecoding.codex-daemon"

#: Where the user's own login items live. macOS's directory, not Claude's — a
#: user who has never installed one simply does not have it yet, so unlike the
#: Claude config directory its absence is something to fix rather than to report.
LAUNCH_AGENTS_PARTS: Final = ("Library", "LaunchAgents")

#: Codex's own home, and the variable that moves it.
CODEX_HOME_VARIABLE: Final = "CODEX_HOME"
DEFAULT_CODEX_HOME_NAME: Final = ".codex"

#: The standalone managed binary, under the symlink Codex's updater moves. #82
#: chose this over the user's `PATH` deliberately, so both this LaunchAgent and
#: the Codex adapter derive the same file from the same ``CODEX_HOME``. What
#: `codex` resolves to in an interactive shell is whatever the user's shell says,
#: which on this product's own author's machine was a gen-1 wrapper function.
MANAGED_BINARY_PARTS: Final = ("packages", "standalone", "current", "codex")

#: What the job runs. `start` waits until the daemon's initialize is ready and
#: exits; `bootstrap` is the one whose updater does not survive a reboot (#82).
DAEMON_ARGUMENTS: Final = ("app-server", "daemon", "start")

#: What says the running daemon's versions, as JSON. It needs a live daemon: with
#: none running it fails on the control socket, which is a fact worth reporting
#: and not an error to raise.
VERSION_ARGUMENTS: Final = ("app-server", "daemon", "version")
CLI_VERSION_FIELD: Final = "cliVersion"
APP_SERVER_VERSION_FIELD: Final = "appServerVersion"

#: #129 measured launchd's soft default at 256 and its hard limit as unlimited;
#: the shared daemon held 271 descriptors after two days.  This is an install
#: invariant rather than a user setting, and both plist limits use this value.
OPEN_FILE_LIMIT: Final = 65_536

#: launchd's own path. Not resolved through `PATH`, because this is one of the
#: few binaries whose location is part of the operating system's contract.
LAUNCHCTL: Final = Path("/bin/launchctl")

#: How many subprocesses one run of this item may make: `launchctl print`,
#: `launchctl bootstrap`, and the `print` that reads the bootstrap back.
COMMANDS_PER_RUN: Final = 3

#: How long any one of them may take, and it is **derived, not chosen**. The only
#: measured ceiling in this picture is the shell's: it gives a whole reconcile
#: `Installation.deadline` seconds before it kills it, because this runs *before*
#: the engine is spawned and a wait without a ceiling is a product that never
#: starts. Three commands have to fit inside that with room for the rest of the
#: run, so each gets a third of it. `tests/test_codex_launch_agent.py` reads the
#: shell's number out of `Installation.swift` and holds the two together, which
#: is the guard #47 records as missing where a constant is spelled twice.
SHELL_RECONCILE_DEADLINE_SECONDS: Final = 30.0
COMMAND_TIMEOUT_SECONDS: Final = SHELL_RECONCILE_DEADLINE_SECONDS / COMMANDS_PER_RUN


def default_codex_home(environ: Mapping[str, str], home: Path | None = None) -> Path:
    """The Codex home this run installs for."""
    stated = environ.get(CODEX_HOME_VARIABLE)
    if stated and stated.strip():
        return Path(stated.strip()).expanduser()
    return (home or Path.home()) / DEFAULT_CODEX_HOME_NAME


def default_launch_agents_directory(home: Path | None = None) -> Path:
    return (home or Path.home()).joinpath(*LAUNCH_AGENTS_PARTS)


def managed_binary(codex_home: Path) -> Path:
    return codex_home.joinpath(*MANAGED_BINARY_PARTS)


def plist_path(launch_agents_directory: Path) -> Path:
    return launch_agents_directory / f"{LABEL}.plist"


def _run(arguments: Sequence[str]) -> tuple[int, str]:
    """Run one command and come back with its status and whatever it said."""
    try:
        finished = subprocess.run(  # noqa: S603 - every argument here is this module's own
            list(arguments),
            check=False,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except OSError as refusal:
        return (-1, str(refusal))
    except subprocess.TimeoutExpired:
        return (-1, f"{arguments[0]} did not answer within {COMMAND_TIMEOUT_SECONDS:.0f} seconds")
    return (finished.returncode, (finished.stdout + finished.stderr).strip())


@dataclass(frozen=True, slots=True)
class HeldJob:
    """The identity launchd exposes for one loaded job definition."""

    program: str
    login_asid: int | None


@dataclass(frozen=True, slots=True)
class Launchd:
    """The user's own launchd domain, and the two questions this item asks it.

    A per-login-session `gui/<uid>` domain rather than the system one, ported from
    `legacy@1d32845:install.sh:207-208`: this job belongs to whoever is logged in,
    because the Codex daemon it starts is theirs and serves their terminals.

    Every entry point below takes a `Launchd` it cannot default, and `run` is
    resolved when it is *called* rather than when this class is defined. Both are
    guards against the same accident, which is not hypothetical: two drafts of
    this module reached the launchd of the machine running the tests — the first
    loaded a job naming a plist pytest deleted a second later, and the second
    installed the real login job and started the real shared daemon. So there is
    no default `Launchd` for the same reason `base_dir` runs through
    `locations`, and `_run` is late-bound so `tests/conftest.py` can take the
    real `launchctl` away from the whole suite at once.
    """

    domain: str
    #: ``None`` is the real ``launchctl``, looked up at call time.
    run: Callable[[Sequence[str]], tuple[int, str]] | None = None

    def ask(self, arguments: Sequence[str]) -> tuple[int, str]:
        return (self.run or _run)(arguments)

    def held_job(self) -> HeldJob | None:
        """The identity launchd currently holds for this job, if any.

        Three answers, and the third is the one that matters. `None` is *not
        loaded*. A non-empty ``program`` is the program in the job launchd holds,
        which is **not** necessarily the one in the file on disk: nothing here
        ever reloads a job, so after a render changes, the file and the loaded
        job disagree until the next login. Asking launchd rather than reading our
        own file back is the only way a status run can say that out loud.

        An empty ``program`` is *loaded, and launchd did not say what it runs*.
        ``login_asid`` is the GUI login's audit session identifier. macOS changes
        it at login, not at a kickstart inside the same login, which lets install
        distinguish a genuine reload opportunity from a second reconcile (#132).
        """
        status, said = self.ask([str(LAUNCHCTL), "print", f"{self.domain}/{LABEL}"])
        if status != 0:
            return None
        found_program = re.search(r"^\s*program\s*=\s*(.+?)\s*$", said, re.MULTILINE)
        found_asid = re.search(r"^\s*asid\s*=\s*(\d+)\s*$", said, re.MULTILINE)
        return HeldJob(
            program=found_program.group(1) if found_program else "",
            login_asid=int(found_asid.group(1)) if found_asid else None,
        )

    def bootstrap(self, path: Path) -> tuple[HeldJob | None, str]:
        """Load the job now and return the identity read back from launchd.

        The exit status is not the answer: bootstrapping a job that is already
        loaded fails, and that is the state this is trying to reach. So it is
        attempted and then the identity read-back decides.
        """
        _, said = self.ask([str(LAUNCHCTL), "bootstrap", self.domain, str(path)])
        held = self.held_job()
        if held is not None:
            return (held, "")
        return (None, f"launchd did not load {LABEL}: {said or 'it said nothing'}")


def default_launchd() -> Launchd:
    return Launchd(domain=f"gui/{os.getuid()}")


def job(binary: Path, codex_home: Path, log_path: Path) -> dict[str, Any]:
    """The launchd job description, as a plist document.

    `RunAtLoad` and no `KeepAlive`: this starts the daemon once and exits, and a
    `KeepAlive` would have launchd restart `daemon start` forever the moment it
    finishes doing the one thing it exists to do.

    `CODEX_HOME` is written even when it is the default, because launchd hands a
    job none of the user's shell environment. Without it, a user who moved their
    Codex home would get a daemon on one home and TUIs on another, and an empty
    roster that nothing explains.
    """
    return {
        "Label": LABEL,
        "ProgramArguments": [str(binary), *DAEMON_ARGUMENTS],
        "RunAtLoad": True,
        "EnvironmentVariables": {CODEX_HOME_VARIABLE: str(codex_home)},
        "SoftResourceLimits": {"NumberOfFiles": OPEN_FILE_LIMIT},
        "HardResourceLimits": {"NumberOfFiles": OPEN_FILE_LIMIT},
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
    }


def render(document: Mapping[str, Any]) -> str:
    """The plist's contents. Text, so it goes through the boundary's one write."""
    return plistlib.dumps(dict(document), sort_keys=True).decode("utf-8")


@dataclass(frozen=True, slots=True)
class JobFile:
    """One read of the plist, including the exact bytes launchd could load."""

    document: dict[str, Any]
    sha256: str


def _read(path: Path) -> JobFile | None | str:
    """The job that is there, `None` when there is none, or why neither."""
    try:
        contents = path.read_bytes()
        document: Any = plistlib.loads(contents)
    except FileNotFoundError:
        return None
    except OSError as refusal:
        return f"{path}: {refusal}"
    # `plistlib` raises three unrelated types for "this is not a plist": its own
    # for a binary one, `ExpatError` for XML that does not close, and `ValueError`
    # for XML that closes around something a plist cannot hold. All three mean the
    # same thing here, and none of them may reach a caller as an exception: this
    # runs before the engine, from a shell that has nowhere to put a traceback.
    except (plistlib.InvalidFileException, ExpatError, ValueError) as unreadable:
        return f"{path}: not a property list, so this install would destroy it: {unreadable}"
    if not isinstance(document, dict):
        return f"{path}: does not contain a property-list dictionary"
    if document.get("Label") != LABEL:
        return (
            f"{path}: carries the job {document.get('Label')!r}, which this product "
            f"never wrote. Nothing was changed."
        )
    return JobFile(document=document, sha256=hashlib.sha256(contents).hexdigest())


def daemon_versions(
    codex_home: Path, run: Callable[[Sequence[str]], tuple[int, str]] | None = None
) -> str:
    """One sentence about the running daemon, for a status run to print.

    Never called on the install path: a subprocess to a socket that is usually
    absent belongs in the verb a person typed, not in the reconcile that runs
    before the engine at every launch.
    """
    binary = managed_binary(codex_home)
    status, said = (run or _run)([str(binary), *VERSION_ARGUMENTS])
    if status != 0:
        # The last line, because a `codex` refusal is a short reason under a
        # longer "Error:" banner and the reason is the part worth printing.
        reason = said.splitlines()[-1] if said else "it gave no reason"
        return f"the shared daemon is not answering: {reason}"
    try:
        reported: Any = json.loads(said)
    except json.JSONDecodeError:
        return f"the shared daemon answered, and not with JSON: {said[:120]}"
    if not isinstance(reported, dict):
        return "the shared daemon answered with something that is not a version document"
    cli = reported.get(CLI_VERSION_FIELD)
    app_server = reported.get(APP_SERVER_VERSION_FIELD)
    # Checked before they are compared, because a document with neither field
    # makes both of them `None` and `None == None` would report a daemon that
    # said nothing at all as a daemon whose versions agree.
    if not isinstance(cli, str) or not isinstance(app_server, str):
        return (
            "the shared daemon answered without saying its versions: "
            f"{CLI_VERSION_FIELD}={cli!r}, {APP_SERVER_VERSION_FIELD}={app_server!r}"
        )
    # A disagreement, reported as a disagreement (#233). This said "a Session
    # started by this CLI will not speak to that daemon", which is a prediction
    # and a wrong one: measured on 2026-09-05, a plain `codex --sandbox
    # workspace-write` from CLI 0.153.0 joins the 0.149.1 daemon and its thread
    # appears in `thread/loaded/list`. What keeps a TUI out is a `-c` override,
    # which makes it run its own core, and that is not a fact about versions.
    # The engine's own copy of this sentence is `codex/shared_daemon.py`'s
    # `DaemonAddress.note`, corrected in the same ticket — the two are the same
    # claim to a reader and were wrong in the same way.
    if cli != app_server:
        return f"the CLI is {cli!r} and the running app-server is {app_server!r}, so they disagree"
    return f"the shared daemon is answering, CLI and app-server both {cli!r}"


def _no_managed_binary(codex_home: Path) -> Outcome:
    return Outcome(
        NAME,
        State.ABSENT,
        note=(
            f"no managed Codex binary at {managed_binary(codex_home)} — nothing to start, "
            "so nothing to install"
        ),
    )


def _previous_render_note(path: Path) -> str:
    return f"{path} — loaded job is a previous render; applies at the next login"


def _unknown_render_note(path: Path) -> str:
    return f"{path} — loaded job is of unknown render; applies at the next login"


def _unknown_identity_note(path: Path, reason: str) -> str:
    return f"{path} — the job is loaded, and launchd {reason}; loaded render is unknown"


def _program_in(standing: JobFile | None) -> str | None:
    if standing is None:
        return None
    arguments = standing.document.get("ProgramArguments")
    if not isinstance(arguments, list) or not arguments or not isinstance(arguments[0], str):
        return None
    return arguments[0]


def _program_mismatch_note(path: Path, actual: str, expected: str) -> str:
    return (
        f"{path} is current, and the job launchd holds runs {actual}, not {expected}. "
        "It is not reloaded, because that would stop the daemon live Sessions are on; "
        "the file applies at the next login."
    )


def _loaded_identity_problem(path: Path, held: HeldJob | None, expected_program: str) -> str:
    if held is None or not held.program:
        return _unknown_identity_note(path, "did not say what it runs")
    if held.program != expected_program:
        return _program_mismatch_note(path, held.program, expected_program)
    if held.login_asid is None:
        return _unknown_identity_note(path, "did not say which login loaded it")
    return ""


def inspect(
    launch_agents_directory: Path,
    codex_home: Path,
    log_path: Path,
    record_path: Path,
    launchd: Launchd,
) -> Outcome:
    """What is on this machine, without changing any of it.

    Three facts, and all are reported: whether the plist is the one this build
    renders, whether launchd currently holds the job, and whether the recorded
    loaded-render SHA matches the file. A plist that is current with no job, or
    whose loaded SHA differs, is a machine that is not current now.
    """
    if not managed_binary(codex_home).exists():
        return _no_managed_binary(codex_home)

    path = plist_path(launch_agents_directory)
    standing = _read(path)
    if isinstance(standing, str):
        return Outcome(NAME, State.ABSENT, ok=False, note=standing)

    # Asked once. Asked twice, the two answers can differ — launchd is a live
    # thing — and the report would then carry a `state` decided by one reading
    # and a sentence describing the other.
    running = launchd.held_job()
    binary = managed_binary(codex_home)
    held = "the job is not loaded" if running is None else "the job is loaded"
    if standing is None:
        if running is not None:
            return Outcome(NAME, State.STALE, note=_unknown_render_note(path))
        return Outcome(NAME, State.ABSENT, note=f"no login job at {path} — {held}")
    if standing.document != job(binary, codex_home, log_path):
        return Outcome(
            NAME, State.STALE, note=f"{path} is a job this build would write differently"
        )
    if running is None:
        return Outcome(NAME, State.STALE, note=f"{path} is current, and {held}")
    identity_problem = _loaded_identity_problem(path, running, str(binary))
    if identity_problem:
        return Outcome(NAME, State.STALE, note=identity_problem)
    loaded = read_bootstrapped_render(record_path)
    if loaded is None or loaded.render_sha256 is None:
        return Outcome(NAME, State.STALE, note=_unknown_render_note(path))
    if loaded.login_asid is None:
        return Outcome(
            NAME,
            State.STALE,
            note=_unknown_identity_note(path, "did not record which login loaded it"),
        )
    if loaded.render_sha256 != standing.sha256:
        return Outcome(NAME, State.STALE, note=_previous_render_note(path))
    return Outcome(NAME, State.CURRENT, note=f"{path} — {held}")


def install(
    launch_agents_directory: Path,
    codex_home: Path,
    log_path: Path,
    record_path: Path,
    launchd: Launchd,
) -> Outcome:
    """Put the job where launchd finds it, and have launchd hold it now. Idempotent."""
    if not managed_binary(codex_home).exists():
        return _no_managed_binary(codex_home)

    path = plist_path(launch_agents_directory)
    standing = _read(path)
    if isinstance(standing, str):
        return Outcome(NAME, State.ABSENT, ok=False, note=standing)

    wanted = job(managed_binary(codex_home), codex_home, log_path)
    wanted_text = render(wanted)
    wanted_sha256 = hashlib.sha256(wanted_text.encode("utf-8")).hexdigest()
    held = launchd.held_job()  # asked before the write, so the note below is true of it
    loaded = read_bootstrapped_render(record_path)
    disk_program = _program_in(standing if isinstance(standing, JobFile) else None)

    if held is not None:
        # A missing record proves nothing about the job already in launchd. Keep
        # that uncertainty for this login. If the ASID changed, launchd loaded
        # whatever bytes were on disk *before this reconcile writes*, so those
        # bytes become the authority for the new login (#132 advisor ruling).
        if loaded is None:
            loaded = BootstrappedRender(render_sha256=None, login_asid=held.login_asid)
            failure = write_bootstrapped_render(record_path, loaded)
            if failure:
                return Outcome(NAME, State.STALE, ok=False, note=failure)
        elif loaded.login_asid is None:
            loaded = BootstrappedRender(render_sha256=None, login_asid=held.login_asid)
            failure = write_bootstrapped_render(record_path, loaded)
            if failure:
                return Outcome(NAME, State.STALE, ok=False, note=failure)
        elif held.login_asid is not None and held.login_asid != loaded.login_asid:
            loaded = BootstrappedRender(
                render_sha256=(
                    standing.sha256
                    if isinstance(standing, JobFile)
                    and bool(held.program)
                    and held.program == disk_program
                    else None
                ),
                login_asid=held.login_asid,
            )
            failure = write_bootstrapped_render(record_path, loaded)
            if failure:
                return Outcome(NAME, State.STALE, ok=False, note=failure)

    rewritten = False
    if standing is None or standing.document != wanted:
        # launchd will not spawn a job whose output path names a directory that
        # is not there, and it reports that as the job failing to start — a
        # failure whose reason would land in the file it could not open.
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as refusal:
            return Outcome(NAME, State.ABSENT, ok=False, note=f"{log_path.parent}: {refusal}")
        failure = replace_text(path, wanted_text)
        if failure:
            return Outcome(NAME, State.ABSENT, ok=False, note=failure)
        rewritten = True

    if held is not None:
        identity_problem = _loaded_identity_problem(path, held, str(managed_binary(codex_home)))
        if identity_problem:
            return Outcome(NAME, State.STALE, changed=rewritten, note=identity_problem)
        if rewritten:
            if loaded is not None and loaded.render_sha256 == wanted_sha256:
                return Outcome(
                    NAME,
                    State.CURRENT,
                    changed=True,
                    note=f"{path} written — the same render is already loaded",
                )
            if loaded is None or loaded.render_sha256 is None:
                return Outcome(
                    NAME,
                    State.STALE,
                    changed=True,
                    note=(
                        f"{path} written — loaded job is of unknown render; "
                        "applies at the next login"
                    ),
                )
            # Reloading it means `bootout`, and `bootout` stops the daemon the
            # user's own TUIs are attached to. The job the user has is the one
            # that was already right for them; the new render is for next login.
            return Outcome(
                NAME,
                State.STALE,
                changed=True,
                note=(
                    f"{path} written — the loaded job is the previous render and was not "
                    "reloaded, because that would stop the daemon live Sessions are on. "
                    "It applies at the next login."
                ),
            )
        if loaded is None or loaded.render_sha256 is None:
            return Outcome(NAME, State.STALE, note=_unknown_render_note(path))
        if loaded.login_asid is None:
            return Outcome(
                NAME,
                State.STALE,
                note=_unknown_identity_note(path, "did not record which login loaded it"),
            )
        if loaded.render_sha256 != standing.sha256:
            return Outcome(NAME, State.STALE, note=_previous_render_note(path))
        return Outcome(NAME, State.CURRENT, note=f"{path} — the job is loaded")

    bootstrapped, refusal = launchd.bootstrap(path)
    if refusal:
        return Outcome(NAME, State.STALE, changed=rewritten, ok=False, note=refusal)
    identity_problem = _loaded_identity_problem(path, bootstrapped, str(managed_binary(codex_home)))
    bootstrapped_asid = bootstrapped.login_asid if bootstrapped is not None else None
    loaded = BootstrappedRender(
        render_sha256=(wanted_sha256 if rewritten or standing is None else standing.sha256)
        if not identity_problem
        else None,
        login_asid=bootstrapped_asid,
    )
    failure = write_bootstrapped_render(record_path, loaded)
    if failure:
        return Outcome(NAME, State.STALE, changed=True, ok=False, note=failure)
    if identity_problem:
        return Outcome(NAME, State.STALE, changed=True, note=identity_problem)
    return Outcome(NAME, State.CURRENT, changed=True, note=f"{path} — the job is loaded")


def uninstall(launch_agents_directory: Path) -> Outcome:
    """Take the job's file back, and leave the running daemon alone.

    No `bootout`. See the module note: by now the user's own Codex Sessions are
    thin clients of the daemon this job started, and stopping it would take every
    one of them down. Without the file, launchd does not load it again.
    """
    path = plist_path(launch_agents_directory)
    standing = _read(path)
    if isinstance(standing, str):
        return Outcome(NAME, State.STALE, ok=False, note=standing)
    if standing is None:
        return Outcome(NAME, State.ABSENT, note=f"nothing of ours at {path}")

    failure = remove_file(path)
    if failure:
        return Outcome(NAME, State.STALE, ok=False, note=failure)
    return Outcome(
        NAME,
        State.ABSENT,
        changed=True,
        note=(
            f"{path} removed — a daemon that is already running was left alone and "
            "lives until logout"
        ),
    )

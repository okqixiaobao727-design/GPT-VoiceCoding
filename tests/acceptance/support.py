"""What the acceptance run is made of: a run directory, a journal, and one engine.

`docs/acceptance-design.md` said this module would be `tests/e2e/support.py`,
lifted. **The E2E suite was never built**, and its standing is #62's decision, not
this ticket's — so the support code lives here, owned by the acceptance, and #62
lifts from it if it decides the fake-far-side suite is worth building. What the
two would genuinely have shared is small: a `bridgectl` runner and derived
deadlines. Everything else here — the real bundle, the real credentials, the run
directory, the verdict — has no counterpart in a hermetic suite.

Three rules this module exists to hold:

* **Every product action goes through the bundle's own `bridgectl`.** Not the
  editable install's, not an in-process call. The thing being accepted is the
  `.app` on disk.
* **Nothing is asserted that was not observed.** A control-plane reply says what
  the engine believes; the workspace, the chat and `engine.log` say what happened.
  Both go in the journal, marked for which they are.
* **Deadlines are derived, never picked.** Reply deadlines come from
  `control_plane.client.timeout_for`, the same function `bridgectl` itself uses.
  The far-side waits are the only numbers chosen here, and each one carries what
  it was measured against.
"""

from __future__ import annotations

import asyncio
import filecmp
import json
import os
import re
import shutil
import subprocess
import threading
import time
import tomllib
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from gpt_voicecoding.adapters.agent.claude.settings import DEFAULT_ACK_TIMEOUT_SECONDS
from gpt_voicecoding.control_plane.client import DEFAULT_TIMEOUT_SECONDS, ask
from gpt_voicecoding.seams.control_plane import Action, Request

# --- where a run lives ------------------------------------------------------

ACCEPTANCE_ROOT = Path.home() / "Library" / "Application Support" / "GPT-VoiceCoding" / "acceptance"
ACCEPTANCE_ROOT_VARIABLE = "GPTVOICECODING_ACCEPTANCE_ROOT"

#: The bundle under test. A location, overridable, because a run against a
#: side-by-side install is a legitimate thing to want and hard-coding
#: `/Applications` would make it impossible.
BUNDLE_VARIABLE = "GPTVOICECODING_ACCEPTANCE_BUNDLE"
DEFAULT_BUNDLE = Path("/Applications/GPT-VoiceCoding.app")

#: The engine's real configuration, the one the run derives its own from.
SOURCE_CONFIG_VARIABLE = "GPTVOICECODING_ACCEPTANCE_SOURCE_CONFIG"

#: Darwin caps an AF_UNIX path at 103 bytes, so the run's socket cannot live in
#: the run directory — that path is already 70 characters before the run id. The
#: same reasoning `config.RUNTIME_ROOT` applies, applied again.
SOCKET_ROOT = Path("/tmp")


def acceptance_root() -> Path:
    override = os.environ.get(ACCEPTANCE_ROOT_VARIABLE)
    return Path(override).expanduser() if override else ACCEPTANCE_ROOT


def bundle_path() -> Path:
    override = os.environ.get(BUNDLE_VARIABLE)
    return Path(override).expanduser() if override else DEFAULT_BUNDLE


def bundled_python(bundle: Path | None = None) -> Path:
    return (bundle or bundle_path()) / "Contents/Resources/engine/bin/python3"


def bundled_bridgectl(bundle: Path | None = None) -> Path:
    return (bundle or bundle_path()) / "Contents/Resources/engine/bin/bridgectl"


def source_config_path() -> Path:
    override = os.environ.get(SOURCE_CONFIG_VARIABLE)
    if override:
        return Path(override).expanduser()
    engine = Path.home() / "Library" / "Application Support" / "GPT-VoiceCoding" / "engine"
    return engine / "config.toml"


# --- the journal ------------------------------------------------------------


class Journal:
    """One JSON line per event, in the order the run produced them.

    Locked because the engine's reader thread and the test's own thread both
    write to it, and a half-written line is worse than a missing one: the file
    is the evidence every verdict points at.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()

    def __call__(self, event: str, **fields: Any) -> dict[str, Any]:
        line = {"at": datetime.now(UTC).isoformat(), "event": event, **fields}
        with self._lock, self.path.open("a") as sink:
            sink.write(json.dumps(line, default=str) + "\n")
        return line

    def read(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]


# --- the login shell's PATH, in one place -----------------------------------

#: Mirrors `shell/Sources/ShellCore/LoginShellPath.swift`, which is the method the
#: menu-bar shell uses and therefore the PATH the engine really runs on. `-lic`,
#: not `-lc`: zsh sources `~/.zshrc` only when interactive, and `~/.zshrc` is
#: where `nvm` and `brew shellenv` actually write. The sentinels separate the
#: answer from an interactive profile's chatter, and **exactly two or nothing** —
#: a third means something other than the `printf` wrote the marker, and then no
#: part of the output is the answer.
PATH_SENTINEL = "<<<GVC-PATH>>>"
PATH_SCRIPT = f"printf '{PATH_SENTINEL}%s{PATH_SENTINEL}' \"$PATH\""
PATH_TIMEOUT_SECONDS = 2.0


def login_shell_path() -> str | None:
    """The user's own PATH, or None — never a guess, never a partial answer."""
    shell = os.environ.get("SHELL")
    if not shell or not os.access(shell, os.X_OK):
        return None
    try:
        finished = subprocess.run(
            [shell, "-lic", PATH_SCRIPT],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=PATH_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    parts = finished.stdout.split(PATH_SENTINEL)
    if len(parts) != 3:  # exactly two sentinels bound exactly one answer
        return None
    answer = parts[1].strip(" \t")
    if not answer or "\n" in answer or "\0" in answer:
        return None
    if not any(entry.startswith("/") for entry in answer.split(":")):
        return None
    return answer


# --- provenance -------------------------------------------------------------


@dataclass(frozen=True)
class Provenance:
    """Whether the installed bundle is the tree this run is being asked to accept."""

    bundle: Path
    commit: str
    matches: bool
    differences: tuple[str, ...]

    @property
    def reason(self) -> str:
        if self.matches:
            return f"the bundle's engine is byte-identical to {self.commit}"
        listed = ", ".join(self.differences[:5])
        return (
            f"the bundle's engine differs from the working tree at {self.commit}: {listed}"
            f"{' …' if len(self.differences) > 5 else ''}"
        )


#: Where gen-1 put itself on this machine. Named here so the environment block
#: below can say whether it is still there.
GEN1_RUNTIME = Path.home() / "Library" / "Application Support" / "GPT-VoiceCoding" / "runtime"
GEN1_HOOK = GEN1_RUNTIME / "bridge-hook"
CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"


def environment_facts() -> dict[str, Any]:
    """What else is installed over the Sessions this run starts — recorded, not refused.

    Three of gen-1's parts are still on this machine while the map's disposal
    ticket (#54) waits behind every build ticket, and each one touches what this
    run means:

    * **The shell functions.** `~/.zshrc` redefines `claude` and `codex` to route
      into `claude-hosted` / `codex-hosted`. The harness executes the resolved
      binary and never a shell, so they cannot apply to a Session it starts — but
      while they are installed, the Sessions *Simon* starts are wrapped and this
      run's green does not describe them. That gap closes on #54, not here.
    * **The user-scope hooks.** `~/.claude/settings.json` carries gen-1's
      `bridge-hook` on `SessionStart`, `Stop`, `Notification` and `SessionEnd` —
      the same file and two of the same slots ADR 0011 installs into. They are
      the foreign hooks the installer's merge has to keep, and they are real
      rather than hypothetical.
    * **Its daemon.** Measured 2026-08-26: not running, its socket absent, and
      the hook logs `skipped (bridge-unavailable)`. A *running* gen-1 daemon
      would be a second bridge over the same Sessions, so the run says whether
      one is there.

    Refusing on any of this would deadlock the map — the disposal is last and
    this run is first — so it is written down instead, on the verdict where a
    reader of a green run can see what the green was measured beside.
    """
    hooks: list[str] = []
    if CLAUDE_SETTINGS.exists():
        try:
            settings = json.loads(CLAUDE_SETTINGS.read_text())
        except (OSError, json.JSONDecodeError):
            settings = {}
        for event, entries in (settings.get("hooks") or {}).items():
            if str(GEN1_HOOK) in json.dumps(entries):
                hooks.append(event)
    shell = os.environ.get("SHELL", "")
    functions: list[str] = []
    if shell and os.access(shell, os.X_OK):
        for name in ("claude", "codex"):
            probe = subprocess.run(
                [shell, "-lic", f"type {name} 2>/dev/null"],
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=PATH_TIMEOUT_SECONDS,
            )
            if "hosted" in probe.stdout or "function" in probe.stdout:
                functions.append(f"{name}: {probe.stdout.strip().splitlines()[:1]}")
    socket = GEN1_RUNTIME.parent / "bridge.sock"
    return {
        "gen1_runtime_present": GEN1_RUNTIME.exists(),
        "gen1_hooks_in_user_settings": sorted(hooks),
        "gen1_daemon_listening": socket.exists(),
        "shell_wrappers": functions,
        "scrubbed_agent_markers": sorted(
            name
            for name in os.environ
            if name.startswith("CLAUDE_CODE_")
            or name in ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT")
        ),
    }


def compare_engine_to_tree(bundle: Path, repository: Path) -> Provenance:
    """`diff -r` the bundle's installed package against `src/`, as `docs/app-bundle.md` does.

    Only the project's own package is compared. The interpreter and the locked
    wheels beneath it are what the signature and the lock cover; what a run has to
    know is that the *product* inside the `.app` is the product in this checkout.
    """
    commit = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    installed = next(
        (bundle / "Contents/Resources/engine/lib").glob("python*/site-packages/gpt_voicecoding"),
        None,
    )
    if installed is None:
        return Provenance(bundle, commit, False, ("no gpt_voicecoding package inside the bundle",))
    tree = repository / "src" / "gpt_voicecoding"
    differences = tuple(_differing(tree, installed))
    return Provenance(bundle, commit, not differences, differences)


def _differing(tree: Path, installed: Path) -> Iterator[str]:
    """Every `.py` the two sides do not agree on — **in both directions**.

    Walking the tree alone answers "is everything in this checkout in the
    bundle", which is not the question. The question is whether the engine
    inside the `.app` **is** this checkout, and a bundle carrying a module the
    checkout has since deleted is not — it is an installation that still has the
    old file, still imports it, and would still run it. A one-way compare called
    that byte-identical and let the run attribute its verdict to a tree that
    never produced it.
    """
    for source in sorted(tree.rglob("*.py")):
        relative = source.relative_to(tree)
        candidate = installed / relative
        if not candidate.exists():
            yield f"{relative} missing from the bundle"
        elif not filecmp.cmp(source, candidate, shallow=False):
            yield f"{relative} differs"
    for extra in sorted(installed.rglob("*.py")):
        relative = extra.relative_to(installed)
        if not (tree / relative).exists():
            yield f"{relative} is in the bundle and not in the tree"


# --- the run's configuration ------------------------------------------------


@dataclass(frozen=True)
class DerivedConfig:
    path: Path
    socket_path: Path
    state_path: Path
    log_path: Path
    project_name: str
    workspace: Path
    token_variable: str
    chat_id: str


def derive_config(
    *,
    source: Path,
    run_directory: Path,
    workspace: Path,
    socket_path: Path,
    project_name: str,
) -> DerivedConfig:
    """The user's real config, with only what a run must not share redirected.

    Every value is copied, because the point of the run is to accept the engine
    the user actually configured. Three are **replaced** — the socket, the state
    and the log — because they are what a second engine would otherwise fight the
    first one over. The launcher's tables are **dropped**, which is a change of
    kind and so is said out loud:

    `[launch]`, `[[launch.projects]]` and the `session_launcher` seam are what
    #72 parked. `config.of` no longer reads any of them — `REQUIRED_SEAMS` is
    `("call", "companion_channel")` (`config.py:70`) and there is no `launch`
    section — so carrying them would be inert. Inert is not harmless here: the
    user's real `[[launch.projects]]` names twelve of his actual project
    directories, and a run config that names them is a run config that could
    point an agent at one. `[adapters] session_launcher` goes with them because
    it names a module the parked tree no longer has. What is left is a config the
    engine under test could have been given.
    """
    run_directory.mkdir(parents=True, exist_ok=True)
    document = tomllib.loads(source.read_text())
    engine = dict(document.get("engine", {}))
    engine["socket_path"] = str(socket_path)
    engine["state_path"] = str(run_directory / "state.json")
    document["engine"] = engine

    log = dict(document["log"])
    log["path"] = str(run_directory / "engine.log")
    document["log"] = log

    dropped = [name for name in ("launch",) if document.pop(name, None) is not None]
    adapters = dict(document["adapters"])
    if adapters.pop("session_launcher", None) is not None:
        dropped.append("adapters.session_launcher")
    settings = dict(adapters.get("settings", {}))
    if settings.pop("session_launcher", None) is not None:
        dropped.append("adapters.settings.session_launcher")
    adapters["settings"] = settings
    document["adapters"] = adapters

    channel = dict(document["adapters"]["settings"]["companion_channel"])

    path = run_directory / "config.toml"
    path.write_text(_as_toml(document))
    (run_directory / "config-dropped.json").write_text(json.dumps(dropped, indent=2) + "\n")
    return DerivedConfig(
        path=path,
        socket_path=socket_path,
        state_path=Path(engine["state_path"]),
        log_path=Path(log["path"]),
        project_name=project_name,
        workspace=workspace,
        token_variable=str(channel["token_env"]),
        chat_id=str(channel["chat_id"]),
    )


def _as_toml(document: dict[str, Any]) -> str:
    """The smallest TOML writer that covers this document, and it says so.

    Python ships a TOML *reader* and no writer, and the acceptance will not take a
    dependency to emit four tables. What it covers is exactly the shape
    `docs/control-plane.md` documents: tables, arrays of tables, strings, numbers,
    booleans and string arrays. A key it cannot render raises rather than being
    silently dropped — a config missing a key the user set would make the run
    accept an engine the user is not running.
    """
    lines: list[str] = []
    _emit_table(document, (), lines)
    return "\n".join(lines) + "\n"


def _emit_table(table: dict[str, Any], prefix: tuple[str, ...], lines: list[str]) -> None:
    scalars = {key: value for key, value in table.items() if not _is_table_like(value)}
    if prefix and scalars:
        lines.append(f"[{'.'.join(_quoted(part) for part in prefix)}]")
    elif prefix:
        lines.append(f"[{'.'.join(_quoted(part) for part in prefix)}]")
    for key, value in scalars.items():
        lines.append(f"{_quoted(key)} = {_as_value(value)}")
    if scalars or prefix:
        lines.append("")
    for key, value in table.items():
        if isinstance(value, dict):
            _emit_table(value, (*prefix, key), lines)
        elif _is_array_of_tables(value):
            for entry in value:
                lines.append(f"[[{'.'.join(_quoted(part) for part in (*prefix, key))}]]")
                for inner_key, inner_value in entry.items():
                    lines.append(f"{_quoted(inner_key)} = {_as_value(inner_value)}")
                lines.append("")


def _is_table_like(value: Any) -> bool:
    return isinstance(value, dict) or _is_array_of_tables(value)


def _is_array_of_tables(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(item, dict) for item in value)


_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _quoted(key: str) -> str:
    return key if _BARE_KEY.match(key) else json.dumps(key)


def _as_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(_as_value(item) for item in value) + "]"
    raise TypeError(
        f"the acceptance's TOML writer cannot render {value!r} ({type(value).__name__})"
    )


# --- the engine under test --------------------------------------------------

#: How long the engine gets to bind its socket before the run gives up on it.
#: The engine's start is a config load, an adapter import and a bind — no network
#: and no subprocess — so this is generous rather than derived; a slow one is a
#: finding, and the log says why.
ENGINE_START_SECONDS = 30.0

#: The grace a stopped engine gets to unlink its socket and let its Sessions go.
ENGINE_STOP_SECONDS = 20.0


class Engine:
    """The bundle's own interpreter, running the bundle's own engine.

    Spawned by the harness rather than by the menu-bar shell: repeatability over
    coverage, and the shell is out of scope (`docs/acceptance-design.md` § Ruled).
    What the shell *does* contribute — the login shell's PATH — is reproduced
    here, because without it the launcher cannot find `claude` or `codex`.
    """

    def __init__(
        self,
        *,
        config: DerivedConfig,
        bundle: Path,
        journal: Journal,
        token: str,
        path_value: str,
    ) -> None:
        self._config = config
        self._bundle = bundle
        self._journal = journal
        self._token = token
        self._path_value = path_value
        self._process: subprocess.Popen[bytes] | None = None

    @property
    def environment(self) -> dict[str, str]:
        """The real HOME, the login shell's PATH, and the token by its own name.

        `HOME` is the user's own because the real `claude` keeps its login and its
        session registry there, and an engine given a temporary one would launch
        an agent that has never been logged in. That is the one thing the run
        cannot isolate; the workspaces and every path the engine writes are under
        the run directory instead.
        """
        environment = dict(os.environ)
        environment["PATH"] = self._path_value
        environment[self._config.token_variable] = self._token
        return environment

    def start(self) -> None:
        command = [
            str(bundled_python(self._bundle)),
            "-m",
            "gpt_voicecoding.engine",
            "--config",
            str(self._config.path),
        ]
        self._journal("engine.start", command=command, socket=str(self._config.socket_path))
        # **Not redirected**, and that is ADR 0004 rather than an oversight: "the
        # engine owns its log … nothing that starts the engine redirects output".
        # The legacy log grew ~1 GB/month and could not be rotated precisely
        # because a shell redirect owned the descriptor, and a harness that took
        # the descriptor here would be that shell. The engine `dup2`s the
        # configured log — `engine-<lane>/engine.log`, under the run directory —
        # onto its own stdout and stderr before it exists, so a redirect here
        # would only ever have caught the moments before that, which the ADR
        # accounts for in as many words: "Output before adoption is discarded —
        # an engine dying that early is surfaced by silence on the socket."
        # `start` below is that silence, with the reason on the exception.
        self._process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            env=self.environment,
        )
        expiry = time.monotonic() + ENGINE_START_SECONDS
        while time.monotonic() < expiry:
            if self._config.socket_path.exists():
                self._journal("engine.listening", socket=str(self._config.socket_path))
                return
            if self._process.poll() is not None:
                raise EngineRefused(
                    f"the engine exited {self._process.returncode} before binding its socket; "
                    f"log at {self._config.log_path} (ADR 0004: output before the engine adopts "
                    f"its own log is discarded, and this silence is how that surfaces)"
                )
            time.sleep(0.2)
        raise EngineRefused(
            f"the engine did not bind {self._config.socket_path} within "
            f"{ENGINE_START_SECONDS:.0f}s; log at {self._config.log_path}"
        )

    def stop(self) -> None:
        if self._process is None or self._process.poll() is not None:
            return
        self._process.terminate()
        try:
            self._process.wait(timeout=ENGINE_STOP_SECONDS)
        except subprocess.TimeoutExpired:
            self._journal("engine.killed", reason="did not exit on SIGTERM")
            self._process.kill()
            self._process.wait(timeout=ENGINE_STOP_SECONDS)
        self._journal("engine.stopped", returncode=self._process.returncode)

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    def log_lines(self) -> list[str]:
        if not self._config.log_path.exists():
            return []
        return self._config.log_path.read_text(errors="replace").splitlines()


class EngineRefused(RuntimeError):
    """The engine under test never came up."""


# --- the surface ------------------------------------------------------------


@dataclass(frozen=True)
class Answer:
    """One `bridgectl` call and what came back."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        """Exit 0 — the engine answered and did not refuse. `bridgectl` § Three exits."""
        return self.returncode == 0

    @property
    def text(self) -> str:
        return self.stdout.strip() if self.ok else self.stderr.strip()


class Bridgectl:
    """Every product action the run performs, through the bundle's own CLI."""

    def __init__(self, *, bundle: Path, socket_path: Path, journal: Journal) -> None:
        self._executable = bundled_bridgectl(bundle)
        self._socket = socket_path
        self._journal = journal

    def __call__(self, *arguments: str, timeout: float | None = None) -> Answer:
        deadline = timeout if timeout is not None else _default_deadline()
        # `--timeout` is passed only when the caller states one. Left off, the CLI
        # picks its own deadline, which is the behaviour the run is accepting;
        # passing it every time would hide exactly the mismatch `relay` records.
        stated = ["--timeout", f"{deadline:.1f}"] if timeout is not None else []
        argv = [str(self._executable), "--socket", str(self._socket), *stated, *arguments]
        finished = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            # The CLI carries its own deadline; this one only stops a CLI that
            # has stopped carrying it. Generous over it on purpose, so a timeout
            # here always means the *surface* hung and never that the action was
            # merely slow — the distinction #28 was about.
            timeout=deadline + 30.0,
        )
        answer = Answer(tuple(argv), finished.returncode, finished.stdout, finished.stderr)
        self._journal(
            "bridgectl",
            command=list(arguments),
            returncode=answer.returncode,
            stdout=answer.stdout.strip(),
            stderr=answer.stderr.strip(),
            deadline_seconds=deadline,
            deadline_stated=timeout is not None,
        )
        return answer


def _default_deadline() -> float:
    """The deadline `bridgectl` itself would use — read from the client, not copied.

    This used to call `control_plane.client.timeout_for` and take the action,
    because `launch` had a longer budget than everything else. #72 parked the
    launcher and `timeout_for` went with it: there is one number now, and a
    parameter kept for a per-action future that may never come is a parameter
    that lies about what this function does. When an action needs its own budget
    again, this grows one then. `relay` already needs more than the surface gives
    it and says so in one place — `RELAY_DEADLINE_SECONDS`, passed explicitly.
    """
    return DEFAULT_TIMEOUT_SECONDS


#: What a relay actually needs, derived rather than picked — and *not* what the
#: surface gives it.
#:
#: `bridgectl` hands every action the 10-second default
#: (`control_plane/client.py:44`), while the engine's own proof of delivery on
#: the Claude lane waits `DEFAULT_ACK_TIMEOUT_SECONDS` — 45 seconds
#: (`adapters/agent/claude/settings.py:34`) — for the Session to call
#: `acknowledge_answer`. Measured at build time on this machine: `bridgectl relay`
#: answers "the engine … did not answer within 10s" while `engine.log` goes on to
#: say "relay … not proven delivered … within 45s; it waits". The surface cannot
#: reach the reply. That is #28's shape pointed at `relay`, and the run records it
#: as a finding of its own.
#:
#: The harness passes this instead, so what step 5 observes is the *relay*, not
#: the CLI's deadline. Headroom over the ack wait, in the style of
#: `LAUNCH_TIMEOUT_SECONDS`: the reply is emitted when the wait resolves, so the
#: surface has to outlive it rather than race it.
#:
#: The mismatch itself is no longer a step. #60 raised it as `5b`, and #73's nine
#: names do not include it — it is a defect in `relay`, and `relay` is #77's, so
#: it belongs in that step's evidence rather than as a tenth red line of its own.
#: The two numbers stay named here so the evidence can quote them.
RELAY_DEADLINE_SECONDS = DEFAULT_ACK_TIMEOUT_SECONDS + 30.0
SURFACE_RELAY_DEADLINE_SECONDS = DEFAULT_TIMEOUT_SECONDS
ENGINE_RELAY_PROOF_SECONDS = DEFAULT_ACK_TIMEOUT_SECONDS


# --- the one documented side channel ----------------------------------------


def control_plane_status(socket_path: Path, journal: Journal) -> dict[str, Any]:
    """Read `status`'s **payload**, not `bridgectl`'s rendering of it.

    This exists for exactly one value: `approval_id`. It is in the control-plane
    reply (`control_plane/payloads.py:180`) and it reaches **no human surface** —
    `bridgectl status` prints only a count of pending approvals
    (`control_plane/commands.py:209`), the escalated announcement does not carry
    it (`core/approvals.py:43`), the Swift shell never mentions it, and
    `engine.log` records it only for a verdict that arrives too late. So
    `bridgectl approve <id>` cannot be driven by anyone.

    Legacy has **no** counterpart behaviour to cite: its permission handling
    pushed "Claude needs your permission to use X" as a *notice*
    (`bridge/daemon.py:143` in the legacy checkout) and the user answered at the
    keyboard. There was never an id-addressed `approve` route, which is why
    nobody found this.

    That is a finding, and the lane records it as one. This side channel is how
    the run gets *past* it, so a single expensive run reports every red rather
    than the first. Every call is journaled as `side-channel`, so no verdict can
    rest on it without the journal saying so.
    """
    return control_plane_payload(
        Action.STATUS,
        socket_path=socket_path,
        journal=journal,
        why="approval_id reaches no surface",
    )


def control_plane_payload(
    action: Action,
    *,
    socket_path: Path,
    journal: Journal,
    payload: dict[str, Any] | None = None,
    why: str,
) -> dict[str, Any]:
    """One control-plane reply, read as the payload `bridgectl` itself receives.

    `bridgectl` is `ask` plus `render`, so this is not a private route into the
    engine — it is the same request with the rendering left off. It exists for
    facts the rendering does not carry, and there are two:

    * `approval_id`, above; and
    * the roster row's own fields. `_roster_lines`
      (`control_plane/commands.py:168`) renders five of them into one line, and
      the steps after `roster` need others — the Session name #78 stabilises,
      the `waiting_for` #75 fills, the `progress` #76 reads, the
      `ChildClassification` #79 sets. None of those tickets locks a *text
      format*, and none should have to: a step asserting on the wording of that
      one line would be asking a build ticket to invent a format and then
      holding it to this harness's guess at one. So the steps read the fields
      and leave the wording to whoever writes it.

    Every call is journaled as `side-channel` with its reason, so no verdict can
    rest on one without the journal saying so.
    """
    reply = asyncio.run(ask(Request(action=action, payload=dict(payload or {})), path=socket_path))
    data = dict(reply.data)
    journal("side-channel", why=why, action=action.value, data=data)
    return data


# --- far-side deadlines -----------------------------------------------------


@dataclass(frozen=True)
class FarSideDeadlines:
    """Every wait on something the engine does not answer for.

    Each is a *measured* number with headroom, not a guess, and each says what it
    was measured against. `docs/acceptance-design.md` § Deadlines requires that;
    `conftest.py` fills them in from the measurement recorded on ticket #60.
    """

    agent_turn_seconds: float
    telegram_round_trip_seconds: float
    workspace_effect_seconds: float
    absence_window_seconds: float


def wait_for(
    predicate,  # noqa: ANN001 - any zero-argument question
    *,
    deadline_seconds: float,
    poll_seconds: float = 0.5,
):  # noqa: ANN201 - whatever the predicate returns
    """Poll a question until it answers truthily, or return its last falsey answer."""
    expiry = time.monotonic() + deadline_seconds
    answer = predicate()
    while not answer and time.monotonic() < expiry:
        time.sleep(poll_seconds)
        answer = predicate()
    return answer


# --- the verdict ------------------------------------------------------------


class Result(StrEnum):
    """The closed set a step's verdict comes from, and it is closed for a reason.

    `docs/acceptance-design.md` § Artifacts names exactly these four. They were
    four bare module-level strings, which is the shape that lets a typo become a
    fifth state nobody notices until a reader is told a run `PASSSED`. A StrEnum
    keeps them comparable to and serialisable as the same strings — `verdict.json`
    is byte-identical either way — while making the set the type rather than a
    convention. The same choice, for the same reason, as `seams/delivery.Delivery`
    and `core/lifecycle.Lifecycle`.
    """

    PASS = "PASS"
    FAIL = "FAIL"
    REFUSED = "REFUSED"
    SKIPPED = "SKIPPED"


#: Named at module level as well, because every call site reads better as
#: `support.PASS` than `support.Result.PASS`, and the enum is what they are.
PASS = Result.PASS
FAIL = Result.FAIL
REFUSED = Result.REFUSED
SKIPPED = Result.SKIPPED


@dataclass
class StepVerdict:
    step: str
    result: str
    evidence: str


@dataclass
class Verdict:
    """`verdict.json` — the one artifact a reader needs.

    A run is `PASS` only when every step on every lane is `PASS`
    (`docs/acceptance-design.md` § Artifacts and the verdict).
    """

    run_id: str
    bundle: str
    commit: str
    provenance: str
    versions: dict[str, str] = field(default_factory=dict)
    #: What else was installed over the Sessions this run started. See
    #: `environment_facts`: recorded rather than refused, because a green run
    #: beside a live gen-1 install means something different from a green run
    #: without one.
    environment: dict[str, Any] = field(default_factory=dict)
    #: Things the run *arranged* or *saw* but does not grade — the trust gate,
    #: #44's approval directory. Kept out of `lanes` so the graded step set stays
    #: exactly the nine the build tickets cite, and kept out of nothing else.
    observations: list[dict[str, Any]] = field(default_factory=list)
    lanes: dict[str, list[StepVerdict]] = field(default_factory=dict)
    #: What the run promised to observe. `missing` is the difference between this
    #: and what it recorded, and `result` will not say PASS while any is outstanding.
    expected_lanes: tuple[str, ...] = ()
    expected_steps: tuple[str, ...] = ()

    def observe(self, lane: str, what: str, detail: str) -> None:
        self.observations.append({"lane": lane, "what": what, "detail": detail})

    def record(self, lane: str, step: str, result: Result, evidence: str) -> StepVerdict:
        recorded = StepVerdict(step=step, result=Result(result), evidence=evidence)
        self.lanes.setdefault(lane, []).append(recorded)
        return recorded

    def refuse(self, lane: str, reason: str) -> None:
        """Write a refusal down before raising it.

        Preflight refuses rather than runs, and the design says the refusal
        carries "verdict `REFUSED` with the reason". A refusal that only raised
        left `verdict.json` with no trace of why the run stopped: the reason
        reached the terminal, and nothing that outlives it.
        """
        self.record(lane, "preflight", REFUSED, reason)

    @property
    def missing(self) -> tuple[str, ...]:
        """Every (lane, step) the run promised and did not record.

        A run is judged on what it **set out** to observe, not on what it managed
        to. Without this, a lane that never ran — a collection error, a fixture
        raising, a lane deselected by hand — contributes no rows at all, and a
        verdict made only of the surviving lane's greens says `PASS` for a run
        that observed half the product. That is the one failure mode a verdict
        file must not have, because it is the one nobody re-checks.
        """
        absent: list[str] = []
        for lane in self.expected_lanes:
            recorded = {step.step for step in self.lanes.get(lane, [])}
            absent.extend(f"{lane}/{step}" for step in self.expected_steps if step not in recorded)
        return tuple(absent)

    @property
    def result(self) -> Result:
        results = [step.result for steps in self.lanes.values() for step in steps]
        if not results:
            return REFUSED
        if any(result == REFUSED for result in results):
            return REFUSED
        if self.missing:
            return FAIL
        return PASS if all(result == PASS for result in results) else FAIL

    def write(self, path: Path) -> Path:
        path.write_text(
            json.dumps(
                {
                    "run_id": self.run_id,
                    "result": str(self.result),
                    "missing": list(self.missing),
                    "bundle": self.bundle,
                    "commit": self.commit,
                    "provenance": self.provenance,
                    "versions": self.versions,
                    "environment": self.environment,
                    "observations": self.observations,
                    "lanes": {
                        lane: [
                            {
                                "step": step.step,
                                "result": str(step.result),
                                "evidence": step.evidence,
                            }
                            for step in steps
                        ]
                        for lane, steps in self.lanes.items()
                    },
                },
                indent=2,
            )
            + "\n"
        )
        return path


# --- versions the verdict names ---------------------------------------------


def binary_version(binary: str, path_value: str) -> str:
    """`<binary> --version` on the PATH the engine will be handed, or why not."""
    resolved = shutil.which(binary, path=path_value)
    if resolved is None:
        return "not on the engine's PATH"
    try:
        finished = subprocess.run(
            [resolved, "--version"], capture_output=True, text=True, timeout=30.0
        )
    except (subprocess.TimeoutExpired, OSError) as failure:
        return f"{resolved}: {failure}"
    return f"{resolved}: {(finished.stdout or finished.stderr).strip().splitlines()[0]}"


def run_id(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")


def new_run_directory(identifier: str | None = None) -> Path:
    directory = acceptance_root() / (identifier or run_id())
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def fresh_workspace(run_directory: Path, lane: str, path_value: str) -> Path:
    """A disposable `git init` directory, one per lane, kept with the run."""
    workspace = run_directory / f"workspace-{lane}"
    workspace.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--quiet", str(workspace)],
        check=True,
        capture_output=True,
        env={**os.environ, "PATH": path_value},
    )
    return workspace


def token_from_environment(variable: str) -> str:
    """The bot token, by the name the run's own config gives it, or a refusal."""
    token = os.environ.get(variable)
    if not token:
        raise LookupError(
            f"{variable} is not set. The acceptance drives the real bot, and the token has no "
            f"durable home yet (#55). Export it into the shell that runs pytest."
        )
    return token


def matching_lines(lines: Sequence[str], pattern: str) -> list[str]:
    compiled = re.compile(pattern)
    return [line for line in lines if compiled.search(line)]


def files_under(directory: Path) -> set[Path]:
    """Every file an agent could have left, ignoring the `git init` skeleton."""
    return {
        path.relative_to(directory)
        for path in directory.rglob("*")
        if path.is_file() and ".git" not in path.parts
    }


def read_if_exists(path: Path) -> str | None:
    return path.read_text(errors="replace") if path.exists() else None


def flatten(values: Iterable[Any]) -> str:
    return ", ".join(str(value) for value in values)


# --- walking a lane's journey ----------------------------------------------


class StepFailed(Exception):
    """This step did not observe what it must. The lane goes on."""


class LaneBlocked(Exception):
    """This step did not observe what it must, and nothing after it can run."""


class Journey:
    """One lane's walk through the steps, recording a verdict for every one.

    A step that fails does **not** end the run. An acceptance whose job is to
    find bugs is worth far more when one expensive walk reports every red than
    when it reports the first — so an ordinary failure is recorded and the walk
    continues, and only a step that leaves nothing to observe (no Session, no
    engine) blocks the rest, which are then `SKIPPED` rather than silently absent.
    """

    def __init__(self, *, lane: str, verdict: Verdict, journal: Journal, steps: Sequence[str]):
        self.lane = lane
        self._verdict = verdict
        self._journal = journal
        self._remaining = list(steps)
        self._blocked: str | None = None

    def run(self, step: str, action) -> Any:  # noqa: ANN001 - a zero-argument step
        """Run one step, record its verdict, and return whatever it observed."""
        if step in self._remaining:
            self._remaining.remove(step)
        if self._blocked is not None:
            self._record(step, SKIPPED, f"blocked by {self._blocked}")
            return None
        self._journal("step.start", lane=self.lane, step=step)
        try:
            evidence = action()
        except LaneBlocked as blocking:
            self._record(step, FAIL, str(blocking))
            self._blocked = step
            return None
        except StepFailed as failure:
            self._record(step, FAIL, str(failure))
            return None
        self._record(step, PASS, str(evidence))
        return evidence

    def observe(self, what: str, detail: str) -> None:
        """Write down something the run arranged, or saw and does not grade.

        Kept out of the graded step set on purpose. The nine steps are cited
        **verbatim** by the "Red first" line of every build ticket (#74–#80), so
        a tenth row in `lanes` would read as a tenth red line for someone to
        clear. These land in `verdict.observations` instead, where a reader sees
        them beside the run they belong to and nobody mistakes them for exits.
        """
        self._verdict.observe(self.lane, what, detail)
        self._journal("observation", lane=self.lane, what=what, detail=detail)

    def skip_rest(self, why: str) -> None:
        for step in list(self._remaining):
            self._record(step, SKIPPED, why)
            self._remaining.remove(step)

    def _record(self, step: str, result: Result, evidence: str) -> None:
        self._verdict.record(self.lane, step, result, evidence)
        self._journal("step.verdict", lane=self.lane, step=step, result=result, evidence=evidence)


# --- the trust gate ---------------------------------------------------------

#: Where each agent records that a directory has been trusted. Both were read off
#: this machine rather than remembered: `~/.claude.json` keeps a `projects` map
#: whose entries carry `hasTrustDialogAccepted`, and `~/.codex/config.toml` keeps
#: `[projects."<path>"] trust_level = "trusted"`.
CLAUDE_STATE = Path.home() / ".claude.json"
CODEX_CONFIG = Path.home() / ".codex" / "config.toml"
CLAUDE_TRUST_KEY = "hasTrustDialogAccepted"


class TrustGate:
    """Trust one disposable workspace for both agents, and put the files back after.

    **Why this is a precondition and not a step.** A launch into a directory the
    agent has never seen stops at a full-screen "Is this a project you created or
    one you trust?" and the Session never registers — measured on this machine at
    build time, and it is the same gate #18 reports for Codex. A fresh workspace
    is what `docs/acceptance-design.md` requires, so every run would end at step
    1a for a reason that is real but singular. Arranging trust is how the other
    seven steps stay observable; that the product cannot cross this gate on its
    own is recorded as its own finding rather than hidden by the arrangement.

    Both files are the user's, so both are backed up into the run directory before
    a byte is written and both are restored on the way out — the entry this adds
    is removed, and nothing else in either file is touched.

    **Trusted under both spellings.** Measured on 2026-08-26: `claude agents
    --json` reports a Session's `cwd` resolved, and `codex`'s `session_meta.cwd`
    the same. A run directory under Application Support is not behind a symlink
    today, so the two spellings coincide — but a gate that only holds while that
    stays true is a gate that fails once and confusingly. Both are granted when
    they differ, and both are revoked.
    """

    def __init__(self, workspace: Path, *, run_directory: Path, journal: Journal) -> None:
        self._workspace = workspace
        self._paths = sorted({str(workspace), os.path.realpath(workspace)})
        self._run_directory = run_directory
        self._journal = journal
        self._claude_granted: list[str] = []
        self._codex_block: str | None = None

    def __enter__(self) -> TrustGate:
        self._trust_claude()
        self._trust_codex()
        return self

    def __exit__(self, *_: object) -> None:
        self._untrust_claude()
        self._untrust_codex()

    def _backup(self, path: Path) -> None:
        if path.exists():
            shutil.copy2(path, self._run_directory / f"{path.name}.before-trust")

    def _trust_claude(self) -> None:
        if not CLAUDE_STATE.exists():
            self._journal("trust.absent", agent="claude", path=str(CLAUDE_STATE))
            return
        self._backup(CLAUDE_STATE)
        state = json.loads(CLAUDE_STATE.read_text())
        projects = state.setdefault("projects", {})
        granted = [path for path in self._paths if path not in projects]
        for path in granted:
            projects[path] = {CLAUDE_TRUST_KEY: True}
        if not granted:
            self._journal("trust.already", agent="claude", workspaces=self._paths)
            return
        CLAUDE_STATE.write_text(json.dumps(state, indent=2))
        self._claude_granted = granted
        self._journal("trust.granted", agent="claude", workspaces=granted)

    def _untrust_claude(self) -> None:
        if not self._claude_granted or not CLAUDE_STATE.exists():
            return
        state = json.loads(CLAUDE_STATE.read_text())
        projects = state.get("projects", {})
        removed = [path for path in self._claude_granted if projects.pop(path, None) is not None]
        if removed:
            CLAUDE_STATE.write_text(json.dumps(state, indent=2))
        self._journal("trust.revoked", agent="claude", workspaces=removed)

    def _trust_codex(self) -> None:
        if not CODEX_CONFIG.exists():
            self._journal("trust.absent", agent="codex", path=str(CODEX_CONFIG))
            return
        self._backup(CODEX_CONFIG)
        existing = CODEX_CONFIG.read_text()
        wanted = [path for path in self._paths if f'[projects."{path}"]' not in existing]
        if not wanted:
            self._journal("trust.already", agent="codex", workspaces=self._paths)
            return
        # Appended as one block and removed as the same block, so the user's own
        # file keeps its order, its comments and its formatting.
        self._codex_block = "".join(
            f'\n[projects."{path}"]\ntrust_level = "trusted"\n' for path in wanted
        )
        CODEX_CONFIG.write_text(existing + self._codex_block)
        self._journal("trust.granted", agent="codex", workspaces=wanted)

    def _untrust_codex(self) -> None:
        if self._codex_block is None or not CODEX_CONFIG.exists():
            return
        existing = CODEX_CONFIG.read_text()
        if self._codex_block in existing:
            CODEX_CONFIG.write_text(existing.replace(self._codex_block, "", 1))
        self._journal("trust.revoked", agent="codex", workspace=str(self._workspace))

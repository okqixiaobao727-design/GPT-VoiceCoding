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
from pathlib import Path
from typing import Any

from gpt_voicecoding.adapters.agent.claude.settings import DEFAULT_ACK_TIMEOUT_SECONDS
from gpt_voicecoding.control_plane.client import (
    DEFAULT_TIMEOUT_SECONDS,
    ask,
    timeout_for,
)
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
    """Every `.py` under `tree` that the bundle lacks or holds differently."""
    for source in sorted(tree.rglob("*.py")):
        relative = source.relative_to(tree)
        candidate = installed / relative
        if not candidate.exists():
            yield f"{relative} missing from the bundle"
        elif not filecmp.cmp(source, candidate, shallow=False):
            yield f"{relative} differs"


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
    the user actually configured. Four are replaced — the socket, the state, the
    log and the project list — because they are the four a second engine would
    otherwise fight the first one over, and the workspace is the one thing the
    agents are allowed to write into.
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

    launch = dict(document["launch"])
    launch["projects"] = [
        {"name": project_name, "workspace": str(workspace), "spoken_aliases": [project_name]}
    ]
    document["launch"] = launch

    channel = dict(document["adapters"]["settings"]["companion_channel"])

    path = run_directory / "config.toml"
    path.write_text(_as_toml(document))
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
        stdio: Path,
    ) -> None:
        self._config = config
        self._bundle = bundle
        self._journal = journal
        self._token = token
        self._path_value = path_value
        self._stdio = stdio
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
        self._process = subprocess.Popen(
            command,
            stdout=self._stdio.open("wb"),
            stderr=subprocess.STDOUT,
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
                    f"stdio at {self._stdio}, log at {self._config.log_path}"
                )
            time.sleep(0.2)
        raise EngineRefused(
            f"the engine did not bind {self._config.socket_path} within "
            f"{ENGINE_START_SECONDS:.0f}s; stdio at {self._stdio}"
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
        action = _action_named(arguments[0]) if arguments else None
        deadline = timeout if timeout is not None else _deadline_for(action)
        # `--timeout` is passed only when the caller states one. Left off, the CLI
        # picks its own deadline from `timeout_for`, which is the behaviour the run
        # is accepting; passing it every time would hide exactly the defect step 5b
        # records.
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


def _action_named(word: str) -> Action | None:
    for action in Action:
        if str(action) == word:
            return action
    return None


def _deadline_for(action: Action | None) -> float:
    """The action's own deadline, from the function `bridgectl` itself calls."""
    return timeout_for(action) if action is not None else timeout_for(Action.STATUS)


#: What a relay actually needs, derived rather than picked — and *not* what the
#: surface gives it.
#:
#: `timeout_for` hands every action but `launch` the 10-second default
#: (`control_plane/client.py:39`), while the engine's own proof of delivery on
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
RELAY_DEADLINE_SECONDS = DEFAULT_ACK_TIMEOUT_SECONDS + 30.0

#: The two numbers step 5b compares, kept beside the derivation that reads them.
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
    reply = asyncio.run(ask(Request(action=Action.STATUS, payload={}), path=socket_path))
    data = dict(reply.data)
    journal("side-channel", why="approval_id reaches no surface", action="status", data=data)
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

PASS = "PASS"
FAIL = "FAIL"
REFUSED = "REFUSED"
SKIPPED = "SKIPPED"


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
    lanes: dict[str, list[StepVerdict]] = field(default_factory=dict)

    def record(self, lane: str, step: str, result: str, evidence: str) -> StepVerdict:
        recorded = StepVerdict(step=step, result=result, evidence=evidence)
        self.lanes.setdefault(lane, []).append(recorded)
        return recorded

    @property
    def result(self) -> str:
        results = [step.result for steps in self.lanes.values() for step in steps]
        if not results:
            return REFUSED
        if any(result == REFUSED for result in results):
            return REFUSED
        return PASS if all(result == PASS for result in results) else FAIL

    def write(self, path: Path) -> Path:
        path.write_text(
            json.dumps(
                {
                    "run_id": self.run_id,
                    "result": self.result,
                    "bundle": self.bundle,
                    "commit": self.commit,
                    "provenance": self.provenance,
                    "versions": self.versions,
                    "lanes": {
                        lane: [vars(step) for step in steps] for lane, steps in self.lanes.items()
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

    def record_arranged(self, step: str, why: str) -> None:
        """Write down something the run arranged rather than observed."""
        self._record(step, SKIPPED, why)

    def skip_rest(self, why: str) -> None:
        for step in list(self._remaining):
            self._record(step, SKIPPED, why)
            self._remaining.remove(step)

    def _record(self, step: str, result: str, evidence: str) -> None:
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
    """

    def __init__(self, workspace: Path, *, run_directory: Path, journal: Journal) -> None:
        self._workspace = workspace
        self._run_directory = run_directory
        self._journal = journal
        self._claude_restored = False
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
        if str(self._workspace) in projects:
            self._journal("trust.already", agent="claude", workspace=str(self._workspace))
            return
        projects[str(self._workspace)] = {CLAUDE_TRUST_KEY: True}
        CLAUDE_STATE.write_text(json.dumps(state, indent=2))
        self._claude_restored = True
        self._journal("trust.granted", agent="claude", workspace=str(self._workspace))

    def _untrust_claude(self) -> None:
        if not self._claude_restored or not CLAUDE_STATE.exists():
            return
        state = json.loads(CLAUDE_STATE.read_text())
        removed = state.get("projects", {}).pop(str(self._workspace), None)
        if removed is not None:
            CLAUDE_STATE.write_text(json.dumps(state, indent=2))
        self._journal("trust.revoked", agent="claude", workspace=str(self._workspace))

    def _trust_codex(self) -> None:
        if not CODEX_CONFIG.exists():
            self._journal("trust.absent", agent="codex", path=str(CODEX_CONFIG))
            return
        self._backup(CODEX_CONFIG)
        existing = CODEX_CONFIG.read_text()
        heading = f'[projects."{self._workspace}"]'
        if heading in existing:
            self._journal("trust.already", agent="codex", workspace=str(self._workspace))
            return
        # Appended as a block and removed as the same block, so the user's own
        # file keeps its order, its comments and its formatting.
        self._codex_block = f'\n{heading}\ntrust_level = "trusted"\n'
        CODEX_CONFIG.write_text(existing + self._codex_block)
        self._journal("trust.granted", agent="codex", workspace=str(self._workspace))

    def _untrust_codex(self) -> None:
        if self._codex_block is None or not CODEX_CONFIG.exists():
            return
        existing = CODEX_CONFIG.read_text()
        if self._codex_block in existing:
            CODEX_CONFIG.write_text(existing.replace(self._codex_block, "", 1))
        self._journal("trust.revoked", agent="codex", workspace=str(self._workspace))

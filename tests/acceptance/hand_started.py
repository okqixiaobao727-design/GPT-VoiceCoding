"""Starting a Session the way the user does: the ordinary command, in a pty.

The launch journey asked the product to start a Session and then watched what it
had started. The bridge journey cannot: a bridge is judged on Sessions **it did
not start**, so the harness has to stand in for the person at the keyboard —
open a terminal, run `claude` or `codex`, and leave the product to find it.

Three things that took measuring rather than remembering (2026-08-26, ticket #73,
against `claude` 2.1.246 and `codex-cli` 0.149.1):

* **The environment has to be scrubbed.** This harness is itself run from inside
  a Claude Code session, and Claude Code marks its children — `CLAUDECODE`,
  `CLAUDE_PID` and the whole `CLAUDE_CODE_*` family, including the
  `CLAUDE_CODE_MESSAGING_SOCKET` and `_TOKEN` pair #71 rides on. Measured: a
  `claude` started with those inherited is treated by Claude Code as one of its
  own children rather than as a Session — transcript saving off, and *absent from
  `claude agents --json` altogether*. A harness that did not scrub them would
  start runs the product is right to ignore, and read the roster's correct
  silence as a bug. `child_environment` puts the marker back on purpose, which is
  how the `child` step gets a Child Process to look at.

* **The shell function is not the command.** `~/.zshrc:169-183` on this machine
  redefines `claude` and `codex` as functions routing into gen-1's
  `claude-hosted` / `codex-hosted` wrapper. The harness resolves and executes the
  **binary**, never a shell, so the function cannot apply — which is what "no
  wrapper" (#70, #73) means operationally. The wrapper's presence is recorded as
  an environment fact rather than refused: #54 removes it, and #54 is last on the
  map while this run is first.

* **The screen is for typing, not for reading.** Both TUIs redraw with cursor
  addressing, and `codex`'s output in a pty interleaves to roughly one glyph per
  line once the escapes are stripped. Nothing here parses it. The raw stream is
  written to the run's artifacts as evidence a human can read; every assertion in
  `journey.py` rests on the roster, the rollout, the filesystem, `engine.log` or
  the chat.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import pty
import re
import shutil
import signal
import struct
import subprocess
import termios
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

#: Everything Claude Code exports into a process it spawned. Scrubbed so the
#: Session the harness starts is a **main** Session — the only kind v1.0 covers.
AGENT_MARKER_PREFIX = "CLAUDE_CODE_"
AGENT_MARKER_NAMES = ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT")

#: The one marker `child_environment` puts back. Measured above: it is what turns
#: an otherwise identical run into something the official roster does not list —
#: upstream's own name for the distinction, not the glossary's. In this project
#: the concept is a **Child Process** (`CONTEXT.md`), and "child Session" is a
#: synonym the glossary asks us to avoid.
CHILD_MARKER = "CLAUDE_CODE_CHILD_SESSION"

#: A terminal the size a person's would be. A pty opens at 0×0, and a TUI given
#: no room lays out against a width it never has — deterministic geometry costs
#: one ioctl and removes a whole class of "it rendered differently that time".
TERMINAL_ROWS = 40
TERMINAL_COLUMNS = 120

#: How long a typed instruction is left to settle before the return key follows.
#: Measured at build time: both TUIs read a fast paste in chunks, and a carriage
#: return arriving inside the same read is taken by the composer as a newline
#: instead of a submit. 1.5s was the smallest value that submitted on every
#: attempt across both lanes.
SUBMIT_SETTLE_SECONDS = 1.5

#: How much of the raw stream is kept in memory for a failure message. The whole
#: stream goes to disk regardless; this is the tail a verdict can quote.
SCREEN_TAIL_BYTES = 8000

#: Grace between asking a hand-started Session to go and insisting.
STOP_GRACE_SECONDS = 10.0

_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[()][AB012]|\x1b[=>]")


def terminal_environment(path_value: str) -> dict[str, str]:
    """The environment a terminal the user opened would carry, not this agent's."""
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(AGENT_MARKER_PREFIX) and name not in AGENT_MARKER_NAMES
    }
    environment["PATH"] = path_value
    environment["TERM"] = "xterm-256color"
    return environment


def child_environment(path_value: str) -> dict[str, str]:
    """A terminal environment with the child marker put back, for the `child` step."""
    environment = terminal_environment(path_value)
    environment[CHILD_MARKER] = "1"
    return environment


def resolve(binary: str, path_value: str) -> Path | None:
    """Where the ordinary command really is — the binary, never the shell function."""
    found = shutil.which(binary, path=path_value)
    return Path(found) if found else None


class SessionRefused(RuntimeError):
    """The command would not start, so there is no Session to judge anything on."""


class HandStartedSession:
    """One `claude` or `codex`, running in a pty, exactly as a person would run it."""

    def __init__(
        self,
        *,
        lane: str,
        binary: Path,
        arguments: tuple[str, ...],
        workspace: Path,
        environment: dict[str, str],
        journal,  # support.Journal
        transcript: Path,
    ) -> None:
        self.lane = lane
        self.binary = binary
        self.arguments = arguments
        self.workspace = workspace
        self.environment = environment
        self.journal = journal
        self.transcript = transcript
        self._master: int | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._tail: deque[bytes] = deque(maxlen=64)
        self._reader: threading.Thread | None = None
        self._stop = threading.Event()

    # --- lifecycle --------------------------------------------------------

    def start(self) -> None:
        master, slave = pty.openpty()
        fcntl.ioctl(
            slave,
            termios.TIOCSWINSZ,
            struct.pack("HHHH", TERMINAL_ROWS, TERMINAL_COLUMNS, 0, 0),
        )
        os.set_blocking(master, False)
        command = [str(self.binary), *self.arguments]
        try:
            self._process = subprocess.Popen(
                command,
                cwd=str(self.workspace),
                env=self.environment,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                # Its own process group, so a stop reaches the TUI and everything
                # it spawned rather than only the one pid the harness knows.
                start_new_session=True,
            )
        except OSError as unstartable:
            os.close(master)
            os.close(slave)
            raise SessionRefused(f"{command[0]} would not start: {unstartable}") from None
        os.close(slave)
        self._master = master
        self.transcript.parent.mkdir(parents=True, exist_ok=True)
        self._reader = threading.Thread(target=self._drain, name=f"pty-{self.lane}", daemon=True)
        self._reader.start()
        self.journal(
            "session.hand_started",
            lane=self.lane,
            command=command,
            workspace=str(self.workspace),
            pid=self._process.pid,
            pty_log=str(self.transcript),
            scrubbed=sorted(
                name
                for name in os.environ
                if name.startswith(AGENT_MARKER_PREFIX) or name in AGENT_MARKER_NAMES
            ),
        )

    def stop(self) -> None:
        """Ask the whole process group to go, then insist; always close the pty."""
        process = self._process
        self._stop.set()
        if process is not None and process.poll() is None:
            for attempt in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(os.getpgid(process.pid), attempt)
                except (ProcessLookupError, PermissionError):
                    break
                try:
                    process.wait(timeout=STOP_GRACE_SECONDS)
                    break
                except subprocess.TimeoutExpired:
                    continue
        if self._reader is not None:
            self._reader.join(timeout=2.0)
        if self._master is not None:
            try:
                os.close(self._master)
            except OSError:
                pass
            self._master = None
        self.journal(
            "session.stopped",
            lane=self.lane,
            pid=process.pid if process else None,
            returncode=process.poll() if process else None,
        )

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process else None

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    # --- driving ----------------------------------------------------------

    def submit(self, words: str) -> None:
        """Type an instruction and press return, the way a person does."""
        self.write(words)
        time.sleep(SUBMIT_SETTLE_SECONDS)
        self.write("\r")
        self.journal("session.submitted", lane=self.lane, words=words)

    def choose_first_option(self) -> None:
        """Press return on whatever dialog is showing, taking its default.

        Both TUIs open a choice list with the first item selected and `Enter to
        confirm`; on Claude that first item is `1. Yes` (measured). The harness
        never reads the dialog to decide — this exists so a lane can be released
        when the *product* was supposed to answer and did not, and the journal
        says which happened.
        """
        self.write("\r")
        self.journal("session.pressed_return", lane=self.lane)

    def write(self, text: str) -> None:
        if self._master is None:
            raise SessionRefused("nothing is running to type into")
        os.write(self._master, text.encode())

    # --- evidence ---------------------------------------------------------

    def screen_tail(self) -> str:
        """The recent raw stream, escapes stripped — evidence only, never parsed."""
        joined = b"".join(self._tail).decode("utf-8", "replace")
        return _ANSI.sub("", joined).replace("\r", "")[-SCREEN_TAIL_BYTES:]

    def _drain(self) -> None:
        assert self._master is not None
        with self.transcript.open("wb") as sink:
            while not self._stop.is_set():
                try:
                    data = os.read(self._master, 65536)
                except BlockingIOError:
                    time.sleep(0.05)
                    continue
                except OSError as closed:
                    if closed.errno in (errno.EIO, errno.EBADF):
                        break
                    raise
                if not data:
                    break
                sink.write(data)
                sink.flush()
                self._tail.append(data)


# --- ground truth ----------------------------------------------------------
#
# What the harness knows independently of the product, so `roster` can be judged
# rather than taken on the engine's word. Both sources are the agents' own — the
# official roster command and the rollout the TUI writes — read directly.


@dataclass(frozen=True)
class GroundTruth:
    """Who the harness started, according to the agent itself."""

    session_id: str
    pid: int
    workspace: Path
    name: str | None
    status: str | None
    record: Path | None

    def describe(self) -> str:
        return (
            f"{self.session_id or '<no session id yet>'} (pid {self.pid}, name {self.name!r}, "
            f"status {self.status!r}, workspace {self.workspace}, "
            f"record {self.record.name if self.record else None})"
        )


def claude_ground_truth(pid: int, environment: dict[str, str]) -> GroundTruth | None:
    """The official roster, filtered to one pid. `claude agents --json`, #74's source.

    Read by the harness as an oracle, never as a substitute for the product: what
    `roster` asserts is that `bridgectl` reports what this returns.
    """
    for row in claude_rows(environment):
        if row.get("pid") != pid:
            continue
        session_id = str(row.get("sessionId", ""))
        return GroundTruth(
            session_id=session_id,
            pid=pid,
            workspace=Path(str(row.get("cwd", ""))),
            name=row.get("name"),
            status=row.get("status"),
            record=claude_transcript(session_id),
        )
    return None


def claude_rows(environment: dict[str, str]) -> list[dict]:
    """Every row `claude agents --json` shows, decoded. Empty when it cannot be read."""
    binary = resolve("claude", environment.get("PATH", os.environ["PATH"]))
    if binary is None:
        return []
    finished = subprocess.run(
        [str(binary), "agents", "--json"],
        capture_output=True,
        text=True,
        timeout=30.0,
        env=environment,
    )
    try:
        rows = json.loads(finished.stdout)
    except json.JSONDecodeError:
        return []
    return rows if isinstance(rows, list) else []


def claude_transcript(session_id: str) -> Path | None:
    """`~/.claude/projects/<flattened cwd>/<session id>.jsonl`.

    Found by session id rather than by flattening the path: the flattening
    replaces `/`, `.` **and `_`** with `-` (measured — a workspace named
    `gvc-probe2-_27jcxas` lands under `…-gvc-probe2--27jcxas`), and a rule that
    has to be rediscovered is a rule the harness should not depend on.
    """
    root = Path.home() / ".claude" / "projects"
    if not root.exists():
        return None
    return next(iter(sorted(root.glob(f"*/{session_id}.jsonl"))), None)


def codex_ground_truth(pid: int, workspace: Path, since: float) -> GroundTruth:
    """What the harness knows about the `codex` it started — and it always knows something.

    **Measured 2026-08-26, and it overturned this function's first shape.** Codex
    writes no rollout when a Session starts; it writes one when the first *turn*
    starts. A full acceptance run proved it the hard way: `codex` sat in
    `starting MCP servers` for the whole 180s ground-truth wait, its workspace
    stayed empty, and the lane reported a harness failure where the product had
    not yet been asked anything. Waiting on the rollout is a deadlock — the
    harness will not type into a Session it has not confirmed, and the Session
    writes nothing until it is typed into.

    So the oracle for this lane is **the process the harness itself started**.
    That is not a workaround, it is the same evidence the product's own Codex
    discovery has: #74's `adapters/agent/codex/processes.py` enumerates running
    `codex` TUIs *by pid and cwd*, and every row it yields is `unattached`. The
    session id joins later, from the rollout, once there is one — and until then
    a roster row is matched on the pid, which `SessionTarget` carries.
    """
    rollout = codex_rollout(workspace, since)
    meta = _first_session_meta(rollout) if rollout else None
    return GroundTruth(
        session_id=str(meta.get("session_id", "")) if meta else "",
        pid=tui_pid(pid),
        workspace=workspace,
        name=None,
        status=None,
        record=rollout,
    )


def tui_pid(started: int) -> int:
    """The process that *is* the Session, starting from the one the harness ran.

    **Measured on 2026-08-26: `codex` on this machine is an npm shim.** The
    thing on `PATH` is a node script that spawns the real binary as a child, so
    the pid the harness holds is the shim's and the TUI — the process that draws
    the interface and writes the rollout — is one level down:

        70191   1      node …/@openai/codex/bin/codex
        70196   70191  …/@openai/codex-darwin-arm64/vendor/…/bin/codex

    The product reports the **native** one, and that is the right answer rather
    than a discrepancy to paper over: a Homebrew or direct install has no shim
    at all, so a `SessionTarget` built on the shim would change shape with how
    Codex happened to be installed. The oracle resolves down to meet it.

    **The join is ancestry, and only then the argv.** The search is restricted
    to descendants of the pid this harness itself started, which is what makes
    the answer *this* Session rather than a coincidence; the argument vector
    only picks which descendant. A pid that is already the native binary — the
    no-shim install — is returned unchanged.

    **Deliberately not shared with `adapters/agent/codex/processes.py`**, which
    makes the same judgement for the product. An oracle that imported the
    classifier it is checking would turn `roster` into the product agreeing with
    itself. The rule is written out again here, in eight lines, on purpose.
    """
    if _is_native_codex(started):
        return started
    for pid in _descendants(started):
        if _is_native_codex(pid):
            return pid
    return started


def _is_native_codex(pid: int) -> bool:
    """Whether that process is the Codex binary itself rather than a launcher."""
    argv = _argv_of(pid)
    return bool(argv) and Path(argv[0]).name == "codex" and Path(argv[0]).suffix == ""


def _descendants(pid: int) -> list[int]:
    """Every process below this one, breadth first. Empty if `ps` cannot say."""
    try:
        listing = subprocess.run(
            ["/bin/ps", "-axo", "pid=,ppid="], capture_output=True, text=True, timeout=10.0
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    children: dict[int, list[int]] = {}
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            children.setdefault(int(parts[1]), []).append(int(parts[0]))
    found: list[int] = []
    queue = list(children.get(pid, ()))
    while queue:
        current = queue.pop(0)
        found.append(current)
        queue.extend(children.get(current, ()))
    return found


def _argv_of(pid: int) -> list[str]:
    try:
        listing = subprocess.run(
            ["/bin/ps", "-p", str(pid), "-o", "args="],
            capture_output=True,
            text=True,
            timeout=10.0,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return listing.strip().split()


def codex_rollout(workspace: Path, since: float) -> Path | None:
    """This run's rollout, if `codex` has written one yet — re-located, never cached.

    The first line is `session_meta`, carrying `session_id`, `cwd`, `originator`
    and `thread_source` — the fields #74's P13 row cites. The workspace is
    compared by realpath because `session_meta.cwd` is resolved.
    """
    root = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "sessions"
    if not root.exists():
        return None
    wanted = os.path.realpath(workspace)
    for rollout in sorted(root.rglob("rollout-*.jsonl"), key=lambda p: p.stat().st_mtime):
        if rollout.stat().st_mtime < since:
            continue
        meta = _first_session_meta(rollout)
        if meta is not None and os.path.realpath(str(meta.get("cwd", ""))) == wanted:
            return rollout
    return None


def codex_turn_policy(rollout: Path | None) -> str:
    """What Codex says the last turn ran under, in Codex's own words.

    Every turn appends a `turn_context` record carrying `approval_policy`,
    `approvals_reviewer` and `sandbox_policy` (measured 2026-08-27 on codex-cli
    0.149.1 and 0.150.0, including on the run that opened #105). The `approval` step names
    this rather than the flag the harness passed, because two of the three are
    not the harness's to claim: the **product** pins the policy and the reviewer
    on every turn it starts (`agent/codex/threads.py:36-40`), so reading them
    back off the far side is the difference between a run that says the pin was
    applied and a run that assumes it.

    The last record wins: it is the turn whose permission the step just graded.
    """
    if rollout is None:
        return "no policy: codex has written no record of this Session yet"
    latest: dict | None = None
    try:
        with rollout.open() as lines:
            for line in lines:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("type") == "turn_context" and isinstance(record.get("payload"), dict):
                    latest = record["payload"]
    except OSError as unreadable:
        return f"no policy: {rollout.name} could not be read ({unreadable})"
    if latest is None:
        return f"no policy: {rollout.name} carries no turn_context record"
    sandbox = latest.get("sandbox_policy")
    sandbox_kind = sandbox.get("type") if isinstance(sandbox, dict) else sandbox
    return (
        f"sandbox {sandbox_kind!r}, approval_policy {latest.get('approval_policy')!r}, "
        f"approvals_reviewer {latest.get('approvals_reviewer')!r} "
        f"(codex's own `turn_context`, {rollout.name})"
    )


def _first_session_meta(rollout: Path) -> dict | None:
    try:
        with rollout.open() as lines:
            for line in lines:
                record = json.loads(line)
                if record.get("type") == "session_meta":
                    payload = record.get("payload")
                    return payload if isinstance(payload, dict) else None
                return None
    except (OSError, json.JSONDecodeError):
        return None
    return None

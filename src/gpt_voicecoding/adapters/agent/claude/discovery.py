"""What Claude Sessions are running, asked of Claude Code's own roster.

`claude agents --json` is the **official** answer to "what is running", and it is
launch-independent: it lists Sessions this engine never started, which is what
makes a bridge over the user's own Sessions possible at all (#70). Nothing here
reads a transcript, a lock file or a process table — one command, one JSON
document, mapped onto the seam field for field.

**Coverage is per `CLAUDE_CONFIG_DIR`, and that is a decision rather than a
limit** (#71). Claude Code keeps its Session registry inside the config
directory, so this command answers for one directory and installation writes to
the same one. Simon scoped v1.0 to the main account on 2026-08-26, so that is
one whole universe and not a gap.

**A child Session is absent from this roster, and the roster is right.** A
`claude` that inherits `CLAUDE_CODE_*` / `CLAUDECODE` / `CLAUDE_PID` from a
parent agent runs as a child: transcript saving off, and not listed here (#73,
measured). So every row this returns is a main Session, and #79 owns finding the
children some other way.

**A `waiting` row carries `waitingFor`**, the same label the registry record
carries, and it is read the same way — through `waiting_labels.py`, so this
reader and the Reply Window sweep cannot disagree about what a wait is (#150).

**No version pin.** #71's decision, taken knowingly: this rides surface that may
move, and the safeguard is honest failure rather than a gate that would refuse
every Session on the machine the day after an upgrade. `PROVEN_AGAINST_VERSION`
is documentation for the next re-probe.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

from gpt_voicecoding.adapters.agent import _naming
from gpt_voicecoding.adapters.agent._project import ProjectNames
from gpt_voicecoding.adapters.agent.claude import waiting_labels
from gpt_voicecoding.adapters.agent.claude.waiting_labels import (
    NOTHING_READ_YET,
    StopDisposition,
)
from gpt_voicecoding.seams.agent import (
    LaneDiscovery,
    ProgressObservation,
    SessionInspection,
    SessionLifecycle,
    SessionState,
    WaitingFor,
)
from gpt_voicecoding.seams.identity import AgentKind, SessionTarget

_log = logging.getLogger(__name__)

#: The command, and the flag that makes it answerable by a machine. `--all` is
#: deliberately not passed: it adds completed background agents, and a roster of
#: Sessions the user can be told about is a roster of ones that are running.
ROSTER_COMMAND: Final = ("claude", "agents", "--json")

#: The build every shape below was read off, on Simon's machine on 2026-08-26.
#: Documentation for the next re-probe, never a gate — see this module's docstring.
PROVEN_AGAINST_VERSION: Final = "2.1.246"

#: How long the roster command is given before it is treated as unavailable. A
#: discovery that hangs is a discovery loop that stops, so this is a ceiling on
#: the whole lane rather than a guess at how fast the command is.
COMMAND_TIMEOUT_SECONDS: Final = 15.0

#: What Claude Code calls a Session the user is sitting in front of. Other kinds
#: exist behind `--all` and are not Sessions in this product's sense.
INTERACTIVE_KIND: Final = "interactive"

#: `status` walks these across one turn (#73, measured). Anything else is a
#: Session doing something this build has not seen a word for, which is
#: `RUNNING` — the reading that keeps a Relay waiting rather than delivering it
#: into a state nobody has looked at.
STATUS_WORDS: Final = {
    "idle": SessionState.IDLE,
    "busy": SessionState.RUNNING,
    "waiting": SessionState.WAITING,
}


@dataclass(frozen=True, slots=True)
class CommandResult:
    """What running the roster command produced."""

    code: int
    stdout: str
    stderr: str


#: How the roster command is run. Injected so the mapping can be tested against
#: measured bytes rather than against whatever `claude` this machine has.
Runner = Callable[[list[str]], Awaitable[CommandResult]]


async def run_command(argv: list[str]) -> CommandResult:
    """Run one command and collect it. The only place this lane touches a process."""
    process = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(), COMMAND_TIMEOUT_SECONDS)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise
    return CommandResult(
        code=process.returncode or 0,
        stdout=out.decode("utf-8", errors="replace"),
        stderr=err.decode("utf-8", errors="replace"),
    )


async def discover(
    *, run: Runner = run_command, projects: ProjectNames | None = None
) -> LaneDiscovery:
    """Every Claude Session running under this config directory, or why none.

    **A lane that could not look says so; it never reports an empty machine.**
    The two are the same shape and opposite facts, and Bridge Core acts on the
    difference: an error leaves the roster's Claude rows exactly as they were,
    while an empty answer ends them.
    """
    try:
        result = await run(list(ROSTER_COMMAND))
    except (OSError, TimeoutError) as unreachable:
        return LaneDiscovery(error=f"could not run `{' '.join(ROSTER_COMMAND)}`: {unreachable}")

    if result.code != 0:
        said = (result.stderr or result.stdout).strip() or "no output"
        return LaneDiscovery(
            error=f"`{' '.join(ROSTER_COMMAND)}` exited {result.code}: {said[:400]}"
        )

    try:
        document: Any = json.loads(result.stdout)
    except json.JSONDecodeError as unreadable:
        return LaneDiscovery(
            error=f"`{' '.join(ROSTER_COMMAND)}` did not answer with JSON: {unreadable}"
        )
    if not isinstance(document, list):
        return LaneDiscovery(
            error=(
                f"`{' '.join(ROSTER_COMMAND)}` answered with "
                f"{type(document).__name__}, not a list of Sessions"
            )
        )

    return LaneDiscovery(rows=tuple(await _rows(document, projects or ProjectNames())))


async def _rows(document: list[Any], projects: ProjectNames) -> list[SessionInspection]:
    """Every row that can be read, skipping the ones that cannot.

    One unreadable row is not a broken roster, and refusing the whole document
    over it would hide every healthy Session on the machine. The skip is logged
    so it is a thing somebody can find rather than a silence.
    """
    found: list[SessionInspection] = []
    for row in document:
        if not isinstance(row, dict):
            continue
        inspection = _inspection(row)
        if inspection is None:
            _log.info("skipped a roster row this build cannot address: %r", row)
            continue
        found.append(await _named(inspection, row, projects))
    return found


async def _named(
    inspection: SessionInspection, row: dict[str, Any], projects: ProjectNames
) -> SessionInspection:
    """The same row, carrying its Session Name.

    **The task half is the roster's own `name`** — `workspace-claude-ed` and the
    like — which is the official answer to "what is this Session called" and is
    on every row from the moment the Session exists (#73). Nothing is asked of
    the Session to get it and no transcript is opened for it, which is what makes
    it stable enough to be the name the user speaks
    (`legacy@1d32845:bridge/hook.py:215-253` had the Session report a task title
    over the hook instead — *dropped, because* the amended #67 port table removed
    that route on 2026-08-25).

    The project half is the workspace's, resolved here because this is the lane
    that knows the workspace. A row with neither half stays unnamed.
    """
    task = _name(row)
    if task is None:
        return inspection
    project = await projects.of(inspection.workspace)
    if project is None:
        return inspection
    return replace(inspection, name=_naming.compose(project, task))


def _inspection(row: dict[str, Any]) -> SessionInspection | None:
    """One roster row as the seam holds it, or `None` if it is not addressable."""
    session_id = row.get("sessionId")
    pid = row.get("pid")
    if not isinstance(session_id, str) or not session_id.strip():
        return None
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        # A Claude target without a pid is ambiguous by construction: `--resume`
        # forks a second process under the same session id.
        return None
    kind = row.get("kind")
    if isinstance(kind, str) and kind.strip() and kind != INTERACTIVE_KIND:
        # Only a stated non-interactive kind is skipped. A row that does not say
        # is kept: this command is not asked for the other kinds, so a missing
        # field is far more likely to be a field that moved than a Session that
        # is not one — and blanking the roster over it is the worse mistake.
        return None

    state = STATUS_WORDS.get(str(row.get("status", "")), SessionState.RUNNING)
    cwd = row.get("cwd")
    return SessionInspection(
        target=SessionTarget(agent=AgentKind.CLAUDE, session_id=session_id.strip(), pid=pid),
        # Already a realpath when Claude Code writes it (#73). Kept as given, so
        # a join against it compares what the agent itself believes.
        workspace=Path(str(cwd)) if isinstance(cwd, str) and cwd.strip() else Path(),
        lifecycle=SessionLifecycle.LIVE,
        state=state,
        waiting_for=_waiting_for(state, row.get("waitingFor")),
        # `progress` and `last_activity` are transcript facts (#76). `startedAt`
        # is on this row and is deliberately not read as either: when a Session
        # began is not when it last did anything.
        progress=ProgressObservation(),
        last_activity=None,
    )


def _waiting_for(state: SessionState, label: Any) -> WaitingFor:
    """What the roster alone can honestly say a Session is waiting for.

    Two things, now that Claude Code's own `waitingFor` label rides the row
    (#150, `waiting_labels.py`). The row still carries no tool, no dialog handle
    and no prompt, so the ordinary answer stays `UNKNOWN` with `caught_up=
    False` — the seam's word for *ask again* — and #75 answers it from the
    transcript that does carry those fields.

    The one thing the label settles here is the **negative**: a `dialog open` or
    a `goal proposal` is the user driving their own TUI, and a Session at one is
    not waiting on anybody. Saying so is what keeps a slash-command picker off
    the roster's `needs_the_user`, which is the gate Bridge Core's reconcile
    pass announces from.

    **A named wait is deliberately not promoted here, and the mechanism is the
    `approval_id`.** `permission prompt` classifies as `PERMISSION` and the
    Reply Window sweep announces it as one — but that sweep is handed the parked
    dialog's handle by the hook holding it open, and a roster row has no such
    field to carry (`seams/agent.py:181-185`). Bridge Core deduplicates a wait
    on `(target, approval_id or kind)` (`core/bridge.py:726-731`), so a row
    promoted to `PERMISSION` could only ever key `(target, PERMISSION)`, which
    is not the `(target, approval_id)` the live path used for the same dialog.
    The reconcile pass would therefore miss the dedup and escalate a *second*
    notice — "a tool needs your permission — answer it at the terminal" — for a
    decision the Approval Relay is already holding answerable elsewhere, which
    is precisely the double announcement `core/bridge.py:749-768` exists to
    prevent and which #150 exists to reduce.

    Widening that key is Bridge Core's, and #150 puts it out of scope. So the
    roster keeps its *ask again*, and its `waiting_for` is deliberately narrower
    than the registry reader's: the classification is shared, the acting on it
    is not. #151, which gives a Stop Notice the Session's own progress
    observation, reads this seam and should know that the narrowing is a dedup
    constraint rather than something the roster failed to measure.
    """
    if state is not SessionState.WAITING:
        return WaitingFor()
    if (
        waiting_labels.classify(label if isinstance(label, str) else None).disposition
        is StopDisposition.NEVER_A_STOP
    ):
        return WaitingFor()
    return NOTHING_READ_YET


def _name(row: dict[str, Any]) -> str | None:
    """The task half of this Session's name, straight off the official roster."""
    name = row.get("name")
    return name.strip() if isinstance(name, str) and name.strip() else None

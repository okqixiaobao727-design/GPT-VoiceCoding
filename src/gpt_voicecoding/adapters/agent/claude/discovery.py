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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from gpt_voicecoding.seams.agent import (
    LaneDiscovery,
    SessionInspection,
    SessionLifecycle,
    SessionState,
    WaitingFor,
    WaitingKind,
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


async def discover(*, run: Runner = run_command) -> LaneDiscovery:
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

    return LaneDiscovery(rows=tuple(_rows(document)))


def _rows(document: list[Any]) -> list[SessionInspection]:
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
        found.append(inspection)
    return found


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
        waiting_for=_waiting_for(state),
        # `progress` and `last_activity` are transcript facts (#76). `startedAt`
        # is on this row and is deliberately not read as either: when a Session
        # began is not when it last did anything.
        progress=None,
        last_activity=None,
        name=_name(row),
    )


def _waiting_for(state: SessionState) -> WaitingFor:
    """What the roster alone can honestly say a Session is waiting for.

    Almost nothing. `waiting` is the permission state on the builds measured so
    far (#73), but the roster carries no tool, no dialog handle and no prompt,
    and a question dialog has never been observed from here — so claiming
    `PERMISSION` would be reading one measurement as a closed set. `UNKNOWN`
    with `caught_up=False` is the seam's own word for *ask again*, and #75
    answers it from the transcript that does carry those fields.
    """
    if state is not SessionState.WAITING:
        return WaitingFor()
    return WaitingFor(kind=WaitingKind.UNKNOWN, caught_up=False)


def _name(row: dict[str, Any]) -> str | None:
    """The agent's own name for this Session — `workspace-claude-ed` and the like."""
    name = row.get("name")
    return name.strip() if isinstance(name, str) and name.strip() else None

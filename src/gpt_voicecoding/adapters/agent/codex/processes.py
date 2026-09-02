"""Running `codex` TUIs, found in the process table when the daemon cannot say.

**No legacy analogue.** The reference implementation never enumerated Codex
processes: it knew about the Sessions it had launched, from its own launch
records (`legacy@1d32845:bridge/daemon.py:1192-1257`), and a Session it had not
launched did not exist for it. This is new because the product changed — v1.0 is
a bridge over Sessions the *user* starts (#68) — so it is written fresh rather
than ported, and the one legacy habit kept is `pgrep` for an exact executable
name (`legacy@1d32845:bridge/host.py:795`).

**Most `codex` processes are not Sessions, and the filter is the whole module.**
Measured on this machine on 2026-08-26: `pgrep -x codex` returned five processes
and *none* of them was a Session — four `codex mcp-server` (one per Claude Code
session on the machine) and one `codex … app-server` inside ChatGPT.app. A
roster built on the process name alone would have invented five Sessions the
user could then be told had stopped. So a candidate is judged on its **argument
vector**: the subcommand list below is `codex --help` on 0.149.1, verbatim, and
a process running any of those is doing a job rather than holding a Session.
Anything else — no subcommand, a bare `[PROMPT]`, `resume` or `fork` — has a
TUI-shaped argv, but reaches the roster only with a controlling terminal. #144
captured a bare `codex` with `PPID=1` and `TTY=??`: detached debris, not positive
evidence of a current interactive run.

**Every candidate has positive interactive-process evidence, but not every
candidate has Session identity.** Its argv is a TUI and `ps` names its
controlling terminal. Only `codex resume <canonical UUID>` carries a thread id
that the rollout or daemon can independently name. Bare, prompt, picker,
`--last`, `fork`, and remote invocations carry no shared key, so this module
returns their pid and workspace but no identity.

**A candidate without an argv thread id is the ordinary case, not the
exceptional one** (#201). It used to be read here and discarded one caller up,
which made the roster reachable only by `codex resume <UUID>` — a shape no
hand-started TUI has. Such a candidate is now returned like any other, and
`codex/roster.py` decides what it can vouch for. Identity still never comes
from here: what this module adds for that rule is **when the process started**,
which is what lets a terminal be ruled out as the owner of a thread that
already existed before it did.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

_log = logging.getLogger(__name__)

#: The executable a Codex Session runs under. Matched exactly (`pgrep -x`), so
#: ChatGPT.app's bundled `Codex Framework` helpers do not match — they did match
#: `ps -o comm=` on this machine, which is why the exact form is the one used.
EXECUTABLE: Final = "codex"

#: The build the two tables below were read off `codex --help` on, on Simon's
#: machine on 2026-08-26. Same shape as the Claude lane's pins
#: (`claude/discovery.py:56`, `claude/registry.py:47`, `claude/approval.py:93`):
#: documentation for the next re-probe, never a gate.
#:
#: **They are transcribed, not derived, and that is the decision.** Reading
#: `codex --help` at discovery time would cost a subprocess on every pass and
#: put a free-form help text on the path that decides which processes are
#: Sessions — and it would fail *silently* on an upgrade that reworded it, which
#: is the failure this module exists to prevent. Transcribing means an upgrade
#: that adds a subcommand shows up as a job listed as a Session until someone
#: re-probes, which is loud, bounded, and recorded here.
PROVEN_AGAINST_VERSION: Final = "0.149.1"

#: `codex --help` on 0.149.1, verbatim, minus the two that open a TUI (`resume`
#: and `fork`) and minus `help`. A process whose first non-flag argument is one
#: of these is doing a job; it is not a Session the user is sitting in.
NON_INTERACTIVE_SUBCOMMANDS: Final = frozenset(
    {
        "agents",
        "app",
        "app-server",
        "apply",
        "a",
        "archive",
        "cloud",
        "completion",
        "debug",
        "delete",
        "doctor",
        "e",
        "exec",
        "exec-server",
        "features",
        "help",
        "login",
        "logout",
        "mcp",
        "mcp-server",
        "migrate-rollouts",
        "plugin",
        "queue",
        "remote-control",
        "review",
        "sandbox",
        "unarchive",
        "update",
    }
)

#: The global options that take a **separate** value, from `codex --help` on
#: 0.149.1, verbatim. This table is load-bearing rather than decoration: without
#: it, ChatGPT.app's own `codex -c features.code_mode_host=true app-server …`
#: reads as a Session, because the value of `-c` lands where the subcommand
#: should be. That was not hypothetical — it is what this module did when it was
#: first run against this machine.
VALUE_TAKING_OPTIONS: Final = frozenset(
    {
        "-a",
        "--add-dir",
        "--ask-for-approval",
        "-c",
        "--cd",
        "-C",
        "--config",
        "--disable",
        "--enable",
        "-i",
        "--image",
        "--local-provider",
        "-m",
        "--model",
        "-p",
        "--profile",
        "--remote",
        "--remote-auth-token-env",
        "-s",
        "--sandbox",
    }
)

#: How long the process table and the cwd lookups get, together.
COMMAND_TIMEOUT_SECONDS: Final = 10.0

#: How coarse a start time computed from `etime` is, and therefore how much
#: slack any comparison against it must allow.
#:
#: **Read here rather than guessed, because it follows from the field.** `etime`
#: is whole seconds and truncated, so a start computed from it is up to a second
#: later than the true one whatever else happens. A rule that excludes a thread
#: created before a terminal started must therefore allow one second, or it
#: would drop the true thread of a TUI that opened one in the same second it
#: launched — the fast start, which is the ordinary one.
#:
#: **Which side the clock read is on decides the other half of the error, and
#: only one side is safe.** `ps` samples `etime` at some moment inside the await
#: that launches it, and `enumerate_sessions` subtracts it from one clock read:
#:
#: * Read *after* the await, the error is `(sample - read) + truncation` — both
#:   terms positive, so the whole duration of the `ps` is added and the terminal
#:   reads **younger** than it is. That is not academic: on run
#:   `20260902T071547Z` a two-lane machine's `ps` cost more than this allowance,
#:   a hand-started TUI (19:15:56.366) read as younger than the thread it opened
#:   in that same second (`rollout-2026-09-02T19-15-56-01a060f9…`), the
#:   start-time filter ruled it out as that thread's owner, and a live Session
#:   dropped off the roster mid-run.
#: * Read *before* it, the error is `truncation - (ps duration)` — so a terminal
#:   can read **older** than it is, by however long the `ps` took. That widens,
#:   by that duration, the window in which a thread created just *before* a
#:   terminal could still be vouched for by it.
#:
#: The second is the safe direction and this module takes it. What the window
#: would have to contain to matter is a thread created in the sub-second before
#: its own terminal launched, and the case ADR 0020 wrote the rule against is
#: nothing like that shape: the loaded-but-dead root it excludes was sixteen
#: minutes older than the terminal that could not own it. Truncation, the half
#: that does not move, is still covered by the one second below.
START_TIME_RESOLUTION_SECONDS: Final = 1.0

#: macOS `ps`'s explicit answer that a process has no controlling terminal.
#: #144 captured this value on a detached `codex` process whose argv and cwd
#: otherwise looked like a TUI.
NO_CONTROLLING_TERMINAL: Final = "??"


@dataclass(frozen=True, slots=True)
class Candidate:
    """One running Codex TUI, with an exact native id only when argv carries it.

    `started_at` is epoch seconds, computed from `etime` against a clock read
    once per pass. It is `None` only for a candidate composed by hand: the
    reader below skips a `ps` row whose elapsed time it cannot parse, because a
    terminal that cannot say when it started cannot vouch for a thread's
    liveness by place (#201) and reporting it as if it could would reinstate
    #123's ghost rows.
    """

    pid: int
    workspace: Path
    session_id: str | None = None
    started_at: float | None = None


#: Runs one command and returns its stdout, or raises. Injected for tests.
Runner = Callable[[list[str]], Awaitable[str]]

#: Reads the wall clock, in epoch seconds. Injected so a start time computed
#: from an elapsed reading is a fixed number in a test.
Clock = Callable[[], float]


async def run_command(argv: list[str]) -> str:
    """Run one read-only command and collect its stdout.

    A non-zero exit is not an exception here: `pgrep` exits 1 when it matched
    nothing, which is the ordinary answer "no Codex is running" and not a
    failure to look.
    """
    process = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    try:
        out, _ = await asyncio.wait_for(process.communicate(), COMMAND_TIMEOUT_SECONDS)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise
    return out.decode("utf-8", errors="replace")


async def enumerate_sessions(
    *, run: Runner = run_command, now: Clock = time.time
) -> tuple[Candidate, ...]:
    """Every live interactive `codex` TUI, by pid, workspace, argv id and start.

    Raises `OSError` or `TimeoutError` if the process table cannot be read at
    all — the caller turns that into a lane error, because not being able to
    look is not the same as there being nothing to see.

    **The clock is read once, beside the `ps` that it dates.** `ps` reports
    elapsed time, and turning that into a start time is the one thing on this
    path that needs to know what time it is; doing it at this boundary is what
    lets the composition rule (`codex/roster.py`) decide everything from its
    arguments.

    **Once, not once per candidate**, because the `lsof` below is a subprocess
    per pid and may take seconds: a clock read after it would date the last
    candidate's `etime` — sampled in the same `ps` as the first — against a
    later moment, and push its computed start forward by the whole lookup. The
    one-second allowance `START_TIME_RESOLUTION_SECONDS` states would not cover
    that, and the cost would be a real hand-started root read as predating its
    own terminal and dropped, which is the bug this all exists to fix.

    **Before the `ps`, not after it**, for the same reason one moment further
    back: awaiting that subprocess is itself time, and a clock read afterwards
    adds all of it to every start it computes. See
    `START_TIME_RESOLUTION_SECONDS` for both directions of the error and which
    one is safe; run `20260902T071547Z` is where the unsafe one dropped a live
    Session.
    """
    found: list[Candidate] = []
    # Read before the launch: `ps` samples `etime` at some moment inside this
    # await, and the earliest moment consistent with the reading is the only one
    # that cannot make a terminal younger than it is.
    sampled_at = now()
    listed = await _interactive_pids(run)
    for pid, session_id, elapsed in listed:
        started_at = sampled_at - elapsed
        workspace = await _cwd_of(pid, run)
        if workspace is None:
            # A process that ended between the listing and the lookup, or one
            # this user may not inspect. Either way there is no Session here to
            # name a workspace for, and a row without one cannot be joined to
            # anything.
            continue
        found.append(
            Candidate(
                pid=pid,
                workspace=workspace,
                session_id=session_id,
                started_at=started_at,
            )
        )
    return tuple(found)


async def _interactive_pids(run: Runner) -> list[tuple[int, str | None, float]]:
    """TTY-backed `codex` processes, their argv thread id, and how long they have run."""
    listing = await run(["/bin/ps", "-axo", "pid=,ppid=,tty=,etime=,args="])
    found: list[tuple[int, str | None, float]] = []
    for line in listing.splitlines():
        pid, terminal, elapsed, argv = _split(line)
        if pid is None or terminal is None or elapsed is None or not argv:
            continue
        if terminal == NO_CONTROLLING_TERMINAL:
            continue
        if Path(argv[0]).name != EXECUTABLE or not is_interactive(argv):
            continue
        found.append((pid, session_id_from_argv(argv), elapsed))
    return found


def elapsed_seconds(text: str) -> float | None:
    """How long a process has run, from `ps`'s `etime`, or `None` if unreadable.

    `[[dd-]hh:]mm:ss`, which is POSIX's own spelling of the field. Chosen over
    `lstart` deliberately: `lstart` is an absolute time but a locale-formatted
    one — this machine prints `Wed  2 Sep 08:52:06 2026` where an en_US machine
    prints `Wed Sep  2 08:52:06 2026` — so parsing it would make the roster
    depend on the locale of whoever's launchd started the engine.
    """
    days, _, clock = text.strip().rpartition("-")
    parts = clock.split(":")
    if not 2 <= len(parts) <= 3 or (days and not days.isdigit()):
        return None
    if not all(part.isdigit() for part in parts):
        return None
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return seconds + int(days or 0) * 86_400


def is_interactive(argv: list[str]) -> bool:
    """Whether this argument vector opens a Session rather than doing a job.

    The **subcommand position** decides, and finding it means stepping over the
    options *and their values* — see `VALUE_TAKING_OPTIONS` for why that is not
    optional. Nothing else about the options is parsed: the question is "is
    there a subcommand", not "what did the user configure".

    Reaching the end without a positional is `codex` with flags only, which is
    the plainest Session there is; a positional that is not a known subcommand
    is the optional `[PROMPT]`, which opens one too.
    """
    tokens = iter(argv[1:])
    for token in tokens:
        if token in VALUE_TAKING_OPTIONS:
            next(tokens, None)  # step over the value this option takes
            continue
        if token.startswith("-"):
            continue  # a boolean flag, or `--flag=value`, which carries its own
        if "=" in token and not token.strip().startswith("="):
            # A backstop for an option this table does not know yet: every value
            # `-c` takes has this shape, and the failure it guards against is a
            # job listed as a Session the user can be told stopped. A single
            # bare `key=value` prompt loses its Session instead, which is the
            # cheaper of the two mistakes and recoverable by typing anything else.
            _log.info("stepping over %r in a codex argv: it is shaped like an option value", token)
            continue
        return token not in NON_INTERACTIVE_SUBCOMMANDS
    return True


def session_id_from_argv(argv: list[str]) -> str | None:
    """The exact UUID in `codex resume <SESSION_ID>`, or no shared identity.

    `resume --last`, the picker, and a session *name* all identify something
    only inside Codex. A canonical UUID is the one argv fact a rollout and a
    daemon thread independently carry, so every other TUI shape returns
    `None`. `fork <UUID>` names the source thread rather than the new one and is
    deliberately excluded.
    """
    tokens = iter(argv[1:])
    for token in tokens:
        if token in VALUE_TAKING_OPTIONS:
            next(tokens, None)
            continue
        if token.startswith("-") or ("=" in token and not token.strip().startswith("=")):
            continue
        if token != "resume":
            return None
        for argument in tokens:
            if argument in VALUE_TAKING_OPTIONS:
                next(tokens, None)
                continue
            if argument.startswith("-"):
                continue
            try:
                parsed = uuid.UUID(argument)
            except ValueError:
                return None
            canonical = str(parsed)
            return canonical if argument.lower() == canonical else None
        return None
    return None


def _split(line: str) -> tuple[int | None, str | None, float | None, list[str]]:
    """Read `pid ppid tty etime args` from one macOS `ps` row.

    Split on the four leading fields only, because `args=` is last precisely so
    that a path with spaces in it stays one field's problem rather than the
    parser's. `etime` is a single unpadded token, so it splits like the rest.
    """
    head, _, rest = line.strip().partition(" ")
    parent, _, rest = rest.strip().partition(" ")
    terminal, _, rest = rest.strip().partition(" ")
    elapsed, _, argv = rest.strip().partition(" ")
    if not head.isdigit() or not parent.isdigit() or not terminal:
        return None, None, None, []
    return int(head), terminal, elapsed_seconds(elapsed), argv.split()


async def _cwd_of(pid: int, run: Runner) -> Path | None:
    """Where that process is running, or `None` if it cannot be asked.

    `lsof -Fn` because it is the only way to read another process's cwd on
    macOS, and the `-F` machine format because its output is one field per line
    rather than a table that changes shape with the widest path on the machine.
    """
    try:
        listing = await run(["/usr/sbin/lsof", "-a", "-d", "cwd", "-p", str(pid), "-Fn"])
    except (OSError, TimeoutError):
        return None
    for line in listing.splitlines():
        if line.startswith("n") and line[1:].strip():
            return Path(line[1:].strip())
    return None

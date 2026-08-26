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
Anything else — no subcommand, a bare `[PROMPT]`, `resume` or `fork` — is a TUI.

**Every candidate is a Session nobody has spoken to yet.** This source knows a
pid, a cwd and when the process started, and nothing else; it has no thread id,
because Codex writes the rollout that carries one at the first turn (#73). Tying
a candidate back to a thread is `rollouts.newest_for`'s job, and it only ever
succeeds once the Session has done something.

**The start time is here because the workspace outlives the Session.** The join
`discovery.py` makes is workspace-to-rollout, and yesterday's rollout sits in
the same directory as today's fresh TUI — so without a start time the newest
rollout there is claimed by whoever is running there now, naming an
un-spoken-to Session with a dead thread's id. `ps` supplies it as `etime`
rather than `etimes`, which macOS does not have (measured 2026-08-26), and as
an *elapsed* duration rather than an instant, so every row on one reading is
subtracted from the **same** clock reading: two rows of one `ps` are one moment,
and letting them drift apart would be inventing an ordering the table does not
have.
"""

from __future__ import annotations

import asyncio
import logging
import time
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

#: `etime` counts in whole seconds, so a process that started 4.9 s ago reads as
#: 4 and the start time derived from it lands *after* the true one. The rollout
#: filter compares against that start time, so erring late would discard a
#: rollout the Session really wrote; erring early can at worst re-admit one
#: second of a previous Session that Codex cannot have written a turn into. One
#: second of slack buys the cheaper of those two mistakes.
START_TIME_GRANULARITY_SECONDS: Final = 1.0

#: Reads the wall clock. Injected so a test can hold one moment still.
Clock = Callable[[], float]


@dataclass(frozen=True, slots=True)
class Candidate:
    """One running Codex TUI, as the process table alone can describe it."""

    pid: int
    workspace: Path
    #: When this process began, in epoch seconds, derived from `ps`'s elapsed
    #: time. Required rather than defaulted: a candidate that cannot say when it
    #: started cannot be joined to a rollout without risking a dead thread's id.
    started_at: float


#: Runs one command and returns its stdout, or raises. Injected for tests.
Runner = Callable[[list[str]], Awaitable[str]]


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
    """Every running `codex` TUI this user owns, by pid, workspace and start time.

    Raises `OSError` or `TimeoutError` if the process table cannot be read at
    all — the caller turns that into a lane error, because not being able to
    look is not the same as there being nothing to see.
    """
    reading = now()
    found: list[Candidate] = []
    for pid, elapsed in await _interactive_pids(run):
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
                started_at=reading - elapsed - START_TIME_GRANULARITY_SECONDS,
            )
        )
    return tuple(found)


async def _interactive_pids(run: Runner) -> list[tuple[int, float]]:
    """The `codex` processes that are Sessions, with how long each has been up.

    A row whose elapsed time cannot be read is dropped rather than defaulted:
    the only safe default would be "started at the beginning of time", which is
    exactly the claim that lets it adopt a dead Session's rollout.
    """
    listing = await run(["/bin/ps", "-axo", "pid=,etime=,args="])
    found: list[tuple[int, float]] = []
    for line in listing.splitlines():
        pid, elapsed, argv = _split(line)
        if pid is None or not argv:
            continue
        if Path(argv[0]).name != EXECUTABLE or not is_interactive(argv):
            continue
        if elapsed is None:
            _log.info("codex pid %s reported an elapsed time this reader cannot parse", pid)
            continue
        found.append((pid, elapsed))
    return found


def _elapsed_seconds(field: str) -> float | None:
    """`ps`'s `etime`, `[[dd-]hh:]mm:ss`, as seconds. `None` if it is not that.

    macOS `ps` has no `etimes`, so the duration arrives formatted and has to be
    read back (measured 2026-08-26). `lstart` would give an instant rather than
    a duration, but it is a localised date and would move with the machine.
    """
    days, _, clock = field.partition("-")
    if not clock:
        days, clock = "0", days
    parts = clock.split(":")
    if not days.isdigit() or not 2 <= len(parts) <= 3 or not all(p.isdigit() for p in parts):
        return None
    hours, minutes, seconds = ([0] * (3 - len(parts))) + [int(p) for p in parts]
    return int(days) * 86_400 + hours * 3_600 + minutes * 60 + seconds


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


def _split(line: str) -> tuple[int | None, float | None, list[str]]:
    """`  1234 05:00 /path/to/codex resume --last` → `(1234, 300.0, [...])`.

    Split on the two leading fields only, because `args=` is last precisely so
    that a path with spaces in it stays one field's problem rather than the
    parser's.
    """
    head, _, rest = line.strip().partition(" ")
    if not head.isdigit():
        return None, None, []
    elapsed, _, argv = rest.strip().partition(" ")
    return int(head), _elapsed_seconds(elapsed), argv.split()


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

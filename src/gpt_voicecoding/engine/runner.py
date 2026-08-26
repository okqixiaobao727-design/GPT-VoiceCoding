"""Starting the engine from a command line, and stopping it when asked.

Deliberately small: read the arguments, load the configuration, take ownership of
the log, assemble, serve until a signal says otherwise, and exit with a number
that means something.

**Adoption happens here, between loading the configuration and building the
engine** — ADR 0004 puts it "before the engine object exists", and this is the
last moment that is true. The engine itself never adopts: an `Engine` that
redirected the standard streams when it was constructed would take a test
runner's output with it, and adoption is a property of *this process*, not of the
object it goes on to build.

**Which side of adoption a refusal lands on is therefore decided here.** A
configuration that cannot be read or does not say enough is refused before the
log is owned, so it is spoken on the terminal that started the engine — which is
the only place it can be seen, because there is no log yet and an engine that
dies this early never answers a status query either. Everything after adoption —
an adapter that cannot be imported, a socket that cannot be bound, an adapter
whose far side is not there — is written to stderr exactly as before and lands in
the engine's own log, because stderr *is* the log from that point on. Nothing is
lost either way; the two failures are simply readable in different places, and
the exit code says the same thing in both.

**Every phase before the engine serves — reading the configuration, assembling,
starting — refuses with one sentence and exit 2, on any exception. Only a failure
*while serving* may look like a crash.** That is the contract, and it is stated
as a rule rather than as three cases because it was twice repaired one case at a
time: first only `ConfigError` was a refusal, then only `OSError` around the
serve, and each time some other type — and every shipped adapter raises its own —
fell through to the interpreter as exit 1 and a traceback.

Found from the app bundle, where that is at its worst: post-adoption stderr *is*
the log, so the menu-bar shell's stderr panel is empty and the shell restarts on
every exit, turning a first-run misconfiguration into a silent crash loop. The
most likely first run of all hit it — a Companion Channel whose credential
variable is not set refuses at *assembly*, which was the last uncovered phase.

Nothing is swallowed: the whole traceback goes to the diagnostics of whichever
phase raised it, and the last line is a sentence. A `TypeError` inside somebody's
`connect` is a bug, and that traceback is the only thing that explains it.

**The environment is cleaned once, here, before anything is spawned.** Every
adapter child inherits what this process holds, so ADR 0004's stripped prefixes
are applied at the one point all of them descend from rather than at each spawn
site. A process this engine did not spawn — a Session the user started in their
own terminal — inherits that terminal's environment instead, which this call
cannot discharge for it and does not pretend to.

Signals: SIGINT and SIGTERM both mean stop, and stopping is orderly — the loops
are cancelled and the socket file is removed, so the next start is not left
claiming its own debris.

**Stopping is written down, and it is bounded.** Both because of #96, where this
engine took SIGTERM, wrote nothing, and was still alive when the SIGKILL came
twenty seconds later — leaving the `codex app-server` it had spawned orphaned on
its socket, which refused the next run. The defect itself was one unbounded wait
in the control plane, and it is fixed there; what is here is the pair of
properties that would have made it a five-minute diagnosis instead of a session:
the log says which signal arrived and when the stop finished, and no single
component can hold the stop past `SHUTDOWN_SECONDS`. An engine that overruns
says which phase it was in and leaves anyway — the alternative is not a tidier
shutdown, it is the same SIGKILL with nothing written down.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from gpt_voicecoding.config import ConfigError, EngineConfig, default_config_path, load
from gpt_voicecoding.control_plane.ownership import SocketPathTooLong
from gpt_voicecoding.control_plane.server import AlreadyServing
from gpt_voicecoding.engine.composition import (
    DEFAULT_TICK_SECONDS,
    Engine,
)
from gpt_voicecoding.engine.logfile import own_the_log, strip_environment

#: What an exit code means. Anything but 0 is a start that did not happen, or a
#: run that ended badly; the menu-bar shell restarts on every one of them.
EXIT_OK = 0
EXIT_REFUSED = 2

#: The whole orderly shutdown's ceiling, and the outer guarantee behind every
#: bound inside it. Derived rather than chosen, from two directions that have to
#: meet:
#:
#: - **From above**, whoever stops this engine gives it a finite grace and then
#:   kills it — twenty seconds for the acceptance harness
#:   (`tests/acceptance/support.py`), twenty for launchd's default `ExitTimeOut`.
#:   Overrunning that grace is how #96 lost its `codex app-server`, so this has
#:   to leave real margin under it rather than sit at it.
#: - **From below**, the phases each carry their own bound and they add up:
#:   3s for the control plane (`control_plane/server.py`), then the adapters,
#:   of which the Codex one dominates at 1s + 1s to let the Sessions go
#:   (`codex_app_server/wire.py`, and they go concurrently) plus 5s for the
#:   app-server itself (`codex_app_server/process.py`) — about eleven seconds if
#:   every single one is hit at once, which nothing has ever done.
#:
#: Twelve is the smallest round number above the second and comfortably below
#: the first. A phase that overruns it is named and abandoned, because the
#: alternative is not a tidier shutdown — it is a SIGKILL with nothing written.
SHUTDOWN_SECONDS = 12.0

_log = logging.getLogger(__name__)


def parse(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="gpt-voicecoding-engine",
        description="Run the GPT-VoiceCoding engine in the foreground.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=default_config_path(),
        help="the engine's configuration file (default: %(default)s)",
    )
    return parser.parse_args(argv)


def adopt_the_log(
    config: EngineConfig,
    *,
    check_seconds: float | None = DEFAULT_TICK_SECONDS,
    redirect_standard_streams: bool = True,
) -> None:
    """Clean the environment, then take ownership of the log — in that order.

    In that order because the strip is worth a line in the log, and the log has
    to exist before anything can say so. A variable that vanishes silently is the
    same kind of surprise as the one that filled 98.1% of the reference
    implementation's log.

    `redirect_standard_streams` is False only for a test process, which would
    otherwise point the test runner's own stdout at a scratch file and take every
    later line of output with it — the same escape hatch, for the same reason,
    that `OwnedLogStream.adopt` carries. `check_seconds` is None only for a test
    that wants no thread running behind the log it just opened.
    """
    removed = strip_environment(os.environ, config.log.stripped_environment_prefixes)
    own_the_log(
        config.log.path,
        max_bytes=config.log.max_bytes,
        retained_files=config.log.retained_files,
        # The same interval the hub reads its ceilings on, and for the same
        # reason: it is already this engine's answer to how finely it reads the
        # clock, and inventing a second number would be inventing a second
        # answer. The log's own bound is configuration; how often it is *looked
        # at* is mechanism.
        check_seconds=check_seconds,
        redirect_standard_streams=redirect_standard_streams,
    )
    # The log's first line is the log describing its own bound. A reader who
    # finds a truncated history should be able to see, in the file itself, that
    # it was bounded on purpose and by how much.
    _log.info(
        "owning %s, at most %d bytes per generation and %d kept",
        config.log.path,
        config.log.max_bytes,
        config.log.retained_files,
    )
    if removed:
        # A warning rather than a note: this engine has just altered the
        # environment every process it spawns will inherit, and the operator who
        # set those variables is entitled to know it was undone.
        _log.warning("dropped inherited environment variables: %s", ", ".join(removed))


def main(
    argv: Sequence[str] | None = None,
    *,
    check_seconds: float | None = DEFAULT_TICK_SECONDS,
    redirect_standard_streams: bool = True,
) -> int:
    """Run the engine until it is told to stop. Never daemonises."""
    arguments = parse(argv)
    try:
        config = load(arguments.config)
    except ConfigError as refusal:
        print(f"the engine cannot start: {refusal}", file=sys.stderr)
        return EXIT_REFUSED

    adopt_the_log(
        config,
        check_seconds=check_seconds,
        redirect_standard_streams=redirect_standard_streams,
    )

    try:
        engine = Engine.assemble(config)
    except Exception as refusal:
        # Every phase before serving refuses the same way. `EngineAssemblyError`
        # was the only type caught here, and `built` re-raises only `TypeError`
        # as one — so an adapter factory raising its own settings error escaped,
        # which is the *first* thing a new install hits: the Telegram spoke
        # raises exactly that when the variable `token_env` names is not set.
        _log.error("the engine could not be assembled", exc_info=refusal)
        print(f"the engine cannot start: {_start_refusal_detail(refusal, config)}", file=sys.stderr)
        return EXIT_REFUSED

    try:
        asyncio.run(_serve(engine))
    except StartRefused as refused:
        # The whole traceback goes to the log, and the last line is a sentence.
        # Both, on purpose: an adapter's own refusal reads as one line, and a
        # `TypeError` inside somebody's `connect` is a bug whose only diagnostic
        # is the traceback — collapsing it would throw that away.
        _log.error("the engine could not start", exc_info=refused.cause)
        print(
            f"the engine cannot start: {_start_refusal_detail(refused.cause, config)}",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    except (AlreadyServing, SocketPathTooLong, OSError) as refusal:
        # Reached only while serving; a start that raises one of these arrives
        # above, wrapped.
        print(f"the engine cannot serve on {config.socket_path}: {refusal}", file=sys.stderr)
        return EXIT_REFUSED
    return EXIT_OK


def _start_refusal_detail(cause: BaseException, config: EngineConfig) -> str:
    """One sentence for a start that did not happen, in the failure's own words.

    The socket is named when the socket is the problem, because "already in use"
    without the path is a sentence with nowhere to go.
    """
    if isinstance(cause, AlreadyServing | SocketPathTooLong | OSError):
        return f"nothing can serve on {config.socket_path}: {cause}"
    return str(cause) or type(cause).__name__


class StartRefused(Exception):
    """The engine never came up. Carries what actually stopped it.

    A start failure and a serving failure are different sentences and were, until
    this existed, told apart only by exception *type* — so an adapter that raised
    anything but an `OSError` fell through to the interpreter and became an exit
    code of 1 with a traceback, in a runner that promises 2 with a reason. The
    shipped adapters all raise their own types.
    """

    def __init__(self, cause: BaseException) -> None:
        super().__init__(str(cause))
        self.cause = cause


async def _serve(engine: Engine) -> None:
    """Serve until a signal arrives, then shut down in order."""
    stopping = asyncio.Event()
    #: Which signal it was, for the log. A list rather than a `nonlocal` because
    #: the handler is a plain callback the loop calls, not a closure over a cell
    #: this function can rebind.
    arrived: list[signal.Signals] = []

    def stop(received: signal.Signals) -> None:
        arrived.append(received)
        stopping.set()

    loop = asyncio.get_running_loop()
    for received in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(received, stop, received)

    try:
        await engine.start()
    except Exception as refusal:
        # `start` has already closed whatever it opened, so there is nothing
        # here to clean up — only something to name. `CancelledError` is not an
        # `Exception` and is left to propagate, because a cancelled start is a
        # shutdown, not a refusal.
        raise StartRefused(refusal) from refusal
    try:
        await stopping.wait()
    finally:
        await _stopping(engine, arrived)


async def _stopping(engine: Engine, arrived: Sequence[signal.Signals]) -> None:
    """Shut the engine down, saying so, and never past `SHUTDOWN_SECONDS`.

    The cause is named because there are two — a signal, and a `serve` that was
    cancelled from inside this process — and an operator reading the log after
    the fact cannot otherwise tell "the user quit" from "something else stopped
    it".
    """
    _log.info("stopping on %s", arrived[0].name if arrived else "a cancelled serve")
    began = time.monotonic()
    try:
        async with asyncio.timeout(SHUTDOWN_SECONDS):
            await engine.aclose()
    except TimeoutError:
        # The last "stopping: …" line above this one is the phase that did not
        # finish. Leaving is the right answer: whoever stopped this engine is
        # holding a grace period, and overrunning it converts an unclean stop
        # into a SIGKILL, which is strictly worse — nothing further would run.
        _log.error(
            "the shutdown did not finish within %.0fs and is being abandoned; "
            "the line above this one names the phase that was still running",
            SHUTDOWN_SECONDS,
        )
        return
    _log.info("stopped in %.2fs", time.monotonic() - began)

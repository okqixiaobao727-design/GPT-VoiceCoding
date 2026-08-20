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

**The environment is cleaned once, here, before anything is spawned.** Every
adapter child inherits what this process holds, so ADR 0004's stripped prefixes
are applied at the one point all of them descend from rather than at each spawn
site. A Session launched into a long-lived tmux server inherits that server's
environment instead, which is the launcher adapter's own obligation, not one this
call can discharge for it.

Signals: SIGINT and SIGTERM both mean stop, and stopping is orderly — the loops
are cancelled and the socket file is removed, so the next start is not left
claiming its own debris.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from collections.abc import Sequence
from pathlib import Path

from gpt_voicecoding.config import ConfigError, EngineConfig, default_config_path, load
from gpt_voicecoding.control_plane.ownership import SocketPathTooLong
from gpt_voicecoding.control_plane.server import AlreadyServing
from gpt_voicecoding.engine.composition import (
    DEFAULT_TICK_SECONDS,
    Engine,
    EngineAssemblyError,
)
from gpt_voicecoding.engine.logfile import own_the_log, strip_environment

#: What an exit code means. Anything but 0 is a start that did not happen, or a
#: run that ended badly; the menu-bar shell restarts on every one of them.
EXIT_OK = 0
EXIT_REFUSED = 2

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
    except EngineAssemblyError as refusal:
        print(f"the engine cannot start: {refusal}", file=sys.stderr)
        return EXIT_REFUSED

    try:
        asyncio.run(_serve(engine))
    except (AlreadyServing, SocketPathTooLong, OSError) as refusal:
        # OSError covers both the socket and an adapter whose far side is not
        # there: a `connect` that raises is a start that did not happen, and the
        # engine has already closed whatever it opened before saying so.
        print(f"the engine cannot serve on {config.socket_path}: {refusal}", file=sys.stderr)
        return EXIT_REFUSED
    return EXIT_OK


async def _serve(engine: Engine) -> None:
    """Serve until a signal arrives, then shut down in order."""
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for received in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(received, stopping.set)

    await engine.start()
    try:
        await stopping.wait()
    finally:
        await engine.aclose()

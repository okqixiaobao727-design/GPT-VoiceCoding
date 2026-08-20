"""Starting the engine from a command line, and stopping it when asked.

Deliberately small: read the arguments, load the configuration, assemble, serve
until a signal says otherwise, and exit with a number that means something.

**A refusal to start is spoken on stderr, not hidden in a log.** ADR 0004 gives
the log to the engine itself, and adoption happens after argument parsing, so
output produced before that has nowhere to go. Everything here runs in that
window, which is exactly why the failures it reports — a missing configuration
file, a seam with nothing behind it, a socket path that cannot be bound — are
reported to the terminal that started it.

Signals: SIGINT and SIGTERM both mean stop, and stopping is orderly — the loops
are cancelled and the socket file is removed, so the next start is not left
claiming its own debris.
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from collections.abc import Sequence
from pathlib import Path

from gpt_voicecoding.config import ConfigError, default_config_path, load
from gpt_voicecoding.control_plane.ownership import SocketPathTooLong
from gpt_voicecoding.control_plane.server import AlreadyServing
from gpt_voicecoding.engine.composition import Engine, EngineAssemblyError

#: What an exit code means. Anything but 0 is a start that did not happen, or a
#: run that ended badly; the menu-bar shell restarts on every one of them.
EXIT_OK = 0
EXIT_REFUSED = 2


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


def main(argv: Sequence[str] | None = None) -> int:
    """Run the engine until it is told to stop. Never daemonises."""
    arguments = parse(argv)
    try:
        config = load(arguments.config)
        engine = Engine.assemble(config)
    except (ConfigError, EngineAssemblyError) as refusal:
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

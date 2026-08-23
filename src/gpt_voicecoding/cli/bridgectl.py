"""``bridgectl`` — a control-plane surface, and nothing more.

It dials the engine's socket, sends one request, prints what came back, and
exits. It holds **no policy and no state**: every question is answered by the
hub, every refusal is the hub's own words, and nothing is cached between runs —
a second copy of the truth is how the reference implementation's status line
came to report a configuration file instead of an engine.

There is no per-command argument parser here either. The command line is parsed
by `control_plane.commands`, the same parser the Companion Channel's `/`
grammar uses, so the two surfaces cannot drift into two command sets.

Three exits, and they mean different things: the engine answered (0), the engine
refused (1), or there was no engine to ask (2). A surface that collapsed the
last two would tell a user their switch does not exist when in fact nothing is
running.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

from gpt_voicecoding.config import ConfigError, default_config_path, load
from gpt_voicecoding.control_plane.client import (
    EngineSilent,
    EngineUnreachable,
    ask,
    timeout_for,
)
from gpt_voicecoding.control_plane.commands import USAGE, CommandError, build_request, render
from gpt_voicecoding.seams.control_plane import Action, Request

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_UNREACHABLE = 2


def parse(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="bridgectl",
        description="Talk to a running GPT-VoiceCoding engine.",
        epilog="commands:\n  " + "\n  ".join(USAGE[action] for action in Action),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", type=Path, default=None, help="the engine's configuration")
    parser.add_argument(
        "--socket", type=Path, default=None, help="the engine's socket, instead of reading config"
    )
    # No default here: which deadline applies depends on the action, which is not
    # known until the command line has been parsed. `None` means "not asked for",
    # so an operator's own number still outranks whatever the action would pick.
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="seconds to wait, instead of the deadline this action carries",
    )
    parser.add_argument("command", help="one of the commands listed below")
    parser.add_argument("arguments", nargs=argparse.REMAINDER, help="whatever it takes")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the ``bridgectl`` console script."""
    arguments = parse(argv)

    try:
        request = build_request(arguments.command, arguments.arguments)
    except CommandError as unreadable:
        print(str(unreadable), file=sys.stderr)
        return EXIT_UNREACHABLE

    try:
        socket_path = _socket_path(arguments)
    except ConfigError as refusal:
        print(f"{refusal}. Point at a running engine with --socket.", file=sys.stderr)
        return EXIT_UNREACHABLE

    timeout = arguments.timeout if arguments.timeout is not None else timeout_for(request.action)
    try:
        reply = asyncio.run(ask(request, path=socket_path, timeout=timeout))
    except EngineSilent as unanswered:
        print(_still_in_flight(unanswered, request), file=sys.stderr)
        return EXIT_UNREACHABLE
    except EngineUnreachable as down:
        print(str(down), file=sys.stderr)
        return EXIT_UNREACHABLE

    rendered = render(reply)
    if reply.ok:
        print(rendered)
        return EXIT_OK
    print(rendered, file=sys.stderr)
    return EXIT_REFUSED


def _still_in_flight(unanswered: EngineSilent, request: Request) -> str:
    """Say what a silent deadline actually means, and for a launch, what to do.

    The engine said nothing, so there is no engine speech to relay and none is
    invented here: the deadline is this surface's own, and so is the report of
    it. What is added for a launch is a fact about the hub's behaviour — a
    launch is held under its request id and a repeat of the identical request
    joins it rather than starting a second one — so the safe recovery is the
    one an operator is least likely to guess. The wrong guess is named too,
    because telling someone only the right move still leaves the plausible
    wrong one looking free.
    """
    if request.action is not Action.LAUNCH:
        return str(unanswered)
    request_id = request.payload.get("request_id")
    return (
        f"{unanswered}; the launch may still be in flight. Re-issue this exact "
        f"command with the same --request-id {request_id} to join it and hear how "
        "it ended — a fresh --request-id would start a second agent in the same "
        "workspace."
    )


def _socket_path(arguments: argparse.Namespace) -> Path:
    """Where the engine is. Told directly, or read from the same file it read."""
    if arguments.socket is not None:
        return Path(arguments.socket)
    return load(arguments.config or default_config_path()).socket_path

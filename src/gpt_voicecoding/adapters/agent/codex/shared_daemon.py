"""This engine's client of the shared Codex app-server daemon. Join-only.

**Why this exists at all.** #82 proved that the shared daemon is the only source
that knows a thread's id, its name and what it has been doing, and #83 installed
the login job that starts it — but nothing in this engine ever dialled it, so
every Codex row came off the process table and no thread could be read. Progress
on the Codex lane was `None` by construction (#76, advisor ruling Q1). This is
the dial, and only the dial.

**Join-only, and that is a rule rather than a scope note.** This product starts
a daemon the user's Sessions will join and never stops one they are attached to
(#83, ADR 0012): by the time this engine shuts down, the user's `codex` TUIs are
thin clients of that daemon, and closing it would end their Sessions. So nothing
here spawns, bootstraps or boots out anything — it opens a connection, keeps it,
and lets go of its own end.

**Where the socket is comes from the daemon, never from `CODEX_HOME`.**
`codex app-server daemon version` answers a document naming the socket it is
listening on, which is the one address that cannot go stale under a moved home
or an updated managed binary. Ported from #82's prototype
(`scripts/prototype_codex_daemon.py:51-68,442-454`), which is where this shape
was proved against a real daemon. `installation/codex_launch_agent.py` runs the
same command for a different question — whether the daemon a person just
installed is answering, as one sentence for a status run — and the two are
deliberately not one function: installation may import no part of the engine
(ADR 0012's one-way rule), and this is the engine.

**No version pin** (Simon's ruling on #67). A CLI and a daemon whose versions
disagree still get dialled; the disagreement rides out as a note on
`LaneDiscovery.degraded`, so the user can see the lane is running on a
disagreement instead of the lane simply going quiet.

**Locating is re-tried, not remembered.** The address is looked up only when
there is no live connection, so a healthy engine spawns no subprocess per tick;
an engine whose daemon is down probes once per discovery instead, which is the
only honest way to notice it came back.

**Against legacy** (ADR 0010, `CLAUDE.md`): **dropped, because gen 1 had no
shared daemon to join.** It drove a launched, wrapped, per-Session app-server it
owned — `legacy@1d32845:bridge/codex.py:1319-1347` opened a client per read
against a socket its own runtime had spawned — and #82 recorded that whole route
as dropped from porting. What survives from it is the *shape* of the read side:
locate, connect, ask, and never fall back to another source when this one cannot
answer.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from gpt_voicecoding.adapters.codex_app_server.process import AppServerError, attach
from gpt_voicecoding.adapters.codex_app_server.settings import CodexSettings
from gpt_voicecoding.adapters.codex_app_server.wire import AppServerConnection, WireError

_log = logging.getLogger(__name__)

#: What is asked of the `codex` binary to find the daemon. `version` because it
#: is the one daemon subcommand that only reports — `start` would make this the
#: owner of a lifecycle it is forbidden to own.
DAEMON_VERSION_ARGUMENTS: Final = ("app-server", "daemon", "version")

#: How long that one command gets before it is given up on. Measured at 139 ms
#: against a daemon that is up, so this is not a budget the ordinary case spends
#: — it is the bound on the case that has no ordinary one, a `codex` that never
#: answers. Without it, `communicate()` waits forever and the five-second
#: discovery cadence waits with it: `core/bridge.py:505` awaits `discover()` with
#: no deadline of its own, and it is one loop over the adapters, so one hung
#: subprocess here stops the **Claude** lane's roster too. The number is the
#: lane's other subprocess reader's (`codex/processes.py:138`), because it is the
#: same risk read off the same machine.
COMMAND_TIMEOUT_SECONDS: Final = 10.0

#: How much of an unreadable answer is quoted back in the reason. Long enough to
#: recognise what came out — a shell banner, an HTML error page, a stack trace's
#: first line — and short enough that a daemon answering megabytes cannot put
#: them into a roster reply with a 64 KB ceiling on it.
UNREADABLE_ANSWER_QUOTED_CHARS: Final = 120

SOCKET_PATH_FIELD: Final = "socketPath"
CLI_VERSION_FIELD: Final = "cliVersion"
APP_SERVER_VERSION_FIELD: Final = "appServerVersion"

#: How the command is run. Injected so a test never reaches the real machine —
#: the same rule `tests/conftest.py` holds the whole suite to after two drafts of
#: the installation boundary started a real daemon from a test run.
Runner = Callable[[list[str]], Awaitable[tuple[int, str]]]


@dataclass(frozen=True, slots=True)
class DaemonAddress:
    """Where the shared daemon listens, and what it said about itself."""

    socket_path: Path
    cli_version: str
    app_server_version: str

    @property
    def note(self) -> str:
        """Why rows read through this connection deserve a caveat, if they do.

        Both versions are checked for being *said* before they are compared: a
        document carrying neither field makes them both empty, and `"" == ""`
        would report a daemon that said nothing at all as one whose versions
        agree (`installation/codex_launch_agent.py:301-306`, the same trap).
        """
        if not self.cli_version or not self.app_server_version:
            return (
                f"the shared Codex daemon did not say its versions "
                f"({CLI_VERSION_FIELD}={self.cli_version!r}, "
                f"{APP_SERVER_VERSION_FIELD}={self.app_server_version!r})"
            )
        if self.cli_version != self.app_server_version:
            return (
                f"the Codex CLI is {self.cli_version!r} and the running app-server is "
                f"{self.app_server_version!r} — a Session started by this CLI will not "
                "join that daemon"
            )
        return ""


async def locate(executable: str, *, run: Runner | None = None) -> tuple[DaemonAddress | None, str]:
    """Where the shared daemon is, or the reason nothing could be found.

    Never raises. This runs inside a five-second discovery tick, and a lane that
    threw because a binary moved would take the roster down with it — the honest
    answer is a reason the rows can carry (`LaneDiscovery.degraded`, #74).
    """
    try:
        status, said = await (run or _run)([executable, *DAEMON_VERSION_ARGUMENTS])
    except TimeoutError:
        # Before `OSError`, and that ordering is the behaviour rather than a
        # style: `TimeoutError` **is** an `OSError`, so caught second a daemon
        # that hung would be reported as a `codex` that is not installed — the
        # one reason that sends somebody looking in the wrong place.
        return None, (
            f"{executable} did not answer where the shared Codex daemon is within "
            f"{COMMAND_TIMEOUT_SECONDS:.0f} seconds"
        )
    except OSError as unrunnable:
        return None, f"{executable} could not be run: {unrunnable}"
    if status != 0:
        # The last line, because a `codex` refusal is a short reason under a
        # longer "Error:" banner and the reason is the part worth carrying.
        reason = said.splitlines()[-1].strip() if said.strip() else "it gave no reason"
        return None, f"the shared Codex daemon is not answering: {reason}"
    try:
        reported: Any = json.loads(said)
    except json.JSONDecodeError:
        return None, (
            "the shared Codex daemon answered, and not with JSON: "
            f"{said[:UNREADABLE_ANSWER_QUOTED_CHARS]}"
        )
    if not isinstance(reported, dict):
        return None, "the shared Codex daemon answered with something that is not a document"
    socket_path = reported.get(SOCKET_PATH_FIELD)
    if not isinstance(socket_path, str) or not socket_path.strip():
        return None, (
            f"the shared Codex daemon answered without a {SOCKET_PATH_FIELD}, and this "
            "engine will not guess one"
        )
    return (
        DaemonAddress(
            socket_path=Path(socket_path.strip()),
            cli_version=_said(reported.get(CLI_VERSION_FIELD)),
            app_server_version=_said(reported.get(APP_SERVER_VERSION_FIELD)),
        ),
        "",
    )


class SharedDaemon:
    """One connection to the daemon somebody else owns, held for as long as it lives."""

    def __init__(
        self,
        *,
        settings: CodexSettings,
        version: str,
        locate: Callable[[str], Awaitable[tuple[DaemonAddress | None, str]]] = locate,
        attach: Callable[..., Awaitable[Any]] = attach,
    ) -> None:
        self._settings = settings
        self._version = version
        self._locate = locate
        self._attach = attach
        self._connection: AppServerConnection | None = None
        self._note = ""
        #: Held across the whole dial, because the dial is where the race is.
        #: The engine has two callers that arrive independently — the five-second
        #: discovery cadence and a control-plane `progress` ask — and the check
        #: for a live connection is separated from writing the new one by two
        #: awaits, which is room enough for both to find none and both to attach.
        self._dialling = asyncio.Lock()

    @property
    def note(self) -> str:
        """What the lane should say about rows read through this, if anything.

        Empty when there is nothing to say. It is deliberately not the same as
        "there is no connection": a daemon that answers with a mismatched version
        is joined *and* worth a caveat, and one that is simply absent is a
        caveat with no connection behind it.
        """
        return self._note

    async def client(self) -> AppServerConnection | None:
        """A live connection to the daemon, or `None` with `note` saying why not.

        A connection the far side dropped is not reused: the daemon can be
        restarted under a running engine — its own updater does exactly that —
        and an engine that kept a dead handle would report an empty roster for
        the rest of its life.

        **One dial at a time, and the answer is asked for twice.** A live
        connection is answered without taking the lock, which is the ordinary
        case and stays free; a caller that finds none waits, and then asks again
        — because what it was waiting on is very likely the dial that answers its
        own question. Without this, two callers arriving together both attached,
        the daemon held two clients of an engine that is meant to be one of them,
        and the loser was dropped with nothing left to close it.
        """
        held = self._connection
        if held is not None and held.is_open:
            return held

        async with self._dialling:
            held = self._connection
            if held is not None and held.is_open:
                return held
            self._connection = None

            address, reason = await self._locate(self._settings.executable)
            if address is None:
                self._note = reason
                return None
            try:
                connection = await self._attach(
                    address.socket_path, version=self._version, settings=self._settings
                )
            except (WireError, AppServerError, OSError) as unreachable:
                self._note = (
                    f"the shared Codex daemon at {address.socket_path} did not accept a "
                    f"connection: {unreachable}"
                )
                return None
            _log.info("joined the shared Codex daemon at %s", address.socket_path)
            self._connection = connection
            self._note = address.note
            return connection

    async def aclose(self) -> None:
        """Let go of this engine's end. The daemon and its Sessions carry on.

        Behind the same lock as the dial, so shutdown waits for a dial in flight
        rather than racing it: clearing the field while one was running would
        have that dial write its connection back afterwards, and this engine
        would walk away from a client it had just made.
        """
        async with self._dialling:
            connection, self._connection = self._connection, None
            self._note = ""
        if connection is not None:
            await connection.aclose()


def _said(value: Any) -> str:
    """One version field as a string, and an unsaid one as nothing."""
    return value.strip() if isinstance(value, str) else ""


async def _run(arguments: list[str]) -> tuple[int, str]:
    """Run one short command and take what it said, however it ended.

    `stderr` is folded into `stdout` because a `codex` that refuses writes its
    reason to one and its document to the other, and the caller wants whichever
    turned up.

    **The bound lives here, in the runner, rather than at the call site**, so a
    caller injecting a `Runner` of its own is the only thing that can be
    unbounded — and the only callers that do are tests. Raises `TimeoutError`,
    which `locate` turns into a reason like every other way this can fail.
    """
    process = await asyncio.create_subprocess_exec(
        *arguments,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        said, _ = await asyncio.wait_for(process.communicate(), COMMAND_TIMEOUT_SECONDS)
    except TimeoutError:
        # Killed rather than left, because the wait ending is not the command
        # ending: a `codex` abandoned here would go on holding whatever it was
        # stuck on, and the next tick would start another one beside it.
        process.kill()
        await process.wait()
        raise
    return process.returncode or 0, said.decode("utf-8", errors="replace")

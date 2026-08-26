"""Joining the shared Codex app-server daemon, and admitting when it cannot be.

#82 established that the shared daemon is the only route to a thread's own
truth, and #83 installed the login job that starts it. Nothing in this engine
ever dialled it: `CodexAgentAdapter._shared_daemon()` returned `None`
unconditionally, so every Codex row came from the process table and no thread
could be read at all. This is the join (advisor ruling on #76, Q1).

Join-only, and the tests say so: nothing here spawns a daemon, stops one, or
outlives one. By the time this engine shuts down the user's `codex` TUIs are
thin clients of that daemon, and a product that stopped it would end their
Sessions (ADR 0012, #83's written rule).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from gpt_voicecoding.adapters.agent.codex.shared_daemon import (
    DAEMON_VERSION_ARGUMENTS,
    DaemonAddress,
    SharedDaemon,
    locate,
)
from gpt_voicecoding.adapters.codex_app_server.settings import CodexSettings

SOCKET = Path("/tmp/app-server-control.sock")


def answering(document: object, *, status: int = 0) -> object:
    """A `codex app-server daemon version` that answers exactly this."""

    async def run(arguments: list[str]) -> tuple[int, str]:
        assert arguments[1:] == list(DAEMON_VERSION_ARGUMENTS)
        return status, document if isinstance(document, str) else json.dumps(document)

    return run


def running(**extra: object) -> dict[str, object]:
    return {
        "status": "running",
        "socketPath": str(SOCKET),
        "cliVersion": "0.149.1",
        "appServerVersion": "0.149.1",
        **extra,
    }


class TestFindingIt:
    """One lookup, ported from #82's prototype rather than invented again."""

    def test_the_socket_path_comes_from_the_daemon_itself(self) -> None:
        """Never derived from `CODEX_HOME`: the daemon is the one that knows."""
        address, reason = asyncio.run(locate("codex", run=answering(running())))

        assert address == DaemonAddress(
            socket_path=SOCKET, cli_version="0.149.1", app_server_version="0.149.1"
        )
        assert reason == ""

    def test_a_daemon_that_is_not_running_is_a_reason_not_an_exception(self) -> None:
        """The lane keeps working from the process table; it just says why (#74)."""
        address, reason = asyncio.run(
            locate("codex", run=answering("Error:\nno daemon is running", status=1))
        )

        assert address is None
        assert "no daemon is running" in reason

    def test_a_codex_that_is_not_there_is_the_same_kind_of_reason(self) -> None:
        """A missing binary must not raise out of a five-second discovery tick."""

        async def missing(arguments: list[str]) -> tuple[int, str]:
            raise FileNotFoundError(arguments[0])

        address, reason = asyncio.run(locate("codex", run=missing))

        assert address is None
        assert "codex" in reason

    def test_a_refusal_that_said_nothing_still_says_something(self) -> None:
        """A reason nobody can read is worse than the honest admission of none."""
        address, reason = asyncio.run(locate("codex", run=answering("", status=1)))

        assert address is None
        assert "gave no reason" in reason

    @pytest.mark.parametrize(
        "document",
        [
            "not json at all",
            ["a", "list"],
            {"status": "running"},
            {"status": "running", "socketPath": "   "},
        ],
        ids=["unparseable", "not-a-document", "no-socket-path", "blank-socket-path"],
    )
    def test_an_answer_without_a_usable_socket_names_nothing(self, document: object) -> None:
        """A shape this build cannot read is a reason, never a guessed path."""
        address, reason = asyncio.run(locate("codex", run=answering(document)))

        assert address is None
        assert reason


class TestTheVersionMismatch:
    """No pin (#67, Simon's ruling): a mismatch is reported, never refused."""

    def test_a_mismatch_is_something_the_rows_carry_rather_than_lose(self) -> None:
        address = DaemonAddress(
            socket_path=SOCKET, cli_version="0.148.0", app_server_version="0.149.1"
        )

        assert "0.148.0" in address.note
        assert "0.149.1" in address.note

    def test_matching_versions_have_nothing_to_say(self) -> None:
        assert (
            DaemonAddress(
                socket_path=SOCKET, cli_version="0.149.1", app_server_version="0.149.1"
            ).note
            == ""
        )

    def test_a_daemon_that_named_no_versions_is_not_read_as_agreeing(self) -> None:
        """`None == None` would report a daemon that said nothing as one that agrees."""
        assert DaemonAddress(socket_path=SOCKET, cli_version="", app_server_version="").note


class TestTheConnection:
    """Attached once, kept, and re-attached when the far side goes away."""

    def daemon(self, attached: list[Path], *, fails: Exception | None = None) -> SharedDaemon:
        class _Connection:
            def __init__(self) -> None:
                self.is_open = True
                self.closed = 0

            async def aclose(self) -> None:
                self.is_open = False
                self.closed += 1

        async def attach(path: Path, **_: object) -> object:
            attached.append(path)
            if fails is not None:
                raise fails
            return _Connection()

        return SharedDaemon(
            settings=CodexSettings(),
            version="test",
            locate=lambda executable: locate(executable, run=answering(running())),
            attach=attach,
        )

    def test_one_connection_serves_every_tick(self) -> None:
        """Dialling per tick would open a client per five seconds, forever."""
        attached: list[Path] = []
        daemon = self.daemon(attached)

        first = asyncio.run(daemon.client())
        second = asyncio.run(daemon.client())

        assert first is second
        assert attached == [SOCKET]

    def test_a_connection_that_went_away_is_dialled_again(self) -> None:
        """The daemon can be restarted under us; the next tick must find it."""
        attached: list[Path] = []
        daemon = self.daemon(attached)

        first = asyncio.run(daemon.client())
        first.is_open = False  # type: ignore[union-attr]
        second = asyncio.run(daemon.client())

        assert second is not first
        assert attached == [SOCKET, SOCKET]

    def test_a_daemon_that_refuses_the_dial_leaves_the_lane_working(self) -> None:
        """`None` plus a note: the rows come from the process table, as before."""
        attached: list[Path] = []
        daemon = self.daemon(attached, fails=OSError("connection refused"))

        assert asyncio.run(daemon.client()) is None
        assert "connection refused" in daemon.note

    def test_a_daemon_that_cannot_be_found_says_so_without_dialling(self) -> None:
        attached: list[Path] = []
        daemon = SharedDaemon(
            settings=CodexSettings(),
            version="test",
            locate=lambda executable: locate(
                executable, run=answering("Error:\nno daemon", status=1)
            ),
            attach=lambda path, **_: attached.append(path),  # type: ignore[misc,return-value]
        )

        assert asyncio.run(daemon.client()) is None
        assert attached == []
        assert "no daemon" in daemon.note

    def test_letting_go_closes_this_engine_s_client_and_nothing_else(self) -> None:
        """The daemon lives on: the user's TUIs are attached to it (#83's rule)."""
        attached: list[Path] = []
        daemon = self.daemon(attached)
        connection = asyncio.run(daemon.client())

        asyncio.run(daemon.aclose())

        assert connection.closed == 1  # type: ignore[union-attr]
        assert asyncio.run(daemon.client()) is not connection


class TestItNeverOwnsTheDaemon:
    """ADR 0012 and #83: this product starts no daemon and stops none it joined.

    Asserted on what the module *runs*, not on what its source says: a test that
    scanned the text would trip over the paragraph explaining the rule, and
    would still pass for a module that spelled the forbidden verb differently.
    """

    def test_the_only_daemon_subcommand_asked_for_is_the_one_that_reports(self) -> None:
        """`start` and `bootstrap` are lifecycle; `version` only tells."""
        assert DAEMON_VERSION_ARGUMENTS[-1] == "version"

    def test_nothing_else_is_ever_run(self) -> None:
        """One command, exactly as spelled — and it is the whole of what runs."""
        ran: list[list[str]] = []

        async def watched(arguments: list[str]) -> tuple[int, str]:
            ran.append(arguments)
            return 0, json.dumps(running())

        asyncio.run(locate("codex", run=watched))

        assert ran == [["codex", *DAEMON_VERSION_ARGUMENTS]]

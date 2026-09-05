"""The boot wait's reading of whether the shared Codex daemon holds this Session (#232).

Why the reading exists is `support.codex_daemon_membership`'s own docstring and is
not restated here. What is graded is the reading and the sentence it composes,
and because both are answers to one JSON-RPC call they are pinned against a faked
daemon: no real `codex` is run by this file, no socket is opened, and none is
needed.

The 2026-09-05 measurement this ticket rests on has one home, beside the pin block
in `support.py`, and is not copied here: a table restated in four files is a table
that will be edited in one of them.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import support

from gpt_voicecoding.adapters.agent.codex.shared_daemon import DaemonAddress

#: The thread the harness's own TUI wrote into its rollout.
MINE = "01998f4c-0d5a-7c31-9f2b-6a0c1e77aa10"

#: A thread the daemon holds that is somebody else's.
THEIRS = "01998f4c-0d5a-7c31-9f2b-6a0c1e77bb20"

SOCKET = Path("/tmp/codex-app-server-501/daemon.sock")

ADDRESS = DaemonAddress(socket_path=SOCKET, cli_version="0.153.0", app_server_version="0.149.1")

#: The flags run `20260904T202319Z` launched the Codex lane with.
FLAGS_WITH_THE_OVERRIDE = (
    "--sandbox",
    "workspace-write",
    "-m",
    "gpt-5.6-luna",
    "-c",
    'model_reasoning_effort="high"',
)


def locating(address: DaemonAddress | None, reason: str = ""):
    async def locate(executable: str) -> tuple[DaemonAddress | None, str]:
        assert executable, "the daemon is located through an executable, never a guessed path"
        return address, reason

    return locate


class Connection:
    """One `thread/loaded/list` answer, and a record of having been let go of."""

    def __init__(self, answer: Any = None, raises: Exception | None = None) -> None:
        self.answer = answer
        self.raises = raises
        self.asked: list[str] = []
        self.closed = False

    async def request(
        self, method: str, params: Any = None, *, timeout_seconds: float | None = None
    ) -> Any:
        self.asked.append(method)
        if self.raises is not None:
            raise self.raises
        return self.answer

    async def aclose(self) -> None:
        self.closed = True


def attaching(connection: Connection | None, raises: Exception | None = None):
    async def attach(socket_path: Path, **_: Any) -> Connection:
        if raises is not None:
            raise raises
        assert connection is not None
        return connection

    return attach


def membership(
    thread_id: str = MINE,
    *,
    address: DaemonAddress | None = ADDRESS,
    locate_reason: str = "",
    connection: Connection | None = None,
    attach_raises: Exception | None = None,
) -> support.DaemonMembership:
    return support.codex_daemon_membership(
        thread_id,
        executable="codex",
        locate=locating(address, locate_reason),
        attach=attaching(connection, attach_raises),
    )


class TestWhatTheDaemonWasAskedAndAnswered:
    def test_a_thread_the_daemon_holds_is_held(self) -> None:
        connection = Connection({"data": [THEIRS, MINE]})

        read = membership(connection=connection)

        assert read.held is True
        assert read.thread_id == MINE
        assert read.held_threads == (THEIRS, MINE)
        assert connection.asked == ["thread/loaded/list"]

    def test_a_thread_the_daemon_does_not_hold_is_not_held(self) -> None:
        """The `-c` case. The daemon answered, and this thread is not in the answer."""
        read = membership(connection=Connection({"data": [THEIRS]}))

        assert read.held is False
        assert read.held_threads == (THEIRS,)

    def test_the_daemon_is_let_go_of_however_the_read_ends(self) -> None:
        """This run joins a daemon somebody else owns and never keeps a client of it."""
        connection = Connection(raises=RuntimeError("the wire went away"))

        membership(connection=connection)

        assert connection.closed

    def test_the_address_the_reading_came_through_is_carried(self) -> None:
        read = membership(connection=Connection({"data": [MINE]}))

        assert str(SOCKET) in read.daemon
        assert "0.153.0" in read.daemon
        assert "0.149.1" in read.daemon


class TestWhatWasNotObservedIsNotClaimed:
    """`held` is a tri-state, and `None` is the whole reason it is one.

    The rule this module is held to is `codex/shared_daemon.py`'s: never claim
    anything about the daemon this build did not observe. A daemon that could not
    be found, could not be dialled, or answered a shape this build cannot read is
    not evidence that a thread is absent from it — and refusing the lane on one
    of those would blame the ticket's own cause for somebody else's outage.
    """

    def test_a_session_with_no_thread_id_yet_is_not_a_missing_thread(self) -> None:
        read = membership("")

        assert read.held is None
        assert read.reason

    def test_a_daemon_that_could_not_be_located_is_not_a_missing_thread(self) -> None:
        read = membership(address=None, locate_reason="codex could not be run: [Errno 2]")

        assert read.held is None
        assert "[Errno 2]" in read.reason

    def test_a_daemon_that_could_not_be_dialled_is_not_a_missing_thread(self) -> None:
        read = membership(attach_raises=OSError("no such socket"))

        assert read.held is None
        assert "no such socket" in read.reason

    def test_a_daemon_that_did_not_answer_is_not_a_missing_thread(self) -> None:
        read = membership(connection=Connection(raises=TimeoutError()))

        assert read.held is None
        assert "thread/loaded/list" in read.reason

    @pytest.mark.parametrize(
        "answer",
        [None, {"data": "01998f4c"}, {"threads": []}, ["01998f4c"]],
        ids=["nothing", "not-a-list", "no-data-key", "not-a-document"],
    )
    def test_an_answer_this_build_cannot_read_is_not_a_missing_thread(self, answer: Any) -> None:
        read = membership(connection=Connection(answer))

        assert read.held is None
        assert read.reason


class TestTheRefusalSaysWhatWasMeasuredAndWhatWasLaunched:
    def test_only_an_observed_absence_refuses(self) -> None:
        for read in (
            membership(connection=Connection({"data": [MINE]})),
            membership(""),
            membership(address=None, locate_reason="nothing answered"),
        ):
            assert read.refusal(FLAGS_WITH_THE_OVERRIDE) is None

    def test_the_refusal_names_the_thread_the_flags_and_the_override(self) -> None:
        """#232: nine SKIPPED steps behind a `roster` red said none of this."""
        refusal = membership(connection=Connection({"data": [THEIRS]})).refusal(
            FLAGS_WITH_THE_OVERRIDE
        )

        assert refusal is not None
        assert MINE in refusal
        assert 'model_reasoning_effort="high"' in refusal
        assert "-c" in refusal
        assert str(SOCKET) in refusal

    def test_the_refusal_of_a_lane_carrying_no_override_does_not_blame_one(self) -> None:
        """The sentence is a reading, not a stock explanation of the ticket's own cause."""
        refusal = membership(connection=Connection({"data": [THEIRS]})).refusal(
            ("--sandbox", "workspace-write")
        )

        assert refusal is not None
        assert "run its own core" not in refusal
        assert "unknown to this run" in refusal


class TestTheReadIsTheProductsOwnQuestionAskedTheProductsOwnWay:
    def test_the_method_is_the_adapters_own_constant(self) -> None:
        """Two spellings of `thread/loaded/list` is one spelling too many."""
        from gpt_voicecoding.adapters.agent.codex import discovery

        assert support.DAEMON_ROSTER_METHOD == discovery.ROSTER_METHOD

    def test_nothing_here_opens_a_second_route_to_the_daemon(self) -> None:
        """The dial is `shared_daemon.locate` and `codex_app_server.attach`, both defaulted."""
        import inspect

        from gpt_voicecoding.adapters.agent.codex import shared_daemon
        from gpt_voicecoding.adapters.codex_app_server import process

        defaults = inspect.signature(support.codex_daemon_membership).parameters

        assert defaults["locate"].default is shared_daemon.locate
        assert defaults["attach"].default is process.attach


class TestTheJournalNamesTheFact:
    """#232's third criterion, pinned on the record rather than on the walk.

    `settle_daemon_membership` writes the reading with `asdict`, so what the
    journal carries is this dataclass's own fields. Both halves are worth a test:
    that they survive `json.dumps(..., default=str)` — a tuple and a `None` are
    the two shapes that could have needed a custom encoder — and that the fields
    the criterion asks for are among them.
    """

    def test_the_reading_lands_in_the_journal_as_json(self, tmp_path: Path) -> None:
        from dataclasses import asdict

        journal = support.Journal(tmp_path / "journal.jsonl")
        read = membership(connection=Connection({"data": [THEIRS]}))

        journal(
            "daemon.membership", lane="codex", flags=list(FLAGS_WITH_THE_OVERRIDE), **asdict(read)
        )
        (written,) = journal.read()

        assert written["event"] == "daemon.membership"
        assert written["thread_id"] == MINE
        assert written["held"] is False
        assert written["held_threads"] == [THEIRS]
        assert str(SOCKET) in written["daemon"]
        assert "-c" in written["flags"]

    def test_an_unread_daemon_is_recorded_rather_than_left_out(self, tmp_path: Path) -> None:
        """The path that does *not* refuse is the one a journal has to carry."""
        from dataclasses import asdict

        journal = support.Journal(tmp_path / "journal.jsonl")
        read = membership(address=None, locate_reason="codex is not installed")

        journal("daemon.membership", lane="codex", **asdict(read))
        (written,) = journal.read()

        assert written["held"] is None
        assert written["reason"] == "codex is not installed"


def test_the_reading_never_leaves_a_loop_of_its_own_running() -> None:
    """`asyncio.run` inside a synchronous harness, and nothing outliving the call."""
    membership(connection=Connection({"data": [MINE]}))

    with pytest.raises(RuntimeError):
        asyncio.get_running_loop()

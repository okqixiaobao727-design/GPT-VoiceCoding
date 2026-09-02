"""The address the engine publishes so a hand-started Session's hook can find it.

ADR 0011 decided this and #86 built it. It is the difference between an installed
`PermissionRequest` hook that reaches the engine and one that exits before it
opens a socket: a Session the user started has no bootstrap variable, because
there was no launch wrapper to set one.

Every absence answers the same way — `None`, which the hook turns into printing
nothing and leaving the dialog with the human.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from gpt_voicecoding.adapters.agent.claude.bootstrap import (
    CHANNEL_CONFIG_VARIABLE,
    CLAIM_LOCK_SUFFIX,
    AddressHeld,
    approval_socket_path_in,
    dial_timeout_in,
    publish_address,
    withdraw_address,
)
from gpt_voicecoding.adapters.agent.claude.settings import ClaudeSettings
from gpt_voicecoding.locations import address_path
from sockets import listening


def test_a_hook_with_no_variable_reads_the_published_address(tmp_path: Path) -> None:
    socket = tmp_path / "approvals.sock"
    publish_address(socket, ClaudeSettings(), base_dir=tmp_path)

    assert approval_socket_path_in({}, base_dir=tmp_path) == socket


def test_a_launch_that_carried_an_address_still_wins(tmp_path: Path) -> None:
    """The variable is the direct answer; the file is only the fallback."""
    publish_address(tmp_path / "published.sock", ClaudeSettings(), base_dir=tmp_path)
    told = {
        CHANNEL_CONFIG_VARIABLE: json.dumps(
            {"approvalSocketPath": str(tmp_path / "handed-over.sock")}
        )
    }

    assert approval_socket_path_in(told, base_dir=tmp_path) == tmp_path / "handed-over.sock"


def test_no_engine_published_anything(tmp_path: Path) -> None:
    assert approval_socket_path_in({}, base_dir=tmp_path) is None


def test_a_withdrawn_address_is_gone(tmp_path: Path) -> None:
    """A stale address costs every dialog in the directory a full dial timeout."""
    socket_path = tmp_path / "approvals.sock"
    publish_address(socket_path, ClaudeSettings(), base_dir=tmp_path)
    withdraw_address(socket_path, base_dir=tmp_path)

    assert approval_socket_path_in({}, base_dir=tmp_path) is None


def test_withdrawing_an_address_nobody_published_is_not_an_error(tmp_path: Path) -> None:
    withdraw_address(tmp_path / "approvals.sock", base_dir=tmp_path)


def test_an_unreadable_address_is_silence(tmp_path: Path) -> None:
    published = address_path(tmp_path)
    published.parent.mkdir(parents=True, exist_ok=True)
    published.write_text("half a fi", encoding="utf-8")

    assert approval_socket_path_in({}, base_dir=tmp_path) is None


def test_publishing_twice_replaces_rather_than_appends(tmp_path: Path) -> None:
    publish_address(tmp_path / "first.sock", ClaudeSettings(), base_dir=tmp_path)
    publish_address(tmp_path / "second.sock", ClaudeSettings(), base_dir=tmp_path)

    assert approval_socket_path_in({}, base_dir=tmp_path) == tmp_path / "second.sock"


def test_the_dial_timeout_travels_with_the_address(tmp_path: Path) -> None:
    settings = ClaudeSettings()
    publish_address(tmp_path / "approvals.sock", settings, base_dir=tmp_path)

    assert dial_timeout_in({}, base_dir=tmp_path) == settings.request_timeout_seconds


# -- the address is a claim, not a broadcast (#202) ------------------------
#
# Two engines on one machine — two acceptance lanes, or an acceptance engine
# beside the installed app — both publish here. Publishing used to overwrite and
# withdrawing used to unlink, both unconditionally, so the last engine to start
# owned every permission dialog on the machine and the first to stop took the
# address away from the other. The rule is now the one legacy already applied to
# its control socket (`bridge/daemon.py:711` `_claim_socket_path`, reference
# state `1d32845`): take over a socket file nobody answers, never displace a live
# one.


def test_an_address_whose_socket_answers_is_not_overwritten(
    tmp_path: Path, socket_root: Path
) -> None:
    """The first live engine keeps the route, and the second is refused by name."""
    with listening(socket_root / "held.sock") as holder:
        publish_address(holder, ClaudeSettings(), base_dir=tmp_path)
        before = address_path(tmp_path).read_bytes()

        with pytest.raises(AddressHeld) as refused:
            publish_address(socket_root / "mine.sock", ClaudeSettings(), base_dir=tmp_path)

        assert refused.value.holder == holder
        assert str(holder) in str(refused.value)
        assert address_path(tmp_path).read_bytes() == before


def test_an_address_nobody_answers_is_debris_and_is_taken_over(
    tmp_path: Path, socket_root: Path
) -> None:
    """A stopped engine's leftover address costs a dialog a dial into nothing."""
    publish_address(socket_root / "gone.sock", ClaudeSettings(), base_dir=tmp_path)
    mine = socket_root / "mine.sock"

    publish_address(mine, ClaudeSettings(), base_dir=tmp_path)

    assert approval_socket_path_in({}, base_dir=tmp_path) == mine


def test_an_engine_republishing_its_own_live_address_is_not_refused(
    tmp_path: Path, socket_root: Path
) -> None:
    """A live socket that is this engine's own is not another engine holding it."""
    with listening(socket_root / "mine.sock") as mine:
        publish_address(mine, ClaudeSettings(), base_dir=tmp_path)
        publish_address(mine, ClaudeSettings(), base_dir=tmp_path)

    assert approval_socket_path_in({}, base_dir=tmp_path) == mine


def test_withdrawing_leaves_another_engines_address_in_place(
    tmp_path: Path, socket_root: Path
) -> None:
    """The engine that stops first must not take the route from the one still up."""
    theirs = socket_root / "theirs.sock"
    publish_address(theirs, ClaudeSettings(), base_dir=tmp_path)

    withdraw_address(socket_root / "mine.sock", base_dir=tmp_path)

    assert approval_socket_path_in({}, base_dir=tmp_path) == theirs


def test_two_publishers_do_not_collide_on_the_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Measured on acceptance run `20260902T012313Z`: one fixed temporary name
    beside the address, and the engine that lost the race logged
    ``[Errno 2] No such file or directory: '….address.json.writing'``."""
    written: list[str] = []
    replace = os.replace

    def record(source: object, destination: object) -> None:
        written.append(str(source))
        replace(source, destination)  # type: ignore[arg-type]

    monkeypatch.setattr("gpt_voicecoding.adapters.agent.claude.bootstrap.os.replace", record)
    publish_address(tmp_path / "first.sock", ClaudeSettings(), base_dir=tmp_path)
    publish_address(tmp_path / "second.sock", ClaudeSettings(), base_dir=tmp_path)

    assert len(written) == 2
    assert len(set(written)) == 2


def test_a_refused_publish_leaves_no_temporary_file_behind(
    tmp_path: Path, socket_root: Path
) -> None:
    with listening(socket_root / "held.sock") as holder:
        publish_address(holder, ClaudeSettings(), base_dir=tmp_path)

        with pytest.raises(AddressHeld):
            publish_address(socket_root / "mine.sock", ClaudeSettings(), base_dir=tmp_path)

    address = address_path(tmp_path)
    directory = address.parent
    left = {entry.name for entry in directory.iterdir()}
    assert left == {address.name, f".{address.name}{CLAIM_LOCK_SUFFIX}"}, (
        "the address and the claim lock, and no half-written temporary"
    )


def test_only_one_of_several_engines_publishing_at_once_wins(
    tmp_path: Path, socket_root: Path
) -> None:
    """Probing and writing are two syscalls, and the claim is the pair.

    Without a lock around them, engines that all read "nobody is here" all write,
    and the last one still wins — which is the defect #202 opened with, surviving
    the fix that only reordered it. Each engine here binds a real socket first,
    so every loser is refused by a holder that is genuinely answering.
    """
    engines = 8
    ready = threading.Barrier(engines)
    #: Nobody lets go of its socket until every engine has had its turn. An
    #: engine that closed early would leave a *dead* socket behind, and the
    #: engines still to come would rightly read that as debris and take it over —
    #: the correct behaviour, and not the one under test here.
    finished = threading.Barrier(engines)
    outcomes: list[str] = []
    guard = threading.Lock()

    def engine(index: int) -> None:
        with listening(socket_root / f"engine-{index}.sock") as own:
            ready.wait()
            try:
                publish_address(own, ClaudeSettings(), base_dir=tmp_path)
            except AddressHeld:
                outcome = "refused"
            else:
                outcome = str(own)
            with guard:
                outcomes.append(outcome)
            finished.wait()

    threads = [threading.Thread(target=engine, args=(index,)) for index in range(engines)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    winners = [outcome for outcome in outcomes if outcome != "refused"]
    assert len(winners) == 1, f"exactly one engine holds the route, got {winners}"


def test_an_engine_that_lost_the_address_to_a_takeover_does_not_withdraw_it(
    tmp_path: Path, socket_root: Path
) -> None:
    """Reading who owns it and unlinking are two syscalls too.

    This is that pair written out in order: an engine publishes, stops answering,
    a second engine takes the debris over, and only then does the first one get
    to its own `aclose`. The address it finds is no longer its own, and an engine
    that unlinked it would take the route from the engine that is up.
    """
    mine = socket_root / "mine.sock"
    publish_address(mine, ClaudeSettings(), base_dir=tmp_path)
    theirs = socket_root / "theirs.sock"
    publish_address(theirs, ClaudeSettings(), base_dir=tmp_path)

    withdraw_address(mine, base_dir=tmp_path)

    assert approval_socket_path_in({}, base_dir=tmp_path) == theirs

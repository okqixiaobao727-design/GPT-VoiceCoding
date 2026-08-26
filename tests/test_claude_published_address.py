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
from pathlib import Path

from gpt_voicecoding.adapters.agent.claude.bootstrap import (
    CHANNEL_CONFIG_VARIABLE,
    approval_socket_path_in,
    dial_timeout_in,
    publish_address,
    withdraw_address,
)
from gpt_voicecoding.adapters.agent.claude.settings import ClaudeSettings
from gpt_voicecoding.locations import address_path


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
    publish_address(tmp_path / "approvals.sock", ClaudeSettings(), base_dir=tmp_path)
    withdraw_address(tmp_path)

    assert approval_socket_path_in({}, base_dir=tmp_path) is None


def test_withdrawing_an_address_nobody_published_is_not_an_error(tmp_path: Path) -> None:
    withdraw_address(tmp_path)


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

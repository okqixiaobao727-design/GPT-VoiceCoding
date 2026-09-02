"""What the Claude adapter does when another engine already holds the address.

#202: the approval address is one file per user per machine, and the hook is a
process Claude Code starts with no configuration, so it can only ever read that
one path. Two engines on this machine — two acceptance lanes, or an acceptance
engine beside the installed app — therefore cannot both own the Claude approval
route, and the rule is *first live engine wins*.

The refused engine is not a failed engine. It loses the Approval Relay for the
run, which is the same degraded start the adapter already takes when its approval
socket will not bind, and it says so twice: in the log, and in the loaded-report
ADR 0003 makes the authority on what an engine actually loaded.

Every test here runs under a `base_dir` of its own. The real
`~/Library/Application Support/GPT-VoiceCoding/engine/address.json` belongs to
whatever engine the developer has running, and a test that wrote it would be
taking his route away — which is the very defect this file pins.
"""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import pytest

from fakes import PROGRESS_CAPTURE
from gpt_voicecoding.adapters.agent.claude import bootstrap
from gpt_voicecoding.adapters.agent.claude.adapter import ClaudeAgentAdapter
from gpt_voicecoding.adapters.agent.claude.bootstrap import approval_socket_path_in
from gpt_voicecoding.adapters.agent.claude.settings import ClaudeSettings


@pytest.fixture
def published(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The published address, moved off this machine's real one for the test."""
    path = tmp_path / "engine" / "address.json"
    monkeypatch.setattr(bootstrap, "address_path", lambda base_dir=None: path)
    return path


def adapter_for(socket_root: Path, claude_config_directory: Path) -> ClaudeAgentAdapter:
    return ClaudeAgentAdapter(
        progress_capture=PROGRESS_CAPTURE,
        settings=ClaudeSettings(
            socket_directory=socket_root,
            request_timeout_seconds=2.0,
            # Under the config directory, or `verify` stops at its first gate and
            # never reaches the roster the refusal is about.
            registry_directory=claude_config_directory / "sessions",
        ),
        claude_config_directory=claude_config_directory,
    )


def a_peer_engine_holding(published: Path, socket_root: Path) -> socket.socket:
    """Another engine, reduced to the two things that make it a holder: a socket
    somebody answers, and its address in the file."""
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_root / "peer.sock"))
    server.listen(1)
    bootstrap.publish_address(socket_root / "peer.sock", ClaudeSettings())
    assert published.exists()
    return server


def test_a_refused_engine_still_connects_and_names_the_holder(
    published: Path, socket_root: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Losing one route must never cost the two that still work."""
    peer = a_peer_engine_holding(published, socket_root)
    before = published.read_bytes()
    adapter = adapter_for(socket_root, tmp_path / "claude")

    async def scenario() -> None:
        with caplog.at_level("WARNING"):
            await adapter.connect()
        await adapter.aclose()

    try:
        asyncio.run(scenario())
    finally:
        peer.close()

    assert published.read_bytes() == before, "the holder's address is left byte-for-byte"
    assert str(socket_root / "peer.sock") in caplog.text
    assert approval_socket_path_in({}) == socket_root / "peer.sock"


def test_a_refused_engine_reports_the_refusal_in_its_loaded_report(
    published: Path, socket_root: Path, tmp_path: Path
) -> None:
    """ADR 0003: a liveness check reads what the engine actually loaded.

    And the refusal is *added* to that reading, never substituted for it. These
    hooks are not installed, which is the operator's first problem and stays the
    headline; an engine that answered "another engine has the address" and
    nothing else would have hidden it.
    """
    peer = a_peer_engine_holding(published, socket_root)
    config_directory = tmp_path / "claude"
    adapter = adapter_for(socket_root, config_directory)

    async def scenario() -> tuple[str, str]:
        await adapter.connect()
        before = await adapter.verify()
        await adapter.aclose()
        return before.detail, before.outcome.value

    try:
        detail, outcome = asyncio.run(scenario())
    finally:
        peer.close()

    assert outcome == "fail"
    assert str(socket_root / "peer.sock") in detail, "the report names the holder"
    assert str(config_directory) in detail, (
        "the not-installed reason survives the refusal rather than being replaced by it"
    )


def test_an_unheld_address_is_claimed_and_handed_back(
    published: Path, socket_root: Path, tmp_path: Path
) -> None:
    adapter = adapter_for(socket_root, tmp_path / "claude")

    async def scenario() -> tuple[Path | None, bool]:
        await adapter.connect()
        claimed = approval_socket_path_in({})
        await adapter.aclose()
        return claimed, published.exists()

    claimed, still_there = asyncio.run(scenario())

    assert claimed == adapter.approval_socket_path()
    assert not still_there, "the engine that claimed it is the engine that takes it back"


def test_a_refused_engine_does_not_withdraw_the_holders_address(
    published: Path, socket_root: Path, tmp_path: Path
) -> None:
    """The defect in the other direction: the first engine to stop used to
    unlink whatever was there, taking the route from an engine still up."""
    peer = a_peer_engine_holding(published, socket_root)
    adapter = adapter_for(socket_root, tmp_path / "claude")

    async def scenario() -> None:
        await adapter.connect()
        await adapter.aclose()

    try:
        asyncio.run(scenario())
    finally:
        peer.close()

    assert approval_socket_path_in({}) == socket_root / "peer.sock"


def test_a_refused_engine_with_an_empty_roster_does_not_report_pass(
    published: Path, socket_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The false green ADR 0003 exists to prevent.

    Both Claude routes read the one published address — `approval_hook.py:208`
    and `registration.py:149` — so an engine refused the address is an engine no
    Session can register with. Its roster is then empty *because* it lost the
    address, and `verify`'s empty-roster branch would call that PASS: "no Claude
    Session is registered, so there is no inbox to reach". A guard that says
    nothing while the route is dead is the day-long outage in ADR 0003's header.
    """
    from gpt_voicecoding.installation import claude_hooks

    peer = a_peer_engine_holding(published, socket_root)
    config_directory = tmp_path / "claude"
    # Installed hooks, so the report gets past its first gate and reaches the
    # roster branch this test is about.
    monkeypatch.setattr(
        claude_hooks,
        "reach",
        lambda directory, base_dir=None: claude_hooks.Reach(
            installed=True, note="hooks are installed"
        ),
    )
    adapter = adapter_for(socket_root, config_directory)

    async def scenario() -> tuple[str, str]:
        await adapter.connect()
        result = await adapter.verify()
        await adapter.aclose()
        return result.outcome.value, result.detail

    try:
        outcome, detail = asyncio.run(scenario())
    finally:
        peer.close()

    assert outcome == "fail", "an engine nothing can register with is not passing"
    assert str(socket_root / "peer.sock") in detail, "the report names the holder"
    assert "can register" in detail, (
        "the report says why the roster is empty, so this is the refusal's branch "
        "rather than an earlier gate failing for its own reason"
    )

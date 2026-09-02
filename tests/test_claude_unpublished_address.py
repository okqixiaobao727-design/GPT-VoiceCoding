"""What `verify` reports when this engine published no approval address at all.

#204, generalising #202. `connect` has three ways to end without a published
address: the approval listener will not bind (`ApprovalError`), a peer engine
already holds the address (`AddressHeld`, #202), or the address file cannot be
written (`OSError`). Both Claude routes read that one published address — the
`PermissionRequest` hook (`approval_hook.py`) and the `SessionStart` registration
hook (`registration.py`), ADR 0019 — so *any* of the three leaves an engine no
Session can register with, whose roster is then empty **because** of the failure.

`verify`'s empty-roster branch would call that PASS: "no Claude Session is
registered, so there is no inbox to reach". That is the guard that says nothing
while the route is dead, which is what ADR 0003 exists to prevent. The peer cause
is pinned in `test_claude_address_claim.py`; the other two are pinned here.

Every test runs under a `base_dir` of its own: the real
`~/Library/Application Support/GPT-VoiceCoding/engine/address.json` belongs to
whatever engine the developer has running.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from fakes import PROGRESS_CAPTURE
from gpt_voicecoding.adapters.agent.claude import adapter as adapter_module
from gpt_voicecoding.adapters.agent.claude import bootstrap
from gpt_voicecoding.adapters.agent.claude.adapter import ClaudeAgentAdapter
from gpt_voicecoding.adapters.agent.claude.approval import ApprovalError, ApprovalListener
from gpt_voicecoding.adapters.agent.claude.settings import ClaudeSettings
from gpt_voicecoding.installation import claude_hooks

BIND_REFUSED = "/nowhere/approval.sock is 264 bytes, and a Unix socket path may not exceed 103"
UNWRITABLE = "[Errno 13] Permission denied: 'address.json'"


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
            # never reaches the roster this file is about.
            registry_directory=claude_config_directory / "sessions",
        ),
        claude_config_directory=claude_config_directory,
    )


def hooks_are_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Past the first gate, so the report reaches the empty-roster branch."""
    monkeypatch.setattr(
        claude_hooks,
        "reach",
        lambda directory, base_dir=None: claude_hooks.Reach(
            installed=True, note="hooks are installed"
        ),
    )


def a_socket_that_will_not_bind(monkeypatch: pytest.MonkeyPatch) -> None:
    async def refuse(self: ApprovalListener) -> None:
        raise ApprovalError(BIND_REFUSED)

    monkeypatch.setattr(ApprovalListener, "start", refuse)


def verify_after_connect(adapter: ClaudeAgentAdapter) -> tuple[str, str]:
    async def scenario() -> tuple[str, str]:
        await adapter.connect()
        result = await adapter.verify()
        await adapter.aclose()
        return result.outcome.value, result.detail

    return asyncio.run(scenario())


def test_a_bind_failure_with_an_empty_roster_does_not_report_pass(
    published: Path, socket_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An engine whose approval socket would not bind published no address, so no
    Session can register with it — and its empty roster is that failure, not a
    quiet machine."""
    a_socket_that_will_not_bind(monkeypatch)
    hooks_are_installed(monkeypatch)
    adapter = adapter_for(socket_root, tmp_path / "claude")

    outcome, detail = verify_after_connect(adapter)

    assert outcome == "fail", "an engine nothing can register with is not passing"
    assert BIND_REFUSED in detail, "the report names the bind error verbatim"
    assert "can register" in detail, "the report says why the roster is empty"
    assert not published.exists(), "a socket that never bound publishes no address"


def test_an_unwritable_address_with_an_empty_roster_does_not_report_pass(
    published: Path, socket_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The third cause: the socket bound, but the claim could not be written."""

    def unwritable(*args: object, **kwargs: object) -> Path:
        raise OSError(UNWRITABLE)

    monkeypatch.setattr(adapter_module, "publish_address", unwritable)
    hooks_are_installed(monkeypatch)
    adapter = adapter_for(socket_root, tmp_path / "claude")

    outcome, detail = verify_after_connect(adapter)

    assert outcome == "fail"
    assert UNWRITABLE in detail, "the report names the write error verbatim"
    assert str(published) in detail, "and the address file it was about"
    assert "can register" in detail


def test_a_bind_failure_does_not_replace_a_missing_hook_block(
    published: Path, socket_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The substitution is the empty-roster PASS and nothing else (ADR 0019).

    These hooks are not installed, which is the operator's first problem and
    stays the headline; the bind failure is prefixed to it, never put in its
    place.
    """
    a_socket_that_will_not_bind(monkeypatch)
    config_directory = tmp_path / "claude"
    adapter = adapter_for(socket_root, config_directory)

    outcome, detail = verify_after_connect(adapter)

    assert outcome == "fail"
    assert BIND_REFUSED in detail, "the bind cause is named"
    assert str(config_directory) in detail, (
        "the not-installed reason survives the bind failure rather than being replaced by it"
    )
    assert "can register" not in detail, (
        "this is the missing-hooks answer with a prefix, not the substituted one"
    )

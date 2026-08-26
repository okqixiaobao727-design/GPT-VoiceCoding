"""The `SessionStart` hook, and the fact it is the sole source of exactly one field.

Two things are asserted, and the second is the one that matters. The hook does
what it is for: it carries `transcript_path`, the Session's inbox socket and its
token to the engine, over the **same** socket the approval hook dials — one
ingress, because ADR 0011 publishes one address.

And it does nothing else. A user-scope hook fires for every Session in the
config directory, so a Session no engine is holding must cost the process it
started and nothing more: no socket, no writes, nothing on stdout — where
Claude Code would read it as context to add to the user's own conversation.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from gpt_voicecoding.adapters.agent.claude import registration
from gpt_voicecoding.adapters.agent.claude.adapter import ClaudeAgentAdapter
from gpt_voicecoding.adapters.agent.claude.approval import REGISTRATION_TYPE, TYPE_FIELD
from gpt_voicecoding.adapters.agent.claude.bootstrap import publish_address
from gpt_voicecoding.adapters.agent.claude.settings import ClaudeSettings
from gpt_voicecoding.locations import address_path

SESSION_ID = "d3a776ae-3b60-437d-bc70-ba57a2b280c6"

#: A `SessionStart` payload, in the shape Claude Code sends one.
PAYLOAD = {
    "session_id": SESSION_ID,
    "cwd": "/tmp/workspace-claude",
    "transcript_path": "/Users/simon/.claude/projects/-tmp-workspace-claude/x.jsonl",
    "hook_event_name": "SessionStart",
}

ENVIRONMENT = {
    registration.MESSAGING_SOCKET_VARIABLE: "/tmp/claude-inbox.sock",
    registration.MESSAGING_TOKEN_VARIABLE: "a-token",
}


class TestTheLineItSends:
    def test_it_carries_the_field_this_hook_exists_for(self) -> None:
        """`transcript_path`: not in the roster, and not derivable (#71)."""
        message = registration.registration_for(PAYLOAD, ENVIRONMENT)
        assert message is not None
        assert message["transcript_path"] == PAYLOAD["transcript_path"]

    def test_it_carries_the_sessions_own_inbox_socket_and_token(self) -> None:
        message = registration.registration_for(PAYLOAD, ENVIRONMENT)
        assert message is not None
        assert message["messaging_socket"] == "/tmp/claude-inbox.sock"
        assert message["messaging_token"] == "a-token"

    def test_it_is_typed_so_the_one_ingress_can_tell_it_from_a_dialog(self) -> None:
        message = registration.registration_for(PAYLOAD, ENVIRONMENT)
        assert message is not None
        assert message[TYPE_FIELD] == REGISTRATION_TYPE

    def test_a_payload_with_no_session_id_names_nobody_and_is_not_sent(self) -> None:
        assert registration.registration_for({"cwd": "/tmp"}, ENVIRONMENT) is None

    def test_a_build_that_exports_no_messaging_variables_still_registers(self) -> None:
        """The transcript path alone is worth the hook; absent is not an error."""
        message = registration.registration_for(PAYLOAD, {})
        assert message is not None
        assert message["messaging_socket"] is None
        assert message["transcript_path"] == PAYLOAD["transcript_path"]


class TestWhenNoEngineIsHoldingThisSession:
    def test_it_opens_no_socket_and_does_nothing(self, tmp_path: Path) -> None:
        """The first gate, and the reason the cost to an unheld Session is ~33 ms."""
        assert registration.register(PAYLOAD, {}) is False

    def test_a_published_address_nothing_is_listening_on_is_silence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        environment = dict(ENVIRONMENT)
        environment["GPT_VOICECODING_CLAUDE_CHANNEL_CONFIG"] = json.dumps(
            {"approvalSocketPath": str(tmp_path / "nobody.sock"), "dialTimeoutSeconds": 0.2}
        )
        assert registration.register(PAYLOAD, environment) is False

    def test_it_never_writes_to_stdout(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Claude Code reads a `SessionStart` hook's stdout as context for the Session."""
        registration.register(PAYLOAD, {})
        assert capsys.readouterr().out == ""


_names = itertools.count()


@pytest.fixture
def socket_root() -> Iterator[Path]:
    """A short private root: Darwin caps an ``AF_UNIX`` path at 103 bytes, so
    this cannot live under pytest's ``tmp_path``."""
    home = Path("/tmp") / f"vc-reg-{next(_names)}-{id(object())}"
    home.mkdir(mode=0o700)
    yield home
    shutil.rmtree(home, ignore_errors=True)


class TestReachingTheEngineOverTheOneIngress:
    """A real socket, because "one ingress" is a claim about a socket."""

    def adapter(self, root: Path) -> ClaudeAgentAdapter:
        (root / "sessions").mkdir(parents=True, exist_ok=True)
        return ClaudeAgentAdapter(
            settings=ClaudeSettings(
                registry_directory=root / "sessions", socket_directory=root / "sockets"
            )
        )

    def run(self, tmp_path: Path, payload: dict) -> ClaudeAgentAdapter:
        adapter = self.adapter(tmp_path)

        async def scenario() -> None:
            await adapter.connect()
            try:
                environment = dict(ENVIRONMENT)
                environment["GPT_VOICECODING_CLAUDE_CHANNEL_CONFIG"] = json.dumps(
                    {
                        "approvalSocketPath": str(adapter.approval_socket_path()),
                        "dialTimeoutSeconds": 5,
                    }
                )
                await asyncio.get_running_loop().run_in_executor(
                    None, registration.register, payload, environment
                )
                # The hook does not wait for the engine, so the test does.
                for _ in range(50):
                    if adapter.reported(SESSION_ID) is not None:
                        break
                    await asyncio.sleep(0.02)
            finally:
                await adapter.aclose()

        asyncio.run(scenario())
        return adapter

    def test_the_engine_records_what_the_session_reported(self, socket_root: Path) -> None:
        adapter = self.run(socket_root, PAYLOAD)
        report = adapter.reported(SESSION_ID)
        assert report is not None
        assert report.transcript_path == Path(str(PAYLOAD["transcript_path"]))
        assert report.messaging_socket == Path("/tmp/claude-inbox.sock")
        assert report.messaging_token == "a-token"

    def test_it_says_so_in_the_log(
        self, socket_root: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A run's own evidence that the registration landed."""
        with caplog.at_level("INFO"):
            self.run(socket_root, PAYLOAD)
        assert any("registration received for" in record.message for record in caplog.records)

    def test_no_roster_row_is_created_by_a_registration(self, socket_root: Path) -> None:
        """The roster is `claude agents --json`, which sees Sessions whose hook never ran."""
        adapter = self.run(socket_root, PAYLOAD)
        assert adapter.reachable() == ()


class TestWhatTheHookIsInstalledAs:
    def test_both_of_adr_0011s_hooks_are_installed(self) -> None:
        from gpt_voicecoding.installation import claude_hooks

        hooks = claude_hooks.desired_hooks(Path("/usr/bin/python3"))
        assert set(hooks) == {claude_hooks.APPROVAL_EVENT, claude_hooks.REGISTRATION_EVENT}

    def test_the_registration_hook_runs_the_module_this_ticket_built(self) -> None:
        from gpt_voicecoding.installation import claude_hooks

        hooks = claude_hooks.desired_hooks(Path("/usr/bin/python3"))
        command = hooks[claude_hooks.REGISTRATION_EVENT][0]["hooks"][0]["command"]
        assert claude_hooks.REGISTRATION_MODULE == registration.__name__
        assert claude_hooks.REGISTRATION_MODULE in command

    def test_it_is_given_a_short_ceiling_because_it_runs_while_a_session_opens(self) -> None:
        from gpt_voicecoding.installation import claude_hooks

        hooks = claude_hooks.desired_hooks(Path("/usr/bin/python3"))
        registration_timeout = hooks[claude_hooks.REGISTRATION_EVENT][0]["hooks"][0]["timeout"]
        approval_timeout = hooks[claude_hooks.APPROVAL_EVENT][0]["hooks"][0]["timeout"]
        assert registration_timeout < approval_timeout

    def test_both_hooks_are_recognised_as_ours_so_uninstall_takes_them_back(self) -> None:
        from gpt_voicecoding.installation import claude_hooks

        hooks = claude_hooks.desired_hooks(Path("/usr/bin/python3"))
        for group in hooks.values():
            assert all(claude_hooks.is_ours(handler) for handler in group[0]["hooks"])


def test_the_address_file_is_the_one_both_hooks_read() -> None:
    """One published address, so one ingress — ADR 0011 and #86's file."""
    published = publish_address(Path("/tmp/approvals.sock"), ClaudeSettings())
    try:
        assert published == address_path()
    finally:
        published.unlink(missing_ok=True)

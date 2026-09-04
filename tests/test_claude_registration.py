"""The `SessionStart` hook, and the fact it is the sole source of exactly one field.

Two things are asserted, and the second is the one that matters. The hook does
what it is for: it carries `transcript_path`, the Session's inbox socket and its
token to the engine, over the **same** socket the approval hook dials — one
ingress, because ADR 0011 publishes one address.

And it does nothing else. A user-scope hook fires for every Session in the
config directory, so a Session no engine is holding must cost the process it
started and nothing more: no socket, no writes, nothing on stdout — where
Claude Code would read it as context to add to the user's own conversation.

**Reachability and existence stay two questions.** What the hook moves is the
first one: it hands over an inbox address nothing outside that Session could
discover. It adds no roster row, and it could not — the roster is
`claude agents --json` alone, which is `test_claude_discovery.py`'s subject and
sees Sessions whose hook never ran.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from fakes import PROGRESS_CAPTURE
from gpt_voicecoding.adapters.agent.claude import bootstrap, registration
from gpt_voicecoding.adapters.agent.claude.adapter import ClaudeAgentAdapter
from gpt_voicecoding.adapters.agent.claude.approval import REGISTRATION_TYPE, TYPE_FIELD
from gpt_voicecoding.adapters.agent.claude.bootstrap import publish_address
from gpt_voicecoding.adapters.agent.claude.settings import ClaudeSettings
from gpt_voicecoding.locations import address_path
from gpt_voicecoding.seams.identity import AgentKind, SessionTarget

SESSION_ID = "d3a776ae-3b60-437d-bc70-ba57a2b280c6"

#: The pid #73 measured beside that session id in `claude agents --json`.
PID = 3538

TARGET = SessionTarget(agent=AgentKind.CLAUDE, session_id=SESSION_ID, pid=PID)

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
    # Measured 2026-08-26 in a sandbox config directory: a `SessionStart` hook
    # process sees `CLAUDE_PID`, and its own `os.getppid()` is that number.
    registration.PID_VARIABLE: str(PID),
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
    @pytest.fixture(autouse=True)
    def _nobody_published_an_address(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The premise of this class, arranged rather than hoped for.

        `register` takes no `base_dir`, so a call with neither a channel-config
        variable nor a published address of its own resolves the machine's real
        `~/Library/Application Support/GPT-VoiceCoding/engine/address.json` —
        whichever engine the developer has up. #229 caught these tests reading
        it: while an acceptance engine was publishing, the first one went red
        and the third handed a fake Session to a real engine. So every test in
        this class resolves inside a directory it owns, where nothing published.

        Autouse rather than requested, because "no engine is holding this
        Session" is the class's subject and not any one test's arrangement.

        The seam is `bootstrap.address_path` rather than the channel-config
        variable the second test hands in, because that variable answers inside
        `_told` before the published file is ever read — and the fall-through to
        that file is the path a Session with no variable takes, which is the
        path #229 is about. `test_claude_unpublished_address.py:44` and
        `test_claude_address_claim.py:39` take the same question away from the
        machine at the same seam.

        The patch binds `bootstrap`'s own global, so a `published_address`
        rewritten to call `locations.address_path` directly would leave it
        inert — and these tests would pass while reading the developer's engine
        again, which is #229 coming back saying nothing. So the arrangement is
        read back through the seam before any test runs.
        """
        engine = tmp_path / "engine"
        published = engine / "address.json"
        monkeypatch.setattr(bootstrap, "address_path", lambda base_dir=None: published)
        engine.mkdir()
        unreached = tmp_path / "unreached.sock"
        published.write_text(json.dumps({"approvalSocketPath": str(unreached)}))
        assert bootstrap.approval_socket_path_in({}) == unreached
        published.unlink()

    def test_it_opens_no_socket_and_does_nothing(self) -> None:
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
            progress_capture=PROGRESS_CAPTURE,
            settings=ClaudeSettings(
                registry_directory=root / "sessions", socket_directory=root / "sockets"
            ),
        )

    def run(
        self,
        tmp_path: Path,
        payload: dict,
        *,
        environment: dict[str, str] | None = None,
        expect: bool = True,
    ) -> ClaudeAgentAdapter:
        adapter = self.adapter(tmp_path)
        reached: list[SessionTarget] = []

        async def scenario() -> None:
            await adapter.connect()
            try:
                environment_ = dict(ENVIRONMENT if environment is None else environment)
                environment_["GPT_VOICECODING_CLAUDE_CHANNEL_CONFIG"] = json.dumps(
                    {
                        "approvalSocketPath": str(adapter.approval_socket_path()),
                        "dialTimeoutSeconds": 5,
                    }
                )
                await asyncio.get_running_loop().run_in_executor(
                    None, registration.register, payload, environment_
                )
                # The hook does not wait for the engine, so the test does.
                for _ in range(50):
                    if (adapter.reported(TARGET) is not None) == expect:
                        break
                    await asyncio.sleep(0.02)
                # `aclose` takes every channel down with it, so what this
                # adapter could reach has to be read while it is still up.
                reached.extend(adapter.reachable())
            finally:
                await adapter.aclose()

        asyncio.run(scenario())
        self.reached = tuple(reached)
        return adapter

    def test_the_engine_records_what_the_session_reported(self, socket_root: Path) -> None:
        adapter = self.run(socket_root, PAYLOAD)
        report = adapter.reported(TARGET)
        assert report is not None
        assert report.transcript_path == Path(str(PAYLOAD["transcript_path"]))
        assert report.messaging_socket == Path("/tmp/claude-inbox.sock")
        assert report.messaging_token == "a-token"

    def test_the_report_names_an_exact_process_not_just_a_session_id(
        self, socket_root: Path
    ) -> None:
        """`--resume` forks two processes under one session id; the pid tells them apart."""
        adapter = self.run(socket_root, PAYLOAD)
        report = adapter.reported(TARGET)
        assert report is not None
        assert report.pid == PID
        assert report.target == TARGET

    def test_it_says_so_in_the_log(
        self, socket_root: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A run's own evidence that the registration landed."""
        with caplog.at_level("INFO"):
            self.run(socket_root, PAYLOAD)
        assert any("registration received for" in record.message for record in caplog.records)

    def test_the_session_becomes_reachable(self, socket_root: Path) -> None:
        """The point of the hook: the inbox address exists only inside that Session.

        Nothing outside a Session can discover where its inbox listens — the
        reference implementation learned it from a launch wrapper it owned, and
        v1.0 does not launch Sessions (#72). A registration that only recorded
        the report would leave `_channels` empty, so every Relay and every
        approval would be refused by an engine that had been told the address.
        """
        self.run(socket_root, PAYLOAD)
        assert self.reached == (TARGET,)

    def test_a_registration_with_no_socket_reaches_nothing_and_still_records(
        self, socket_root: Path
    ) -> None:
        """A build exporting no messaging variables: the transcript path is still worth having."""
        environment = {registration.PID_VARIABLE: str(PID)}
        adapter = self.run(socket_root, PAYLOAD, environment=environment)

        assert self.reached == ()
        report = adapter.reported(TARGET)
        assert report is not None
        assert report.transcript_path == Path(str(PAYLOAD["transcript_path"]))

    def test_a_registration_with_no_pid_names_no_session_at_all(self, socket_root: Path) -> None:
        """A Claude target needs a pid, so there is nothing to attach the report to."""
        environment = {
            registration.MESSAGING_SOCKET_VARIABLE: "/tmp/claude-inbox.sock",
            registration.MESSAGING_TOKEN_VARIABLE: "a-token",
        }
        adapter = self.run(socket_root, PAYLOAD, environment=environment, expect=False)

        assert self.reached == ()
        assert adapter.reported(TARGET) is None


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


def test_the_address_file_is_the_one_both_hooks_read(tmp_path: Path) -> None:
    """One published address, so one ingress — ADR 0011 and #86's file.

    Under a `base_dir`, because the real one belongs to whatever engine this
    developer has running and this test used to write and then unlink it — which
    is #202's defect, committed by the test suite.
    """
    published = publish_address(Path("/tmp/approvals.sock"), ClaudeSettings(), base_dir=tmp_path)

    assert published == address_path(tmp_path)

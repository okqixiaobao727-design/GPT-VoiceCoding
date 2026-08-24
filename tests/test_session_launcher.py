"""The Session Launcher's two adapters, against a fake process and tmux layer.

What is tested here is the launcher's own contract — the one Bridge Core is
entitled to rely on and cannot check for itself:

- exactly one launch per request identity, and one outcome;
- an outcome that is authoritative and truthful, carrying the real error and
  registering nothing when the launch did not happen;
- a workspace that is *verified* rather than intended, on both agents;
- a child environment the launcher owns completely, with nothing inherited from
  either the engine's own secrets or a tmux server's stale shell;
- a close that is exact, idempotent, fails closed on a stale identity, and
  reports per-child outcomes only where the adapter really owns a child.

The real `claude` and `codex` are never run. A launched Session is stood in for
by a small script that does the one thing the launcher actually reads: it
registers itself the way Claude Code does. That is the seam between "this
launcher works" and "that product behaves as measured", and the second half is
what the manual proof in `scripts/` is for.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest

from codex_fake import FakeAppServer
from gpt_voicecoding.adapters.session_launcher import DirectChildLauncher, TmuxLauncher
from gpt_voicecoding.adapters.session_launcher.claiming import (
    ClaimError,
    candidates,
    claim,
    is_descendant,
    snapshot,
)
from gpt_voicecoding.adapters.session_launcher.codex import CodexPreparation
from gpt_voicecoding.adapters.session_launcher.console import Console
from gpt_voicecoding.adapters.session_launcher.environment import child_environment
from gpt_voicecoding.adapters.session_launcher.plan import PreparationError
from gpt_voicecoding.adapters.session_launcher.settings import LauncherSettings, SettingsError
from gpt_voicecoding.adapters.session_launcher.tmux import (
    Tmux,
    TmuxError,
    _TmuxAppServer,
    pane_command,
)
from gpt_voicecoding.seams.identity import (
    AgentKind,
    RequestId,
    SessionLabel,
    SessionTarget,
    new_request_id,
)
from gpt_voicecoding.seams.session_launcher import (
    ChildOutcome,
    CloseRequest,
    CloseStatus,
    LaunchRequest,
    LaunchStatus,
)
from gpt_voicecoding.seams.verify import VerifyOutcome

LABEL = SessionLabel(project="gpt-voicecoding", task="build the session launcher")

#: The variable an engine keeps its Companion Channel's bot token in. Named here
#: because the leak it stands for is the reason the child environment is an
#: allowlist rather than a filtered copy of the engine's own.
TOKEN_VARIABLE = "GPT_VOICECODING_TELEGRAM_TOKEN"

#: What a stale tmux server has been carrying since whichever shell started it.
#: This exact variable, because it is the one ADR 0004 measured: nothing in the
#: repository ever set it, and it was 98.1% of a 68 MB log.
DIRTY_VARIABLE = "MallocStackLogging"


# -- standing in for a Claude Session ------------------------------------


def a_fake_claude(path: Path, registry: Path, *, session_id: str, live_seconds: float = 30) -> Path:
    """A script that registers itself the way Claude Code does, then stays up.

    It writes the record under its **own** pid, which is what makes the ancestry
    check in `claiming` meaningful: the process that registers is the one the
    launcher has to prove it started.
    """
    path.write_text(
        textwrap.dedent(f"""\
            #!/usr/bin/env python3
            import json, os, sys, time
            from pathlib import Path
            registry = Path({str(registry)!r})
            registry.mkdir(parents=True, exist_ok=True)
            (registry / f"{{os.getpid()}}.json").write_text(json.dumps({{
                "pid": os.getpid(),
                "sessionId": {session_id!r},
                "cwd": os.getcwd(),
                "version": "2.1.238",
                "peerProtocol": 1,
                "messagingSocketPath": f"/tmp/cc-socks/{{os.getpid()}}.sock",
                "status": "idle",
            }}))
            print("argv:", " ".join(sys.argv[1:]), flush=True)
            time.sleep({live_seconds})
            """),
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def a_script(path: Path, body: str) -> Path:
    """Any other stand-in child: something that exits, or floods, or refuses."""
    path.write_text(f"#!/usr/bin/env python3\n{textwrap.dedent(body)}", encoding="utf-8")
    path.chmod(0o755)
    return path


class FakeClaudeEngine:
    """The three things the running Claude Agent adapter answers for a launch."""

    def __init__(self, registry_directory: Path) -> None:
        self._registry_directory = registry_directory
        self.registered: list[tuple[SessionTarget, Path]] = []

    def registry_directory(self) -> Path:
        return self._registry_directory

    def launch_bootstrap(self, channel_socket_path: Path) -> str:
        return json.dumps({"socketPath": str(channel_socket_path), "approvalSocketPath": "/tmp/a"})

    def register_session(self, target: SessionTarget, socket_path: Path) -> None:
        self.registered.append((target, socket_path))


class FakeCodexEngine:
    """The launched app-server address the running Codex Agent adapter takes."""

    def __init__(self) -> None:
        self.registered: list[tuple[SessionTarget, Path]] = []

    async def register_session(self, target: SessionTarget, socket_path: Path) -> None:
        self.registered.append((target, socket_path))


def a_request(workspace: Path, *, env: dict[str, str] | None = None) -> LaunchRequest:
    return LaunchRequest(
        request_id=new_request_id(),
        agent=AgentKind.CLAUDE,
        workspace=workspace,
        label=LABEL,
        env=env or {},
    )


#: Short runtime roots these tests made, cleaned up when the module is done.
#: They have to be short: a launch mints a Unix socket underneath one, and
#: `pytest`'s own `tmp_path` is already most of the 103 bytes Darwin allows —
#: which is exactly the length check the launcher does before it starts anything,
#: so a test rooted there would fail on the harness rather than on the code.
_RUNTIME_ROOTS: list[Path] = []


#: Fake tmux servers a test made. They really start processes, so they really
#: have to be shut down — a test that leaves a Session running is a test that
#: leaks one per run.
_FAKE_SERVERS: list = []


@pytest.fixture(autouse=True)
def _clean_up_after_every_test():
    yield
    while _FAKE_SERVERS:
        _FAKE_SERVERS.pop().shut_down()
    while _RUNTIME_ROOTS:
        shutil.rmtree(_RUNTIME_ROOTS.pop(), ignore_errors=True)


def settings_for(tmp_path: Path, *, binary: Path, **extra) -> LauncherSettings:
    runtime = Path(tempfile.mkdtemp(prefix="vc-t-", dir="/tmp"))
    _RUNTIME_ROOTS.append(runtime)
    return LauncherSettings(
        claude_binary=binary,
        codex_binary=binary,
        runtime_directory=runtime,
        **extra,
    )


# -- the child's environment ---------------------------------------------


class TestTheChildEnvironment:
    """The launcher owns it completely, and "completely" is default-deny."""

    def test_the_requested_variables_are_set_exactly(self) -> None:
        built = child_environment(
            {"GPT_VC": "1", "OTHER": "two"}, terminal_type="xterm", source={"PATH": "/bin"}
        )

        assert built["GPT_VC"] == "1"
        assert built["OTHER"] == "two"

    def test_the_engines_own_secrets_are_not_forwarded(self) -> None:
        """The Companion Channel's bot token lives in the engine's environment.

        Handing a launched coding agent the credentials of the bridge that
        launched it is what a "copy the environment and filter it" baseline
        would do, and it is why this one is an allowlist.
        """
        built = child_environment(
            {},
            terminal_type="xterm",
            source={"PATH": "/bin", TOKEN_VARIABLE: "secret-bot-token"},
        )

        assert TOKEN_VARIABLE not in built
        assert "secret-bot-token" not in built.values()

    def test_a_variable_nobody_named_does_not_travel(self) -> None:
        """ADR 0004's variable, which no subtractive rule would have caught."""
        built = child_environment(
            {}, terminal_type="xterm", source={"PATH": "/bin", DIRTY_VARIABLE: "1"}
        )

        assert DIRTY_VARIABLE not in built

    def test_the_terminal_is_stated_rather_than_inherited(self) -> None:
        built = child_environment({}, terminal_type="screen-256color", source={"TERM": "dumb"})

        assert built["TERM"] == "screen-256color"

    def test_what_an_agent_needs_to_be_itself_does_travel(self) -> None:
        built = child_environment(
            {}, terminal_type="xterm", source={"PATH": "/bin", "HOME": "/home/x"}
        )

        assert built["PATH"] == "/bin"
        assert built["HOME"] == "/home/x"


# -- claiming a Session --------------------------------------------------


def a_record(registry: Path, pid: int, *, cwd: Path, session_id: str = "s-1") -> None:
    registry.mkdir(parents=True, exist_ok=True)
    (registry / f"{pid}.json").write_text(
        json.dumps(
            {
                "pid": pid,
                "sessionId": session_id,
                "cwd": str(cwd),
                "version": "2.1.238",
                "peerProtocol": 1,
                "messagingSocketPath": f"/tmp/cc-socks/{pid}.sock",
                "status": "idle",
            }
        ),
        encoding="utf-8",
    )


class TestClaimingASession:
    """Which record is *this* launch's, and the three things that have to hold."""

    def test_a_session_that_was_already_there_is_never_claimed(self, tmp_path: Path) -> None:
        """The user's own Session in the same workspace is not this launch's."""
        registry = tmp_path / "registry"
        workspace = tmp_path / "ws"
        workspace.mkdir()
        a_record(registry, 111, cwd=workspace)
        before = snapshot(registry)

        found = candidates(
            registry, workspace=workspace, before=before, ancestor=111, parent_of=lambda _: None
        )

        assert found == ()

    def test_a_session_in_another_workspace_is_not_claimed(self, tmp_path: Path) -> None:
        registry = tmp_path / "registry"
        workspace = tmp_path / "ws"
        workspace.mkdir()
        (tmp_path / "elsewhere").mkdir()
        before = snapshot(registry)
        a_record(registry, 222, cwd=tmp_path / "elsewhere")

        found = candidates(
            registry, workspace=workspace, before=before, ancestor=222, parent_of=lambda _: None
        )

        assert found == ()

    def test_a_session_we_did_not_start_is_not_claimed(self, tmp_path: Path) -> None:
        """Novelty and workspace only narrow; descent is the positive evidence."""
        registry = tmp_path / "registry"
        workspace = tmp_path / "ws"
        workspace.mkdir()
        before = snapshot(registry)
        a_record(registry, 333, cwd=workspace)

        found = candidates(
            registry, workspace=workspace, before=before, ancestor=999, parent_of=lambda _: None
        )

        assert found == ()

    def test_a_grandchild_is_ours(self, tmp_path: Path) -> None:
        """Under tmux the pane runs a shell and the agent is its child."""
        registry = tmp_path / "registry"
        workspace = tmp_path / "ws"
        workspace.mkdir()
        before = snapshot(registry)
        a_record(registry, 500, cwd=workspace)
        tree = {500: 400, 400: 300}

        found = candidates(
            registry,
            workspace=workspace,
            before=before,
            ancestor=300,
            parent_of=tree.get,
        )

        assert [record.pid for record in found] == [500]

    def test_a_workspace_reached_through_a_symlink_is_the_same_workspace(
        self, tmp_path: Path
    ) -> None:
        """`/tmp/x` is registered as `/private/tmp/x` on Darwin, and is one place."""
        registry = tmp_path / "registry"
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)
        before = snapshot(registry)
        a_record(registry, 600, cwd=real)

        found = candidates(
            registry, workspace=link, before=before, ancestor=600, parent_of=lambda _: None
        )

        assert [record.pid for record in found] == [600]

    def test_two_candidates_are_refused_rather_than_chosen(self, tmp_path: Path) -> None:
        registry = tmp_path / "registry"
        workspace = tmp_path / "ws"
        workspace.mkdir()
        before = snapshot(registry)
        a_record(registry, 700, cwd=workspace, session_id="a")
        a_record(registry, 701, cwd=workspace, session_id="b")
        tree = {700: 1, 701: 1}

        with pytest.raises(ClaimError, match="cannot be told"):
            asyncio.run(
                claim(
                    registry,
                    workspace=workspace,
                    before=before,
                    ancestor=1,
                    timeout_seconds=0.5,
                    parent_of=tree.get,
                )
            )

    def test_a_child_that_is_already_gone_is_said_so_at_once(self, tmp_path: Path) -> None:
        """Not "slow to register" — gone. The two are different facts."""
        registry = tmp_path / "registry"
        registry.mkdir()
        workspace = tmp_path / "ws"
        workspace.mkdir()

        with pytest.raises(ClaimError, match="already gone"):
            asyncio.run(
                claim(
                    registry,
                    workspace=workspace,
                    before=snapshot(registry),
                    ancestor=1,
                    timeout_seconds=30,
                    still_running=lambda: False,
                )
            )

    def test_an_ancestry_walk_cannot_spin(self) -> None:
        """A process table that answers in a circle must not hang a launch."""
        assert not is_descendant(1, 99, parent_of={1: 2, 2: 1}.get)


# -- the pseudo-terminal -------------------------------------------------


class TestTheConsole:
    """Owning the master end is an obligation: drain it, or the Session stops."""

    def test_a_child_that_floods_is_never_blocked(self, tmp_path: Path) -> None:
        """The failure this guards is a Session that stalls invisibly.

        Far more is written than any pipe or terminal buffer holds. If the master
        end were not drained the child would block partway and never exit, so the
        child completing at all is the assertion.
        """
        script = a_script(
            tmp_path / "flood.py",
            """
            import sys
            for _ in range(2000):
                sys.stdout.write("x" * 1024)
            sys.stdout.flush()
            """,
        )

        async def run() -> int:
            console = Console(tail_bytes=4096)
            await console.start([str(script)], env={"PATH": "/usr/bin:/bin"}, cwd=tmp_path)
            try:
                return await asyncio.wait_for(console.wait(), 30)
            finally:
                await console.close()

        assert asyncio.run(run()) == 0

    def test_only_the_tail_is_kept(self, tmp_path: Path) -> None:
        """A bounded buffer, not a log. ADR 0004: the engine owns exactly one log."""
        script = a_script(
            tmp_path / "chatty.py",
            """
            import sys
            for index in range(500):
                sys.stdout.write(f"line-{index}\\n")
            sys.stdout.flush()
            """,
        )

        async def run() -> str:
            console = Console(tail_bytes=256)
            await console.start([str(script)], env={"PATH": "/usr/bin:/bin"}, cwd=tmp_path)
            await asyncio.wait_for(console.wait(), 30)
            await asyncio.sleep(0.2)
            tail = console.tail()
            await console.close()
            return tail

        tail = asyncio.run(run())
        assert len(tail.encode("utf-8")) <= 512
        assert "line-499" in tail
        assert "line-0\n" not in tail

    def test_a_finished_child_leaves_no_zombie(self, tmp_path: Path) -> None:
        """The reaping the launcher takes on by owning the child environment."""
        script = a_script(tmp_path / "quick.py", "pass\n")

        async def run() -> tuple[int, int]:
            console = Console()
            await console.start([str(script)], env={"PATH": "/usr/bin:/bin"}, cwd=tmp_path)
            pid = console.pid
            await asyncio.wait_for(console.wait(), 30)
            await console.close()
            return pid, console.returncode or 0

        pid, code = asyncio.run(run())
        assert code == 0
        # A reaped child is gone from the process table entirely. An unreaped one
        # would still be there, as a zombie, and `kill -0` would find it.
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


# -- the direct-child adapter --------------------------------------------


class TestTheDirectChildLauncher:
    def _launcher(self, tmp_path: Path, binary: Path, registry: Path) -> DirectChildLauncher:
        launcher = DirectChildLauncher(settings=settings_for(tmp_path, binary=binary))
        launcher.use_claude(FakeClaudeEngine(registry))
        return launcher

    def test_a_launched_session_comes_back_with_the_exact_identity(self, tmp_path: Path) -> None:
        registry = tmp_path / "registry"
        workspace = tmp_path / "ws"
        workspace.mkdir()
        binary = a_fake_claude(tmp_path / "claude", registry, session_id="abc-123")
        launcher = self._launcher(tmp_path, binary, registry)

        outcome = asyncio.run(self._launch_then_close(launcher, a_request(workspace)))

        assert outcome.status is LaunchStatus.LAUNCHED
        assert outcome.target is not None
        assert outcome.target.session_id == "abc-123"
        assert outcome.target.pid is not None

    def test_a_non_default_registry_directory_preserves_the_launch_outcome(
        self, tmp_path: Path
    ) -> None:
        registry = tmp_path / "deployment-registry"
        decoy_registry = tmp_path / "launcher-registry-decoy"
        workspace = tmp_path / "ws"
        workspace.mkdir()
        a_record(decoy_registry, os.getpid(), cwd=workspace, session_id="decoy-session")
        binary = a_fake_claude(tmp_path / "claude", registry, session_id="configured-session")
        launcher = DirectChildLauncher(settings=settings_for(tmp_path, binary=binary))
        launcher.use_claude(FakeClaudeEngine(registry))

        outcome = asyncio.run(self._launch_then_close(launcher, a_request(workspace)))

        assert outcome.status is LaunchStatus.LAUNCHED
        assert outcome.target is not None
        assert outcome.target.session_id == "configured-session"

    def test_the_launch_carries_the_hook_plugin_and_the_permission_mode(
        self, tmp_path: Path
    ) -> None:
        """Obligation 7, and the decision not to pre-approve, both visible in argv.

        The stand-in prints what it was given, so this asserts on what the child
        actually received rather than on what the launcher meant to send.
        """
        registry = tmp_path / "registry"
        workspace = tmp_path / "ws"
        workspace.mkdir()
        binary = a_fake_claude(tmp_path / "claude", registry, session_id="abc-123")
        launcher = self._launcher(tmp_path, binary, registry)

        async def run() -> str:
            request = a_request(workspace)
            outcome = await launcher.launch(request)
            assert outcome.target is not None
            console = launcher._live[outcome.target].console
            await asyncio.sleep(0.5)
            said = console.tail()
            await launcher.aclose()
            return said

        said = asyncio.run(run())
        assert "--permission-mode default" in said
        assert "--plugin-dir" in said

    def test_the_launch_renders_the_channel_plugin_with_the_deployment_interpreter(
        self, tmp_path: Path
    ) -> None:
        """The launcher, not its test, supplies the Session Channel manifest."""
        registry = tmp_path / "registry"
        workspace = tmp_path / "ws"
        workspace.mkdir()
        binary = a_fake_claude(tmp_path / "claude", registry, session_id="abc-123")
        interpreter = tmp_path / "bundle-python3"
        settings = settings_for(
            tmp_path,
            binary=binary,
            interpreter=interpreter,
        )
        launcher = DirectChildLauncher(settings=settings)
        launcher.use_claude(FakeClaudeEngine(registry))

        async def run() -> tuple[list[dict], list[Path]]:
            outcome = await launcher.launch(a_request(workspace))
            assert outcome.target is not None
            manifests = [
                json.loads(path.read_text(encoding="utf-8"))
                for path in settings.runtime_directory.rglob("plugin.json")
            ]
            await launcher.aclose()
            return manifests, list(settings.runtime_directory.iterdir())

        manifests, left_after_close = asyncio.run(run())
        channel_manifests = [manifest for manifest in manifests if "channels" in manifest]
        assert len(channel_manifests) == 1
        assert channel_manifests[0]["mcpServers"]["gpt-voicecoding-claude-channel"][
            "command"
        ] == str(interpreter)
        assert left_after_close == []

    def test_the_launch_loads_and_selects_the_session_channel(self, tmp_path: Path) -> None:
        """The product supplies both the inline plugin and its channel selector."""
        registry = tmp_path / "registry"
        workspace = tmp_path / "ws"
        workspace.mkdir()
        binary = a_fake_claude(tmp_path / "claude", registry, session_id="abc-123")
        launcher = self._launcher(tmp_path, binary, registry)

        async def run() -> str:
            outcome = await launcher.launch(a_request(workspace))
            assert outcome.target is not None
            console = launcher._live[outcome.target].console
            await asyncio.sleep(0.5)
            said = console.tail()
            await launcher.aclose()
            return said

        said = asyncio.run(run())
        assert said.count("--plugin-dir") == 2
        assert "--channels plugin:gpt-voicecoding-session-channel@gpt-voicecoding-channel" in said

    def test_the_same_request_id_never_starts_a_second_child(self, tmp_path: Path) -> None:
        registry = tmp_path / "registry"
        workspace = tmp_path / "ws"
        workspace.mkdir()
        binary = a_fake_claude(tmp_path / "claude", registry, session_id="abc-123")
        launcher = self._launcher(tmp_path, binary, registry)

        async def run():
            request = a_request(workspace)
            first = await launcher.launch(request)
            second = await launcher.launch(request)
            registered = len(list((registry).glob("*.json")))
            await launcher.aclose()
            return first, second, registered

        first, second, registered = asyncio.run(run())
        assert first is second
        assert registered == 1

    def test_a_binary_that_is_not_there_fails_with_the_real_error(self, tmp_path: Path) -> None:
        registry = tmp_path / "registry"
        workspace = tmp_path / "ws"
        workspace.mkdir()
        launcher = self._launcher(tmp_path, tmp_path / "no-such-claude", registry)

        outcome = asyncio.run(launcher.launch(a_request(workspace)))

        assert outcome.status is LaunchStatus.FAILED
        assert outcome.target is None
        assert "no-such-claude" in outcome.detail

    def test_a_child_that_exits_at_once_registers_nothing(self, tmp_path: Path) -> None:
        """No phantom registration, and the outcome says what actually happened."""
        registry = tmp_path / "registry"
        registry.mkdir()
        workspace = tmp_path / "ws"
        workspace.mkdir()
        binary = a_script(
            tmp_path / "dies.py",
            """
            import sys
            print("I cannot start: no credentials", flush=True)
            sys.exit(3)
            """,
        )
        launcher = self._launcher(tmp_path, binary, registry)

        outcome = asyncio.run(launcher.launch(a_request(workspace)))

        assert outcome.status is LaunchStatus.FAILED
        assert outcome.target is None
        assert "already gone" in outcome.detail
        # And what it actually printed survives into the outcome, which is the
        # difference between a launcher that failed and one that says why.
        assert "no credentials" in outcome.detail

    def test_a_workspace_that_is_not_there_is_refused_before_anything_starts(
        self, tmp_path: Path
    ) -> None:
        registry = tmp_path / "registry"
        binary = a_fake_claude(tmp_path / "claude", registry, session_id="abc")
        launcher = self._launcher(tmp_path, binary, registry)

        outcome = asyncio.run(launcher.launch(a_request(tmp_path / "missing")))

        assert outcome.status is LaunchStatus.FAILED
        assert "no workspace at" in outcome.detail

    def test_a_relative_workspace_is_refused(self, tmp_path: Path) -> None:
        """Obligation 6's failure mode: a Session that runs wherever we happened to be."""
        registry = tmp_path / "registry"
        binary = a_fake_claude(tmp_path / "claude", registry, session_id="abc")
        launcher = self._launcher(tmp_path, binary, registry)

        outcome = asyncio.run(launcher.launch(a_request(Path("relative/place"))))

        assert outcome.status is LaunchStatus.FAILED
        assert "absolute" in outcome.detail

    def test_the_session_really_runs_in_the_workspace(self, tmp_path: Path) -> None:
        """The readback, end to end: the record's cwd is what was asked for."""
        registry = tmp_path / "registry"
        workspace = tmp_path / "ws"
        workspace.mkdir()
        binary = a_fake_claude(tmp_path / "claude", registry, session_id="abc-123")
        launcher = self._launcher(tmp_path, binary, registry)

        outcome = asyncio.run(self._launch_then_close(launcher, a_request(workspace)))

        assert outcome.target is not None
        record = json.loads((registry / f"{outcome.target.pid}.json").read_text())
        assert Path(os.path.realpath(record["cwd"])) == Path(os.path.realpath(workspace))

    def test_the_requested_environment_reaches_the_child(self, tmp_path: Path) -> None:
        registry = tmp_path / "registry"
        workspace = tmp_path / "ws"
        workspace.mkdir()
        binary = a_script(
            tmp_path / "says-env.py",
            """
            import os, time
            token = os.environ.get("GPT_VOICECODING_TELEGRAM_TOKEN", "<unset>")
            print("GPT_VC=" + os.environ.get("GPT_VC", "<unset>"), flush=True)
            print("TOKEN=" + token, flush=True)
            time.sleep(5)
            """,
        )
        launcher = self._launcher(tmp_path, binary, registry)
        os.environ[TOKEN_VARIABLE] = "secret-bot-token"
        try:

            async def run() -> str:
                request = a_request(workspace, env={"GPT_VC": "yes"})
                await launcher.launch(request)
                await asyncio.sleep(0.5)
                # The launch fails (this stand-in never registers), so the tail is
                # read off the outcome rather than off a live Session.
                return (await launcher.launch(request)).detail

            said = asyncio.run(run())
        finally:
            del os.environ[TOKEN_VARIABLE]

        assert "GPT_VC=yes" in said
        assert "TOKEN=<unset>" in said

    def test_an_engine_with_no_claude_adapter_refuses_rather_than_launching(
        self, tmp_path: Path
    ) -> None:
        """A Claude Session with no Relay route is not a Session worth starting."""
        registry = tmp_path / "registry"
        workspace = tmp_path / "ws"
        workspace.mkdir()
        binary = a_fake_claude(tmp_path / "claude", registry, session_id="abc")
        launcher = DirectChildLauncher(settings=settings_for(tmp_path, binary=binary))

        outcome = asyncio.run(launcher.launch(a_request(workspace)))

        assert outcome.status is LaunchStatus.FAILED
        assert "no Claude Agent adapter" in outcome.detail

    async def _launch_then_close(self, launcher: DirectChildLauncher, request: LaunchRequest):
        outcome = await launcher.launch(request)
        await launcher.aclose()
        return outcome

    def test_it_verifies_by_reaching_for_the_binaries(self, tmp_path: Path) -> None:
        launcher = DirectChildLauncher(settings=settings_for(tmp_path, binary=tmp_path / "absent"))

        result = asyncio.run(launcher.verify())

        assert result.outcome is VerifyOutcome.FAIL
        assert "absent" in result.detail


class TestClosingADirectChild:
    def _launched(self, tmp_path: Path):
        registry = tmp_path / "registry"
        workspace = tmp_path / "ws"
        workspace.mkdir()
        binary = a_fake_claude(tmp_path / "claude", registry, session_id="abc-123")
        launcher = DirectChildLauncher(settings=settings_for(tmp_path, binary=binary))
        launcher.use_claude(FakeClaudeEngine(registry))
        return launcher, workspace

    def test_a_close_ends_the_session(self, tmp_path: Path) -> None:
        launcher, workspace = self._launched(tmp_path)

        async def run():
            outcome = await launcher.launch(a_request(workspace))
            assert outcome.target is not None
            pid = outcome.target.pid
            closed = await launcher.close(
                CloseRequest(request_id=new_request_id(), target=outcome.target)
            )
            return closed, pid

        closed, pid = asyncio.run(run())
        assert closed.status is CloseStatus.CLOSED
        with pytest.raises(ProcessLookupError):
            os.kill(pid or 0, 0)

    def test_a_repeated_close_is_a_success_that_touches_nothing(self, tmp_path: Path) -> None:
        launcher, workspace = self._launched(tmp_path)

        async def run():
            outcome = await launcher.launch(a_request(workspace))
            assert outcome.target is not None
            request = CloseRequest(request_id=new_request_id(), target=outcome.target)
            await launcher.close(request)
            return await launcher.close(
                CloseRequest(request_id=new_request_id(), target=outcome.target)
            )

        assert asyncio.run(run()).status is CloseStatus.ALREADY_CLOSED

    def test_an_identity_this_launcher_never_started_is_refused(self, tmp_path: Path) -> None:
        """Fail closed on a stale identity: saying "already closed" would be a lie."""
        launcher, _ = self._launched(tmp_path)
        stranger = SessionTarget(agent=AgentKind.CLAUDE, session_id="never-seen", pid=4242)

        outcome = asyncio.run(
            launcher.close(CloseRequest(request_id=new_request_id(), target=stranger))
        )

        assert outcome.status is CloseStatus.FAILED
        assert "holds no Session" in outcome.detail

    def test_closing_a_session_that_already_exited_is_a_success(self, tmp_path: Path) -> None:
        launcher, workspace = self._launched(tmp_path)

        async def run():
            outcome = await launcher.launch(a_request(workspace))
            assert outcome.target is not None
            held = launcher._live[outcome.target]
            held.console._process.kill()  # the Session died on its own
            await held.console.wait()
            return await launcher.close(
                CloseRequest(request_id=new_request_id(), target=outcome.target)
            )

        assert asyncio.run(run()).status is CloseStatus.ALREADY_CLOSED


# -- the tmux adapter ----------------------------------------------------


class FakeTmux(Tmux):
    """A tmux server stood in for by a shell, which really runs the pane command.

    Running it for real is the point. A fake that only recorded the command would
    prove the launcher *composed* one, and every interesting claim here is about
    what the child actually ends up with — that the Session registers under a
    descendant of the pane, and that the environment it really receives carries
    none of what the server was holding.

    So this fake carries a **dirty environment of its own**: `MallocStackLogging`,
    the variable ADR 0004 measured, and the engine's bot token. It runs the pane
    command as its child, so anything not excluded by `env -i` would be inherited
    exactly the way a real tmux server's would.
    """

    def __init__(self, *, available: bool = True) -> None:
        super().__init__(Path("/usr/bin/true"), session="test")
        self.available = available
        self.commands: list[tuple[str, ...]] = []
        self.windows: dict[str, subprocess.Popen] = {}
        self.killed: list[str] = []
        self.refuse_kill: set[str] = set()
        self.server_environment = {
            **os.environ,
            DIRTY_VARIABLE: "1",
            TOKEN_VARIABLE: "secret-bot-token",
        }
        self._next = 0
        _FAKE_SERVERS.append(self)

    async def is_available(self) -> bool:
        return self.available

    async def open_window(self, name: str, command: str, *, cwd: Path) -> str:
        self.commands.append(("new-window", name, command, str(cwd)))
        self._next += 1
        window = f"@{self._next}"
        self.windows[window] = subprocess.Popen(
            ["/bin/sh", "-c", command],
            cwd=str(cwd),
            env=self.server_environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )
        return window

    async def pane_pid(self, window: str) -> int:
        return self.windows[window].pid

    #: Set to make every liveness query fail the way an unreachable tmux does.
    unreachable = False

    async def is_live(self, window: str) -> bool:
        if self.unreachable:
            raise TmuxError("tmux list-windows -a: no server running on /tmp/tmux-501/default")
        held = self.windows.get(window)
        return held is not None and held.poll() is None

    async def screen(self, window: str) -> str:
        return "what was on screen"

    async def kill_window(self, window: str) -> None:
        if window in self.refuse_kill:
            raise TmuxError(f"tmux kill-window -t {window} failed: server refused")
        self.killed.append(window)
        held = self.windows.pop(window, None)
        if held is not None and held.poll() is None:
            held.kill()
            held.wait(timeout=10)

    def shut_down(self) -> None:
        """End anything still running. A test's own cleanup, not tmux's."""
        for held in self.windows.values():
            if held.poll() is None:
                held.kill()
                held.wait(timeout=10)
        self.windows.clear()


class RecordingTmux(Tmux):
    """A real `Tmux`, with only the subprocess call replaced.

    Used where what is under test is the *command sequence* rather than the
    launcher around it, so the assertions are about what tmux would actually be
    asked to do.
    """

    def __init__(self, *, session_exists: bool) -> None:
        super().__init__(Path("/usr/bin/true"), session="test")
        self.session_exists = session_exists
        self.ran: list[tuple[str, ...]] = []

    async def run(self, *arguments: str) -> str:
        self.ran.append(arguments)
        if arguments[0] == "has-session" and not self.session_exists:
            raise TmuxError("can't find session: test")
        return "@7"


class TestEnsuringTheSession:
    """The engine has no terminal, so nothing here may need one."""

    def test_it_never_asks_tmux_to_attach(self) -> None:
        """`new-session -A` attaches when the session exists, and attaching needs a tty.

        This is a regression test for a bug a real launch found and no unit test
        would have: run by hand from inside tmux it works, and run from a daemon
        — which is the only way this adapter ever really runs — it fails with
        `open terminal failed: not a terminal`.
        """
        tmux = RecordingTmux(session_exists=True)

        asyncio.run(tmux.ensure_session())

        assert [command[0] for command in tmux.ran] == ["has-session"]
        assert not any("-A" in command for command in tmux.ran)

    def test_a_missing_session_is_created_detached(self) -> None:
        tmux = RecordingTmux(session_exists=False)

        asyncio.run(tmux.ensure_session())

        assert [command[0] for command in tmux.ran] == ["has-session", "new-session"]
        assert "-d" in tmux.ran[1]

    def test_losing_the_race_to_create_the_session_is_not_a_failed_launch(self) -> None:
        """Asking and creating are two commands, so two launches can collide.

        Both see the session missing, both try to create it, and the loser is
        told `duplicate session`. What was wanted has happened, so failing that
        launch would be refusing on the strength of somebody else's success.
        """

        class Colliding(RecordingTmux):
            """A tmux where the session appears between the ask and the create."""

            async def run(self, *arguments: str) -> str:
                self.ran.append(arguments)
                if arguments[0] == "has-session":
                    # Missing when first asked; there by the time we re-check.
                    if len([c for c in self.ran if c[0] == "has-session"]) == 1:
                        raise TmuxError("can't find session: test")
                    return ""
                if arguments[0] == "new-session":
                    raise TmuxError("duplicate session: test")
                return "@7"

        tmux = Colliding(session_exists=False)

        asyncio.run(tmux.ensure_session())  # must not raise

        assert [command[0] for command in tmux.ran] == [
            "has-session",
            "new-session",
            "has-session",
        ]

    def test_a_session_that_really_cannot_be_created_still_raises(self) -> None:
        """The re-check must not turn every creation failure into a success."""

        class Broken(RecordingTmux):
            async def run(self, *arguments: str) -> str:
                self.ran.append(arguments)
                raise TmuxError("no server running")

        with pytest.raises(TmuxError):
            asyncio.run(Broken(session_exists=False).ensure_session())


class TestThePaneCommand:
    def test_it_starts_from_an_empty_environment(self) -> None:
        """`env -i`: whatever the tmux server has been carrying reaches nothing."""
        line = pane_command(["/bin/agent"], {"PATH": "/bin"})

        assert line.split()[0].strip("'") == "/usr/bin/env"
        assert "-i" in line.split()

    def test_every_piece_is_quoted(self, tmp_path: Path) -> None:
        """An interpreter path under "Application Support" contains a space.

        Unquoted, that launch fails with 127 and the only symptom is a permission
        dialog nobody ever answers. It has cost this repository a probe once.
        """
        line = pane_command(
            ["/Applications/My App/bin/claude", "--plugin-dir", "/a b/c"],
            {"HOME": "/Users/some one"},
        )

        assert "'/Applications/My App/bin/claude'" in line
        assert "'HOME=/Users/some one'" in line


class TestTheTmuxLauncher:
    def _launcher(self, tmp_path: Path, tmux: FakeTmux, *, binary: Path, registry: Path):
        launcher = TmuxLauncher(settings=settings_for(tmp_path, binary=binary), tmux=tmux)
        launcher.use_claude(FakeClaudeEngine(registry))
        return launcher

    async def _with_short_confirm(self, launcher: TmuxLauncher, request: LaunchRequest):
        """Spend the timeout path without spending the timeout."""
        launcher._confirm = 1.0
        return await launcher.launch(request)

    def test_tmux_that_is_not_there_reports_unavailable_not_failed(self, tmp_path: Path) -> None:
        """Two different facts: this adapter cannot run here, versus a launch failed."""
        registry = tmp_path / "registry"
        workspace = tmp_path / "ws"
        workspace.mkdir()
        tmux = FakeTmux(available=False)
        launcher = self._launcher(tmp_path, tmux, binary=tmp_path / "claude", registry=registry)

        outcome = asyncio.run(launcher.launch(a_request(workspace)))

        assert outcome.status is LaunchStatus.UNAVAILABLE
        assert outcome.target is None

    def test_a_stale_tmux_server_environment_reaches_no_session(self, tmp_path: Path) -> None:
        """Obligation 1, asserted against a server that really carries the variable.

        The pane command is what the tmux server will run, so it is the whole of
        what the child's environment can be. `env -i` means the server's own
        environment — dirty variable, engine's bot token and all — is not part of
        it, whatever that server has been holding since it started.
        """
        registry = tmp_path / "registry"
        workspace = tmp_path / "ws"
        workspace.mkdir()
        tmux = FakeTmux()
        binary = a_fake_claude(tmp_path / "claude", registry, session_id="abc")
        launcher = self._launcher(tmp_path, tmux, binary=binary, registry=registry)

        asyncio.run(launcher.launch(a_request(workspace)))

        assert tmux.commands, "no window was ever opened"
        command = tmux.commands[0][2]
        assert DIRTY_VARIABLE in tmux.server_environment  # the server really has it
        assert DIRTY_VARIABLE not in command
        assert TOKEN_VARIABLE not in command
        assert "-i" in command.split()

    def test_the_launch_carries_the_same_flags_as_the_headless_one(self, tmp_path: Path) -> None:
        """Visibility is the only difference between the two adapters."""
        registry = tmp_path / "registry"
        workspace = tmp_path / "ws"
        workspace.mkdir()
        tmux = FakeTmux()
        binary = a_fake_claude(tmp_path / "claude", registry, session_id="abc")
        launcher = self._launcher(tmp_path, tmux, binary=binary, registry=registry)

        asyncio.run(launcher.launch(a_request(workspace)))

        command = tmux.commands[0][2]
        assert "--permission-mode" in command
        assert "default" in command
        assert command.count("--plugin-dir") == 2
        assert "--channels" in command
        assert "plugin:gpt-voicecoding-session-channel@gpt-voicecoding-channel" in command

    def test_a_launch_that_never_registers_fails_and_kills_its_window(self, tmp_path: Path) -> None:
        """Nothing is left running after a launch that did not happen."""
        registry = tmp_path / "registry"
        registry.mkdir()
        workspace = tmp_path / "ws"
        workspace.mkdir()
        tmux = FakeTmux()
        # A stand-in that starts perfectly well and simply never registers — the
        # shape of a Session stalled on a first-run dialog nobody can see.
        binary = a_script(tmp_path / "silent.py", "import time\ntime.sleep(30)\n")
        launcher = self._launcher(tmp_path, tmux, binary=binary, registry=registry)

        outcome = asyncio.run(self._with_short_confirm(launcher, a_request(workspace)))

        assert outcome.status is LaunchStatus.FAILED
        assert tmux.killed, "the window of a failed launch was left running"
        assert not any(held.poll() is None for held in tmux.windows.values())

    def test_it_verifies_against_the_tmux_it_would_actually_use(self, tmp_path: Path) -> None:
        launcher = TmuxLauncher(
            settings=settings_for(tmp_path, binary=tmp_path / "claude"),
            tmux=FakeTmux(available=False),
        )

        result = asyncio.run(launcher.verify())

        assert result.outcome is VerifyOutcome.FAIL

    def test_shutting_down_leaves_visible_sessions_alone(self, tmp_path: Path) -> None:
        """A human's window is not this engine's to close when it stops."""
        registry = tmp_path / "registry"
        workspace = tmp_path / "ws"
        workspace.mkdir()
        tmux = FakeTmux()
        binary = a_fake_claude(tmp_path / "claude", registry, session_id="abc")
        launcher = self._launcher(tmp_path, tmux, binary=binary, registry=registry)

        async def run():
            outcome = await launcher.launch(a_request(workspace))
            await launcher.aclose()
            return outcome

        outcome = asyncio.run(run())
        # Stated so this cannot pass by never having launched anything.
        assert outcome.status is LaunchStatus.LAUNCHED
        assert tmux.killed == []
        assert any(held.poll() is None for held in tmux.windows.values())


class TestClosingATmuxSession:
    """The two ways a close can quietly stop being true, and neither may happen."""

    def _launched(self, tmp_path: Path):
        registry = tmp_path / "registry"
        workspace = tmp_path / "ws"
        workspace.mkdir()
        tmux = FakeTmux()
        binary = a_fake_claude(tmp_path / "claude", registry, session_id="abc-123")
        launcher = TmuxLauncher(settings=settings_for(tmp_path, binary=binary), tmux=tmux)
        launcher.use_claude(FakeClaudeEngine(registry))
        return launcher, tmux, workspace

    def test_a_tmux_that_cannot_be_asked_fails_rather_than_presuming_the_session_gone(
        self, tmp_path: Path
    ) -> None:
        """ "Cannot tell" is not "already closed".

        A liveness query that fails is a question with no answer. Reading it as
        "the Session exited" is fail-open twice over: the close reports a success
        that did not happen, and the launcher then forgets a Session that is
        still running.
        """
        launcher, tmux, workspace = self._launched(tmp_path)

        async def run():
            outcome = await launcher.launch(a_request(workspace))
            assert outcome.target is not None
            tmux.unreachable = True
            return await launcher.close(
                CloseRequest(request_id=new_request_id(), target=outcome.target)
            )

        closed = asyncio.run(run())
        assert closed.status is CloseStatus.FAILED
        assert "no server running" in closed.detail

    def test_a_partial_close_is_not_forgotten_and_may_be_retried(self, tmp_path: Path) -> None:
        """A window that went while its app-server survived is still ours to answer for.

        Recording it as closed would make the very next close a cheerful
        `already_closed` about a process that is still running — which is the
        exact shape of untruth the seam's close semantics forbid.
        """
        launcher, tmux, workspace = self._launched(tmp_path)

        # The **real** app-server host, over a real window the fake tmux refuses
        # to kill. A stand-in that simply always reported failure would pass
        # whatever the host's own state machine did, and the bug this guards
        # against lives in exactly that state machine: letting go of the window
        # identity before the kill is known to have worked.
        class Preparation:
            def __init__(self, host):
                self.host = host

            async def discard(self):
                return await self.host.close()

        async def run():
            outcome = await launcher.launch(a_request(workspace))
            assert outcome.target is not None
            host = _TmuxAppServer(tmux, name="app-server-test")
            await host.start(["/bin/sh", "-c", "sleep 30"], env={"PATH": "/bin"}, cwd=workspace)
            tmux.refuse_kill.add(host._window)
            launcher._live[outcome.target].preparation = Preparation(host)

            first = await launcher.close(
                CloseRequest(request_id=new_request_id(), target=outcome.target)
            )
            second = await launcher.close(
                CloseRequest(request_id=new_request_id(), target=outcome.target)
            )
            return first, second

        first, second = asyncio.run(run())
        assert first.status is CloseStatus.FAILED
        assert [child.closed for child in first.children] == [False]
        # The repeat must neither claim success nor lose sight of the child: the
        # app-server is still running, so the second outcome has to say so too.
        assert second.status is CloseStatus.FAILED
        assert [child.closed for child in second.children] == [False]


class TestOneLaunchPerIdentity:
    """The seam's first promise, under the concurrency that actually breaks it."""

    @pytest.mark.parametrize("adapter", ["direct-child", "tmux"])
    def test_two_overlapping_requests_with_one_identity_start_one_child(
        self, tmp_path: Path, adapter: str
    ) -> None:
        """Both callers arrive before either finishes, which a result cache cannot catch.

        Checking a cache of *finished* launches and then awaiting is not enough:
        with two concurrent calls nothing has finished yet, so both find it empty
        and both start a Session. The identity has to be claimed before the first
        await.
        """
        registry = tmp_path / "registry"
        workspace = tmp_path / "ws"
        workspace.mkdir()
        binary = a_fake_claude(tmp_path / "claude", registry, session_id="abc-123")
        settings = settings_for(tmp_path, binary=binary)
        if adapter == "tmux":
            launcher = TmuxLauncher(settings=settings, tmux=FakeTmux())
        else:
            launcher = DirectChildLauncher(settings=settings)
        launcher.use_claude(FakeClaudeEngine(registry))

        async def run():
            request = a_request(workspace)
            first, second = await asyncio.gather(launcher.launch(request), launcher.launch(request))
            started = len(list(registry.glob("*.json")))
            await launcher.aclose()
            return first, second, started

        first, second, started = asyncio.run(run())
        assert first is second, "two overlapping requests produced two outcomes"
        assert started == 1, f"one request identity started {started} Sessions"


class TestSettings:
    def test_an_unknown_key_refuses_to_start(self) -> None:
        with pytest.raises(SettingsError, match="does not have"):
            LauncherSettings.of({"permission_mode": "bypassPermissions"})

    def test_permission_mode_is_not_a_setting(self) -> None:
        """The decision not to pre-approve is not the operator's to dial.

        Whatever a launch waves through, the Approval Relay never sees. This is
        asserted rather than left implicit because the natural next change to
        this file is somebody adding the key.
        """
        assert "permission_mode" not in {field for field in LauncherSettings.__dataclass_fields__}

    def test_registry_directory_belongs_to_the_claude_agent(self) -> None:
        with pytest.raises(SettingsError, match="does not have"):
            LauncherSettings.of({"registry_directory": "/tmp/other-registry"})

    def test_a_binary_that_is_named_but_absent_is_refused_by_name(self, tmp_path: Path) -> None:
        settings = LauncherSettings(claude_binary=tmp_path / "nowhere")

        with pytest.raises(SettingsError, match="nowhere"):
            settings.binary_for(AgentKind.CLAUDE)


class TestBothAdaptersMeetTheSeam:
    """Whatever else differs, the two answer the same verbs the same way."""

    @pytest.mark.parametrize("build", [DirectChildLauncher, TmuxLauncher])
    def test_a_stale_identity_fails_closed_on_either_adapter(self, build) -> None:
        launcher = build()
        stranger = SessionTarget(agent=AgentKind.CODEX, session_id="never-seen")

        outcome = asyncio.run(
            launcher.close(CloseRequest(request_id=RequestId("r-1"), target=stranger))
        )

        assert outcome.status is CloseStatus.FAILED
        assert outcome.detail


# -- the Codex side ------------------------------------------------------


class FakeAppServerHost:
    """Stands in for wherever the per-TUI app-server runs, and really binds one.

    The preparation decides the socket path and then waits for something to
    listen on it, so a host that only recorded the argv would never get past that
    wait. This one starts the repository's own scripted app-server on exactly the
    path the argv names, which is what lets the notification half be driven.
    """

    def __init__(self) -> None:
        self.started: tuple[str, ...] = ()
        self.environment: dict[str, str] = {}
        self.cwd: Path | None = None
        self.server: FakeAppServer | None = None
        self.closed = False

    async def start(self, argv, *, env, cwd: Path) -> None:
        self.started = tuple(argv)
        self.environment = dict(env)
        self.cwd = cwd
        path = Path(argv[-1].removeprefix("unix://"))
        self.server = await FakeAppServer(path=path).start()

    async def close(self) -> tuple[ChildOutcome, ...]:
        self.closed = True
        if self.server is not None:
            await self.server.aclose()
        return (ChildOutcome(ref="app-server:fake", closed=True),)


def a_codex_request(workspace: Path, **extra) -> LaunchRequest:
    return LaunchRequest(
        request_id=new_request_id(),
        agent=AgentKind.CODEX,
        workspace=workspace,
        label=LABEL,
        **extra,
    )


def a_thread(cwd: Path, *, thread_id: str = "01a0-thread") -> dict:
    """A `thread/started` payload shaped like the one codex 0.149.0 really emits."""
    return {"thread": {"id": thread_id, "cwd": str(cwd), "status": {"type": "idle"}, "turns": []}}


class TestPreparingACodexSession:
    def _preparation(self, tmp_path: Path, workspace: Path, host: FakeAppServerHost):
        return CodexPreparation(
            a_codex_request(workspace),
            settings=settings_for(tmp_path, binary=Path("/bin/sh")),
            host=host,
            engine=FakeCodexEngine(),
            confirm_timeout_seconds=3.0,
        )

    def test_the_tui_is_pointed_at_the_workspace_twice_over(self, tmp_path: Path) -> None:
        """`-C` decides the thread's working root; the process cwd decides the banner.

        Both, measured on codex 0.149.0: setting only the app-server's directory
        leaves the thread somewhere nobody chose, and setting only `-C` leaves a
        Session whose displayed directory disagrees with the one it works in.
        """
        workspace = tmp_path / "ws"
        workspace.mkdir()
        host = FakeAppServerHost()
        preparation = self._preparation(tmp_path, workspace, host)

        async def run():
            plan = await preparation.prepare()
            await preparation.discard()
            return plan

        plan = asyncio.run(run())
        assert "-C" in plan.argv
        assert str(workspace) in plan.argv
        assert "--remote" in plan.argv
        assert plan.cwd == workspace
        # And the app-server itself was started in the workspace, so a thread
        # that somehow arrived without `-C` would still not be somewhere random.
        assert host.cwd == workspace

    def test_the_announced_thread_becomes_the_target(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        host = FakeAppServerHost()
        preparation = self._preparation(tmp_path, workspace, host)

        async def run():
            await preparation.prepare()
            assert host.server is not None
            await host.server.notify_all("thread/started", a_thread(workspace))
            target = await preparation.confirm(ancestor=1)
            await preparation.discard()
            return target

        target = asyncio.run(run())
        assert target.agent is AgentKind.CODEX
        assert target.session_id == "01a0-thread"
        # A Codex target carries no pid: a thread is not a process.
        assert target.pid is None

    def test_a_thread_in_the_wrong_directory_fails_the_launch(self, tmp_path: Path) -> None:
        """Obligation 6, and the half that actually bites.

        Setting the workspace is not the same as the Session being in it. A
        launch that registered this would tell Bridge Core the agent is reading
        and writing somewhere it is not.
        """
        workspace = tmp_path / "ws"
        workspace.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        host = FakeAppServerHost()
        preparation = self._preparation(tmp_path, workspace, host)

        async def run():
            await preparation.prepare()
            assert host.server is not None
            await host.server.notify_all("thread/started", a_thread(elsewhere))
            try:
                await preparation.confirm(ancestor=1)
            finally:
                await preparation.discard()

        with pytest.raises(PreparationError, match="not in the workspace"):
            asyncio.run(run())

    def test_a_workspace_reached_through_a_symlink_still_matches(self, tmp_path: Path) -> None:
        """The same `/tmp` versus `/private/tmp` trap the Claude side has."""
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)
        host = FakeAppServerHost()
        preparation = self._preparation(tmp_path, link, host)

        async def run():
            await preparation.prepare()
            assert host.server is not None
            await host.server.notify_all("thread/started", a_thread(real))
            target = await preparation.confirm(ancestor=1)
            await preparation.discard()
            return target

        assert asyncio.run(run()).session_id == "01a0-thread"

    def test_two_threads_are_refused_rather_than_chosen(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        host = FakeAppServerHost()
        preparation = self._preparation(tmp_path, workspace, host)

        async def run():
            await preparation.prepare()
            assert host.server is not None
            await host.server.notify_all("thread/started", a_thread(workspace, thread_id="one"))
            await host.server.notify_all("thread/started", a_thread(workspace, thread_id="two"))
            await asyncio.sleep(0.3)
            try:
                await preparation.confirm(ancestor=1)
            finally:
                await preparation.discard()

        with pytest.raises(PreparationError, match="cannot be told"):
            asyncio.run(run())

    def test_a_tui_that_never_speaks_says_what_is_most_likely_holding_it(
        self, tmp_path: Path
    ) -> None:
        """The trust dialog, named — because a headless Session cannot answer one.

        Measured: codex shows a blocking directory-trust prompt for any directory
        it has not seen, trust is recorded per exact directory and is not
        inherited from a parent, and a Session sitting on that prompt announces
        no thread at all. The launch fails truthfully and the message is
        actionable rather than merely correct.
        """
        workspace = tmp_path / "ws"
        workspace.mkdir()
        host = FakeAppServerHost()
        preparation = self._preparation(tmp_path, workspace, host)

        async def run():
            await preparation.prepare()
            try:
                await preparation.confirm(ancestor=1)
            finally:
                await preparation.discard()

        with pytest.raises(PreparationError, match="directory-trust"):
            asyncio.run(run())

    def test_a_tui_that_is_already_gone_is_said_so(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        host = FakeAppServerHost()
        preparation = self._preparation(tmp_path, workspace, host)

        async def run():
            await preparation.prepare()
            try:
                await preparation.confirm(ancestor=1, still_running=lambda: False)
            finally:
                await preparation.discard()

        with pytest.raises(PreparationError, match="already gone"):
            asyncio.run(run())

    def test_the_launch_pre_approves_nothing(self, tmp_path: Path) -> None:
        """No `approvalPolicy` is chosen for the user, on this side either.

        Whatever a launch waves through, the Approval Relay never sees. This
        launcher starts no thread of its own and sends no policy, so the user's
        configured one is what the Session runs under.
        """
        workspace = tmp_path / "ws"
        workspace.mkdir()
        host = FakeAppServerHost()
        preparation = self._preparation(tmp_path, workspace, host)

        async def run():
            await preparation.prepare()
            assert host.server is not None
            sent = list(host.server.calls)
            await preparation.discard()
            return sent

        sent = asyncio.run(run())
        assert [call.method for call in sent if call.method != "initialize"] == []
        assert "--ask-for-approval" not in host.started
        assert "--dangerously-bypass-approvals-and-sandbox" not in host.started

    def test_the_app_servers_environment_is_the_launchers_own(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        host = FakeAppServerHost()
        preparation = self._preparation(tmp_path, workspace, host)
        os.environ[TOKEN_VARIABLE] = "secret-bot-token"
        try:

            async def run():
                await preparation.prepare()
                await preparation.discard()

            asyncio.run(run())
        finally:
            del os.environ[TOKEN_VARIABLE]

        assert TOKEN_VARIABLE not in host.environment
        assert DIRTY_VARIABLE not in host.environment
        assert "TERM" in host.environment

    def test_discarding_reports_the_child_it_really_owns(self, tmp_path: Path) -> None:
        """`children` is not empty here, because this adapter really owns one."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        host = FakeAppServerHost()
        preparation = self._preparation(tmp_path, workspace, host)

        async def run():
            await preparation.prepare()
            return await preparation.discard()

        children = asyncio.run(run())
        assert host.closed
        assert [child.ref for child in children] == ["app-server:fake"]
        assert all(child.closed for child in children)

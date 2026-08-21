"""Starting a Codex Session: an app-server, a TUI that is its client, and a readback.

A Codex TUI is a thin client of an app-server (`codex --remote unix://PATH`), so
a launch is two processes rather than one, and who owns the first of them decides
what an engine restart does to the user's Session. That is settled by ADR 0008
along the same line as visibility: the tmux adapter hands the app-server to the
tmux server, which outlives this engine; the direct-child adapter keeps it,
because a headless Session whose pseudo-terminal died with the engine is not a
Session anybody can still use. This module is the part that is the same either
way, and it takes the hosting as a parameter.

**How the thread's identity and workspace are learned.** Not by assumption, and
not by waiting for a human:

- `codex --remote` announces its thread with a `thread/started` notification
  **at startup, before anyone types**. Measured against codex 0.149.0 with a real
  TUI on a real tty and no keystrokes sent. Older notes in this repository said a
  Session had to do something before it could be observed; that stopped being
  true and the note has been corrected.
- That notification carries the thread's `cwd`, so obligation 6's "into this
  workspace" is **verified** rather than intended. A thread that comes back in
  the wrong directory fails the launch.

**How the workspace is set.** `-C <dir>` on the TUI, *and* the TUI's own process
directory. Measured, again on 0.149.0: `-C` overrides the app-server's cwd for
the thread, which is what the agent actually reads and writes; the TUI's own
process directory is what its banner shows. Setting only one of them leaves a
Session whose displayed directory and working directory disagree.

**No `approvalPolicy` is sent.** Whatever a launch pre-approves, the Approval
Relay never sees. The user's own configured policy is left exactly as it is; see
`plan.PERMISSION_MODE` for the same decision on the Claude side.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from gpt_voicecoding.adapters.agent.claude.privacy import verify_bindable_length
from gpt_voicecoding.adapters.codex_app_server.process import attach, prepare_private_directory
from gpt_voicecoding.adapters.codex_app_server.settings import CodexSettings
from gpt_voicecoding.adapters.session_launcher.environment import child_environment
from gpt_voicecoding.adapters.session_launcher.plan import (
    CONFIRM_TIMEOUT_SECONDS,
    Launch,
    PreparationError,
    workspace_of,
)
from gpt_voicecoding.adapters.session_launcher.settings import LauncherSettings, SettingsError
from gpt_voicecoding.seams.identity import AgentKind, SessionTarget
from gpt_voicecoding.seams.session_launcher import ChildOutcome, LaunchRequest

#: What one launch's private runtime directory is called, and what the per-TUI
#: app-server listens on inside it.
LAUNCH_DIRECTORY_PREFIX = "vc-codex-"
APP_SERVER_SOCKET_NAME = "app-server.sock"

#: How long the app-server is given to bind its socket. It is a process start,
#: not a wire, and a named constant rather than a settings key for the reason
#: `plan.CONFIRM_TIMEOUT_SECONDS` states.
APP_SERVER_TIMEOUT_SECONDS = 30.0

#: How often the socket is looked for while waiting for it.
POLL_SECONDS = 0.2

#: What this launch calls itself when it attaches to watch for the thread.
WATCHER_VERSION = "session-launcher"


class AppServerHost(Protocol):
    """Where the per-TUI app-server runs, and who reaps it.

    The two implementations differ in exactly one thing that matters: whether the
    process outlives this engine. See ADR 0008.
    """

    async def start(self, argv: Sequence[str], *, env: Mapping[str, str], cwd: Path) -> None: ...

    async def close(self) -> tuple[ChildOutcome, ...]:
        """End it if this host owns it, and report only what it actually owns."""
        ...


class CodexEngineFacts(Protocol):
    """The one thing only the running Codex Agent adapter can be told."""

    async def register_session(self, target: SessionTarget, socket_path: Path) -> None: ...


class CodexPreparation:
    """One Codex Session: an app-server, then the TUI that is its only client."""

    def __init__(
        self,
        request: LaunchRequest,
        *,
        settings: LauncherSettings,
        host: AppServerHost,
        engine: CodexEngineFacts | None = None,
        confirm_timeout_seconds: float = CONFIRM_TIMEOUT_SECONDS,
    ) -> None:
        self._request = request
        self._settings = settings
        self._host = host
        self._engine = engine
        self._timeout = confirm_timeout_seconds
        self._directory = (
            settings.runtime_directory / f"{LAUNCH_DIRECTORY_PREFIX}{request.request_id}"
        )
        self._socket = self._directory / APP_SERVER_SOCKET_NAME
        self._watcher: Any = None
        self._started: list[dict[str, Any]] = []

    @property
    def app_server_socket_path(self) -> Path:
        """Where this launch's app-server listens. The Agent adapter is told this."""
        return self._socket

    async def prepare(self) -> Launch:
        """Start the app-server, watch it for a thread, and answer with the TUI."""
        workspace = workspace_of(self._request)
        try:
            binary = self._settings.binary_for(AgentKind.CODEX)
        except SettingsError as unrunnable:
            raise PreparationError(str(unrunnable)) from None

        verify_bindable_length(self._socket)
        prepare_private_directory(self._directory)
        environment = child_environment(
            self._request.env, terminal_type=self._settings.terminal_type
        )
        await self._host.start(
            (str(binary), "app-server", "--listen", f"unix://{self._socket}"),
            env=environment,
            cwd=workspace,
        )
        await self._await_socket()
        await self._watch()

        return Launch(
            # `-C` decides the thread's working root; the process directory below
            # decides what the TUI's own banner shows. Both, so they agree.
            argv=(str(binary), "--remote", f"unix://{self._socket}", "-C", str(workspace)),
            env=environment,
            cwd=workspace,
        )

    async def confirm(
        self, *, ancestor: int, still_running: Callable[[], bool] | None = None
    ) -> SessionTarget:
        """Wait for the thread this launch's TUI announced, and check where it is."""
        thread = await self._await_thread(still_running)
        session_id = thread.get("id")
        if not isinstance(session_id, str) or not session_id.strip():
            raise PreparationError(f"the app-server announced a thread with no id: {thread!r}")

        wanted = _real(workspace_of(self._request))
        reported = thread.get("cwd")
        if not isinstance(reported, str) or _real(Path(reported)) != wanted:
            raise PreparationError(
                f"the Session started in {reported!r}, not in the workspace this launch "
                f"asked for ({wanted}); registering it would tell Bridge Core it is "
                "somewhere it is not"
            )

        target = SessionTarget(agent=AgentKind.CODEX, session_id=session_id)
        if self._engine is not None:
            await self._engine.register_session(target, self._socket)
        return target

    async def discard(self) -> tuple[ChildOutcome, ...]:
        """Stop watching, and let the host say what it actually took down."""
        watcher, self._watcher = self._watcher, None
        if watcher is not None:
            with contextlib.suppress(Exception):
                await watcher.aclose()
        return await self._host.close()

    # -- the two bounded waits -------------------------------------------

    async def _await_socket(self) -> None:
        if not await _until(self._socket.is_socket, APP_SERVER_TIMEOUT_SECONDS):
            raise PreparationError(
                f"the app-server this launch started never bound {self._socket} within "
                f"{APP_SERVER_TIMEOUT_SECONDS:.0f}s"
            )

    async def _watch(self) -> None:
        """Attach as one more client, only to hear the thread announce itself."""
        try:
            self._watcher = await attach(
                self._socket,
                version=WATCHER_VERSION,
                settings=CodexSettings(),
                on_notification=self._heard,
            )
        except Exception as unreachable:
            raise PreparationError(
                f"the app-server this launch started could not be reached at "
                f"{self._socket}: {unreachable}"
            ) from None

    def _heard(self, message: Mapping[str, Any]) -> None:
        if message.get("method") != "thread/started":
            return
        thread = (message.get("params") or {}).get("thread")
        if isinstance(thread, dict):
            self._started.append(thread)

    async def _await_thread(
        self, still_running: Callable[[], bool] | None
    ) -> Mapping[str, Any]:
        """Wait, bounded, for exactly one thread — and refuse if two appear.

        Two is a refusal for the same reason claiming a Claude record is: this
        launch started one TUI, so a second thread on this app-server is
        something this launch cannot account for, and picking between them is how
        a launch comes to register a Session it did not start.
        """
        gone = False

        def settled() -> bool:
            nonlocal gone
            if self._started:
                return True
            if still_running is not None and not still_running():
                gone = True
                return True
            return False

        await _until(settled, self._timeout)
        if len(self._started) > 1:
            named = ", ".join(str(thread.get("id")) for thread in self._started)
            raise PreparationError(
                f"{len(self._started)} threads started on this launch's app-server, so which "
                f"one it started cannot be told: {named}"
            )
        if self._started:
            return self._started[0]
        if gone:
            raise PreparationError(
                "the TUI this launch started is already gone, and it announced no thread"
            )
        raise PreparationError(
            f"the Session this launch started announced no thread within {self._timeout:.0f}s. "
            f"If codex has not seen {workspace_of(self._request)} before, its directory-trust "
            "dialog is the most likely thing holding it, and a headless Session cannot "
            "answer one: open that workspace once in a visible session, or trust it in the "
            "codex configuration."
        )


async def _until(condition: Callable[[], bool], seconds: float) -> bool:
    """Poll one condition until it holds or the budget is spent."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while True:
        if condition():
            return True
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(POLL_SECONDS)


def _real(path: Path) -> Path:
    """Symlinks resolved on both sides: `/tmp` and `/private/tmp` are one place."""
    return Path(os.path.realpath(path))

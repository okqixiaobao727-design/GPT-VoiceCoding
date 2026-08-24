"""Starting a Claude Session: two inline plugins, one selector, one environment, one readback.

Everything the three Relay routes need from a launch arrives here, and each route
fails independently and fails open:

- **`--plugin-dir <rendered hook plugin>`** installs the `PermissionRequest` hook
  for this Session and no other (ADR 0007). A Session launched without it has no
  hook to fire, which is silent and leaves the dialog with its human.
- **`--plugin-dir <rendered channel plugin>` plus `--channels <selector>`** loads
  and selects the Session Channel for this Session (ADR 0007), once the
  administrator-owned managed settings admit that plugin identity. Without any
  one of those three, the channel cannot carry words into the Session.
- **the bootstrap variable**, carrying where this launch's Session Channel should
  listen and where this engine parks dialogs. A Session launched without it gets
  a channel server that cannot bind and a hook that exits without printing.

Neither inline plugin is installed into a user settings file — their paths and
the selector are per-invocation and the addresses are per-launch — so this
engine still touches nothing a user owns. The administrator-managed channel
allowlist remains a deployment precondition; the engine does not own or edit it.

**The binary is exec'd by absolute path.** This is not defensive style: the
machine this was built on defines a `claude` shell function that rewrites the
invocation into a different wrapper, and the only symptom of the redirect was an
unexpected channel name in a session banner. The launch succeeds, the Session
registers, and only the Relay behaves as though nothing were installed.
"""

from __future__ import annotations

import contextlib
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from gpt_voicecoding.adapters.agent.claude.bootstrap import CHANNEL_CONFIG_VARIABLE
from gpt_voicecoding.adapters.agent.claude.hook_plugin import (
    remove_hook_plugin,
    write_hook_plugin,
)
from gpt_voicecoding.adapters.agent.claude.plugin import channel_selector, write_plugin
from gpt_voicecoding.adapters.agent.claude.privacy import (
    prepare_private_directory,
    verify_bindable_length,
)
from gpt_voicecoding.adapters.session_launcher.claiming import ClaimError, claim, snapshot
from gpt_voicecoding.adapters.session_launcher.environment import child_environment
from gpt_voicecoding.adapters.session_launcher.plan import (
    CONFIRM_TIMEOUT_SECONDS,
    PERMISSION_MODE,
    Launch,
    PreparationError,
    workspace_of,
)
from gpt_voicecoding.adapters.session_launcher.settings import LauncherSettings, SettingsError
from gpt_voicecoding.seams.identity import AgentKind, SessionTarget
from gpt_voicecoding.seams.session_launcher import ChildOutcome, LaunchRequest

#: What one launch's private runtime directory is called. The request id rather
#: than a counter, so the directory a launch owns is named after the launch and
#: two attempts can never collide.
LAUNCH_DIRECTORY_PREFIX = "vc-launch-"

#: Where this launch's Session Channel is told to listen, inside that directory.
CHANNEL_SOCKET_NAME = "channel.sock"

#: Where the session-scoped hook plugin is rendered, inside that directory.
HOOK_PLUGIN_DIRECTORY = "hook-plugin"

#: Where the session-scoped channel plugin is rendered, inside that directory.
CHANNEL_PLUGIN_DIRECTORY = "channel-plugin"


class ClaudeEngineFacts(Protocol):
    """The two things only the running Claude Agent adapter can answer.

    A Protocol rather than the adapter class, so this module depends on the two
    questions it actually asks rather than on the whole spoke — and so a test can
    answer them without constructing one.
    """

    def launch_bootstrap(self, channel_socket_path: Path) -> str: ...

    def register_session(self, target: SessionTarget, socket_path: Path) -> None: ...


class ClaudePreparation:
    """One Claude Session, from a request to the exact identity it registered as."""

    def __init__(
        self,
        request: LaunchRequest,
        *,
        settings: LauncherSettings,
        engine: ClaudeEngineFacts,
        confirm_timeout_seconds: float = CONFIRM_TIMEOUT_SECONDS,
    ) -> None:
        self._request = request
        self._settings = settings
        self._engine = engine
        self._timeout = confirm_timeout_seconds
        self._directory = (
            settings.runtime_directory / f"{LAUNCH_DIRECTORY_PREFIX}{request.request_id}"
        )
        self._channel_socket = self._directory / CHANNEL_SOCKET_NAME
        self._plugin_directory = self._directory / HOOK_PLUGIN_DIRECTORY
        self._channel_plugin_directory = self._directory / CHANNEL_PLUGIN_DIRECTORY
        #: Taken before the spawn, so claiming can tell a Session this launch
        #: started from one that was already there. See `claiming`.
        self._before: frozenset[int] = frozenset()

    @property
    def channel_socket_path(self) -> Path:
        """Where this launch told its Session Channel to listen."""
        return self._channel_socket

    async def prepare(self) -> Launch:
        """Render both plugins, mint the addresses, and build the invocation."""
        workspace = workspace_of(self._request)
        try:
            binary = self._settings.binary_for(AgentKind.CLAUDE)
        except SettingsError as unrunnable:
            raise PreparationError(str(unrunnable)) from None

        # Checked before anything is written: a socket path too long to bind is
        # a launch that will look fine and produce a channel nobody can reach,
        # and the length is knowable now.
        verify_bindable_length(self._channel_socket)
        prepare_private_directory(self._directory)
        write_hook_plugin(self._plugin_directory, self._settings.interpreter)
        write_plugin(self._channel_plugin_directory, self._settings.interpreter)

        self._before = snapshot(self._settings.registry_directory)
        return Launch(
            argv=(
                str(binary),
                "--permission-mode",
                PERMISSION_MODE,
                "--plugin-dir",
                str(self._plugin_directory),
                "--plugin-dir",
                str(self._channel_plugin_directory),
                "--channels",
                channel_selector(),
            ),
            env=child_environment(
                {
                    CHANNEL_CONFIG_VARIABLE: self._engine.launch_bootstrap(self._channel_socket),
                    **self._request.env,
                },
                terminal_type=self._settings.terminal_type,
            ),
            cwd=workspace,
        )

    async def confirm(
        self, *, ancestor: int, still_running: Callable[[], bool] | None = None
    ) -> SessionTarget:
        """Claim the record this launch's Session wrote, and check what it says.

        The workspace readback is the check that makes "into this workspace"
        true rather than intended. It is compared through `realpath` inside
        `claiming`, so a launch into `/tmp/x` is not failed for registering as
        `/private/tmp/x`.
        """
        try:
            record = await claim(
                self._settings.registry_directory,
                workspace=workspace_of(self._request),
                before=self._before,
                ancestor=ancestor,
                timeout_seconds=self._timeout,
                still_running=still_running,
            )
        except ClaimError as unclaimable:
            raise PreparationError(str(unclaimable)) from None

        target = SessionTarget(agent=AgentKind.CLAUDE, session_id=record.session_id, pid=record.pid)
        # The channel's address is the one thing the Agent adapter cannot
        # discover: Claude Code spawns that server from an environment variable
        # this launch generated, so only this launch knows where it listens.
        self._engine.register_session(target, self._channel_socket)
        return target

    async def discard(self) -> tuple[ChildOutcome, ...]:
        """Remove the hook's files, then the owned launch directory and channel plugin."""
        with contextlib.suppress(OSError):
            remove_hook_plugin(self._plugin_directory)
        with contextlib.suppress(OSError):
            shutil.rmtree(self._directory, ignore_errors=True)
        return ()

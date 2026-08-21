"""What to start, and who it turned out to be — per agent, and per launch.

A launch has two halves that vary for different reasons, so they are two things
here rather than one:

- **`Launch`** is what to start: an argv, an environment, a working directory.
  It is all any process starter needs, which is what lets the direct-child
  adapter and the tmux adapter share every agent-shaped decision and differ only
  in how a process comes into being.
- **`Preparation`** is the agent-shaped part around it: what has to exist before
  the argv is meaningful, and how the started process is turned into the exact
  `SessionTarget` Bridge Core will register.

**Confirmation is a readback, never an assumption.** Both agents report their own
working directory — Claude Code in its registry record, Codex in the
`thread/started` it emits — and a launch is truthful about "into this workspace"
only if that readback is checked. A mismatch fails the launch rather than
registering a Session that Bridge Core believes is somewhere it is not.

**No pre-approving flag is ever passed, on either side.** Claude Sessions are
launched with `--permission-mode default`, which is the mode that stops and asks;
Codex threads are started without an `approvalPolicy`, leaving the user's own.
This is one decision with one reason: whatever the launcher waves through, the
Approval Relay never sees. It is stated rather than inherited, because a default
that moves would move this with it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from gpt_voicecoding.seams.identity import SessionTarget
from gpt_voicecoding.seams.session_launcher import ChildOutcome, LaunchRequest

#: The Claude Code permission mode a Session is launched in. `default` is the
#: mode that displays a dialog and waits; `auto` and `bypassPermissions`
#: pre-approve, and a Session in one of those raises no `AwaitingApproval`
#: because there is nothing to stall — the hook only ever sees what would have
#: stopped for a human. Stated here rather than left to Claude Code's own
#: default, because inheriting it would mean this system's Approval Relay
#: quietly stops existing the day that default changes.
PERMISSION_MODE = "default"

#: How long a launch waits for the Session it started to say who it is. A named
#: constant rather than a settings key: there is no use case for dialling it, and
#: a configuration key with no use case is a parameter nobody maintains. It is
#: generous because what is being waited for is a TUI's whole startup.
CONFIRM_TIMEOUT_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class Launch:
    """One process to start. Everything a starter needs, and nothing about how."""

    argv: tuple[str, ...]
    env: Mapping[str, str]
    cwd: Path


class Preparation(Protocol):
    """One agent's launch, from "nothing yet" to "this exact Session".

    The three verbs are in the order a launch uses them, and `discard` is the one
    that keeps a failure from leaving debris: whatever `prepare` created — a
    rendered plugin directory, an app-server — is the preparation's to take back
    when the launch does not complete.
    """

    async def prepare(self) -> Launch:
        """Build whatever the argv depends on, and answer with what to start."""
        ...

    async def confirm(self, *, ancestor: int, still_running: object) -> SessionTarget:
        """Read back who actually started, or raise saying why it cannot be told."""
        ...

    async def discard(self) -> tuple[ChildOutcome, ...]:
        """Take back what `prepare` created. Reports only destinations it owns."""
        ...


class PreparationError(Exception):
    """A launch could not be prepared, or could not be confirmed."""


def workspace_of(request: LaunchRequest) -> Path:
    """The workspace a request names, as an absolute path.

    A relative workspace would be resolved against whatever directory the engine
    happens to be running in, which is exactly the silent-inheritance failure
    obligation 6 records: the Session would run somewhere nobody chose.
    """
    workspace = Path(request.workspace)
    if not workspace.is_absolute():
        raise PreparationError(
            f"a workspace must be an absolute path; {workspace} would be resolved against "
            "whatever directory this engine happens to be running in"
        )
    if not workspace.is_dir():
        raise PreparationError(f"no workspace at {workspace}")
    return workspace

"""What generation is told, and the things it refuses to invent.

The CLI location, the engine it reaches and the parser's current launch form are
knowable outside instruction generation. The composition root therefore hands
them in rather than letting prose remember or rediscover them. Bridge Core reads
no file and probes no filesystem.

The refusal matters more than the plumbing. A generated instruction that names
a CLI which is not there is an invented detail, and inventing detail is the
first thing the catalogue's own rules forbid. So there is no fallback string:
without a real invocation there are no delegated instructions.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path

from gpt_voicecoding.core.instructions.blocks import InstructionError


@dataclass(frozen=True, slots=True)
class ControlPlaneCli:
    """The control-plane surface a generated thread is told to act through."""

    #: Where the executable really is on this machine.
    command: Path
    #: The engine's own version, so a thread and its engine can be told apart.
    version: str
    #: Which engine to talk to. Passed explicitly, because a thread that read
    #: the configuration file itself could reach a different engine than the one
    #: that generated its instructions.
    socket_path: Path

    def __post_init__(self) -> None:
        if not self.command.is_absolute():
            raise InstructionError(
                f"the control-plane CLI must be named by where it really is; {str(self.command)!r} "
                "is somewhere only whoever ran it could resolve"
            )
        if not self.version.strip():
            raise InstructionError("the control-plane CLI must carry the engine's version")
        if not self.socket_path.is_absolute():
            raise InstructionError(
                f"the engine's socket must be an absolute path; {str(self.socket_path)!r} is not"
            )

    @property
    def invocation(self) -> str:
        """The command line, quoted so a path with spaces survives a shell."""
        return f"{shlex.quote(str(self.command))} --socket {shlex.quote(str(self.socket_path))}"


@dataclass(frozen=True, slots=True)
class InstructionContext:
    """Everything generation is parameterised by. Handed in, never discovered."""

    cli: ControlPlaneCli
    launch_usage: str

    def __post_init__(self) -> None:
        if not self.launch_usage.strip():
            raise InstructionError("generated instructions need the parser's launch usage")

    @property
    def launch_invocation(self) -> str:
        """The real CLI invocation followed by the parser-owned launch form."""
        return f"{self.cli.invocation} {self.launch_usage.strip()}"

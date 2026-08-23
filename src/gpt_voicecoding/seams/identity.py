"""Who a Relay is addressed to, and what correlates one attempt across routes.

Three locked rules are enforced here by shape rather than by discipline:

- **A label is not a target.** `SessionLabel` is for matching and for speech; it
  has no session id and no pid, so it cannot be passed where a command expects
  one. Commands carry a `SessionTarget`.
- **Claude Sessions are addressed by pid.** `--resume` forks a second process
  under the same session id, so a Claude target without a pid is ambiguous and
  is refused at construction.
- **`request_id` is one sender-minted UUID**, reused across every delivery of
  the same intent: Claude sends it as both `uuid` and `msg_id`, Codex as
  `clientUserMessageId`, and launch callers carry it through the control plane.
  It stays a plain string precisely so every route can carry it unchanged.
  Bridge Core and adapters map or bind it; they never replace it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import NewType

#: The correlation id for one request intent, minted once by its sender.
RequestId = NewType("RequestId", str)

#: What separates the two halves of a Session Label when it is rendered.
LABEL_SEPARATOR = " · "


def new_request_id() -> RequestId:
    """Mint the one id a sender carries across every delivery of an intent."""
    return RequestId(str(uuid.uuid4()))


class AgentKind(StrEnum):
    """The terminal coding agents this system launches, watches and Relays into."""

    CLAUDE = "claude"
    CODEX = "codex"

    @property
    def addressed_by_pid(self) -> bool:
        """Whether a session id alone is too weak to name one process.

        True for Claude: a resumed session forks a second process under the same
        session id, so the pid is what distinguishes them.
        """
        return self is AgentKind.CLAUDE


@dataclass(frozen=True, slots=True)
class SessionLabel:
    """``<git project name> · <task title>`` — for matching and for speech only.

    Deliberately holds nothing a command could address: see this module's
    docstring.
    """

    project: str
    task: str

    def __post_init__(self) -> None:
        for half, value in (("project", self.project), ("task", self.task)):
            if not value.strip():
                raise ValueError(f"a Session Label's {half} half may not be empty")
            if LABEL_SEPARATOR.strip() in value:
                raise ValueError(
                    f"a Session Label's {half} half may not contain {LABEL_SEPARATOR.strip()!r}"
                )

    def __str__(self) -> str:
        return f"{self.project}{LABEL_SEPARATOR}{self.task}"

    @classmethod
    def parse(cls, text: str) -> SessionLabel:
        """Read back a rendered label, refusing anything that is not exactly one."""
        halves = text.split(LABEL_SEPARATOR.strip())
        if len(halves) != 2:
            raise ValueError(f"not a Session Label: {text!r}")
        return cls(project=halves[0].strip(), task=halves[1].strip())


@dataclass(frozen=True, slots=True)
class SessionTarget:
    """The exact identity a command carries. Never inferred, never a label."""

    agent: AgentKind
    session_id: str
    pid: int | None = None

    def __post_init__(self) -> None:
        if not self.session_id.strip():
            raise ValueError("a session id may not be empty")
        if self.pid is not None and self.pid <= 0:
            raise ValueError(f"not a process id: {self.pid!r}")
        if self.agent.addressed_by_pid and self.pid is None:
            raise ValueError(
                f"a {self.agent} target needs a pid: a resumed session forks a second "
                "process under the same session id"
            )

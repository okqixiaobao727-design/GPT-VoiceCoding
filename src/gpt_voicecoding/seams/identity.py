"""Who a Relay is addressed to, and what correlates one attempt across routes.

Three locked rules are enforced here by shape rather than by discipline:

- **A Session Name is not a target.** `SessionName` is for matching and for
  speech; it has no session id and no pid, so it cannot be passed where a
  command expects one. Commands carry a `SessionTarget`.
- **Claude Sessions are addressed by pid.** `--resume` forks a second process
  under the same session id, so a Claude target without a pid is ambiguous and
  is refused at construction.
- **A target names *something*, and a session id is not always what it names.**
  Measured on 2026-08-26 (#73): `codex` writes the rollout that carries its
  session id when the first *turn* starts, not when the Session does — a full
  run watched one sit for 180 s with no id at all. A Session that exists, is
  running, and has not been spoken to yet is therefore nameable only by its
  process, so `session_id` is optional and the invariant is "at least one of
  session id and pid". Claude is the exception at the other end: its official
  roster always carries an id, so a Claude target without one is a defect in
  whoever built it, not an un-named Session.
- **`request_id` is one sender-minted UUID**, reused across every delivery of
  the same intent: Claude sends it as both `uuid` and `msg_id`, Codex as
  `clientUserMessageId`, and control-plane callers carry it through unchanged.
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

#: What separates the two halves of a Session Name when it is rendered.
NAME_SEPARATOR = " · "


def new_request_id() -> RequestId:
    """Mint the one id a sender carries across every delivery of an intent."""
    return RequestId(str(uuid.uuid4()))


class AgentKind(StrEnum):
    """The terminal coding agents this system watches and Relays into."""

    CLAUDE = "claude"
    CODEX = "codex"

    @property
    def addressed_by_pid(self) -> bool:
        """Whether a session id alone is too weak to name one process.

        True for Claude: a resumed session forks a second process under the same
        session id, so the pid is what distinguishes them.
        """
        return self is AgentKind.CLAUDE

    @property
    def always_named(self) -> bool:
        """Whether this agent has told us its session id by the time we can see it.

        True for Claude, whose official roster carries `sessionId` on every row
        from the moment the Session exists. False for Codex, which writes the
        rollout carrying its id at the first turn (#73, measured) — so an
        un-spoken-to Codex Session is legitimately anonymous, and refusing it
        would make every fresh TUI invisible.
        """
        return self is AgentKind.CLAUDE


@dataclass(frozen=True, slots=True)
class SessionName:
    """``<project> · <task>`` — for matching and for speech only.

    The one name the user and the system have for a Session (`CONTEXT.md`,
    *Session Name*). Deliberately holds nothing a command could address: see
    this module's docstring. Composed by the lane that observed the Session
    (`adapters/agent/_naming.py`) and frozen by the Session registry, so what a
    surface renders is what was true the first time the Session was seen.
    """

    project: str
    task: str

    def __post_init__(self) -> None:
        for half, value in (("project", self.project), ("task", self.task)):
            if not value.strip():
                raise ValueError(f"a Session Name's {half} half may not be empty")
            if NAME_SEPARATOR.strip() in value:
                raise ValueError(
                    f"a Session Name's {half} half may not contain {NAME_SEPARATOR.strip()!r}"
                )

    def __str__(self) -> str:
        return f"{self.project}{NAME_SEPARATOR}{self.task}"

    @classmethod
    def parse(cls, text: str) -> SessionName:
        """Read back a rendered name, refusing anything that is not exactly one."""
        halves = text.split(NAME_SEPARATOR.strip())
        if len(halves) != 2:
            raise ValueError(f"not a Session Name: {text!r}")
        return cls(project=halves[0].strip(), task=halves[1].strip())


@dataclass(frozen=True, slots=True)
class SessionTarget:
    """The exact identity a command carries. Never inferred, never a name."""

    agent: AgentKind
    #: `None` means "this Session has not been named yet", which is a real and
    #: ordinary state — see this module's docstring. An *empty* id is not that:
    #: it is a name nobody wrote, and it is refused.
    session_id: str | None = None
    pid: int | None = None

    def __post_init__(self) -> None:
        if self.session_id is not None and not self.session_id.strip():
            raise ValueError("a session id may not be empty; an unnamed Session carries None")
        if self.pid is not None and self.pid <= 0:
            raise ValueError(f"not a process id: {self.pid!r}")
        if self.session_id is None and self.pid is None:
            raise ValueError("a target names a Session by its session id, its pid, or both")
        if self.agent.addressed_by_pid and self.pid is None:
            raise ValueError(
                f"a {self.agent} target needs a pid: a resumed session forks a second "
                "process under the same session id"
            )
        if self.agent.always_named and self.session_id is None:
            raise ValueError(
                f"a {self.agent} target needs a session id: its official roster always "
                "carries one, so a row without one is a defect in whoever built it"
            )

    @property
    def named(self) -> bool:
        """Whether this Session has told anyone its session id yet.

        Asked rather than tested against `None` at every call site, because the
        answer is a fact about the Session — `codex` before its first turn — and
        not a shape a reader should have to recognise.
        """
        return self.session_id is not None

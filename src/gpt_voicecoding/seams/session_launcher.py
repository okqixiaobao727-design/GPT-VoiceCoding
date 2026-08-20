"""The Session Launcher seam — bringing a Session into existence, and closing it.

Verbs Bridge Core calls: `launch`, `close`, and `verify`.

`close` is part of this contract on the authority of the migration inventory's
`closing.md` dispositions, which fix its semantics: exactly one session target,
fail closed on missing or stale identity, idempotent repeats, and truthful
per-child outcomes only where the adapter actually owns child destinations.
ADR 0001's verb list predates that sync and is amended to match.

Launching and conversing are orthogonal: this seam only creates and destroys
Sessions, and the Agent seam talks to them. The Session *registry* is Bridge Core
state, not a module — there is deliberately no Session module (ADR 0001).

**Pane semantics never cross this seam.** A tmux server, session, window or pane
is the tmux adapter's business; Bridge Core sees a workspace going in and an
outcome coming back. `ChildOutcome.ref` is an opaque adapter-owned string for
exactly this reason.

The launcher **owns the child environment** — which is what makes a Claude
rendezvous supplement possible at all, and what makes the launcher responsible
for reaping.

An outcome is authoritative and truthful: exactly one launch per request id, the
real error on failure, the exact identity Bridge Core will register on success.
No success-on-failure, no silent retry, no substitute launch.

Adapters: a direct child process (the default) and tmux (optional).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from gpt_voicecoding.seams.identity import AgentKind, RequestId, SessionLabel, SessionTarget
from gpt_voicecoding.seams.verify import VerifyResult


class LaunchStatus(StrEnum):
    LAUNCHED = "launched"
    FAILED = "failed"
    #: This adapter cannot run here at all — tmux is not installed. Distinct from
    #: a launch that was attempted and failed.
    UNAVAILABLE = "unavailable"


class CloseStatus(StrEnum):
    CLOSED = "closed"
    #: The idempotent repeat, and the already-exited Session. A success.
    ALREADY_CLOSED = "already_closed"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class LaunchRequest:
    """One launch, identified so that a repeat cannot become a second child."""

    request_id: RequestId
    agent: AgentKind
    workspace: Path
    label: SessionLabel
    #: Exactly the variables to set on the child, and no others.
    env: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "env", MappingProxyType(dict(self.env)))


@dataclass(frozen=True, slots=True)
class LaunchOutcome:
    """What actually happened. `target` is what Bridge Core will register."""

    request_id: RequestId
    status: LaunchStatus
    target: SessionTarget | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if self.status is LaunchStatus.LAUNCHED:
            if self.target is None:
                raise ValueError("a launched Session must return the exact identity to register")
        else:
            if self.target is not None:
                raise ValueError(f"a {self.status} launch registers nothing")
            if not self.detail.strip():
                raise ValueError(f"a {self.status} launch must carry the real error")


@dataclass(frozen=True, slots=True)
class CloseRequest:
    """Exactly one session target. Never a label, never "the foreground one"."""

    request_id: RequestId
    target: SessionTarget


@dataclass(frozen=True, slots=True)
class ChildOutcome:
    """One destination this adapter actually owns, and what became of it.

    `ref` is opaque and adapter-owned. Bridge Core neither parses nor stores it.
    """

    ref: str
    closed: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class CloseOutcome:
    """What actually closed. `children` is empty unless this adapter owns children."""

    request_id: RequestId
    status: CloseStatus
    detail: str = ""
    children: tuple[ChildOutcome, ...] = ()

    def __post_init__(self) -> None:
        if self.status in (CloseStatus.FAILED, CloseStatus.UNAVAILABLE) and not self.detail.strip():
            raise ValueError(f"a {self.status} close must carry the real error")


@runtime_checkable
class SessionLauncher(Protocol):
    """Creating and destroying Sessions. Says nothing about talking to them."""

    async def launch(self, request: LaunchRequest) -> LaunchOutcome:
        """Bring exactly one Session into existence, and report what happened."""
        ...

    async def close(self, request: CloseRequest) -> CloseOutcome:
        """Close exactly one Session. Idempotent; fails closed on a stale identity."""
        ...

    async def verify(self) -> VerifyResult:
        """Report which implementation this is and whether it can run here."""
        ...

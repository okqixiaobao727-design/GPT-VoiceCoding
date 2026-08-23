"""What Bridge Core raises when it refuses.

Every error here is a *refusal*, not a surprise: the state components fail closed
on an unknown switch, an unknown or stale Session, or an ambiguous label, because
the alternative — guessing — is the failure class this repository exists to
avoid. Callers that must speak the refusal aloud get the offending value back on
the exception rather than having to parse a message.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from gpt_voicecoding.seams.identity import RequestId, SessionTarget

if TYPE_CHECKING:  # a refusal names the Sessions it refused between
    from gpt_voicecoding.core.sessions import Session


class BridgeCoreError(Exception):
    """Base for every refusal Bridge Core's state components raise."""


class UnknownSwitchError(BridgeCoreError):
    """No switch by that name is registered on this board."""

    def __init__(self, name: str) -> None:
        super().__init__(f"unknown switch: {name!r}")
        self.name = name


class SessionError(BridgeCoreError):
    """Base for every refusal about a Session target."""


class UnknownSessionError(SessionError):
    """No Session by that identity was ever registered, or it has been forgotten."""

    def __init__(self, target: SessionTarget) -> None:
        super().__init__(f"unknown Session: {target}")
        self.target = target


class StaleSessionError(SessionError):
    """The identity names a Session that is no longer reachable under it.

    Deliberately distinct from unknown: a wrong pid under a known session id is a
    *fork*, and the pids that are live are worth reporting back.
    """

    def __init__(
        self, target: SessionTarget, *, reason: str, live_pids: tuple[int, ...] = ()
    ) -> None:
        super().__init__(f"stale Session target {target}: {reason}")
        self.target = target
        self.reason = reason
        self.live_pids = live_pids


class DuplicateSessionError(SessionError):
    """That identity is already registered. Registering it again would split truth."""

    def __init__(self, target: SessionTarget) -> None:
        super().__init__(f"Session already registered: {target}")
        self.target = target


class LabelMatchError(BridgeCoreError):
    """Base for a label that did not resolve to exactly one Session."""


class NoLabelMatchError(LabelMatchError):
    """Nothing matched. Ask; do not guess."""

    def __init__(self, query: str) -> None:
        super().__init__(f"no live Session matches {query!r}")
        self.query = query


class AmbiguousLabelError(LabelMatchError):
    """More than one matched. Refuse and name them, rather than picking one."""

    def __init__(self, query: str, candidates: tuple[Session, ...]) -> None:
        super().__init__(f"{len(candidates)} live Sessions match {query!r}")
        self.query = query
        self.candidates = candidates


class RelayError(BridgeCoreError):
    """Base for every refusal about an entry in the undelivered Relay queue."""


class DuplicateRelayError(RelayError):
    """That request id is already queued. One request id is one attempt."""

    def __init__(self, request_id: RequestId) -> None:
        super().__init__(f"Relay already queued: {request_id}")
        self.request_id = request_id


class UnknownRelayError(RelayError):
    """Nothing pending under that request id — never queued, or already released."""

    def __init__(self, request_id: RequestId) -> None:
        super().__init__(f"no pending Relay: {request_id}")
        self.request_id = request_id


class StateFormatError(BridgeCoreError):
    """The persisted state cannot be read as this version of Bridge Core's truth.

    Fails closed rather than starting blank: silently discarding the user's
    switch state would look exactly like the system deciding to stop speaking.
    """

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"cannot read persisted state at {path}: {reason}")
        self.path = path
        self.reason = reason


class SeamUnavailableError(BridgeCoreError):
    """Something was asked for that needs a seam this engine has nothing behind.

    Raised rather than answered with a hopeful default: an engine assembled
    without a Session Launcher cannot launch, and the honest answer to "launch
    this" is that this engine cannot, not a failure that reads like the
    Launcher tried.
    """

    def __init__(self, seam: str) -> None:
        super().__init__(f"this engine has nothing loaded behind the {seam} seam")
        self.seam = seam


class ConflictingLaunchError(BridgeCoreError):
    """One launch identity was reused for a different resolved launch intent."""

    def __init__(self, request_id: RequestId) -> None:
        super().__init__(
            f"launch request identity {request_id!r} is already bound to a different intent"
        )
        self.request_id = request_id


class ProjectMatchError(BridgeCoreError):
    """A spoken project reference did not resolve to exactly one configured project."""


class UnknownProjectError(ProjectMatchError):
    """No configured canonical name or spoken alias matched."""

    def __init__(self, query: str, available: tuple[str, ...]) -> None:
        names = ", ".join(repr(name) for name in available)
        super().__init__(f"no configured project matches {query!r}; configured projects: {names}")
        self.query = query
        self.available = available


class AmbiguousProjectError(ProjectMatchError):
    """More than one configured project matched, so Bridge Core refuses to choose."""

    def __init__(self, query: str, candidates: tuple[str, ...]) -> None:
        names = ", ".join(repr(name) for name in candidates)
        super().__init__(f"{len(candidates)} configured projects match {query!r}: {names}")
        self.query = query
        self.candidates = candidates


class InvalidLaunchLabelError(BridgeCoreError):
    """The resolved project and supplied task cannot form the canonical Session Label."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"cannot construct the Session Label: {reason}")
        self.reason = reason


class SecondCallRefused(BridgeCoreError):
    """Something asked to open a voice surface while the system already owns one.

    The one-call-at-a-time invariant, raised rather than silently absorbed: a
    caller that meant to *open* a call has a different plan to make when one is
    already up, and hiding that is how the reference implementation's escalation
    path pressed the toggle on top of a system-owned call and left two
    assistants talking to each other.
    """

    def __init__(self, call_id: str) -> None:
        super().__init__(f"the system already owns call {call_id!r}; nothing may open a second")
        self.call_id = call_id


class VoiceInstructionsMissing(BridgeCoreError):
    """Something asked to open a voice surface on an engine that generated no rules.

    Raised at the one door a call can be opened through, so the rule lives in
    exactly one place and every caller meets the same refusal. An engine with no
    instruction context has no house rules to start a voice thread on, and
    starting one anyway would put a model on the user's speakers with nothing
    telling it what it may say — which is the one thing the generated
    instructions exist to prevent.
    """

    def __init__(self) -> None:
        super().__init__("this engine generated no voice instructions, so it cannot start a call")

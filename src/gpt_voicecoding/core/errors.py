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

"""What Bridge Core raises when it refuses.

Every error here is a *refusal*, not a surprise: the state components fail closed
on an unknown switch, an unknown or stale Session, or an ambiguous name, because
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


class ChildSessionError(SessionError):
    """That identity names a Child Process. It is seen, never spoken to (#68).

    A refusal rather than a silent skip, and structural rather than a rule the
    Relay pipeline remembers: a crew's reviewer answering a question meant for
    the Session that spawned it is the user's own words landing under somebody
    else's authority.
    """

    def __init__(self, target: SessionTarget, parent: SessionTarget | None = None) -> None:
        whose = f" spawned by {parent}" if parent is not None else ""
        super().__init__(
            f"{target} is a Child Process{whose}: it appears in the roster and is never "
            "Relayed into"
        )
        self.target = target
        self.parent = parent


class ProgressUnavailable(SessionError):
    """Nothing on this machine can read what that Session said.

    The honest error #76 asks for and #171 keeps on the `history` verb, and it is
    a refusal rather than an empty page, for the reason both tickets turn on:
    "nobody could read it" and "it has said nothing" are different facts, and a
    surface handed the first as the second reports a working Session as an idle
    one. A Session that *was* read and has nothing before the cursor comes back
    as an ordinary answer with an empty page.

    **Named for the observation, not for the retired verb.** `progress` is still
    what the Agent seam calls the reading a lane takes of a Session's own record
    (`ProgressObservation`); what retired is the *action* that published it
    exactly (#171). This is that reading being unavailable, and it is raised by
    `history` alone.

    Two ways to arrive here, and neither may be papered over: a Codex Session the
    shared daemon does not hold — its rollout is on disk and reading it would be
    a second source answering with worse evidence (port table P6, P13) — and a
    Session whose first turn has not written a record yet (#73).

    **Ported** from the reference implementation, which rejected the same verb
    with the source's own reason rather than falling back to a terminal, a screen
    or the other agent (`legacy@1d32845:bridge/daemon.py:2222-2246`).
    """

    def __init__(self, target: SessionTarget) -> None:
        super().__init__(
            f"nothing has read what {target} said: this engine reads a Session's "
            "own record and never infers one"
        )
        self.target = target


class DuplicateSessionError(SessionError):
    """That identity is already registered. Registering it again would split truth."""

    def __init__(self, target: SessionTarget) -> None:
        super().__init__(f"Session already registered: {target}")
        self.target = target


class NameMatchError(BridgeCoreError):
    """Base for a Session Name that did not resolve to exactly one Session."""


class NoNameMatchError(NameMatchError):
    """Nothing matched. Ask; do not guess."""

    def __init__(self, query: str) -> None:
        super().__init__(f"no live Session matches {query!r}")
        self.query = query


class AmbiguousNameError(NameMatchError):
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


class CallInstructionsMissing(BridgeCoreError):
    """A call was asked for on an engine that generated no rules for it to run on.

    **It names which half is missing.** A call addresses two audiences — prose
    for the Voice and rules for the Call Agent (ADR 0018) — and the error this
    replaced was called `VoiceInstructionsMissing` while being raised on the
    Agent's set, because that was the half the one string `ensure_call` took
    reached (#193's deferred note). A `Dial` carries both, so a refusal that did
    not say which one was absent would send a reader to the wrong generator.

    Raised where Bridge Core builds the `Dial`, which is the one place that knows
    what it generated. The blank check that used to live in the interlock is gone
    with it: a `Dial` refuses its own empty halves at construction, so by the time
    one exists there is nothing left to check.
    """

    def __init__(self, missing: str) -> None:
        super().__init__(f"this engine generated no {missing}, so it cannot dial a call")


class LaneUnreadable(BridgeCoreError):
    """A lane could not be read at all, so nothing can be said about its Sessions.

    The hub's translation of the Agent seam's `LaneUnavailable`, which is the one
    thing on that seam that raises. It is deliberately *not* an answer with empty
    progress: "I could not look" and "it has said nothing" are different facts,
    and a surface that rendered the first as the second would report a working
    Session as an idle one every time `claude` fell off the PATH.

    It carries the lane's own words rather than a rephrasing, and the row it was
    asked about is left exactly as the roster last saw it (`seams/agent.py`,
    `LaneUnavailable`).

    **Ported** from the reference implementation's own rule for the same verb:
    a progress source that could not answer produced a rejection carrying its
    reason, and never a fallback to a terminal, a screen or the other agent
    (`legacy@1d32845:bridge/daemon.py:2222-2246`). **Adapted**: legacy's
    rejection was a reply document built by the daemon, and this is a refusal the
    control plane maps onto the closed error set.
    """

    def __init__(self, agent: str, reason: str) -> None:
        super().__init__(f"the {agent} lane could not be read: {reason}")
        self.agent = agent
        self.reason = reason

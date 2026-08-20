"""The Session registry — Bridge Core state, and deliberately not a module.

Launching is the Session Launcher seam and conversing is the Agent seam; what
Sessions *exist* is held here, in the hub, and every surface queries it rather
than keeping a copy (ADR 0001).

Three refusals are the point of this file, and each one is a defect the
reference implementation carried:

- **An unknown identity fails closed.** Nothing resolves to "probably that one".
- **A stale identity is not an unknown one.** A wrong pid under a known session
  id means the Session forked — `--resume` starts a second process under the
  same session id — so the refusal names the pids that *are* live instead of
  pretending the session id was never seen.
- **A label disambiguates or asks.** Labels are for matching and for speech; two
  candidates are answered by refusing and naming both, never by picking.

There is one registry and one Reply Window state per Session. The reference
implementation ran two live ledgers and rendered both; nothing here may grow a
second.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

from gpt_voicecoding.core.errors import (
    AmbiguousLabelError,
    DuplicateSessionError,
    NoLabelMatchError,
    StaleSessionError,
    UnknownSessionError,
)
from gpt_voicecoding.seams.agent import ReplyWindow
from gpt_voicecoding.seams.identity import SessionLabel, SessionTarget


class SessionState(StrEnum):
    """Whether this Session can still be Relayed into."""

    LIVE = "live"
    ENDED = "ended"


@dataclass(frozen=True, slots=True)
class Session:
    """One terminal coding-agent run the system launched, watches and Relays into."""

    target: SessionTarget
    label: SessionLabel
    workspace: Path
    registered_at: float
    state: SessionState = SessionState.LIVE
    reply_window: ReplyWindow = ReplyWindow.CLOSED


def _normalise(text: str) -> str:
    """Case- and whitespace-insensitive form used for label matching only."""
    return " ".join(text.split()).casefold()


class SessionRegistry:
    """What Sessions exist. Holds state; decides no policy about them."""

    def __init__(self) -> None:
        self._sessions: dict[SessionTarget, Session] = {}

    def register(self, session: Session) -> Session:
        """Record a Session the Launcher reported. Refuses to register truth twice."""
        target = session.target
        if target in self._sessions:
            raise DuplicateSessionError(target)
        if not target.agent.addressed_by_pid and self._by_session_id(target):
            raise DuplicateSessionError(target)
        self._sessions[target] = session
        return session

    def resolve(self, target: SessionTarget) -> Session:
        """The Session that exact identity names, or a refusal saying why not."""
        candidates = self._by_session_id(target)
        if not candidates:
            raise UnknownSessionError(target)

        if target.agent.addressed_by_pid:
            matched = [held for held in candidates if held.target.pid == target.pid]
            if not matched:
                raise StaleSessionError(
                    target,
                    reason="that session id runs under a different process",
                    live_pids=tuple(
                        held.target.pid
                        for held in candidates
                        if held.state is SessionState.LIVE and held.target.pid is not None
                    ),
                )
            session = matched[0]
        else:
            session = candidates[0]

        if session.state is not SessionState.LIVE:
            raise StaleSessionError(target, reason=f"that Session is {session.state}")
        return session

    def match_label(self, query: str) -> Session:
        """Find the one live Session a spoken label names, or refuse.

        The query is matched as a fragment, and **more than one match refuses**,
        with every candidate named. An exact label is deliberately *not* given
        precedence, for three reasons:

        - The costs are asymmetric. A refusal costs one spoken round trip; a
          wrong pick delivers the user's own words into the wrong Session,
          silently, carrying the user's authority.
        - Exactness is only evidence when the text is trustworthy, and the
          primary source here is a realtime voice transcript. "ship it" may be
          the user meaning the short label, or the transcriber clipping "ship it
          later". Exactness of lossy text says nothing about intent.
        - "A label is not a target" is locked. Letting an exact label win
          promotes it to a target by right, which is the first step back toward
          addressing by label.

        The collision only exists while two live labels stand in a fragment
        relation. That is worth fixing where labels are minted — by keeping a
        fresh title word-level distinct from the live ones — rather than by
        making matching cleverer here.
        """
        wanted = _normalise(query)
        candidates = [held for held in self.live() if wanted in _normalise(str(held.label))]

        if not candidates:
            raise NoLabelMatchError(query)
        if len(candidates) > 1:
            raise AmbiguousLabelError(query, tuple(candidates))
        return candidates[0]

    def set_reply_window(self, target: SessionTarget, window: ReplyWindow) -> Session:
        """Record what the Agent adapter observed about this Session's willingness."""
        return self._replace(target, reply_window=window)

    def mark_ended(self, target: SessionTarget) -> Session:
        """A Session is gone. Its Reply Window closes with it."""
        session = self.resolve(target)
        ended = replace(session, state=SessionState.ENDED, reply_window=ReplyWindow.CLOSED)
        self._sessions[session.target] = ended
        return ended

    def forget(self, target: SessionTarget) -> None:
        """Drop a Session entirely. Resolving it afterwards is unknown, not stale."""
        session = self._sessions.pop(target, None)
        if session is None:
            raise UnknownSessionError(target)

    def live(self) -> tuple[Session, ...]:
        """The roster, in registration order."""
        return tuple(held for held in self._sessions.values() if held.state is SessionState.LIVE)

    def all(self) -> tuple[Session, ...]:
        """Every Session held, ended ones included, in registration order."""
        return tuple(self._sessions.values())

    def restore(self, sessions: tuple[Session, ...]) -> None:
        """Adopt a persisted roster, replacing whatever is held."""
        self._sessions = {}
        for session in sessions:
            self.register(session)

    def _by_session_id(self, target: SessionTarget) -> list[Session]:
        return [
            held
            for held in self._sessions.values()
            if held.target.agent is target.agent and held.target.session_id == target.session_id
        ]

    def _replace(self, target: SessionTarget, **changes: Any) -> Session:
        session = self.resolve(target)
        updated = replace(session, **changes)
        self._sessions[session.target] = updated
        return updated

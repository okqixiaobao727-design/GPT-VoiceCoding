"""Bridge Core's objects rendered onto the wire, and wire addresses read back.

Translation, and nothing else. Two rules hold this module honest:

- **Nothing is softened.** A delivery state renders as the state it is, and a
  refusal carries the refusal's own words. The reference implementation's
  expensive habit was a surface deciding what a result "really meant" on the
  user's behalf.
- **A Session Name is never an address.** `SessionTarget` is what crosses the wire, and
  it is read back through the same constructor Bridge Core uses, so a Claude
  target without a pid is refused here rather than becoming an ambiguous
  command later. Turning a spoken name into a target is the router's job, on
  the way in from the Companion Channel.

Every reader raises `InvalidPayload`, which the action layer turns into one
error code. Readers are strict: a payload key that is present with the wrong
type is a refusal, never a silently coerced value — `"on": "false"` is truthy,
and the switch it would flip on is the master.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from gpt_voicecoding.core import briefing
from gpt_voicecoding.core.approvals import ApprovalOutcome, PendingApproval
from gpt_voicecoding.core.bridge import Status
from gpt_voicecoding.core.briefing import Decision, RosterBrief, SessionBrief
from gpt_voicecoding.core.relay_queue import PendingRelay
from gpt_voicecoding.core.relays import RelayOutcome
from gpt_voicecoding.core.sessions import Session
from gpt_voicecoding.core.verification import SeamVerification
from gpt_voicecoding.seams.agent import (
    ApprovalVerdict,
    ChildClassification,
    ProgressObservation,
    RelayRoute,
    ReplyWindow,
    WaitingFor,
)
from gpt_voicecoding.seams.call import CallSnapshot
from gpt_voicecoding.seams.identity import AgentKind, SessionTarget


class InvalidPayload(Exception):
    """The action is known; what arrived with it cannot be used."""


class NothingPending(Exception):
    """The request named something the hub is not holding — an answered dialog.

    Bridge Core discards a verdict for a request that already resolved, on
    purpose: its closing notice has already gone out. That is the right policy
    and the wrong silence for a surface, whose user is owed the news that their
    verdict landed on nothing.
    """


# ----------------------------------------------------------------------
# Reading what a surface sent.
# ----------------------------------------------------------------------


def read_text(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InvalidPayload(f"{key!r} must be a non-empty string")
    return value.strip()


def read_flag(payload: Mapping[str, Any], key: str) -> bool:
    """A switch has exactly two states, so anything but a bool is refused."""
    value = payload.get(key)
    if not isinstance(value, bool):
        raise InvalidPayload(f"{key!r} is on or off; {value!r} is neither")
    return value


def read_optional_ordinal(payload: Mapping[str, Any], key: str) -> int | None:
    """A cursor a surface read off an earlier page, or nothing at all.

    Absent, or JSON `null`, asks for the newest page — the same reading `brief`
    gives an absent target. A `bool` is refused before the number check because
    `True` is an `int` in Python and would page from ordinal 1; a negative one is
    refused because ordinals count from 0 and nothing lies before the oldest
    entry (#171).
    """
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidPayload(f"{key!r} is an entry's ordinal, or absent for the newest page")
    if value < 0:
        raise InvalidPayload("an ordinal counts from the oldest entry, at 0")
    return value


def read_target(payload: Mapping[str, Any], key: str = "target") -> SessionTarget:
    """The exact identity a command carries. Never a name, never inferred."""
    raw = payload.get(key)
    if not isinstance(raw, dict):
        raise InvalidPayload(f"{key!r} must name a Session: agent, session_id and pid")

    pid = raw.get("pid")
    if pid is not None and not isinstance(pid, int):
        raise InvalidPayload("a pid is a whole number, or absent")
    raw_id = raw.get("session_id")
    if raw_id is not None and not isinstance(raw_id, str):
        raise InvalidPayload("a session id is text, or absent on a Session not named yet")
    # A `codex` Session has no session id until its first turn (#73), so absent
    # is a state and not a malformed payload. `SessionTarget` refuses the
    # combinations that name nothing.
    session_id = raw_id.strip() if isinstance(raw_id, str) and raw_id.strip() else None
    try:
        return SessionTarget(agent=read_agent(raw, "agent"), session_id=session_id, pid=pid)
    except ValueError as refusal:
        raise InvalidPayload(str(refusal)) from None


def read_optional_target(payload: Mapping[str, Any], key: str = "target") -> SessionTarget | None:
    """The address an action may carry, or `None` when it carried none.

    An omitted key and an explicit JSON `null` are one answer — "no address" —
    which is how `read_route` already reads an absent value, and how a surface
    that builds its payload from a nullable field will write it.

    **Unusable is not absent.** A `target` that is present and names nothing is
    a refusal, because a reader that widened it would answer the whole roster to
    a surface that asked about one Session.
    """
    return None if payload.get(key) is None else read_target(payload, key)


def read_agent(payload: Mapping[str, Any], key: str = "agent") -> AgentKind:
    try:
        return AgentKind(read_text(payload, key))
    except ValueError:
        known = ", ".join(str(kind) for kind in AgentKind)
        raise InvalidPayload(
            f"{payload.get(key)!r} is not an agent this system runs: {known}"
        ) from None


def read_route(payload: Mapping[str, Any], key: str = "route") -> RelayRoute:
    """Route follows the user's explicit intent, so it is read, never inferred."""
    raw = payload.get(key)
    if raw is None:
        return RelayRoute.DELIVER
    try:
        return RelayRoute(raw)
    except ValueError:
        known = ", ".join(str(route) for route in RelayRoute)
        raise InvalidPayload(f"{raw!r} is not a route: {known}") from None


def read_verdict(payload: Mapping[str, Any], key: str = "verdict") -> ApprovalVerdict:
    try:
        return ApprovalVerdict(read_text(payload, key))
    except ValueError:
        known = ", ".join(str(verdict) for verdict in ApprovalVerdict)
        raise InvalidPayload(f"{payload.get(key)!r} is not a verdict: {known}") from None


# ----------------------------------------------------------------------
# Rendering what Bridge Core answered.
# ----------------------------------------------------------------------


def target_document(target: SessionTarget) -> dict[str, Any]:
    return {"agent": str(target.agent), "session_id": target.session_id, "pid": target.pid}


def session_document(
    session: Session,
    *,
    progress: Mapping[str, Any],
    reply_window: ReplyWindow | None = None,
) -> dict[str, Any]:
    """One Session as a surface renders it: spoken by name, addressed by target.

    Every field of the roster row travels, because a surface that had to ask a
    second question to render one line would be a second reader of the same
    Session — which is the thing `SessionInspection` exists to prevent. The
    rendering of these fields is each surface's own; the facts are not.
    """
    return {
        "target": target_document(session.target),
        # One name, rendered as the one string the user hears and types after
        # `@` — `<project> · <task>`. #78 collapsed the `label`/`name` pair
        # that used to travel here into it; a surface that wants the halves
        # parses them back with `SessionName.parse`.
        "name": str(session.name) if session.name is not None else None,
        "workspace": str(session.workspace),
        "first_seen": session.first_seen,
        "lifecycle": str(session.lifecycle),
        "state": str(session.state),
        "waiting_for": waiting_for_document(session.waiting_for),
        "progress": dict(progress),
        "last_activity": (
            session.last_activity.isoformat() if session.last_activity is not None else None
        ),
        "child": child_document(session.child),
        # Derived on the row and rendered here, so no surface re-derives it and
        # no two surfaces can disagree about the same Session.
        "reply_window": str(reply_window or session.reply_window),
    }


def waiting_for_document(waiting_for: WaitingFor) -> dict[str, Any]:
    """What a Session stopped on, as structure rather than as a rendered sentence."""
    return {
        "kind": str(waiting_for.kind),
        "caught_up": waiting_for.caught_up,
        "prompt": waiting_for.prompt,
        "options": [
            {
                "text": option.text,
                "description": option.description,
                "recommended": option.recommended,
            }
            for option in waiting_for.options
        ],
        "recommendation": waiting_for.recommendation,
        "tool_name": waiting_for.tool_name,
        "detail": waiting_for.detail,
        "approval_id": waiting_for.approval_id,
    }


def child_document(child: ChildClassification) -> dict[str, Any]:
    """Whether this row is the user's Session or something one of them spawned."""
    return {
        "kind": str(child.kind),
        "parent": target_document(child.parent) if child.parent is not None else None,
    }


def pending_relay_document(pending: PendingRelay) -> dict[str, Any]:
    return {
        "request_id": str(pending.request_id),
        "target": target_document(pending.target),
        "kind": str(pending.kind),
        "text": pending.text,
        "route": str(pending.route),
        "queued_at": pending.queued_at,
        "expires_at": pending.expires_at,
        # The grade of the last attempt, or nothing at all when none was made.
        # `null` rather than `unknown`: the absence of an attempt is not an
        # attempt that proved nothing.
        "outcome": None if pending.receipt is None else str(pending.receipt.outcome),
    }


def pending_approval_document(pending: PendingApproval) -> dict[str, Any]:
    return {
        "approval_id": pending.request.approval_id,
        "target": target_document(pending.request.target),
        "tool_name": pending.request.tool_name,
        "detail": pending.request.detail,
        "options": list(pending.request.options),
        "opened_at": pending.opened_at,
        "expires_at": pending.expires_at,
    }


def status_document(
    status: Status,
    *,
    progress_for: Callable[[ProgressObservation], Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "switches": status.switches.as_mapping(),
        "sessions": [
            session_document(
                session,
                progress=progress_for(session.progress),
                reply_window=status.reply_windows.get(session.target, session.reply_window),
            )
            for session in status.sessions
        ],
        "lanes": {str(agent): reason for agent, reason in status.lanes.items()},
        "degraded_lanes": {str(agent): reason for agent, reason in status.degraded_lanes.items()},
        "call_id": status.call_id,
        "pending_relays": [pending_relay_document(item) for item in status.pending_relays],
        "pending_approvals": [pending_approval_document(item) for item in status.pending_approvals],
    }


def brief_document(brief: RosterBrief | SessionBrief) -> dict[str, Any]:
    """One brief on the wire: its structure, and the one rendering of it.

    **Both, deliberately.** `text` is what a surface prints, and it is the
    engine's own — `briefing.text` is the only renderer of Session state there
    is (#166 B6), so a `bridgectl` that composed its own line would be the
    second voice this whole module exists to prevent. The structure travels
    beside it because later work reads fields rather than lines: the dial's
    hand-over is built from the brief, never from its rendering.
    """
    rendered = {"text": briefing.text(brief)}
    if isinstance(brief, RosterBrief):
        return {"kind": "roster", "roster": roster_brief_document(brief), **rendered}
    return {"kind": "session", "session": session_brief_document(brief), **rendered}


def roster_brief_document(brief: RosterBrief) -> dict[str, Any]:
    return {
        "counts": {str(state): count for state, count in brief.counts.items()},
        "focus": target_document(brief.focus) if brief.focus is not None else None,
        "rows": [
            {
                "target": target_document(row.target),
                "name": str(row.name) if row.name is not None else None,
                "agent": str(row.agent),
                "state": str(row.state),
                "focus": row.focus,
            }
            for row in brief.rows
        ],
    }


def session_brief_document(brief: SessionBrief) -> dict[str, Any]:
    return {
        "target": target_document(brief.target),
        "name": str(brief.name) if brief.name is not None else None,
        "agent": str(brief.agent),
        "state": str(brief.state),
        "newest": {"state": str(brief.newest.state), "text": brief.newest.text},
        "decision": decision_document(brief.decision),
        "answerable_here": brief.answerable_here,
        "last_activity_at": (
            brief.last_activity_at.isoformat() if brief.last_activity_at is not None else None
        ),
    }


def decision_document(decision: Decision | None) -> dict[str, Any] | None:
    """What the Session is waiting on, whole. `null` when it waits on nothing."""
    if decision is None:
        return None
    return {
        "prompt": decision.prompt,
        "options": [
            {
                "text": option.text,
                "description": option.description,
                "recommended": option.recommended,
            }
            for option in decision.options
        ],
        "recommendation": decision.recommendation,
        "tool": decision.tool,
        "summary": decision.summary,
    }


def call_document(snapshot: CallSnapshot) -> dict[str, Any]:
    return {"state": str(snapshot.state), "call_id": snapshot.call_id}


def relay_document(outcome: RelayOutcome) -> dict[str, Any]:
    """The receipt: where the words are, what was proved, and why.

    `receipt` is `null` when nothing was attempted — never a grade of `unknown`,
    which is a positive observation this document must not invent. The attempt's
    own evidence rides inside it, for the log and for a surface that is
    diagnosing rather than telling the user something.
    """
    return {
        "request_id": str(outcome.request_id),
        "target": target_document(outcome.target),
        "state": str(outcome.state),
        "route": str(outcome.route),
        "receipt": (
            None
            if outcome.receipt is None
            else {"outcome": str(outcome.receipt.outcome), "reason": outcome.receipt.reason}
        ),
        "reason": str(outcome.reason),
    }


def approval_document(outcome: ApprovalOutcome) -> dict[str, Any]:
    return {
        "approval_id": outcome.request.approval_id,
        "target": target_document(outcome.request.target),
        "verdict": str(outcome.verdict),
        "state": str(outcome.state),
        "outcome": str(outcome.outcome),
        "closing_notice": outcome.closing_notice,
    }


def verification_document(reports: tuple[SeamVerification, ...]) -> dict[str, Any]:
    return {
        "seams": [
            {
                "seam": report.seam,
                "outcome": str(report.outcome),
                "configured": report.configured,
                "loaded": report.loaded,
                "detail": report.detail,
            }
            for report in reports
        ]
    }

"""One request, one Bridge Core verb, one reply.

This is the whole engine side of the control plane's meaning, and it is
deliberately thin: it validates a payload, calls the hub, renders the answer.
It holds **no policy and no state** — the same rule `bridgectl` is held to,
for the same reason. Anything here that started deciding would be a second
decision-maker beside the hub.

Every action calls a **hub verb**, never a pipeline inside it. Outsiders see one
Bridge Core (ADR 0001), so a surface that knew which of the five pipelines owned
which decision would be a surface that has to change when the hub rearranges
itself — and one that could reach a pipeline the hub would have guarded.

**ADR 0002 is honoured by omission.** Nothing in this file consults switch
state, and there is no branch that could. The reference implementation gated
seven actions — `sessions` and six more — behind the Duty Switch, so a user
away from the computer with Duty off could see nothing and do nothing; that
dispatch behaviour is dropped, not ported, and
`tests/test_control_plane_actions.py` proves every action still answers with
every switch off.

**A refusal keeps its identity.** Bridge Core's refusals map onto the closed
error set by type, and the message that travels is the refusal's own words —
surfaces render it verbatim rather than rephrasing it, so honest wording lives
in one place.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from gpt_voicecoding.control_plane import payloads
from gpt_voicecoding.control_plane.payloads import InvalidPayload, NothingPending
from gpt_voicecoding.core.bridge import BridgeCore
from gpt_voicecoding.core.errors import (
    BridgeCoreError,
    SecondCallRefused,
    StaleSessionError,
    UnknownRelayError,
    UnknownSessionError,
    UnknownSwitchError,
)
from gpt_voicecoding.seams.control_plane import Action, ErrorCode, Reply, Request

Handler = Callable[[Mapping[str, Any]], Awaitable[dict[str, Any]]]

#: Bridge Core's refusals, in the order they are tested — most specific first.
_CODES: tuple[tuple[type[BridgeCoreError], ErrorCode], ...] = (
    (UnknownSwitchError, ErrorCode.UNKNOWN_SWITCH),
    (StaleSessionError, ErrorCode.STALE_SESSION),
    (UnknownSessionError, ErrorCode.UNKNOWN_SESSION),
    (UnknownRelayError, ErrorCode.UNKNOWN_PENDING),
    (SecondCallRefused, ErrorCode.SECOND_CALL_REFUSED),
)


def code_for(refusal: BridgeCoreError) -> ErrorCode:
    """Which code a refusal travels under. Unmapped refusals still travel."""
    for kind, code in _CODES:
        if isinstance(refusal, kind):
            return code
    return ErrorCode.REFUSED


class ControlPlane:
    """The engine side of the control plane. Translation, never decision."""

    def __init__(self, core: BridgeCore) -> None:
        self._core = core
        self.handlers: dict[Action, Handler] = {
            Action.STATUS: self._status,
            Action.SWITCH: self._switch,
            Action.SESSIONS: self._sessions,
            Action.PROGRESS: self._progress,
            Action.LIVE: self._live,
            Action.RELAY: self._relay,
            Action.APPROVE: self._approve,
            Action.VERIFY: self._verify,
        }

    @property
    def commands(self) -> frozenset[str]:
        """The command words this engine answers to, for the inbound-text grammar.

        One command set: `/status` on the Companion Channel and `bridgectl
        status` are the same action, dispatched here, with no second table to
        drift from this one.
        """
        return frozenset(str(action) for action in self.handlers)

    async def handle(self, request: Request) -> Reply:
        """Answer exactly one request. Never raises; a refusal is a reply."""
        handler = self.handlers.get(request.action)
        if handler is None:  # a closed action set with a hole in it
            return Reply.refused(
                request.action,
                ErrorCode.UNKNOWN_ACTION,
                f"this engine has no handler for {request.action}",
            )
        try:
            return Reply.answered(request.action, await handler(request.payload))
        except InvalidPayload as unusable:
            return Reply.refused(request.action, ErrorCode.INVALID_PAYLOAD, str(unusable))
        except NothingPending as gone:
            return Reply.refused(request.action, ErrorCode.UNKNOWN_PENDING, str(gone))
        except BridgeCoreError as refusal:
            return Reply.refused(request.action, code_for(refusal), str(refusal))

    # ------------------------------------------------------------------
    # The actions. Each one is a payload read, a hub call, and a render.
    # ------------------------------------------------------------------

    async def _status(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return payloads.status_document(self._core.status())

    async def _sessions(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return {"sessions": payloads.status_document(self._core.status())["sessions"]}

    async def _progress(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """How far along one exact Session is, rendered as one roster row.

        The same document `sessions` renders, deliberately: a surface that had to
        learn a second shape to show one Session's progress would be a second
        reader of the same facts. What this action adds is *when* — the row comes
        back read at the moment it was asked for, rather than at the last tick.
        """
        session = await self._core.progress(payloads.read_target(payload))
        reply_window = self._core.status().reply_windows.get(
            session.target,
            session.reply_window,
        )
        return {
            "session": payloads.session_document(
                session,
                reply_window=reply_window,
            )
        }

    async def _switch(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        name = payloads.read_text(payload, "name")
        on = payloads.read_flag(payload, "on")
        previous = await self._core.flip_switch(name, on)
        return {"name": name, "on": on, "previous": previous}

    async def _live(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """The Live Toggle. One action, and every surface calls this one."""
        return payloads.call_document(await self._core.live_toggle())

    async def _relay(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """An Answer Relay: the user's own words, carrying the user's authority.

        There is deliberately no action for system-authored words: a surface
        asking for one would be a surface claiming to be the system.
        """
        outcome = await self._core.relay(
            payloads.read_target(payload),
            payloads.read_text(payload, "text"),
            route=payloads.read_route(payload),
        )
        return payloads.relay_document(outcome)

    async def _approve(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        approval_id = payloads.read_text(payload, "approval_id")
        verdict = payloads.read_verdict(payload)
        outcome = await self._core.answer_approval(approval_id, verdict)
        if outcome is None:
            raise NothingPending(
                f"nothing is waiting under {approval_id!r} — it was answered or it expired"
            )
        return payloads.approval_document(outcome)

    async def _verify(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return payloads.verification_document(await self._core.verify())

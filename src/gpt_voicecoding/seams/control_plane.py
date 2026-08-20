"""The control plane — an interface Bridge Core *exposes*, rather than calls out through.

Status queries and switch flips, carried as JSON over a Unix domain socket.

Surfaces that speak it: the menu-bar shell, ``bridgectl``, the Companion Channel,
and spoken commands inside a Live Call. It is never gated by any switch — see
ADR 0002, which is absolute.

This module is the **vocabulary**, and only the vocabulary: the closed action
set, the closed error set, and the two envelopes every line on the wire is one
of. It lives in ``seams`` for the reason every other seam's vocabulary does —
it is the one thing both sides share, and here both sides are the engine and
whichever surface is talking to it. Framing, sockets and ownership checks are
mechanism and live in ``gpt_voicecoding.control_plane``; rendering Bridge Core's
answers into a payload is translation and lives there too.

**Both sets are closed.** Adding an action or an error code is a change to a
contract the Swift shell implements against, not a convenience — the payload
shapes are written down in ``docs/control-plane.md`` for exactly that reason.

The bound on a request is stated here rather than chosen by whichever side is
reading, because a limit only one side knows is a limit that surprises the
other.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any

#: Bumped when the shapes below change incompatibly. Carried on every reply so a
#: surface can tell an engine that disagrees from one too old to have been asked.
PROTOCOL_VERSION = 1

#: The longest line either side will read. Generous for a roster, small enough
#: that a peer cannot make the engine hold an unbounded buffer.
MAX_REQUEST_BYTES = 64 * 1024


class Action(StrEnum):
    """Everything a surface may ask for. Closed, and free of legacy aliases.

    The reference implementation's CLI kept its superseded Stop command beside
    the current one; this set starts clean, and every name here is also a
    Companion Channel command word (``/status``) and a ``bridgectl`` subcommand.
    """

    #: Everything the hub knows: switches, roster, call, pending work.
    STATUS = "status"
    #: Flip one switch. Never gated — ADR 0002.
    SWITCH = "switch"
    #: The Session roster on its own, for a surface that only renders that.
    SESSIONS = "sessions"
    #: The Live Toggle: end the call the system owns, or start one if none is up.
    LIVE = "live"
    #: Bring exactly one Session into existence in a workspace.
    LAUNCH = "launch"
    #: Close exactly one Session, by exact identity.
    CLOSE = "close"
    #: An Answer Relay — the user's own words, for one exact Session.
    RELAY = "relay"
    #: The user's verdict on one pending permission request.
    APPROVE = "approve"
    #: What the engine actually loaded behind each seam — ADR 0003.
    VERIFY = "verify"


class ErrorCode(StrEnum):
    """Why a request was refused. Closed, so a surface can branch on it.

    A refusal keeps its identity across the wire: Bridge Core's refusals map
    onto these codes, and ``message`` carries the refusal's own words, which
    surfaces render verbatim rather than rephrase.
    """

    #: The line was not one JSON object this protocol can represent.
    MALFORMED_REQUEST = "malformed_request"
    #: A well-formed line naming an action this engine does not have.
    UNKNOWN_ACTION = "unknown_action"
    #: The action is known; what came with it is not usable.
    INVALID_PAYLOAD = "invalid_payload"
    #: No switch by that name is registered on this engine.
    UNKNOWN_SWITCH = "unknown_switch"
    #: No Session by that identity was ever registered here.
    UNKNOWN_SESSION = "unknown_session"
    #: Known session id, unreachable under that identity — a fork, or an end.
    STALE_SESSION = "stale_session"
    #: Nothing pending under that id — never queued, or already answered.
    UNKNOWN_PENDING = "unknown_pending"
    #: Something asked to open a voice surface while the system owns one.
    SECOND_CALL_REFUSED = "second_call_refused"
    #: The Launcher tried and could not — the real error travels in `message`.
    LAUNCH_FAILED = "launch_failed"
    #: The close was attempted and failed. An idempotent repeat is a success.
    CLOSE_FAILED = "close_failed"
    #: Nothing is loaded behind the seam this action needs.
    SEAM_UNAVAILABLE = "seam_unavailable"
    #: Any other refusal Bridge Core raised. Still carries its own words.
    REFUSED = "refused"
    #: Raised by a *surface*, never by the engine: nothing answered the socket.
    ENGINE_UNREACHABLE = "engine_unreachable"


class MalformedRequest(Exception):
    """A document that cannot be read as a request. Carries the code to send back."""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


def _frozen(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(payload))


@dataclass(frozen=True, slots=True)
class Request:
    """One thing a surface is asking for, and what it brought with it."""

    action: Action
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", _frozen(self.payload))

    def as_document(self) -> dict[str, Any]:
        return {"action": str(self.action), "payload": dict(self.payload)}

    @classmethod
    def of(cls, document: Any) -> Request:
        """Read one decoded JSON document, refusing anything that is not a request."""
        if not isinstance(document, dict):
            raise MalformedRequest(ErrorCode.MALFORMED_REQUEST, "a request is a JSON object")

        raw = document.get("action")
        if not isinstance(raw, str):
            raise MalformedRequest(ErrorCode.MALFORMED_REQUEST, "a request names an action")
        try:
            action = Action(raw)
        except ValueError:
            raise MalformedRequest(
                ErrorCode.UNKNOWN_ACTION, f"this engine has no action called {raw!r}"
            ) from None

        payload = document.get("payload", {})
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            raise MalformedRequest(
                ErrorCode.MALFORMED_REQUEST, f"the payload of {raw!r} is not an object"
            )
        return cls(action=action, payload=payload)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Request):
            return NotImplemented
        return self.action is other.action and dict(self.payload) == dict(other.payload)

    def __hash__(self) -> int:
        return hash((self.action, tuple(sorted(self.payload))))


@dataclass(frozen=True, slots=True)
class ReplyError:
    """Why one request was refused, in the refusal's own words."""

    code: ErrorCode
    message: str

    def __post_init__(self) -> None:
        if not self.message.strip():
            raise ValueError("a refusal must carry the words a surface will render")


@dataclass(frozen=True, slots=True)
class Reply:
    """The one answer to one request. Exactly one of `data` or `error` is set."""

    ok: bool
    #: The action being answered, or None when the line never named a usable one.
    action: Action | None = None
    data: Mapping[str, Any] = field(default_factory=dict)
    error: ReplyError | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "data", _frozen(self.data))
        if self.ok and self.error is not None:
            raise ValueError("an answer does not also carry a refusal")
        if not self.ok and self.error is None:
            raise ValueError("a refusal must say why")

    @classmethod
    def answered(cls, action: Action, data: Mapping[str, Any] | None = None) -> Reply:
        return cls(ok=True, action=action, data=data or {})

    @classmethod
    def refused(cls, action: Action | None, code: ErrorCode, message: str) -> Reply:
        return cls(ok=False, action=action, error=ReplyError(code=code, message=message))

    def as_document(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "ok": self.ok,
            "action": str(self.action) if self.action is not None else None,
            "protocol": PROTOCOL_VERSION,
        }
        if self.ok:
            document["data"] = dict(self.data)
        else:
            assert self.error is not None  # __post_init__ guarantees it
            document["error"] = {"code": str(self.error.code), "message": self.error.message}
        return document

    @classmethod
    def of(cls, document: Any) -> Reply:
        """Read one decoded reply. A surface uses this; the engine never does."""
        if not isinstance(document, dict):
            raise MalformedRequest(ErrorCode.MALFORMED_REQUEST, "a reply is a JSON object")

        raw = document.get("action")
        action = Action(raw) if isinstance(raw, str) else None

        if document.get("ok") is True:
            data = document.get("data", {})
            if not isinstance(data, dict):
                raise MalformedRequest(ErrorCode.MALFORMED_REQUEST, "an answer carries an object")
            return cls(ok=True, action=action, data=data)

        error = document.get("error")
        if not isinstance(error, dict):
            raise MalformedRequest(ErrorCode.MALFORMED_REQUEST, "a refusal must say why")
        try:
            code = ErrorCode(error.get("code"))
        except ValueError:
            code = ErrorCode.REFUSED
        message = error.get("message")
        return cls(
            ok=False,
            action=action,
            error=ReplyError(
                code=code, message=message if isinstance(message, str) and message.strip() else "?"
            ),
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Reply):
            return NotImplemented
        return (
            self.ok is other.ok
            and self.action is other.action
            and dict(self.data) == dict(other.data)
            and self.error == other.error
        )

    def __hash__(self) -> int:
        return hash((self.ok, self.action, self.error))

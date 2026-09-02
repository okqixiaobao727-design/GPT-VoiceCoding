"""Publish one progress observation as a compact roster summary or exact detail.

This private deep module owns every publication capacity. Agent adapters receive
only its derived capture ceiling at composition; callers never choose a budget.
The projection and canonical Reply encoding live here as later vertical slices
exercise them through the Control Plane action and socket seams (ADR 0016).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gpt_voicecoding.control_plane import payloads
from gpt_voicecoding.core.bridge import Status
from gpt_voicecoding.core.sessions import Session
from gpt_voicecoding.seams.agent import (
    ProgressAvailability,
    ProgressCapture,
    ProgressEntry,
    ProgressObservation,
    ProgressOmission,
    ProgressRole,
    ReplyWindow,
)
from gpt_voicecoding.seams.control_plane import (
    MAX_REQUEST_BYTES,
    Action,
    ErrorCode,
    Reply,
)
from gpt_voicecoding.seams.identity import AgentKind, SessionTarget


def encode_reply(reply: Reply) -> bytes:
    """The canonical Control Plane wire encoding, including its line delimiter."""
    return json.dumps(reply.as_document(), ensure_ascii=False).encode("utf-8") + b"\n"


@dataclass(frozen=True, slots=True)
class _ProgressCapture(ProgressCapture):
    """Validated source capture policy derived from a complete Reply capacity."""

    max_bytes: int

    def __post_init__(self) -> None:
        if self.max_bytes <= 0:
            raise ValueError("the progress capture capacity must be positive")

    def select(
        self,
        entries: Sequence[ProgressEntry],
    ) -> tuple[tuple[ProgressEntry, ...], ProgressOmission]:
        kept = list(entries)
        while kept and self._encoded_size(kept) > self.max_bytes:
            kept.pop(0)
        if not entries:
            omission = ProgressOmission.NONE
        elif not kept:
            omission = ProgressOmission.NEWEST_OVERSIZE
        elif len(kept) < len(entries):
            omission = ProgressOmission.OLDER
        else:
            omission = ProgressOmission.NONE
        return tuple(kept), omission

    @staticmethod
    def _encoded_size(entries: Sequence[ProgressEntry]) -> int:
        return len(
            json.dumps(
                [{"role": str(entry.role), "text": entry.text} for entry in entries],
                ensure_ascii=False,
            ).encode("utf-8")
        )


@dataclass(frozen=True, slots=True)
class ProgressPublication:
    """The one progress publication policy for this engine process."""

    max_bytes: int = MAX_REQUEST_BYTES
    _capture: _ProgressCapture = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.max_bytes <= 0:
            raise ValueError("the Control Plane publication capacity must be positive")
        object.__setattr__(self, "_capture", self._derived_capture())

    @property
    def capture(self) -> ProgressCapture:
        """The single publication-derived source policy supplied to both adapters."""
        return self._capture

    def status_document(self, status: Status) -> dict[str, Any]:
        """The whole status payload, with progress projected as roster summaries."""
        return payloads.status_document(
            status,
            progress_for=self.summary_document,
        )

    def exact_document(
        self,
        session: Session,
        *,
        reply_window: ReplyWindow,
    ) -> dict[str, Any]:
        """One exact Session payload with the newest whole progress tail that fits."""
        observation = session.progress
        entries = list(observation.recent)
        omission = observation.omission

        while True:
            data = {
                "session": payloads.session_document(
                    session,
                    progress=self._document(
                        observation,
                        recent=tuple(entries),
                        omission=omission,
                    ),
                    reply_window=reply_window,
                )
            }
            reply = Reply.answered(Action.PROGRESS, data)
            if self.fits(reply) or not entries:
                return data

            entries.pop(0)
            omission = ProgressOmission.OLDER if entries else ProgressOmission.NEWEST_OVERSIZE

    def summary_document(self, observation: ProgressObservation) -> dict[str, Any]:
        """The uniform progress fields for a roster row, carrying no chat body."""
        omission = observation.omission
        if observation.availability is ProgressAvailability.READABLE and observation.has_history:
            omission = ProgressOmission.STATUS_SUMMARY
        return self._document(observation, recent=(), omission=omission)

    def final(self, reply: Reply) -> Reply:
        """Return a wire-safe reply, or a small refusal when its skeleton cannot fit."""
        if self.fits(reply):
            return reply
        bounded = Reply.refused(
            reply.action,
            ErrorCode.REFUSED,
            "the complete Control Plane reply exceeds its byte limit",
        )
        if not self.fits(bounded):
            raise ValueError("the Control Plane capacity cannot carry its bounded refusal")
        return bounded

    def fits(self, reply: Reply) -> bool:
        """Whether the complete canonical wire line fits this publication."""
        return len(encode_reply(reply)) <= self.max_bytes

    def _derived_capture(self) -> _ProgressCapture:
        """Subtract the smallest valid exact Reply envelope from the largest capacity."""
        probe = ProgressEntry(role=ProgressRole.ASSISTANT, text="x")
        observation = ProgressObservation.from_capture(
            recent=(probe,),
            omission=ProgressOmission.NONE,
            read_at=datetime(1970, 1, 1, tzinfo=UTC),
        )
        session = Session(
            target=SessionTarget(agent=AgentKind.CODEX, session_id="x"),
            workspace=Path("."),
            first_seen=0.0,
            progress=observation,
        )
        progress = self._document(
            observation,
            recent=observation.recent,
            omission=observation.omission,
        )
        minimum = Reply.answered(
            Action.PROGRESS,
            {
                "session": payloads.session_document(
                    session,
                    progress=progress,
                    reply_window=ReplyWindow.CLOSED,
                )
            },
        )
        envelope_bytes = len(encode_reply(minimum)) - _ProgressCapture._encoded_size((probe,))
        capacities = (max(1, self.max_bytes - envelope_bytes),)
        return _ProgressCapture(max(capacities))

    @staticmethod
    def _document(
        observation: ProgressObservation,
        *,
        recent: tuple[ProgressEntry, ...],
        omission: ProgressOmission,
    ) -> dict[str, Any]:
        return {
            "availability": str(observation.availability),
            "has_history": observation.has_history,
            "omission": str(omission),
            "read_at": (
                observation.read_at.isoformat() if observation.read_at is not None else None
            ),
            "recent": [{"role": str(entry.role), "text": entry.text} for entry in recent],
        }

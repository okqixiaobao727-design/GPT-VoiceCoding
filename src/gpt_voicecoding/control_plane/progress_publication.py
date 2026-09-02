"""Publish what a Session said: a roster summary, or a History page.

This private deep module owns every publication capacity. Agent adapters receive
only its derived capture ceiling at composition; callers never choose a budget.
The projection and canonical Reply encoding live here as later vertical slices
exercise them through the Control Plane action and socket seams (ADR 0016).

**Two publications, and the second is bounded differently.** The roster summary
carries no chat body at all; the History page carries a *count* of entries, with
the encoded Reply's byte limit as a ceiling that omits an entry's text rather
than its slot. The exact-detail publication that stood between them retired with
the `progress` action (#171) — the Session Brief carries the newest entry whole
and a page carries that entry and everything before it.
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
    HistoryPage,
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


#: The widest ordinal this module will assume when it sizes a page's slots.
#: Nineteen digits is every count a 64-bit integer can reach, so a bound computed
#: with it holds for every record any Session will ever have — and the bound is
#: then a pure function of the dial and the ceiling rather than of whichever page
#: happens to be in hand. Sizing on the live ordinal would let one configuration
#: be legal at ordinal 9 and illegal at ordinal 10.
_WIDEST_ORDINAL = 10**18


@dataclass(frozen=True, slots=True)
class ProgressPublication:
    """The one publication policy for this engine process."""

    max_bytes: int = MAX_REQUEST_BYTES
    _capture: _ProgressCapture = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.max_bytes <= 0:
            raise ValueError("the Control Plane publication capacity must be positive")
        object.__setattr__(self, "_capture", self._derived_capture())

    @property
    def largest_page(self) -> int:
        """The most entry slots this capacity can carry, measured rather than assumed.

        A slot is an `ordinal`, a `role` and `omission="oversize"` — the shape an
        entry takes when its text could not be carried, which is the largest a
        page can be while still saying something true about every entry on it.
        Measured at the widest ordinal and the longer role name, so the answer is
        a floor and never an optimistic one, and so it is a pure function of this
        capacity rather than of whichever page happens to be in hand.

        **Read at composition, where the dial and the ceiling meet** (#171).
        `[policy] history_page_entries` is `CorePolicy`'s and the capacity is
        this module's; neither can answer alone whether a page can be published,
        so `engine/composition.py` asks this and refuses a configuration whose
        page could only ever be every entry marked `oversize`.
        """
        sizes = [self._slots_size(count) for count in (0, 1, 2)]
        first, each = sizes[1] - sizes[0], sizes[2] - sizes[1]
        if self.max_bytes < sizes[1]:
            return 0
        return 1 + (self.max_bytes - sizes[0] - first) // each

    def _slots_size(self, count: int) -> int:
        """The encoded Reply for a page of `count` omitted slots, at their widest."""
        page = HistoryPage(
            entries=tuple(
                ProgressEntry(
                    ordinal=_WIDEST_ORDINAL - index,
                    role=ProgressRole.ASSISTANT,
                    text="x",
                )
                for index in range(count)
            ),
            older=True,
            read_at=datetime(1970, 1, 1, tzinfo=UTC),
        )
        document = self._history_document(page, omitted={entry.ordinal for entry in page.entries})
        return len(encode_reply(Reply.answered(Action.HISTORY, document)))

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

    def history_document(self, page: HistoryPage) -> dict[str, Any]:
        """One History page, bounded by its count and ceilinged by the wire (#171).

        **The count is the page and the bytes are the ceiling.** Every entry the
        window selected keeps its slot; an entry that would push the encoded
        Reply past its limit is published as *existing but omitted* — its
        `ordinal`, its `role`, `omission="oversize"` and no text — so the page
        always advances and one large message never blocks the ones before it.
        Text is never cut (ADR 0016).

        The largest entries go first, because dropping the text of the one entry
        that does not fit is the smallest honest edit; ties break oldest-first so
        the newest entry a user is most likely to be reading survives longest.

        **There is no case left where the slots themselves do not fit.** Slots
        are not free, so a `[policy] history_page_entries` dialled past what this
        capacity carries would have no honest publication — every entry marked
        `oversize` when the truth is that the page was dialled too big. That
        configuration is refused where it is composed rather than papered over
        here (`__post_init__`), which is ADR 0016's own rule — capacity decides
        what the source may be asked for — applied to the count instead of to
        the bytes. So the fully-omitted page below always fits.
        """
        omitted: set[int] = set()
        order = sorted(
            page.entries,
            key=lambda entry: (-len(entry.text.encode("utf-8")), entry.ordinal),
        )
        for entry in order:
            document = self._history_document(page, omitted=omitted)
            if self.fits(Reply.answered(Action.HISTORY, document)):
                return document
            omitted.add(entry.ordinal)
        return self._history_document(page, omitted=omitted)

    def _history_document(self, page: HistoryPage, *, omitted: set[int]) -> dict[str, Any]:
        return {
            "entries": [
                (
                    {
                        "ordinal": entry.ordinal,
                        "role": str(entry.role),
                        "omission": str(ProgressOmission.OVERSIZE),
                    }
                    if entry.ordinal in omitted
                    else {
                        "ordinal": entry.ordinal,
                        "role": str(entry.role),
                        "text": entry.text,
                    }
                )
                for entry in page.entries
            ],
            "older": page.older,
            "read_at": page.read_at.isoformat() if page.read_at is not None else None,
        }

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
        probe = ProgressEntry(ordinal=0, role=ProgressRole.ASSISTANT, text="x")
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
            Action.BRIEF,
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

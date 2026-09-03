"""The numbers the policy pipelines run on, in one place and configurable.

These are locked *defaults*, not constants: the Approval Relay budget is
"600 s, configurable" by decision, and a ceiling baked into the pipeline that
enforces it is a number nobody can change without a release. The Relay queue
already refuses to own its own deadline for the same reason — it holds the
deadline it is handed.

Nothing here is a policy *decision*; the decisions live in the pipelines. This
is only the dial they read.
"""

from __future__ import annotations

from dataclasses import dataclass

#: The queued-delivery ceiling: ten minutes, then a reported failure.
DEFAULT_RELAY_CEILING_SECONDS = 600.0

# **No dial names a hold duration** (ADR 0015, amended by #191). The Approval
# Relay's budget lived here and was read in two places — the pending-approval
# sweep and the parked-question sweep. A held hook's life is the wire's to bound,
# so there is nothing left for policy to say about it.

#: One legacy heartbeat of silence: 1 × 60 seconds, then end the owned call.
#: legacy@1d32845:config.plist:74-78; bridge/config.py:94-96.
DEFAULT_SILENCE_END_SECONDS = 60.0

#: How long after any end of a call the system stays off the voice side: 30
#: seconds. **New** — legacy has no cool-down at all, and `livecall.py:561-581`
#: is the incident that shows why one is needed
#: (`legacy@1d32845:bridge/livecall.py:561-581`, ADR 0010).
DEFAULT_COOL_DOWN_SECONDS = 30.0

#: How long a human pause is allowed to be before it counts as silence: 5
#: seconds. Legacy's heartbeat settle window, **adapted** — same shape, a
#: different cause (`legacy@1d32845:config.plist:86-87`,
#: `legacy@1d32845:bridge/livecall.py:528-536`).
#:
#: It covers the *pause*, and nothing about audio. Playout lag used to have to
#: be added on top, because `VoiceSpeech(speaking=False)` meant the Voice had
#: finished generating and the speaker was still draining; since #195 that edge
#: means the audio has finished playing and the transport's own buffer is the
#: realtime adapter's to wait out (`seams/call.py::VoiceSpeech`). So this number
#: is a statement about people, measured in nothing.
DEFAULT_SPEECH_SETTLE_SECONDS = 5.0

#: How many entries one History page holds, both roles counted (#171, ADR 0016).
#: Legacy's tail was 12 messages / 32 KB with no cursor of any kind
#: (`legacy@1d32845:config.plist:449-452`); **dropped, because** a fixed tail
#: cannot answer "the five before those". Five is what the 0901 flow asks to be
#: read out in one breath.
DEFAULT_HISTORY_PAGE_ENTRIES = 5


@dataclass(frozen=True, slots=True)
class CorePolicy:
    """Every configurable dial the pipelines read. Passed in, never imported.

    Durations and one count: what they have in common is that they are dialled
    in `[policy]` and given meaning by whichever pipeline reads them, never by
    this type.
    """

    #: How long an unsolicited Relay may wait for the Reply Window to open.
    relay_ceiling_seconds: float = DEFAULT_RELAY_CEILING_SECONDS
    #: How long an owned Live Call may have no user or system activity.
    silence_end_seconds: float = DEFAULT_SILENCE_END_SECONDS
    #: How long after any end of a call the Call Keeper will not dial again.
    cool_down_seconds: float = DEFAULT_COOL_DOWN_SECONDS
    #: How long after either side stops speaking before silence starts counting.
    speech_settle_seconds: float = DEFAULT_SPEECH_SETTLE_SECONDS
    #: How many entries one History page holds. A count, not a byte budget: the
    #: encoded Reply's ceiling stays a ceiling the wire applies (ADR 0016).
    #: Only positivity is checked here — whether the line can carry that many
    #: entry *slots* is the Control Plane's capacity to answer, and composition
    #: refuses the pair (`engine/composition.py`).
    history_page_entries: int = DEFAULT_HISTORY_PAGE_ENTRIES

    def __post_init__(self) -> None:
        for name, seconds in (
            ("relay_ceiling_seconds", self.relay_ceiling_seconds),
            ("silence_end_seconds", self.silence_end_seconds),
            ("cool_down_seconds", self.cool_down_seconds),
            ("speech_settle_seconds", self.speech_settle_seconds),
        ):
            if seconds <= 0:
                raise ValueError(
                    f"{name} must be a real duration; {seconds!r} would expire everything "
                    "the moment it was accepted"
                )
        if isinstance(self.history_page_entries, bool) or not isinstance(
            self.history_page_entries, int
        ):
            raise ValueError("history_page_entries is a whole number of entries")
        if self.history_page_entries <= 0:
            raise ValueError(
                f"history_page_entries must hold at least one entry; "
                f"{self.history_page_entries!r} would page through nothing forever"
            )

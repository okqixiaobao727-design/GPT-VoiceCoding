"""What was configured against what is actually loaded — ADR 0003, from the hub.

The rule the ADR fixes has two halves, and they live in different places. The
half an *adapter* owns is `seams.verify`: which implementation this is, and
whether its far side answers. The half only the *hub* can own is here, because
only the hub knows what configuration asked for — and the reference
implementation's outage was exactly this comparison never being made: the status
line echoed the client's own configuration back and called it an observation.

So the composition root records what it loaded behind each seam, hands that
inventory to Bridge Core, and this decides. Three outcomes, not two: a machine
that deliberately runs without an adapter and one that silently lost its adapter
must not look the same, so "nothing configured anywhere" is handed to the
operator rather than passed or failed.

The root records only what was *configured*. What is loaded is the adapter's own
answer, never the root's record of what it constructed: the root knows what it
built, the adapter knows what it is, and ADR 0003 is about the second — the
reference outage was a line that looked like an observation and was an echo.

**What is compared is presence, not spelling.** Configuration names a factory
(`module:attribute`); an adapter reports its own implementation string. Those
are two notations for the same fact, and demanding they match character for
character would fail on every correctly wired machine — the cry-wolf failure
ADR 0003 explicitly refuses. The disagreement that matters is the one the
reference outage was made of: a seam was named and the engine is running with
nothing behind it, or nothing was named and something is loaded anyway.

**One thing this deliberately cannot catch**: configuration naming a real
adapter whose factory resolved to the null one. Telling those apart would need
a table here of which references are null, which is the hub growing
adapter-shaped knowledge — and within one process, what was configured is what
was constructed, so it cannot happen without a defect in the composition root
itself. The residual is stated rather than papered over.

Every seam here is pluggable and every one of them has a `verify` verb, so
`reported` being absent means one thing only: nothing is loaded behind that
seam at all — which is **not** what an adapter reporting an empty `loaded`
means. That one is the null implementation, present and answering, and an engine
that deliberately runs without a seam filled says so by naming it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from gpt_voicecoding.seams.verify import VerifyOutcome, VerifyResult

#: The names an inventory uses for the seams that can be filled. The Agent seam
#: is named per agent — one adapter each — so it carries a prefix rather than a
#: single name, and the composition root appends the `AgentKind`.
CALL_SEAM = "call"
CHANNEL_SEAM = "companion_channel"
LAUNCHER_SEAM = "session_launcher"
AGENT_SEAM_PREFIX = "agent."


@runtime_checkable
class Verifiable(Protocol):
    """What every pluggable seam offers: an adapter answering for itself."""

    async def verify(self) -> VerifyResult: ...


@dataclass(frozen=True, slots=True)
class SeamLoad:
    """One seam, as configuration named it. The loaded side is the adapter's to say.

    `configured` is **empty** when nothing was named, which is a known state
    (ADR 0003) and distinct from the seam not being listed at all — that means
    this engine was never asked about it.
    """

    seam: str
    configured: str


@dataclass(frozen=True, slots=True)
class SeamVerification:
    """The hub's answer about one seam. What a surface renders, verbatim."""

    seam: str
    outcome: VerifyOutcome
    configured: str
    loaded: str
    detail: str = ""


def compare(load: SeamLoad, reported: VerifyResult | None) -> SeamVerification:
    """Judge one seam. `reported` is None when nothing is loaded behind it.

    The two ways a seam can have "nothing real" behind it are **not** the same
    thing, and reading them as one was a defect: `reported is None` means no
    adapter is there at all, while an adapter reporting an empty `loaded` is the
    *null implementation*, which is a real implementation that answered — the
    seam contract says so in as many words. Collapsing them made an engine that
    deliberately runs without text reach, and names the null adapter to say so,
    report the outage it was configured to avoid.
    """
    if reported is None:
        if not load.configured:
            return SeamVerification(
                seam=load.seam,
                outcome=VerifyOutcome.MANUAL,
                configured=load.configured,
                loaded="",
                detail="nothing is configured behind this seam, and nothing is loaded",
            )
        return SeamVerification(
            seam=load.seam,
            outcome=VerifyOutcome.FAIL,
            configured=load.configured,
            loaded="",
            detail=f"configuration names {load.configured}; the engine loaded nothing",
        )

    if not reported.loaded:
        # The null implementation, answering for itself. Its own outcome is the
        # answer — but only if it is the one outcome that shape may carry: a
        # `VerifyResult` refuses MANUAL beside a real module string and cannot
        # refuse PASS beside an empty one, so that combination is caught here
        # rather than trusted through.
        if reported.outcome is not VerifyOutcome.MANUAL:
            return SeamVerification(
                seam=load.seam,
                outcome=VerifyOutcome.FAIL,
                configured=load.configured,
                loaded="",
                detail=(
                    f"an adapter reported {reported.outcome} while naming no implementation, "
                    "which is not a state this system has"
                ),
            )
        return SeamVerification(
            seam=load.seam,
            outcome=reported.outcome,
            configured=load.configured,
            loaded="",
            detail=reported.detail,
        )

    if not load.configured:
        return SeamVerification(
            seam=load.seam,
            outcome=VerifyOutcome.FAIL,
            configured=load.configured,
            loaded=reported.loaded,
            detail=f"configuration names nothing; the engine loaded {reported.loaded}",
        )

    return SeamVerification(
        seam=load.seam,
        outcome=reported.outcome,
        configured=load.configured,
        loaded=reported.loaded,
        detail=reported.detail,
    )

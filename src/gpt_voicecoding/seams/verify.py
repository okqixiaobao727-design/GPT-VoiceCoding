"""Liveness, as ADR 0003 defines it — for every seam whose adapter is pluggable.

The rule the ADR fixes: a liveness check reports what the engine *actually
loaded*, never what a configuration file says it should have loaded. So `verify`
is a seam verb, and an adapter answers two things about itself — which
implementation it is, and whether it can positively reach its far side.

`loaded` is the module string of the real implementation, and the **empty
string** when the null implementation is loaded. Empty is a *known* state, and
it is distinct from the field being absent, which means an engine too old to have
been asked — a distinction that lives on the control-plane wire, not here.

Comparing `loaded` against configuration, and naming the disagreement rather than
silently picking one side, is Bridge Core's job: only the hub knows what was
configured.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class VerifyOutcome(StrEnum):
    """Three outcomes, not two. The third is the one that matters."""

    PASS = "pass"
    FAIL = "fail"
    #: Nothing is configured anywhere — the honest shape of a question the check
    #: cannot answer, handed to the operator rather than passed or failed.
    MANUAL = "manual"


@dataclass(frozen=True, slots=True)
class VerifyResult:
    """What one adapter reports about itself when asked."""

    outcome: VerifyOutcome
    loaded: str
    detail: str = ""

    def __post_init__(self) -> None:
        if self.outcome is VerifyOutcome.FAIL and not self.detail.strip():
            raise ValueError("a failed verify must name the layer that failed")
        if self.outcome is VerifyOutcome.MANUAL and self.loaded:
            raise ValueError(
                "a manual outcome means nothing is configured, so nothing real is loaded"
            )

"""The numbers the policy pipelines run on, in one place and configurable.

Both of these are locked *defaults*, not constants: the Approval Relay budget is
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

#: The Approval Relay budget: ten minutes, then `ask` — never deny.
DEFAULT_APPROVAL_BUDGET_SECONDS = 600.0


@dataclass(frozen=True, slots=True)
class CorePolicy:
    """Every configurable duration the pipelines read. Passed in, never imported."""

    #: How long an unsolicited Relay may wait for the Reply Window to open.
    relay_ceiling_seconds: float = DEFAULT_RELAY_CEILING_SECONDS
    #: How long the user has to answer a pending permission request by voice.
    approval_budget_seconds: float = DEFAULT_APPROVAL_BUDGET_SECONDS

    def __post_init__(self) -> None:
        for name, seconds in (
            ("relay_ceiling_seconds", self.relay_ceiling_seconds),
            ("approval_budget_seconds", self.approval_budget_seconds),
        ):
            if seconds <= 0:
                raise ValueError(
                    f"{name} must be a real duration; {seconds!r} would expire everything "
                    "the moment it was accepted"
                )

"""The Claude lane's journey, against a real `claude` the harness started by hand.

One test, nine steps, one verdict per step. It is deliberately *not* a fail-fast
test: `Walk` records every step and the assertion at the end is on the lane's
whole verdict, so one expensive walk reports every red rather than the first.

What this lane is expected to find on today's `main` is stated on #73 rather than
guessed at here — the nine steps are the red lines #74–#80 clear, and every one
of them is red until the ticket that owns it lands.
"""

from __future__ import annotations

import journey
import pytest
import support

pytestmark = pytest.mark.acceptance

LANE = journey.CLAUDE


@pytest.mark.parametrize("lane", [LANE], indirect=True)
def test_the_claude_lane(
    lane, lane_engine, hand_started_session, person, journal, verdict, request
):  # noqa: ANN001
    engine, config, bridgectl = lane_engine
    session, started_at = hand_started_session

    verified = bridgectl("verify")
    if not verified.ok:
        pytest.skip(f"`bridgectl verify` refused against this run's config: {verified.text}")

    walk = journey.Walk(
        lane=lane,
        session=session,
        engine=engine,
        config=config,
        bridgectl=bridgectl,
        person=person,
        journal=journal,
        verdict=verdict,
        far_side=request.getfixturevalue("far_side"),
        environment=request.getfixturevalue("terminal_environment"),
        started_at=started_at,
    )
    walk.walk()

    # #44: the engine unlinked its socket but left its approval directory behind.
    # Recorded rather than graded — it is a real open bug and a real detector, but
    # it is not one of the nine names the build tickets cite. Checked after the
    # engine is down, because that is when the listener stops.
    engine.stop()
    leftovers = sorted(config.socket_path.parent.glob("vc-approvals-*"))
    verdict.observe(
        LANE.name,
        "approval directory removed (#44)",
        f"{config.socket_path.parent} holds {[str(path) for path in leftovers] or 'nothing'}",
    )

    failed = [step for step in verdict.lanes[LANE.name] if step.result != support.PASS]
    assert not failed, "\n".join(f"{step.step}: {step.result} — {step.evidence}" for step in failed)

"""Both lanes' journey, against real agents the harness started by hand.

One test per lane, nine steps each, one verdict per step. It is deliberately
*not* a fail-fast test: `Walk` records every step and the assertion at the end is
on the lane's whole verdict, so one expensive walk reports every red rather than
the first.

**One test, parametrised, rather than one module per lane.** The two used to be
separate files whose bodies were the same forty lines with one name changed —
and the same forty lines is where a fix applied to one lane and not the other
comes from. The lane is a value (`journey.Lane`), so the difference between the
lanes lives entirely in that value; pytest's own parametrisation names the lane
in the test id, so a failure still says which lane failed without a module per
lane to say it.

What each lane is expected to find on today's `main` is stated on #73 rather than
guessed at here: the nine steps are the red lines #74–#80 clear, and each is red
until the ticket that owns it lands.
"""

from __future__ import annotations

import journey
import pytest
import support

pytestmark = pytest.mark.acceptance


@pytest.mark.parametrize("lane", journey.LANES, indirect=True, ids=lambda one: one.name)
def test_the_lane(lane, lane_engine, hand_started_session, person, journal, verdict, request):  # noqa: ANN001, ANN201
    engine, config, bridgectl = lane_engine
    session, started_at = hand_started_session

    verified = bridgectl("verify")
    if not verified.ok:
        # A refusal, not a skip: the design says preflight refuses with `REFUSED`
        # and the reason, and a skipped lane that left no row would have been a
        # lane the verdict could not tell apart from a lane that passed.
        verdict.refuse(
            lane.name, f"`bridgectl verify` refused against this run's config: {verified.text}"
        )
        pytest.fail(f"`bridgectl verify` refused against this run's config: {verified.text}")

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
    # Recorded rather than graded — a real open bug and a real detector, but not
    # one of the nine names the build tickets cite. Checked after the engine is
    # down, because that is when the listener stops.
    engine.stop()
    leftovers = sorted(config.socket_path.parent.glob("vc-approvals-*"))
    verdict.observe(
        lane.name,
        "approval directory removed (#44)",
        f"{config.socket_path.parent} holds {[str(path) for path in leftovers] or 'nothing'}",
    )

    failed = [step for step in verdict.lanes[lane.name] if step.result != support.PASS]
    assert not failed, "\n".join(f"{step.step}: {step.result} — {step.evidence}" for step in failed)

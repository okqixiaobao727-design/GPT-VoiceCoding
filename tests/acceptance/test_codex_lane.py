"""The Codex lane's journey, against the real `codex`.

Same eight steps as the Claude lane, and the same walk — a real agent is a real
agent. What differs is what today's `main` does to it: ADR 0009 records a measured
deadlock on this lane (`thread/resume` refused with `no rollout found` until a
turn has started, and an unsubscribed client never hearing `thread/status/changed`),
entered by every Session the product launches bare. The Opening Instruction that
would make it unreachable is unimplemented, so this lane is expected to be redder
than the Claude one, and the point of the run is to say exactly how.
"""

from __future__ import annotations

import journey
import pytest
import support

pytestmark = pytest.mark.acceptance

LANE = journey.Lane(name="codex", agent="codex", project="acceptance-codex")


@pytest.mark.parametrize("lane_engine", [LANE.name], indirect=True)
def test_the_codex_lane(lane_engine, person, journal, verdict, request) -> None:
    engine, config, bridgectl = lane_engine

    verified = bridgectl("verify")
    if not verified.ok:
        pytest.skip(f"`bridgectl verify` refused against this run's config: {verified.text}")

    walk = journey.Walk(
        lane=LANE,
        engine=engine,
        config=config,
        bridgectl=bridgectl,
        person=person,
        journal=journal,
        verdict=verdict,
        far_side=request.getfixturevalue("far_side"),
    )
    walk.walk()

    # #44: the engine unlinked its socket but left its approval directory behind.
    # Checked after the engine is down, because that is when the listener stops.
    engine.stop()
    leftovers = sorted(config.socket_path.parent.glob("vc-approvals-*"))
    verdict.record(
        LANE.name,
        "8b approval directory removed (#44)",
        support.PASS if not leftovers else support.FAIL,
        f"{config.socket_path.parent} holds {[str(path) for path in leftovers] or 'nothing'}",
    )

    failed = [step for step in verdict.lanes[LANE.name] if step.result != support.PASS]
    assert not failed, "\n".join(f"{step.step}: {step.result} — {step.evidence}" for step in failed)

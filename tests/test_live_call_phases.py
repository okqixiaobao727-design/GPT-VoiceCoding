"""The fast contract for selecting one phase of the Live Call walk (#223)."""

from __future__ import annotations

import subprocess
import sys

import journey
import live_call
import live_call_step
import pytest
import support


def test_no_phase_selection_grades_the_whole_walk_in_ticket_order() -> None:
    selected = live_call_step.select_phases()

    assert selected.graded == live_call_step.PHASES
    assert selected.arranged == ()
    assert selected.phases == live_call_step.PHASES


def test_detail_is_graded_on_arranged_dial_and_relay_ground() -> None:
    selected = live_call_step.select_phases(("detail",))

    assert selected.graded == ("detail",)
    assert selected.arranged == ("dial", "relay")
    assert selected.phases == ("dial", "relay", "detail")


def test_ground_closure_is_transitive_and_keeps_ticket_order(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(live_call_step, "PHASES", ("dial", "relay", "detail"))
    monkeypatch.setattr(
        live_call_step,
        "PHASE_GROUND",
        {"dial": (), "relay": ("dial",), "detail": ("relay",)},
    )

    selected = live_call_step.select_phases(("detail",))

    assert selected.graded == ("detail",)
    assert selected.arranged == ("dial", "relay")
    assert selected.phases == ("dial", "relay", "detail")


def test_repeated_phase_selection_is_deduplicated_in_ticket_order() -> None:
    selected = live_call_step.select_phases(("hang-up", "relay", "hang-up"))

    assert selected.graded == ("relay", "hang-up")
    assert selected.arranged == ("dial",)
    assert selected.phases == ("dial", "relay", "hang-up")


def test_every_phase_has_backwards_only_declared_ground() -> None:
    assert tuple(live_call_step.PHASE_GROUND) == live_call_step.PHASES
    for phase, ground in live_call_step.PHASE_GROUND.items():
        assert set(ground) <= {"dial", "relay"}
        assert all(
            live_call_step.PHASES.index(one) < live_call_step.PHASES.index(phase) for one in ground
        )


def test_heard_fragments_come_from_the_sentences_put_on_the_track(tmp_path) -> None:  # noqa: ANN001
    settings = live_call.HarnessSettings(
        observations=tmp_path / "observations", wav_directory=tmp_path
    )

    assert live_call_step.HEARD_FRAGMENTS == live_call.HEARD_FRAGMENTS
    assert set(live_call.HEARD_FRAGMENTS) == set(settings.requests)
    assert all(
        fragment in settings.requests[variant]
        for variant, fragment in live_call.HEARD_FRAGMENTS.items()
    )


def test_an_unknown_phase_refuses_with_the_phase_list() -> None:
    with pytest.raises(live_call_step.UnknownPhase) as refused:
        live_call_step.select_phases(("details",))

    assert "'details'" in str(refused.value)
    assert ", ".join(live_call_step.PHASES) in str(refused.value)


def test_phase_without_the_live_call_step_is_a_usage_error() -> None:
    collected = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "tests/acceptance/test_lanes.py",
            "--step",
            "roster",
            "--phase",
            "detail",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    refusal = collected.stdout + collected.stderr
    assert collected.returncode != pytest.ExitCode.OK
    assert "--phase requires --step 'live call'" in refusal
    assert ", ".join(live_call_step.PHASES) in refusal


class _Journey:
    def __init__(self) -> None:
        self.observations: list[tuple[str, str]] = []

    def observe(self, what: str, detail: str) -> None:
        self.observations.append((what, detail))


def test_the_phase_outlet_writes_every_field_in_one_fixed_order() -> None:
    journey = _Journey()
    result = live_call_step._PhaseResult(
        phase="detail",
        rule="the Voice carries the Session's newest message",
        source="#198 ruling 5",
        graded={"answer carried newest": True},
        recorded={"brief verbs": ["brief session"]},
        engine_held="newest: the dictated reply",
    )

    detail = result.finish(journey, disposition="graded")

    assert journey.observations == [("live call detail", detail)]
    fields = (
        "graded phase detail",
        "rule: the Voice carries the Session's newest message",
        "source: #198 ruling 5",
        "graded facts: answer carried newest=True",
        "recorded facts: brief verbs=['brief session']",
        "engine held: newest: the dictated reply",
    )
    assert [detail.index(field) for field in fields] == sorted(
        detail.index(field) for field in fields
    )


def test_a_failed_graded_fact_is_recorded_before_the_one_phase_raise_site() -> None:
    journey = _Journey()
    result = live_call_step._PhaseResult(
        phase="detail",
        rule="the Voice carries the Session's newest message",
        source="#198 ruling 5",
        graded={"answer carried newest": False},
        recorded={"brief verbs": []},
        engine_held="newest: the dictated question",
        failed=("answer carried newest",),
    )

    with pytest.raises(support.StepFailed) as failed:
        result.finish(journey, disposition="graded")

    assert journey.observations == [("live call detail", str(failed.value))]
    assert "failed facts: answer carried newest" in str(failed.value)


def test_a_failed_arranged_fact_blocks_the_lane_in_the_same_fixed_shape() -> None:
    journey = _Journey()
    result = live_call_step._PhaseResult(
        phase="relay",
        rule="direct Relay settles History and Brief",
        source="#223 story 15",
        graded={},
        recorded={"history settled": False, "brief settled": True},
        engine_held="newest: the dictated question",
        failed=("history settled",),
    )

    with pytest.raises(support.LaneBlocked) as failed:
        result.finish(journey, disposition="arranged")

    assert journey.observations == [("live call relay", str(failed.value))]
    assert str(failed.value).startswith("arranged phase relay;")


def test_walk_live_call_binds_the_phase_selection_to_the_phase_module(monkeypatch) -> None:  # noqa: ANN001
    selected = live_call_step.select_phases(("detail",))
    walk = object.__new__(journey.Walk)
    walk.phase_selection = selected
    received: list[tuple[object, live_call_step.PhaseSelection]] = []

    def run(bound: object, phases: live_call_step.PhaseSelection) -> str:
        received.append((bound, phases))
        return "detail passed"

    monkeypatch.setattr(live_call_step, "run", run)

    assert walk.live_call() == "detail passed"
    assert received == [(walk, selected)]

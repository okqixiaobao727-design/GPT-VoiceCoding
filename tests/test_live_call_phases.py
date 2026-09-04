"""The fast contract for selecting one phase of the Live Call walk (#223)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import journey
import live_call
import live_call_step
import pytest
import support


def test_no_phase_selection_grades_the_whole_walk_in_ticket_order() -> None:
    assert live_call_step.PHASES == (
        "dial",
        "hand-over",
        "relay",
        "detail",
        "history",
        "long answer",
        "mid-call news",
        "hang-up",
        "undelivered",
    )

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
    assert live_call_step.PHASE_GROUND == {
        "dial": (),
        "hand-over": ("dial",),
        "relay": ("dial",),
        "detail": ("dial", "relay"),
        "history": ("dial", "relay"),
        "long answer": ("dial",),
        "mid-call news": ("dial", "relay"),
        "hang-up": ("dial",),
        "undelivered": (),
    }
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


def test_brief_and_live_call_share_one_newest_message_reader() -> None:
    printed = "二号工位 · Reply READY\n  state: idle\n  newest: the dictated reply\n"

    assert live_call_step._newest_message is journey._newest_message
    assert journey._newest_message(printed) == "the dictated reply"


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


def test_the_phase_usage_error_does_not_need_the_acceptance_extra() -> None:
    """The refusal above is a fact about the command line, not about the venv.

    `tests/acceptance/conftest.py` used to `importorskip("telethon")` at import,
    which aborts the conftest before the hook carrying that refusal is registered.
    CI installs `.[dev]` and never `.[acceptance]`, so the refusal was unreachable
    in the one place it is graded (#198). The import is what this pins: a conftest
    that imports without the actor is what keeps the refusal reachable.
    """
    # A finder that refuses telethon the way an uninstalled package does. Assigning
    # `sys.modules['telethon'] = None` raises a bare `ImportError` instead, which is
    # not what a venv without the extra does and would prove the wrong thing.
    without_the_actor = (
        "import sys\n"
        "class Refuse:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'telethon' or name.startswith('telethon.'):\n"
        "            raise ModuleNotFoundError(f'No module named {name!r}', name=name)\n"
        "        return None\n"
        "sys.meta_path.insert(0, Refuse())\n"
        "import conftest\n"
        "assert conftest.telegram_person is None\n"
        "assert conftest.pytest_runtest_setup is not None\n"
    )
    imported = subprocess.run(
        [sys.executable, "-c", without_the_actor],
        capture_output=True,
        text=True,
        check=False,
        cwd=Path(live_call_step.__file__).parent,
    )

    assert imported.returncode == 0, imported.stdout + imported.stderr


def test_only_an_in_call_phase_runs_inside_the_walks_first_call() -> None:
    """`undelivered` holds its own call, so a selection of it alone has no in-call phase.

    The step-level transport fact asks whether a wav utterance could have reached
    the track, and only an in-call phase puts one there. Grading it against a
    selection that chose no in-call phase failed run `20260904T102740Z` for not
    exercising what it never selected, while the fact it did select was green.
    """
    assert live_call_step.OUTSIDE_THE_FIRST_CALL == "undelivered"
    assert live_call_step.in_call_phases(("undelivered",)) == ()
    assert live_call_step.in_call_phases(("detail", "undelivered")) == ("detail",)
    assert live_call_step.in_call_phases(live_call_step.PHASES) == tuple(
        phase for phase in live_call_step.PHASES if phase != "undelivered"
    )
    # The whole run always selects in-call phases, so it still grades the fact.
    assert live_call_step.in_call_phases(live_call_step.select_phases().phases)


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

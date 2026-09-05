"""What the run config has to say for a mic-free Live Call to be possible (#183).

Three wirings, none of which can be read off the acceptance run itself — that
costs a real call and a real bot — and all of which are ordinary code:

* the derived config points `[adapters] call` at the harness's own adapter and
  gives it its per-lane settings;
* it points `[delegate] cli` at a wrapper that logs every `bridgectl` the Call
  Agent runs, by **absolute** path, so a PATH shadow cannot intercept it;
* the engine is given a `PYTHONPATH` on which `live_call` resolves, because the
  engine is the bundle's interpreter and the module is the harness's.

A wiring that is wrong here fails the acceptance run twenty minutes and one call
in. That is what these are for.
"""

from __future__ import annotations

import inspect
import os
import re
import stat
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import journey
import live_call
import live_call_step
import pytest
import support

from gpt_voicecoding.core.instructions import (
    ControlPlaneCli,
    InstructionContext,
    voice_instructions,
)
from gpt_voicecoding.seams.call import SpokenBrief, SpokenRosterBrief

A_CONFIG = """
[engine]
socket_path = "/tmp/never.sock"
state_path = "/tmp/never.json"

[log]
path = "/tmp/never.log"
max_bytes = 8388608
retained_files = 3
level = "INFO"
stripped_environment_prefixes = ["CLAUDE", "CODEX"]

[delegate]
model = "a-model"
cli = "/usr/bin/true"

[adapters]
call = "gpt_voicecoding.adapters.call.realtime:realtime_call"
companion_channel = "gpt_voicecoding.adapters.companion_channel.telegram:telegram_channel"

[adapters.agents]
claude = "gpt_voicecoding.adapters.agent.claude:claude_agent"
codex = "gpt_voicecoding.adapters.agent.codex:codex_agent"

[adapters.settings.companion_channel]
token_env = "GPTVOICECODING_TELEGRAM_TOKEN"
chat_id = "8675309"
"""


def _nowhere(event: str, **fields: object) -> None:  # noqa: ARG001
    """A journal that writes nowhere: these tests grade the config, not the log."""


def _derived(tmp_path: Path, name: str = "claude", **extra: object) -> support.DerivedConfig:
    source = tmp_path / "config.toml"
    source.write_text(A_CONFIG)
    return support.derive_config(
        source=source,
        run_directory=tmp_path / f"engine-{name}",
        workspace=tmp_path / f"workspace-{name}",
        socket_path=tmp_path / f"{name}.sock",
        project_name=f"acceptance-{name}",
        **extra,  # type: ignore[arg-type]
    )


def _written(derived: support.DerivedConfig) -> dict:
    return tomllib.loads(derived.path.read_text())


# --- the transport seam -----------------------------------------------------


def test_the_call_seam_is_left_alone_when_no_lane_asks_for_a_spoken_call(tmp_path: Path) -> None:
    """The default is still the user's real config: this run accepts their engine."""
    written = _written(_derived(tmp_path))
    assert written["adapters"]["call"] == "gpt_voicecoding.adapters.call.realtime:realtime_call"
    assert "call" not in written["adapters"]["settings"]


def test_a_spoken_call_names_the_harness_adapter_and_its_own_paths(tmp_path: Path) -> None:
    derived = _derived(tmp_path, harness_live_call=True)
    written = _written(derived)
    assert written["adapters"]["call"] == live_call.REFERENCE
    table = written["adapters"]["settings"]["call"]
    assert table["observations"] == str(derived.call_observations)
    assert Path(table["wav_directory"]).is_relative_to(tmp_path)


def test_the_harness_table_is_one_the_harness_adapter_can_actually_read(tmp_path: Path) -> None:
    """The config's own keys and the module's own reader, checked against each other.

    A table this file wrote and no adapter ever read is exactly the failure that
    only shows up on a real call.
    """
    derived = _derived(tmp_path, harness_live_call=True)
    table = _written(derived)["adapters"]["settings"]["call"]
    harness, remainder = live_call.HarnessSettings.split(table)
    assert harness.observations == derived.call_observations
    assert harness.request == live_call.REQUEST
    # Whatever is left has to be readable by the shipped adapter's own settings.
    from gpt_voicecoding.adapters.call.realtime.settings import RealtimeCallSettings

    RealtimeCallSettings.of(remainder)


def test_two_lanes_never_share_an_observation_file(tmp_path: Path) -> None:
    """Both lanes hold this call at once, and one file would be two runs' notes."""
    paths = {
        lane.name: _derived(tmp_path, lane.name, harness_live_call=True).call_observations
        for lane in journey.LANES
    }
    assert len(set(paths.values())) == len(journey.LANES), paths


# --- the CLI wrapper --------------------------------------------------------


def test_the_wrapper_is_named_by_absolute_path_so_a_shadow_cannot_intercept(
    tmp_path: Path,
) -> None:
    """The engine puts `[delegate] cli` into the instructions verbatim
    (`composition.py:_instruction_context`), so an absolute path is what makes
    the Call Agent's `bridgectl` this run's wrapper rather than whatever its
    PATH resolves."""
    derived = _derived(
        tmp_path, harness_live_call=True, control_plane_cli=tmp_path / "bin" / "real"
    )
    stated = _written(derived)["delegate"]["cli"]
    assert Path(stated).is_absolute()
    assert stated == str(derived.cli_wrapper)


def test_the_wrapper_logs_every_run_and_still_runs_the_real_cli(tmp_path: Path) -> None:
    """One invocation in, one line out, and the real CLI's own output through."""
    real = tmp_path / "real-bridgectl"
    real.write_text('#!/bin/sh\necho "real saw: $*"\nexit 0\n')
    real.chmod(real.stat().st_mode | stat.S_IXUSR)

    wrapper = support.write_cli_wrapper(tmp_path / "wrapper", real=real, log=tmp_path / "runs.log")
    assert os.access(wrapper, os.X_OK)

    finished = subprocess.run(
        [str(wrapper), "live"], capture_output=True, text=True, timeout=30.0, check=False
    )
    assert finished.returncode == 0
    assert "real saw: live" in finished.stdout

    logged = support.cli_wrapper_runs(tmp_path / "runs.log")
    assert len(logged) == 1
    assert "live" in logged[0]
    # UTC, so two lanes' logs and the engine log can be read on one timeline.
    assert logged[0].endswith("Z live") or " live" in logged[0]
    assert logged[0][:4].isdigit()


def test_the_wrapper_carries_the_real_clis_exit_code_back(tmp_path: Path) -> None:
    """A refusal has to stay a refusal: the Call Agent branches on it."""
    real = tmp_path / "real-bridgectl"
    real.write_text("#!/bin/sh\nexit 3\n")
    real.chmod(real.stat().st_mode | stat.S_IXUSR)
    wrapper = support.write_cli_wrapper(tmp_path / "wrapper", real=real, log=tmp_path / "runs.log")
    finished = subprocess.run([str(wrapper)], capture_output=True, timeout=30.0, check=False)
    assert finished.returncode == 3


def test_every_run_is_logged_rather_than_the_last_one(tmp_path: Path) -> None:
    """The step's assertion is a lower bound (#181 finding 1), so the log appends."""
    real = tmp_path / "real-bridgectl"
    real.write_text("#!/bin/sh\nexit 0\n")
    real.chmod(real.stat().st_mode | stat.S_IXUSR)
    wrapper = support.write_cli_wrapper(tmp_path / "wrapper", real=real, log=tmp_path / "runs.log")
    for arguments in (["status"], ["live"], ["live"]):
        subprocess.run([str(wrapper), *arguments], timeout=30.0, check=True)
    assert len(support.cli_wrapper_runs(tmp_path / "runs.log")) == 3


def test_no_wrapper_log_is_no_runs_rather_than_an_error(tmp_path: Path) -> None:
    assert support.cli_wrapper_runs(tmp_path / "never.log") == []


# --- the engine's import path -----------------------------------------------


def test_the_engine_can_import_the_harness_adapter_by_the_name_it_is_configured_under(
    tmp_path: Path,
) -> None:
    """`[adapters] call` is a `module:attribute` the *engine* imports, and the
    engine is the bundle's interpreter, which knows nothing about `tests/`."""
    derived = _derived(tmp_path, harness_live_call=True)
    engine = support.Engine(
        config=derived,
        bundle=tmp_path / "bundle",
        journal=_nowhere,
        token="a-token",
        path_value="/usr/bin:/bin",
    )
    roots = engine.environment["PYTHONPATH"].split(os.pathsep)
    assert str(support.harness_root()) in roots
    assert (support.harness_root() / f"{live_call.REFERENCE.split(':')[0]}.py").exists()


def test_an_existing_pythonpath_is_extended_rather_than_replaced(tmp_path: Path) -> None:
    """The user's own environment reaches the engine (`Engine.environment`), and
    dropping an entry of it would be this harness changing what it accepts."""
    derived = _derived(tmp_path, harness_live_call=True)
    engine = support.Engine(
        config=derived,
        bundle=tmp_path / "bundle",
        journal=_nowhere,
        token="a-token",
        path_value="/usr/bin:/bin",
        environment={"PYTHONPATH": "/somewhere/of/theirs"},
    )
    roots = engine.environment["PYTHONPATH"].split(os.pathsep)
    assert "/somewhere/of/theirs" in roots
    assert str(support.harness_root()) in roots


# --- the tenth step ---------------------------------------------------------


def test_the_live_call_step_runs_alone(tmp_path: Path) -> None:  # noqa: ARG001
    """#183's blocker clause: this step must be runnable by itself.

    Alone means *without a walk*, not without a setup step: v1's second phase
    has the engine dial about a Session that stopped, so it brings `roster` and
    nothing else (#195). One setup step is what gives it an address; the clause
    is about not having to walk the other nine to reach the call.
    """
    chosen = journey.select(["live call"])
    assert chosen.selected == ("live call",)
    assert chosen.setup == ("roster",)
    assert chosen.steps == ("roster", "live call")
    assert not chosen.whole_lane


def test_the_live_call_steps_are_last_so_a_full_run_dials_after_it_has_walked(
    tmp_path: Path,  # noqa: ARG001
) -> None:
    """A call holds the interlock, so on a whole-lane run they come after the
    steps that drive turns rather than in the middle of them."""
    assert journey.STEPS[-len(journey.LIVE_CALL_STEPS) :] == journey.LIVE_CALL_STEPS


def test_the_step_is_bound_to_a_method_like_every_other_name(tmp_path: Path) -> None:  # noqa: ARG001
    for step in journey.LIVE_CALL_STEPS:
        assert step in journey.PREREQUISITES
        assert step in journey.STEPS
    assert hasattr(journey.Walk, "live_call")


def test_the_three_call_steps_are_one_walk(tmp_path: Path) -> None:  # noqa: ARG001
    """#198 folds v0's route, v1's dial and v2's mid-call news into one step.

    The names went with them: a run asking for `live call long` used to get one
    call's worth of the flow, and what proves the 0901 flow is the whole walk.
    Selecting a name nothing answers to is a refusal rather than an empty run.
    """
    assert journey.LIVE_CALL_STEPS == ("live call",)
    assert "live call long" not in journey.STEPS
    assert "live call briefed" not in journey.STEPS
    assert not hasattr(journey.Walk, "live_call_long")
    assert not hasattr(journey.Walk, "live_call_briefed")


def test_only_a_run_that_walks_the_step_swaps_the_users_call_adapter(tmp_path: Path) -> None:
    """Every other step accepts the Call adapter the user actually configured."""
    without = _written(_derived(tmp_path, "a", harness_live_call=False))
    assert without["adapters"]["call"] != live_call.REFERENCE
    assert without["delegate"]["cli"] == "/usr/bin/true"


# --- what the two lanes heard differently -----------------------------------


def test_a_recogniser_that_splits_a_word_still_counts_as_having_heard_it() -> None:
    """Run `20260902T093755Z`: one lane logged `结束通话` and the other `结束通 话`.

    The same four seconds of synthesised audio, two engines, two transcripts.
    The request carries no spaces, so a space in the transcript is the
    recogniser's — and a substring test that counted one graded which recogniser
    answered rather than whether the words arrived (#181 finding 1).
    """
    heard = (
        "2026-09-02 21:40:04,522 INFO gpt_voicecoding.core.bridge: user speech, "
        "for the voice thread to act on: '那个你把电话挂了吧,我想让你结束通 话'"
    )
    assert live_call_step.LIVE_CALL_HEARD_SUBSTRING not in heard
    assert live_call_step._unspaced(
        live_call_step.LIVE_CALL_HEARD_SUBSTRING
    ) in live_call_step._unspaced(heard)


def test_the_substring_is_really_part_of_the_request_the_harness_speaks() -> None:
    """A fragment nothing puts on the track would never be heard at all."""
    assert live_call_step.LIVE_CALL_HEARD_SUBSTRING in live_call.REQUEST
    assert live_call_step.LIVE_CALL_LONG_HEARD_SUBSTRING in live_call.LONG_REQUEST


def test_the_long_request_asks_for_no_hang_up() -> None:
    """Run `20260902T162146Z`: one sentence asking for both got neither graded.

    The Voice counted for 220s and the Call Agent then ran nothing at all, so
    #183's hand-off assertion went red for a reason that was not #184's. The two
    asks are two utterances now, and this is the half that must not carry one.
    """
    assert live_call_step.LIVE_CALL_HEARD_SUBSTRING not in live_call.LONG_REQUEST
    assert live_call.REQUEST != live_call.LONG_REQUEST


def test_the_wrapper_records_under_a_path_with_a_space_in_it(tmp_path: Path) -> None:
    """The acceptance's real run directory is under `~/Library/Application Support`.

    Unquoted, `>> $log` is an ambiguous redirect and `exec $real` names nothing,
    so every run the Call Agent made would vanish — which is what run
    `20260902T093755Z` recorded: two lanes, a call each, and two empty wrapper
    logs. A `tmp_path` has no spaces, so only a test that puts one there sees it.
    """
    spaced = tmp_path / "Application Support" / "a run"
    spaced.mkdir(parents=True)
    real = spaced / "real bridgectl"
    real.write_text('#!/bin/sh\necho "real saw: $*"\nexit 0\n')
    real.chmod(real.stat().st_mode | stat.S_IXUSR)

    wrapper = support.write_cli_wrapper(spaced / "wrapper", real=real, log=spaced / "runs.log")
    finished = subprocess.run(
        [str(wrapper), "live"], capture_output=True, text=True, timeout=30.0, check=False
    )
    assert finished.returncode == 0, finished.stderr
    assert "real saw: live" in finished.stdout
    assert len(support.cli_wrapper_runs(spaced / "runs.log")) == 1


# --- the verbs the Call Agent chose -----------------------------------------


def test_the_verbs_are_read_off_the_wrapper_log_including_invented_ones(
    tmp_path: Path,
    monkeypatch,  # noqa: ANN001
) -> None:
    """Run `20260902T095448Z`'s two lanes, verbatim.

    The codex lane ran `--help` and then `live`; the claude lane ran `call end`,
    which is not an action this engine serves. Both are recorded: a verb the
    parser dropped for not matching anything known would hide exactly the guess
    #195 is deferred to fix.
    """
    log = tmp_path / "runs.log"
    log.write_text(
        "2026-09-02T09:57:05Z --socket /tmp/a/control.sock --help\n"
        "2026-09-02T09:57:12Z --socket /tmp/a/control.sock live\n"
        "2026-09-02T09:55:35Z --socket /tmp/a/control.sock call end\n"
    )
    walk = object.__new__(journey.Walk)
    walk.config = support.DerivedConfig(
        path=tmp_path / "config.toml",
        socket_path=tmp_path / "s.sock",
        state_path=tmp_path / "state.json",
        log_path=tmp_path / "engine.log",
        project_name="acceptance",
        workspace=tmp_path,
        token_variable="T",
        chat_id="1",
        cli_wrapper_log=log,
    )
    assert live_call_step._LiveCallRun(walk)._verbs_run() == ["live", "call end"]


def test_a_wrapper_log_of_only_options_names_no_verb(tmp_path: Path) -> None:
    """`--help` alone is a run the lower bound counts and a verb it does not."""
    log = tmp_path / "runs.log"
    log.write_text("2026-09-02T09:57:05Z --socket /tmp/a/control.sock --help\n")
    walk = object.__new__(journey.Walk)
    walk.config = support.DerivedConfig(
        path=tmp_path / "config.toml",
        socket_path=tmp_path / "s.sock",
        state_path=tmp_path / "state.json",
        log_path=tmp_path / "engine.log",
        project_name="acceptance",
        workspace=tmp_path,
        token_variable="T",
        chat_id="1",
        cli_wrapper_log=log,
    )
    assert live_call_step._LiveCallRun(walk)._verbs_run() == []
    assert len(support.cli_wrapper_runs(log)) == 1


# --- who ended the call -----------------------------------------------------


def test_a_call_that_went_down_on_its_own_is_not_credited_to_the_agent() -> None:
    """A dropped connection also leaves `bridgectl status` saying `call: none`.

    Reading only that clock made the verdict carry two contradicting sentences:
    an end reason saying the connection went away by itself, beside a claim that
    the Call Agent ended it. The audio path is the one that knows.
    """
    lost = "the connection went away by itself: ICE failed"
    assert live_call_step._ended_by(end_reason=lost, by_ceiling=False, by_agent=True) == "lost"
    assert live_call_step._ended_by(end_reason=lost, by_ceiling=False, by_agent=False) == "lost"


def test_an_ending_the_agent_asked_for_is_the_agents() -> None:
    closed = "this side closed the audio path"
    assert live_call_step._ended_by(end_reason=closed, by_ceiling=False, by_agent=True) == "agent"


def test_a_call_the_engines_own_ceiling_ended_is_not_the_agents() -> None:
    """Run `20260902T162146Z` recorded `agent` for a call nobody asked to end.

    The ceiling closes the audio path from this side, exactly as a `bridgectl
    live` the Call Agent ran does, so the end reason cannot tell them apart. The
    engine's own log line is the only thing that can (#184).
    """
    closed = "this side closed the audio path"
    assert live_call_step._ended_by(end_reason=closed, by_ceiling=True, by_agent=True) == "ceiling"
    assert live_call_step._ended_by(end_reason=closed, by_ceiling=True, by_agent=False) == "ceiling"


def test_a_connection_that_went_away_is_a_loss_before_it_is_a_ceiling() -> None:
    """The audio path is asked first, as it already was for the agent."""
    lost = "the connection went away by itself: ICE failed"
    assert live_call_step._ended_by(end_reason=lost, by_ceiling=True, by_agent=True) == "lost"


def test_an_ending_the_step_had_to_make_itself_is_the_harnesss() -> None:
    """Green does not depend on the verb guess, and the guess stays visible."""
    closed = "this side closed the audio path"
    assert (
        live_call_step._ended_by(end_reason=closed, by_ceiling=False, by_agent=False) == "harness"
    )


def test_no_end_reason_at_all_still_says_who_was_waited_on() -> None:
    assert live_call_step._ended_by(end_reason=None, by_ceiling=False, by_agent=True) == "agent"
    assert live_call_step._ended_by(end_reason=None, by_ceiling=False, by_agent=False) == "harness"


def test_every_answer_is_its_own_argument_rather_than_one_that_is_inferred() -> None:
    """The bug the shape had: `by_agent` was read off "the call was down".

    That is true of a call the ceiling ended, a call that dropped and a call the
    step ended itself, so a caller with no reason to believe the Call Agent
    ended anything could not say so. Every answer is now keyword-only and
    stated (#184).
    """
    parameters = inspect.signature(live_call_step._ended_by).parameters
    assert [name for name in parameters] == ["end_reason", "by_ceiling", "by_agent"]
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY for parameter in parameters.values()
    )
    assert all(parameter.default is inspect.Parameter.empty for parameter in parameters.values())


# --- the ceiling the Voice holds open ---------------------------------------


class _Engine:
    """Only the half of the engine handle these readings touch: its log."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    def log_lines(self) -> list[str]:
        return self._lines


def _walk(tmp_path: Path, *, lines: list[str] | None = None) -> live_call_step._LiveCallRun:
    walk = object.__new__(journey.Walk)
    walk.engine = _Engine(lines or [])
    walk.config = support.DerivedConfig(
        path=tmp_path / "config.toml",
        socket_path=tmp_path / "s.sock",
        state_path=tmp_path / "state.json",
        log_path=tmp_path / "engine.log",
        project_name="acceptance",
        workspace=tmp_path,
        token_variable="T",
        chat_id="1",
    )
    return live_call_step._LiveCallRun(walk)


def _said(message: str) -> str:
    """One engine log line, in the shape `engine/logfile.py` writes."""
    return f"2026-09-03 10:00:00,000 INFO gpt_voicecoding.core.bridge: {message}"


def test_only_an_open_voice_span_after_the_previous_ask_mark_blocks_the_next(
    tmp_path: Path,
) -> None:
    """Each ask owns the spans after its mark; an older call cannot block this one (#223)."""
    before = [_said(live_call_step.VOICE_SPEAKING_LINE)]
    after = [
        _said(live_call_step.VOICE_QUIET_LINE),
        _said(live_call_step.VOICE_SPEAKING_LINE),
    ]
    walk = _walk(tmp_path, lines=[*before, *after])

    assert walk._a_voice_span_is_open(since=len(before)) is True

    walk.engine._lines.append(_said(live_call_step.VOICE_QUIET_LINE))
    assert walk._a_voice_span_is_open(since=len(before)) is False


@pytest.mark.parametrize(
    "activity",
    (
        live_call_step.VOICE_SPEAKING_LINE,
        live_call_step.VOICE_QUIET_LINE,
        live_call_step.VOICE_SAID_LINE % "回答",
    ),
)
def test_each_voice_signal_counts_as_activity_after_the_stimulus(
    tmp_path: Path, activity: str
) -> None:
    walk = _walk(tmp_path, lines=[_said("older noise"), _said(activity)])

    assert walk._voice_was_active(since=1) is True
    assert walk._voice_was_active(since=2) is False


class _PacingClock:
    """A monotonic clock advanced only by the harness sleeps."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_a_new_voice_span_restarts_the_settle_window_before_the_next_ask(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A momentary gap is not settled if the Voice starts speaking in it (#223)."""
    clock = _PacingClock()
    monkeypatch.setattr(live_call_step.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(live_call_step.time, "sleep", clock.sleep)
    monkeypatch.setattr(live_call_step, "LIVE_CALL_POLL_SECONDS", 0.5)
    walk = _walk(tmp_path)
    monkeypatch.setattr(walk, "_call_is_down", lambda: False)
    monkeypatch.setattr(walk, "_speech_settle_seconds", lambda: 2.0)
    monkeypatch.setattr(
        walk,
        "_a_voice_span_is_open",
        lambda *, since: clock.now < 2.0 or 3.5 <= clock.now < 4.5,
    )
    monkeypatch.setattr(walk, "_voice_was_active", lambda *, since: True, raising=False)
    facts = live_call_step._PhaseFacts("graded")

    walk._wait_for_a_quiet_track(live_call.LONG, since=17, activity_owed=True, facts=facts)

    assert clock.now == pytest.approx(6.5)
    assert facts.graded == {}
    assert facts.failed == []
    assert facts.recorded["voice track waits"] == [
        f"{live_call.LONG}: owed=True, settled=True, waited=6.5s"
    ]


def test_no_edges_wait_for_the_delayed_voice_answer_before_settling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run `20260904T050406Z`: 0/0 edges preceded a delayed Voice start.

    A bare settle window is not proof that the previous answer finished when
    the backend has not started it yet. The gate must first observe activity,
    then the closing edge, then one uninterrupted settle window.
    """
    clock = _PacingClock()
    monkeypatch.setattr(live_call_step.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(live_call_step.time, "sleep", clock.sleep)
    monkeypatch.setattr(live_call_step, "LIVE_CALL_POLL_SECONDS", 0.5)
    walk = _walk(tmp_path)

    class _DelayedVoice(_Engine):
        def log_lines(self) -> list[str]:
            if clock.now < 3.0:
                return []
            if clock.now < 4.0:
                return [_said(live_call_step.VOICE_SPEAKING_LINE)]
            return [
                _said(live_call_step.VOICE_SPEAKING_LINE),
                _said(live_call_step.VOICE_QUIET_LINE),
            ]

    walk.engine = _DelayedVoice([])
    monkeypatch.setattr(walk, "_call_is_down", lambda: False)
    monkeypatch.setattr(walk, "_speech_settle_seconds", lambda: 2.0)
    facts = live_call_step._PhaseFacts("graded", phase="history")

    walk._wait_for_a_quiet_track(live_call.HISTORY, since=0, activity_owed=True, facts=facts)

    assert clock.now == pytest.approx(6.0)
    assert facts.recorded["voice track waits"] == [
        f"{live_call.HISTORY}: owed=True, settled=True, waited=6.0s"
    ]


def test_a_mark_that_owes_no_activity_settles_on_an_empty_track(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A call opened under `ask_for_nothing` is quiet by contract (#223)."""
    clock = _PacingClock()
    monkeypatch.setattr(live_call_step.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(live_call_step.time, "sleep", clock.sleep)
    monkeypatch.setattr(live_call_step, "LIVE_CALL_POLL_SECONDS", 0.5)
    walk = _walk(tmp_path)
    monkeypatch.setattr(walk, "_call_is_down", lambda: False)
    monkeypatch.setattr(walk, "_speech_settle_seconds", lambda: 2.0)
    facts = live_call_step._PhaseFacts("graded", phase="hand-over")

    walk._wait_for_a_quiet_track(live_call.NEEDS, since=0, activity_owed=False, facts=facts)

    assert clock.now == pytest.approx(2.0)
    assert facts.recorded["voice track waits"] == [
        f"{live_call.NEEDS}: owed=False, settled=True, waited=2.0s"
    ]


def test_a_mark_that_owes_activity_still_blocks_when_voice_never_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The silent-opening carve-out cannot weaken a real stimulus (#223)."""
    clock = _PacingClock()
    monkeypatch.setattr(live_call_step.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(live_call_step.time, "sleep", clock.sleep)
    monkeypatch.setattr(live_call_step, "LIVE_CALL_ANSWER_SECONDS", 1.0)
    monkeypatch.setattr(live_call_step, "LIVE_CALL_POLL_SECONDS", 0.5)
    walk = _walk(tmp_path)
    monkeypatch.setattr(walk, "_call_is_down", lambda: False)
    monkeypatch.setattr(walk, "_speech_settle_seconds", lambda: 2.0)
    facts = live_call_step._PhaseFacts("graded", phase="history")

    with pytest.raises(support.LaneBlocked):
        walk._wait_for_a_quiet_track(live_call.HISTORY, since=0, activity_owed=True, facts=facts)

    assert facts.recorded["voice track waits"] == [
        f"{live_call.HISTORY}: owed=True, settled=False, waited=3.5s"
    ]


def _arranged_relay(
    tmp_path: Path, decision: str | None
) -> tuple[live_call_step._LiveCallRun, live_call_step._LiveCallState]:
    walk = _walk(
        tmp_path,
        lines=[_said("older activity"), _said(live_call_step.VOICE_QUIET_LINE)],
    )
    walk._voice_track = live_call_step._VoiceTrackMark(at=1, activity_owed=False)
    walk.lane = SimpleNamespace(name="claude")
    walk.journal = lambda *_, **__: None

    focus = SimpleNamespace(workspace=tmp_path, address=lambda _run: "claude:session")
    state = live_call_step._LiveCallState(
        focus=focus,  # type: ignore[arg-type]
        ringing=focus,  # type: ignore[arg-type]
        waiting=focus,  # type: ignore[arg-type]
        turn_seconds=1.0,
        ceiling_seconds=10.0,
        cool_down_seconds=1.0,
        speech_settle_seconds=1.0,
        opening_mark=0,
    )

    def bridgectl(verb: str, *_args: object, **_kwargs: object) -> support.Answer:
        if verb == "relay":
            if decision is not None:
                walk.engine._lines.append(_said(decision))
            return support.Answer((), 0, "state=delivered", "")
        if verb == "history":
            return support.Answer((), 0, live_call_step.LIVE_CALL_DICTATED_REPLY_SUBSTRING, "")
        assert verb == "brief"
        return support.Answer(
            (), 0, f"newest: {live_call_step.LIVE_CALL_DICTATED_REPLY_SUBSTRING}", ""
        )

    walk.bridgectl = bridgectl
    return walk, state


@pytest.mark.parametrize(
    ("decision", "expected_mark"),
    (
        (
            live_call_step.MID_CALL_SPOKEN_LINE % ("the dictated reply", "none"),
            live_call_step._VoiceTrackMark(at=2, activity_owed=True),
        ),
        (
            live_call_step.MID_CALL_NOTHING_LINE,
            live_call_step._VoiceTrackMark(at=1, activity_owed=False),
        ),
    ),
)
def test_arranged_relay_marks_only_a_decision_that_made_voice_speak(
    tmp_path: Path,
    decision: str,
    expected_mark: live_call_step._VoiceTrackMark,
) -> None:
    """The relay invocation is the latest stimulus only when Keeper spoke it (#223)."""
    walk, state = _arranged_relay(tmp_path, decision)
    facts = live_call_step._PhaseFacts("arranged", phase="relay")

    walk._arrange_relay(state, facts)

    assert walk._voice_track == expected_mark
    assert decision in facts.recorded["mid-call decision"]


def test_arranged_relay_blocks_when_keeper_makes_no_speech_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reply surfaces cannot tell whether the direct Relay made Voice speak (#223)."""
    walk, state = _arranged_relay(tmp_path, None)
    monkeypatch.setattr(support, "wait_for", lambda predicate, **_kwargs: predicate())

    with pytest.raises(support.LaneBlocked, match="no mid-call speech decision"):
        walk._arrange_relay(state, live_call_step._PhaseFacts("arranged", phase="relay"))


def test_focus_stop_becomes_the_latest_voice_stimulus_before_it_is_driven(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The next ask must wait on Voice activity caused by mid-call Focus news (#223)."""
    walk = _walk(tmp_path)
    walk._voice_track = live_call_step._VoiceTrackMark(at=0, activity_owed=False)
    focus = object()
    ringing = object()

    def drive(session: object, *_args: object) -> None:
        if session is ringing:
            walk.engine._lines.append(_said("the non-Focus Session rang"))
            return
        assert session is focus
        assert walk._voice_track == live_call_step._VoiceTrackMark(
            at=len(walk.engine.log_lines()), activity_owed=True
        )
        walk.engine._lines.extend(
            (
                _said(live_call_step.VOICE_QUIET_LINE),
                _said(
                    live_call_step.MID_CALL_SPOKEN_LINE
                    % (live_call_step.THE_QUESTION_ASKED, "none")
                ),
                _said(live_call_step.VOICE_SAID_LINE % live_call_step.THE_QUESTION_ASKED),
            )
        )

    monkeypatch.setattr(walk, "_drive_extra_session", drive)
    monkeypatch.setattr(walk, "_cue_lines", lambda *_args, **_kwargs: ["EVENT"])
    monkeypatch.setattr(walk, "_cue_order", lambda **_kwargs: ["EVENT"])
    monkeypatch.setattr(walk, "_call_line", lambda: "call: up")
    monkeypatch.setattr(
        walk, "_await_the_question", lambda *_args: live_call_step.THE_QUESTION_ASKED
    )
    monkeypatch.setattr(walk, "_while_the_call_is_up", lambda condition, **_kwargs: condition())

    facts = live_call_step._PhaseFacts("graded", phase="mid-call news")
    walk._mid_call_the_focus_session_speaks_and_the_rest_rings(
        mark=0,
        focus=focus,  # type: ignore[arg-type]
        focus_at=tmp_path / "focus",
        focus_address="claude:focus",
        ringing=ringing,  # type: ignore[arg-type]
        ringing_at=tmp_path / "ringing",
        ringing_address="claude:ringing",
        ringing_name="ringing",
        turn=1.0,
        cool_down=1.0,
        settle=1.0,
        facts=facts,
    )

    assert facts.failed == []


def test_an_unsettled_voice_track_blocks_the_phase_before_the_next_ask(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dirty track proves nothing about the product, so the next WAV stays out (#223)."""
    clock = _PacingClock()
    last_words = "还在回答上一句"
    monkeypatch.setattr(live_call_step.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(live_call_step.time, "sleep", clock.sleep)
    monkeypatch.setattr(live_call_step, "LIVE_CALL_ANSWER_SECONDS", 1.0)
    monkeypatch.setattr(live_call_step, "LIVE_CALL_POLL_SECONDS", 0.5)
    walk = _walk(
        tmp_path,
        lines=[
            _said(live_call_step.VOICE_SAID_LINE % last_words),
            _said(live_call_step.VOICE_SPEAKING_LINE),
        ],
    )
    monkeypatch.setattr(walk, "_call_is_down", lambda: False)
    monkeypatch.setattr(walk, "_speech_settle_seconds", lambda: 2.0)
    facts = live_call_step._PhaseFacts("graded", phase="long answer")

    with pytest.raises(support.LaneBlocked) as blocked:
        walk._wait_for_a_quiet_track(live_call.PLAIN, since=0, activity_owed=True, facts=facts)

    assert "phase 'long answer'" in str(blocked.value)
    assert f"variant {live_call.PLAIN!r}" in str(blocked.value)
    assert "waited 3.5s" in str(blocked.value)
    assert last_words in str(blocked.value)
    assert facts.recorded["voice track waits"] == [
        f"{live_call.PLAIN}: owed=True, settled=False, waited=3.5s"
    ]


def test_ask_by_voice_never_queues_after_the_track_wait_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pacing gate is inside the one ask primitive, ahead of its only queue (#223)."""
    walk = _walk(tmp_path)
    walk._voice_track = live_call_step._VoiceTrackMark(at=23, activity_owed=False)
    waited: list[tuple[str, int, bool]] = []
    queued: list[str] = []

    def block(variant: str, *, since: int, activity_owed: bool, facts: object) -> None:  # noqa: ARG001
        waited.append((variant, since, activity_owed))
        raise support.LaneBlocked("the Voice track did not settle")

    monkeypatch.setattr(walk, "_wait_for_a_quiet_track", block)
    monkeypatch.setattr(live_call, "ask_next", lambda _directory, variant: queued.append(variant))

    with pytest.raises(support.LaneBlocked):
        walk._ask_by_voice(live_call.LONG, live_call_step._PhaseFacts("graded"))

    assert waited == [(live_call.LONG, 23, False)]
    assert queued == []


def test_a_queued_ask_becomes_the_next_activity_owed_mark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    walk = _walk(tmp_path, lines=[_said("before"), _said("opening")])
    walk._voice_track = live_call_step._VoiceTrackMark(at=1, activity_owed=False)
    waited: list[tuple[int, bool]] = []

    monkeypatch.setattr(
        walk,
        "_wait_for_a_quiet_track",
        lambda _variant, *, since, activity_owed, facts: waited.append(  # noqa: ARG005
            (since, activity_owed)
        ),
    )
    monkeypatch.setattr(live_call, "ask_next", lambda *_args: None)
    monkeypatch.setattr(walk, "_while_the_call_is_up", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(walk, "_user_speech_landed_at", lambda *_args, **_kwargs: 2)

    walk._ask_by_voice(live_call.LONG, live_call_step._PhaseFacts("graded"))

    assert waited == [(1, False)]
    assert walk._voice_track == live_call_step._VoiceTrackMark(at=2, activity_owed=True)


def test_both_edges_of_the_voice_are_counted_off_the_engine_log(tmp_path: Path) -> None:
    """A finished answer: the span opened once and closed once (#184)."""
    walk = _walk(
        tmp_path,
        lines=[
            _said(live_call_step.VOICE_SPEAKING_LINE),
            _said(live_call_step.VOICE_QUIET_LINE),
        ],
    )
    assert walk._voice_speech_edges() == {True: 1, False: 1}


def test_an_answer_cut_in_half_leaves_a_start_edge_with_no_stop(tmp_path: Path) -> None:
    """The bug's own shape: the ceiling fired mid-answer, so no `done` ever came.

    This is what the long step grades on, and it is why the assertion counts
    edges rather than measuring a duration — no pace the Voice counts at can
    produce a stop edge from a call that was already hung up.
    """
    walk = _walk(tmp_path, lines=[_said(live_call_step.VOICE_SPEAKING_LINE)])
    assert walk._voice_speech_edges() == {True: 1, False: 0}


def test_a_call_cut_after_an_earlier_answer_finished_still_reads_as_cut(
    tmp_path: Path,
) -> None:
    """Why the rule is "no more starts than stops" and not "at least one of each".

    The Voice may answer before the request lands — it did on run
    `20260902T162146Z`, where the start edge preceded the user transcript.
    Counting only presence would then let an earlier closed span stand in for
    the answer's open one, and a run hung up mid-count would read green.
    """
    walk = _walk(
        tmp_path,
        lines=[
            _said(live_call_step.VOICE_SPEAKING_LINE),
            _said(live_call_step.VOICE_QUIET_LINE),
            _said("user speech, for the voice thread to act on: '请你从一数到两百'"),
            _said(live_call_step.VOICE_SPEAKING_LINE),
        ],
    )
    edges = walk._voice_speech_edges()
    assert edges[True] and edges[False]
    assert edges[False] < edges[True]


def test_an_utterance_that_ended_without_a_delta_is_not_a_span_left_open(
    tmp_path: Path,
) -> None:
    """A `done` with no delta before it makes stops outnumber starts, honestly."""
    walk = _walk(
        tmp_path,
        lines=[
            _said(live_call_step.VOICE_SPEAKING_LINE),
            _said(live_call_step.VOICE_QUIET_LINE),
            _said(live_call_step.VOICE_QUIET_LINE),
        ],
    )
    edges = walk._voice_speech_edges()
    assert edges[False] >= edges[True] >= 1


def test_a_step_reads_only_the_lines_its_own_call_produced(tmp_path: Path) -> None:
    """Two steps dial on one engine, so the log carries the call before this one.

    Counting the whole log would let `live call`'s own answer stand in for the
    long one's, and its ending stand in for the long one's ending — a green
    step resting on evidence from a different call (#184).
    """
    before = [
        _said(live_call_step.VOICE_SPEAKING_LINE),
        _said(live_call_step.VOICE_QUIET_LINE),
        _said("ended the Live Call after 60 seconds without call activity"),
    ]
    walk = _walk(tmp_path, lines=[*before, _said(live_call_step.VOICE_SPEAKING_LINE)])

    assert walk._voice_speech_edges() == {True: 2, False: 1}
    assert walk._voice_speech_edges(since=len(before)) == {True: 1, False: 0}
    assert walk._ceiling_ended_the_call() is True
    assert walk._ceiling_ended_the_call(since=len(before)) is False


class _Talking(_Engine):
    """An engine whose log grows a little on every read, like a call in progress."""

    def __init__(self, lines: list[str], *, per_read: int = 1) -> None:
        super().__init__([])
        self._pending = list(lines)
        self._per_read = per_read
        self._given: list[str] = []

    def log_lines(self) -> list[str]:
        for _ in range(self._per_read):
            if self._pending:
                self._given.append(self._pending.pop(0))
        return list(self._given)


def test_the_watch_measures_first_edge_to_last_across_a_broken_up_answer(
    tmp_path: Path,
) -> None:
    """Run `20260902T170324Z`: two hundred numbers arrived as twenty-three turns.

    Waiting for the first moment the spans balanced called the gap after the
    first turn the end of the answer, and the step then failed a green engine
    for answering "only" fifty-two seconds. The watch measures the whole
    stretch instead, from the first edge to the last one it ever sees.
    """
    turns = []
    for _ in range(3):
        turns += [_said(live_call_step.VOICE_SPEAKING_LINE), _said(live_call_step.VOICE_QUIET_LINE)]
    walk = _walk(tmp_path)
    walk.engine = _Talking(turns)
    walk.bridgectl = lambda *_, **__: None  # never asked: the call is read below
    downs = iter([False] * 6 + [True] * 4)
    walk._call_is_down = lambda: next(downs, True)  # type: ignore[method-assign]

    watch = walk._watch_the_voice(0, deadline_seconds=30.0, poll_seconds=0.01)

    assert watch.went_down is True
    assert watch.edges == {True: 3, False: 3}
    assert watch.first_voice_at is not None and watch.last_voice_at is not None
    # Every turn after the first moved the mark, so the stretch spans them all.
    assert watch.last_voice_at > watch.first_voice_at
    assert watch.down_at >= watch.last_voice_at


def test_a_call_whose_voice_never_speaks_is_watched_without_a_stretch(
    tmp_path: Path,
) -> None:
    """No edge, no answer: the step says so rather than dividing by nothing."""
    walk = _walk(tmp_path, lines=[_said("nothing about the Voice at all")])
    walk._call_is_down = lambda: True  # type: ignore[method-assign]

    watch = walk._watch_the_voice(0, deadline_seconds=5.0, poll_seconds=0.01)

    assert watch.edges == {True: 0, False: 0}
    assert watch.first_voice_at is None
    assert watch.last_voice_at is None


def test_the_watch_can_stop_when_the_answer_closes_rather_than_when_the_call_does(
    tmp_path: Path,
) -> None:
    """#198's stretch runs on a call the walk speaks into again afterwards.

    So it cannot wait for the ending. `quiet_seconds` is the other stopping
    condition: every span closed, and then no new edge for that long.
    """
    walk = _walk(tmp_path)
    walk.engine = _Talking(
        [_said(live_call_step.VOICE_SPEAKING_LINE), _said(live_call_step.VOICE_QUIET_LINE)]
    )
    walk.bridgectl = lambda *_, **__: None  # never asked: the call is read below
    walk._call_is_down = lambda: False  # type: ignore[method-assign]

    watch = walk._watch_the_voice(0, deadline_seconds=30.0, poll_seconds=0.01, quiet_seconds=0.05)

    assert watch.went_down is False
    assert watch.edges == {True: 1, False: 1}
    assert watch.first_voice_at is not None


def test_a_span_still_open_does_not_stop_the_watch_early(tmp_path: Path) -> None:
    """A start with no stop is #169's bug, and the phase has to see the whole stretch."""
    walk = _walk(tmp_path)
    walk.engine = _Talking([_said(live_call_step.VOICE_SPEAKING_LINE)])
    walk.bridgectl = lambda *_, **__: None
    walk._call_is_down = lambda: False  # type: ignore[method-assign]

    watch = walk._watch_the_voice(0, deadline_seconds=0.3, poll_seconds=0.01, quiet_seconds=0.05)

    # It ran to its deadline rather than stopping on the quiet, because the span
    # the Voice opened never closed.
    assert watch.edges == {True: 1, False: 0}
    assert watch.went_down is False


def test_the_long_answer_watch_includes_a_voice_start_before_user_speech_lands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run `20260904T043017Z`: Voice started 0.5s before the request landed.

    Starting the watch at `landed_at` missed that opening edge. The first
    transcript fragment then looked like a closed answer after one quiet
    window, even though Voice did not close its span until almost a minute
    later. The quiet-track gate makes `engine_mark` the clean pre-request
    boundary, so the long-answer watch must use that mark.
    """
    clock = _PacingClock()
    monkeypatch.setattr(live_call_step.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(live_call_step.time, "sleep", clock.sleep)
    walk = _walk(tmp_path)

    class _PreLandingVoice(_Engine):
        def log_lines(self) -> list[str]:
            lines = [
                _said(live_call_step.VOICE_SPEAKING_LINE),
                _said("user speech, for the voice thread to act on: '请你从一数到两百'"),
                _said(live_call_step.VOICE_SAID_LINE % "一二三"),
            ]
            # The transcript has been quiet for longer than the 10s cue window
            # before Voice closes its span at 12s.
            if clock.now >= 12.0:
                lines.append(_said(live_call_step.VOICE_QUIET_LINE))
            return lines

    walk.engine = _PreLandingVoice([])
    ask = live_call_step._SpokenAsk(
        engine_mark=0,
        wrapper_mark=3,
        landed_at=1,
        heard=True,
    )
    watched_from: list[int] = []
    monkeypatch.setattr(walk, "_ask_by_voice", lambda _variant, _facts: ask)
    monkeypatch.setattr(walk, "_call_is_down", lambda: False)
    monkeypatch.setattr(walk, "_call_line", lambda: "call: active")
    real_watch = walk._watch_the_voice

    def watch(mark: int, **kwargs: object) -> live_call_step._VoiceWatch:
        watched_from.append(mark)
        return real_watch(mark, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(walk, "_watch_the_voice", watch)

    detail = walk._the_voice_holds_the_call_open(
        1.0, live_call_step._PhaseFacts("graded", phase="long answer")
    )

    assert watched_from == [ask.engine_mark]
    assert clock.now == pytest.approx(22.0)
    assert "1 start / 1 stop" in detail


def test_a_span_the_voice_had_open_before_the_mark_is_still_the_voice_answering(
    tmp_path: Path,
) -> None:
    """Run `20260904T004431Z`: one span covered the previous answer and this one.

    Both lanes. The span opened before the counting request landed and closed
    only when the answer ended, so no start edge fell inside the window and a
    watch counting edges alone saw nothing happen through minutes of speech. A
    `transcript/done` is the Voice saying something, and `_VoiceWatch` is
    documented as asking exactly that.
    """
    walk = _walk(tmp_path)
    walk.engine = _Talking(
        [_said(live_call_step.VOICE_SAID_LINE % "一二三"), _said("something else")]
    )
    walk.bridgectl = lambda *_, **__: None
    walk._call_is_down = lambda: False  # type: ignore[method-assign]

    watch = walk._watch_the_voice(0, deadline_seconds=30.0, poll_seconds=0.01, quiet_seconds=0.05)

    # No edge at all, and the watch still knows the Voice answered — and still
    # stops on the quiet rather than running to its deadline.
    assert watch.edges == {True: 0, False: 0}
    assert watch.last_voice_at is not None
    assert watch.went_down is False


def test_without_the_quiet_window_the_watch_is_the_one_the_long_step_wrote(
    tmp_path: Path,
) -> None:
    """`quiet_seconds=None` runs until the call goes down, which is #184's watch."""
    walk = _walk(tmp_path)
    walk.engine = _Talking(
        [_said(live_call_step.VOICE_SPEAKING_LINE), _said(live_call_step.VOICE_QUIET_LINE)]
    )
    walk.bridgectl = lambda *_, **__: None
    downs = iter([False] * 8 + [True] * 4)
    walk._call_is_down = lambda: next(downs, True)  # type: ignore[method-assign]

    watch = walk._watch_the_voice(0, deadline_seconds=30.0, poll_seconds=0.01)

    assert watch.went_down is True


class _LongRecital(_Engine):
    """A two-hundred-number recital paced by the fake clock (#243).

    The engine log the long-answer phase reads: the Voice opens its span with
    the request, says the numbers as it goes, and closes the span only when the
    playout has drained — which run `20260905T075128Z` measured at 417s.
    """

    def __init__(self, clock: _PacingClock, *, stopped_at: float | None) -> None:
        super().__init__([])
        self._clock = clock
        self._stopped_at = stopped_at

    def log_lines(self) -> list[str]:
        lines = [
            _said(live_call_step.VOICE_SPEAKING_LINE),
            _said("user speech, for the voice thread to act on: '请你从一数到两百'"),
        ]
        # One transcript fragment a minute, so the watch keeps seeing activity
        # for as long as the recital runs.
        lines += [
            _said(live_call_step.VOICE_SAID_LINE % f"fragment {index}")
            for index in range(1, int(self._clock.now // 60) + 1)
        ]
        if self._stopped_at is not None and self._clock.now >= self._stopped_at:
            lines.append(_said(live_call_step.VOICE_QUIET_LINE))
        return lines


def _long_answer_phase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    stopped_at: float | None,
    down_at: float | None,
) -> tuple[live_call_step._PhaseFacts, _PacingClock]:
    """Run the long-answer phase over a fake engine log on a fake clock (#243)."""
    clock = _PacingClock()
    monkeypatch.setattr(live_call_step.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(live_call_step.time, "sleep", clock.sleep)
    walk = _walk(tmp_path)
    walk.engine = _LongRecital(clock, stopped_at=stopped_at)
    monkeypatch.setattr(
        walk,
        "_ask_by_voice",
        lambda _variant, _facts: live_call_step._SpokenAsk(
            engine_mark=0, wrapper_mark=0, landed_at=0, heard=True
        ),
    )
    monkeypatch.setattr(walk, "_call_is_down", lambda: down_at is not None and clock.now >= down_at)
    monkeypatch.setattr(walk, "_call_line", lambda: "call: active")
    facts = live_call_step._PhaseFacts("graded", phase="long answer")
    try:
        walk._the_voice_holds_the_call_open(60.0, facts)
    except live_call_step._PhaseStopped:
        pass
    return facts, clock


def test_the_long_answer_watch_waits_out_a_recital_that_outruns_the_old_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run `20260905T075128Z`: the playout drained at 417s, the watch gave up at 360s.

    `LIVE_CALL_ANSWER_SECONDS + ceiling` is a general answer allowance plus a
    ceiling, and the same build recited the same request in 287s on the run
    before. The watch keeps watching an open span while the call is up, so the
    stop edge is seen and the answer is graded on what the Voice actually did.
    """
    facts, clock = _long_answer_phase(tmp_path, monkeypatch, stopped_at=417.0, down_at=None)

    assert facts.failed == []
    assert facts.graded["Voice spans closed"] is True
    assert facts.graded["Voice answer outlasted Silence Ceiling"] is True
    assert facts.graded["call stayed up"] is True
    # It waited past the old 360s deadline for the stop edge, and did not sit
    # out the full hard cap once the span had closed.
    assert 417.0 <= clock.now < live_call_step.LIVE_CALL_LONG_ANSWER_SECONDS


def test_a_call_that_goes_down_mid_answer_is_still_graded_as_the_ceiling_bug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The span left open by a call taken away mid-answer is #169, and says so."""
    facts, clock = _long_answer_phase(tmp_path, monkeypatch, stopped_at=None, down_at=70.0)

    assert facts.graded["Voice spans closed"] is False
    assert facts.failed == ["Voice spans closed"]
    assert "#169" in facts.failure
    assert "went down" in facts.failure
    assert clock.now < live_call_step.LIVE_CALL_LONG_ANSWER_SECONDS


def test_a_span_still_open_at_the_hard_cap_is_the_harnesss_patience_not_the_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The watch's own cap is a fact about the watch, and the failure text says which."""
    facts, clock = _long_answer_phase(tmp_path, monkeypatch, stopped_at=None, down_at=None)

    assert facts.graded["Voice spans closed"] is False
    assert facts.recorded["call went down"] is False
    assert facts.recorded["watch ran out"] is True
    assert "the bug itself (#169)" not in facts.failure
    assert "the watch ran out" in facts.failure
    assert f"{live_call_step.LIVE_CALL_LONG_ANSWER_SECONDS:.0f}s" in facts.failure
    assert clock.now >= live_call_step.LIVE_CALL_LONG_ANSWER_SECONDS


def test_the_engines_own_line_is_what_names_the_ceiling_as_the_ender(tmp_path: Path) -> None:
    """The number in it is configuration, so the sentence around it is matched."""
    walk = _walk(tmp_path, lines=[_said("ended the Live Call after 12.5 seconds")])
    assert walk._ceiling_ended_the_call() is False

    ended = _walk(
        tmp_path,
        lines=[_said("ended the Live Call after 12.5 seconds without call activity")],
    )
    assert ended._ceiling_ended_the_call() is True


def test_the_ceiling_is_read_out_of_the_lane_that_is_running(tmp_path: Path) -> None:
    walk = _walk(tmp_path)
    walk.config.path.write_text(A_CONFIG + "\n[policy]\nsilence_end_seconds = 12.5\n")
    assert walk._silence_ceiling_seconds() == 12.5


def test_a_lane_that_sets_no_ceiling_is_running_the_shipped_one(tmp_path: Path) -> None:
    """The engine's default, not a copy of it — the step never types the number."""
    walk = _walk(tmp_path)
    walk.config.path.write_text(A_CONFIG)
    assert walk._silence_ceiling_seconds() == live_call_step.DEFAULT_SILENCE_END_SECONDS


def test_the_silence_the_ceiling_waits_out_is_graded_to_the_polls_granularity() -> None:
    """Both ends of that span are polls, so each is up to one interval late.

    A ceiling that waited its full sixty seconds must not read as an early
    ending because of which poll saw what.
    """
    assert live_call_step.LIVE_CALL_POLL_SECONDS > 0
    assert live_call_step.LIVE_CALL_POLL_SECONDS < live_call_step.DEFAULT_SILENCE_END_SECONDS


def test_the_long_step_waits_out_the_answer_and_the_silence_after_it(
    tmp_path: Path,  # noqa: ARG001
) -> None:
    """Nothing may end the call before the ceiling would have.

    The answer is 220s measured; the ceiling then needs its own stretch of
    silence on top before it fires. A step that gave up sooner would be the
    thing that decided how long the call lasted.
    """
    assert live_call_step.LIVE_CALL_ANSWER_SECONDS > 220.0
    assert live_call_step.LIVE_CALL_ANSWER_SECONDS > live_call_step.LIVE_CALL_END_SECONDS


# --- which utterance goes on the track --------------------------------------


def test_a_wav_directory_nobody_wrote_in_plays_the_request_v0_accepted(
    tmp_path: Path,
) -> None:
    """The default is #183's sentence: a step that does not care gets that one."""
    assert live_call.variants_asked_for(tmp_path, (live_call.PLAIN, live_call.LONG)) == (
        live_call.PLAIN,
    )


def test_the_step_names_the_variant_and_the_harness_reads_it_back(tmp_path: Path) -> None:
    """The per-call channel, both halves. One engine holds both steps' calls."""
    live_call.ask_for(tmp_path / "wav", live_call.LONG)
    known = (live_call.PLAIN, live_call.LONG)
    assert live_call.variants_asked_for(tmp_path / "wav", known) == (live_call.LONG,)

    live_call.ask_for(tmp_path / "wav", live_call.PLAIN)
    assert live_call.variants_asked_for(tmp_path / "wav", known) == (live_call.PLAIN,)


def test_a_step_can_queue_a_second_utterance_behind_the_first(tmp_path: Path) -> None:
    """v2 speaks a relay into a call that is already up, and then waits on it (#196)."""
    live_call.ask_for(tmp_path / "wav", live_call.NEEDS)
    live_call.ask_next(tmp_path / "wav", live_call.RELAY)

    known = (live_call.NEEDS, live_call.RELAY)
    assert live_call.variants_asked_for(tmp_path / "wav", known) == (
        live_call.NEEDS,
        live_call.RELAY,
    )


def test_a_call_can_be_asked_to_open_in_silence(tmp_path: Path) -> None:
    """An empty list is a different answer from no list at all (#196)."""
    live_call.ask_for_nothing(tmp_path / "wav")

    assert live_call.variants_asked_for(tmp_path / "wav", (live_call.PLAIN,)) == ()


def test_the_step_that_dials_replaces_whatever_the_last_call_left(tmp_path: Path) -> None:
    """ "Whatever the step before had queued" is not a request anybody made."""
    live_call.ask_for(tmp_path / "wav", live_call.NEEDS)
    live_call.ask_next(tmp_path / "wav", live_call.RELAY)
    live_call.ask_for(tmp_path / "wav", live_call.PLAIN)

    known = (live_call.PLAIN, live_call.NEEDS, live_call.RELAY)
    assert live_call.variants_asked_for(tmp_path / "wav", known) == (live_call.PLAIN,)


def test_a_variant_the_harness_cannot_play_is_the_default_rather_than_a_crash(
    tmp_path: Path,
) -> None:
    """A call that comes up mute proves nothing; one that speaks #183's words says so."""
    (tmp_path / "wav").mkdir()
    (tmp_path / "wav" / live_call.NEXT_VARIANT_FILE).write_text("whistling\n")
    assert live_call.variants_asked_for(tmp_path / "wav", (live_call.PLAIN,)) == (live_call.PLAIN,)


def test_every_variant_the_settings_carry_is_one_the_step_can_ask_for() -> None:
    """The two halves of the mapping are the same two names, or one is unreachable."""
    settings = live_call.HarnessSettings(
        observations=Path("/tmp/o.jsonl"), wav_directory=Path("/tmp/wav")
    )
    assert set(settings.requests) == {
        live_call.PLAIN,
        live_call.LONG,
        live_call.NEEDS,
        live_call.RELAY,
        live_call.DETAIL,
        live_call.HISTORY,
        live_call.EARLIER,
        live_call.NARROWING,
    }
    assert settings.requests[live_call.PLAIN] == live_call.REQUEST
    assert settings.requests[live_call.LONG] == live_call.LONG_REQUEST


def test_the_relay_utterance_names_the_workspace_the_step_creates() -> None:
    """One value, said out loud and made on disk — or the Voice names nothing (#196).

    The project half of a Session Name is the workspace directory's basename
    (`adapters/agent/_project.py`), which is the only half the harness picks. So
    the sentence and the directory are built from the same string, and this is
    what stops one of them being renamed alone.
    """
    for lane in journey.LANES:
        focus = lane.call_workspaces.focus
        assert focus in live_call.relay_request(focus)
        assert live_call_step.LIVE_CALL_RELAY_HEARD_SUBSTRING in live_call.relay_request(focus)


def test_every_utterance_that_asks_about_a_session_names_it(tmp_path: Path) -> None:  # noqa: ARG001
    """Detail and History ask about one Session, and the Call Agent picks the target.

    Run `20260903T081717Z` is the precedent the relay utterance already carries:
    an utterance that named no Session had the Call Agent go looking, and on one
    lane it briefed two of the nine Sessions this machine was running. Detail,
    History and its older page ask about the same Session, so each says which.
    """
    for lane in journey.LANES:
        focus = lane.call_workspaces.focus
        for utterance in (
            live_call.detail_request(focus),
            live_call.history_request(focus),
            live_call.earlier_request(focus),
        ):
            assert focus in utterance


def test_the_answer_utterance_carries_the_words_the_session_is_told() -> None:
    """Phase 3 is an *answer* to the question the Session stopped on (#198).

    The Session asked `journey.THE_QUESTION_ASKED`, so the
    payload is what the user says back — and what the Session's next turn then
    carries, which is what the step reads. Deliberately not `收到`: that is the
    wording the Voice says for a **queued** receipt (`instructions/voice.py`),
    and a payload spelling it would make the receipt and its echo the same
    string.
    """
    said = live_call.relay_request(live_call.FOCUS_WORKSPACE_NAME)

    assert live_call_step.LIVE_CALL_ANSWER_SUBSTRING in said
    assert live_call_step.RELAY_RECEIPT_QUEUED not in said


def test_the_question_a_session_stops_on_is_one_the_voice_reads_out() -> None:
    """The Voice speaks the user's language, so an English line it quotes it renders.

    Run `20260903T235107Z`'s claude lane answered `它最近问:「要继续吗?」` of a
    Session told to say `Should I continue?` — a faithful translation, graded as
    the Voice failing to say what the Session last said. The dictated question is
    Chinese for `live_call.DICTATED_REPLY`'s reason, and what is graded is its
    words without the punctuation the Voice has been seen to change.
    """
    question = journey.THE_QUESTION_ASKED

    assert question in journey.ASK_A_QUESTION.words
    assert journey.QUESTION_ASKED_SPOKEN_SUBSTRING in question
    # Either width satisfies `core/briefing.py::_ASKS`; the full-width mark is
    # the one that rule's own comment is surest of (#176 §1.2).
    assert question.endswith("？")
    # Not a run of the answer to it, or the two could pass for each other.
    assert journey.QUESTION_ASKED_SPOKEN_SUBSTRING not in live_call.DICTATED_REPLY
    assert live_call_step.LIVE_CALL_DICTATED_REPLY_SUBSTRING not in question
    # No punctuation in either graded fragment, for the same reason.
    for fragment in (
        journey.QUESTION_ASKED_SPOKEN_SUBSTRING,
        live_call_step.LIVE_CALL_DICTATED_REPLY_SUBSTRING,
    ):
        assert not any(mark in fragment for mark in "?？。，,「」“”:：")


def test_the_claude_lane_holds_its_question_and_the_codex_lane_says_it() -> None:
    """#238: the route the Detail answer rides is the lane's, and only the route.

    A plain-text question ends the turn as text, so the Answer Relay rides the
    inbox and Claude Code announces it to the Session as a peer's words (ADR
    0013, 2026-09-05 amendment) — the same build read that wrapper both ways on
    two runs, which is the red this ticket closes. Asked through
    `AskUserQuestion` the question is *held*, the Relay takes the ADR 0015 hook
    route, and what the phase grades stops resting on a model's reading.

    The Codex lane keeps the plain-text question because the product has no held
    question there at all: its adapter projects `PERMISSION` and `UNKNOWN` and
    never `QUESTION`, the same fact `Lane.question` records for #128.
    """
    assert "AskUserQuestion" in journey.CLAUDE.call_asking.words
    assert journey.CLAUDE.call_asking is journey.ASK_A_QUESTION_THROUGH_THE_TOOL
    assert "AskUserQuestion" not in journey.CODEX.call_asking.words
    assert journey.CODEX.call_asking is journey.ASK_A_QUESTION_THEN_SAY
    # Two to four is what the tool takes, and both labels are offered by name so
    # the walk knows every word it put in front of the Voice.
    assert len(journey.CALL_QUESTION_OPTIONS) == 2
    for label in journey.CALL_QUESTION_OPTIONS:
        assert label in journey.CLAUDE.call_asking.words


def test_the_offered_labels_share_no_run_with_anything_graded() -> None:
    """An option label rides the brief's own `option:` lines into the hand-over.

    So a label spelling a graded fragment is a fragment the Voice can say while
    reading the question out, passing a check about a different field. The
    labels are also not the spoken answer: what arrives through the held hook is
    the user's own words, which match no label — the Answer Relay carries words
    rather than a choice (ADR 0015).
    """
    graded = (
        journey.QUESTION_ASKED_SPOKEN_SUBSTRING,
        live_call_step.LIVE_CALL_DICTATED_REPLY_SUBSTRING,
        live_call_step.LIVE_CALL_ANSWER_SUBSTRING,
        live_call_step.RELAY_RECEIPT_QUEUED,
        live_call_step.RELAY_RECEIPT_DELIVERED,
    )
    for label in journey.CALL_QUESTION_OPTIONS:
        for fragment in graded:
            assert fragment not in label
            assert label not in fragment
        for lane in journey.LANES:
            for workspace in lane.call_workspaces:
                assert workspace not in label


def test_the_focus_session_is_told_what_to_answer_a_relay_with() -> None:
    """#198 §3a grades the Voice's Detail answer against the Session's `newest`.

    The Voice speaks Chinese and `newest` is whatever language that agent chose,
    so a free-form reply makes the criterion a test of which lane answered in
    which language: run `20260903T231626Z` passed on Codex and failed the Claude
    lane for translating its English `newest` faithfully. So the reply is
    dictated — through the Session's own driving instruction, because run
    `20260903T233723Z` put it in the spoken payload and both lanes' Call Agents
    relayed `可以继续` alone. These are the exclusions that keep it gradeable.
    """
    reply = live_call.DICTATED_REPLY

    # **Every lane's drive**, because the route the question is held on is the
    # lane's since #238 and the dictation is not: whichever way the Focus
    # Session is stopped, the line Detail grades is one the walk wrote.
    for lane in journey.LANES:
        assert reply in lane.call_asking.words
        assert journey.THE_QUESTION_ASKED in lane.call_asking.words
    # Not the plain turn: phases 4 and 5 re-drive with it and their turn has to
    # end on the question, not on an answer to the drive itself.
    assert reply not in journey.ASK_A_QUESTION.words
    # And not in the payload, which stays the one clause the step follows through
    # the air, the Call Agent's argv and the Session's next turn.
    assert reply not in live_call.relay_request(live_call.FOCUS_WORKSPACE_NAME)
    assert live_call_step.LIVE_CALL_DICTATED_REPLY_SUBSTRING in reply
    # Neither receipt wording: an echo of the receipt would otherwise pass.
    assert live_call_step.RELAY_RECEIPT_QUEUED not in reply
    assert live_call_step.RELAY_RECEIPT_DELIVERED not in reply
    # Nor any lane's workspace name, which the answer is graded on elsewhere.
    for lane in journey.LANES:
        for workspace in lane.call_workspaces:
            assert workspace not in reply
    # And no run shared with the relayed payload, so an answer that read the
    # words back rather than the Session's reply does not pass.
    assert (
        live_call_step.LIVE_CALL_DICTATED_REPLY_SUBSTRING
        not in live_call_step.LIVE_CALL_ANSWER_SUBSTRING
    )
    assert " " not in live_call_step.LIVE_CALL_DICTATED_REPLY_SUBSTRING


def test_no_two_lanes_answer_to_the_same_spoken_name() -> None:
    """The Claude lane's engine holds the Codex lane's Sessions (`20260903T093813Z`).

    The Codex daemon is machine-wide, so one engine's roster carries the other
    lane's rows. A name shared between lanes is two rows the relay utterance
    cannot tell apart, which is a Call Agent that goes looking and never relays.
    """
    spoken = [name for lane in journey.LANES for name in lane.call_workspaces]

    assert len(set(spoken)) == len(spoken), spoken


def test_every_lane_names_three_extra_sessions(tmp_path: Path) -> None:  # noqa: ARG001
    """#198's walk drives three Sessions besides the lane's own.

    The one the call is dialled about and relayed into, the one that rings while
    it is up, and the one that stops **inside the Cool-down** after the hang-up —
    which the paid dial then has to be about. Three names, so no phase is graded
    against a Session another phase already moved.
    """
    for lane in journey.LANES:
        named = tuple(lane.call_workspaces)
        assert len(named) == 3
        assert len(set(named)) == 3


def test_the_grade_wording_the_voice_is_told_to_say_is_what_the_step_looks_for() -> None:
    """The receipt wording is the product's, and the step quotes it (#193 §Voice).

    `已转达` for a delivered relay and `收到` for a queued one are shipped in the
    Voice's own instructions; a copy in the harness that nothing checks would
    pass a run where the product had stopped saying either.
    """
    spoken = voice_instructions(
        InstructionContext(
            cli=ControlPlaneCli(
                command=Path("/Applications/GPT-VoiceCoding.app/Contents/MacOS/bridgectl"),
                version="1.4.2",
                socket_path=Path("/tmp/gpt-voicecoding-501/control.sock"),
            )
        )
    ).text

    assert live_call_step.RELAY_RECEIPT_DELIVERED in spoken
    assert live_call_step.RELAY_RECEIPT_QUEUED in spoken


def test_the_settings_build_the_utterance_from_the_workspace_they_carry() -> None:
    """Configured once, per lane, and the sentence follows it (#196)."""
    settings = live_call.HarnessSettings(
        observations=Path("/tmp/o.jsonl"),
        wav_directory=Path("/tmp/wav"),
        focus_workspace="五号工位",
        ringing_workspace="六号工位",
    )

    assert settings.requests[live_call.RELAY] == live_call.relay_request("五号工位")


def test_the_relay_utterance_asks_for_no_hang_up() -> None:
    """This call has to outlive the relay by a whole turn (`LONG_REQUEST`'s reason)."""
    assert live_call_step.LIVE_CALL_HEARD_SUBSTRING not in live_call.RELAY_REQUEST


def test_the_hand_over_question_is_asked_the_way_the_voice_is_told_to_answer_it() -> None:
    """Counts first, names when narrowed — the Voice's own rule, so phase 2 asks twice.

    Run `20260903T222129Z` asked only the general question and graded the missing
    Session name: the Voice had answered `有六个已经结束…`, which is
    `core/instructions/voice.py`'s counted Roster Brief, exactly as shipped.
    """
    spoken = voice_instructions(
        InstructionContext(
            cli=ControlPlaneCli(
                command=Path("/Applications/GPT-VoiceCoding.app/Contents/MacOS/bridgectl"),
                version="1.4.2",
                socket_path=Path("/tmp/gpt-voicecoding-501/control.sock"),
            )
        )
    ).text

    assert "give the counts rather than the list" in spoken
    assert "narrow it" in spoken
    # The narrowing utterance says the name; the general one must not.
    assert live_call.FOCUS_WORKSPACE_NAME in live_call.narrowing_request(
        live_call.FOCUS_WORKSPACE_NAME
    )
    assert live_call.FOCUS_WORKSPACE_NAME not in live_call.NEEDS_REQUEST


def test_a_counted_roster_brief_is_recognised_by_a_numeral_and_its_measure_word() -> None:
    """What the Voice actually said on `20260903T222129Z`, and what a list is not."""
    counted = "有六个已经结束,还有一个停在无法读取的地方。你想看哪一个?"
    listed = "二号工位在等你,三号工位还在跑。"

    assert re.search(live_call_step.ROSTER_COUNT_PATTERN, counted)
    assert not re.search(live_call_step.ROSTER_COUNT_PATTERN, listed)


def test_the_folded_walk_is_the_tenth_step_and_the_only_one_that_dials() -> None:
    """#198's fold, read off the contract every build ticket's "Red first" line cites.

    Ten names, `live call` last, and it is the whole of `LIVE_CALL_STEPS`: a run
    that walks it gets the harness's own Call adapter and the `bridgectl`
    wrapper, and a run that does not keeps the Call adapter the user configured
    (`conftest.py`, #183).
    """
    assert len(journey.STEPS) == 10
    assert journey.STEPS[-1] == "live call"
    assert set(journey.LIVE_CALL_STEPS) <= set(journey.STEPS)


def test_every_step_the_walk_can_be_asked_for_has_a_prerequisite_row() -> None:
    """A name with no row is a name `select` cannot close over (#182)."""
    assert set(journey.PREREQUISITES) == set(journey.STEPS)


def test_the_folded_step_still_stands_on_the_roster(tmp_path: Path) -> None:  # noqa: ARG001
    """Phase 1 grades a dial for carrying the Roster Brief (#198).

    The three Sessions the walk is graded against are its own to start, so no
    address is owed — but a roster the lane has never proved it can read is not
    something to grade a hand-over against, and `roster` is one step rather than
    a walk, so #183's "runnable alone" clause is satisfied by it.
    """
    chosen = journey.select(["live call"])

    assert chosen.selected == ("live call",)
    assert chosen.setup == ("roster",)


def test_the_walk_asks_for_every_variant_it_speaks_and_no_others() -> None:
    """Seven sentences go on one call's track, and each is named once (#198).

    The playlist is a per-call file the transport reads while the call is up
    (`live_call.NEXT_VARIANT_FILE`, #196), so a variant the step appends and the
    settings cannot build is a call that falls silent at that phase.
    """
    spoken = (
        live_call.NEEDS,
        live_call.NARROWING,
        live_call.RELAY,
        live_call.DETAIL,
        live_call.HISTORY,
        live_call.EARLIER,
        live_call.LONG,
        live_call.PLAIN,
    )
    settings = live_call.HarnessSettings(observations=Path("/o.jsonl"), wav_directory=Path("/w"))

    assert len(set(spoken)) == len(spoken)
    for variant in spoken:
        assert settings.requests[variant]


def test_the_hand_over_kinds_the_step_grades_are_the_products_own_class_names() -> None:
    """The adapter writes `type(item).__name__`; a copy here could drift silently."""
    assert live_call_step.ROSTER_BRIEF_KIND == SpokenRosterBrief.__name__
    assert live_call_step.SESSION_BRIEF_KIND == SpokenBrief.__name__
    assert live_call_step.ROSTER_BRIEF_KIND != live_call_step.SESSION_BRIEF_KIND


def test_the_paging_option_the_step_greps_for_is_the_one_the_surface_takes() -> None:
    """`--before` is graded on the Call Agent's argv, so it has to be the real flag (#171)."""
    assert live_call_step.HISTORY_CURSOR_OPTION == "--before"
    rendered = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "gpt_voicecoding.control_plane", "history", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    if rendered.returncode == 0:
        assert live_call_step.HISTORY_CURSOR_OPTION in rendered.stdout


def test_the_three_workspace_roles_are_named_rather_than_ordered() -> None:
    """A third role arrived and four readers of a 3-tuple had to change in step (#198).

    The roles are what those readers mean, so they are what is carried:
    `workspaces.focus` cannot be misread as `workspaces.ringing` the way `[0]`
    can be misread as `[1]`.
    """
    named = live_call.CallWorkspaces(focus="a", ringing="b", waiting="c")

    assert (named.focus, named.ringing, named.waiting) == ("a", "b", "c")
    assert tuple(named) == ("a", "b", "c")


def test_a_lane_cannot_name_two_of_its_sessions_the_same() -> None:
    """Two roles sharing a name is a phase graded against a Session another moved."""
    with pytest.raises(ValueError, match="three names"):
        live_call.CallWorkspaces(focus="二号工位", ringing="二号工位", waiting="四号工位")
